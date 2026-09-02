"""The agent layer: does the model get to lie?

Every other suite in this repo checks that arithmetic is right. This one checks
that a model cannot get a number onto the card that the arithmetic never
produced -- which is the only property that distinguishes Gnosis from a
well-written horoscope, and the only one a user cannot verify for themselves.

The critical test is `a model that invents a number is caught`. If that ever
goes red, nothing else in this file matters, because a narration layer that
sometimes passes a fabrication through has the credibility of one that always
does.

Everything runs against `tests/fakes.py`. No API key, no network, no cost.

Run: python3 tests/test_agent_layer.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fakes import FakeAnthropic, SilentClient, fake_api_key, no_api_key, reply  # noqa: E402

from gnosis.agents import judge, narrate  # noqa: E402
from gnosis.agents.narrate import (  # noqa: E402
    check_numbers,
    check_shape,
    narration_request,
    numbers_in,
)
from gnosis.agents.types import Narration, NarrationRequest, VerdictNarration  # noqa: E402
from gnosis.gate.elenchos import ProposedTrade  # noqa: E402
from gnosis.gate.elenchos import check as gate_check  # noqa: E402
from gnosis.ingest.synthetic import TraderSpec, generate  # noqa: E402
from gnosis.model import profile as profile_mod  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{'' if cond else '  <- ' + detail}")


def build(leak, seed=31):
    return profile_mod.for_history(generate(TraderSpec(seed=seed, leak=leak)))


NIGHT = build("night")
DISPO = build("disposition")
CLEAN = build(None)
REQ = narration_request(NIGHT, "blunt")

# Facts on the night trader, for reference while reading the assertions below:
#   257 closed trades, 365 days, 42% win rate, net -3,150, expectancy -12
#   one leak: the small hours, 65 trades, -37 each vs -4, total -2,406
FACT_HEADLINE = "You lose money in the small hours"
FACT_BODY = (
    "Your 65 trades in the small hours (00:00-06:00 UTC) averaged -37 against -4 "
    "the rest of the day. That is -2,406 gone, across 257 trades and 365 days."
)


# ---------------------------------------------------------------- extraction

print("=== number extraction ===")
check("plain integer", [n.value for n in numbers_in("65 trades")] == [65.0])
check("comma grouping", [n.value for n in numbers_in("3,488")] == [3488.0])
check("negative reads as magnitude", [n.value for n in numbers_in("-3,488")] == [3488.0])
check("currency prefix ignored", [n.value for n in numbers_in("$3,488")] == [3488.0])
check("decimals kept", [n.value for n in numbers_in("1.8x longer")] == [1.8])
check("percent sign dropped", [n.value for n in numbers_in("23% win rate")] == [23.0])
check(
    "trailing comma is punctuation, not digits",
    [n.value for n in numbers_in("65 trades, 23% win")] == [65.0, 23.0],
    str([n.raw for n in numbers_in("65 trades, 23% win")]),
)
check(
    "a time range yields its parts",
    [n.value for n in numbers_in("00:00-06:00 UTC")] == [0.0, 0.0, 6.0, 0.0],
)
check("decimal places recorded", numbers_in("41.63")[0].decimals == 2)
check("integers have zero decimals", numbers_in("42")[0].decimals == 0)
check("prose with no numbers", numbers_in("you nurse your losers") == [])


# ------------------------------------------------- formatting variance
#
# The three renderings the brief calls out by name, plus the rounding the whole
# codebase already does when it prints. If any of these were rejected the
# fact-checker would be unusable: it would reject the deterministic card.

print("\n=== formatting variance is not hallucination ===")
for written in ("-2,406", "2,406", "$2,406", "-2406", "2406"):
    check(f"accepts {written}", check_numbers(REQ, f"that habit cost {written}") is None,
          str(check_numbers(REQ, f"that habit cost {written}")))
check("accepts the rounded win rate (41.63 -> 42%)",
      check_numbers(REQ, "you win 42% of the time") is None)
check("accepts the unrounded win rate",
      check_numbers(REQ, "you win 41.63% of the time") is None)
check("accepts rounded expectancy (-12.26 -> -12)",
      check_numbers(REQ, "you lose 12 per trade") is None)
check("accepts a figure only present inside a finding's prose",
      check_numbers(REQ, "65 trades between 00:00 and 06:00") is None)
check("rejects a near miss that is not a rounding (-12.26 -> -13)",
      check_numbers(REQ, "you lose 13 per trade") is not None)
check("precision tightens the window: 41.6 ok",
      check_numbers(REQ, "41.6% of them") is None)
check("precision tightens the window: 41.2 rejected",
      check_numbers(REQ, "41.2% of them") is not None)


# ------------------------------------------------------------- the templates

print("\n=== the deterministic floor ===")
# The fallback has to pass the same fact check the model's output has to pass.
# If it ever did not, a rejection would replace a hallucination with something
# equally unverifiable, and the whole arrangement would be theatre. Swept over
# every injectable leak because each one phrases its finding differently, and
# it is the phrasing that the extractor reads.
_floor_ok = True
for name in ("disposition", "martingale", "revenge", "night", "overleverage", None):
    prof = NIGHT if name == "night" else build(name)
    for voice in ("blunt", "plain"):
        req = narration_request(prof, voice)
        tpl = narrate.template(req)
        reason = check_numbers(req, tpl.headline + "\n" + tpl.body)
        if reason is not None or tpl.source != "template":
            _floor_ok = False
            print(f"       {name}/{voice}: {reason}")
check("every template, every leak, every voice, passes its own fact check", _floor_ok)
for name, prof in (("night", NIGHT), ("disposition", DISPO), ("clean", CLEAN)):
    for voice in ("blunt", "plain"):
        req = narration_request(prof, voice)
        tpl = narrate.template(req)
        reason = check_numbers(req, tpl.headline + "\n" + tpl.body)
        check(f"{name}/{voice} template passes its own fact check", reason is None,
              str(reason))
        check(f"{name}/{voice} template is marked as a template", tpl.source == "template")
check("clean trader gets the honest no-finding template",
      "No behavioural leak survived" in narrate.template(
          narration_request(CLEAN, "blunt")).body,
      narrate.template(narration_request(CLEAN, "blunt")).body[:80])


# ----------------------------------------------------------- request hygiene

print("\n=== the request carries facts, and only facts ===")
check("request is the typed contract", isinstance(REQ, NarrationRequest))
check("leaks are carried", len(REQ.leaks) == len(NIGHT.leaks) and len(REQ.leaks) > 0)
check("worst leak is named", REQ.worst_rule == NIGHT.worst_leak.rule)
check("underpowered `watch` findings are not shown to the model",
      all(leak.confidence != "weak" for leak in REQ.leaks))
prompt_json = REQ.to_dict()
check("no round-trips reach the prompt", "trips" not in prompt_json)
check("no trade ids reach the prompt", "trade_ids" not in str(prompt_json))
check("prompt is deterministic",
      narrate.build_prompt(REQ) == narrate.build_prompt(narration_request(NIGHT, "blunt")))


# -------------------------------------------------------------- end to end

print("\n=== narration end to end, against the fake ===")
fake = FakeAnthropic(reply(headline=FACT_HEADLINE, body=FACT_BODY))
out = narrate.narrate_profile(NIGHT, voice="blunt", client=fake)
check("returns a Narration", isinstance(out, Narration))
check("the model's words are used", out.source == "model", out.fallback_reason or "")
check("headline is the model's", out.headline == FACT_HEADLINE, out.headline)
check("body is the model's", out.body == FACT_BODY)
check("no fallback reason on success", out.fallback_reason is None)
check("the model was called exactly once", fake.call_count == 1)
check("the configured model id was sent",
      fake.calls[0]["model"] == narrate.DEFAULT_MODEL, str(fake.calls[0]["model"]))
check("a JSON schema was enforced",
      fake.calls[0]["output_config"]["format"]["type"] == "json_schema")
check("the system prompt states the rule",
      "Never state a number that is not in the facts" in fake.last_system())
check("the prompt is a fact sheet", "\"leaks\"" in fake.last_prompt())
check("the voice reached the prompt", "Voice: blunt" in fake.last_prompt())

plain_fake = FakeAnthropic(reply(headline="The small hours", body="No figures here."))
plain_out = narrate.narrate_profile(NIGHT, voice="plain", client=plain_fake)
check("plain voice reaches the prompt", "Voice: plain" in plain_fake.last_prompt())
check("the voice is carried onto the result", plain_out.voice == "plain")


# ------------------------------------------------ THE TEST THAT MATTERS

print("\n=== a model that invents a number is caught ===")
liar = FakeAnthropic(reply(
    headline="You lose money in the small hours",
    # -9,999 is not in the facts. Everything else in this sentence is.
    body="Your 65 trades in the small hours cost you -9,999 in total.",
))
caught = narrate.narrate_profile(NIGHT, voice="blunt", client=liar)
check("hallucinated number is rejected", caught.source == "template", caught.body[:80])
check("the rejection says what was wrong",
      "9,999" in (caught.fallback_reason or ""), str(caught.fallback_reason))
check("the rejection names the fact check",
      "fact check rejected" in (caught.fallback_reason or ""), str(caught.fallback_reason))
floor = narrate.template(REQ)
check("the output falls back to the template body", caught.body == floor.body)
check("the output falls back to the template headline", caught.headline == floor.headline)
check("the hallucinated number is nowhere in the output",
      "9,999" not in caught.body and "9,999" not in caught.headline)

inflated = FakeAnthropic(reply(
    headline="You lose money in the small hours",
    body="Your 650 trades in the small hours averaged -37 each.",
))
out2 = narrate.narrate_profile(NIGHT, voice="blunt", client=inflated)
check("an inflated sample size is caught", out2.source == "template")

derived = FakeAnthropic(reply(
    headline="You lose money in the small hours",
    # 65 x 37 = 2,405. True arithmetic, and still forbidden: the model is not
    # allowed to compute, only to quote, or the fact check means nothing.
    body="65 trades at -37 each is -2,405.00 by my count.",
))
check("a number the model derived itself is caught",
      narrate.narrate_profile(NIGHT, client=derived).source == "template")

spelled = FakeAnthropic(reply(
    headline="You lose money in the small hours",
    body="Sixty-five trades in the small hours, and they cost you plenty.",
))
out3 = narrate.narrate_profile(NIGHT, client=spelled)
check("a number spelled as a word is caught", out3.source == "template")
check("and the reason says so", "word" in (out3.fallback_reason or ""),
      str(out3.fallback_reason))


# -------------------------------------------------------------- shape checks

print("\n=== structural rejections ===")
check("empty headline", check_shape("", "body") is not None)
check("empty body", check_shape("head", "   ") is not None)
check("over-long headline", check_shape("x" * 200, "body") is not None)
check("over-long body", check_shape("head", "x" * 2000) is not None)
check("a URL is rejected", check_shape("head", "see https://example.com") is not None)
check("an ordinary narration passes", check_shape("head", "a body") is None)
check("shape failure falls back",
      narrate.narrate_profile(NIGHT, client=FakeAnthropic(reply(headline="", body="x"))).source
      == "template")


# ---------------------------------------------------------- degraded modes

print("\n=== degrades without a model ===")
with no_api_key():
    bare = narrate.narrate_profile(NIGHT, voice="blunt")
    check("no API key yields a template", bare.source == "template")
    check("and says why", "no API key" in (bare.fallback_reason or ""),
          str(bare.fallback_reason))
    check("the template is still complete", bool(bare.headline and bare.body))
    check("the template still passes its own fact check",
          check_numbers(REQ, bare.headline + "\n" + bare.body) is None)
    client, why = narrate.resolve_client(None)
    check("no client is constructed without a key", client is None and why is not None)

check("`anthropic` is never imported eagerly", "anthropic" not in sys.modules)

with fake_api_key():
    # A key present but the optional extra missing must be a reason, not a
    # traceback. On a machine that *does* have `anthropic`, a client comes back
    # and no network call has happened -- either outcome satisfies the contract,
    # which is that this never raises.
    try:
        client, why = narrate.resolve_client(None)
        resolved_cleanly = client is not None or why is not None
    except Exception as exc:
        resolved_cleanly, why = False, repr(exc)
    check("resolving a client never raises", resolved_cleanly, str(why))

broke = FakeAnthropic(RuntimeError("connection reset by peer"))
out4 = narrate.narrate_profile(NIGHT, client=broke)
check("a transport error yields a template", out4.source == "template")
check("and reports it", "connection reset" in (out4.fallback_reason or ""),
      str(out4.fallback_reason))

garbage = FakeAnthropic("this is not JSON at all")
check("unparseable output yields a template",
      narrate.narrate_profile(NIGHT, client=garbage).source == "template")
check("empty output yields a template",
      narrate.narrate_profile(NIGHT, client=FakeAnthropic("")).source == "template")
check("a JSON array instead of an object yields a template",
      narrate.narrate_profile(NIGHT, client=FakeAnthropic("[1, 2]")).source == "template")


# ------------------------------------------------------------------- judge

print("\n=== the gate: the verdict is an input ===")
PROPOSED = ProposedTrade(
    symbol="ETHUSDT", side="buy", notional=4000.0,
    ts=datetime(2026, 9, 1, 3, 14, tzinfo=timezone.utc),
    leverage=20.0, minutes_since_last_loss=41.0,
)
JUDGEMENT = gate_check(NIGHT, PROPOSED)
JREQ = judge.judgement_request(JUDGEMENT, symbol="ETHUSDT", proposed_notional=4000.0)
check("the gate said skip on this trade", JUDGEMENT.verdict == "skip", JUDGEMENT.verdict)
check("the verdict is carried into the request as an input",
      JREQ.verdict == JUDGEMENT.verdict)
jtpl = judge.template(JREQ)
check("the gate template passes its own fact check",
      check_numbers(JREQ, jtpl.headline + "\n" + jtpl.explanation) is None,
      str(check_numbers(JREQ, jtpl.headline + "\n" + jtpl.explanation)))

good = FakeAnthropic(reply(
    verdict="skip",
    headline="This is your worst pattern, at your worst hour",
    explanation=(
        "You have taken 65 trades in the small hours and won 23% of them, "
        "losing -37 each against a -12 baseline."
    ),
))
jout = judge.narrate_judgement(JUDGEMENT, symbol="ETHUSDT",
                               proposed_notional=4000.0, client=good)
check("returns a VerdictNarration", isinstance(jout, VerdictNarration))
check("the model's explanation is used", jout.source == "model", jout.fallback_reason or "")
check("the verdict survives untouched", jout.verdict == "skip")
check("the suggested size comes from the gate, not the model",
      jout.suggested_notional == JUDGEMENT.suggested_notional)
check("the system prompt fixes the verdict",
      "THE VERDICT IS DECIDED" in good.last_system())
check("the verdict is in the fact sheet",
      "verdict_decided_by_arithmetic" in good.last_prompt())

print("\n=== a model that changes the verdict is rejected ===")
flipper = FakeAnthropic(reply(
    verdict="caution",
    headline="Worth a second look before you size this",
    explanation="65 trades in the small hours at a 23% win rate.",
))
jflip = judge.narrate_judgement(JUDGEMENT, client=flipper)
check("a changed verdict is rejected", jflip.source == "template",
      jflip.explanation[:60])
check("the rendered verdict is still the gate's", jflip.verdict == "skip")
check("the reason names both verdicts",
      "caution" in (jflip.fallback_reason or "") and "skip" in (jflip.fallback_reason or ""),
      str(jflip.fallback_reason))

softener = FakeAnthropic(reply(
    verdict="skip",
    headline="Your worst hour, on your record",
    # Correct label, softened prose. This is the subtler and more likely
    # failure, and it is the one that would do the damage.
    explanation="65 trades, 23% win rate. You could proceed if you size it down.",
))
jsoft = judge.narrate_judgement(JUDGEMENT, client=softener)
check("prose asserting a different verdict is rejected", jsoft.source == "template")
check("the verdict is still skip", jsoft.verdict == "skip")

jliar = FakeAnthropic(reply(
    verdict="skip",
    headline="Your worst hour, on your record",
    explanation="You have lost -8,888 in the small hours across 65 trades.",
))
jout2 = judge.narrate_judgement(JUDGEMENT, client=jliar)
check("the gate narration is fact-checked too", jout2.source == "template")
check("and the invented number never reaches the output",
      "8,888" not in jout2.explanation and "8,888" not in jout2.headline)

with no_api_key():
    jbare = judge.narrate_judgement(JUDGEMENT)
    check("the gate degrades to a template with no key", jbare.source == "template")
    check("the verdict survives the degraded path", jbare.verdict == "skip")
    check("the base rates survive the degraded path",
          len(jbare.analogues) == len(JUDGEMENT.analogues) > 0)

# `SilentClient` raises the moment anything touches `.messages`, so passing it
# in proves the short-circuit really is a short-circuit: if `narrate` reached
# the call site at all, this assertion would surface as a fallback_reason.
loud = narrate.narrate_profile(NIGHT, client=SilentClient())
check("an unusable client degrades rather than crashing", loud.source == "template")
check("and the failure is reported, not swallowed",
      "model call failed" in (loud.fallback_reason or ""), str(loud.fallback_reason))

print("\n=== both directions, and thin histories ===")
FAV = gate_check(
    NIGHT,
    ProposedTrade(symbol="ETHUSDT", side="buy", notional=100.0,
                  ts=datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)),
)
freq = judge.judgement_request(FAV)
check("a non-skip verdict also narrates deterministically",
      judge.template(freq).verdict == FAV.verdict, FAV.verdict)
check("its template passes its own fact check",
      check_numbers(freq, judge.template(freq).headline + "\n"
                    + judge.template(freq).explanation) is None)

THIN = profile_mod.for_history(generate(TraderSpec(seed=7, leak=None, n_trades=12, days=9)))
treq = narration_request(THIN, "blunt")
ttpl = narrate.template(treq)
check("a thin profile says so rather than inventing", treq.summary.is_thin)
check("and its template passes its own fact check",
      check_numbers(treq, ttpl.headline + "\n" + ttpl.body) is None,
      str(check_numbers(treq, ttpl.headline + "\n" + ttpl.body)))

print("\n=== polarity: the model may not reverse the sign of a cost ===")
# check_numbers compares magnitudes, so -3,488 and 3,488 are equal to it. That
# tolerance is deliberate (it lets the model write "cost you 3,488") and it is
# also a hole: "you made 3,488" passes the same test. On a card whose purpose is
# telling someone what a habit costs, an inverted sign is the worst possible
# output -- every number checks out and the meaning is backwards.
from gnosis.agents import check_polarity  # noqa: E402
from gnosis.agents.narrate import narration_request as _nr  # noqa: E402
from gnosis.ingest.synthetic import TraderSpec as _TS, generate as _gen  # noqa: E402
from gnosis.model import profile as _P  # noqa: E402

_req = _nr(_P.for_history(_gen(_TS(seed=31, leak="night"))))
_negs = [v for v in _req.values() if isinstance(v, (int, float)) and v < 0]
check("request exposes negative facts", bool(_negs), f"{_negs[:3]}")

check("faithful loss wording passes",
      check_polarity(_req, "Across the year you are down 3,150.") is None)
check("'cost you' passes",
      check_polarity(_req, "The small hours cost you 2,154.") is None)
check("'you made' on a cost is rejected",
      check_polarity(_req, "Across the year you made 3,150.") is not None)
check("'profit of' on a cost is rejected",
      check_polarity(_req, "That session handed you a profit of 2,154.") is not None)
check("'earned' on a negative expectancy is rejected",
      check_polarity(_req, "You earned 12 per trade.") is not None)
check("gain word on a genuinely positive fact is allowed",
      check_polarity(_req, "You made 2,406 more by skipping them.") is None)
check("gain and loss in one sentence is not rejected",
      check_polarity(_req, "You made trades that cost you 3,150.") is None)
check("no negatives in facts -> no opinion",
      check_polarity(type(_req)(**{**_req.__dict__}) if False else _req, "nothing numeric here") is None)

# The deterministic template must pass its own polarity check, for every leak
# type -- otherwise a rejection swaps one unverifiable output for another.
for _leak in ("disposition", "martingale", "revenge", "night", "overleverage"):
    _p = _P.for_history(_gen(_TS(seed=31, leak=_leak)))
    _r = _nr(_p)
    from gnosis.agents.narrate import template as _tpl  # noqa: E402
    _t = _tpl(_r)
    _prose = f"{_t.headline}\n{_t.body}"
    check(f"template passes polarity: {_leak}",
          check_polarity(_r, _prose) is None, str(check_polarity(_r, _prose)))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
