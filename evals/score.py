"""Score the detectors against the labelled corpus.

Two numbers matter, and only one of them is recall.

**Recall** is per-leak and specific: the detector assigned to the planted leak
must fire. A detector that reports something else does not get credit.

**False positives** are counted on the *clean twins*. Each twin is the same
trader with the pathology removed, so anything reported there is noise by
construction. This is the number that decides whether the tool is usable: a
profiler that invents leaks is worse than no profiler, because the user cannot
tell which findings to trust and will discount all of them.

Recall is also broken out by injection strength, because a single recall figure
hides whether a detector only works on caricatures. A detector that finds a 1.4x
effect is useful; one that only finds a 4x effect is describing the obvious.

Run: python evals/score.py [--traders N] [--seed S]
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gnosis.detectors import LEAK_RULE  # noqa: E402
from gnosis.ingest.synthetic import (  # noqa: E402
    LEAKS,
    TraderSpec,
    generate,
    generate_pair,
)
from gnosis.model import profile as profile_mod  # noqa: E402
from gnosis.stats import CI_HIGH, CI_LOW, PROFILE_FAMILY  # noqa: E402

# Strength bands, so recall can be reported against effect size rather than
# hidden behind an average. 0.6 is a subtle habit; 1.4 is a caricature.
BANDS = [(0.6, "subtle (0.6-0.9)"), (0.9, "clear (0.9-1.2)"), (1.2, "severe (1.2-1.5)")]


def band_of(strength: float) -> str:
    label = BANDS[0][1]
    for lo, name in BANDS:
        if strength >= lo:
            label = name
    return label


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--traders", type=int, default=60, help="pairs per leak type")
    ap.add_argument("--seed", type=int, default=31)
    ap.add_argument("--nulls", type=int, default=0,
                    help="also measure the family-wise error rate on N "
                         "independent traders with nothing injected")
    args = ap.parse_args()

    hits: dict[str, int] = defaultdict(int)
    total: dict[str, int] = defaultdict(int)
    band_hits: dict[tuple[str, str], int] = defaultdict(int)
    band_total: dict[tuple[str, str], int] = defaultdict(int)
    fp_traders = 0
    fp_findings: dict[str, int] = defaultdict(int)
    twins = 0

    for leak in LEAKS:
        for i in range(args.traders):
            seed = args.seed + i * 7919  # a prime stride keeps streams apart
            # Vary the strength so recall is not measured only on caricatures.
            strength = 0.6 + ((i * 0.137) % 0.9)
            spec = TraderSpec(seed=seed, leak=leak, strength=round(strength, 3))
            bad, twin = generate_pair(spec)
            band = band_of(strength)

            # Scored through the real Profile, not the raw detectors: `weak`
            # findings never reach a user, so counting them either way would
            # measure something nobody sees.
            found = {leak_.rule for leak_ in profile_mod.for_history(bad).leaks}
            total[leak] += 1
            band_total[(leak, band)] += 1
            if LEAK_RULE[leak] in found:
                hits[leak] += 1
                band_hits[(leak, band)] += 1

            twin_found = profile_mod.for_history(twin).leaks
            twins += 1
            if twin_found:
                fp_traders += 1
                for leak_ in twin_found:
                    fp_findings[leak_.rule] += 1

    print("=" * 72)
    print("RECALL  (did the detector for the planted leak fire?)")
    print("=" * 72)
    for leak in LEAKS:
        n, h = total[leak], hits[leak]
        pct = 100.0 * h / n if n else 0.0
        print(f"  {leak:14s} {LEAK_RULE[leak]:22s} {h:4d}/{n:<4d}  {pct:5.1f}%")
    all_h, all_n = sum(hits.values()), sum(total.values())
    print(f"  {'OVERALL':14s} {'':22s} {all_h:4d}/{all_n:<4d}  {100.0*all_h/all_n:5.1f}%")

    print()
    print("=" * 72)
    print("RECALL BY EFFECT SIZE  (does it only work on caricatures?)")
    print("=" * 72)
    for _, band in BANDS:
        h = sum(v for (lk, b), v in band_hits.items() if b == band)
        n = sum(v for (lk, b), v in band_total.items() if b == band)
        if n:
            print(f"  {band:20s} {h:4d}/{n:<4d}  {100.0*h/n:5.1f}%")

    print()
    print("=" * 72)
    print("FALSE POSITIVES  (anything at all reported on a clean twin)")
    print("=" * 72)
    rate = 100.0 * fp_traders / twins if twins else 0.0
    print(f"  twins with >=1 finding   {fp_traders:4d}/{twins:<4d}  {rate:5.2f}%")
    if fp_findings:
        for rule, n in sorted(fp_findings.items(), key=lambda kv: -kv[1]):
            print(f"    {rule:24s} {n}")
    else:
        print("    (none)")

    if args.nulls:
        # The twins are structurally paired with a defective trader. These are
        # not: unrelated seeds, nothing injected, nothing to find. It is the
        # family-wise error rate of the whole profile, and it is the number to
        # quote if you only quote one -- a rate measured on a small or
        # structurally-related sample reads better than the truth.
        null_hits: dict[str, int] = defaultdict(int)
        null_traders = 0
        for i in range(args.nulls):
            prof = profile_mod.for_history(
                generate(TraderSpec(seed=1000 + i * 7919, leak=None))
            )
            if prof.leaks:
                null_traders += 1
            for leak_ in prof.leaks:
                null_hits[leak_.rule] += 1
        print()
        print("=" * 72)
        print("FAMILY-WISE ERROR  (independent traders, nothing injected)")
        print("=" * 72)
        rate = 100.0 * null_traders / args.nulls
        print(f"  traders with >=1 finding  {null_traders:4d}/{args.nulls:<4d}  {rate:5.2f}%")
        print(f"  Bonferroni intends <= {100.0 - (CI_HIGH - CI_LOW):.0f}% family-wise")
        for rule, n in sorted(null_hits.items(), key=lambda kv: -kv[1]):
            print(f"    {rule:24s} {n}")

    print()
    print(f"corpus: {all_n} defective traders + {twins} clean twins, "
          f"{args.traders} pairs per leak, seed {args.seed}, "
          f"family={PROFILE_FAMILY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
