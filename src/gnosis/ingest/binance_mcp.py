"""CEX trade history, via the Binance Agent OS MCP server (Account scope).

The third ingest path, after the CSV export and the on-chain wallet. It reads
`https://agent.binance.com/mcp/agentic` with the **Account** scope only --
never Trade, never Transfer -- and flattens what comes back into the same
`Fill`/`CashFlow` stream every detector already understands.

Two constraints shaped this module more than anything about MCP itself, and
both are worth stating before the code rather than discovering afterwards.

**There are no refresh tokens.** The authorization server at
`accounts.binance.com` advertises `grant_types_supported: ["authorization_code"]`
and nothing else: no `refresh_token`, no client credentials, and no
`registration_endpoint` (`POST /register` answers 404, while
`client_id_metadata_document_supported: true` -- so a third party registers by
CIMD rather than dynamic registration). A PKCE public client can therefore
obtain exactly one access token per human interaction, and when it expires a
human has to authorise again. **Nothing built on this endpoint runs
unattended.** That is why the network sits behind an injectable `transport`
rather than an embedded OAuth client: this module never acquires a credential,
it is handed one, and the tests hand it canned responses instead.

**The Agentic sub-account is isolated, and that is a real product limit.** All
Agent OS activity is confined to a dedicated Agentic sub-account -- transfers
cannot leave it and withdrawals to external addresses are blocked, which is
exactly the right security posture and exactly the wrong data source for
behavioural profiling. The trade history reachable through this endpoint is
*the sub-account's* history, not the user's main-account history, and the two
do not overlap. A freshly created Agentic sub-account has no behavioural
history at all: not a thin one, not a noisy one, none. Gnosis needs months of
a person's real decisions, so for most users today the honest answer is that
the CSV export (`binance_csv.py`) is the adapter that sees their actual book
and this one sees a sandbox. `EmptySubAccount` exists to say that out loud
instead of returning an empty `History` that the profiler would then describe
as "insufficient data", which would blame the trader for a plumbing fact.

**What maps to what.** A trade row is a `Fill`: side, quantity, price, and the
commission as `Fill.fee`, valued in the quote asset. Everything in the income
history that is *not* a trade is a `CashFlow` -- funding fees, interest,
rebates, referral kickbacks, welcome bonuses, insurance clears. None of those
is a decision, and `events.py` is explicit that a detector must never see one
as a `Fill`; a funding payment turned into a fill would invent a position the
trader never took. `REALIZED_PNL` income rows are dropped entirely: round-trip
PnL is recomputed FIFO from the fills, and carrying the exchange's own figure
alongside it would double-count every closed trade. `COMMISSION` income rows
that name a `tradeId` already ingested as a fill are dropped for the same
reason -- that money is already on `Fill.fee` -- and any commission row that
cannot be matched is kept as a `CashFlow`, because it is real money that left.

**Tool names are a guess; response shapes are not.** The MCP server's tool
names are not published anywhere Gnosis could read them, so `ToolNames` below
holds a plausible default set and every call site takes an override. The
*payloads* are the ordinary Binance REST responses (`myTrades`, `userTrades`,
`income`), which are documented and stable, and they are matched by alias in
the same way `baw_onchain.py` matches its own -- so a renamed tool is a
one-line fix and a renamed field is already handled. Call `describe_tools()`
to see what this module will ask for.

Zero hard dependencies: no MCP client library, no HTTP library. The only
network code is `http_transport`, which imports `urllib` lazily inside the
function, so this module imports and parses cleanly on a machine with nothing
installed and no account.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from ..model.events import CashFlow, Fill, History, Side, Venue

# (tool_name, arguments) -> decoded JSON result. The whole seam.
#
# Deliberately not "an MCP client": everything above this line is pure mapping,
# so a caller that already has captured responses -- a test, a replay of a
# human-attended session, another agent that holds the token -- drives this
# module without a network stack. Same argument as `baw_onchain.Runner`.
Transport = Callable[[str, Mapping[str, object]], object]

DEFAULT_ENDPOINT = "https://agent.binance.com/mcp/agentic"
DEFAULT_TIMEOUT = 60.0


class McpError(RuntimeError):
    """The MCP server refused, or answered with something unreadable."""


class NotAuthorised(McpError):
    """No token, an expired token, or a token the server will not accept.

    Its own type because it is the failure with a human remedy and no
    programmatic one. There is no refresh token to try, so a caller must not
    retry: it must tell someone to authorise again.
    """


class ScopeMissing(McpError):
    """Authorised, but without the scope this read needs.

    Gnosis asks for Account only. If a token was minted with Market scope
    alone -- the one scope that needs no authorisation at all -- every trade
    read fails this way, and the fix is a new authorisation with Account
    ticked, not a different endpoint.
    """

    def __init__(self, message: str, *, scope: str = "Account") -> None:
        self.scope = scope
        super().__init__(message)


class EmptySubAccount(McpError):
    """Authorised, correct scope, and the sub-account has no trades.

    Not an error in the plumbing and not a thin history: an *isolated* account
    that has never traded. Raised rather than returned as an empty `History`
    so the caller can say why there is nothing to profile instead of letting
    the profiler report "insufficient data" about a person who has plenty of
    it somewhere this endpoint cannot see.
    """


@dataclass(frozen=True)
class ToolNames:
    """What this module will call. Every name here is a guess -- see module doc.

    Override any of them without touching the mapping code:

        load(transport=t, tools=ToolNames(spot_trades="binance_my_trades"))
    """

    spot_trades: str = "get_spot_my_trades"
    futures_trades: str = "get_futures_user_trades"
    income: str = "get_futures_income_history"
    # Not called by `load()`. Carried for callers who want to name the
    # sub-account for provenance; every extra call to an unverified tool name
    # is another guaranteed failure point, so the read path uses three.
    account: str = "get_account_information"


DEFAULT_TOOLS = ToolNames()


def describe_tools(tools: ToolNames = DEFAULT_TOOLS) -> list[str]:
    """The tool names `load()` will ask the server for, in call order.

    Exposed so a caller can print them next to a real `tools/list` and see the
    mismatch, which is the fastest way to find the right names given that they
    are not documented anywhere this module could read.
    """
    return [tools.spot_trades, tools.futures_trades, tools.income]


# Quote assets we can peel off a concatenated pair. Longest first, so BTCUSDT
# resolves to (BTC, USDT). Same list and same reasoning as `binance_csv.py`;
# duplicated rather than imported because these two adapters must be free to
# disagree about what a quote asset is without breaking each other.
_QUOTE_ASSETS = (
    "USDT", "FDUSD", "USDC", "BUSD", "TUSD", "USDP", "DAI", "USD",
    "BTC", "ETH", "BNB", "TRY", "EUR", "BRL", "ARS", "JPY", "GBP", "AUD",
)
_STABLES = frozenset({"USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDP", "USD"})

# Income rows that are already accounted for elsewhere and must not be counted
# twice. REALIZED_PNL is recomputed FIFO from the fills; COMMISSION is on
# `Fill.fee` whenever its trade is one we ingested.
_PNL_INCOME = frozenset({"REALIZED_PNL"})
_COMMISSION_INCOME = frozenset({"COMMISSION"})

# Where a list of records hides inside a tool result.
_ROW_KEYS = ("trades", "rows", "data", "list", "result", "items", "records", "income",
             "incomeList", "userTrades", "myTrades")

# Substrings that identify an auth failure in whatever prose the server returns.
# Matched case-insensitively against the error text as a fallback for when the
# transport could not give us an HTTP status.
_UNAUTH_HINTS = ("unauthorized", "unauthorised", "invalid_token", "invalid token",
                 "expired", "www-authenticate", "401", "not authenticated",
                 "authentication required")
_SCOPE_HINTS = ("scope", "insufficient_scope", "forbidden", "not permitted",
                "permission denied", "403")


def http_transport(
    access_token: str,
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: float = DEFAULT_TIMEOUT,
) -> Transport:
    """A `Transport` that speaks JSON-RPC over HTTP to the live MCP server.

    The default seam, and the only code here that touches a network. `urllib`
    is imported inside the returned closure so that importing this module --
    which is all the tests and the offline CLI ever do -- costs nothing and
    cannot fail on a machine with no network stack configured.

    `access_token` is a bearer token obtained by an authorization-code + PKCE
    flow that this module deliberately does not implement. There is no refresh
    token: when this starts raising `NotAuthorised`, a human has to authorise
    again. Nothing to catch and retry.
    """

    def call(tool: str, arguments: Mapping[str, object]) -> object:
        import urllib.error  # noqa: PLC0415 - lazy on purpose: zero import-time deps
        import urllib.request  # noqa: PLC0415

        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": dict(arguments)},
        }).encode()
        request = urllib.request.Request(  # noqa: S310 - fixed https endpoint
            endpoint,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {access_token}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:  # pragma: no cover - needs a network
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:  # noqa: BLE001 - the status is the fact; the body is a bonus
                detail = ""
            if exc.code == 401:
                raise NotAuthorised(
                    "MCP returned 401. The access token is missing or expired. There is no "
                    "refresh token on this authorization server, so re-authorise "
                    "interactively -- this cannot be recovered from in code"
                ) from exc
            if exc.code == 403:
                raise ScopeMissing(
                    f"MCP returned 403 for {tool!r}. Re-authorise with the Account scope; "
                    f"Market scope alone cannot read a trade history. {detail}"
                ) from exc
            raise McpError(f"MCP returned HTTP {exc.code} for {tool!r}: {detail}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - needs a network
            raise McpError(f"could not reach {endpoint}: {exc.reason}") from exc
        return _decode_rpc(body, tool=tool)

    return call


def _decode_rpc(body: str, *, tool: str) -> object:
    """Unwrap a JSON-RPC / MCP `tools/call` reply down to the payload.

    Accepts both the plain JSON body and the SSE framing the endpoint may use
    (`event: message` / `data: {...}`), because which one comes back depends on
    the `Accept` header and is not worth being brittle about.
    """
    text = (body or "").strip()
    if text.startswith("event:") or text.startswith("data:"):
        chunks = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
        text = chunks[-1] if chunks else ""
    try:
        envelope = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        first = (body or "").strip().splitlines()[:1]
        raise McpError(
            f"{tool}: response was not JSON. First line: "
            f"{(first[0] if first else '<empty>')!r}"
        ) from exc
    return unwrap(envelope, tool=tool)


def unwrap(envelope: object, *, tool: str = "tool") -> object:
    """Peel a JSON-RPC envelope and an MCP tool result down to the data.

    Split out and public because it is where the error classification lives,
    and a caller replaying captured responses through a custom transport wants
    the same `NotAuthorised` / `ScopeMissing` behaviour without reimplementing
    it.
    """
    if not isinstance(envelope, Mapping):
        return envelope

    error = envelope.get("error")
    if isinstance(error, Mapping):
        message = str(error.get("message") or error)
        raise _classify(f"{tool}: {message}", data=error.get("data"))
    if isinstance(error, str) and error:
        raise _classify(f"{tool}: {error}")

    result = envelope.get("result", envelope)
    if not isinstance(result, Mapping):
        return result

    # An MCP tool that failed reports it in-band, with isError on a normal
    # result rather than a JSON-RPC error. Missing scope arrives this way.
    if result.get("isError"):
        raise _classify(f"{tool}: {_text_of(result) or 'tool reported an error'}")

    if "structuredContent" in result:
        structured = result["structuredContent"]
        # Some servers wrap a bare list as {"result": [...]}; unwrap one level.
        if isinstance(structured, Mapping) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    if "content" in result:
        text = _text_of(result)
        if text is None:
            return result["content"]
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            # Prose where JSON was expected is nearly always an auth or scope
            # message, so classify it rather than failing to parse it.
            raise _classify(f"{tool}: expected JSON, got text: {text[:200]!r}") from None
    return result


def _text_of(result: Mapping) -> str | None:
    blocks = result.get("content")
    if not isinstance(blocks, list):
        return None
    parts = [
        str(b.get("text"))
        for b in blocks
        if isinstance(b, Mapping) and b.get("type") in (None, "text") and b.get("text")
    ]
    return "\n".join(parts) if parts else None


def _classify(message: str, *, data: object = None) -> McpError:
    """Pick the most specific error type the message supports.

    Scope is checked before auth: "insufficient scope" contains neither more
    nor less authority than a 401, but it has a different remedy, and telling
    someone to re-authorise when they actually need to tick a box is the kind
    of advice that wastes an afternoon.
    """
    haystack = f"{message} {data if data is not None else ''}".lower()
    if any(hint in haystack for hint in _SCOPE_HINTS):
        return ScopeMissing(
            f"{message} -- Gnosis needs the Account scope (read-only). Re-authorise "
            "with Account granted; Market scope alone cannot read trades"
        )
    if any(hint in haystack for hint in _UNAUTH_HINTS):
        return NotAuthorised(
            f"{message} -- authorise again interactively. This authorization server "
            "issues no refresh token, so there is nothing to retry with"
        )
    return McpError(message)


def _records(payload: object, *, what: str) -> list[Mapping]:
    """The list of rows, out of whatever wrapper it arrived in."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, Mapping)]
    if isinstance(payload, Mapping):
        for key in _ROW_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, Mapping)]
            if isinstance(value, Mapping):
                nested = _records(value, what=what)
                if nested:
                    return nested
        # An account that has never traded is a legitimate answer, and must not
        # look like a parse failure -- `EmptySubAccount` is raised later, with
        # a message that explains the sub-account rather than the payload.
        if not payload or any(payload.get(k) == [] for k in _ROW_KEYS):
            return []
    raise McpError(f"{what}: could not find a list of rows in the response")


def _first(record: Mapping, *names: str) -> object | None:
    """Alias lookup, insensitive to case and to underscore/camel spelling."""
    lowered = {str(k).lower().replace("_", ""): v for k, v in record.items()}
    for name in names:
        value = lowered.get(name.lower().replace("_", ""))
        if value not in (None, ""):
            return value
    return None


def _number(value: object, *, field: str, where: str) -> float:
    if isinstance(value, bool):  # bools are ints in Python and never a quantity
        raise McpError(f"{where}: {field} is a boolean, not a number")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError as exc:
            raise McpError(f"{where}: cannot read {field} as a number: {value!r}") from exc
    raise McpError(f"{where}: cannot read {field} as a number: {value!r}")


def _timestamp(value: object, *, where: str) -> datetime:
    """Epoch ms, epoch seconds, or ISO -- always UTC-aware.

    Binance reports epoch milliseconds. The ms/second split is by magnitude:
    past 1e11 is milliseconds, because seconds would put it in the year 5138.
    Guessing wrong scatters a whole book across the wrong hours, and the
    session detector reads hour-of-day.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / 1000.0 if abs(float(value)) >= 1e11 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return _timestamp(int(text), where=where)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise McpError(f"{where}: cannot read timestamp {value!r}") from exc
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None \
            else parsed.astimezone(timezone.utc)
    raise McpError(f"{where}: cannot read timestamp {value!r}")


def _split_pair(symbol: str) -> tuple[str, str]:
    """(base, quote) for a concatenated pair, longest quote match wins."""
    upper = symbol.upper()
    for quote in sorted(_QUOTE_ASSETS, key=len, reverse=True):
        if upper.endswith(quote) and len(upper) > len(quote):
            return upper[: -len(quote)], quote
    return upper, ""


def _side(record: Mapping, *, where: str) -> Side:
    """Direction, from whichever of the three spellings this payload uses.

    Spot `myTrades` carries `isBuyer`; futures `userTrades` carries both
    `side` and `buyer`. Refusing to guess when none is present matters: a
    fill with the wrong sign does not merely mis-price one trade, it inverts a
    whole round-trip and can turn a loss into a win in the profile.
    """
    explicit = _first(record, "side", "orderSide")
    if explicit is not None:
        text = str(explicit).strip().upper()
        if text in ("BUY", "B", "LONG", "BID"):
            return Side.BUY
        if text in ("SELL", "S", "SHORT", "ASK"):
            return Side.SELL
        raise McpError(f"{where}: side is neither BUY nor SELL: {explicit!r}")
    for key in ("isBuyer", "buyer", "isBuyerMaker"):
        value = _first(record, key)
        if isinstance(value, bool):
            return Side.BUY if value else Side.SELL
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return Side.BUY if value.strip().lower() == "true" else Side.SELL
    raise McpError(
        f"{where}: no side on the trade (looked for side, isBuyer, buyer). Refusing to "
        "guess a direction -- a mis-signed fill inverts the round-trip it belongs to"
    )


def _commission_in_quote(
    record: Mapping,
    *,
    symbol: str,
    price: float,
    fee_prices: Mapping[str, float] | None,
    where: str,
) -> float:
    """The trade's commission, expressed in the quote asset.

    Binance charges the fee in whichever asset the account happened to pay in:
    the quote asset, the base asset, or -- overwhelmingly, because of the
    discount -- BNB. Only the first is already in the units the profile is
    denominated in.

    A fee this function cannot value raises rather than silently becoming
    zero, which is the same stance `binance_csv.py` takes and for the same
    measured reason: `tests/test_roundtrip.py` has a case named "fees can flip
    the sign", and understating fees turns losing trades into winners.
    """
    raw = _first(record, "commission", "fee", "commissionAmount", "tradeFee")
    if raw is None:
        return 0.0
    amount = abs(_number(raw, field="commission", where=where))
    if amount == 0:
        return 0.0
    asset_raw = _first(record, "commissionAsset", "feeAsset", "feeCurrency", "commissionCoin")
    base, quote = _split_pair(symbol)
    if asset_raw is None:
        # No asset named. Assume the quote asset only when the pair has a
        # recognisable stable quote, where the assumption is nearly always
        # right and its error is small; otherwise refuse.
        if quote in _STABLES:
            return amount
        raise McpError(
            f"{where}: commission of {amount} names no asset and {symbol} has no stable "
            "quote to assume, so it cannot be valued"
        )
    asset = str(asset_raw).upper()
    if asset == quote or (quote in _STABLES and asset in _STABLES):
        return amount
    if asset == base:
        return amount * price
    price_in_quote = (fee_prices or {}).get(asset)
    if price_in_quote is None:
        raise McpError(
            f"{where}: commission is {amount} {asset}, which is neither side of {symbol} "
            f"and cannot be valued from the payload. Pass fee_prices={{'{asset}': <price "
            f"in {quote or 'quote'}>}} -- treating an unvaluable fee as zero turns losing "
            "trades into winners"
        )
    return amount * price_in_quote


def _leverage(record: Mapping, *, where: str) -> float | None:
    """Leverage, if the payload happens to carry it. `None`, never 1.0.

    Binance's `userTrades` rows do not carry leverage -- it is a property of
    the position, fetched separately -- so this is usually `None`, and the
    leverage detector will correctly find nothing to compare. `events.py` is
    explicit that `None` and 1.0 are different claims, and defaulting to 1.0
    would dump every futures fill into the low-leverage arm and quietly
    destroy the comparison the detector exists to make.
    """
    raw = _first(record, "leverage", "lev", "positionLeverage")
    if raw is None:
        return None
    value = _number(raw, field="leverage", where=where)
    if value <= 0:
        raise McpError(f"{where}: leverage must be positive, got {value}")
    return value


def _row_id(record: Mapping, index: int) -> str:
    value = _first(record, "id", "tradeId", "tranId", "orderId", "executionId")
    return str(value) if value is not None else f"idx{index:06d}"


def fills_from_trades(
    rows: Sequence[Mapping],
    *,
    venue: Venue,
    prefix: str,
    fee_prices: Mapping[str, float] | None = None,
) -> list[Fill]:
    """Map `myTrades` / `userTrades` rows onto `Fill`s.

    One function for both dialects: the fields that differ (`isBuyer` versus
    `side`, `realizedPnl` present or absent) are handled by alias, and the
    only genuine difference is the venue and the id prefix.
    """
    fills: list[Fill] = []
    for i, row in enumerate(rows):
        rid = _row_id(row, i)
        where = f"{prefix} trade {rid}"
        symbol_raw = _first(row, "symbol", "pair", "market", "contract")
        if symbol_raw is None:
            raise McpError(f"{where}: no symbol on the trade")
        symbol = str(symbol_raw).upper()
        price = _number(
            _first(row, "price", "avgPrice", "averagePrice") or 0, field="price", where=where
        )
        qty = _number(
            _first(row, "qty", "quantity", "executedQty", "amount") or 0,
            field="qty", where=where,
        )
        ts = _timestamp(
            _first(row, "time", "timestamp", "tradeTime", "transactTime", "updateTime") or "",
            where=where,
        )
        fee = _commission_in_quote(
            row, symbol=symbol, price=price, fee_prices=fee_prices, where=where
        )

        meta: dict = {"source": "mcp", "trade_id": rid, "venue_kind": prefix}
        for key, names in (
            ("order_id", ("orderId", "clientOrderId")),
            ("realized_pnl", ("realizedPnl", "realisedPnl", "realizedProfit")),
            ("position_side", ("positionSide",)),
            ("maker", ("maker", "isMaker")),
            ("quote_qty", ("quoteQty", "quoteQuantity")),
            ("commission_asset", ("commissionAsset", "feeAsset")),
        ):
            value = _first(row, *names)
            if value is not None:
                meta[key] = value

        try:
            fills.append(Fill(
                fill_id=f"mcp-{prefix}-{rid}",
                ts=ts,
                symbol=symbol,
                side=_side(row, where=where),
                qty=qty,
                price=price,
                fee=fee,
                venue=venue,
                leverage=_leverage(row, where=where),
                meta=meta,
            ))
        except ValueError as exc:
            # `Fill.__post_init__` guards qty > 0 and a tz-aware ts. Re-raised
            # as an McpError so a caller catches one family, not two.
            raise McpError(f"{where}: {exc}") from exc
    return fills


def flows_from_income(
    rows: Sequence[Mapping],
    *,
    known_trade_ids: frozenset[str] = frozenset(),
) -> list[CashFlow]:
    """Map income-history rows onto `CashFlow`s. Never onto `Fill`s.

    Funding, interest, rebates, kickbacks and bonuses are money that moved
    without a decision. `events.py` reserves `CashFlow` for exactly that, and a
    detector that saw a funding payment as a fill would report a position the
    trader never opened.

    Two kinds are dropped rather than mapped, both to avoid double-counting:
    `REALIZED_PNL`, because round-trip PnL is recomputed FIFO from the fills,
    and `COMMISSION` rows whose `tradeId` is already among `known_trade_ids`,
    because that money is already on `Fill.fee`. An unmatched commission row is
    kept -- it is real, and dropping it would understate the cost of trading.
    """
    flows: list[CashFlow] = []
    for i, row in enumerate(rows):
        where = f"income row {_row_id(row, i)}"
        kind_raw = _first(row, "incomeType", "type", "kind", "category")
        if kind_raw is None:
            raise McpError(f"{where}: no incomeType, so we cannot tell what this money was")
        kind = str(kind_raw).upper()
        if kind in _PNL_INCOME:
            continue
        trade_id = _first(row, "tradeId", "tradeID")
        if kind in _COMMISSION_INCOME and trade_id is not None \
                and str(trade_id) in known_trade_ids:
            continue

        amount = _number(
            _first(row, "income", "amount", "value", "delta") or 0, field="income", where=where
        )
        if amount == 0:
            continue
        symbol = str(_first(row, "symbol", "pair") or _first(row, "asset", "coin") or "")
        meta = {"source": "mcp", "income_type": kind}
        for key, names in (("asset", ("asset", "coin")), ("tran_id", ("tranId", "id")),
                           ("trade_id", ("tradeId",)), ("info", ("info",))):
            value = _first(row, *names)
            if value is not None:
                meta[key] = value
        flows.append(CashFlow(
            ts=_timestamp(
                _first(row, "time", "timestamp", "transactionTime") or "", where=where
            ),
            symbol=symbol.upper(),
            kind=kind.lower(),
            amount=amount,  # signed as the exchange reports it: negative is a cost
            meta=meta,
        ))
    return flows


def build_history(
    *,
    spot_trades: Sequence[Mapping] = (),
    futures_trades: Sequence[Mapping] = (),
    income: Sequence[Mapping] = (),
    account: str = "",
    fee_prices: Mapping[str, float] | None = None,
    futures_venue: Venue = Venue.FUTURES_USDS,
) -> History:
    """Turn decoded MCP payloads into a `History`. No network involved.

    Split out from `load()` for the same reason `baw_onchain` splits its own:
    the mapping is the part that can be wrong, and it must be testable against
    captured responses on a machine with no token -- which, given that there
    are no refresh tokens, is every machine most of the time.
    """
    fills = fills_from_trades(
        spot_trades, venue=Venue.SPOT, prefix="spot", fee_prices=fee_prices
    )
    fills += fills_from_trades(
        futures_trades, venue=futures_venue, prefix="futures", fee_prices=fee_prices
    )
    known = frozenset(str(_first(r, "id", "tradeId") or "")
                      for r in (*spot_trades, *futures_trades)) - {""}
    flows = flows_from_income(income, known_trade_ids=known)
    return History(fills=fills, flows=flows, source=f"binance:mcp:{account or 'agentic'}")


def load(
    *,
    transport: Transport,
    tools: ToolNames = DEFAULT_TOOLS,
    symbols: Sequence[str] = (),
    start_time: int | None = None,
    end_time: int | None = None,
    limit: int = 1000,
    fee_prices: Mapping[str, float] | None = None,
    include_income: bool = True,
) -> History:
    """Read the Agentic sub-account's trade history into a `History`.

    `transport` is required and has no default: this module never acquires a
    credential. Obtain a token by an authorization-code + PKCE flow against
    `accounts.binance.com/agentic-oauth/authorize`, then pass
    `transport=http_transport(token)`. When it expires, a human authorises
    again -- there is no refresh token.

    `symbols` is needed for spot: Binance's `myTrades` is per-symbol, so with
    no symbols given the spot leg is skipped rather than silently returning
    nothing. Futures income and trades are account-wide.

    Raises `NotAuthorised`, `ScopeMissing`, or `EmptySubAccount` -- the last
    when the call succeeded and the isolated Agentic sub-account simply has no
    trades in it, which is the normal state of a new one and is a fact about
    the account, not about the trader.
    """
    window: dict[str, object] = {"limit": limit}
    if start_time is not None:
        window["startTime"] = start_time
    if end_time is not None:
        window["endTime"] = end_time

    spot_rows: list[Mapping] = []
    for symbol in symbols:
        spot_rows += _records(
            transport(tools.spot_trades, {"symbol": symbol, **window}),
            what=f"{tools.spot_trades}({symbol})",
        )
    futures_rows = _records(
        transport(tools.futures_trades, dict(window)), what=tools.futures_trades
    )
    income_rows: list[Mapping] = []
    if include_income:
        income_rows = _records(transport(tools.income, dict(window)), what=tools.income)

    history = build_history(
        spot_trades=spot_rows,
        futures_trades=futures_rows,
        income=income_rows,
        fee_prices=fee_prices,
    )
    if not history.fills:
        raise EmptySubAccount(
            "the Agentic sub-account has no trades. Agent OS activity is confined to an "
            "isolated sub-account, so this is not your main-account history and a new "
            "sub-account has none at all. Export your main account's trade history as CSV "
            "and use `gnosis.ingest.binance_csv` to profile the book you actually traded"
        )
    return history
