"""Leverage that buys volatility instead of edge.

Leverage does not change expectancy in a frictionless world -- it scales both
tails. In practice it reliably makes retail outcomes worse, through two
mechanisms that have nothing to do with the multiplier itself: liquidation
converts a temporary drawdown into a permanent loss, and the psychological
pressure of a large position causes earlier, worse exits.

So the question is not "does this trader use leverage" -- that is a choice, not
a leak. It is whether their own high-leverage trades perform worse *than their
own low-leverage trades*, which is a claim about them and is answerable.
"""

from __future__ import annotations

from ..model.roundtrip import RoundTrip
from ..stats import MIN_SAMPLE, PROFILE_FAMILY, assess, family_ci
from .base import Leak, closed_only

HIGH_LEVERAGE = 10.0


class LeverageDrag:
    """High-leverage trades underperforming the same trader's low-leverage ones.

    Compared on percentage return, not absolute PnL. Absolute would be circular:
    leveraged positions are larger, so of course they move more money. The
    question is whether each dollar risked did worse.
    """

    rule = "leverage_drag"

    def run(self, trips: list[RoundTrip]) -> list[Leak]:
        trips = closed_only(trips)
        high = [t for t in trips if (t.max_leverage or 0) >= HIGH_LEVERAGE]
        low = [t for t in trips if (t.max_leverage or 0) < HIGH_LEVERAGE]
        if len(high) < MIN_SAMPLE or len(low) < MIN_SAMPLE:
            return []

        cmp_ = assess(
            [t.return_pct for t in high], [t.return_pct for t in low], seed=23,
            ci=family_ci(PROFILE_FAMILY),
        )
        if not cmp_.significant or cmp_.delta >= 0:
            return []

        realised = sum(t.net_pnl for t in high)
        worst = min(high, key=lambda t: t.net_pnl)
        return [Leak(
            rule=self.rule,
            title="Leverage is costing you, not helping",
            finding=(
                f"Your {len(high)} trades at {HIGH_LEVERAGE:.0f}x or above returned "
                f"{cmp_.mean_slice:+.2f}% per trade against {cmp_.mean_rest:+.2f}% "
                f"on everything else — the same edge, worse execution. "
                f"They realised {realised:,.0f} in total; the worst single one was "
                f"{worst.net_pnl:,.0f} on {worst.symbol}."
            ),
            confidence="statistical",
            # Cost is expressed in return points, so translate back through the
            # notional actually at risk rather than reporting a percentage as if
            # it were money.
            cost=sum(t.notional for t in high) * cmp_.delta / 100.0,
            n=len(high),
            trade_ids=[t.entry_fill_ids[0] for t in high[:20] if t.entry_fill_ids],
            comparison=cmp_,
            detail={
                "threshold": HIGH_LEVERAGE,
                "mean_return_high_pct": round(cmp_.mean_slice, 3),
                "mean_return_low_pct": round(cmp_.mean_rest, 3),
                "worst_symbol": worst.symbol,
            },
        )]
