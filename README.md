# DataHub Chaperone

**Agents are getting write access to the data catalog. Chaperone is the seatbelt.**

Chaperone is a governance proxy that sits between an AI agent and the [DataHub](https://datahub.com/) MCP Server. Every tool call the agent makes is checked against the catalog's *own* metadata — tags, glossary terms, tiers, ownership, and lineage — before it reaches DataHub. Then the outcome is written back into DataHub as first-class metadata, so the graph learns what its agents are doing.

```
┌─────────────┐      ┌──────────────────┐      ┌──────────────┐
│  AI agent   │─────▶│    Chaperone     │─────▶│  DataHub     │
│ (any MCP    │◀─────│  policy engine   │◀─────│  MCP Server  │
│  client)    │      │                  │      └──────────────┘
└─────────────┘      │ allow / redact / │              │
                     │ review / deny    │              ▼
                     └──────────────────┘      ┌──────────────┐
                              │                │  DataHub     │
                              └───────────────▶│  graph       │
                                 writeback     └──────────────┘
```

## The problem

The DataHub MCP Server now ships mutation tools — `add_tags`, `update_description`, `remove_owners`, `set_domains`. That is the right direction: agents that only read are of limited use, and an agent that enriches the catalog is genuinely valuable.

But it changes the risk profile. An agent that mislabels one column is an inconvenience. An agent looping over 400 tables at machine speed is an incident, and the catalog is precisely where the organization's governance decisions live. Today the usual mitigation is a paragraph of prompt text — "be careful with PII" — which is not a control. It is a suggestion, and it degrades as context fills up.

Meanwhile, the information needed to make these calls correctly is *already in DataHub*: which columns are PII, which tables are Tier 1, who owns what, and what breaks downstream. Chaperone's premise is that governance should be read out of the catalog and enforced, not restated in a prompt and hoped for.

## What Chaperone does

**1. It enforces catalog metadata as policy.** Rules are YAML, evaluated against live catalog facts. A data platform team governs their agents through a reviewed pull request rather than by editing prompts.

**2. It gives agents four outcomes, not two.** `allow` and `deny` are blunt. Chaperone adds:
- **`redact`** — the agent gets the schema and lineage it needs, with sensitive *values* stripped. It keeps working; the data does not leak.
- **`review`** — the write is not discarded, it is converted into a DataHub **proposal** for a human owner. The agent's work is preserved; a person decides.

**3. It explains refusals with evidence.** A blocked agent is told *why*, citing the catalog: `denied: dim_customers is Tier1 and feeds 3 dashboards + 1 ML model`. Agents can act on that. They cannot act on "permission denied."

**4. It writes what happened back into DataHub.** This is the part that makes the catalog better instead of just safer:
- The agent is registered as an **`aiAgent` entity** (new in `acryl-datahub` 1.7.0) with its skills, framework, and owners.
- Assets the agent touched are attached as **lineage**, so the graph answers a question it currently cannot: *which agents are operating on this table?*
- Blocked mutations become **proposals**; accepted ones become real metadata.

The next agent — and the next engineer — inherits all of it.

## Quickstart (no Docker, no DataHub instance, ~1 minute)

Chaperone ships with an offline slice of a realistic e-commerce catalog, so you can evaluate it with nothing installed but Python.

```bash
git clone https://github.com/praisejxy/datahub-chaperone
cd datahub-chaperone
pip install -e .
```

Watch it stop an agent from mutating a PII table:

```bash
chaperone check add_tags \
  --urn "urn:li:dataset:(urn:li:dataPlatform:postgres,ecommerce.public.customers,PROD)" \
  --tag Verified
```

Replay a full agent session and see every decision:

```bash
chaperone demo
```

## Connecting a real DataHub

Chaperone uses DataHub's own environment conventions. If the DataHub CLI works on your machine, Chaperone will too:

```bash
export DATAHUB_GMS_URL="http://localhost:8080"     # or https://<tenant>.acryl.io
export DATAHUB_GMS_TOKEN="<your personal access token>"

pip install 'datahub-chaperone[datahub]'
chaperone doctor          # verifies connectivity and prints which graph is in use
```

The policy engine is identical in both modes — only the context provider changes. Offline is for evaluation; live is the real deployment.

## Contributing back to DataHub

Building Chaperone surfaced a reproducible packaging bug in the published `acryl-datahub` 1.7.0 wheel. `datahub datapack --help` crashes with a `FileNotFoundError` whenever stdout is not a TTY — that is, whenever it is called by a script, a CI job, or an agent:

```bash
datahub datapack --help | cat     # FileNotFoundError
datahub datapack --help           # works, which is why it went unnoticed
```

The command deliberately appends extra guidance for non-human callers, but the Markdown file it reads is missing from the wheel: `setup.py` declares `package_data` for `datahub.cli.resources` and never for `datahub.cli.datapack.resources`. The fix is one line. Full analysis, root cause, and reproduction: [docs/upstream-contributions.md](docs/upstream-contributions.md).

## Using it with an MCP client

Point your MCP client at Chaperone instead of at the DataHub MCP Server. Chaperone forwards approved calls upstream. Any client that speaks MCP over stdio works; the config below is the usual shape:

```json
{
  "mcpServers": {
    "datahub-chaperone": {
      "command": "chaperone",
      "args": ["serve"],
      "env": {
        "DATAHUB_GMS_URL": "http://localhost:8080",
        "DATAHUB_GMS_TOKEN": "<your personal access token>"
      }
    }
  }
}
```

Your agent sees the same DataHub tools it always did. The difference is that the dangerous ones are now governed.

## Repository layout

| Path | What it is |
|---|---|
| `src/chaperone/policy.py` | The policy engine — rule evaluation, strictest-wins resolution |
| `src/chaperone/models.py` | Core vocabulary: `AssetContext`, `ToolCall`, `Decision` |
| `src/chaperone/graph/` | Context providers — offline fixture graph and live GMS |
| `src/chaperone/policies/` | Shipped policy packs (YAML) |
| `examples/` | Sample outputs: decision logs, generated proposals, agent entities |
| `docs/upstream-contributions.md` | Bugs found in DataHub and the fixes submitted |

## License

Apache 2.0 — see [LICENSE](LICENSE).
