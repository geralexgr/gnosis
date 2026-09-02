"""Card copy, written by a model that is not allowed to make anything up.

Gnosis has one credibility problem and this file is where it is either solved
or lost. Everything the card says was computed and significance-tested before
this module was called; the only thing left is to say it well. That is a real
job -- "you hold losers 2.1x longer than winners (median 31.4h vs 15.0h)" is
true and nobody reads it twice -- but it is a *writing* job, and the moment a
model is allowed to contribute a fact instead of a sentence, the product turns
into a horoscope with better typography.

So the model here is boxed in from three sides.

**Its input is a fact sheet, not a history.** `narration_request` flattens a
`Profile` into titles, findings, costs and sample sizes. The trades are not in
there. A model cannot slice what it has not been given, and refusing to give
it the raw data is a stronger guarantee than any instruction in a prompt.

**Its output is fact-checked, digit by digit.** `check_numbers` extracts every
number in what the model wrote and requires each one to be a number Gnosis
already computed. Not "roughly consistent with" -- present. This is the most
important function in the package and the one to be suspicious of; its exact
guarantees, and the four things it cannot catch, are documented on it.

**Its absence costs nothing.** With no API key, no `anthropic` package, an API
error, or a failed fact check, `narrate` returns the deterministic template and
records why. The CLI has no idea whether a model ran. A narration layer that
can take the product down when a vendor has an outage is not worth having for
the sake of a nicer adjective.

The template is not a degraded mode that we tolerate. It is the floor, it is
always correct, and the model has to clear it to be used at all.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from ..model.profile import Profile
from .types import (
    CounterfactualFact,
    LeakFact,
    Narration,
    NarrationRequest,
    SummaryFact,
    Voice,
)

# Sonnet is the right tier for this: the task is constrained rewriting of
# supplied facts, not reasoning, and the fact-checker is what actually enforces
# quality. Overridable by the caller; never read from the environment, because
# a narration that silently changes model between runs is unreproducible.
DEFAULT_MODEL = "claude-sonnet-5"

# Deliberately short. The card has 68 columns and a reader with no patience;
# a long narration is a worse narration, and a low ceiling also bounds how much
# damage a runaway generation can do before the fact check throws it away.
MAX_TOKENS = 2000
MAX_HEADLINE = 120
MAX_BODY = 1200


# --------------------------------------------------------------------------
# Number extraction: the shared vocabulary
# --------------------------------------------------------------------------
#
# One regex reads both the facts and the model's reply, which is the whole
# trick. If the extractor has a blind spot it has the same blind spot on both
# sides, so it cannot reject a number the facts genuinely contain -- the
# failure mode is a missed catch, never a spurious one.
#
# Signs are dropped on purpose (see `check_numbers` for what that costs).
# The trailing `\d` in the integer part is what stops "65," in "65 trades,"
# being read as the number "65,".
_NUMBER_RE = re.compile(r"\d(?:[\d,]*\d)?(?:\.\d+)?")

# Numbers written as words escape a digit-based extractor entirely, so they are
# banned outright rather than half-parsed. Only magnitudes are listed: "two
# habits" is ordinary prose and carries no quantitative claim worth checking,
# while "sixty-five trades" and "two thousand dollars" are exactly the claims
# that must not slip past. The prompt asks for digits; this enforces it.
_SPELLED_MAGNITUDES = re.compile(
    r"\b(twenty|thirty|forty|fourty|fifty|sixty|seventy|eighty|ninety|"
    r"hundred|hundreds|thousand|thousands|million|millions|billion|billions|dozen)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Number:
    """A numeric token found in text."""

    raw: str
    value: float      # absolute value; sign is not compared
    decimals: int     # digits written after the point, which sets the tolerance


def numbers_in(text: str) -> list[Number]:
    """Every number in `text`, as written."""
    found: list[Number] = []
    for match in _NUMBER_RE.finditer(text or ""):
        raw = match.group(0)
        cleaned = raw.replace(",", "")
        try:
            value = float(cleaned)
        except ValueError:  # pragma: no cover - the regex cannot produce this
            continue
        decimals = len(cleaned.split(".")[1]) if "." in cleaned else 0
        found.append(Number(raw=raw, value=abs(value), decimals=decimals))
    return found


def allowed_values(request) -> list[float]:
    """The complete set of magnitudes a narration of `request` may contain.

    Two sources, and the second is the one that makes this workable:

    1. The numeric fields of the facts -- costs, sample sizes, expectancies --
       plus the percentage form of every rate, because the profile stores 0.42
       and every renderer in the codebase prints "42%".
    2. Every number appearing inside the *prose* the detectors already wrote.
       A finding says "your 65 trades in the small hours (00:00-06:00 UTC)";
       65, 0 and 6 are legitimately quotable, and harvesting them from the same
       strings the model is shown means the allowed set can never drift out of
       step with what the model was told.
    """
    values = [abs(v) for v in request.values()]
    for text in request.strings():
        values.extend(num.value for num in numbers_in(text))
    return values


def _is_allowed(num: Number, allowed: list[float]) -> bool:
    """Is `num` a legitimate rendering of some computed value?

    The tolerance is exactly "could this have been produced by rounding a real
    value to the precision that was written". A model writing `-37` when the
    expectancy is -36.98 is formatting; a model writing `-42` is inventing, and
    at zero decimal places the half-unit window separates the two cleanly. It
    tightens automatically as the model gets more precise: writing `-36.9` is
    only accepted for a value within 0.05 of it.
    """
    tolerance = 0.5 * (10.0 ** -num.decimals) + 1e-9
    return any(abs(candidate - num.value) <= tolerance for candidate in allowed)


def check_numbers(request, text: str) -> str | None:
    """Fact-check a model's prose against the request that produced it.

    Returns `None` when the text is clean, or a human-readable reason when it
    is not. Callers treat any reason as fatal and fall back to the template --
    there is no "mostly fine" tier, because a card with one invented number on
    it is a card whose other numbers a reasonable person should also discount.

    **What this catches.** Any figure the model states that Gnosis did not
    compute: a fabricated cost, an inflated sample size, a win rate nobody
    measured, a number carried over from a previous conversation, an arithmetic
    "correction" the model performed on the facts it was handed. Also numbers
    written as words, which would otherwise be invisible to a digit scanner.

    **What this cannot catch, and callers should not imagine it can.**

    - *Recombination.* Every number correct, attached to the wrong claim --
      "you lose 2,406 in the London session" when 2,406 was the small hours.
      Both numbers and both phrases are in the facts; only the pairing is
      false. This is the largest hole and it is not closable by counting.
    - *Sign.* Magnitudes are compared, so "you made 3,488" passes on a fact
      that says you lost it. Dropping the sign is what lets "-3,488", "3,488"
      and "$3,488" all be accepted as the same figure, which is the formatting
      variance this has to tolerate; the cost is that direction is unchecked.
    - *Claims with no numbers in them.* "You always panic on Mondays" contains
      nothing to check and sails through. The prompt forbids it and the input
      gives the model nothing to build it from, but this function is blind
      to it.
    - *Tone and framing.* Advice, prediction, and confidence are unpoliced
      here.

    A narrower way to put the guarantee: this proves the model did not
    introduce a *quantity*. It does not prove the model told the truth.
    """
    spelled = _SPELLED_MAGNITUDES.search(text or "")
    if spelled:
        return (
            f"number written as a word ({spelled.group(0)!r}); every figure must "
            f"be in digits so it can be checked against the facts"
        )

    allowed = allowed_values(request)
    offenders = [num.raw for num in numbers_in(text) if not _is_allowed(num, allowed)]
    if offenders:
        unique = list(dict.fromkeys(offenders))
        return (
            "output contains number(s) absent from the input facts: "
            + ", ".join(unique[:6])
        )
    return None


# Words that assert a direction of money. Kept small and unambiguous on
# purpose: this check only fires when the polarity is stated outright, because
# a fuzzy match here would reject honest phrasing and push everything to the
# template for no gain.
_GAIN_WORDS = ("made", "gained", "earned", "profit", "profited", "won", "up by", "added")
_LOSS_WORDS = ("lost", "cost", "losing", "loss", "down by", "gave back", "bled", "paid")


def _sentences(text: str) -> list[str]:
    out, cur = [], ""
    for ch in text:
        cur += ch
        if ch in ".!?\n":
            out.append(cur)
            cur = ""
    if cur.strip():
        out.append(cur)
    return out


def check_polarity(request, text: str) -> str | None:
    """Reject output that reverses the direction of a quantity.

    `check_numbers` deliberately compares magnitudes, so `-3,488` and `3,488`
    are equal to it. That tolerance is what lets the model write "cost you
    3,488" against a fact stored as `-3488.0` -- and it is also a hole, because
    "you made 3,488" passes the same test.

    On a card whose entire purpose is telling someone what a habit costs them,
    an inverted sign is the single worst thing the model could produce: every
    number checks out and the meaning is backwards. So this pass looks at each
    sentence, finds the magnitudes of *negative* facts in it, and rejects the
    sentence if it asserts a gain without also naming a loss.

    It cannot catch an unstated inversion ("your best session was the small
    hours"), and it is not trying to. It closes the case where the model says
    the opposite of the arithmetic in so many words.
    """
    negatives = [abs(v) for v in request.values() if isinstance(v, (int, float)) and v < 0]
    if not negatives:
        return None

    for sentence in _sentences(text):
        low = sentence.lower()
        gains = [w for w in _GAIN_WORDS if w in low]
        if not gains or any(w in low for w in _LOSS_WORDS):
            continue
        for token in re.findall(r"\d(?:[\d,]*\d)?(?:\.\d+)?", sentence):
            try:
                magnitude = float(token.replace(",", ""))
            except ValueError:
                continue
            decimals = len(token.split(".")[1]) if "." in token else 0
            tol = 0.5 * (10 ** -decimals)
            if any(abs(magnitude - neg) <= tol for neg in negatives):
                return (
                    f"output states a gain ({gains[0]!r}) for {token}, which is a "
                    f"cost in the facts"
                )
    return None


def check_shape(headline: str, body: str) -> str | None:
    """Structural sanity, before anything is fact-checked.

    Cheap, and it catches the failure modes that are not about truth: an empty
    field, a model that ignored the length budget and wrote an essay, or one
    that helpfully added a link. A URL in particular is worth rejecting on
    sight -- it is by definition not in the facts, and it is the shape of an
    injected instruction being echoed back.
    """
    if not headline.strip():
        return "empty headline"
    if not body.strip():
        return "empty body"
    if len(headline) > MAX_HEADLINE:
        return f"headline is {len(headline)} chars, over the {MAX_HEADLINE} limit"
    if len(body) > MAX_BODY:
        return f"body is {len(body)} chars, over the {MAX_BODY} limit"
    if "http://" in body or "https://" in body or "http://" in headline:
        return "output contains a URL, which cannot have come from the facts"
    return None


# --------------------------------------------------------------------------
# Building the request
# --------------------------------------------------------------------------

def _leak_fact(leak) -> LeakFact:
    return LeakFact(
        rule=leak.rule, title=leak.title, finding=leak.finding,
        confidence=leak.confidence, cost=leak.cost, n=leak.n,
    )


def narration_request(profile: Profile, voice: Voice = "blunt") -> NarrationRequest:
    """Flatten a `Profile` into the only thing the model gets to see.

    `profile.watch` is not read. Those findings are underpowered by definition
    and `Profile` already decided they do not surface; a model shown one would
    write about it in the same confident register as a proven leak, because
    nothing in the prose marks the difference.
    """
    s = profile.summary
    worst = profile.worst_leak
    return NarrationRequest(
        voice=voice,
        summary=SummaryFact(
            n_trades=s.n_trades, n_symbols=len(s.symbols), span_days=s.span_days,
            total_pnl=s.total_pnl, total_fees=s.total_fees, win_rate=s.win_rate,
            expectancy=s.expectancy, is_thin=s.is_thin,
        ),
        leaks=tuple(_leak_fact(leak) for leak in profile.leaks),
        strengths=tuple(_leak_fact(st) for st in profile.strengths),
        counterfactuals=tuple(
            CounterfactualFact(
                rule=cf.rule, description=cf.description, delta=cf.delta,
                n_removed=cf.n_removed, hindsight=cf.hindsight,
            )
            for cf in profile.counterfactuals
        ),
        worst_rule=worst.rule if worst is not None else None,
        total_leak_cost=profile.total_leak_cost,
    )


# --------------------------------------------------------------------------
# The deterministic floor
# --------------------------------------------------------------------------

def template(request: NarrationRequest) -> Narration:
    """The narration that is always available and always correct.

    Every sentence here is either fixed text or a fact quoted verbatim, so it
    passes its own fact check by construction -- which the test suite asserts,
    because a fallback that could fail validation would be no fallback at all.
    """
    blunt = request.voice == "blunt"
    s = request.summary

    if s.is_thin:
        headline = (
            "Not enough history to roast you yet."
            if blunt else "Insufficient history to profile."
        )
        body = (
            f"{s.n_trades} closed trades across {s.span_days:.0f} days. Gnosis needs "
            f"more than that before it says anything you should act on, and it is "
            f"not going to invent something in the meantime."
        )
        return Narration(headline=headline, body=body, voice=request.voice,
                         source="template")

    worst = next((leak for leak in request.leaks if leak.rule == request.worst_rule), None)
    if worst is None:
        worst = request.leaks[0] if request.leaks else None

    lines: list[str] = [
        f"{s.n_trades} closed trades over {s.span_days:.0f} days, "
        f"{s.win_rate:.0%} of them winners, {s.expectancy:+,.0f} per trade. "
        f"Net {s.total_pnl:+,.0f} after {abs(s.total_fees):,.0f} in fees."
    ]

    if worst is None:
        headline = (
            "Nothing here clears the bar. Genuinely."
            if blunt else "No behavioural leak reached significance."
        )
        lines.append(
            "No behavioural leak survived a confidence interval. That is a result, "
            "not a shrug: the detectors ran and found nothing worth telling you."
        )
        return Narration(headline=headline, body=" ".join(lines), voice=request.voice,
                         source="template")

    headline = worst.title
    lines.append(worst.finding)
    for leak in request.leaks:
        if leak.rule == worst.rule:
            continue
        lines.append(leak.finding)
    if request.strengths:
        prefix = "For balance: " if blunt else "Also observed: "
        lines.append(prefix + request.strengths[0].finding)
    if request.counterfactuals:
        cf = request.counterfactuals[0]
        lines.append(
            f"Had you {cf.description}, you would be {cf.delta:+,.0f} better off "
            f"across the {cf.n_removed} trades that rule removes."
        )
    return Narration(headline=headline, body=" ".join(lines), voice=request.voice,
                     source="template")


# --------------------------------------------------------------------------
# The model call
# --------------------------------------------------------------------------

_VOICE_NOTE = {
    "blunt": (
        "Voice: blunt. Second person, present tense, short sentences, no hedging "
        "and no consolation. This is a roast card the trader is meant to share, "
        "so it has to sting slightly and be accurate enough that they cannot "
        "argue with it. Do not be cruel and do not moralise."
    ),
    "plain": (
        "Voice: plain. Neutral, factual, the register of a report someone might "
        "forward to an accountant. No roast, no jokes, no adjectives doing work "
        "the numbers should do."
    ),
}

_SYSTEM = """You write the copy for Gnosis, a behavioural profiler that reads a trader's own \
history back to them.

Everything you are given has already been computed from executed trades and has already \
passed a significance test. Your job is to make those findings land in a sentence a human \
will actually read. Your job is not to analyse, extend, estimate, or judge them.

Hard rules, in order of importance:

1. Never state a number that is not in the facts you were given. Not a rounded one you \
derived, not a total you added up, not a percentage you converted. If you want to say a \
figure, copy it.
2. Never introduce a finding, cause, or pattern that is not in the facts. If the facts \
mention one habit, there is one habit. Speculating about why the trader does it is \
inventing a finding.
3. Write every number in digits ("65", not "sixty-five").
4. Do not predict, do not advise, and do not tell them what to do next. Describe what the \
record says. This is not investment advice and must not read like it.
5. Lead with the finding named as the worst one. Do not promote a different one.
6. No markdown, no bullet points, no headings, no links, no emoji. Plain sentences.

You will be given a JSON fact sheet. Reply with JSON only: an object with "headline" (a \
short line, under 100 characters, no trailing full stop) and "body" (two to four plain \
sentences).
"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["headline", "body"],
    "additionalProperties": False,
}


def build_prompt(request) -> str:
    """The user turn: a voice note and a fact sheet, and nothing else.

    Serialised with sorted keys and a fixed indent so that profiling the same
    history twice produces byte-identical prompts. Costs nothing, and it is the
    difference between a reproducible demo and one that drifts.
    """
    return (
        _VOICE_NOTE[request.voice]
        + "\n\nFACTS (the complete set of numbers you may use):\n"
        + json.dumps(request.to_dict(), indent=2, sort_keys=True, default=str)
    )


def resolve_client(client=None) -> tuple[object | None, str | None]:
    """Find a model client, or explain why there isn't one.

    Imported lazily and only on this path: `anthropic` is an optional extra, and
    `import gnosis.agents` must keep working on a machine that has never heard
    of it. An injected client (the test double, or a caller's own configured
    one) short-circuits the whole lookup.
    """
    if client is not None:
        return client, None
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None, "no API key in the environment; used the deterministic template"
    try:
        # Imported here and nowhere else: `anthropic` is an optional extra,
        # so a module-level import would make the whole package unimportable on
        # a machine that has never installed it.
        import anthropic
    except ImportError:
        return None, "the `anthropic` package is not installed (pip install 'gnosis[agent]')"
    return anthropic.Anthropic(), None


def response_text(response) -> str:
    """Concatenate the text blocks of a Messages response."""
    parts = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            parts.append(block.text)
    return "".join(parts)


def call_model(client, system: str, prompt: str, schema: dict, *, model: str) -> dict:
    """One structured, non-streaming request. Raises on anything unexpected."""
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        # A schema-constrained reply removes an entire class of parsing failure,
        # and keeps the model from prefacing the JSON with a sentence about how
        # happy it is to help.
        output_config={
            "effort": "low",  # constrained rewriting; depth buys nothing here
            "format": {"type": "json_schema", "schema": schema},
        },
    )
    text = response_text(response)
    if not text.strip():
        raise ValueError("model returned no text")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def narrate_profile(
    profile: Profile,
    *,
    voice: Voice = "blunt",
    client=None,
    model: str = DEFAULT_MODEL,
) -> Narration:
    """Card copy for a profile, from a model if one is available and honest.

    Never raises. Every failure -- no key, no package, a network error, bad
    JSON, a hallucinated number -- lands in `Narration.fallback_reason` and the
    template is returned. The caller renders the result unconditionally.
    """
    request = narration_request(profile, voice)
    return narrate_request(request, client=client, model=model)


def narrate_request(request: NarrationRequest, *, client=None,
                    model: str = DEFAULT_MODEL) -> Narration:
    """`narrate_profile`, for a request that has already been assembled."""
    floor = template(request)

    resolved, why = resolve_client(client)
    if resolved is None:
        return Narration(headline=floor.headline, body=floor.body, voice=request.voice,
                         source="template", fallback_reason=why)

    try:
        parsed = call_model(resolved, _SYSTEM, build_prompt(request), _SCHEMA, model=model)
        headline = str(parsed.get("headline", ""))
        body = str(parsed.get("body", ""))
    # Every failure mode collapses to the same response, deliberately: a
    # timeout, a 429, a schema violation and a malformed body are all just
    # "no usable narration", and branching on them would only create paths
    # that are never exercised.
    except Exception as exc:
        return Narration(headline=floor.headline, body=floor.body, voice=request.voice,
                         source="template",
                         fallback_reason=f"model call failed: {type(exc).__name__}: {exc}")

    prose = headline + "\n" + body
    reason = (
        check_shape(headline, body)
        or check_numbers(request, prose)
        or check_polarity(request, prose)
    )
    if reason is not None:
        return Narration(headline=floor.headline, body=floor.body, voice=request.voice,
                         source="template", fallback_reason=f"fact check rejected: {reason}")

    return Narration(headline=headline.strip(), body=body.strip(), voice=request.voice,
                     source="model")
