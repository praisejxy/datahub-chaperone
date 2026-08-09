"""Context providers: where Chaperone gets its evidence.

Policy decisions need facts about assets. Those facts can come from a live
DataHub GMS or from a bundled fixture graph, and the policy engine must not be
able to tell the difference. That is the entire purpose of this seam.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from chaperone.models import AssetContext


@runtime_checkable
class ContextProvider(Protocol):
    """Resolves DataHub URNs into the evidence policy needs."""

    name: str

    def get_context(self, urn: str) -> AssetContext:
        """Return what the catalog knows about ``urn``.

        Must never raise for an unknown URN. Return ``AssetContext(urn=urn,
        exists=False)`` instead, so a catalog gap degrades into "no evidence"
        rather than taking the proxy down and blocking the agent entirely.
        """
        ...

    def describe(self) -> str:
        """One-line summary shown in the CLI, e.g. which instance is in use."""
        ...


class NullContextProvider:
    """A provider that knows nothing.

    Used when no graph is configured. Every asset comes back as unknown, so
    tag- and lineage-based rules cannot fire. Rules keyed only on the tool name
    still work, which means Chaperone stays useful as a plain write-blocker
    even with no catalog attached.
    """

    name = "null"

    def get_context(self, urn: str) -> AssetContext:
        return AssetContext(urn=urn, exists=False)

    def describe(self) -> str:
        return "no context graph (tool-name rules only)"


class CachingProvider:
    """Memoises lookups for the lifetime of a process.

    An agent typically touches the same handful of assets repeatedly within one
    session, and a live GMS lookup costs a network round trip. Chaperone sits in
    the hot path of every tool call, so repeated lookups would show up directly
    as agent latency.
    """

    def __init__(self, inner: ContextProvider) -> None:
        self._inner = inner
        self._cache: dict[str, AssetContext] = {}
        self.name = inner.name
        self.hits = 0
        self.misses = 0

    def get_context(self, urn: str) -> AssetContext:
        cached = self._cache.get(urn)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        context = self._inner.get_context(urn)
        self._cache[urn] = context
        return context

    def describe(self) -> str:
        return self._inner.describe()

    def invalidate(self, urn: str | None = None) -> None:
        if urn is None:
            self._cache.clear()
        else:
            self._cache.pop(urn, None)
