"""The MCP proxy.

Chaperone speaks MCP on both sides. It reads JSON-RPC from the agent on stdin,
and relays to a real upstream MCP server (by default
``uvx mcp-server-datahub@latest``) over its own stdio pipes.

Interception happens at the protocol level rather than through the upstream
server's Python API. That choice matters: DataHub's MCP server gains and renames
tools between releases, and Chaperone must not need a code change every time it
does. A proxy that only understands ``tools/call`` frames keeps working, and the
same code governs any MCP server, not just DataHub's.

Frames are newline-delimited JSON objects, one per line, per the MCP stdio
transport.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any

from chaperone.audit import AuditLog
from chaperone.models import Decision, ToolCall, Verdict
from chaperone.policy import PolicyEngine

logger = logging.getLogger(__name__)

DEFAULT_UPSTREAM = "uvx mcp-server-datahub@latest"

# JSON-RPC error code for a request refused by policy. -32001 sits in the
# implementation-defined server-error range, so clients treat it as a real
# error rather than a malformed request.
POLICY_DENIED_CODE = -32001

# How long to let in-flight upstream responses drain after the agent hangs up.
# Bounded so a wedged child cannot stop Chaperone from exiting.
SHUTDOWN_DRAIN_SECONDS = 5.0


def split_command(command: str) -> list[str]:
    """Split an upstream command string into argv, correctly on Windows too.

    ``shlex.split`` defaults to POSIX rules, where a backslash is an escape
    character. That silently destroys a Windows path:
    ``C:\\Python\\python.exe`` becomes ``C:Pythonpython.exe`` and the process
    fails to start with a bare "file not found". Non-POSIX mode keeps
    backslashes literal but leaves quote characters in the tokens, so strip a
    matched pair afterwards.
    """
    if os.name != "nt":
        return shlex.split(command)

    tokens = shlex.split(command, posix=False)
    return [
        t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t
        for t in tokens
    ]


class UpstreamProcess:
    """A child MCP server, spoken to over stdio."""

    def __init__(self, command: str | list[str], env: dict[str, str] | None = None) -> None:
        self.command = split_command(command) if isinstance(command, str) else list(command)
        self.env = {**os.environ, **(env or {})}
        self.proc: subprocess.Popen[str] | None = None

    def start(self) -> None:
        logger.info("starting upstream MCP server: %s", " ".join(self.command))
        try:
            self.proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=sys.stderr,  # let upstream diagnostics reach the user's log
                env=self.env,
                text=True,
                encoding="utf-8",
                bufsize=1,  # line buffered: frames must not sit in a buffer
            )
        except OSError as exc:
            # The default upstream is fetched by `uvx`, so "not found" is the
            # single most likely first-run failure. A raw WinError 2 gives the
            # user nothing to act on; name the command we actually tried.
            raise RuntimeError(
                f"could not start upstream MCP server {self.command!r}: {exc}. "
                "Check the --upstream command is installed and on PATH."
            ) from exc

    def send(self, frame: dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("upstream MCP server is not running")
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()

    def readline(self) -> str | None:
        if not self.proc or not self.proc.stdout:
            return None
        line = self.proc.stdout.readline()
        return line or None

    def close_stdin(self) -> None:
        """Signal end-of-input without killing the child.

        A well-behaved MCP server answers whatever is still in flight and then
        exits on EOF. Closing stdin first is what lets those last responses
        arrive; ``stop()`` on its own would discard them.
        """
        if self.proc and self.proc.stdin and not self.proc.stdin.closed:
            try:
                self.proc.stdin.close()
            except Exception:  # already gone; nothing to close
                pass

    def stop(self) -> None:
        if not self.proc:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


class ChaperoneProxy:
    """Governs an MCP session between an agent and an upstream MCP server."""

    def __init__(
        self,
        engine: PolicyEngine,
        audit: AuditLog,
        upstream_command: str | list[str] = DEFAULT_UPSTREAM,
        agent_id: str = "unregistered-agent",
        dry_run: bool = False,
        on_decision: Callable[[Decision], None] | None = None,
    ) -> None:
        self.engine = engine
        self.audit = audit
        self.agent_id = agent_id
        self.dry_run = dry_run
        self.on_decision = on_decision
        self.upstream = UpstreamProcess(upstream_command)

        # Requests we answered ourselves must never be forwarded, and their
        # ids must not collide with upstream's replies.
        self._intercepted_ids: set[str | int] = set()
        # Request ids awaiting a redacted response, mapped to the field names to
        # strip. Per-instance, not class-level: two proxies in one process must
        # not share redaction state.
        self._redact_pending: dict[Any, set[str]] = {}
        # Two locks, deliberately. `_write_lock` serialises writes to stdout so
        # frames from the two directions cannot interleave mid-line;
        # `_state_lock` guards the counters and the pending-redaction map, which
        # the reader thread and the writer thread both touch. Sharing one lock
        # would mean holding the stdout lock while doing bookkeeping.
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self.stats = {"forwarded": 0, "denied": 0, "reviewed": 0, "redacted": 0, "allowed": 0}

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> None:
        """Pump frames in both directions until the agent closes stdin."""
        self.upstream.start()
        pump = threading.Thread(target=self._pump_upstream, daemon=True)
        pump.start()

        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                self._handle_agent_frame(line)
        except KeyboardInterrupt:
            pass
        finally:
            # Close stdin and let the pump drain before tearing the child down.
            # Without this, a response already on the wire is lost: the agent
            # sees its last tool call simply never answered, which looks like a
            # hang rather than a shutdown.
            self.upstream.close_stdin()
            pump.join(timeout=SHUTDOWN_DRAIN_SECONDS)
            self.upstream.stop()
            self.audit.close()

    def _pump_upstream(self) -> None:
        """Relay upstream responses back to the agent, redacting where required."""
        while True:
            line = self.upstream.readline()
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                # Not our business to repair upstream output; pass it through.
                self._write_agent(line)
                continue

            frame = self._maybe_redact_response(frame)
            self._write_agent(json.dumps(frame))

    def _write_agent(self, payload: str) -> None:
        with self._write_lock:
            sys.stdout.write(payload + "\n")
            sys.stdout.flush()

    def _bump(self, key: str, amount: int = 1) -> None:
        with self._state_lock:
            self.stats[key] += amount

    # -- interception ------------------------------------------------------

    def _handle_agent_frame(self, line: str) -> None:
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            # Malformed input is the client's problem to see, not ours to
            # silently swallow. Reply with a parse error rather than dropping
            # the frame, which would hang a client waiting on a response.
            logger.warning("unparseable frame from agent: %.200s", line)
            self._write_agent(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    }
                )
            )
            return

        if frame.get("method") != "tools/call":
            # initialize, tools/list, notifications, ping: not policy-relevant.
            self.upstream.send(frame)
            return

        params = frame.get("params") or {}
        call = ToolCall(
            tool=params.get("name", "<unknown>"),
            arguments=params.get("arguments") or {},
            agent_id=self.agent_id,
            request_id=frame.get("id"),
        )

        decision = self.engine.evaluate(call)
        self.audit.record(decision)
        self._bump(_stat_key(decision.verdict))
        if self.on_decision:
            self.on_decision(decision)

        if decision.verdict is Verdict.ALLOW or self.dry_run:
            self._bump("forwarded")
            self.upstream.send(frame)
            return

        if decision.verdict is Verdict.REDACT:
            # The call itself is fine; the response needs scrubbing. Remember
            # the id so the upstream pump knows to filter this one.
            with self._state_lock:
                self._redact_pending[frame.get("id")] = self.engine.redaction_fields(decision)
            self._bump("forwarded")
            self.upstream.send(frame)
            return

        # DENY and REVIEW: answer the agent ourselves; upstream never sees it.
        self._refuse(frame, decision)

    def _refuse(self, frame: dict[str, Any], decision: Decision) -> None:
        """Reply to the agent in place of upstream.

        Returned as a successful tool result carrying ``isError``, not a
        transport-level error. An agent that receives a JSON-RPC error usually
        retries or gives up; one that receives a readable explanation can
        change course, which is the behaviour we want.
        """
        self._intercepted_ids.add(frame.get("id"))
        text = decision.explain()

        if decision.verdict is Verdict.REVIEW:
            proposal = self.audit.record_proposal(decision)
            text += f"\n\nProposal recorded: `{proposal}`"

        self._write_agent(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": frame.get("id"),
                    "result": {
                        "content": [{"type": "text", "text": text}],
                        "isError": True,
                        "_meta": {
                            "chaperone": {
                                "verdict": decision.verdict.value,
                                "severity": decision.severity.value,
                                "rules": [h.rule_id for h in decision.hits],
                            }
                        },
                    },
                }
            )
        )

    def _maybe_redact_response(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Strip sensitive values from a response we flagged for redaction."""
        with self._state_lock:
            fields = self._redact_pending.pop(frame.get("id"), None)
        if not fields or "result" not in frame:
            return frame

        redacted, count = _redact_values(frame["result"], fields)
        frame["result"] = redacted
        if count:
            self._bump("redacted")
            content = frame["result"].get("content")
            if isinstance(content, list):
                content.append(
                    {
                        "type": "text",
                        "text": (
                            f"[chaperone] {count} sensitive value(s) redacted "
                            f"per catalog classification."
                        ),
                    }
                )
        return frame


def _stat_key(verdict: Verdict) -> str:
    return {
        Verdict.ALLOW: "allowed",
        Verdict.DENY: "denied",
        Verdict.REVIEW: "reviewed",
        Verdict.REDACT: "allowed",
    }[verdict]


def _redact_values(node: Any, fields: set[str], depth: int = 0) -> tuple[Any, int]:
    """Recursively replace values whose key matches a protected field name.

    Walks dicts, lists, and JSON embedded in ``text`` content blocks, because
    MCP tool results frequently carry their payload as a JSON string rather
    than as structured content.
    """
    if depth > 12:  # guard against pathological nesting
        return node, 0

    count = 0
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            protected = isinstance(key, str) and key.lower() in fields
            if protected and isinstance(value, (str, int, float)):
                out[key] = "[REDACTED:PII]"
                count += 1
            else:
                new_value, n = _redact_values(value, fields, depth + 1)
                out[key] = new_value
                count += n
        return out, count

    if isinstance(node, list):
        results = [_redact_values(item, fields, depth + 1) for item in node]
        return [r[0] for r in results], sum(r[1] for r in results)

    if isinstance(node, str) and node.lstrip()[:1] in ("{", "["):
        try:
            parsed = json.loads(node)
        except json.JSONDecodeError:
            return node, 0
        new_value, n = _redact_values(parsed, fields, depth + 1)
        return (json.dumps(new_value), n) if n else (node, 0)

    return node, 0
