# Tools

## Primary: Copilot CLI (via exec)

Delegate coding tasks to the Copilot CLI agent. **Read the `copilot-cli` skill** for the full reference: invocation syntax, flags, agent routing, background execution, session management, resume logic, and debugging.

Quick reference:
```
exec(command: "copilot --agent orchestrator -p \"<prompt>\" --yolo --model claude-opus-4.6 --no-auto-update", pty: true, workdir: "/home/dev/repos/quantipy")
```

Key rules (see skill for details):
- Always absolute paths for `workdir:` — tilde `~` is NOT expanded
- Never nest single quotes — use double quotes for `-p`
- `--agent orchestrator` by default, specialist only for narrow single-shot tasks
- `background: true` for ALL implementation sessions (>2 min)
- Never put `pty:true` or `workdir:` inside the command string — they are separate exec params

## Built-in Tools

| Tool | Use |
|------|----- |
| `exec` | Run shell commands, invoke Copilot CLI |
| `process` | Monitor background processes: `process action:log sessionId:<id>` |
| `Read` / `Write` | File operations (scoped to OpenClaw workspace only) |
| `Glob` / `Grep` | File search (scoped to OpenClaw workspace only) |
| `memory_search` | Search OpenClaw memory |
| `web_search` / `web_fetch` | Web research |
| `cron_create` | Schedule recurring or one-shot tasks |
| `cron_delete` | Remove a scheduled task by ID |
| `cron_list` | List all active cron jobs |


**Note:** Read/Write/Glob/Grep are scoped to the OpenClaw workspace, NOT to target repos. To access target repo files, use `exec` with `ls`, `cat`, `find`, etc.

**Critical:** NEVER use `exec` to create/modify/delete code files in target repos. Use `exec` ONLY to invoke Copilot CLI or to run read-only commands (ls, cat, git log, pytest, etc.).

## Memory (via MemPalace MCP)

| Tool | Use |
|------|-----|
| `mempalace_status` | Palace health + drawer overview |
| `mempalace_search` | Semantic search across stored experiment content |
| `mempalace_add_drawer` | Store verbatim experiment results (wing/room/content) |
| `mempalace_delete_drawer` | Remove a drawer by ID (data correction only) |
| `mempalace_kg_query` | Query entity relationships from knowledge graph |
| `mempalace_kg_add` | Add temporal fact (subject→predicate→object) |
| `mempalace_kg_invalidate` | Mark a fact as no longer true |
| `mempalace_kg_timeline` | Chronological story of an entity |
| `mempalace_kg_stats` | Knowledge graph overview |
| `mempalace_diary_write` | Record session summary for continuity |
| `mempalace_diary_read` | Browse past session notes |
| `mempalace_list_wings` | List all wings with drawer counts |
| `mempalace_list_rooms` | List rooms within a wing |
| `mempalace_check_duplicate` | Pre-write duplicate detection |

Read the `mempalace` skill for when/how to use each tool in the autoresearch loop. If mempalace tools error, continue without — it's additive, not blocking.

## Long-Running Tasks

See the `copilot-cli` skill for the full background execution protocol and task status conventions.

### Task Status Convention

| Event | Format |
|-------|--------|
| Launch | `[TASK:running] <description> \| started: <HH:MM UTC>` |
| Complete | `[TASK:complete] <description> \| duration: <Xm> \| result: <1-line>` |
| Incomplete | `[TASK:incomplete] <description> \| session: <uuid>` |
| Failure | `[TASK:failed] <description> \| error: <1-line reason>` |
| Timeout | `[TASK:timeout] <description> \| exceeded 2h TTL` |

These markers allow the gateway to detect task status on reconnect and display it on G2 glasses.
