"""The profile as one self-contained HTML file.

Everything the document needs is inside it: the CSS is in a `<style>` block,
the two charts are SVG emitted by the functions below, and there is no
JavaScript, no font request, no image and no link out. Open it from disk on a
machine with no network and it renders exactly as it will in six months, when
whichever CDN would otherwise have been holding the stylesheet has reorganised
its paths. `tests/test_report.py` asserts the property directly rather than
trusting this paragraph: no `http://` and no `https://` anywhere in the output.

Two consequences of that constraint worth stating, because both look like
laziness and are not:

**The charts are hand-rolled SVG.** Two bar charts, drawn from the same
`RoundTrip` list every detector reads. A charting library would be a second
dependency and a second arithmetic engine, and the second engine is the real
objection -- a library that bins or aggregates is a place where the picture can
disagree with the numbers printed beside it. These functions do no aggregation
the profile has not already done.

**Inline `<svg>` carries no `xmlns`.** It does not need one: an HTML parser
puts `<svg>` in the SVG namespace by itself. Emitting the namespace URI would
put an `http://` in a document that promises it has none, and the promise is
worth more than the attribute.

Colour follows `prefers-color-scheme`, both ways, from one set of custom
properties. A dark-mode reader opening a searing white report at 3am is a small
thing that makes a document feel unmaintained.

**Redaction.** `--redact` keeps percentages, ratios and counts and removes
absolute currency amounts, matching `scripts/publish_card.py` exactly -- the
same deny-by-default rule, for the same reason. The psychology is the shareable
part of a trading record; the balance is the trader's net worth, and once it is
in a file that gets forwarded it is not coming back. Every string that reaches
the document from a profile passes through `redact()` when it is on, including
the ones where over-redaction is merely cosmetic, because the two errors are
not symmetric: a redacted count is an annoyance and a missed figure is the
thing the user was trying not to send.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from ..detectors.sizing import HIGH_LEVERAGE
from ..detectors.timing import SESSIONS
from ..model.profile import Profile
from ..model.roundtrip import RoundTrip

# --------------------------------------------------------------------------
# Redaction -- the same rule as scripts/publish_card.py
# --------------------------------------------------------------------------
#
# Duplicated rather than imported because `scripts/` is not a package and this
# module is; importing a script by file path from inside a library is worse
# than sixteen lines of regex. `tests/test_report.py` pins the two together by
# asserting they agree on a real card, so the copy cannot drift silently.

_KEEP_UNITS = (
    r"trades?|days?|weeks?|months?|years?|hours?|hrs?|minutes?|mins?|seconds?|secs?"
    r"|symbols?|receipts?|times?|positions?|tokens?|swaps?|fills?|orders?"
)
_NUMBER = re.compile(r"(?:[$€£¥]\s?)?[-+−]?\d[\d,]*(?:\.\d+)?")
_KEEP_AFTER = re.compile(rf"\s*(?:%|[xX]\b|{_KEEP_UNITS})", re.IGNORECASE)
_PROTECTED = re.compile(r"\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?")
_SENTINEL = "\x00"

REDACTED = "—"


def _stage(text: str) -> tuple[str, list[str]]:
    """Swap ratio/clock spans for a digit-free sentinel, in order."""
    kept: list[str] = []

    def protect(match: re.Match) -> str:
        kept.append(match.group(0))
        return _SENTINEL

    return _PROTECTED.sub(protect, text), kept


def redact(text: str) -> str:
    """Remove absolute currency amounts; keep percentages, ratios and counts."""
    staged, kept = _stage(text)

    def scrub(match: re.Match) -> str:
        return match.group(0) if _KEEP_AFTER.match(staged[match.end():]) else REDACTED

    staged = _NUMBER.sub(scrub, staged)
    for original in kept:
        staged = staged.replace(_SENTINEL, original, 1)
    return staged


def currency_amounts(text: str) -> list[str]:
    """Every number in `text` that redaction would treat as money.

    The inverse of `redact`, so a test can assert the property directly instead
    of checking that a few figures somebody thought of have disappeared.
    """
    staged, _ = _stage(text)
    return [
        m.group(0) for m in _NUMBER.finditer(staged)
        if not _KEEP_AFTER.match(staged[m.end():])
    ]


# --------------------------------------------------------------------------
# Escaping
# --------------------------------------------------------------------------

def _esc(value: object) -> str:
    """HTML-escape, including both quote characters.

    Written out rather than imported from the standard library's `html` module,
    which this file shares a name with. Absolute imports mean `import html`
    here does resolve to the standard library -- but only until somebody runs
    this file as a script, at which point its own directory goes on `sys.path`
    and it shadows itself. Four lines removes the whole class of confusion.

    Escaping `>` is not decorative: the tests strip tags with a regex to
    recover the visible text, and an unescaped `>` inside an attribute would
    end a tag early and let markup leak into the text being checked.
    """
    return (
        str(value)
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )


# --------------------------------------------------------------------------
# Number formatting
# --------------------------------------------------------------------------

@dataclass
class _Fmt:
    """Formatting that knows whether money is allowed to appear.

    Absolute amounts are suppressed structurally here -- the figure is never
    written -- rather than by writing it and scrubbing the document afterwards.
    Free-text findings from the detectors still go through `redact()`, because
    those have amounts baked into prose and there is nothing structural to
    suppress.
    """

    redacted: bool

    def money(self, value: float | None, *, sign: bool = True) -> str:
        if value is None:
            return "not estimated"
        if self.redacted:
            return REDACTED
        return f"{value:+,.0f}" if sign else f"{value:,.0f}"

    def prose(self, text: str) -> str:
        return redact(text) if self.redacted else text

    @staticmethod
    def pct(value: float) -> str:
        return f"{value:.0%}"


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------

@dataclass
class Bar:
    label: str      # printed under the bar; must survive redaction
    value: float    # realised PnL, quote currency
    n: int


def session_bars(trips: list[RoundTrip]) -> list[Bar]:
    """Realised PnL per pre-registered session.

    The same four sessions the detector tests, in the same order, so the
    picture and the finding cannot disagree about where a session begins. The
    label is written with colons (`00:00-06:00`) rather than as `00-06`
    because redaction protects clock times and would otherwise eat the second
    half of a hyphenated pair.
    """
    out: list[Bar] = []
    for name, hours in SESSIONS.items():
        inside = [t for t in trips if t.hour_utc in hours]
        out.append(Bar(
            label=f"{hours.start:02d}:00–{hours.stop:02d}:00",
            value=sum(t.net_pnl for t in inside),
            n=len(inside),
        ))
    return out


def leverage_bars(trips: list[RoundTrip]) -> list[Bar]:
    """Realised PnL split at the same threshold the leverage detector uses.

    Three buckets, not two: trades with no leverage recorded at all are their
    own bar rather than being folded into the low arm. `None` and 1.0 are
    different claims -- `events.py` is explicit about that -- and a spot book
    silently counted as "low leverage" would make the comparison read as
    evidence about leverage when it is evidence about venue.
    """
    spot = [t for t in trips if t.max_leverage is None]
    low = [t for t in trips if t.max_leverage is not None and t.max_leverage < HIGH_LEVERAGE]
    high = [t for t in trips if (t.max_leverage or 0) >= HIGH_LEVERAGE]
    buckets = [
        (f"under {HIGH_LEVERAGE:.0f}x", low),
        (f"{HIGH_LEVERAGE:.0f}x and above", high),
        ("no leverage recorded", spot),
    ]
    return [Bar(label=label, value=sum(t.net_pnl for t in ts), n=len(ts))
            for label, ts in buckets if ts]


def bar_chart(bars: list[Bar], *, fmt: _Fmt, caption: str,
              width: int = 660, height: int = 250) -> str:
    """A bar chart, in SVG, by hand.

    Baseline sits wherever zero falls in the actual range rather than in the
    middle, so an all-negative book does not get half a chart of empty space
    above a floating row of bars.

    Under redaction the bars stay and the value labels go. The *shape* of the
    chart is a set of ratios between one trader's own sessions, which is the
    part worth sharing; the axis figures are the balance, which is not.
    """
    if not bars:
        return ""
    pad_x, pad_top, pad_bottom = 10, 28, 52
    plot_h = height - pad_top - pad_bottom
    values = [b.value for b in bars]
    hi, lo = max(0.0, max(values)), min(0.0, min(values))
    span = (hi - lo) or 1.0

    def y_of(v: float) -> float:
        return pad_top + plot_h * (hi - v) / span

    zero_y = y_of(0.0)
    slot = (width - 2 * pad_x) / len(bars)
    bar_w = min(slot * 0.55, 90.0)

    parts: list[str] = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{_esc(caption)}" preserveAspectRatio="xMidYMid meet">',
        f"<desc>{_esc(caption)}</desc>",
        f'<line class="zero" x1="{pad_x}" y1="{zero_y:.1f}" '
        f'x2="{width - pad_x}" y2="{zero_y:.1f}" />',
    ]
    for i, bar in enumerate(bars):
        x = pad_x + i * slot + (slot - bar_w) / 2
        y = y_of(bar.value)
        top, bottom = min(y, zero_y), max(y, zero_y)
        h = max(bottom - top, 1.0)
        cls = "pos" if bar.value >= 0 else "neg"
        mid = x + bar_w / 2
        parts.append(
            f'<rect class="bar {cls}" x="{x:.1f}" y="{top:.1f}" '
            f'width="{bar_w:.1f}" height="{h:.1f}" rx="3" />'
        )
        label_y = top - 8 if bar.value >= 0 else bottom + 16
        parts.append(
            f'<text class="val" x="{mid:.1f}" y="{label_y:.1f}" text-anchor="middle">'
            f"{_esc(fmt.money(bar.value))}</text>"
        )
        parts.append(
            f'<text class="axis" x="{mid:.1f}" y="{height - pad_bottom + 22:.1f}" '
            f'text-anchor="middle">{_esc(bar.label)}</text>'
        )
        parts.append(
            f'<text class="axis dim" x="{mid:.1f}" y="{height - pad_bottom + 38:.1f}" '
            f'text-anchor="middle">{bar.n} trades</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------
# Stylesheet
# --------------------------------------------------------------------------

# One palette, defined light and redefined under `prefers-color-scheme: dark`.
# System font stack only -- a webfont would be a network request, which is the
# one thing this document promises never to make.
CSS = """
:root {
  color-scheme: light dark;
  --bg: #fbfaf8; --panel: #ffffff; --ink: #14110d; --dim: #6b6357;
  --line: #e3ddd3; --pos: #1f7a4d; --neg: #b3261e; --accent: #8a6d3b;
  --chip: #f0ece4;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12110f; --panel: #1a1815; --ink: #ece7df; --dim: #9d958a;
    --line: #2e2a25; --pos: #58c893; --neg: #f08b84; --accent: #d9b779;
    --chip: #232019;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2.2rem 1.2rem 4rem; background: var(--bg); color: var(--ink);
  font: 15px/1.6 ui-sans-serif, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
main { max-width: 46rem; margin: 0 auto; }
h1 { font-size: 1.5rem; letter-spacing: .16em; margin: 0; text-transform: uppercase; }
h2 { font-size: .78rem; letter-spacing: .18em; text-transform: uppercase;
     color: var(--dim); margin: 2.6rem 0 .9rem; font-weight: 600; }
h3 { font-size: 1.02rem; margin: 0 0 .35rem; }
p { margin: .45rem 0; }
.greek { color: var(--accent); font-size: .9rem; letter-spacing: .1em; }
.meta { color: var(--dim); font-size: .82rem; margin-top: .5rem; }
.rule { border: 0; border-top: 1px solid var(--line); margin: 1.6rem 0 0; }
.tiles { display: flex; flex-wrap: wrap; gap: .5rem; margin: 1.1rem 0 0; }
.tile { background: var(--panel); border: 1px solid var(--line); border-radius: 9px;
        padding: .6rem .85rem; min-width: 8.2rem; flex: 1 1 8.2rem; }
.tile .k { display: block; color: var(--dim); font-size: .7rem;
           letter-spacing: .12em; text-transform: uppercase; }
.tile .v { display: block; font-size: 1.16rem; font-variant-numeric: tabular-nums;
           margin-top: .15rem; }
.card { background: var(--panel); border: 1px solid var(--line); border-left: 3px solid var(--line);
        border-radius: 9px; padding: .95rem 1.1rem; margin: .7rem 0; }
.card.worst { border-left-color: var(--neg); }
.card.good { border-left-color: var(--pos); }
.cost { font-variant-numeric: tabular-nums; font-weight: 600; }
.cost.neg { color: var(--neg); } .cost.pos { color: var(--pos); }
.chip { display: inline-block; background: var(--chip); color: var(--dim);
        border-radius: 999px; padding: .08rem .55rem; font-size: .7rem;
        letter-spacing: .08em; text-transform: uppercase; margin-left: .4rem;
        vertical-align: .12em; }
dl.detail { display: grid; grid-template-columns: max-content 1fr; gap: .18rem .9rem;
            margin: .7rem 0 0; font-size: .84rem; }
dl.detail dt { color: var(--dim); } dl.detail dd { margin: 0;
            font-variant-numeric: tabular-nums; }
details { margin-top: .7rem; font-size: .84rem; }
summary { cursor: pointer; color: var(--dim); }
.ids { margin-top: .5rem; line-height: 2; }
code { background: var(--chip); border-radius: 4px; padding: .08rem .35rem;
       font: .78rem/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.chart { width: 100%; height: auto; display: block; margin: .4rem 0 .2rem; }
.chart .zero { stroke: var(--line); stroke-width: 1; }
.chart .bar.pos { fill: var(--pos); } .chart .bar.neg { fill: var(--neg); }
.chart .val { fill: var(--ink); font-size: 12px; font-variant-numeric: tabular-nums; }
.chart .axis { fill: var(--dim); font-size: 11px; }
.chart .axis.dim { fill: var(--dim); font-size: 10px; opacity: .75; }
.caveat { color: var(--dim); font-size: .84rem; border-left: 2px solid var(--line);
          padding-left: .8rem; margin: .8rem 0 0; }
footer { color: var(--dim); font-size: .78rem; margin-top: 2.6rem;
         border-top: 1px solid var(--line); padding-top: .9rem; }
"""


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------

def _tiles(profile: Profile, fmt: _Fmt) -> str:
    s = profile.summary
    tiles = [
        ("trades", f"{s.n_trades} trades"),
        ("span", f"{s.span_days:.0f} days"),
        ("symbols", f"{len(s.symbols)} symbols"),
        ("win rate", fmt.pct(s.win_rate)),
        ("net realised", fmt.money(s.total_pnl)),
        ("fees", fmt.money(-abs(s.total_fees))),
        ("expectancy", f"{fmt.money(s.expectancy)} / trade"),
    ]
    cells = "".join(
        f'<div class="tile"><span class="k">{_esc(k)}</span>'
        f'<span class="v">{_esc(v)}</span></div>'
        for k, v in tiles
    )
    return f'<div class="tiles">{cells}</div>'


def _receipts(ids: list[str]) -> str:
    """The trade ids behind a finding, folded away behind a native disclosure.

    Kept even under redaction. An id is an identifier, not an amount -- it says
    nothing about the size of a book to anyone who cannot already read it --
    and it is the entire reason a reader is entitled to check a finding rather
    than believe it.
    """
    if not ids:
        return ""
    body = " ".join(f"<code>{_esc(i)}</code>" for i in ids)
    return (
        f"<details><summary>{len(ids)} trade receipts</summary>"
        f'<div class="ids">{body}</div></details>'
    )


def _detail(detail: dict, fmt: _Fmt) -> str:
    """The detector's own working, as a definition list.

    Every value goes through `redact()` when redaction is on, which is
    deliberately over-eager: a ratio or a count here loses its digits along
    with the amounts. The counts that matter survive in the finding sentence
    above, where a unit word protects them, and the alternative -- deciding
    per key whether a number is money -- is a rule that silently stops being
    right the next time a detector adds a field.
    """
    if not detail:
        return ""
    rows = "".join(
        f"<dt>{_esc(k.replace('_', ' '))}</dt><dd>{_esc(fmt.prose(_scalar(v)))}</dd>"
        for k, v in detail.items()
    )
    return f'<dl class="detail">{rows}</dl>'


def _scalar(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _leak_card(leak, fmt: _Fmt, *, worst: bool = False) -> str:
    cost = ""
    if leak.cost is not None:
        cls = "neg" if leak.cost < 0 else "pos"
        cost = f'<span class="cost {cls}">{_esc(fmt.money(leak.cost))}</span> '
    chip = f'<span class="chip">{_esc(leak.confidence)}</span>'
    return (
        f'<section class="card{" worst" if worst else ""}">'
        f"<h3>{cost}{_esc(leak.title)}{chip}</h3>"
        f"<p>{_esc(fmt.prose(leak.finding))}</p>"
        f"{_detail(leak.detail, fmt)}"
        f"{_receipts(leak.trade_ids)}"
        f"</section>"
    )


def _counterfactuals(profile: Profile, fmt: _Fmt) -> str:
    out: list[str] = []
    if profile.counterfactuals:
        out.append("<h2>The universes where you are richer</h2>")
        for cf in profile.counterfactuals:
            out.append(
                f'<section class="card good"><h3>'
                f'<span class="cost pos">{_esc(fmt.money(cf.delta))}</span> '
                f"if you had {_esc(fmt.prose(cf.description))}</h3>"
                f"<p>Removes {cf.n_removed} trades and re-totals.</p>"
                f"{_receipts(cf.trade_ids)}</section>"
            )
        out.append(
            '<p class="caveat">Each of these removes the matching trades and re-totals. '
            "It assumes every remaining trade is unchanged, which is an assumption and "
            "not a fact — a trader who stops trading in the small hours may simply "
            "trade more at noon, and this cannot see that. These are descriptions of "
            "the past, not forecasts. "
            "A counterfactual is only shown here at all because a detector already "
            "proved a significant leak in that same slice; slice any history four ways "
            "and one slice always looks worth removing.</p>"
        )
    if profile.observations:
        out.append("<h2>Observations, not recommendations</h2>")
        for cf in profile.observations:
            tag = ' <span class="chip">hindsight</span>' if cf.hindsight else ""
            out.append(
                f'<section class="card"><h3>'
                f'<span class="cost">{_esc(fmt.money(cf.delta))}</span> '
                f"if you had {_esc(fmt.prose(cf.description))}{tag}</h3>"
                f"<p>Removes {cf.n_removed} trades and re-totals.</p></section>"
            )
        out.append(
            '<p class="caveat">No detector proved a leak in these slices, so none of '
            "them is a recommendation. The ones marked <em>hindsight</em> are worse "
            "than unproven: the rule was chosen by looking at the very outcome then "
            "quoted as evidence for it, so it would look profitable on a history "
            "generated from pure noise.</p>"
        )
    return "".join(out)


# --------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------

def render_html(
    profile: Profile,
    *,
    redacted: bool = False,
    generated_at: datetime | None = None,
    title: str = "Rekt Wrapped",
) -> str:
    """The whole profile as one standalone HTML document.

    `generated_at` is a parameter rather than a call to the clock so that the
    same profile renders to the same bytes twice — a report that differs from
    its predecessor only in a timestamp is a report nobody can diff. Pass one
    to stamp it.

    Under redaction the timestamp is dropped entirely. A deny-by-default
    currency filter cannot tell a year from an amount, and a document that
    argues with its own redactor about whether `2026` is money is worse than
    one without a date on it.
    """
    fmt = _Fmt(redacted=redacted)
    s = profile.summary
    source = _esc(fmt.prose(s.source))
    body: list[str] = []

    body.append(
        f"<header><h1>{_esc(title)}</h1>"
        f'<div class="greek">γνῶθι σεαυτόν — know thyself</div>'
        f'<p class="meta">{source}'
        + (
            ""
            if redacted or generated_at is None
            else f" · generated {_esc(generated_at.strftime('%Y-%m-%d %H:%M UTC'))}"
        )
        + (' · <span class="chip">redacted</span>' if redacted else "")
        + "</p><hr class=\"rule\" /></header>"
    )

    if s.is_thin:
        # The whole report, when there is not enough history to have one. Saying
        # nothing loudly is what earns the right to be believed elsewhere.
        body.append(
            "<p>"
            + _esc(fmt.prose(
                f"Only {s.n_trades} closed trades across {s.span_days:.0f} days. "
                f"That is not enough to tell you anything you should act on, so "
                f"Gnosis is not going to invent something."
            ))
            + '</p><p class="caveat">'
            + _esc(fmt.prose(
                "Gnosis needs at least 30 closed trades over at least 14 days before "
                "it will profile a book, and at least 8 observations in each arm of "
                "any comparison. This is a real result, not a failure to look."
            ))
            + "</p>"
        )
        return _document(body, title=title, source=s.source, redacted=redacted)

    body.append(_tiles(profile, fmt))

    worst = profile.worst_leak
    if worst is not None:
        body.append("<h2>Your most expensive habit</h2>")
        body.append(_leak_card(worst, fmt, worst=True))

    others = [leak for leak in profile.leaks if leak is not worst]
    if others:
        body.append("<h2>Also</h2>")
        body.extend(_leak_card(leak, fmt) for leak in others)

    if not profile.leaks:
        body.append("<h2>Findings</h2>")
        body.append(
            "<p>No behavioural leak cleared the significance bar. That is a real "
            "result, not a failure to look — the detectors ran and found nothing "
            "that survives a confidence interval.</p>"
        )

    if profile.strengths:
        body.append("<h2>What you actually do well</h2>")
        for st in profile.strengths:
            body.append(
                f'<section class="card good"><h3>{_esc(st.title)}</h3>'
                f"<p>{_esc(fmt.prose(st.finding))}</p>"
                f"{_detail(st.detail, fmt)}{_receipts(st.trade_ids)}</section>"
            )

    body.append("<h2>Where the money went</h2>")
    body.append(bar_chart(
        session_bars(profile.trips), fmt=fmt,
        caption="Realised PnL by session, UTC. Bars below the line lost money.",
    ))
    body.append(
        '<p class="caveat">Realised PnL by session (UTC). These four blocks are fixed '
        "in advance and are the same ones the session detector tests, so the picture "
        "and the finding cannot disagree about where a session begins. A bar being "
        "negative is not by itself a finding — only a difference that survives a "
        "confidence interval appears above.</p>"
    )
    body.append(bar_chart(
        leverage_bars(profile.trips), fmt=fmt,
        caption=f"Realised PnL split at {HIGH_LEVERAGE:.0f}x leverage.",
    ))
    body.append(
        f'<p class="caveat">Realised PnL split at {HIGH_LEVERAGE:.0f}x, the same '
        "threshold the leverage detector uses. Absolute PnL here is the honest thing "
        "to plot and the wrong thing to conclude from: leveraged positions are larger, "
        "so of course they move more money. The detector compares percentage return "
        "instead.</p>"
    )

    body.append(_counterfactuals(profile, fmt))

    n_receipts = sum(len(leak.trade_ids) for leak in profile.leaks)
    body.append(
        f"<footer>Every number above was computed and significance-tested before it "
        f"was rendered; nothing here does arithmetic of its own. {n_receipts} trade "
        f"receipts back the findings. Gnosis is an informational tool: it is not "
        f"investment advice, it does not predict returns, and its counterfactuals "
        f"describe the past rather than forecasting the future."
        + ("" if not redacted else " Absolute currency amounts have been removed.")
        + "</footer>"
    )
    return _document(body, title=title, source=s.source, redacted=redacted)


def _document(body: list[str], *, title: str, source: str, redacted: bool) -> str:
    """Wrap the sections in a complete, standalone document.

    `<meta name="color-scheme">` alongside the CSS property so the browser
    paints the right canvas colour before the stylesheet is applied — without
    it a dark-mode reader gets a white flash on open.
    """
    heading = f"{title} · {redact(source) if redacted else source}"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        '<meta name="color-scheme" content="light dark" />\n'
        f"<title>Gnosis — {_esc(heading)}</title>\n"
        f"<style>{CSS}</style>\n"
        "</head>\n<body>\n<main>\n"
        + "\n".join(body)
        + "\n</main>\n</body>\n</html>\n"
    )
