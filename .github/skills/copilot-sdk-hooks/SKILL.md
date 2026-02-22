```skill
---
name: copilot-sdk-hooks
description:
  GitHub Copilot SDK hook system for intercepting tool execution, modifying
  prompts and results, enforcing permissions, handling errors, and managing
  session lifecycle. Use when implementing pre/post tool-use hooks, modifying
  user prompts, injecting session context, building audit logs, enforcing tool
  policies, or handling agent errors with retry/skip/abort. Triggers on tasks
  involving onPreToolUse, onPostToolUse, onUserPromptSubmitted, onSessionStart,
  onSessionEnd, onErrorOccurred, permissionDecision, or hook return values.
---

# Copilot SDK Hooks & Interception

Intercept, validate, modify, and audit every step of the Copilot agent's
execution through six hook points. The control layer for safety, compliance,
and customization.

## When to Apply

Reference these guidelines when:

- Implementing permission control for tool execution
- Modifying tool arguments before execution (pre-tool-use)
- Transforming tool results before they reach the LLM (post-tool-use)
- Modifying or filtering user prompts before processing
- Injecting additional context at session start
- Building audit logs for tool calls and conversations
- Implementing error handling with retry/skip/abort strategies
- Combining hooks for complex interception pipelines
- Debugging hook invocation order or return value semantics

## Rule Categories by Priority

| Priority | Category                | Impact   | Prefix      |
| -------- | ----------------------- | -------- | ----------- |
| 1        | Pre-Tool-Use            | CRITICAL | `pre-`      |
| 2        | Post-Tool-Use           | HIGH     | `post-`     |
| 3        | User Prompt             | HIGH     | `prompt-`   |
| 4        | Session Lifecycle       | HIGH     | `life-`     |
| 5        | Error Handling          | MEDIUM   | `err-`      |
| 6        | Hook Patterns           | MEDIUM   | `pattern-`  |

---

## 1. Pre-Tool-Use — Permission & Argument Control (CRITICAL)

### `pre-hook-signature`
The `onPreToolUse` hook fires before every tool execution:

**TypeScript:**
```typescript
const session = await client.createSession({
  hooks: {
    onPreToolUse: async (input, invocation) => {
      // input: { timestamp, cwd, toolName, toolArgs }
      // invocation: { sessionId }
      return {
        permissionDecision: "allow",    // "allow" | "deny" | "ask"
        permissionDecisionReason: "",   // Reason shown to user (deny/ask)
        modifiedArgs: undefined,        // Modified tool arguments
        additionalContext: undefined,   // Extra context for the LLM
        suppressOutput: false,          // Suppress output in transcript
      };
    },
  },
});
```

**Python:**
```python
async def on_pre_tool_use(input_data, invocation):
    # input_data: dict with timestamp, cwd, toolName, toolArgs
    # invocation: dict with sessionId
    return {"permissionDecision": "allow"}

session = await client.create_session({
    "hooks": {
        "on_pre_tool_use": on_pre_tool_use,
    }
})
```

### `pre-permission-decisions`
Three permission decisions:

| Decision | Effect | When to Use |
|----------|--------|-------------|
| `"allow"` | Tool executes normally | Trusted tools, validated args |
| `"deny"` | Tool blocked, reason sent to LLM | Dangerous operations, policy violations |
| `"ask"` | Prompt the user for approval | Uncertain or high-impact operations |

```typescript
onPreToolUse: async (input) => {
  const { toolName, toolArgs } = input;

  // Block destructive operations
  if (toolName === "exec" && toolArgs.command?.includes("rm -rf")) {
    return {
      permissionDecision: "deny",
      permissionDecisionReason: "Destructive shell commands are not allowed.",
    };
  }

  // Ask user for file writes outside workspace
  if (toolName === "write_file" && !toolArgs.path?.startsWith("./")) {
    return {
      permissionDecision: "ask",
      permissionDecisionReason: "File write outside workspace. Approve?",
    };
  }

  return { permissionDecision: "allow" };
},
```

### `pre-modify-args`
Modify tool arguments before execution:

```typescript
onPreToolUse: async (input) => {
  if (input.toolName === "search" && input.toolArgs.query) {
    return {
      permissionDecision: "allow",
      modifiedArgs: {
        ...input.toolArgs,
        query: `${input.toolArgs.query} site:docs.example.com`,  // Restrict search
        limit: Math.min(input.toolArgs.limit || 10, 50),         // Cap results
      },
    };
  }
  return { permissionDecision: "allow" };
},
```

### `pre-inject-context`
Add context that the LLM sees alongside the tool call:

```typescript
onPreToolUse: async (input) => {
  if (input.toolName === "write_file") {
    return {
      permissionDecision: "allow",
      additionalContext: "Remember: all files must include the copyright header.",
    };
  }
  return { permissionDecision: "allow" };
},
```

### `pre-null-means-default`
Returning `null`, `undefined`, or `None` means "no modification, allow by
default". This is the most permissive behavior:

```typescript
// These are equivalent:
onPreToolUse: async () => null,
onPreToolUse: async () => ({ permissionDecision: "allow" }),
```

---

## 2. Post-Tool-Use — Result Transformation (HIGH)

### `post-hook-signature`
The `onPostToolUse` hook fires after every tool execution:

**TypeScript:**
```typescript
hooks: {
  onPostToolUse: async (input, invocation) => {
    // input: { timestamp, cwd, toolName, toolArgs, toolResult }
    // invocation: { sessionId }
    return {
      modifiedResult: undefined,       // Replace the tool result
      additionalContext: undefined,    // Extra context for the LLM
      suppressOutput: false,           // Suppress output in transcript
    };
  },
}
```

**Python:**
```python
async def on_post_tool_use(input_data, invocation):
    return {
        "modifiedResult": None,
        "additionalContext": None,
        "suppressOutput": False,
    }
```

### `post-modify-result`
Transform or filter tool results before the LLM sees them:

```typescript
onPostToolUse: async (input) => {
  // Redact sensitive data from file reads
  if (input.toolName === "read_file" && input.toolResult) {
    const sanitized = input.toolResult
      .replace(/password\s*=\s*"[^"]*"/g, 'password="[REDACTED]"')
      .replace(/api_key\s*=\s*"[^"]*"/g, 'api_key="[REDACTED]"');
    return { modifiedResult: sanitized };
  }

  // Truncate large tool outputs
  if (input.toolResult && input.toolResult.length > 10000) {
    return {
      modifiedResult: input.toolResult.slice(0, 10000) + "\n... [truncated]",
      additionalContext: `Original result was ${input.toolResult.length} chars.`,
    };
  }

  return null;  // No modification
},
```

### `post-audit-logging`
Log all tool calls for compliance:

```typescript
onPostToolUse: async (input) => {
  await auditLog.write({
    timestamp: input.timestamp,
    tool: input.toolName,
    args: input.toolArgs,
    resultLength: input.toolResult?.length ?? 0,
    cwd: input.cwd,
  });
  return null;  // Don't modify the result
},
```

### `post-suppress-output`
Hide tool output from the session transcript:

```typescript
onPostToolUse: async (input) => {
  if (input.toolName === "internal_metrics") {
    return { suppressOutput: true };  // Tool ran, but output hidden
  }
  return null;
},
```

---

## 3. User Prompt — Input Modification (HIGH)

### `prompt-hook-signature`
The `onUserPromptSubmitted` hook fires when the user sends a message:

**TypeScript:**
```typescript
hooks: {
  onUserPromptSubmitted: async (input, invocation) => {
    // input: { timestamp, cwd, prompt }
    // invocation: { sessionId }
    return {
      modifiedPrompt: undefined,       // Replace the user's prompt
      additionalContext: undefined,    // Extra context injected
      suppressOutput: false,           // Suppress in transcript
    };
  },
}
```

### `prompt-modify`
Transform the user's prompt before the agent processes it:

```typescript
onUserPromptSubmitted: async (input) => {
  let prompt = input.prompt;

  // Add project context to every prompt
  return {
    modifiedPrompt: prompt,
    additionalContext: `Current project: SpineSense backend (Python 3.12, FastAPI).
Working directory: ${input.cwd}`,
  };
},
```

### `prompt-filter`
Filter or sanitize user input:

```typescript
onUserPromptSubmitted: async (input) => {
  // Remove accidental credentials from prompts
  const cleaned = input.prompt
    .replace(/(?:sk-|ghp_|gho_)[a-zA-Z0-9]{20,}/g, "[CREDENTIAL_REMOVED]");

  if (cleaned !== input.prompt) {
    return {
      modifiedPrompt: cleaned,
      additionalContext: "Note: credentials were detected and removed from the prompt.",
    };
  }
  return null;
},
```

---

## 4. Session Lifecycle Hooks (HIGH)

### `life-session-start`
Fires when a session begins. Inject initial context:

**TypeScript:**
```typescript
hooks: {
  onSessionStart: async (input, invocation) => {
    // input: { timestamp, cwd, source, initialPrompt? }
    // source: how the session was initiated
    return {
      additionalContext: "User prefers TypeScript. Use strict mode always.",
      modifiedConfig: undefined,  // Modify session config
    };
  },
}
```

**Python:**
```python
async def on_session_start(input_data, invocation):
    return {
        "additionalContext": "User prefers TypeScript. Use strict mode always."
    }
```

Use `onSessionStart` to:
- Inject user preferences
- Load project-specific context
- Set up logging infrastructure
- Initialize external connections

### `life-session-end`
Fires when a session ends. Clean up and summarize:

```typescript
hooks: {
  onSessionEnd: async (input, invocation) => {
    // input: { timestamp, cwd, reason, finalMessage?, error? }
    // reason: why the session ended

    await analytics.track("session_ended", {
      reason: input.reason,
      hadError: !!input.error,
    });

    return {
      suppressOutput: false,
      cleanupActions: ["flush_logs", "close_connections"],
      sessionSummary: "Session completed successfully.",
    };
  },
}
```

### `life-session-end-reasons`
The `reason` field tells you why the session ended:

| Reason | Meaning |
|--------|---------|
| `"user_requested"` | User ended the session |
| `"timeout"` | Session timed out |
| `"error"` | Fatal error occurred |
| `"destroyed"` | `session.destroy()` called |
| `"compaction"` | Session compacted |

---

## 5. Error Handling Hook (MEDIUM)

### `err-hook-signature`
Fires when errors occur during the session:

**TypeScript:**
```typescript
hooks: {
  onErrorOccurred: async (input, invocation) => {
    // input: { timestamp, cwd, error, errorContext, recoverable }
    return {
      suppressOutput: false,
      errorHandling: "retry",     // "retry" | "skip" | "abort"
      retryCount: 3,              // For "retry" strategy
      userNotification: "An error occurred, retrying...",
    };
  },
}
```

### `err-handling-strategies`
Three error handling strategies:

| Strategy | Behavior | Use When |
|----------|----------|----------|
| `"retry"` | Retry the operation (up to `retryCount` times) | Transient failures (network, rate limits) |
| `"skip"` | Skip the failed operation, continue | Non-critical tool failures |
| `"abort"` | Stop the entire session run | Fatal or unrecoverable errors |

```typescript
onErrorOccurred: async (input) => {
  if (input.error.includes("rate limit")) {
    return { errorHandling: "retry", retryCount: 3 };
  }
  if (input.error.includes("file not found")) {
    return { errorHandling: "skip" };
  }
  if (!input.recoverable) {
    return { errorHandling: "abort", userNotification: "Fatal error. Stopping." };
  }
  return null;  // Default handling
},
```

### `err-recoverable-flag`
The `recoverable` boolean in the input signals whether the error is inherently
retryable. Use it as a guide:

```typescript
onErrorOccurred: async (input) => {
  if (input.recoverable) {
    return { errorHandling: "retry", retryCount: 2 };
  }
  return { errorHandling: "abort" };
},
```

---

## 6. Hook Patterns (MEDIUM)

### `pattern-hook-invocation-order`
Hooks fire in this order during a typical agent turn:

```
1. onUserPromptSubmitted     (user sends message)
2. onPreToolUse              (for each tool call)
3.   [tool executes]
4. onPostToolUse             (for each tool call)
5.   [repeat 2-4 for each tool in the turn]
6. onSessionStart            (at session creation/resume)
7. onSessionEnd              (at session destruction)
8. onErrorOccurred           (whenever an error happens)
```

`onSessionStart` fires once at the beginning. Tool hooks fire per tool call.
Multiple tools per turn means multiple pre/post hook invocations.

### `pattern-combined-pre-post`
Use pre + post together for complete tool governance:

```typescript
hooks: {
  onPreToolUse: async (input) => {
    const start = Date.now();
    // Store start time for cost tracking
    return {
      permissionDecision: "allow",
      additionalContext: `Tool call started at ${start}`,
    };
  },
  onPostToolUse: async (input) => {
    await costTracker.record(input.toolName, input.toolResult?.length ?? 0);
    return null;
  },
},
```

### `pattern-audit-pipeline`
Complete audit logging across all hooks:

```typescript
const auditLog = [];

hooks: {
  onUserPromptSubmitted: async (input) => {
    auditLog.push({ type: "prompt", prompt: input.prompt, ts: input.timestamp });
    return null;
  },
  onPreToolUse: async (input) => {
    auditLog.push({
      type: "tool_start",
      tool: input.toolName,
      args: input.toolArgs,
      ts: input.timestamp,
    });
    return { permissionDecision: "allow" };
  },
  onPostToolUse: async (input) => {
    auditLog.push({
      type: "tool_end",
      tool: input.toolName,
      resultSize: input.toolResult?.length ?? 0,
      ts: input.timestamp,
    });
    return null;
  },
  onSessionEnd: async (input) => {
    await writeAuditLog(auditLog);
    return null;
  },
},
```

### `pattern-policy-enforcement`
Enforce organizational policies through hooks:

```typescript
const ALLOWED_TOOLS = new Set(["read_file", "search", "write_file"]);
const BLOCKED_PATHS = ["/etc/", "/var/", "/root/", "/proc/"];

hooks: {
  onPreToolUse: async (input) => {
    // Tool allowlist
    if (!ALLOWED_TOOLS.has(input.toolName)) {
      return {
        permissionDecision: "deny",
        permissionDecisionReason: `Tool ${input.toolName} is not in the approved list.`,
      };
    }

    // Path restriction
    const path = input.toolArgs.path || input.toolArgs.file || "";
    if (BLOCKED_PATHS.some(p => path.startsWith(p))) {
      return {
        permissionDecision: "deny",
        permissionDecisionReason: `Access to ${path} is restricted by policy.`,
      };
    }

    return { permissionDecision: "allow" };
  },
},
```

### `pattern-context-enrichment`
Inject dynamic context at every prompt:

```typescript
hooks: {
  onUserPromptSubmitted: async (input) => {
    const gitBranch = await exec("git branch --show-current");
    const recentFiles = await exec("git diff --name-only HEAD~3");

    return {
      additionalContext: `Current branch: ${gitBranch}
Recently changed files:\n${recentFiles}`,
    };
  },
  onSessionStart: async () => {
    const projectInfo = await fs.readFile("package.json", "utf8");
    return {
      additionalContext: `Project config:\n${projectInfo}`,
    };
  },
},
```

### `pattern-hook-error-isolation`
Hook errors are caught silently and return `undefined`. This means:
- A crashing hook **does not** stop tool execution
- The agent continues as if the hook returned `null`
- Always add try/catch for your own error reporting

```typescript
onPreToolUse: async (input) => {
  try {
    await auditService.log(input);
    return validateToolCall(input);
  } catch (error) {
    // Log the error — the SDK won't surface it
    console.error("Hook error:", error);
    // Return a safe default
    return { permissionDecision: "allow" };
  }
},
```

---

## Anti-Patterns

| Anti-Pattern | Why It's Wrong | Correct Approach |
|---|---|---|
| No pre-tool-use hook with tools | All tool calls denied by default | Always implement permission handling |
| Throwing errors in hooks | Silently swallowed, no feedback | Use try/catch, return explicit decisions |
| Heavy async work in hooks | Blocks the agent pipeline | Keep hooks fast; queue heavy work |
| Modifying result to empty string | LLM has no context about the tool call | Return meaningful summaries, even for filtered results |
| `permissionDecision: "ask"` for everything | User fatigue, defeats automation | Ask only for high-impact/uncertain operations |
| No audit in post-tool-use | No compliance trail | Log every tool execution for debugging at minimum |
| Hardcoding policies in hooks | Inflexible, hard to update | Load policies from config files |
| `suppressOutput: true` on all hooks | Can't debug behavior | Suppress selectively for sensitive operations only |
| Ignoring `recoverable` flag | Retrying fatal errors wastes resources | Check `recoverable` before choosing retry |
| Not re-attaching hooks on resume | Hooks aren't persisted across sessions | Re-provide hooks in `resumeSession()` config |

## References

- Hooks Overview: `.archive/copilot-sdk/docs/hooks/overview.md`
- Pre-Tool-Use: `.archive/copilot-sdk/docs/hooks/pre-tool-use.md`
- Post-Tool-Use: `.archive/copilot-sdk/docs/hooks/post-tool-use.md`
- User Prompt Submitted: `.archive/copilot-sdk/docs/hooks/user-prompt-submitted.md`
- Session Lifecycle: `.archive/copilot-sdk/docs/hooks/session-lifecycle.md`
- Error Handling: `.archive/copilot-sdk/docs/hooks/error-handling.md`
```
