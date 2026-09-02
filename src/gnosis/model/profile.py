"""The Profile: everything Gnosis knows about one trader, as a portable object.

This is the artifact. The card renders it, the pre-trade gate queries it, and
`profile.json` is what a user would hand to an agent. Nothing downstream is
allowed to recompute statistics -- if a number is not in here, it does not get
shown.

One rule is enforced here rather than in the renderers, because it is the
difference between an analytics tool and astrology:

    **A counterfactual only surfaces if a detector found a significant leak in
    the same dimension.**

Counterfactuals are raw arithmetic with no significance test -- slice any
history four ways and one slice will look better removed. On a trader with no
night-time problem, "you'd be up $162 if you never traded at 4am" is pure noise
presented as insight. Requiring a detector to have already proven the leak is
what stops that, and it is why `for_history` assembles both and cross-checks
them instead of just concatenating two lists.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..counterfactual import Combined, Counterfactual, all_counterfactuals, combine
from ..detectors import run_all, strengths
from ..detectors.base import Leak
from ..stats import expectancy, win_rate
from .events import History, is_conversion
from .roundtrip import RoundTrip, reconstruct, reconstruct_realised

# A trade smaller than this in quote currency is not a decision. Binance's own
# minimum notional sits around 5 USDT, so anything at a fraction of one is
# residue -- a sliver left by FIFO matching, or an exchange artifact. They are
# excluded before any statistic is computed, because a position of $0.0001 that
# paid a $1.20 fee is a -860,000% return, and one of those in an average is
# enough to make the leverage detector meaningless.
MIN_TRADE_NOTIONAL = 1.0

# Which detector rule licenses which counterfactual to be shown.
COUNTERFACTUAL_LICENCE = {
    "skip_session": "session_performance",
    "cap_leverage": "leverage_drag",
    "never_average_down": "averaging_down",
    # Deliberately absent: `best_symbols_only` is hindsight-selected and can
    # never be licensed. It is retained in the profile for interest and
    # rendered as an observation, never as a recommendation.
}


@dataclass
class Summary:
    n_trades: int
    n_open: int
    span_days: float
    total_pnl: float
    total_fees: float
    win_rate: float
    expectancy: float
    symbols: list[str]
    source: str

    @property
    def is_thin(self) -> bool:
        """Too little history to profile honestly.

        Below this, the detectors will mostly return "insufficient data"
        anyway, but saying so once and clearly is better than a report full of
        shrugs.
        """
        return self.n_trades < 30 or self.span_days < 14


@dataclass
class Profile:
    summary: Summary
    # Only findings that clear the bar. `base.py` documents that `weak`
    # findings never surface on their own; enforcing that here rather than in
    # each renderer is what makes the guarantee real -- the first version
    # documented it and then rendered them anyway, which is where a chunk of
    # the measured false-positive rate came from.
    leaks: list[Leak] = field(default_factory=list)
    # Underpowered patterns, kept for the gate and for debugging. Never rendered.
    watch: list[Leak] = field(default_factory=list)
    strengths: list[Leak] = field(default_factory=list)
    counterfactuals: list[Counterfactual] = field(default_factory=list)
    # Kept unlicensed so the renderer can show them under a different heading.
    observations: list[Counterfactual] = field(default_factory=list)
    # The joint effect of the licensed rules. Individual counterfactuals are not
    # additive and read as though they are, so this is computed once here and
    # rendered alongside them rather than left for the reader to get wrong.
    combined: Combined | None = None
    trips: list[RoundTrip] = field(default_factory=list, repr=False)

    @property
    def worst_leak(self) -> Leak | None:
        """The headline finding: the most expensive habit where one is costed.

        Falls back to the first surviving leak when none carry a dollar figure.
        Some findings deliberately have no cost -- the disposition effect can be
        measured but not priced from fills alone -- and a card that dropped its
        headline section entirely for such a trader would be hiding its best
        finding because it could not put a number on it.
        """
        costed = [leak for leak in self.leaks if leak.costs_money]
        if costed:
            return min(costed, key=lambda leak: leak.cost or 0.0)
        return self.leaks[0] if self.leaks else None

    @property
    def total_leak_cost(self) -> float:
        return sum(leak.cost for leak in self.leaks if leak.costs_money)

    def to_dict(self) -> dict[str, Any]:
        """Portable form. This is what an agent consumes."""
        # `is_thin` is a property, so `asdict` drops it. An agent reading this
        # JSON must be able to see that the profile declined to have an opinion
        # without re-deriving the rule, so it is surfaced explicitly.
        summary = asdict(self.summary)
        summary["is_thin"] = self.summary.is_thin
        return {
            "summary": summary,
            "leaks": [
                {
                    "rule": leak.rule, "title": leak.title, "finding": leak.finding,
                    "confidence": leak.confidence, "cost": leak.cost, "n": leak.n,
                    "trade_ids": leak.trade_ids, "detail": leak.detail,
                }
                for leak in self.leaks
            ],
            "strengths": [
                {"rule": s.rule, "title": s.title, "finding": s.finding,
                 "value": s.cost, "n": s.n, "detail": s.detail}
                for s in self.strengths
            ],
            "counterfactuals": [
                {k: v for k, v in asdict(c).items() if k != "removed_keys"}
                for c in self.counterfactuals
            ],
            "counterfactuals_combined": (
                asdict(self.combined) if self.combined else None
            ),
            "observations": [
                {k: v for k, v in asdict(c).items() if k != "removed_keys"}
                for c in self.observations
            ],
        }


def for_history(history: History, *, realised: bool | None = None) -> Profile:
    """Build a complete profile from a normalised history.

    `realised` picks the trade model. `None` chooses automatically: a book that
    is mostly spot is reconstructed as realised sales, because a spot investor
    never goes flat and flat-to-flat would find almost nothing. A leveraged book
    is reconstructed flat-to-flat, which is the decision unit there. See
    `roundtrip.reconstruct_realised`.
    """
    # Stablecoin-to-stablecoin conversions are excluded before anything is
    # measured. They are not market decisions, and they are numerous enough in a
    # real book to set the baseline that every finding is compared against.
    fills = [f for f in history.fills if not is_conversion(f.symbol)]

    if realised is None:
        leveraged = sum(1 for f in fills if f.venue.is_leveraged)
        realised = leveraged < len(fills) / 2 if fills else False

    closed, still_open = (
        reconstruct_realised(fills) if realised else reconstruct(fills)
    )
    closed = [t for t in closed if t.notional >= MIN_TRADE_NOTIONAL]

    pnls = [t.net_pnl for t in closed]
    summary = Summary(
        n_trades=len(closed),
        n_open=len(still_open),
        span_days=round(history.span_days, 1),
        total_pnl=round(sum(pnls), 2),
        total_fees=round(history.total_fees(), 2),
        win_rate=round(win_rate(pnls), 4),
        expectancy=round(expectancy(pnls), 2),
        symbols=history.symbols,
        source=history.source,
    )

    # Order events reach only the detectors that ask for them (stop migration
    # is the one that does). Filtered for conversions on the same basis as the
    # fills, so a stablecoin conversion's orders cannot be correlated to a trade.
    orders = [o for o in history.orders if not is_conversion(o.symbol)]
    all_leaks = run_all(closed, orders)
    leaks = [leak for leak in all_leaks if leak.confidence != "weak"]
    watch = [leak for leak in all_leaks if leak.confidence == "weak"]
    # A licence is (rule, subject). The session detector reports which session
    # it proved; only that session's counterfactual may be shown. Detectors
    # whose finding has no subject licence the whole family.
    licences: set[tuple[str, str | None]] = set()
    for leak in leaks:
        # A detector that proved a *specific* slice (which session) licenses
        # only that slice. One with no slice licenses its whole family.
        subject = leak.detail.get("session")
        licences.add((leak.rule, subject) if subject else (leak.rule, None))

    licensed, unlicensed = [], []
    for cf in all_counterfactuals(closed):
        required = COUNTERFACTUAL_LICENCE.get(cf.rule)
        ok = required is not None and (
            (required, cf.subject) in licences or (required, None) in licences
        )
        (licensed if ok else unlicensed).append(cf)

    licensed_combined = combine(closed, licensed)

    return Profile(
        summary=summary,
        leaks=leaks,
        combined=licensed_combined,
        watch=watch,
        strengths=strengths(closed),
        counterfactuals=licensed,
        observations=unlicensed,
        trips=closed,
    )
