"""The typed contract between the arithmetic and the model.

This module is the border post. On one side is everything Gnosis computed and
significance-tested; on the other is a model that will happily write a fluent
paragraph about a leak that does not exist. The only traffic allowed across is
a `NarrationRequest` going out and a `Narration` coming back, and both are
structured objects rather than free text.

Three properties are deliberate and each of them is load-bearing:

**The request carries facts, not history.** A request holds leak titles, the
findings the detectors already phrased, costs, sample sizes and the summary
statistics. It does not hold the fills, the round-trips, or anything the model
could slice for itself. A model handed 257 trades and asked what it notices
will notice something; a model handed four proven findings can only rearrange
those four. Restricting the input is cheaper and far more reliable than
instructing the output.

**Every request can enumerate its own vocabulary of numbers.** `values()` and
`strings()` exist so the fact-checker in `narrate.py` can ask a request "is
this figure one of yours?" without knowing what kind of request it is. Because
the same extractor runs over the request's prose and over the model's prose,
the check is symmetric by construction rather than by a list of special cases
somebody has to remember to update.

**The result records its own provenance.** A `Narration` says whether a model
wrote it or a template did, and if a model was tried and rejected, why. A
narration layer that silently swallows a hallucination and prints a template
is indistinguishable, from the outside, from one that never had a problem --
and the difference is the only thing the user would want to know.

The verdict is conspicuously an *input* on `JudgementRequest` and an echoed
input on `VerdictNarration`. `gate/elenchos.py` decides it with arithmetic. The
model is being asked to phrase a decision that has already been made, and if
its output disagrees with the decision, the output is wrong by definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# `blunt` is the Rekt Wrapped register: short, second person, no hedging, the
# tone that makes the card get shared. `plain` is the same facts written for a
# report a user might paste to an accountant -- neutral, no roast. The voice
# changes the sentence and nothing else; both are narrating identical facts,
# and a voice that could change which facts appear would be a second decision
# point, which is precisely what this layer is not allowed to have.
Voice = Literal["blunt", "plain"]
VOICES: tuple[Voice, ...] = ("blunt", "plain")

# Where the words came from. Not decoration: every rendered narration is
# traceable to either a model call that passed the fact check or a template
# that cannot fail one.
Source = Literal["model", "template"]

Verdict = Literal["favourable", "proceed", "caution", "skip"]
VERDICTS: tuple[Verdict, ...] = ("favourable", "proceed", "caution", "skip")


@dataclass(frozen=True)
class SummaryFact:
    """The headline statistics, already computed by `Profile`."""

    n_trades: int
    n_symbols: int
    span_days: float
    total_pnl: float
    total_fees: float
    win_rate: float          # a rate in 0..1, as the profile stores it
    expectancy: float
    is_thin: bool

    def values(self) -> list[float]:
        return [
            float(self.n_trades), float(self.n_symbols), self.span_days,
            self.total_pnl, self.total_fees, self.expectancy,
            # Both forms of the rate. The profile stores 0.42; every renderer
            # in the codebase prints "42%". A fact-checker that only knew the
            # stored form would reject the model for writing the number the
            # rest of the product prints.
            self.win_rate, self.win_rate * 100.0,
        ]


@dataclass(frozen=True)
class LeakFact:
    """One significance-tested finding, flattened for the prompt.

    `finding` is the sentence a detector already wrote. It is passed through
    verbatim rather than re-derived, so the model is rephrasing a claim that
    was made by arithmetic instead of composing a new one from parts.
    """

    rule: str
    title: str
    finding: str
    confidence: str          # "proof" | "statistical" | "weak"
    cost: float | None       # signed, quote currency; None when not honestly costable
    n: int

    def strings(self) -> list[str]:
        return [self.title, self.finding]

    def values(self) -> list[float]:
        vals = [float(self.n)]
        if self.cost is not None:
            vals.append(self.cost)
        return vals


@dataclass(frozen=True)
class CounterfactualFact:
    """One licensed alternate universe. Unlicensed ones never reach a request."""

    rule: str
    description: str
    delta: float
    n_removed: int
    hindsight: bool = False

    def strings(self) -> list[str]:
        return [self.description]

    def values(self) -> list[float]:
        return [self.delta, float(self.n_removed)]


@dataclass(frozen=True)
class AnalogueFact:
    """One base rate the gate looked up, exactly as the gate computed it."""

    dimension: str
    n: int
    expectancy: float
    win_rate: float
    baseline_expectancy: float

    def strings(self) -> list[str]:
        return [self.dimension]

    def values(self) -> list[float]:
        return [
            float(self.n), self.expectancy, self.baseline_expectancy,
            self.win_rate, self.win_rate * 100.0,
            # The gate's own headline quotes the gap, so allow it to be quoted.
            self.expectancy - self.baseline_expectancy,
        ]


def _harvest(items: list[Any]) -> tuple[list[str], list[float]]:
    strings: list[str] = []
    values: list[float] = []
    for item in items:
        if hasattr(item, "strings"):
            strings.extend(item.strings())
        if hasattr(item, "values"):
            values.extend(item.values())
    return strings, values


@dataclass(frozen=True)
class NarrationRequest:
    """Everything the model is allowed to know when writing card copy.

    Assembled by `narrate.narration_request` from a `Profile`. Note what is
    absent: no round-trips, no fills, no `watch` list. Underpowered findings
    are excluded upstream by `Profile`, and letting them in here through a
    side door would undo that -- a model given a "weak" finding will write
    about it with exactly the same confidence as a proven one.
    """

    voice: Voice
    summary: SummaryFact
    leaks: tuple[LeakFact, ...] = ()
    strengths: tuple[LeakFact, ...] = ()
    counterfactuals: tuple[CounterfactualFact, ...] = ()
    # The single most expensive habit, by rule name. The card leads with it and
    # the model must not promote a different one to the headline.
    worst_rule: str | None = None
    total_leak_cost: float = 0.0

    def strings(self) -> list[str]:
        out: list[str] = []
        for group in (self.leaks, self.strengths, self.counterfactuals):
            out.extend(_harvest(list(group))[0])
        return out

    def values(self) -> list[float]:
        out = list(self.summary.values())
        out.append(self.total_leak_cost)
        for group in (self.leaks, self.strengths, self.counterfactuals):
            out.extend(_harvest(list(group))[1])
        return out

    def to_dict(self) -> dict[str, Any]:
        """The exact object serialised into the prompt.

        Written by hand rather than with `asdict` so that adding a field to a
        fact type is a deliberate decision to show it to the model, not an
        accident of dataclass reflection.
        """
        return {
            "voice": self.voice,
            "summary": {
                "closed_trades": self.summary.n_trades,
                "symbols_traded": self.summary.n_symbols,
                "days_of_history": self.summary.span_days,
                "net_pnl": self.summary.total_pnl,
                "fees_paid": self.summary.total_fees,
                "win_rate_pct": round(self.summary.win_rate * 100.0, 1),
                "expectancy_per_trade": self.summary.expectancy,
            },
            "worst_leak_rule": self.worst_rule,
            "total_cost_of_all_leaks": self.total_leak_cost,
            "leaks": [
                {"rule": leak.rule, "title": leak.title, "finding": leak.finding,
                 "evidence": leak.confidence, "cost": leak.cost, "n_trades": leak.n}
                for leak in self.leaks
            ],
            "strengths": [
                {"rule": s.rule, "title": s.title, "finding": s.finding, "n_trades": s.n}
                for s in self.strengths
            ],
            "counterfactuals": [
                {"description": cf.description, "worth": cf.delta,
                 "trades_removed": cf.n_removed, "hindsight_selected": cf.hindsight}
                for cf in self.counterfactuals
            ],
        }


@dataclass(frozen=True)
class JudgementRequest:
    """Everything the model is allowed to know when narrating the gate.

    `verdict` is here as an input and is not negotiable. So is `headline`: the
    gate already wrote a correct sentence, and the model's job is to make it
    land, not to reach a different conclusion from the same base rates.
    """

    voice: Voice
    verdict: Verdict
    gate_headline: str
    analogues: tuple[AnalogueFact, ...] = ()
    reasons: tuple[str, ...] = ()
    proposed_notional: float | None = None
    suggested_notional: float | None = None
    symbol: str | None = None

    def strings(self) -> list[str]:
        out = [self.gate_headline, *self.reasons]
        if self.symbol:
            out.append(self.symbol)
        out.extend(_harvest(list(self.analogues))[0])
        return out

    def values(self) -> list[float]:
        out = _harvest(list(self.analogues))[1]
        for n in (self.proposed_notional, self.suggested_notional):
            if n is not None:
                out.append(n)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "voice": self.voice,
            "verdict_decided_by_arithmetic": self.verdict,
            "gate_headline": self.gate_headline,
            "symbol": self.symbol,
            "proposed_notional": self.proposed_notional,
            "notional_the_record_supports": self.suggested_notional,
            "base_rates": [
                {"dimension": a.dimension, "n_trades": a.n,
                 "win_rate_pct": round(a.win_rate * 100.0, 1),
                 "expectancy_per_trade": a.expectancy,
                 "your_baseline_per_trade": a.baseline_expectancy}
                for a in self.analogues
            ],
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class Narration:
    """Card copy, with its provenance attached."""

    headline: str
    body: str
    voice: Voice
    source: Source
    # Why a template is being shown instead of a model's words. `None` when the
    # model's output passed, or when no model was ever going to be called.
    fallback_reason: str | None = None

    @property
    def from_model(self) -> bool:
        return self.source == "model"

    def to_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline, "body": self.body, "voice": self.voice,
            "source": self.source, "fallback_reason": self.fallback_reason,
        }


@dataclass(frozen=True)
class VerdictNarration:
    """A narrated gate decision.

    `verdict` is copied from the `Judgement`, never from the model's reply,
    even when the model's reply agrees. Reading it from the response would
    make the field's correctness depend on the model behaving, and the entire
    point of this layer is that nothing depends on the model behaving.
    """

    verdict: Verdict
    headline: str
    explanation: str
    voice: Voice
    source: Source
    fallback_reason: str | None = None
    # Copied through from the gate so a caller has one object to render.
    suggested_notional: float | None = None
    analogues: tuple[AnalogueFact, ...] = field(default_factory=tuple)

    @property
    def is_warning(self) -> bool:
        return self.verdict in ("caution", "skip")

    @property
    def from_model(self) -> bool:
        return self.source == "model"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict, "headline": self.headline,
            "explanation": self.explanation, "voice": self.voice,
            "source": self.source, "fallback_reason": self.fallback_reason,
            "suggested_notional": self.suggested_notional,
        }
