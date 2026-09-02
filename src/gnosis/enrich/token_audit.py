"""Retro-scan: run the safety check on every token you already bought.

The `query-token-audit` skill exists to be run *before* a swap. Almost nobody
runs it before a swap. So this module runs it afterwards, across a whole
on-chain history at once, and reports how many of the tokens a trader already
holds or already sold would have failed the check they never made.

That is the entire idea, and it is worth exactly one card line. Getting that
line right is the hard part.

## The claim problem

The obvious line — *"6 of your swaps failed the audit you never ran. 5 went to
zero. Cost: $2,180."* — contains three assertions, and only two of them are
things anyone here can know.

**"Failed the audit"** is knowable: it is the audit service's own verdict,
quoted. **"Cost $2,180"** is knowable, with care: it is the realised PnL on
closed round-trips in those tokens, recomputed FIFO from the fills like every
other number Gnosis prints. **"Went to zero"** is not knowable. Nothing in a
security audit reports a price, and nothing in a trade history reports the
current value of a token still held. A position the trader never sold has no
realised outcome at all, and asserting one would be inventing the most
emotionally loaded number on the card.

So this module computes what the history actually supports and words it that
way:

- tokens whose audit came back **failing**, counted
- tokens with **no audit data**, counted separately and never folded into
  either the pass or the fail bucket
- **realised** PnL on the failing tokens, from closed trips only
- positions in failing tokens that are **still open**, reported as open cost
  basis and explicitly excluded from the realised figure
- a **near-total realised loss** count — closed trips where the trader got back
  5% or less of what they put in. That is the honest, computable neighbour of
  "went to zero": it is about what was realised on exits that actually
  happened, and it says so.

An open bag that is worth nothing today is invisible to this module, and that
understatement is deliberate. Understating a loss is a bounded error. Asserting
a total loss that a later sale contradicts destroys the credibility of every
other number on the card, which is the failure mode the whole project exists to
avoid.

## Absence of data is not a pass

`query-token-audit` answers `hasResult` and `isSupported`, and its own skill
document is explicit that when either is false the risk fields are unreliable
and must not be shown. A token with no coverage is reported here as **no
data**, in its own bucket, and the headline says so. Silently counting
uncovered tokens as clean would make the "how many failed" number depend on the
audit service's coverage rather than on the tokens, and would flatter exactly
the long-tail contracts most likely to be dangerous.

## Shape

`scan()` is offline and pure given a `Fetcher`. The fetcher takes
`(contract_address, chain_id)` and returns the decoded audit payload; the
default one (`http_fetcher`) is the only code that touches a network, and it
imports `urllib` lazily so this module costs nothing to import.

Results are cached by `(contract address, chain id)`. The same token appears in
every swap of it — a trader with 40 CAKE swaps has one CAKE contract — and the
cache turns a per-swap scan into a per-token one. `AuditCache` counts its own
hits so a caller can show the saving.

Contract addresses come from `Fill.meta` where the ingest adapter recorded one,
and otherwise from a `contracts` mapping the caller supplies. `baw_onchain.py`
does not record contract addresses today, so in practice the mapping is how
this gets used; a token with no address resolves to **no data**, never to a
pass. Same rule, one layer earlier.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ..model.events import History, Side, Venue
from ..model.roundtrip import reconstruct

# (contract_address, chain_id) -> decoded audit payload. The seam.
Fetcher = Callable[[str, str], object]

AUDIT_URL = (
    "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/security/token/audit"
)
USER_AGENT = "binance-web3/1.4 (Skill)"
DEFAULT_TIMEOUT = 30.0

# Chain ids as `query-token-audit` spells them. Solana is not numeric, which is
# why chain ids are strings everywhere in this module.
CHAIN_IDS: dict[str, str] = {
    "bsc": "56", "bnb": "56", "bnbchain": "56", "binancesmartchain": "56", "56": "56",
    "base": "8453", "8453": "8453",
    "solana": "CT_501", "sol": "CT_501", "ct_501": "CT_501", "501": "CT_501",
    "ethereum": "1", "eth": "1", "mainnet": "1", "1": "1",
}

# The audit's own risk tiers: 0-1 LOW, 2-3 MEDIUM, 4-5 HIGH. Anything at or
# above this is reported as failing. A hit whose riskType is RISK fails
# regardless of the level, because the service marks those critical.
FAIL_RISK_LEVEL = 4
CAUTION_RISK_LEVEL = 2

# Closed trips that returned 5% or less of what went in. The computable,
# realised neighbour of "went to zero" -- and named for what it measures.
NEAR_TOTAL_LOSS_FRACTION = 0.95

_QUOTE_ASSETS = (
    "USDT", "FDUSD", "USDC", "BUSD", "TUSD", "USDP", "DAI", "USD1", "USD",
    "BNB", "ETH", "BTC", "WBNB", "WETH", "SOL",
)

_ADDRESS_KEYS = (
    "contract_address", "contractAddress", "contract", "token_address", "tokenAddress",
    "base_contract", "baseContract", "to_token_address", "toTokenAddress", "address",
)
_CHAIN_KEYS = ("chain_id", "chainId", "binanceChainId", "chain", "network", "chainName")

Status = str  # "fail" | "caution" | "low_risk" | "no_data"


class AuditError(RuntimeError):
    """The audit service answered with something we will not guess at."""


@dataclass(frozen=True)
class Verdict:
    """One token's audit result, as the service reported it.

    `has_data` is the gate on everything else. When it is false the service
    told us its answer is unreliable, and `risk_level` / `risk_level_enum` /
    `hits` are meaningless -- the skill document says not to show them, and
    nothing downstream here reads them.
    """

    contract: str
    chain_id: str
    has_data: bool
    risk_level: int | None = None
    risk_level_enum: str | None = None
    buy_tax: float | None = None
    sell_tax: float | None = None
    # Titles of the checks that came back hit, split by how the service graded
    # them. `RISK` is critical; `CAUTION` is a warning.
    risks: tuple[str, ...] = ()
    cautions: tuple[str, ...] = ()
    # Why there is no data, when there is none. Shown, not swallowed.
    reason: str | None = None

    @property
    def status(self) -> Status:
        if not self.has_data:
            return "no_data"
        if self.risks or (self.risk_level is not None and self.risk_level >= FAIL_RISK_LEVEL):
            return "fail"
        if self.cautions or (
            self.risk_level is not None and self.risk_level >= CAUTION_RISK_LEVEL
        ):
            return "caution"
        return "low_risk"

    @property
    def summary(self) -> str:
        if not self.has_data:
            return f"no audit data ({self.reason or 'not covered'})"
        parts = [f"{self.risk_level_enum or '?'} risk"]
        if self.risk_level is not None:
            parts[0] += f" ({self.risk_level})"
        if self.risks:
            parts.append(f"critical: {', '.join(self.risks[:3])}")
        elif self.cautions:
            parts.append(f"caution: {', '.join(self.cautions[:3])}")
        if self.sell_tax is not None and self.sell_tax > 0:
            parts.append(f"sell tax {self.sell_tax:g}%")
        return " · ".join(parts)


@dataclass
class TokenAudit:
    """One token: what the audit said, and what the history realised on it.

    The two halves are kept separate on purpose. Everything above `verdict`
    comes from an outside service; everything below it is recomputed from the
    trader's own fills. Nothing in this class combines them into a third claim.
    """

    asset: str
    symbols: tuple[str, ...]          # the pairs it was traded as
    contract: str | None
    chain_id: str | None
    verdict: Verdict

    n_buys: int = 0
    n_closed_trips: int = 0
    n_open_trips: int = 0
    realised_pnl: float = 0.0         # closed trips only, net of fees
    closed_cost_basis: float = 0.0
    # Cost basis of positions still open: `qty_opened * avg_entry`. Unrealised,
    # never added to `realised_pnl`, and an upper bound on what is still at
    # risk -- a position that has been partly scaled out still reports the
    # notional it was opened with. Reported as exposure, never as a loss.
    open_cost_basis: float = 0.0

    @property
    def status(self) -> Status:
        return self.verdict.status

    @property
    def realised_return_pct(self) -> float:
        return (self.realised_pnl / self.closed_cost_basis * 100.0) \
            if self.closed_cost_basis else 0.0

    @property
    def is_near_total_realised_loss(self) -> bool:
        """Closed out for 5% or less of what went in.

        Deliberately *not* called "went to zero". It is a statement about
        exits that happened, on trips that closed, and it is silent about any
        position still open and about the token's price today.
        """
        return (
            self.closed_cost_basis > 0
            and self.realised_pnl <= -NEAR_TOTAL_LOSS_FRACTION * self.closed_cost_basis
        )


@dataclass
class AuditReport:
    """The retro-scan, ready for the card.

    Every count here is of *tokens*, not swaps, because the audit is a property
    of a contract and counting it per swap would let one heavily-traded token
    dominate the headline. `n_buys_on_failing` carries the swap count for
    anyone who wants to phrase it the other way.
    """

    tokens: list[TokenAudit] = field(default_factory=list)
    cache_hits: int = 0
    fetches: int = 0

    @property
    def failing(self) -> list[TokenAudit]:
        return [t for t in self.tokens if t.status == "fail"]

    @property
    def caution(self) -> list[TokenAudit]:
        return [t for t in self.tokens if t.status == "caution"]

    @property
    def low_risk(self) -> list[TokenAudit]:
        return [t for t in self.tokens if t.status == "low_risk"]

    @property
    def no_data(self) -> list[TokenAudit]:
        return [t for t in self.tokens if t.status == "no_data"]

    @property
    def n_buys_on_failing(self) -> int:
        return sum(t.n_buys for t in self.failing)

    @property
    def realised_on_failing(self) -> float:
        """Realised PnL across closed trips in failing tokens. Signed."""
        return sum(t.realised_pnl for t in self.failing)

    @property
    def open_cost_basis_on_failing(self) -> float:
        """What is still in failing tokens, at cost. Unrealised, not a loss."""
        return sum(t.open_cost_basis for t in self.failing)

    @property
    def n_near_total_realised_loss(self) -> int:
        return sum(1 for t in self.failing if t.is_near_total_realised_loss)

    @property
    def headline(self) -> str:
        """One line for the card. Says only what the two sources support."""
        if not self.tokens:
            return "No on-chain token buys to audit."
        n_audited = len(self.tokens) - len(self.no_data)
        if not self.failing:
            base = (
                f"{len(self.tokens)} tokens bought · none of the "
                f"{n_audited} with audit data failed"
            )
            return base + (
                f" · {len(self.no_data)} have no audit data, which is not a pass."
                if self.no_data else "."
            )
        realised = self.realised_on_failing
        out = (
            f"{len(self.failing)} of the {len(self.tokens)} tokens you bought fail the "
            f"audit you never ran, across {self.n_buys_on_failing} swaps. "
            f"You realised {realised:+,.0f} on them."
        )
        if self.n_near_total_realised_loss:
            out += (
                f" {self.n_near_total_realised_loss} closed out for 5% or less of what "
                f"you put in."
            )
        open_basis = self.open_cost_basis_on_failing
        if open_basis > 0:
            n_open = sum(1 for t in self.failing if t.n_open_trips)
            out += (
                f" {n_open} still open, {open_basis:,.0f} at cost — unrealised, and not "
                f"in that figure."
            )
        if self.no_data:
            out += f" {len(self.no_data)} tokens have no audit data, which is not a pass."
        return out

    def lines(self) -> list[str]:
        """Per-token detail, worst first. Receipts for the headline."""
        order = {"fail": 0, "caution": 1, "no_data": 2, "low_risk": 3}
        rows = sorted(
            self.tokens,
            key=lambda t: (order[t.status], t.realised_pnl, t.asset),
        )
        out = []
        for t in rows:
            money = f"{t.realised_pnl:+,.0f} realised" if t.n_closed_trips \
                else "nothing closed"
            openness = f" · {t.open_cost_basis:,.0f} still open" if t.open_cost_basis else ""
            out.append(
                f"{t.status.upper():9s} {t.asset:<10s} {t.n_buys:>3d} buys · "
                f"{money}{openness} · {t.verdict.summary}"
            )
        return out

    def to_dict(self) -> dict:
        return {
            "headline": self.headline,
            "n_tokens": len(self.tokens),
            "n_failing": len(self.failing),
            "n_caution": len(self.caution),
            "n_low_risk": len(self.low_risk),
            "n_no_data": len(self.no_data),
            "n_buys_on_failing": self.n_buys_on_failing,
            "realised_on_failing": round(self.realised_on_failing, 2),
            "open_cost_basis_on_failing": round(self.open_cost_basis_on_failing, 2),
            "n_near_total_realised_loss": self.n_near_total_realised_loss,
            "cache_hits": self.cache_hits,
            "fetches": self.fetches,
            "tokens": [
                {
                    "asset": t.asset,
                    "symbols": list(t.symbols),
                    "contract": t.contract,
                    "chain_id": t.chain_id,
                    "status": t.status,
                    "audit": t.verdict.summary,
                    "risk_level": t.verdict.risk_level,
                    "n_buys": t.n_buys,
                    "n_closed_trips": t.n_closed_trips,
                    "n_open_trips": t.n_open_trips,
                    "realised_pnl": round(t.realised_pnl, 2),
                    "realised_return_pct": round(t.realised_return_pct, 2),
                    "open_cost_basis": round(t.open_cost_basis, 2),
                    "near_total_realised_loss": t.is_near_total_realised_loss,
                }
                for t in self.tokens
            ],
        }


class AuditCache:
    """Memoises verdicts by `(contract address, chain id)`.

    A book with forty CAKE swaps contains one CAKE contract. Without this the
    scan would ask the same question forty times, which is slow, rude to a
    public endpoint, and — since audit results are point-in-time — capable of
    returning two different answers inside one report.

    Address comparison is case-insensitive because EVM addresses are written in
    both checksummed and lowercase form and the same contract must not occupy
    two cache slots.
    """

    def __init__(self, seed: Mapping[tuple[str, str], Verdict] | None = None) -> None:
        self._store: dict[tuple[str, str], Verdict] = {}
        self.hits = 0
        self.fetches = 0
        for key, verdict in (seed or {}).items():
            self._store[self._key(*key)] = verdict

    @staticmethod
    def _key(contract: str, chain_id: str) -> tuple[str, str]:
        return (str(contract).strip().lower(), str(chain_id).strip().upper())

    def __len__(self) -> int:
        return len(self._store)

    def get(self, contract: str, chain_id: str) -> Verdict | None:
        return self._store.get(self._key(contract, chain_id))

    def put(self, verdict: Verdict) -> None:
        self._store[self._key(verdict.contract, verdict.chain_id)] = verdict

    def lookup(self, contract: str, chain_id: str, fetcher: Fetcher) -> Verdict:
        """Cached verdict, or one fetch. Never two fetches for one contract.

        A fetch that fails becomes `no_data` with the failure as its reason,
        and is cached so it is not retried forty times. One unreachable token
        must not end a scan of forty -- but it must also not quietly become a
        pass, which is why it lands in the no-data bucket the report counts
        and prints separately.

        A response that arrives and cannot be *parsed* is a different thing and
        raises: that is a shape this module got wrong, not a token the service
        does not know about.
        """
        cached = self.get(contract, chain_id)
        if cached is not None:
            self.hits += 1
            return cached
        self.fetches += 1
        try:
            payload = fetcher(contract, chain_id)
        except Exception as exc:  # noqa: BLE001 - one failed token must not end the scan
            verdict = Verdict(
                contract=contract, chain_id=chain_id, has_data=False,
                reason=f"lookup failed: {exc}",
            )
            self.put(verdict)
            return verdict
        verdict = parse_audit(payload, contract=contract, chain_id=chain_id)
        self.put(verdict)
        return verdict


def normalise_chain(value: object) -> str | None:
    """A chain id as `query-token-audit` spells it, or None if unrecognised.

    Returning None rather than a default is the point. Auditing a BSC address
    against Ethereum's chain id returns `hasResult: false` at best and someone
    else's contract at worst, so an unrecognised chain becomes "no data".
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return CHAIN_IDS.get(text.lower().replace("-", "").replace("_", "").replace(" ", ""),
                         CHAIN_IDS.get(text.lower()))


def parse_audit(payload: object, *, contract: str, chain_id: str) -> Verdict:
    """Read one `query-token-audit` response.

    `hasResult` and `isSupported` gate everything. The skill document is
    explicit that when either is false the risk fields are unreliable and must
    not be displayed, so this returns `has_data=False` and drops them on the
    floor rather than keeping them around for something downstream to read by
    accident.
    """
    if not isinstance(payload, Mapping):
        raise AuditError(f"{contract}: audit response was not an object: {type(payload).__name__}")

    if "data" in payload and isinstance(payload.get("data"), Mapping):
        code = str(payload.get("code", "000000"))
        if code not in ("000000", "0", "None"):
            return Verdict(contract=contract, chain_id=chain_id, has_data=False,
                           reason=f"service returned code {code}")
        data: Mapping = payload["data"]
    elif payload.get("success") is False:
        return Verdict(contract=contract, chain_id=chain_id, has_data=False,
                       reason=f"service returned code {payload.get('code')}")
    else:
        data = payload

    if not (data.get("hasResult") and data.get("isSupported")):
        return Verdict(
            contract=contract, chain_id=chain_id, has_data=False,
            reason=(
                "not supported on this chain" if data.get("isSupported") is False
                else "no audit result"
            ),
        )

    risks: list[str] = []
    cautions: list[str] = []
    items = data.get("riskItems")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            details = item.get("details")
            if not isinstance(details, list):
                continue
            for detail in details:
                if not isinstance(detail, Mapping) or not detail.get("isHit"):
                    continue
                title = str(detail.get("title") or item.get("name") or "unnamed risk")
                if str(detail.get("riskType", "RISK")).upper() == "CAUTION":
                    cautions.append(title)
                else:
                    risks.append(title)

    extra = data.get("extraInfo") if isinstance(data.get("extraInfo"), Mapping) else {}
    level = data.get("riskLevel")
    return Verdict(
        contract=contract,
        chain_id=chain_id,
        has_data=True,
        # All numeric fields arrive as strings; the skill says to convert.
        risk_level=int(float(level)) if isinstance(level, (int, float, str))
        and str(level).strip() not in ("", "None") else None,
        risk_level_enum=str(data["riskLevelEnum"]) if data.get("riskLevelEnum") else None,
        buy_tax=_tax(extra.get("buyTax")),
        sell_tax=_tax(extra.get("sellTax")),
        risks=tuple(risks),
        cautions=tuple(cautions),
    )


def _tax(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace("%", "").strip())
    except ValueError:
        return None


def base_asset(symbol: str) -> str:
    """The traded token out of a concatenated pair. Longest quote match wins."""
    upper = symbol.upper()
    for quote in sorted(_QUOTE_ASSETS, key=len, reverse=True):
        if upper.endswith(quote) and len(upper) > len(quote):
            return upper[: -len(quote)]
    return upper


def token_targets(
    history: History,
    *,
    contracts: Mapping[str, tuple[str, object]] | None = None,
    onchain_only: bool = True,
) -> dict[str, dict]:
    """Every distinct token ever *bought*, with an address if one can be found.

    Buys only: a token the trader only ever sold was acquired somewhere this
    history cannot see (an airdrop, a transfer in), and the audit line is about
    what they chose to buy.

    Addresses come from `Fill.meta` if the ingest adapter recorded one, and
    otherwise from `contracts`, keyed by either the base asset (`"CAKE"`) or
    the pair (`"CAKEUSDT"`). A token with no address is kept in the result with
    `contract=None` so it can be reported as "no data" — dropping it would
    quietly shrink the denominator and flatter the headline.
    """
    lookup = {str(k).upper(): v for k, v in (contracts or {}).items()}
    targets: dict[str, dict] = {}
    for fill in history.fills:
        if fill.side is not Side.BUY:
            continue
        if onchain_only and fill.venue is not Venue.ONCHAIN:
            continue
        asset = base_asset(fill.symbol)
        entry = targets.setdefault(
            asset, {"asset": asset, "symbols": set(), "contract": None, "chain_id": None,
                    "n_buys": 0},
        )
        entry["symbols"].add(fill.symbol)
        entry["n_buys"] += 1

        meta = fill.meta or {}
        if entry["contract"] is None:
            found = next(
                (str(meta[k]) for k in _ADDRESS_KEYS if meta.get(k) not in (None, "")), None
            )
            if found:
                entry["contract"] = found
        if entry["chain_id"] is None:
            chain = next(
                (meta[k] for k in _CHAIN_KEYS if meta.get(k) not in (None, "")), None
            )
            entry["chain_id"] = normalise_chain(chain)

    for asset, entry in targets.items():
        override = lookup.get(asset)
        if override is None:
            override = next(
                (lookup[s] for s in sorted(entry["symbols"]) if s in lookup), None
            )
        if override is not None:
            address, chain = override
            entry["contract"] = str(address)
            entry["chain_id"] = normalise_chain(chain)
        entry["symbols"] = tuple(sorted(entry["symbols"]))
    return targets


def scan(
    history: History,
    *,
    fetcher: Fetcher,
    contracts: Mapping[str, tuple[str, object]] | None = None,
    cache: AuditCache | None = None,
    onchain_only: bool = True,
) -> AuditReport:
    """Audit every token this history ever bought, and price the damage.

    Offline and deterministic given `fetcher`. Everything monetary is
    recomputed from the fills by the same FIFO reconstruction the rest of
    Gnosis uses, so the numbers here and the numbers on the card cannot
    disagree.
    """
    cache = cache if cache is not None else AuditCache()
    targets = token_targets(history, contracts=contracts, onchain_only=onchain_only)

    closed, still_open = reconstruct(history.fills)
    realised: dict[str, list] = {}
    opened: dict[str, list] = {}
    for trip in closed:
        realised.setdefault(base_asset(trip.symbol), []).append(trip)
    for trip in still_open:
        opened.setdefault(base_asset(trip.symbol), []).append(trip)

    tokens: list[TokenAudit] = []
    for asset, entry in sorted(targets.items()):
        contract, chain_id = entry["contract"], entry["chain_id"]
        if not contract:
            verdict = Verdict(
                contract="", chain_id=chain_id or "", has_data=False,
                reason="no contract address in the history; pass contracts={...}",
            )
        elif not chain_id:
            verdict = Verdict(
                contract=contract, chain_id="", has_data=False,
                reason="no recognised chain id; auditing against the wrong chain "
                       "would answer about a different contract",
            )
        else:
            verdict = cache.lookup(contract, chain_id, fetcher)

        closed_trips = realised.get(asset, [])
        open_trips = opened.get(asset, [])
        tokens.append(TokenAudit(
            asset=asset,
            symbols=entry["symbols"],
            contract=contract,
            chain_id=chain_id,
            verdict=verdict,
            n_buys=entry["n_buys"],
            n_closed_trips=len(closed_trips),
            n_open_trips=len(open_trips),
            realised_pnl=sum(t.net_pnl for t in closed_trips),
            closed_cost_basis=sum(t.notional for t in closed_trips),
            open_cost_basis=sum(t.notional for t in open_trips),
        ))

    return AuditReport(tokens=tokens, cache_hits=cache.hits, fetches=cache.fetches)


def http_fetcher(*, timeout: float = DEFAULT_TIMEOUT) -> Fetcher:
    """The live `query-token-audit` endpoint. The only networked code here.

    `urllib` and `uuid` are imported inside the closure so importing this
    module stays free and cannot fail on a machine with nothing installed —
    the same discipline as `binance_mcp.http_transport`.

    The endpoint is public and unauthenticated: no key, no OAuth, nothing to
    expire. It is also point-in-time, and a `LOW` verdict is not a promise of
    safety — the skill document says so, and so does the card.
    """

    def fetch(contract: str, chain_id: str) -> object:
        import json  # noqa: PLC0415 - lazy, see docstring
        import urllib.error  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415
        import uuid  # noqa: PLC0415

        body = json.dumps({
            "binanceChainId": str(chain_id),
            "contractAddress": contract,
            "requestId": str(uuid.uuid4()),
        }).encode()
        request = urllib.request.Request(  # noqa: S310 - fixed https endpoint
            AUDIT_URL,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": USER_AGENT,
                "source": "agent",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.URLError as exc:  # pragma: no cover - needs a network
            raise AuditError(f"token audit request failed for {contract}: {exc}") from exc

    return fetch


def cached_fetcher(responses: Mapping[tuple[str, str], object]) -> Fetcher:
    """A `Fetcher` over captured responses, keyed by `(contract, chain_id)`.

    For tests and for replaying a scan without re-hitting the endpoint. A key
    that is absent raises; `AuditCache.lookup` turns that into a `no_data`
    verdict whose reason names the missing fixture, so a gap in a replay shows
    up in the no-data bucket rather than being mistaken for coverage.
    """
    table = {(str(c).lower(), str(k).upper()): v for (c, k), v in responses.items()}

    def fetch(contract: str, chain_id: str) -> object:
        key = (str(contract).lower(), str(chain_id).upper())
        if key not in table:
            raise AuditError(f"no captured audit response for {key}")
        return table[key]

    return fetch


def card_line(report: AuditReport) -> str:
    """The single line the Rekt Wrapped card would carry. Kept next to the
    arithmetic that licenses it, so the wording cannot drift from the maths."""
    return report.headline
