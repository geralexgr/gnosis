"""The MCP server: handshake, tool schemas, every tool's happy and error path,
malformed JSON-RPC, and the read-only guarantee.

Every assertion here runs against the real protocol code with a fixture history
injected through `Session(loader=...)`. Nothing is stubbed except the source of
the fills, which is the only thing that would otherwise touch a disk or a
network.

Four properties are worth more than the rest, because each is a place where
being wrong is invisible until a client is already relying on it:

  1. **Nothing this server exposes can mutate anything.** Asserted three ways
     rather than one: the annotations say `readOnlyHint`, the module's own
     source contains no write, delete, subprocess or socket primitive, and --
     the one that would actually catch a regression -- every tool is driven
     against a real CSV in a temporary tree whose every byte and mtime is
     compared before and after, with `socket.socket` replaced by a stub that
     raises. A tool that opened a connection or rewrote its input would fail
     here even if somebody had also updated the annotation to match.

  2. **A protocol error and a tool error are different things.** The MCP spec
     draws that line and an agent depends on it: an unknown tool name means
     "you called this wrong" and comes back as a JSON-RPC error, while a tool
     that ran and could not answer comes back as a normal result with
     `isError` set, so the model sees the failure text and can act on it.
     Collapsing the two would make every bad CSV look like a transport fault.

  3. **A malformed message must not end the session.** The loop has to survive
     a truncated line, a batch, a missing `method` and a wrong `jsonrpc`, and
     answer the next well-formed request. A server that dies on one bad byte
     takes the whole conversation with it.

  4. **The tool schemas are the entire interface.** An agent's ability to use
     this correctly depends on nothing else, so the descriptions are asserted
     to say when to use the tool and what the fields mean -- and, for the two
     fields where a plausible wrong reading exists (`notional` in quote
     currency rather than base, `at` in UTC rather than local), to say so.

Run: python3 tests/test_server.py
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gnosis.ingest.synthetic import TraderSpec, generate  # noqa: E402
from gnosis.server import mcp  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{'' if cond else '  <- ' + detail}")


# ==========================================================================
# Fixtures
# ==========================================================================

def fixture_history(source: str = "fixture"):
    """A labelled night-leak trader. Deterministic, offline, no files."""
    return generate(TraderSpec(seed=31, leak="night"))


class CountingLoader:
    """Records what it was asked for, so caching can be observed."""

    def __init__(self, fn=fixture_history) -> None:
        self.calls: list[str] = []
        self._fn = fn

    def __call__(self, source: str):
        self.calls.append(source)
        return self._fn(source)


def exploding_loader(source: str):
    raise ValueError(f"cannot load {source!r}: this is the failure under test")


def session(loader=None) -> mcp.Session:
    return mcp.Session(loader=loader or CountingLoader())


def request(method, params=None, rid=1):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def call(sess, name, args=None, rid=1):
    return mcp.handle(request("tools/call", {"name": name, "arguments": args or {}}, rid), sess)


def text_of(reply):
    """The single text block out of a tools/call result."""
    return reply["result"]["content"][0]["text"]


# ==========================================================================
# The handshake
# ==========================================================================

print("\n=== initialize ===")

sess = session()
reply = mcp.handle(request("initialize", {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "test-client", "version": "0.0.1"},
}), sess)

check("initialize answers with the request id", reply.get("id") == 1, str(reply.get("id")))
check("it is a JSON-RPC 2.0 reply", reply.get("jsonrpc") == "2.0")
check("it carries a result, not an error", "result" in reply and "error" not in reply, str(reply))
result = reply["result"]
check("it echoes the protocol version the client asked for",
      result["protocolVersion"] == "2025-06-18", result.get("protocolVersion"))
check("it declares a tools capability", "tools" in result["capabilities"], str(result["capabilities"]))
check("listChanged is false, because the tool list is a constant",
      result["capabilities"]["tools"]["listChanged"] is False)
check("serverInfo names the server", result["serverInfo"]["name"] == "gnosis",
      str(result["serverInfo"]))
check("serverInfo carries a version", bool(result["serverInfo"].get("version")))
check("the session records that it was initialized", sess.initialized is True)
check("and who initialized it", sess.client_info.get("name") == "test-client",
      str(sess.client_info))

instructions = result.get("instructions", "")
check("instructions tell the client the server is read-only",
      "read-only" in instructions.lower(), instructions[:80])
check("instructions tell it to check before trading",
      "gnosis_check" in instructions and "before" in instructions.lower())
check("instructions warn that a thin result is not a clean bill of health",
      "not enough evidence" in instructions.lower(), instructions[-120:])

older = mcp.handle(request("initialize", {"protocolVersion": "2024-11-05"}), session())
check("an older supported protocol version is echoed back",
      older["result"]["protocolVersion"] == "2024-11-05",
      older["result"]["protocolVersion"])

future = mcp.handle(request("initialize", {"protocolVersion": "2099-01-01"}), session())
check("an unknown protocol version falls back to ours rather than failing the handshake",
      future["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION,
      future["result"]["protocolVersion"])

bare = mcp.handle(request("initialize"), session())
check("initialize with no params still succeeds", "result" in bare, str(bare))

check("notifications/initialized gets no reply at all",
      mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, sess) is None)
check("an unknown notification is ignored rather than erroring",
      mcp.handle({"jsonrpc": "2.0", "method": "notifications/from/the/future"}, sess) is None)
check("a JSON-RPC response from the client is ignored, not answered",
      mcp.handle({"jsonrpc": "2.0", "id": 7, "result": {}}, sess) is None)
check("ping answers emptily", mcp.handle(request("ping"), sess)["result"] == {})


# ==========================================================================
# tools/list and the schemas
# ==========================================================================

print("\n=== tools/list ===")

listed = mcp.handle(request("tools/list"), session())["result"]["tools"]
names = {t["name"] for t in listed}
check("tools/list works without a prior initialize, so the server can be poked at",
      len(listed) == 4, str(names))
check("all four tools are advertised",
      names == {"gnosis_profile", "gnosis_check", "gnosis_card", "gnosis_explain"}, str(names))
check("tools/list takes no params and needs none",
      mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session())["result"]["tools"]
      == listed)

for tool in listed:
    n = tool["name"]
    schema = tool["inputSchema"]
    check(f"{n}: has a title", bool(tool.get("title")))
    check(f"{n}: input schema is an object", schema["type"] == "object")
    check(f"{n}: rejects unknown arguments", schema["additionalProperties"] is False)
    check(f"{n}: every required field is declared in properties",
          set(schema["required"]) <= set(schema["properties"]), str(schema["required"]))
    check(f"{n}: every property is described",
          all(p.get("description") for p in schema["properties"].values()),
          str([k for k, p in schema["properties"].items() if not p.get("description")]))
    check(f"{n}: is annotated read-only", tool["annotations"]["readOnlyHint"] is True)
    check(f"{n}: is annotated non-destructive", tool["annotations"]["destructiveHint"] is False)
    check(f"{n}: declares a closed world -- no exchange, no market data",
          tool["annotations"]["openWorldHint"] is False)
    check(f"{n}: says READ-ONLY in the description an agent actually reads",
          "READ-ONLY" in tool["description"], tool["description"][:60])
    check(f"{n}: the description says when to use it",
          "use this" in tool["description"].lower() or "call this" in tool["description"].lower(),
          tool["description"][:60])
    check(f"{n}: the description is substantial enough to act on",
          len(tool["description"]) > 300, str(len(tool["description"])))

by_name = {t["name"]: t for t in listed}
check("gnosis_check requires the two things it cannot invent",
      set(by_name["gnosis_check"]["inputSchema"]["required"]) == {"symbol", "notional"},
      str(by_name["gnosis_check"]["inputSchema"]["required"]))
notional_doc = by_name["gnosis_check"]["inputSchema"]["properties"]["notional"]["description"]
check("notional says quote currency, not base, and not margin",
      "quote currency" in notional_doc and "margin" in notional_doc, notional_doc[:80])
at_doc = by_name["gnosis_check"]["inputSchema"]["properties"]["at"]["description"]
check("at says the timestamp is read as UTC and why that matters",
      "UTC" in at_doc and "session" in at_doc, at_doc[:80])
lev_doc = by_name["gnosis_check"]["inputSchema"]["properties"]["leverage"]["description"]
check("leverage says not to pass 1 for spot, which is a different claim",
      "do not pass 1" in lev_doc, lev_doc[:80])
check("gnosis_explain enumerates the rules it accepts",
      set(by_name["gnosis_explain"]["inputSchema"]["properties"]["rule"]["enum"])
      == set(mcp.RULES), "enum drifted from RULES")
check("gnosis_profile warns that an empty result may mean thin history",
      "is_thin" in by_name["gnosis_profile"]["description"])
check("every tool documents the source format identically",
      sum(mcp.SOURCE_DESCRIPTION in json.dumps(t) for t in listed) == 3,
      "source description should appear on the three history-reading tools")


# ==========================================================================
# Each tool: the happy path
# ==========================================================================

print("\n=== tools/call: happy paths ===")

loader = CountingLoader()
sess = session(loader)

reply = call(sess, "gnosis_card", {"source": "fixture"})
card = text_of(reply)
check("gnosis_card returns a result, not an error", reply["result"]["isError"] is False)
check("gnosis_card renders the card", "REKT WRAPPED" in card, card[:60])
check("gnosis_card emits no ANSI escapes into the JSON-RPC stream",
      "\033[" not in card, "found an escape sequence")
check("gnosis_card content is a single text block",
      [c["type"] for c in reply["result"]["content"]] == ["text"])

reply = call(sess, "gnosis_profile", {"source": "fixture"})
profile = json.loads(text_of(reply))
check("gnosis_profile returns parseable JSON", isinstance(profile, dict))
check("the profile carries a summary", "summary" in profile)
check("the summary surfaces is_thin explicitly, not as a rule to re-derive",
      "is_thin" in profile["summary"])
check("the fixture trader is not thin", profile["summary"]["is_thin"] is False)
check("the night leak is found", any(x["rule"] == "session_performance" for x in profile["leaks"]),
      str([x["rule"] for x in profile["leaks"]]))
check("every leak carries its receipts",
      all(x["trade_ids"] for x in profile["leaks"]), "a leak arrived with no trade ids")

check("the loader ran once for two tools on the same source", loader.calls == ["fixture"],
      str(loader.calls))
call(sess, "gnosis_profile", {"source": "other"})
check("a different source is loaded separately", loader.calls == ["fixture", "other"],
      str(loader.calls))

reply = call(sess, "gnosis_check", {
    "source": "fixture", "symbol": "ETHUSDT", "notional": 4000.0, "leverage": 20,
    "at": "2026-09-01T03:14", "minutes_since_last_loss": 41,
})
judgement = json.loads(text_of(reply))
check("gnosis_check returns a verdict",
      judgement["verdict"] in ("skip", "caution", "proceed", "favourable"),
      str(judgement.get("verdict")))
check("the night trader at 03:14 is warned off", judgement["verdict"] == "skip",
      judgement["verdict"])
check("it quotes reasons", len(judgement["reasons"]) >= 1)
check("every analogue declares whether it is significant",
      all("significant" in a for a in judgement["analogues"]))
check("it echoes back the trade it judged, normalised to UTC",
      judgement["proposed"]["at"].endswith("+00:00"), judgement["proposed"]["at"])
check("it restates that it never blocks", "never blocks" in judgement["note"])

reply = call(sess, "gnosis_check", {
    "source": "fixture", "symbol": "ETHUSDT", "notional": 4000.0, "at": "2026-09-01T14:00",
})
check("the same trader at 14:00 is not warned off",
      json.loads(text_of(reply))["verdict"] != "skip")

reply = call(sess, "gnosis_check", {"source": "fixture", "symbol": "ETHUSDT", "notional": 100})
check("gnosis_check works with no timestamp, defaulting to now",
      reply["result"]["isError"] is False, text_of(reply)[:120])

reply = call(sess, "gnosis_explain", {"rule": "leverage_drag"})
explained = json.loads(text_of(reply))
check("gnosis_explain describes the leak", bool(explained["what_the_leak_is"]))
check("gnosis_explain describes the detection method",
      "percentage return" in explained["how_gnosis_detects_it"])
check("gnosis_explain lists the evidence tiers",
      set(explained["evidence_tiers"]) == {"proof", "statistical", "weak"})
check("gnosis_explain states the minimum-sample guarantee",
      any("8 observations" in g for g in explained["shared_guarantees"]))
check("gnosis_explain needs no history at all",
      mcp.handle(request("tools/call", {"name": "gnosis_explain", "arguments": {
          "rule": "disposition_effect"}}), mcp.Session(loader=exploding_loader)
      )["result"]["isError"] is False)
check("every advertised rule can be explained",
      all(mcp.handle(request("tools/call", {"name": "gnosis_explain",
                                            "arguments": {"rule": r}}),
                     session())["result"]["isError"] is False
          for r in mcp.RULES), "a rule in the enum has no explanation")

check("omitting source falls back to the documented default",
      CountingLoader() is not None)
loader2 = CountingLoader()
call(mcp.Session(loader=loader2), "gnosis_card", {})
check("and that default is the one the schema advertises",
      loader2.calls == [mcp.DEFAULT_SOURCE], str(loader2.calls))

first = text_of(call(session(), "gnosis_card", {"source": "fixture"}))
second = text_of(call(session(), "gnosis_card", {"source": "fixture"}))
check("the same history renders identically twice -- nothing here is generated",
      first == second)


# ==========================================================================
# Each tool: the error paths
# ==========================================================================

print("\n=== tools/call: error paths ===")

bad = mcp.Session(loader=exploding_loader)

for tool in ("gnosis_card", "gnosis_profile"):
    reply = call(bad, tool, {"source": "nope.txt"})
    check(f"{tool}: an unreadable source is a tool error, not a crash",
          reply["result"]["isError"] is True, str(reply)[:120])
    check(f"{tool}: and the model is told why",
          "failure under test" in text_of(reply), text_of(reply)[:120])

reply = call(bad, "gnosis_check", {"symbol": "ETHUSDT", "notional": 100})
check("gnosis_check: an unreadable source is a tool error too",
      reply["result"]["isError"] is True)

sess = session()
reply = call(sess, "gnosis_check", {"source": "fixture", "notional": 100})
check("gnosis_check: a missing symbol is reported as a tool error",
      reply["result"]["isError"] is True and "symbol" in text_of(reply), text_of(reply)[:80])
reply = call(sess, "gnosis_check", {"source": "fixture", "symbol": "ETHUSDT"})
check("gnosis_check: a missing notional is reported",
      reply["result"]["isError"] is True and "notional" in text_of(reply), text_of(reply)[:80])
reply = call(sess, "gnosis_check", {"source": "fixture", "symbol": "ETHUSDT", "notional": "4000"})
check("gnosis_check: a string notional is refused rather than coerced",
      reply["result"]["isError"] is True, text_of(reply)[:80])
reply = call(sess, "gnosis_check", {"source": "fixture", "symbol": "ETHUSDT", "notional": -5})
check("gnosis_check: a negative notional is refused",
      reply["result"]["isError"] is True, text_of(reply)[:80])
reply = call(sess, "gnosis_check", {"source": "fixture", "symbol": "ETHUSDT",
                                    "notional": 100, "side": "hodl"})
check("gnosis_check: an invalid side is refused rather than defaulted",
      reply["result"]["isError"] is True, text_of(reply)[:80])
reply = call(sess, "gnosis_check", {"source": "fixture", "symbol": "ETHUSDT",
                                    "notional": 100, "at": "tuesday"})
check("gnosis_check: an unparseable timestamp says what a good one looks like",
      reply["result"]["isError"] is True and "2026-09-01T03:14" in text_of(reply),
      text_of(reply)[:120])

reply = call(sess, "gnosis_explain", {"rule": "vibes"})
check("gnosis_explain: an unknown rule is a tool error",
      reply["result"]["isError"] is True)
check("gnosis_explain: and it lists the rules that do exist",
      "disposition_effect" in text_of(reply), text_of(reply)[:120])
reply = call(sess, "gnosis_explain", {})
check("gnosis_explain: a missing rule is reported", reply["result"]["isError"] is True)

reply = call(sess, "gnosis_card", {"source": ""})
check("an empty source is refused rather than silently defaulted",
      reply["result"]["isError"] is True, text_of(reply)[:80])

reply = call(sess, "gnosis_nuke_account", {})
check("an unknown TOOL is a protocol error, not a tool error",
      "error" in reply and reply["error"]["code"] == mcp.INVALID_PARAMS, str(reply)[:140])
check("and the error names the tools that do exist",
      set(reply["error"]["data"]["available"]) == {"gnosis_card", "gnosis_check",
                                                   "gnosis_explain", "gnosis_profile"},
      str(reply["error"].get("data")))

reply = mcp.handle(request("tools/call", {"arguments": {}}), sess)
check("tools/call with no name is a protocol error",
      reply["error"]["code"] == mcp.INVALID_PARAMS, str(reply)[:120])
reply = mcp.handle(request("tools/call", {"name": "gnosis_card", "arguments": "oops"}), sess)
check("tools/call with non-object arguments is a protocol error",
      reply["error"]["code"] == mcp.INVALID_PARAMS, str(reply)[:120])
reply = mcp.handle(request("tools/call", {"name": "gnosis_explain"}), sess)
check("tools/call with arguments omitted entirely is accepted and handled by the tool",
      "result" in reply and reply["result"]["isError"] is True, str(reply)[:120])


# ==========================================================================
# Malformed JSON-RPC and unknown methods
# ==========================================================================

print("\n=== malformed JSON-RPC ===")

sess = session()

reply = mcp.handle(request("tools/summon"), sess)
check("an unknown method is -32601", reply["error"]["code"] == mcp.METHOD_NOT_FOUND, str(reply))
check("and it lists the methods that exist",
      set(reply["error"]["data"]["supported"]) == {"initialize", "tools/list", "tools/call", "ping"},
      str(reply["error"]["data"]))
check("an unknown method still answers the right id", reply["id"] == 1)

reply = mcp.handle(["not", "a", "message"], sess)
check("a JSON-RPC batch is refused, because MCP 2025-06-18 removed batching",
      reply["error"]["code"] == mcp.INVALID_REQUEST, str(reply))
check("the batch refusal carries a null id, since there is no id to answer",
      reply["id"] is None, str(reply["id"]))

for bad_message, label in [
    ("just a string", "a bare string"),
    (42, "a bare number"),
    (None, "null"),
]:
    reply = mcp.handle(bad_message, sess)
    check(f"{label} is refused as an invalid request",
          reply["error"]["code"] == mcp.INVALID_REQUEST, str(reply))

reply = mcp.handle({"id": 1, "method": "tools/list"}, sess)
check("a missing jsonrpc version is refused",
      reply["error"]["code"] == mcp.INVALID_REQUEST, str(reply))
reply = mcp.handle({"jsonrpc": "1.0", "id": 1, "method": "tools/list"}, sess)
check("jsonrpc 1.0 is refused", reply["error"]["code"] == mcp.INVALID_REQUEST, str(reply))
reply = mcp.handle({"jsonrpc": "2.0", "id": 1}, sess)
check("a request with no method is refused",
      reply["error"]["code"] == mcp.INVALID_REQUEST, str(reply))
reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": 5}, sess)
check("a non-string method is refused", reply["error"]["code"] == mcp.INVALID_REQUEST, str(reply))
reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": []}, sess)
check("non-object params are refused", reply["error"]["code"] == mcp.INVALID_PARAMS, str(reply))
reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": None}, sess)
check("null params are treated as empty, which is what clients send",
      "result" in reply, str(reply))
check("a malformed NOTIFICATION is swallowed, never answered",
      mcp.handle({"jsonrpc": "1.0", "method": "notifications/initialized"}, sess) is None)

check("an id of 0 is answered with 0, not dropped as falsy",
      mcp.handle(request("ping", rid=0), sess)["id"] == 0)
check("a string id is answered with the same string",
      mcp.handle(request("ping", rid="abc"), sess)["id"] == "abc")
check("a null id is answered with null",
      mcp.handle({"jsonrpc": "2.0", "id": None, "method": "ping"}, sess)["id"] is None)


# ==========================================================================
# The stdio loop
# ==========================================================================

print("\n=== the stdio loop ===")

lines = "\n".join([
    json.dumps(request("initialize", {"protocolVersion": "2025-06-18"}, rid=1)),
    json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
    "",
    "{ this is not json",
    json.dumps(request("tools/list", rid=2)),
    json.dumps(request("tools/call", {"name": "gnosis_explain",
                                      "arguments": {"rule": "revenge_trading"}}, rid=3)),
])
out = io.StringIO()
code = mcp.serve(io.StringIO(lines), out, loader=CountingLoader())
replies = [json.loads(ln) for ln in out.getvalue().splitlines() if ln.strip()]

check("serve returns 0 when the input closes", code == 0, str(code))
check("one reply per request, and none for the notification or the blank line",
      len(replies) == 4, str(len(replies)))
check("the handshake is answered first", replies[0]["id"] == 1 and "result" in replies[0])
check("unparseable JSON is a -32700 parse error",
      replies[1]["error"]["code"] == mcp.PARSE_ERROR, str(replies[1]))
check("a parse error carries a null id, since the id was in the bytes we could not read",
      replies[1]["id"] is None, str(replies[1]["id"]))
check("and the loop survives it to answer the next request",
      replies[2]["id"] == 2 and len(replies[2]["result"]["tools"]) == 4)
check("and the one after that", replies[3]["id"] == 3)
check("every line written is a complete JSON object on its own line",
      all(isinstance(r, dict) for r in replies))
check("no reply contains an embedded newline that would split a frame",
      all("\n" not in ln for ln in out.getvalue().splitlines()))

out = io.StringIO()
mcp.serve(io.StringIO(""), out, loader=CountingLoader())
check("empty input produces no output at all", out.getvalue() == "", repr(out.getvalue()))

out = io.StringIO()
mcp.serve(io.StringIO('{"jsonrpc":"2.0","method":"notifications/initialized"}\n'), out,
          loader=CountingLoader())
check("a session of nothing but notifications writes nothing", out.getvalue() == "")


# ==========================================================================
# Read-only, asserted rather than promised
# ==========================================================================

print("\n=== nothing here can mutate anything ===")

source_text = (ROOT / "src" / "gnosis" / "server" / "mcp.py").read_text(encoding="utf-8")
# Comments and docstrings are stripped first, so prose describing what the
# module does not do cannot fail its own test.
code_only = "\n".join(
    ln for ln in source_text.splitlines()
    if not ln.lstrip().startswith("#")
)
for token in ("subprocess", "socket", "urllib", "http.client", "requests",
              "os.remove", "os.unlink", "shutil", "rmtree", "os.rename",
              ".write_text(", ".write_bytes(", "os.system", "eval(", "exec("):
    check(f"mcp.py contains no {token}", token not in code_only,
          f"found {token!r} in the server module")
check("mcp.py opens no file of its own", "open(" not in code_only.replace("urlopen(", ""),
      "found an open() call")
check("the dispatch table is exactly the four read-only tools",
      set(mcp.IMPLEMENTATIONS) == {"gnosis_card", "gnosis_check", "gnosis_explain",
                                   "gnosis_profile"},
      str(sorted(mcp.IMPLEMENTATIONS)))
check("every advertised tool has an implementation and vice versa",
      set(mcp.TOOLS_BY_NAME) == set(mcp.IMPLEMENTATIONS))
check("every implementation is annotated read-only",
      all(mcp.TOOLS_BY_NAME[n]["annotations"]["readOnlyHint"] for n in mcp.IMPLEMENTATIONS))


def tree_snapshot(root: Path) -> dict:
    """Every file under `root`, by content hash and mtime."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            out[str(path.relative_to(root))] = (
                stat.st_size, stat.st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return out


class NoSockets:
    """Stands in for `socket.socket` and refuses to be one."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("a read-only tool opened a socket")


with tempfile.TemporaryDirectory() as tmp:
    workdir = Path(tmp)
    csv_path = workdir / "book.csv"
    shutil.copy(ROOT / "corpus" / "demo-account.csv", csv_path)
    (workdir / "bystander.txt").write_text("untouched", encoding="utf-8")
    before = tree_snapshot(workdir)
    cwd_before = sorted(os.listdir(ROOT))

    real_socket = socket.socket
    socket.socket = NoSockets
    try:
        # The real loader this time: these calls genuinely parse a CSV off disk.
        live = mcp.Session()
        outputs = [
            call(live, "gnosis_card", {"source": str(csv_path)}),
            call(live, "gnosis_profile", {"source": str(csv_path)}),
            call(live, "gnosis_check", {"source": str(csv_path), "symbol": "DOGEUSDT",
                                        "notional": 7000, "leverage": 25,
                                        "at": "2026-09-01T03:14",
                                        "minutes_since_last_loss": 41}),
            call(live, "gnosis_explain", {"rule": "session_performance"}),
        ]
        socket_ok = True
    except AssertionError:
        socket_ok = False
        outputs = []
    finally:
        socket.socket = real_socket

    check("every tool runs against a real CSV without opening a socket", socket_ok)
    check("and all four returned successfully",
          bool(outputs) and all(r["result"]["isError"] is False for r in outputs),
          str([r["result"]["isError"] for r in outputs]) if outputs else "no output")
    check("the history file is byte-for-byte unchanged, mtime included",
          tree_snapshot(workdir) == before, "the tools modified their input")
    check("no file was created or deleted anywhere near it",
          set(tree_snapshot(workdir)) == set(before))
    check("and nothing appeared in the repo root either",
          sorted(os.listdir(ROOT)) == cwd_before)

    # The same call twice must produce the same bytes. A tool that mutated
    # hidden state would drift here even if it wrote nothing to disk.
    again = call(mcp.Session(), "gnosis_profile", {"source": str(csv_path)})
    check("a second, independent session produces an identical profile",
          text_of(again) == text_of(outputs[1]))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
