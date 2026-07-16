# Bootstrap - Quantipy Context

## Repository

`/home/dev/repos/quantipy` is the target quantitative-research platform. Its
current `AGENTS.md`, repo skills, source, and Codex agent definitions are the
methodology source of truth. Use `uv`; do not use pip or Poetry.

Key paths:

- `src/quantipy/`: async Python services and research modules.
- `src/quantipy/alpha/`: experiment-owned strategy modules.
- `notebooks/experiments/`: experiment notebooks.
- `RESEARCH_LOG.md`: human-readable experiment record.
- `.agents/skills/` and `.codex/agents/`: current Quantipy instructions.

## Runtime Contracts

Every Quantipy stage loads `quantipy-methodology` and
`quantipy-data-contract`. The compact data skill governs universe selection,
price hydration and cache reuse, corporate actions, unsupported data, execution
timing, history request limits, deterministic batching, and prompt hygiene. Use
the runner-injected platform-readiness receipt and the platform's universe
receipts; do not reconstruct platform capabilities from bootstrap prose.

## Research Scope

Research intraday equity strategies using real platform data, simple and
defensible indicator interactions, and optional Reddit/news sentiment. Define
the universe, prediction and holding horizon, position sizing, transaction
costs, train/CV/OOS split, null tests, and rejection criteria. Positions are
intraday and obey the target repo's close-out rule.

Select each historical universe through the data contract. Market cap is not a
point-in-time universe criterion. Historical `security_types` filtering,
including `security_types=("CS",)` for common stocks, is point-in-time
certified. The actual date range comes from readiness and coverage receipts,
not a hardcoded calendar promise. Require broad, auditable common-calendar
coverage and an untouched OOS holdout; do not invent missing observations or
capabilities.

## Compute Fit

The runner supplies a read-only capability snapshot. Every new debate
submission and implementation result includes a `compute_fit` object with:

- `target`: `none`, `cpu`, `gpu`, or `mixed`.
- `rationale`: fit to the hypothesis and data scale.
- `required_dependencies`: a JSON list; empty for `none` and containing only
  the declared compute dependencies required by the selected path.
- `benchmark_plan`: the planned wall-time, memory, or acceleration check.

`gpu` and `mixed` are valid only when the snapshot proves a usable GPU/CUDA
runtime and every declared dependency. Missing runtime or dependency evidence
is an exact infrastructure blocker. Stage agents do not install dependencies,
change execution devices, or manufacture capability evidence. CPU and `none`
remain valid choices.

## Experiment Artifacts

Each experiment owns:

- `src/quantipy/alpha/<strategy_name>/` module code.
- `notebooks/experiments/<strategy_name>.ipynb` as the orchestration and report.
- Focused tests for features, splits, backtest behavior, and metric extraction.
- Structured verification, review, and final-decision artifacts advanced by the
  deterministic runner.

The notebook records data inventory and receipts, hypothesis and universe
profile, feature engineering, tuning, walk-forward evaluation, transaction
costs, OOS results, null tests, and conclusion. Missing notebook/runtime
tooling is operator-owned infrastructure; PM and stage agents report exact
evidence and do not alter dependencies.

## Ownership And Memory

The PM orchestrates and decides. Implementer and fixer own experiment code in
the persisted disposable worktree. Shared platform, runtime, loader, harness,
or orchestration changes belong to the human/Codex operator. Preserve those
boundaries even when a shared change exposes an experiment defect.

MemPalace is the only durable autonomous research memory and only
`autoresearch-pm` may mutate it. Stage agents have read-only access. Store
compact receipt references and experiment facts, never full universe symbol
arrays.

## Commands

```bash
cd /home/dev/repos/quantipy
uv run pytest
uv run ruff check src/ tests/
uv run mypy src/
```
