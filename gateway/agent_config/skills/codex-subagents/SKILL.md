---
name: codex-subagents
description:
  Delegating implementation, review, and research work from OpenClaw to Codex subagents. Use when running the autonomous Quantipy research loop, splitting work into implementation/review/fix phases, monitoring long-running Codex tasks, or recovering stuck Codex sessions.
---

# Codex Subagent Delegation

OpenClaw is the PM and conversation surface. Native Codex `spawn_agent` is the
execution surface for target-repo research, implementation, review, and fixes.
OpenClaw `sessions_spawn` is not a valid substitute for autoresearch stages.

## Delegation Contract

Every delegated Codex task needs:

1. Target repo and working directory.
2. Native Codex stage agent name from `.codex/agents/*.toml`.
3. Exact task objective.
4. Files or directories to inspect first.
5. Verification commands.
6. Expected return summary.

Use native Codex stage agents only when a task benefits from isolated context
or parallel work. For small edits in this repo, a single Codex turn is usually
enough.

## Quantipy Loop

Use the native Codex stage agents from the `autoresearch` skill. Existing
target repo Codex instructions can inform prompt content, but the stage names
below are authoritative:

Call `spawn_agent` with these configured agent names directly. Do not use
OpenClaw `sessions_spawn`, generic/default agents, inherited models, or
per-spawn model overrides for autoresearch stages; the repo config and native
Codex TOMLs bind each stage to its model.

| Native Codex stage | Role |
|---|---|
| `context_curator` | Read-only MemPalace and canonical decision-receipt context packet |
| `debater_microstructure` | Market mechanics theory |
| `debater_data` | Data availability, coverage, and target construction |
| `debater_skeptic` | Leakage, overfit, and cherry-picking pressure |
| `debater_theory` | Statistical and finance rationale |
| `debater_implementation` | Buildability and verification cost |
| `consensus_arbiter` | 3-of-5 majority decision and implementation brief |
| `implementer` | End-to-end implementation |
| `reviewer` | Single GPT-5.6-sol high methodology review |
| `fixer` | Concrete fixes only |

## Implementation/Review/Fix Pattern

1. Call native `spawn_agent` for the configured `implementer` agent with a narrow prompt and required tests.
2. Wait for completion and inspect the returned summary, changed files, and
   verification output.
3. Call native `spawn_agent` for exactly one configured `reviewer` agent against the diff.
4. If findings exist, call native `spawn_agent` for the configured `fixer` agent with only those findings.
5. Repeat review/fix until the reviewer reports no must-fix issues.
6. Run final verification from the parent context.

For Quantipy work, require a committed `quantipy-experiment-v2` manifest with
exactly `prepare`, `smoke`, `feasibility`, and `model`. Run focused tests, then detach
the exact `env PYTHONDONTWRITEBYTECODE=1 uv --directory <canonical-runtime-root> run --frozen --no-sync quantipy experiment run MANIFEST
--output-root ROOT --run-id
autoresearch-i<iteration>-<commit12>` command. Only `ROOT/run-id/run.json`
under the runner-declared fixed private runs root proves full verification.
Requested panels require their nested typed receipt and bound files. If
focused tests prevent execution, return the exact failed command
and evidence needed for `quantipy_execution_not_started`; do not claim a
missing runtime receipt. G2 reserves that absent run directory with a private
tombstone; retry only from a new implementation/fix commit with its new
deterministic run ID. Notebook, `nbconvert`, and `papermill` execution can
smoke-test or render a report only and never establish PASS.

## Detached Long Tasks

Any hydration, backtest, notebook execution, or similarly long verification
command must create a one-time private command file with
`uv run --no-sync gateway-cli autoresearch-create-command-file --output
<absolute-command-file>` and then use
`/home/dev/repos/g2_openclaw/scripts/run-long-task.sh --run-dir
<absolute-run-dir> --manifest <absolute-manifest.json> --command-file
<absolute-command-file>` with bounded polling and the same durable run artifacts.
The command-file helper reads the schema-v1 stdin protocol, creates the file
atomically with `O_EXCL`/`O_NOFOLLOW` mode 0600, and the launcher rejects all
positional command payloads. The launcher places the worker in a dedicated
transient user-systemd service with explicit memory bounds, outside the OpenClaw
gateway cgroup.
It submits that unit with `systemd-run --no-block`, validates the unit start,
and waits only for coherent startup metadata; it does not retain a
`systemd-run --wait` client in the caller lifecycle. Once the launcher returns,
the caller may exit while the transient unit continues. To control an active
run, resolve its exact unit with
`systemctl --user whoami "$(cat <absolute-run-dir>/pid)"` and use
`systemctl --user stop <unit>` when required;
the worker records a signal stop as terminal failure after a bounded
TERM/grace/KILL sequence. An ordinary uncaught `SIGTERM` commonly yields `143`;
a TERM-resistant child is killed after grace and records signal `9`.
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
   Any actionable blocker is inferred from bounded polling, logs, and recovery
   evidence; it is not a literal launcher status.
4. Poll status on a bounded interval; do not wait forever on a foreground tool
   call.
5. Derive concise PM status from that ledger and the logs.
6. Emit concise progress and completion prose tied to the launched run.
7. Clean up stale processes and stale run directories when they are no longer
   needed.
8. Do not reduce scope just to avoid detached execution. If the task requires a
   long command, launch it safely and report the real status.

For the mandatory Quantipy verification run, the exact command is `env
PYTHONDONTWRITEBYTECODE=1 uv --directory <canonical-runtime-root> run --frozen --no-sync quantipy experiment run MANIFEST --output-root ROOT
--run-id RUN_ID`. It must be launched here, never directly in a foreground
tool call. Set the immutable run manifest's `expected_artifact_path` to the
known `ROOT/RUN_ID/run.json`. Under the non-malicious same-host agent model,
PASS requires the detached worker's sealed artifact/status attestation; a
verifier claim cannot replace it. Evidence must bind the detached run
directory and manifest digest, require EOF-drained bounded-tail log receipts
with truthful truncation metadata, and match current artifact bytes to the
worker's size/SHA-256 attestation.

## Recovery

- If a subagent exits after planning only, resume with: "Skip exploration.
  Execute the implementation plan now."
- If auth fails, run `openclaw models auth login --provider openai` for
  OpenClaw-routed Codex work, or `codex login` for direct local Codex CLI work.
- If a session is silent for several minutes, inspect the detached run
  directory, process state, and logs before killing it.
- Do not retry through another runtime.
