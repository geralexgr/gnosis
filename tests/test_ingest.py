"""Live-adapter regression: Binance CSV exports and `baw` on-chain swaps.

Both adapters sit in front of everything else Gnosis computes, so a mistake
here is invisible and total: a mis-read fee, a dropped fill or a phantom trade
from a failed swap changes every downstream number without changing anything
that looks wrong. The cases below are the ones that actually bite -- the two
CSV dialects, quantities with their asset glued on, rows that must refuse to
parse rather than vanish, and the `FINISHED`/`FAILED`/`PENDING` split that
decides whether an on-chain order is a trade, a cost, or neither.

The `baw` half runs entirely on canned JSON through the injected runner. That
is not only a testing convenience: `baw` needs a phone confirmation to sign in,
so a suite that shelled out to it could never run twice in a row unattended.

Run: python3 tests/test_ingest.py
"""

from __future__ import annotations

import sys
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import json  # noqa: E402

from gnosis.ingest import baw_onchain as baw  # noqa: E402
from gnosis.ingest.binance_csv import CsvFormatError, parse_csv, parse_text  # noqa: E402
from gnosis.model.events import Side, Venue  # noqa: E402
from gnosis.model.roundtrip import reconstruct  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{'' if cond else '  <- ' + detail}")


def raises(exc_type, fn, *args, **kwargs):
    """Return the exception `fn` raised, or None. Keeps the checks one-liners."""
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return exc
    except Exception as exc:  # noqa: BLE001 - a wrong exception type is a failure, not a crash
        return ("wrong-type", exc)
    return None


SPOT_HEADER = "Date(UTC),Pair,Side,Price,Executed,Amount,Fee\n"


def spot_row(row: str, header: str = SPOT_HEADER, **kw):
    return parse_text(header + row + "\n", **kw)


# --------------------------------------------------------------------------
print("=== spot export ===")
hist = parse_csv(FIXTURES / "binance_spot.csv")
check("3 fills parsed", len(hist.fills) == 3, f"got {len(hist.fills)}")
check("source names the file", hist.source == "binance:csv:binance_spot.csv", hist.source)
check("sorted oldest first", [f.symbol for f in hist.fills] == ["ETHUSDT", "ETHUSDT", "BTCUSDT"],
      f"got {[f.symbol for f in hist.fills]}")
buy, sell, btc = hist.fills
check("side parsed", buy.side is Side.BUY and sell.side is Side.SELL)
check("venue spot", all(f.venue is Venue.SPOT for f in hist.fills))
check("spot leverage is None, not 1.0", all(f.leverage is None for f in hist.fills))
check("qty strips trailing asset", abs(buy.qty - 2.0) < 1e-12, f"got {buy.qty}")
check("fractional qty with asset", abs(btc.qty - 0.05) < 1e-12, f"got {btc.qty}")
check("price parsed", abs(buy.price - 3200.0) < 1e-9, f"got {buy.price}")
check("quote-asset fee passes through", abs(sell.fee - 6.6) < 1e-9, f"got {sell.fee}")
# 0.002 ETH of fee, on a fill priced at 3200 -> 6.40 USDT. Converting at the
# fill's own price is exact; guessing zero is what turns losers into winners.
check("base-asset fee valued at fill price", abs(buy.fee - 6.4) < 1e-9, f"got {buy.fee}")
check("fee asset kept in meta", buy.meta.get("fee_asset") == "ETH", str(buy.meta))
check("timestamps are UTC-aware", all(f.ts.tzinfo is not None for f in hist.fills))
check("naive file time read as UTC", buy.ts.astimezone(timezone.utc).hour == 12, str(buy.ts))
check("blank line did not become a fill", len(hist.fills) == 3)
check("fill ids carry the file line", buy.meta["row"] == 5 and buy.fill_id == "csv-000005",
      f"{buy.fill_id} {buy.meta}")
check("quote asset resolved", buy.meta.get("quote_asset") == "USDT", str(buy.meta))

print("\n=== futures export ===")
fut = parse_csv(FIXTURES / "binance_futures.csv")
check("2 fills parsed", len(fut.fills) == 2, f"got {len(fut.fills)}")
f_open, f_close = fut.fills
check("futures venue", all(f.venue is Venue.FUTURES_USDS for f in fut.fills),
      str([f.venue for f in fut.fills]))
check("leverage 20x parsed", f_open.leverage == 20.0, f"got {f_open.leverage}")
check("margin mode kept", f_open.meta.get("margin_mode") == "Cross", str(f_open.meta))
check("realized profit kept, not used", f_close.meta.get("realized_profit") == 200.0,
      str(f_close.meta))
check("futures dialect recorded", f_open.meta.get("dialect") == "futures", str(f_open.meta))

print("\n=== dialects are detected, not assumed ===")
from gnosis.ingest.binance_csv import detect_dialect  # noqa: E402

check("spot header -> spot",
      detect_dialect(["Date(UTC)", "Pair", "Side", "Price", "Executed", "Amount", "Fee"]) == "spot")
check("futures header -> futures",
      detect_dialect(["Date(UTC)", "Symbol", "Side", "Price", "Quantity", "Amount", "Fee",
                      "Realized Profit", "Leverage"]) == "futures")
err = raises(CsvFormatError, parse_text, "Date(UTC),Wallet,Widget,Frobnicator\n2026-01-01,a,b,c\n")
check("unknown header raises", isinstance(err, CsvFormatError), repr(err))
check("error names the unrecognised columns",
      isinstance(err, CsvFormatError) and "Widget" in str(err) and "Frobnicator" in str(err),
      str(err))
check("error names both expected layouts",
      isinstance(err, CsvFormatError) and "spot" in str(err) and "futures" in str(err), str(err))
check("empty file raises", isinstance(raises(CsvFormatError, parse_text, ""), CsvFormatError))
check("header alone is an empty history", len(parse_text(SPOT_HEADER).fills) == 0)

print("\n=== a row we cannot read raises, naming the line ===")
bad_price = raises(CsvFormatError, spot_row, "2026-03-01 12:00:00,ETHUSDT,BUY,wat,2ETH,6400USDT,1USDT")
check("bad price raises", isinstance(bad_price, CsvFormatError), repr(bad_price))
check("bad price names row 2", getattr(bad_price, "row", None) == 2, str(bad_price))
check("bad price quotes the value", "'wat'" in str(bad_price), str(bad_price))

bad_side = raises(CsvFormatError, spot_row, "2026-03-01 12:00:00,ETHUSDT,HODL,3200,2ETH,6400USDT,1USDT")
check("bad side raises", isinstance(bad_side, CsvFormatError), repr(bad_side))
check("bad side quotes the value", "'HODL'" in str(bad_side), str(bad_side))

bad_ts = raises(CsvFormatError, spot_row, "last tuesday,ETHUSDT,BUY,3200,2ETH,6400USDT,1USDT")
check("bad timestamp raises", isinstance(bad_ts, CsvFormatError), repr(bad_ts))
check("bad timestamp names row 2", getattr(bad_ts, "row", None) == 2, str(bad_ts))

zero_qty = raises(CsvFormatError, spot_row, "2026-03-01 12:00:00,ETHUSDT,BUY,3200,0ETH,0USDT,1USDT")
check("zero quantity raises", isinstance(zero_qty, CsvFormatError), repr(zero_qty))
check("zero quantity names row 2", getattr(zero_qty, "row", None) == 2, str(zero_qty))

mismatch = raises(CsvFormatError, spot_row, "2026-03-01 12:00:00,ETHUSDT,BUY,3200,2ETH,100USDT,1USDT")
check("amount vs price x qty mismatch raises", isinstance(mismatch, CsvFormatError), repr(mismatch))

# The point of all of the above: nothing was skipped to get there.
survivors = parse_text(SPOT_HEADER + "2026-03-01 12:00:00,ETHUSDT,BUY,3200,2ETH,6400USDT,1USDT\n")
check("a good row still parses", len(survivors.fills) == 1)

print("\n=== fees in a third asset are refused, not zeroed ===")
bnb_row = "2026-03-01 12:00:00,ETHUSDT,BUY,3200,2ETH,6400USDT,0.01BNB"
bnb = raises(CsvFormatError, spot_row, bnb_row)
check("BNB fee without a price raises", isinstance(bnb, CsvFormatError), repr(bnb))
check("error says which asset and how to fix it",
      isinstance(bnb, CsvFormatError) and "BNB" in str(bnb) and "fee_prices" in str(bnb), str(bnb))
priced = spot_row(bnb_row, fee_prices={"BNB": 600.0})
check("BNB fee converts when priced", abs(priced.fills[0].fee - 6.0) < 1e-9,
      f"got {priced.fills[0].fee}")
check("raw fee amount survives in meta", priced.fills[0].meta.get("fee_amount") == 0.01,
      str(priced.fills[0].meta))
thousands = spot_row('2026-03-01 12:00:00,ETHUSDT,BUY,3200,0.5ETH,"1,600USDT",1USDT')
check("thousands separator in amount", thousands.fills[0].meta["quote_amount"] == 1600.0,
      str(thousands.fills[0].meta))

print("\n=== CSV -> round-trips ===")
closed, still_open = reconstruct(parse_csv(FIXTURES / "binance_spot.csv").fills)
check("one closed ETH trip", len(closed) == 1, f"got {len(closed)}")
check("the BTC buy is left open, not counted", len(still_open) == 1 and still_open[0].is_open)
trip = closed[0]
check("gross pnl 200", abs(trip.gross_pnl - 200.0) < 1e-9, f"got {trip.gross_pnl}")
check("fees 13.0 (6.40 in ETH + 6.60 in USDT)", abs(trip.fees - 13.0) < 1e-9, f"got {trip.fees}")
check("net pnl 187", abs(trip.net_pnl - 187.0) < 1e-9, f"got {trip.net_pnl}")
check("hold time 21.5h", abs(trip.hold_hours - 21.5) < 1e-9, f"got {trip.hold_hours}")
check("receipts trace back to file lines", trip.entry_fill_ids == ["csv-000005"],
      str(trip.entry_fill_ids))

fclosed, _ = reconstruct(parse_csv(FIXTURES / "binance_futures.csv").fills)
check("futures trip closes", len(fclosed) == 1, f"got {len(fclosed)}")
check("futures gross pnl 200", abs(fclosed[0].gross_pnl - 200.0) < 1e-9,
      f"got {fclosed[0].gross_pnl}")
check("leverage reaches the round-trip", fclosed[0].max_leverage == 20.0,
      f"got {fclosed[0].max_leverage}")

# --------------------------------------------------------------------------
# `baw`. Every payload below is canned: the CLI needs a phone confirmation and
# cannot be part of an automated suite.

CONNECTED = {"status": "CONNECTED", "address": "0xab12cd34ef56ab78cd90ef12ab34cd56ef7890ab"}


def fake_baw(status=None, orders=(), txs=()):
    """A runner that replays canned JSON, dispatching on the subcommand."""
    payloads = {
        "wallet status": status if status is not None else CONNECTED,
        "market-order list": {"orders": list(orders)},
        "wallet tx-history": {"transactions": list(txs)},
    }

    def run(argv):
        key = " ".join(a for a in argv if not a.startswith("--"))
        if key not in payloads:
            raise AssertionError(f"unexpected baw invocation: {argv}")
        return json.dumps(payloads[key])

    return run


def swap(oid, status, when, frm, frm_amt, to, to_amt, **extra):
    order = {
        "orderId": oid, "status": status, "createTime": when,
        "fromToken": frm, "fromAmount": str(frm_amt),
        "toToken": to, "toAmount": str(to_amt),
        "txHash": f"0x{oid}", "chain": "BSC",
    }
    order.update(extra)
    return order


T_BUY = 1_772_000_000_000     # ms epoch
T_SELL = T_BUY + 6 * 3_600_000

print("\n=== a swap is a fill ===")
h = baw.load(runner=fake_baw(orders=[
    swap("a1", "FINISHED", T_BUY, "USDT", 1000, "CAKE", 500, gasFeeUsd=0.42),
]))
check("one fill", len(h.fills) == 1, f"got {len(h.fills)}")
f = h.fills[0]
check("USDT->CAKE is a BUY of CAKE", f.side is Side.BUY and f.symbol == "CAKEUSDT",
      f"{f.side} {f.symbol}")
check("qty is the token received", abs(f.qty - 500) < 1e-9, f"got {f.qty}")
check("price is quote per base", abs(f.price - 2.0) < 1e-9, f"got {f.price}")
check("venue onchain", f.venue is Venue.ONCHAIN)
check("leverage is None, not 1.0", f.leverage is None, f"got {f.leverage}")
check("gas is the fee", abs(f.fee - 0.42) < 1e-9, f"got {f.fee}")
check("tx hash kept in meta", f.meta.get("tx_hash") == "0xa1", str(f.meta))
check("source names the wallet", h.source.startswith("baw:0xab12cd"), h.source)
check("epoch ms became UTC-aware", f.ts.tzinfo is not None and f.ts.year == 2026, str(f.ts))

h = baw.load(runner=fake_baw(orders=[
    swap("a2", "FINISHED", T_SELL, "CAKE", 500, "USDT", 1150, gasFeeUsd=0.40),
]))
check("CAKE->USDT is a SELL of CAKE",
      h.fills[0].side is Side.SELL and h.fills[0].symbol == "CAKEUSDT",
      f"{h.fills[0].side} {h.fills[0].symbol}")
check("sell price is quote per base", abs(h.fills[0].price - 2.3) < 1e-9, f"got {h.fills[0].price}")

print("\n=== only FINISHED orders are fills ===")
h = baw.load(runner=fake_baw(orders=[
    swap("f1", "FINISHED", T_BUY, "USDT", 1000, "CAKE", 500, gasFeeUsd=0.42),
    swap("f2", "FAILED", T_BUY + 60_000, "USDT", 900, "CAKE", 440, gasFeeUsd=0.31),
    swap("f3", "PENDING", T_BUY + 120_000, "USDT", 800, "CAKE", 390, gasFeeUsd=0.0),
]))
check("only the finished swap is a fill", len(h.fills) == 1 and h.fills[0].meta["order_id"] == "f1",
      f"got {[f.meta['order_id'] for f in h.fills]}")
check("failed swap produced a cash flow", len(h.flows) == 1, f"got {len(h.flows)}")
flow = h.flows[0]
check("failed swap gas is kind 'gas'", flow.kind == "gas", flow.kind)
check("failed swap gas is a cost (negative)", abs(flow.amount + 0.31) < 1e-9, f"got {flow.amount}")
check("failed swap keeps its status", flow.meta.get("status") == "FAILED", str(flow.meta))
check("failed swap names the pair it aimed at", flow.symbol == "CAKEUSDT", flow.symbol)
check("pending swap is neither fill nor flow",
      all(fl.meta.get("order_id") != "f3" for fl in h.flows))
unknown = raises(baw.BawError, baw.load,
                 runner=fake_baw(orders=[swap("x", "QUANTUM", T_BUY, "USDT", 10, "CAKE", 5)]))
check("unknown status raises rather than guessing", isinstance(unknown, baw.BawError), repr(unknown))
check("unknown status is named", isinstance(unknown, baw.BawError) and "QUANTUM" in str(unknown),
      str(unknown))

print("\n=== gas ===")
h = baw.load(
    runner=fake_baw(orders=[
        swap("g1", "FINISHED", T_BUY, "USDT", 1000, "CAKE", 500,
             gasFee="0.0012", gasAsset="BNB"),
    ]),
    fee_prices={"BNB": 600.0},
)
check("native-token gas converts when priced", abs(h.fills[0].fee - 0.72) < 1e-9,
      f"got {h.fills[0].fee}")
check("raw gas survives in meta",
      h.fills[0].meta.get("gas_asset") == "BNB" and h.fills[0].meta.get("gas_amount") == 0.0012,
      str(h.fills[0].meta))
unpriced = raises(baw.BawError, baw.load, runner=fake_baw(orders=[
    swap("g2", "FINISHED", T_BUY, "USDT", 1000, "CAKE", 500, gasFee="0.0012", gasAsset="BNB"),
]))
check("unpriced gas raises rather than counting zero", isinstance(unpriced, baw.BawError),
      repr(unpriced))
check("unpriced gas error says how to fix it",
      isinstance(unpriced, baw.BawError) and "fee_prices" in str(unpriced), str(unpriced))

h = baw.load(runner=fake_baw(
    orders=[{"orderId": "t1", "status": "FINISHED", "createTime": T_BUY,
             "fromToken": "USDT", "fromAmount": "1000", "toToken": "CAKE", "toAmount": "500",
             "txHash": "0xdeadbeef"}],
    txs=[{"hash": "0xdeadbeef", "time": T_BUY, "gasFeeUsd": 0.19, "method": "swap"},
         {"hash": "0xfeed", "time": T_BUY - 1000, "gasFeeUsd": 0.07, "method": "approve"}],
))
check("gas backfilled from tx-history by hash", abs(h.fills[0].fee - 0.19) < 1e-9,
      f"got {h.fills[0].fee}")
check("the swap's own tx is not double-counted as a flow", len(h.flows) == 1, f"got {len(h.flows)}")
check("a non-order tx (approval) still costs gas",
      h.flows[0].meta.get("method") == "approve" and abs(h.flows[0].amount + 0.07) < 1e-9,
      str(h.flows[0]))

print("\n=== an unconnected wallet is a named failure ===")
unc = raises(baw.WalletNotConnected, baw.load, runner=fake_baw(status={"status": "UNCONNECTED"}))
check("UNCONNECTED raises WalletNotConnected", isinstance(unc, baw.WalletNotConnected), repr(unc))
check("message says a phone confirmation is needed",
      isinstance(unc, baw.WalletNotConnected) and "phone" in str(unc), str(unc))
check("bare-string status is handled too",
      isinstance(raises(baw.WalletNotConnected, baw.load, runner=fake_baw(status="UNCONNECTED")),
                 baw.WalletNotConnected))
not_json = raises(baw.BawError, baw.load, runner=lambda argv: "Please confirm on your phone...")
check("non-JSON output raises with what it saw",
      isinstance(not_json, baw.BawError) and "phone" in str(not_json), repr(not_json))

print("\n=== baw -> round-trips ===")
h = baw.load(runner=fake_baw(orders=[
    swap("r1", "FINISHED", T_BUY, "USDT", 1000, "CAKE", 500, gasFeeUsd=0.42),
    swap("r2", "FAILED", T_BUY + 60_000, "USDT", 500, "CAKE", 240, gasFeeUsd=0.31),
    swap("r3", "FINISHED", T_SELL, "CAKE", 500, "USDT", 1150, gasFeeUsd=0.40),
]))
closed, still_open = reconstruct(h.fills)
check("one closed on-chain trip", len(closed) == 1 and not still_open, f"got {len(closed)}")
trip = closed[0]
check("bought at 2.00, sold at 2.30", abs(trip.avg_entry - 2.0) < 1e-9
      and abs(trip.avg_exit - 2.3) < 1e-9, f"{trip.avg_entry} {trip.avg_exit}")
check("gross pnl 150", abs(trip.gross_pnl - 150.0) < 1e-9, f"got {trip.gross_pnl}")
check("gas of both legs is the fee", abs(trip.fees - 0.82) < 1e-9, f"got {trip.fees}")
check("failed swap did not fake an add", trip.n_adds == 0, f"got {trip.n_adds}")
check("held 6 hours", abs(trip.hold_hours - 6.0) < 1e-9, f"got {trip.hold_hours}")
check("no leverage on an on-chain trip", trip.max_leverage is None, f"got {trip.max_leverage}")

print("\n=== both venues in one history ===")
merged = parse_csv(FIXTURES / "binance_spot.csv").fills + \
    parse_csv(FIXTURES / "binance_futures.csv").fills + h.fills
closed, still_open = reconstruct(merged)
check("three closed trips across three venues", len(closed) == 3, f"got {len(closed)}")
check("venues stay separate",
      {t.venue for t in closed} == {Venue.SPOT, Venue.FUTURES_USDS, Venue.ONCHAIN},
      str({t.venue for t in closed}))
check("BTC appears on two venues without netting",
      len([t for t in closed if t.symbol == "BTCUSDT"]) == 1 and len(still_open) == 1,
      f"{[t.symbol for t in closed]} open={len(still_open)}")
check("every trip has a positive notional", all(t.notional > 0 for t in closed))
check("every trip has receipts", all(t.entry_fill_ids and t.exit_fill_ids for t in closed))
check("hold times are non-negative", all(t.hold_hours >= 0 for t in closed))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
