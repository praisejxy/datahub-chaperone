"""End-to-end tests for the MCP proxy.

The proxy is the component judges and users actually run, and it is the only
place where a policy verdict turns into bytes on a wire. Everything else can be
correct while the proxy still forwards a denied call, so these tests drive it
through its real ``run()`` loop with a fake upstream server rather than calling
the interception helpers directly.

The fake upstream stands in for ``mcp-server-datahub``. Chaperone deliberately
intercepts at the JSON-RPC level and never imports the upstream server, so a
stand-in that speaks the same frames exercises the same code path a real one
would.
"""

from __future__ import annotations

import io
import json
import os
import queue
from typing import Any

import pytest

from chaperone.audit import AuditLog
from chaperone.graph import build_provider
from chaperone.policy import Policy, PolicyEngine
from chaperone.proxy import ChaperoneProxy, UpstreamProcess, split_command

CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,ecommerce.public.customers,PROD)"
ORDER_ITEMS = "urn:li:dataset:(urn:li:dataPlatform:postgres,ecommerce.public.order_items,PROD)"
FCT_ORDERS = "urn:li:dataset:(urn:li:dataPlatform:dbt,analytics.marts.fct_orders,PROD)"
GHOST = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.marts.does_not_exist,PROD)"


class FakeUpstream:
    """Stands in for the real MCP server.

    Records everything Chaperone forwards, and replies with whatever the test
    queued for that request id.

    ``readline`` blocks on a queue rather than returning ``None`` when the
    outbox happens to be empty, because that is what a real pipe does: EOF
    arrives only once the writer closes stdin. A fake that reports EOF early
    would let the proxy's reader thread exit before a reply landed, and the
    tests would pass against a proxy that drops responses.
    """

    def __init__(self, replies: dict[Any, dict[str, Any]] | None = None) -> None:
        self.received: list[dict[str, Any]] = []
        self.replies = replies or {}
        self._outbox: queue.Queue[str | None] = queue.Queue()
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def send(self, frame: dict[str, Any]) -> None:
        self.received.append(frame)
        reply = self.replies.get(frame.get("id"))
        if reply is not None:
            self._outbox.put(json.dumps(reply))

    def readline(self) -> str | None:
        line = self._outbox.get(timeout=5)
        return line + "\n" if line is not None else None

    def close_stdin(self) -> None:
        self._outbox.put(None)  # EOF, once everything queued ahead of it drains

    def stop(self) -> None:
        self.stopped = True
        self._outbox.put(None)

    # -- test helpers --
    def tools_called(self) -> list[str]:
        return [
            (f.get("params") or {}).get("name")
            for f in self.received
            if f.get("method") == "tools/call"
        ]


def run_proxy(
    monkeypatch,
    tmp_path,
    frames: list[dict[str, Any]],
    replies: dict[Any, dict[str, Any]] | None = None,
    **kwargs: Any,
) -> tuple[ChaperoneProxy, FakeUpstream, list[dict[str, Any]]]:
    """Drive the proxy's real run loop over a scripted agent session."""
    monkeypatch.setenv("CHAPERONE_HOME", str(tmp_path))
    monkeypatch.setattr(
        "sys.stdin", io.StringIO("".join(json.dumps(f) + "\n" for f in frames))
    )
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)

    proxy = ChaperoneProxy(
        engine=PolicyEngine(Policy.bundled("default"), build_provider(offline=True)),
        audit=AuditLog(agent_id="test-agent"),
        agent_id="test-agent",
        **kwargs,
    )
    upstream = FakeUpstream(replies)
    proxy.upstream = upstream
    proxy.run()

    written = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    return proxy, upstream, written


def call_frame(request_id: Any, tool: str, **arguments: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }


# -- the guarantee that matters -------------------------------------------

def test_a_denied_call_never_reaches_upstream(monkeypatch, tmp_path):
    """The whole product in one assertion.

    Every other test can pass while this one fails, and if it fails Chaperone is
    worse than useless: it would report a block it did not perform.
    """
    _, upstream, written = run_proxy(
        monkeypatch, tmp_path, [call_frame(1, "update_description", urn=CUSTOMERS, description="x")]
    )
    assert upstream.tools_called() == []
    assert len(written) == 1
    assert written[0]["id"] == 1
    assert written[0]["result"]["isError"] is True


def test_an_allowed_call_is_forwarded_unchanged(monkeypatch, tmp_path):
    frame = call_frame(1, "update_description", urn=ORDER_ITEMS, description="Line items.")
    _, upstream, _ = run_proxy(monkeypatch, tmp_path, [frame])
    assert upstream.received == [frame], "policy must not rewrite an allowed call"


def test_non_tool_frames_pass_straight_through(monkeypatch, tmp_path):
    """initialize/tools/list are not policy-relevant and must not be delayed."""
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]
    _, upstream, _ = run_proxy(monkeypatch, tmp_path, frames)
    assert upstream.received == frames


def test_a_refusal_explains_itself_to_the_agent(monkeypatch, tmp_path):
    """An agent that gets a bare error retries; one that gets a reason adapts."""
    _, _, written = run_proxy(
        monkeypatch, tmp_path, [call_frame(7, "update_description", urn=CUSTOMERS, description="x")]
    )
    result = written[0]["result"]
    text = result["content"][0]["text"]
    assert "PII" in text
    assert "Nothing was changed" in text
    # Machine-readable too, so a client can branch on it without parsing prose.
    meta = result["_meta"]["chaperone"]
    assert meta["verdict"] == "deny"
    assert "pii-mutation-deny" in meta["rules"]


def test_a_refusal_is_a_result_not_a_transport_error(monkeypatch, tmp_path):
    """A JSON-RPC error reads as 'the server broke', which invites a retry."""
    _, _, written = run_proxy(
        monkeypatch, tmp_path, [call_frame(1, "update_description", urn=GHOST, description="x")]
    )
    assert "error" not in written[0]
    assert written[0]["result"]["isError"] is True


def test_a_review_hands_back_a_proposal_id(monkeypatch, tmp_path):
    """The agent's work must survive being blocked, or it just tries again."""
    proxy, upstream, written = run_proxy(
        monkeypatch, tmp_path, [call_frame(1, "update_description", urn=FCT_ORDERS, description="x")]
    )
    assert upstream.tools_called() == []
    assert "Proposal recorded" in written[0]["result"]["content"][0]["text"]
    assert proxy.audit.proposals[-1]["arguments"]["description"] == "x"


# -- redaction ------------------------------------------------------------

def test_sensitive_values_are_stripped_from_the_response(monkeypatch, tmp_path):
    """A read of a PII table is forwarded - only the values are scrubbed."""
    reply = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"rows": [{"id": 1, "email": "ada@example.com", "city": "London"}]}
                    ),
                }
            ]
        },
    }
    _, upstream, written = run_proxy(
        monkeypatch, tmp_path, [call_frame(1, "get_entities", urns=[CUSTOMERS])], {1: reply}
    )

    assert upstream.tools_called() == ["get_entities"], "reads must still reach upstream"
    payload = json.dumps(written[0])
    assert "ada@example.com" not in payload
    assert "[REDACTED:PII]" in payload
    assert "London" in payload, "redaction must not destroy non-sensitive context"
    assert "redacted" in written[0]["result"]["content"][-1]["text"]


def test_responses_to_unflagged_calls_are_untouched(monkeypatch, tmp_path):
    reply = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps({"email": "ada@example.com"})}]},
    }
    _, _, written = run_proxy(
        monkeypatch, tmp_path, [call_frame(1, "get_entities", urns=[ORDER_ITEMS])], {1: reply}
    )
    assert written[0] == reply


# -- robustness -----------------------------------------------------------

def test_a_malformed_frame_gets_a_parse_error_not_silence(monkeypatch, tmp_path):
    """Dropping the frame would hang a client waiting on a response."""
    monkeypatch.setenv("CHAPERONE_HOME", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json\n"))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)

    proxy = ChaperoneProxy(
        engine=PolicyEngine(Policy.bundled("default"), build_provider(offline=True)),
        audit=AuditLog(agent_id="test-agent"),
    )
    proxy.upstream = FakeUpstream()
    proxy.run()

    written = json.loads(out.getvalue().strip())
    assert written["error"]["code"] == -32700


def test_dry_run_reports_verdicts_without_enforcing_them(monkeypatch, tmp_path):
    """Needed to roll a policy out on real traffic before it starts blocking."""
    proxy, upstream, _ = run_proxy(
        monkeypatch,
        tmp_path,
        [call_frame(1, "update_description", urn=CUSTOMERS, description="x")],
        dry_run=True,
    )
    assert upstream.tools_called() == ["update_description"]
    assert proxy.stats["denied"] == 1, "a dry run must still record what it would have blocked"


def test_the_upstream_is_stopped_when_the_agent_disconnects(monkeypatch, tmp_path):
    proxy, upstream, _ = run_proxy(monkeypatch, tmp_path, [])
    assert upstream.started and upstream.stopped


def test_a_reply_still_in_flight_at_shutdown_is_not_dropped(monkeypatch, tmp_path):
    """Regression: the proxy used to kill upstream the moment stdin hit EOF.

    A short session - one call, then disconnect - is the normal shape of an
    agent run, so the last response was routinely lost. To the agent that looks
    like a hang, not a shutdown, and it is invisible unless a test asserts the
    response actually came back.
    """
    reply = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "ok"}]}}
    _, _, written = run_proxy(
        monkeypatch, tmp_path, [call_frame(1, "search", query="orders")], {1: reply}
    )
    assert written == [reply]


def test_every_call_is_audited_whatever_the_verdict(monkeypatch, tmp_path):
    """A governance layer with gaps in its own log cannot be relied on."""
    proxy, _, _ = run_proxy(
        monkeypatch,
        tmp_path,
        [
            call_frame(1, "search", query="orders"),
            call_frame(2, "update_description", urn=CUSTOMERS, description="x"),
            call_frame(3, "update_description", urn=FCT_ORDERS, description="x"),
            call_frame(4, "get_entities", urns=[CUSTOMERS]),
        ],
    )
    summary = proxy.audit.summary()
    assert summary["total_calls"] == 4
    assert summary["verdicts"] == {"allow": 1, "deny": 1, "review": 1, "redact": 1}


# -- launching the upstream process ---------------------------------------

def test_a_windows_path_survives_command_splitting():
    """Regression: POSIX splitting ate the backslashes in a Windows path.

    `shlex.split` treats `\\` as an escape, so `C:\\Python\\python.exe` became
    `C:Pythonpython.exe` and the server died with a bare "file not found" that
    named no path. Judges on Windows would have hit this on first run.
    """
    argv = split_command(r'"C:\Python\python.exe" -m mcp_server_datahub')
    if os.name == "nt":
        assert argv[0] == r"C:\Python\python.exe"
    assert argv[1:] == ["-m", "mcp_server_datahub"]


def test_a_missing_upstream_command_names_itself():
    """The default upstream is fetched by uvx, so this is the likely first failure."""
    with pytest.raises(RuntimeError, match="definitely-not-a-real-binary"):
        UpstreamProcess("definitely-not-a-real-binary --serve").start()
