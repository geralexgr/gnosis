"""Synthetic traders with injected behavioural leaks, and their clean twins.

A behavioural detector cannot be validated against real trade history, because
nobody knows the ground truth: when a detector says "you hold losers", there is
no oracle to confirm the trader actually does. So we manufacture the oracle.
Each trader is generated twice from one structure:

    defective   the base trades, with one specific leak applied as a transform
    clean twin  the same base trades, untransformed

Because both come from the same seed and the same decision list, they differ
*only* in the injected pathology. A detector that fires on the twin is
unambiguously a false positive -- there is nothing there to find. That pairing
is the whole point, and it is what lets the README quote a false-positive rate
instead of a vibe.

The generator is not trying to be a market simulator. Prices are a crude random
walk and returns are drawn, not modelled. It only has to be realistic in the one
dimension being measured: the *relationship* between a behaviour and its cost.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..model.events import Fill, History, OrderEvent, Side, Venue

# The leaks we can inject. Each maps to exactly one detector, and the eval
# scores each pair independently -- a detector is only credited for finding the
# leak that was actually planted.
LEAKS = (
    "disposition",    # cut winners fast, let losers run
    "martingale",     # add to losing positions
    "revenge",        # after a loss: re-enter sooner and bigger
    "night",          # systematically worse in the small hours
    "overleverage",   # high leverage, worse outcomes
    "stop_migration", # move the stop away instead of letting it be hit
    "symbol_leak",    # one symbol donates what the others make
)

_BASE_PRICE = {
    "BTCUSDT": 68_000.0,
    "ETHUSDT": 3_200.0,
    "SOLUSDT": 145.0,
    "BNBUSDT": 610.0,
}


@dataclass
class TraderSpec:
    """Parameters for one generated trader."""

    seed: int
    n_trades: int = 260
    days: int = 365
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")
    fee_bps: float = 4.0
    # Slight positive raw edge before any leak is applied, so that a clean twin
    # is a mildly profitable trader. If the base were negative, every leak would
    # look like it "costs" money simply because trading does.
    edge_bps: float = 12.0
    vol_bps: float = 260.0
    leak: str | None = None
    strength: float = 1.0  # 0 = no effect, 1 = the realistic default


@dataclass
class _Decision:
    """One intended trade, before any leak transforms it.

    Deliberately venue- and price-agnostic: it is a *decision* (what, when,
    which way, how big, how it went), and the leak transforms decisions. Fills
    are rendered from it afterwards. Keeping the two apart is what guarantees
    the twin is structurally identical.
    """

    idx: int
    symbol: str
    opened_at: datetime
    side: Side
    notional: float
    ret_bps: float       # the trade's raw return, before leaks
    hold_hours: float
    leverage: float | None
    adds: list[float] = field(default_factory=list)  # add prices as % from entry
    # The protective stop this trader intended when they opened, as a distance
    # from entry in percent. `None` means they placed no stop at all. Part of
    # the *base* decision, so the defective trader and its twin place the same
    # stop in the same place -- the leak is only what happens to it afterwards.
    stop_pct: float | None = None
    # Successive distances the stop was moved *out* to, in percent from entry,
    # each one further away than the last. Empty for everyone except the
    # stop_migration trader: the twin places its stop and leaves it alone.
    stop_widenings: list[float] = field(default_factory=list)


def _base_decisions(spec: TraderSpec) -> list[_Decision]:
    """The trader's intended behaviour, identical for defective and twin."""
    rng = random.Random(spec.seed)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out: list[_Decision] = []
    for i in range(spec.n_trades):
        opened = start + timedelta(
            days=rng.uniform(0, spec.days),
            hours=rng.uniform(0, 24),
            minutes=rng.uniform(0, 60),
        )
        # A realistic book: mostly modest size with an occasional larger bet.
        notional = rng.choice([500, 800, 1200, 2000, 3500]) * rng.uniform(0.8, 1.3)
        lev = rng.choice([None, None, 3.0, 5.0, 10.0, 20.0])
        out.append(
            _Decision(
                idx=i,
                symbol=rng.choice(spec.symbols),
                opened_at=opened,
                side=Side.BUY if rng.random() < 0.62 else Side.SELL,
                notional=round(notional, 2),
                ret_bps=rng.gauss(spec.edge_bps, spec.vol_bps),
                hold_hours=max(0.25, rng.lognormvariate(1.6, 1.0)),
                leverage=lev,
            )
        )
    out.sort(key=lambda d: d.opened_at)
    for n, d in enumerate(out):
        d.idx = n

    # Protective stops, drawn from their own random stream. Deliberately a
    # second pass with a separate `Random`: adding this feature must not
    # perturb the draws above, or every previously generated trader -- and
    # every measured number in the README -- would silently change for a reason
    # that has nothing to do with stops.
    rng_stop = random.Random(spec.seed ^ 0x570B)
    for d in out:
        # Most trades carry a stop; some are opened without one. A trader who
        # never places a stop cannot migrate one, and the detector must say
        # nothing about them rather than inventing an opinion.
        if rng_stop.random() < 0.85:
            d.stop_pct = round(rng_stop.uniform(1.2, 3.0), 3)
    return out


def _inject(decisions: list[_Decision], spec: TraderSpec) -> list[_Decision]:
    """Apply the leak. A no-op when `spec.leak` is None -- that is the twin."""
    if spec.leak is None:
        return decisions

    rng = random.Random(spec.seed ^ 0x5EED)
    k = spec.strength

    if spec.leak == "disposition":
        # The classic: winners are grabbed early, losers are nursed. The return
        # is not changed -- only how long the position is held, and how much of
        # the move is captured. That matters: it means the detector has to find
        # the *hold-time asymmetry*, not just "this trader loses money".
        for d in decisions:
            if d.ret_bps > 0:
                d.hold_hours = max(0.25, d.hold_hours * (1 - 0.75 * k))
                d.ret_bps *= 1 - 0.45 * k          # exited before the move finished
            else:
                d.hold_hours = d.hold_hours * (1 + 5.0 * k)
                d.ret_bps *= 1 + 0.55 * k          # it kept going

    elif spec.leak == "martingale":
        # Adds into losers, at progressively worse prices.
        for d in decisions:
            if d.ret_bps < -40 and rng.random() < 0.65 * k:
                n_adds = rng.choice([1, 2, 2, 3])
                d.adds = [-(i + 1) * rng.uniform(1.2, 2.6) for i in range(n_adds)]
                # Averaging down enlarges the position into the loss.
                d.ret_bps *= 1 + 0.35 * k

    elif spec.leak == "revenge":
        # After a loss, the next trade comes sooner and larger -- and does worse.
        for prev, nxt in zip(decisions, decisions[1:]):
            if prev.ret_bps < -60 and rng.random() < 0.7 * k:
                # Anchored to when the loss was *booked*, not when it was
                # opened. Tilt is a reaction to realising the loss, and a
                # trade opened before the prior one closed is not a reaction
                # to it at all.
                booked = prev.opened_at + timedelta(hours=prev.hold_hours)
                if nxt.opened_at > booked:
                    nxt.opened_at = booked + timedelta(minutes=rng.uniform(4, 45))
                nxt.notional *= 1 + 1.4 * k
                nxt.ret_bps -= 150 * k
        decisions.sort(key=lambda d: d.opened_at)

    elif spec.leak == "night":
        # Systematically worse between 00:00 and 06:00 UTC.
        for d in decisions:
            if 0 <= d.opened_at.hour < 6:
                d.ret_bps -= 190 * k

    elif spec.leak == "overleverage":
        # High leverage correlates with worse outcomes -- rushed, late entries.
        for d in decisions:
            if d.leverage and d.leverage >= 10:
                d.ret_bps -= 165 * k

    elif spec.leak == "stop_migration":
        # The stop is placed, the position goes against it, and instead of
        # being hit the stop is cancelled and re-placed further away. Note what
        # is *not* changed: the fill stream is identical in shape to the twin's
        # -- same entry, same adds, same single exit. Only the order events and
        # the size of the realised loss differ, which is exactly the situation
        # the detector has to cope with. A detector that could find this in the
        # fills alone would be finding something else.
        for d in decisions:
            if d.stop_pct is None or d.ret_bps >= 0:
                continue  # nobody widens a stop on a position that is winning
            # Half of them at full strength, not all of them: the trades where
            # the stop was left alone are the control arm the detector needs,
            # and a trader who moves *every* stop hands it an empty comparison.
            if rng.random() >= 0.5 * k:
                continue
            dist = d.stop_pct
            for _ in range(rng.choice([1, 1, 2])):
                dist *= rng.uniform(1.7, 2.5)
                d.stop_widenings.append(round(dist, 3))
            # The whole point of the habit: the loss that the stop existed to
            # cap is taken later and larger.
            d.ret_bps *= 1 + 0.85 * k

    elif spec.leak == "symbol_leak":
        # One symbol the trader has no edge in, chosen *before* any outcome is
        # known -- picked off the seed, not off the returns. If it were picked
        # by outcome the corpus would be teaching the detector to do the very
        # hindsight selection it is supposed to resist.
        donor = rng.choice(sorted(spec.symbols))
        for d in decisions:
            if d.symbol == donor:
                d.ret_bps -= 205 * k

    else:
        raise ValueError(f"unknown leak: {spec.leak}")

    return decisions


def _render(decisions: list[_Decision], spec: TraderSpec, source: str) -> History:
    """Turn decisions into an actual fill stream."""
    rng = random.Random(spec.seed ^ 0xF111)
    fills: list[Fill] = []
    orders: list[OrderEvent] = []
    fee_rate = spec.fee_bps / 10_000.0
    n = 0

    for d in decisions:
        # A price for this symbol at this moment: a slow drift plus noise. The
        # absolute level is irrelevant to every detector; only the entry/exit
        # relationship carries signal.
        base = _BASE_PRICE.get(d.symbol, 100.0)
        entry = base * (1 + rng.gauss(0, 0.06))
        qty = d.notional / entry
        venue = Venue.FUTURES_USDS if d.leverage else Venue.SPOT

        n += 1
        fills.append(Fill(
            fill_id=f"{spec.seed}-{d.idx}-e{n}", ts=d.opened_at, symbol=d.symbol,
            side=d.side, qty=qty, price=entry, fee=d.notional * fee_rate,
            venue=venue, leverage=d.leverage,
        ))

        # The protective order life-cycle. A stop closes the position, so it
        # sits on the opposite side, at a trigger on the losing side of entry.
        closed_at = d.opened_at + timedelta(hours=d.hold_hours)
        if d.stop_pct is not None:
            n += 1
            trigger = entry * (1 - d.stop_pct / 100.0 * d.side.sign)
            orders.append(OrderEvent(
                event_id=f"{spec.seed}-{d.idx}-s{n}",
                ts=d.opened_at + timedelta(seconds=5),
                symbol=d.symbol, action="place", order_type="stop_market",
                side=d.side.opposite, trigger_price=trigger, qty=qty,
                meta={"protects": f"{spec.seed}-{d.idx}"},
            ))
            for j, dist in enumerate(d.stop_widenings):
                # Cancel-and-replace, which is what this looks like on a real
                # venue: two events, no fill, and a worse trigger.
                moved_at = d.opened_at + timedelta(
                    hours=d.hold_hours * (0.30 + 0.25 * j))
                n += 1
                orders.append(OrderEvent(
                    event_id=f"{spec.seed}-{d.idx}-c{n}", ts=moved_at,
                    symbol=d.symbol, action="cancel", order_type="stop_market",
                    side=d.side.opposite, trigger_price=trigger, qty=qty,
                ))
                trigger = entry * (1 - dist / 100.0 * d.side.sign)
                n += 1
                orders.append(OrderEvent(
                    event_id=f"{spec.seed}-{d.idx}-s{n}",
                    ts=moved_at + timedelta(seconds=2),
                    symbol=d.symbol, action="place", order_type="stop_market",
                    side=d.side.opposite, trigger_price=trigger, qty=qty,
                    meta={"protects": f"{spec.seed}-{d.idx}"},
                ))
            n += 1
            orders.append(OrderEvent(
                event_id=f"{spec.seed}-{d.idx}-c{n}", ts=closed_at,
                symbol=d.symbol, action="cancel", order_type="stop_market",
                side=d.side.opposite, trigger_price=trigger, qty=qty,
                meta={"reason": "position closed"},
            ))

        # Adds, spaced through the first part of the hold.
        total_qty = qty
        for j, pct in enumerate(d.adds):
            add_price = entry * (1 + pct / 100.0 * d.side.sign)
            add_qty = qty * rng.uniform(0.6, 1.1)
            n += 1
            fills.append(Fill(
                fill_id=f"{spec.seed}-{d.idx}-a{n}",
                ts=d.opened_at + timedelta(hours=d.hold_hours * (0.2 + 0.2 * j)),
                symbol=d.symbol, side=d.side, qty=add_qty, price=add_price,
                fee=add_qty * add_price * fee_rate, venue=venue, leverage=d.leverage,
            ))
            total_qty += add_qty

        # The exit price that realises the intended return for this direction.
        exit_price = entry * (1 + d.ret_bps / 10_000.0 * d.side.sign)
        n += 1
        fills.append(Fill(
            fill_id=f"{spec.seed}-{d.idx}-x{n}",
            ts=closed_at,
            symbol=d.symbol, side=d.side.opposite, qty=total_qty, price=exit_price,
            fee=total_qty * exit_price * fee_rate, venue=venue, leverage=d.leverage,
        ))

    return History(fills=fills, orders=orders, source=source)


def generate(spec: TraderSpec) -> History:
    """One trader, with `spec.leak` applied (or clean if it is None)."""
    label = spec.leak or "clean"
    return _render(_inject(_base_decisions(spec), spec), spec,
                   f"synthetic:seed={spec.seed}:{label}")


def generate_pair(spec: TraderSpec) -> tuple[History, History]:
    """A defective trader and its clean twin, from one structure.

    The twin is the control. Anything a detector reports on it is noise by
    construction, because the only difference between the two histories is the
    leak that was planted.
    """
    if spec.leak is None:
        raise ValueError("generate_pair needs a leak to inject; got None")
    twin_spec = TraderSpec(**{**spec.__dict__, "leak": None})
    return generate(spec), generate(twin_spec)
