# Tools

## Primary: Copilot CLI (via exec)

Delegate coding tasks to the Copilot CLI agent. Copilot runs autonomously with its own file search, multi-file edit, terminal, and planning capabilities.

### Invocation
```
bash pty:true workdir:~/repos/<project> command:"copilot -p '<prompt>' --yolo --model gpt-5.4 --no-auto-update"
```

### Key Flags
| Flag | Purpose |
|------|---------|
| `-p "prompt"` | Non-interactive mode (exits after completion) |
| `--yolo` | Full auto — all permissions, no confirmation |
| `--model <model>` | Model selection (gpt-5.4, claude-opus-4.6, gpt-5.2-codex, etc.) |
| `--add-dir <dir>` | Allow access to additional directories |
| `--no-ask-user` | Autonomous mode, no questions |
| `--no-auto-update` | Skip update checks |
| `--output-format json` | Structured JSONL output |
| `--resume` | Resume previous session |

### Background Tasks
For long-running work, use background mode:
```
bash pty:true workdir:~/repos/quantipy background:true command:"copilot -p 'Your task' --yolo --model gpt-5.4 --no-auto-update"
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
