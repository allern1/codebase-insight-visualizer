---
name: codebase-insight-visualizer
description: Analyze a codebase and produce an interactive single-file architecture dashboard — deterministic dependency facts (script-extracted, AI cannot alter) plus AI semantic sticky notes (design trade-offs, alternatives, known risks, runtime flows). Use when the user asks to map/visualize project structure, dependencies, module boundaries, runtime flows ("how does this run?"), or to see what code an AI changed (gold highlighting, time-machine snapshot diff). Works with or without repository evidence; supports incremental diffs to save tokens.
license: MIT
metadata:
  version: "0.1.0"
  author: allern1
---

# Codebase Insight Visualizer

Turn "dead code" into living knowledge. This skill ships with its complete zero-dependency Python runtime: `scripts/` (scanner / fingerprint / lifecycle / manifest_builder / build), `schemas/`, `instructions.md`, `config.yaml`.

## Locate the runtime

This `SKILL.md` is installed alongside the runtime (same directory). Resolve `SKILL_ROOT` as the directory containing this file — most agent hosts expose the skill directory; otherwise `cd` to the cloned repo root (commands below use `$SKILL_ROOT`).

```bash
SKILL_ROOT="$(dirname "$(dirname "$(readlink -f "$0")")")"   # if run from a shell
# In an agent session, prefer the host-provided skill dir; commands use the same layout:
#   $SKILL_ROOT/scripts/scanner.py, $SKILL_ROOT/instructions.md, $SKILL_ROOT/config.yaml
```

## Pipeline (order is mandatory: facts before reason)

1. **Scan facts** (deterministic; second run is incremental, only changes are revisited):

   ```bash
   python "$SKILL_ROOT/scripts/scanner.py" --project <project-path> --config "$SKILL_ROOT/config.yaml"
   ```

   Outputs `fact_graph.json` in the cache dir (`~/.cache/project_viz/<root-hash>/` by default): nodes, import edges (`factual: true`), repository evidence (`revision`, `root`). The AI must NOT alter these facts.

2. **AI semantic enrichment**: read `fact_graph.json`, follow `$SKILL_ROOT/instructions.md` strictly. Fill ONLY: `design_reason` / `alternatives` / `known_risks` / `code_snippet` / `story` / `flows` / `soul_questions`. Iron rules: never modify fact fields; never invent topology; every `known_risk` carries `file:line`; adjacent flow steps without a factual edge MUST declare `gap_hint`. Emit one pure JSON file (no markdown fences).

3. **Stitch & validate**:

   ```bash
   python "$SKILL_ROOT/scripts/manifest_builder.py" \
     --fact-graph <fact_graph.json> --ai-output <ai.json> \
     --out <final_manifest.json> [--old-manifest <previous>] \
     --ai-files <files created/modified by THIS AI session, comma-separated>
   ```

   **`--ai-files` is mandatory when an AI session produced the code being analyzed** — the executing agent knows exactly which files it wrote/changed; without it, committed AI code cannot be attributed (the triple-rule judge then only sees an mtime hint). Invented nodes / unbaked edges are dropped and reported. Any validation failure preserves the previous artifact and exits non-zero.

4. **Build the deliverable**:

   ```bash
   python "$SKILL_ROOT/scripts/build.py" --manifest <final_manifest.json> \
     --out dashboard.html [--snapshots-dir <cache>/snapshots] [--echarts <local echarts.min.js>]
   ```

   `--snapshots-dir` injects the time-machine diff; `--echarts` inlines ECharts for intranet/offline delivery. Commands are zero-dependency Python (3.11+); templates ship in `demo/`.

## Iron rules

- No `fact_graph.json` → run step 1 first; never draw topology from memory.
- Facts (`id` / `file_path` / `hash` / `symbols` / `status` / factual edges) are read-only for the AI.
- Incremental: unchanged nodes keep old semantics verbatim; deleted nodes keep their story archive.
- If AI output is invalid JSON: drop this round's semantics, generate the degraded fact-only dashboard, and say so.

## Report back

Node count / AI share / delta stats / output path / validation receipt. Mention the gold (AI-generated) nodes and the time-machine diff view when snapshots exist.
