#!/usr/bin/env bash
# The demo, as a script, so the video is a recording of something reproducible
# rather than a performance. Every command runs from a fresh clone with no
# credentials, no network and no account -- which is the point being made.
#
#   ./scripts/demo.sh          run it
#   ./scripts/demo.sh --slow   pause between beats, for screen recording
#
# Beat order is deliberate. It opens on the trader with NO planted problem,
# because that is the only beat that is not circular: the demo book has its
# habits planted on purpose, so finding them proves the plumbing works, not that
# the method is sound. Refusing to find anything in a clean book is the claim
# that actually distinguishes this from astrology, and it belongs first.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
PY=${PY:-python3}

PAUSE=0
[ "${1:-}" = "--slow" ] && PAUSE=1
beat() { [ "$PAUSE" = "1" ] && sleep "${1:-2}" || true; }
say()  { printf '\n\033[2m# %s\033[0m\n\n' "$1"; }

say "1. A trader with no planted problem. Watch it decline to invent one."
$PY -m gnosis.cli card "synthetic::7"
beat 6

say "2. Now a FICTIONAL futures book with habits planted in it, so you can check the answers."
$PY -m gnosis.cli card corpus/demo-account.csv
beat 8

say "3. The same engine, asked about a trade before it happens. 03:14 UTC, 25x, 41 min after a loss."
$PY -m gnosis.cli check corpus/demo-account.csv \
    --symbol DOGEUSDT --notional 7000 --leverage 25 \
    --at 2026-09-01T03:14 --since-loss 41
beat 6

say "4. And the other direction -- their best setup, under-sized. It is not a brake."
$PY -m gnosis.cli check corpus/demo-account.csv \
    --symbol BTCUSDT --notional 900 --leverage 5 --at 2026-09-01T13:30
beat 6

say "5. Reproduce the false-positive rate. Smaller corpus than the README's headline, so the numbers differ -- that is stated, not hidden."
$PY evals/score.py --traders 12 | tail -20
