### Runtime mechanics: claude-cli

Spawn with `mcp__openclaw__sessions_spawn`, then `mcp__openclaw__sessions_yield`.
Never use Claude's own `Agent`, `Workflow`, or `Task` tools: OpenClaw never
records their subagents. Tell every child in its task
text not to use them either.

- Implementer and runner: `agentId` `research-implementer` or `research-runner`,
  `model` and `thinking` from the roles above, `taskName`, `task`, and
  `cwd` set to the attempt checkout.
- Reviewer: spawn with the reservation's `spawn_arguments` exactly as returned
  (`agentId` `research-reviewer`, `cwd`, `model`, `thinking`, `taskName`, `task`
  ending in `Review label: <task_name>`). Never compose or edit them; `cwd` comes
  from the reservation, not the attempt checkout. It is read-only and
  must not write, repair, or spawn.

A research CLI call passes the guard only as ONE literal line:
`@GATEWAY_CLI@ research <subcommand> --root <active root> ...`, with the literal
root and arguments. Never use shell variables, `$(...)`, escaped
quotes, pipes, redirects, `;`/`&&` chains, env prefixes, or `cd ... &&`; anything
else mentioning the CLI is blocked. The runner may run only `research run`.

After a gateway restart `agent.wait` reports only `timeout`; the durable record is
`task_runs`, and `lost` is terminal evidence, never a reason to respawn.

Records: `task_runs`, `subagent_runs` and
per-agent `session_nodes`; pass `--openclaw-state-dir`. Model and effort proof is
the transcript's `api-request` records. Binding rests on the agent id, recorded
model and effort, the reservation label in the child task, and the strict
verdict.
