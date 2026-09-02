"""When a trader is good, and when they should be asleep.

Hour-of-day is the single most actionable split in this whole tool, because the
remedy costs nothing: not trading is free, requires no skill, and can be adopted
the same evening.

The statistical hazard is obvious and is handled in `stats.assess`. Slicing 200
trades 24 ways guarantees some hour looks terrible. So we do not test 24 hours;
we test contiguous *sessions* chosen in advance, which is a small number of
pre-registered hypotheses rather than a fishing expedition.
"""

from __future__ import annotations

from ..model.roundtrip import RoundTrip
from ..stats import MIN_SAMPLE, PROFILE_FAMILY, assess, family_ci
from .base import Leak, closed_only

# Fixed in advance, deliberately. These correspond to real market sessions, so
# a finding has a mechanism behind it ("you trade the Asia session badly")
# rather than being an artefact of where the slicing happened to land.
SESSIONS = {
    "the small hours (00:00-06:00 UTC)": range(0, 6),
    "the Asia session (06:00-12:00 UTC)": range(6, 12),
    "the London session (12:00-16:00 UTC)": range(12, 16),
    "the US session (16:00-24:00 UTC)": range(16, 24),
}


class SessionPerformance:
    """A block of hours in which the trader is reliably worse."""

    rule = "session_performance"

    def run(self, trips: list[RoundTrip]) -> list[Leak]:
        trips = closed_only(trips)
        if len(trips) < MIN_SAMPLE * 2:
            return []

        out: list[Leak] = []
        for name, hours in SESSIONS.items():
            inside = [t for t in trips if t.hour_utc in hours]
            outside = [t for t in trips if t.hour_utc not in hours]
            cmp_ = assess(
                [t.net_pnl for t in inside], [t.net_pnl for t in outside], seed=19,
                ci=family_ci(PROFILE_FAMILY),
            )
            # Only report sessions that are *worse*. A session where someone is
            # unusually good is real and interesting, but it belongs in the
            # strengths section, not among the leaks -- see `strengths()`.
            if not cmp_.significant or cmp_.delta >= 0:
                continue
            total = sum(t.net_pnl for t in inside)
            out.append(Leak(
                rule=self.rule,
                title=f"You lose money in {name.split(' (')[0]}",
                finding=(
                    f"Your {len(inside)} trades in {name} averaged "
                    f"{cmp_.mean_slice:,.0f} against {cmp_.mean_rest:,.0f} the rest of "
                    f"the day, for a total of {total:,.0f}."
                ),
                confidence="statistical",
                cost=cmp_.total_cost,
                n=len(inside),
                trade_ids=[t.entry_fill_ids[0] for t in inside[:20] if t.entry_fill_ids],
                comparison=cmp_,
                detail={"session": name, "hours": [h for h in hours]},
            ))
        # If several sessions qualify, the worst one is the story.
        out.sort(key=lambda leak: leak.cost or 0.0)
        return out[:1]


def strengths(trips: list[RoundTrip]) -> list[Leak]:
    """Sessions where the trader is reliably *better* than their baseline.

    Carried deliberately. A tool that only ever reports failure gets read once
    and closed; and more practically, the pre-trade gate needs to know when to
    say "this is your best setup and you habitually size it too small".
    """
    trips = closed_only(trips)
    if len(trips) < MIN_SAMPLE * 2:
        return []
    out: list[Leak] = []
    for name, hours in SESSIONS.items():
        inside = [t for t in trips if t.hour_utc in hours]
        outside = [t for t in trips if t.hour_utc not in hours]
        cmp_ = assess([t.net_pnl for t in inside], [t.net_pnl for t in outside], seed=19,
                      ci=family_ci(PROFILE_FAMILY))
        # Better than baseline is not the same as good. A session that loses
        # less than the others is not a strength, and calling it one makes the
        # whole card sound like it is grading on a curve.
        if not cmp_.significant or cmp_.delta <= 0 or cmp_.mean_slice <= 0:
            continue
        out.append(Leak(
            rule="session_strength",
            title=f"You are good in {name.split(' (')[0]}",
            finding=(
                f"Your {len(inside)} trades in {name} averaged {cmp_.mean_slice:,.0f} "
                f"against {cmp_.mean_rest:,.0f} elsewhere."
            ),
            confidence="statistical",
            cost=cmp_.total_cost,  # positive here
            n=len(inside),
            trade_ids=[t.entry_fill_ids[0] for t in inside[:20] if t.entry_fill_ids],
            comparison=cmp_,
            detail={"session": name},
        ))
    out.sort(key=lambda leak: -(leak.cost or 0.0))
    return out[:1]
