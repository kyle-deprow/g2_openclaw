# Copilot Bridge — Design Document

## 1. Overview

The Copilot Bridge is a bidirectional integration layer between **OpenClaw** (an autonomous coding agent) and the **GitHub Copilot SDK**. It enables each system to leverage the other's strengths:

- **OpenClaw → Copilot:** Delegates coding tasks (file edits, refactoring, code generation) to Copilot via plugin tools and an MCP server.
- **Copilot → OpenClaw:** Reads OpenClaw's memory, context, and user preferences via a dedicated MCP server, giving Copilot sessions awareness of the agent's knowledge base.

### What it is NOT

- Not a replacement for OpenClaw or GitHub Copilot — it is a thin coordination layer.
- Not a general-purpose LLM proxy — it specifically bridges OpenClaw's plugin/MCP interfaces with the Copilot SDK's session model.
- Not a standalone service — it runs embedded within the OpenClaw process (plugin) or as stdio-based MCP servers spawned on demand.

### Where it sits

```
┌──────────┐                                         ┌──────────────────┐
│ OpenClaw │                                         │ GitHub Copilot   │
│  Agent   │                                         │    SDK / CLI     │
└────┬─────┘                                         └────────┬─────────┘
     │                                                        │
     │  ┌──────────────────────────────────────────────────┐  │
     │  │              Copilot Bridge                      │  │
     │  │                                                  │  │
     ├──┤  plugin.ts    → tools for OpenClaw to call       │  │
     │  │  mcp-server.ts → MCP tools for OpenClaw to call  ├──┤
     │  │  mcp-openclaw.ts → MCP tools for Copilot to call │  │
     │  │  client.ts     → SDK session management          ├──┤
     │  │  hooks.ts      → permissions, audit, security    │  │
     │  └──────────────────────────────────────────────────┘  │
     │                                                        │
```

---

## 2. Architecture Diagram

```
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │                          DATA FLOW                                          │
 │                                                                             │
 │  ┌──────────┐  plugin tools                ┌───────────────┐  SDK sessions  │
 │  │          │  copilot ──────────────────→│               │──────────────→ │
 │  │          │  copilot_sessions ─────────→│               │               │
 │  │ OpenClaw │                              │ Copilot Bridge │  ┌──────────┐ │
 │  │  Agent   │                              │   (client.ts)  │→│ GitHub   │ │
 │  │          │  MCP tools (mcp-server.ts)   │                │  │ Copilot  │ │
 │  │          │  copilot_read_file ────────→│                │→│ SDK      │ │
 │  │          │  copilot_create_file ──────→│                │  │          │ │
 │  │          │  copilot_list_files ───────→│                │  │          │ │
 │  │          │  copilot_code_task ────────→│                │  │          │ │
 │  │          │                              │                │  │          │ │
 │  │          │←── openclaw_memory_search ──│ mcp-openclaw.ts│←─│          │ │
 │  │          │←── openclaw_memory_read ────│  (MCP server)  │←─│          │ │
 │  │          │←── openclaw_user_prefs ─────│                │←─│          │ │
 │  └──────────┘                              └───────────────┘  └──────────┘ │
 └─────────────────────────────────────────────────────────────────────────────┘
```

**Two communication patterns:**

| Direction | Mechanism | Transport |
|-----------|-----------|-----------|
| OpenClaw → Copilot (plugin) | Plugin tools registered via `plugin.ts` | In-process function calls |
| OpenClaw → Copilot (MCP) | MCP tools served by `mcp-server.ts` | stdio (JSON-RPC) |
| Copilot → OpenClaw | MCP tools served by `mcp-openclaw.ts` | stdio (JSON-RPC) |

---

## 3. Module Architecture

| File | Responsibility |
|------|---------------|
| `client.ts` | Core `CopilotBridge` class — wraps the Copilot SDK `CopilotClient`. Manages session lifecycle (create, send, destroy, resume), BYOK provider config, streaming, and session persistence to `~/.copilot-bridge/sessions.json`. |
| `config.ts` | Loads `BridgeConfig` from environment variables (with dotenv). Validates log levels, BYOK providers, ports, and retry counts. |
| `types.ts` | Shared value types: `CodingTaskRequest`, `CodingTaskResult`, `ToolCallRecord`, `StreamingDelta`, `ProviderConfig`, `BridgeError`. |
| `interfaces.ts` | Contract interfaces: `ICopilotClient`, `ICopilotSession`, `IPermissionHandler`, `IProviderConfig`. Enables testing via dependency injection. |
| `hooks.ts` | SDK session hooks factory (`createHooks`). Implements permission evaluation, path restriction, secret redaction, audit logging, prompt injection of project context, and error classification (retry/skip/abort). |
| `plugin.ts` | OpenClaw plugin (`copilot-bridge`). Exports two tools: `copilot` and `copilot_sessions`. Manages a shared bridge singleton. |
| `mcp-server.ts` | MCP server exposing Copilot capabilities to OpenClaw. Four tools: `copilot_read_file`, `copilot_create_file`, `copilot_list_files`, `copilot_code_task`. Runs on stdio transport. Includes lazy init, mutex for `copilot_code_task`, and clean shutdown. |
| `mcp-openclaw.ts` | MCP server exposing OpenClaw memory to Copilot sessions. Three read-only tools: `openclaw_memory_search`, `openclaw_memory_read`, `openclaw_user_prefs`. Includes WebSocket client for OpenClaw gateway, path validation, and keyword-based memory search. |
| `index.ts` | Barrel re-exports for the package public API. |

**Supporting files:**

| File | Purpose |
|------|---------|
| `openclaw-mcp-config.json` | MCP server registration config for OpenClaw (points to `mcp-server.ts` via `npx tsx`) |
| `openclaw-persona-snippet.md` | Persona instructions injected into OpenClaw to guide tool usage (when to delegate vs. handle directly, prompt-writing tips) |
| `package.json` | Node.js package — depends on `@github/copilot-sdk`, `@modelcontextprotocol/sdk`, `dotenv`, `zod` |

---

## 4. Integration Points

### 4.1 OpenClaw → Copilot: Plugin Tools

Registered via `plugin.ts` as an OpenClaw plugin. OpenClaw loads the plugin and gains two tools:

| Tool | Description | Key Parameters |
|------|-------------|---------------|
| `copilot` | Delegate a coding task to a Copilot session. Creates/resumes session by `workingDir`. Returns result as markdown. | `prompt` (required), `workingDir` (required), `persona?`, `timeout?` |
| `copilot_sessions` | Manage sessions. `action="list"` lists active sessions, `action="destroy"` + `project` kills a specific session. | `action` (required: `"list"` or `"destroy"`), `project?` |

**`copilot`** calls `bridge.runTask()` — creates or resumes a Copilot SDK session keyed by `workingDir`, sends the prompt, waits for the response, and returns formatted markdown with tool calls and stats.

**`copilot_sessions`** provides session management — list all active sessions or destroy a specific one by project name/path to start fresh.

### 4.2 OpenClaw → Copilot: MCP Server

Served by `mcp-server.ts` over stdio. OpenClaw connects via the config in `openclaw-mcp-config.json`.

| Tool | Description | Key Parameters |
|------|-------------|---------------|
| `copilot_read_file` | Read a workspace file via Copilot's `workspace.readFile` RPC | `path` |
| `copilot_create_file` | Create a file via Copilot's `workspace.createFile` RPC | `path`, `content` |
| `copilot_list_files` | List directory contents via Copilot's `workspace.listFiles` RPC | `directory` (optional) |
| `copilot_code_task` | Agent-mediated coding task — full `runTask` behind a mutex | `prompt`, `workingDir`, `timeout` |

The `copilot_code_task` tool serialises concurrent calls through a promise-based mutex to avoid session conflicts.

### 4.3 Copilot → OpenClaw: MCP Server

Served by `mcp-openclaw.ts` over stdio. Automatically spawned per Copilot SDK session (configured in `client.ts` `sessionConfig.mcpServers`).

| Tool | Description | Key Parameters |
|------|-------------|---------------|
| `openclaw_memory_search` | Keyword search over `~/.openclaw/memory/*.md` files | `query`, `limit` (default 5) |
| `openclaw_memory_read` | Read a specific memory file (default: `MEMORY.md`) | `file` (optional) |
| `openclaw_user_prefs` | Read `~/.openclaw/USER.md` user preferences | (none) |

All three tools are **read-only**. They include `_depth` tracking for cycle detection (see §5).

The `mcp-openclaw.ts` module also contains an `OpenClawClient` WebSocket client for direct RPC to the OpenClaw gateway (host/port from config), with exponential backoff reconnection.

---

## 5. Security

### Permission Hooks

The `createHooks()` factory in `hooks.ts` returns SDK-compatible session hooks that enforce security policy on every tool call:

| Hook | Behaviour |
|------|-----------|
| `onPreToolUse` | Evaluates tool name and args against `PermissionPolicy`. Checks blocked tools, blocked argument patterns (regex), ask-approval tools, and allowlist. **Fails closed** — any internal hook error results in `deny`. |
| `onPostToolUse` | Redacts secrets from tool output, truncates results exceeding 10 KB. |
| `onUserPromptSubmitted` | Redacts secrets from prompts, injects `projectContext` if configured. |
| `onErrorOccurred` | Classifies errors: rate-limit → retry, transient → retry (max 2), file-not-found → skip, other → abort. |

### Permission Policy

```typescript
interface PermissionPolicy {
  allowedTools: string[];   // Allowlist (empty = allow all)
  blockedTools: string[];   // Blocklist (always deny)
  askTools: string[];       // Require approval
  blockedPatterns: RegExp[]; // Deny if tool args match
}
```

Evaluation order: blocked tools → blocked patterns → ask tools → allowlist → default allow.

### Audit Logging

`AuditLogger` writes JSONL entries to `<auditLogDir>/audit-YYYY-MM-DD.jsonl`. Each entry includes timestamp, session ID, hook type, tool name, redacted input/output, and elapsed time. Writes are buffered and flushed after each hook invocation.

### Path Restrictions

Two layers of path validation:

1. **Workspace containment** (`hooks.ts`): `onPreToolUse` validates all path-like tool arguments (`path`, `file`, `filePath`, `directory`, `dir`, `destination`, `target`, `outputPath`, `inputPath`) resolve within the working directory. Follows symlinks for defence-in-depth.
2. **Memory directory containment** (`mcp-openclaw.ts`): `validateMemoryPath()` ensures memory file reads cannot escape `~/.openclaw/memory/`. Rejects null bytes, resolves symlinks.

### Secret Redaction

`redactSecrets()` replaces tokens matching known patterns (GitHub PATs `ghp_`/`gho_`, AWS keys `AKIA...`, `sk-` prefixes, `password=`, `api_key=`, `token=`, `secret=`, `Bearer`) with `[REDACTED]`. Applied to:
- Tool arguments (pre-tool-use)
- Tool results (post-tool-use)
- User prompts (prompt hook)

### Cycle Detection

Both MCP servers use a `_depth` parameter on every tool to track call depth. `checkDepth()` returns an error response when `_depth >= MAX_CALL_DEPTH` (**3**). This prevents infinite loops where Copilot calls an OpenClaw tool that calls back into Copilot.

---

## 6. Configuration

All configuration is loaded from environment variables via `config.ts`:

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `COPILOT_GITHUB_TOKEN` | `string` | — | GitHub token for Copilot SDK authentication |
| `COPILOT_BYOK_PROVIDER` | `enum` | — | BYOK provider: `openai`, `azure`, `anthropic`, `ollama` |
| `COPILOT_BYOK_API_KEY` | `string` | — | API key for the BYOK provider |
| `COPILOT_BYOK_BASE_URL` | `string` | — | Base URL for the BYOK provider |
| `COPILOT_BYOK_MODEL` | `string` | — | Model name for the BYOK provider |
| `COPILOT_CLI_PATH` | `string` | — | Path to the Copilot CLI binary |
| `COPILOT_LOG_LEVEL` | `enum` | `info` | Log verbosity: `debug`, `info`, `warning`, `error`, `none`, `all` |
| `OPENCLAW_HOST` | `string` | `127.0.0.1` | OpenClaw gateway hostname |
| `OPENCLAW_PORT` | `number` | `18789` | OpenClaw gateway port |
| `OPENCLAW_GATEWAY_TOKEN` | `string` | — | Auth token for OpenClaw WebSocket connection |
| `COPILOT_AUDIT_LOG_DIR` | `string` | `<module>/../.copilot-bridge/audit` | Directory for audit log files |
| `COPILOT_PROJECT_CONTEXT` | `string` | — | Additional context injected into every prompt |
| `COPILOT_MAX_RETRIES` | `number` | `3` | Max retries for transient/rate-limit errors |

A `.env` file is automatically loaded via `dotenv`.

---

## 7. Setup

### Register the OpenClaw Plugin

The plugin provides `copilot` and `copilot_sessions` tools directly to OpenClaw:

```bash
cd copilot_bridge
npm run register        # register the plugin with OpenClaw
npm run unregister      # remove the plugin
```

### Register the MCP Servers

**For OpenClaw** (so it can call `copilot_read_file`, `copilot_create_file`, etc.):

```bash
cd copilot_bridge
npm run register:mcp    # register mcp-server.ts with OpenClaw
npm run unregister:mcp  # remove the MCP server
```

Alternatively, copy `openclaw-mcp-config.json` into your OpenClaw MCP configuration:

```json
{
  "mcp": {
    "servers": {
      "copilot": {
        "command": "npx",
        "args": ["tsx", "copilot_bridge/src/mcp-server.ts"]
      }
    }
  }
}
```

**For Copilot sessions** (so they can call `openclaw_memory_search`, etc.):

No manual registration needed. The `CopilotBridge` automatically configures `mcp-openclaw.js` as a local MCP server in every SDK session it creates.

---

## Integration Status (2026-03-12)

- Plugin registered with OpenClaw: `copilot` and `copilot_sessions` tools active
- OpenClaw `tools.allow` includes `copilot` and `copilot_sessions`
- Sessions use LRU eviction (max 8 concurrent sessions)
- One session per `workingDir` — sessions persist across multiple `copilot()` calls
- `.github/agents/` and `.github/skills/` in workingDir auto-discovered at session creation
- BYOK configured for Azure OpenAI via environment variables
- MCP server (`mcp-openclaw.ts`) provides Copilot sessions read-only access to OpenClaw memory

### Validate the Connection

```bash
cd copilot_bridge
npm run validate        # ping the SDK, check auth, print status
```

### Persona Instructions

Copy `openclaw-persona-snippet.md` into your OpenClaw agent persona to teach the agent when and how to use the bridge tools.
