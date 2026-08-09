"""Offline context graph, loaded from a JSON fixture.

Chaperone must be runnable by someone who has just cloned the repo and has no
DataHub instance, no Docker, and no credentials. This provider reads a small
metadata graph from disk and answers the same questions a live GMS would.

The fixture format is intentionally close to DataHub's own concepts (urns, tags,
glossary terms, ownership, tiers, upstream lineage) so that what you learn from
the offline graph transfers directly to the real thing.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from chaperone.models import AssetContext

DEFAULT_FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "ecommerce.json"

CRITICAL_ENTITY_TYPES = frozenset(
    {"mlModel", "mlModelGroup", "mlFeatureTable", "mlFeature", "dashboard", "chart"}
)
"""Entity types whose breakage is visible outside the data team.

Datasets are excluded on purpose: a dataset one hop down is an ordinary
dependency, while a deployed model or an executive dashboard one hop down means a
metadata change is immediately load-bearing.
"""


def _entity_type_of(urn: str) -> str:
    """Derive an entity type from a URN without needing the DataHub SDK.

    ``urn:li:dataset:(urn:li:dataPlatform:snowflake,db.tbl,PROD)`` -> ``dataset``
    """
    if not urn.startswith("urn:li:"):
        return "unknown"
    remainder = urn[len("urn:li:") :]
    return remainder.split(":", 1)[0] or "unknown"


class OfflineContextProvider:
    """Answers context questions from a bundled JSON metadata graph."""

    name = "offline"

    def __init__(self, fixture: Path | str | None = None) -> None:
        self.fixture_path = Path(fixture) if fixture else DEFAULT_FIXTURE
        if not self.fixture_path.exists():
            raise FileNotFoundError(
                f"Chaperone fixture graph not found: {self.fixture_path}. "
                "Pass --fixture to point at your own exported graph."
            )
        raw: dict[str, Any] = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        self.label: str = raw.get("name", self.fixture_path.stem)
        self._assets: dict[str, dict[str, Any]] = {a["urn"]: a for a in raw.get("assets", [])}

        # Lineage is stored as upstream edges (mirroring DataHub's
        # upstreamLineage aspect). Blast radius needs the reverse direction, so
        # invert once at load time.
        self._downstream: dict[str, list[str]] = defaultdict(list)
        for asset in self._assets.values():
            for upstream in asset.get("upstreams", []):
                self._downstream[upstream].append(asset["urn"])

        # Field-level edges let column rules reason about blast radius too.
        for edge in raw.get("field_lineage", []):
            self._downstream[edge["upstream"]].append(edge["downstream"])

        # Per-instance, not @lru_cache on the method. A cache on the method is
        # stored on the class and keyed on `self`, which keeps every provider
        # ever constructed alive for the life of the process. Holding it here
        # means the cache dies with the provider that owns it.
        self._hop_cache: dict[str, tuple[tuple[str, ...], ...]] = {}

    def describe(self) -> str:
        return (
            f"offline graph '{self.label}' "
            f"({len(self._assets)} assets, {self.fixture_path.name})"
        )

    def _downstream_by_hop(self, urn: str) -> tuple[tuple[str, ...], ...]:
        """Downstream assets grouped by distance, nearest first.

        Breadth-first rather than depth-first because *hop distance is the
        signal*. A purely transitive count collapses under its own weight: in a
        connected warehouse nearly every table eventually reaches the executive
        dashboard, so counting all descendants scores a leaf table the same as a
        core fact table and the policy stops discriminating between them.

        Keeping the hop structure lets a rule say "one hop from a deployed
        model" instead of "somewhere upstream of one", which is the difference
        between a useful warning and noise. Uses a visited set, so the cycles
        that real catalogs contain terminate instead of looping.
        """
        cached = self._hop_cache.get(urn)
        if cached is not None:
            return cached

        levels: list[tuple[str, ...]] = []
        seen: set[str] = {urn}
        frontier = [u for u in self._downstream.get(urn, []) if u not in seen]
        while frontier:
            seen.update(frontier)
            levels.append(tuple(sorted(frontier)))
            frontier = sorted(
                {
                    nxt
                    for current in frontier
                    for nxt in self._downstream.get(current, [])
                    if nxt not in seen
                }
            )
        result = tuple(levels)
        self._hop_cache[urn] = result
        return result

    def _transitive_downstream(self, urn: str) -> tuple[str, ...]:
        return tuple(sorted(u for level in self._downstream_by_hop(urn) for u in level))

    def _lineage_facts(self, urn: str) -> dict[str, Any]:
        """The lineage numbers policy rules actually consult.

        Several different questions, deliberately kept separate:

        * ``direct_downstream_count`` - what breaks immediately.
        * ``downstream_type_hops`` - for each kind of downstream entity, how
          many hops away the nearest one is. This is what lets a rule say
          "a deployed model reads this directly" instead of the much weaker
          "a deployed model is somewhere downstream of this".
        * ``downstream_count`` - the full transitive reach, kept for reporting
          and for the explanation text, but a poor rule threshold on its own.
        """
        levels = self._downstream_by_hop(urn)
        transitive = [u for level in levels for u in level]

        type_hops: dict[str, int] = {}
        for depth, level in enumerate(levels, start=1):
            for downstream_urn in level:
                type_hops.setdefault(self._type_of(downstream_urn), depth)
        critical_hop = min(
            (h for t, h in type_hops.items() if t in CRITICAL_ENTITY_TYPES),
            default=None,
        )

        return {
            "downstream_count": len(transitive),
            "downstream_urns": transitive,
            "downstream_types": self._type_breakdown(tuple(transitive)),
            "direct_downstream_count": len(levels[0]) if levels else 0,
            "direct_downstream_types": self._type_breakdown(levels[0]) if levels else {},
            "downstream_type_hops": type_hops,
            "critical_downstream_hops": critical_hop,
        }

    def _type_of(self, urn: str) -> str:
        """Entity type from the catalog record, falling back to the URN shape."""
        record = self._assets.get(urn)
        return record.get("type", _entity_type_of(urn)) if record else _entity_type_of(urn)

    def get_context(self, urn: str) -> AssetContext:
        record = self._assets.get(urn)

        if record is None:
            # A schemaField whose parent dataset we do know about: inherit the
            # dataset's governance so column-level calls are not a blind spot.
            # A field carries its parent's sensitivity even when the catalog has
            # no explicit row for that column.
            parent = self._parent_dataset_of(urn)
            if parent is not None:
                inherited = self.get_context(parent)
                return AssetContext(
                    urn=urn,
                    entity_type="schemaField",
                    name=urn.rsplit(",", 1)[-1].rstrip(")"),
                    platform=inherited.platform,
                    tags=inherited.tags,
                    glossary_terms=inherited.glossary_terms,
                    owners=inherited.owners,
                    domain=inherited.domain,
                    tier=inherited.tier,
                    deprecated=inherited.deprecated,
                    exists=True,
                    **self._lineage_facts(urn),
                )
            return AssetContext(urn=urn, entity_type=_entity_type_of(urn), exists=False)

        return AssetContext(
            urn=urn,
            entity_type=record.get("type", _entity_type_of(urn)),
            name=record.get("name"),
            platform=record.get("platform"),
            tags=list(record.get("tags", [])),
            glossary_terms=list(record.get("glossary_terms", [])),
            owners=list(record.get("owners", [])),
            domain=record.get("domain"),
            tier=record.get("tier"),
            deprecated=bool(record.get("deprecated", False)),
            description=record.get("description"),
            query_count=int(record.get("query_count", 0)),
            exists=True,
            **self._lineage_facts(urn),
        )

    def _parent_dataset_of(self, urn: str) -> str | None:
        """Pull the dataset URN out of a schemaField URN, if it is one.

        Shape: ``urn:li:schemaField:(<dataset-urn>,<fieldPath>)``
        """
        prefix = "urn:li:schemaField:("
        if not urn.startswith(prefix):
            return None
        inner = urn[len(prefix) :].rstrip(")")
        dataset_urn, _, _field = inner.rpartition(",")
        return dataset_urn if dataset_urn in self._assets else None

    def _type_breakdown(self, urns: tuple[str, ...]) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for urn in urns:
            counts[self._type_of(urn)] += 1
        return dict(counts)

    # -- helpers used by the CLI's reporting commands ---------------------

    def all_urns(self) -> list[str]:
        return list(self._assets)

    def assets_with_label(self, label: str) -> list[str]:
        wanted = label.lower()

        def labels_of(record: dict) -> set[str]:
            return {
                t.lower()
                for t in (*record.get("tags", []), *record.get("glossary_terms", []))
            }

        return [urn for urn, record in self._assets.items() if wanted in labels_of(record)]
