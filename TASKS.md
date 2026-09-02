# Gnosis — build tracker

Binance Agent OS Mini Hackathon · Track A · deadline **8 Sep 2026 23:59 UTC**.
Started 1 Sep 2026.

Working agreement: this file is the single source of truth for what is built and
what is left. Completed items move to the changelog at the bottom with a one-line
note on what actually shipped, so a cold reader can tell what is real.

---

## Status

**Now:** review findings worked through. Remaining: git history, the video, submission.
**Test count:** 1,063 assertions across nine suites, all passing.
**Blocked on nothing.**
**Open questions for the user:** listed at the bottom.

---

## Phase 1 — deterministic spine  *(everything else depends on this)*

- [x] `model/events.py` — normalised event types (Fill, Swap, Funding, Transfer)
- [x] `model/roundtrip.py` — reconstruct closed round-trips from raw fills (FIFO lots)
- [x] `model/profile.py` — the portable `profile.json` artifact
- [x] `tests/test_roundtrip.py` — FIFO correctness, partial closes, flips, fees

## Phase 2 — corpus  *(needed to measure anything)*

- [x] `ingest/synthetic.py` — generate realistic traders with **injected** behavioural leaks
- [x] Clean twin generation — same trader, leak removed, so a firing detector on a twin is unambiguously noise
- [x] corpus built on demand by `evals/score.py`; no separate script needed
- [x] scaled corpus via `evals/score.py --traders N --seed S`

## Phase 3 — detectors  *(5 deep, not 15 shallow)*

- [x] `detectors/base.py` — `Leak`, `Detector` protocol, confidence tiers
- [x] `disposition.py` — winners cut short / losers held; stop migration
- [x] `tilt.py` — revenge trading (post-loss latency + size), win-streak inflation
- [x] `timing.py` — PnL by hour-of-day UTC, by hold-time bucket
- [x] `sizing.py` — expectancy by leverage bucket, size discipline
- [x] `selection.py` — symbol competence, entry chase
- [x] `enrich/token_audit.py` — retro token-audit over past swaps

## Phase 4 — statistics  *(this is what stops it being a horoscope)*

- [x] `stats.py` — expectancy, bootstrap confidence intervals
- [x] Significance gate — min sample size (n>=8), else emit "insufficient data"
- [x] Every claim carries the trade ids that produced it (inspectable receipts)
- [x] `counterfactual.py` — the alternate universes (no 00-06 UTC, cap leverage, top-2 symbols, just held)

## Phase 5 — the two surfaces

- [x] `card.py` — Rekt Wrapped shareable card
- [x] `gate/elenchos.py` — pre-trade base-rate lookup, verdict, suggested size
- [x] Gate fires in **both** directions (also "you size your best setup too small")
- [x] `cli.py` — `gnosis profile`, `gnosis card`, `gnosis check`

## Phase 6 — Binance integration

- [x] `ingest/baw_onchain.py` — `baw wallet tx-history` / `market-order list` adapter
- [x] `ingest/binance_csv.py` — Binance CSV export (spot + futures dialects)
- [x] MCP: `ingest/binance_mcp.py` (Account scope, read-only)
- [x] `query-token-audit` retro-scan
- [x] `square-post` publishing — `scripts/publish_card.py`, dry-run + redacted by default
- [x] Ship a `SKILL.md` and open a PR to `binance/binance-skills-hub`

## Phase 7 — agent layer

- [x] `agents/narrate.py` — model writes the card copy from computed facts only
- [x] `agents/judge.py` — model makes the marginal pre-trade call
- [x] `tests/fakes.py` — scripted model double so the whole thing runs offline

## Phase 8 — verification

- [x] `evals/score.py` — recall + false-positive rate over matched pairs
- [x] `scripts/check.sh` — full verification run
- [x] CI workflow

## Phase 9 — submission

- [x] README — problem-first, architecture diagram, measured numbers, honest limits
- [x] Architecture diagrams — Mermaid in `docs/architecture.md`
- [x] `scripts/demo.sh` — the demo as a reproducible script (`--slow` for recording)
- [ ] Demo video (target 45s) — record from `scripts/demo.sh`
- [ ] Quote-repost + survey + follow/repost

---

## Open questions for the user

1. **Real trade history?** Do you have a Binance export (CSV) or a wallet with swap
   history to profile? Until then everything runs on synthetic fixtures.
2. **Square posting** — do you have (or can you get) a Binance Square Creator
   Center OpenAPI key? Decides whether phase 6's publishing step is real or cut.
3. **Jurisdiction** — EEA is on the excluded list. Unresolved.

---

## Resolved: multiple-comparison correction

Measured, not guessed. Sweep at 75 pairs per setting, after the `weak` fix:

| correction | recall | false positives |
|---|---|---|
| none (`family=1`) | 96.0% | 13.33% — all `session_performance` |
| **family-wide (`family=8`)** | **89.3%** | **0.00%** |

**Chose `family=8`.** Six points of recall is a fair price for a clean
false-positive rate: a profiler that invents leaks gets correctly disbelieved on
everything else it says, so the FP number is the one that decides whether the
tool is usable at all. Overridable via `GNOSIS_FAMILY` for anyone who wants to
see the trade-off themselves.

## Measured — 280 defective traders + 280 clean twins, family=13

| | |
|---|---|
| recall | **85.4%** |
| false positives | **2.50%** (7/280, all `session_performance`) |
| weakest detector | `stop_migration` 62.5% — reported, not tuned until it looks better |

## Superseded numbers (kept so the trend is legible)

`python evals/score.py --traders 20` — 100 defective traders + 100 clean twins:

| | |
|---|---|
| recall (correct detector fired) | **95/100 (95.0%)** |
| recall on *subtle* effects (0.6-0.9x) | 88.6% |
| recall on severe effects (1.2-1.5x) | 100% |
| **false positives on clean twins** | **0/100 (0.00%)** |
| bootstrap null rate, measured | 12.5% against a nominal 10% |

## Review round — five reviewers, findings worked through

Four hostile reviewers plus an integrations agent. Two hit the session rate
limit (the statistician's audit never ran, and is still owed).

**From the first-time user** — every item actioned:
- `gnosis card` with **no arguments printed a confident profile of a trader who
  does not exist.** The worst bug in the repo for a project whose thesis is that
  it never invents a finding. `source` is now required.
- Added `./gnosis`, a wrapper that carries `PYTHONPATH` so it is never the
  user's problem. My first attempt at this — catching `ModuleNotFoundError` in
  `main()` — could never have worked: the error fires in the import machinery
  before any of the code runs.
- `SKILL.md` claimed three times that the CLI could not read a CSV. All false,
  and it is the file an *agent* reads, so it would have told users their most
  common request needed hand-written Python. Rewritten, along with five more
  stale claims in its "Not yet available" section.
- README restructured: pitch → diagram → three commands → the actual card →
  both gate verdicts → glossary → safety → *then* methodology. Added "Using your
  own data", a troubleshooting path to `inspect_csv.py`, runtimes on the slow
  commands, and links to four orphaned docs.
- `Status` said five detectors (seven), that `stop_migration` had no detector
  (it fires), and that adapters were untested against real exports (one had
  parsed a real export cleanly).

**From the hostile judge:**
- `scripts/demo.sh` — the **video narration** — opened by calling the fictional
  book "a real Binance futures export". I wrote that. On a submission that is a
  public post, it is the kind of thing a competitor screenshots. Rewritten, and
  the beats reordered to open on the clean trader, which is the only
  non-circular beat in the demo.
- **Counterfactuals double-counted in the money shot.** Three licensed rules
  reported +13,082, +12,484 and +9,584 against a book that lost 11,577 — they
  summed to three times the entire loss, because the night trades *are* the 20x
  trades *are* the DOGE trades. The card now computes and shows the joint
  figure (+14,283) and names the overlap (90 of 130 trades claimed twice).
- CI hardcoded "95.0% recall" in its job summary while its own header comment
  bragged that nothing is taken on faith. Removed; it now runs all nine suites
  rather than four.

**From the adversarial code review** — nine confirmed defects, all fixed:
1. `reconstruct_realised` **crashed** (`IndexError`) on any book that sold out
   cleanly — one buy and one sell. It is the default path for spot books.
2. Realised mode **silently inverted shorts**, re-pairing a short's cover
   against the next short's open. Three profitable shorts read as a loss, with
   hold times inflated ~48x, feeding a detector whose whole metric is hold time.
3. The dust sweep used a fraction of *cumulative* opened quantity, so a
   deliberate scale-out to 0.4 BTC was swept away as dust. Now a fraction of the
   closing fill, and restricted to unleveraged venues. PnL conservation over
   random futures streams: **0/300 violations, was 5/300**.
4. `Disposition.cost` reported the **sum of every losing trade** — a metric
   summed over an arm selected by that metric. Guaranteed negative, guaranteed
   to be the biggest number on the card, and it stole the headline from findings
   with real counterfactuals behind them. Now `None`: the asymmetry is
   measurable, its cost is not computable from fills, and the field exists to
   say so.
5. `Revenge` selected its slice on size and compared on dollars — biased before
   any behaviour was measured. Now compares on `return_pct`, like every other
   size-slicing detector.
6. The gate and the Revenge detector disagreed about "your usual size"
   (`sorted()[n//2]` vs a true median), violating an invariant stated in the
   gate's own comments.
7. `_analogue`'s complement used `t not in matched` — O(n²) dataclass equality,
   and wrong, since field-identical trips compare equal.

Added a **PnL conservation property test** over random streams, and realised-mode
tests for shorts and clean sell-outs.

**Verification of the verification:** the safety guarantees are now
mutation-tested. Neutering the webhook's `send=True` guard or the publisher's
`--yes` guard makes tests fail, so those are enforced, not merely documented.

**2 Sep, after the reviews** — headline numbers re-derived after the detector
fixes: recall **83.6%** (was 85.4%), false positives **2.50%** on twins. Added a
second, more conservative measurement the reviews implied was missing: the
family-wise error rate on **independent** null traders, **4.00%** — twins are
structurally paired with a defective trader, so they are not the same test.
Both are now reproducible via `evals/score.py --traders 40 --nulls 150`.

`revenge_trading` fell 72.5% -> 60.0%, which is the price of an unbiased
estimator and is stated as such rather than quietly restored.

Corrected the assertion total from a hardcoded 1,063 to the actual **1,061**,
and made `check.sh` derive and print it so the number cannot go stale again.

## Changelog

**1 Sep** — `model/events.py`: Fill / OrderEvent / CashFlow / History. Two venues
(CEX + on-chain) flatten to one `Fill` type; venue-specific detail survives in
`meta`. `OrderEvent` is separate because stop-migration leaves no trace in the
fill stream.

**1 Sep** — `model/roundtrip.py`: flat-to-flat FIFO reconstruction. Handles adds,
scaling out, position flips through zero, per-venue and per-symbol isolation.
Accumulates the behavioural counters (`n_adds_underwater`, `n_partial_exits`,
`worst_observed_price`) during the single ordered walk. Open positions are
returned separately and excluded from statistics — an unrealised loss is not yet
a decision to hold a loser.

**1 Sep** — `scripts/demo.sh`: the demo as a script, so the video records
something reproducible rather than a performance. Five beats; the fourth is the
one that matters — a trader with no planted leak, where the card says *"no
behavioural leak cleared the significance bar"* and stops. That is the
difference from a horoscope, on camera, in one screen.

**2 Sep** — `scripts/make_demo_account.py` + `corpus/demo-account.csv`: a
**fictional** futures book, committed so the demo runs from a fresh clone. Two
jobs — a spot investor's export has too few closed positions to profile, and the
futures CSV dialect had never been tested against a file. It parsed 649/649 and
then earned its keep immediately by exposing **phantom trips**: float residue of
3.6e-12 DOGE surviving an absolute 1e-12 epsilon, opening a position that closed
with a real fee on a 6e-13 notional — a -200 trillion percent return that was
silently suppressing a real `leverage_drag` finding. Epsilon is now relative to
position size, with an economic floor (`MIN_TRADE_NOTIONAL`).

The persona is structured by session — disciplined on majors by day, 25x on an
alt at 3am — so the leaks co-occur the way they do in a real book. Profile finds
five leaks and one genuine strength; the gate returns SKIP on the 3am DOGE trade
(suggesting 1,322 not 7,000) and FAVOURABLE on the daytime BTC trade,
**suggesting sizing up to 2,645 from 900**.

**2 Sep** — **Real data broke three things a synthetic corpus never could.**
Ran the first real Binance spot export through the parser:
every row parsed first try, but the model was wrong in three ways.
(1) *Dust*: Binance charges some spot fees in the base asset, so a position can
never reach exactly flat — most of that trader's positions never closed. Added a
dust sweep, recording the abandoned quantity rather than dropping it.
(2) *Wrong trade unit for spot*: flat-to-flat is a futures concept; a spot
investor never goes flat. Added a FIFO realised-sale model, auto-selected by
whether the book is leveraged. (3) *Stablecoin conversions*: the large majority of realised
"trades" were stablecoin-to-stablecoin at ~0.05% median absolute return, which would have set
the baseline every finding is measured against. Now filtered before anything is
computed. After all three fixes the export yields fewer than thirty genuine trades and Gnosis
correctly declines to profile it.

**2 Sep** — `detectors/execution.py` (stop migration) and `detectors/selection.py`
(symbol competence), `PROFILE_FAMILY` 8 -> 13, order events routed through
`Profile`. `scripts/inspect_csv.py` + `tests/test_robustness.py` (185 assertions)
found and fixed **10 real parser defects**, including truncated rows silently
booking fee-free fills and `1e400` becoming `inf`. `ingest/binance_mcp.py`,
`enrich/token_audit.py`, `scripts/publish_card.py`, `tests/test_integrations.py`
(165 assertions).

**1 Sep** — Agent layer wired into the CLI as `--narrate` on `card` and `check`,
plus `card.render_narrated`. Off by default so the demo and the suite stay
deterministic — a card whose wording changes between runs is a card whose
numbers people stop trusting. Model copy replaces only the headline and the
habit description; counts, totals and counterfactuals are always rendered from
the profile. Verified end to end against the scripted double: faithful copy
renders, a hallucinated figure falls back to the template naming the number it
rejected.

**1 Sep** — **Elenchos was miscalibrated and is fixed.** The gate did no
significance testing at all — it compared raw means and needed only
`MIN_SAMPLE`, while the detectors were bootstrap-gated and family-corrected. On
clean traders with no planted leak it returned a strong verdict on **19 of 24**
neutral proposals. Analogues now run the same test as the detectors; only
significant ones drive a verdict. False alarms on clean traders: **0 of 120**.
Also fixed in the gate: an analogue labelled "oversized, soon after a loss" whose
comparison set filtered only on size; a `--side` flag the gate never read; and a
`suggested_notional` that could equal the proposal.

**1 Sep** — `SKILL.md`, `.github/workflows/verify.yml` (3.10/3.13 matrix,
publishes recall + FP to the job summary so every run re-derives the README's
claims), `docs/architecture.md` (3 Mermaid diagrams). CLI now reads real CSV
exports and the `baw` wallet, not just fixtures.

**1 Sep** — `ingest/binance_csv.py` + `ingest/baw_onchain.py` (**101 assertions**).
CSV headers matched by alias sets rather than position, so a file that gains a
column fails loudly instead of silently mis-reading. Unparseable rows raise with
the line number — a profile built on silently-dropped fills is worse than no
profile. On-chain: only `FINISHED` swaps become Fills; a `FAILED` swap that
burned gas becomes a `CashFlow`, never a Fill.

**1 Sep** — `agents/` + `tests/fakes.py` (**125 assertions**). The model never
decides: it receives already-significance-tested facts and its output is
fact-checked against them, falling back to the deterministic template on any
violation. Added `check_polarity` to close a hole the layer shipped with —
`check_numbers` compares magnitudes, so "you **made** 3,150" passed against a
fact of -3,150. On a card whose purpose is naming what a habit costs, an
inverted sign is the worst possible output: every number checks out and the
meaning is backwards.

**1 Sep** — **False-positive rate was overstated as 0.00% and is not.** At 100
clean twins the eval read 0/100; at 200 twins the true rate surfaced as
**15/200 (7.5%)**. Nothing regressed — the smaller corpus simply lacked the
samples to show it. Two real causes, both mine:

1. *Only the session detector corrected for multiple comparisons.* The other
   four each tested at a raw 90% interval whose measured null rate is 12.5%.
   Correcting within a detector rather than across the profile is precisely how
   a ~7.5% family-wise rate happens. Fixed: `PROFILE_FAMILY`, applied at all six
   `assess()` call sites.
2. *`weak` findings were surfacing.* `detectors/base.py` documents that a `weak`
   finding "never surfaces to a user on its own" — and then `averaging_down`
   returned one unconditionally and `card.py` rendered every leak it was given.
   The contract was documented and not enforced. Fixed in `Profile`: `leaks`
   now holds only surfacing findings, `watch` holds the underpowered ones.

Also fixed: `evals/score.py` called the detectors directly, bypassing the
Profile, so it was scoring findings a user would never see. It now runs the real
end-to-end path.

**1 Sep** — `gate/elenchos.py`: pre-trade cross-examination. Never blocks;
speaks in both directions (also flags when the trader is about to under-size
their best pattern). The verdict is arithmetic — a model asked "should I take
this trade?" produces confidence unrelated to evidence.

**1 Sep** — `card.py` + `cli.py`: `gnosis card / profile / check`, all offline.
Fixed two rendering flaws found by reading the output: a *losing* session was
being reported as a strength because it beat baseline, and the card quoted two
differently-derived numbers for the same counterfactual.

**1 Sep** — `pyproject.toml`, `scripts/check.sh`, `README.md`, `LICENSE`.
Zero runtime dependencies, so the whole thing clones and runs with no pip, no
credentials and no network.

**1 Sep** — `ingest/synthetic.py`: five injectable leaks, each with a clean twin
built from the same decision list, so a detector firing on a twin is
unambiguously noise. Fixed one generator bug: revenge trades were anchored to
the prior trade's *open* rather than the moment the loss was booked.

**1 Sep** — `stats.py`: bootstrap CIs, min-sample refusal, Bonferroni-style
`family_ci()` for detectors testing several slices. Optimised the resampler
(`random.choices`) — 11x faster, identical statistics, and the difference
between a 2.4s profile and a 200ms one.

**1 Sep** — five detectors + registry. All five find their planted leak; none
fire on any twin.

**1 Sep** — `counterfactual.py` + `model/profile.py`: the alternate universes,
**gated behind a proven leak**. Raw counterfactuals are unsafe — any history
sliced four ways shows one slice worth removing. The clean twin shows five
tempting counterfactuals and the profile suppresses all of them. Licences match
the *specific* slice proven, not the family.

**1 Sep** — `tests/test_stats.py`: **17 assertions.** Calibrates the null rate
rather than assuming it.

**1 Sep** — `tests/test_roundtrip.py`: **42 assertions, all passing.** Caught two
real bugs during development: a shared class attribute corrupting the
weighted-average exit across trips, and an inverted adverse-excursion sign that
reported losses as gains on long positions.
