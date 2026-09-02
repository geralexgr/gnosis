"""Generate a FICTIONAL Binance futures account for the demo.

Nothing here is a record of anyone. It is a fixture, generated from a seed, and
it exists for two reasons.

**A demo needs a book worth profiling.** Gnosis needs closed positions to say
anything, and it correctly refuses below thirty. A real spot investor's export
does not contain thirty closed positions — they accumulate and hold, which is
why the one real export tested against this project yielded fewer than thirty
realised trades over a multi-year span and was declined. Futures books close, so they can be profiled.

**The futures CSV dialect was never tested against a real file.** The parser was
written against Binance's documented column names. Emitting this fixture in that
exact format exercises the untested half of the parser end to end.

The persona is a composite of documented retail pathologies, not a person: an
active perp trader who is genuinely competent in the daytime on the majors, and
who gives most of it back at 3am on impulse leverage. Every leak below is *made
true in the data* — none of it is asserted anywhere. If the detectors do not
find them, that is a finding about the detectors.

    python3 scripts/make_demo_account.py --out demo-account.csv
"""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta, timezone

# Realistic levels for the window this fixture covers.
BASE_PRICE = {
    "BTCUSDT": 67_000.0,
    "ETHUSDT": 3_100.0,
    "SOLUSDT": 148.0,
    "DOGEUSDT": 0.145,
}
# Where the trader is competent, and where they are not. This is the persona.
GOOD_SYMBOLS = ("BTCUSDT", "ETHUSDT")
DONOR_SYMBOL = "DOGEUSDT"          # keeps trading it, keeps losing
NIGHT_HOURS = range(0, 6)          # insomnia trading, systematically worse


def _price(rng: random.Random, symbol: str, drift: float) -> float:
    base = BASE_PRICE[symbol]
    return base * (1 + drift) * (1 + rng.gauss(0, 0.04))


def build(seed: int, n_positions: int, days: int) -> list[dict]:
    rng = random.Random(seed)
    start = datetime(2026, 1, 6, tzinfo=timezone.utc)
    rows: list[dict] = []
    last_loss_at: datetime | None = None
    baseline_notional = 2_400.0

    for _ in range(n_positions):
        opened = start + timedelta(
            days=rng.uniform(0, days), hours=rng.uniform(0, 24), minutes=rng.uniform(0, 60)
        )
        hour = opened.hour
        at_night = hour in NIGHT_HOURS

        # Symbol choice is session-dependent, which is what makes this a
        # persona rather than a noise generator: disciplined on the majors in
        # daylight, gambling on the alt at 3am. The leaks then *co-occur* the
        # way they do in a real book -- night, leverage and the donor symbol all
        # loading onto the same trades -- which is a harder and more honest test
        # of detectors that each claim to isolate one of them.
        if at_night:
            symbol = DONOR_SYMBOL if rng.random() < 0.42 else rng.choice(
                ("SOLUSDT", "ETHUSDT", DONOR_SYMBOL)
            )
        else:
            symbol = DONOR_SYMBOL if rng.random() < 0.07 else rng.choice(
                GOOD_SYMBOLS + GOOD_SYMBOLS + ("SOLUSDT",)
            )

        # Leverage: disciplined by day, impulsive at night.
        leverage = rng.choice([20, 25, 20, 10]) if at_night else rng.choice([3, 5, 5, 10])

        # Revenge: soon after booking a loss, size up.
        notional = baseline_notional * rng.uniform(0.7, 1.4)
        revenge = False
        if last_loss_at is not None:
            gap_min = (opened - last_loss_at).total_seconds() / 60.0
            if 0 < gap_min < 75 and rng.random() < 0.55:
                revenge = True
                notional *= rng.uniform(2.1, 3.2)

        direction = "BUY" if rng.random() < 0.58 else "SELL"

        # The edge, before the leaks bite. Genuinely positive on the majors by day.
        # Genuinely good at the majors in daylight. This matters for the demo
        # and for the product: a tool that only ever reports failure gets read
        # once and closed, and the pre-trade gate has to be able to say "this is
        # your best setup and you habitually size it too small".
        edge_bps = 205.0 if (symbol in GOOD_SYMBOLS and not at_night) else 10.0
        if at_night:
            edge_bps -= 150.0
        if leverage >= 20:
            edge_bps -= 74.0
        if symbol == DONOR_SYMBOL:
            edge_bps -= 96.0
        if revenge:
            edge_bps -= 130.0
        ret_bps = rng.gauss(edge_bps, 210.0)

        # Disposition: winners cut early, losers nursed.
        if ret_bps > 0:
            hold_h = max(0.3, rng.lognormvariate(0.8, 0.7))
            ret_bps *= 0.62
        else:
            hold_h = max(0.5, rng.lognormvariate(2.5, 0.9))
            ret_bps *= 1.24

        drift = (opened - start).days / max(days, 1) * 0.22
        entry = _price(rng, symbol, drift)
        qty = notional / entry
        sign = 1 if direction == "BUY" else -1
        fee_rate = 0.0004

        legs = [(opened, direction, qty, entry, 0.0)]
        total_qty = qty

        # Averaging down: on losers, sometimes add at worse prices.
        if ret_bps < -60 and rng.random() < 0.42:
            for j in range(rng.choice([1, 2, 2, 3])):
                add_px = entry * (1 - sign * (j + 1) * rng.uniform(0.011, 0.024))
                add_qty = qty * rng.uniform(0.6, 1.2)
                legs.append((
                    opened + timedelta(hours=hold_h * (0.15 + 0.18 * j)),
                    direction, add_qty, add_px, 0.0,
                ))
                total_qty += add_qty

        avg_entry = sum(q * p for _, _, q, p, _ in legs) / total_qty
        exit_px = avg_entry * (1 + sign * ret_bps / 10_000.0)
        realised = (exit_px - avg_entry) * total_qty * sign
        closed_at = opened + timedelta(hours=hold_h)
        legs.append((
            closed_at, "SELL" if direction == "BUY" else "BUY",
            total_qty, exit_px, realised,
        ))

        for ts, side, q, px, pnl in legs:
            rows.append({
                "Date(UTC)": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "Symbol": symbol,
                "Side": side,
                "Price": f"{px:.6f}".rstrip("0").rstrip("."),
                "Quantity": f"{q:.6f}".rstrip("0").rstrip(".") + symbol.replace("USDT", ""),
                "Amount": f"{q * px:.4f}USDT",
                "Fee": f"{q * px * fee_rate:.6f}USDT",
                "Realized Profit": f"{pnl:.4f}USDT",
                "Leverage": f"{leverage}x",
                "Margin Mode": "Cross",
            })

        if realised < 0:
            last_loss_at = closed_at

    # Binance exports newest first.
    rows.sort(key=lambda r: r["Date(UTC)"], reverse=True)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="demo-account.csv")
    ap.add_argument("--seed", type=int, default=4242)
    ap.add_argument("--positions", type=int, default=260)
    ap.add_argument("--days", type=int, default=240)
    args = ap.parse_args()

    rows = build(args.seed, args.positions, args.days)
    cols = list(rows[0].keys())
    # UTF-8 BOM and CRLF, exactly as Binance writes them.
    with open(args.out, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(",".join(cols) + "\r\n")
        for r in rows:
            fh.write(",".join(r[c] for c in cols) + "\r\n")
    print(f"wrote {len(rows)} rows to {args.out}  (FICTIONAL — generated, seed={args.seed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
