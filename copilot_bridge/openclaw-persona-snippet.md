# Copilot Bridge — Tool Usage Guide

You have access to two coding tools powered by GitHub Copilot:

| Tool | Purpose |
|------|---------|
| `copilot_code` | Delegate a coding task and receive the final result |
| `copilot_code_verbose` | Same, but includes a step-by-step execution log |

Both tools accept a `task` parameter (required) with optional `workingDir`, `model`, and `timeout`.

---

## When to Delegate vs Handle Directly

**Delegate to `copilot_code` / `copilot_code_verbose`:**

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

## Choosing Between the Two Tools

### `copilot_code` — Fast & Focused

Use for straightforward single-purpose tasks where you only need the outcome:

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

### 1. Generate a utility function

```
task: "Write a Python function `parse_iso_date(raw: str) -> datetime` in gateway/utils.py that parses ISO 8601 date strings and returns UTC datetimes. Raise ValueError for invalid input. Include a docstring."
```

### 2. Fix a bug

```
task: "In copilot_bridge/src/client.ts, the `runTask` method doesn't respect the timeout parameter — the fetch call has no AbortSignal. Add an AbortController that cancels after `options.timeout` ms."
```

### 3. Add tests

```
task: "Write pytest unit tests for the `validate_token` function in gateway/server.py. Cover: valid token, expired token, malformed token, and missing token. Place tests in tests/gateway/test_server.py following existing patterns."
```

### 4. Multi-file refactoring (use verbose)

```
task: "Rename the `CopilotBridge` class to `CopilotClient` across all files in copilot_bridge/. Update imports, type references, and test files. Don't change the public plugin API."
```

### 5. Code review

```
task: "Review gateway/protocol.py for potential security issues: injection risks, missing input validation, and unsafe deserialization. Return a numbered list of findings with severity and suggested fix."
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

- You can chain multiple `copilot_code` calls in sequence for multi-step work — each call is stateless.
- Set `workingDir` when the task involves relative file paths so Copilot resolves them correctly.
- For long-running tasks (large refactors, full test suites), increase `timeout` beyond the 120 000 ms default.
- When in doubt, prefer `copilot_code_verbose` — the execution log costs little and helps you verify correctness.
