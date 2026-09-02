"""Outbound notifications: the one place in Gnosis that talks *out*.

Everything else here reads. Ingest reads an export or a wallet, the detectors
read round-trips, the card and the report read a profile. This package posts,
and that single difference is why it is the most carefully fenced code in the
project.

Two fences, both non-negotiable:

**Nothing sends without `send=True`.** The default is a dry run that returns
the exact bytes and the exact destination and touches no socket. A judgement is
a statement about a person's trading, and a webhook is usually a channel with
other people in it; a tool that posts on its first invocation because a flag
defaulted the wrong way will eventually post someone's drawdown to their team.

**Currency is off unless it is asked for.** `show_amounts=False` is the
default, and it applies to the payload rather than to the rendering, so there
is no path where a figure reaches the wire because one formatter forgot.

The transport is injected, so the tests drive the real payload builders and the
real refusal logic without a socket existing anywhere in the suite.
"""

from __future__ import annotations

from .webhook import (
    Delivery,
    Flavour,
    build_payload,
    detect_flavour,
    notify_judgement,
    notify_profile,
    send_payload,
)

__all__ = [
    "Delivery",
    "Flavour",
    "build_payload",
    "detect_flavour",
    "notify_judgement",
    "notify_profile",
    "send_payload",
]
