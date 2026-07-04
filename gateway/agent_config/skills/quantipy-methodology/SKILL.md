---
name: quantipy-methodology
description: Deterministic source-of-truth loading for Quantipy stage agents before context, debate, implementation, review, and fix work.
version: 1.0.0
---

# Quantipy Methodology

This skill is for OpenClaw stage agents working on Quantipy. It keeps
methodology, repo rules, Codex role definitions, and target-repo skills sourced
from `/home/dev/repos/quantipy` at task time.

Do not copy Quantipy's `AGENTS.md`, `.agents/skills`, or `.codex/agents` files
into this repo. Quantipy is the source of truth. If the target repo changes,
load the current files from `/home/dev/repos/quantipy` again.

## Required Preflight

Before doing context, debate, consensus, implementation, review, or fix work:

1. Read `/home/dev/repos/quantipy/AGENTS.md` completely.
2. List `/home/dev/repos/quantipy/.agents/skills/*/SKILL.md`.
3. Read each target-repo skill that is relevant to the stage and task.
4. For any relevant skill that points to rule files, read the required rule
   files from Quantipy before acting on that skill.
5. List `/home/dev/repos/quantipy/.codex/agents/*.toml`.
6. Read the Codex agent definition files relevant to the stage.
7. State which Quantipy files were loaded before giving conclusions or making
   changes.

If a listed file is missing, say exactly which path is missing and continue only
if the remaining files are enough to perform the stage safely. Do not invent or
reconstruct missing methodology from memory.

## Stage Routing

Use this routing as a minimum. Read additional Quantipy files when the task
clearly needs them.

| Stage agent | Required Quantipy sources |
|-------------|---------------------------|
| `context-curator` | `AGENTS.md`, `experiment-data`, `data-querying`, relevant `.codex/agents/explorer.toml` and `researcher.toml` |
| `debater-microstructure` | `AGENTS.md`, `backtesting`, `experiment-data`, relevant `theorist.toml` and `researcher.toml` |
| `debater-data` | `AGENTS.md`, `data-collection`, `data-querying`, `experiment-data`, `backtesting`, relevant `researcher.toml` |
| `debater-skeptic` | `AGENTS.md`, `experiment-data`, `backtesting`, relevant `contrarian.toml` and `reviewer.toml` |
| `debater-theory` | `AGENTS.md`, `backtesting`, `experiment-data`, relevant `theorist.toml` and `researcher.toml` |
| `debater-implementation` | `AGENTS.md`, `backend-python`, `backtesting`, `data-querying`, `experiment-data`, relevant `backend-python.toml` and `orchestrator.toml` |
| `consensus-arbiter` | `AGENTS.md`, skills loaded by the debaters for the candidate theories, relevant `contrarian.toml`, `theorist.toml`, and `reviewer.toml` |
| `implementer` | `AGENTS.md`, `backend-python`, `backtesting`, `data-querying`, `experiment-data`, relevant `backend-python.toml` and `orchestrator.toml` |
| `reviewer` | `AGENTS.md`, `experiment-data`, `backtesting`, `data-querying`, relevant `reviewer.toml` and `contrarian.toml` |
| `fixer` | `AGENTS.md`, `backend-python`, the skill(s) governing the defect, relevant `backend-python.toml`, `orchestrator.toml`, and `reviewer.toml` |

## Execution Rules

- Treat target-repo methodology as current only after reading it in the active
  stage turn.
- Use shell commands from the OpenClaw workspace when needed to read Quantipy
  files outside the workspace, for example `sed`, `rg`, `find`, and `jq`.
- Do not change Quantipy methodology files unless the stage task explicitly asks
  for a methodology update.
- Do not substitute old G2 OpenClaw bootstrap knowledge for Quantipy's current
  target-repo instructions.
- If Quantipy methodology conflicts with this repo's autoresearch orchestration,
  report the conflict to the PM instead of silently choosing one.
