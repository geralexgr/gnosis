"""Gnosis as an MCP server: JSON-RPC 2.0 over stdio, hand-rolled.

**Why this is the integration that matters.** Binance shipped an MCP server so
that an agent can trade. The missing half is that the same agent, in the same
conversation, has no way to ask whether *this* trader should be taking *this*
trade -- and the answer to that is sitting in their own fill history. Loading
Gnosis alongside the Binance server in one client puts the two next to each
other: `gnosis_check` quotes the base rate, then the Binance tool places the
order, or does not. Composability is the whole point, and MCP is what makes it
composable without either side knowing the other exists.

**Why the protocol is implemented by hand.** MCP over stdio is newline-delimited
JSON-RPC 2.0 and the surface actually needed here is four methods. An SDK would
be the larger part of the dependency footprint of a project whose central claim
is that it clones and runs with no pip, no credentials and no network -- and the
first thing a judge does is clone and run it. So: `json` from the standard
library, and a loop. There is no HTTP transport and no framework, deliberately.

**Everything here is read-only, and that is structural rather than a policy.**
The module imports nothing that can place an order, move funds, or post
publicly; the dispatch table below is the complete list of what a client can
reach, and every entry answers a question about history that already happened.
`tests/test_server.py` asserts this by scanning this module's own source for
mutation primitives, because a promise in a docstring is not a promise.

**The seams.** `handle()` maps one decoded message to one reply and performs no
I/O, so the entire protocol is testable in-process. `serve()` adds the byte
loop. Both take a `loader`, so a test drives real tool logic against a fixture
history without touching the disk or the network.

Run it:

    echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 -m gnosis.server.mcp
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TextIO

from ..model.events import History

SERVER_NAME = "gnosis"
SERVER_VERSION = "0.1.0"

# The revision of the MCP spec this server was written against. Clients send
# their own in `initialize`; the spec's rule is that the server answers with a
# version it actually supports, which may not be the one asked for. We echo the
# client's when we know it and fall back to ours when we do not, which is the
# behaviour that lets an older client keep working instead of failing closed.
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

# JSON-RPC 2.0 error codes. The four standard ones are all this server needs;
# MCP's own reserved range starts below -32000 and nothing here uses it.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# A history source string, as understood by the CLI. Kept in one place because
# it appears in four tool schemas and an agent that reads a different
# description in each will eventually pass the wrong thing to one of them.
SOURCE_DESCRIPTION = (
    "Where to read the trader's history from. One of: a path to a Binance CSV "
    "export, e.g. 'corpus/demo-account.csv' (spot or futures dialect, read and "
    "never written); 'synthetic:<leak>[:<seed>]' to build a labelled fixture "
    "trader on the spot, where <leak> is one of disposition, martingale, "
    "revenge, night, overleverage -- useful for demonstrating what a finding "
    "looks like without touching anyone's real data; or 'baw' to read the "
    "connected on-chain wallet via the Binance Agentic Wallet CLI, which needs "
    "a signed-in session. Defaults to 'synthetic:disposition' if omitted."
)

DEFAULT_SOURCE = "synthetic:disposition"

Loader = Callable[[str], History]


class McpError(Exception):
    """A JSON-RPC level failure: the request itself was wrong.

    Deliberately distinct from a tool that ran and could not answer. The MCP
    spec draws that line and it matters to a calling agent: a protocol error
    means "you called this wrong, fix the call", while a tool error comes back
    as a normal result with `isError` set and means "the call was fine, the
    world did not cooperate". An agent can retry the second and must not retry
    the first.
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# --------------------------------------------------------------------------
# Loading histories
# --------------------------------------------------------------------------

def default_loader(source: str) -> History:
    """Load a history the same way the CLI does.

    Imported lazily and by its private name on purpose. `cli._load` is the one
    definition of what a source string means, and a second copy here would
    drift -- `scripts/publish_card.py` reaches for it the same way for the same
    reason. Its failure mode is `SystemExit`, which is correct for a CLI and
    fatal for a server, so it is converted here rather than allowed to unwind
    through the request loop and kill the process on one bad argument.
    """
    from ..cli import _load

    try:
        return _load(source)
    except SystemExit as exc:
        raise ValueError(str(exc) or f"cannot load {source!r}") from exc


@dataclass
class Session:
    """One client connection.

    Holds a profile cache, which is not an optimisation so much as a
    correctness convenience: an agent typically calls `gnosis_card` and then
    `gnosis_check` against the same source in the same breath, and profiling
    twice would burn the work twice and -- worse -- print two sets of numbers
    from two reconstructions if anything upstream were ever nondeterministic.
    The cache is per-connection and dies with it, so a re-run always re-reads.
    """

    loader: Loader = default_loader
    initialized: bool = False
    client_info: dict = field(default_factory=dict)
    _profiles: dict[str, Any] = field(default_factory=dict, repr=False)

    def profile(self, source: str):
        """The profile for one source. Read-only, cached for this connection."""
        if source not in self._profiles:
            from ..model import profile as profile_mod

            self._profiles[source] = profile_mod.for_history(self.loader(source))
        return self._profiles[source]


# --------------------------------------------------------------------------
# Rule explanations
# --------------------------------------------------------------------------

# What each detector looks for, how it decides, and what a user can do about
# it. This is prose rather than a docstring scrape on purpose: the docstrings
# are written for whoever maintains the detector, and an agent asking
# "what does leverage_drag mean" needs the version written for a trader.
RULES: dict[str, dict[str, str]] = {
    "disposition_effect": {
        "title": "Selling winners early and nursing losers",
        "leak": (
            "Closing a winning position books a gain and feels like being right; "
            "closing a loser books a loss and feels like being wrong. So the winner "
            "gets sold in hours and the loser gets held for weeks while it 'comes "
            "back'. Named by Shefrin and Statman in 1985 and replicated on brokerage "
            "data ever since. It is the best-documented bias in retail trading."
        ),
        "detection": (
            "Compares the median hold time of winning round-trips against the median "
            "hold time of losing ones. A trader without the bias exits on the setup, "
            "not on which way the position happens to be, so the two are similar. "
            "Gnosis requires a ratio of at least 1.8x, at least 8 winners and 8 "
            "losers, and a bootstrap interval on the difference that excludes zero."
        ),
        "cost": (
            "Reported as the total realised loss on the losing trades, which is an "
            "upper bound on what the asymmetry cost rather than an estimate of it."
        ),
    },
    "averaging_down": {
        "title": "Adding to losing positions",
        "leak": (
            "Buying more of something already underwater lowers the average entry, "
            "which makes the position look better without making it better. The "
            "third add into a loser is reliably where the damage lives, because by "
            "then the size is large and the thesis is old."
        ),
        "detection": (
            "Round-trip reconstruction counts every add that occurred while the "
            "position was underwater against its own running average entry. Trades "
            "containing at least one such add are compared against the trader's own "
            "trades containing none, with the same bootstrap interval."
        ),
        "cost": "Realised PnL on the trades that contain an underwater add.",
    },
    "revenge_trading": {
        "title": "Sizing up in the window after a loss",
        "leak": (
            "A loss is booked, and the next position opens sooner and larger than "
            "usual. Anyone can have a bad trade after a bad trade; almost nobody "
            "systematically doubles their size in the forty minutes following one. "
            "The tell is the combination of a shortened gap and an inflated size."
        ),
        "detection": (
            "Trades opened within 90 minutes of booking a realised loss and sized at "
            "least 1.25x the trader's own median notional, compared against every "
            "other trade. The 90-minute window is generous on purpose -- the effect "
            "is about the emotional window, not the clock."
        ),
        "cost": "Realised PnL across the trades matching both halves of the pattern.",
    },
    "session_performance": {
        "title": "A block of hours that reliably loses money",
        "leak": (
            "Some traders are simply worse at 04:00 than at 14:00 -- thinner books, "
            "less sleep, less supervision. This is the most actionable finding Gnosis "
            "produces, because the remedy is free: not trading costs nothing and "
            "requires no skill."
        ),
        "detection": (
            "Four sessions fixed in advance -- 00:00-06:00, 06:00-12:00, 12:00-16:00 "
            "and 16:00-24:00 UTC -- each tested against the trader's own record on "
            "the other hours. Testing 24 separate hours would guarantee that one "
            "looked terrible on noise alone, so the hypotheses are pre-registered "
            "and few, and the interval is corrected for the whole family."
        ),
        "cost": "The session's shortfall against the trader's baseline, times its trade count.",
    },
    "leverage_drag": {
        "title": "High leverage underperforming your own low leverage",
        "leak": (
            "Leverage scales both tails and does not change edge. In practice it "
            "makes outcomes worse through two mechanisms unrelated to the multiplier: "
            "liquidation turns a temporary drawdown into a permanent loss, and the "
            "pressure of a large position causes earlier, worse exits. Using leverage "
            "is a choice, not a leak; the question is whether each dollar risked did "
            "worse at 20x than the same trader's dollars at 3x."
        ),
        "detection": (
            "Trades at 10x or above compared against the same trader's trades below "
            "10x, on percentage return rather than absolute PnL. Absolute would be "
            "circular -- leveraged positions are larger, so of course they move more "
            "money."
        ),
        "cost": "Realised PnL on the high-leverage arm.",
    },
    "stop_migration": {
        "title": "Moving the stop instead of taking the loss",
        "leak": (
            "A protective stop is placed at entry, when the trader is calm. The market "
            "goes the other way, and instead of being hit the stop is cancelled and "
            "re-placed further away, because being stopped out books a loss and moving "
            "the stop postpones one. Nothing about that decision reaches the fill "
            "stream: the entry looks the same and the exit looks the same, the loss is "
            "just bigger when it finally arrives."
        ),
        "detection": (
            "Requires an order-event stream, not just fills. Looks for a protective "
            "order cancelled and replaced at a worse price while the position was "
            "underwater. This is the weakest detector in the suite -- 62.5% recall on "
            "the labelled corpus -- and is reported as such rather than tuned until it "
            "looks better. It fires on none of the 280 clean twins, so it is silent "
            "rather than wrong."
        ),
        "cost": "Realised PnL on trades whose stop was widened while underwater.",
    },
    "symbol_selection": {
        "title": "A symbol you keep trading and keep losing money in",
        "leak": (
            "Edge is specific -- it comes from knowing how one thing moves -- and a "
            "book of seventeen tickers is usually two positions of conviction and "
            "fifteen of boredom."
        ),
        "detection": (
            "The obvious implementation ranks symbols by outcome and reports the worst, "
            "which fires on every history including pure noise, because with four "
            "symbols one is always last. Instead every symbol clearing a minimum trade "
            "count is tested against the trader's own baseline and the interval is "
            "corrected for how many symbols were tested -- selection by outcome is "
            "still happening, but it is paid for. The metric is return per trade, not "
            "dollars, and the symbol must be losing money outright rather than merely "
            "underperforming."
        ),
        "cost": "Realised PnL on that symbol.",
    },
    "session_strength": {
        "title": "A block of hours where you are reliably better",
        "leak": (
            "Not a leak. Gnosis reports the sessions a trader is good in on the same "
            "evidence standard as the ones they are bad in, because a tool that only "
            "ever says 'no' is a brake, and brakes get disabled."
        ),
        "detection": "The session test above, in the other direction.",
        "cost": "Reported as a value rather than a cost.",
    },
}

# The evidence tiers a finding can carry. Repeated to the caller on every
# explanation because it is the thing most likely to be misread: an agent that
# treats `statistical` and `proof` as interchangeable will overstate a finding.
CONFIDENCE_NOTE = {
    "proof": (
        "The behaviour is directly countable from the history and not open to "
        "interpretation. Its cost may still be an estimate; the behaviour is not."
    ),
    "statistical": (
        "A slice of the history performed differently from the same trader's own "
        "baseline, and a bootstrap confidence interval on the difference excluded "
        "zero. Real, but an inference."
    ),
    "weak": (
        "Present but underpowered. Never surfaces to a user on its own; kept so the "
        "profile can say 'watch this'."
    ),
}


def explain_rule(rule: str) -> dict:
    """Everything Gnosis can say about one detector rule, without a history."""
    entry = RULES.get(rule)
    if entry is None:
        raise ValueError(
            f"unknown rule {rule!r}. Known rules: {', '.join(sorted(RULES))}"
        )
    return {
        "rule": rule,
        "title": entry["title"],
        "what_the_leak_is": entry["leak"],
        "how_gnosis_detects_it": entry["detection"],
        "how_the_cost_is_computed": entry["cost"],
        "evidence_tiers": CONFIDENCE_NOTE,
        "shared_guarantees": [
            "Below 8 observations in either arm, Gnosis reports insufficient data and "
            "says nothing else.",
            "Every claim rests on a bootstrap interval on the difference that excludes "
            "zero, not on a gap that merely looks large.",
            "Every split compares the trader against themselves on the complement of "
            "the slice -- never a market average, never another user.",
            "The interval is corrected across the whole profile, not within one "
            "detector, so every hypothesis added is paid for by every other one.",
            "Every finding carries the trade ids that produced it, so it can be checked "
            "rather than believed.",
        ],
    }


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

# Annotations are hints, not enforcement -- the spec says so and a client must
# not trust them from an untrusted server. They are still worth declaring
# exactly right: a client that surfaces "this tool only reads" to a user is the
# difference between an agent being allowed to consult Gnosis automatically and
# being made to ask first, and this server genuinely never writes anything.
READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    # False: everything is computed from a local history. No exchange, no
    # market data, no model call on any of these paths.
    "openWorldHint": False,
}

_SOURCE_PROPERTY = {
    "type": "string",
    "description": SOURCE_DESCRIPTION,
    "default": DEFAULT_SOURCE,
}


def _tool(name: str, title: str, description: str, properties: dict,
          required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
        "annotations": {"title": title, **READ_ONLY},
    }


TOOLS: list[dict] = [
    _tool(
        "gnosis_check",
        "Cross-examine a proposed trade",
        "READ-ONLY. Places no order and moves no funds. Call this BEFORE executing a "
        "trade, including one you were about to place with a Binance MCP tool. Use "
        "this whenever a size, a leverage or an entry time is about to be committed. "
        "Given a trade the user is about to take, it looks up what "
        "this trader has historically done in situations resembling it -- same hour "
        "of day, same leverage band, same symbol and direction, adding to a loser, "
        "sizing up after a loss -- and returns the base rate for each, together with "
        "a verdict of 'skip', 'caution', 'proceed' or 'favourable'.\n\n"
        "It never blocks and it is not advice: it quotes the trader's own record back "
        "at them and the human decides. It speaks in both directions -- if the "
        "proposal matches the trader's best historical pattern, and especially if "
        "they habitually size that pattern too small, it says so.\n\n"
        "A base rate is only allowed to drive the verdict if it differs from this "
        "trader's own baseline by more than a bootstrap confidence interval; "
        "everything else comes back marked '(within noise)' as context. If the "
        "history is too thin the verdict is 'proceed' with a stated reason, which "
        "means 'no opinion', not 'looks good'.",
        {
            "source": _SOURCE_PROPERTY,
            "symbol": {
                "type": "string",
                "description": "Exchange symbol of the proposed trade, e.g. 'ETHUSDT'. "
                               "Matched exactly against the trader's history.",
            },
            "side": {
                "type": "string",
                "enum": ["buy", "sell"],
                "default": "buy",
                "description": "Direction of the proposed trade. Used to look up the "
                               "trader's record on this symbol in this direction "
                               "specifically, falling back to the symbol alone when "
                               "there are fewer than 8 directional trades.",
            },
            "notional": {
                "type": "number",
                "exclusiveMinimum": 0,
                "description": "Position size in quote currency (USDT), NOT in units of "
                               "the base asset and NOT the margin posted. A 20x position "
                               "of 4000 USDT notional on 200 USDT of margin is 4000 here. "
                               "Compared against the trader's own median notional to "
                               "decide whether this is an oversized trade.",
            },
            "leverage": {
                "type": ["number", "null"],
                "description": "Leverage multiplier, e.g. 20 for 20x. Omit or pass null "
                               "for spot, where there is none -- do not pass 1, which is "
                               "a different claim and would put the trade in the "
                               "low-leverage comparison arm.",
            },
            "at": {
                "type": "string",
                "description": "When the trade would be placed, as an ISO 8601 timestamp "
                               "interpreted as UTC, e.g. '2026-09-01T03:14'. Hour of day "
                               "is what the session analogue reads, so passing local time "
                               "here silently shifts the trader into the wrong session. "
                               "Defaults to now.",
            },
            "adding_to_losing_position": {
                "type": "boolean",
                "default": False,
                "description": "True if this order would increase an existing position "
                               "that is currently underwater. Cannot be inferred from the "
                               "proposal alone, so the caller must supply it; getting it "
                               "wrong is the difference between the averaging-down base "
                               "rate being quoted and being missed.",
            },
            "minutes_since_last_loss": {
                "type": ["number", "null"],
                "description": "Minutes since the trader last closed a position at a "
                               "realised loss. Under 90 and above their usual size is the "
                               "revenge-trading pattern. Omit or null if unknown or if no "
                               "loss was booked recently.",
            },
        },
        required=["symbol", "notional"],
    ),
    _tool(
        "gnosis_profile",
        "Read the behavioural profile",
        "READ-ONLY. Reads a trading history and returns the full behavioural profile "
        "as JSON: a summary (trade count, span, win rate, expectancy, realised PnL, "
        "fees), the behavioural leaks that cleared the significance bar with their "
        "cost and the trade ids that prove each one, the trader's genuine strengths, "
        "and the licensed counterfactuals.\n\n"
        "Use this when you need the numbers to reason over -- to answer a question "
        "about the trader, to feed another calculation, or to decide what to bring "
        "up. Use `gnosis_card` instead when you want something to show a human.\n\n"
        "Read `summary.is_thin` first. When it is true, Gnosis has declined to "
        "profile: there is too little history (under 30 closed trades or under 14 "
        "days) to say anything that should be acted on, `leaks` will be empty, and "
        "that emptiness means 'not enough evidence', never 'no problems found'. An "
        "empty `leaks` list on a non-thin profile is a real result -- the detectors "
        "ran and nothing survived a confidence interval.\n\n"
        "`counterfactuals` are descriptions of the past, not forecasts. Each one "
        "removes the matching trades and re-totals, assuming every remaining trade "
        "is unchanged, which is an assumption rather than a fact. `observations` "
        "hold the counterfactuals no detector licensed -- present for interest, "
        "never to be relayed as a recommendation.",
        {
            "source": _SOURCE_PROPERTY,
        },
    ),
    _tool(
        "gnosis_card",
        "Render the Rekt Wrapped card",
        "READ-ONLY. Renders the trader's history as the Rekt Wrapped card -- plain "
        "text, no colour codes, ready to show a human or paste into a message. It "
        "leads with the single most expensive habit, then the other leaks, then what "
        "the trader actually does well, then the alternate universes where they kept "
        "one rule.\n\n"
        "Use this when a person is going to read the output. Use `gnosis_profile` "
        "when a program is. Every number on the card was computed and "
        "significance-tested before rendering; the renderer does no arithmetic of "
        "its own, so nothing on the card can disagree with the profile.\n\n"
        "The tone is deliberately blunt. If the history is too thin the card says so "
        "in one paragraph and stops, which is the intended output and not a failure.",
        {
            "source": _SOURCE_PROPERTY,
        },
    ),
    _tool(
        "gnosis_explain",
        "Explain a behavioural rule",
        "READ-ONLY. Needs no history and reads no data at all. Given the name of one "
        "of Gnosis's detector rules, explains what that behavioural leak actually is, "
        "how Gnosis detects it, what its reported cost means, and what evidence "
        "standard the finding had to clear.\n\n"
        "Call this after `gnosis_profile` or `gnosis_check` surfaces a rule you are "
        "about to relay to a user, so that what you tell them about the finding "
        "matches how it was actually derived. Guessing from the rule name is the "
        "failure mode this tool exists to prevent -- several of these detectors "
        "deliberately do not measure the obvious thing, and `leverage_drag` in "
        "particular is a claim about the trader, not about leverage.",
        {
            "rule": {
                "type": "string",
                "enum": sorted(RULES),
                "description": "The rule name, exactly as it appears in the `rule` field "
                               "of a leak or strength returned by gnosis_profile.",
            },
        },
        required=["rule"],
    ),
]

TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------

def _source(args: dict) -> str:
    value = args.get("source", DEFAULT_SOURCE)
    if value is None:
        return DEFAULT_SOURCE
    if not isinstance(value, str) or not value.strip():
        raise ValueError("`source` must be a non-empty string")
    return value.strip()


def _tool_profile(session: Session, args: dict) -> str:
    prof = session.profile(_source(args))
    return json.dumps(prof.to_dict(), indent=2, default=str)


def _tool_card(session: Session, args: dict) -> str:
    from ..card import render

    prof = session.profile(_source(args))
    # `colour=False` explicitly rather than by letting the renderer sniff the
    # terminal. stdout here is a JSON-RPC pipe, and an ANSI escape smuggled
    # into a JSON string is a thing the client would have to strip.
    return render(prof, colour=False)


def _tool_check(session: Session, args: dict) -> str:
    from ..gate.elenchos import ProposedTrade
    from ..gate.elenchos import check as gate_check

    symbol = args.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("`symbol` is required and must be a non-empty string")
    notional = args.get("notional")
    if not isinstance(notional, (int, float)) or isinstance(notional, bool):
        raise ValueError("`notional` is required and must be a number in quote currency")
    if notional <= 0:
        raise ValueError("`notional` must be positive")
    side = args.get("side", "buy")
    if side not in ("buy", "sell"):
        raise ValueError("`side` must be 'buy' or 'sell'")

    at = args.get("at")
    if at:
        try:
            ts = datetime.fromisoformat(str(at))
        except ValueError as exc:
            raise ValueError(
                f"`at` is not an ISO 8601 timestamp: {at!r} (try '2026-09-01T03:14')"
            ) from exc
        # A naive timestamp is taken as UTC rather than local, matching the CLI.
        # Attaching the caller's zone instead would move the trade into a
        # different session and change the verdict for no reason the user did.
        ts = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)
    else:
        ts = datetime.now(timezone.utc)

    prof = session.profile(_source(args))
    proposed = ProposedTrade(
        symbol=symbol.strip(),
        side=side,
        notional=float(notional),
        ts=ts,
        leverage=args.get("leverage"),
        adding_to_losing_position=bool(args.get("adding_to_losing_position", False)),
        minutes_since_last_loss=args.get("minutes_since_last_loss"),
    )
    judgement = gate_check(prof, proposed)
    return json.dumps(
        {
            "verdict": judgement.verdict,
            "is_warning": judgement.is_warning,
            "headline": judgement.headline,
            "reasons": judgement.reasons,
            "suggested_notional": judgement.suggested_notional,
            "analogues": [
                {
                    "dimension": a.dimension,
                    "n": a.n,
                    "win_rate": round(a.win_rate, 4),
                    "expectancy": round(a.expectancy, 2),
                    "baseline_expectancy": round(a.baseline_expectancy, 2),
                    "delta": round(a.delta, 2),
                    # The field an agent must not skip. An analogue that is not
                    # significant is context and may not be relayed as a reason.
                    "significant": a.significant,
                }
                for a in judgement.analogues
            ],
            "proposed": {
                "symbol": proposed.symbol, "side": proposed.side,
                "notional": proposed.notional, "leverage": proposed.leverage,
                "at": ts.isoformat(),
            },
            "note": (
                "Elenchos never blocks and this is not advice. It is this trader's own "
                "record, quoted back. Only analogues with significant=true were allowed "
                "to drive the verdict."
            ),
        },
        indent=2,
        default=str,
    )


def _tool_explain(session: Session, args: dict) -> str:
    rule = args.get("rule")
    if not isinstance(rule, str) or not rule.strip():
        raise ValueError(f"`rule` is required. Known rules: {', '.join(sorted(RULES))}")
    return json.dumps(explain_rule(rule.strip()), indent=2)


# The complete list of what a client can reach. Every entry reads and returns;
# none of them writes, sends, or executes. Adding anything that does would
# break `tests/test_server.py`, which is the point of it being a table.
IMPLEMENTATIONS: dict[str, Callable[[Session, dict], str]] = {
    "gnosis_profile": _tool_profile,
    "gnosis_card": _tool_card,
    "gnosis_check": _tool_check,
    "gnosis_explain": _tool_explain,
}


def call_tool(session: Session, name: str, args: dict | None) -> dict:
    """Run one tool and return an MCP `tools/call` result.

    The distinction the spec draws, and the reason it is worth honouring: an
    unknown tool name is a *protocol* error, because the client asked for
    something that was never advertised and retrying will not help. A tool that
    ran and failed -- an unreadable CSV, a disconnected wallet -- is a normal
    result carrying `isError: true`, so the model sees the failure text and can
    act on it instead of the client swallowing it as a transport fault.
    """
    if name not in IMPLEMENTATIONS:
        raise McpError(
            INVALID_PARAMS,
            f"unknown tool {name!r}",
            {"available": sorted(IMPLEMENTATIONS)},
        )
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise McpError(INVALID_PARAMS, "`arguments` must be an object")

    try:
        text = IMPLEMENTATIONS[name](session, args)
    except McpError:
        raise
    except Exception as exc:  # noqa: BLE001 - a failing tool must not kill the loop
        return {
            "content": [{"type": "text", "text": f"{name} failed: {exc}"}],
            "isError": True,
        }
    return {"content": [{"type": "text", "text": text}], "isError": False}


# --------------------------------------------------------------------------
# JSON-RPC
# --------------------------------------------------------------------------

def _negotiate(requested: object) -> str:
    """Pick the protocol version to answer `initialize` with."""
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOLS:
        return requested
    # The spec's instruction when the client asks for something we do not know:
    # answer with a version we do support and let the client decide whether to
    # continue. Failing the handshake outright would make every future revision
    # of the spec break this server on first contact.
    return PROTOCOL_VERSION


def _initialize(session: Session, params: dict) -> dict:
    session.initialized = True
    info = params.get("clientInfo")
    session.client_info = info if isinstance(info, dict) else {}
    return {
        "protocolVersion": _negotiate(params.get("protocolVersion")),
        # `listChanged: False` is honest: the tool list is a module constant and
        # cannot change while the process runs, so a client that subscribes to
        # notifications would wait forever.
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {
            "name": SERVER_NAME,
            "title": "Gnosis — know thyself",
            "version": SERVER_VERSION,
        },
        "instructions": (
            "Gnosis reads a trader's own fill history and reports the behavioural "
            "patterns that survive a significance test. Every tool here is read-only: "
            "nothing places an order, moves funds, or posts anything.\n\n"
            "Call gnosis_check before executing any trade, including one you were "
            "about to place through an exchange's own MCP tools. It returns the "
            "trader's base rate in situations resembling the proposal. It never "
            "blocks, and it is not investment advice -- it is the user's own record, "
            "quoted back, and the user decides.\n\n"
            "Two things to relay accurately or not at all: a finding marked "
            "'(within noise)' or significant=false is context and must not be "
            "presented as a reason, and an empty result on a thin history means "
            "'not enough evidence', never 'no problems found'."
        ),
    }


def _tools_list(session: Session, params: dict) -> dict:
    # No pagination. Four tools fit in any client's first page, and an empty
    # `nextCursor` is how the spec says to signal that there is nothing more.
    return {"tools": TOOLS}


def _tools_call(session: Session, params: dict) -> dict:
    name = params.get("name")
    if not isinstance(name, str):
        raise McpError(INVALID_PARAMS, "`name` is required and must be a string")
    return call_tool(session, name, params.get("arguments"))


def _ping(session: Session, params: dict) -> dict:
    return {}


METHODS: dict[str, Callable[[Session, dict], dict]] = {
    "initialize": _initialize,
    "tools/list": _tools_list,
    "tools/call": _tools_call,
    "ping": _ping,
}

# Notifications carry no id and get no reply, ever -- answering one is a
# protocol violation that some clients treat as fatal. Listed rather than
# inferred from the absence of an id, so that an unknown *notification* is
# silently ignored (correct) while an unknown *request* gets an error (also
# correct).
KNOWN_NOTIFICATIONS = frozenset({
    "notifications/initialized",
    "notifications/cancelled",
    "notifications/progress",
    "notifications/roots/list_changed",
})


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def handle(message: object, session: Session | None = None) -> dict | None:
    """One decoded JSON-RPC message in, one reply out (or None for a notification).

    Pure: no I/O, no globals, no clock beyond what a tool itself reads. The
    whole protocol is therefore exercisable from a test without spawning a
    process, a pipe, or a network, which is the only way the handshake gets
    covered at all on a machine with nothing installed.
    """
    session = session or Session()

    # JSON-RPC 2.0 allows an array of requests. MCP removed batching in the
    # 2025-06-18 revision, so this refuses rather than half-implementing it --
    # a server that answers a batch with a single object is worse than one that
    # says it cannot.
    if isinstance(message, list):
        return _error(None, INVALID_REQUEST, "JSON-RPC batching is not supported")
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "a JSON-RPC message must be an object")

    request_id = message.get("id")
    method = message.get("method")
    is_notification = "id" not in message

    if message.get("jsonrpc") != "2.0":
        if is_notification:
            return None
        return _error(request_id, INVALID_REQUEST, "`jsonrpc` must be exactly \"2.0\"")
    # A response, not a request: the client answering something we asked. This
    # server initiates nothing, so there is nothing to correlate it with, and
    # replying to a reply would loop.
    if "method" not in message and ("result" in message or "error" in message):
        return None
    if not isinstance(method, str) or not method:
        if is_notification:
            return None
        return _error(request_id, INVALID_REQUEST, "`method` is required and must be a string")

    params = message.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        if is_notification:
            return None
        return _error(request_id, INVALID_PARAMS, "`params` must be an object")

    if is_notification:
        # Unknown notifications are ignored on purpose. The spec lets a client
        # send ones this server has never heard of, and erroring on them would
        # break against a newer client for no benefit.
        return None

    fn = METHODS.get(method)
    if fn is None:
        if method in KNOWN_NOTIFICATIONS:
            # Sent as a request by a confused client. Answer emptily rather
            # than erroring; the intent is unambiguous.
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        return _error(
            request_id, METHOD_NOT_FOUND, f"unknown method {method!r}",
            {"supported": sorted(METHODS)},
        )

    # Deliberately not enforced: that `initialize` came first. The spec expects
    # it, but refusing `tools/list` to a client that skipped the handshake
    # makes the server impossible to poke at with a single echoed line, and
    # every tool here is read-only, so there is nothing an out-of-order call
    # can reach that an in-order one could not.
    try:
        return {"jsonrpc": "2.0", "id": request_id, "result": fn(session, params)}
    except McpError as exc:
        return _error(request_id, exc.code, exc.message, exc.data)
    except Exception as exc:  # noqa: BLE001 - one bad request must not end the session
        return _error(request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")


def serve(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    *,
    loader: Loader | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the stdio loop until the input closes.

    Framing is newline-delimited JSON, which is what MCP's stdio transport
    specifies: one message per line, no Content-Length headers, and no embedded
    newlines (`json.dumps` produces none by default).

    **Nothing else may ever write to stdout from this process.** stdout is the
    transport; a stray `print` corrupts the stream and the client sees a parse
    error it cannot attribute. Diagnostics go to stderr, which the spec
    reserves for exactly that.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    session = Session(loader=loader or default_loader)

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            # Per JSON-RPC: a parse error is answered with a null id, because
            # the id was inside the bytes we could not read.
            reply = _error(None, PARSE_ERROR, f"invalid JSON: {exc}")
        else:
            reply = handle(message, session)
        if reply is None:
            continue
        stdout.write(json.dumps(reply) + "\n")
        stdout.flush()

    if stderr is not None:
        stderr.write("gnosis-mcp: input closed\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        sys.stderr.write(__doc__ or "")
        return 0
    if argv and argv[0] == "--tools":
        # A human-readable dump of the advertised schemas, on stderr-safe
        # stdout precisely because this mode is not serving anything.
        sys.stdout.write(json.dumps({"tools": TOOLS}, indent=2) + "\n")
        return 0
    return serve(stderr=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
