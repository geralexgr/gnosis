"""Command line: card, profile, check.

The deterministic layer is the whole CLI. Nothing here needs credentials, a
model, or a network, which is what makes the demo reproducible from a fresh
clone — a judge can run every command in the README without a Binance account.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .card import render, render_narrated
from .gate.elenchos import ProposedTrade, check as gate_check
from .ingest.synthetic import LEAKS, TraderSpec, generate
from .model import profile as profile_mod
from .model.events import History


def _load(path: str) -> History:
    """Load a history from a CSV export, the on-chain wallet, or a fixture.

    `synthetic:<leak>[:seed]` builds a labelled trader on the spot, which is
    what makes every command in the README runnable with no account.
    """
    if path.startswith("synthetic"):
        parts = path.split(":")
        leak = parts[1] if len(parts) > 1 and parts[1] else None
        seed = int(parts[2]) if len(parts) > 2 else 31
        if leak and leak not in LEAKS:
            raise SystemExit(f"unknown leak {leak!r}; choose from {', '.join(LEAKS)}")
        return generate(TraderSpec(seed=seed, leak=leak))
    if path.endswith((".xlsx", ".xls")):
        raise SystemExit(
            f"{path} is a spreadsheet, not a CSV.\n"
            f"Open it and 'Save As' CSV, then try again."
        )
    if path == "baw" or path.startswith("baw:"):
        from .ingest.baw_onchain import load as baw_load
        return baw_load()
    if path.endswith(".csv"):
        from .ingest.binance_csv import CsvFormatError, parse_csv
        try:
            return parse_csv(path)
        except FileNotFoundError:
            raise SystemExit(f"no such file: {path}") from None
        except CsvFormatError as exc:
            # The parser's message is the most useful text in the project and
            # it used to be delivered as an eleven-frame traceback. Point at
            # the diagnostic, which answers this exact question completely.
            raise SystemExit(
                f"{exc}\n\n"
                f"To see exactly what Gnosis makes of your file — encoding, "
                f"delimiter,\nheader, and which rows failed and why:\n\n"
                f"    python3 scripts/inspect_csv.py {path}"
            ) from None
    raise SystemExit(
        f"cannot load {path!r}.\n"
        f"  a Binance CSV export:  path/to/export.csv\n"
        f"  the on-chain wallet:   baw    (needs `baw` installed and signed in)\n"
        f"  a labelled fixture:    synthetic:night:42"
    )


def cmd_card(args) -> int:
    prof = profile_mod.for_history(_load(args.source))
    if not args.narrate:
        print(render(prof))
        return 0
    # Narration is opt-in and never the default. Two reasons: the demo and the
    # test suite must stay deterministic, and a card that silently changes
    # wording between runs is a card whose numbers people stop trusting.
    from .agents import narrate_profile
    n = narrate_profile(prof, voice=args.voice)
    print(render_narrated(prof, n.headline, n.body))
    if n.source == "template":
        print(f"  (deterministic template — {n.fallback_reason})\n")
    return 0


def cmd_profile(args) -> int:
    prof = profile_mod.for_history(_load(args.source))
    print(json.dumps(prof.to_dict(), indent=2, default=str))
    return 0


def cmd_check(args) -> int:
    prof = profile_mod.for_history(_load(args.source))
    ts = (
        datetime.fromisoformat(args.at).replace(tzinfo=timezone.utc)
        if args.at
        else datetime.now(timezone.utc)
    )
    proposed = ProposedTrade(
        symbol=args.symbol, side=args.side, notional=args.notional, ts=ts,
        leverage=args.leverage,
        adding_to_losing_position=args.adding_to_loser,
        minutes_since_last_loss=args.since_loss,
    )
    j = gate_check(prof, proposed)
    mark = {"skip": "✗", "caution": "!", "proceed": "·", "favourable": "✓"}[j.verdict]
    print()
    print(f"  {mark} {j.verdict.upper()}  {j.headline}")
    print()
    for r in j.reasons:
        print(f"    · {r}")
    if j.suggested_notional is not None:
        print()
        print(f"    Your record suggests {j.suggested_notional:,.0f}, "
              f"not {args.notional:,.0f}.")
    if args.narrate:
        from .agents import narrate_judgement
        vn = narrate_judgement(
            j, symbol=args.symbol, proposed_notional=args.notional, voice=args.voice
        )
        # The verdict is an input to narration, never an output of it. If these
        # ever disagree, the narration layer has a bug and the arithmetic wins.
        assert vn.verdict == j.verdict, "narration must not change the verdict"
        if vn.source == "model":
            print()
            print(f"    {vn.explanation}")
        else:
            # The fallback explanation is assembled from the same reasons that
            # were just printed above, so repeating it adds nothing but noise.
            # Say why there is no narration and stop.
            print()
            print(f"    (no narration — {vn.fallback_reason})")
        print()
    print("    Elenchos never blocks. This is your own history, quoted back.")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="gnosis", description="Know thyself. Your fills already do."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # No default. A tool whose entire claim is "we never invent a finding"
    # must not invent an entire trader when invoked with no arguments, which
    # is exactly what a default of `synthetic:disposition` did -- it printed a
    # confident, personal-looking profile of somebody who does not exist.
    src = {"help": (
        "where the history comes from: a Binance CSV export (my-export.csv), "
        "the demo book (corpus/demo-account.csv), the on-chain wallet (baw), "
        "or a labelled fixture (synthetic:night)"
    )}

    def add_narration_flags(parser):
        parser.add_argument(
            "--narrate", action="store_true",
            help="let a model phrase the output (needs ANTHROPIC_API_KEY). "
                 "Every number it writes is checked against the computed facts; "
                 "on any mismatch the deterministic template is used instead.",
        )
        parser.add_argument(
            "--voice", default=None, choices=["blunt", "plain"],
            help="tone for --narrate: 'blunt' is the roast voice used on the "
                 "card, 'plain' is neutral. Ignored without --narrate.",
        )

    c = sub.add_parser("card", help="render the Rekt Wrapped card")
    c.add_argument("source", **src)
    add_narration_flags(c)
    c.set_defaults(fn=cmd_card, default_voice="blunt")

    pr = sub.add_parser("profile", help="emit profile.json")
    pr.add_argument("source", **src)
    pr.set_defaults(fn=cmd_profile)

    ck = sub.add_parser("check", help="cross-examine a proposed trade")
    ck.add_argument("source", **src)
    ck.add_argument("--symbol", default="ETHUSDT")
    ck.add_argument("--side", default="buy", choices=["buy", "sell"])
    ck.add_argument("--notional", type=float, default=4000.0)
    ck.add_argument("--leverage", type=float, default=None)
    ck.add_argument("--at", default=None, help="ISO time, UTC (e.g. 2026-09-01T03:14)")
    ck.add_argument("--adding-to-loser", action="store_true")
    ck.add_argument("--since-loss", type=float, default=None,
                    help="minutes since the last realised loss")
    add_narration_flags(ck)
    ck.set_defaults(fn=cmd_check, default_voice="plain")

    args = p.parse_args(argv)
    if getattr(args, "voice", None) is None:
        args.voice = getattr(args, "default_voice", "blunt")
    return args.fn(args)


def _run() -> int:
    """Entry point with a friendlier message for the usual first failure.

    Gnosis is deliberately not installed -- it has no dependencies and running
    it from the source tree is the point. The cost of that choice is that the
    first thing most people see is a bare `ModuleNotFoundError` naming a module
    they have never heard of, with nothing to suggest the fix.
    """
    try:
        return main()
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] == "gnosis":
            raise SystemExit(
                "Could not import gnosis.\n\n"
                "Gnosis has no install step by design. Run it from the repo "
                "root with:\n\n"
                "    export PYTHONPATH=src\n"
                "    python3 -m gnosis.cli card corpus/demo-account.csv\n"
            ) from None
        raise


if __name__ == "__main__":
    sys.exit(_run())
