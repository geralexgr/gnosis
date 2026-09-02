#!/usr/bin/env python3
"""Render a trading history to a single self-contained HTML report.

    python3 scripts/make_report.py corpus/demo-account.csv --out report.html
    python3 scripts/make_report.py synthetic:night --redact --out share.html
    python3 scripts/make_report.py synthetic:night            # to stdout

The output is one file with no external references at all — no stylesheet, no
font, no image, no script, no link out. It opens from disk on a machine with no
network, survives being attached to an email, and will render the same way in
six months.

Two differences from `scripts/publish_card.py`, both deliberate:

**Redaction is off by default here, and on by default there.** That script
publishes to a social network, where the default has to be the safe one because
the mistake is irreversible. This one writes a file to the user's own disk,
where their own balance is not a disclosure. `--redact` is the flag for when
the file is going to leave — it keeps every percentage, ratio and count and
removes absolute currency amounts, by exactly the same rule.

**It will overwrite `--out` without asking.** Regenerating a report you already
have is the normal case, and a prompt on every run would be answered `y` by
reflex until the day it mattered. Nothing else on disk is touched: the history
source is opened for reading and never written.

No dependencies, no network, no credentials.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gnosis.cli import _load as load_history  # noqa: E402
from gnosis.model import profile as profile_mod  # noqa: E402
from gnosis.report.html import render_html  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="make_report.py",
        description="Render a Gnosis profile as one self-contained HTML file.",
    )
    parser.add_argument(
        "source", nargs="?", default="synthetic:disposition",
        help="history source: a Binance CSV export, 'baw', or synthetic:<leak>[:seed]",
    )
    parser.add_argument(
        "--out", default=None,
        help="file to write. Omit to print the document to stdout.",
    )
    parser.add_argument(
        "--redact", action="store_true",
        help="remove absolute currency amounts, keeping percentages, ratios and "
             "counts. Use this for a report that is going to be shared: the "
             "psychology is the shareable part, the balance is not.",
    )
    parser.add_argument(
        "--title", default="Rekt Wrapped",
        help="heading and browser title for the document",
    )
    parser.add_argument(
        "--no-timestamp", action="store_true",
        help="omit the generation time, so the same history renders to identical "
             "bytes twice and two reports can be diffed",
    )
    args = parser.parse_args(argv)

    profile = profile_mod.for_history(load_history(args.source))
    html = render_html(
        profile,
        redacted=args.redact,
        generated_at=None if args.no_timestamp else datetime.now(timezone.utc),
        title=args.title,
    )

    if args.out is None:
        sys.stdout.write(html)
        return 0

    path = Path(args.out)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    # To stderr, so `--out /dev/stdout` still produces a clean document.
    sys.stderr.write(
        f"wrote {path} ({len(html.encode('utf-8')):,} bytes"
        f"{', redacted' if args.redact else ''})\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
