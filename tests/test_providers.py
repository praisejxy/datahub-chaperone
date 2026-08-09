"""Both context providers must answer in the same shape.

Chaperone's central promise is that a policy written and tested against the
bundled offline fixture runs unchanged against a real DataHub instance. That
promise lives entirely in these two classes projecting into the same
``AssetContext``, so it is worth testing directly rather than trusting that two
independently-edited files stay in step.

The GMS provider is exercised against a stub graph client. This is not a
substitute for testing against a live instance - it cannot catch a GraphQL
schema change - but it does catch the failure that actually bites: the two
providers drifting apart in field coverage or type spelling.
"""

from __future__ import annotations

from typing import Any

import pytest

from chaperone.graph.gms import GmsContextProvider
from chaperone.graph.offline import OfflineContextProvider
from chaperone.models import AssetContext

FCT_ORDERS = "urn:li:dataset:(urn:li:dataPlatform:dbt,analytics.marts.fct_orders,PROD)"
ORDER_ITEMS = "urn:li:dataset:(urn:li:dataPlatform:postgres,ecommerce.public.order_items,PROD)"
CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,ecommerce.public.customers,PROD)"


class StubGraph:
    """Replays canned GraphQL responses, shaped like a real GMS reply."""

    def __init__(self, entity: dict[str, Any], lineage: list[dict[str, Any]]) -> None:
        self._entity = entity
        self._lineage = lineage
        self.queries: list[str] = []

    def execute_graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.queries.append(query)
        if "searchAcrossLineage" in query:
            return {
                "searchAcrossLineage": {
                    "total": len(self._lineage) + 1,  # GMS counts the queried entity
                    "searchResults": self._lineage,
                }
            }
        return {"entity": self._entity}


def gms_with(entity: dict[str, Any], lineage: list[dict[str, Any]]) -> GmsContextProvider:
    provider = GmsContextProvider(server="http://stub", token=None)
    provider._graph = StubGraph(entity, lineage)
    return provider


# -- shape parity ---------------------------------------------------------

def test_both_providers_fill_the_same_fields():
    """A field one provider populates and the other silently leaves at its
    default is how a policy starts behaving differently in production."""
    offline = OfflineContextProvider().get_context(FCT_ORDERS)
    live = gms_with(
        entity={
            "urn": FCT_ORDERS,
            "type": "DATASET",
            "name": "fct_orders",
            "platform": {"name": "dbt"},
            "properties": {"description": "Order fact table."},
            "tags": {"tags": [{"tag": {"urn": "urn:li:tag:Tier1", "properties": {"name": "Tier1"}}}]},
            "glossaryTerms": {"terms": [{"term": {"urn": "urn:li:glossaryTerm:Revenue",
                                                  "properties": {"name": "Revenue"}}}]},
            "ownership": {"owners": [{"owner": {"urn": "urn:li:corpGroup:analytics-eng"}}]},
            "domain": {"domain": {"urn": "urn:li:domain:sales", "properties": {"name": "Sales"}}},
        },
        lineage=[
            {"degree": 1, "entity": {"urn": "urn:li:mlFeatureTable:(x,churn)", "type": "ML_FEATURE_TABLE"}},
            {"degree": 2, "entity": {"urn": "urn:li:mlModel:(x,churn_v4)", "type": "ML_MODEL"}},
            {"degree": 3, "entity": {"urn": "urn:li:dashboard:(looker,exec)", "type": "DASHBOARD"}},
        ],
    ).get_context(FCT_ORDERS)

    populated = {
        name
        for name in AssetContext.model_fields
        for ctx in (offline,)
        if getattr(ctx, name) != AssetContext.model_fields[name].get_default(call_default_factory=True)
    }
    for name in populated:
        assert getattr(live, name) or name == "query_count", (
            f"GMS provider leaves `{name}` empty where the offline provider fills it"
        )


def test_gms_reports_hop_distance_not_just_presence():
    live = gms_with(
        entity={"urn": FCT_ORDERS, "type": "DATASET", "name": "fct_orders"},
        lineage=[
            {"degree": 1, "entity": {"urn": "urn:li:dataset:(a,b,PROD)", "type": "DATASET"}},
            {"degree": 2, "entity": {"urn": "urn:li:mlModel:(x,churn_v4)", "type": "ML_MODEL"}},
            {"degree": 3, "entity": {"urn": "urn:li:dashboard:(looker,exec)", "type": "DASHBOARD"}},
        ],
    ).get_context(FCT_ORDERS)

    assert live.direct_downstream_count == 1
    assert live.downstream_type_hops == {"dataset": 1, "mlModel": 2, "dashboard": 3}
    assert live.critical_downstream_hops == 2


def test_gms_entity_types_use_the_same_spelling_as_policies():
    """GraphQL says ML_FEATURE_TABLE; urns and policy YAML say mlFeatureTable."""
    live = gms_with(
        entity={"urn": FCT_ORDERS, "type": "DATASET", "name": "fct_orders"},
        lineage=[{"degree": 1, "entity": {"urn": "urn:li:mlFeatureTable:(x,y)",
                                          "type": "ML_FEATURE_TABLE"}}],
    ).get_context(FCT_ORDERS)
    assert live.entity_type == "dataset"
    assert set(live.downstream_types) == {"mlFeatureTable"}


def test_missing_degree_is_treated_as_direct():
    """Fail closed. A GMS that omits `degree` must not read as zero blast radius."""
    live = gms_with(
        entity={"urn": FCT_ORDERS, "type": "DATASET", "name": "fct_orders"},
        lineage=[{"entity": {"urn": "urn:li:dashboard:(looker,exec)", "type": "DASHBOARD"}}],
    ).get_context(FCT_ORDERS)
    assert live.direct_downstream_count == 1
    assert live.critical_downstream_hops == 1


# -- failure behaviour ----------------------------------------------------

class BrokenGraph:
    def execute_graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("GMS is down")


def test_a_gms_outage_does_not_raise_into_the_agent():
    """Chaperone sits in the critical path of every tool call. If it raises,
    it takes the agent down with it - worse than the risk it guards against."""
    provider = GmsContextProvider(server="http://stub")
    provider._graph = BrokenGraph()
    ctx = provider.get_context(FCT_ORDERS)
    assert ctx.exists is False
    assert ctx.downstream_count == 0


def test_unknown_asset_is_reported_not_invented():
    live = gms_with(entity={}, lineage=[]).get_context("urn:li:dataset:(x,nope,PROD)")
    assert live.exists is False


# -- offline provider specifics -------------------------------------------

def test_offline_lineage_terminates_on_a_cycle(tmp_path):
    """Real catalogs contain lineage cycles. A DFS without a visited set hangs."""
    fixture = tmp_path / "cycle.json"
    fixture.write_text(
        """
        {"name": "cyclic", "assets": [
          {"urn": "urn:li:dataset:(p,a,PROD)", "type": "dataset", "name": "a",
           "upstreams": ["urn:li:dataset:(p,c,PROD)"]},
          {"urn": "urn:li:dataset:(p,b,PROD)", "type": "dataset", "name": "b",
           "upstreams": ["urn:li:dataset:(p,a,PROD)"]},
          {"urn": "urn:li:dataset:(p,c,PROD)", "type": "dataset", "name": "c",
           "upstreams": ["urn:li:dataset:(p,b,PROD)"]}
        ]}
        """,
        encoding="utf-8",
    )
    ctx = OfflineContextProvider(fixture).get_context("urn:li:dataset:(p,a,PROD)")
    assert ctx.downstream_count == 2
    assert ctx.direct_downstream_count == 1


def test_an_asset_with_no_consumers_has_no_blast_radius():
    ctx = OfflineContextProvider().get_context(
        "urn:li:dashboard:(looker,exec_revenue_overview)"
    )
    assert ctx.downstream_count == 0
    assert ctx.direct_downstream_count == 0
    assert ctx.critical_downstream_hops is None


def test_a_missing_fixture_says_what_to_do(tmp_path):
    with pytest.raises(FileNotFoundError, match="--fixture"):
        OfflineContextProvider(tmp_path / "absent.json")
