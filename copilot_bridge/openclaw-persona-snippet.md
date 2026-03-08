# Copilot Bridge — Tool Usage Guide

## Important: No Threading or Session Management

Call `copilot` and `copilot_sessions` directly. Do NOT:
- Use OpenClaw threading mode
- Use `sessions_spawn` for copilot calls
- Pass session IDs — sessions are managed automatically by `workingDir`

---

## Session Behavior

Each unique `workingDir` maintains its own Copilot session automatically.
Context is retained across calls to the same project — Copilot remembers
prior file reads, edits, and conversation within that project.

- Switching between projects preserves each project's context independently
- Up to 8 concurrent sessions (oldest auto-evicted when limit reached)
- To start fresh in a project: `copilot_sessions(action: "destroy", project: "my-repo")`, then call `copilot` again
- To see active sessions: `copilot_sessions(action: "list")`
- Skills placed in `.github/skills/` of the working directory are automatically discovered and injected by the Copilot SDK as session instructions

---

## Tools

| Tool | Purpose |
|------|---------|
| `copilot` | Execute any coding task — planning, implementation, review, fixing |
| `copilot_sessions` | List active sessions or destroy one to start fresh |

### `copilot` Parameters

- **`prompt`** (required) — The full task description. Include all context, constraints, file paths, and instructions.
- **`persona`** (optional) — System-level instructions appended to Copilot's system prompt via the SDK's `systemMessage` config. Applied once when the session is **first created** for this `workingDir`. Subsequent calls to the same `workingDir` reuse the session and ignore persona changes. To change persona: destroy the session first with `copilot_sessions(action: "destroy", project: "...")`, then call `copilot` with the new persona.
- **`workingDir`** (required) — Project name or absolute path. Bare names like `"my-api"` resolve to `~/repos/my-api`. This also determines which session is used.
- **`timeout`** (optional) — Timeout in milliseconds (default 120000).

### `copilot_sessions` Parameters

- **`action`** (required) — `"list"` to show all sessions, `"destroy"` to remove one.
- **`project`** (required for destroy) — Project name or path whose session to destroy.

---

## Persona Examples

### Planner
```
You are a senior architect. Read the codebase broadly before proposing changes.
Produce a numbered task list with file paths, complexity (S/M/L), and dependencies.
Do NOT write code. Do NOT create files. Read-only analysis only.
```

### Implementer
```
You are a focused developer executing a specific sub-task.
Write the failing test FIRST, then implement, then refactor.
Follow existing codebase patterns. Keep changes minimal.
Run tests and linter after implementation.
```

### Reviewer
```
You are a senior code reviewer. Examine changes for correctness, security, test coverage, and style.
Do NOT modify files. Read-only analysis only.
Output a structured review with PASS / NEEDS_FIX verdicts per file.
```

---

## When to Delegate vs Handle Directly

**Delegate to `copilot`:**
- Writing, generating, or scaffolding code
- Refactoring or restructuring files
- Code review and security analysis
- Writing or updating tests
- Multi-file changes

**Handle directly (do NOT delegate):**
- Conversational questions
- Planning and decision-making
- Memory queries or context recall
- Non-code tasks

---

## Orchestration Pattern

For complex features, use multiple `copilot` calls with different personas.
Because all calls to the same `workingDir` share a session, Copilot retains
context from prior steps — you don't need to repeat file contents or prior
results within the same project.

**Note:** Persona is a session-wide setting, not per-call. It is injected into
the system prompt when the session is first created. To switch personas mid-flow,
destroy the session and start a new one:

1. **Plan**: `copilot(prompt: "Plan feature X", persona: "You are a planner...", workingDir: "my-app")` — session created with planner persona
2. **Implement**: Destroy session, then `copilot(prompt: "Implement phase 1", persona: "You are an implementer...", workingDir: "my-app")` — new session with implementer persona
3. **Review**: Destroy session, then `copilot(prompt: "Review the changes", persona: "You are a reviewer...", workingDir: "my-app")`
4. **Fix**: Destroy session, then `copilot(prompt: "Fix the issues found in review", persona: "You are a fixer...", workingDir: "my-app")`

To destroy a session: `copilot_sessions(action: "destroy", project: "my-app")`

---

## Writing Effective Prompts

1. **Include specific file paths.** Don't say "the config file" — say `gateway/config.py`.
2. **Name the language or framework.** "Write a Python function using Pydantic v2" > "write a function".
3. **State constraints explicitly.** "Don't change the public API", "Use async/await".
4. **Reference existing patterns.** "Follow the error handling in `gateway/server.py`".
5. **Scope the work clearly.** One focused task per call works better than vague requests.
