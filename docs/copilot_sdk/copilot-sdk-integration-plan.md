# OpenClaw + Copilot SDK Integration — Implementation Plan

## Overview

Integrate OpenClaw (AI assistant runtime, localhost:18789) with the GitHub Copilot SDK (JSON-RPC 2.0 agent runtime for code-aware tasks). OpenClaw orchestrates high-level planning, memory, and automation; Copilot SDK handles code-aware tool execution (file I/O, terminal, search). The integration progresses from basic SDK validation → plugin delegation → bidirectional MCP → hooks/audit → advanced orchestration.

### Source Code Location

```
src/copilot_bridge/                ← Node.js/TypeScript package
├── package.json
├── tsconfig.json
├── .env.example
├── src/
│   ├── client.ts                  ← CopilotClient wrapper with config & lifecycle
│   ├── plugin.ts                  ← OpenClaw plugin entry point
│   ├── mcp-server.ts             ← MCP server exposing Copilot tools to OpenClaw
│   ├── mcp-client.ts             ← MCP client for OpenClaw memory (used by Copilot sessions)
│   ├── hooks.ts                   ← Hook implementations (audit, permissions, context)
│   ├── orchestrator.ts            ← Multi-step task orchestrator
│   ├── config.ts                  ← Unified config (env vars, dotenv)
│   ├── types.ts                   ← Shared TypeScript types
│   └── index.ts                   ← Package exports
├── tests/
│   ├── client.test.ts
│   ├── plugin.test.ts
│   ├── mcp-server.test.ts
│   ├── mcp-client.test.ts
│   ├── hooks.test.ts
│   ├── orchestrator.test.ts
│   └── integration/
│       ├── sdk-smoke.test.ts
│       ├── plugin-e2e.test.ts
│       └── mcp-bridge.test.ts
└── scripts/
    └── validate-connection.ts     ← Quick connectivity check script
```

### Agent/Owner Types

| Owner | Domain | Language |
|-------|--------|----------|
| `copilot-sdk-integration` | Copilot SDK client, sessions, tools, hooks, MCP servers | TypeScript |
| `openclaw-development` | OpenClaw config, plugins, MCP registration, agent setup | TypeScript/YAML |
| `backend-python` | Python-side utilities, test harnesses, config validation | Python |

---

# Phase 1: Copilot SDK Client Bootstrap

## Phase Goal

Get a CopilotClient running in a standalone Node.js service, authenticated against the Copilot CLI (or BYOK provider), able to create sessions, send prompts, and receive streamed responses. Pure SDK validation — no OpenClaw integration yet. This proves the SDK works in our environment and establishes the client wrapper that all later phases build on.

## Prerequisites

- Node.js ≥ 18 and npm installed
- GitHub Copilot CLI installed (`npm install -g @github/copilot-cli` or via `gh extension install github/gh-copilot`)
- GitHub authentication token OR a BYOK provider API key (OpenAI, Anthropic, etc.)
- No OpenClaw instance required for this phase

## Task Breakdown

### C1.1 — Project Scaffold & Config

- **Description:** Create the `copilot_bridge` TypeScript project with `package.json`, `tsconfig.json`, and `config.ts`. Config loads from env vars and `.env` via `dotenv`. Define all config fields needed across all phases (mark Phase 1 fields as required, rest as optional).
- **Owner:** copilot-sdk-integration
- **Dependencies:** None
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/package.json`
  - `src/copilot_bridge/tsconfig.json`
  - `src/copilot_bridge/.env.example`
  - `src/copilot_bridge/src/config.ts`
  - `src/copilot_bridge/src/index.ts`
- **Acceptance criteria:**
  - `package.json` declares `@github/copilot-sdk` as dependency, `vitest` + `typescript` as dev deps
  - `tsconfig.json` targets ES2022, module NodeNext, strict mode
  - `config.ts` exports `loadConfig(): BridgeConfig` reading from env vars with defaults:
    - `COPILOT_AUTH_TOKEN` (optional — falls back to stored CLI credentials)
    - `COPILOT_BYOK_PROVIDER` (optional — `openai | azure | anthropic | ollama`)
    - `COPILOT_BYOK_API_KEY` (optional)
    - `COPILOT_BYOK_BASE_URL` (optional)
    - `COPILOT_BYOK_MODEL` (optional)
    - `COPILOT_CLI_PATH` (optional — override CLI binary path)
    - `COPILOT_LOG_LEVEL` (default `info`)
    - `OPENCLAW_HOST` (default `127.0.0.1`)
    - `OPENCLAW_PORT` (default `18789`)
    - `OPENCLAW_GATEWAY_TOKEN` (optional for Phase 1)
  - `npm install` succeeds
  - `npx tsc --noEmit` passes

### C1.2 — Shared Types

- **Description:** Define TypeScript types for the integration layer. These types represent the contract between OpenClaw and Copilot SDK — task requests, task results, streaming events, error types.
- **Owner:** copilot-sdk-integration
- **Dependencies:** None
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/src/types.ts`
- **Acceptance criteria:**
  - `CodingTaskRequest`: `{ prompt: string; workingDir?: string; model?: string; tools?: string[]; sessionId?: string; timeout?: number; }`
  - `CodingTaskResult`: `{ success: boolean; content: string; toolCalls: ToolCallRecord[]; errors: string[]; sessionId: string; elapsed: number; }`
  - `ToolCallRecord`: `{ tool: string; args: Record<string, unknown>; result: string; timestamp: number; }`
  - `StreamingDelta`: `{ type: "text" | "tool_start" | "tool_end" | "error" | "done"; content: string; tool?: string; }`
  - `BridgeError`: custom error class with `code`, `details`, `recoverable` fields
  - All types exported from `index.ts`

### C1.3 — CopilotClient Wrapper

- **Description:** Wrap `CopilotClient` from the SDK with our config, lifecycle management, and error handling. Support both GitHub auth and BYOK modes. Implement start/stop, health check, and connection state tracking.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.1 (config), C1.2 (types)
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/client.ts`
  - `src/copilot_bridge/tests/client.test.ts`
- **Acceptance criteria:**
  - `CopilotBridge` class wrapping `CopilotClient`:
    - `constructor(config: BridgeConfig)` — stores config, does NOT start client
    - `async start(): Promise<void>` — calls `client.start()`, waits for ready state, logs connection info
    - `async stop(): Promise<void>` — calls `client.stop()`, cleans up
    - `async isReady(): Promise<boolean>` — checks client connection state
    - `async getStatus(): Promise<{ connected: boolean; authMethod: string; model: string; }>`
  - GitHub auth mode: uses token or stored credentials
  - BYOK mode: configures provider from `COPILOT_BYOK_*` env vars
  - Auto-restart on crash (configurable)
  - Structured logging at configurable level
  - Tests: mock CopilotClient, verify start/stop lifecycle, verify BYOK config passed correctly

### C1.4 — Session Runner

- **Description:** Implement the core session creation and prompt execution flow. Create a session, send a prompt, collect the full response (blocking mode), and return a structured result. Support both `sendAndWait` (blocking) and streaming modes.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.3 (client wrapper)
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/client.ts` (extend — add session methods)
  - `src/copilot_bridge/tests/client.test.ts` (extend)
- **Acceptance criteria:**
  - `CopilotBridge.runTask(request: CodingTaskRequest): Promise<CodingTaskResult>` — blocking execution:
    - Creates session with model, tools config
    - Sends prompt via `sendAndWait()`
    - Collects response content and any tool call records
    - Returns `CodingTaskResult` with timing info
    - Destroys session after (unless `sessionId` provided for persistence)
  - `CopilotBridge.runTaskStreaming(request: CodingTaskRequest): AsyncGenerator<StreamingDelta>` — streaming:
    - Creates session, sends prompt
    - Yields `StreamingDelta` events as they arrive
    - Yields `{ type: "done", content: "" }` at end
  - Timeout: wraps execution with configurable timeout (default 120s)
  - Error mapping: SDK errors → `BridgeError` with appropriate codes
  - Tests: mock session responses, verify blocking and streaming modes, verify timeout

### C1.5 — Connectivity Validation Script

- **Description:** Create a standalone script that validates the full chain: load config → start client → create session → send "Hello, respond with just 'OK'" → verify response → stop. Used for CI and manual smoke testing.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.3, C1.4
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/scripts/validate-connection.ts`
- **Acceptance criteria:**
  - Runs via `npx tsx scripts/validate-connection.ts`
  - Logs each step: "Loading config...", "Starting client...", "Creating session...", "Sending prompt...", "Response received: ...", "Stopping client..."
  - Exit code 0 on success, 1 on failure with clear error message
  - Supports `--byok` flag to test BYOK mode specifically
  - Completes in < 30s

### C1.6 — SDK Smoke Integration Test

- **Description:** Write integration tests that exercise the real Copilot SDK (not mocked). These validate the SDK actually works in our environment. Skipped in CI unless `COPILOT_INTEGRATION=1` is set.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.4, C1.5
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/tests/integration/sdk-smoke.test.ts`
- **Acceptance criteria:**
  - Test 1: Create client, start, verify `isReady()` returns true
  - Test 2: Create session, send simple prompt, verify non-empty response
  - Test 3: Create session with streaming, verify delta events arrive
  - Test 4: Create session with timeout, send complex prompt, verify timeout fires
  - Test 5: BYOK mode (if configured) — verify alternative provider works
  - All tests guarded by `describe.skipIf(!process.env.COPILOT_INTEGRATION)`
  - `npm test` runs unit tests only (fast); `COPILOT_INTEGRATION=1 npm test` runs everything

## Parallel Execution Plan

```
── Time →

Agent A (copilot-sdk-integration):
  [C1.1 scaffold + config] → [C1.3 client wrapper] → [C1.4 session runner] → [C1.5 validate script]
        ~0.5h                       ~2h                      ~2h                     ~0.5h

Agent B (copilot-sdk-integration):
  [C1.2 shared types] → (feeds into C1.3) ─────────────────────────────→ [C1.6 integration tests]
        ~0.5h                                                                  ~1.5h
```

**Parallelization:**
- C1.1 and C1.2 start simultaneously (no dependencies)
- C1.3 merges both (needs config + types)
- C1.4 depends on C1.3
- C1.5 and C1.6 can run in parallel once C1.4 is done

## Integration Checkpoint

1. **`npm install` and `npx tsc --noEmit`** — project compiles
2. **`npm test`** — all unit tests pass (mocked)
3. **`npx tsx scripts/validate-connection.ts`** — real SDK session succeeds
4. **Streaming works** — deltas arrive incrementally, not all at once
5. **BYOK works** — if configured, alternative provider responds
6. **Timeout fires** — long-running prompt correctly times out

## Definition of Done

- [ ] `npm install` in `src/copilot_bridge/` succeeds
- [ ] `npx tsc --noEmit` passes with no errors
- [ ] `npm test` — all unit tests pass
- [ ] `validate-connection.ts` completes successfully with GitHub auth or BYOK
- [ ] Streaming mode yields incremental deltas
- [ ] Timeout mechanism works and returns a BridgeError
- [ ] `.env.example` documents all config variables
- [ ] Client wrapper handles start/stop/restart lifecycle cleanly

---

# Phase 2: OpenClaw Plugin — Basic Delegation

## Phase Goal

Build an OpenClaw plugin that accepts a natural-language coding task description from the OpenClaw agent, delegates it to a Copilot SDK session, and returns the result. This enables OpenClaw conversations to include code generation, refactoring, and file manipulation via Copilot. Text-in, text-out — the simplest useful integration.

## Prerequisites

- Phase 1 complete: CopilotBridge client wraps SDK, sessions work, streaming works
- OpenClaw Gateway running on localhost:18789 with `OPENCLAW_GATEWAY_TOKEN` set
- OpenClaw CLI installed and `openclaw gateway status` shows healthy
- Understanding of OpenClaw plugin registration (tool definitions in `~/.openclaw/config`)

## Task Breakdown

### C2.1 — OpenClaw Plugin Entry Point

- **Description:** Create the OpenClaw plugin module that registers a `copilot_code` tool with OpenClaw. The tool accepts a `task` description and optional `workingDir`, `model`, and `timeout` params. It creates a CopilotBridge instance (or reuses a shared one), runs the task, and returns the formatted result.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.4 (session runner)
- **Complexity:** L
- **Files created:**
  - `src/copilot_bridge/src/plugin.ts`
  - `src/copilot_bridge/tests/plugin.test.ts`
- **Acceptance criteria:**
  - Exports a `copilotCodeTool` object matching OpenClaw's tool schema:
    ```
    { name: "copilot_code", description: "Delegate a coding task to GitHub Copilot...",
      parameters: { task: string (required), workingDir?: string, model?: string, timeout?: number } }
    ```
  - `execute(params)` function:
    - Gets or creates shared CopilotBridge instance (singleton, lazy init)
    - Calls `bridge.runTask({ prompt: params.task, workingDir: params.workingDir, ... })`
    - Formats result as markdown: code blocks for code, summary of tool calls, error details if failed
    - Returns formatted string to OpenClaw
  - Handles errors gracefully — returns human-readable error message, never crashes the Gateway
  - Logs task start/end with timing
  - Tests: mock CopilotBridge, verify tool schema, verify result formatting, verify error handling

### C2.2 — Streaming Variant Plugin

- **Description:** Create a streaming variant `copilot_code_stream` that yields progress updates back to OpenClaw as the Copilot session executes. This gives the user visibility into what Copilot is doing (tool calls, intermediate output) rather than waiting for the full result.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C2.1 (basic plugin)
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/plugin.ts` (extend — add streaming tool)
  - `src/copilot_bridge/tests/plugin.test.ts` (extend)
- **Acceptance criteria:**
  - `copilotCodeStreamTool` with same parameters as `copilot_code`
  - Returns an async iterator of progress messages:
    - `"🔧 Calling tool: {toolName}..."` on tool start
    - `"✅ {toolName} complete"` on tool end
    - `"📝 {delta_text}"` for text deltas (batched, not every token)
    - `"✅ Task complete ({elapsed}s)"` on finish
    - `"❌ Error: {message}"` on failure
  - OpenClaw receives progressive updates in conversation
  - Tests: mock streaming session, verify message sequence

### C2.3 — OpenClaw Plugin Registration Config

- **Description:** Create the OpenClaw config snippet and registration instructions for enabling the Copilot bridge plugin. Create a setup script that validates OpenClaw is running and registers the tools.
- **Owner:** openclaw-development
- **Dependencies:** C2.1 (plugin exists)
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/openclaw-config.json` (config snippet to merge into `~/.openclaw/config`)
  - `src/copilot_bridge/scripts/register-plugin.ts`
- **Acceptance criteria:**
  - `openclaw-config.json` contains the tool definitions in OpenClaw's expected format:
    ```json
    { "tools": [
      { "name": "copilot_code", "module": "./src/copilot_bridge/src/plugin.ts", ... },
      { "name": "copilot_code_stream", "module": "./src/copilot_bridge/src/plugin.ts", ... }
    ]}
    ```
  - `register-plugin.ts` script:
    - Reads current `~/.openclaw/config`
    - Merges tool definitions (doesn't overwrite existing tools)
    - Writes updated config
    - Validates by calling `openclaw gateway status`
    - Logs instructions for restarting Gateway if needed
  - Documentation in script output explains what was added

### C2.4 — OpenClaw Agent System Prompt Update

- **Description:** Create an OpenClaw agent persona snippet that teaches the agent how and when to use the `copilot_code` tool. The agent should know: what tasks to delegate (code generation, refactoring, file editing), what to keep (conversation, planning, memory), and how to format task descriptions for best results.
- **Owner:** openclaw-development
- **Dependencies:** C2.1 (tool API known)
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/openclaw-persona-snippet.md`
- **Acceptance criteria:**
  - Markdown snippet suitable for inclusion in an OpenClaw agent's `USER.md` or system prompt
  - Covers:
    - When to use `copilot_code` vs handling directly (decision tree)
    - How to write effective task descriptions (include file paths, language, constraints)
    - How to interpret results (code blocks, tool call summaries)
    - Error recovery guidance (retry with more specific prompt, fall back to manual)
  - Example prompts showing good delegation
  - 50-100 lines

### C2.5 — Plugin End-to-End Test

- **Description:** End-to-end test with real OpenClaw and Copilot SDK. Send a message to OpenClaw that triggers the `copilot_code` tool, verify the response includes Copilot-generated code.
- **Owner:** openclaw-development
- **Dependencies:** C2.1, C2.3, C2.4 (all plugin components)
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/tests/integration/plugin-e2e.test.ts`
- **Acceptance criteria:**
  - Test 1: Send "Write a Python function that reverses a string" → response contains a Python function
  - Test 2: Send "Refactor this code: [paste code]" → response contains refactored code
  - Test 3: Send "What is the capital of France?" → agent does NOT use `copilot_code` (handles directly)
  - Test 4: Trigger timeout → response contains error message, OpenClaw continues normally
  - Tests guarded by `COPILOT_INTEGRATION=1 && OPENCLAW_INTEGRATION=1`
  - Full round-trip latency logged

## Parallel Execution Plan

```
── Time →

Agent A (copilot-sdk-integration):
  [C2.1 plugin entry point] ──→ [C2.2 streaming variant] ──────────────────→ [C2.5 e2e test]
           ~3h                           ~2h                                       ~2h

Agent B (openclaw-development):
  [C2.4 persona snippet] ──→ [C2.3 plugin registration] ──→ (feeds into C2.5)
           ~1h                        ~1h
```

**Parallelization:**
- C2.1 and C2.4 start simultaneously (plugin code vs. persona doc)
- C2.2 depends on C2.1; C2.3 depends on C2.1
- C2.3 and C2.4 can feed into C2.5 independently
- C2.5 needs everything — joint verification

## Integration Checkpoint

1. **Plugin registers with OpenClaw** — `copilot_code` appears in tool list
2. **Basic delegation works** — "Write a hello world in Python" → returns Python code
3. **Streaming updates** — `copilot_code_stream` shows tool call progress
4. **Error handling** — timeout, SDK crash, and bad prompts produce recoverable errors
5. **Agent decision-making** — OpenClaw uses `copilot_code` only for coding tasks
6. **Sequential tasks** — multiple coding delegations in one conversation work

## Definition of Done

- [ ] `copilot_code` tool registered with OpenClaw and visible in agent tool list
- [ ] OpenClaw can delegate a coding task and receive formatted code response
- [ ] Streaming variant shows progressive tool call updates
- [ ] Errors (timeout, SDK crash) handled gracefully without crashing Gateway
- [ ] Agent persona guides when to delegate vs. handle directly
- [ ] End-to-end test passes with real OpenClaw + Copilot SDK
- [ ] Plugin cleans up SDK sessions (no resource leaks after 10+ delegations)
- [ ] Response formatting: code in fenced blocks, tool calls summarized, timing reported

---

# Phase 3: MCP Bridge — Bidirectional Tool Access

## Phase Goal

Build a bidirectional MCP bridge: (a) expose Copilot SDK's code-aware tools (file read/write, terminal, search) as an MCP server that OpenClaw can call directly, and (b) expose OpenClaw's memory and context tools as an MCP server that Copilot SDK sessions can access. This enables both platforms to use each other's capabilities natively through the standard MCP protocol.

## Prerequisites

- Phase 2 complete: plugin delegation working end-to-end
- Understanding of MCP protocol (stdio and HTTP/SSE transports)
- OpenClaw MCP config: `mcp.servers` in `~/.openclaw/config`
- Copilot SDK MCP config: `mcpServers` in session config

## Task Breakdown

### C3.1 — Copilot-to-OpenClaw MCP Server (Copilot Code Tools → OpenClaw)

- **Description:** Build an MCP server (stdio transport) that wraps a CopilotBridge session, exposing Copilot's built-in coding tools as MCP tools. OpenClaw connects to this server and can call tools like `copilot_read_file`, `copilot_write_file`, `copilot_terminal`, `copilot_search` directly.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.4 (session runner)
- **Complexity:** L
- **Files created:**
  - `src/copilot_bridge/src/mcp-server.ts`
  - `src/copilot_bridge/tests/mcp-server.test.ts`
- **Acceptance criteria:**
  - MCP server starts via stdio (`node src/mcp-server.ts`)
  - Exposes tools:
    - `copilot_code_task` — send a coding prompt, get response (wraps `runTask`)
    - `copilot_read_file` — read a file via Copilot session
    - `copilot_write_file` — write/create a file via Copilot session
    - `copilot_terminal` — execute a terminal command via Copilot session
    - `copilot_search` — search workspace files via Copilot session
  - Each tool has proper JSON Schema parameter definitions
  - Server maintains a persistent CopilotBridge instance (started on first tool call)
  - Server handles concurrent tool calls (queued if same session)
  - Clean shutdown on SIGTERM/SIGINT
  - Tests: mock MCP client, verify tool discovery, verify tool execution, verify error propagation

### C3.2 — OpenClaw-to-Copilot MCP Server (OpenClaw Memory → Copilot)

- **Description:** Build an MCP server that wraps OpenClaw's memory and context capabilities, exposing them as tools that Copilot SDK sessions can access. This lets Copilot sessions query user memory, project context, and conversation history.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.4 (session runner), OpenClaw wire protocol knowledge
- **Complexity:** L
- **Files created:**
  - `src/copilot_bridge/src/mcp-client.ts`
  - `src/copilot_bridge/tests/mcp-client.test.ts`
- **Acceptance criteria:**
  - MCP server starts via stdio (`node src/mcp-client.ts`)
  - Exposes tools:
    - `openclaw_memory_search` — vector search OpenClaw memory (`mem_search` tool equivalent)
    - `openclaw_memory_read` — read MEMORY.md or daily memory files
    - `openclaw_context` — get current conversation context/summary from OpenClaw session
    - `openclaw_user_prefs` — read USER.md for user preferences
  - Connects to OpenClaw Gateway via WebSocket to execute queries
  - Auth: uses `OPENCLAW_GATEWAY_TOKEN` for WebSocket connection
  - Handles OpenClaw Gateway disconnects with reconnection
  - Tests: mock OpenClaw WebSocket server, verify tool discovery, verify memory queries

### C3.3 — OpenClaw MCP Registration

- **Description:** Configure OpenClaw to connect to the Copilot MCP server (C3.1). Add the MCP server to `~/.openclaw/config` under `mcp.servers`. Create a registration/validation script.
- **Owner:** openclaw-development
- **Dependencies:** C3.1 (MCP server exists)
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/scripts/register-mcp.ts`
  - `src/copilot_bridge/openclaw-mcp-config.json`
- **Acceptance criteria:**
  - `openclaw-mcp-config.json` contains MCP server config:
    ```json
    { "mcp": { "servers": {
      "copilot": {
        "command": "node",
        "args": ["src/copilot_bridge/src/mcp-server.ts"],
        "transport": "stdio"
      }
    }}}
    ```
  - `register-mcp.ts` merges config into `~/.openclaw/config`
  - After registration + Gateway restart, OpenClaw agent can call `copilot_*` tools
  - Validation: `openclaw agent --message "List the tools you have"` shows copilot tools

### C3.4 — Copilot Session MCP Registration

- **Description:** Configure Copilot SDK sessions to connect to the OpenClaw MCP server (C3.2). The `mcpServers` config is passed in session creation.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C3.2 (OpenClaw MCP server exists)
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/src/client.ts` (extend — add MCP config to session creation)
  - `src/copilot_bridge/tests/client.test.ts` (extend)
- **Acceptance criteria:**
  - `CopilotBridge.runTask()` now passes `mcpServers` config when creating sessions:
    ```typescript
    mcpServers: {
      openclaw: {
        command: "node",
        args: ["src/copilot_bridge/src/mcp-client.ts"],
        transport: "stdio"
      }
    }
    ```
  - Copilot sessions can call `openclaw_memory_search`, `openclaw_context`, etc.
  - MCP server lifecycle tied to session lifecycle (starts with session, stops when session destroyed)
  - Tests: verify MCP config passed to session, verify tools accessible in session

### C3.5 — MCP Bridge Integration Test

- **Description:** End-to-end bidirectional test: OpenClaw calls a Copilot tool via MCP, and a Copilot session calls an OpenClaw tool via MCP, all in the same interaction.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C3.1, C3.2, C3.3, C3.4
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/tests/integration/mcp-bridge.test.ts`
- **Acceptance criteria:**
  - Test 1: OpenClaw → `copilot_read_file` → Copilot reads a file → result returned to OpenClaw
  - Test 2: OpenClaw → `copilot_terminal` → Copilot runs `ls` → output returned
  - Test 3: Copilot session → `openclaw_memory_search` → OpenClaw memory queried → result available to Copilot
  - Test 4: Combined — OpenClaw delegates task, Copilot reads memory mid-task for context
  - Test 5: MCP server crash recovery — server restarts, next tool call works
  - Tests guarded by `COPILOT_INTEGRATION=1 && OPENCLAW_INTEGRATION=1`

## Parallel Execution Plan

```
── Time →

Agent A (copilot-sdk-integration):
  [C3.1 Copilot→OpenClaw MCP server] ──────→ [C3.4 session MCP config] ──→ [C3.5 integration test]
              ~4h                                    ~1h                          ~2h

Agent B (copilot-sdk-integration):
  [C3.2 OpenClaw→Copilot MCP server] ──────→ (feeds into C3.4 and C3.5)
              ~4h

Agent C (openclaw-development):
  ──────────────────────── wait for C3.1 ──→ [C3.3 OpenClaw MCP registration]
                                                      ~1h
```

**Parallelization:**
- C3.1 and C3.2 are fully independent MCP servers — build in parallel
- C3.3 depends on C3.1 (needs to register the server)
- C3.4 depends on C3.2 (needs the server to connect to)
- C3.5 needs all four — final verification

## Integration Checkpoint

1. **OpenClaw sees Copilot tools** — `copilot_read_file`, `copilot_terminal`, etc. in tool list
2. **OpenClaw can call Copilot tools** — file read, terminal exec, search all work
3. **Copilot can call OpenClaw tools** — memory search, context query work mid-session
4. **MCP servers start/stop cleanly** — no zombie processes
5. **Error propagation** — MCP tool errors surface as readable messages in both directions
6. **Concurrent calls** — multiple tool calls queued correctly (no races)

## Definition of Done

- [ ] Copilot MCP server exposes 5 tools, discoverable via MCP tool listing
- [ ] OpenClaw MCP server exposes 4 tools, discoverable via MCP tool listing
- [ ] OpenClaw agent can call `copilot_read_file` and receive file contents
- [ ] OpenClaw agent can call `copilot_terminal` and receive command output
- [ ] Copilot session can call `openclaw_memory_search` and receive memory results
- [ ] Bidirectional test passes: OpenClaw → Copilot tool + Copilot → OpenClaw tool in one flow
- [ ] MCP servers handle graceful shutdown (SIGTERM)
- [ ] No zombie MCP server processes after 10+ tool call cycles
- [ ] Error for MCP server crash → readable error message, auto-recovery on next call

---

# Phase 4: Hooks, Audit & Permission Control

## Phase Goal

Implement the Copilot SDK hook system for tool governance, audit logging, prompt modification, and error handling. Every Copilot tool call goes through permission checks. Every result is logged. Project context is injected into every session. Errors are handled with configurable retry/skip/abort strategies. This is the safety and compliance layer.

## Prerequisites

- Phase 3 complete: MCP bridge working bidirectionally
- Understanding of Copilot SDK hook system (6 hook types)
- Logging infrastructure available (structured JSON logs)
- Policy requirements defined (which tools to restrict, audit retention)

## Task Breakdown

### C4.1 — Hook Infrastructure & Types

- **Description:** Define the hook handler types, the audit log format, the permission policy schema, and the hook registration pattern. This is the foundation that all specific hook implementations build on.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.2 (shared types)
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/hooks.ts` (initial — types + registration)
  - `src/copilot_bridge/tests/hooks.test.ts`
- **Acceptance criteria:**
  - `AuditEntry` type: `{ timestamp, sessionId, hookType, toolName?, input, output, elapsed }`
  - `PermissionPolicy` type: `{ allowedTools: string[], blockedTools: string[], askTools: string[], blockedPatterns: RegExp[] }`
  - `HookConfig` type: `{ auditLogPath: string, policy: PermissionPolicy, projectContext: string, maxRetries: number }`
  - `createHooks(config: HookConfig): SessionHooks` — factory that returns all 6 hooks wired to the config
  - `SessionHooks` matches the SDK's `hooks` shape: `{ onPreToolUse, onPostToolUse, onUserPromptSubmitted, onSessionStart, onSessionEnd, onErrorOccurred }`
  - Tests: verify hook factory creates all 6 hooks, verify types compile

### C4.2 — Pre-Tool-Use: Permission Enforcement

- **Description:** Implement the `onPreToolUse` hook with policy-based permission enforcement. Tools are allowed, denied, or require user confirmation based on the policy config. Arguments can be modified (e.g., restrict file paths to workspace).
- **Owner:** copilot-sdk-integration
- **Dependencies:** C4.1 (hook infrastructure)
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/hooks.ts` (extend — pre-tool-use implementation)
  - `src/copilot_bridge/tests/hooks.test.ts` (extend)
- **Acceptance criteria:**
  - `onPreToolUse` handler:
    - Checks `policy.blockedTools` → `deny` with reason
    - Checks `policy.blockedPatterns` against tool args → `deny` if match (e.g., `rm -rf`)
    - Checks `policy.askTools` → `ask` with descriptive reason
    - Checks `policy.allowedTools` → `allow` (if non-empty, acts as allowlist)
    - Default (no policy match) → `allow`
    - Modifies file path args to be relative to workspace (prevents escape)
  - Permission decisions logged to audit trail
  - Tests: verify allow/deny/ask for each policy type, verify path restriction, verify pattern matching

### C4.3 — Post-Tool-Use: Audit Logging & Result Filtering

- **Description:** Implement the `onPostToolUse` hook for comprehensive audit logging and optional result filtering (e.g., redact secrets, truncate large outputs).
- **Owner:** copilot-sdk-integration
- **Dependencies:** C4.1 (hook infrastructure)
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/hooks.ts` (extend — post-tool-use implementation)
  - `src/copilot_bridge/tests/hooks.test.ts` (extend)
- **Acceptance criteria:**
  - `onPostToolUse` handler:
    - Writes `AuditEntry` to JSON Lines file at `config.auditLogPath`
    - Includes: timestamp, tool name, args (sanitized), result length, elapsed time
    - Optional result modification: redact patterns matching `/(sk-|ghp_|password=)[^\s"]+/g`
    - Optional result truncation: cap at 10000 chars with `[truncated]` marker
    - Returns `null` when no modification needed (pass-through)
  - Audit log rotates daily (append to `audit-YYYY-MM-DD.jsonl`)
  - Tests: verify audit entries written, verify redaction, verify truncation, verify null pass-through

### C4.4 — Prompt & Session Lifecycle Hooks

- **Description:** Implement `onUserPromptSubmitted` (inject project context into every prompt), `onSessionStart` (initialize audit session, load project config), and `onSessionEnd` (finalize audit, cleanup).
- **Owner:** copilot-sdk-integration
- **Dependencies:** C4.1 (hook infrastructure)
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/hooks.ts` (extend — prompt + lifecycle hooks)
  - `src/copilot_bridge/tests/hooks.test.ts` (extend)
- **Acceptance criteria:**
  - `onUserPromptSubmitted`:
    - Injects `config.projectContext` as `additionalContext` (e.g., "This is a Python 3.12 project using FastAPI...")
    - Strips accidental credentials from prompt text
    - Logs prompt (sanitized) to audit trail
  - `onSessionStart`:
    - Creates audit session entry with timestamp and session ID
    - Loads project context from `package.json` or `pyproject.toml` in working dir
    - Returns `additionalContext` with project metadata
  - `onSessionEnd`:
    - Writes session summary to audit log (total tool calls, elapsed time, final status)
    - Flushes any buffered audit entries
  - Tests: verify context injection, verify credential stripping, verify session lifecycle entries

### C4.5 — Error Handling Hook

- **Description:** Implement `onErrorOccurred` with configurable retry/skip/abort strategies based on error type and recoverability.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C4.1 (hook infrastructure)
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/src/hooks.ts` (extend — error handling)
  - `src/copilot_bridge/tests/hooks.test.ts` (extend)
- **Acceptance criteria:**
  - `onErrorOccurred` handler:
    - Rate limit errors → `retry` with `retryCount: config.maxRetries` (default 3)
    - Network/transient errors (if `recoverable: true`) → `retry` with count 2
    - File not found / tool errors → `skip` (continue session, notify user)
    - Unrecoverable errors → `abort` with user notification
    - All errors logged to audit trail with full context
  - Tests: verify retry strategy for rate limits, verify skip for tool errors, verify abort for fatal errors

### C4.6 — Wire Hooks into CopilotBridge

- **Description:** Integrate the hooks into the existing CopilotBridge client so every session automatically gets hooks from config. Update `runTask` and `runTaskStreaming` to create sessions with hooks.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C4.2, C4.3, C4.4, C4.5 (all hooks implemented)
- **Complexity:** M
- **Files created/modified:**
  - `src/copilot_bridge/src/client.ts` (modify — add hooks to session creation)
  - `src/copilot_bridge/src/config.ts` (modify — add hook config fields)
  - `src/copilot_bridge/tests/client.test.ts` (extend)
- **Acceptance criteria:**
  - `BridgeConfig` extended with: `auditLogDir`, `permissionPolicy`, `projectContext`, `maxRetries`
  - `CopilotBridge.runTask()` passes `hooks: createHooks(config)` to session creation
  - Hooks are re-created per session (not shared across concurrent sessions)
  - Existing tests still pass (hooks optional — default to permissive)
  - New tests verify hooks fire during real session execution

## Parallel Execution Plan

```
── Time →

Agent A (copilot-sdk-integration):
  [C4.1 hook infrastructure] ──→ [C4.2 pre-tool-use permissions] ──→ [C4.6 wire into CopilotBridge]
           ~1.5h                           ~2h                                ~2h

Agent B (copilot-sdk-integration):
  ── wait for C4.1 ──────────→ [C4.3 post-tool-use audit] ──→ [C4.5 error handling] ──→ (feeds into C4.6)
                                        ~2h                          ~1h

Agent C (copilot-sdk-integration):
  ── wait for C4.1 ──────────→ [C4.4 prompt + lifecycle hooks] ──→ (feeds into C4.6)
                                        ~2h
```

**Parallelization:**
- C4.1 is the foundation — must complete first
- C4.2, C4.3, and C4.4 are fully independent hook implementations — 3-way parallel
- C4.5 can run alongside C4.4 (both are independent of C4.2/C4.3)
- C4.6 merges everything — needs all hooks done

## Integration Checkpoint

1. **Permissions enforced** — blocked tools return deny, ask tools prompt user
2. **Audit log written** — every tool call produces a JSON Lines entry
3. **Context injected** — project info appears in every session
4. **Credentials stripped** — accidental secrets in prompts redacted
5. **Error retry works** — rate limit triggers retry, skip and abort work correctly
6. **Audit rotation** — log files rotate daily
7. **Hooks transparent** — existing functionality unchanged when hooks are permissive

## Definition of Done

- [ ] Pre-tool-use hook enforces allow/deny/ask based on policy config
- [ ] Post-tool-use hook writes JSON Lines audit entries for every tool call
- [ ] Prompt hook injects project context and strips credentials
- [ ] Session lifecycle hooks create/finalize audit sessions
- [ ] Error hook implements retry/skip/abort with configurable strategy
- [ ] All hooks wired into CopilotBridge session creation
- [ ] Audit log files rotate daily at `{auditLogDir}/audit-YYYY-MM-DD.jsonl`
- [ ] Hooks are optional — default config is fully permissive (no breaking changes)
- [ ] Path restriction prevents Copilot from accessing files outside workspace
- [ ] 100% of existing tests pass (hooks don't break anything)

---

# Phase 5: Advanced Orchestration & Session Management

## Phase Goal

Implement multi-step task orchestration where OpenClaw breaks complex requests into sub-tasks, delegates each to separate Copilot SDK sessions (potentially in parallel), and synthesizes results. Add session persistence for long-running coding tasks that survive restarts. Integrate plan mode for structured multi-step workflows. Coordinate BYOK configuration between both platforms.

## Prerequisites

- Phase 4 complete: hooks, audit, and permissions working
- Stable CopilotBridge with hooks and MCP bridge
- Understanding of Copilot SDK session persistence (`~/.copilot/session-state/`)
- Understanding of Copilot SDK plan mode and workspace files

## Task Breakdown

### C5.1 — Task Decomposition Engine

- **Description:** Build a task decomposition engine that takes a high-level coding request (e.g., "Add authentication to the API") and breaks it into ordered, potentially parallelizable sub-tasks. Uses OpenClaw's agent to do the planning, then delegates each sub-task to Copilot.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C2.1 (plugin), C4.6 (hooks wired)
- **Complexity:** L
- **Files created:**
  - `src/copilot_bridge/src/orchestrator.ts`
  - `src/copilot_bridge/tests/orchestrator.test.ts`
- **Acceptance criteria:**
  - `TaskOrchestrator` class:
    - `async planTasks(description: string): Promise<TaskPlan>` — uses LLM (via OpenClaw or directly) to decompose into sub-tasks
    - `async executePlan(plan: TaskPlan): Promise<OrchestratedResult>` — executes sub-tasks in dependency order
  - `TaskPlan` type: `{ tasks: SubTask[]; dependencies: Map<string, string[]>; }` where `SubTask` has `id`, `description`, `workingDir`, `estimatedComplexity`
  - `OrchestratedResult`: `{ tasks: Array<{ id: string; result: CodingTaskResult; }>; totalElapsed: number; summary: string; }`
  - Dependency resolution: topological sort, tasks with no dependencies run in parallel
  - Progress tracking: emits events for each task start/complete/fail
  - Failure handling: if a task fails, skip dependents and report partial results
  - Tests: verify decomposition, verify dependency ordering, verify parallel execution, verify failure handling

### C5.2 — Parallel Session Pool

- **Description:** Manage a pool of Copilot SDK sessions for concurrent sub-task execution. Limit concurrency to avoid overwhelming the system. Handle session lifecycle (create, reuse, destroy) efficiently.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.4 (session runner), C4.6 (hooks)
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/orchestrator.ts` (extend — session pool)
  - `src/copilot_bridge/tests/orchestrator.test.ts` (extend)
- **Acceptance criteria:**
  - `SessionPool` class:
    - `constructor(bridge: CopilotBridge, maxConcurrency: number)` — default 3 concurrent sessions
    - `async acquire(): Promise<Session>` — get or create a session (waits if pool full)
    - `release(session: Session): void` — return session to pool (or destroy if tainted)
    - `async drain(): Promise<void>` — destroy all sessions (for shutdown)
  - Pool respects `maxConcurrency` — never more than N active sessions
  - Sessions reused across sub-tasks when possible (save creation overhead)
  - Tainted sessions (errors occurred) are destroyed, not reused
  - Tests: verify concurrency limit, verify acquire/release cycle, verify tainted session cleanup

### C5.3 — Session Persistence & Resume

- **Description:** Implement session persistence for long-running coding tasks. Save session state so tasks can resume after plugin restart, Gateway restart, or system reboot.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.4 (session runner)
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/client.ts` (extend — persistence methods)
  - `src/copilot_bridge/tests/client.test.ts` (extend)
- **Acceptance criteria:**
  - `CopilotBridge.runTask()` accepts optional `persistSession: true` flag
  - When persisted: session ID saved to `~/.copilot-bridge/sessions.json` with metadata (task description, start time, last activity)
  - `CopilotBridge.resumeTask(sessionId: string, prompt: string): Promise<CodingTaskResult>` — resumes a persisted session
  - `CopilotBridge.listPersistedSessions(): Promise<SessionMetadata[]>` — list all resumable sessions
  - `CopilotBridge.destroyPersistedSession(sessionId: string): Promise<void>` — cleanup
  - Stale session cleanup: sessions older than 24h auto-cleaned
  - Tests: verify save/resume/list/destroy cycle, verify stale cleanup

### C5.4 — OpenClaw Orchestration Plugin

- **Description:** Create an OpenClaw plugin `copilot_orchestrate` that accepts a complex task description, invokes the task decomposition engine, executes the plan, and returns a synthesized result with sub-task summaries.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C5.1 (orchestrator), C5.2 (session pool)
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/plugin.ts` (extend — add orchestration tool)
  - `src/copilot_bridge/tests/plugin.test.ts` (extend)
- **Acceptance criteria:**
  - `copilotOrchestrateTool`:
    - Parameters: `{ task: string (required), maxConcurrency?: number, timeout?: number }`
    - Returns structured result: task plan summary → per-task results → overall summary
    - Progress updates via streaming (if OpenClaw supports it)
  - Registered alongside `copilot_code` and `copilot_code_stream` in OpenClaw config
  - Agent persona updated to know when to use `copilot_orchestrate` vs `copilot_code`
  - Tests: mock orchestrator, verify tool schema, verify result formatting

### C5.5 — BYOK Coordination

- **Description:** Implement shared BYOK configuration so both OpenClaw and Copilot SDK can use the same provider keys without duplication. Create a unified config that feeds both platforms.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.1 (config)
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/src/config.ts` (extend — BYOK coordination)
  - `src/copilot_bridge/scripts/setup-byok.ts`
- **Acceptance criteria:**
  - `setup-byok.ts` script:
    - Reads existing OpenClaw config for provider keys
    - Maps to Copilot SDK BYOK format
    - Writes `.env` with coordinated config
    - Validates both platforms can authenticate with the same key
  - Supported providers: OpenAI, Anthropic, Azure OpenAI, Ollama
  - Documentation: which models work best for which tasks (coding vs conversation)
  - Tests: verify config mapping for each provider

### C5.6 — Orchestration End-to-End Test

- **Description:** Full orchestration test: complex multi-file task decomposed, sub-tasks executed in parallel, results synthesized.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C5.1, C5.2, C5.3, C5.4
- **Complexity:** L
- **Files created:**
  - `src/copilot_bridge/tests/integration/orchestration-e2e.test.ts`
- **Acceptance criteria:**
  - Test 1: "Add input validation to all API endpoints" → decomposes into per-file tasks → executes → returns combined results
  - Test 2: Resume interrupted orchestration — kill mid-task, resume, completes remaining sub-tasks
  - Test 3: Sub-task failure — one task fails, others complete, partial results returned with clear failure info
  - Test 4: Concurrent execution — verify max 3 sessions active simultaneously
  - Test 5: BYOK mode — orchestration works with alternative provider
  - Tests guarded by `COPILOT_INTEGRATION=1 && OPENCLAW_INTEGRATION=1`

## Parallel Execution Plan

```
── Time →

Agent A (copilot-sdk-integration):
  [C5.1 task decomposition] ──→ [C5.4 orchestration plugin] ──→ [C5.6 e2e test]
           ~4h                          ~2h                          ~3h

Agent B (copilot-sdk-integration):
  [C5.2 session pool] ──→ (feeds into C5.4) ──→ [C5.3 session persistence]
         ~2h                                           ~2h

Agent C (copilot-sdk-integration):
  [C5.5 BYOK coordination] ──→ (feeds into C5.6)
         ~1h
```

**Parallelization:**
- C5.1, C5.2, and C5.5 all start immediately in parallel (independent concerns)
- C5.3 can start after C5.2 or in parallel (independent of orchestrator)
- C5.4 merges C5.1 + C5.2 (needs both decomposition and session pool)
- C5.6 needs everything — final verification

## Integration Checkpoint

1. **Task decomposition** — complex request produces ordered sub-tasks with dependencies
2. **Parallel execution** — independent sub-tasks run concurrently (up to pool limit)
3. **Session persistence** — task survives plugin restart and resumes correctly
4. **Orchestration plugin** — `copilot_orchestrate` works from OpenClaw conversation
5. **Failure handling** — partial results returned when sub-tasks fail
6. **BYOK coordination** — shared provider keys work across both platforms
7. **Resource cleanup** — no leaked sessions after orchestration completes

## Definition of Done

- [ ] Complex tasks decompose into ordered sub-tasks with dependency graph
- [ ] Sub-tasks execute in parallel respecting concurrency limit (default 3)
- [ ] Session pool manages create/reuse/destroy lifecycle efficiently
- [ ] Persisted sessions survive plugin restart and resume correctly
- [ ] `copilot_orchestrate` tool registered with OpenClaw and works from conversation
- [ ] Partial results returned when some sub-tasks fail (with clear error reporting)
- [ ] BYOK config coordinated between OpenClaw and Copilot SDK
- [ ] Stale sessions auto-cleaned after 24h
- [ ] End-to-end orchestration test passes with real services
- [ ] Agent persona knows when to use orchestration vs single-task delegation
- [ ] No session leaks after 10+ orchestration runs

---

# Cross-Phase Dependency Map

```
Phase 1: SDK Bootstrap
  C1.1 ──┐
  C1.2 ──┤
         ├── C1.3 ── C1.4 ──┬── C1.5
         │                   └── C1.6
         │
Phase 2: Plugin Delegation
         ├── C2.1 ──┬── C2.2
         │          └── C2.3 ────── C2.5
         │   C2.4 ──────────────┘
         │
Phase 3: MCP Bridge
         ├── C3.1 ── C3.3 ────── C3.5
         │   C3.2 ── C3.4 ────┘
         │
Phase 4: Hooks & Audit
         ├── C4.1 ──┬── C4.2 ──┐
         │          ├── C4.3 ──┤
         │          ├── C4.4 ──┼── C4.6
         │          └── C4.5 ──┘
         │
Phase 5: Orchestration
         └── C5.1 ──┬── C5.4 ── C5.6
             C5.2 ──┘          ┘
             C5.3 ─────────────
             C5.5 ─────────────
```

**Phase boundaries are hard gates.** Each phase's "Definition of Done" must be verified before starting the next phase. Within each phase, parallelization is maximized per the execution plans above.

---

# Summary: Task Count & Estimates

| Phase | Tasks | Parallel Tracks | Estimated Total | Critical Path |
|-------|-------|-----------------|-----------------|---------------|
| 1: SDK Bootstrap | 6 | 2 | ~5h | C1.1 → C1.3 → C1.4 → C1.5 |
| 2: Plugin Delegation | 5 | 2 | ~5h | C2.1 → C2.2 → C2.5 |
| 3: MCP Bridge | 5 | 3 | ~6h | C3.1 → C3.3 → C3.5 |
| 4: Hooks & Audit | 6 | 3 | ~5.5h | C4.1 → C4.2 → C4.6 |
| 5: Orchestration | 6 | 3 | ~7h | C5.1 → C5.4 → C5.6 |
| **Total** | **28** | — | **~28.5h** | — |

With maximum parallelization across 2-3 agents per phase, wall-clock time for each phase is roughly 60% of total task hours.
