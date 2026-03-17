---
name: copilot-cli
description: Copilot CLI delegation infrastructure — invocation, background execution, session management, process sentinels, resume logic, log inspection, and debugging. Read this skill before any Copilot delegation.
version: 1.0.0
---

# Copilot CLI — Delegation Infrastructure

This skill covers everything about invoking, monitoring, resuming, and debugging Copilot CLI sessions. Read it before your first delegation and whenever a Copilot task fails unexpectedly.

## Invocation Basics

### Command Structure
```
exec(command: "copilot --agent orchestrator -p \"<prompt>\" --yolo --model claude-opus-4.6 --no-auto-update", pty: true, workdir: "/home/dev/repos/quantipy")
```

### Key Flags
| Flag | Purpose |
|------|---------|
| `-p "prompt"` | Non-interactive mode (exits after completion) |
| `--yolo` | Full auto — all permissions, no confirmation |
| `--model <model>` | Model selection (claude-opus-4.6 is default) |
| `--agent <name>` | Route to `.github/agents/<name>.agent.md` in the repo |
| `--add-dir <dir>` | Allow access to additional directories |
| `--no-ask-user` | Autonomous mode, no questions |
| `--no-auto-update` | Skip update checks (always use this) |
| `--output-format json` | Structured JSONL output |
| `--resume=<uuid>` | Resume a specific session by ID |
| `--continue` | Resume the globally most recent session |

### Critical: exec Parameter Syntax
The `exec` tool takes NAMED PARAMETERS — they are NOT part of the command string.

Correct:
```
exec(command: "copilot ...", pty: true, background: true, workdir: "/home/dev/repos/quantipy")
```

WRONG (causes "command not found"):
```
exec(command: "pty:true workdir:/foo copilot ...")
```

**Never put `pty:true`, `background:true`, or `workdir:` inside the command string.**

### Critical: Paths and Quoting
- **Always use absolute paths:** `workdir: "/home/dev/repos/quantipy"` — tilde `~` is NOT expanded.
- **Never nest single quotes:** Use double quotes for `-p`. Single quotes inside single quotes break.
  - Correct: `-p "Your prompt here with apostrophes"`
  - Wrong: `-p 'Your prompt here'`

## Agent Routing

**Default: `--agent orchestrator`.** The orchestrator reads other `.agent.md` files in the repo and delegates internally — it handles multi-step work, review cycles, and specialist routing.

Only bypass with `--agent <name>` for narrow single-shot tasks:
- `--agent backend-python` — quick migration/fix
- `--agent researcher` — structured research debate (ideation phase only)

## Background Execution Protocol

**ALL implementation sessions MUST use `background:true`.** Any Copilot run expected to take >2 minutes.

### The 5-Step Launch Sequence

Execute ALL steps in ONE turn. Do NOT respond with text before step 4.

1. **Get expiry epoch:**
   `exec(command: "echo $(( $(date +%s) + 7200 ))")`

2. **Record repo HEAD:**
   `exec(command: "cd /home/dev/repos/quantipy && git rev-parse HEAD")`
   Save this as HEAD_AT_LAUNCH — the sentinel needs it.

3. **Launch Copilot:**
   `exec(command: "copilot --agent orchestrator --yolo -p '<prompt>' --model claude-opus-4.6 --no-auto-update", pty: true, background: true, workdir: "/home/dev/repos/quantipy")`
   Note the PID from the output.

4. **Create sentinel (MANDATORY — same turn):**
   Call `cron_create` with PID, expiry, HEAD_AT_LAUNCH. See Sentinel Template below.

5. **Confirm to human:**
   Post `[TASK:running] <description> | started: <HH:MM UTC>`

**Why this order matters:** If you respond before creating the sentinel, the task runs blind — there is NO way to learn when Copilot finishes. This is the #1 most common failure mode.

### After Launch
| Event | Action |
|-------|--------|
| Sentinel: `[TASK:complete]` | Evaluate results → continue loop → delete cron |
| Sentinel: `[TASK:incomplete]` | Resume session (see Resume Logic) → delete cron |
| Sentinel: `[TASK:timeout]` | Post `[TASK:timeout]`, investigate → delete cron |
| Sentinel: `[TASK:failed]` | Log failure → move to next action → delete cron |

## Process Sentinel Template

The sentinel monitors a background Copilot process. It checks every 5 minutes whether the process is alive, and when it exits, captures git state, test results, notebook metrics, and session ID. It uses `sessions_send` to inject results into the G2 session.

**Before creating, get:** expiry epoch + repo HEAD (steps 1-2 above).

```
cron_create: schedule "every 5m", prompt "COPILOT SENTINEL. PID=<PID>. Repo=<REPO_PATH>. Expiry=<EXPIRY_EPOCH>. HEAD_AT_LAUNCH=<HEAD_HASH>.
Step 1: exec bash command:\"ps -p <PID> -o pid= 2>/dev/null || echo EXITED\"
Step 2: exec bash command:\"date +%s\"
If output does NOT contain EXITED → respond 'PID <PID> alive'. STOP. No other tool calls.
If current epoch > Expiry → use sessions_send(sessionKey:\"agent:claw:g2\", message:\"[TASK:timeout] Copilot PID <PID> exceeded 2h TTL\"). Then cron_delete this cron. STOP.
If EXITED → run FOUR commands:
  exec bash command:\"cd <REPO_PATH> && git rev-parse HEAD\"
  exec bash command:\"cd <REPO_PATH> && git log --oneline -3\"
  exec bash command:\"cd <REPO_PATH> && uv run pytest -q --tb=line 2>&1 | tail -5\"
  exec bash command:\"ls -t /home/dev/.copilot/session-state/ | head -1\"
If HEAD == HEAD_AT_LAUNCH (no new commits) → use sessions_send(sessionKey:\"agent:claw:g2\", message:\"[TASK:incomplete] PID <PID> exited with no new commits. Session: <session-id>. Git: <git log>. Tests: <test summary>\"). Then cron_delete. STOP.
Else → use sessions_send(sessionKey:\"agent:claw:g2\", message:\"[TASK:complete] PID <PID> exited. Commits: <git log>. Tests: <test summary>.\"). Then cron_delete. STOP."
```

### Sentinel Configuration Rules

| Rule | Why |
|------|-----|
| Do NOT use `delivery "announce", channel "g2"` | The G2 gateway is NOT a registered OpenClaw channel. Channel-based delivery ALWAYS fails. |
| Use `sessions_send` for exit notifications | The sentinel uses `sessions_send(sessionKey:"agent:claw:g2", ...)` to inject results into the G2 session. This requires `tools.sessions.visibility=all` in openclaw.json. |
| Do NOT specify `model` | Default works. Specifying a model causes auth errors in isolated crons. |
| Do NOT specify `execution` | Default (isolated) is correct. `execution: "main"` FAILS for named agents. |
| Do NOT pass `context` | Not a valid `cron_create` parameter. |
| Prompt must be self-contained | Include PID, repo path, HEAD, and expiry. Isolated crons have NO conversation history. |
| Hard TTL: 2 hours | Embed expiry epoch (creation + 7200s). Sentinel self-deletes on expiry. |

### How It Works
- **Alive ticks (~90% of calls):** One `ps` command → "alive" response. Cheap, no delivery needed.
- **Exit tick (once):** Runs git log + pytest. Compares HEAD to HEAD_AT_LAUNCH. Uses `sessions_send` to inject `[TASK:complete]` or `[TASK:incomplete]` into the G2 session.
- **Full evaluation:** Happens in YOUR next turn when you see the sessions_send message in the G2 session.

## Incomplete Task Resume

When sentinel reports `[TASK:incomplete]` (Copilot exited but HEAD unchanged — no new commits), Copilot spent its session on exploration/planning without producing code. This is the most common failure with the orchestrator agent.

### Resume Protocol

1. **Resume the session** using the session ID from the sentinel:
   ```
   exec(command: "copilot --resume=<session-id> -p \"Your previous session explored and planned but did not implement any code. Skip exploration. Execute the implementation plan now — create modules, tests, notebook, run pytest, commit on success.\" --yolo --model claude-opus-4.6 --no-auto-update", pty: true, background: true, workdir: "<REPO_PATH>")
   ```

2. **Create a new sentinel** for the resumed session (same template, new PID, same HEAD_AT_LAUNCH).

3. **Max 2 resumes.** If the 2nd resume also produces `[TASK:incomplete]`:
   - Report `[TASK:failed] Copilot unable to produce output after 2 resumes. Session: <session-id>`
   - Move to next action (discard proposal, try next experiment, etc.)

### Why Sessions Go Incomplete
- The orchestrator spawns explore subagents that consume all turns on codebase reading
- The orchestrator writes a plan.md in session-state but never starts implementation
- There is NO hard turn limit in Copilot CLI — the agent simply chose to stop after planning

## Session Management

### Where Sessions Live
`/home/dev/.copilot/session-state/<uuid>/` — each contains:
- `workspace.yaml` — cwd, git root, branch, summary, timestamps
- `events.jsonl` — full conversation history
- `session.db` — tool state, checkpoints
- `plan.md` — (if created) the agent's implementation plan

### Discover Sessions for a Repo
```
exec bash command:"for d in $(ls -t /home/dev/.copilot/session-state/); do grep -l 'cwd: /home/dev/repos/quantipy' /home/dev/.copilot/session-state/$d/workspace.yaml 2>/dev/null && grep '^summary:' /home/dev/.copilot/session-state/$d/workspace.yaml; done"
```

### When to Start Fresh vs. Resume
| Situation | Action |
|-----------|--------|
| New task, clean slate | New session (no `--resume`) |
| Follow-up to just-completed task | `--continue` |
| Return to a specific earlier session | `--resume=<uuid>` |
| Task exited with no output | `--resume=<uuid>` with directive to implement |

## Log Inspection & Debugging

### Copilot CLI Process Logs
```
~/.copilot/logs/process-<timestamp>-<pid>.log
```
Find the log for a recent session:
```
exec bash command:"ls -lt ~/.copilot/logs/ | head -5"
```

### What to Look For in Logs

**Session shutdown telemetry** (at end of log):
```
exec bash command:"tail -100 ~/.copilot/logs/<logfile> | grep -A20 'session_shutdown'"
```
Key fields:
- `shutdown_type: "routine"` — normal exit (agent chose to stop)
- `model_*_request_count` — how many API calls per model
- `lines_added` / `files_modified_count` — did it produce anything?
- `total_premium_requests` — cost indicator

**Tool usage breakdown:**
```
exec bash command:"grep -o '\"tool\":\"[^\"]*\"' ~/.copilot/logs/<logfile> | sort | uniq -c | sort -rn | head -10"
```
If `view` and `bash` dominate with zero `create`/`edit` → the agent explored but never coded.

**Subagent tracking:**
```
exec bash command:"grep 'subagent_completed\|subagent_created' ~/.copilot/logs/<logfile> | head -10"
```
Shows which subagents were spawned (explore, plan, implement).

### Common Failure Patterns

| Symptom | Cause | Fix |
|---------|-------|-----|
| 0 files modified, plan.md in session-state | Orchestrator explored but never implemented | Resume with `--resume=<uuid>` + explicit "implement now" |
| Import errors in notebook | Copilot used wrong module path | Fix prompt to specify exact import paths |
| Tests pass but notebook fails to execute | Missing dependency or data | Check notebook cell errors, fix deps |
| PID exits immediately (< 30s) | Auth failure or model error | Check `~/.copilot/logs/` for error, verify model config |
| Session hangs (sentinel shows alive for >1h) | Complex task or stuck in retry loop | Check process CPU: `ps -p <PID> -o %cpu` — if 0%, kill it |

## Code Delegation — Absolute Rule

**NEVER create, modify, or delete code files directly.** Not with Write, not with `exec cat/echo/tee/sed`, not with any tool. ALL code changes in target repos go through Copilot CLI.

You are a manager. You delegate. You verify. You do not type code.

Violations produce untested, uncommitted, unreviewed code. Copilot CLI handles multi-file edits, test runs, commits, and error recovery. You cannot replicate that with shell one-liners.

**Only acceptable way to change code:**
```
exec(command: "copilot --agent orchestrator -p \"<prompt>\" --yolo --model claude-opus-4.6 --no-auto-update", pty: true, background: true, workdir: "/home/dev/repos/<repo>")
```

## Delegation Modes

### Pre-Handoff Scaffolding Review

**Before EVERY delegation to a target repo, evaluate whether the repo's agent files need improvement.** This is NOT a mandatory update — it's a conditional check. Only update if there's evidence of a problem.

**When to review scaffolding:**
- First delegation to a new repo (agents may not exist yet)
- 2+ CRASHes or `[TASK:incomplete]` with the same root cause pattern
- Agent produced wrong output type (e.g., explored when told to implement)
- Copilot ignored instructions clearly stated in `.github/copilot-instructions.md`
- New convention or pattern discovered that existing agents don't know about

**When NOT to review scaffolding:**
- Task completed successfully — don't fix what isn't broken
- First attempt at a new experiment type — let it run first, evaluate after
- Minor quality issues (formatting, naming) — not worth the overhead

**Review procedure (lightweight — read-only commands in YOUR turn):**
```
exec bash command:"cat <REPO_PATH>/.github/copilot-instructions.md | head -30"
exec bash command:"ls <REPO_PATH>/.github/agents/"
exec bash command:"head -20 <REPO_PATH>/.github/agents/orchestrator.agent.md"
```

Assess: Are instructions still accurate? Do agents reference correct paths/patterns? Are there stale agents nobody uses?

**If update needed — delegate to Copilot (non-blocking, before the main task):**
```
exec(command: "copilot --agent orchestrator -p 'Read .github/copilot-instructions.md and .github/agents/. Based on these recent failures: <describe patterns>. Update instructions to prevent these. Remove stale agent files not used in 2+ rounds. Keep lean.' --yolo --model claude-opus-4.6 --no-auto-update", pty: true, workdir: "<REPO_PATH>")
```

This is a blocking foreground call (not background) — it's fast (~1 min) and must complete before the main task launches. The main task needs correct agents to succeed.

**Key rule: scaffolding updates are a means, not an end.** Don't get stuck in a meta-loop of endlessly improving agent files. Review → fix if broken → move on to the actual work.

### SCAFFOLD — Setup Coding Environment
Before first delegation to a repo, ensure it has `.github/copilot-instructions.md` + `.github/agents/*.agent.md`.
```
exec(command: "copilot --agent orchestrator -p 'Read .github/copilot-instructions.md and .github/agents/. Fix stale refs, add missing patterns, remove irrelevant rules. Keep lean.' --yolo --model claude-opus-4.6 --no-auto-update", pty: true, workdir: "/home/dev/repos/quantipy")
```

### RESEARCH — Delegate a Question
Structure the prompt with: what you're looking for, constraints (our data: 1-min OHLCV, sentiment, volume), what to return.
```
exec(command: "copilot --agent orchestrator -p 'Search the web for <topic>. Return: name, formula, data requirements, references.' --yolo --model claude-opus-4.6 --no-auto-update", pty: true, workdir: "/home/dev/repos/quantipy")
```

### ENGINEER — Delegate Implementation
Structure the prompt with: exact files, existing patterns to follow, tech requirements, verification command.
```
exec(command: "copilot --agent orchestrator -p '<task>. Follow pattern in <file>. Run uv run pytest after.' --yolo --model claude-opus-4.6 --no-auto-update", pty: true, background: true, workdir: "/home/dev/repos/quantipy")
```

## Prompt Discipline

Every `copilot -p` invocation MUST include:
- The repo's tech stack and constraints
- What already exists (name specific modules)
- What specifically to do (exact files, functions, behavior)

BAD — vague:
```
copilot -p "Add some technical indicators"
```

GOOD — specific:
```
copilot -p "Add RSI calculation to src/quantipy/technical_indicators/calculators/momentum.py. Follow the pattern in volume.py — dataclass calculator, async service method, pytest tests in tests/technical_indicators/. Use 1-min OHLCV data from the price_data module. Run tests after."
```

## Task Status Convention

After launching, completing, or failing a background task, post a structured status:

| Event | Format |
|-------|--------|
| Launch | `[TASK:running] <description> \| started: <HH:MM UTC>` |
| Complete | `[TASK:complete] <description> \| duration: <Xm> \| result: <1-line>` |
| Incomplete | `[TASK:incomplete] <description> \| session: <uuid>` |
| Failure | `[TASK:failed] <description> \| error: <1-line reason>` |
| Timeout | `[TASK:timeout] <description> \| exceeded 2h TTL` |

These markers allow the gateway to detect task status on reconnect and display it on G2 glasses.
