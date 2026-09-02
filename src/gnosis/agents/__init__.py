"""The agent layer: the only place in Gnosis where a model runs.

Everything upstream of this package -- ingest, round-trip reconstruction, the
five detectors, the bootstrap, the counterfactual licensing, the pre-trade
verdict -- is arithmetic, and is finished before anything here is called. What
is left over is the one job a model is actually better at than a template:
saying a true thing in a sentence a human will read.

The package is therefore built around a single asymmetry. The model is given
facts and asked for prose; it is never given prose and asked for facts. A
narration that introduces a number, a finding, or a verdict which is not
already in its input is not a better narration, it is a fabrication, and
`narrate.check_numbers` exists to catch exactly that and throw the output away.

Two entry points, both of which take an optional `client` (so the test double
can be injected) and neither of which ever raises:

    from gnosis.agents import narrate_profile, narrate_judgement

    copy = narrate_profile(profile, voice="blunt")     # Rekt Wrapped card copy
    call = narrate_judgement(judgement, voice="plain") # the Elenchos gate

Nothing here is required for Gnosis to work. `anthropic` is an optional extra
and every import of it is lazy; with no API key the whole package degrades to
deterministic templates, records why on the result, and the CLI never notices.
"""

from __future__ import annotations

from . import judge, narrate
from .judge import judgement_request, narrate_judgement
from .narrate import check_numbers, check_polarity, narrate_profile, narration_request, numbers_in
from .types import (
    AnalogueFact,
    CounterfactualFact,
    JudgementRequest,
    LeakFact,
    Narration,
    NarrationRequest,
    SummaryFact,
    VerdictNarration,
    Voice,
)

__all__ = [
    "AnalogueFact",
    "CounterfactualFact",
    "JudgementRequest",
    "LeakFact",
    "Narration",
    "NarrationRequest",
    "SummaryFact",
    "VerdictNarration",
    "Voice",
    "check_numbers",
    "check_polarity",
    "judge",
    "judgement_request",
    "narrate",
    "narrate_judgement",
    "narrate_profile",
    "narration_request",
    "numbers_in",
]
