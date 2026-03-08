# Copilot Bridge — Tool Usage Guide

You have ONE coding tool powered by GitHub Copilot:

| Tool | Purpose |
|------|---------|
| `copilot` | Execute any coding task — planning, implementation, review, fixing |

---

## Tool Parameters

- **`prompt`** (required) — The full task description. Include all context, constraints, file paths, and instructions. The better the prompt, the better the result.
- **`persona`** (optional) — Behavioral directives prepended to the prompt. Use to set the agent's role, constraints, and output format.
- **`workingDir`** (required) — Project name or absolute path. Bare names like `"my-api"` resolve to `~/repos/my-api`.
- **`timeout`** (optional) — Timeout in milliseconds (default 120000).

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

For complex features, use multiple `copilot` calls with different personas:

1. **Plan**: `copilot(prompt: "Plan feature X", persona: "You are a planner...")` 
2. **Implement**: `copilot(prompt: "Implement phase 1 of the plan: ...", persona: "You are an implementer...")`
3. **Review**: `copilot(prompt: "Review the changes in these files: ...", persona: "You are a reviewer...")`
4. **Fix**: `copilot(prompt: "Fix these issues: ...", persona: "You are a fixer...")`

You control the loop. Copilot handles each step. Adjust `.github/agents/` and `.github/copilot-instructions.md` in the target repo to shape Copilot's behavior — it loads these dynamically.

---

## Writing Effective Prompts

1. **Include specific file paths.** Don't say "the config file" — say `gateway/config.py`.
2. **Name the language or framework.** "Write a Python function using Pydantic v2" > "write a function".
3. **State constraints explicitly.** "Don't change the public API", "Use async/await".
4. **Reference existing patterns.** "Follow the error handling in `gateway/server.py`".
5. **Scope the work clearly.** One focused task per call works better than vague requests.
