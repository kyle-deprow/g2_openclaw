# Tools

## Primary: Copilot CLI (via exec)

Delegate coding tasks to the Copilot CLI agent. Copilot runs autonomously with its own file search, multi-file edit, terminal, and planning capabilities.

### Critical: exec workdir

**Always use absolute paths for `workdir:`** — tilde (`~`) is NOT expanded.
- Correct: `workdir:/home/dev/repos/quantipy`
- Wrong: `workdir:~/repos/quantipy`

### Critical: Prompt quoting

**Never nest single quotes inside single quotes.** Use double quotes for the `-p` prompt:
- Correct: `-p "Your prompt here"`
- Wrong: `-p 'Your prompt here'` (breaks if prompt contains apostrophes)

### Invocation
```
bash pty:true workdir:/home/dev/repos/quantipy command:"copilot --agent orchestrator -p \"<prompt>\" --yolo --model claude-opus-4.6 --no-auto-update"
```
Use `--agent orchestrator` by default. It delegates internally to specialist agents defined in the repo's `.github/agents/`. Only use `--agent <name>` for a direct specialist when the task is narrow and single-purpose.

### Key Flags
| Flag | Purpose |
|------|---------|
| `-p "prompt"` | Non-interactive mode (exits after completion) |
| `--yolo` | Full auto — all permissions, no confirmation |
| `--model <model>` | Model selection (gpt-5.4, claude-opus-4.6, gpt-5.2-codex, etc.) |
| `--agent <name>` | Route to specialist agent defined in `.github/agents/<name>.agent.md` |
| `--add-dir <dir>` | Allow access to additional directories |
| `--no-ask-user` | Autonomous mode, no questions |
| `--no-auto-update` | Skip update checks |
| `--output-format json` | Structured JSONL output |
| `--resume` | Resume previous session |

### Session Management

Copilot CLI persists sessions at `/home/dev/.copilot/session-state/<uuid>/`. Each session has:
- `workspace.yaml` — cwd, git root, branch, summary, timestamps
- `events.jsonl` — full conversation history
- `session.db` — tool state, checkpoints

#### Launch in a specific repo
```
bash pty:true workdir:/home/dev/repos/quantipy command:"copilot --agent orchestrator -p \"<prompt>\" --yolo --model claude-opus-4.6 --no-auto-update"
```
The `workdir:` parameter sets Copilot's cwd. Copilot auto-detects the git root and scopes all file operations to that repo.

#### Resume most recent session
```
bash pty:true workdir:/home/dev/repos/quantipy command:"copilot -p \"<follow-up prompt>\" --yolo --continue --no-auto-update"
```
`--continue` resumes the **globally** most recent session (any repo). Use when you just finished a task and want to follow up.

#### Resume a specific session by ID
```
bash pty:true workdir:/home/dev/repos/quantipy command:"copilot -p \"<follow-up prompt>\" --yolo --resume=<session-id> --no-auto-update"
```
Use when resuming a specific earlier session. The session retains full conversation history and repo context.

#### Discover sessions for a repo
```
bash command:"for d in $(ls -t /home/dev/.copilot/session-state/); do grep -l 'cwd: /home/dev/repos/quantipy' /home/dev/.copilot/session-state/$d/workspace.yaml 2>/dev/null && grep '^summary:' /home/dev/.copilot/session-state/$d/workspace.yaml; done"
```
Lists session UUIDs + summaries for a specific repo, most recent first.

#### When to start fresh vs. resume
| Situation | Action |
|-----------|--------|
| New task, clean slate | New session (no `--resume`) |
| Follow-up to just-completed task | `--continue` |
| Return to a specific earlier session | `--resume=<uuid>` |
| Task failed mid-way, need to retry | `--resume=<uuid>` with corrected prompt |

### Model Selection
- **claude-opus-4.6** — Complex analysis, long context

## Built-in Tools

| Tool | Use |
|------|----- |
| `exec` | Run shell commands, invoke Copilot CLI |
| `Read` / `Write` | File operations (scoped to OpenClaw workspace only) |
| `Glob` / `Grep` | File search (scoped to OpenClaw workspace only) |
| `memory_search` | Search OpenClaw memory |
| `web_search` / `web_fetch` | Web research |

**Note:** Read/Write/Glob/Grep are scoped to the OpenClaw workspace, NOT to target repos. To access target repo files, use `exec` with `ls`, `cat`, `find`, etc.

## Long-Running Tasks

For Copilot sessions expected to take >2 minutes, use background mode:

### Launch
```
bash pty:true workdir:/home/dev/repos/quantipy background:true command:"copilot --agent orchestrator --yolo -p \"<task>\" --model claude-opus-4.6 --no-auto-update"
```

### Monitor Progress
```
process action:log sessionId:<id-from-launch>
```

### Task Status Convention
After launching, completing, or failing a background task, ALWAYS post a structured status message:

| Event | Format |
|-------|--------|
| Launch | `[TASK:running] <description> \| started: <HH:MM UTC>` |
| Complete | `[TASK:complete] <description> \| duration: <Xm> \| result: <1-line>` |
| Failure | `[TASK:failed] <description> \| error: <1-line reason>` |

These markers allow the gateway to detect task status on reconnect and display it to the user.

### Monitoring Cron
After launching a background task, create a monitoring cron:
```
cron_create: schedule "every 5m", delivery "none", mode "main", prompt "Check status of background process <sessionId>. If complete, post [TASK:complete] with results. If failed, post [TASK:failed] with error. If still running, do nothing. Delete this cron when task finishes."
```
