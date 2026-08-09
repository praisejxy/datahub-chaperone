"""Tests for the audit log and DataHub writeback.

The writeback is the part of Chaperone that *contributes back* to the context
graph rather than only reading it, and it is also the part most likely to fail
invisibly: every method deliberately swallows exceptions so that a governance
failure never takes the agent down with it. That design is right, but it means a
broken payload would look exactly like a success. These tests therefore assert
on the aspects actually generated, not on the absence of an exception.

No live DataHub is required: the SDK builds the metadata change proposals
locally, and a stub emitter captures them.
"""

from __future__ import annotations

from typing import Any

import pytest

from chaperone.audit import AuditLog, DataHubWriteback
from chaperone.graph import build_provider
from chaperone.models import ToolCall, Verdict
from chaperone.policy import Policy, PolicyEngine

pytest.importorskip("datahub", reason="needs the [datahub] extra")

CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,ecommerce.public.customers,PROD)"
ORDER_ITEMS = "urn:li:dataset:(urn:li:dataPlatform:postgres,ecommerce.public.order_items,PROD)"
FCT_ORDERS = "urn:li:dataset:(urn:li:dataPlatform:dbt,analytics.marts.fct_orders,PROD)"


class StubEmitter:
    """Captures MCPs instead of sending them to a GMS."""

    def __init__(self) -> None:
        self.emitted: list[Any] = []

    def emit(self, mcp: Any) -> None:
        self.emitted.append(mcp)


def writeback_with_stub() -> tuple[DataHubWriteback, StubEmitter]:
    wb = DataHubWriteback(server="http://stub:8080", token=None)
    emitter = StubEmitter()
    wb._emitter = emitter
    return wb, emitter


def aspect_names(emitter: StubEmitter) -> set[str]:
    return {type(m.aspect).__name__ for m in emitter.emitted}


def aspect(emitter: StubEmitter, name: str) -> Any:
    return next(m for m in emitter.emitted if type(m.aspect).__name__ == name)


# -- agent registration ---------------------------------------------------

def test_registering_an_agent_puts_it_in_the_lineage_graph():
    """The point of the whole exercise.

    An agent that only reads metadata is invisible to the catalog. Emitting
    UpstreamLineage against the aiAgent entity is what lets an engineer looking
    at `customers` see that an agent consumed it.
    """
    wb, emitter = writeback_with_stub()
    urn = wb.register_agent(
        agent_id="catalog-steward-agent",
        name="Catalog Steward",
        description="Documents datasets under Chaperone supervision.",
        consumed_datasets=[CUSTOMERS, ORDER_ITEMS],
        skills=["documentation"],
    )

    assert urn == "urn:li:aiAgent:catalog-steward-agent"
    assert "AIAgentInfoClass" in aspect_names(emitter)

    lineage = aspect(emitter, "UpstreamLineageClass")
    assert {u.dataset for u in lineage.aspect.upstreams} == {CUSTOMERS, ORDER_ITEMS}


def test_bare_skill_names_are_accepted():
    """The SDK validates skills as urns. Callers should not have to know that.

    Without coercion this raises inside `register_agent`, which catches it and
    returns None - a silent no-op indistinguishable from success. Regression
    test for exactly that.
    """
    wb, emitter = writeback_with_stub()
    urn = wb.register_agent(
        agent_id="a", name="A", description="",
        consumed_datasets=[CUSTOMERS],
        skills=["documentation", "urn:li:agentSkill:tagging"],
    )
    assert urn is not None, "bare skill names must not silently abort registration"
    assert emitter.emitted


def test_one_malformed_urn_does_not_lose_the_whole_session():
    """Partial lineage beats none."""
    wb, emitter = writeback_with_stub()
    urn = wb.register_agent(
        agent_id="a", name="A", description="",
        consumed_datasets=[CUSTOMERS, "dim_customer", ""],
    )
    assert urn is not None
    lineage = aspect(emitter, "UpstreamLineageClass")
    assert {u.dataset for u in lineage.aspect.upstreams} == {CUSTOMERS}


def test_writeback_is_skipped_when_no_server_is_configured():
    """Offline is the default path; it must not error or hang."""
    wb = DataHubWriteback(server=None, token=None)
    assert wb.available is False
    assert wb.register_agent("a", "A", "", [CUSTOMERS]) is None
    assert wb.annotate_blocked_asset(CUSTOMERS, "blocked") is False


class BrokenEmitter:
    def emit(self, mcp: Any) -> None:
        raise RuntimeError("GMS refused the write")


def test_a_writeback_failure_never_reaches_the_agent():
    """Failing to record an agent's activity must not break the agent."""
    wb = DataHubWriteback(server="http://stub:8080")
    wb._emitter = BrokenEmitter()
    assert wb.register_agent("a", "A", "", [CUSTOMERS]) is None
    assert wb.annotate_blocked_asset(CUSTOMERS, "blocked") is False


def test_blocked_assets_are_annotated_with_the_reason():
    wb, emitter = writeback_with_stub()
    assert wb.annotate_blocked_asset(CUSTOMERS, "tagged PII; owner sign-off required")
    tags = aspect(emitter, "GlobalTagsClass")
    assert tags.entityUrn == CUSTOMERS
    assert tags.aspect.tags[0].tag == "urn:li:tag:AgentBlocked"
    assert "PII" in (tags.aspect.tags[0].context or "")


# -- audit log ------------------------------------------------------------

@pytest.fixture()
def engine() -> PolicyEngine:
    return PolicyEngine(Policy.bundled("default"), build_provider(offline=True))


@pytest.fixture()
def log(tmp_path, monkeypatch) -> AuditLog:
    monkeypatch.setenv("CHAPERONE_HOME", str(tmp_path))
    return AuditLog(agent_id="test-agent")


def test_the_log_records_what_the_agent_consumed(log, engine):
    for call in (
        ToolCall(tool="get_entities", arguments={"urns": [CUSTOMERS]}, agent_id="test-agent"),
        ToolCall(tool="update_description", arguments={"urn": ORDER_ITEMS}, agent_id="test-agent"),
    ):
        log.record(engine.evaluate(call))

    assert set(log.consumed_datasets()) == {CUSTOMERS, ORDER_ITEMS}
    assert log.summary()["total_calls"] == 2


def test_only_dataset_urns_become_lineage(log, engine):
    """A dashboard the agent read is not an upstream dataset."""
    log.record(engine.evaluate(ToolCall(
        tool="get_entities",
        arguments={"urns": ["urn:li:dashboard:(looker,customer_360)"]},
        agent_id="test-agent",
    )))
    assert log.consumed_datasets() == []


def test_a_review_preserves_the_agents_work(log, engine):
    """A blocked call that discards the agent's intent just makes it retry."""
    call = ToolCall(
        tool="update_description",
        arguments={"urn": FCT_ORDERS, "description": "Order fact table."},
        agent_id="test-agent",
    )
    decision = engine.evaluate(call)
    assert decision.verdict is Verdict.REVIEW

    proposal_id = log.record_proposal(decision)
    saved = log.proposals[-1]
    assert proposal_id.startswith("chap-")
    assert saved["arguments"]["description"] == "Order fact table."
    assert saved["reasons"], "a proposal with no stated reason cannot be triaged"
    assert saved["status"] == "pending"


def test_the_log_survives_a_restart(tmp_path, monkeypatch, engine):
    """The decision log is append-only; a new session must not truncate it."""
    monkeypatch.setenv("CHAPERONE_HOME", str(tmp_path))
    call = ToolCall(tool="get_entities", arguments={"urns": [CUSTOMERS]}, agent_id="a")

    first = AuditLog(agent_id="a")
    first.record(engine.evaluate(call))
    first.close()

    second = AuditLog(agent_id="a")
    second.record(engine.evaluate(call))
    second.close()

    lines = (tmp_path / "decisions.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
