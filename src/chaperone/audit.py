"""Audit trail and DataHub writeback.

Every decision is appended to a local JSONL log, which is the always-available
record. When a live DataHub is configured, decisions additionally become
metadata in the graph:

* the agent itself, as an ``aiAgent`` entity with the datasets it consumed
  attached as upstream lineage;
* blocked mutations, as proposal records a human can act on.

That second half is the point. A log file tells you what your agent did. Writing
it into the catalog means the *catalog* can answer "which agents touch this
table?" - a question DataHub cannot answer today, and one that gets more urgent
as more agents get write access.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from chaperone.models import Decision

logger = logging.getLogger(__name__)

def default_audit_dir() -> Path:
    """Where decisions are written, resolved per call rather than at import.

    Reading ``CHAPERONE_HOME`` lazily means a host that sets it after importing
    Chaperone still gets the directory it asked for. Resolving it once at import
    time silently ignores the setting, which is the kind of bug that only shows
    up as "the audit log is empty" long after the session that needed it.
    """
    return Path(os.environ.get("CHAPERONE_HOME") or Path.home() / ".chaperone")


class AuditLog:
    """Append-only decision log, with optional DataHub writeback."""

    def __init__(
        self,
        path: Path | str | None = None,
        agent_id: str = "unregistered-agent",
        session_id: str | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.path = Path(path) if path else default_audit_dir() / "decisions.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        self.decisions: list[Decision] = []
        self.proposals: list[dict[str, Any]] = []
        self._touched: dict[str, set[str]] = {"read": set(), "written": set(), "blocked": set()}

    # -- recording ---------------------------------------------------------

    def record(self, decision: Decision) -> None:
        self.decisions.append(decision)
        self._track_assets(decision)

        entry = {
            "ts": time.time(),
            "session": self.session_id,
            "agent": decision.call.agent_id,
            "tool": decision.call.tool,
            "verdict": decision.verdict.value,
            "severity": decision.severity.value,
            "targets": decision.call.targets(),
            "rules": [
                {"id": h.rule_id, "verdict": h.verdict.value, "message": h.message.strip()}
                for h in decision.hits
            ],
            "elapsed_ms": round(decision.elapsed_ms, 3),
        }
        self._handle.write(json.dumps(entry) + "\n")
        self._handle.flush()

    def _track_assets(self, decision: Decision) -> None:
        """Attribute the call's targets to read / written / blocked.

        Targets the catalog says do not exist are skipped. Everything tracked
        here is later written back to DataHub - blocked assets get tagged, read
        and written ones become agent lineage - and emitting an aspect against
        an unknown urn *creates* that entity. Tracking a hallucinated urn would
        therefore make Chaperone commit the very thing
        ``unknown-asset-mutation-deny`` exists to refuse.
        """
        from chaperone.policy import MUTATION_TOOLS

        missing = {ctx.urn for ctx in decision.contexts if not ctx.exists}
        is_write = decision.call.tool in MUTATION_TOOLS
        for urn in decision.call.targets():
            if urn in missing:
                continue
            if decision.blocked:
                self._touched["blocked"].add(urn)
            elif is_write:
                self._touched["written"].add(urn)
            else:
                self._touched["read"].add(urn)

    def record_proposal(self, decision: Decision) -> str:
        """Capture a blocked mutation as a reviewable proposal.

        A REVIEW verdict must not lose the agent's work. The intent is stored
        verbatim so a human can accept it later, either through
        ``chaperone proposals apply`` or DataHub's own proposal workflow.
        """
        proposal_id = f"chap-{self.session_id}-{len(self.proposals) + 1:03d}"
        record = {
            "id": proposal_id,
            "ts": time.time(),
            "agent": decision.call.agent_id,
            "tool": decision.call.tool,
            "arguments": decision.call.arguments,
            "targets": decision.call.targets(),
            "reasons": [h.message.strip() for h in decision.hits],
            "status": "pending",
        }
        self.proposals.append(record)

        proposals_path = self.path.parent / "proposals.jsonl"
        with proposals_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        return proposal_id

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    # -- reporting ---------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for decision in self.decisions:
            key = decision.verdict.value
            counts[key] = counts.get(key, 0) + 1
        return {
            "session": self.session_id,
            "agent": self.agent_id,
            "total_calls": len(self.decisions),
            "verdicts": counts,
            "assets_read": sorted(self._touched["read"]),
            "assets_written": sorted(self._touched["written"]),
            "assets_blocked": sorted(self._touched["blocked"]),
            "proposals": len(self.proposals),
        }

    def consumed_datasets(self) -> list[str]:
        """Dataset URNs the agent actually read - its upstream lineage."""
        return sorted(
            urn
            for urn in (self._touched["read"] | self._touched["written"])
            if urn.startswith("urn:li:dataset:")
        )


def _skill_urn(skill: str) -> str:
    """``documentation`` -> ``urn:li:agentSkill:documentation``, idempotently."""
    return skill if skill.startswith("urn:li:agentSkill:") else f"urn:li:agentSkill:{skill}"


def _valid_dataset_urns(urns: list[str]) -> list[str]:
    """Keep only well-formed dataset urns.

    A single malformed urn makes the SDK reject the whole ``Agent``, which would
    lose the entire session's lineage. Dropping the bad one and registering the
    rest is the better failure: partial lineage beats none.
    """
    from datahub.metadata.urns import DatasetUrn

    kept: list[str] = []
    for urn in urns:
        try:
            DatasetUrn.from_string(urn)
            kept.append(urn)
        except Exception:
            logger.warning("skipping malformed dataset urn in agent lineage: %s", urn)
    return kept


def build_agent_entity(
    agent_id: str,
    name: str,
    description: str,
    consumed_datasets: list[str],
    owners: list[str] | None = None,
    skills: list[str] | None = None,
    platform: str = "mcp",
) -> Any | None:
    """Build the ``aiAgent`` entity for a session, without needing a connection.

    Kept separate from :meth:`DataHubWriteback.register_agent` so the payload can
    be produced and inspected offline. Emitting requires a GMS; *constructing*
    does not, and a reviewer with no DataHub instance should still be able to see
    exactly what Chaperone would write into the graph.

    Returns ``None`` when the entity cannot be built - most often because
    ``acryl-datahub`` is not installed, which is a supported configuration: the
    SDK is an optional dependency and the policy engine must run without it.
    """
    try:
        from datahub.api.entities.agent.agent import Agent

        return Agent(
            id=agent_id,
            name=name,
            description=description,
            # The SDK validates these as urns, not names. Coercing here rather
            # than asking callers to know the urn grammar - and because the
            # alternative is a validation error swallowed by the caller's
            # handler, which would report success and write nothing.
            skills=[_skill_urn(s) for s in (skills or [])],
            consumes_datasets=_valid_dataset_urns(consumed_datasets),
            owners=list(owners or []),
            platform=platform,
        )
    except ImportError:
        logger.info("acryl-datahub not installed; agent entity not built")
        return None
    except Exception as exc:
        logger.warning("agent entity build failed: %s", exc)
        return None


def agent_entity_payload(agent: Any | None) -> list[dict[str, Any]]:
    """Render an ``Agent``'s metadata change proposals as plain JSON.

    This is the writeback made inspectable: the same aspects that would be sent
    to GMS, as data a person can read in a diff or a sample-output file. A
    ``None`` agent (no SDK installed) renders as an empty payload.
    """
    payload: list[dict[str, Any]] = []
    if agent is None:
        return payload
    for mcp in agent.generate_mcp():
        aspect = getattr(mcp, "aspect", None)
        payload.append(
            {
                "entityUrn": mcp.entityUrn,
                "aspectName": mcp.aspectName,
                "aspect": aspect.to_obj() if hasattr(aspect, "to_obj") else aspect,
            }
        )
    return payload


class DataHubWriteback:
    """Publishes Chaperone's findings into a live DataHub instance.

    Import of the DataHub SDK is deferred so the offline path stays dependency
    free. Every method degrades to a logged warning rather than raising: failing
    to *record* an agent's activity must never break the agent.
    """

    def __init__(self, server: str | None = None, token: str | None = None) -> None:
        self.server = server or os.environ.get("DATAHUB_GMS_URL")
        self.token = token or os.environ.get("DATAHUB_GMS_TOKEN")
        self._emitter: Any | None = None

    @property
    def available(self) -> bool:
        return bool(self.server)

    def _get_emitter(self) -> Any:
        if self._emitter is None:
            from datahub.emitter.rest_emitter import DatahubRestEmitter

            self._emitter = DatahubRestEmitter(gms_server=self.server, token=self.token)
        return self._emitter

    def register_agent(
        self,
        agent_id: str,
        name: str,
        description: str,
        consumed_datasets: list[str],
        owners: list[str] | None = None,
        skills: list[str] | None = None,
        platform: str = "mcp",
    ) -> str | None:
        """Create/update the ``aiAgent`` entity and attach consumed datasets.

        This is what turns an agent from an anonymous API client into a node in
        the lineage graph, so an engineer looking at a table can see which
        agents read it.
        """
        if not self.available:
            logger.info("no DATAHUB_GMS_URL set; skipping agent registration")
            return None
        try:
            agent = build_agent_entity(
                agent_id=agent_id,
                name=name,
                description=description,
                consumed_datasets=consumed_datasets,
                owners=owners,
                skills=skills,
                platform=platform,
            )
            for mcp in agent.generate_mcp():
                self._get_emitter().emit(mcp)
            logger.info(
                "registered agent %s with %d consumed datasets", agent.urn, len(consumed_datasets)
            )
            return agent.urn
        except Exception as exc:
            logger.warning("agent registration failed: %s", exc)
            return None

    def annotate_blocked_asset(self, urn: str, reason: str, tag: str = "AgentBlocked") -> bool:
        """Tag an asset an agent was stopped from changing.

        Makes the intervention visible in the catalog UI rather than only in a
        log file nobody reads.
        """
        if not self.available:
            return False
        try:
            from datahub.emitter.mcp import MetadataChangeProposalWrapper
            from datahub.metadata.schema_classes import (
                GlobalTagsClass,
                TagAssociationClass,
            )

            mcp = MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=GlobalTagsClass(
                    tags=[TagAssociationClass(tag=f"urn:li:tag:{tag}", context=reason[:200])]
                ),
            )
            self._get_emitter().emit(mcp)
            return True
        except Exception as exc:
            logger.warning("could not annotate %s: %s", urn, exc)
            return False
