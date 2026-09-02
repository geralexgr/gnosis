"""Symbol competence: the two you make money in, and the fifteen you donate to.

Most people are spread far too thin. Edge is specific -- it comes from knowing
how one thing moves -- and a book of seventeen tickers is usually two positions
of conviction and fifteen of boredom.

This is the detector in Gnosis most likely to be nonsense, and the reason is
worth stating before the code rather than after it. The obvious implementation
is to rank symbols by outcome, take the worst, and report it. That fires on
*every* history, including one generated from pure noise, because with four
symbols one of them is always last. It is the same defect that makes
`best_symbols_only` in `counterfactual.py` permanently `hindsight=True` and
permanently unlicensable: the symbol is chosen using the very number then quoted
as evidence about it.

Two ways out, and this detector takes the first:

1. **Test every symbol, and pay for every test.** Each symbol that clears a
   minimum trade count is tested against the trader's own baseline on the
   complement, and the confidence interval is corrected for how many symbols
   were tested. Selection by outcome is still happening -- we report the worst
   survivor -- but it is *paid for*, which is the difference between selection
   and selection bias.
2. Test the dispersion of per-symbol expectancy against a permutation null.
   Cleaner as a global statement ("symbol matters for this trader") but it
   names no symbol, and a finding the user cannot act on is not worth the row
   on the card.

Two further constraints, both there to keep the claim honest rather than merely
significant:

- The metric is **return per trade, not dollars.** A symbol traded in size will
  dominate a dollar comparison whatever the edge, and "you lose money in BTC"
  when BTC is simply where the size is would be measuring the position sizer.
- The symbol must be **losing money outright**, not merely underperforming.
  "Your worst symbol is still profitable" is a fact about a bell curve, not a
  leak, and telling someone to stop trading it would be advice derived from a
  ranking rather than from a loss.
"""

from __future__ import annotations

from collections import Counter

from ..model.roundtrip import RoundTrip
from ..stats import MIN_SAMPLE, PROFILE_FAMILY, assess, family_ci
from .base import Leak, closed_only


class SymbolCompetence:
    """A symbol this trader should not be trading, chosen with the correction paid."""

    rule = "symbol_selection"

    # Higher than MIN_SAMPLE deliberately. Eight trades is enough to compare two
    # arms whose membership was fixed in advance; it is not enough to survive
    # being picked out of a field of candidates afterwards.
    MIN_PER_SYMBOL = 12

    # How many symbol hypotheses `PROFILE_FAMILY` already pays for. A trader who
    # spreads themselves across more than this is running more tests than the
    # family accounts for, so the detector widens the correction itself -- the
    # trader with seventeen tickers must clear a higher bar than the trader with
    # four, because they have given chance seventeen chances instead of four.
    SYMBOL_SLOTS = 4

    def run(self, trips: list[RoundTrip]) -> list[Leak]:
        trips = closed_only(trips)
        counts = Counter(t.symbol for t in trips)
        tested = sorted(
            sym for sym, n in counts.items()
            if n >= self.MIN_PER_SYMBOL and len(trips) - n >= MIN_SAMPLE
        )
        # One symbol is not a selection problem, it is a specialisation. There
        # has to be a choice on the table for "you should not trade this one" to
        # mean anything.
        if len(tested) < 2:
            return []

        ci = family_ci(PROFILE_FAMILY + max(0, len(tested) - self.SYMBOL_SLOTS))
        baseline = sum(t.net_pnl for t in trips)

        out: list[Leak] = []
        for sym in tested:
            inside = [t for t in trips if t.symbol == sym]
            outside = [t for t in trips if t.symbol != sym]
            cmp_ = assess(
                [t.return_pct for t in inside], [t.return_pct for t in outside],
                seed=31, ci=ci,
            )
            if not cmp_.significant or cmp_.delta >= 0:
                continue
            # Worse than the rest of the book *and* losing money in its own
            # right. Without this, the detector reports a ranking.
            if cmp_.mean_slice >= 0:
                continue

            realised = sum(t.net_pnl for t in inside)
            out.append(Leak(
                rule=self.rule,
                title=f"{sym} is where your profits go",
                finding=(
                    f"Your {len(inside)} {sym} trades returned "
                    f"{cmp_.mean_slice:+.2f}% each against {cmp_.mean_rest:+.2f}% "
                    f"across your other {len(outside)} trades, and realised "
                    f"{realised:,.0f} against {baseline:,.0f} for the book as a "
                    f"whole. Tested against all {len(tested)} symbols you trade "
                    f"often enough to judge, not picked after the fact."
                ),
                confidence="statistical",
                # Return points translated back through the notional at risk,
                # rather than reporting a percentage as if it were money.
                cost=sum(t.notional for t in inside) * cmp_.delta / 100.0,
                n=len(inside),
                trade_ids=[t.entry_fill_ids[0] for t in inside[:20] if t.entry_fill_ids],
                comparison=cmp_,
                detail={
                    "symbol": sym,
                    "symbols_tested": tested,
                    "mean_return_symbol_pct": round(cmp_.mean_slice, 3),
                    "mean_return_rest_pct": round(cmp_.mean_rest, 3),
                    "realised_pnl": round(realised, 2),
                },
            ))

        # If several symbols survive the correction, the worst one is the story.
        # The others are the same advice said twice.
        out.sort(key=lambda leak: leak.cost or 0.0)
        return out[:1]
