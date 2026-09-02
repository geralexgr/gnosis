"""Stop migration: moving the stop instead of taking the loss.

The single most expensive habit in retail trading, and the only one in Gnosis
that is invisible in the fill stream. A protective stop is placed at the moment
of entry, when the trader is calm and the position is flat. The market goes the
other way. The stop is about to be hit -- and instead of being hit it is
cancelled and re-placed further away, because being stopped out books a loss and
moving the stop postpones one.

Nothing about that decision reaches the executions. The entry looks the same,
the exit looks the same; the loss is simply bigger when it finally arrives. It
is only visible as a cancel-and-replace of a protective order at a worse price,
which is why `OrderEvent` exists at all.

**How order events reach this detector.** Every other detector takes
`list[RoundTrip]` and nothing else, and `run_all` is called from
`model/profile.py` with exactly that. Rather than change the `Detector`
protocol -- which would break every other detector and every call site --
this detector declares `wants_orders = True`, and `run_all` passes the order
stream only to detectors that ask for it. Every existing detector and every
existing call site is untouched. The alternatives were both worse:
threading orders through `RoundTrip` would mean the reconstruction layer
carrying data it never reads, and a module-level order registry would be
actively dangerous here, because a defective trader and its clean twin share
their seed and therefore their fill ids -- a global keyed by fill id would let
one twin's stops leak into the other's profile, which is precisely the failure
mode the whole corpus exists to catch.
"""

from __future__ import annotations

from ..model.events import OrderEvent, Side
from ..model.roundtrip import RoundTrip
from ..stats import MIN_SAMPLE, PROFILE_FAMILY, assess, family_ci
from .base import Leak, closed_only


def _orders_by_trip(
    trips: list[RoundTrip], orders: list[OrderEvent],
) -> dict[int, list[OrderEvent]]:
    """Attach each order event to the round-trip it was protecting.

    Correlation is by symbol and time window: an order belongs to the trip in
    the same symbol that was open when the order was actioned. Venue is
    deliberately not part of the key -- an order event on a real venue does not
    always carry one, and a symbol's trips rarely overlap in time.

    Where two trips in one symbol *do* overlap (the same symbol traded on spot
    and on futures at once), an order is given to the earliest containing trip
    and to only one trip. Double-counting one cancel-replace as two migrations
    would inflate the very number this detector reports.
    """
    by_symbol: dict[str, list[RoundTrip]] = {}
    for t in trips:
        if t.closed_at is not None:
            by_symbol.setdefault(t.symbol, []).append(t)
    for group in by_symbol.values():
        group.sort(key=lambda t: t.opened_at)

    out: dict[int, list[OrderEvent]] = {}
    for ev in sorted(orders, key=lambda o: (o.ts, o.event_id)):
        for t in by_symbol.get(ev.symbol, ()):
            assert t.closed_at is not None
            if t.opened_at <= ev.ts <= t.closed_at:
                out.setdefault(id(t), []).append(ev)
                break
    return out


def _widenings(trip: RoundTrip, events: list[OrderEvent], min_move_pct: float) -> int:
    """How many times a protective stop on this trip was moved further away.

    Only orders sitting on the *losing* side of the average entry count.
    `OrderEvent.is_protective` is true for take-profits as well, and pushing a
    take-profit further out is a different decision with a different sign -- it
    risks giving back an open gain, not enlarging a loss -- so it is excluded
    here rather than quietly folded into the same number.

    A cancel on its own says nothing (a stop is legitimately cancelled when the
    position closes). What is damning is the replacement, so only `place` and
    `amend` events move the reference price.
    """
    if not trip.avg_entry:
        return 0
    # Which way "further away" points: a long's stop sits below entry, a
    # short's above it.
    adverse = -1.0 if trip.direction is Side.BUY else 1.0
    protective_side = trip.direction.opposite

    prev: float | None = None
    moves = 0
    for ev in events:
        if not ev.is_protective or ev.side is not protective_side:
            continue
        if ev.action not in ("place", "amend"):
            continue
        trigger = ev.trigger_price if ev.trigger_price is not None else ev.price
        if trigger is None:
            continue
        # On the losing side of entry, i.e. an actual stop rather than a target.
        if (trigger - trip.avg_entry) * adverse <= 0:
            continue
        if prev is not None:
            move_pct = (trigger - prev) / trip.avg_entry * 100.0 * adverse
            if move_pct > min_move_pct:
                moves += 1
        prev = trigger
    return moves


class StopMigration:
    """A protective stop cancelled and re-placed further away, while underwater.

    The comparison has to be chosen carefully, because the obvious one is
    circular: stops are only ever widened on trades that are already going
    badly, so "trades where you moved the stop did worse than your average
    trade" is guaranteed to be true and proves nothing.

    So the control arm is not the whole book. It is the trades that *also* went
    against the trader and *also* had a stop, and where the stop was left where
    it was put. Both arms are losing positions with a protective order on them;
    the only difference between them is what the trader did with that order.
    That comparison can come out either way, which is what makes it worth
    running.
    """

    rule = "stop_migration"
    # Tells `run_all` to hand this detector the order stream. See the module
    # docstring for why the seam is here rather than in the Detector protocol.
    wants_orders = True

    # Below this many migrations there is no habit, just an incident or two.
    MIN_EVENTS = 5
    # Trigger moves smaller than this fraction of a percent of entry are venue
    # noise -- re-quoting a stop after a partial fill, tick rounding -- not a
    # decision to give the trade more room.
    MIN_MOVE_PCT = 0.05

    def run(
        self, trips: list[RoundTrip], orders: list[OrderEvent] | None = None,
    ) -> list[Leak]:
        trips = closed_only(trips)
        if not orders:
            # No order stream, no claim. The behaviour is unobservable from
            # fills, and guessing at it from the fills is how a detector starts
            # inventing findings.
            return []

        attached = _orders_by_trip(trips, list(orders))

        widened: list[RoundTrip] = []
        held: list[RoundTrip] = []
        total_moves = 0
        for t in trips:
            events = attached.get(id(t))
            if not events:
                continue
            moves = _widenings(t, events, self.MIN_MOVE_PCT)
            if moves:
                widened.append(t)
                total_moves += moves
            elif t.net_pnl < 0:
                # The control: it went against you, the stop was there, and you
                # left it alone. Winners are excluded from the control and
                # *not* from the slice, which biases the test against this
                # detector rather than in its favour.
                held.append(t)

        if total_moves < self.MIN_EVENTS or len(widened) < MIN_SAMPLE:
            return []
        if len(held) < MIN_SAMPLE:
            return []

        # Return percentage, not dollars: a widened stop is often on a bigger
        # position, and comparing absolute PnL would be measuring size.
        cmp_ = assess(
            [t.return_pct for t in widened], [t.return_pct for t in held], seed=29,
            ci=family_ci(PROFILE_FAMILY),
        )
        realised = sum(t.net_pnl for t in widened)
        # Cancelling a stop and re-placing it lower is countable from the order
        # stream and not open to interpretation, so the behaviour is proof.
        # Only its cost is an inference, and that is what the interval gates.
        confidence = "proof" if cmp_.significant else "weak"
        worst = min(widened, key=lambda t: t.net_pnl)

        return [Leak(
            rule=self.rule,
            title="You move your stop instead of taking the loss",
            finding=(
                f"You widened a protective stop {total_moves} times across "
                f"{len(widened)} trades. Those trades returned "
                f"{cmp_.mean_slice:+.2f}% against {cmp_.mean_rest:+.2f}% on the "
                f"{len(held)} losing trades where you left the stop alone, and "
                f"realised {realised:,.0f} in total. The worst single one was "
                f"{worst.net_pnl:,.0f} on {worst.symbol}."
            ),
            confidence=confidence,
            # Expressed in return points, so translate back through the notional
            # actually at risk rather than reporting a percentage as money.
            cost=(
                sum(t.notional for t in widened) * cmp_.delta / 100.0
                if cmp_.significant and cmp_.delta < 0 else None
            ),
            n=len(widened),
            trade_ids=[t.entry_fill_ids[0] for t in widened[:20] if t.entry_fill_ids],
            comparison=cmp_,
            detail={
                "migrations": total_moves,
                "trades_with_migration": len(widened),
                "control_losing_trades_stop_held": len(held),
                "mean_return_widened_pct": round(cmp_.mean_slice, 3),
                "mean_return_held_pct": round(cmp_.mean_rest, 3),
                "worst_symbol": worst.symbol,
            },
        )]
