"""Calibration of the significance machinery.

The detectors are only trustworthy if `assess` is. These tests check the two
things that would silently poison every finding: that a real difference is
found, and that noise is *not* — at a rate we have actually measured rather
than assumed.

Run: python tests/test_stats.py
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gnosis.stats import MIN_SAMPLE, assess, family_ci, percentile  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{'' if cond else '  <- ' + detail}")


def null_rate(n_s, n_r, ci=None, trials=400, heavy=False, seed=99):
    """How often we claim a difference when both arms are the same distribution."""
    rng = random.Random(seed)
    hits = 0
    for i in range(trials):
        if heavy:
            a = [rng.gauss(0, 100) * (6 if rng.random() < 0.04 else 1) for _ in range(n_s)]
            b = [rng.gauss(0, 100) * (6 if rng.random() < 0.04 else 1) for _ in range(n_r)]
        else:
            a = [rng.gauss(0, 100) for _ in range(n_s)]
            b = [rng.gauss(0, 100) for _ in range(n_r)]
        if assess(a, b, seed=i, ci=ci).significant:
            hits += 1
    return 100.0 * hits / trials


print("=== percentile ===")
check("median of 1..5", percentile([1, 2, 3, 4, 5], 50) == 3.0)
check("interpolates", abs(percentile([0, 10], 25) - 2.5) < 1e-9)
check("single value", percentile([7], 90) == 7)

print("\n=== refuses to speak on small samples ===")
c = assess([1.0] * 3, [2.0] * 20)
check("insufficient set", c.insufficient is not None)
check("not significant", not c.significant)
check(f"needs {MIN_SAMPLE} per arm", str(MIN_SAMPLE) in (c.insufficient or ""))

print("\n=== finds a real difference ===")
rng = random.Random(5)
worse = [rng.gauss(-80, 100) for _ in range(40)]
better = [rng.gauss(+30, 100) for _ in range(120)]
c = assess(worse, better, seed=1)
check("flagged", c.significant)
check("delta negative", c.delta < 0, f"{c.delta}")
check("interval excludes zero", c.ci_high < 0, f"ci_high={c.ci_high}")
check("total_cost scales by n", abs(c.total_cost - c.delta * c.n_slice) < 1e-9)

print("\n=== null calibration (the number that keeps this honest) ===")
# The percentile bootstrap is mildly anti-conservative at these sample sizes.
# We measure it rather than assume nominal, and allow a band around it — if
# this ever drifts far, every finding downstream is suspect.
g = null_rate(40, 120)
h = null_rate(40, 120, heavy=True)
f4 = null_rate(40, 120, ci=family_ci(4))
small = null_rate(8, 8)
print(f"     gaussian 90% CI      {g:5.1f}%  (nominal 10%)")
print(f"     heavy-tailed 90% CI  {h:5.1f}%  (nominal 10%)")
print(f"     family_ci(4)         {f4:5.1f}%  (nominal 2.5%)")
print(f"     n=8 vs 8, 90% CI     {small:5.1f}%  (nominal 10%)")
check("gaussian null in 6-16%", 6.0 <= g <= 16.0, f"{g}%")
check("heavy tails do not break it", 6.0 <= h <= 18.0, f"{h}%")
check("family correction tightens", f4 < g, f"{f4}% vs {g}%")
check("family correction near nominal", f4 <= 6.0, f"{f4}%")
check("small samples not wildly worse", small <= 20.0, f"{small}%")

print("\n=== family_ci ===")
check("4 hypotheses -> 1.25/98.75", family_ci(4) == (1.25, 98.75), f"{family_ci(4)}")
check("1 hypothesis is the default", family_ci(1) == (5.0, 95.0), f"{family_ci(1)}")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
