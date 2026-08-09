"""Tests for scenario loading.

Bundled scenarios are resolved by *name* so that ``--scenario walkthrough``
works from any working directory, including from an installed wheel where the
repository is not on disk. That makes the packaging configuration part of the
contract: a scenario that resolves in a source checkout but not in the wheel is
the exact failure mode documented in docs/upstream-contributions.md, and it is
invisible unless a test asserts the file is really there.
"""

from __future__ import annotations

import json

import pytest

from chaperone.graph import build_provider
from chaperone.models import Verdict
from chaperone.policy import Policy, PolicyEngine
from chaperone.scenarios import (
    DEFAULT_SCENARIO,
    bundled_scenarios,
    load_scenario,
    resolve_scenario,
)


def test_the_walkthrough_scenario_is_bundled() -> None:
    assert "walkthrough" in bundled_scenarios()
    assert resolve_scenario("walkthrough").is_file()


def test_a_bundled_scenario_loads_by_name_not_only_by_path() -> None:
    calls = load_scenario("walkthrough")
    assert calls, "walkthrough scenario is empty"
    assert all(c.tool for c in calls)


def test_an_unknown_scenario_names_the_available_ones() -> None:
    with pytest.raises(FileNotFoundError, match="walkthrough"):
        resolve_scenario("does-not-exist")


def test_a_path_still_wins_over_a_name(tmp_path) -> None:
    path = tmp_path / "custom.json"
    path.write_text(json.dumps([{"tool": "search", "arguments": {"query": "x"}}]), encoding="utf-8")
    assert load_scenario(str(path))[0].tool == "search"


def test_the_walkthrough_covers_all_four_verdicts() -> None:
    """The scenario exists to demonstrate the verdict range.

    If a policy change collapsed one of the four outcomes the replay would still
    run and still look fine, while silently no longer showing what it claims to.
    """
    engine = PolicyEngine(Policy.bundled("default"), build_provider(offline=True))
    verdicts = {engine.evaluate(call).verdict for call in load_scenario("walkthrough")}
    assert verdicts == set(Verdict), f"missing {set(Verdict) - verdicts}"


def test_the_default_scenario_also_covers_all_four_verdicts() -> None:
    engine = PolicyEngine(Policy.bundled("default"), build_provider(offline=True))
    verdicts = {engine.evaluate(call).verdict for call in DEFAULT_SCENARIO}
    assert verdicts == set(Verdict), f"missing {set(Verdict) - verdicts}"
