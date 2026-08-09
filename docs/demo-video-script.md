# Demo video script (under 3 minutes)

Everything below is a real command in this repo. Nothing is mocked and nothing
needs a DataHub instance, an API key, or Docker.

## Before you record

```bash
pip install -e .
chaperone demo --scenario walkthrough --pace 1.5
```

Run it once to warm the import cache — the first run of any Python CLI is a
second or two slower, and that pause on take one always looks like a bug.

Terminal setup that matters:

- **Use a UTF-8 terminal.** Windows Terminal or VS Code's terminal are fine.
  The old `cmd.exe` console renders the box-drawing characters as `??`. Check
  with `chcp 65001` if unsure.
- **Font size up, window narrow.** Aim for ~100 columns and a font big enough
  to read on a phone. Judges scrub these on laptops at half size.
- **Dark background, no transparency.**
- `clear` between shots.

Record at 1080p. OBS Studio is free; the Xbox Game Bar (`Win+G`) is already
installed on Windows and is enough for a screen capture with a mic.

---

## The script

Total spoken time is about 2:35, leaving margin under the 3:00 limit. The
timings are cumulative.

### Shot 1 — the problem (0:00–0:25)

**On screen:** the README's opening lines, or just your face. No terminal yet.

> "The DataHub MCP server now ships mutation tools. Agents don't just read the
> catalog anymore — they can retag it, redescribe it, reassign ownership.
>
> One agent mislabelling one column is an inconvenience. An agent looping over
> four hundred tables at machine speed is an incident — and the catalog is
> exactly where your governance decisions live.
>
> Today the mitigation is a line in a prompt: 'be careful with PII.' That's not
> a control. It's a suggestion, and it degrades as the context window fills."

### Shot 2 — the idea (0:25–0:40)

**On screen:** the ASCII diagram in the README.

> "Chaperone is a proxy that sits between the agent and DataHub. Every tool
> call gets checked against the catalog's own metadata — tags, tiers, owners,
> lineage — before it reaches DataHub.
>
> The governance rules are already *in* the catalog. Chaperone reads them out
> and enforces them, instead of restating them in a prompt and hoping."

### Shot 3 — the replay (0:40–1:50)

This is the core of the video. Run it live:

```bash
chaperone demo --scenario walkthrough --pace 1.5
```

Six decisions in about ten seconds. Talk over them as they appear — don't
narrate every line, just land the four verdicts:

> "Here's a documentation agent working through a real catalog slice.
>
> Call one: a search. Allowed, no concerns.
>
> Call two: it documents `order_items`, a low-risk leaf table. Allowed — this
> is the agent doing useful work, and Chaperone stays out of the way.
>
> Call three reads the customers table. **Redacted.** And this is the
> interesting part: the agent still gets the schema and the lineage it needs to
> keep working. Only the sensitive values are stripped. Allow-or-deny would
> have either leaked the data or stopped the agent dead.
>
> Call four edits `fct_orders`. **Review** — because it's Tier 1, and because
> it feeds a feature table one hop out and a deployed model two hops out. The
> agent's edit isn't thrown away, it becomes a proposal for a human owner.
>
> Call five: an unowned PII table. **Denied.**
>
> Call six is my favourite. The agent hallucinated a URN — `dim_customer`,
> singular. That asset doesn't exist. Chaperone refuses to create metadata
> against an unverifiable asset and tells the agent to check with `search`
> first."

Let the summary table land on screen: 6 calls, verdicts broken out, proposals
raised.

### Shot 4 — the refusal is evidence (1:50–2:10)

**On screen:** scroll back to the `fct_orders` REVIEW panel, or re-run:

```bash
chaperone check update_description \
  --urn "urn:li:dataset:(urn:li:dataPlatform:dbt,analytics.marts.fct_orders,PROD)" \
  --arg description="Order fact table."
```

> "Every refusal cites the catalog. Not 'permission denied' — *'`fct_orders` is
> read directly by 3 assets and reaches 8 in total, and feeds production ML.'*
>
> An agent can act on that. It can go find the owner, or propose the change
> instead. That's the difference between a guardrail and a wall."

### Shot 5 — writing back to the graph (2:10–2:35)

**On screen:** the writeback panel at the end of the replay, then the file.

```bash
chaperone demo --scenario walkthrough --write-examples examples
cat examples/agent-entity.json
```

> "And this is the part that makes the catalog better, not just safer.
>
> At the end of every session Chaperone registers the agent as an `aiAgent`
> entity — the entity type new in acryl-datahub 1.7.0 — and attaches every
> dataset it touched as upstream lineage.
>
> So DataHub can now answer a question it couldn't before: *which agents are
> operating on this table?* The next agent, and the next engineer, inherits
> that.
>
> Chaperone doesn't just read the context graph. It contributes back to it."

### Shot 6 — close (2:35–2:50)

**On screen:** the repo page on GitHub.

> "Apache 2.0, seventy tests, runs offline with no DataHub instance needed.
> Building it also turned up a packaging bug in acryl-datahub 1.7.0 that breaks
> `datahub datapack` for every non-interactive caller — root cause and the
> one-line fix are in the repo."

---

## If you only have time for one take

Shots 3 and 5 are the ones that score. The replay proves the policy engine
works on real catalog metadata, and the writeback is the judging criterion
everything else is measured against — "strong submissions go beyond reading
metadata and contribute back to the graph."

## Things not to do

- Don't speed up the video in post to fit the limit. Cut a shot instead.
- Don't show `pip install` running. Have it installed already.
- Don't read the rule IDs aloud. They're on screen; say what they mean.
- Don't apologise for the offline fixture. It's a deliberate feature: a judge
  can reproduce every frame of this video in about a minute.
