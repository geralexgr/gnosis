"""Tilt: trading decisions driven by the last result rather than the setup.

Two shapes, opposite signs, same root cause -- the previous trade's outcome
leaking into the next trade's sizing.

*Revenge* is the expensive one. A loss is booked, and the next position opens
sooner and larger than the trader's own baseline. The tell is not that the trade
loses; it is the combination of a shortened gap and an inflated size following a
loss specifically. Anyone can have a bad trade after a bad trade. Almost nobody
*systematically* doubles their size in the forty minutes after one.

*Streak inflation* is the mirror: size creeping up after consecutive wins.
Cheaper on average, but it sets up the drawdown, because the largest position is
reliably the one placed at peak confidence.
"""

from __future__ import annotations

from statistics import median

from ..model.roundtrip import RoundTrip
from ..stats import MIN_SAMPLE, PROFILE_FAMILY, assess, family_ci
from .base import Leak, closed_only


class Revenge:
    """Re-entering sooner and bigger after a loss."""

    rule = "revenge_trading"
    # A trade opened within this long of booking a loss is "immediately after".
    # 90 minutes is generous on purpose -- the effect is about the emotional
    # window, not the clock, and a tight threshold would miss slow markets.
    WINDOW_MIN = 90.0
    MIN_SIZE_RATIO = 1.25  # must actually be bigger, not just sooner

    def run(self, trips: list[RoundTrip]) -> list[Leak]:
        trips = sorted(closed_only(trips), key=lambda t: t.opened_at)
        if len(trips) < MIN_SAMPLE * 2:
            return []

        baseline_notional = median(t.notional for t in trips) or 1.0
        revenge: list[RoundTrip] = []
        normal: list[RoundTrip] = []

        for prev, nxt in zip(trips, trips[1:]):
            # Measured from when the loss was *booked*, not when it was opened.
            if prev.closed_at is None:
                continue
            gap_min = (nxt.opened_at - prev.closed_at).total_seconds() / 60.0
            is_revenge = (
                not prev.is_winner
                and 0 <= gap_min <= self.WINDOW_MIN
                and nxt.notional >= baseline_notional * self.MIN_SIZE_RATIO
            )
            (revenge if is_revenge else normal).append(nxt)

        if len(revenge) < MIN_SAMPLE or len(normal) < MIN_SAMPLE:
            return []

        # Compared on percentage return, not dollars. The revenge arm is 100%
        # oversized trades by construction and the control is the whole rest of
        # the book, so dollar PnL is confounded by size before any behaviour is
        # measured. Tested on null traders with no post-loss behaviour at all
        # and identical percentage returns at every size, the dollar version
        # showed a mean "revenge delta" of -4.93 and called 61% of nulls
        # negative -- a systematic bias, not noise. Every other detector that
        # slices on size compares on `return_pct`; this one now does too.
        cmp_ = assess(
            [t.return_pct for t in revenge], [t.return_pct for t in normal],
            seed=17, ci=family_ci(PROFILE_FAMILY),
        )
        if not cmp_.significant:
            return []

        med_size = median(t.notional for t in revenge)
        return [Leak(
            rule=self.rule,
            title="You trade bigger right after a loss",
            finding=(
                f"{len(revenge)} times you re-entered within {self.WINDOW_MIN:.0f} minutes "
                f"of booking a loss, at {med_size / baseline_notional:.1f}x your usual size. "
                f"Those trades returned {cmp_.mean_slice:+.2f}% each against "
                f"{cmp_.mean_rest:+.2f}% everywhere else."
            ),
            confidence="statistical",
            # Translated back through the notional actually at risk, because
            # the comparison is now in return points, not dollars.
            cost=sum(t.notional for t in revenge) * cmp_.delta / 100.0,
            n=len(revenge),
            trade_ids=[t.entry_fill_ids[0] for t in revenge[:20] if t.entry_fill_ids],
            comparison=cmp_,
            detail={
                "window_minutes": self.WINDOW_MIN,
                "median_revenge_notional": round(med_size, 2),
                "baseline_notional": round(baseline_notional, 2),
                "size_multiple": round(med_size / baseline_notional, 2),
            },
        )]
