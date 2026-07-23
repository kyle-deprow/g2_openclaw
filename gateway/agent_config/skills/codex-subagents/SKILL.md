---
name: codex-subagents
description:
  Delegating implementation, review, and research work from OpenClaw to Codex subagents. Use when running the autonomous Quantipy research loop, splitting work into implementation/review/fix phases, monitoring long-running Codex tasks, or recovering stuck Codex sessions.
---

# Codex Subagent Delegation

OpenClaw is the PM and conversation surface. Codex subagents are the execution
surface for target-repo research, implementation, review, and fixes.

## Delegation Contract

Every delegated Codex task needs:

1. Target repo and working directory.
2. OpenClaw stage agent name.
3. Exact task objective.
4. Files or directories to inspect first.
5. Verification commands.
6. Expected return summary.

Use Codex subagents only when a task benefits from isolated context or parallel
work. For small edits in this repo, a single Codex turn is usually enough.

## Quantipy Loop

Use the OpenClaw stage agents from the `autoresearch` skill. Existing target
repo Codex instructions can inform prompt content, but the stage names below
are authoritative:

Spawn these configured agent IDs directly. Do not use generic/default subagents,
inherited models, or per-spawn model overrides for autoresearch stages; the
repo config binds each stage to its model.

| OpenClaw stage | Role |
|---|---|
| `context-curator` | Read-only MemPalace and `RESEARCH_LOG.md` context packet |
| `debater-microstructure` | Market mechanics theory |
| `debater-data` | Data availability, coverage, and target construction |
| `debater-skeptic` | Leakage, overfit, and cherry-picking pressure |
| `debater-theory` | Statistical and finance rationale |
| `debater-implementation` | Buildability and verification cost |
| `consensus-arbiter` | 3-of-5 majority decision and implementation brief |
| `implementer` | End-to-end implementation |
| `reviewer` | Single GPT-5.6-sol high methodology review |
| `fixer` | Concrete fixes only |

## Implementation/Review/Fix Pattern

1. Spawn the configured `implementer` agent with a narrow prompt and required tests.
2. Wait for completion and inspect the returned summary, changed files, and
   verification output.
3. Spawn exactly one configured `reviewer` agent against the diff.
4. If findings exist, spawn the configured `fixer` agent with only those findings.
5. Repeat review/fix until the reviewer reports no must-fix issues.
6. Run final verification from the parent context.

## Detached Long Tasks

Any hydration, backtest, notebook execution, or similarly long verification
command must use `/home/dev/repos/g2_openclaw/scripts/run-long-task.sh --run-dir
<absolute-run-dir> -- ...` with bounded polling and the same durable run
artifacts. The launcher places the worker in a dedicated transient user-systemd
service with explicit memory bounds, outside the OpenClaw gateway cgroup.
It submits that unit with `systemd-run --no-block`, validates the unit start,
and waits only for coherent startup metadata; it does not retain a
`systemd-run --wait` client in the caller lifecycle. Once the launcher returns,
the caller may exit while the transient unit continues. To control an active
run, resolve its exact unit with
`systemctl --user whoami "$(cat <absolute-run-dir>/pid)"` and use
`systemctl --user stop <unit>` when required;
the worker records a signal stop as terminal failure and preserves the child's
actual exit status: an ordinary uncaught `SIGTERM` commonly yields `143`, while
a child that handles or delays `SIGTERM` may yield its own code (for example,
`7`).
Direct foreground execution is invalid. If `systemd-run` or the launcher cannot be
used, fail closed and report the infrastructure blocker without emitting a
stage artifact. Foreground tool calls that can outlive the OpenClaw watchdog are
unsafe because the PM can lose status and recovery evidence while the tool is
still blocked in the foreground.

Requirements:

1. Use a unique absolute run directory per launched command.
2. Record and read `pid`, `started_at`, `stdout.log`, `stderr.log`,
   `exit_code`, and `status.json`.
3. Treat launcher `status.json` as a narrow process ledger: it emits only
   `running`, `succeeded`, or `failed`.
4. Poll status on a bounded interval; do not wait forever on a foreground tool
   call.
5. Derive PM markers from that ledger and the logs. `[TASK:blocked]` is a
   PM-level bounded-polling classification for actionable blockers; it is not a
   literal launcher status.
6. Emit concise progress and completion markers tied to the launched run.
7. Clean up stale processes and stale run directories when they are no longer
   needed.
8. Do not reduce scope just to avoid detached execution. If the task requires a
   long command, launch it safely and report the real status.

## Status Markers

Long-running tasks should report concise markers that the gateway can surface
back to the G2 glasses:

```text
[TASK:started] <short objective>
[TASK:progress] <current phase>
[TASK:blocked] <blocking condition and needed action>
[TASK:done] <summary and verification>
```

`[TASK:blocked]` comes from PM interpretation of bounded polling, logs, and
recovery evidence. The launcher itself never writes `blocked` into
`status.json`.

## Recovery

- If a subagent exits after planning only, resume with: "Skip exploration.
  Execute the implementation plan now."
- If auth fails, run `openclaw models auth login --provider openai` for
  OpenClaw-routed Codex work, or `codex login` for direct local Codex CLI work.
- If a session is silent for several minutes, inspect the detached run
  directory, process state, and logs before killing it.
- Do not retry through another runtime.
