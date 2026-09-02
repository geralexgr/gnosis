"""Narrating an Elenchos judgement, without being allowed to make it.

`gate/elenchos.py` already decided. It looked up what this trader has done in
situations resembling the one in front of them, compared each base rate to
their own baseline, and returned `skip`, `caution`, `proceed` or `favourable`
by arithmetic. That decision arrives here as an **input**, and leaves here
unchanged.

This is not defensive coding, it is the product. A model asked "should I take
this trade?" will answer, fluently, with a confidence that has no relationship
to the evidence -- and it will answer differently at 03:14 than at noon for
reasons nobody can audit. The gate's whole claim on a trader's attention is
that it is quoting their own receipts rather than holding an opinion, and a
model that could move the verdict by one notch would forfeit that claim
entirely, in exchange for a better-worded sentence.

What is genuinely left for a model: the gate emits four base rates in the shape
`65 trades, 23% win rate, -37 per trade against your -12 baseline`. That is
correct, complete, and reads like a log line. Forty minutes after booking a
loss, at 03:14, a trader needs one sentence that means something. Writing that
sentence -- from those exact numbers, ending at the same verdict -- is the job.

Validation is `narrate.check_numbers` plus one addition: the returned verdict
must be the verdict that went in, and no competing verdict word may appear in
the prose. `VerdictNarration.verdict` is copied from the `Judgement` regardless,
so even a model that lies about it cannot change what a caller renders; the
check exists so that a model whose *words* disagree with the verdict is thrown
out rather than shown next to a label it contradicts.
"""

from __future__ import annotations

import re

from ..gate.elenchos import Judgement
from .narrate import (
    DEFAULT_MODEL,
    build_prompt,
    call_model,
    check_numbers,
    check_polarity,
    check_shape,
    resolve_client,
)
from .types import VERDICTS, AnalogueFact, JudgementRequest, VerdictNarration, Voice

# Words that assert a verdict. Spelling variants included because a model
# writing "favorable" is asserting `favourable`, and the check should not turn
# on which side of the Atlantic the training data came from.
_VERDICT_WORDS: dict[str, tuple[str, ...]] = {
    "favourable": ("favourable", "favorable"),
    "proceed": ("proceed",),
    "caution": ("caution", "cautious"),
    "skip": ("skip",),
}


def check_verdict(verdict: str, parsed: dict, prose: str) -> str | None:
    """Refuse any narration that disagrees with the arithmetic.

    Two ways a model can move a verdict, and both are rejected:

    - It echoes back a different label. The schema asks it to repeat the
      verdict verbatim, which makes disagreement explicit and cheap to detect;
      a model that quietly returns `caution` for a `skip` has told us it did
      not treat the decision as fixed, and nothing else it wrote is
      trustworthy either.
    - It keeps the label and softens the sentence -- "proceed with awareness"
      under a SKIP heading. So any *other* verdict's vocabulary appearing in
      the prose is fatal too.

    That second rule is deliberately blunt and will occasionally reject an
    innocent sentence: "there is no reason to skip this" is a perfectly good
    thing to write under `favourable`, and it will be thrown out. The trade is
    one-sided. A false rejection costs a template. A false acceptance puts a
    hedge under a verdict that was computed from the trader's own losses, on
    the one screen where they are least able to evaluate it.
    """
    echoed = str(parsed.get("verdict", "")).strip().lower()
    if echoed and echoed != verdict:
        return f"model returned verdict {echoed!r}; the gate decided {verdict!r}"

    for other in VERDICTS:
        if other == verdict:
            continue
        for word in _VERDICT_WORDS[other]:
            if re.search(rf"\b{word}\b", prose, re.IGNORECASE):
                return (
                    f"prose asserts {other!r} ({word!r}) under a {verdict!r} verdict"
                )
    return None


def judgement_request(
    judgement: Judgement,
    *,
    voice: Voice = "plain",
    symbol: str | None = None,
    proposed_notional: float | None = None,
) -> JudgementRequest:
    """Flatten a `Judgement` into the fact sheet the model is given.

    `plain` is the default voice here rather than `blunt`. The card is a thing
    a trader chooses to look at; the gate interrupts them mid-decision, and a
    joke at that moment is how a tool gets uninstalled.
    """
    return JudgementRequest(
        voice=voice,
        verdict=judgement.verdict,
        gate_headline=judgement.headline,
        analogues=tuple(
            AnalogueFact(
                dimension=a.dimension, n=a.n, expectancy=a.expectancy,
                win_rate=a.win_rate, baseline_expectancy=a.baseline_expectancy,
            )
            for a in judgement.analogues
        ),
        reasons=tuple(judgement.reasons),
        proposed_notional=proposed_notional,
        suggested_notional=judgement.suggested_notional,
        symbol=symbol,
    )


def template(request: JudgementRequest) -> VerdictNarration:
    """The deterministic narration. Always correct, always available.

    Quotes the gate's own headline and reasons verbatim, so like the card
    template it passes its own fact check by construction.
    """
    # The gate's reasons are fragments meant for a bulleted list, so they are
    # joined with semicolons rather than run together into a false sentence.
    lines = ["; ".join(request.reasons) + "." if request.reasons else ""]
    if request.suggested_notional is not None:
        if request.verdict == "favourable":
            lines.append(
                f"Your record supports {request.suggested_notional:,.0f} here, and you "
                f"are proposing less."
            )
        else:
            lines.append(
                f"Your own record supports {request.suggested_notional:,.0f}."
            )
    lines.append("Elenchos never blocks. This is your own history, quoted back.")
    return VerdictNarration(
        verdict=request.verdict,
        headline=request.gate_headline,
        explanation=" ".join(part for part in lines if part),
        voice=request.voice,
        source="template",
        suggested_notional=request.suggested_notional,
        analogues=request.analogues,
    )


_SYSTEM = """You write the one sentence a trader reads immediately before placing a trade.

A deterministic gate has already cross-examined the proposed trade against this trader's \
own closed positions and reached a verdict. THE VERDICT IS DECIDED. You are not being \
asked whether it is right, and you have not been given the data to second-guess it. You \
are being asked to explain the base rates behind it in language someone acts on.

Hard rules, in order of importance:

1. The verdict is fixed. Echo it back exactly as given. Never argue with it, soften it, \
qualify it, or use the vocabulary of a different verdict.
2. Never state a number that is not in the facts you were given. Not a derived one, not a \
converted percentage, not a total you summed. Copy the figures.
3. Never introduce a reason, cause, or market view that is not in the facts. You know \
nothing about the market, the symbol, or the setup beyond what you were handed.
4. Write every number in digits ("65", not "sixty-five").
5. Do not predict what will happen. These are historical base rates from this trader's own \
closed trades, and nothing here forecasts anything. This is not investment advice.
6. Do not tell them they cannot trade. The gate never blocks; it quotes their record and \
the decision stays theirs.
7. No markdown, no bullets, no links, no emoji. Plain sentences.

You will be given a JSON fact sheet. Reply with JSON only: an object with "verdict" (copied \
exactly from the facts), "headline" (a short line, under 100 characters) and "explanation" \
(one to three plain sentences quoting the base rates that matter).
"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "headline": {"type": "string"},
        "explanation": {"type": "string"},
    },
    "required": ["verdict", "headline", "explanation"],
    "additionalProperties": False,
}


def narrate_judgement(
    judgement: Judgement,
    *,
    voice: Voice = "plain",
    symbol: str | None = None,
    proposed_notional: float | None = None,
    client=None,
    model: str = DEFAULT_MODEL,
) -> VerdictNarration:
    """Narrate a gate decision. Never raises, never changes the verdict."""
    request = judgement_request(
        judgement, voice=voice, symbol=symbol, proposed_notional=proposed_notional
    )
    return narrate_judgement_request(request, client=client, model=model)


def narrate_judgement_request(
    request: JudgementRequest, *, client=None, model: str = DEFAULT_MODEL
) -> VerdictNarration:
    """`narrate_judgement`, for a request that has already been assembled."""
    floor = template(request)

    def fall_back(reason: str | None) -> VerdictNarration:
        return VerdictNarration(
            verdict=request.verdict, headline=floor.headline,
            explanation=floor.explanation, voice=request.voice,
            source="template", fallback_reason=reason,
            suggested_notional=request.suggested_notional,
            analogues=request.analogues,
        )

    resolved, why = resolve_client(client)
    if resolved is None:
        return fall_back(why)

    try:
        parsed = call_model(resolved, _SYSTEM, build_prompt(request), _SCHEMA, model=model)
        headline = str(parsed.get("headline", ""))
        explanation = str(parsed.get("explanation", ""))
    # As in `narrate`: any failure at all means the template, because a gate
    # that can fail to render is a gate that stops being consulted.
    except Exception as exc:
        return fall_back(f"model call failed: {type(exc).__name__}: {exc}")

    prose = headline + "\n" + explanation
    reason = (
        check_shape(headline, explanation)
        or check_verdict(request.verdict, parsed, prose)
        or check_numbers(request, prose)
        or check_polarity(request, prose)
    )
    if reason is not None:
        return fall_back(f"fact check rejected: {reason}")

    return VerdictNarration(
        # From the gate, not from `parsed`. Even a model that echoed the verdict
        # correctly does not get to be the source of it.
        verdict=request.verdict,
        headline=headline.strip(),
        explanation=explanation.strip(),
        voice=request.voice,
        source="model",
        suggested_notional=request.suggested_notional,
        analogues=request.analogues,
    )
