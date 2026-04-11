---
name: mempalace
description: Persistent memory layer for structured experiment tracking via MemPalace MCP. Automatically activated — use mempalace tools to store experiment results, search prior work, track temporal facts, and build cross-experiment knowledge.
version: 1.0.0
---

# MemPalace — Experiment Memory

## Activation

This skill is **always active**. Use mempalace tools automatically at the appropriate phases — no user prompt needed. MemPalace is your persistent memory for cross-experiment learning.

## Automatic Usage Rules

**On session start:**
1. `mempalace_status` — load palace overview
2. `mempalace_diary_read` with `agent_name: "autoresearch"` — review recent session notes

**On every research iteration:**
1. **Before ideation** — `mempalace_search` for prior experiments + `mempalace_kg_query` for entity relationships
2. **After logging results** — `mempalace_add_drawer` with verbatim result + `mempalace_kg_add` for structured facts
3. **During reflection** — `mempalace_search` + `mempalace_kg_query` for cross-experiment meta-patterns
4. **End of session** — `mempalace_diary_write` with session summary

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
- General conversation memory (use built-in `memory_search`)
- Ephemeral status updates (use `[TASK:status]` conventions)
- Raw data (experiments write notebooks + RESEARCH_LOG.md)

## Tool Reference

### mempalace_search
Semantic search across all stored experiment content.

**When:** Phase 1 (RESUME), Phase 2 (IDEATE), Phase 7 (REFLECT).
**Examples:**
```
mempalace_search(query: "VWAP deviation feature results", wing: "wing_quantipy", limit: 10)
mempalace_search(query: "experiments with IS Sharpe above 0.5")
mempalace_search(query: "failure modes in mean-reversion strategies")
```

### mempalace_add_drawer
Store verbatim experiment content in the palace.

**When:** After Phase 6 (LOG) of every autoresearch iteration.
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

**When:** After Phase 6 (LOG) — one triple per key relationship.
**Examples:**
```
mempalace_kg_add(subject: "T15-AMA", predicate: "achieved_is_sharpe", object: "0.73", valid_from: "2026-04-01")
mempalace_kg_add(subject: "T15-AMA", predicate: "used_feature", object: "EMA spread × mention zscore", valid_from: "2026-04-01")
mempalace_kg_add(subject: "T15-AMA", predicate: "used_model", object: "HistGradientBoosting", valid_from: "2026-04-01")
mempalace_kg_add(subject: "T15-AMA", predicate: "decision", object: "KEEP", valid_from: "2026-04-01")
```

### mempalace_kg_query
Query entity relationships from the knowledge graph.

**When:** Phase 1 (RESUME) — load prior experiment context.
**Examples:**
```
mempalace_kg_query(entity: "T15-AMA")
mempalace_kg_query(entity: "HistGradientBoosting", direction: "incoming")
mempalace_kg_query(entity: "NVDA", as_of: "2026-03-01")
```

### mempalace_kg_invalidate
Mark a fact as no longer true.

**When:** When an experiment result is superseded or a finding is corrected.
**Example:**
```
mempalace_kg_invalidate(subject: "T9-HRA", predicate: "status", object: "active")
```

### mempalace_diary_write
Record a session-level summary for continuity across sessions.

**When:** End of every autoresearch session.
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
| Phase 1 (RESUME) | `mempalace_search` for prior experiments + `mempalace_kg_query` for entity context |
| Phase 2 (IDEATE) | Include search results in researcher prompt |
| Phase 6 (LOG) | `mempalace_add_drawer` (verbatim result) + `mempalace_kg_add` (structured facts) |
| Phase 7 (REFLECT) | `mempalace_search` for meta-patterns + `mempalace_kg_query` for cross-experiment relationships |
| Session end | `mempalace_diary_write` with session summary |

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

## Graceful Degradation

If `mempalace_*` tools return errors:
- **Continue without memory.** It's additive, not blocking.
- Log the error to daily memory file.
- The autoresearch loop must NOT halt because MemPalace is unavailable.
