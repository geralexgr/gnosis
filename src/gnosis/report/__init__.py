"""Renderers that produce a file rather than a terminal.

`card.py` writes for a terminal, which is the right surface for the demo and
the wrong one for everything after it: a card cannot be emailed, cannot be kept
alongside a month of others, and loses its meaning the moment the scrollback
goes. This package is the durable form of the same profile.

The constraint that shapes everything here is **one file, no fetches**. A
report that pulls a stylesheet, a font, or a chart library from a CDN stops
working on a plane, stops working behind a corporate proxy, stops working in an
email client, and stops working entirely the day that CDN changes a path. It
also quietly tells whoever hosts the asset every time someone opens the
document, which is not a property a trading record should have. So the CSS is
inline, the charts are hand-rolled SVG, and there is no JavaScript at all.

Nothing here recomputes a statistic, for the same reason `card.py` does not: a
renderer that does arithmetic is a renderer that can disagree with the profile
it is rendering.
"""

from __future__ import annotations

from .html import currency_amounts, redact, render_html

__all__ = ["currency_amounts", "redact", "render_html"]
