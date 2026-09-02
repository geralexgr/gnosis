"""Rekt Wrapped: the shareable card.

Rendering is deliberately dumb. Every number on the card was computed and
significance-tested upstream; this module chooses layout and nothing else. It
must never compute a statistic, because a renderer that does arithmetic is a
renderer that can disagree with the profile it is rendering.

The tone is blunt on purpose. A gentle report of a behavioural leak gets read
once and filed; the whole reason the roast framing exists is that people
actually look at it, and share it, which is the only way the underlying
analysis reaches anyone.

Colour degrades to plain text when stdout is not a terminal, so piping to a
file produces something you can paste.
"""

from __future__ import annotations

import os
import sys

from .model.profile import Profile

WIDTH = 68


class _C:
    """ANSI codes, blanked when the output is not a terminal."""

    def __init__(self, on: bool) -> None:
        self.dim = "\033[2m" if on else ""
        self.bold = "\033[1m" if on else ""
        self.red = "\033[31m" if on else ""
        self.green = "\033[32m" if on else ""
        self.yellow = "\033[33m" if on else ""
        self.cyan = "\033[36m" if on else ""
        self.off = "\033[0m" if on else ""


def _use_colour() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _rule(c: _C, char: str = "─") -> str:
    return f"{c.dim}{char * WIDTH}{c.off}"


def _wrap(text: str, indent: str = "  ") -> list[str]:
    """Naive greedy wrap. No dependency, and the text is short."""
    words, lines, cur = text.split(), [], indent
    for w in words:
        if len(cur) + len(w) + 1 > WIDTH:
            lines.append(cur.rstrip())
            cur = indent
        cur += w + " "
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def render_narrated(
    profile: Profile, headline: str, body: str, *, colour: bool | None = None
) -> str:
    """Render the card with model-written copy in place of the templated prose.

    The narrated text replaces only the headline and the description of the
    worst habit. Everything else on the card -- the counts, the totals, the
    counterfactual figures, the receipt line -- is rendered from the profile
    exactly as `render` does, because those are computed values and there is no
    version of "let the model phrase it" that should be allowed to touch them.

    The caller is responsible for having fact-checked the text first; see
    `agents.narrate`. This function does no validation of its own, and is
    deliberately not exported as a way to put arbitrary prose on a card.
    """
    c = _C(_use_colour() if colour is None else colour)
    full = render(profile, colour=colour)

    if profile.summary.is_thin or profile.worst_leak is None:
        # Nothing to substitute into: a thin profile has no habit section, and
        # inventing one is exactly what this project exists not to do.
        return full

    lines = full.split("\n")
    try:
        start = next(i for i, ln in enumerate(lines) if "YOUR MOST EXPENSIVE HABIT" in ln)
    except StopIteration:
        return full
    # The section runs to the next rule, whatever the renderer put in between.
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith(f"{c.dim}─")),
        len(lines),
    )

    replacement = ["", f"  {c.bold}{headline}{c.off}"]
    for para in body.split("\n"):
        if para.strip():
            replacement += _wrap(para.strip())
    replacement.append("")
    return "\n".join(lines[: start + 1] + replacement + lines[end:])


def render(profile: Profile, *, colour: bool | None = None) -> str:
    c = _C(_use_colour() if colour is None else colour)
    s = profile.summary
    out: list[str] = []

    out.append("")
    out.append(f"  {c.bold}REKT WRAPPED{c.off}  {c.dim}· γνῶθι σεαυτόν ·{c.off}")
    out.append(_rule(c, "━"))

    if s.is_thin:
        out.append("")
        out += _wrap(
            f"Only {s.n_trades} closed trades across {s.span_days:.0f} days. "
            f"That is not enough to tell you anything you should act on, so "
            f"Gnosis is not going to invent something."
        )
        out.append("")
        out.append(_rule(c, "━"))
        return "\n".join(out)

    pnl_c = c.green if s.total_pnl >= 0 else c.red
    out.append("")
    out.append(
        f"  {s.n_trades} trades · {s.span_days:.0f} days · "
        f"{len(s.symbols)} symbols · {s.win_rate:.0%} win rate"
    )
    out.append(
        f"  net {pnl_c}{s.total_pnl:+,.0f}{c.off}   "
        f"fees {c.red}{-abs(s.total_fees):+,.0f}{c.off}   "
        f"expectancy {s.expectancy:+,.0f}/trade"
    )
    out.append("")

    worst = profile.worst_leak
    if worst is not None:
        out.append(_rule(c))
        out.append(f"  {c.bold}{c.red}YOUR MOST EXPENSIVE HABIT{c.off}")
        out.append("")
        out.append(f"  {c.bold}{worst.title}{c.off}")
        out += _wrap(worst.finding)
        out.append("")

    others = [leak for leak in profile.leaks if leak is not worst]
    if others:
        out.append(_rule(c))
        out.append(f"  {c.bold}ALSO{c.off}")
        out.append("")
        for leak in others:
            cost = f"{c.red}{leak.cost:+,.0f}{c.off}  " if leak.costs_money else ""
            out.append(f"  {cost}{c.bold}{leak.title}{c.off}")
            out += _wrap(leak.finding, indent="    ")
            out.append("")

    if profile.strengths:
        out.append(_rule(c))
        out.append(f"  {c.bold}{c.green}WHAT YOU ACTUALLY DO WELL{c.off}")
        out.append("")
        for st in profile.strengths:
            out.append(f"  {c.bold}{st.title}{c.off}")
            out += _wrap(st.finding, indent="    ")
        out.append("")

    if profile.counterfactuals:
        out.append(_rule(c))
        out.append(f"  {c.bold}THE UNIVERSES WHERE YOU ARE RICHER{c.off}")
        out.append("")
        for cf in profile.counterfactuals[:4]:
            out.append(
                f"  {c.green}{cf.delta:+9,.0f}{c.off}  if you had {cf.description}"
            )
        out.append("")
        cb = profile.combined
        if cb is not None and cb.has_overlap:
            # These do not add up, and left alone they look as though they do.
            out.append(
                f"  {c.bold}{cb.delta:+9,.0f}{c.off}  all of the above, together"
            )
            out += _wrap(
                f"Not the sum of the lines above. {cb.n_overlapping} of the "
                f"{cb.n_removed} trades removed are claimed by more than one "
                f"rule — the same trade is often late, over-leveraged and in "
                f"the wrong symbol at once.",
                indent="  ",
            )
            out.append("")
        out.append(
            f"  {c.dim}Removes those trades and re-totals. Assumes the rest{c.off}"
        )
        out.append(f"  {c.dim}are unchanged, which is an assumption, not a fact.{c.off}")
        out.append("")

    if not profile.leaks:
        out.append(_rule(c))
        out.append("")
        out += _wrap(
            "No behavioural leak cleared the significance bar. That is a real "
            "result, not a failure to look — the detectors ran and found "
            "nothing that survives a confidence interval."
        )
        out.append("")

    out.append(_rule(c, "━"))
    n_receipts = sum(len(leak.trade_ids) for leak in profile.leaks)
    out.append(
        f"  {c.dim}every number above is computed, not generated · "
        f"{n_receipts} trade receipts{c.off}"
    )
    out.append("")
    return "\n".join(out)
