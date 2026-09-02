"""A scripted stand-in for the Anthropic client, so the agent layer is testable.

The agent layer's guarantee is that a model cannot put a number on the card
that Gnosis did not compute. That guarantee is only worth anything if there is
a test where a model *tries* -- and a real model, asked nicely, will decline to
hallucinate, which makes it useless for testing the one path that matters.

So the double is scripted rather than clever. You hand it the exact replies you
want, in order, and it hands them back. That makes "the model invented a cost
of -9,999" a two-line test instead of an act of prompt engineering, and it
means the whole suite runs with no API key, no network, and no cost.

It mimics the shape of `anthropic.Anthropic` closely enough that
`narrate.call_model` runs its real code path against it -- the same
`client.messages.create(...)` call, the same `.content` block walk, the same
`json.loads`. A double that returned a pre-parsed dict would test nothing but
itself.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeBlock:
    """One content block, as the SDK returns them."""

    type: str
    text: str


@dataclass
class FakeResponse:
    content: list[FakeBlock]
    stop_reason: str = "end_turn"


def reply(**fields: Any) -> str:
    """A scripted reply, as the JSON text a schema-constrained model would emit."""
    return json.dumps(fields)


class _Messages:
    def __init__(self, client: FakeAnthropic) -> None:
        self._client = client

    def create(self, **kwargs: Any) -> FakeResponse:
        return self._client._next(kwargs)


class FakeAnthropic:
    """A model client that replays a queue of canned replies.

    Each scripted item is one of:

    - `str`   -- returned verbatim as the response text (usually JSON from
                 `reply()`, but a malformed string is exactly how you test the
                 parser).
    - `dict`  -- JSON-encoded for you.
    - `Exception` instance -- raised, which is how transport failures, rate
                 limits and timeouts are simulated. The layer's contract is
                 that any exception degrades to the template, so it should not
                 matter which one.

    Every call is recorded in `.calls`, so a test can assert on what was sent:
    that the prompt is a fact sheet and not a trade history, that the model id
    is the one requested, that the system prompt states the rules.
    """

    def __init__(self, *scripted: Any) -> None:
        self.scripted: list[Any] = list(scripted)
        self.calls: list[dict[str, Any]] = []
        self.messages = _Messages(self)

    def queue(self, *scripted: Any) -> FakeAnthropic:
        """Append more replies. Returns self, so it chains."""
        self.scripted.extend(scripted)
        return self

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def last_prompt(self) -> str:
        """The user turn of the most recent call."""
        return self.calls[-1]["messages"][0]["content"]

    def last_system(self) -> str:
        return self.calls[-1]["system"]

    def _next(self, kwargs: dict[str, Any]) -> FakeResponse:
        self.calls.append(kwargs)
        if not self.scripted:
            # Louder than returning an empty reply: a test that runs out of
            # script is a test that is not asserting what it thinks it is.
            raise AssertionError(
                f"FakeAnthropic ran out of scripted replies on call {len(self.calls)}"
            )
        item = self.scripted.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, dict):
            item = json.dumps(item)
        return FakeResponse(content=[FakeBlock(type="text", text=str(item))])


@dataclass
class SilentClient:
    """A client that must never be called.

    Used to prove the no-credentials path really is short-circuited rather
    than merely producing template-shaped output for some other reason.
    """

    calls: list[Any] = field(default_factory=list)

    @property
    def messages(self) -> Any:
        raise AssertionError("the model was called when it should not have been")


@contextmanager
def no_api_key():
    """Run a block with every credential environment variable unset.

    Restores whatever was there, because a developer with a real key in their
    shell must get the same test result as CI, and because silently clobbering
    someone's environment is a rude thing for a test helper to do.
    """
    names = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    saved = {name: os.environ.pop(name, None) for name in names}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)


@contextmanager
def fake_api_key(value: str = "sk-ant-not-a-real-key"):
    """Run a block with a credential present but no network involved.

    For the paths that must be reachable only when a key exists. Nothing here
    ever opens a socket: the tests still inject `FakeAnthropic`, and this only
    exercises the branch that decides whether to look for a client at all.
    """
    saved = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = value
    try:
        yield
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved
        else:
            os.environ.pop("ANTHROPIC_API_KEY", None)
