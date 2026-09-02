"""Elenchos: the pre-trade cross-examination.

Named for the Socratic ἔλεγχος -- the method of refuting someone using only
their own prior statements. That is exactly the mechanism. The gate asserts no
rule of its own and holds no opinion about markets. It looks up what this
trader has historically done in situations resembling the one in front of them,
and reports the base rate.

Two design commitments, both load-bearing:

**It never blocks.** It returns a verdict and gets out of the way. A tool that
refuses gets uninstalled the first time it is wrong during a fast market, and
then protects nobody. Quoting a base rate and letting the human overrule it is
the only version that survives contact with a real trader.

**It speaks in both directions.** If the proposed trade matches the trader's
*best* historical pattern, it says so -- including when they habitually size
that pattern too small. A gate that only ever says "no" is a brake, and brakes
get disabled. This is the single biggest difference between Elenchos and the
risk-limit products traders already ignore.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from ..detectors.sizing import HIGH_LEVERAGE
from ..detectors.timing import SESSIONS
from ..model.profile import Profile
from ..model.events import Side
from ..model.roundtrip import RoundTrip
from statistics import median

from ..stats import (
    MIN_SAMPLE,
    PROFILE_FAMILY,
    Comparison,
    assess,
    expectancy,
    family_ci,
    win_rate,
)

Verdict = Literal["favourable", "proceed", "caution", "skip"]


@dataclass
class ProposedTrade:
    """What an agent (or a human) is about to do."""

    symbol: str
    side: str                    # "buy" | "sell"
    notional: float
    ts: datetime
    leverage: float | None = None
    # State the caller must supply, because it cannot be inferred from the
    # proposal alone. These are exactly the conditions the leaks are about.
    adding_to_losing_position: bool = False
    minutes_since_last_loss: float | None = None

    @property
    def hour_utc(self) -> int:
        return self.ts.hour


@dataclass
class Analogue:
    """One dimension on which the proposal resembles past trades.

    Carries its own significance test. An earlier version did not, and the
    consequence was severe enough to be worth recording: the detectors were
    bootstrap-gated and family-corrected while the gate compared raw means and
    required only a minimum sample. Measured on *clean* synthetic traders with
    no planted leak, that gate returned a strong verdict on 19 of 24 neutral
    proposals. The astrology had simply moved downstream of the detectors.
    """

    dimension: str          # "the small hours (00:00-06:00 UTC)"
    n: int
    expectancy: float
    win_rate: float
    baseline_expectancy: float
    comparison: Comparison | None = None

    @property
    def delta(self) -> float:
        return self.expectancy - self.baseline_expectancy

    @property
    def significant(self) -> bool:
        """Does this dimension differ from the trader's baseline by more than noise?"""
        return self.comparison is not None and self.comparison.significant


@dataclass
class Judgement:
    verdict: Verdict
    headline: str
    analogues: list[Analogue] = field(default_factory=list)
    # What the trader's own record suggests instead. None when nothing to say.
    suggested_notional: float | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def is_warning(self) -> bool:
        return self.verdict in ("caution", "skip")


# The window the Revenge detector uses. Kept identical on purpose: the gate and
# the profile must not disagree about what "soon after a loss" means, or a user
# is told two different things about the same behaviour.
REVENGE_WINDOW_MIN = 90.0


def _after_loss_oversized(trips: list[RoundTrip], median_notional: float) -> list[RoundTrip]:
    """Trades that were both oversized and opened soon after booking a loss."""
    ordered = sorted(trips, key=lambda t: t.opened_at)
    out: list[RoundTrip] = []
    for prev, nxt in zip(ordered, ordered[1:]):
        if prev.closed_at is None or prev.is_winner:
            continue
        gap = (nxt.opened_at - prev.closed_at).total_seconds() / 60.0
        if 0 <= gap <= REVENGE_WINDOW_MIN and nxt.notional >= median_notional * 1.25:
            out.append(nxt)
    return out


def _matching(trips: list[RoundTrip], predicate) -> list[RoundTrip]:
    return [t for t in trips if predicate(t)]


def _analogue(trips: list[RoundTrip], predicate, dimension: str,
              baseline: float) -> Analogue | None:
    """Base rate for trades resembling the proposal on one dimension.

    Held to exactly the standard the detectors are held to: the same bootstrap
    interval and the same family correction. A base rate the trader can see but
    which does not differ from their baseline is reported as context and is not
    allowed to drive a verdict.
    """
    matched = _matching(trips, predicate)
    # Identity, not equality. `t not in matched` compared ~18 dataclass fields
    # n x |matched| times -- quadratic, and wrong: two field-identical trips
    # compare equal, so the complement dropped trades it should have kept. The
    # caller at the post-loss analogue already builds its predicate on `id(t)`
    # for exactly this reason.
    matched_ids = {id(t) for t in matched}
    rest = [t for t in trips if id(t) not in matched_ids]
    if len(matched) < MIN_SAMPLE:
        return None
    pnls = [t.net_pnl for t in matched]
    cmp_ = (
        assess([t.net_pnl for t in matched], [t.net_pnl for t in rest],
               seed=29, ci=family_ci(PROFILE_FAMILY))
        if len(rest) >= MIN_SAMPLE
        else None
    )
    return Analogue(
        dimension=dimension, n=len(matched),
        expectancy=expectancy(pnls), win_rate=win_rate(pnls),
        baseline_expectancy=baseline, comparison=cmp_,
    )


def check(profile: Profile, proposed: ProposedTrade) -> Judgement:
    """Cross-examine a proposed trade against the trader's own record."""
    trips = profile.trips
    if profile.summary.is_thin:
        return Judgement(
            verdict="proceed",
            headline="Not enough history to have an opinion.",
            reasons=[
                f"Only {profile.summary.n_trades} closed trades over "
                f"{profile.summary.span_days:.0f} days. Gnosis needs more before it "
                f"can tell you anything you should act on."
            ],
        )

    baseline = profile.summary.expectancy
    analogues: list[Analogue] = []

    session = next(
        (name for name, hours in SESSIONS.items() if proposed.hour_utc in hours), None
    )
    if session:
        hours = SESSIONS[session]
        a = _analogue(trips, lambda t: t.hour_utc in hours, session, baseline)
        if a:
            analogues.append(a)

    if proposed.leverage and proposed.leverage >= HIGH_LEVERAGE:
        a = _analogue(
            trips, lambda t: (t.max_leverage or 0) >= HIGH_LEVERAGE,
            f"at {HIGH_LEVERAGE:.0f}x or above", baseline,
        )
        if a:
            analogues.append(a)

    if proposed.adding_to_losing_position:
        a = _analogue(
            trips, lambda t: t.n_adds_underwater > 0,
            "adding to a losing position", baseline,
        )
        if a:
            analogues.append(a)

    if proposed.minutes_since_last_loss is not None and proposed.minutes_since_last_loss <= 90:
        # `sorted(...)[len//2]` is the upper-middle element, not the median:
        # on [100, 200, 300, 400] it gives 300 where the median is 250. The
        # Revenge detector uses a true median for the same threshold, so the
        # gate and the profile disagreed about "your usual size" on the same
        # book -- the exact failure the comment above REVENGE_WINDOW_MIN says
        # must not happen.
        median_notional = median(t.notional for t in trips) if trips else 0.0
        if proposed.notional >= median_notional * 1.25:
            # The comparison set must satisfy *both* halves of the label. An
            # earlier version filtered only on size, so a base rate over every
            # oversized trade was presented as one over post-loss oversized
            # trades -- a different and much larger population. A label that
            # does not describe its own sample is worse than no label.
            oversized_after_loss = _after_loss_oversized(trips, median_notional)
            ids = {id(t) for t in oversized_after_loss}
            a = _analogue(
                trips, lambda t: id(t) in ids,
                "sized well above your usual, soon after a loss", baseline,
            )
            if a:
                analogues.append(a)

    # Direction matters, so the symbol analogue keys on it when the trader has
    # enough trades that way. Falls back to symbol alone rather than adding a
    # second hypothesis to the family.
    want = Side.BUY if proposed.side == "buy" else Side.SELL
    directional = [t for t in trips if t.symbol == proposed.symbol and t.direction is want]
    if len(directional) >= MIN_SAMPLE:
        a = _analogue(
            trips, lambda t: t.symbol == proposed.symbol and t.direction is want,
            f"{proposed.side}ing {proposed.symbol}", baseline,
        )
    else:
        a = _analogue(
            trips, lambda t: t.symbol == proposed.symbol, f"on {proposed.symbol}", baseline
        )
    if a:
        analogues.append(a)

    return _judge(profile, proposed, analogues, baseline)


def _judge(profile: Profile, proposed: ProposedTrade,
           analogues: list[Analogue], baseline: float) -> Judgement:
    """Turn the base rates into a verdict.

    Deliberately arithmetic rather than a model call. The model's job is to
    explain this judgement to a human, never to make it -- a model asked
    "should I take this trade?" produces an answer whose confidence is
    unrelated to the evidence.

    Only *significant* analogues may drive a verdict. Everything else is
    reported as context. This is the same bar the detectors clear, and holding
    the gate to a lower one is what let an earlier version return a strong
    opinion on 19 of 24 neutral proposals against clean traders.
    """
    if not analogues:
        return Judgement(
            verdict="proceed",
            headline="Nothing in your history resembles this closely enough to judge.",
            reasons=["No dimension had enough comparable trades to quote a base rate."],
        )

    reasons = [
        f"{a.dimension}: {a.n} trades, {a.win_rate:.0%} win rate, "
        f"{a.expectancy:+,.0f} per trade against your {baseline:+,.0f} baseline"
        + ("" if a.significant else "  (within noise)")
        for a in analogues
    ]

    decisive = [a for a in analogues if a.significant]
    if not decisive:
        return Judgement(
            verdict="proceed",
            headline=(
                "Your record has base rates for this, but none of them differ from "
                "your baseline by more than noise."
            ),
            analogues=analogues,
            reasons=reasons,
        )

    median_notional = median(t.notional for t in profile.trips)
    worst = min(decisive, key=lambda a: a.delta)
    best = max(decisive, key=lambda a: a.delta)

    if worst.delta < 0:
        # Losing money outright is a skip; merely underperforming your own
        # baseline while still profitable is a caution. Scale-free, unlike the
        # ratio-against-baseline this used to compute -- which sent traders
        # whose baseline sat near zero straight to "skip" off a few dollars.
        verdict: Verdict = "skip" if worst.expectancy < 0 else "caution"
        suggested = None
        if verdict == "skip":
            candidate = round(median_notional * 0.5, 2)
            # Only worth saying if it is materially different from the ask.
            if candidate < proposed.notional * 0.9:
                suggested = candidate
        return Judgement(
            verdict=verdict,
            headline=(
                f"This matches your most expensive pattern — {worst.dimension}. "
                f"{worst.n} prior trades, {worst.win_rate:.0%} win rate, "
                f"{worst.expectancy:+,.0f} each."
            ),
            analogues=analogues,
            suggested_notional=suggested,
            reasons=reasons,
        )

    if best.delta > 0 and best.expectancy > 0:
        suggested = None
        # The other half of the job: when this is their best setup and they are
        # about to under-size it, say so.
        if proposed.notional < median_notional * 0.9:
            suggested = round(median_notional, 2)
        return Judgement(
            verdict="favourable",
            headline=(
                f"This matches one of your better patterns — {best.dimension}. "
                f"{best.n} prior trades, {best.win_rate:.0%} win rate, "
                f"{best.expectancy:+,.0f} each."
            ),
            analogues=analogues,
            suggested_notional=suggested,
            reasons=reasons,
        )

    return Judgement(
        verdict="proceed",
        headline="Nothing in your record argues against this.",
        analogues=analogues,
        reasons=reasons,
    )
