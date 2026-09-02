"""The HTML report and the outbound webhook.

Two modules that both produce something meant to leave the machine, which is
why they are tested together: the failure they share is *disclosure*, and the
properties below are the ones a docstring cannot guarantee.

  1. **The report fetches nothing.** Asserted as a property of the bytes, not
     as a review of the source: no `http://` and no `https://` anywhere in the
     document, no `src`, no `href`, no `<script>`, no `<link>`, no `@import`
     and no `url()` in the CSS. A report that pulls one stylesheet stops
     working offline, stops working in an email client, and tells whoever
     hosts the asset every time a trading record is opened.

  2. **Redaction actually removes currency.** Checked against the script's own
     definition of what money looks like, over the visible text of the whole
     document -- not by looking for a handful of figures the test happened to
     think of. The rule is also pinned to `scripts/publish_card.py` by running
     both over a real card and requiring identical output, so the copy in
     `report/html.py` cannot drift.

  3. **The webhook will not send without `send=True`.** Driven through the real
     code path with a spy transport that fails the test if it is called. The
     dry run has to produce the exact bytes and open nothing.

  4. **A webhook payload carries no currency unless it was asked for.** Applied
     when the payload is built rather than when it is rendered, so there is no
     formatter that can leak a figure by forgetting.

Nothing here touches a network, and the socket layer is stubbed out during the
webhook section to prove it.

Run: python3 tests/test_report.py
"""

from __future__ import annotations

import importlib.util
import json
import re
import socket
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gnosis.card import render as render_card  # noqa: E402
from gnosis.gate.elenchos import ProposedTrade  # noqa: E402
from gnosis.gate.elenchos import check as gate_check  # noqa: E402
from gnosis.ingest.synthetic import TraderSpec, generate  # noqa: E402
from gnosis.model import profile as profile_mod  # noqa: E402
from gnosis.model.profile import Profile, Summary  # noqa: E402
from gnosis.notify import webhook as wh  # noqa: E402
from gnosis.report import html as report  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{'' if cond else '  <- ' + detail}")


def load_script(name: str):
    """Import a file from `scripts/`, which is not a package."""
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"_script_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def visible_text(html: str) -> str:
    """What a reader actually sees, near enough for a redaction check.

    Drops the stylesheet, then the trade-id `<code>` elements, then every tag.
    Ids go because an id is an identifier and not an amount -- `csv-000605`
    says nothing about the size of anyone's book -- and they are the whole
    reason a finding can be audited rather than believed, so they survive
    redaction on purpose. Attribute values disappear with their tags, which is
    what makes SVG geometry invisible to this and its `<text>` labels visible.
    """
    text = re.sub(r"<style.*?</style>", " ", html, flags=re.S)
    text = re.sub(r"<code[^>]*>.*?</code>", " ", text, flags=re.S)
    return re.sub(r"<[^>]+>", " ", text)


PROFILE = profile_mod.for_history(generate(TraderSpec(seed=31, leak="night")))
DEMO = profile_mod.for_history(
    __import__("gnosis.ingest.binance_csv", fromlist=["parse_csv"]).parse_csv(
        str(ROOT / "corpus" / "demo-account.csv")
    )
)
DOC = report.render_html(PROFILE)
REDACTED = report.render_html(PROFILE, redacted=True)


# ==========================================================================
# A standalone document
# ==========================================================================

print("\n=== the report is a standalone HTML document ===")

check("it declares a doctype", DOC.startswith("<!DOCTYPE html>"), DOC[:40])
check("it declares a language", '<html lang="en">' in DOC)
check("it declares a charset", 'charset="utf-8"' in DOC)
check("it declares a viewport, so it is readable on a phone", 'name="viewport"' in DOC)
check("it has a title", "<title>" in DOC and "</title>" in DOC)
check("the title names the source", "synthetic" in DOC[:DOC.index("</title>")], DOC[:400])
check("it closes html", DOC.rstrip().endswith("</html>"))
check("head and body are both present and closed",
      DOC.count("<head>") == DOC.count("</head>") == 1
      and DOC.count("<body>") == DOC.count("</body>") == 1)
for tag in ("section", "div", "dl", "details", "svg", "main", "footer", "header"):
    check(f"<{tag}> elements are balanced",
          DOC.count(f"<{tag}") == DOC.count(f"</{tag}>"),
          f"{DOC.count(f'<{tag}')} open vs {DOC.count(f'</{tag}>')} close")

print("\n=== ...that fetches nothing ===")

for doc, label in ((DOC, "report"), (REDACTED, "redacted report")):
    check(f"the {label} contains no http:// anywhere", "http://" not in doc)
    check(f"the {label} contains no https:// anywhere", "https://" not in doc)
    check(f"the {label} has no src attribute", not re.search(r"\bsrc\s*=", doc))
    check(f"the {label} has no href attribute", not re.search(r"\bhref\s*=", doc))
    check(f"the {label} has no <script>", "<script" not in doc.lower())
    check(f"the {label} has no <link>", "<link" not in doc.lower())
    check(f"the {label} has no <img>", "<img" not in doc.lower())
    check(f"the {label} has no <iframe>", "<iframe" not in doc.lower())
    check(f"the {label} has no @import in its CSS", "@import" not in doc)
    check(f"the {label} loads no url() asset", "url(" not in doc)
check("inline svg carries no xmlns, which would smuggle a URI into the document",
      "xmlns" not in DOC)
check("the stylesheet is inline", "<style>" in DOC and "</style>" in DOC)

print("\n=== ...that respects the reader's theme ===")

check("it honours prefers-color-scheme", "prefers-color-scheme: dark" in DOC)
check("it declares color-scheme so the canvas is painted before the CSS lands",
      'name="color-scheme"' in DOC and "color-scheme: light dark" in DOC)
light_tokens = set(re.findall(r"(--[a-z]+):", report.CSS.split("@media")[0]))
dark_tokens = set(re.findall(r"(--[a-z]+):", report.CSS.split("@media")[1].split("}")[0]))
check("the light palette defines tokens at all", len(light_tokens) >= 8, str(light_tokens))
check("every colour token defined for light is redefined for dark",
      light_tokens <= dark_tokens, str(sorted(light_tokens - dark_tokens)))
check("no colour is hard-coded outside the token block",
      report.CSS.count("#") == len(re.findall(r"--[a-z]+: #", report.CSS)),
      "a hex colour was written outside a custom property")


# ==========================================================================
# Content
# ==========================================================================

print("\n=== everything the profile knows reaches the page ===")

seen = visible_text(DOC)
check("the summary is rendered",
      f"{PROFILE.summary.n_trades} trades" in seen, seen[:200])
check("the win rate is rendered", f"{PROFILE.summary.win_rate:.0%}" in seen)
check("the net realised figure is rendered", f"{PROFILE.summary.total_pnl:+,.0f}" in seen)
check("every leak title appears", all(leak.title in seen for leak in PROFILE.leaks),
      str([leak.title for leak in PROFILE.leaks if leak.title not in seen]))
check("every leak's finding sentence appears",
      all(leak.finding in seen for leak in PROFILE.leaks))
check("every leak's confidence tier is labelled",
      all(leak.confidence in seen for leak in PROFILE.leaks))
check("every leak's receipts are in the document",
      all(tid in DOC for leak in PROFILE.leaks for tid in leak.trade_ids),
      "a trade id went missing")
check("receipts are counted in a disclosure the reader can open",
      "trade receipts</summary>" in DOC)
check("the worst leak is given its own heading",
      "Your most expensive habit" in seen and PROFILE.worst_leak.title in seen)
check("strengths are rendered",
      all(s.title in seen for s in PROFILE.strengths) if PROFILE.strengths else True)
check("counterfactuals are rendered",
      all(cf.description in seen for cf in PROFILE.counterfactuals))
check("the counterfactual caveat says the rest are assumed unchanged",
      "assumes every remaining trade is unchanged" in seen.lower(), "caveat missing")
check("...and that these describe the past rather than forecasting",
      "descriptions of the past, not forecasts" in seen)
check("...and that a detector had to license the slice first",
      "a detector already proved a significant leak" in seen)
check("unlicensed counterfactuals are separated from the licensed ones",
      ("Observations, not recommendations" in seen) == bool(PROFILE.observations))
if PROFILE.observations:
    check("the hindsight caveat explains why those are not advice",
          "chosen by looking at the very outcome" in seen)
    check("hindsight-selected rules are chipped as such",
          ("hindsight" in seen) == any(cf.hindsight for cf in PROFILE.observations))
check("the footer says the numbers were computed, not generated",
      "computed and significance-tested" in seen)
check("the footer disclaims investment advice", "not investment advice" in seen)

print("\n=== the charts are hand-rolled SVG ===")

check("there are two charts", DOC.count("<svg") == 2, str(DOC.count("<svg")))
check("no chart library is loaded",
      not any(lib in DOC.lower() for lib in
              ("chart.js", "plotly", "highcharts", "echarts", "d3.min", "apexcharts")))
bars = report.session_bars(PROFILE.trips)
check("one session bar per pre-registered session", len(bars) == 4, str(len(bars)))
check("the session bars account for every trade",
      sum(b.n for b in bars) == len(PROFILE.trips),
      f"{sum(b.n for b in bars)} vs {len(PROFILE.trips)}")
check("the session bars account for every dollar",
      abs(sum(b.value for b in bars) - sum(t.net_pnl for t in PROFILE.trips)) < 1e-6)
check("session labels are written as clock times, which redaction protects",
      all(":" in b.label for b in bars), str([b.label for b in bars]))
lev = report.leverage_bars(PROFILE.trips)
check("the leverage bars account for every trade",
      sum(b.n for b in lev) == len(PROFILE.trips), str([(b.label, b.n) for b in lev]))
check("no leverage bucket is empty", all(b.n > 0 for b in lev))
check("losing bars are drawn in the negative colour class",
      ('class="bar neg"' in DOC) == any(b.value < 0 for b in bars + lev))
check("winning bars are drawn in the positive colour class",
      ('class="bar pos"' in DOC) == any(b.value >= 0 for b in bars + lev))
check("every bar carries its trade count under it",
      all(f"{b.n} trades" in DOC for b in bars))
check("each chart has an accessible label", DOC.count("role=\"img\"") == 2)
check("each chart has a text description for a screen reader", DOC.count("<desc>") == 2)
check("no bar has negative height, which browsers refuse to draw",
      not re.search(r'height="-', DOC))
check("a chart of nothing renders as nothing rather than a broken axis",
      report.bar_chart([], fmt=report._Fmt(redacted=False), caption="x") == "")
flat = report.bar_chart(
    [report.Bar("00:00–06:00", 0.0, 3)], fmt=report._Fmt(redacted=False), caption="x"
)
check("an all-zero chart still draws a bar rather than dividing by zero",
      "<rect" in flat and "nan" not in flat.lower(), flat[:120])


# ==========================================================================
# Redaction
# ==========================================================================

print("\n=== redaction removes currency and keeps everything else ===")

pc = load_script("publish_card.py")
card_text = render_card(DEMO, colour=False)
check("the report's redactor agrees with publish_card.py on a real card",
      report.redact(card_text) == pc.redact(card_text),
      "the duplicated rule has drifted")
check("...and so does its inverse",
      report.currency_amounts(card_text) == pc.currency_amounts(card_text))

demo_doc = report.render_html(DEMO)
demo_redacted = report.render_html(DEMO, redacted=True)
for label, doc in (("fixture", REDACTED), ("demo account", demo_redacted)):
    leftovers = report.currency_amounts(visible_text(doc))
    check(f"the redacted {label} report contains no currency amount at all",
          leftovers == [], f"left behind: {leftovers[:8]}")

check("the unredacted report does contain currency, so the test above means something",
      report.currency_amounts(visible_text(demo_doc)) != [])
check("the net realised figure is gone", f"{DEMO.summary.total_pnl:+,.0f}" not in demo_redacted)
check("the fee total is gone", f"{DEMO.summary.total_fees:,.0f}" not in demo_redacted)
check("the worst leak's cost is gone", f"{DEMO.worst_leak.cost:+,.0f}" not in demo_redacted)

redacted_seen = visible_text(demo_redacted)
check("the win rate percentage survives", f"{DEMO.summary.win_rate:.0%}" in redacted_seen)
check("the trade count survives", f"{DEMO.summary.n_trades} trades" in redacted_seen)
check("the day span survives", f"{DEMO.summary.span_days:.0f} days" in redacted_seen)
check("the symbol count survives", f"{len(DEMO.summary.symbols)} symbols" in redacted_seen)
check("session clock times survive", "00:00" in redacted_seen and "06:00" in redacted_seen)
check("leverage ratios survive", "10x" in redacted_seen)
check("a hold-time ratio written as 4.9x survives",
      report.redact("you hold losers 4.9x longer") == "you hold losers 4.9x longer",
      report.redact("you hold losers 4.9x longer"))
check("...and a 1.9:1 style ratio survives too",
      report.redact("a 1.9:1 ratio") == "a 1.9:1 ratio", report.redact("a 1.9:1 ratio"))
check("every leak title still appears", all(leak.title in redacted_seen for leak in DEMO.leaks))
check("the charts are still drawn -- the shape is ratios, not balances",
      demo_redacted.count("<svg") == 2)
check("but their value labels are struck out",
      demo_redacted.count(report.REDACTED) >= 6, str(demo_redacted.count(report.REDACTED)))
check("receipts survive redaction, because an id is not an amount",
      all(tid in demo_redacted for leak in DEMO.leaks for tid in leak.trade_ids))
check("the document says it has been redacted", "redacted" in redacted_seen)
check("...and the footer says what was removed",
      "Absolute currency amounts have been removed" in redacted_seen)
check("the generation timestamp is dropped, since a year looks like an amount",
      "2026-09-02" not in report.render_html(
          DEMO, redacted=True, generated_at=datetime(2026, 9, 2, tzinfo=timezone.utc)))


# ==========================================================================
# Edge cases
# ==========================================================================

print("\n=== edge cases ===")

thin = Profile(summary=Summary(
    n_trades=6, n_open=1, span_days=3.0, total_pnl=-120.0, total_fees=4.0,
    win_rate=0.3333, expectancy=-20.0, symbols=["ETHUSDT"], source="synthetic:thin",
))
thin_doc = report.render_html(thin)
check("a thin profile still produces a valid document",
      thin_doc.startswith("<!DOCTYPE html>") and thin_doc.rstrip().endswith("</html>"))
check("...that says there is not enough history",
      "not enough to tell you anything you should act on" in visible_text(thin_doc))
check("...and invents no habit to fill the space",
      "most expensive habit" not in visible_text(thin_doc).lower())
check("...and draws no charts off six trades", "<svg" not in thin_doc)
check("...and states the bar it failed to clear",
      "30 closed trades" in visible_text(thin_doc))
check("a thin profile redacts cleanly too",
      report.currency_amounts(visible_text(report.render_html(thin, redacted=True))) == [])

clean = profile_mod.for_history(generate(TraderSpec(seed=7, leak=None)))
clean_doc = report.render_html(clean)
if not clean.leaks and not clean.summary.is_thin:
    check("a profile with no leaks says the detectors ran and found nothing",
          "No behavioural leak cleared the significance bar" in visible_text(clean_doc))
else:
    check("a clean-twin profile renders without error", clean_doc.endswith("</html>\n"))

hostile = Profile(summary=Summary(
    n_trades=40, n_open=0, span_days=90.0, total_pnl=1.0, total_fees=0.0,
    win_rate=0.5, expectancy=0.02, symbols=["ETHUSDT"],
    source='<script>alert("xss")</script>&<>"',
))
hostile_doc = report.render_html(hostile)
check("a hostile source string is escaped, not executed",
      "<script>alert" not in hostile_doc and "&lt;script&gt;" in hostile_doc,
      hostile_doc[:400])
check("...and the document stays balanced", hostile_doc.rstrip().endswith("</html>"))

check("the same profile renders to identical bytes twice",
      report.render_html(PROFILE) == report.render_html(PROFILE))
stamped = report.render_html(
    PROFILE, generated_at=datetime(2026, 9, 2, 13, 45, tzinfo=timezone.utc)
)
check("a timestamp is stamped when one is passed", "2026-09-02 13:45 UTC" in stamped)
check("...and is the only difference from the unstamped render",
      len(stamped) > len(DOC) and stamped.replace(" · generated 2026-09-02 13:45 UTC", "") == DOC)
check("a custom title reaches both the heading and the browser tab",
      "August book" in report.render_html(PROFILE, title="August book")[:2000])


# ==========================================================================
# scripts/make_report.py
# ==========================================================================

print("\n=== scripts/make_report.py ===")

mr = load_script("make_report.py")
with tempfile.TemporaryDirectory() as tmp:
    out = Path(tmp) / "nested" / "report.html"
    code = mr.main(["synthetic:night:31", "--out", str(out), "--no-timestamp"])
    check("the script exits 0", code == 0, str(code))
    check("it creates the file, and any directory it needs", out.exists())
    written = out.read_text(encoding="utf-8")
    check("what it wrote is what render_html produces", written == DOC)
    check("it fetches nothing", "http://" not in written and "https://" not in written)

    red = Path(tmp) / "share.html"
    mr.main(["synthetic:night:31", "--out", str(red), "--redact", "--no-timestamp"])
    check("--redact removes currency from the written file",
          report.currency_amounts(visible_text(red.read_text(encoding="utf-8"))) == [])
    check("redaction is off unless asked for, because this writes to your own disk",
          report.currency_amounts(visible_text(written)) != [])
    check("rerunning overwrites rather than failing",
          mr.main(["synthetic:night:31", "--out", str(out), "--no-timestamp"]) == 0)


# ==========================================================================
# The webhook
# ==========================================================================

print("\n=== the webhook refuses to send ===")

SLACK = "https://hooks.slack.com/services/T00/B00/xxxx"
DISCORD = "https://discord.com/api/webhooks/123/abc"
GENERIC = "https://example.invalid/collector"

check("a Slack URL is recognised", wh.detect_flavour(SLACK) == "slack")
check("a Discord URL is recognised", wh.detect_flavour(DISCORD) == "discord")
check("a Discord CDN alias is recognised",
      wh.detect_flavour("https://discordapp.com/api/webhooks/1/x") == "discord")
check("anything else is generic rather than an error", wh.detect_flavour(GENERIC) == "generic")
check("the hostname decides, not a substring of the path",
      wh.detect_flavour("https://example.invalid/hooks.slack.com/x") == "generic")
check("a subdomain of a known host is still recognised",
      wh.detect_flavour("https://canary.discord.com/api/webhooks/1/x") == "discord")


class SpyTransport:
    def __init__(self, status=200, body="ok") -> None:
        self.calls: list[tuple] = []
        self.status, self.body = status, body

    def __call__(self, url, body, headers):
        self.calls.append((url, body, dict(headers)))
        return self.status, self.body


def exploding_transport(url, body, headers):
    raise AssertionError("the transport was called during a dry run")


JUDGEMENT = gate_check(DEMO, ProposedTrade(
    symbol="DOGEUSDT", side="buy", notional=7000.0,
    ts=datetime(2026, 9, 1, 3, 14, tzinfo=timezone.utc),
    leverage=25.0, minutes_since_last_loss=41.0,
))
check("the fixture judgement is a warning, so the message under test has content",
      JUDGEMENT.verdict == "skip", JUDGEMENT.verdict)

real_socket = socket.socket
socket.socket = lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened a socket"))
try:
    for url in (SLACK, DISCORD, GENERIC):
        flavour = wh.detect_flavour(url)
        dry = wh.notify_judgement(url, JUDGEMENT, symbol="DOGEUSDT", notional=7000,
                                  transport=exploding_transport)
        check(f"{flavour}: a dry run does not send", dry.sent is False)
        check(f"{flavour}: a dry run says why nothing was sent", "send=True" in dry.reason)
        check(f"{flavour}: it still produces the exact bytes",
              json.loads(dry.body.decode("utf-8")) == dry.payload)
        check(f"{flavour}: and names the destination", dry.url == url)
        check(f"{flavour}: describe() marks it as a dry run", "DRY RUN" in dry.describe())

    dry = wh.notify_profile(GENERIC, DEMO, transport=exploding_transport)
    check("a profile summary is a dry run by default too", dry.sent is False)

    check("send=False explicitly is still a dry run",
          wh.notify_judgement(GENERIC, JUDGEMENT, symbol="X", send=False,
                              transport=exploding_transport).sent is False)
    check("the module states that no environment variable can flip the default",
          "no environment variable that flips it" in wh.send_payload.__doc__)
finally:
    socket.socket = real_socket

print("\n=== ...and sends only when told to ===")

spy = SpyTransport()
sent = wh.notify_judgement(SLACK, JUDGEMENT, symbol="DOGEUSDT", notional=7000,
                           send=True, transport=spy)
check("send=True sends exactly once", len(spy.calls) == 1, str(len(spy.calls)))
check("it reports that it sent", sent.sent is True)
check("it records the status", sent.status == 200)
check("the URL is the one given", spy.calls[0][0] == SLACK)
check("the body is JSON", json.loads(spy.calls[0][1].decode("utf-8")) == sent.payload)
check("the content type is set", spy.calls[0][2]["Content-Type"] == "application/json")
check("it identifies itself", "gnosis" in spy.calls[0][2]["User-Agent"])

spy = SpyTransport(status=403, body="forbidden")
err = None
try:
    wh.notify_judgement(GENERIC, JUDGEMENT, symbol="X", send=True, transport=spy)
except wh.WebhookError as exc:
    err = exc
check("a non-2xx response raises rather than reporting success", err is not None)
check("...and the status is in the message", err and "403" in str(err), str(err))

spy = SpyTransport(status=204, body="")
check("Discord's empty 204 counts as sent",
      wh.notify_judgement(DISCORD, JUDGEMENT, symbol="X", send=True, transport=spy).sent is True)

spy = SpyTransport()
err = None
try:
    wh.notify_judgement("http://example.invalid/hook", JUDGEMENT, symbol="X",
                        send=True, transport=spy)
except wh.WebhookError as exc:
    err = exc
check("plain http is refused: a judgement names a trader, a symbol and a size",
      err is not None and spy.calls == [], str(err))
check("a localhost collector over http is allowed, since it crosses no network",
      wh.notify_judgement("http://localhost:9000/hook", JUDGEMENT, symbol="X",
                          send=True, transport=SpyTransport()).sent is True)

print("\n=== ...and never carries money unless asked ===")


def strings_in(payload) -> str:
    """Every string value in a payload, which is where prose can leak a figure."""
    if isinstance(payload, dict):
        return " ".join(strings_in(v) for v in payload.values())
    if isinstance(payload, list):
        return " ".join(strings_in(v) for v in payload)
    return payload if isinstance(payload, str) else ""


for url in (SLACK, DISCORD, GENERIC):
    flavour = wh.detect_flavour(url)
    quiet = wh.notify_judgement(url, JUDGEMENT, symbol="DOGEUSDT", notional=7000).payload
    leftovers = report.currency_amounts(strings_in(quiet))
    check(f"{flavour}: a judgement carries no currency by default",
          leftovers == [], f"left behind: {leftovers[:6]}")
    loud = wh.notify_judgement(url, JUDGEMENT, symbol="DOGEUSDT", notional=7000,
                               show_amounts=True).payload
    check(f"{flavour}: show_amounts=True puts the figures back",
          report.currency_amounts(strings_in(loud)) != [])
    check(f"{flavour}: the verdict survives redaction",
          JUDGEMENT.verdict.upper() in strings_in(quiet))
    check(f"{flavour}: the win rates survive redaction", "%" in strings_in(quiet))

    prof_quiet = wh.notify_profile(url, DEMO).payload
    check(f"{flavour}: a profile summary carries no currency by default",
          report.currency_amounts(strings_in(prof_quiet)) == [],
          str(report.currency_amounts(strings_in(prof_quiet))[:6]))
    check(f"{flavour}: it still names the worst habit",
          DEMO.worst_leak.title in strings_in(prof_quiet))

check("the proposed size is withheld unless amounts are on",
      "7,000" not in strings_in(wh.notify_judgement(GENERIC, JUDGEMENT, symbol="D",
                                                    notional=7000).payload))
check("...and a re-size is described rather than quantified",
      "smaller size than the one proposed" in strings_in(
          wh.notify_judgement(GENERIC, JUDGEMENT, symbol="D", notional=7000).payload))

print("\n=== ...formatted the way each destination expects ===")

slack_payload = wh.notify_judgement(SLACK, JUDGEMENT, symbol="DOGEUSDT").payload
check("Slack gets blocks", isinstance(slack_payload.get("blocks"), list))
check("Slack also gets a text fallback, or the sidebar preview is blank",
      bool(slack_payload.get("text")))
check("the first Slack block is a header", slack_payload["blocks"][0]["type"] == "header")
check("Slack blocks all declare a type",
      all("type" in b for b in slack_payload["blocks"]))
check("Slack is told this is not investment advice",
      "not investment advice" in strings_in(slack_payload))

discord_payload = wh.notify_judgement(DISCORD, JUDGEMENT, symbol="DOGEUSDT").payload
check("Discord gets an embed", isinstance(discord_payload.get("embeds"), list))
embed = discord_payload["embeds"][0]
check("the embed is colour-coded by verdict", embed["color"] == wh.COLOUR["skip"])
check("the embed title is within Discord's 256-character limit", len(embed["title"]) <= 256)
check("the embed description is within the 4096-character limit",
      len(embed["description"]) <= 4096)
check("every embed field value is within the 1024-character limit",
      all(len(f["value"]) <= 1024 for f in embed.get("fields", [])))
check("Discord is told this is not investment advice",
      "not investment advice" in strings_in(discord_payload))

generic_payload = wh.notify_judgement(GENERIC, JUDGEMENT, symbol="DOGEUSDT").payload
check("a generic receiver gets structured facts, not just prose",
      generic_payload["facts"]["verdict"] == "skip")
check("...including whether each analogue was significant",
      all("significant" in a for a in generic_payload["facts"]["analogues"]))
check("...and a rendered text field for a receiver that just wants to print",
      "SKIP" in generic_payload["text"])
check("it names itself as the source", generic_payload["source"] == "gnosis")

thin_payload = wh.notify_profile(GENERIC, thin).payload
check("a thin profile says it has no opinion rather than sending an empty card",
      thin_payload["facts"]["is_thin"] is True and "no opinion" in thin_payload["summary"],
      thin_payload["summary"][:80])
check("...and does not present that as a clean bill of health",
      "not a failure" in thin_payload["summary"])

err = None
try:
    wh.build_payload(wh.profile_message(DEMO), "telepathy")
except wh.WebhookError as exc:
    err = exc
check("build_payload refuses an unknown flavour", err is not None, str(err))

src = (ROOT / "src" / "gnosis" / "notify" / "webhook.py").read_text(encoding="utf-8")
check("urllib is imported lazily, inside the transport function",
      "\nimport urllib" not in src and "    import urllib.request" in src)
check("there is no retry loop, so a delivered-then-failed post is not duplicated",
      "retry" not in src.lower().replace("no retries", "").replace(
          "retrying", "").replace("a retry", ""))

described = wh.notify_judgement(GENERIC, JUDGEMENT, symbol="D").describe()
check("describe() shows the body that would have been sent",
      "body" in described and "DRY RUN" in described, described[:80])

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
