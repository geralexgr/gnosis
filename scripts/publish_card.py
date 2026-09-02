#!/usr/bin/env python3
"""Publish a Rekt Wrapped card to Binance Square, via the `square-post` skill.

Two things about this script are unusual, and both are the point.

**It will not post unless you say `--yes`.** The default is a dry run that
prints the exact bytes that would be sent and the exact command that would send
them, and then exits without touching a network. Publishing to a social network
is irreversible in the way that matters — a delete does not un-see a post — and
what is being published here is a person's trading record. A tool that posts on
its first invocation because a flag defaulted the wrong way is a tool that will
eventually post someone's drawdown while they were still reading the help text.
`square-post` itself offers no confirmation step, so this is where one goes.

**It redacts money by default.** A Rekt Wrapped card carries realised PnL, fee
totals, per-trade expectancy and the symbols traded. The *psychology* is the
shareable part — "42% win rate", "65 trades in the small hours", "you hold
losers 4x longer than winners" is a card people post about themselves. The
balance is not: it is the trader's net worth, in public, permanently, indexed.
So `--redact` is on by default and removes absolute currency amounts while
keeping every percentage, ratio and count, and `--show-amounts` is the explicit
flag that puts the money back.

Redaction is **deny-by-default**: a number survives only if it is followed by
something that identifies it as a percentage (`42%`), a ratio (`10x`, `1.9:1`),
a clock time (`03:14`) or a counted unit (`257 trades`, `365 days`). Anything
else becomes an em dash. That is deliberately over-eager — it will occasionally
redact something harmless — because the two errors are not symmetric. Redacting
a count nobody minded is a cosmetic annoyance; missing one currency figure
publishes the number the user was trying not to publish.

Authentication follows the skill exactly: `BINANCE_SQUARE_OPENAPI_KEY` from the
environment, then `~/.config/binance-square/openapi-key`. The key is never
passed as a command-line argument — CLI args are visible in `ps` output and
shell history — and never printed in full; only the first five and last four
characters, which is the masking the skill's own `lib.mjs` uses.

    python3 scripts/publish_card.py synthetic:night            # dry run
    python3 scripts/publish_card.py synthetic:night --yes      # posts, redacted
    python3 scripts/publish_card.py my-export.csv --show-amounts --yes

No dependencies. `node` and a checkout of the `square-post` skill are needed
only to actually post; the dry run needs neither.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gnosis.card import render  # noqa: E402
from gnosis.cli import _load as load_history  # noqa: E402
from gnosis.model import profile as profile_mod  # noqa: E402

CONFIG_PATH = Path.home() / ".config" / "binance-square" / "openapi-key"
KEY_ENV = "BINANCE_SQUARE_OPENAPI_KEY"
CREATOR_CENTER = "https://www.binance.com/square/creator-center/home"
SKILL_REPO = "https://github.com/binance/binance-skills-hub"
SKILL_SUBPATH = "skills/binance/square-post"
# `post-text.mjs --text <content> [--title <title>]`, per the skill's own docs.
POST_TEXT_SCRIPT = "scripts/post-text.mjs"
# Square rejects overlong bodies with code 20013. Short posts are the tighter
# limit; an article (`--title`) is allowed much more. The exact ceiling is not
# published, so this refuses rather than truncating -- a card cut off mid-number
# is worse than no card.
SHORT_POST_LIMIT = 1000

REDACTED = "—"

# argv -> (returncode, stdout, stderr). The seam, so the tests can prove the
# no-`--yes` guarantee without a network, a key, or node installed.
Runner = Callable[[Sequence[str], dict], "tuple[int, str, str]"]


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

# Units that make a number a count rather than an amount of money. Matched
# after optional whitespace, so "257 trades" and "20 trade receipts" both keep
# their number.
_KEEP_UNITS = (
    r"trades?|days?|weeks?|months?|years?|hours?|hrs?|minutes?|mins?|seconds?|secs?"
    r"|symbols?|receipts?|times?|positions?|tokens?|swaps?|fills?|orders?"
)
# A number, with an optional currency mark and an optional sign. The card
# formats money with `,.0f` and often with a leading `+`, but a positive mean
# renders bare -- so bare numbers must be caught too, which is why this is
# deny-by-default rather than a hunt for dollar signs.
_NUMBER = re.compile(r"(?:[$€£¥]\s?)?[-+−]?\d[\d,]*(?:\.\d+)?")
# Things a number may be followed by and still be kept.
_KEEP_AFTER = re.compile(rf"\s*(?:%|[xX]\b|{_KEEP_UNITS})", re.IGNORECASE)
# Clock times and ratios: 03:14, 00:00-06:00, 1.9:1. Protected wholesale before
# the number pass runs, because their halves look exactly like bare amounts.
_PROTECTED = re.compile(r"\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?")
# A sentinel with no digits in it -- an earlier version numbered its
# placeholders and the number pass promptly redacted the numbering, which is a
# good illustration of why this pass is tested rather than eyeballed.
_SENTINEL = "\x00"


def _stage(text: str) -> tuple[str, list[str]]:
    """Swap ratio/clock spans for a digit-free sentinel, in order."""
    kept: list[str] = []

    def protect(match: re.Match) -> str:
        kept.append(match.group(0))
        return _SENTINEL

    return _PROTECTED.sub(protect, text), kept


def redact(text: str) -> str:
    """Remove absolute currency amounts; keep percentages, ratios and counts.

    Deny-by-default, for the reason in the module docstring: over-redacting is
    cosmetic, under-redacting publishes the thing the user asked not to
    publish.
    """
    staged, kept = _stage(text)

    def scrub(match: re.Match) -> str:
        return match.group(0) if _KEEP_AFTER.match(staged[match.end():]) else REDACTED

    staged = _NUMBER.sub(scrub, staged)
    for original in kept:
        staged = staged.replace(_SENTINEL, original, 1)
    return staged


def currency_amounts(text: str) -> list[str]:
    """Every number in `text` that redaction would treat as money.

    The inverse of `redact`, kept beside it so a test can assert that a
    redacted card contains none — checking the property directly rather than
    checking that a particular known figure disappeared.
    """
    staged, _ = _stage(text)
    return [
        match.group(0)
        for match in _NUMBER.finditer(staged)
        if not _KEEP_AFTER.match(staged[match.end():])
    ]


# --------------------------------------------------------------------------
# Key handling
# --------------------------------------------------------------------------

def mask_key(key: str) -> str:
    """First five, last four — the same masking `square-post/lib.mjs` uses."""
    if not key:
        return ""
    if len(key) <= 9:
        return f"{key[:2]}..."
    return f"{key[:5]}...{key[-4:]}"


def resolve_key(env: dict | None = None) -> tuple[str | None, str]:
    """`(key, where_it_came_from)`. Env first, then the skill's config path.

    Returns `None` rather than raising, because the dry run is useful without a
    key and refusing to render a preview to someone who has not signed up yet
    would be obnoxious.
    """
    env = os.environ if env is None else env
    from_env = (env.get(KEY_ENV) or "").strip()
    if from_env:
        return from_env, f"${KEY_ENV}"
    try:
        if CONFIG_PATH.exists():
            saved = CONFIG_PATH.read_text(encoding="utf-8").strip()
            if saved:
                return saved, str(CONFIG_PATH)
    except OSError:
        pass
    return None, "not found"


def explain_missing_key() -> str:
    return (
        f"No Binance Square OpenAPI key found.\n"
        f"  looked at:  ${KEY_ENV}\n"
        f"              {CONFIG_PATH}\n"
        f"\n"
        f"  Create one in the Creator Center: {CREATOR_CENTER}\n"
        f"  Then either export it for this shell:\n"
        f"      export {KEY_ENV}=<your key>\n"
        f"  or save it once (0600, never as a CLI argument):\n"
        f"      {KEY_ENV}=<your key> node scripts/save-key.mjs\n"
        f"  from the square-post skill directory.\n"
    )


# --------------------------------------------------------------------------
# Composition and posting
# --------------------------------------------------------------------------

def build_post(source: str, *, redacted: bool = True, colour: bool = False) -> str:
    """Render the card for `source` and apply the privacy pass.

    `colour=False` always in practice: ANSI escapes are meaningless in a Square
    post and would be published literally.
    """
    profile = profile_mod.for_history(load_history(source))
    text = render(profile, colour=colour)
    return redact(text) if redacted else text


def default_runner(argv: Sequence[str], env: dict) -> tuple[int, str, str]:
    """Run the skill's node script. Only reached behind `--yes`."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, key only in env
        list(argv), capture_output=True, text=True, env=env, check=False
    )
    return result.returncode, result.stdout, result.stderr


def find_script(skill_dir: str | None) -> Path | None:
    """Locate `post-text.mjs`, or None with the caller explaining how to get it."""
    candidates = []
    if skill_dir:
        candidates.append(Path(skill_dir).expanduser())
    env_dir = os.environ.get("SQUARE_POST_SKILL_DIR")
    if env_dir:
        candidates.append(Path(env_dir).expanduser())
    candidates += [
        Path.home() / ".claude" / "skills" / "square-post",
        Path.home() / "skills" / "binance" / "square-post",
        Path.cwd() / "skills" / "binance" / "square-post",
    ]
    for base in candidates:
        script = base / POST_TEXT_SCRIPT
        if script.exists():
            return script
    return None


def publish(
    text: str,
    *,
    confirmed: bool,
    title: str | None = None,
    skill_dir: str | None = None,
    node: str = "node",
    runner: Runner | None = None,
    env: dict | None = None,
    out=None,
) -> int:
    """Print the post, and send it only if `confirmed`.

    The single choke point. `confirmed` comes from `--yes` and from nowhere
    else — there is no environment variable, no config file and no "assume yes"
    path, because every one of those is a way for this to fire unattended.
    """
    out = out or sys.stdout
    # Resolved at call time, not bound as a default argument, so a caller (and
    # the test suite) can substitute the process runner by name.
    runner = runner or default_runner
    env = dict(os.environ if env is None else env)
    key, origin = resolve_key(env)

    print("", file=out)
    print("─" * 68, file=out)
    print(f"  WOULD POST TO BINANCE SQUARE   ({len(text)} chars"
          f"{', article: ' + title if title else ', short post'})", file=out)
    print("─" * 68, file=out)
    print(text, file=out)
    print("─" * 68, file=out)
    print(f"  key:  {mask_key(key) if key else '<none>'}   from {origin}", file=out)

    if not confirmed:
        print("  DRY RUN — nothing was posted. Add --yes to publish.", file=out)
        print("", file=out)
        return 0

    if not key:
        print("", file=out)
        print(explain_missing_key(), file=out)
        return 1

    if title is None and len(text) > SHORT_POST_LIMIT:
        print(f"  REFUSING: {len(text)} chars exceeds the short-post limit of "
              f"{SHORT_POST_LIMIT}.", file=out)
        print("  Pass --title \"...\" to publish it as an article instead.", file=out)
        return 1

    script = find_script(skill_dir)
    if script is None:
        print("", file=out)
        print(f"  Could not find {POST_TEXT_SCRIPT}. Clone {SKILL_REPO} and point at it:",
              file=out)
        print(f"      --skill-dir <checkout>/{SKILL_SUBPATH}", file=out)
        return 1
    if shutil.which(node) is None:
        print(f"  {node!r} is not on PATH. The square-post scripts need Node 18+.", file=out)
        return 1

    argv = [node, str(script), "--text", text]
    if title:
        argv += ["--title", title]
    # The key goes in the environment and only in the environment. `square-post`
    # refuses a `--key` argument for the same reason.
    env[KEY_ENV] = key

    print("  POSTING…", file=out)
    code, stdout, stderr = runner(argv, env)
    if stdout:
        print(stdout.rstrip(), file=out)
    if stderr:
        print(stderr.rstrip(), file=out)
    print("", file=out)
    return code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="publish_card.py",
        description="Publish a Rekt Wrapped card to Binance Square. "
                    "Dry run by default; redacts currency amounts by default.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Nothing is posted without --yes. Currency amounts are removed unless\n"
            "--show-amounts is given. The card is your trading record: the\n"
            "percentages are the shareable part, the balance is not.\n"
            f"\nGet a key from the Creator Center: {CREATOR_CENTER}"
        ),
    )
    p.add_argument(
        "source", nargs="?", default="synthetic:disposition",
        help="history source: a CSV export, `baw`, or synthetic:<leak>[:seed]",
    )
    p.add_argument(
        "--yes", action="store_true",
        help="actually publish. Without this the script prints the post and exits.",
    )
    p.add_argument(
        "--redact", dest="redact", action="store_true", default=True,
        help="remove currency amounts, keep percentages and counts (default)",
    )
    p.add_argument(
        "--show-amounts", dest="redact", action="store_false",
        help="publish the real money figures. Requires meaning it.",
    )
    p.add_argument("--title", default=None,
                   help="publish as a Square article with this title")
    p.add_argument("--skill-dir", default=None,
                   help=f"path to a checkout of {SKILL_SUBPATH}")
    p.add_argument("--node", default="node", help="node executable (default: node)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        text = build_post(args.source, redacted=args.redact)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - a stack trace helps nobody here
        print(f"could not build the card from {args.source!r}: {exc}", file=sys.stderr)
        return 1

    if args.redact:
        leaked = currency_amounts(text)
        # A belt-and-braces check on the one guarantee this script makes. If
        # redaction ever regresses, this refuses to post rather than publishing
        # the figures it promised to remove.
        if leaked:
            print(f"redaction failed — {len(leaked)} amount(s) survived: "
                  f"{leaked[:5]}", file=sys.stderr)
            return 1
    return publish(
        text,
        confirmed=args.yes,
        title=args.title,
        skill_dir=args.skill_dir,
        node=args.node,
    )


if __name__ == "__main__":
    sys.exit(main())
