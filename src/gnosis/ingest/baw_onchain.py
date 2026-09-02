"""On-chain swaps, via the Binance Agentic Wallet CLI (`baw`).

Half of a modern retail book is not on the exchange. It is swaps: USDT into
some token on a DEX, held, swapped back. The behaviour Gnosis looks for -- the
disposition effect, adding to losers, sizing up after a loss -- is exactly the
same behaviour there, and a profile that only reads the CEX side will report a
trader's habits from half their evidence and never say so.

`baw` is the seam. Three of its commands carry everything needed:

    baw wallet status --json         is anyone signed in
    baw market-order list --json     DEX swap orders, with status and txHash
    baw wallet tx-history --json     the raw on-chain transactions, and gas

**A swap is a fill.** Swapping USDT for CAKE is a buy of CAKE at
`usdt_in / cake_out`, on `Venue.ONCHAIN`, and the gas is the fee. Swapping the
other way is the sell that closes it. That reduction is the whole reason
`events.py` flattens both venues into one `Fill` type -- once a swap is a fill,
every detector already written works on it unmodified, and nothing downstream
needs to know a chain was involved.

`leverage` is `None`, never 1.0. There is no leverage on a spot swap, and
`events.py` is explicit that `None` and 1.0 are different claims: 1.0 would put
every swap into the low-leverage arm of the leverage detector and dilute the
comparison the detector exists to make.

**Only `FINISHED` orders are fills.** This matters more than it sounds. A
`FAILED` swap still burned gas -- the transaction reverted on chain and the
network kept the fee -- so the money genuinely left, but no position was taken
and no decision was executed. Recording it as a fill would invent a trade that
never happened and corrupt every round-trip built on that symbol. It is
recorded as a `CashFlow` with kind `"gas"` instead, which is what
`events.py` reserves that type for: money that moved without a trade. A
`PENDING` order has not resolved into either and is dropped, deliberately and
loudly documented rather than quietly counted.

**This adapter cannot run unattended.** `baw` authenticates through a phone
confirmation, and the authorization server advertises `authorization_code` with
no refresh token, so there is no credential that can be cached and replayed by
a scheduled job. Someone has to be holding the phone. That is why Gnosis is
batch-and-gate rather than a daemon, and why the subprocess call sits behind an
injectable `runner`: the tests drive this module with canned JSON, and so can
anything else that has already captured output from a human-attended session.

Field names below are matched by alias because the `--json` payloads are not a
published schema and have already changed shape once. Anything the aliases do
not cover raises rather than defaulting -- see `binance_csv.py` for the same
argument at more length.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone

from ..model.events import CashFlow, Fill, History, Side, Venue

# argv *after* the executable, so a test double can dispatch on the subcommand
# without caring what the binary is called or where it lives.
Runner = Callable[[Sequence[str]], str]

DEFAULT_TIMEOUT = 120.0


class BawError(RuntimeError):
    """The CLI is missing, refused, or answered with something unreadable."""


class WalletNotConnected(BawError):
    """No wallet is signed in, so there is nothing to read.

    Separate from `BawError` because it is the one failure with an obvious
    human remedy -- run `baw wallet connect` and confirm on the phone -- and
    callers should be able to say that rather than printing a stack trace.
    """


# Statuses that mean the swap executed. `baw` uses FINISHED; nothing else is
# accepted as a fill, because inventing a position that was never taken is the
# most damaging thing this module could do.
_FILLED = frozenset({"FINISHED"})
# Resolved without executing. Gas may still have been spent, and that is a real
# cost of trading on chain, so it is kept as a CashFlow.
_UNFILLED = frozenset({"FAILED", "CANCELLED", "CANCELED", "EXPIRED", "REJECTED"})
# Not yet resolved. Not a fill, not a settled cost, not our business yet.
_UNRESOLVED = frozenset({"PENDING", "PROCESSING", "SUBMITTED", "NEW", "OPEN"})

# Quote-side assets. A swap out of one of these is a buy of the other leg; a
# swap into one is a sell. Two stables swapped for each other fall through the
# same rule and become a near-zero-PnL "buy", which is what a conversion is.
_QUOTE_LIKE = frozenset({"USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USD1", "USDP"})

_ORDER_KEYS = ("orders", "data", "list", "result", "items", "rows", "records")
_TX_KEYS = ("transactions", "txs", "txList", "data", "list", "result", "items", "rows")


def cli_runner(argv: Sequence[str], *, executable: str = "baw", timeout: float = DEFAULT_TIMEOUT) -> str:
    """Run `baw <argv>` and return stdout.

    The default seam. Everything about it is boring on purpose: no shell, no
    retries, no parsing. Retrying is wrong here anyway -- a failure is usually
    an expired session, and the fix is a human with a phone, not a second
    attempt.
    """
    binary = shutil.which(executable)
    if binary is None:
        raise BawError(
            f"{executable!r} is not on PATH. Install the Binance Agentic Wallet CLI, or pass "
            "runner= with captured output -- this module never needs the live CLI to parse"
        )
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user-supplied binary
        [binary, *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise BawError(f"`{executable} {' '.join(argv)}` exited {result.returncode}: {detail}")
    return result.stdout


def _payload(text: str, *, what: str) -> object:
    """Decode `--json` output, or say what we got instead.

    `baw` prints human-readable text when a subcommand does not support
    `--json`, or when it wants to interrupt with a login prompt. Both arrive
    here as a JSON decode failure, and the raw first line is far more useful to
    the caller than the decoder's column number.
    """
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        first = (text or "").strip().splitlines()[:1]
        raise BawError(
            f"{what} did not return JSON. First line of output: "
            f"{(first[0] if first else '<empty>')!r}"
        ) from exc


def _records(payload: object, keys: Sequence[str]) -> list[Mapping]:
    """Pull the list of records out of whatever wrapper it arrived in."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, Mapping)]
    if isinstance(payload, Mapping):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, Mapping)]
            if isinstance(value, Mapping):
                nested = _records(value, keys)
                if nested:
                    return nested
        # An empty history is a legitimate answer and must not look like an error.
        if any(payload.get(k) == [] for k in keys):
            return []
        if not payload:
            return []
    raise BawError(f"could not find a list of records in the response; keys tried: {list(keys)}")


def _first(record: Mapping, *names: str) -> object | None:
    """Alias lookup, case-insensitive on the first underscore/camel spelling."""
    lowered = {str(k).lower().replace("_", ""): v for k, v in record.items()}
    for name in names:
        value = lowered.get(name.lower().replace("_", ""))
        if value not in (None, ""):
            return value
    return None


def _number(value: object, *, field: str, where: str) -> float:
    if isinstance(value, bool):  # bools are ints in Python and never a quantity
        raise BawError(f"{where}: {field} is a boolean, not a number")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError as exc:
            raise BawError(f"{where}: cannot read {field} as a number: {value!r}") from exc
    raise BawError(f"{where}: cannot read {field} as a number: {value!r}")


def _timestamp(value: object, *, where: str) -> datetime:
    """Epoch milliseconds, epoch seconds, or an ISO string -- always UTC-aware.

    The millisecond/second split is decided by magnitude: anything past 1e11 is
    milliseconds, because seconds would put it in the year 5138. Guessing wrong
    here would scatter a whole book across the wrong hours of the day, and the
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
            raise BawError(f"{where}: cannot read timestamp {value!r}") from exc
        if parsed.tzinfo is None:
            # `baw` reports UTC; attaching the local zone instead would shift
            # every hour-of-day statistic by the analyst's own offset.
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    raise BawError(f"{where}: cannot read timestamp {value!r}")


def _leg(record: Mapping, prefix: str, *, where: str) -> tuple[str, float]:
    """One side of a swap: (asset, amount).

    Handles both the flat spelling (`fromToken`, `fromAmount`) and the nested
    one (`fromToken: {symbol, amount}`), because both have been observed.
    """
    token = _first(record, f"{prefix}Token", f"{prefix}TokenSymbol", f"{prefix}Symbol",
                   f"{prefix}Asset", f"{prefix}Coin")
    amount = _first(record, f"{prefix}Amount", f"{prefix}TokenAmount", f"{prefix}Qty",
                    f"{prefix}Quantity", f"{prefix}Value")
    if isinstance(token, Mapping):
        amount = amount if amount is not None else _first(token, "amount", "quantity", "value")
        token = _first(token, "symbol", "asset", "name", "ticker")
    if token is None:
        raise BawError(f"{where}: no {prefix}-token on the order")
    if amount is None:
        raise BawError(f"{where}: no {prefix}-amount on the order")
    value = _number(amount, field=f"{prefix}Amount", where=where)
    if value <= 0:
        raise BawError(f"{where}: {prefix}Amount must be positive, got {value}")
    return str(token).upper(), value


def _gas_in_quote(
    record: Mapping,
    *,
    quote: str,
    fee_prices: Mapping[str, float] | None,
    where: str,
) -> tuple[float, str | None, float]:
    """Gas for one transaction, expressed in the quote asset.

    Returns `(value_in_quote, native_asset, native_amount)` so the raw figure
    can be kept in `meta` alongside the converted one.

    Gas is paid in the chain's native token (BNB on BSC), which is not either
    side of most swaps. If the payload carries a fiat-valued field we use it;
    otherwise the caller must supply a price. Defaulting to zero is the one
    thing we will not do -- gas on a small swap is a large fraction of its PnL,
    and understating it turns losers into winners.
    """
    valued = _first(record, "gasFeeUsd", "gasCostUsd", "feeUsd", "gasUsd", "networkFeeUsd",
                    "gasFeeValue", "feeValueUsd")
    if valued is not None:
        # Taken as the quote asset. Exact for a USDT/USDC-denominated book,
        # which is every book this adapter has been pointed at; approximate and
        # documented as such for anything else.
        return _number(valued, field="gas (usd)", where=where), None, 0.0

    # Ordered most-specific first. `gasUsed` is deliberately not in this list:
    # it is a count of gas units, not a cost, and mistaking one for the other
    # overstates the fee by several orders of magnitude.
    raw = _first(record, "gasFee", "gasCost", "networkFee", "txFee", "gas", "fee")
    if raw is None:
        return 0.0, None, 0.0
    amount = _number(raw, field="gas", where=where)
    if amount == 0:
        return 0.0, None, 0.0
    asset_raw = _first(record, "gasAsset", "gasToken", "feeAsset", "feeToken", "nativeToken",
                       "chainToken", "gasSymbol")
    if asset_raw is None:
        raise BawError(
            f"{where}: gas of {amount} has no asset on it, so it cannot be valued. "
            "Nothing in the payload names the native token"
        )
    asset = str(asset_raw).upper()
    if asset == quote.upper() or asset in _QUOTE_LIKE:
        return amount, asset, amount
    price = (fee_prices or {}).get(asset)
    if price is None:
        raise BawError(
            f"{where}: gas is {amount} {asset}, which cannot be valued in {quote} from the "
            f"payload alone. Pass fee_prices={{'{asset}': <price in {quote}>}} -- treating gas "
            "as zero understates the cost of every on-chain trade"
        )
    return amount * price, asset, amount


def _pair(from_asset: str, from_amount: float, to_asset: str, to_amount: float,
          *, where: str) -> tuple[str, Side, float, float]:
    """Which token was traded, in which direction, at what price.

    The rule: a swap *out of* a stable is a buy of the other leg; a swap *into*
    a stable is a sell of it. Token-for-token swaps have no quote asset at all,
    so we treat the token received as bought and price it in the token given
    up. That is a real limit -- such a trip only closes if the trader later
    swaps back through the same asset -- and it is stated here rather than
    papered over.
    """
    if _is_quote_like(from_asset) and not _is_quote_like(to_asset):
        base, quote, side = to_asset, from_asset, Side.BUY
        qty, notional = to_amount, from_amount
    elif _is_quote_like(to_asset):
        base, quote, side = from_asset, to_asset, Side.SELL
        qty, notional = from_amount, to_amount
    else:
        base, quote, side = to_asset, from_asset, Side.BUY
        qty, notional = to_amount, from_amount
    if qty <= 0:
        raise BawError(f"{where}: swap has no quantity on the traded leg")
    return f"{base}{quote}", side, qty, notional / qty


def _is_quote_like(asset: str) -> bool:
    return asset.upper() in _QUOTE_LIKE


def _status(record: Mapping, *, where: str) -> str:
    value = _first(record, "status", "orderStatus", "state")
    if value is None:
        raise BawError(f"{where}: order has no status, so we cannot tell whether it executed")
    return str(value).upper()


def _order_id(record: Mapping, index: int) -> str:
    value = _first(record, "orderId", "id", "orderNo", "clientOrderId", "txHash", "hash")
    return str(value) if value is not None else f"idx{index:06d}"


def _tx_hash(record: Mapping) -> str | None:
    value = _first(record, "txHash", "hash", "transactionHash", "txId", "txid")
    return str(value) if value is not None else None


def wallet_address(status_payload: object) -> str:
    """The connected address, or raise `WalletNotConnected`.

    `baw wallet status --json` answers `UNCONNECTED` when nobody is signed in.
    Because there are no refresh tokens, that is the normal state of any
    machine that has been left alone for a while, so it gets a named exception
    and a message that says what to do about it.
    """
    payload = status_payload
    if isinstance(payload, str):
        payload = {"status": payload}
    if not isinstance(payload, Mapping):
        raise BawError(f"unreadable wallet status: {status_payload!r}")

    state = _first(payload, "status", "state", "connection", "walletStatus")
    address = _first(payload, "address", "walletAddress", "account", "publicAddress", "evmAddress")
    if isinstance(address, Mapping):
        address = _first(address, "address", "evm", "value")

    if state is not None and str(state).upper().replace("-", "").replace("_", "") == "UNCONNECTED":
        raise WalletNotConnected(
            "baw reports UNCONNECTED. Run `baw wallet connect` and confirm on the phone -- "
            "there are no refresh tokens, so this cannot be automated and a signed-in session "
            "does not survive being left alone"
        )
    if address is None:
        raise WalletNotConnected(
            f"baw wallet status returned no address (status: {state!r}); treat the session as "
            "not connected and sign in again"
        )
    return str(address)


def build_history(
    orders: Sequence[Mapping],
    transactions: Sequence[Mapping] = (),
    *,
    address: str = "",
    quote: str = "USDT",
    fee_prices: Mapping[str, float] | None = None,
) -> History:
    """Turn decoded `baw` payloads into a `History`. No CLI involved.

    Split out from `load()` so the mapping can be tested, and reused, against
    captured JSON -- which is the only way it can be exercised at all on a
    machine without a phone-confirmed session.
    """
    # Gas often lives on the transaction rather than the order, so index it by
    # hash first and let each order fall back to its own transaction.
    gas_by_hash: dict[str, Mapping] = {}
    for tx in transactions:
        h = _tx_hash(tx)
        if h:
            gas_by_hash.setdefault(h.lower(), tx)

    fills: list[Fill] = []
    flows: list[CashFlow] = []
    seen_hashes: set[str] = set()

    for i, order in enumerate(orders):
        oid = _order_id(order, i)
        where = f"order {oid}"
        status = _status(order, where=where)
        tx_hash = _tx_hash(order)
        if tx_hash:
            seen_hashes.add(tx_hash.lower())

        if status in _UNRESOLVED:
            # Not yet a fact about the trader's behaviour either way.
            continue
        if status not in _FILLED and status not in _UNFILLED:
            raise BawError(
                f"{where}: unknown status {status!r}. Refusing to guess whether it executed -- "
                f"known: {sorted(_FILLED | _UNFILLED | _UNRESOLVED)}"
            )

        ts = _timestamp(
            _first(order, "updateTime", "finishTime", "createTime", "time", "timestamp",
                   "createdAt", "updatedAt") or "",
            where=where,
        )
        # Gas may be reported on the order or only on its transaction.
        gas_source: Mapping = order
        if tx_hash and tx_hash.lower() in gas_by_hash:
            probe = _first(order, "gasFee", "gasCost", "networkFee", "txFee", "gasFeeUsd", "feeUsd")
            if probe is None:
                gas_source = gas_by_hash[tx_hash.lower()]
        gas, gas_asset, gas_amount = _gas_in_quote(
            gas_source, quote=quote, fee_prices=fee_prices, where=where
        )

        meta: dict = {"order_id": oid, "status": status, "venue_kind": "dex_swap"}
        if tx_hash:
            meta["tx_hash"] = tx_hash
        chain = _first(order, "chain", "chainId", "network", "chainName")
        if chain is not None:
            meta["chain"] = chain
        if gas_asset:
            meta["gas_asset"] = gas_asset
            meta["gas_amount"] = gas_amount

        if status in _UNFILLED:
            # A reverted swap: the gas left the wallet, the position never
            # existed. Recorded as money that moved without a trade, which is
            # exactly what CashFlow is for -- and never as a Fill, because a
            # phantom entry would corrupt every round-trip on that symbol.
            if gas == 0:
                continue
            symbol = _failed_symbol(order, where=where) or (gas_asset or "GAS")
            flows.append(CashFlow(
                ts=ts, symbol=symbol, kind="gas", amount=-abs(gas), meta=meta,
            ))
            continue

        from_asset, from_amount = _leg(order, "from", where=where)
        to_asset, to_amount = _leg(order, "to", where=where)
        symbol, side, qty, price = _pair(
            from_asset, from_amount, to_asset, to_amount, where=where
        )
        meta.update({
            "from": f"{from_amount:g}{from_asset}",
            "to": f"{to_amount:g}{to_asset}",
        })
        try:
            fills.append(Fill(
                fill_id=f"baw-{oid}",
                ts=ts,
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                fee=gas,
                venue=Venue.ONCHAIN,
                # Not 1.0. There is no leverage here, and claiming 1x would put
                # every swap in the leverage detector's low arm. See events.py.
                leverage=None,
                meta=meta,
            ))
        except ValueError as exc:
            raise BawError(f"{where}: {exc}") from exc

    # Gas burned by transactions that are not swap orders -- approvals, sends,
    # failed contract calls. Real money, no decision attached, so: CashFlow.
    for i, tx in enumerate(transactions):
        h = _tx_hash(tx)
        if h and h.lower() in seen_hashes:
            continue
        where = f"tx {h or i}"
        gas, gas_asset, gas_amount = _gas_in_quote(
            tx, quote=quote, fee_prices=fee_prices, where=where
        )
        if gas == 0:
            continue
        meta = {"tx_hash": h, "venue_kind": "onchain_tx"}
        if gas_asset:
            meta["gas_asset"] = gas_asset
            meta["gas_amount"] = gas_amount
        method = _first(tx, "method", "type", "action", "functionName")
        if method is not None:
            meta["method"] = method
        flows.append(CashFlow(
            ts=_timestamp(
                _first(tx, "time", "timestamp", "blockTime", "createTime", "createdAt") or "",
                where=where,
            ),
            symbol=gas_asset or "GAS",
            kind="gas",
            amount=-abs(gas),
            meta=meta,
        ))

    return History(fills=fills, flows=flows, source=f"baw:{_short(address)}")


def _failed_symbol(order: Mapping, *, where: str) -> str | None:
    """The pair a failed swap was aiming at, when both legs are readable.

    Failed orders sometimes carry only the intended amounts, and sometimes not
    even that, so this is best-effort by design: the gas is the fact we are
    recording, and the symbol is context on it.
    """
    try:
        from_asset, from_amount = _leg(order, "from", where=where)
        to_asset, to_amount = _leg(order, "to", where=where)
    except BawError:
        return None
    return _pair(from_asset, from_amount, to_asset, to_amount, where=where)[0]


def _short(address: str) -> str:
    if len(address) > 14:
        return f"{address[:8]}...{address[-4:]}"
    return address or "unknown"


def load(
    *,
    runner: Runner | None = None,
    quote: str = "USDT",
    fee_prices: Mapping[str, float] | None = None,
) -> History:
    """Read the connected wallet's swap history into a `History`.

    Requires a signed-in `baw` session, which requires a human with a phone.
    Raises `WalletNotConnected` when there is not one, and `BawError` for
    anything else the CLI does that we cannot make sense of.

    `runner` takes the argv after the executable and returns stdout; pass one
    to replay captured JSON. `fee_prices` maps a gas asset (BNB) to its price
    in `quote`, and is only needed when the payload does not already carry a
    fiat-valued gas figure.
    """
    run = runner or cli_runner
    address = wallet_address(_payload(run(["wallet", "status", "--json"]), what="wallet status"))
    orders = _records(
        _payload(run(["market-order", "list", "--json"]), what="market-order list"), _ORDER_KEYS
    )
    transactions = _records(
        _payload(run(["wallet", "tx-history", "--json"]), what="wallet tx-history"), _TX_KEYS
    )
    return build_history(
        orders, transactions, address=address, quote=quote, fee_prices=fee_prices
    )
