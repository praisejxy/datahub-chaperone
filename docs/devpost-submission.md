# Devpost submission text

Copy the sections below into the matching Devpost fields. Every number here was
read out of the working repository, not estimated.

---

## Tagline

Agents are getting write access to the data catalog. Chaperone is the seatbelt.

---

## What it does

Chaperone is a governance proxy that sits between an AI agent and the DataHub
MCP Server. Every tool call the agent makes is checked against the catalog's
*own* metadata — tags, glossary terms, tiers, ownership, and lineage — before it
reaches DataHub. Then the outcome is written back into DataHub as first-class
metadata, so the graph learns what its agents are doing.

Point an MCP client at `chaperone serve` instead of at the DataHub MCP Server.
The agent sees the same tools it always did. The difference is that the
dangerous ones are now governed.

## The problem

The DataHub MCP Server now ships mutation tools — `add_tags`,
`update_description`, `remove_owners`, `set_domains`. That is the right
direction: agents that only read are of limited use, and an agent that enriches
the catalog is genuinely valuable.

But it changes the risk profile. An agent that mislabels one column is an
inconvenience. An agent looping over 400 tables at machine speed is an
incident — and the catalog is precisely where an organization's governance
decisions live.

Today the usual mitigation is a paragraph of prompt text: "be careful with
PII." That is not a control. It is a suggestion, and it degrades as the context
window fills up.

Meanwhile the information needed to make these calls correctly is *already in
DataHub*: which columns are PII, which tables are Tier 1, who owns what, and
what breaks downstream. Chaperone's premise is that governance should be read
out of the catalog and enforced, not restated in a prompt and hoped for.

## Four outcomes, not two

`allow` and `deny` are blunt. An allow-or-deny gate either leaks the data or
stops the agent dead. Chaperone adds two verdicts that keep the agent working:

- **`redact`** — the agent gets the schema and lineage it needs, with sensitive
  *values* stripped. It keeps working; the data does not leak.
- **`review`** — the write is not discarded, it is converted into a proposal for
  a human owner. The agent's work is preserved; a person decides.

Rules are YAML evaluated against live catalog facts, so a data platform team
governs their agents through a reviewed pull request rather than by editing
prompts. The shipped pack has 12 rules: 3 allow, 1 redact, 5 review, 3 deny.

## Refusals cite evidence

A blocked agent is told *why*, quoting the catalog:

> `fct_orders` feeds production ML (mlFeatureTable at 1 hop, mlModel at 2 hops).
> Metadata changes here can silently invalidate features and deployed models, so
> `update_description` needs a human owner.

An agent can act on that. It can find the owner, or propose the change instead.
It cannot act on "permission denied." That is the difference between a guardrail
and a wall.

Blast radius is computed hop-by-hop rather than as a total descendant count,
because "reaches 40 assets" is not actionable and "feeds an ML model 2 hops out"
is.

## It contributes back to the graph

This is the part that makes the catalog better instead of only safer. At the end
of every session Chaperone:

- registers the agent as an **`aiAgent` entity** — the entity type new in
  `acryl-datahub` 1.7.0 — with its skills, platform, and owners;
- attaches every dataset the agent touched as **upstream lineage**, so DataHub
  answers a question it currently cannot: *which agents are operating on this
  table?*
- turns held writes into **proposals**, and tags assets an agent was stopped
  from changing so the intervention is visible in the catalog UI rather than
  only in a log file nobody reads.

The next agent — and the next engineer — inherits all of it.

## Open-source contribution to DataHub

Building Chaperone surfaced a reproducible packaging bug in the published
`acryl-datahub` 1.7.0 wheel. `datahub datapack --help` crashes with a
`FileNotFoundError` whenever stdout is not a TTY — that is, whenever it is
called by a script, a CI job, or an agent:

```
datahub datapack --help | cat     # FileNotFoundError
datahub datapack --help           # works, which is why it went unnoticed
```

The command deliberately appends extra guidance for non-human callers, but the
Markdown file it reads is missing from the wheel: `setup.py` declares
`package_data` for `datahub.cli.resources` and never for
`datahub.cli.datapack.resources`. The fix is one line.

Root cause, a causal proof (dropping the file in fixes it; removing it breaks it
again), and the patch are in `docs/upstream-contributions.md`.

The irony was useful: the same bug class would have broken Chaperone's own
wheel, since it ships policy YAML, a fixture graph, and scenario JSON as package
data. So the wheel was verified the way the upstream bug should have
been caught: built, installed with `--no-deps` into a clean virtualenv, and the
CLI run from an unrelated directory, confirming the policy pack and fixture graph
load when the source tree is not on the path.

## How we built it

Chaperone is a **protocol-level MCP proxy**. It speaks raw JSON-RPC over stdio
and never imports the upstream DataHub MCP Server, so it is version-agnostic: a
new DataHub tool appears through the proxy without a Chaperone release, and only
tools the policy names are intercepted.

- `policy.py` — rule evaluation with additive, strictest-wins resolution. Every
  rule that matches contributes a hit; the final verdict is the harshest.
- `graph/` — pluggable context providers. The offline provider reads a bundled
  17-asset slice of a realistic e-commerce catalog; the live provider queries
  GMS. The policy engine is identical either way — only the provider changes.
- `proxy.py` — the stdio pump. Human-facing output goes to stderr, because
  stdout carries MCP protocol frames only.
- `audit.py` — the decision log, proposals, and the DataHub writeback.

2,588 lines of Python. 72 tests. Pydantic v2, Click, Rich. `acryl-datahub` is an
optional dependency: the suite passes with it installed (72 tests) and without it
(60 pass, 1 skipped), because the policy engine has to work for someone
evaluating Chaperone before they have a DataHub instance.

## Challenges we ran into

Three bugs worth naming, all found by testing the thing rather than reading it:

**The proxy dropped the last response.** On stdin EOF, `run()` killed the child
process immediately — before the reader thread had relayed replies still in
flight. A one-call session is the *normal* shape of an agent run, so the final
response was routinely lost, which to the agent looked like a hang. Fixed by
closing stdin, draining the pump with a bounded join, and only then stopping the
child.

**Windows paths were being destroyed by `shlex.split`.** It defaults to POSIX
rules, where a backslash is an escape character, so `C:\Python\python.exe` became
`C:Pythonpython.exe`. Fixed with a platform-aware splitter.

**Chaperone nearly created the asset it had just refused to touch.** Blocked
assets get tagged `AgentBlocked` on writeback, and emitting an aspect against an
unknown URN *creates* that entity in DataHub. When an agent hallucinated a URN,
Chaperone denied the write and then tracked the nonexistent asset for tagging —
reporting the refusal to the agent and undoing it behind their back. Now
anything the catalog reports as nonexistent is never tracked at all.

Each one is pinned by a regression test, and each test was verified to fail when
the fix is reverted, so none of them pass vacuously.

## What's next

- Column-level policy, so `redact` can act on a single field rather than an
  asset.
- Rate limiting per agent, since "400 tables at machine speed" is the failure
  mode the project exists to prevent and volume is currently unbounded.
- Landing the upstream `package_data` fix in `datahub-project/datahub`.
- Splitting held writes from hard denials in the writeback, so a Tier-1 asset
  routed to review is not tagged with the same label as one that was refused
  outright.

## Try it — about one minute, no Docker and no DataHub instance

```
git clone https://github.com/praisejxy/datahub-chaperone
cd datahub-chaperone
pip install -e .
chaperone demo --scenario walkthrough --pace 1.5
```

Six tool calls that reach all four verdicts in escalating order, ending with the
writeback payload. Then watch a single call get stopped:

```
chaperone check add_tags \
  --urn "urn:li:dataset:(urn:li:dataPlatform:postgres,ecommerce.public.customers,PROD)" \
  --tag Verified
```

`chaperone policy` prints the active rules, and `chaperone doctor` reports
connectivity and which graph is in use. Sample outputs — the decision log,
generated proposals, and the `aiAgent` entity payload — are committed in
`examples/`.

## Links

- Repo: https://github.com/praisejxy/datahub-chaperone
- License: Apache 2.0
- Upstream bug analysis: `docs/upstream-contributions.md`

---

## Field-by-field checklist

| Devpost field | What to put |
|---|---|
| Project name | DataHub Chaperone |
| Tagline | Agents are getting write access to the data catalog. Chaperone is the seatbelt. |
| Repo URL | `https://github.com/praisejxy/datahub-chaperone` (Apache-2.0 shows in About) |
| Testing URL | Same repo URL — the quickstart is the test path |
| Video | YouTube, **Public** (not Unlisted), under 3:00 |
| Track | Agents That Do Real Work |
| Built with | python, datahub, mcp, pydantic, click, rich |

Before submitting, confirm the repo's About panel shows **Apache-2.0** — the
rules require the license be detectable there, not merely present as a file.

