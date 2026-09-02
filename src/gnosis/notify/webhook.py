"""Post a judgement or a profile summary to Slack, Discord, or a plain webhook.

The useful shape of Gnosis in a team is not a card someone renders once. It is
a line in a channel at the moment a gate returns `skip`, so that the decision
and its evidence are in the same place as the trade. That is what this module
is for, and it is deliberately the smallest thing that does it.

**The destination decides the format, and the URL decides the destination.**
Slack wants Block Kit, Discord wants embeds, and everything else wants JSON it
can shape itself. Asking the caller to choose is asking them to get it wrong;
the hostname already says which one it is, so `detect_flavour` reads it. A URL
that matches nothing known is `generic` rather than an error — an unknown host
is far more likely to be a proxy, a relay or a self-hosted collector than a
mistake, and refusing to post to it would be refusing the common case.

**Nothing sends without `send=True`.** The default returns the exact bytes and
the exact destination and opens no socket. See the package docstring; this is
the whole reason the module has a `Delivery` return type instead of returning
a status code.

**Currency is opt-in.** `show_amounts=False` suppresses absolute figures at the
point the payload is *built*, not at the point it is rendered, so there is no
formatter that can leak one by forgetting. Percentages, win rates, trade counts
and verdicts always go: those are the part that makes the message useful, and
they are not the part that is anyone else's business. Redaction of the prose
uses the same deny-by-default rule as `report/html.py` and
`scripts/publish_card.py`.

**The transport is injected.** `Transport` takes a URL, a body and headers, and
returns `(status, text)`. The default one is built on `urllib.request` from the
standard library, imported inside the function so that importing this module
never pulls in the networking stack at all. Tests pass a spy and no socket is
ever created.

    from gnosis.notify import notify_judgement

    dry = notify_judgement(url, judgement, symbol="ETHUSDT")   # nothing sent
    print(dry.payload)
    live = notify_judgement(url, judgement, symbol="ETHUSDT", send=True)
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

from ..gate.elenchos import Judgement
from ..model.profile import Profile
from ..report.html import redact

Flavour = Literal["slack", "discord", "generic"]

# (url, body, headers) -> (status_code, response_text)
Transport = Callable[[str, bytes, Mapping[str, str]], "tuple[int, str]"]

DEFAULT_TIMEOUT = 15.0

# Hosts whose payload shape we know. Matched on the hostname rather than on a
# substring of the whole URL, so a path or a query parameter that happens to
# contain "slack" cannot redirect a Discord payload to a Slack formatter.
_SLACK_HOSTS = ("hooks.slack.com", "slack.com")
_DISCORD_HOSTS = ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com")

# What each verdict looks like at a glance. Text, not images: an emoji renders
# in every client and a hosted icon would be a network fetch and a tracking
# pixel in the same request.
MARK = {"skip": "✗", "caution": "!", "proceed": "·", "favourable": "✓"}
COLOUR = {
    "skip": 0xB3261E, "caution": 0xC98A24, "proceed": 0x6B6357, "favourable": 0x1F7A4D,
}


class WebhookError(RuntimeError):
    """The destination refused, or the URL is not one we will post to."""


@dataclass
class Delivery:
    """The outcome of a notify call — including, and especially, a dry run.

    `sent` is the field that matters and it is False by default. A caller that
    wants to know whether anything actually left the machine reads this rather
    than inferring it from the absence of an exception, because a dry run
    succeeds too.
    """

    url: str
    flavour: Flavour
    payload: dict
    sent: bool = False
    status: int | None = None
    response: str = ""
    reason: str = ""
    headers: dict = field(default_factory=dict)

    @property
    def body(self) -> bytes:
        """Exactly the bytes that would be, or were, put on the wire."""
        return json.dumps(self.payload).encode("utf-8")

    def describe(self) -> str:
        """A human-readable dry-run preview."""
        head = "SENT" if self.sent else "DRY RUN — nothing was sent"
        return (
            f"{head}\n  to      {self.url}\n  format  {self.flavour}\n"
            f"  reason  {self.reason}\n  body    {json.dumps(self.payload, indent=2)}"
        )


def detect_flavour(url: str) -> Flavour:
    """Which payload shape this URL expects, from its hostname."""
    host = (urlparse(url).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in _SLACK_HOSTS):
        return "slack"
    if any(host == h or host.endswith("." + h) for h in _DISCORD_HOSTS):
        return "discord"
    return "generic"


def _require_https(url: str) -> None:
    """Refuse to post over plain HTTP.

    A judgement names a symbol, a size and a verdict about a specific person's
    trading, and there is no version of this that should cross a network in
    clear text. `localhost` is exempt because it does not cross one.
    """
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and (parsed.hostname or "") in ("localhost", "127.0.0.1", "::1"):
        return
    raise WebhookError(
        f"refusing to post to {parsed.scheme or 'a schemeless'} URL {url!r}. "
        "A judgement names a trader, a symbol and a size; use https (or localhost "
        "for a local collector)"
    )


# --------------------------------------------------------------------------
# Facts -> a neutral message, before any destination formatting
# --------------------------------------------------------------------------

@dataclass
class Message:
    """One notification, before it is shaped for a particular destination.

    Built once and formatted three ways, so Slack and Discord cannot end up
    quoting different numbers from the same judgement.
    """

    title: str
    summary: str
    lines: list[str]
    verdict: str | None = None
    facts: dict = field(default_factory=dict)


def _clean(text: str, *, show_amounts: bool) -> str:
    return text if show_amounts else redact(text)


def judgement_message(
    judgement: Judgement,
    *,
    symbol: str,
    notional: float | None = None,
    show_amounts: bool = False,
) -> Message:
    """A pre-trade verdict, as a message.

    The `(within noise)` marker that `elenchos` puts on non-significant
    analogues is carried through verbatim. Dropping it to save a line would
    turn context into evidence, which is the exact failure the gate's own
    significance test exists to prevent.
    """
    mark = MARK.get(judgement.verdict, "·")
    size = ""
    if notional is not None and show_amounts:
        size = f" · {notional:,.0f}"
    lines = [_clean(reason, show_amounts=show_amounts) for reason in judgement.reasons]
    if judgement.suggested_notional is not None and show_amounts:
        lines.append(
            f"Your record suggests {judgement.suggested_notional:,.0f}, not {notional:,.0f}."
        )
    elif judgement.suggested_notional is not None:
        lines.append("Your record suggests a smaller size than the one proposed.")
    lines.append("Elenchos never blocks. This is your own history, quoted back.")
    return Message(
        title=f"{mark} {judgement.verdict.upper()} — {symbol}{size}",
        summary=_clean(judgement.headline, show_amounts=show_amounts),
        lines=lines,
        verdict=judgement.verdict,
        facts={
            "symbol": symbol,
            "verdict": judgement.verdict,
            "is_warning": judgement.is_warning,
            "analogues": [
                {
                    "dimension": a.dimension,
                    "n": a.n,
                    "win_rate": round(a.win_rate, 4),
                    "significant": a.significant,
                }
                for a in judgement.analogues
            ],
        },
    )


def profile_message(profile: Profile, *, show_amounts: bool = False) -> Message:
    """A profile summary, as a message.

    Deliberately not the whole card. A channel post is a pointer: the headline
    habit, the count behind it, and enough for someone to decide whether to go
    and read the report. Pasting a full card into a channel is how a tool gets
    muted.
    """
    s = profile.summary
    if s.is_thin:
        return Message(
            title="Gnosis — not enough history",
            summary=(
                f"{s.n_trades} closed trades across {s.span_days:.0f} days is below the "
                f"bar, so Gnosis has no opinion. That is a result, not a failure."
            ),
            lines=[],
            facts={"is_thin": True, "n_trades": s.n_trades},
        )

    header = f"{s.n_trades} trades · {s.span_days:.0f} days · {s.win_rate:.0%} win rate"
    if show_amounts:
        header += f" · net {s.total_pnl:+,.0f} · expectancy {s.expectancy:+,.0f}/trade"

    worst = profile.worst_leak
    summary = (
        _clean(f"{worst.title}. {worst.finding}", show_amounts=show_amounts)
        if worst is not None
        else "No behavioural leak cleared the significance bar — the detectors ran "
             "and nothing survived a confidence interval."
    )
    lines = []
    for leak in profile.leaks:
        if leak is worst:
            continue
        cost = f"{leak.cost:+,.0f}  " if (show_amounts and leak.cost is not None) else ""
        # "n=47" would be read as an amount by a deny-by-default redactor, which
        # is the correct call for a bare number and the wrong outcome for a
        # sample size. Spelling the unit out is what keeps the count.
        lines.append(f"{cost}{leak.title} ({leak.confidence}, {leak.n} trades)")
    for st in profile.strengths:
        lines.append(f"✓ {st.title}")

    return Message(
        title="Rekt Wrapped — γνῶθι σεαυτόν",
        summary=summary,
        lines=[header, *lines],
        facts={
            "is_thin": False,
            "n_trades": s.n_trades,
            "win_rate": round(s.win_rate, 4),
            "n_leaks": len(profile.leaks),
            "rules": [leak.rule for leak in profile.leaks],
        },
    )


# --------------------------------------------------------------------------
# Destination formatting
# --------------------------------------------------------------------------

def _slack(message: Message) -> dict:
    """Block Kit.

    `text` is set as well as `blocks` because Slack uses it for the
    notification preview and the accessibility fallback; a blocks-only payload
    shows up in the sidebar as "This content can't be displayed".
    """
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": message.title,
                                    "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": message.summary}},
    ]
    if message.lines:
        blocks.append({"type": "section", "text": {
            "type": "mrkdwn",
            "text": "\n".join(f"• {line}" for line in message.lines),
        }})
    blocks.append({"type": "context", "elements": [{
        "type": "mrkdwn",
        "text": "_Gnosis · computed from this trader's own fills, "
                "significance-tested · not investment advice_",
    }]})
    return {"text": f"{message.title} — {message.summary}", "blocks": blocks}


def _discord(message: Message) -> dict:
    """An embed.

    Discord rejects an embed description over 4096 characters and a field value
    over 1024, and it rejects the whole message rather than truncating. Trimmed
    here so a long profile does not turn into a silent 400.
    """
    embed: dict[str, Any] = {
        "title": message.title[:256],
        "description": message.summary[:4096],
        "color": COLOUR.get(message.verdict or "", 0x8A6D3B),
        "footer": {"text": "Gnosis · your own fills, significance-tested · "
                           "not investment advice"},
    }
    if message.lines:
        embed["fields"] = [{
            "name": "Your record",
            "value": "\n".join(f"• {line}" for line in message.lines)[:1024],
        }]
    return {"content": message.title, "embeds": [embed]}


def _generic(message: Message) -> dict:
    """Plain JSON, for anything that is not Slack or Discord.

    Structured rather than pre-rendered: a collector on the other end wants the
    verdict as a field it can route on, not a sentence it has to parse. The
    rendered text is included too, so a receiver that just wants to print
    something can.
    """
    return {
        "source": "gnosis",
        "title": message.title,
        "summary": message.summary,
        "detail": message.lines,
        "facts": message.facts,
        "text": "\n".join([message.title, message.summary, *message.lines]),
    }


FORMATTERS: dict[str, Callable[[Message], dict]] = {
    "slack": _slack,
    "discord": _discord,
    "generic": _generic,
}


def build_payload(message: Message, flavour: Flavour) -> dict:
    """Shape one message for one destination."""
    formatter = FORMATTERS.get(flavour)
    if formatter is None:
        raise WebhookError(f"unknown webhook flavour {flavour!r}")
    return formatter(message)


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------

def urllib_transport(
    url: str, body: bytes, headers: Mapping[str, str], *, timeout: float = DEFAULT_TIMEOUT
) -> tuple[int, str]:
    """The default transport: one POST, no retries, standard library only.

    `urllib` is imported here rather than at module scope so that importing
    `gnosis.notify` never pulls in the networking stack — which also means a
    test that imports this module cannot accidentally acquire the ability to
    make a request.

    No retries on purpose. A webhook that failed because the channel is gone
    will fail again, and a webhook that failed after the message arrived would
    be posted twice by a retry. Duplicating a verdict in a channel is worse
    than missing one.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(  # noqa: S310 - scheme is checked by _require_https
        url, data=body, headers=dict(headers), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read().decode("utf-8", "replace")[:2000]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:2000]
    except OSError as exc:
        raise WebhookError(f"POST to {url} failed: {exc}") from exc


def send_payload(
    url: str,
    payload: dict,
    *,
    flavour: Flavour,
    send: bool = False,
    transport: Transport | None = None,
) -> Delivery:
    """Post a payload, or -- by default -- do not.

    The `send` parameter has no default that sends, at any layer of this
    module, and there is no environment variable that flips it. The only way a
    request leaves this process is a caller passing `send=True` at the call
    site, where whoever wrote it can see it.
    """
    headers = {"Content-Type": "application/json", "User-Agent": "gnosis/0.1"}
    delivery = Delivery(url=url, flavour=flavour, payload=payload, headers=headers)
    if not send:
        delivery.reason = (
            "dry run: send=True was not passed, so nothing was transmitted. "
            "The body above is exactly what would have been sent."
        )
        return delivery

    _require_https(url)
    post = transport or urllib_transport
    status, text = post(url, delivery.body, headers)
    delivery.status = status
    delivery.response = text
    # Slack answers 200 with the literal body "ok"; Discord answers 204 with
    # nothing. Anything outside 2xx is a failure and is raised rather than
    # returned, because a caller that ignored a silent 403 would believe a
    # verdict had been delivered that nobody ever saw.
    if not 200 <= status < 300:
        raise WebhookError(f"{flavour} webhook returned {status}: {text[:300]}")
    delivery.sent = True
    delivery.reason = f"sent, {status}"
    return delivery


# --------------------------------------------------------------------------
# The two entry points
# --------------------------------------------------------------------------

def notify_judgement(
    url: str,
    judgement: Judgement,
    *,
    symbol: str,
    notional: float | None = None,
    send: bool = False,
    show_amounts: bool = False,
    transport: Transport | None = None,
    flavour: Flavour | None = None,
) -> Delivery:
    """Post a pre-trade verdict. Dry run unless `send=True`."""
    message = judgement_message(
        judgement, symbol=symbol, notional=notional, show_amounts=show_amounts
    )
    shape = flavour or detect_flavour(url)
    return send_payload(
        url, build_payload(message, shape), flavour=shape, send=send, transport=transport
    )


def notify_profile(
    url: str,
    profile: Profile,
    *,
    send: bool = False,
    show_amounts: bool = False,
    transport: Transport | None = None,
    flavour: Flavour | None = None,
) -> Delivery:
    """Post a profile summary. Dry run unless `send=True`."""
    message = profile_message(profile, show_amounts=show_amounts)
    shape = flavour or detect_flavour(url)
    return send_payload(
        url, build_payload(message, shape), flavour=shape, send=send, transport=transport
    )
