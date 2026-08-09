"""Live DataHub context provider.

Talks to a real DataHub instance over GraphQL to answer the same questions the
offline fixture graph answers. ``acryl-datahub`` is imported lazily so that the
zero-infrastructure path carries no heavy dependency.

Configuration follows DataHub's own conventions, so if the DataHub CLI or the
DataHub MCP server already works on your machine, Chaperone works too:

    DATAHUB_GMS_URL    e.g. http://localhost:8080  or  https://<tenant>.acryl.io
    DATAHUB_GMS_TOKEN  a personal access token
"""

from __future__ import annotations

import logging
import os
from typing import Any

from chaperone.models import AssetContext

logger = logging.getLogger(__name__)

# GraphQL reports entity types as SCREAMING_SNAKE enum names (`ML_FEATURE_TABLE`)
# while urns and DataHub's own docs use camelCase (`mlFeatureTable`). Policies are
# written in camelCase, so normalise here rather than making every rule author
# guess which spelling their backend produces.
CRITICAL_ENTITY_TYPES = frozenset(
    {"mlModel", "mlModelGroup", "mlFeatureTable", "mlFeature", "dashboard", "chart"}
)


def _normalise_type(raw: str | None) -> str:
    if not raw:
        return "unknown"
    if "_" not in raw:
        # Already camelCase, or a single lowercase word such as `DATASET`.
        return raw if any(c.islower() for c in raw) else raw.lower()
    head, *rest = raw.lower().split("_")
    return head + "".join(part.title() for part in rest)

# One query answers everything policy needs, so a decision costs a single round
# trip rather than one per signal. Lineage is capped at 1 hop here and expanded
# via searchAcrossLineage only when a rule actually asks for blast radius.
_ENTITY_QUERY = """
query ChaperoneContext($urn: String!) {
  entity(urn: $urn) {
    urn
    type
    ... on Dataset {
      name
      platform { name }
      deprecation { deprecated }
      properties { description }
      tags { tags { tag { urn properties { name } } } }
      glossaryTerms { terms { term { urn properties { name } } } }
      ownership { owners { owner { ... on CorpUser { urn } ... on CorpGroup { urn } } } }
      domain { domain { urn properties { name } } }
    }
    ... on Dashboard {
      properties { name description }
      tags { tags { tag { urn properties { name } } } }
      ownership { owners { owner { ... on CorpUser { urn } ... on CorpGroup { urn } } } }
    }
  }
}
"""

# The `degree` field is why this query is worth the round trip: it is DataHub's
# own hop distance, so the live provider derives the same hop-aware signals the
# offline BFS computes rather than approximating them.
_DOWNSTREAM_QUERY = """
query ChaperoneBlastRadius($urn: String!, $count: Int!) {
  searchAcrossLineage(
    input: {urn: $urn, direction: DOWNSTREAM, query: "*", start: 0, count: $count}
  ) {
    total
    searchResults {
      degree
      entity { urn type }
    }
  }
}
"""


class GmsContextProvider:
    """Reads asset context from a live DataHub GMS."""

    name = "gms"

    def __init__(
        self,
        server: str | None = None,
        token: str | None = None,
        max_downstream: int = 200,
    ) -> None:
        self.server = server or os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
        self.token = token or os.environ.get("DATAHUB_GMS_TOKEN")
        self.max_downstream = max_downstream
        self._graph: Any | None = None

    @property
    def graph(self) -> Any:
        if self._graph is None:
            try:
                from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph
            except ImportError as exc:  # pragma: no cover - dependency guidance
                raise ImportError(
                    "Live DataHub mode needs the DataHub SDK. Install it with:\n"
                    "    pip install 'datahub-chaperone[datahub]'\n"
                    "Or run offline with: chaperone serve --offline"
                ) from exc
            self._graph = DataHubGraph(
                DatahubClientConfig(server=self.server, token=self.token)
            )
        return self._graph

    def describe(self) -> str:
        auth = "token" if self.token else "no token"
        return f"live DataHub at {self.server} ({auth})"

    def check_connection(self) -> bool:
        try:
            return bool(self.graph.test_connection() or True)
        except Exception as exc:
            logger.warning("DataHub connection check failed: %s", exc)
            return False

    def get_context(self, urn: str) -> AssetContext:
        """Fetch context, degrading to 'unknown' rather than raising.

        Chaperone is in the critical path of every agent tool call. A GMS blip
        must not brick the agent, so a failed lookup yields no evidence and the
        call is judged on tool-name rules alone.
        """
        try:
            payload = self.graph.execute_graphql(_ENTITY_QUERY, variables={"urn": urn})
        except Exception as exc:
            logger.warning("Context lookup failed for %s: %s", urn, exc)
            return AssetContext(urn=urn, exists=False)

        entity = (payload or {}).get("entity")
        if not entity:
            return AssetContext(urn=urn, exists=False)

        props = entity.get("properties") or {}
        tags = [
            (t.get("tag", {}).get("properties") or {}).get("name")
            or t.get("tag", {}).get("urn", "").rsplit(":", 1)[-1]
            for t in ((entity.get("tags") or {}).get("tags") or [])
        ]
        terms = [
            (t.get("term", {}).get("properties") or {}).get("name")
            or t.get("term", {}).get("urn", "").rsplit(":", 1)[-1]
            for t in ((entity.get("glossaryTerms") or {}).get("terms") or [])
        ]
        owners = [
            o.get("owner", {}).get("urn")
            for o in ((entity.get("ownership") or {}).get("owners") or [])
            if o.get("owner", {}).get("urn")
        ]
        domain_node = ((entity.get("domain") or {}).get("domain") or {})

        return AssetContext(
            urn=urn,
            entity_type=_normalise_type(entity.get("type") or "DATASET"),
            name=entity.get("name") or props.get("name"),
            platform=((entity.get("platform") or {}).get("name")),
            tags=[t for t in tags if t],
            glossary_terms=[t for t in terms if t],
            owners=owners,
            domain=(domain_node.get("properties") or {}).get("name") or domain_node.get("urn"),
            tier=self._tier_from(tags),
            deprecated=bool((entity.get("deprecation") or {}).get("deprecated")),
            description=props.get("description"),
            exists=True,
            **self._lineage_facts(urn),
        )

    def _lineage_facts(self, urn: str) -> dict[str, Any]:
        """Blast-radius signals, shaped exactly like the offline provider's.

        Identical output between the two providers is what lets a policy written
        against the bundled fixture run unchanged against a real instance.
        """
        empty: dict[str, Any] = {
            "downstream_count": 0,
            "downstream_urns": [],
            "downstream_types": {},
            "direct_downstream_count": 0,
            "direct_downstream_types": {},
            "downstream_type_hops": {},
            "critical_downstream_hops": None,
        }
        try:
            payload = self.graph.execute_graphql(
                _DOWNSTREAM_QUERY, variables={"urn": urn, "count": self.max_downstream}
            )
        except Exception as exc:
            logger.debug("Blast-radius lookup failed for %s: %s", urn, exc)
            return empty

        result = (payload or {}).get("searchAcrossLineage") or {}
        urns: list[str] = []
        types: dict[str, int] = {}
        direct_types: dict[str, int] = {}
        type_hops: dict[str, int] = {}
        direct_count = 0

        for item in result.get("searchResults") or []:
            entity = item.get("entity") or {}
            if not entity.get("urn"):
                continue
            urns.append(entity["urn"])
            kind = _normalise_type(entity.get("type"))
            types[kind] = types.get(kind, 0) + 1
            # `degree` is DataHub's 1-based hop distance. Treat a missing value
            # as 1 so a GMS that omits it still gates writes, rather than
            # reporting an empty blast radius and quietly allowing everything.
            degree = int(item.get("degree") or 1)
            if degree < type_hops.get(kind, degree + 1):
                type_hops[kind] = degree
            if degree == 1:
                direct_count += 1
                direct_types[kind] = direct_types.get(kind, 0) + 1

        return {
            # searchAcrossLineage counts the queried entity itself in `total`.
            "downstream_count": max(int(result.get("total", 0)) - 1, len(urns)),
            "downstream_urns": urns,
            "downstream_types": types,
            "direct_downstream_count": direct_count,
            "direct_downstream_types": direct_types,
            "downstream_type_hops": type_hops,
            "critical_downstream_hops": min(
                (h for t, h in type_hops.items() if t in CRITICAL_ENTITY_TYPES),
                default=None,
            ),
        }

    @staticmethod
    def _tier_from(tags: list[str]) -> str | None:
        """DataHub conventionally encodes criticality as a Tier1/Tier2 tag."""
        for tag in tags:
            if tag and tag.lower().replace(" ", "").startswith("tier"):
                return tag
        return None
