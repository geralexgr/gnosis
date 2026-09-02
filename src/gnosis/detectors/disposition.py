"""The disposition effect: cutting winners short and letting losers run.

The best-documented bias in retail trading, and the most expensive. Shefrin and
Statman named it in 1985; it has been replicated on brokerage data ever since.
The mechanism is that closing a winner books a gain and feels like being right,
while closing a loser books a loss and feels like being wrong -- so the winner
gets sold and the loser gets held until it "comes back".

What makes it detectable is that it leaves an *asymmetry in hold time* that has
nothing to do with the market. A trader with no bias holds winners and losers
for similar durations, because the decision to exit is driven by the setup, not
by which way the position happens to be. A wide gap is the signature.
"""

from __future__ import annotations

from statistics import median

from ..model.roundtrip import RoundTrip
from ..stats import MIN_SAMPLE, PROFILE_FAMILY, assess, family_ci
from .base import Leak, closed_only


class Disposition:
    """Winners held for far less time than losers."""

    rule = "disposition_effect"
    # Below this ratio the asymmetry is not worth a sentence. 2x is already a
    # strong effect; the literature finds ratios above 1.5 in biased samples.
    MIN_RATIO = 1.8

    def run(self, trips: list[RoundTrip]) -> list[Leak]:
        trips = closed_only(trips)
        winners = [t for t in trips if t.is_winner]
        losers = [t for t in trips if not t.is_winner]
        if len(winners) < MIN_SAMPLE or len(losers) < MIN_SAMPLE:
            return []

        w_hold = median(t.hold_hours for t in winners)
        l_hold = median(t.hold_hours for t in losers)
        if w_hold <= 0:
            return []
        ratio = l_hold / w_hold
        if ratio < self.MIN_RATIO:
            return []

        # Confirm the hold-time gap is not noise, using hold time itself as the
        # metric. The cost is estimated separately below.
        cmp_ = assess(
            [t.hold_hours for t in losers], [t.hold_hours for t in winners], seed=11,
            ci=family_ci(PROFILE_FAMILY),
        )
        if not cmp_.significant:
            return []

        # There is no honest dollar figure for this one, and the earlier
        # attempt was circular: it reported `sum(net_pnl for losers)` -- the
        # sum of a metric over an arm selected *by* that metric. It is
        # guaranteed negative, guaranteed to be the largest magnitude on the
        # card, and it stole the headline from findings that had a real
        # counterfactual behind them. On an EV-neutral trader it reported a
        # -2,350 "cost" against a book that was up 68.
        #
        # Costing it properly would mean replaying the market to see what the
        # winners would have made if held, which the fill stream cannot answer.
        # So the cost is None -- the field exists for exactly this case -- and
        # the finding reports the asymmetry, which is measured and real.
        loser_total = sum(t.net_pnl for t in losers)

        return [Leak(
            rule=self.rule,
            title="You sell winners early and nurse losers",
            finding=(
                f"You hold losers {ratio:.1f}x longer than winners "
                f"(median {l_hold:.1f}h vs {w_hold:.1f}h) across "
                f"{len(winners)} winners and {len(losers)} losers. "
                f"What that asymmetry costs cannot be computed from fills alone "
                f"— it would need the price path you did not trade."
            ),
            confidence="statistical",
            cost=None,
            n=len(winners) + len(losers),
            trade_ids=[t.entry_fill_ids[0] for t in losers[:20] if t.entry_fill_ids],
            comparison=cmp_,
            detail={
                "median_hold_winners_h": round(w_hold, 2),
                "median_hold_losers_h": round(l_hold, 2),
                "ratio": round(ratio, 2),
                "n_winners": len(winners),
                "n_losers": len(losers),
            },
        )]


class Martingale:
    """Adding to positions that are already underwater."""

    rule = "averaging_down"
    MIN_EVENTS = 5

    def run(self, trips: list[RoundTrip]) -> list[Leak]:
        trips = closed_only(trips)
        with_adds = [t for t in trips if t.n_adds_underwater > 0]
        without = [t for t in trips if t.n_adds_underwater == 0]
        total_adds = sum(t.n_adds_underwater for t in with_adds)
        if total_adds < self.MIN_EVENTS or len(with_adds) < MIN_SAMPLE:
            return []

        cmp_ = assess(
            [t.net_pnl for t in with_adds], [t.net_pnl for t in without], seed=13,
            ci=family_ci(PROFILE_FAMILY),
        )
        realised = sum(t.net_pnl for t in with_adds)
        # The behaviour is countable, so it is proof. Only the comparison to
        # baseline is an inference, and we downgrade if that did not hold.
        confidence = "proof" if cmp_.significant else "weak"

        deepest = max(with_adds, key=lambda t: t.n_adds_underwater)
        return [Leak(
            rule=self.rule,
            title="You add to losing positions",
            finding=(
                f"You averaged down {total_adds} times across {len(with_adds)} trades. "
                f"Those trades made {realised:,.0f} against "
                f"{cmp_.mean_rest:,.0f} average on everything else. "
                f"Worst instance: {deepest.n_adds_underwater} adds into one "
                f"{deepest.symbol} position."
            ),
            confidence=confidence,
            cost=cmp_.total_cost if cmp_.significant else None,
            n=len(with_adds),
            trade_ids=[t.entry_fill_ids[0] for t in with_adds[:20] if t.entry_fill_ids],
            comparison=cmp_,
            detail={
                "total_underwater_adds": total_adds,
                "trades_affected": len(with_adds),
                "worst_symbol": deepest.symbol,
                "worst_add_count": deepest.n_adds_underwater,
            },
        )]
