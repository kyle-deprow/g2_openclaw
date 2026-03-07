# Copilot Bridge — Tool Usage Guide

You have access to coding tools powered by GitHub Copilot, organised around **persistent sessions**:

| Tool | Purpose |
|------|---------|
| `copilot_code_start` | Start a new coding session (requires `task` + `workingDir`) |
| `copilot_code_message` | Send a follow-up message into an existing session |
| `copilot_code_verbose` | Same as message, but includes a step-by-step execution log |
| `copilot_code_transcript` | Retrieve the recent transcript of a session |
| `copilot_list_sessions` | List all active sessions |
| `copilot_destroy_session` | End a session and free resources |

---

## Session Workflow

1. **Always start with `copilot_code_start`** — pass the project name as `workingDir`.
2. Bare names like `"my-api"` resolve to `~/repos/my-api` automatically.
3. Absolute paths like `/home/dev/repos/g2_openclaw` are used as-is.
4. The directory is created automatically if it doesn't exist.
5. After starting, use `copilot_code_message` for follow-ups — no need to pass `workingDir` again.

```
# Start a session
copilot_code_start(task: "Set up a new Express API", workingDir: "my-api")

# Follow up in the same session
copilot_code_message(task: "Add a /health endpoint")
copilot_code_message(task: "Write unit tests for the health endpoint")
```

---

## When to Delegate vs Handle Directly

**Delegate to Copilot tools:**

- Writing, generating, or scaffolding code
- Refactoring or restructuring existing files
- Editing files in-place (renaming symbols, extracting functions, etc.)
- Debugging — reading code, tracing logic, proposing fixes
- Code analysis — reviewing for bugs, security issues, or style
- Writing or updating tests
- Applying patterns across multiple files

**Handle directly (do NOT delegate):**

- Conversational questions ("What does this project do?")
- Planning, brainstorming, or decision-making
- Memory queries or context recall
- Non-code tasks (drafting emails, summarising docs)
- Simple explanations of a concept or term
- File listing, searching, or navigation without edits

> **Rule of thumb:** If the task requires reading or writing source files, delegate it. If it's purely conversational, handle it yourself.

---

## Choosing Between Tools

### `copilot_code_start` → `copilot_code_message` — Session-Based Flow

Use for all coding work. Start a session, then iterate:

- Write a function, class, or module
- Fix a known bug
- Add a missing import or type annotation
- Generate a unit test for a specific function

### `copilot_code_verbose` — Detailed & Diagnostic

Use when you need visibility into *how* Copilot solved the task:

- Multi-file refactoring (to confirm which files were touched)
- Debugging investigations (to see which tools Copilot invoked)
- Complex tasks where the first attempt might miss something
- When you plan to explain the steps back to the user

---

## Writing Effective Task Descriptions

The quality of the `task` parameter directly determines the quality of the result. Follow these guidelines:

1. **Include specific file paths.** Don't say "the config file" — say `gateway/config.py`.
2. **Name the language or framework.** "Write a Python function using Pydantic v2" is better than "write a function".
3. **State constraints explicitly.** E.g., "Don't change the public API", "Keep backward compatibility", "Use async/await".
4. **Reference existing patterns.** "Follow the same error-handling pattern used in `gateway/server.py`".
5. **Scope the work clearly.** "Add input validation to the `create_patient` endpoint" beats "improve the API".

---

## Example Prompts

### 1. Start a new project session

```
copilot_code_start(
    task: "Set up a TypeScript Express API with health check, error middleware, and tests",
    workingDir: "my-api"
)
```

### 2. Follow up — fix a bug

```
copilot_code_message(task: "The /health endpoint returns 500 when the DB is down. Add a try-catch and return 503 with a diagnostic payload.")
```

### 3. Follow up — add tests

```
copilot_code_message(task: "Write pytest unit tests for the `validate_token` function in gateway/server.py. Cover: valid token, expired token, malformed token, and missing token.")
```

### 4. Verbose — multi-file refactoring

```
copilot_code_verbose(task: "Rename the `CopilotBridge` class to `CopilotClient` across all files in copilot_bridge/. Update imports, type references, and test files. Don't change the public plugin API.")
```

### 5. Work on an existing repo (absolute path)

```
copilot_code_start(
    task: "Review gateway/protocol.py for potential security issues: injection risks, missing input validation, and unsafe deserialization.",
    workingDir: "/home/dev/repos/g2_openclaw"
)
```

---

## Error Recovery

| Symptom | Action |
|---------|--------|
| Result contains "Error" | Re-read the error message. Rephrase the task with more specifics or fix the prerequisite (e.g., missing file). |
| Task times out | Break the task into smaller, independent sub-tasks and run them sequentially. |
| Result seems wrong or incomplete | Re-run with `copilot_code_verbose` to inspect the execution log. Check if the task description was ambiguous. |
| "Not connected" or auth error | The Copilot SDK session may have expired. Ask the user to restart the gateway (`openclaw gateway restart`). |

---

## Tips

- Set `workingDir` to the project name when starting a session — Copilot resolves relative file paths from there.
- For long-running tasks (large refactors, full test suites), increase `timeout` beyond the 120 000 ms default.
- When in doubt, prefer `copilot_code_verbose` — the execution log costs little and helps you verify correctness.
- Use `copilot_list_sessions` to see what sessions are active and `copilot_destroy_session` to clean up.
