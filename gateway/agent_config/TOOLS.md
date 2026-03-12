# Tools

## ⚠️ MANDATORY: Use `copilot` for ALL coding tasks

**NEVER use exec to run codex, claude-code, or any other coding agent CLI.**
**NEVER build apps manually with exec + write when a coding task is involved.**
**NEVER read a built-in coding-agent skill — it does not apply to this setup.**

For any task that involves writing code, creating projects, building features, or modifying codebases:
→ Use the `copilot` tool. Always. No exceptions.

The `copilot` tool delegates to GitHub Copilot which handles planning, implementation, review, and fixes autonomously. You do NOT need codex, claude-code, or any other CLI coding tool. They are not installed and must not be used.

**Workflow: exec for setup → copilot for all coding → done.**

---

## exec

Run shell commands. Use ONLY for workspace setup (mkdir, git init, cp scaffolding) before delegating to `copilot`. Do NOT use exec to write code, run codex, or run claude-code.

Common uses:
- `mkdir -p ~/repos/<project>` — create project directories
- `git init` — initialize repos
- `cp -r <source> <dest>` — copy scaffolding files
- `ls`, `cat` — inspect files when Read/Glob aren't sufficient

## copilot

`copilot(prompt, workingDir, timeout)` — Delegate coding tasks.

- **prompt**: The FULL task prompt. Include the user's requirements VERBATIM — every tech, API, path, constraint. Never paraphrase.
- **workingDir**: The user's specified project path (e.g., `~/repos/weather`). Use their EXACT path.
- **timeout**: Default 900000ms (15 min). Pass 0 for no timeout on multi-phase builds.

**Session behavior:**
- One session per workingDir. Sessions persist across calls — do NOT destroy between plan and implement.
- `.github/agents/` and `.github/skills/` are auto-discovered at session creation.
- The orchestrator delegates to specialist agents internally.

## copilot_sessions

`copilot_sessions(action, project)` — Manage sessions.

- `action: "list"` — Show active sessions.
- `action: "destroy", project: "..."` — Only use when truly starting over.

## Scaffolding Reference

Source: `~/repos/ai_scaffolding/`

| Source Path | Target Path |
|-------------|-------------|
| `ai_scaffolding/agents/<name>.agent.md` | `<project>/.github/agents/<name>.agent.md` |
| `ai_scaffolding/skills/<name>/` | `<project>/.github/skills/<name>/` |

Always scaffold `orchestrator.agent.md`. Select others by project type:
- React project → `react-best-practices` agent + skill
- Python backend → `backend-python` agent + skill
- Component-heavy → `composition-patterns` agent + skill
