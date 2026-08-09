"""Chaperone's command line.

``serve`` is the real entry point - the MCP proxy an agent connects to. The other
commands exist so that a person can inspect and trust the policy without wiring
up an agent first: ``check`` for one call, ``demo`` for a scripted session,
``doctor`` for connectivity, ``policy`` to read the active rules.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from chaperone import __version__
from chaperone.audit import (
    AuditLog,
    DataHubWriteback,
    agent_entity_payload,
    build_agent_entity,
)
from chaperone.graph import build_provider
from chaperone.models import Decision, ToolCall, Verdict
from chaperone.policy import Policy, PolicyEngine

console = Console(stderr=True)

VERDICT_STYLE = {
    Verdict.ALLOW: "bold green",
    Verdict.REDACT: "bold yellow",
    Verdict.REVIEW: "bold magenta",
    Verdict.DENY: "bold red",
}


def _load_engine(
    offline: bool | None,
    fixture: str | None,
    policy_files: tuple[str, ...],
    pack: str,
) -> PolicyEngine:
    provider = build_provider(offline=offline, fixture=fixture)
    policies = [Policy.bundled(pack)]
    policies.extend(Policy.from_yaml(p) for p in policy_files)
    policy = Policy.merged(*policies) if len(policies) > 1 else policies[0]
    return PolicyEngine(policy, provider)


def _render(decision: Decision) -> None:
    style = VERDICT_STYLE[decision.verdict]
    header = f"[{style}]{decision.verdict.value.upper()}[/] `{decision.call.tool}`"
    body = [header, ""]
    if decision.hits:
        for hit in decision.hits:
            body.append(f"[dim]{hit.rule_id}[/] · [{VERDICT_STYLE[hit.verdict]}]"
                        f"{hit.verdict.value}[/] · {hit.severity.value}")
            body.append(f"  {' '.join(hit.message.split())}")
            body.append("")
    else:
        body.append("[green]No policy concerns.[/]")
    body.append(
        f"[dim]{decision.elapsed_ms:.2f} ms · {len(decision.contexts)} asset(s) inspected[/]"
    )
    console.print(Panel("\n".join(body), border_style=style.split()[-1], expand=False))


AGENT_SKILLS = ["documentation", "classification", "lineage-analysis"]


def _writeback(audit: AuditLog, agent_id: str) -> list[dict]:
    """Contribute the session back to the graph, and report what was written.

    Runs at the end of every session, live or offline. With a GMS configured the
    ``aiAgent`` entity and its lineage are emitted and blocked assets are tagged;
    without one, the payload is still built and shown, so the contribution is
    inspectable rather than merely claimed.
    """
    consumed = audit.consumed_datasets()
    blocked = audit.summary()["assets_blocked"]
    writeback = DataHubWriteback()

    agent = build_agent_entity(
        agent_id=agent_id,
        name=agent_id.replace("-", " ").title(),
        description=(
            f"Governed by Chaperone {__version__}. Session {audit.session_id}: "
            f"{len(audit.decisions)} tool calls, {len(audit.proposals)} proposal(s) raised."
        ),
        consumed_datasets=consumed,
        skills=AGENT_SKILLS,
    )
    payload = agent_entity_payload(agent)

    lines = [
        f"aiAgent   : [cyan]{agent.urn if agent else f'urn:li:aiAgent:{agent_id}'}[/]",
        f"lineage   : {len(consumed)} dataset(s) attached as upstreams",
        f"proposals : {len(audit.proposals)}",
        f"blocked   : {len(blocked)} asset(s) to tag AgentBlocked",
    ]
    if agent is None:
        lines.append(
            "[yellow]acryl-datahub not installed - "
            r"pip install 'datahub-chaperone\[datahub]' to emit[/]"
        )
        console.print(Panel("\n".join(lines), title="writeback to DataHub",
                            border_style="cyan", expand=False))
        return payload

    if writeback.available:
        urn = writeback.register_agent(
            agent_id=agent_id,
            name=agent.name,
            description=agent.description,
            consumed_datasets=consumed,
            skills=AGENT_SKILLS,
        )
        for urn_blocked in blocked:
            writeback.annotate_blocked_asset(urn_blocked, "blocked by Chaperone policy")
        lines.append(
            f"[green]emitted to {writeback.server}[/]" if urn
            else "[yellow]emit failed; see --verbose[/]"
        )
    else:
        lines.append("[dim]no DATAHUB_GMS_URL set - payload built, not emitted[/]")

    console.print(Panel("\n".join(lines), title="writeback to DataHub",
                        border_style="cyan", expand=False))
    return payload


# -- shared options -------------------------------------------------------

def graph_options(fn):
    fn = click.option("--offline", is_flag=True, default=None,
                      help="Force the bundled fixture graph instead of a live DataHub.")(fn)
    fn = click.option("--fixture", default=None, type=click.Path(exists=True, dir_okay=False),
                      help="Use a specific offline graph JSON file.")(fn)
    fn = click.option("--policy", "policy_files", multiple=True,
                      type=click.Path(exists=True, dir_okay=False),
                      help="Additional policy YAML to stack on top of the pack (repeatable).")(fn)
    fn = click.option("--pack", default="default", show_default=True,
                      help="Bundled policy pack to start from.")(fn)
    return fn


@click.group()
@click.version_option(__version__, prog_name="chaperone")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging to stderr.")
def main(verbose: bool) -> None:
    """Governance for AI agents operating on a DataHub catalog."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


@main.command()
@graph_options
@click.option("--upstream", default=None,
              help="Upstream MCP server command. Defaults to 'uvx mcp-server-datahub@latest'.")
@click.option("--agent-id", default="unregistered-agent", show_default=True,
              help="Identity recorded on every decision and used for the aiAgent entity.")
@click.option("--audit-log", default=None, type=click.Path(dir_okay=False),
              help="Where to append decisions. Defaults to ~/.chaperone/decisions.jsonl")
@click.option("--dry-run", is_flag=True,
              help="Evaluate and log, but forward everything. Use to measure a policy safely.")
def serve(offline, fixture, policy_files, pack, upstream, agent_id, audit_log, dry_run) -> None:
    """Run the governing MCP proxy (this is what an agent connects to).

    Speaks MCP on stdin/stdout, so stdout carries protocol frames only - all
    human-facing output goes to stderr.
    """
    from chaperone.proxy import DEFAULT_UPSTREAM, ChaperoneProxy

    engine = _load_engine(offline, fixture, policy_files, pack)
    audit = AuditLog(path=audit_log, agent_id=agent_id)

    console.print(
        Panel(
            f"[bold]Chaperone {__version__}[/]\n"
            f"context   : {engine.provider.describe()}\n"
            f"policy    : {engine.policy.name} ({len(engine.policy.active_rules())} rules)\n"
            f"upstream  : {upstream or DEFAULT_UPSTREAM}\n"
            f"agent     : {agent_id}\n"
            f"audit     : {audit.path}"
            + ("\n[yellow]dry-run: nothing will be blocked[/]" if dry_run else ""),
            title="governing MCP session",
            border_style="cyan",
            expand=False,
        )
    )

    proxy = ChaperoneProxy(
        engine=engine,
        audit=audit,
        upstream_command=upstream or DEFAULT_UPSTREAM,
        agent_id=agent_id,
        dry_run=dry_run,
        on_decision=lambda d: console.print(
            f"[{VERDICT_STYLE[d.verdict]}]{d.verdict.value.upper():7}[/] "
            f"{d.call.tool:26} {(d.call.targets() or ['-'])[0][:70]}"
        ),
    )
    proxy.run()

    summary = audit.summary()
    console.print(Panel(json.dumps(summary, indent=2), title="session summary",
                        border_style="cyan", expand=False))
    _writeback(audit, agent_id)


@main.command()
@graph_options
@click.argument("tool")
@click.option("--urn", "urns", multiple=True, help="Target URN (repeatable).")
@click.option("--tag", "tags", multiple=True, help="Tag argument, for tag tools.")
@click.option("--arg", "raw_args", multiple=True, metavar="KEY=VALUE",
              help="Arbitrary tool argument (repeatable).")
@click.option("--json", "as_json", is_flag=True, help="Emit the decision as JSON.")
def check(offline, fixture, policy_files, pack, tool, urns, tags, raw_args, as_json) -> None:
    """Evaluate a single tool call without running an agent.

    \b
    chaperone check add_tags --urn "urn:li:dataset:(...)" --tag Verified
    """
    arguments: dict = {}
    if urns:
        arguments["urns" if len(urns) > 1 else "urn"] = list(urns) if len(urns) > 1 else urns[0]
    if tags:
        arguments["tags"] = list(tags)
    for item in raw_args:
        key, _, value = item.partition("=")
        arguments[key] = value

    engine = _load_engine(offline, fixture, policy_files, pack)
    decision = engine.evaluate(ToolCall(tool=tool, arguments=arguments, agent_id="cli"))

    if as_json:
        click.echo(decision.model_dump_json(indent=2))
    else:
        _render(decision)
        console.print(decision.explain())

    # Exit non-zero when blocked, so this composes into CI.
    sys.exit(1 if decision.blocked else 0)


@main.command()
@graph_options
def policy(offline, fixture, policy_files, pack) -> None:
    """Show the active policy rules."""
    engine = _load_engine(offline, fixture, policy_files, pack)
    table = Table(title=engine.policy.name, show_lines=False)
    table.add_column("rule", style="cyan", no_wrap=True)
    table.add_column("verdict")
    table.add_column("sev")
    table.add_column("applies when", overflow="fold")

    for rule in engine.policy.active_rules():
        conditions = {
            k: v for k, v in rule.when.model_dump(exclude_none=True).items() if v not in ([], {})
        }
        table.add_row(
            rule.id,
            f"[{VERDICT_STYLE[rule.verdict]}]{rule.verdict.value}[/]",
            rule.severity.value,
            ", ".join(f"{k}={v}" for k, v in conditions.items()) or "any call",
        )
    console.print(table)
    console.print(f"[dim]context: {engine.provider.describe()}[/]")


@main.command()
@graph_options
def doctor(offline, fixture, policy_files, pack) -> None:
    """Check configuration, connectivity, and that the policy loads."""
    import os

    rows = []
    rows.append(("chaperone", "ok", __version__))

    try:
        engine = _load_engine(offline, fixture, policy_files, pack)
        rule_count = len(engine.policy.active_rules())
        rows.append(("policy", "ok", f"{engine.policy.name}, {rule_count} rules"))
        rows.append(("context graph", "ok", engine.provider.describe()))
    except Exception as exc:
        rows.append(("policy/context", "FAIL", str(exc)))
        engine = None

    gms = os.environ.get("DATAHUB_GMS_URL")
    rows.append(("DATAHUB_GMS_URL", "ok" if gms else "unset", gms or "offline mode will be used"))
    rows.append((
        "DATAHUB_GMS_TOKEN",
        "ok" if os.environ.get("DATAHUB_GMS_TOKEN") else "unset",
        "set" if os.environ.get("DATAHUB_GMS_TOKEN") else "needed for a live instance",
    ))

    try:
        import datahub  # noqa: F401
        rows.append(("acryl-datahub", "ok", "writeback available"))
    except ImportError:
        rows.append(("acryl-datahub", "missing", "pip install 'datahub-chaperone[datahub]'"))

    writeback = DataHubWriteback()
    rows.append((
        "writeback",
        "ok" if writeback.available else "disabled",
        "agent entities + lineage will be emitted" if writeback.available
        else "no GMS configured; decisions stay local",
    ))

    table = Table(show_header=True)
    table.add_column("check", style="cyan")
    table.add_column("status")
    table.add_column("detail", overflow="fold")
    for name, status, detail in rows:
        style = {"ok": "green", "FAIL": "red", "missing": "yellow", "unset": "yellow",
                 "disabled": "yellow"}.get(status, "white")
        table.add_row(name, f"[{style}]{status}[/]", detail)
    console.print(table)


@main.command()
@graph_options
@click.option("--scenario", type=click.Path(exists=True, dir_okay=False),
              help="A JSON list of tool calls to replay. Defaults to the bundled scenario.")
@click.option("--write-examples", type=click.Path(file_okay=False),
              help="Write the decision log and summary to this directory.")
@click.option("--pace", type=float, default=0.0, metavar="SECONDS",
              help="Pause between decisions, so the replay can be followed live.")
def demo(offline, fixture, policy_files, pack, scenario, write_examples, pace) -> None:
    """Replay a scripted agent session and show every decision.

    This is the fastest way to see what Chaperone does. It needs no agent, no
    API key, and no DataHub instance.
    """
    from chaperone.scenarios import DEFAULT_SCENARIO, load_scenario

    engine = _load_engine(offline, fixture, policy_files, pack)
    calls = load_scenario(scenario) if scenario else DEFAULT_SCENARIO
    audit = AuditLog(
        path=(Path(write_examples) / "decisions.jsonl") if write_examples else None,
        agent_id="catalog-steward-agent",
    )

    console.print(Panel(
        f"[bold]Replaying {len(calls)} tool calls[/]\n"
        f"context: {engine.provider.describe()}\n"
        f"policy : {engine.policy.name}",
        title="chaperone demo", border_style="cyan", expand=False,
    ))

    for index, call in enumerate(calls, start=1):
        # Pause before the decision, not after, so the last one does not leave a
        # trailing dead beat at the end of the replay.
        if pace and index > 1:
            time.sleep(pace)
        decision = engine.evaluate(call)
        audit.record(decision)
        if decision.verdict is Verdict.REVIEW:
            audit.record_proposal(decision)
        console.print(f"\n[dim]── {index}/{len(calls)} ── {call.intent or call.tool}[/]")
        _render(decision)

    summary = audit.summary()
    table = Table(title="session summary")
    table.add_column("metric", style="cyan")
    table.add_column("value")
    table.add_row("tool calls", str(summary["total_calls"]))
    for verdict, count in sorted(summary["verdicts"].items()):
        table.add_row(f"  {verdict}", str(count))
    table.add_row("assets read", str(len(summary["assets_read"])))
    table.add_row("assets blocked", str(len(summary["assets_blocked"])))
    table.add_row("proposals raised", str(summary["proposals"]))
    console.print(table)

    payload = _writeback(audit, "catalog-steward-agent")
    audit.close()

    if write_examples:
        out = Path(write_examples)
        out.mkdir(parents=True, exist_ok=True)
        (out / "session-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        (out / "proposals.json").write_text(json.dumps(audit.proposals, indent=2), encoding="utf-8")
        (out / "agent-entity.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        console.print(f"[green]wrote examples to {out}[/]")


if __name__ == "__main__":
    main()
