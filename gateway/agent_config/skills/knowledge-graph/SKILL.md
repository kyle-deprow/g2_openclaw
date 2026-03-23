---
name: knowledge-graph
description: Temporal knowledge graph for structured experiment tracking via Graphiti MCP. Use graph tools to record experiments, features, models, failure modes, and cross-experiment relationships.
version: 1.0.0
---

# Knowledge Graph — Experiment Memory

## When to Use

Use the knowledge graph (`graph_*` tools) to:
- Record experiment results with structured relationships (experiment → used feature → produced metric)
- Track failure modes across experiments (which features/models keep failing, and why)
- Query cross-experiment patterns ("which features appeared in experiments with IS Sharpe > 0.5?")
- Build temporal context ("what changed between experiment T8 v1 and T8 v2?")

Do NOT use for:
- General conversation memory (use built-in `memory_search`)
- Ephemeral status updates (use `[TASK:status]` conventions)
- Raw data storage (experiments write notebooks + RESEARCH_LOG.md)

## Entity Types

| Type | Description |
|------|-------------|
| Experiment | A named research experiment (T1-SVG, T8-BPV, T12-VWAP-REV) |
| Feature | An engineered feature or signal (VWAP deviation, volume imbalance, sentiment z-score) |
| Model | An ML model or algorithm (LightGBM, XGBoost, HMM, HistGradientBoosting) |
| DataSource | A data input channel (OHLCV 1-min, Reddit sentiment, news sentiment) |
| Ticker | A traded instrument (NVDA, AMD) |
| FailureMode | An identified failure pattern (data leakage, insufficient OOS, annualization bug) |
| Metric | A measured result (IS Sharpe 0.82, OOS Sharpe 0.34, win rate 0.54) |

## Tool Reference

### graph_add_memory
Record a structured event (experiment result, observation).

**When:** After Phase 6 (LOG) of every autoresearch iteration.

**Example:**
```
graph_add_memory(
  name: "T12-VWAP-REV result",
  episode_body: "Experiment T12-VWAP-REV used VWAP deviation + volume imbalance features with LightGBM on NVDA 1-min bars. IS walk-forward Sharpe: 0.73 net. OOS Sharpe: 0.41. Reviewer: PASS. Decision: KEEP.",
  source: "autoresearch",
  source_description: "OpenClaw autonomous research loop"
)
```

Graphiti extracts entities and relationships from natural language automatically.

### graph_search_nodes
Find entities by semantic query.

**When:** Phase 1 (RESUME) and Phase 2 (IDEATE).

**Example:** `graph_search_nodes(query: "VWAP deviation feature")`

### graph_search_memory_facts
Find relationships between entities.

**When:** Phase 2 (IDEATE) — cross-experiment patterns. Phase 7 (REFLECT) — meta-analysis.

**Example:** `graph_search_memory_facts(query: "experiments using LightGBM on NVDA")`

### graph_get_episodes
Retrieve recent episodes chronologically.

**When:** Session start — get temporal context.

### graph_get_status
Check graph server + FalkorDB connectivity.

### graph_delete_episode / graph_delete_entity_edge
Data correction only. Rare.

### graph_clear_graph
**DESTRUCTIVE.** Dev resets only. Never in autoresearch.

## Autoresearch Integration

| Phase | Graph Action |
|-------|-------------|
| Phase 1 (RESUME) | `graph_search_nodes` + `graph_search_memory_facts` for cross-experiment patterns |
| Phase 2 (IDEATE) | Include graph context in researcher prompt |
| Phase 6 (LOG) | `graph_add_memory` with full experiment result |
| Phase 7 (REFLECT) | `graph_search_memory_facts` for meta-patterns |

## Episode Body Template

Include ALL of these:
- Experiment name (e.g., T12-VWAP-REV)
- Features used (named)
- Model used
- Tickers traded
- Data sources used
- Key metrics (IS Sharpe, OOS Sharpe, win rate, max drawdown)
- Reviewer verdict (PASS/FAIL + reasons)
- Decision (KEEP/DISCARD)
- Failure modes encountered or avoided

One episode per experiment iteration.

## Graceful Degradation

If `graph_*` tools return errors (FalkorDB down, MCP server crashed):
- **Continue without graph.** It's additive, not blocking.
- Log the error to daily memory file.
- The autoresearch loop must NOT halt because the graph is unavailable.
