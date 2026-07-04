---
name: mempalace
description: PM-only MemPalace write-capable experiment memory layer for final autoresearch decisions. Stores completed experiment results, final metrics, reviewer verdicts, and structured knowledge graph facts.
version: 1.0.0
---

# MemPalace — PM Experiment Memory

## Activation

This skill is PM-only and write-capable. It must be assigned only to the
top-level autoresearch PM agent (`main`). Non-PM stage agents use
`mempalace-readonly` instead and must not receive this skill.

MemPalace is the only durable research memory layer for autoresearch. Use
MemPalace read tools automatically at PM setup and recovery phases, then use
write tools only after a completed experiment has a final decision.

Do not use OpenClaw built-in memory tools (`memory_search`, `memory_get`) or
Markdown memory files (`MEMORY.md`, `memory/YYYY-MM-DD.md`) for research
continuity. Those layers are disabled by policy so experiment state stays
structured, auditable, and queryable through MemPalace.

## Automatic Usage Rules

**On session start:**
1. `mempalace_status` — load palace overview
2. `mempalace_diary_read` with `agent_name: "autoresearch"` — review recent session notes

**On every research iteration:**
1. **Context setup** — PM reads prior experiments with `mempalace_search` and `mempalace_kg_query`, then delegates a read-only context packet to `context-curator`
2. **Debate/review/implementation/fix stages** — non-PM agents use only `mempalace-readonly` plus denied mutation tools in config
3. **Final log stage only** — after implementation, verification, review, fixes, and decision, the PM writes experiment results with `mempalace_add_drawer`, `mempalace_kg_add`, and `mempalace_diary_write`

Only the PM may write to MemPalace, and only after an experiment has a final
decision (`KEEP`, `DISCARD`, or `CRASH`). Debate notes, consensus drafts,
winning theories, `NO_CONSENSUS` outcomes, implementation plans, and in-progress
review findings are not MemPalace writes.

Non-PM stage agents are structurally read-only: they should not receive this
skill and should deny every MemPalace mutation/operation tool in config.

## What Goes in MemPalace

### Drawers (Verbatim Storage — `mempalace_add_drawer`)
- Full experiment result summaries (wing: `wing_quantipy`, room: `room_<experiment_id>`)
- Notebook conclusions (verbatim text from the conclusion section)
- Reviewer verdicts with reasoning

### Knowledge Graph (Structured Facts — `mempalace_kg_add`)
- Experiment → used → Feature (e.g., "T15-AMA" → "used" → "EMA9/EMA21 spread")
- Experiment → achieved → Metric (e.g., "T15-AMA" → "achieved" → "IS Sharpe 0.73")
- Experiment → failed_due_to → FailureMode (e.g., "T9-HRA" → "failed_due_to" → "data leakage")
- Feature → applied_to → Ticker (e.g., "VWAP deviation" → "applied_to" → "NVDA")
- Experiment → used_model → Model (e.g., "T15-AMA" → "used_model" → "HistGradientBoosting")

### Do NOT Store
- Ephemeral status updates (use `[TASK:status]` conventions)
- Raw data (experiments write notebooks + RESEARCH_LOG.md)
- Duplicates of existing drawers or KG facts; search before writing

## Tool Reference

### mempalace_search
Semantic search across all stored experiment content.

**When:** Context stage and any explicit PM recovery check.
**Examples:**
```
mempalace_search(query: "VWAP deviation feature results", wing: "wing_quantipy", limit: 10)
mempalace_search(query: "experiments with IS Sharpe above 0.5")
mempalace_search(query: "failure modes in mean-reversion strategies")
```

### mempalace_add_drawer
Store verbatim experiment content in the palace.

**When:** Final PM log stage after a completed experiment decision only.
**Example:**
```
mempalace_add_drawer(
  wing: "wing_quantipy",
  room: "room_t15_ama",
  content: "Experiment T15-AMA: Attention-Regime Adaptive MA Signal Classifier. Used EMA9/EMA21 spread × mention_zscore interaction feature with HistGradientBoostingClassifier. 7 tickers (PLTR, SOFI, HOOD, RIVN, LCID, MARA, BB). IS walk-forward Sharpe: 0.73 net. OOS Sharpe: 0.41. Reviewer: PASS. Decision: KEEP."
)
```

### mempalace_kg_add
Add a structured temporal fact to the knowledge graph.

**When:** Final PM log stage after a completed experiment decision only — one
triple per key relationship.
**Examples:**
```
mempalace_kg_add(subject: "T15-AMA", predicate: "achieved_is_sharpe", object: "0.73", valid_from: "2026-04-01")
mempalace_kg_add(subject: "T15-AMA", predicate: "used_feature", object: "EMA spread × mention zscore", valid_from: "2026-04-01")
mempalace_kg_add(subject: "T15-AMA", predicate: "used_model", object: "HistGradientBoosting", valid_from: "2026-04-01")
mempalace_kg_add(subject: "T15-AMA", predicate: "decision", object: "KEEP", valid_from: "2026-04-01")
```

### mempalace_kg_query
Query entity relationships from the knowledge graph.

**When:** Context stage — load prior experiment context.
**Examples:**
```
mempalace_kg_query(entity: "T15-AMA")
mempalace_kg_query(entity: "HistGradientBoosting", direction: "incoming")
mempalace_kg_query(entity: "NVDA", as_of: "2026-03-01")
```

### mempalace_kg_invalidate
Mark a fact as no longer true.

**When:** PM-only manual correction after a completed experiment record is
superseded or proven wrong. Do not use during debate, consensus,
implementation, review, or fix stages.
**Example:**
```
mempalace_kg_invalidate(subject: "T9-HRA", predicate: "status", object: "active")
```

### Other mutation tools

The MCP server also exposes maintenance and bulk mutation tools such as
`mempalace_checkpoint`, `mempalace_update_drawer`, `mempalace_delete_by_source`,
`mempalace_mine`, `mempalace_sync`, tunnel/hallway mutation tools,
`mempalace_hook_settings`, and `mempalace_reconnect`.

**When:** PM-only manual maintenance after a completed experiment decision or
operator-approved correction. Non-PM agents deny these tools in config.

### mempalace_diary_write
Record a session-level summary for continuity across sessions.

**When:** Final PM log stage after a completed experiment decision. Session
summaries may be written only if they summarize completed experiment outcomes.
**Example:**
```
mempalace_diary_write(
  agent_name: "autoresearch",
  entry: "Ran T15-AMA experiment. IS Sharpe 0.73, OOS 0.41. Key insight: attention regime proxy (mention_zscore) is the strongest interaction term. Next: try OBV-sentiment herding.",
  topic: "autoresearch-session"
)
```

### mempalace_diary_read
Browse past session notes for continuity.

**When:** Session start — get recent context.
**Example:**
```
mempalace_diary_read(agent_name: "autoresearch", last_n: 5)
```

### mempalace_status
Check palace health and drawer counts.

**When:** Session start — verify memory is accessible.

### mempalace_kg_timeline
Get chronological story of an entity.

**When:** Deep research into a specific experiment or feature's evolution.
**Example:**
```
mempalace_kg_timeline(entity: "HistGradientBoosting")
```

## Autoresearch Integration

| Phase | MemPalace Action |
|-------|-----------------|
| Session start | `mempalace_status` + `mempalace_diary_read` |
| Context | `mempalace_search` for prior experiments + `mempalace_kg_query` for entity context |
| Debate | Include curator MemPalace findings in every debater prompt; debaters use `mempalace-readonly` only |
| Review | Reviewer uses `mempalace-readonly` only for prior objections and methodology failures |
| Final log | PM-only: `mempalace_add_drawer` (verbatim result) + `mempalace_kg_add` (structured facts) + `mempalace_diary_write` |
| Session end | No write unless summarizing completed experiment decisions |

## Drawer Content Template

Include ALL of these in the drawer content:
- Experiment name (e.g., T15-AMA)
- Features used (named)
- Model used
- Tickers traded
- Data sources used
- Key metrics (IS Sharpe, OOS Sharpe, win rate, max drawdown)
- Reviewer verdict (PASS/FAIL + reasons)
- Decision (KEEP/DISCARD)
- Failure modes encountered or avoided

One drawer per experiment iteration. Wing: `wing_quantipy`. Room: `room_<experiment_id>`.

## Failure Policy

If `mempalace_*` tools return errors:
- Stop the autoresearch loop.
- Report the MemPalace failure to the human.
- Do not continue with hidden or unstructured state.
