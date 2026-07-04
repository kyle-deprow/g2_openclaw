# Tools

## Primary: OpenClaw Codex Runtime

Delegate coding, research, and review work through OpenClaw's Codex runtime and
subagent mechanism. The repo-managed config enables the `codex` plugin and pins
the OpenAI provider runtime to `codex`.

Auth requirement for the host:

```bash
openclaw models auth login --provider openai
```

Model references use configured OpenAI IDs such as `openai/gpt-5.4`,
`openai/gpt-5.5`, and `openai/gpt-5-mini`.

Key rules:

- Use OpenClaw subagents for target-repo coding and review work.
- The `main` agent is the autoresearch PM and loop controller on
  `openai/gpt-5.5` high.
- Use `context-curator`, the five `debater-*` agents, `consensus-arbiter`,
  `implementer`, `reviewer`, and `fixer` for the autoresearch stages described
  in the autoresearch skill.
- Spawn configured stage agents by ID. Do not spawn generic/default agents for
  autoresearch stages, and do not pass ad hoc model overrides.
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

OpenClaw built-in memory tools (`memory_search`, `memory_get`) are denied by
policy. Use MemPalace read tools for research context. Only the PM agent loads
the write-capable `mempalace` skill and MemPalace write tools; non-PM agents
load `mempalace-readonly`.

## Memory (via MemPalace MCP)

| Tool | Use |
|------|-----|
| `mempalace_status` | Palace health and drawer overview |
| `mempalace_search` | Semantic search across stored experiment content |
| `mempalace_add_drawer` | PM-only final experiment logging |
| `mempalace_delete_drawer` | PM-only correction of completed records |
| `mempalace_kg_query` | Query entity relationships from the knowledge graph |
| `mempalace_kg_add` | PM-only final experiment fact logging |
| `mempalace_kg_invalidate` | PM-only correction of completed records |
| `mempalace_kg_timeline` | Chronological story of an entity |
| `mempalace_kg_stats` | Knowledge graph overview |
| `mempalace_diary_write` | PM-only summary after completed experiment decisions |
| `mempalace_diary_read` | Browse past session notes |
| `mempalace_list_wings` | List all wings with drawer counts |
| `mempalace_list_rooms` | List rooms within a wing |
| `mempalace_check_duplicate` | PM-only pre-write duplicate detection |

Read `mempalace` only when acting as the PM. Read `mempalace-readonly` when
acting as any context, debate, implementation, review, or fix stage agent. If
MemPalace tools error, stop the loop and report the blocker. Do not continue
with hidden or unstructured state.

Non-PM stage agents both deny every MemPalace mutation/operation tool in config
and avoid the write-capable `mempalace` skill. They may read context, but they
cannot write or alter drawers, KG facts, diaries, tunnels, hallways, hook
settings, mined content, checkpoints, sync state, or source-linked records.

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
