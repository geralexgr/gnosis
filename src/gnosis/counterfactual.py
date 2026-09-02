"""The alternate universes: what one rule, applied consistently, would be worth.

These are the most persuasive numbers Gnosis produces and the easiest to abuse,
so the method is deliberately narrow. A counterfactual here is always the same
operation: **take the trades matching a rule, remove them, and re-total.**
Nothing is re-simulated, no price path is invented, no trade is re-sized.

That buys honesty at the cost of realism, and the trade is worth naming:

- It assumes the remaining trades are unchanged. A trader who stops trading at
  4am may simply trade more at noon, and this cannot see that.
- It is *descriptive of the past*, not a forecast. "Skipping the small hours
  would have been worth $4,100" is a statement about trades that happened.
- Rules chosen by looking at the outcome are survivorship-biased by
  construction. `best_symbols_only` is the clearest offender, so it carries an
  explicit `hindsight=True` flag and the renderer labels it differently.

Every counterfactual reports the trades it removed, so the arithmetic can be
checked by hand.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .detectors.sizing import HIGH_LEVERAGE
from .detectors.timing import SESSIONS
from .model.roundtrip import RoundTrip


@dataclass
class Counterfactual:
    rule: str
    description: str      # "never traded 00:00-06:00 UTC"
    delta: float          # what applying the rule would have been worth
    n_removed: int
    actual_total: float
    hypothetical_total: float
    # True when the rule was chosen by looking at the answer. Such results are
    # not advice and must be rendered as observations, never recommendations.
    hindsight: bool = False
    # The specific slice this counterfactual concerns -- the session name, the
    # leverage threshold. A licence must match the subject, not just the rule:
    # proving that 00:00-06:00 is bad does not license a claim about 06:00-12:00.
    subject: str | None = None
    # A readable sample, capped, for display.
    trade_ids: list[str] = field(default_factory=list)
    # Every trade this rule removes. Not rendered -- it exists so overlap
    # between counterfactuals can be computed honestly. Without it, three rules
    # that remove largely the same trades each report their full value against
    # the same baseline, and a reader adds them up.
    removed_keys: frozenset = field(default_factory=frozenset)

    @property
    def helps(self) -> bool:
        return self.delta > 0


def _apply(trips: list[RoundTrip], keep, rule: str, description: str,
           *, hindsight: bool = False, subject: str | None = None) -> Counterfactual:
    """Total with `keep` applied, against the actual total."""
    actual = sum(t.net_pnl for t in trips)
    kept = [t for t in trips if keep(t)]
    removed = [t for t in trips if not keep(t)]
    hypo = sum(t.net_pnl for t in kept)
    return Counterfactual(
        rule=rule,
        description=description,
        delta=hypo - actual,
        n_removed=len(removed),
        actual_total=actual,
        hypothetical_total=hypo,
        hindsight=hindsight,
        subject=subject,
        trade_ids=[t.entry_fill_ids[0] for t in removed[:20] if t.entry_fill_ids],
        removed_keys=frozenset(id(t) for t in removed),
    )


def skip_session(trips: list[RoundTrip], name: str) -> Counterfactual:
    hours = SESSIONS[name]
    return _apply(
        trips, lambda t: t.hour_utc not in hours,
        "skip_session", f"never traded {name}", subject=name,
    )


def cap_leverage(trips: list[RoundTrip], cap: float = HIGH_LEVERAGE) -> Counterfactual:
    """Skip trades above a leverage cap.

    Note this removes them rather than re-sizing them at the cap, which is the
    conservative reading: we cannot know what a 5x version of a 20x trade would
    have done, because the exit was itself a function of the pressure.
    """
    return _apply(
        trips, lambda t: (t.max_leverage or 0) < cap,
        "cap_leverage", f"never traded above {cap:.0f}x", subject=f"{cap:.0f}x",
    )


def never_average_down(trips: list[RoundTrip]) -> Counterfactual:
    return _apply(
        trips, lambda t: t.n_adds_underwater == 0,
        "never_average_down", "never added to a losing position",
    )


def best_symbols_only(trips: list[RoundTrip], k: int = 2) -> Counterfactual:
    """Keep only the k symbols that made the most money.

    Flagged `hindsight` because the symbols are selected using the very
    outcome being measured. It is included because traders find it
    illuminating -- concentration is real and most people are spread far too
    thin -- but it is an observation about the past, not a strategy.
    """
    by_symbol: dict[str, float] = defaultdict(float)
    for t in trips:
        by_symbol[t.symbol] += t.net_pnl
    best = {s for s, _ in sorted(by_symbol.items(), key=lambda kv: -kv[1])[:k]}
    return _apply(
        trips, lambda t: t.symbol in best,
        "best_symbols_only", f"only traded {', '.join(sorted(best))}",
        hindsight=True,
    )


def all_counterfactuals(trips: list[RoundTrip]) -> list[Counterfactual]:
    """Every universe, best first. Only the ones that would have helped."""
    closed = [t for t in trips if not t.is_open]
    if not closed:
        return []
    out = [
        cap_leverage(closed),
        never_average_down(closed),
        best_symbols_only(closed),
    ]
    out.extend(skip_session(closed, name) for name in SESSIONS)
    # A rule that removed nothing is not a finding, it is a no-op.
    out = [c for c in out if c.n_removed > 0 and c.helps]
    out.sort(key=lambda c: -c.delta)
    return out


@dataclass
class Combined:
    """The joint effect of several rules applied together.

    Necessary because individual counterfactuals are not additive and look as
    though they are. On the demo book, three licensed rules report +13,082,
    +12,484 and +9,584 against a book that lost 11,577 in total -- they sum to
    three times the entire loss. Nothing is wrong with any single figure; they
    simply describe overlapping sets of trades, each measured against the same
    untouched baseline. The night trades *are* the high-leverage trades *are*
    the trades in the symbol that loses money.

    Presenting them without this is the one place where a project built on not
    overstating things quietly overstates by 3x.
    """

    delta: float
    n_removed: int
    n_overlapping: int
    rules: list[str]

    @property
    def has_overlap(self) -> bool:
        return self.n_overlapping > 0


def combine(trips: list[RoundTrip], cfs: list[Counterfactual]) -> Combined | None:
    """Apply every rule at once and re-total. `None` for fewer than two rules."""
    if len(cfs) < 2:
        return None
    closed = [t for t in trips if not t.is_open]
    union: set = set()
    for cf in cfs:
        union |= cf.removed_keys
    kept = [t for t in closed if id(t) not in union]
    actual = sum(t.net_pnl for t in closed)
    # Sum of the individual removal counts, minus the true union size, is the
    # number of trades that more than one rule claims.
    overlap = sum(cf.n_removed for cf in cfs) - len(union)
    return Combined(
        delta=sum(t.net_pnl for t in kept) - actual,
        n_removed=len(union),
        n_overlapping=max(0, overlap),
        rules=[cf.description for cf in cfs],
    )
