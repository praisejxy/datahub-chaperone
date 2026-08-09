# Upstream contributions to DataHub

Building Chaperone meant running the DataHub CLI and SDK the way an agent runs
them — non-interactively, with output piped somewhere. That is a slightly
unusual mode, and it surfaced a reproducible packaging bug that only appears in
exactly that situation.

---

## 1. `datahub datapack --help` crashes when stdout is not a TTY

**Affects:** `acryl-datahub` 1.7.0 (the published wheel; the git checkout is fine)
**Severity:** breaks the command entirely for any non-interactive caller
**Type:** packaging — a data file is missing from the built distribution

### Symptom

Running the command interactively works. Piping it — which is what a CI job, a
script, or an agent shelling out does — crashes:

```bash
datahub datapack --help | cat
```

```
FileNotFoundError: [Errno 2] No such file or directory:
  '.../site-packages/datahub/cli/datapack/resources/DATAPACK_AGENT_CONTEXT.md'
```

The failure is a hard traceback, not a warning, and it takes out `--help` —
so the command cannot even be introspected.

### Why the TTY check matters

`_DatapackGroup.format_help` in `datahub/cli/datapack/datapack_cli.py`
deliberately appends extra guidance when it detects it is *not* talking to a
human:

```python
def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
    super().format_help(ctx, formatter)
    formatter.write(f"\n{_EXPERIMENTAL_NOTICE}\n")
    if not sys.stdout.isatty():
        agent_text = (
            importlib.resources.files("datahub.cli.datapack.resources")
            .joinpath("DATAPACK_AGENT_CONTEXT.md")
            .read_text(encoding="utf-8")
        )
        formatter.write("\n")
        formatter.write(agent_text)
```

The intent is good: give an AI agent more context than a human needs. The
side effect is that the crash is invisible to the developer testing by hand and
guaranteed for the agent the feature was written for.

### Root cause

`DATAPACK_AGENT_CONTEXT.md` **exists in the repository** at
`metadata-ingestion/src/datahub/cli/datapack/resources/`, so this is not a
missing file — it is a missing packaging rule.

`metadata-ingestion/setup.py` lists data files explicitly and does not set
`include_package_data`. It covers the sibling directory but not this one:

```python
package_data={
    "datahub": ["py.typed", "constraints.txt"],
    ...
    "datahub.cli.gql": ["*.gql"],
    "datahub.cli.resources": ["*.md"],      # <- covers cli/resources
                                            # <- nothing covers cli/datapack/resources
},
```

The installed wheel shows the consequence exactly:

```
datahub/cli/resources/           GRAPHQL_AGENT_CONTEXT.md, INIT_AGENT_CONTEXT.md,
                                 LINEAGE_AGENT_CONTEXT.md, SEARCH_AGENT_CONTEXT.md
datahub/cli/datapack/resources/  __init__.py          <- the .md never shipped
```

The four `.md` files under the covered directory are present. The one under the
uncovered directory is not.

### Fix

One line in `metadata-ingestion/setup.py`:

```python
     "datahub.cli.gql": ["*.gql"],
     "datahub.cli.resources": ["*.md"],
+    "datahub.cli.datapack.resources": ["*.md"],
```

### Suggested hardening

The packaging fix resolves the bug. Separately, help text is a poor place for a
hard failure: a missing optional resource should degrade, not crash. Wrapping
the read makes the command robust against the same class of packaging slip in
future:

```python
if not sys.stdout.isatty():
    try:
        agent_text = (
            importlib.resources.files("datahub.cli.datapack.resources")
            .joinpath("DATAPACK_AGENT_CONTEXT.md")
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError):
        logger.debug("datapack agent context not available in this build")
    else:
        formatter.write("\n")
        formatter.write(agent_text)
```

### Reproduction

```bash
pip install 'acryl-datahub==1.7.0'
datahub datapack --help | cat        # FileNotFoundError
datahub datapack --help              # works — masked by the isatty() branch
```

Verified on `acryl-datahub` 1.7.0, Python 3.12, Windows. The
`isatty()` branch makes it platform-independent: any piped invocation hits it.

---

## How this relates to Chaperone

The same failure mode is the reason Chaperone's own `pyproject.toml` pins its
package data explicitly:

```toml
[tool.hatch.build]
include = [
  "src/chaperone/**/*.py",
  "src/chaperone/policies/*.yaml",
  "src/chaperone/fixtures/*.json",
]
```

Chaperone's policy packs and offline fixture graph are exactly the same kind of
non-Python asset that has to be loaded at runtime. Had they been left out, the
installed package would import cleanly and then fail the moment anyone ran it
from a wheel rather than from a checkout — which is precisely the bug above.
