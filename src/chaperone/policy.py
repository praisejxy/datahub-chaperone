"""The policy engine.

A policy is a list of rules. Each rule states a condition over the *catalog's*
knowledge of an asset and a verdict to apply when it matches. Rules are data,
not code, so a data platform team can govern their agents by editing YAML in a
reviewed pull request instead of patching a proxy.

Design commitments:

* **Additive.** Rules never cancel each other. The strictest verdict wins, so
  adding a rule can only ever tighten behaviour - you cannot accidentally punch
  a hole in an existing control.
* **Evidence-bearing.** A rule that fires must say which catalog fact triggered
  it. "Denied" is useless to an agent; "denied because this column is tagged PII
  and feeds 3 dashboards" tells it what to do next.
* **Fail-closed on writes, fail-open on reads.** If context is missing, an
  unknown read is allowed but an unknown *mutation* is still gated, because the
  cost of a wrong write is unbounded.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from chaperone.models import (
    AssetContext,
    Decision,
    RuleHit,
    Severity,
    ToolCall,
    Verdict,
    strictest,
)

POLICY_DIR = Path(__file__).resolve().parent / "policies"

# Tools from the DataHub MCP server that change catalog state. Kept as a set of
# names rather than a prefix check because upstream tool naming is not uniform
# (`save_document` mutates; `search_documents` does not).
MUTATION_TOOLS = frozenset(
    {
        "add_tags",
        "remove_tags",
        "add_glossary_terms",
        "remove_glossary_terms",
        "add_terms",
        "remove_terms",
        "add_owners",
        "remove_owners",
        "set_domains",
        "remove_domains",
        "update_description",
        "add_structured_properties",
        "remove_structured_properties",
        "set_lifecycle_stage",
        "save_document",
        "create_glossary_term",
        "create_glossary_term_version",
        "add_related_terms",
        "accept_or_reject_proposals",
    }
)

REMOVAL_TOOLS = frozenset({t for t in MUTATION_TOOLS if t.startswith("remove_")})


class Condition(BaseModel):
    """When a rule applies.

    Every field is optional and all supplied fields must match (logical AND).
    An empty condition matches everything, which is how a blanket rule such as
    "all mutations need review" is expressed.
    """

    tools: list[str] | None = None
    """Match these tool names.

    Three wildcards are understood, so policies do not have to enumerate a tool
    list that upstream keeps changing:

    * ``*`` - every tool
    * ``*mutation*`` - every tool that changes catalog state
    * ``*removal*`` - the subset that deletes curated metadata
    * ``*read*`` - everything that is not a mutation
    """

    has_any_label: list[str] | None = None
    """Fires when the asset carries any of these tags or glossary terms."""

    has_all_labels: list[str] | None = None

    tier_in: list[str] | None = None

    min_downstream: int | None = None
    """Total transitive blast radius threshold.

    Prefer ``min_direct_downstream`` or ``critical_within_hops`` for gating
    rules: transitive reach saturates in a connected warehouse, so a threshold
    on it ends up matching a leaf table and a core fact table alike.
    """

    min_direct_downstream: int | None = None
    """Threshold on assets *one hop* down - what a bad change breaks today."""

    downstream_type_in: list[str] | None = None
    """Fires when the blast radius touches e.g. a dashboard or feature table."""

    within_hops: int | None = None
    """Restricts ``downstream_type_in`` to entities at most N hops downstream.

    Without it, ``downstream_type_in`` asks "is one of these anywhere below me",
    which in a connected warehouse is true almost everywhere. With
    ``within_hops: 2`` it asks the question a steward would actually ask.
    """

    deprecated: bool | None = None

    unowned: bool | None = None
    """``True`` matches assets with no owner - nobody to catch a mistake."""

    entity_type_in: list[str] | None = None

    unknown_asset: bool | None = None
    """``True`` matches assets absent from the catalog."""

    def tools_match(self, tool: str) -> bool:
        if not self.tools:
            return True
        for pattern in self.tools:
            if pattern == "*":
                return True
            if pattern == "*mutation*":
                if tool in MUTATION_TOOLS:
                    return True
            elif pattern == "*removal*":
                if tool in REMOVAL_TOOLS:
                    return True
            elif pattern == "*read*":
                if tool not in MUTATION_TOOLS:
                    return True
            elif pattern == tool:
                return True
        return False

    def asset_match(self, ctx: AssetContext) -> bool:
        if self.unknown_asset is not None and self.unknown_asset != (not ctx.exists):
            return False
        if self.has_any_label and not any(ctx.has_label(x) for x in self.has_any_label):
            return False
        if self.has_all_labels and not all(ctx.has_label(x) for x in self.has_all_labels):
            return False
        if self.tier_in:
            tier = (ctx.tier or "").lower().replace(" ", "")
            if tier not in {t.lower().replace(" ", "") for t in self.tier_in}:
                return False
        if self.min_downstream is not None and ctx.downstream_count < self.min_downstream:
            return False
        if (
            self.min_direct_downstream is not None
            and ctx.direct_downstream_count < self.min_direct_downstream
        ):
            return False
        if self.downstream_type_in:
            wanted = {t.lower() for t in self.downstream_type_in}
            reach = {t.lower(): h for t, h in ctx.downstream_type_hops.items()}
            # Fall back to the type breakdown when a provider cannot supply hop
            # distances, so `within_hops` degrades to "anywhere downstream"
            # rather than silently matching nothing.
            if not reach:
                reach = {k.lower(): 1 for k in ctx.downstream_types}
            limit = self.within_hops
            if not any(
                t in reach and (limit is None or reach[t] <= limit) for t in wanted
            ):
                return False
        if self.deprecated is not None and ctx.deprecated != self.deprecated:
            return False
        if self.unowned is not None:
            # "Unowned" is a statement about a real asset that nobody looks
            # after. An asset the catalog has never heard of is a different
            # problem, handled by `unknown_asset` - letting it match here would
            # attach a misleading second reason to every hallucinated URN.
            if not ctx.exists:
                return False
            if self.unowned != (not ctx.owners):
                return False
        if self.entity_type_in:
            if ctx.entity_type.lower() not in {t.lower() for t in self.entity_type_in}:
                return False
        return True

    def is_asset_agnostic(self) -> bool:
        """True when this condition only inspects the tool, not any asset.

        Such a rule must still fire for a call that names no URN at all, so the
        engine evaluates it once outside the per-asset loop.
        """
        return all(
            getattr(self, field) is None
            for field in (
                "has_any_label",
                "has_all_labels",
                "tier_in",
                "min_downstream",
                "min_direct_downstream",
                "downstream_type_in",
                "within_hops",
                "deprecated",
                "unowned",
                "entity_type_in",
                "unknown_asset",
            )
        )


class Rule(BaseModel):
    id: str
    verdict: Verdict
    message: str
    severity: Severity = Severity.MEDIUM
    when: Condition = Field(default_factory=Condition)
    redact_fields: list[str] = Field(default_factory=list)
    """For REDACT verdicts: response keys or column names to strip."""

    enabled: bool = True

    def render(self, ctx: AssetContext | None, call: ToolCall) -> str:
        """Fill the message template with the evidence that triggered it.

        ``{hops}`` and ``{matched}`` are resolved against *this rule's own*
        ``downstream_type_in``, not against the asset's nearest critical
        neighbour. A rule about ML that reported the hop distance to a dashboard
        would be quoting a true number as evidence for a different claim, which
        is the failure mode this whole project exists to prevent.
        """
        return self.message.format(
            tool=call.tool,
            agent=call.agent_id,
            urn=ctx.urn if ctx else "-",
            name=(ctx.name if ctx and ctx.name else (ctx.urn if ctx else "-")),
            tags=", ".join(ctx.tags) if ctx and ctx.tags else "none",
            tier=(ctx.tier if ctx and ctx.tier else "untiered"),
            downstream=(ctx.downstream_count if ctx else 0),
            direct=(ctx.direct_downstream_count if ctx else 0),
            hops=self._hops_for(ctx),
            matched=self._matched_for(ctx),
            owners=", ".join(ctx.owners) if ctx and ctx.owners else "nobody",
            breakdown=_format_breakdown(ctx) if ctx else "-",
            direct_breakdown=_format_direct_breakdown(ctx) if ctx else "-",
        )

    def _relevant_hops(self, ctx: AssetContext | None) -> dict[str, int]:
        """Hop distance per downstream type, narrowed to what this rule asked about."""
        if ctx is None:
            return {}
        if not self.when.downstream_type_in:
            return dict(ctx.downstream_type_hops)
        wanted = {t.lower() for t in self.when.downstream_type_in}
        return {t: h for t, h in ctx.downstream_type_hops.items() if t.lower() in wanted}

    def _hops_for(self, ctx: AssetContext | None) -> str:
        relevant = self._relevant_hops(ctx)
        if relevant:
            return str(min(relevant.values()))
        if ctx and ctx.critical_downstream_hops is not None:
            return str(ctx.critical_downstream_hops)
        return "-"

    def _matched_for(self, ctx: AssetContext | None) -> str:
        """The specific downstream entities this rule fired on, with distances."""
        relevant = self._relevant_hops(ctx)
        if not relevant:
            return "no matching downstream consumers"
        return ", ".join(
            f"{kind} at {hops} hop{'s' if hops != 1 else ''}"
            for kind, hops in sorted(relevant.items(), key=lambda kv: (kv[1], kv[0]))
        )


def _format_breakdown(ctx: AssetContext) -> str:
    if not ctx.downstream_types:
        return "no downstream consumers"
    return ", ".join(f"{n} {kind}" for kind, n in sorted(ctx.downstream_types.items()))


def _format_direct_breakdown(ctx: AssetContext) -> str:
    if not ctx.direct_downstream_types:
        return "no direct consumers"
    return ", ".join(
        f"{n} {kind}" for kind, n in sorted(ctx.direct_downstream_types.items())
    )


class Policy(BaseModel):
    name: str = "unnamed policy"
    description: str = ""
    rules: list[Rule] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: Path | str) -> Policy:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(raw)

    @classmethod
    def bundled(cls, name: str = "default") -> Policy:
        """Load a policy pack that ships with Chaperone."""
        path = POLICY_DIR / f"{name}.yaml"
        if not path.exists():
            available = ", ".join(sorted(p.stem for p in POLICY_DIR.glob("*.yaml"))) or "none"
            raise FileNotFoundError(f"No bundled policy '{name}'. Available: {available}")
        return cls.from_yaml(path)

    @classmethod
    def merged(cls, *policies: Policy) -> Policy:
        """Stack policy packs. Later packs add rules; none can remove one."""
        rules: list[Rule] = []
        seen: set[str] = set()
        for policy in policies:
            for rule in policy.rules:
                if rule.id in seen:
                    continue
                seen.add(rule.id)
                rules.append(rule)
        return cls(
            name=" + ".join(p.name for p in policies),
            description="Merged policy pack.",
            rules=rules,
        )

    def active_rules(self) -> list[Rule]:
        return [r for r in self.rules if r.enabled]


class PolicyEngine:
    """Evaluates a policy against tool calls."""

    def __init__(self, policy: Policy, provider: Any) -> None:
        self.policy = policy
        self.provider = provider

    def evaluate(self, call: ToolCall) -> Decision:
        import time

        started = time.perf_counter()
        targets = call.targets()
        contexts = [self.provider.get_context(urn) for urn in targets]
        hits: list[RuleHit] = []

        for rule in self.policy.active_rules():
            if not rule.when.tools_match(call.tool):
                continue

            if rule.when.is_asset_agnostic():
                # Fires once for the call itself. Attach the most significant
                # asset (if any) purely so the message can cite something.
                anchor = _most_significant(contexts)
                hits.append(
                    RuleHit(
                        rule_id=rule.id,
                        verdict=rule.verdict,
                        severity=rule.severity,
                        message=rule.render(anchor, call),
                        target_urn=anchor.urn if anchor else None,
                        evidence={"tool": call.tool},
                    )
                )
                continue

            for ctx in contexts:
                if rule.when.asset_match(ctx):
                    hits.append(
                        RuleHit(
                            rule_id=rule.id,
                            verdict=rule.verdict,
                            severity=rule.severity,
                            message=rule.render(ctx, call),
                            target_urn=ctx.urn,
                            evidence={
                                "tags": ctx.tags,
                                "tier": ctx.tier,
                                "downstream_count": ctx.downstream_count,
                                "direct_downstream_count": ctx.direct_downstream_count,
                                "critical_downstream_hops": ctx.critical_downstream_hops,
                                "downstream_types": ctx.downstream_types,
                                "owners": ctx.owners,
                            },
                        )
                    )

        verdict = strictest(*(h.verdict for h in hits)) if hits else Verdict.ALLOW
        return Decision(
            call=call,
            verdict=verdict,
            hits=hits,
            contexts=contexts,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    def redaction_fields(self, decision: Decision) -> set[str]:
        """Column/field names to strip from a response, per firing REDACT rules."""
        fields: set[str] = set()
        by_id = {r.id: r for r in self.policy.active_rules()}
        for hit in decision.hits:
            rule = by_id.get(hit.rule_id)
            if rule and rule.verdict is Verdict.REDACT:
                fields.update(f.lower() for f in rule.redact_fields)
        return fields


def _most_significant(contexts: list[AssetContext]) -> AssetContext | None:
    """Pick the asset a blanket rule should cite: widest blast radius first."""
    if not contexts:
        return None
    return max(contexts, key=lambda c: (c.downstream_count, len(c.labels)))
