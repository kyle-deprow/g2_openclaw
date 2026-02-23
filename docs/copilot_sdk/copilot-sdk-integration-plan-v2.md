# OpenClaw + Copilot SDK Integration — Implementation Plan v2

## Changes from v1

All changes reference the review at `.archive/copilot-sdk-integration-review.md`.

| # | Review § | Change |
|---|----------|--------|
| 1 | §1.1 | Added `onPermissionRequest: () => ({ decision: "allow" })` to all session creation in Phases 1–3 |
| 2 | §1.2 | Moved BYOK `provider` config from client constructor to `createSession()` calls; added `provider` to `CodingTaskRequest` |
| 3 | §1.3 | Redesigned C3.1: deterministic tools via session RPC (`readFile`, `createFile`, `listFiles`), agent-mediated tool via `sendAndWait`; dropped fake `copilot_terminal`/`copilot_search` wrappers |
| 4 | §1.4 | Rewrote C2.1/C2.3 to use OpenClaw's actual plugin API (`~/.openclaw/plugins/`, `OpenClawPlugin` interface via jiti) |
| 5 | §1.5 | Added cycle detection task C3.6; made OpenClaw MCP server read-only; added max-depth guard |
| 6 | §2.1 | Removed explicit `client.start()`; use `client.ping()` + `client.getAuthStatus()` to trigger implicit connection |
| 7 | §2.2 | Added `streaming: true` to session config in `runTaskStreaming()` |
| 8 | §2.3 | Redesigned C2.2: returns single aggregated result with step-by-step log, not async iterator |
| 9 | §2.4 | Fixed MCP config: OpenClaw uses `command`+`args` (no transport field); Copilot SDK uses `type: "local"` |
| 10 | §2.5 | Added `getAuthStatus()` check before first session creation in `ensureReady()` |
| 11 | §2.6 | Renamed `COPILOT_AUTH_TOKEN` to `COPILOT_GITHUB_TOKEN`; documented SDK auto-detection chain |
| 12 | §2.7 | Added C1.7 — CI/CD, linting, formatting setup task |
| 13 | §2.8 | `stop()` checks returned `Error[]`, logs errors, falls back to `forceStop()` |
| 14 | §4.1 | Added C1.8 — SDK abstraction interface to isolate from Technical Preview API changes |
| 15 | §4.2 | Added C5.0 — concurrency spike before session pool to verify parallel execution |
| 16 | §4.3 | C3.4 uses shared persistent MCP server process instead of per-session spawning |
| 17 | §4.4 | Added session key strategy documentation to C5.1 task |
| 18 | §4.5 | C5.3 stores provider type/baseUrl in metadata, reads API keys from env on resume |
| 19 | §3.1 | Fixed cross-phase dependency map ASCII art |
| 20 | §3.2–3.5 | Re-estimated C3.1 (split into C3.1a/C3.1b), reassigned C3.2 to openclaw-development, corrected summary table |

---

## Progress Tracker

| Phase | Status | Tests | Date Completed |
|-------|--------|-------|----------------|
| 1: SDK Bootstrap | ✅ Complete (impl + review + fix) | 21 | 2026-02-22 |
| 2: Plugin Delegation | ✅ Complete (impl + review + fix) | 38 | 2026-02-22 |
| 3: MCP Bridge | ✅ Complete (impl + review + fix) | 123 | 2026-02-22 |
| 4: Hooks & Audit | ✅ Complete (impl + review + fix) | 203 | 2026-02-22 |
| 5: Orchestration | ✅ Complete (impl + review + fix) | 248 | 2026-02-22 |

**Current baseline:** 247 passing / 1 failing (pre-existing hooks backward-compat test), tsc clean, biome 10 errors + 63 warnings

### Known Issue
- `client.test.ts` > "default config produces permissive hooks (backward compatible)" — FAILS (`expected 'deny' to be 'allow'`). The `fail-closed` catch block in `onPreToolUse` returns deny when the audit logger fails to write (test environment has no audit dir). Pre-existing since Phase 4 hooks implementation.

---

## Overview

Integrate OpenClaw (AI assistant runtime, localhost:18789) with the GitHub Copilot SDK (JSON-RPC 2.0 agent runtime for code-aware tasks). OpenClaw orchestrates high-level planning, memory, and automation; Copilot SDK handles code-aware tool execution (file I/O, terminal, search). The integration progresses from SDK validation → plugin delegation → bidirectional MCP → hooks/audit → advanced orchestration.

**Key architectural constraint (Technical Preview):** The Copilot SDK is in Technical Preview — its API surface may change. All integration code uses a thin abstraction interface (`ICopilotSession`, `ICopilotClient`) so that SDK API changes only require updating the wrapper, not the entire integration layer.

### Source Code Location

```
src/copilot_bridge/                ← Node.js/TypeScript package
├── package.json
├── tsconfig.json
├── biome.json                     ← Linting + formatting config
├── .env.example
├── src/
│   ├── interfaces.ts              ← Abstraction interface (ICopilotClient, ICopilotSession)
│   ├── client.ts                  ← CopilotClient wrapper implementing interfaces
│   ├── plugin.ts                  ← OpenClaw plugin entry point (OpenClawPlugin)
│   ├── mcp-server.ts             ← MCP server exposing Copilot tools to OpenClaw
│   ├── mcp-openclaw.ts           ← MCP server exposing OpenClaw memory to Copilot (read-only)
│   ├── hooks.ts                   ← Hook implementations (audit, permissions, context)
│   ├── orchestrator.ts            ← Multi-step task orchestrator
│   ├── config.ts                  ← Unified config (env vars, dotenv)
│   ├── types.ts                   ← Shared TypeScript types
│   └── index.ts                   ← Package exports
├── tests/
│   ├── client.test.ts
│   ├── plugin.test.ts
│   ├── mcp-server.test.ts
│   ├── mcp-openclaw.test.ts
│   ├── hooks.test.ts
│   ├── orchestrator.test.ts
│   └── integration/
│       ├── sdk-smoke.test.ts
│       ├── plugin-e2e.test.ts
│       └── mcp-bridge.test.ts
├── scripts/
│   ├── validate-connection.ts     ← Connectivity check script
│   └── concurrency-spike.ts       ← Phase 5 prerequisite: parallel session test
└── .github/
    └── workflows/
        └── ci.yml                 ← GitHub Actions CI pipeline
```

### Agent/Owner Types

| Owner | Domain | Language |
|-------|--------|----------|
| `copilot-sdk-integration` | Copilot SDK client, sessions, tools, hooks, MCP servers | TypeScript |
| `openclaw-development` | OpenClaw config, plugins, MCP registration, agent setup, OpenClaw wire protocol | TypeScript/YAML |
| `backend-python` | Python-side utilities, test harnesses, config validation | Python |

---

# Phase 1: Copilot SDK Client Bootstrap

## Phase Goal

Get a CopilotClient running in a standalone Node.js service, authenticated against the Copilot CLI (or BYOK provider), able to create sessions, send prompts, and receive streamed responses. Includes an abstraction interface to isolate from SDK API changes. Pure SDK validation — no OpenClaw integration yet.

## Prerequisites

- Node.js ≥ 18 and npm installed
- GitHub Copilot CLI installed (`npm install -g @github/copilot-cli` or via `gh extension install github/gh-copilot`)
- GitHub authentication token OR a BYOK provider API key (OpenAI, Anthropic, etc.)
- No OpenClaw instance required for this phase

## Task Breakdown

### C1.1 — Project Scaffold & Config

- **Description:** Create the `copilot_bridge` TypeScript project with `package.json`, `tsconfig.json`, `biome.json`, and `config.ts`. Config loads from env vars and `.env` via `dotenv`. Define all config fields needed across all phases.
- **Owner:** copilot-sdk-integration
- **Dependencies:** None
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/package.json`
  - `src/copilot_bridge/tsconfig.json`
  - `src/copilot_bridge/biome.json`
  - `src/copilot_bridge/.env.example`
  - `src/copilot_bridge/src/config.ts`
  - `src/copilot_bridge/src/index.ts`
- **Acceptance criteria:**
  - `package.json` declares `@github/copilot-sdk` as dependency (pinned exact version), `vitest`, `typescript`, `@biomejs/biome` as dev deps
  - `tsconfig.json` targets ES2022, module NodeNext, strict mode
  - `config.ts` exports `loadConfig(): BridgeConfig` reading env vars with defaults:
    - `COPILOT_GITHUB_TOKEN` (optional — falls back to SDK's auto-detection chain: `GH_TOKEN` → `GITHUB_TOKEN` → stored CLI credentials)
    - `COPILOT_BYOK_PROVIDER` (optional — `openai | azure | anthropic | ollama`)
    - `COPILOT_BYOK_API_KEY` (optional)
    - `COPILOT_BYOK_BASE_URL` (optional)
    - `COPILOT_BYOK_MODEL` (optional)
    - `COPILOT_CLI_PATH` (optional — override CLI binary path)
    - `COPILOT_LOG_LEVEL` (default `info`)
    - `OPENCLAW_HOST` (default `127.0.0.1`)
    - `OPENCLAW_PORT` (default `18789`)
    - `OPENCLAW_GATEWAY_TOKEN` (optional for Phase 1)
  - Config comments document the SDK auth priority chain
  - `npm install` succeeds; `npx tsc --noEmit` passes; `npx biome check .` passes

### C1.2 — Shared Types

- **Description:** Define TypeScript types for the integration layer, including the `provider` field on task requests.
- **Owner:** copilot-sdk-integration
- **Dependencies:** None
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/src/types.ts`
- **Acceptance criteria:**
  - `ProviderConfig`: `{ type: "openai" | "azure" | "anthropic" | "ollama"; baseUrl?: string; apiKey?: string; model?: string; }`
  - `CodingTaskRequest`: `{ prompt: string; workingDir?: string; model?: string; provider?: ProviderConfig; tools?: string[]; sessionId?: string; timeout?: number; streaming?: boolean; }`
  - `CodingTaskResult`: `{ success: boolean; content: string; toolCalls: ToolCallRecord[]; errors: string[]; sessionId: string; elapsed: number; }`
  - `ToolCallRecord`: `{ tool: string; args: Record<string, unknown>; result: string; timestamp: number; }`
  - `StreamingDelta`: `{ type: "text" | "tool_start" | "tool_end" | "error" | "done"; content: string; tool?: string; }`
  - `BridgeError`: custom error class with `code`, `details`, `recoverable` fields
  - All types exported from `index.ts`

### C1.3 — CopilotClient Wrapper

- **Description:** Wrap `CopilotClient` from the SDK behind an abstraction interface. Support both GitHub auth and BYOK modes. BYOK config is stored for passing to sessions, NOT the client constructor. Connection triggered implicitly via `ping()` + `getAuthStatus()`.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.1 (config), C1.2 (types), C1.8 (interfaces)
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/client.ts`
  - `src/copilot_bridge/tests/client.test.ts`
- **Acceptance criteria:**
  - `CopilotBridge` class implementing `ICopilotClient`:
    - `constructor(config: BridgeConfig)` — stores config, creates `CopilotClient` with `{ githubToken, cliPath, logLevel, autoRestart: true }` — NO BYOK fields in constructor
    - `async ensureReady(): Promise<void>` — calls `client.ping("health")` to trigger implicit connection, then `client.getAuthStatus()` and verifies `status === "signed-in"` or BYOK active; throws `BridgeError` with clear message if auth fails
    - `async stop(): Promise<void>` — calls `const errors = await client.stop()`; if `errors.length > 0`, logs each error and calls `client.forceStop()` as fallback
    - `async isReady(): Promise<boolean>` — ping succeeds
    - `async getStatus(): Promise<{ connected: boolean; authMethod: string; }>`
  - BYOK config stored as `this.defaultProvider: ProviderConfig` — passed to sessions, not client
  - Auto-restart on crash (SDK default `autoRestart: true`)
  - Structured logging at configurable level
  - Tests: mock CopilotClient, verify `ensureReady()` calls `ping()` + `getAuthStatus()`, verify `stop()` handles error array, verify BYOK config NOT passed to constructor

### C1.4 — Session Runner

- **Description:** Implement session creation and prompt execution. Sessions include `onPermissionRequest: () => ({ decision: "allow" })` by default, `streaming: true` for streaming mode, and BYOK `provider` config from request or defaults.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.3 (client wrapper)
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/client.ts` (extend — add session methods)
  - `src/copilot_bridge/tests/client.test.ts` (extend)
- **Acceptance criteria:**
  - `CopilotBridge.runTask(request: CodingTaskRequest): Promise<CodingTaskResult>` — blocking:
    - Creates session with: `{ model, provider: request.provider ?? this.defaultProvider, onPermissionRequest: async () => ({ decision: "allow" }), streaming: false }`
    - Sends prompt via `sendAndWait()`
    - Collects response content and tool call records
    - Returns `CodingTaskResult` with timing info
    - Destroys session after (unless `sessionId` provided)
  - `CopilotBridge.runTaskStreaming(request: CodingTaskRequest): AsyncGenerator<StreamingDelta>` — streaming:
    - Creates session with `streaming: true` and `onPermissionRequest: async () => ({ decision: "allow" })`
    - Yields `StreamingDelta` events for `assistant.message_delta`, `tool.call_start`, `tool.call_end`
    - Yields `{ type: "done", content: "" }` at end
  - Timeout: wraps execution with configurable timeout (default 120s)
  - Error mapping: SDK errors → `BridgeError` with appropriate codes
  - Tests: mock session, verify `onPermissionRequest` always passed, verify `streaming: true` set for streaming mode, verify `provider` passed at session level

### C1.5 — Connectivity Validation Script

- **Description:** Standalone script that validates the full chain. Includes `getAuthStatus()` check before session creation.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.3, C1.4
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/scripts/validate-connection.ts`
- **Acceptance criteria:**
  - Runs via `npx tsx scripts/validate-connection.ts`
  - Steps: "Loading config..." → "Triggering connection (ping)..." → "Checking auth status..." → "Creating session..." → "Sending prompt..." → "Response: ..." → "Stopping client..."
  - Explicitly logs auth status result before session creation
  - Supports `--byok` flag for BYOK mode testing
  - Exit code 0 on success, 1 on failure with clear error message
  - Completes in < 30s

### C1.6 — SDK Smoke Integration Test

- **Description:** Integration tests that exercise the real Copilot SDK (not mocked). Skipped unless `COPILOT_INTEGRATION=1`.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.4, C1.5
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/tests/integration/sdk-smoke.test.ts`
- **Acceptance criteria:**
  - Test 1: `ensureReady()` succeeds, `isReady()` returns true
  - Test 2: `runTask` with simple prompt, verify non-empty response and `success: true`
  - Test 3: `runTaskStreaming` yields delta events (requires `streaming: true`)
  - Test 4: Timeout fires for complex prompt
  - Test 5: BYOK mode works (if `COPILOT_BYOK_*` configured)
  - Test 6: Permission handler fires — session with tools uses `onPermissionRequest`
  - All tests guarded by `describe.skipIf(!process.env.COPILOT_INTEGRATION)`

### C1.7 — CI/CD, Linting & Formatting

- **Description:** Set up automated code quality: Biome for linting + formatting, GitHub Actions CI pipeline, lint-staged for pre-commit.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.1 (scaffold)
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/.github/workflows/ci.yml`
  - `src/copilot_bridge/biome.json` (if not already in C1.1)
- **Acceptance criteria:**
  - CI workflow runs: `npx tsc --noEmit`, `npx biome check .`, `npx vitest run` on push/PR
  - Integration tests skipped in CI (no `COPILOT_INTEGRATION` set)
  - `package.json` scripts: `lint`, `format`, `test`, `test:integration`, `typecheck`
  - `npx biome check .` passes on all existing code

### C1.8 — SDK Abstraction Interface

- **Description:** Define TypeScript interfaces that abstract the Copilot SDK's API surface. All integration code depends on these interfaces, not the SDK directly. Only `client.ts` imports from `@github/copilot-sdk`.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.2 (types)
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/src/interfaces.ts`
- **Acceptance criteria:**
  - `ICopilotClient`: `ensureReady()`, `stop()`, `isReady()`, `getStatus()`, `createSession(config)`, `ping(msg)`
  - `ICopilotSession`: `sendAndWait(prompt)`, `onEvent(type, cb)`, `destroy()`, `rpc`
  - `IPermissionHandler`: `(request: { toolName, toolArgs, description }) => Promise<{ decision: "allow" | "deny" | "ask"; reason?: string }>`
  - `IProviderConfig`: mirrors SDK's `ProviderConfig` shape
  - All integration code (plugin, MCP, orchestrator) imports from `interfaces.ts`, never from `@github/copilot-sdk` directly
  - Tests: verify `CopilotBridge` implements `ICopilotClient`

## Parallel Execution Plan

```
── Time →

Agent A (copilot-sdk-integration):
  [C1.1 scaffold] → [C1.3 client wrapper] → [C1.4 session runner] → [C1.5 validate script]
      ~0.5h                ~2h                      ~2h                     ~0.5h

Agent B (copilot-sdk-integration):
  [C1.2 types] ─┬→ [C1.8 abstraction interface] → (feeds into C1.3)
      ~0.5h     │           ~1h
                └→ [C1.7 CI/CD setup] ────────────────────────────→ [C1.6 integration tests]
                        ~1h                                                ~1.5h
```

**Parallelization:**
- C1.1 and C1.2 start simultaneously
- C1.8 and C1.7 can also start once C1.2 is done (parallel track B)
- C1.3 merges C1.1 + C1.2 + C1.8
- C1.4 depends on C1.3
- C1.5 and C1.6 can run in parallel once C1.4 is done

## Integration Checkpoint

1. **`npm install` and `npx tsc --noEmit`** — compiles
2. **`npx biome check .`** — no lint/format issues
3. **`npm test`** — all unit tests pass (mocked)
4. **`validate-connection.ts`** — logs auth status, creates session, gets response
5. **Streaming mode** — deltas arrive incrementally (verified `streaming: true`)
6. **Permission handler** — sessions include `onPermissionRequest` (verified in tests)
7. **BYOK config** — passed at session level, not client constructor
8. **`stop()` error handling** — errors logged, `forceStop()` fallback works

## Definition of Done

- [x] `npm install` succeeds with pinned SDK version
- [x] `npx tsc --noEmit` and `npx biome check .` pass
- [x] `npm test` — all unit tests pass
- [x] `validate-connection.ts` completes with auth status check
- [x] Streaming mode yields incremental deltas (`streaming: true` verified)
- [x] Every `createSession()` includes `onPermissionRequest`
- [x] BYOK `provider` config passed in session creation, not client constructor
- [x] `stop()` handles `Error[]` return, falls back to `forceStop()`
- [x] Abstraction interface isolates SDK imports to `client.ts` only
- [x] CI workflow runs typecheck + lint + tests

---

# Phase 2: OpenClaw Plugin — Basic Delegation

## Phase Goal

Build an OpenClaw plugin using the actual `OpenClawPlugin` interface that registers `copilot_code` and `copilot_code_verbose` tools. The plugin is discovered from `~/.openclaw/plugins/copilot-bridge/`. Text-in, text-out — the simplest useful integration.

## Prerequisites

- Phase 1 complete
- OpenClaw Gateway running on localhost:18789 with `OPENCLAW_GATEWAY_TOKEN` set
- `openclaw gateway status` shows healthy
- Understanding of OpenClaw plugin API: `~/.openclaw/plugins/`, `OpenClawPlugin` interface, jiti loading

## Task Breakdown

### C2.1 — OpenClaw Plugin Entry Point

- **Description:** Create the OpenClaw plugin module conforming to the `OpenClawPlugin` interface. Registers a `copilot_code` tool. Plugin discovered from `~/.openclaw/plugins/copilot-bridge/`.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.4 (session runner)
- **Complexity:** L
- **Files created:**
  - `src/copilot_bridge/src/plugin.ts`
  - `src/copilot_bridge/tests/plugin.test.ts`
- **Acceptance criteria:**
  - Exports default `OpenClawPlugin` object:
    ```typescript
    export default {
      name: 'copilot-bridge',
      version: '1.0.0',
      tools: [copilotCodeTool, copilotCodeVerboseTool],
      async onLoad(api) { /* init CopilotBridge */ }
    } satisfies OpenClawPlugin;
    ```
  - `copilotCodeTool`:
    - `name: "copilot_code"`, parameters: `{ task: string (required), workingDir?: string, model?: string, timeout?: number }`
    - `async execute({ task, workingDir, model, timeout })` → creates/reuses shared CopilotBridge, calls `runTask()`, formats result as markdown (code blocks for code, tool call summary, timing)
    - Returns `{ result: "..." }` — single string result matching OpenClaw's plugin API
  - Error handling: returns human-readable error message in `result`, never throws
  - Logs task start/end with timing
  - Tests: mock CopilotBridge, verify `OpenClawPlugin` shape, verify result formatting, verify error returns

### C2.2 — Verbose Variant Plugin (Step-by-Step Log)

- **Description:** Create `copilot_code_verbose` that runs the task in streaming mode internally but returns a single aggregated result with a step-by-step execution log — showing which tools Copilot called and what they returned.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C2.1
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/plugin.ts` (extend)
  - `src/copilot_bridge/tests/plugin.test.ts` (extend)
- **Acceptance criteria:**
  - `copilotCodeVerboseTool` with same parameters as `copilot_code`
  - Internally uses `runTaskStreaming()` to collect all events
  - Returns single `{ result: "..." }` containing:
    ```
    ## Execution Log
    1. 🔧 Called `read_file` (src/main.ts) — 245 chars
    2. 🔧 Called `write_file` (src/main.ts) — 312 chars
    3. ✅ Complete (4.2s)

    ## Result
    [final LLM response text]
    ```
  - NO async iterator return — single string, per OpenClaw's plugin `execute()` contract
  - Tests: mock streaming session, verify aggregated log format

### C2.3 — Plugin Registration & Discovery

- **Description:** Set up the plugin directory structure for OpenClaw discovery. Create symlink/copy script and validation.
- **Owner:** openclaw-development
- **Dependencies:** C2.1 (plugin exists)
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/scripts/register-plugin.ts`
- **Acceptance criteria:**
  - Script creates `~/.openclaw/plugins/copilot-bridge/` directory
  - Symlinks (or copies) `src/copilot_bridge/src/plugin.ts` as `index.ts` in the plugin directory
  - Validates: check `~/.openclaw/plugins/copilot-bridge/index.ts` exists and is valid TypeScript
  - Logs instructions: "Plugin registered. Restart Gateway: `openclaw gateway restart`"
  - After Gateway restart, `openclaw agent --message "List your tools"` shows `copilot_code` and `copilot_code_verbose`

### C2.4 — OpenClaw Agent Persona Snippet

- **Description:** Persona snippet teaching the agent when/how to use `copilot_code`.
- **Owner:** openclaw-development
- **Dependencies:** C2.1 (tool API known)
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/openclaw-persona-snippet.md`
- **Acceptance criteria:**
  - 50-100 line markdown snippet for inclusion in `USER.md` or system prompt
  - Decision tree: when to delegate (code gen, refactoring, file editing) vs. handle directly (conversation, planning, memory queries)
  - How to write effective task descriptions (include file paths, language, constraints)
  - When to use `copilot_code` vs `copilot_code_verbose`
  - Error recovery guidance
  - Example prompts

### C2.5 — Plugin End-to-End Test

- **Description:** E2E test with real OpenClaw and Copilot SDK.
- **Owner:** openclaw-development
- **Dependencies:** C2.1, C2.3, C2.4
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/tests/integration/plugin-e2e.test.ts`
- **Acceptance criteria:**
  - Test 1: "Write a Python function that reverses a string" → response contains Python function
  - Test 2: "Refactor this code: [paste]" → response contains refactored code
  - Test 3: "What is the capital of France?" → agent does NOT use `copilot_code`
  - Test 4: Trigger timeout → error in result, OpenClaw continues normally
  - Test 5: `copilot_code_verbose` returns step-by-step log
  - Tests guarded by `COPILOT_INTEGRATION=1 && OPENCLAW_INTEGRATION=1`

## Parallel Execution Plan

```
── Time →

Agent A (copilot-sdk-integration):
  [C2.1 plugin entry point] ──→ [C2.2 verbose variant] ──────────────→ [C2.5 e2e test]
           ~3h                          ~2h                                  ~2h

Agent B (openclaw-development):
  [C2.4 persona snippet] ──→ [C2.3 plugin registration] ──→ (feeds into C2.5)
           ~1h                        ~1h
```

## Integration Checkpoint

1. **Plugin discovered by OpenClaw** — `copilot_code` in tool list after Gateway restart
2. **Basic delegation** — "Write hello world in Python" → returns Python code
3. **Verbose mode** — `copilot_code_verbose` shows tool call execution log
4. **Error handling** — timeout/crash produces recoverable error in result string
5. **Agent decision-making** — non-coding tasks handled directly
6. **Sequential tasks** — multiple delegations in one conversation work

## Definition of Done

- [x] Plugin at `~/.openclaw/plugins/copilot-bridge/index.ts` discovered by Gateway
- [x] `copilot_code` delegates and returns formatted code
- [x] `copilot_code_verbose` returns step-by-step log
- [x] Errors handled gracefully (single result string, never crashes Gateway)
- [x] Persona snippet guides delegation decisions
- [x] E2E test passes with real services
- [x] No resource leaks after 10+ delegations

---

# Phase 3: MCP Bridge — Bidirectional Tool Access

## Phase Goal

Build a bidirectional MCP bridge with two tiers: (a) deterministic tools via session RPC (`readFile`, `createFile`, `listFiles`) and agent-mediated tool via `sendAndWait`, exposed as an MCP server for OpenClaw; (b) read-only OpenClaw memory tools exposed as an MCP server for Copilot sessions. Includes cycle detection to prevent infinite loops.

## Prerequisites

- Phase 2 complete
- MCP protocol understanding (stdio transport)
- OpenClaw MCP config: `mcp.servers` with `command` + `args`
- Copilot SDK MCP config: `mcpServers` with `type: "local"`

## Task Breakdown

### C3.1a — Copilot MCP Server: Deterministic Tools

- **Description:** Build the first tier of the MCP server (stdio): deterministic tools that use session RPC methods directly, providing reliable results without LLM mediation.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.4 (session runner)
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/mcp-server.ts`
  - `src/copilot_bridge/tests/mcp-server.test.ts`
- **Acceptance criteria:**
  - MCP server starts via stdio (`node src/mcp-server.ts`)
  - Deterministic tools (session RPC-backed):
    - `copilot_read_file` → `session.rpc["workspace.readFile"]({ path })` — returns file contents
    - `copilot_create_file` → `session.rpc["workspace.createFile"]({ path, content })` — creates/overwrites file
    - `copilot_list_files` → `session.rpc["workspace.listFiles"]()` — returns file listing
  - Each tool has JSON Schema parameter definitions
  - Server maintains persistent CopilotBridge + session (lazy init on first tool call)
  - Session includes `onPermissionRequest: () => ({ decision: "allow" })`
  - Clean shutdown on SIGTERM/SIGINT
  - Tests: mock session RPC, verify tool discovery, verify deterministic results

### C3.1b — Copilot MCP Server: Agent-Mediated Tool

- **Description:** Add the agent-mediated tier: a `copilot_code_task` tool that wraps `sendAndWait()` for complex, non-deterministic tasks. Clearly document that this is LLM-mediated.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C3.1a
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/mcp-server.ts` (extend)
  - `src/copilot_bridge/tests/mcp-server.test.ts` (extend)
- **Acceptance criteria:**
  - `copilot_code_task` tool:
    - Parameters: `{ prompt: string, workingDir?: string, timeout?: number }`
    - Wraps `bridge.runTask()` — sends natural language prompt, returns full response
    - Tool description explicitly states: "Agent-mediated. Results are non-deterministic."
  - Concurrent tool calls from OpenClaw are queued (one session at a time)
  - Total exposed tools: 4 (3 deterministic + 1 agent-mediated)
  - Tests: verify agent-mediated tool returns LLM response, verify queuing

### C3.2 — OpenClaw Memory MCP Server (Read-Only)

- **Description:** Build an MCP server exposing OpenClaw's memory/context as **read-only** tools for Copilot SDK sessions. No agent-triggering tools — only direct data lookups.
- **Owner:** openclaw-development
- **Dependencies:** OpenClaw wire protocol knowledge
- **Complexity:** L
- **Files created:**
  - `src/copilot_bridge/src/mcp-openclaw.ts`
  - `src/copilot_bridge/tests/mcp-openclaw.test.ts`
- **Acceptance criteria:**
  - MCP server starts via stdio (`node src/mcp-openclaw.ts`)
  - Read-only tools:
    - `openclaw_memory_search` — vector search via OpenClaw `memory_search` tool (WebSocket RPC call)
    - `openclaw_memory_read` — read MEMORY.md or daily memory files (file read, no agent call)
    - `openclaw_user_prefs` — read USER.md (file read, no agent call)
  - **No `openclaw_context` tool** — removed to prevent agent-triggering cycles
  - Connects to OpenClaw Gateway WebSocket using `OPENCLAW_GATEWAY_TOKEN`
  - Handles disconnects with reconnection (exponential backoff)
  - Tests: mock OpenClaw WebSocket, verify read-only tool set, verify no agent-triggering tools

### C3.3 — OpenClaw MCP Registration

- **Description:** Register the Copilot MCP server (C3.1a/b) with OpenClaw.
- **Owner:** openclaw-development
- **Dependencies:** C3.1b
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/scripts/register-mcp.ts`
  - `src/copilot_bridge/openclaw-mcp-config.json`
- **Acceptance criteria:**
  - `openclaw-mcp-config.json` uses correct OpenClaw format (no `transport` field):
    ```json
    { "mcp": { "servers": {
      "copilot": {
        "command": "node",
        "args": ["src/copilot_bridge/src/mcp-server.ts"]
      }
    }}}
    ```
  - `register-mcp.ts` merges into `~/.openclaw/config` under `mcp.servers`
  - After Gateway restart, `copilot_read_file`, `copilot_create_file`, `copilot_list_files`, `copilot_code_task` appear as tools
  - Validation: `openclaw agent --message "List your tools"` shows copilot tools

### C3.4 — Copilot Session MCP Config (Shared Persistent Server)

- **Description:** Configure Copilot SDK sessions to connect to a **shared, persistent** OpenClaw MCP server process (not per-session).
- **Owner:** copilot-sdk-integration
- **Dependencies:** C3.2
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/src/client.ts` (extend — MCP config)
  - `src/copilot_bridge/tests/client.test.ts` (extend)
- **Acceptance criteria:**
  - `CopilotBridge` starts the OpenClaw MCP server once at init (persistent process)
  - Sessions created with `mcpServers` using correct SDK format:
    ```typescript
    mcpServers: {
      openclaw: {
        type: "local",
        command: "node",
        args: ["src/copilot_bridge/src/mcp-openclaw.ts"]
      }
    }
    ```
  - MCP server process shared across all sessions (not spawned per session)
  - Process lifecycle tied to `CopilotBridge` lifecycle (starts at `ensureReady`, stops at `stop()`)
  - Tests: verify `type: "local"` in config, verify single process for multiple sessions

### C3.5 — MCP Bridge Integration Test

- **Description:** End-to-end bidirectional test.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C3.1b, C3.2, C3.3, C3.4, C3.6
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/tests/integration/mcp-bridge.test.ts`
- **Acceptance criteria:**
  - Test 1: OpenClaw → `copilot_read_file` → deterministic file read → result returned
  - Test 2: OpenClaw → `copilot_code_task` → agent-mediated response → result returned
  - Test 3: Copilot session → `openclaw_memory_search` → search results available
  - Test 4: Combined — OpenClaw delegates task, Copilot reads memory mid-task
  - Test 5: MCP server crash recovery — next tool call works after restart
  - Test 6: Cycle detection — verify max-depth guard prevents infinite loops
  - Tests guarded by `COPILOT_INTEGRATION=1 && OPENCLAW_INTEGRATION=1`

### C3.6 — Cycle Detection & Depth Limiting

- **Description:** Implement safeguards against infinite call loops in the bidirectional MCP bridge. Pass a depth counter through MCP tool calls; reject calls exceeding max depth.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C3.1a, C3.2
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/src/mcp-server.ts` (extend — depth tracking)
  - `src/copilot_bridge/src/mcp-openclaw.ts` (extend — depth tracking)
- **Acceptance criteria:**
  - Both MCP servers accept optional `_depth: number` parameter on every tool call
  - On incoming call: if `_depth >= 3`, return error: "Maximum call depth exceeded (cycle detected)"
  - On outgoing call (MCP server → Copilot or OpenClaw): increment `_depth` by 1
  - Default `_depth` = 0 when called by the primary orchestrator
  - OpenClaw MCP server is read-only (no agent calls) — cycle prevention by design
  - Tests: simulate depth=3 call, verify rejection; verify normal calls at depth 0-2 succeed

## Parallel Execution Plan

```
── Time →

Agent A (copilot-sdk-integration):
  [C3.1a deterministic MCP] → [C3.1b agent-mediated] → [C3.4 session MCP cfg] → [C3.5 integration]
          ~2.5h                       ~2h                      ~1h                     ~2h

Agent B (openclaw-development):
  [C3.2 OpenClaw memory MCP] ──────────────────────→ [C3.3 OpenClaw MCP registration]
          ~4h                                                 ~1h

Agent C (copilot-sdk-integration):
  ── wait for C3.1a + C3.2 ──→ [C3.6 cycle detection] ──→ (feeds into C3.5)
                                       ~1h
```

**Parallelization:**
- C3.1a and C3.2 start in parallel (independent MCP servers)
- C3.1b depends on C3.1a; C3.3 depends on C3.1b
- C3.6 can start once C3.1a and C3.2 skeletons exist (needs both to add depth tracking)
- C3.4 depends on C3.2
- C3.5 needs everything

## Integration Checkpoint

1. **OpenClaw sees 4 Copilot tools** — 3 deterministic + 1 agent-mediated
2. **Deterministic tools work** — `copilot_read_file` returns exact file contents
3. **Agent-mediated tool works** — `copilot_code_task` returns LLM response
4. **Read-only OpenClaw tools work** — `openclaw_memory_search` returns results
5. **No `openclaw_context`** — no agent-triggering tools exposed
6. **Cycle detection** — depth ≥ 3 rejected with clear error
7. **Shared MCP server** — one process for all sessions, not per-session spawn
8. **MCP config formats correct** — OpenClaw uses `command`+`args`, SDK uses `type: "local"`

## Definition of Done

- [x] Copilot MCP server exposes 4 tools (3 deterministic, 1 agent-mediated)
- [x] OpenClaw MCP server exposes 3 read-only tools (no agent-triggering)
- [x] `copilot_read_file` returns deterministic file contents via session RPC
- [x] `copilot_code_task` returns LLM response via `sendAndWait`
- [x] `openclaw_memory_search` queries OpenClaw memory successfully
- [x] Cycle detection rejects depth ≥ 3 calls
- [x] Shared MCP server process — no per-session spawning
- [x] MCP servers handle graceful shutdown (SIGTERM)
- [x] Bidirectional test passes
- [x] No zombie processes after 10+ tool call cycles

---

# Phase 4: Hooks, Audit & Permission Control

## Phase Goal

Implement the Copilot SDK hook system. Phase 1–3 used a permissive `allow-all` permission handler. Phase 4 upgrades this to policy-based permission enforcement, comprehensive audit logging, project context injection, and configurable error handling (retry/skip/abort).

## Prerequisites

- Phase 3 complete
- Understanding of Copilot SDK hook system (6 hook types from `copilot-sdk-hooks` skill)
- Policy requirements defined

## Task Breakdown

### C4.1 — Hook Infrastructure & Types

- **Description:** Define hook handler types, audit log format, permission policy schema, and the factory that produces all 6 hooks.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.2 (types), C1.8 (interfaces)
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/hooks.ts`
  - `src/copilot_bridge/tests/hooks.test.ts`
- **Acceptance criteria:**
  - `AuditEntry`: `{ timestamp, sessionId, hookType, toolName?, input, output, elapsed }`
  - `PermissionPolicy`: `{ allowedTools: string[], blockedTools: string[], askTools: string[], blockedPatterns: RegExp[] }`
  - `HookConfig`: `{ auditLogDir: string, policy: PermissionPolicy, projectContext: string, maxRetries: number }`
  - `createHooks(config: HookConfig): SessionHooks` — factory returning all 6 hooks
  - Hooks match SDK shape: `{ onPreToolUse, onPostToolUse, onUserPromptSubmitted, onSessionStart, onSessionEnd, onErrorOccurred }`
  - These hooks **replace** the simple `onPermissionRequest` used in Phases 1–3 (the pre-tool-use hook now handles permissions)
  - Tests: verify factory creates all 6 hooks, verify type compatibility

### C4.2 — Pre-Tool-Use: Permission Enforcement

- **Description:** Policy-based permission enforcement replacing the allow-all handler from Phases 1–3.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C4.1
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/hooks.ts` (extend)
  - `src/copilot_bridge/tests/hooks.test.ts` (extend)
- **Acceptance criteria:**
  - `onPreToolUse` handler:
    - Checks `blockedTools` → `permissionDecision: "deny"` with reason
    - Checks `blockedPatterns` against serialized tool args → `deny` if match
    - Checks `askTools` → `permissionDecision: "ask"` with reason
    - Checks `allowedTools` (if non-empty, acts as allowlist) → `allow`
    - Default → `permissionDecision: "allow"`
    - Modifies file path args to be relative to workspace (prevents path traversal)
  - All decisions logged to audit trail
  - Tests: verify each policy type, path restriction, pattern matching

### C4.3 — Post-Tool-Use: Audit Logging & Result Filtering

- **Description:** Comprehensive audit logging and optional result redaction/truncation.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C4.1
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/hooks.ts` (extend)
  - `src/copilot_bridge/tests/hooks.test.ts` (extend)
- **Acceptance criteria:**
  - `onPostToolUse` handler:
    - Writes `AuditEntry` as JSON Lines to `{auditLogDir}/audit-YYYY-MM-DD.jsonl`
    - Redacts patterns: `/(sk-|ghp_|gho_|password\s*=\s*")[^\s"]+/g` → `[REDACTED]`
    - Truncates results > 10000 chars with `[truncated]` marker
    - Returns `null` when no modification needed
  - Daily log rotation (new file per day)
  - Tests: verify audit entries, redaction, truncation, null pass-through

### C4.4 — Prompt & Session Lifecycle Hooks

- **Description:** Context injection into prompts and session lifecycle tracking.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C4.1
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/hooks.ts` (extend)
  - `src/copilot_bridge/tests/hooks.test.ts` (extend)
- **Acceptance criteria:**
  - `onUserPromptSubmitted`: injects `config.projectContext` as `additionalContext`, strips credentials from prompt, logs sanitized prompt
  - `onSessionStart`: creates audit session entry, loads project context from `package.json`/`pyproject.toml` if present, returns `additionalContext`
  - `onSessionEnd`: writes session summary (total tool calls, elapsed, final status), flushes buffered entries
  - Tests: verify context injection, credential stripping, lifecycle entries

### C4.5 — Error Handling Hook

- **Description:** Configurable retry/skip/abort error handling.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C4.1
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/src/hooks.ts` (extend)
  - `src/copilot_bridge/tests/hooks.test.ts` (extend)
- **Acceptance criteria:**
  - `onErrorOccurred`:
    - Rate limit → `errorHandling: "retry"`, `retryCount: config.maxRetries` (default 3)
    - Transient + `recoverable: true` → `retry`, count 2
    - Tool errors (file not found) → `errorHandling: "skip"`
    - Unrecoverable → `errorHandling: "abort"`, `userNotification: "..."`
    - All errors logged to audit trail
  - Tests: verify each strategy

### C4.6 — Wire Hooks into CopilotBridge

- **Description:** Replace the allow-all `onPermissionRequest` from Phases 1–3 with the full hook system.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C4.2, C4.3, C4.4, C4.5
- **Complexity:** M
- **Files created/modified:**
  - `src/copilot_bridge/src/client.ts` (modify)
  - `src/copilot_bridge/src/config.ts` (modify — add hook config)
  - `src/copilot_bridge/tests/client.test.ts` (extend)
- **Acceptance criteria:**
  - `BridgeConfig` extended with: `auditLogDir`, `permissionPolicy`, `projectContext`, `maxRetries`
  - `CopilotBridge.runTask()` passes `hooks: createHooks(config)` to session creation
  - Hooks replace the `onPermissionRequest: () => allow` from Phases 1–3
  - Hooks re-created per session (not shared)
  - Default config (no policy) is fully permissive — backward compatible
  - All Phase 1–3 tests still pass

## Parallel Execution Plan

```
── Time →

Agent A (copilot-sdk-integration):
  [C4.1 hook infrastructure] ──→ [C4.2 pre-tool-use] ──→ [C4.6 wire into bridge]
           ~1.5h                        ~2h                       ~2h

Agent B (copilot-sdk-integration):
  ── wait for C4.1 ──────────→ [C4.3 post-tool-use audit] ──→ [C4.5 error handling]
                                        ~2h                          ~1h

Agent C (copilot-sdk-integration):
  ── wait for C4.1 ──────────→ [C4.4 prompt + lifecycle hooks]
                                        ~2h
```

## Integration Checkpoint

1. **Permissions enforced** — blocked tools denied, ask tools prompt
2. **Audit log written** — JSON Lines entry per tool call
3. **Context injected** — project info in every session
4. **Credentials stripped** — secrets redacted from prompts
5. **Error retry** — rate limits trigger retry; abort works
6. **Backward compatible** — default config = permissive, Phase 1–3 tests pass

## Definition of Done

- [x] Pre-tool-use enforces allow/deny/ask per policy
- [x] Post-tool-use writes daily-rotating audit logs
- [x] Prompt hook injects context, strips credentials
- [x] Session lifecycle hooks track start/end
- [x] Error hook implements retry/skip/abort
- [x] Hooks wired into every session via `createHooks()`
- [x] Default config is fully permissive (no breaking changes)
- [x] Path restriction prevents workspace escape (including symlink defence)
- [x] All previous tests still pass (203 total)

---

# Phase 5: Advanced Orchestration & Session Management

## Phase Goal

Multi-step task orchestration, parallel session pool with verified concurrency, session persistence, and BYOK coordination. Preceded by a concurrency spike to validate parallel session execution on a single CLI process.

## Prerequisites

- Phase 4 complete
- Concurrency spike (C5.0) results reviewed
- Understanding of SDK session persistence

## Task Breakdown

### C5.0 — Concurrency Spike: Parallel Session Verification

- **Description:** Before building the session pool, verify whether multiple concurrent sessions on a single `CopilotClient` actually execute in parallel or are serialized. This determines the pool design.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.4 (session runner)
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/scripts/concurrency-spike.ts`
- **Acceptance criteria:**
  - Script creates 3 sessions on one CopilotClient
  - Sends a long-running prompt to each simultaneously (e.g., "Count slowly to 10, pausing between each number")
  - Measures: wall-clock time for all 3 vs. single session time
  - If parallel: 3 sessions ≈ 1x single session time → use single-client pool
  - If serialized: 3 sessions ≈ 3x single session time → pool needs multiple `CopilotClient` instances
  - Outputs: "RESULT: parallel" or "RESULT: serialized" with timing data
  - Results documented in a brief findings file for team reference

### C5.1 — Task Decomposition Engine

- **Description:** Breaks high-level coding requests into ordered, parallelizable sub-tasks. Includes session key strategy documentation for OpenClaw integration.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C2.1 (plugin), C4.6 (hooks), C5.0 (spike results)
- **Complexity:** L
- **Files created:**
  - `src/copilot_bridge/src/orchestrator.ts`
  - `src/copilot_bridge/tests/orchestrator.test.ts`
- **Acceptance criteria:**
  - `TaskOrchestrator` class:
    - `planTasks(description: string): Promise<TaskPlan>` — LLM-based decomposition
    - `executePlan(plan: TaskPlan): Promise<OrchestratedResult>` — executes in dependency order
  - `TaskPlan`: `{ tasks: SubTask[], dependencies: Map<string, string[]> }`
  - Topological sort for dependency resolution; independent tasks run in parallel
  - Progress events for each task start/complete/fail
  - Failure handling: skip dependents, report partial results
  - **Session key strategy documented**: each sub-task uses unique OpenClaw session key (`agent:claw:copilot-task:<taskId>`) for parallel execution; shared context loaded via memory tools, not shared sessions
  - Tests: verify decomposition, ordering, parallel execution, failure handling

### C5.2 — Parallel Session Pool

- **Description:** Manage concurrent Copilot sessions. Design adapts based on C5.0 spike results (single-client pool vs. multi-client pool).
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.4, C4.6, C5.0 (spike results)
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/orchestrator.ts` (extend)
  - `src/copilot_bridge/tests/orchestrator.test.ts` (extend)
- **Acceptance criteria:**
  - `SessionPool` class:
    - `constructor(bridge: CopilotBridge, maxConcurrency: number)` — default 3
    - `acquire(): Promise<Session>` — get or create (waits if pool full)
    - `release(session): void` — return to pool (destroy if tainted)
    - `drain(): Promise<void>` — destroy all
  - If C5.0 shows serialized: pool manages multiple `CopilotClient` instances (one per concurrent slot)
  - If C5.0 shows parallel: pool shares one client, creates multiple sessions
  - Tainted sessions (errors) destroyed, not reused
  - Tests: verify concurrency limit, acquire/release, tainted cleanup

### C5.3 — Session Persistence & Resume

- **Description:** Save/resume sessions for long-running tasks. BYOK provider type stored in metadata, API key from env on resume.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.4
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/client.ts` (extend)
  - `src/copilot_bridge/tests/client.test.ts` (extend)
- **Acceptance criteria:**
  - `runTask()` accepts `persistSession: true`
  - Metadata saved to `~/.copilot-bridge/sessions.json`: `{ sessionId, task, startTime, lastActivity, providerType?, providerBaseUrl? }`
  - `resumeTask(sessionId, prompt)` — resumes session; reconstructs `provider` config from metadata + current env vars (API key NOT stored in metadata)
  - `listPersistedSessions()`, `destroyPersistedSession(sessionId)`
  - Stale cleanup: sessions > 24h auto-removed
  - Re-provides `onPermissionRequest` and hooks on resume (not persisted by SDK)
  - Tests: save/resume/list/destroy cycle, BYOK resume, stale cleanup, hooks on resume

### C5.4 — OpenClaw Orchestration Plugin

- **Description:** `copilot_orchestrate` tool for complex multi-file tasks.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C5.1, C5.2
- **Complexity:** M
- **Files created:**
  - `src/copilot_bridge/src/plugin.ts` (extend)
  - `src/copilot_bridge/tests/plugin.test.ts` (extend)
- **Acceptance criteria:**
  - `copilotOrchestrateTool`:
    - Parameters: `{ task: string, maxConcurrency?: number, timeout?: number }`
    - Returns single result: task plan summary → per-task results → overall summary
    - Returns `{ result: "..." }` per OpenClaw plugin API
  - Registered in plugin alongside `copilot_code` and `copilot_code_verbose`
  - Agent persona updated for when to use orchestrate vs single-task
  - Tests: mock orchestrator, verify result formatting

### C5.5 — BYOK Coordination

- **Description:** Unified BYOK config for both OpenClaw and Copilot SDK.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C1.1 (config)
- **Complexity:** S
- **Files created:**
  - `src/copilot_bridge/src/config.ts` (extend)
  - `src/copilot_bridge/scripts/setup-byok.ts`
- **Acceptance criteria:**
  - Script reads OpenClaw config for provider keys, maps to Copilot SDK `ProviderConfig`
  - Writes `.env` with coordinated config
  - Validates both platforms authenticate with same key
  - Supported: OpenAI, Anthropic, Azure, Ollama
  - Documentation: model recommendations per task type

### C5.6 — Orchestration End-to-End Test

- **Description:** Full orchestration test with parallel execution, persistence, and BYOK.
- **Owner:** copilot-sdk-integration
- **Dependencies:** C5.1, C5.2, C5.3, C5.4, C5.5
- **Complexity:** L
- **Files created:**
  - `src/copilot_bridge/tests/integration/orchestration-e2e.test.ts`
- **Acceptance criteria:**
  - Test 1: Complex task decomposes → parallel sub-tasks → combined results
  - Test 2: Resume interrupted orchestration — kill mid-task, resume, complete
  - Test 3: Sub-task failure → partial results with clear failure info
  - Test 4: Concurrent sessions respect pool limit
  - Test 5: BYOK orchestration works
  - Guarded by `COPILOT_INTEGRATION=1 && OPENCLAW_INTEGRATION=1`

## Parallel Execution Plan

```
── Time →

Agent A (copilot-sdk-integration):
  [C5.0 concurrency spike] → [C5.1 task decomposition] → [C5.4 orchestration plugin] → [C5.6 e2e]
         ~1.5h                         ~4h                         ~2h                       ~3h

Agent B (copilot-sdk-integration):
  [C5.0 spike] → [C5.2 session pool] → (feeds into C5.4) ──→ [C5.3 session persistence]
                        ~2h                                           ~2h

Agent C (copilot-sdk-integration):
  [C5.5 BYOK coordination] ──→ (feeds into C5.6)
         ~1h
```

**Note:** C5.0 blocks both C5.1 and C5.2 — its results determine the pool architecture.

## Integration Checkpoint

1. **Concurrency spike** — results documented, pool design informed
2. **Task decomposition** — complex request → ordered sub-tasks with dependencies
3. **Parallel execution** — respects pool limit, architecture matches spike findings
4. **Session persistence** — survives restart, BYOK config reconstructed from env
5. **Orchestration plugin** — works from OpenClaw conversation
6. **Hooks re-provided on resume** — permissions and audit active after resume

## Definition of Done

- [x] Concurrency spike completed and documented
- [x] Tasks decompose with dependency graph
- [x] Parallel execution respects concurrency limit
- [x] Pool design matches spike findings (single-client or multi-client)
- [x] Sessions persist and resume (including hooks and BYOK)
- [x] `copilot_orchestrate` tool works from OpenClaw
- [x] Partial results on sub-task failure
- [x] Stale sessions cleaned after 24h
- [x] E2E orchestration test passes
- [x] No session leaks after 10+ runs

---

# Cross-Phase Dependency Map

```
Phase 1: SDK Bootstrap
  C1.1 ──┐
  C1.2 ──┼── C1.8 ──┐
         │          ├── C1.3 ── C1.4 ──┬── C1.5
         │          │                   └── C1.6
  C1.7 ──┘          │
                     │
Phase 2: Plugin Delegation
                     ├── C2.1 ──┬── C2.2
                     │          └───────── C2.5
                     │   C2.4 ── C2.3 ──┘
                     │
Phase 3: MCP Bridge
                     ├── C3.1a ── C3.1b ── C3.3 ──┐
                     │   C3.2 ──── C3.4 ───────────┼── C3.5
                     │   C3.6 (after C3.1a + C3.2) ┘
                     │
Phase 4: Hooks & Audit
                     ├── C4.1 ──┬── C4.2 ──┐
                     │          ├── C4.3 ──┤
                     │          ├── C4.4 ──┼── C4.6
                     │          └── C4.5 ──┘
                     │
Phase 5: Orchestration
                     └── C5.0 ──┬── C5.1 ──┬── C5.4 ──┐
                                └── C5.2 ──┘           ├── C5.6
                                    C5.3 ──────────────┤
                                    C5.5 ──────────────┘
```

**Phase boundaries are hard gates.** Each phase's Definition of Done must be verified before starting the next phase.

---

# Summary: Task Count & Estimates

| Phase | Tasks | Parallel Tracks | Estimated Total | Critical Path |
|-------|-------|-----------------|-----------------|---------------|
| 1: SDK Bootstrap | 8 | 2 | ~7h | C1.1 → C1.8 → C1.3 → C1.4 → C1.5 |
| 2: Plugin Delegation | 5 | 2 | ~5h | C2.1 → C2.2 → C2.5 |
| 3: MCP Bridge | 7 | 3 | ~8.5h | C3.1a → C3.1b → C3.3 → C3.5 |
| 4: Hooks & Audit | 6 | 3 | ~5.5h | C4.1 → C4.2 → C4.6 |
| 5: Orchestration | 7 | 3 | ~9h | C5.0 → C5.1 → C5.4 → C5.6 |
| **Total** | **33** | — | **~35h** | — |

With 2 agents for Phases 1–2 and up to 3 agents for Phases 3–5, wall-clock time per phase is roughly 55–65% of total task hours.
