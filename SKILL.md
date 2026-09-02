---
title: Gnosis — Behavioural Pre-Trade Gate
name: gnosis-behavioural-gate
description: |
  Use before placing, sizing, or approving a trade, and whenever the user asks about their own
  trading habits, leaks, or track record. Triggers: "should I take this trade", "check this trade",
  "am I about to tilt", "is this revenge trading", "how do I do at this hour", "what's my win rate
  at 20x", "have I done this before", "size this trade", "how big should this be",
  "what's my worst habit", "where am I losing money", "am I holding losers", "do I cut winners
  too early", "am I averaging down again", "my Rekt Wrapped", "wrapped card", "year in review
  for my trading", "profile my trade history", "behavioural audit", "trading psychology report",
  "base rate for trades like this", "counterfactual, what if I never traded at 4am",
  "disposition effect", "averaging down", "revenge trading", "leverage drag",
  "session performance", or any request to judge a proposed trade against the user's own history.
  Do NOT use it to predict prices, pick entries, or generate signals — Gnosis has no opinion about
  markets, only about this trader.
metadata:
  version: '0.1.0'
  author: geralexgr
license: MIT
---

# Gnosis — Behavioural Pre-Trade Gate

**γνῶθι σεαυτόν.** Gnosis reads a trader's own fill history back to them at the moment
it matters. It does not forecast, it does not rank tokens, and it holds no view on any
market. It answers one question: *what has this trader historically done in situations
that resemble the one in front of them, and what did it cost?*

Two surfaces over one deterministic engine:

- **`gnosis check`** (Elenchos) — the pre-trade gate. Call it *before* placing an order.
  It quotes the base rate and returns a verdict. **It never blocks.**
- **`gnosis card`** / **`gnosis profile`** (Rekt Wrapped) — the retrospective. The card
  is human-readable; the profile is JSON for another agent to consume.

Everything the gate *decides* is arithmetic on the trader's own closed round-trips. A
model may be used to write the sentence, never to reach the conclusion — and the CLI
never invokes one at all (see [The agent layer](#the-agent-layer)). That is the point: a
model asked "should I take this trade?" returns confidence unrelated to evidence.

---

## Command Routing

| User Intent | Command | Reference |
|---|---|---|
| Should I take this trade? / check this trade before I place it | `gnosis check <source> --symbol … --notional …` | [Elenchos: the pre-trade gate](#elenchos-the-pre-trade-gate) |
| Am I revenge trading? / I just took a loss, is this too soon | `gnosis check <source> --since-loss <minutes> --notional …` | [Elenchos: the pre-trade gate](#elenchos-the-pre-trade-gate) |
| Am I averaging down again? / should I add to this loser | `gnosis check <source> --adding-to-loser` | [Elenchos: the pre-trade gate](#elenchos-the-pre-trade-gate) |
| How do I perform at 20x? / is this leverage bad for me | `gnosis check <source> --leverage 20` | [Elenchos: the pre-trade gate](#elenchos-the-pre-trade-gate) |
| How do I trade at 3am? / what's my record in this session | `gnosis check <source> --at 2026-09-01T03:14` | [Elenchos: the pre-trade gate](#elenchos-the-pre-trade-gate) |
| How big should this be? / size this trade | `gnosis check …` → read `suggested_notional` | [Reading a verdict](#reading-a-verdict) |
| What is my worst habit? / where am I losing money | `gnosis card <source>` | [Rekt Wrapped: the card](#rekt-wrapped-the-card) |
| My Rekt Wrapped / year in review / shareable card | `gnosis card <source>` | [Rekt Wrapped: the card](#rekt-wrapped-the-card) |
| Give me my profile as data / I want to feed this to another agent | `gnosis profile <source>` | [`profile` JSON shape](#profile-json-shape) |
| What if I had never traded the small hours? | `gnosis profile <source>` → `counterfactuals[]` | [`profile` JSON shape](#profile-json-shape) |
| Show me the trades that prove it | any command → `trade_ids[]` | [`profile` JSON shape](#profile-json-shape) |
| Profile my real Binance CSV export | `./gnosis card my-export.csv` | [Real histories](#real-histories) |
| Profile my on-chain wallet | Python: `baw_onchain.load()` — needs a signed-in `baw` | [Real histories](#real-histories) |
| Can Gnosis read my live Binance account over the API? | **No.** Say so. | [Not yet available](#not-yet-available) |
| How does this actually work? | — | [`docs/architecture.md`](docs/architecture.md) |

---

## Prerequisites

Zero runtime dependencies, no credentials, no network. Python **3.10+**.

```bash
git clone <repo> && cd gnosis
export PYTHONPATH=src
python3 -m gnosis.cli card synthetic:night
```

Or install it so the `gnosis` entry point exists:

```bash
pip install -e .          # then: gnosis card synthetic:night
```

Every example below uses `python3 -m gnosis.cli` with `PYTHONPATH=src`, because that
form works from a fresh clone with nothing installed.

---

## Build the command

### The `<source>` argument

Every command takes one **required** positional `source`. There is deliberately no
default: a tool whose claim is that it never invents a finding must not invent an entire
trader when invoked with no arguments.

| Source | Meaning |
|---|---|
| `my-export.csv` | A Binance trade-history CSV export. Spot and futures layouts are auto-detected |
| `corpus/demo-account.csv` | The committed **fictional** demo book |
| `baw` | The on-chain Binance Agentic Wallet (requires `baw` installed and signed in) |
| `synthetic:<leak>` | A labelled trader with that leak injected, seed 31 |
| `synthetic:<leak>:<seed>` | Same, with an explicit seed |
| `synthetic::<seed>` | A **clean** trader — no injected leak |
| anything else | Exits with a message listing the four accepted forms |

Valid `<leak>` values: `disposition`, `martingale`, `revenge`, `night`, `overleverage`,
`stop_migration`, `symbol_leak`. An unknown leak name exits with
`unknown leak '…'; choose from …`.

**A CSV that will not parse** exits with the columns it could not match and points at
`python3 scripts/inspect_csv.py <file>`, which diagnoses encoding, delimiter, header and
every failing row, and cannot itself crash.

**Expect a spot account to be declined.** Gnosis needs 30 closed positions; spot
investors accumulate and hold. A real multi-year spot export produced fewer than thirty realised
trades and was refused. Futures and margin books close properly and profile well.

### Elenchos: the pre-trade gate

```bash
PYTHONPATH=src python3 -m gnosis.cli check <source> \
    [--symbol ETHUSDT] [--side buy|sell] [--notional 4000] \
    [--leverage 20] [--at 2026-09-01T03:14] \
    [--adding-to-loser] [--since-loss 41]
```

| Flag | Type | Default | What it does |
|---|---|---|---|
| `--symbol` | str | `ETHUSDT` | Adds a per-symbol analogue |
| `--side` | `buy`\|`sell` | `buy` | Recorded on the proposal; does not currently select an analogue |
| `--notional` | float | `4000.0` | Quote-currency size. Drives the size comparison and `suggested_notional` |
| `--leverage` | float | none | Adds the "at 10x or above" analogue **only when ≥ 10** |
| `--at` | ISO 8601 | now (UTC) | Selects the session analogue. Parsed as UTC; a `Z` or offset is ignored/overwritten |
| `--adding-to-loser` | flag | off | Adds the "adding to a losing position" analogue. Must be supplied by the caller — it cannot be inferred |
| `--since-loss` | float (minutes) | none | Only has an effect at **≤ 90 minutes**, and only if `--notional` is ≥ 1.25× the trader's median |

One caveat on the last row. The analogue it produces is labelled *"sized well above your
usual, soon after a loss"*, but the historical slice it measures is **every trade sized
≥ 1.25× the median** — the "soon after a loss" half is a condition on the *proposal*, not
a filter on the comparison set. The `n` will therefore look large (108 trades in the
example below). Read it as "your record on oversized trades", which is what it is.

Real output:

```
$ PYTHONPATH=src python3 -m gnosis.cli check synthetic:night \
      --leverage 20 --at 2026-09-01T03:14 --since-loss 41

  ✗ SKIP  This matches your most expensive pattern — the small hours (00:00-06:00 UTC). 65 prior trades, 23% win rate, -37 each.

    · the small hours (00:00-06:00 UTC): 65 trades, 23% win rate, -37 per trade against your -12 baseline
    · at 10x or above: 76 trades, 49% win rate, -7 per trade against your -12 baseline
    · sized well above your usual, soon after a loss: 108 trades, 35% win rate, -21 per trade against your -12 baseline
    · on ETHUSDT: 67 trades, 40% win rate, -7 per trade against your -12 baseline

    Your record suggests 643, not 4,000.

    Elenchos never blocks. This is your own history, quoted back.
```

**`check` prints text, not JSON, and always exits 0.** There is no `--json` flag on any
Gnosis command. Do not construct one and do not branch on the exit code. Parse the
verdict from the first line (`SKIP` / `CAUTION` / `PROCEED` / `FAVOURABLE`), or call the
library and read the `Judgement` object — that is the reliable path, and it is what an
agent integration should do.

### Rekt Wrapped: the card

```bash
PYTHONPATH=src python3 -m gnosis.cli card <source>
```

Renders a fixed-width shareable card: headline summary, the single most expensive habit,
and any **licensed** counterfactuals. Human-readable only. Safe to paste into chat or
into a Binance Square post.

### `profile` JSON shape

```bash
PYTHONPATH=src python3 -m gnosis.cli profile <source> > profile.json
```

This is the only command that emits JSON, and it is what one agent should hand another.

```jsonc
{
  "summary": {
    "n_trades": 257, "n_open": 0, "span_days": 365.1,
    "total_pnl": -3150.38, "total_fees": 359.08,
    "win_rate": 0.4163, "expectancy": -12.26,
    "symbols": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"],
    "source": "synthetic:seed=31:night"
  },
  "leaks": [{
    "rule": "session_performance",
    "title": "You lose money in the small hours",
    "finding": "Your 65 trades in the small hours (00:00-06:00 UTC) averaged -37 …",
    "confidence": "statistical",          // "proof" | "statistical"
    "cost": -2153.79,                     // signed, quote currency; may be null
    "n": 65,
    "trade_ids": ["31-2-x6", "…"],        // capped at 20 receipts
    "detail": { "session": "the small hours (00:00-06:00 UTC)", "hours": [0,1,2,3,4,5] }
  }],
  // shapes below are illustrative; `strengths` is empty on this particular trader
  "strengths":       [{ "rule": "session_strength", "title": "…", "finding": "…",
                        "value": 0.0, "n": 0, "detail": {…} }],
  "counterfactuals": [{ "rule": "skip_session", "description": "never traded …",
                        "delta": 2405.85, "n_removed": 65,
                        "actual_total": -3150.38, "hypothetical_total": -744.53,
                        "hindsight": false, "subject": "the small hours (00:00-06:00 UTC)",
                        "trade_ids": ["…"] }],
  "observations":    [ /* same shape as counterfactuals */ ]
}
```

Things to get right when consuming this:

- **`leaks` only ever contains findings that cleared the bar.** Underpowered (`weak`)
  findings are held in a separate `watch` list that is *not serialised*. If it is in
  `leaks`, it is safe to say out loud.
- **`counterfactuals` are licensed; `observations` are not.** A counterfactual only
  appears in `counterfactuals` if a detector proved a significant leak in that *same
  specific slice*. Everything else lands in `observations`. **Never present an entry from
  `observations` as a recommendation** — it is unproven arithmetic, and on a clean trader
  it is pure noise. `hindsight: true` marks rules chosen by looking at the answer
  (`best_symbols_only`); those can never be licensed.
- **`summary` does not carry an `is_thin` flag.** Compute it yourself:
  `n_trades < 30 or span_days < 14`. See below.
- `confidence` is `"proof"` (directly countable behaviour) or `"statistical"` (a slice
  differs from the trader's own baseline and a bootstrap interval excludes zero).
- `trade_ids` are fill ids. Offer them when the user pushes back; a finding they cannot
  audit is a finding they are entitled to disbelieve.

---

## Reading a verdict

| Verdict | Mark | Means | What the agent should do |
|---|---|---|---|
| `favourable` | `✓` | Matches one of the trader's better patterns | Proceed. If `suggested_notional` is set, they habitually under-size this setup — say so |
| `proceed` | `·` | Nothing in the record argues against it | Proceed quietly |
| `caution` | `!` | Matches a below-baseline, loss-making slice | Quote the analogue. Let the user decide |
| `skip` | `✗` | Matches their most expensive pattern | Quote the analogue **and** `suggested_notional`. Still let the user decide |

`suggested_notional` is never "don't trade" — on `skip` it is half the trader's median
notional, on `favourable` it is their median. It is a re-size, not a veto.

**Elenchos never blocks, and neither should you.** Report the verdict, quote the numbers,
and place the order if the user still wants it. A gate that refuses gets uninstalled the
first time it is wrong during a fast market, and then protects nobody.

---

## When NOT to use this skill

This section is load-bearing. Gnosis is built to be believed, and it earns that by
refusing to speak when it cannot.

**1. Thin history — fewer than ~30 closed trades, or under 14 days.**
`Summary.is_thin` is `n_trades < 30 or span_days < 14`. On a thin history the gate
short-circuits to `proceed` with the headline *"Not enough history to have an opinion."*
Do not paper over that with general trading advice. Relay it:

```
proceed | Not enough history to have an opinion.
  Only 20 closed trades over 342 days. Gnosis needs more before it can
  tell you anything you should act on.
```

**2. Fewer than 8 comparable trades on any dimension.** Every base rate requires
`MIN_SAMPLE = 8` observations in *both* arms. Below that the analogue is dropped
entirely, and if every analogue drops the verdict is `proceed` with *"Nothing in your
history resembles this closely enough to judge."* That is not an endorsement of the
trade. Say the difference out loud.

**3. Open positions.** Statistics run on **closed round-trips only**. An unrealised loss
is not yet a decision to hold a loser. `summary.n_open` tells you how many are excluded.

**4. Anything forward-looking.** Counterfactuals are *descriptions of the past*: take
matching trades out and re-total. Nothing is re-simulated and no price path is invented.
"You'd be up $2,406" is a statement about trades that already happened, not a forecast.

**5. Price prediction, entries, signals, token selection.** Out of scope entirely. Gnosis
has no market data. Route those elsewhere.

**6. Financial advice.** Gnosis is informational. It does not predict returns. Do not
convert a verdict into a recommendation to buy or sell.

### One calibration limit worth stating

The README's measured **0.00% false-positive rate applies to the profile and the card**,
not to `check`. The detectors behind the profile are significance-gated (bootstrap CI
excluding zero, plus a family-wise correction across 8 hypotheses). **Elenchos is not.**
Its analogues clear only the 8-observation minimum, and its verdict comes from a
severity ratio, `|delta| / (|baseline| + 1.0)`, which is scale-dependent: a trader whose
baseline expectancy sits near zero will draw `skip` verdicts off small dollar deltas. In
a sample of clean synthetic traders, `check` returned `skip` on a majority of neutral
proposals.

Practical consequence: **treat `card` / `profile` findings as evidence, and a `check`
verdict as a prompt to look at the analogue numbers.** Always quote the analogue line
(n, win rate, per-trade expectancy vs baseline) rather than the verdict alone. The
numbers are real; the label above them is coarse.

---

## Using it as a library

The reliable integration for an agent — structured objects, no text parsing:

```python
from datetime import datetime, timezone
from gnosis.model import profile as profile_mod
from gnosis.model.events import History          # build this from your own fills
from gnosis.gate.elenchos import ProposedTrade, check

prof = profile_mod.for_history(history)          # History of Fill events
if prof.summary.is_thin:
    ...                                          # say nothing; there is nothing to say

j = check(prof, ProposedTrade(
    symbol="ETHUSDT", side="buy", notional=4000.0,
    ts=datetime(2026, 9, 1, 3, 14, tzinfo=timezone.utc),
    leverage=20.0,
    adding_to_losing_position=False,
    minutes_since_last_loss=41.0,
))
j.verdict            # "favourable" | "proceed" | "caution" | "skip"
j.is_warning         # True for caution/skip
j.analogues          # [Analogue(dimension, n, expectancy, win_rate, baseline_expectancy)]
j.suggested_notional # float | None
```

`Fill` requires a **timezone-aware** `ts` and a strictly positive `qty`; it raises
`ValueError` otherwise. `leverage=None` on spot and on-chain is meaningful and is not the
same as `1.0`.

### Real histories

Both adapters are reachable straight from the CLI — pass the path, or `baw`:

```bash
./gnosis card my-export.csv          # a Binance CSV export, spot or futures
./gnosis card baw                    # the on-chain wallet (needs `baw`, signed in)
./gnosis check my-export.csv --symbol BTCUSDT --notional 900 --at 2026-09-01T13:30
```

If the CSV will not parse, the error names the columns it could not match and points at
`python3 scripts/inspect_csv.py my-export.csv`, which reports encoding, delimiter, header,
and every failing row — and never itself crashes.

They are also available as library calls when you need to build a `History` yourself:

**Binance trade-history CSV export** (no credentials, no network):

```python
from gnosis.ingest.binance_csv import parse_csv, CsvFormatError
history = parse_csv("my-export.csv")                    # spot or futures layout, auto-detected
history = parse_csv("my-export.csv", fee_prices={"BNB": 612.0})
```

It **refuses rather than guesses**: a row it cannot read raises `CsvFormatError` naming
the line number and the offending value, and a fee paid in a third asset (almost always
BNB, because of the fee discount) raises until you supply `fee_prices`. Do not work
around that by dropping rows — a profile built on a partially-eaten history is biased in
an unknown direction and is worse than no profile. Ask the user for the BNB price
instead. It does not read funding, interest, or transfers, and it recomputes PnL FIFO
rather than trusting the export's own `Realized Profit` (which is preserved in
`Fill.meta`).

**On-chain, via the `baw` CLI:**

```python
from gnosis.ingest.baw_onchain import load, WalletNotConnected
history = load()                     # shells out to `baw wallet status / market-order list / wallet tx-history`
```

Requires a signed-in `baw` session, which requires a human with a phone. Raises
`WalletNotConnected` when there is not one — relay that, do not retry. Only `FINISHED`
swaps become fills; failed swaps still cost gas and become `CashFlow` entries; pending
swaps are neither. Unpriced gas raises rather than silently counting zero.

### The agent layer

`gnosis.agents` is the only place a model runs, and it is **optional**. It writes prose
from facts that were computed and significance-tested first; it is never given the trade
history and is never allowed to reach a conclusion.

```python
from gnosis.agents import narrate_profile, narrate_judgement
copy = narrate_profile(profile, voice="blunt")            # "blunt" | "plain"
call = narrate_judgement(judgement, voice="plain")        # the gate's verdict, in words
```

Both entry points **never raise**. With no API key, no `anthropic` package, a network
error, a schema violation, or a **failed fact check**, they return the deterministic
template and record why in `fallback_reason`; `source` is `"template"` or the model.
`narrate.check_numbers` extracts every number the model wrote and requires each one to be
a number Gnosis already computed — an invented figure throws the whole narration away.
`narrate_judgement` cannot change the verdict; a narration that asserts a different one
is rejected.

**Narration is off by default.** Without `--narrate`, every line the CLI prints is a
deterministic template and no model is contacted. With `--narrate` and an
`ANTHROPIC_API_KEY`, only *computed facts* are sent — the leak titles, findings, totals
and sample sizes — never individual fills, never the raw history. Any number the model
writes that is not already in those facts throws the narration away and falls back to the
template, naming the number it rejected.

---

## Not yet available

Named rather than implied. Do not claim any of this works:

- **Live Binance CEX API ingest from the CLI.** `ingest/binance_mcp.py` exists and is
  tested against canned responses, but it is not reachable from the command line and has
  never been run against the live MCP server. Note also that it reads the isolated
  *Agentic sub-account*, not the main account — a fresh sub-account has no behavioural
  history at all, so for most users the CSV export is the only path that sees their book.
- **`query-token-audit` retro-scan and `square-post` publishing from the CLI.** Both
  exist (`enrich/token_audit.py`, `scripts/publish_card.py`) and are tested, but neither
  is wired into `gnosis` itself. Run the script directly.
- **A `--json` flag on `check`.** The card and the gate print for humans; use
  `./gnosis profile <source>` for machine-readable output, or the library.
- **Entry chase and on-chain slippage detectors.** Symbol competence ships;
  entry-price chasing and swap slippage/gas leakage do not.
- **Profiling a typical spot account.** Not a defect but the most likely disappointment:
  spot investors accumulate and hold, so they rarely have the 30 closed positions Gnosis
  requires. A real spot export spanning years yielded fewer than thirty realised trades
  and was declined. Futures and margin books close, and profile properly.

---

## What is measured

`python3 evals/score.py --traders 40` — 200 traders with an injected leak, each paired
with a **clean twin** built from the same decision list, so anything reported on a twin
is unambiguously noise.

| | |
|---|---|
| recall (the *correct* detector fired) | 95.0% |
| false positives across 200 clean twins | 0.00% |
| bootstrap null rate, measured | 12.5% against a nominal 10% |

Recall by injected effect size: 88.6% subtle (0.6–0.9×), 96.7% clear, 100% severe.
Smaller corpora read differently — at `--traders 8` recall is around 82%, because 8 pairs
per leak is not enough to resolve subtle effects. Quote the 40-trader numbers, and quote
the corpus size with them.

## Disclosures

MIT licensed. Gnosis is an informational tool. It is not investment advice, it does not
predict returns, and its counterfactuals describe the past rather than forecasting the
future.
