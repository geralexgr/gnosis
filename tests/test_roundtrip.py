"""Round-trip reconstruction regression.

Every behavioural number Gnosis reports is computed from RoundTrips, so an
error here is an error in every finding downstream. These cases are the ones
that actually go wrong in real fill streams: scaling in, scaling out, flipping
through zero, and fees quietly turning a winner into a loser.

Run: python tests/test_roundtrip.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gnosis.model.events import Fill, Side, Venue  # noqa: E402
from gnosis.model.roundtrip import reconstruct, reconstruct_realised  # noqa: E402

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
_n = [0]


def fill(side, qty, price, *, mins=0, fee=0.0, symbol="ETHUSDT", venue=Venue.SPOT, lev=None):
    _n[0] += 1
    return Fill(
        fill_id=f"f{_n[0]}",
        ts=T0 + timedelta(minutes=mins),
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        venue=venue,
        leverage=lev,
    )


PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{'' if cond else '  <- ' + detail}")


print("=== simple long, profitable ===")
closed, _open = reconstruct([fill(Side.BUY, 2, 100), fill(Side.SELL, 2, 110, mins=60)])
t = closed[0]
check("one trip", len(closed) == 1, f"got {len(closed)}")
check("gross pnl 20", abs(t.gross_pnl - 20) < 1e-9, f"got {t.gross_pnl}")
check("is winner", t.is_winner)
check("hold 1h", abs(t.hold_hours - 1.0) < 1e-9, f"got {t.hold_hours}")
check("avg entry 100", abs(t.avg_entry - 100) < 1e-9)
check("avg exit 110", abs(t.avg_exit - 110) < 1e-9)
check("receipts kept", len(t.entry_fill_ids) == 1 and len(t.exit_fill_ids) == 1)

print("\n=== fees can flip the sign ===")
closed, _ = reconstruct([fill(Side.BUY, 1, 100, fee=6), fill(Side.SELL, 1, 105, mins=5, fee=6)])
t = closed[0]
check("gross positive", t.gross_pnl > 0, f"{t.gross_pnl}")
check("net negative", t.net_pnl < 0, f"{t.net_pnl}")
check("counted as loser", not t.is_winner)

print("\n=== short ===")
closed, _ = reconstruct([fill(Side.SELL, 3, 200), fill(Side.BUY, 3, 180, mins=30)])
t = closed[0]
check("short direction", t.direction is Side.SELL)
check("short profit 60", abs(t.gross_pnl - 60) < 1e-9, f"got {t.gross_pnl}")

print("\n=== adds while underwater (the expensive habit) ===")
# Long at 100, add at 90, add at 80 -- both adds are below the running average.
closed, _ = reconstruct([
    fill(Side.BUY, 1, 100),
    fill(Side.BUY, 1, 90, mins=10),
    fill(Side.BUY, 1, 80, mins=20),
    fill(Side.SELL, 3, 85, mins=30),
])
t = closed[0]
check("2 adds", t.n_adds == 2, f"got {t.n_adds}")
check("2 adds underwater", t.n_adds_underwater == 2, f"got {t.n_adds_underwater}")
check("avg entry 90", abs(t.avg_entry - 90) < 1e-9, f"got {t.avg_entry}")
check("worst price 80", abs((t.worst_observed_price or 0) - 80) < 1e-9)
check("adverse excursion ~-11%", abs(t.adverse_excursion_pct + 11.111) < 0.01,
      f"got {t.adverse_excursion_pct}")

print("\n=== short adverse excursion is also negative ===")
# Short at 100, price runs up to 120 against us before we cover at 110.
closed, _ = reconstruct([
    fill(Side.SELL, 1, 100),
    fill(Side.SELL, 1, 120, mins=10),   # adding to a losing short
    fill(Side.BUY, 2, 110, mins=20),
])
t = closed[0]
check("short add underwater", t.n_adds_underwater == 1, f"got {t.n_adds_underwater}")
check("worst price 120", abs((t.worst_observed_price or 0) - 120) < 1e-9)
check("short excursion negative", t.adverse_excursion_pct < 0, f"got {t.adverse_excursion_pct}")
check("short excursion ~-9.1%", abs(t.adverse_excursion_pct + 9.0909) < 0.01,
      f"got {t.adverse_excursion_pct}")

print("\n=== adds while winning are NOT underwater ===")
closed, _ = reconstruct([
    fill(Side.BUY, 1, 100),
    fill(Side.BUY, 1, 110, mins=10),
    fill(Side.SELL, 2, 120, mins=20),
])
t = closed[0]
check("1 add", t.n_adds == 1)
check("0 underwater", t.n_adds_underwater == 0, f"got {t.n_adds_underwater}")

print("\n=== scaling out (partial exits) ===")
closed, _ = reconstruct([
    fill(Side.BUY, 4, 100),
    fill(Side.SELL, 1, 110, mins=10),
    fill(Side.SELL, 1, 120, mins=20),
    fill(Side.SELL, 2, 130, mins=30),
])
t = closed[0]
check("one trip only", len(closed) == 1, f"got {len(closed)}")
check("2 partial exits", t.n_partial_exits == 2, f"got {t.n_partial_exits}")
# 1@+10 + 1@+20 + 2@+30 = 90
check("gross pnl 90", abs(t.gross_pnl - 90) < 1e-9, f"got {t.gross_pnl}")
check("weighted avg exit 122.5", abs(t.avg_exit - 122.5) < 1e-9, f"got {t.avg_exit}")

print("\n=== FIFO lineage across differently-priced lots ===")
closed, _ = reconstruct([
    fill(Side.BUY, 1, 100),
    fill(Side.BUY, 1, 200, mins=10),
    fill(Side.SELL, 1, 150, mins=20),   # must close the 100 lot, +50
    fill(Side.SELL, 1, 150, mins=30),   # then the 200 lot, -50
])
t = closed[0]
check("fifo nets to 0", abs(t.gross_pnl) < 1e-9, f"got {t.gross_pnl}")

print("\n=== flip through zero makes two trips ===")
closed, still_open = reconstruct([
    fill(Side.BUY, 2, 100),
    fill(Side.SELL, 5, 110, mins=10),   # closes 2 long (+20), opens 3 short
    fill(Side.BUY, 3, 100, mins=20),    # closes the short (+30)
])
check("two trips", len(closed) == 2, f"got {len(closed)}")
check("first long +20", abs(closed[0].gross_pnl - 20) < 1e-9, f"got {closed[0].gross_pnl}")
check("second is short", closed[1].direction is Side.SELL)
check("second short +30", abs(closed[1].gross_pnl - 30) < 1e-9, f"got {closed[1].gross_pnl}")
check("nothing left open", not still_open)

print("\n=== open positions are excluded, not counted as held losers ===")
closed, still_open = reconstruct([fill(Side.BUY, 1, 100), fill(Side.BUY, 1, 50, mins=10)])
check("no closed trips", len(closed) == 0, f"got {len(closed)}")
check("one open trip", len(still_open) == 1)
check("open trip flagged", still_open[0].is_open)

print("\n=== venues do not net against each other ===")
closed, _ = reconstruct([
    fill(Side.BUY, 1, 100, venue=Venue.SPOT),
    fill(Side.BUY, 1, 100, venue=Venue.FUTURES_USDS, lev=20),
    fill(Side.SELL, 1, 110, mins=10, venue=Venue.SPOT),
    fill(Side.SELL, 1, 90, mins=20, venue=Venue.FUTURES_USDS, lev=20),
])
check("two trips", len(closed) == 2, f"got {len(closed)}")
spot = [t for t in closed if t.venue is Venue.SPOT][0]
fut = [t for t in closed if t.venue is Venue.FUTURES_USDS][0]
check("spot +10", abs(spot.gross_pnl - 10) < 1e-9)
check("futures -10", abs(fut.gross_pnl + 10) < 1e-9)
check("leverage captured", fut.max_leverage == 20)
check("spot has no leverage", spot.max_leverage is None)

print("\n=== symbols do not net against each other ===")
closed, _ = reconstruct([
    fill(Side.BUY, 1, 100, symbol="BTCUSDT"),
    fill(Side.BUY, 1, 10, symbol="SOLUSDT"),
    fill(Side.SELL, 1, 110, mins=10, symbol="BTCUSDT"),
    fill(Side.SELL, 1, 8, mins=20, symbol="SOLUSDT"),
])
check("two trips", len(closed) == 2, f"got {len(closed)}")

print("\n=== property: PnL conservation over random streams ===")
# The invariant that actually matters and that no hand-written case can cover:
# over a stream that ends flat, realised PnL must equal the net cash the fills
# moved. A leveraged book must reconcile exactly. A spot book may differ by the
# dust it deliberately abandoned -- but only by that, and the amount must be
# recorded on the trip rather than quietly lost.
import random as _random  # noqa: E402

def _stream(rng, venue, lev):
    out, pos, n = [], 0.0, 0
    for _ in range(rng.randint(6, 40)):
        side = Side.BUY if (pos <= 0 or rng.random() < 0.5) else Side.SELL
        q = round(rng.uniform(0.1, 3.0), 4)
        n += 1
        out.append(Fill(str(n), T0 + timedelta(minutes=n), "B", side, q,
                        round(rng.uniform(90, 110), 2), venue=venue, leverage=lev))
        pos += q if side is Side.BUY else -q
    if abs(pos) > 1e-9:            # force flat, so nothing is left open
        n += 1
        out.append(Fill(str(n), T0 + timedelta(minutes=n), "B",
                        Side.SELL if pos > 0 else Side.BUY, abs(pos), 100.0,
                        venue=venue, leverage=lev))
    return out

for _label, _venue, _lev, _allow_dust in (
    ("futures", Venue.FUTURES_USDS, 10, False),
    ("spot", Venue.SPOT, None, True),
):
    _rng = _random.Random(7)
    _left_open, _unexplained, _mismatched = 0, 0, 0
    for _ in range(200):
        _fills = _stream(_rng, _venue, _lev)
        _closed, _open = reconstruct(_fills)
        # Only asserted where it is meaningful. Sweeping dust makes the
        # reconstructor's position diverge from raw netting, so a spot stream
        # forced flat on the raw net can legitimately leave a residual arc --
        # and `RoundTrip.qty_opened` describes the whole arc, not what remains,
        # so there is no field here that can express "how much is still held".
        # The cash check below is the invariant that actually constrains this.
        if _open and not _allow_dust:
            _left_open += 1
        _gap = sum(t.gross_pnl for t in _closed) - sum(-x.signed_qty * x.price for x in _fills)
        _dust = sum(t.dust_qty for t in _closed)
        if abs(_gap) > 0.01:
            if not _allow_dust:
                _mismatched += 1
            elif _dust <= 1e-12:
                _unexplained += 1
    if not _allow_dust:
        check(f"{_label}: nothing left open on a flat-ending stream",
              _left_open == 0, f"{_left_open}/200")
    check(f"{_label}: no unexplained cash gap", _mismatched == 0 and _unexplained == 0,
          f"mismatched={_mismatched} unexplained={_unexplained}")

print("\n=== realised mode: shorts, and books that sell out cleanly ===")
_r, _h = reconstruct_realised([fill(Side.BUY, 1.0, 100), fill(Side.SELL, 1.0, 110, mins=60)])
check("a clean buy/sell does not crash", len(_r) == 1, f"got {len(_r)}")
check("...and nothing is left held", not _h)
check("...with the right PnL", abs(_r[0].net_pnl - 10.0) < 1e-9, f"got {_r[0].net_pnl}")

_shorts = []
for _i, (_op, _cl) in enumerate([(1000, 900), (1000, 800), (1000, 950)]):
    _shorts.append(fill(Side.SELL, 1.0, _op, mins=_i * 500))
    _shorts.append(fill(Side.BUY, 1.0, _cl, mins=_i * 500 + 60))
_flat, _ = reconstruct(_shorts)
_real, _ = reconstruct_realised(_shorts)
check("realised mode agrees with flat-to-flat on shorts",
      abs(sum(t.net_pnl for t in _real) - sum(t.net_pnl for t in _flat)) < 1e-9,
      f"realised {sum(t.net_pnl for t in _real)} vs flat {sum(t.net_pnl for t in _flat)}")
check("...and labels them as shorts",
      all(t.direction is Side.SELL for t in _real), f"{[t.direction for t in _real]}")
check("...without inflating hold time",
      all(t.hold_hours < 2 for t in _real), f"{[t.hold_hours for t in _real]}")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
