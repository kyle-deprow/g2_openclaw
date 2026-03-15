# Tools

## Primary: Copilot CLI (via exec)

Delegate coding tasks to the Copilot CLI agent. Copilot runs autonomously with its own file search, multi-file edit, terminal, and planning capabilities.

### Invocation
```
bash pty:true workdir:~/repos/<project> command:"copilot --agent orchestrator -p '<prompt>' --yolo --model claude-opus-4.6 --no-auto-update"
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

Copilot CLI persists sessions at `~/.copilot/session-state/<uuid>/`. Each session has:
- `workspace.yaml` — cwd, git root, branch, summary, timestamps
- `events.jsonl` — full conversation history
- `session.db` — tool state, checkpoints

#### Launch in a specific repo
```
bash pty:true workdir:~/repos/quantipy command:"copilot -p '<prompt>' --yolo --model claude-opus-4.6 --no-auto-update"
```
The `workdir:` parameter sets Copilot's cwd. Copilot auto-detects the git root and scopes all file operations to that repo.

#### Resume most recent session
```
bash pty:true workdir:~/repos/quantipy command:"copilot -p '<follow-up prompt>' --yolo --continue --no-auto-update"
```
`--continue` resumes the **globally** most recent session (any repo). Use when you just finished a task and want to follow up.

#### Resume a specific session by ID
```
bash pty:true workdir:~/repos/quantipy command:"copilot -p '<follow-up prompt>' --yolo --resume=<session-id> --no-auto-update"
```
Use when resuming a specific earlier session. The session retains full conversation history and repo context.

#### Discover sessions for a repo
```
bash command:"for d in $(ls -t ~/.copilot/session-state/); do grep -l 'cwd: /home/dev/repos/quantipy' ~/.copilot/session-state/$d/workspace.yaml 2>/dev/null && grep '^summary:' ~/.copilot/session-state/$d/workspace.yaml; done"
```
Lists session UUIDs + summaries for a specific repo, most recent first.

#### When to start fresh vs. resume
| Situation | Action |
|-----------|--------|
| New task, clean slate | New session (no `--resume`) |
| Follow-up to just-completed task | `--continue` |
| Return to a specific earlier session | `--resume=<uuid>` |
| Task failed mid-way, need to retry | `--resume=<uuid>` with corrected prompt |

### Background Tasks
For long-running work, use background mode:
```
bash pty:true workdir:~/repos/quantipy background:true command:"copilot -p 'Your task' --yolo --model claude-opus-4.6 --no-auto-update"
```
Monitor with `process action:log sessionId:XXX`

### Model Selection
- **gpt-5.4** — Default, fast, capable
- **claude-opus-4.6** — Complex analysis, long context
- **gpt-5.2-codex** — Code-optimized

## Built-in Tools

| Tool | Use |
|------|----- |
| `exec` | Run shell commands, invoke Copilot CLI |
| `Read` / `Write` | File operations |
| `Glob` / `Grep` | File search |
| `memory_search` | Search OpenClaw memory |
| `web_search` / `web_fetch` | Web research |
