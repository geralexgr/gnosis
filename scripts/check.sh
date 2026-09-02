#!/usr/bin/env bash
# Full verification run. Everything here must pass before a commit.
# No credentials, no network, no model -- by design.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-python3}
export PYTHONPATH=src

echo "=== round-trip reconstruction ==="
$PY tests/test_roundtrip.py | tail -2
echo
echo "=== statistics calibration ==="
$PY tests/test_stats.py | tail -2
echo
echo "=== ingest adapters (CSV + baw) ==="
$PY tests/test_ingest.py | tail -2
echo
echo "=== agent layer (scripted model double, no API key) ==="
$PY tests/test_agent_layer.py | tail -2
echo
echo "=== detectors (stop migration, symbol competence) ==="
$PY tests/test_detectors.py | tail -2
echo
echo "=== robustness (malformed and hostile input) ==="
$PY tests/test_robustness.py | tail -2
echo
echo "=== integrations (MCP client, token audit, publisher) ==="
$PY tests/test_integrations.py | tail -2
echo
echo "=== MCP server (Gnosis as a tool other agents call) ==="
$PY tests/test_server.py | tail -2
echo
echo "=== HTML report and webhooks ==="
$PY tests/test_report.py | tail -2
echo
echo "=== card renders on every leak type ==="
for leak in disposition martingale revenge night overleverage; do
  $PY -m gnosis.cli card "synthetic:$leak" > /dev/null && echo "  ok   $leak"
done
echo
echo "=== profile.json is valid JSON on every leak type ==="
for leak in disposition martingale revenge night overleverage; do
  $PY -m gnosis.cli profile "synthetic:$leak" | $PY -c "import json,sys; json.load(sys.stdin)" \
    && echo "  ok   $leak"
done
echo
echo "=== assertion total (derived, not quoted from memory) ==="
TOTAL=0
for suite in tests/test_*.py; do
  N=$($PY "$suite" 2>&1 | tail -1 | grep -oE '^[0-9]+' || echo 0)
  TOTAL=$((TOTAL + N))
done
echo "  $TOTAL assertions across $(ls tests/test_*.py | wc -l | tr -d ' ') suites"
echo
echo "=== corpus scoring (recall + false positives) ==="
$PY evals/score.py --traders "${TRADERS:-20}" | tail -24
