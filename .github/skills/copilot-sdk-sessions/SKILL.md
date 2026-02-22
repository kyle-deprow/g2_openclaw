```skill
---
name: copilot-sdk-sessions
description:
  GitHub Copilot SDK session lifecycle, event system, streaming, persistence,
  and configuration. Use when creating or resuming sessions, handling session
  events, configuring streaming responses, managing session persistence and
  resume, setting agent modes, working with plans, or debugging session state
  issues. Triggers on tasks involving createSession, resumeSession, sendAndWait,
  session events, streaming deltas, session persistence, infinite sessions,
  compaction, or workspace file operations.
---

# Copilot SDK Sessions & Events

Create, configure, stream, persist, and resume Copilot sessions. Master the
session lifecycle, the 40+ event types, and the streaming pipeline.

## When to Apply

Reference these guidelines when:

- Creating sessions with model, tools, system message, and config options
- Sending prompts and handling responses (blocking and streaming)
- Subscribing to and filtering session events
- Implementing streaming UIs with delta events
- Persisting sessions and resuming across restarts
- Configuring infinite sessions and compaction thresholds
- Working with agent modes (interactive, plan, autopilot)
- Managing plans, workspace files, and session artifacts
- Aborting in-flight requests or switching models mid-session
- Debugging session errors, truncation, or idle detection

## Rule Categories by Priority

| Priority | Category              | Impact   | Prefix      |
| -------- | --------------------- | -------- | ----------- |
| 1        | Session Lifecycle     | CRITICAL | `sess-`     |
| 2        | Event System          | CRITICAL | `event-`    |
| 3        | Streaming             | HIGH     | `stream-`   |
| 4        | Persistence & Resume  | HIGH     | `persist-`  |
| 5        | Advanced Controls     | MEDIUM   | `ctrl-`     |

---

## 1. Session Lifecycle (CRITICAL)

### `sess-create-minimal`
Simplest session — 3 lines:

**TypeScript:**
```typescript
const session = await client.createSession({ model: "gpt-4.1" });
const response = await session.sendAndWait({ prompt: "Hello" });
console.log(response?.data.content);
```

**Python:**
```python
session = await client.create_session({"model": "gpt-4.1"})
response = await session.send_and_wait({"prompt": "Hello"})
print(response.data.content)
```

### `sess-create-full-config`
Complete `SessionConfig` — all available options:

| Field (TS) | Field (Python) | Type | Default | Description |
|------------|----------------|------|---------|-------------|
| `sessionId` | `session_id` | `string` | random UUID | Custom ID for resumability |
| `clientName` | `client_name` | `string` | — | App identifier for User-Agent |
| `model` | `model` | `string` | — | Model ID (e.g. `"gpt-4.1"`) |
| `reasoningEffort` | `reasoning_effort` | `"low"\|"medium"\|"high"\|"xhigh"` | — | Reasoning depth |
| `streaming` | `streaming` | `boolean` | `false` | Enable delta events |
| `systemMessage` | `system_message` | `SystemMessageConfig` | — | System prompt (see below) |
| `tools` | `tools` | `Tool[]` | `[]` | Custom tool definitions |
| `availableTools` | `available_tools` | `string[]` | all | Allowlist of tool names |
| `excludedTools` | `excluded_tools` | `string[]` | `[]` | Blocklist of tool names |
| `mcpServers` | `mcp_servers` | `Record<string, MCPConfig>` | — | MCP server configs |
| `skillDirectories` | `skill_directories` | `string[]` | `[]` | Skill directories to load |
| `disabledSkills` | `disabled_skills` | `string[]` | `[]` | Skills to deactivate |
| `customAgents` | `custom_agents` | `CustomAgentConfig[]` | — | Custom sub-agents |
| `provider` | `provider` | `ProviderConfig` | — | BYOK config (see client skill) |
| `hooks` | `hooks` | `SessionHooks` | — | Hook handlers (see hooks skill) |
| `workingDirectory` | `working_directory` | `string` | cwd | Working dir for tools |
| `configDir` | `config_dir` | `string` | — | Override config directory |
| `infiniteSessions` | `infinite_sessions` | `InfiniteSessionConfig` | — | Auto-compaction |
| `onPermissionRequest` | `on_permission_request` | `(req) => decision` | — | Permission callback |
| `onUserInputRequest` | `on_user_input_request` | `(req) => response` | — | ask_user callback |

### `sess-system-message`
Inject custom instructions via system message:

```typescript
// Append to Copilot's default system prompt
const session = await client.createSession({
  systemMessage: {
    type: "append",
    content: "Always respond in Spanish. Use formal language.",
  },
});

// Replace the entire system prompt (use carefully)
const session = await client.createSession({
  systemMessage: {
    type: "replace",
    content: "You are a code review assistant. Only discuss code.",
  },
});
```

Prefer `"append"` — it preserves Copilot's built-in capabilities. Use
`"replace"` only when you need complete control over agent behavior.

### `sess-send-patterns`
Two sending modes:

```typescript
// Fire and wait — blocks until response or idle
const response = await session.sendAndWait(
  { prompt: "Analyze this code" },
  60000  // optional timeout in ms
);
// response is AssistantMessageEvent or undefined (on timeout/abort)

// Fire and stream — returns immediately, handle events
const messageId = await session.send({ prompt: "Write a function" });
// Listen for events via session.on(...)
```

`sendAndWait` resolves when `session.idle` fires. Use it for simple request-
response. Use `send()` + events for interactive streaming UIs.

### `sess-destroy-vs-delete`
Two levels of session cleanup:

| Method | Effect | Session Resumable? |
|--------|--------|--------------------|
| `session.destroy()` | Releases memory, CLI state freed | Yes — state on disk |
| `client.deleteSession(id)` | Removes from disk permanently | No |

Always `destroy()` when done with a session. Only `deleteSession()` when you
want to permanently remove all session history.

### `sess-abort`
Cancel in-flight processing:

```typescript
await session.abort();
// Fires "abort" event, then "session.idle"
```

Use to interrupt long-running agent tasks. The session remains usable after abort.

### `sess-get-messages`
Retrieve full conversation history:

```typescript
const messages = await session.getMessages();
// Returns SessionEvent[] — full history including tool calls
```

---

## 2. Event System (CRITICAL)

### `event-subscription`
Subscribe to session events:

**TypeScript:**
```typescript
// All events (untyped)
const unsub = session.on((event) => {
  console.log(event.type, event.data);
});

// Specific event type (typed)
const unsub = session.on("assistant.message", (event) => {
  console.log(event.data.content);  // TypeScript knows the shape
});

// Unsubscribe
unsub();
```

**Python:**
```python
from copilot.generated.session_events import SessionEventType

def handler(event):
    if event.type == SessionEventType.ASSISTANT_MESSAGE:
        print(event.data.content)

unsub = session.on(handler)

# Unsubscribe
unsub()
```

### `event-base-shape`
All session events share this structure:

```typescript
{
  id: string;           // Unique event ID
  timestamp: string;    // ISO timestamp
  parentId: string | null;  // Parent event for threading
  ephemeral?: boolean;  // If true, not persisted in transcript
  type: SessionEventType;
  data: { ... };        // Type-specific payload
}
```

### `event-types-reference`
Complete event type catalog:

**Session lifecycle events:**

| Event | Ephemeral | Payload | Use For |
|-------|-----------|---------|---------|
| `session.start` | No | `{}` | Session initialized |
| `session.resume` | No | `{}` | Session resumed from storage |
| `session.idle` | Yes | `{}` | All processing complete |
| `session.error` | No | `{errorType, message, stack?, statusCode?}` | Error occurred |
| `session.shutdown` | Yes | `{metrics}` | Session shutting down |
| `session.title_changed` | Yes | `{title}` | Title auto-generated |
| `session.info` | No | `{message}` | Informational message |
| `session.warning` | No | `{message}` | Warning message |
| `session.model_change` | No | `{model}` | Model switched |
| `session.mode_changed` | No | `{mode}` | Agent mode changed |
| `session.truncation` | No | `{...}` | Context truncated |
| `session.context_changed` | No | `{...}` | Working dir changed |
| `session.usage_info` | Yes | `{...}` | Token usage snapshot |
| `session.compaction_start` | No | `{}` | Compaction beginning |
| `session.compaction_complete` | No | `{}` | Compaction finished |
| `session.plan_changed` | No | `{action}` | Plan created/updated/deleted |
| `session.workspace_file_changed` | No | `{...}` | Workspace file modified |
| `session.handoff` | No | `{...}` | Remote session handoff |
| `session.snapshot_rewind` | Yes | `{...}` | Snapshot rewind |

**Assistant events:**

| Event | Ephemeral | Payload | Use For |
|-------|-----------|---------|---------|
| `assistant.turn_start` | No | `{}` | Agent turn begins |
| `assistant.intent` | Yes | `{intent}` | Agent declared intent |
| `assistant.reasoning` | No | `{content}` | Full reasoning block |
| `assistant.reasoning_delta` | Yes | `{reasoningId, deltaContent}` | Streaming reasoning chunk |
| `assistant.message` | No | `{content, messageId}` | Final complete message |
| `assistant.message_delta` | Yes | `{messageId, deltaContent, totalResponseSizeBytes?}` | Streaming message chunk |
| `assistant.turn_end` | No | `{}` | Agent turn ends |
| `assistant.usage` | Yes | `{...}` | Token usage for this call |

**Tool events:**

| Event | Ephemeral | Payload | Use For |
|-------|-----------|---------|---------|
| `tool.user_requested` | No | `{toolName}` | User explicitly asked for tool |
| `tool.execution_start` | No | `{toolCallId, toolName, args}` | Tool invocation begins |
| `tool.execution_partial_result` | Yes | `{toolCallId, partialResult}` | Incremental tool output |
| `tool.execution_progress` | Yes | `{toolCallId, message}` | Progress message |
| `tool.execution_complete` | No | `{toolCallId, result}` | Tool finished |

**Other events:**

| Event | Ephemeral | Payload | Use For |
|-------|-----------|---------|---------|
| `user.message` | No | `{content}` | User sent a message |
| `abort` | No | `{}` | Session aborted |
| `skill.invoked` | No | `{skillName}` | Skill loaded |
| `subagent.started` | No | `{agentName}` | Custom agent started |
| `subagent.completed` | No | `{agentName, result}` | Custom agent finished |
| `subagent.failed` | No | `{agentName, error}` | Custom agent errored |
| `subagent.selected` | No | `{agentName}` | Custom agent selected |
| `hook.start` | No | `{hookType}` | Hook invocation began |
| `hook.end` | No | `{hookType}` | Hook invocation ended |
| `system.message` | No | `{content}` | System/developer message |
| `pending_messages.modified` | Yes | `{...}` | Pending messages changed |

### `event-ephemeral-vs-persistent`
Ephemeral events (like deltas, usage_info, idle) are not stored in the session
transcript. Persistent events are part of the conversation history returned by
`getMessages()`. Design your event handlers accordingly:

- **Persistent events** → update state, build history views
- **Ephemeral events** → update UI in real-time, then discard

### `event-forward-compatibility`
The SDK passes unknown event types through without error. Your handler should
ignore events it doesn't recognize — never throw on unknown `event.type`.

---

## 3. Streaming (HIGH)

### `stream-enable`
Enable streaming in session config:

```typescript
const session = await client.createSession({
  model: "gpt-4.1",
  streaming: true,
});
```

Without `streaming: true`, you only get `assistant.message` (final). With it,
you also get `assistant.message_delta` and `assistant.reasoning_delta`.

### `stream-delta-pattern`
Standard streaming UI pattern:

```typescript
let buffer = "";

session.on("assistant.message_delta", (event) => {
  buffer += event.data.deltaContent;
  renderToUI(buffer);  // Progressive display
});

session.on("assistant.message", (event) => {
  buffer = "";                    // Reset buffer
  renderFinal(event.data.content);  // Complete message
});

session.on("session.idle", () => {
  markComplete();  // All processing done
});
```

### `stream-reasoning-deltas`
When `reasoningEffort` is set, reasoning tokens are also streamed:

```typescript
session.on("assistant.reasoning_delta", (event) => {
  renderReasoning(event.data.deltaContent);
});

session.on("assistant.reasoning", (event) => {
  renderFullReasoning(event.data.content);
});
```

### `stream-tool-progress`
Tools emit progress events during execution:

```typescript
session.on("tool.execution_start", (event) => {
  showSpinner(`Running ${event.data.toolName}...`);
});

session.on("tool.execution_progress", (event) => {
  updateStatus(event.data.message);
});

session.on("tool.execution_partial_result", (event) => {
  showPartialOutput(event.data.partialResult);
});

session.on("tool.execution_complete", (event) => {
  hideSpinner();
});
```

### `stream-send-and-wait-still-works`
`sendAndWait()` works with streaming enabled — it resolves when `session.idle`
fires. Events are still emitted, so you can stream AND get the final result:

```typescript
// Stream to UI AND get final result
session.on("assistant.message_delta", (e) => process.stdout.write(e.data.deltaContent));
const result = await session.sendAndWait({ prompt: "Explain this code" });
// result contains the complete message
```

---

## 4. Persistence & Resume (HIGH)

### `persist-session-id`
Provide a custom `sessionId` to enable resumability:

```typescript
const session = await client.createSession({
  sessionId: "user-alice-review-2026-02-22",
  model: "gpt-4.1",
});
```

Without custom ID, the SDK generates a random UUID and the session cannot be
resumed. Use structured IDs: `{user}-{task}-{date}` or `{workflow}-{step}`.

### `persist-resume-pattern`
Resume from any client instance pointing to the same storage:

```typescript
// Day 1, Client A:
const session = await client.createSession({
  sessionId: "project-analysis",
  model: "gpt-4.1",
});
await session.sendAndWait({ prompt: "Analyze src/" });
await session.destroy();  // State persisted to disk

// Day 2, Client B (or after restart):
const session = await client.resumeSession("project-analysis");
await session.sendAndWait({ prompt: "What issues did you find?" });
// Full conversation context restored
```

### `persist-resume-config-override`
Override settings when resuming:

```typescript
const session = await client.resumeSession("my-session", {
  model: "gpt-5",            // Switch model
  tools: [newTool],          // Add new tools
  provider: byokConfig,      // Change provider (BYOK keys not persisted)
  mcpServers: { ... },       // Reconfigure MCP servers
  hooks: { ... },            // Re-attach hooks
});
```

**What IS persisted:** conversation history, tool call results, planning state.
**What is NOT persisted:** provider/API keys, custom tool handlers, hooks, in-memory state.

### `persist-storage-location`
Session state lives at `~/.copilot/session-state/{sessionId}/`:

```
~/.copilot/session-state/
└── project-analysis/
    ├── checkpoints/       # Conversation snapshots
    ├── plan.md            # Agent planning state
    └── files/             # Session artifacts
```

### `persist-infinite-sessions`
Auto-compaction for long-running sessions:

```typescript
const session = await client.createSession({
  infiniteSessions: {
    enabled: true,
    compactionThreshold: 100000,  // tokens before compaction
  },
});
```

When the context window fills up, compaction summarizes older context to make
room. This keeps sessions alive indefinitely without hitting token limits.

### `persist-session-listing`
List and filter sessions:

```typescript
const sessions = await client.listSessions({
  status: "active",  // or other filters
});

for (const meta of sessions) {
  console.log(meta.sessionId, meta.title, meta.lastModified);
}
```

---

## 5. Advanced Controls (MEDIUM)

### `ctrl-agent-modes`
Switch between agent execution modes:

```typescript
// Get current mode
const mode = await session.rpc["mode.get"]();

// Set mode
await session.rpc["mode.set"]({ mode: "plan" });
```

| Mode | Behavior |
|------|----------|
| `interactive` | Default — agent executes immediately |
| `plan` | Agent creates a plan, waits for approval |
| `autopilot` | Agent executes without asking permission |

### `ctrl-plans`
Read and manage the agent's plan:

```typescript
const plan = await session.rpc["plan.read"]();      // Get plan.md content
await session.rpc["plan.update"]({ content: "..." }); // Edit plan
await session.rpc["plan.delete"]();                   // Clear plan
```

Plans are part of session state and persist across resume.

### `ctrl-model-switching`
Switch models mid-session:

```typescript
const current = await session.rpc["model.getCurrent"]();
await session.rpc["model.switchTo"]({ model: "gpt-5" });
```

The session history is preserved. The new model takes over with full context.

### `ctrl-workspace-files`
Interact with workspace files:

```typescript
const files = await session.rpc["workspace.listFiles"]();
const content = await session.rpc["workspace.readFile"]({ path: "src/main.ts" });
await session.rpc["workspace.createFile"]({ path: "new.ts", content: "..." });
```

### `ctrl-permission-handler`
Handle permission requests for tool execution:

```typescript
const session = await client.createSession({
  onPermissionRequest: async (request) => {
    // request: { toolName, toolArgs, description }
    if (request.toolName === "exec") {
      return { decision: "deny", reason: "Shell commands not allowed" };
    }
    return { decision: "allow" };
  },
});
```

Without a handler, permissions default to **deny**. Always provide one for
sessions that use tools.

### `ctrl-user-input-handler`
Handle ask_user requests from the agent:

```typescript
const session = await client.createSession({
  onUserInputRequest: async (request) => {
    // request: { prompt, choices? }
    if (request.choices) {
      return { response: request.choices[0] };  // Pick first option
    }
    return { response: "Yes, proceed" };  // Free text
  },
});
```

### `ctrl-fleet-mode`
Start fleet mode for multi-machine workloads:

```typescript
await session.rpc["fleet.start"]({ config: { ... } });
```

---

## Anti-Patterns

| Anti-Pattern | Why It's Wrong | Correct Approach |
|---|---|---|
| No `sessionId` for resumable tasks | Can't resume after restart | Always provide structured `sessionId` |
| Throwing on unknown event types | Breaks forward compatibility | Ignore unknown events silently |
| `sendAndWait` without timeout | Hangs forever on errors | Set reasonable timeout (60–300s) |
| Not calling `destroy()` | Memory leak in CLI | Always destroy when done |
| `deleteSession` when you mean `destroy` | Permanent data loss | `destroy()` preserves state, `delete` removes it |
| `systemMessage.type: "replace"` by default | Loses Copilot's built-in capabilities | Use `"append"` unless you need full control |
| No permission handler with tools | All tool calls denied by default | Always provide `onPermissionRequest` |
| Ignoring ephemeral flag | Storing transient UI events | Only persist non-ephemeral events |
| Streaming without `session.idle` handler | No way to know processing is done | Always listen for `session.idle` |
| Not re-attaching hooks on resume | Hooks aren't persisted | Re-provide hooks in `resumeSession()` config |

## References

- Getting Started: `.archive/copilot-sdk/docs/getting-started.md`
- Session Persistence: `.archive/copilot-sdk/docs/guides/session-persistence.md`
- Compatibility: `.archive/copilot-sdk/docs/compatibility.md`
```
