"""Reconstruct closed round-trips from a raw fill stream.

Exchanges hand you fills. Behaviour lives in *trades* -- the whole arc from
flat, through however many adds and partial exits, back to flat. "You added to a
loser three times" is a statement about one arc, and it is unrecoverable from
fills unless you rebuild the arc first. Every detector in Gnosis reads
`RoundTrip`, never `Fill`, for exactly this reason.

Definition used here: **a round-trip runs flat to flat.** Adds and partial
closes belong to the trip they occur in. A position that flips (a sell large
enough to cross through zero) closes one trip and opens another in the opposite
direction, because that is what it is -- two decisions, not one.

Matching is FIFO. Binance reports realised PnL on futures using its own
averaging, so our per-lot number will not always tie out to the exchange's to
the cent. That is a deliberate trade: FIFO preserves *which* entry a given exit
closed, and the disposition detectors need that lineage. Where absolute PnL
accuracy matters more than lineage -- the counterfactuals -- we reconcile
against reported income instead.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from .events import Fill, Side, Venue


# A position this close to flat is flat. Real spot books never reach exactly
# zero: Binance often charges the fee in the *base* asset, so buying 100 units
# and paying 0.1 units in fees leaves 99.9 to sell, and "sell everything" leaves
# a residue every single time. Measured on a real spot export, roughly three
# quarters of the apparently-open positions were residues of well under one
# percent of the quantity bought -- so most of that trader's positions never
# closed and the history could not be profiled at all.
#
# Expressed as a fraction of what the trip opened, so it is scale-free and needs
# no price. Futures books close exactly and are unaffected.
DUST_FRACTION = 0.01

# Quantity below which a residue is float noise rather than a position.
#
# An absolute epsilon cannot work across assets whose quantities differ by
# fifteen orders of magnitude: 1e-12 is meaningful for BTC and is *below the
# representable precision* of a 29,000-unit DOGE position, where float64 leaves
# residues around 3e-12 after FIFO subtraction. Those residues used to open
# trips of their own -- one entry, one exit, a real fee, and a notional of
# 6e-13, which produced a return of -200 trillion percent and destroyed every
# percentage-based statistic downstream.
#
# Scaling with the position removes the whole class of bug.
QTY_EPS_REL = 1e-9
QTY_EPS_ABS = 1e-12


def _negligible(qty: float, scale: float) -> bool:
    """Is `qty` indistinguishable from zero at this position's magnitude?"""
    return qty <= max(QTY_EPS_ABS, abs(scale) * QTY_EPS_REL)


@dataclass
class _Lot:
    """One still-open parcel of a position, held FIFO."""

    qty: float
    price: float
    fill_id: str
    ts: datetime


@dataclass
class RoundTrip:
    """One complete flat-to-flat arc.

    Carries its own receipts: `entry_fill_ids` and `exit_fill_ids` let any
    number in the final report be traced back to the executions that produced
    it. A finding a user cannot audit is a finding they are entitled to
    disbelieve.
    """

    symbol: str
    venue: Venue
    direction: Side
    opened_at: datetime
    closed_at: datetime | None  # None while still open

    qty_opened: float = 0.0
    avg_entry: float = 0.0
    avg_exit: float = 0.0
    gross_pnl: float = 0.0
    fees: float = 0.0

    max_leverage: float | None = None

    # Behavioural counters, accumulated as the arc is walked. Computing these
    # here rather than in a detector keeps the expensive part (walking fills in
    # order, knowing the running average entry) done exactly once.
    n_adds: int = 0
    n_adds_underwater: int = 0
    n_partial_exits: int = 0
    # Quantity abandoned as dust when the trip closed. Recorded rather than
    # dropped silently: a trip that "closed" while leaving something behind
    # should be able to say so.
    dust_qty: float = 0.0
    # Worst price touched by any fill in this trip, relative to entry. A proxy
    # for maximum adverse excursion -- honest but coarse, since we only observe
    # prices where the trader acted. Named to make that limit obvious.
    worst_observed_price: float | None = None

    entry_fill_ids: list[str] = field(default_factory=list)
    exit_fill_ids: list[str] = field(default_factory=list)

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def is_winner(self) -> bool:
        return self.net_pnl > 0

    @property
    def hold_seconds(self) -> float:
        if self.closed_at is None:
            return 0.0
        return (self.closed_at - self.opened_at).total_seconds()

    @property
    def hold_hours(self) -> float:
        return self.hold_seconds / 3600.0

    @property
    def notional(self) -> float:
        return self.qty_opened * self.avg_entry

    @property
    def return_pct(self) -> float:
        """Net PnL as a percentage of notional at risk. 0.0 if no notional.

        This, not absolute PnL, is what makes trades comparable across a book
        that grew -- a $50 win on $500 and a $50 win on $50,000 are not the
        same decision.
        """
        n = self.notional
        # Guarded independently of the epsilon above: even with residue trips
        # eliminated, a legitimately tiny position should not be allowed to
        # contribute a five-figure percentage to an average.
        if n <= 1e-9:
            return 0.0
        return self.net_pnl / n * 100.0

    @property
    def hour_utc(self) -> int:
        return self.opened_at.hour

    @property
    def adverse_excursion_pct(self) -> float:
        """How far the trip went against entry, at the worst observed price."""
        if self.worst_observed_price is None or not self.avg_entry:
            return 0.0
        # Signed so that adverse is always negative, whichever way we were
        # positioned: a long's worst price sits below entry (drop already
        # negative), a short's sits above it (drop positive, so invert).
        drop = (self.worst_observed_price - self.avg_entry) / self.avg_entry * 100.0
        return drop if self.direction is Side.BUY else -drop


class _PositionBuilder:
    """Walks one (symbol, venue) fill stream, emitting trips as they close."""

    def __init__(self, symbol: str, venue: Venue) -> None:
        self.symbol = symbol
        self.venue = venue
        self.lots: deque[_Lot] = deque()
        self.trip: RoundTrip | None = None
        # Quantity closed so far *within the current trip*, needed to keep a
        # running weighted-average exit across multiple partial closes.
        self._closed_qty = 0.0

    @property
    def open_qty(self) -> float:
        return sum(lot.qty for lot in self.lots)

    @property
    def avg_entry(self) -> float:
        q = self.open_qty
        if not q:
            return 0.0
        return sum(lot.qty * lot.price for lot in self.lots) / q

    def _start(self, fill: Fill) -> None:
        self.trip = RoundTrip(
            symbol=self.symbol,
            venue=self.venue,
            direction=fill.side,
            opened_at=fill.ts,
            closed_at=None,
        )

    def _is_underwater(self, price: float) -> bool:
        """Is `price` worse than our average entry, for the direction we hold?"""
        avg = self.avg_entry
        if not avg or self.trip is None:
            return False
        return price < avg if self.trip.direction is Side.BUY else price > avg

    def _note_price(self, price: float) -> None:
        """Track the worst price this trip has observed."""
        t = self.trip
        if t is None:
            return
        if t.worst_observed_price is None:
            t.worst_observed_price = price
        elif t.direction is Side.BUY:
            t.worst_observed_price = min(t.worst_observed_price, price)
        else:
            t.worst_observed_price = max(t.worst_observed_price, price)

    def add(self, fill: Fill) -> list[RoundTrip]:
        """Apply one fill. Returns any trips that closed as a result.

        Returns a list because a single fill can close at most one trip, but a
        flip closes one and opens another -- and callers should not have to
        care which case they are in.
        """
        closed: list[RoundTrip] = []

        if self.trip is None:
            self._start(fill)

        assert self.trip is not None
        self.trip.fees += fill.fee
        if fill.leverage is not None:
            self.trip.max_leverage = max(self.trip.max_leverage or 0.0, fill.leverage)

        if fill.side is self.trip.direction:
            self._open(fill)
        else:
            remainder = self._reduce(fill)
            # Sweep a residue too small to be a position. Done here rather than
            # in `_reduce` so that the flip path below still sees an empty book.
            #
            # The threshold is a fraction of *what this fill just sold*, not of
            # the quantity ever opened. Cumulative was badly wrong: after fifty
            # adds of 1 BTC, one percent of `qty_opened` is 0.5 BTC, so a
            # deliberate scale-out to 0.4 BTC was swept away as dust -- $40,000
            # abandoned, a $20,000 realised gain deleted, and a phantom short
            # left in the open book. Measured on random futures streams, 1.7%
            # violated PnL conservation because of it.
            #
            # Relative to the closing fill, the rule says what it means: you
            # tried to close the position and a sliver survived. Restricted to
            # unleveraged venues, because contract books close exactly -- the
            # comment above used to claim futures were unaffected and nothing
            # enforced it.
            if self.lots and not fill.venue.is_leveraged:
                left = self.open_qty
                if left <= fill.qty * DUST_FRACTION:
                    self.trip.dust_qty = left
                    self.lots.clear()
            if not self.lots:
                closed.append(self._close(fill.ts))
                # A fill big enough to cross zero opens a fresh trip on the
                # other side with whatever is left over. The leftover carries
                # the same fill id -- one execution legitimately appears in two
                # trips, and the receipts should say so rather than invent an id.
                if not _negligible(remainder, fill.qty):
                    leftover = Fill(
                        fill_id=fill.fill_id,
                        ts=fill.ts,
                        symbol=fill.symbol,
                        side=fill.side,
                        qty=remainder,
                        price=fill.price,
                        venue=fill.venue,
                        leverage=fill.leverage,
                    )
                    self._start(leftover)
                    assert self.trip is not None
                    if leftover.leverage is not None:
                        self.trip.max_leverage = leftover.leverage
                    self._open(leftover)
        return closed

    def _open(self, fill: Fill) -> None:
        """Open or add to the position."""
        assert self.trip is not None
        # Underwater must be judged against the average entry *before* this
        # fill moves it, or an add can never look underwater.
        if self.lots:
            self.trip.n_adds += 1
            if self._is_underwater(fill.price):
                self.trip.n_adds_underwater += 1
        self._note_price(fill.price)
        self.lots.append(_Lot(fill.qty, fill.price, fill.fill_id, fill.ts))
        self.trip.entry_fill_ids.append(fill.fill_id)
        # Running weighted average across everything opened in this trip.
        prev_qty = self.trip.qty_opened
        self.trip.avg_entry = (
            (self.trip.avg_entry * prev_qty + fill.price * fill.qty) / (prev_qty + fill.qty)
            if prev_qty + fill.qty
            else 0.0
        )
        self.trip.qty_opened += fill.qty

    def _reduce(self, fill: Fill) -> float:
        """Close against open lots, FIFO. Returns unmatched quantity."""
        assert self.trip is not None
        self._note_price(fill.price)
        self.trip.exit_fill_ids.append(fill.fill_id)

        remaining = fill.qty
        closed_qty = 0.0
        while not _negligible(remaining, fill.qty) and self.lots:
            lot = self.lots[0]
            take = min(lot.qty, remaining)
            # Long profits when price rises; short profits when it falls.
            if self.trip.direction is Side.BUY:
                self.trip.gross_pnl += (fill.price - lot.price) * take
            else:
                self.trip.gross_pnl += (lot.price - fill.price) * take
            lot.qty -= take
            remaining -= take
            closed_qty += take
            if _negligible(lot.qty, take):
                self.lots.popleft()

        if closed_qty:
            prior = self.trip.avg_exit * self._closed_qty
            self._closed_qty += closed_qty
            self.trip.avg_exit = (prior + fill.price * closed_qty) / self._closed_qty

        # A reduction that leaves the position open is a partial exit -- scaling
        # out. One that empties it is simply the close, and is not counted here.
        if self.lots and _negligible(remaining, fill.qty):
            self.trip.n_partial_exits += 1
        return remaining

    def _close(self, ts: datetime) -> RoundTrip:
        assert self.trip is not None
        trip = self.trip
        trip.closed_at = ts
        self.trip = None
        self._closed_qty = 0.0
        return trip

    def pending(self) -> RoundTrip | None:
        """The still-open trip, if any. Callers must not treat it as closed."""
        return self.trip


def reconstruct_realised(fills: list[Fill]) -> tuple[list[RoundTrip], list[RoundTrip]]:
    """Rebuild trades as *realised sales*, FIFO-matched. The spot model.

    Flat-to-flat is the correct unit for a leveraged book, where a position is
    opened and closed and the arc between is one decision. It is the wrong unit
    for spot, because a spot investor never goes flat -- they accumulate an
    asset over years and sell parts of it. Measured on a real spot export
    spanning years: fewer than twenty flat-to-flat trips, which is below the
    threshold at which Gnosis will say anything at all. The same history
    contains hundreds of sales,
    every one of them a decision with an entry, an exit, a holding period and a
    realised result.

    So in this mode each *closing* fill is one trade, matched FIFO against the
    lots it consumed -- the same convention tax authorities use, and for the
    same reason: it is the only way to assign a holding period to part of a
    position.

    Both directions are tracked. An earlier version kept only long inventory and
    dropped any sell it could not match, which did not skip shorts as its
    docstring claimed -- it re-paired a short's *cover* against the *next*
    short's *open*, inverting the sign of the result and inflating hold times by
    a factor of fifty. Three profitable shorts were reported as a loss.

    The output is `RoundTrip`, deliberately. Every detector then works unchanged
    on either model, and neither has to know which one produced its input.
    """
    longs: dict[tuple[str, Venue], deque[_Lot]] = {}
    shorts: dict[tuple[str, Venue], deque[_Lot]] = {}
    realised: list[RoundTrip] = []

    def close_against(
        book: deque[_Lot], fill: Fill, direction: Side
    ) -> tuple[float, RoundTrip | None]:
        """Consume `book` FIFO with `fill`. Returns (unmatched qty, trip)."""
        remaining = fill.qty
        matched: list[tuple[_Lot, float]] = []
        while not _negligible(remaining, fill.qty) and book:
            lot = book[0]
            take = min(lot.qty, remaining)
            matched.append((_Lot(take, lot.price, lot.fill_id, lot.ts), take))
            lot.qty -= take
            remaining -= take
            if _negligible(lot.qty, take):
                book.popleft()
        if not matched:
            return remaining, None

        qty = sum(q for _, q in matched)
        avg_entry = sum(lot.price * q for lot, q in matched) / qty
        sign = 1 if direction is Side.BUY else -1
        gross = (fill.price - avg_entry) * qty * sign
        first_price = matched[0][0].price
        # "Underwater" means worse than entry *for the direction held*.
        underwater = sum(
            1 for lot, _ in matched[1:]
            if (lot.price < first_price if direction is Side.BUY else lot.price > first_price)
        )
        prices = [lot.price for lot, _ in matched] + [fill.price]
        return remaining, RoundTrip(
            symbol=fill.symbol, venue=fill.venue, direction=direction,
            opened_at=min(lot.ts for lot, _ in matched), closed_at=fill.ts,
            qty_opened=qty, avg_entry=avg_entry, avg_exit=fill.price,
            gross_pnl=gross, fees=fill.fee, max_leverage=fill.leverage,
            n_adds=max(0, len(matched) - 1), n_adds_underwater=underwater,
            worst_observed_price=(min(prices) if direction is Side.BUY else max(prices)),
            entry_fill_ids=[lot.fill_id for lot, _ in matched],
            exit_fill_ids=[fill.fill_id],
        )

    for fill in sorted(fills, key=lambda f: (f.ts, f.fill_id)):
        key = (fill.symbol, fill.venue)
        long_book = longs.setdefault(key, deque())
        short_book = shorts.setdefault(key, deque())

        if fill.side is Side.BUY:
            # A buy first covers any short, then accumulates.
            remaining, trip = close_against(short_book, fill, Side.SELL)
            if trip is not None:
                realised.append(trip)
            if not _negligible(remaining, fill.qty):
                long_book.append(_Lot(remaining, fill.price, fill.fill_id, fill.ts))
        else:
            # A sell first realises against inventory, then opens a short.
            remaining, trip = close_against(long_book, fill, Side.BUY)
            if trip is not None:
                realised.append(trip)
            if not _negligible(remaining, fill.qty):
                short_book.append(_Lot(remaining, fill.price, fill.fill_id, fill.ts))

    still_held: list[RoundTrip] = []
    for books, direction in ((longs, Side.BUY), (shorts, Side.SELL)):
        for (sym, ven), book in books.items():
            # `book` may have been drained by popleft; indexing it then raises.
            # This crashed on any book that ever sold out cleanly, which is to
            # say on the simplest possible history: one buy and one sell.
            if not book:
                continue
            total = sum(lot.qty for lot in book)
            if _negligible(total, book[0].qty):
                continue
            still_held.append(RoundTrip(
                symbol=sym, venue=ven, direction=direction,
                opened_at=book[0].ts, closed_at=None, qty_opened=total,
                avg_entry=sum(lot.qty * lot.price for lot in book) / total,
                entry_fill_ids=[lot.fill_id for lot in book],
            ))
    return realised, still_held


def reconstruct(fills: list[Fill]) -> tuple[list[RoundTrip], list[RoundTrip]]:
    """Rebuild round-trips from a fill stream.

    Returns `(closed, open)`. Positions still open at the end of the history are
    returned separately and deliberately excluded from behavioural statistics:
    an unrealised loss is not yet a decision to hold a loser, and counting it as
    one would let a single open bag rewrite a trader's whole profile.
    """
    builders: dict[tuple[str, Venue], _PositionBuilder] = {}
    closed: list[RoundTrip] = []

    for fill in sorted(fills, key=lambda f: (f.ts, f.fill_id)):
        key = (fill.symbol, fill.venue)
        if key not in builders:
            builders[key] = _PositionBuilder(fill.symbol, fill.venue)
        closed.extend(builders[key].add(fill))

    still_open = [b.pending() for b in builders.values()]
    return closed, [t for t in still_open if t is not None]
