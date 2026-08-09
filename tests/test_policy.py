"""Tests for the policy engine.

These are written as statements about *governance behaviour*, not about
implementation details, because the policy YAML is the part most likely to be
edited by a user. A test that breaks when someone reorders a rule would be noise;
these break only when the guarantees change.
"""

from __future__ import annotations

import pytest

from chaperone.graph import build_provider
from chaperone.models import ToolCall, Verdict, strictest
from chaperone.policy import Condition, Policy, PolicyEngine, Rule

CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,ecommerce.public.customers,PROD)"
ORDER_ITEMS = "urn:li:dataset:(urn:li:dataPlatform:postgres,ecommerce.public.order_items,PROD)"
FCT_ORDERS = "urn:li:dataset:(urn:li:dataPlatform:dbt,analytics.marts.fct_orders,PROD)"
DIM_CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:dbt,analytics.marts.dim_customers,PROD)"
LEGACY = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.legacy.orders_snapshot_2023,PROD)"
GHOST = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.marts.does_not_exist,PROD)"
EMAIL_FIELD = (
    "urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:postgres,"
    "ecommerce.public.customers,PROD),email)"
)


@pytest.fixture(scope="module")
def engine() -> PolicyEngine:
    return PolicyEngine(Policy.bundled("default"), build_provider(offline=True))


def verdict_for(engine: PolicyEngine, tool: str, **arguments) -> Verdict:
    return engine.evaluate(ToolCall(tool=tool, arguments=arguments, agent_id="test")).verdict


# -- reads ----------------------------------------------------------------

def test_plain_read_is_allowed(engine):
    assert verdict_for(engine, "search", query="orders") is Verdict.ALLOW


def test_read_of_sensitive_asset_is_redacted_not_blocked(engine):
    """An agent must still be able to work on a PII table - just not see values."""
    assert verdict_for(engine, "get_entities", urns=[CUSTOMERS]) is Verdict.REDACT


def test_read_of_unknown_asset_is_allowed(engine):
    """Catalogs are never complete; an unknown asset is not grounds to block a read."""
    assert verdict_for(engine, "get_entities", urns=[GHOST]) is Verdict.ALLOW


# -- writes ---------------------------------------------------------------

def test_low_risk_write_is_allowed(engine):
    """A leaf table with an owner and no sensitivity is the easy case."""
    assert verdict_for(engine, "update_description", urn=ORDER_ITEMS, description="x") is Verdict.ALLOW


def test_write_to_sensitive_asset_is_denied(engine):
    assert verdict_for(engine, "update_description", urn=CUSTOMERS, description="x") is Verdict.DENY


def test_adding_a_classification_to_sensitive_asset_is_not_denied(engine):
    """Protective changes must not be punished, or agents stop making them.

    The write may still be held for review because of blast radius, but it must
    never be an outright DENY.
    """
    assert verdict_for(engine, "add_tags", urn=CUSTOMERS, tags=["Verified"]) is not Verdict.DENY


def test_declassifying_sensitive_asset_is_denied(engine):
    assert verdict_for(engine, "remove_tags", urn=CUSTOMERS, tags=["PII"]) is Verdict.DENY


def test_write_to_high_blast_radius_asset_needs_review(engine):
    assert verdict_for(engine, "update_description", urn=FCT_ORDERS, description="x") is Verdict.REVIEW


def test_write_to_hallucinated_urn_is_denied(engine):
    """The single most common agent failure: confidently editing a table that
    does not exist."""
    assert verdict_for(engine, "update_description", urn=GHOST, description="x") is Verdict.DENY


def test_removal_of_curated_metadata_needs_review(engine):
    assert verdict_for(engine, "remove_tags", urn=DIM_CUSTOMERS, tags=["Tier1"]) is Verdict.REVIEW


def test_write_to_unowned_asset_needs_review(engine):
    assert verdict_for(engine, "set_domains", urn=LEGACY, domains=["Sales"]) is Verdict.REVIEW


def test_a_rule_cites_its_own_trigger_not_a_nearby_fact(engine):
    """Evidence must match the claim it is offered for.

    `dim_customers` has a dashboard one hop down and a feature table two hops
    down. The ML rule fired on the feature table, so it must say so - quoting
    the nearer dashboard's distance would be a true number offered as evidence
    for a different claim, which is exactly the failure Chaperone exists to
    catch in agents.
    """
    decision = engine.evaluate(
        ToolCall(tool="remove_tags", arguments={"urn": DIM_CUSTOMERS}, agent_id="test")
    )
    ml_hit = next(h for h in decision.hits if h.rule_id == "ml-downstream-review")
    assert "mlFeatureTable at 2 hops" in ml_hit.message
    assert "dashboard" not in ml_hit.message


# -- blast radius is measured in hops, not descendants --------------------

def test_leaf_table_and_core_table_are_told_apart(engine):
    """The signal that makes the blast-radius rule worth having.

    `order_items` and `fct_orders` have almost identical *transitive* reach in
    this fixture - both eventually feed the executive dashboard, because
    everything does. Only hop distance separates a raw source table nobody reads
    directly from a mart that five things read directly. If a future change
    makes these two verdicts equal, the rule has stopped discriminating and is
    reviewing (or ignoring) the whole warehouse.
    """
    provider = build_provider(offline=True)
    leaf = provider.get_context(ORDER_ITEMS)
    core = provider.get_context(FCT_ORDERS)

    assert leaf.downstream_count >= 8, "fixture no longer exercises a saturated graph"
    assert leaf.direct_downstream_count < core.direct_downstream_count

    assert verdict_for(engine, "update_description", urn=ORDER_ITEMS, description="x") is Verdict.ALLOW
    assert verdict_for(engine, "update_description", urn=FCT_ORDERS, description="x") is Verdict.REVIEW


def test_distance_to_production_ml_is_recorded(engine):
    """`fct_orders` -> churn_features -> churn_predictor_v4 is two hops."""
    ctx = build_provider(offline=True).get_context(FCT_ORDERS)
    assert ctx.downstream_type_hops["mlFeatureTable"] == 1
    assert ctx.downstream_type_hops["mlModel"] == 2
    assert ctx.critical_downstream_hops == 1


def test_within_hops_narrows_a_downstream_type_match():
    """The same rule fires or not purely on distance."""
    provider = build_provider(offline=True)

    def engine_with(hops: int | None) -> PolicyEngine:
        return PolicyEngine(
            Policy(name="t", rules=[
                Rule(id="ml", verdict=Verdict.REVIEW, message="ml",
                     when=Condition(tools=["*mutation*"],
                                    downstream_type_in=["mlModel"],
                                    within_hops=hops)),
            ]),
            provider,
        )

    # A model sits 2 hops below fct_orders: inside a 2-hop window, outside a 1-hop one.
    assert verdict_for(engine_with(2), "update_description", urn=FCT_ORDERS) is Verdict.REVIEW
    assert verdict_for(engine_with(1), "update_description", urn=FCT_ORDERS) is Verdict.ALLOW
    assert verdict_for(engine_with(None), "update_description", urn=FCT_ORDERS) is Verdict.REVIEW


# -- column-level ---------------------------------------------------------

def test_schema_field_inherits_parent_sensitivity(engine):
    """Column-level calls must not be a way around table-level classification."""
    decision = engine.evaluate(
        ToolCall(tool="update_description", arguments={"urn": EMAIL_FIELD}, agent_id="test")
    )
    assert decision.verdict is Verdict.DENY
    assert decision.contexts[0].exists
    assert "pii" in {t.lower() for t in decision.contexts[0].tags}


# -- evidence and explanation --------------------------------------------

def test_blocked_decision_explains_itself_with_catalog_evidence(engine):
    decision = engine.evaluate(
        ToolCall(tool="update_description", arguments={"urn": CUSTOMERS}, agent_id="test")
    )
    explanation = decision.explain()
    assert "DENY" in explanation
    assert "PII" in explanation
    # An agent needs to know nothing changed, not just that it failed.
    assert "Nothing was changed" in explanation


def test_review_explanation_tells_agent_work_was_preserved(engine):
    decision = engine.evaluate(
        ToolCall(tool="update_description", arguments={"urn": FCT_ORDERS}, agent_id="test")
    )
    assert "proposal" in decision.explain().lower()


def test_hits_carry_the_triggering_evidence(engine):
    decision = engine.evaluate(
        ToolCall(tool="update_description", arguments={"urn": FCT_ORDERS}, agent_id="test")
    )
    hit = next(h for h in decision.hits if h.rule_id == "high-blast-radius-review")
    assert hit.evidence["direct_downstream_count"] >= 3
    assert hit.target_urn == FCT_ORDERS


# -- resolution semantics -------------------------------------------------

def test_strictest_verdict_wins():
    assert strictest(Verdict.ALLOW, Verdict.DENY, Verdict.REVIEW) is Verdict.DENY
    assert strictest(Verdict.ALLOW, Verdict.REDACT) is Verdict.REDACT
    assert strictest() is Verdict.ALLOW


def test_a_permissive_rule_cannot_unlock_a_denied_call():
    """Policy is additive. Adding an `allow` rule must not create a hole."""
    policy = Policy(
        name="test",
        rules=[
            Rule(id="deny-all-writes", verdict=Verdict.DENY, message="no",
                 when=Condition(tools=["*mutation*"])),
            Rule(id="allow-everything", verdict=Verdict.ALLOW, message="yes",
                 when=Condition(tools=["*"])),
        ],
    )
    engine = PolicyEngine(policy, build_provider(offline=True))
    assert verdict_for(engine, "add_tags", urn=ORDER_ITEMS) is Verdict.DENY


def test_multiple_targets_take_the_strictest(engine):
    """A batch call is as risky as its riskiest member."""
    assert verdict_for(engine, "add_tags", urns=[ORDER_ITEMS, CUSTOMERS], tags=["x"]) is not Verdict.ALLOW


def test_disabled_rules_do_not_fire():
    policy = Policy(
        name="test",
        rules=[Rule(id="off", verdict=Verdict.DENY, message="no", enabled=False,
                    when=Condition(tools=["*"]))],
    )
    engine = PolicyEngine(policy, build_provider(offline=True))
    assert verdict_for(engine, "add_tags", urn=ORDER_ITEMS) is Verdict.ALLOW


# -- target extraction ----------------------------------------------------

def test_urns_are_found_wherever_they_appear():
    """Tool argument shapes vary across the DataHub MCP surface."""
    assert ToolCall(tool="t", arguments={"urn": CUSTOMERS}).targets() == [CUSTOMERS]
    assert ToolCall(tool="t", arguments={"urns": [CUSTOMERS]}).targets() == [CUSTOMERS]
    assert ToolCall(tool="t", arguments={"dataset": CUSTOMERS}).targets() == [CUSTOMERS]
    nested = ToolCall(tool="t", arguments={"targets": [{"entity_urn": CUSTOMERS}]})
    assert nested.targets() == [CUSTOMERS]


def test_duplicate_targets_are_collapsed():
    call = ToolCall(tool="t", arguments={"a": CUSTOMERS, "b": CUSTOMERS})
    assert call.targets() == [CUSTOMERS]


def test_non_urn_strings_are_ignored():
    assert ToolCall(tool="t", arguments={"query": "select * from customers"}).targets() == []


def test_call_with_no_targets_still_evaluates(engine):
    """Blanket, asset-agnostic rules must fire even with nothing to point at."""
    decision = engine.evaluate(ToolCall(tool="get_me", arguments={}, agent_id="test"))
    assert decision.verdict is Verdict.ALLOW
    assert decision.contexts == []
