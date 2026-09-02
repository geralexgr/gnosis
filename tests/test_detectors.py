"""The two order-aware and selection detectors, against planted leaks and twins.

Both detectors added here are the kind that go wrong quietly. Stop migration
reads a stream nothing else reads, so a correlation bug would show up as a
plausible-looking number rather than a crash. Symbol competence picks what to
report *after* seeing the outcomes, which is the exact shape of a detector that
finds something in every history including a random one.

So the assertions come in fours, for each: it fires on the planted leak, it is
silent on that trader's clean twin, it is silent when the evidence is thin, and
whatever it does report carries the trade ids that produced it.

Run: python tests/test_detectors.py
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gnosis.detectors import run_all  # noqa: E402
from gnosis.detectors.execution import StopMigration, _orders_by_trip, _widenings  # noqa: E402
from gnosis.detectors.selection import SymbolCompetence  # noqa: E402
from gnosis.ingest.synthetic import TraderSpec, generate, generate_pair  # noqa: E402
from gnosis.model.events import Fill, OrderEvent, Side, Venue  # noqa: E402
from gnosis.model.roundtrip import reconstruct  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{'' if cond else '  <- ' + detail}")


T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
_n = [0]


def fill(side, qty, price, *, mins=0, fee=0.0, symbol="ETHUSDT"):
    _n[0] += 1
    return Fill(fill_id=f"f{_n[0]}", ts=T0 + timedelta(minutes=mins), symbol=symbol,
                side=side, qty=qty, price=price, fee=fee, venue=Venue.SPOT)


def order(action, trigger, *, mins=0, symbol="ETHUSDT", side=Side.SELL,
          order_type="stop_market"):
    _n[0] += 1
    return OrderEvent(event_id=f"o{_n[0]}", ts=T0 + timedelta(minutes=mins),
                      symbol=symbol, action=action, order_type=order_type,
                      side=side, trigger_price=trigger, qty=1.0)


def profile_leaks(history, *, with_orders=True):
    """What a user would actually be shown: surfacing findings only.

    Mirrors `Profile`, which drops `weak` findings before anything renders. A
    test that scored the raw detectors would be asserting on findings nobody
    ever sees.
    """
    closed, _ = reconstruct(history.fills)
    leaks = run_all(closed, history.orders if with_orders else None)
    return {leak.rule: leak for leak in leaks if leak.confidence != "weak"}


def planted_symbol(spec):
    """The symbol `_inject` degrades, re-derived rather than inferred.

    Deliberately not "whichever symbol did worst" -- that is the hindsight
    selection this detector exists to resist, and using it here would let a
    detector that always names the worst symbol pass this test.
    """
    return random.Random(spec.seed ^ 0x5EED).choice(sorted(spec.symbols))


# ---------------------------------------------------------------- stop migration

print("=== stop migration: the widening rule ===")
long_trip, _ = reconstruct([fill(Side.BUY, 1, 100), fill(Side.SELL, 1, 96, mins=300)])
widened = [order("place", 98.0, mins=1), order("cancel", 98.0, mins=100),
           order("place", 95.0, mins=101)]
tightened = [order("place", 95.0, mins=1), order("cancel", 95.0, mins=100),
             order("place", 98.0, mins=101)]
check("stop moved away counts", _widenings(long_trip[0], widened, 0.05) == 1,
      f"got {_widenings(long_trip[0], widened, 0.05)}")
check("stop pulled in does not", _widenings(long_trip[0], tightened, 0.05) == 0)
check("a lone cancel is not a migration",
      _widenings(long_trip[0], [order("place", 98.0, mins=1),
                                order("cancel", 98.0, mins=300)], 0.05) == 0)
check("re-quoting by a tick is not a migration",
      _widenings(long_trip[0], [order("place", 98.0, mins=1),
                                order("place", 97.99, mins=100)], 0.05) == 0)
check("take-profit pushed further out is not a stop",
      _widenings(long_trip[0], [order("place", 104.0, mins=1, order_type="take_profit"),
                                order("place", 112.0, mins=100,
                                      order_type="take_profit")], 0.05) == 0)

short_trip, _ = reconstruct([fill(Side.SELL, 1, 100), fill(Side.BUY, 1, 104, mins=300)])
short_widened = [order("place", 102.0, mins=1, side=Side.BUY),
                 order("place", 106.0, mins=100, side=Side.BUY)]
check("a short's stop widens upward",
      _widenings(short_trip[0], short_widened, 0.05) == 1)
check("direction is not assumed long",
      _widenings(short_trip[0], [order("place", 102.0, mins=1, side=Side.BUY),
                                 order("place", 100.5, mins=100, side=Side.BUY)],
                 0.05) == 0)

print("\n=== stop migration: correlating orders to trips ===")
trips, _ = reconstruct([fill(Side.BUY, 1, 100), fill(Side.SELL, 1, 96, mins=300),
                        fill(Side.BUY, 1, 100, symbol="BTCUSDT"),
                        fill(Side.SELL, 1, 96, mins=300, symbol="BTCUSDT")])
attached = _orders_by_trip(trips, [
    order("place", 98.0, mins=1),                        # inside, right symbol
    order("place", 95.0, mins=100),                      # inside, right symbol
    order("place", 90.0, mins=100, symbol="BTCUSDT"),    # other symbol
    order("place", 80.0, mins=9_000),                    # after the trip closed
])
eth = [t for t in trips if t.symbol == "ETHUSDT"][0]
btc = [t for t in trips if t.symbol == "BTCUSDT"][0]
check("orders land on their own symbol", len(attached[id(eth)]) == 2,
      f"got {len(attached[id(eth)])}")
check("other symbol kept apart", len(attached[id(btc)]) == 1)
check("orders outside the window are dropped",
      sum(len(v) for v in attached.values()) == 3)

print("\n=== stop migration: on the corpus ===")
spec = TraderSpec(seed=31 + 7919, leak="stop_migration", strength=1.3)
bad, twin = generate_pair(spec)
found = profile_leaks(bad)
check("fires on the planted leak", "stop_migration" in found,
      f"got {sorted(found)}")
leak = found.get("stop_migration")
if leak is not None:
    closed, _ = reconstruct(bad.fills)
    ids = {i for t in closed for i in t.entry_fill_ids}
    check("receipts are populated", bool(leak.trade_ids))
    check("receipts are real fill ids", set(leak.trade_ids) <= ids)
    check("it costs money", leak.costs_money, f"cost={leak.cost}")
    check("the behaviour is countable, so proof", leak.confidence == "proof",
          leak.confidence)
    check("it counted migrations", leak.detail["migrations"] >= leak.n,
          str(leak.detail))
    check("the control arm is losing trades with the stop left alone",
          leak.detail["control_losing_trades_stop_held"] >= 8, str(leak.detail))
else:  # keep the count stable whichever branch runs
    for name in ("receipts are populated", "receipts are real fill ids",
                 "it costs money", "the behaviour is countable, so proof",
                 "it counted migrations",
                 "the control arm is losing trades with the stop left alone"):
        check(name, False, "detector did not fire")

check("silent on the clean twin", "stop_migration" not in profile_leaks(twin),
      str(profile_leaks(twin).get("stop_migration")))
check("the twin still places stops", len(twin.orders) > 100, f"{len(twin.orders)}")
check("the defective trader issues more order events",
      len(bad.orders) > len(twin.orders), f"{len(bad.orders)} vs {len(twin.orders)}")

# The habit leaves no trace in the fill stream. A detector that "found" it
# without the orders would be finding something else, and saying so.
check("silent when no order stream is supplied",
      "stop_migration" not in profile_leaks(bad, with_orders=False))

closed_bad, _ = reconstruct(bad.fills)
check("silent on a thin history",
      StopMigration().run(closed_bad[:12], bad.orders) == [])
check("silent on an empty history", StopMigration().run([], bad.orders) == [])

print("\n=== stop migration: the seam into run_all ===")
check("run_all still takes trips alone", len(run_all(closed_bad)) >= 0)
check("the other detectors are unaffected by the orders argument",
      {leak_.rule for leak_ in run_all(closed_bad) if leak_.rule != "stop_migration"}
      == {leak_.rule for leak_ in run_all(closed_bad, bad.orders)
          if leak_.rule != "stop_migration"})
check("only the order-aware detector asks for orders",
      StopMigration().wants_orders and
      not getattr(SymbolCompetence(), "wants_orders", False))

# ------------------------------------------------------------ symbol competence

print("\n=== symbol competence: on the corpus ===")
spec_s = TraderSpec(seed=31 + 7919, leak="symbol_leak", strength=1.3)
bad_s, twin_s = generate_pair(spec_s)
found_s = profile_leaks(bad_s)
check("fires on the planted leak", "symbol_selection" in found_s,
      f"got {sorted(found_s)}")
leak_s = found_s.get("symbol_selection")
if leak_s is not None:
    closed_s, _ = reconstruct(bad_s.fills)
    ids_s = {i for t in closed_s for i in t.entry_fill_ids}
    check("names the symbol that was actually planted",
          leak_s.detail["symbol"] == planted_symbol(spec_s),
          f"{leak_s.detail['symbol']} vs {planted_symbol(spec_s)}")
    check("receipts are populated", bool(leak_s.trade_ids))
    check("receipts are real fill ids", set(leak_s.trade_ids) <= ids_s)
    check("receipts belong to the named symbol",
          all(t.symbol == leak_s.detail["symbol"] for t in closed_s
              if t.entry_fill_ids and t.entry_fill_ids[0] in set(leak_s.trade_ids)))
    check("it costs money", leak_s.costs_money, f"cost={leak_s.cost}")
    check("the symbol loses money outright, not merely relatively",
          leak_s.detail["mean_return_symbol_pct"] < 0, str(leak_s.detail))
    check("it says how many symbols it tested",
          len(leak_s.detail["symbols_tested"]) >= 2, str(leak_s.detail))
else:
    for name in ("names the symbol that was actually planted",
                 "receipts are populated", "receipts are real fill ids",
                 "receipts belong to the named symbol", "it costs money",
                 "the symbol loses money outright, not merely relatively",
                 "it says how many symbols it tested"):
        check(name, False, "detector did not fire")

check("silent on the clean twin", "symbol_selection" not in profile_leaks(twin_s),
      str(profile_leaks(twin_s).get("symbol_selection")))

print("\n=== symbol competence: refusing the hindsight shortcut ===")
closed_twin, _ = reconstruct(twin_s.fills)
check("silent on a thin history", SymbolCompetence().run(closed_twin[:20]) == [])
check("silent on an empty history", SymbolCompetence().run([]) == [])

single = generate(TraderSpec(seed=77, symbols=("ETHUSDT",), n_trades=200))
closed_single, _ = reconstruct(single.fills)
check("silent when there is only one symbol to trade",
      SymbolCompetence().run(closed_single) == [],
      f"{len(closed_single)} trips in one symbol")

# A symbol with a handful of trades is always available to be the worst one.
# Reporting it is how this detector would become astrology, so it must not
# clear the minimum however bad those few trades were.
thin_fills = []
for i in range(120):
    thin_fills += [fill(Side.BUY, 1, 100, mins=i * 600, symbol="BTCUSDT"),
                   fill(Side.SELL, 1, 100 + (3 if i % 2 else -2), mins=i * 600 + 60,
                        symbol="BTCUSDT")]
for i in range(6):
    thin_fills += [fill(Side.BUY, 1, 100, mins=i * 900 + 5, symbol="DOGEUSDT"),
                   fill(Side.SELL, 1, 70, mins=i * 900 + 65, symbol="DOGEUSDT")]
closed_thin, _ = reconstruct(thin_fills)
worst = min(closed_thin, key=lambda t: t.return_pct)
check("the disastrous symbol really is the worst by outcome",
      worst.symbol == "DOGEUSDT")
check("but six trades never earn a verdict",
      SymbolCompetence().run(closed_thin) == [],
      str(SymbolCompetence().run(closed_thin)))

# The honesty test that matters: eight four-symbol books with nothing planted
# in them. One symbol is always last; the detector must almost never say so.
noise_fires = 0
for i in range(8):
    _, clean = generate_pair(TraderSpec(seed=101 + i * 7919, leak="disposition"))
    trips_clean, _ = reconstruct(clean.fills)
    if SymbolCompetence().run(trips_clean):
        noise_fires += 1
print(f"     fired on {noise_fires}/8 books with no symbol leak planted")
check("does not fire on noise", noise_fires <= 1, f"{noise_fires}/8")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
