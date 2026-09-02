"""Detector contract.

Detectors are deterministic and never call a model. Their job is to find a
behavioural pattern, prove what it cost, and attach the trade ids that prove it.
Whether the pattern is worth telling the user about -- and how to say it -- is
the agent layer's job.

The split matters for a reason specific to this product: a model asked to "find
problems in this trade history" will always find some, because that is what it
was asked for. Sycophancy and pattern-matching pressure both point the same way.
Moving the *decision* into arithmetic means the model never gets to invent a
leak; it only ever narrates one that survived a significance test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from ..model.roundtrip import RoundTrip
from ..stats import Comparison

# How much a leak's evidence is actually worth.
#
#   proof         the behaviour is directly countable from the history and not
#                 open to interpretation -- "you added to a losing position 14
#                 times" is a fact, not a reading. Its *cost* may still be
#                 estimated, but the behaviour itself is not in question.
#   statistical   the claim rests on a slice performing differently from the
#                 trader's own baseline, and a bootstrap interval excluding
#                 zero. Real, but it is an inference.
#   weak          a pattern is present but underpowered. Never surfaces to a
#                 user on its own; retained so the profile can say "watch this".
Confidence = Literal["proof", "statistical", "weak"]


@dataclass
class Leak:
    """One behavioural finding, with its receipts."""

    rule: str
    title: str          # short label, for the card
    finding: str        # one plain sentence, numbers included
    confidence: Confidence
    # Signed, in quote currency. Negative costs money. `None` when the
    # behaviour is real but its cost could not be honestly estimated -- which
    # we say, rather than printing a confident zero.
    cost: float | None
    n: int              # observations behind the claim
    # The trades that produced this finding. Every number in the final report
    # must be traceable to executions, or the user is entitled to disbelieve it.
    trade_ids: list[str] = field(default_factory=list)
    comparison: Comparison | None = None
    detail: dict = field(default_factory=dict)

    @property
    def costs_money(self) -> bool:
        return self.cost is not None and self.cost < 0

    def __str__(self) -> str:
        money = f"  ({self.cost:+,.0f})" if self.cost is not None else ""
        return f"[{self.confidence:11s}] {self.rule:22s} n={self.n:<4d}{money}  {self.finding}"


class Detector(Protocol):
    rule: str

    def run(self, trips: list[RoundTrip]) -> list[Leak]: ...


def closed_only(trips: list[RoundTrip]) -> list[RoundTrip]:
    """Statistics run on closed trades only.

    An open position has no realised outcome. Including it would let one
    unrealised bag rewrite the profile, and would also mean the same history
    profiled twice, a day apart, tells a different story for no reason the user
    did anything to cause.
    """
    return [t for t in trips if not t.is_open]
