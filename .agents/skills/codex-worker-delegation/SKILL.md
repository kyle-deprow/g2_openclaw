---
name: codex-worker-delegation
description: Delegate bounded coding, review, and verification work to native Codex subagents using gpt-5.6-luna at xhigh reasoning. Use when the main Codex session should orchestrate implementation instead of editing the delegated code itself.
---

# Native Codex Worker Delegation

The main Codex session is the orchestrator. Use the native `spawn_agent` tool to
hand off bounded implementation work to a Codex subagent. Do not invoke
`codex exec`, `codex resume`, or any other Codex CLI worker from the shell.

## Coding worker

For ordinary repository implementation work, use the native `worker` role with
the explicitly requested Luna tier:

```text
spawn_agent({
  agent_type: "worker",
  model: "gpt-5.6-luna",
  reasoning_effort: "xhigh",
  message: "<strict task prompt>"
})
```

The model and reasoning override are intentional for this delegation path.
For a repo-configured specialist such as `backend-python`, `g2-development`,
or `azure-bicep`, use its configured `agent_type` instead and preserve the
model and reasoning declared in `.codex/agents/<agent>.toml`.

Do not use the OpenClaw `sessions_spawn` tool for this workflow. That creates an
OpenClaw session, not a native Codex subagent handoff.

## Handoff prompt contract

Every coding handoff must include:

1. The exact absolute or repo-relative files the worker owns.
2. The behavior, interfaces, edge cases, and acceptance criteria.
3. A scope fence naming files and actions that are out of scope.
4. The exact verification commands and a requirement to report real output.
5. A final response contract listing changed files, tests run, remaining risks,
   and ending with `DONE` only when the task is complete.

Use this shape:

```text
You are the implementation worker. You are not alone in the repository; do not
revert edits made by other agents. Own only the files listed below.

## Task
<specific implementation objective>

## Files you may change
- <path>

## Files you must not change
- <path or category>

## Requirements
- <precise behavior and edge cases>

## Verification
Run `<exact command>` and report its actual result.

## Completion report
List changed files, verification output, and unresolved issues. Reply DONE as
the final line only if the implementation is complete.
```

For multi-phase work, write a durable plan in `.archive/PLAN-<task>.md` and
give the worker its path. Keep the plan read-only unless explicitly listed as
an owned file. Split parallel work into disjoint write sets.

## Orchestration cycle

1. Inspect enough of the repository to define a bounded task and decide what
   the main session will do locally while the worker runs.
2. Spawn one worker per disjoint implementation slice. Do not duplicate their
   work in the parent session.
3. Continue non-overlapping local work while workers run. Use `wait_agent`
   only when the returned result is needed for the next critical-path step.
4. Inspect the worker's changed files and diff when it returns. Do not trust a
   claimed test result without running the relevant verification yourself.
5. For meaningful changes, spawn a separate Luna worker for an independent
   review, with read-only scope and the exact diff or commit under review.
6. If review finds defects, send concrete file-and-line findings to the owning
   worker with `send_input` so it can fix them in context. Re-review the result.
7. The parent orchestrator owns final integration, verification, and commits
   unless the user explicitly delegates those actions.

## Review prompt minimum

Ask reviewers to check behavioral correctness, regressions, error handling,
scope compliance, tests, typing, and project-specific skill rules. Require
findings ordered as `Must-Fix`, `Should-Fix`, and `Nits`, followed by one of:
`READY`, `NEEDS WORK`, or `MAJOR ISSUES`.

## Safety boundaries

- Never grant a worker a broader filesystem scope than the target repository.
- Never point a worker at `~/.openclaw/`, live runtime state, credentials, or
  deployment scripts unless the user explicitly requests that operation.
- Do not dispatch parallel workers with overlapping write sets.
- Do not ask a worker to modify `/home/dev/repos/quantipy` outside the defined
  autoresearch workflow.
- If a worker stops after planning, send a follow-up through `send_input`:
  `Skip exploration. Execute the approved implementation plan now.`
- If a worker fails, inspect its returned status and worktree before retrying;
  do not silently switch runtimes or models.

## Verification for this repository

Use the smallest relevant command first, then broaden based on risk:

```text
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy gateway tests
cd g2_app && npm test
```
