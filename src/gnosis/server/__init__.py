"""Gnosis as a server, so an agent can consult it mid-conversation.

There is exactly one server here and it speaks MCP over stdio. The reason it
exists is a gap in the way agents currently trade: Binance ships an MCP server
whose tools *place orders*, and a client that has loaded it can size and send a
position without anything in the loop that knows what this particular trader
has historically done in this particular situation. Gnosis on the other side of
the same client closes that gap -- the agent asks "should I?" against the
user's own receipts before it asks "can I?" against the exchange.

The tools here are read-only without exception, and say so in their own
descriptions and annotations. That is not a limitation to be lifted later. A
gate that can also trade is not a gate; it is a second trading surface with an
opinion, and the first time it acts on that opinion it stops being something a
trader will leave installed.

    from gnosis.server import serve, handle

`serve` runs the stdio loop; `handle` maps one decoded JSON-RPC message to one
reply and touches no I/O at all, which is what makes the protocol testable
offline.

The re-exports below are resolved lazily. That is not a load-time
micro-optimisation: `python3 -m gnosis.server.mcp` imports this package first
and then executes `mcp` a second time as `__main__`, and an eager `from .mcp
import ...` here makes the interpreter warn about exactly that on every start.
A server whose first line of output is a RuntimeWarning looks broken even when
it is not.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "PROTOCOL_VERSION",
    "SERVER_NAME",
    "SERVER_VERSION",
    "TOOLS",
    "McpError",
    "Session",
    "call_tool",
    "handle",
    "serve",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from . import mcp

        return getattr(mcp, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
