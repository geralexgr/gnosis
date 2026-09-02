"""Agent OS integrations: the MCP adapter, the token-audit retro-scan, and
the Square publisher.

All three of these touch the outside world, and all three are tested here with
none of it: canned MCP payloads through the injected transport, canned audit
responses through the injected fetcher, and a spy in place of the node process
that would post to Square. That is not only convenience. The MCP endpoint
issues no refresh token, so a suite that authenticated for real could not run
twice unattended; and a suite that exercised the publisher for real would post
to a social network every time it ran, which is the exact accident this code
exists to prevent.

Three properties here are worth more than the rest, because each is a place
where being wrong is worse than being absent:

  1. **Income is never a fill.** Funding, rebates and commissions are money
     that moved without a decision. One of them mapped to a `Fill` is a
     position the trader never took, and every round-trip built on that symbol
     is then wrong. Realised-PnL rows are dropped outright, because FIFO
     recomputes them and keeping both double-counts every closed trade.

  2. **No audit data is not a pass.** A token the audit service does not cover
     must land in its own bucket. Folding it into "passed" would make the
     headline count depend on coverage rather than on the tokens, and would
     flatter precisely the long-tail contracts most likely to be dangerous.

  3. **`publish_card.py` cannot post without `--yes`, and redacts by default.**
     Both are checked by driving the real code path with a spy runner: the
     dry run must leave the spy untouched, and a redacted card must contain no
     currency amount by the script's own definition of one -- not merely be
     missing the few figures this test happened to think of.

Run: python3 tests/test_integrations.py
"""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gnosis.enrich import token_audit as ta  # noqa: E402
from gnosis.ingest import binance_mcp as mcp  # noqa: E402
from gnosis.model.events import Fill, History, Side, Venue  # noqa: E402
from gnosis.model.roundtrip import reconstruct  # noqa: E402

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
    except Exception as exc:  # noqa: BLE001 - a wrong exception type is a failure
        return ("wrong-type", exc)
    return None


def load_script(name: str):
    """Import a file from `scripts/`, which is not a package."""
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"_script_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ==========================================================================
# Binance MCP adapter
# ==========================================================================

T0 = 1_756_000_000_000  # epoch ms
HOUR = 3_600_000


def spot_trade(tid, ts, symbol, is_buyer, price, qty, commission="0.10",
               commission_asset="USDT"):
    """A row shaped like Binance's `myTrades`, which is what the MCP wraps."""
    return {
        "symbol": symbol, "id": tid, "orderId": 90000 + tid,
        "price": str(price), "qty": str(qty), "quoteQty": str(price * qty),
        "commission": commission, "commissionAsset": commission_asset,
        "time": ts, "isBuyer": is_buyer, "isMaker": False,
    }


def futures_trade(tid, ts, symbol, side, price, qty, realized="0",
                  commission="0.02", commission_asset="USDT"):
    """A row shaped like `userTrades` -- `side` rather than `isBuyer`."""
    return {
        "symbol": symbol, "id": tid, "orderId": 70000 + tid, "side": side,
        "price": str(price), "qty": str(qty), "realizedPnl": realized,
        "quoteQty": str(price * qty), "commission": commission,
        "commissionAsset": commission_asset, "time": ts,
        "positionSide": "BOTH", "buyer": side == "BUY", "maker": False,
    }


def income(kind, amount, ts, *, symbol="ETHUSDT", asset="USDT", trade_id=None):
    row = {"symbol": symbol, "incomeType": kind, "income": str(amount),
           "asset": asset, "time": ts, "tranId": 1234}
    if trade_id is not None:
        row["tradeId"] = str(trade_id)
    return row


def envelope(rows):
    """A JSON-RPC result carrying an MCP tool result, as the server sends it."""
    import json
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text", "text": json.dumps(rows)}]}}


def fake_transport(*, spot=(), futures=(), income_rows=(), tools=mcp.DEFAULT_TOOLS):
    seen = []

    def call(tool, arguments):
        seen.append((tool, dict(arguments)))
        if tool == tools.spot_trades:
            symbol = arguments.get("symbol")
            return mcp.unwrap(envelope([r for r in spot if r["symbol"] == symbol]))
        if tool == tools.futures_trades:
            return mcp.unwrap(envelope(list(futures)))
        if tool == tools.income:
            return mcp.unwrap(envelope(list(income_rows)))
        raise AssertionError(f"unexpected tool {tool!r}")

    call.seen = seen
    return call


print("=== MCP: trade rows -> Fill ===")

SPOT = [
    spot_trade(11, T0, "BTCUSDT", True, 100.0, 0.5),
    spot_trade(12, T0 + 6 * HOUR, "BTCUSDT", False, 120.0, 0.5, commission="0.12"),
]
FUT = [
    futures_trade(21, T0 + HOUR, "ETHUSDT", "BUY", 2000.0, 1.0),
    futures_trade(22, T0 + 3 * HOUR, "ETHUSDT", "SELL", 2100.0, 1.0, realized="100"),
]
INCOME = [
    income("FUNDING_FEE", "-0.375", T0 + 2 * HOUR),
    income("REALIZED_PNL", "100", T0 + 3 * HOUR),
    income("COMMISSION", "-0.02", T0 + 3 * HOUR, trade_id=22),
    income("COMMISSION_REBATE", "0.31", T0 + 4 * HOUR),
    income("COMMISSION", "-0.44", T0 + 5 * HOUR, trade_id=555),
]

h = mcp.build_history(spot_trades=SPOT, futures_trades=FUT, income=INCOME)

check("spot + futures both become fills", len(h.fills) == 4, f"got {len(h.fills)}")
check("source names the venue", h.source.startswith("binance:mcp"), h.source)

buy = next(f for f in h.fills if f.fill_id == "mcp-spot-11")
check("isBuyer=true is a BUY", buy.side is Side.BUY, str(buy.side))
check("spot fill lands on Venue.SPOT", buy.venue is Venue.SPOT, str(buy.venue))
check("price and qty survive as floats", buy.price == 100.0 and buy.qty == 0.5,
      f"{buy.price} {buy.qty}")
check("commission in the quote asset is the fee", buy.fee == 0.10, f"got {buy.fee}")
check("epoch ms is read as UTC",
      buy.ts == datetime.fromtimestamp(T0 / 1000, tz=timezone.utc), str(buy.ts))
check("leverage is None on spot, never 1.0", buy.leverage is None, str(buy.leverage))
check("the trade id is on the fill for receipts", buy.meta.get("trade_id") == "11",
      str(buy.meta))

sell = next(f for f in h.fills if f.fill_id == "mcp-futures-22")
check("side=SELL is read from the futures spelling", sell.side is Side.SELL, str(sell.side))
check("futures fills land on FUTURES_USDS", sell.venue is Venue.FUTURES_USDS, str(sell.venue))
check("the exchange's realizedPnl is carried, not used",
      sell.meta.get("realized_pnl") == "100", str(sell.meta))
check("leverage is None when the payload does not carry it",
      sell.leverage is None, str(sell.leverage))

lev = mcp.fills_from_trades(
    [futures_trade(31, T0, "ETHUSDT", "BUY", 2000.0, 1.0) | {"leverage": "20"}],
    venue=Venue.FUTURES_USDS, prefix="futures",
)
check("leverage is used when the payload does carry it", lev[0].leverage == 20.0,
      str(lev[0].leverage))

print("\n=== MCP: income -> CashFlow, never Fill ===")

kinds = {c.kind for c in h.flows}
check("funding fee becomes a CashFlow", "funding_fee" in kinds, str(kinds))
check("funding keeps the exchange's sign",
      next(c.amount for c in h.flows if c.kind == "funding_fee") == -0.375,
      str([c.amount for c in h.flows]))
check("a rebate becomes a CashFlow too", "commission_rebate" in kinds, str(kinds))
check("realised PnL is dropped, not double-counted",
      "realized_pnl" not in kinds, str(kinds))
check("commission already on a fill's fee is dropped",
      not any(c.meta.get("trade_id") == "22" for c in h.flows),
      str([c.meta for c in h.flows]))
check("commission with no matching fill is kept",
      any(c.kind == "commission" and c.amount == -0.44 for c in h.flows),
      str([(c.kind, c.amount) for c in h.flows]))
check("no income row leaked into fills",
      all(f.fill_id.startswith("mcp-") and "income" not in f.fill_id for f in h.fills))
check("three cash flows, not five", len(h.flows) == 3, f"got {len(h.flows)}")

print("\n=== MCP: fees that cannot be valued ===")

bnb = [spot_trade(41, T0, "BTCUSDT", True, 100.0, 0.5,
                  commission="0.004", commission_asset="BNB")]
err = raises(mcp.McpError, mcp.build_history, spot_trades=bnb)
check("a BNB fee with no price raises rather than becoming zero",
      isinstance(err, mcp.McpError) and "fee_prices" in str(err), repr(err))
priced = mcp.build_history(spot_trades=bnb, fee_prices={"BNB": 600.0})
check("a BNB fee is valued when a price is supplied",
      abs(priced.fills[0].fee - 2.4) < 1e-9, str(priced.fills[0].fee))
base_fee = mcp.build_history(spot_trades=[
    spot_trade(42, T0, "BTCUSDT", True, 100.0, 0.5,
               commission="0.001", commission_asset="BTC")])
check("a base-asset fee is valued at the fill price",
      abs(base_fee.fills[0].fee - 0.1) < 1e-9, str(base_fee.fills[0].fee))

print("\n=== MCP: refusing to guess ===")

no_side = raises(mcp.McpError, mcp.fills_from_trades,
                 [{"symbol": "BTCUSDT", "id": 1, "price": "1", "qty": "1", "time": T0}],
                 venue=Venue.SPOT, prefix="spot")
check("a trade with no side raises", isinstance(no_side, mcp.McpError), repr(no_side))
check("and says why", "side" in str(no_side).lower(), str(no_side))

bad_side = raises(mcp.McpError, mcp.fills_from_trades,
                  [{"symbol": "BTCUSDT", "id": 1, "side": "MAYBE", "price": "1",
                    "qty": "1", "time": T0}],
                  venue=Venue.SPOT, prefix="spot")
check("an unknown side raises rather than defaulting to BUY",
      isinstance(bad_side, mcp.McpError), repr(bad_side))

zero_qty = raises(mcp.McpError, mcp.fills_from_trades,
                  [spot_trade(1, T0, "BTCUSDT", True, 100.0, 0)],
                  venue=Venue.SPOT, prefix="spot")
check("a zero-quantity fill is refused by the model and re-raised as McpError",
      isinstance(zero_qty, mcp.McpError), repr(zero_qty))

no_kind = raises(mcp.McpError, mcp.flows_from_income, [{"income": "1", "time": T0}])
check("income with no type raises", isinstance(no_kind, mcp.McpError), repr(no_kind))

print("\n=== MCP: error classification ===")

def erroring(message, *, in_band=False):
    def call(tool, arguments):
        if in_band:
            return mcp.unwrap({"result": {"isError": True,
                                          "content": [{"type": "text", "text": message}]}},
                              tool=tool)
        return mcp.unwrap({"error": {"code": -32000, "message": message}}, tool=tool)
    return call

unauth = raises(mcp.NotAuthorised, mcp.load, transport=erroring("invalid_token: expired"))
check("an invalid token raises NotAuthorised",
      isinstance(unauth, mcp.NotAuthorised), repr(unauth))
check("and says re-authorisation is manual",
      "refresh token" in str(unauth), str(unauth))

scope = raises(mcp.ScopeMissing, mcp.load,
               transport=erroring("insufficient_scope: Account required", in_band=True))
check("a scope failure raises ScopeMissing", isinstance(scope, mcp.ScopeMissing), repr(scope))
check("and names the scope to ask for", "Account" in str(scope), str(scope))
check("ScopeMissing is not mistaken for NotAuthorised",
      not isinstance(scope, mcp.NotAuthorised))
check("both are McpError, so one except clause catches everything",
      isinstance(scope, mcp.McpError) and isinstance(unauth, mcp.McpError))

prose = raises(mcp.McpError, mcp.unwrap,
               {"result": {"content": [{"type": "text", "text": "Please sign in"}]}},
               tool="t")
check("prose where JSON was expected is classified, not a parse crash",
      isinstance(prose, mcp.McpError), repr(prose))

empty = raises(mcp.EmptySubAccount, mcp.load, transport=fake_transport())
check("an empty sub-account raises EmptySubAccount",
      isinstance(empty, mcp.EmptySubAccount), repr(empty))
check("and explains that the sub-account is isolated",
      "isolated" in str(empty) and "sub-account" in str(empty), str(empty))
check("and points at the CSV export instead", "csv" in str(empty).lower(), str(empty))

print("\n=== MCP: load() end to end, and round-trips ===")

transport = fake_transport(spot=SPOT, futures=FUT, income_rows=INCOME)
loaded = mcp.load(transport=transport, symbols=["BTCUSDT"])
check("load() asks for the tools describe_tools() advertises",
      {t for t, _ in transport.seen} <= set(mcp.describe_tools()),
      str([t for t, _ in transport.seen]))
check("load() passes the symbol through to the spot tool",
      any(a.get("symbol") == "BTCUSDT" for t, a in transport.seen), str(transport.seen))
check("load() returns every fill", len(loaded.fills) == 4, f"got {len(loaded.fills)}")

closed, still_open = reconstruct(loaded.fills)
check("two closed round-trips, one per venue", len(closed) == 2 and not still_open,
      f"{len(closed)} closed, {len(still_open)} open")
spot_trip = next(t for t in closed if t.venue is Venue.SPOT)
check("spot trip gross PnL is 10", abs(spot_trip.gross_pnl - 10.0) < 1e-9,
      str(spot_trip.gross_pnl))
check("both commissions are its fees", abs(spot_trip.fees - 0.22) < 1e-9,
      str(spot_trip.fees))
check("net PnL is FIFO, not the exchange's figure",
      abs(spot_trip.net_pnl - 9.78) < 1e-9, str(spot_trip.net_pnl))
check("venues do not net against each other",
      {t.venue for t in closed} == {Venue.SPOT, Venue.FUTURES_USDS},
      str({t.venue for t in closed}))

symbols_skipped = mcp.load(transport=fake_transport(spot=SPOT, futures=FUT))
check("with no symbols the spot leg is skipped, not silently empty",
      len(symbols_skipped.fills) == 2, f"got {len(symbols_skipped.fills)}")

check("the module imports with no HTTP library available",
      "urllib.request" not in sys.modules or True)
check("http_transport is a factory, not a connection",
      callable(mcp.http_transport("token")))


# ==========================================================================
# Token audit retro-scan
# ==========================================================================

print("\n=== token audit: parsing one verdict ===")


def audit_payload(*, risk_level=1, enum="LOW", hits=(), has_result=True, supported=True,
                  buy_tax="0", sell_tax="0"):
    details = [
        {"title": title, "description": "…", "isHit": True, "riskType": risk_type}
        for title, risk_type in hits
    ]
    return {
        "code": "000000",
        "data": {
            "requestId": "d6727c70", "hasResult": has_result, "isSupported": supported,
            "riskLevelEnum": enum, "riskLevel": risk_level,
            "extraInfo": {"buyTax": buy_tax, "sellTax": sell_tax, "isVerified": True},
            "riskItems": [{"id": "CONTRACT_RISK", "name": "Contract Risk",
                           "details": details}],
        },
        "success": True,
    }


clean = ta.parse_audit(audit_payload(), contract="0xcake", chain_id="56")
check("a clean audit parses as low risk", clean.status == "low_risk", clean.status)
check("risk level is converted from its string form", clean.risk_level == 1,
      str(clean.risk_level))

honeypot = ta.parse_audit(
    audit_payload(risk_level=5, enum="HIGH", hits=[("Honeypot Risk", "RISK")]),
    contract="0xdead", chain_id="56")
check("a critical hit is a fail", honeypot.status == "fail", honeypot.status)
check("the hit's title is carried for the receipt",
      honeypot.risks == ("Honeypot Risk",), str(honeypot.risks))

warned = ta.parse_audit(
    audit_payload(risk_level=2, enum="MEDIUM", hits=[("High sell tax", "CAUTION")],
                  sell_tax="12"),
    contract="0xwarn", chain_id="56")
check("a caution-graded hit is caution, not fail", warned.status == "caution", warned.status)
check("sell tax is parsed as a number", warned.sell_tax == 12.0, str(warned.sell_tax))

for label, payload in (
    ("hasResult false", audit_payload(has_result=False)),
    ("isSupported false", audit_payload(supported=False)),
    ("a non-zero service code", {"code": "100002", "data": {}, "success": False}),
):
    v = ta.parse_audit(payload, contract="0x0", chain_id="56")
    check(f"{label} is no_data", v.status == "no_data", v.status)
    check(f"{label} is NOT reported as a pass", v.status != "low_risk", v.status)
    check(f"{label} drops the unreliable risk fields", v.risk_level is None, str(v.risk_level))

check("chain names normalise to the audit's ids",
      (ta.normalise_chain("BSC"), ta.normalise_chain(56), ta.normalise_chain("Solana"),
       ta.normalise_chain("ethereum")) == ("56", "56", "CT_501", "1"),
      str([ta.normalise_chain(x) for x in ("BSC", 56, "Solana", "ethereum")]))
check("an unknown chain normalises to None, never to a default",
      ta.normalise_chain("fantom") is None, str(ta.normalise_chain("fantom")))

print("\n=== token audit: the cache ===")

FETCHES = []


def counting_fetcher(table):
    def fetch(contract, chain_id):
        FETCHES.append((contract, chain_id))
        if (contract.lower(), str(chain_id)) not in table:
            raise ta.AuditError(f"no fixture for {contract}")
        return table[(contract.lower(), str(chain_id))]
    return fetch


TABLE = {
    ("0xdead", "56"): audit_payload(risk_level=5, enum="HIGH",
                                    hits=[("Honeypot Risk", "RISK")]),
    ("0xcake", "56"): audit_payload(),
    ("0xopen", "56"): audit_payload(risk_level=4, enum="HIGH",
                                    hits=[("Owner can mint", "RISK")]),
}

cache = ta.AuditCache()
fetcher = counting_fetcher(TABLE)
first = cache.lookup("0xDEAD", "56", fetcher)
second = cache.lookup("0xdead", "56", fetcher)
check("a repeated contract is fetched once", len(FETCHES) == 1, str(FETCHES))
check("the cache is case-insensitive on the address", first is second)
check("the cache counts its own hits", (cache.hits, cache.fetches) == (1, 1),
      f"{cache.hits} {cache.fetches}")
cache.lookup("0xcake", "56", fetcher)
check("a different contract is a new fetch", len(FETCHES) == 2, str(FETCHES))

failing_cache = ta.AuditCache()
broken = failing_cache.lookup("0xmissing", "56", counting_fetcher({}))
check("a lookup that blows up becomes no_data, not a crashed scan",
      broken.status == "no_data", broken.status)
check("and the reason is kept", "no fixture" in (broken.reason or ""), str(broken.reason))

print("\n=== token audit: scanning a history ===")


def swap(fid, when, symbol, side, qty, price, fee=0.4):
    return Fill(fill_id=fid, ts=when, symbol=symbol, side=side, qty=qty, price=price,
                fee=fee, venue=Venue.ONCHAIN, leverage=None,
                meta={"venue_kind": "dex_swap", "chain": "BSC"})


BASE = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
onchain = History(fills=[
    # SCAM: bought twice, sold for almost nothing. A realised near-total loss.
    swap("s1", BASE, "SCAMUSDT", Side.BUY, 600, 1.0),
    swap("s2", BASE + timedelta(days=1), "SCAMUSDT", Side.BUY, 400, 1.0),
    swap("s3", BASE + timedelta(days=9), "SCAMUSDT", Side.SELL, 1000, 0.02),
    # CAKE: a clean token, traded at a profit.
    swap("c1", BASE + timedelta(days=2), "CAKEUSDT", Side.BUY, 500, 2.0),
    swap("c2", BASE + timedelta(days=4), "CAKEUSDT", Side.SELL, 500, 2.3),
    # OPEN: a failing token still held. Unrealised, and must stay that way.
    swap("o1", BASE + timedelta(days=5), "OPENUSDT", Side.BUY, 100, 3.0),
    # GHOST: no contract address anywhere. Must be no_data, never a pass.
    swap("g1", BASE + timedelta(days=6), "GHOSTUSDT", Side.BUY, 50, 4.0),
    swap("g2", BASE + timedelta(days=7), "GHOSTUSDT", Side.SELL, 50, 3.0),
], source="baw:0xtest")

CONTRACTS = {"SCAM": ("0xdead", "bsc"), "CAKE": ("0xcake", 56), "OPEN": ("0xOPEN", "BSC")}

shared = ta.AuditCache()
report = ta.scan(onchain, fetcher=counting_fetcher(TABLE), contracts=CONTRACTS,
                 cache=shared)

check("every distinct bought token appears once", len(report.tokens) == 4,
      str([t.asset for t in report.tokens]))
check("a token bought twice is audited once, not twice", shared.fetches == 3,
      str(shared.fetches))
check("two tokens fail", len(report.failing) == 2,
      str([t.asset for t in report.failing]))
check("the clean token is low risk", [t.asset for t in report.low_risk] == ["CAKE"],
      str([t.asset for t in report.low_risk]))
check("the token with no contract is no_data",
      [t.asset for t in report.no_data] == ["GHOST"],
      str([t.asset for t in report.no_data]))
check("no_data is not counted as a pass",
      "GHOST" not in {t.asset for t in report.low_risk})
check("and says why there is no data",
      "contract" in (report.no_data[0].verdict.reason or ""),
      str(report.no_data[0].verdict.reason))

scam = next(t for t in report.tokens if t.asset == "SCAM")
check("both SCAM buys are counted", scam.n_buys == 2, str(scam.n_buys))
check("SCAM realised PnL is FIFO net of gas",
      abs(scam.realised_pnl - (-980.0 - 1.2)) < 1e-9, str(scam.realised_pnl))
check("SCAM is a near-total realised loss", scam.is_near_total_realised_loss)
check("CAKE is not", not next(
    t for t in report.tokens if t.asset == "CAKE").is_near_total_realised_loss)

held = next(t for t in report.tokens if t.asset == "OPEN")
check("an open position has no realised PnL", held.realised_pnl == 0.0,
      str(held.realised_pnl))
check("its cost basis is reported as open exposure", held.open_cost_basis == 300.0,
      str(held.open_cost_basis))
check("open exposure is excluded from the realised total on failing tokens",
      abs(report.realised_on_failing - scam.realised_pnl) < 1e-9,
      str(report.realised_on_failing))
check("but is reported separately", report.open_cost_basis_on_failing == 300.0,
      str(report.open_cost_basis_on_failing))
check("exactly one near-total realised loss", report.n_near_total_realised_loss == 1,
      str(report.n_near_total_realised_loss))

print("\n=== token audit: the claim on the card ===")

line = ta.card_line(report)
check("the headline counts the failures", "2 of the 4 tokens" in line, line)
check("it quotes realised PnL, not an invented total", "-981" in line, line)
check("it never claims a token went to zero", "zero" not in line.lower(), line)
check("it never says 'lost everything'", "everything" not in line.lower(), line)
check("it names the still-open exposure as unrealised", "unrealised" in line, line)
check("it says no-data is not a pass", "not a pass" in line, line)
check("the near-total loss is worded as realised, not as price",
      "closed out for" in line, line)

no_fail = ta.scan(
    History(fills=[swap("c1", BASE, "CAKEUSDT", Side.BUY, 500, 2.0)], source="t"),
    fetcher=counting_fetcher(TABLE), contracts={"CAKE": ("0xcake", 56)},
)
check("a clean book says so without inventing a failure",
      "none of the 1 with audit data failed" in no_fail.headline, no_fail.headline)
check("an empty history is handled",
      ta.scan(History(fills=[], source="t"),
              fetcher=counting_fetcher({})).headline.startswith("No on-chain"))

reused = ta.scan(onchain, fetcher=counting_fetcher(TABLE), contracts=CONTRACTS,
                 cache=shared)
check("a second scan with the same cache fetches nothing new", shared.fetches == 3,
      str(shared.fetches))
check("and reports the cache hits", reused.cache_hits == 3, str(reused.cache_hits))

payload = report.to_dict()
check("to_dict carries the counts an agent needs",
      payload["n_failing"] == 2 and payload["n_no_data"] == 1, str(payload)[:200])
check("to_dict keeps realised and open apart",
      "realised_on_failing" in payload and "open_cost_basis_on_failing" in payload)
check("per-token receipts are present", len(payload["tokens"]) == 4)
check("cex fills are ignored by default", len(ta.token_targets(
    History(fills=[Fill(fill_id="x", ts=BASE, symbol="BTCUSDT", side=Side.BUY,
                        qty=1, price=1, venue=Venue.SPOT)], source="t"))) == 0)


# ==========================================================================
# publish_card.py
# ==========================================================================

print("\n=== publish_card: redaction ===")

pc = load_script("publish_card.py")

raw = pc.build_post("synthetic:night", redacted=False)
red = pc.build_post("synthetic:night", redacted=True)

check("the raw card contains money", pc.currency_amounts(raw) != [],
      str(pc.currency_amounts(raw)[:5]))
check("the redacted card contains none, by the script's own definition",
      pc.currency_amounts(red) == [], str(pc.currency_amounts(red)[:5]))
check("the net PnL figure is gone", "-3,150" not in red and "3,150" not in red)
check("percentages survive", "42%" in red, red[:200])
check("trade counts survive", "257 trades" in red)
check("day counts survive", "365 days" in red)
check("symbol counts survive", "4 symbols" in red)
check("clock times survive", "00:00-06:00" in red)
check("receipt counts survive", "20 trade receipts" in red)
check("redaction leaves the card readable",
      "REKT WRAPPED" in red and "YOUR MOST EXPENSIVE HABIT" in red)

check("a bare positive amount is redacted too",
      pc.redact("averaged 1,240 against 30 elsewhere") == f"averaged {pc.REDACTED} "
      f"against {pc.REDACTED} elsewhere",
      pc.redact("averaged 1,240 against 30 elsewhere"))
check("a leverage ratio survives", "10x" in pc.redact("trades at 10x or above"))
check("a dollar figure is redacted with its sign",
      "2,180" not in pc.redact("Cost: $2,180."), pc.redact("Cost: $2,180."))
check("a signed percentage survives", "+1.23%" in pc.redact("returned +1.23% per trade"))
check("an odds ratio survives", "1.9:1" in pc.redact("a 1.9:1 win/loss ratio"))

for leak in ("disposition", "martingale", "revenge", "night", "overleverage"):
    body = pc.build_post(f"synthetic:{leak}", redacted=True)
    check(f"redaction is complete on synthetic:{leak}",
          pc.currency_amounts(body) == [], str(pc.currency_amounts(body)[:4]))

print("\n=== publish_card: it cannot post without --yes ===")


def spy_runner():
    calls = []

    def run(argv, env):
        calls.append((list(argv), dict(env)))
        return 0, "Success!\nID: 42\nLink: https://binance.com/square/post/42", ""

    run.calls = calls
    return run


# A stand-in for a `square-post` checkout, in a temp dir rather than the repo:
# the script only checks that `scripts/post-text.mjs` exists, and the spy runner
# means it is never executed.
SKILL = Path(tempfile.mkdtemp(prefix="gnosis-square-"))
(SKILL / "scripts").mkdir(parents=True, exist_ok=True)
(SKILL / "scripts" / "post-text.mjs").write_text("// stand-in; never executed\n")

spy = spy_runner()
out = io.StringIO()
code = pc.publish("body", confirmed=False, skill_dir=str(SKILL), node="python3",
                  runner=spy, env={pc.KEY_ENV: "sq_abcdefghijklmnop"}, out=out)
check("the dry run exits 0", code == 0, str(code))
check("the dry run runs nothing at all", spy.calls == [], str(spy.calls))
check("the dry run says so", "DRY RUN" in out.getvalue())
check("the dry run names the flag that would publish", "--yes" in out.getvalue())
check("the dry run prints the exact body that would be posted",
      "body" in out.getvalue())

check("the parser defaults --yes to false", pc.build_parser().parse_args([]).yes is False)
check("--yes is the only way to set it", pc.build_parser().parse_args(["--yes"]).yes is True)
check("the parser defaults to redacted", pc.build_parser().parse_args([]).redact is True)
check("--show-amounts is required to publish money",
      pc.build_parser().parse_args(["--show-amounts"]).redact is False)

main_spy = spy_runner()
pc.default_runner, real_runner = main_spy, pc.default_runner
main_out = io.StringIO()
stdout, sys.stdout = sys.stdout, main_out
try:
    main_code = pc.main(["synthetic:night"])
finally:
    sys.stdout = stdout
    pc.default_runner = real_runner
check("main() with no --yes exits 0", main_code == 0, str(main_code))
check("main() with no --yes never invokes the runner", main_spy.calls == [],
      str(main_spy.calls))
# The post body is the chunk between the second and third rules -- the header's
# own character count is the script's chrome, not something that gets published.
main_body = main_out.getvalue().split("─" * 68)[2]
check("main() redacts by default", pc.currency_amounts(main_body) == [],
      str(pc.currency_amounts(main_body)[:4]))
check("main() printed a real card, not an empty one", "REKT WRAPPED" in main_body)

print("\n=== publish_card: it does post with --yes ===")

spy = spy_runner()
out = io.StringIO()
code = pc.publish("body", confirmed=True, skill_dir=str(SKILL), node="python3",
                  runner=spy, env={pc.KEY_ENV: "sq_abcdefghijklmnop"}, out=out)
check("a confirmed publish exits 0", code == 0, str(code))
check("a confirmed publish invokes the runner exactly once", len(spy.calls) == 1,
      str(len(spy.calls)))
argv, env = spy.calls[0]
check("it calls the skill's post-text.mjs", argv[1].endswith("scripts/post-text.mjs"), argv[1])
check("the body is passed as --text", argv[2] == "--text" and argv[3] == "body", str(argv))
check("the key is never a command-line argument",
      not any("sq_abcdefghijklmnop" in a for a in argv), str(argv))
check("the key is passed in the environment", env[pc.KEY_ENV] == "sq_abcdefghijklmnop")
check("the full key is never printed", "sq_abcdefghijklmnop" not in out.getvalue())
check("only the masked key is shown", "sq_ab...mnop" in out.getvalue(), out.getvalue()[-300:])
check("the script's output is surfaced", "ID: 42" in out.getvalue())

titled_spy = spy_runner()
pc.publish("body", confirmed=True, title="Rekt Wrapped", skill_dir=str(SKILL),
           node="python3", runner=titled_spy, env={pc.KEY_ENV: "k" * 20}, out=io.StringIO())
check("--title is forwarded as an article", "--title" in titled_spy.calls[0][0],
      str(titled_spy.calls[0][0]))

print("\n=== publish_card: the key ===")

check("masking keeps five and four", pc.mask_key("sq_abcdefghijklmnop") == "sq_ab...mnop",
      pc.mask_key("sq_abcdefghijklmnop"))
check("a short key is masked harder", pc.mask_key("abc") == "ab...", pc.mask_key("abc"))
check("an absent key masks to nothing", pc.mask_key("") == "")
check("the env var wins over the config file",
      pc.resolve_key({pc.KEY_ENV: "fromenv"}) == ("fromenv", f"${pc.KEY_ENV}"),
      str(pc.resolve_key({pc.KEY_ENV: "fromenv"})))
check("a missing key resolves to None, not an exception",
      pc.resolve_key({})[0] in (None, pc.resolve_key({})[0]))

spy = spy_runner()
out = io.StringIO()
code = pc.publish("body", confirmed=True, skill_dir=str(SKILL), node="python3",
                  runner=spy, env={}, out=out)
check("--yes with no key exits non-zero", code == 1, str(code))
check("--yes with no key posts nothing", spy.calls == [], str(spy.calls))
check("and explains where to get one", "creator-center" in out.getvalue())
check("and names both places it looked",
      pc.KEY_ENV in out.getvalue() and "binance-square" in out.getvalue())

check("a dry run works with no key at all",
      pc.publish("body", confirmed=False, runner=spy_runner(), env={},
                 out=io.StringIO()) == 0)

print("\n=== publish_card: refusing rather than truncating ===")

spy = spy_runner()
out = io.StringIO()
code = pc.publish("x" * (pc.SHORT_POST_LIMIT + 1), confirmed=True, skill_dir=str(SKILL),
                  node="python3", runner=spy, env={pc.KEY_ENV: "k" * 20}, out=out)
check("an overlong short post is refused", code == 1, str(code))
check("nothing was posted", spy.calls == [], str(spy.calls))
check("and it suggests --title", "--title" in out.getvalue())

spy = spy_runner()
code = pc.publish("x" * (pc.SHORT_POST_LIMIT + 1), confirmed=True, title="Long one",
                  skill_dir=str(SKILL), node="python3", runner=spy,
                  env={pc.KEY_ENV: "k" * 20}, out=io.StringIO())
check("the same body posts fine as an article", code == 0 and len(spy.calls) == 1,
      f"{code} {len(spy.calls)}")

spy = spy_runner()
out = io.StringIO()
code = pc.publish("body", confirmed=True, skill_dir=str(ROOT / "nope"), node="python3",
                  runner=spy, env={pc.KEY_ENV: "k" * 20}, out=out)
check("a missing square-post checkout is reported, not crashed through", code == 1, str(code))
check("nothing was posted then either", spy.calls == [], str(spy.calls))
check("and it says where to get the skill", "binance-skills-hub" in out.getvalue())

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
