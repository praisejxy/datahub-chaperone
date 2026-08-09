"""The writeback must degrade when ``acryl-datahub`` is not installed.

The SDK is an optional extra, so the common install has no ``datahub`` module at
all. The end-of-session writeback still runs in that configuration, and an
unguarded import there crashes ``demo`` and ``serve`` *after* the agent's work is
done - the worst possible moment, and invisible to anyone developing with the
extra installed. These tests deliberately run with the import broken.

Unlike ``test_audit.py`` there is no ``importorskip`` here: the whole point is
the path taken when the SDK is absent, which must be exercised on every machine.
"""

from __future__ import annotations

import sys

import pytest

from chaperone.audit import agent_entity_payload, build_agent_entity

CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,ecommerce.public.customers,PROD)"


@pytest.fixture
def no_datahub_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make importing the agent entity module raise ImportError.

    Binding the name to ``None`` in ``sys.modules`` is how Python signals a
    failed import for an already-attempted module, so this reproduces a machine
    without the extra without having to uninstall anything.
    """
    monkeypatch.setitem(sys.modules, "datahub.api.entities.agent.agent", None)


def test_building_an_agent_entity_without_the_sdk_returns_none(no_datahub_sdk) -> None:
    agent = build_agent_entity(
        agent_id="catalog-steward-agent",
        name="Catalog Steward",
        description="session summary",
        consumed_datasets=[CUSTOMERS],
        skills=["documentation"],
    )
    assert agent is None


def test_the_payload_of_a_missing_agent_is_empty_not_an_error() -> None:
    # The caller writes this straight to examples/agent-entity.json, so it has
    # to be JSON-serialisable rather than None.
    assert agent_entity_payload(None) == []


def test_a_session_still_finishes_cleanly_without_the_sdk(
    no_datahub_sdk, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The whole `demo` command, with the SDK import broken."""
    from click.testing import CliRunner

    from chaperone.cli import main

    monkeypatch.setenv("CHAPERONE_HOME", str(tmp_path))
    monkeypatch.delenv("DATAHUB_GMS_URL", raising=False)

    result = CliRunner().invoke(main, ["demo", "--offline"])

    assert result.exit_code == 0, result.output
