"""The statistics that decide whether a pattern is real.

This module exists because the difference between Gnosis and a horoscope is
entirely here. It is trivially easy to look at 200 trades, find that the eleven
opened on a Tuesday lost money, and announce that the user has a Tuesday
problem. With enough slices, something always looks damning.

Three rules, enforced by `assess()` and not negotiable by callers:

1. **Minimum sample.** Below `MIN_SAMPLE` observations we report "insufficient
   data" and say nothing else. Being willing to say nothing is what earns the
   right to be believed on everything else.
2. **The interval must exclude zero.** A difference is only claimed when a
   bootstrap confidence interval on it does not straddle zero.
3. **Compare the trader to themselves.** Every split is measured against the
   trader's own baseline on the complement of the slice -- never against an
   external benchmark, a market average, or another user.

No SciPy. The bootstrap is a few lines, it has no distributional assumptions
worth arguing about, and keeping the dependency list at zero is what lets the
deterministic layer run anywhere.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from statistics import mean

# Below this many observations in *either* arm we decline to draw a conclusion.
# Eight is not a deep statistical truth; it is the point below which a single
# outlier trade dominates the mean, which is the failure mode that matters here.
MIN_SAMPLE = 8

# Bootstrap resamples. 2000 is plenty for a CI we round to whole dollars, and
# keeps a full profile run in well under a second.
RESAMPLES = 2000

# How many hypotheses a full profile tests: four pre-registered sessions, one
# test each for disposition, averaging down, revenge, leverage and stop
# migration, and four pre-registered symbol slots. Every detector corrects
# against this, not against its own count -- the user runs the whole profile, so
# the whole profile is the family. Correcting only within a detector is how the
# first version reported a 7.5% false-positive rate.
#
# The symbol detector gets four slots because four is the width of the default
# book; a trader spread across more symbols is running more tests than that, and
# `SymbolCompetence` widens the correction itself for the excess rather than
# quietly spending another detector's budget.
#
# Raising this from 8 to 13 costs the original five detectors a little recall.
# That is the correct direction, not a regression: two more detectors is two
# more chances for chance, and a family size that does not grow when the profile
# does is simply wrong.
PROFILE_FAMILY = int(os.environ.get("GNOSIS_FAMILY", "13"))

# Two-sided 90% interval. Deliberately looser than 95%: this is a personal
# analytics tool whose cost of a miss (never mentioning a real leak) is higher
# than its cost of a marginal false alarm the user can inspect and dismiss.
CI_LOW, CI_HIGH = 5.0, 95.0


@dataclass(frozen=True)
class Comparison:
    """The result of comparing one slice of trades against its complement."""

    n_slice: int
    n_rest: int
    mean_slice: float
    mean_rest: float
    ci_low: float
    ci_high: float
    # Set when the sample was too small to say anything. When this is not None,
    # every other field is meaningless and must not be rendered.
    insufficient: str | None = None

    @property
    def delta(self) -> float:
        """Slice minus baseline. Negative means the slice is the problem."""
        return self.mean_slice - self.mean_rest

    @property
    def significant(self) -> bool:
        """True when the interval excludes zero and both arms are large enough."""
        if self.insufficient is not None:
            return False
        return (self.ci_low > 0) or (self.ci_high < 0)

    @property
    def total_cost(self) -> float:
        """What the slice cost, in the units of the metric, across its trades.

        Reported rather than the per-trade delta because "this habit costs you
        $4,100" is a sentence people act on and "-$29.7 expectancy" is not.
        """
        return self.delta * self.n_slice


def percentile(xs: list[float], p: float) -> float:
    """Linear-interpolated percentile. `xs` need not be sorted."""
    if not xs:
        raise ValueError("percentile of empty sequence")
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def bootstrap_delta(
    slice_vals: list[float], rest_vals: list[float], *, seed: int = 0,
    ci: tuple[float, float] = (CI_LOW, CI_HIGH),
) -> tuple[float, float]:
    """Confidence interval on `mean(slice) - mean(rest)`, by resampling.

    Both arms are resampled independently with replacement, which is the right
    model here: the two groups are different trades, not paired observations.
    """
    rng = random.Random(seed)
    n_s, n_r = len(slice_vals), len(rest_vals)
    # `random.choices` rather than an index loop: identical statistics, an order
    # of magnitude faster, and the difference between a profile that returns in
    # 200 ms and one that returns in two seconds. The eval runs this tens of
    # thousands of times, which is where it actually bites.
    choices = rng.choices
    deltas = [
        sum(choices(slice_vals, k=n_s)) / n_s - sum(choices(rest_vals, k=n_r)) / n_r
        for _ in range(RESAMPLES)
    ]
    return percentile(deltas, ci[0]), percentile(deltas, ci[1])


def family_ci(n_hypotheses: int) -> tuple[float, float]:
    """Tighten the interval when a detector tests several slices at once.

    A detector that pre-registers four sessions and reports the worst one is
    running four tests, and at a 90% interval roughly one trader in three will
    show a "significant" session by chance alone. Splitting the family-wise
    error across the hypotheses (Bonferroni, crude but transparent) is what
    stops the timing detector inventing a pattern in noise.
    """
    alpha = (100.0 - (CI_HIGH - CI_LOW)) / max(1, n_hypotheses)
    return alpha / 2.0, 100.0 - alpha / 2.0


def assess(
    slice_vals: list[float], rest_vals: list[float], *, seed: int = 0,
    ci: tuple[float, float] | None = None,
) -> Comparison:
    """Compare a slice against the trader's own baseline.

    This is the only sanctioned way to turn two groups of trades into a claim.
    Detectors call it and report what it returns; they never compute their own
    means and decide for themselves that a gap looks big enough.
    """
    n_s, n_r = len(slice_vals), len(rest_vals)
    if n_s < MIN_SAMPLE or n_r < MIN_SAMPLE:
        need = MIN_SAMPLE
        return Comparison(
            n_slice=n_s, n_rest=n_r, mean_slice=0.0, mean_rest=0.0,
            ci_low=0.0, ci_high=0.0,
            insufficient=(
                f"needs {need} trades on each side, have {n_s} and {n_r}"
            ),
        )
    lo, hi = bootstrap_delta(slice_vals, rest_vals, seed=seed,
                             ci=ci or (CI_LOW, CI_HIGH))
    return Comparison(
        n_slice=n_s, n_rest=n_r,
        mean_slice=mean(slice_vals), mean_rest=mean(rest_vals),
        ci_low=lo, ci_high=hi,
    )


def expectancy(pnls: list[float]) -> float:
    """Mean net PnL per trade. The number a trader actually recognises."""
    return mean(pnls) if pnls else 0.0


def win_rate(pnls: list[float]) -> float:
    return (sum(1 for p in pnls if p > 0) / len(pnls)) if pnls else 0.0
