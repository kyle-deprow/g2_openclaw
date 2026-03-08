# Tools

## copilot

`copilot(prompt, persona, workingDir, timeout)` — Delegate any coding task.

- **prompt**: Full task description with file paths, constraints, context.
- **persona**: System instructions for the session (planner, implementer, reviewer, fixer).
- **workingDir**: Project name or path. Determines which session is used.

Persona is set once at session creation. To switch: destroy session first.

## copilot_sessions

`copilot_sessions(action, project)` — Manage Copilot sessions.

- `action: "list"` — Show active sessions.
- `action: "destroy", project: "..."` — Destroy a session to switch persona or start fresh.

## Notes

- Skills in `.github/skills/` of the target repo are auto-discovered by Copilot.
- Detailed persona templates are available as MCP context — keep prompts focused on the task, not boilerplate.
