"""Core vocabulary for Chaperone.

Three ideas carry the whole system:

* :class:`AssetContext` - what DataHub knows about a thing an agent is about to
  touch. This is the evidence a decision is made from.
* :class:`ToolCall` - a single attempt by an agent to use a DataHub MCP tool.
* :class:`Decision` - the verdict, the reasons behind it, and the evidence.
  Decisions are the durable artifact: they are what gets written back into the
  catalog so the next agent inherits the knowledge.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Verdict(str, Enum):
    """What Chaperone decided to do with a tool call."""

    ALLOW = "allow"
    """Forward the call upstream unchanged."""

    REVIEW = "review"
    """Do not mutate. Convert the intent into a DataHub proposal for a human."""

    REDACT = "redact"
    """Forward the call, but strip sensitive values out of the response."""

    DENY = "deny"
    """Refuse the call and explain why, using catalog evidence."""


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_VERDICT_RANK = {
    Verdict.ALLOW: 0,
    Verdict.REDACT: 1,
    Verdict.REVIEW: 2,
    Verdict.DENY: 3,
}


def strictest(*verdicts: Verdict) -> Verdict:
    """Return the most restrictive verdict.

    Policy rules are additive and never cancel each other out: if any rule says
    DENY, the answer is DENY. A permissive rule cannot unlock what a stricter
    one closed.
    """
    if not verdicts:
        return Verdict.ALLOW
    return max(verdicts, key=lambda v: _VERDICT_RANK[v])


class AssetContext(BaseModel):
    """DataHub's knowledge about a single asset (or schema field).

    Deliberately flat and provider-agnostic: the offline fixture graph and a
    live GMS instance both project into this shape, so policy never has to care
    which backend answered.
    """

    urn: str
    entity_type: str = "dataset"
    name: str | None = None
    platform: str | None = None

    tags: list[str] = Field(default_factory=list)
    glossary_terms: list[str] = Field(default_factory=list)
    owners: list[str] = Field(default_factory=list)
    domain: str | None = None
    tier: str | None = None
    deprecated: bool = False
    description: str | None = None

    downstream_count: int = 0
    """All assets that would eventually be affected by a change here.

    Kept for reporting and explanations; a poor rule threshold on its own,
    because in a connected warehouse everything eventually reaches everything.
    """

    direct_downstream_count: int = 0
    """Assets one hop downstream - what breaks immediately on a change."""

    critical_downstream_hops: int | None = None
    """Hop distance to the first dashboard or deployed model downstream, if any.

    ``1`` means a model or dashboard reads this asset directly; ``None`` means
    no critical asset is reachable from here at all.
    """

    downstream_urns: list[str] = Field(default_factory=list)
    downstream_types: dict[str, int] = Field(default_factory=dict)
    """e.g. {"dashboard": 3, "mlFeatureTable": 1} - who feels the pain."""

    direct_downstream_types: dict[str, int] = Field(default_factory=dict)
    """Same breakdown, restricted to one hop - who feels it immediately."""

    downstream_type_hops: dict[str, int] = Field(default_factory=dict)
    """Nearest hop distance per downstream entity type.

    e.g. ``{"dataset": 1, "mlFeatureTable": 2, "dashboard": 3}``. This is the
    honest version for rules: transitively, almost every table in a warehouse
    "feeds a dashboard", so only the distance carries information.
    """

    query_count: int = 0
    exists: bool = True

    @property
    def labels(self) -> set[str]:
        """Tags and glossary terms, lowercased, for case-insensitive matching."""
        return {t.lower() for t in (*self.tags, *self.glossary_terms)}

    def has_label(self, label: str) -> bool:
        return label.lower() in self.labels

    def is_field(self) -> bool:
        """True for schemaField urns (column-level targets)."""
        return self.urn.startswith("urn:li:schemaField:")


class ToolCall(BaseModel):
    """An agent's attempt to invoke one DataHub MCP tool."""

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    agent_id: str = "unknown-agent"
    session_id: str | None = None
    request_id: str | int | None = None
    timestamp: float = Field(default_factory=time.time)

    intent: str | None = None
    """Plain-language description of what the agent was trying to accomplish.

    Only present for scripted scenarios and clients that volunteer it. Used for
    display, never for policy: an agent's stated intent is not evidence.
    """

    def targets(self) -> list[str]:
        """Extract the DataHub URNs this call acts on.

        The DataHub MCP tools are not uniform - targets arrive as ``urn``,
        ``urns``, ``entity_urn``, ``dataset``, and so on, sometimes as a bare
        string and sometimes as a list. Rather than hardcode a per-tool table
        that silently rots when upstream adds a tool, we sweep the arguments
        for anything URN-shaped. Over-collecting is safe here: an extra target
        can only ever make the verdict stricter, never more permissive.
        """
        found: list[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, str):
                if value.startswith("urn:li:"):
                    found.append(value)
            elif isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    walk(item)

        walk(self.arguments)
        # Preserve order, drop duplicates.
        return list(dict.fromkeys(found))


class RuleHit(BaseModel):
    """One policy rule that fired, and why."""

    rule_id: str
    verdict: Verdict
    severity: Severity = Severity.MEDIUM
    message: str
    target_urn: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class Decision(BaseModel):
    """The outcome of chaperoning one tool call."""

    call: ToolCall
    verdict: Verdict
    hits: list[RuleHit] = Field(default_factory=list)
    contexts: list[AssetContext] = Field(default_factory=list)
    elapsed_ms: float = 0.0

    @property
    def severity(self) -> Severity:
        order = [Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
        if not self.hits:
            return Severity.INFO
        return max((h.severity for h in self.hits), key=order.index)

    @property
    def blocked(self) -> bool:
        return self.verdict in (Verdict.DENY, Verdict.REVIEW)

    def reasons(self) -> list[str]:
        return [h.message for h in self.hits]

    def explain(self) -> str:
        """Human- and agent-readable justification.

        This text is what an agent sees when it is stopped, so it states the
        catalog evidence rather than just refusing. A good refusal teaches the
        agent what to do instead.
        """
        if not self.hits:
            return f"Allowed: no policy concerns for `{self.call.tool}`."

        lines = [f"Chaperone verdict: **{self.verdict.value.upper()}** for `{self.call.tool}`", ""]
        for hit in self.hits:
            target = f" — `{hit.target_urn}`" if hit.target_urn else ""
            lines.append(f"- [{hit.rule_id}] {hit.message}{target}")

        if self.verdict is Verdict.REVIEW:
            lines += [
                "",
                "This change was not discarded. It has been captured as a DataHub "
                "proposal for a human owner to accept or reject.",
            ]
        elif self.verdict is Verdict.DENY:
            lines += [
                "",
                "Nothing was changed in DataHub. If this edit is genuinely intended, "
                "an owner of the asset must make it, or the policy must be amended.",
            ]
        return "\n".join(lines)
