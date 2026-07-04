# Tools

## Primary: OpenClaw Codex Runtime

Delegate coding, research, and review work through OpenClaw's Codex runtime and
subagent mechanism. The repo-managed config enables the `codex` plugin and pins
the OpenAI provider runtime to `codex`.

Auth requirement for the host:

```bash
openclaw models auth login --provider openai
```

Model references use configured OpenAI IDs such as `openai/gpt-5.4` and
`openai/gpt-5-mini`.

Key rules:

- Use OpenClaw subagents for target-repo coding and review work.
- Prefer the `orchestrator` subagent for implementation and planning.
- Use `researcher`, `contrarian`, `explorer`, `theorist`, and `reviewer` for the
  autoresearch phases described in the autoresearch skill.
- Run long implementation and review tasks in the background.
- Do not silently fall back to another runtime, provider, or model if Codex auth
  or runtime selection fails.

## Built-in Tools

| Tool | Use |
|------|-----|
| `exec` | Run shell commands and read-only target-repo inspection commands |
| `process` | Monitor background tasks: `process action:log sessionId:<id>` |
| `Read` / `Write` | File operations scoped to the OpenClaw workspace |
| `Glob` / `Grep` | File search scoped to the OpenClaw workspace |
| `memory_search` | Search OpenClaw memory |
| `web_search` / `web_fetch` | Web research |
| `cron_create` | Schedule recurring or one-shot tasks |
| `cron_delete` | Remove a scheduled task by ID |
| `cron_list` | List all active cron jobs |

Read/Write/Glob/Grep are scoped to the OpenClaw workspace, not to target repos.
To inspect target repo files, use `exec` with read-only commands such as `ls`,
`cat`, `rg`, `git log`, and `pytest`.

Critical: never use `exec` to create, modify, or delete code files in target
repos. Implementation changes go through OpenClaw Codex subagents so the work
has a plan, verification, commits, and recoverable history.

## Memory (via MemPalace MCP)

| Tool | Use |
|------|-----|
| `mempalace_status` | Palace health and drawer overview |
| `mempalace_search` | Semantic search across stored experiment content |
| `mempalace_add_drawer` | Store verbatim experiment results |
| `mempalace_delete_drawer` | Remove a drawer by ID for data correction |
| `mempalace_kg_query` | Query entity relationships from the knowledge graph |
| `mempalace_kg_add` | Add temporal fact |
| `mempalace_kg_invalidate` | Mark a fact as no longer true |
| `mempalace_kg_timeline` | Chronological story of an entity |
| `mempalace_kg_stats` | Knowledge graph overview |
| `mempalace_diary_write` | Record session summary for continuity |
| `mempalace_diary_read` | Browse past session notes |
| `mempalace_list_wings` | List all wings with drawer counts |
| `mempalace_list_rooms` | List rooms within a wing |
| `mempalace_check_duplicate` | Pre-write duplicate detection |

Read the `mempalace` skill for when and how to use each tool in the autoresearch
loop. If MemPalace tools error, continue without them; they are additive, not
blocking.

## Long-Running Tasks

Use background execution for implementation and review tasks expected to run
longer than a couple of minutes. Always record the launch time and the target
repo HEAD before delegating implementation so completion can be evaluated.

### Task Status Convention

| Event | Format |
|-------|--------|
| Launch | `[TASK:running] <description> | started: <HH:MM UTC>` |
| Complete | `[TASK:complete] <description> | duration: <Xm> | result: <1-line>` |
| Incomplete | `[TASK:incomplete] <description> | session: <uuid>` |
| Failure | `[TASK:failed] <description> | error: <1-line reason>` |
| Timeout | `[TASK:timeout] <description> | exceeded 2h TTL` |

These markers allow the gateway to detect task status on reconnect and display
it on G2 glasses.
