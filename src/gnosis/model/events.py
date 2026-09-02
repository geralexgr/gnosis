"""Normalised event types.

Gnosis profiles behaviour across two venues that agree on almost nothing. A
Binance CEX fill knows about leverage, funding and a margin mode; an on-chain
swap knows about gas, slippage and a contract address. Neither knows what the
other means.

Everything upstream is therefore flattened into `Fill` -- a single directional
execution of some size at some price, at some time, for some cost. A swap of
USDT for CAKE is a buy of CAKE; the gas is a fee. That reduction is lossy on
purpose: the behaviour we are looking for (holding losers, sizing up after a
loss, trading at 4am) is venue-independent, and any detector that needed to know
which venue it was looking at would be measuring plumbing rather than a person.

Venue-specific facts that *do* carry behavioural signal survive in `meta`, and
detectors that use them declare it -- see `detectors/onchain.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


# Assets that are pegged to the same thing. A trade between two of them is a
# currency conversion, not a market view, and including them poisons every
# statistic in the profile: on a real export the large majority of
# realised "trades" were stablecoin-to-stablecoin conversions with a median
# absolute return near 0.05%,
# which would have set the baseline expectancy that every behavioural finding is
# measured against.
STABLECOINS = frozenset({
    "USDT", "USDC", "USD1", "FDUSD", "BUSD", "TUSD", "DAI", "USDP", "PYUSD",
    "EUR", "EURI", "TRY", "BRL", "GBP", "AUD", "ARS", "ZAR", "JPY",
})


def split_pair(symbol: str) -> tuple[str, str]:
    """Best-effort (base, quote) split. Returns (symbol, "") if unrecognised.

    Longest quote first, so USD1USDT resolves as (USD1, USDT) rather than
    (USD1USD, T) -- and so that a quote which is a prefix of another cannot
    shadow it.
    """
    for quote in sorted(STABLECOINS | {"BTC", "ETH", "BNB"}, key=len, reverse=True):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return symbol, ""


def is_conversion(symbol: str) -> bool:
    """True when both legs are pegged to the same thing."""
    base, quote = split_pair(symbol)
    return bool(quote) and base in STABLECOINS and quote in STABLECOINS


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        """+1 for buy, -1 for sell. Position deltas are `sign * qty`."""
        return 1 if self is Side.BUY else -1


class Venue(str, Enum):
    SPOT = "spot"
    MARGIN = "margin"
    FUTURES_USDS = "futures_usds"
    FUTURES_COIN = "futures_coin"
    ONCHAIN = "onchain"

    @property
    def is_leveraged(self) -> bool:
        return self in (Venue.MARGIN, Venue.FUTURES_USDS, Venue.FUTURES_COIN)


@dataclass(frozen=True)
class Fill:
    """One execution. The atom of everything Gnosis computes.

    `qty` is always positive -- direction lives in `side`. Storing a signed
    quantity instead reads fine until you try to net two fills and silently
    add a short to a long.
    """

    fill_id: str
    ts: datetime
    symbol: str
    side: Side
    qty: float
    price: float
    fee: float = 0.0
    venue: Venue = Venue.SPOT
    # Only meaningful on leveraged venues. `None` on spot and on-chain, which is
    # different from 1.0 -- an unleveraged spot buy is not a 1x futures position,
    # and the leverage detector must not conflate them.
    leverage: float | None = None
    # Free-form venue detail: tx hash, chain id, contract address, order type,
    # whether it reduced a position. Detectors reach in here by key and must
    # tolerate absence.
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"fill {self.fill_id}: qty must be positive, got {self.qty}")
        if self.price < 0:
            raise ValueError(f"fill {self.fill_id}: price must be non-negative, got {self.price}")
        if self.ts.tzinfo is None:
            raise ValueError(f"fill {self.fill_id}: ts must be timezone-aware")

    @property
    def notional(self) -> float:
        return self.qty * self.price

    @property
    def signed_qty(self) -> float:
        return self.side.sign * self.qty

    @property
    def hour_utc(self) -> int:
        return self.ts.astimezone(timezone.utc).hour


@dataclass(frozen=True)
class OrderEvent:
    """A non-fill order action: placed, cancelled, amended.

    Carried separately from fills because the most expensive habit in retail
    trading -- moving a stop further away while underwater -- leaves no trace in
    the fill stream at all. It is only visible as a cancel-and-replace of a
    protective order at a worse price, so we need the orders that never filled.
    """

    event_id: str
    ts: datetime
    symbol: str
    action: str  # "place" | "cancel" | "amend"
    order_type: str  # "limit" | "stop" | "stop_market" | "take_profit" | ...
    side: Side
    price: float | None = None
    trigger_price: float | None = None
    qty: float | None = None
    meta: dict = field(default_factory=dict)

    @property
    def is_protective(self) -> bool:
        """Stops and take-profits -- orders whose job is to close a position."""
        return "stop" in self.order_type.lower() or "take_profit" in self.order_type.lower()


@dataclass(frozen=True)
class CashFlow:
    """Money that moved without a trade: funding, interest, rebates, airdrops.

    Kept out of `Fill` because these are not decisions. They matter for honest
    PnL accounting -- a carry position's entire result is funding -- but a
    detector looking for behaviour must never treat them as one.
    """

    ts: datetime
    symbol: str
    kind: str  # "funding" | "interest" | "commission_rebate" | "gas" | ...
    amount: float  # signed: negative is a cost
    meta: dict = field(default_factory=dict)


@dataclass
class History:
    """Everything known about one trader, normalised.

    Deliberately a dumb container. It sorts and it slices; it does not compute
    round-trips (that is `roundtrip.py`) and it does not judge (that is the
    detectors). Keeping it inert means the corpus generator and the live
    adapters produce the same thing and nothing downstream can tell them apart.
    """

    fills: list[Fill] = field(default_factory=list)
    orders: list[OrderEvent] = field(default_factory=list)
    flows: list[CashFlow] = field(default_factory=list)
    # Where this came from, for provenance in the report. "synthetic:seed=31",
    # "binance:csv", "baw:0xabc...".
    source: str = "unknown"

    def __post_init__(self) -> None:
        self.sort()

    def sort(self) -> None:
        self.fills.sort(key=lambda f: (f.ts, f.fill_id))
        self.orders.sort(key=lambda o: (o.ts, o.event_id))
        self.flows.sort(key=lambda c: c.ts)

    @property
    def symbols(self) -> list[str]:
        return sorted({f.symbol for f in self.fills})

    @property
    def span_days(self) -> float:
        """Calendar days covered. 0.0 when there is nothing or one fill.

        Detectors use this to refuse to draw conclusions from a week of data,
        which is the difference between a profile and an insult.
        """
        if len(self.fills) < 2:
            return 0.0
        return (self.fills[-1].ts - self.fills[0].ts).total_seconds() / 86400.0

    def for_symbol(self, symbol: str) -> list[Fill]:
        return [f for f in self.fills if f.symbol == symbol]

    def total_fees(self) -> float:
        return sum(f.fee for f in self.fills)
