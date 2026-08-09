"""Context providers for Chaperone."""

from __future__ import annotations

import os
from pathlib import Path

from chaperone.graph.base import CachingProvider, ContextProvider, NullContextProvider
from chaperone.graph.offline import DEFAULT_FIXTURE, OfflineContextProvider

__all__ = [
    "CachingProvider",
    "ContextProvider",
    "DEFAULT_FIXTURE",
    "NullContextProvider",
    "OfflineContextProvider",
    "build_provider",
]


def build_provider(
    offline: bool | None = None,
    fixture: Path | str | None = None,
    server: str | None = None,
    token: str | None = None,
) -> ContextProvider:
    """Pick a context provider, preferring a real catalog when one is reachable.

    Resolution order:

    1. ``offline=True`` (or ``CHAPERONE_OFFLINE=1``) forces the fixture graph.
    2. An explicit ``server``, or ``DATAHUB_GMS_URL`` in the environment, selects
       live DataHub.
    3. Otherwise fall back to the fixture graph, so a fresh clone just works.

    Every provider is wrapped in a cache because Chaperone runs on the hot path
    of each agent tool call.
    """
    if offline is None:
        offline = os.environ.get("CHAPERONE_OFFLINE", "").lower() in ("1", "true", "yes")

    resolved_server = server or os.environ.get("DATAHUB_GMS_URL")

    if not offline and resolved_server:
        from chaperone.graph.gms import GmsContextProvider

        return CachingProvider(GmsContextProvider(server=resolved_server, token=token))

    return CachingProvider(OfflineContextProvider(fixture=fixture))
