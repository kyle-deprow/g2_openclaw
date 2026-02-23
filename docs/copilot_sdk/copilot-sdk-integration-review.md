# Integration Plan Review

## Grade: B- — Solid structure with correct phasing instincts, but several SDK API misunderstandings and an architecturally flawed MCP design that would cause failures in Phases 1–3

The plan follows the established G2 implementation plan format closely — task structure, parallel execution diagrams, acceptance criteria, and Definition of Done checklists all meet the quality bar set by the Phase 1 and Phase 3 plans. The phasing order (bootstrap → plugin → MCP → hooks → orchestration) is correct in principle. However, cross-referencing against the actual Copilot SDK skills files reveals multiple API mismatches that would block implementation, and the MCP bridge design in Phase 3 conflates "agent prompt interface" with "direct tool invocation" in a way that would need significant redesign.

---

## 1. Critical Issues (Must Fix Before Building)

### 1.1 All Sessions in Phases 1–3 Will Silently Deny Every Tool Call

**Tasks affected:** C1.4, C1.5, C1.6, C2.1, C2.2, C2.5, C3.1, C3.2, C3.5

**Problem:** The Copilot SDK **denies all tool calls by default** unless a permission handler is provided. Both the sessions skill (`ctrl-permission-handler`) and the tools skill (`pipe-permission-flow`) are explicit:

> "Without a handler, permissions default to **deny**. Always provide one for sessions that use tools."

The plan defers all permission handling to Phase 4 (C4.2 pre-tool-use hooks). This means every session created in Phases 1–3 will have tools available but permanently blocked. The SDK smoke test (C1.6) will see tool calls silently fail. The plugin delegation (C2.1) will send prompts to Copilot, Copilot will try to use tools, tools will be denied, and Copilot will return "I tried to use a tool but was blocked." The MCP bridge tests (C3.5) will be non-functional.

**Fix:** Add `onPermissionRequest: async () => ({ decision: "allow" })` (or a minimal allow-all handler) to every `createSession()` call starting in Phase 1. Phase 4 then *upgrades* this permissive default to a policy-based handler. Add this as a requirement in C1.3 acceptance criteria and document it as a conscious decision (allow-all in dev, restrict in Phase 4).

### 1.2 BYOK Provider Config Is Per-Session, Not Per-Client — Plan Puts It in the Wrong Place

**Tasks affected:** C1.1, C1.3, C1.4, C5.5

**Problem:** The plan defines `COPILOT_BYOK_PROVIDER`, `COPILOT_BYOK_API_KEY`, `COPILOT_BYOK_BASE_URL`, `COPILOT_BYOK_MODEL` as client-level config (C1.1) and applies them in the `CopilotBridge` constructor (C1.3). But the SDK's `ProviderConfig` is a **session-level** option, not a client-level option.

From `copilot-sdk-sessions/SKILL.md` (`sess-create-full-config`), `provider` is a field on `SessionConfig`:

```typescript
const session = await client.createSession({
  model: "gpt-4.1",
  provider: { type: "openai", baseUrl: "...", apiKey: "..." },
});
```

From `copilot-sdk-client/SKILL.md`, the `CopilotClient` constructor accepts `githubToken`, `useLoggedInUser`, `cliPath`, `logLevel`, `autoRestart`, `cliArgs` — no BYOK/provider fields.

The plan's `CopilotBridge.getStatus()` returns `{ authMethod, model }` as if these are client-level properties, but model and auth method can vary per session when using BYOK.

**Fix:** Keep BYOK env vars in `BridgeConfig` for convenience, but pass them as `provider` in `createSession()` calls inside `runTask()` / `runTaskStreaming()`, not in the `CopilotClient` constructor. The `CodingTaskRequest` already has an optional `model` field — add an optional `provider` field too. Update C1.3 acceptance criteria to remove BYOK from constructor logic and add it to session creation.

### 1.3 MCP Server C3.1 Cannot Expose Copilot's Built-in Tools as Individual MCP Tools

**Tasks affected:** C3.1, C3.3, C3.5

**Problem:** C3.1 claims to expose five individual MCP tools: `copilot_read_file`, `copilot_write_file`, `copilot_terminal`, `copilot_search`, and `copilot_code_task`. The design assumes you can invoke Copilot's internal tools directly, like calling `read_file("src/main.ts")` through the SDK.

This is not how the SDK works. Copilot's built-in tools (file I/O, terminal, search) are **agent-internal** — the LLM decides when and how to use them. The external API is `session.sendAndWait({ prompt: "..." })`. You can't call `read_file` as a function.

There are limited session-level RPC methods that partially overlap. From `copilot-sdk-sessions/SKILL.md` (`ctrl-workspace-files`):

```typescript
const content = await session.rpc["workspace.readFile"]({ path: "src/main.ts" });
await session.rpc["workspace.createFile"]({ path: "new.ts", content: "..." });
```

But these are RPC methods on the CLI process, not the agent's tools. There's no documented `workspace.writeFile` (only `createFile`), no `terminal.exec`, no `search` RPC method. Wrapping `copilot_terminal` as "send a prompt asking the agent to run a command" is fragile — the agent may refuse, modify the command, or run additional commands.

**Fix:** Redesign C3.1 with two tiers:

1. **Deterministic tools** (use session RPC where available): `copilot_read_file` via `workspace.readFile`, `copilot_create_file` via `workspace.createFile`, `copilot_list_files` via `workspace.listFiles`. These are reliable.
2. **Agent-mediated tools** (prompt-based, non-deterministic): `copilot_code_task` wraps `sendAndWait()` with a natural language prompt. This is the only reliable way to access terminal, search, and complex multi-tool flows. Document that these are LLM-mediated and results are non-deterministic.

Drop `copilot_write_file`, `copilot_terminal`, and `copilot_search` as individual MCP tools. They cannot be reliably implemented. Update acceptance criteria and tool count (5 → 4, with only 3 deterministic).

### 1.4 OpenClaw Plugin Registration Mechanism Doesn't Match OpenClaw's Actual Architecture

**Tasks affected:** C2.1, C2.3, C2.5

**Problem:** C2.3 describes registering tools via a JSON config snippet merged into `~/.openclaw/config`:

```json
{ "tools": [
  { "name": "copilot_code", "module": "./src/copilot_bridge/src/plugin.ts", ... }
]}
```

This format does not exist in OpenClaw. Per the OpenClaw research (`04_tools_plugins_and_mcp.md`), there are three extension mechanisms:

1. **Plugins** — TypeScript modules in `~/.openclaw/plugins/` loaded via jiti, exporting an `OpenClawPlugin` interface with `name`, `version`, `tools[]`, `hooks`, `onLoad()`.
2. **MCP servers** — Configured in `~/.openclaw/config` under `mcp.servers` with `command`/`args` or `url`.
3. **Skills** — Directories in `~/.openclaw/skills/` with `SKILL.md` manifests.

None of these use a `"tools": [{ "module": "..." }]` config format. The plan's `register-plugin.ts` script merges a non-existent config schema.

**Fix:** Choose one of two viable approaches:

- **Plugin approach (recommended):** Create `~/.openclaw/plugins/copilot-bridge/index.ts` exporting `OpenClawPlugin` with the tool definitions. The `register-plugin.ts` script symlinks or copies the plugin directory. This matches OpenClaw's plugin discovery mechanism.
- **MCP approach:** Build the Copilot bridge as an MCP server (which C3.1 already does) and register it under `mcp.servers` in the OpenClaw config. Skip the plugin system entirely and let OpenClaw call Copilot tools via MCP from the start. This would merge Phase 2 and Phase 3 but simplify the architecture.

Update C2.1 to export an `OpenClawPlugin` interface, C2.3 to create the correct directory structure, and C2.5 tests to validate the actual registration path.

### 1.5 Bidirectional MCP Bridge Has No Cycle Detection — Infinite Loop Risk

**Tasks affected:** C3.1, C3.2, C3.4, C3.5

**Problem:** The MCP bridge creates this call chain:
```
OpenClaw → copilot_code_task (MCP) → Copilot session → openclaw_memory_search (MCP) → OpenClaw Gateway
```

This is a valid use case (Copilot fetching memory context mid-task). But nothing prevents:
```
OpenClaw → copilot_code_task → Copilot decides to ask OpenClaw → openclaw_context
→ OpenClaw context handler triggers agent → agent calls copilot_code_task → ∞
```

The OpenClaw agent, when processing a `copilot_code_task` result that includes `openclaw_context` data, might decide to delegate again. The Copilot session, given access to OpenClaw tools, might call `openclaw_context` which does an RPC `agent` request to OpenClaw, which has Copilot tools available...

**Fix:** Add a new task (C3.1.1 or modify C3.2) to implement cycle detection:

1. Pass a `depth` counter or `callChain` header through MCP tool calls.
2. The OpenClaw MCP server (C3.2) should expose **read-only** tools only (`memory_search`, `memory_read`, `user_prefs`). Remove `openclaw_context` which could trigger agent calls. Memory search is a direct data lookup — no agent loop involved.
3. Add a max-depth guard (e.g., depth ≥ 3 → return error) in both MCP servers.
4. Document the unidirectional data flow: OpenClaw *commands* Copilot; Copilot *reads* from OpenClaw. No command cycles.

---

## 2. Important Issues (Should Fix)

### 2.1 TypeScript `start()` Is Implicit — Plan Calls It Explicitly

**Tasks affected:** C1.3, C1.5

**Problem:** The client skill (`client-start-stop`) states:

> "start() is implicit — first RPC call triggers connection" (TypeScript)

The plan's `CopilotBridge` defines `async start()` that "calls `client.start()`." This method may not exist in the TypeScript SDK. The Python SDK requires explicit `start()`, but the TypeScript SDK auto-connects on first RPC.

**Fix:** For TypeScript, remove explicit `start()` call. Instead, `CopilotBridge.start()` should call `client.ping("hello")` or `client.getAuthStatus()` to trigger the implicit connection and verify readiness. Update C1.3 acceptance criteria. If the plan supports both languages, note the difference.

### 2.2 `streaming: true` Never Set in Session Config

**Tasks affected:** C1.4, C2.2

**Problem:** C1.4 implements `runTaskStreaming()` that yields `StreamingDelta` events. But the sessions skill (`stream-enable`) is clear:

> "Without `streaming: true`, you only get `assistant.message` (final). With it, you also get `assistant.message_delta` and `assistant.reasoning_delta`."

The plan never mentions setting `streaming: true` in the session config. The `runTaskStreaming()` method would receive zero delta events.

**Fix:** Update C1.4 acceptance criteria: `runTaskStreaming()` creates session with `streaming: true`. `runTask()` (blocking mode) can use `streaming: false`. Add `streaming` to the session creation logic.

### 2.3 C2.2 Streaming Plugin Assumes OpenClaw Supports Async Iterator Returns

**Tasks affected:** C2.2

**Problem:** C2.2 says `copilotCodeStreamTool` "Returns an async iterator of progress messages." But the OpenClaw plugin API (`04_tools_plugins_and_mcp.md`) shows tools have a simple `execute()` function that returns a single result:

```typescript
async execute({ input }) {
  return { result: `Processed: ${input}` };
}
```

There's no documented support for streaming/async generator returns from plugin tools. OpenClaw's streaming is at the agent level (assistant deltas), not the tool level.

**Fix:** Either (a) redesign as a single result that includes a summary of all intermediate steps (losing real-time updates but staying within the API), or (b) verify if OpenClaw's plugin API supports generator/streaming tool results (check actual source code), or (c) use OpenClaw's event/stream system — the plugin could emit events via the Gateway WebSocket that the UI subscribes to. Document the chosen approach and update C2.2 acceptance criteria.

### 2.4 MCP Transport Config Field Mismatch

**Tasks affected:** C3.3, C3.4

**Problem:** The plan uses `transport: "stdio"` in MCP config:

```json
{ "copilot": { "command": "node", "args": [...], "transport": "stdio" } }
```

The Copilot SDK uses `type: "local"` (not `transport: "stdio"`):

```typescript
mcpServers: { "copilot": { type: "local", command: "node", args: [...] } }
```

OpenClaw MCP config uses `command` + `args` (no explicit transport field — stdio is implied by presence of `command`).

**Fix:** Update C3.3 to use OpenClaw's actual MCP config format (`command` + `args`, no `transport`). Update C3.4 to use the Copilot SDK's format (`type: "local"`, `command`, `args`). Add the correct config snippets to acceptance criteria.

### 2.5 Missing `getAuthStatus()` Check Before Session Creation

**Tasks affected:** C1.3, C1.5

**Problem:** The client skill anti-patterns table warns:

> "Skipping `getAuthStatus()` check → Confusing runtime errors — Verify auth before `createSession()`"

The plan's `CopilotBridge` has `isReady()` and `getStatus()` but neither calls `getAuthStatus()`. The validation script (C1.5) goes straight from client start to session creation. If auth is broken, the error message will be cryptic.

**Fix:** Add `getAuthStatus()` call to `CopilotBridge.start()` (or the implicit trigger). Verify `status === "signed-in"` before allowing session creation. Add to C1.5 validation script: check auth status after start, fail with clear message if not authenticated.

### 2.6 `CopilotClient` Constructor Option Names Don't Match Plan Config

**Tasks affected:** C1.1, C1.3

**Problem:** The plan defines env var `COPILOT_AUTH_TOKEN` and passes it to the client. The SDK constructor uses `githubToken`:

```typescript
const client = new CopilotClient({ githubToken: process.env.GH_TOKEN });
```

The plan's `COPILOT_CLI_PATH` maps to `cliPath` (correct), but `COPILOT_AUTH_TOKEN` should map to `githubToken`, not a custom field. The SDK also has an auth priority chain where `COPILOT_GITHUB_TOKEN` env var is checked automatically — the plan's custom env var name bypasses this.

**Fix:** Rename `COPILOT_AUTH_TOKEN` to `COPILOT_GITHUB_TOKEN` (or `GH_TOKEN`) to match the SDK's auto-detection chain. If the user sets this env var, the SDK may pick it up automatically without needing constructor config. Document the auth priority chain from `auth-method-priority` in the config comments.

### 2.7 No CI/CD, Linting, or Formatting Setup Task

**Tasks affected:** All phases

**Problem:** The G2 Phase 1 plan includes project scaffold with `vitest`, `ruff`, linting config. This plan creates `package.json` and `tsconfig.json` but has no task for ESLint/Prettier/Biome configuration, CI pipeline setup, or pre-commit hooks. Over 5 phases and 28 tasks, code quality will drift without automated checks.

**Fix:** Add a task (C1.1.1 or extend C1.1) for: ESLint config, Prettier/Biome config, `lint-staged` + `husky` (or equivalent), a GitHub Actions CI workflow that runs `tsc --noEmit`, `vitest`, and `eslint` on PR. Complexity: S.

### 2.8 `client.stop()` Returns `Error[]` — Plan Ignores Return Value

**Tasks affected:** C1.3

**Problem:** The client skill (`client-stop-vs-force-stop`) states:

> "`stop()` returns `Error[]` — check for session destruction failures."

The plan's `CopilotBridge.stop()` calls `client.stop()` and "cleans up" but doesn't mention checking or logging the returned errors. The anti-patterns table warns: "Ignoring `stop()` error array → Silent session leaks."

**Fix:** Update C1.3 acceptance criteria: `stop()` checks returned `Error[]`, logs any errors, and uses `forceStop()` as fallback if `stop()` errors. Add a test case for `stop()` returning errors.

---

## 3. Minor Issues (Nice to Fix)

### 3.1 Cross-Phase Dependency Map Has Rendering Errors

**Problem:** The ASCII art dependency map at the bottom of the plan shows:

```
Phase 5: Orchestration
  └── C5.1 ──┬── C5.4 ── C5.6
      C5.2 ──┘          ┘
      C5.3 ─────────────
      C5.5 ─────────────
```

C5.6 has a dangling `┘` that doesn't connect to anything. C5.3 and C5.5 have no target — they just trail off. According to the Phase 5 text, C5.3 feeds into C5.6 (session persistence needed for resume test) and C5.5 feeds into C5.6 (BYOK needed for BYOK test). The arrows should show this.

**Fix:** Update the ASCII diagram to show C5.3 and C5.5 connecting to C5.6.

### 3.2 Time Estimates Are Optimistic for MCP Server Tasks

**Problem:** C3.1 (Copilot MCP server) is estimated at 4h with complexity L. This task involves: building an MCP server from scratch (JSON-RPC over stdio), wrapping CopilotBridge with session management, handling concurrent tool calls with queuing, clean shutdown, and comprehensive tests. The equivalent OpenClaw client in the G2 plan (P3.1) was 3h for a simpler WebSocket client with sequential message handling.

Building a functional MCP server is closer to 6–8h, especially given the architectural issues identified in §1.3 that require redesign.

**Fix:** Re-estimate C3.1 at 6–8h and consider splitting into two tasks: (a) MCP server skeleton with `copilot_code_task` only, (b) add RPC-based deterministic tools. This also has cascade effects on the Phase 3 total estimate.

### 3.3 Inconsistent Owner Assignment for C3.2

**Problem:** C3.2 (OpenClaw-to-Copilot MCP server) is assigned to `copilot-sdk-integration` owner, but it's primarily about OpenClaw's WebSocket protocol, memory query format, and Gateway RPC methods. The `openclaw-development` owner would be more appropriate, or at minimum it should be a split-ownership task.

**Fix:** Assign C3.2 to `openclaw-development` or mark as joint ownership. The MCP server skeleton is generic (copilot-sdk-integration), but the OpenClaw Gateway WebSocket client inside it requires OpenClaw domain knowledge.

### 3.4 Plan References `@github/copilot-sdk` — Verify Package Name

**Problem:** C1.1 lists `@github/copilot-sdk` as the npm dependency. The client skill file references it as `@github/copilot-sdk` in imports. Verify this is the actual published package name — "Technical Preview" packages sometimes have different registry names (e.g., `@github/copilot` or a private registry).

**Fix:** Verify the exact package name and registry URL. If it's a private/preview package, document the installation steps (registry config, access tokens) in C1.1.

### 3.5 Summary Table Claims "2–3 Agents Per Phase" But Phase 1 Only Has 2 Tracks

**Problem:** The summary says "With maximum parallelization across 2-3 agents per phase" but Phase 1 and Phase 2 each only have 2 parallel tracks. Phase 4 has 3 tracks but they all share one dependency gate (C4.1). The "2-3 agents" claim is slightly misleading — it's really 2 agents for Phases 1-2, and 3 agents for a brief window in Phases 3-5.

**Fix:** Adjust the summary to be more precise: "2 agents for Phases 1–2, up to 3 agents for brief windows in Phases 3–5."

---

## 4. Risks & Assumptions

### 4.1 Copilot SDK Is "Technical Preview" — API Surface May Change

**Likelihood:** High
**Impact:** High

The SDK is explicitly marked as Technical Preview. Any API (constructor options, session config fields, hook signatures, RPC methods, MCP config format) could change between versions. The plan makes no provision for an abstraction layer between the integration code and the raw SDK API.

**Mitigation:** Add a thin abstraction interface (`ICopilotSession`, `ICopilotClient`) that wraps SDK calls. All integration code calls the interface; only the wrapper calls the SDK directly. When the SDK changes, only the wrapper needs updating. This adds ~2h to Phase 1 but saves potentially days of refactoring later. Pin the SDK version in `package.json` (exact version, not range).

### 4.2 One CLI Process Per CopilotClient — Session Pool Concurrency May Be Serialized

**Likelihood:** Medium
**Impact:** High

The SDK spawns a single CLI subprocess per `CopilotClient` instance. All sessions share this process via JSON-RPC multiplexing. The Session Pool (C5.2) assumes concurrent sessions execute in parallel, but the CLI may serialize RPC calls on a single connection. True parallelism might require multiple `CopilotClient` instances, each spawning its own CLI process — with significant memory/CPU overhead.

**Mitigation:** Add a spike task (before C5.2) to measure actual concurrency: create 3 sessions on one client, send long prompts simultaneously, measure whether they complete in parallel or sequentially. If serialized, redesign the pool to use multiple `CopilotClient` instances and add resource limits.

### 4.3 MCP Server Process Lifecycle — Spawning Per Session Is Expensive

**Likelihood:** Medium
**Impact:** Medium

C3.4 ties the OpenClaw MCP server lifecycle to the Copilot session lifecycle: "starts with session, stops when session destroyed." With the session pool (C5.2, max 3 concurrent sessions), this means 3 MCP server processes spawning and dying repeatedly. Each MCP server in C3.2 opens a WebSocket to OpenClaw Gateway, authenticates, and becomes ready. This overhead could add 1–3s per session creation.

**Mitigation:** Use a shared, long-lived MCP server process instead of per-session spawning. The MCP server can multiplex requests (MCP protocol supports this). Update C3.4 to use a persistent MCP server and map session context through tool parameters.

### 4.4 OpenClaw Gateway WebSocket Concurrency — Multiple Simultaneous agent Requests

**Likelihood:** Medium
**Impact:** Medium

C3.2's MCP server and C5.2's session pool could result in multiple simultaneous `agent` RPC requests to the OpenClaw Gateway. The OpenClaw architecture research (`02_agent_architecture_and_context.md`) shows "Runs are serialized per session key." If all requests use the same session key (`agent:claw:g2`), they'll queue behind each other, defeating the parallel session pool. If they use different session keys, they lose shared context.

**Mitigation:** Design the session key strategy before C3.2. Options: (a) unique session key per Copilot task (full parallelism, no shared context), (b) shared session key (serialized, shared context), (c) hybrid (main session for context, sub-sessions for parallel tasks). Document the tradeoff in C5.1.

### 4.5 BYOK API Keys Not Persisted on Resume — Session Persistence Design Impact

**Likelihood:** High
**Impact:** Low

The client skill (`byok-limitations`) states:

> "API keys not persisted on resume. Must re-provide `provider` config when calling `resumeSession()`."

C5.3 implements session persistence and resume. If BYOK is used, the persisted session metadata must include enough info to reconstruct the `provider` config on resume (without storing actual API keys — those come from env vars).

**Mitigation:** C5.3 should store provider *type* and *baseUrl* in session metadata, but read API keys from current env vars on resume. Add a test case: persist session with BYOK, restart process, resume session — verify provider config is reconstructed correctly.

### 4.6 OpenClaw Plugin API Stability and Version Compatibility

**Likelihood:** Medium
**Impact:** Medium

The plan depends on OpenClaw's plugin API (`OpenClawPlugin` interface, jiti loading, `~/.openclaw/plugins/` discovery). This API is documented in research notes but may evolve. No OpenClaw version is pinned.

**Mitigation:** Pin the OpenClaw version. Add a compatibility check in the plugin's `onLoad()` that verifies the OpenClaw Gateway version supports the required plugin API features.

---

## 5. What's Good

- **Phasing order is correct.** Bootstrap → basic delegation → bidirectional access → governance → orchestration is the right progression. Each phase builds on the last without over-reaching.
- **Task granularity is appropriate.** Most tasks are genuinely achievable in one agent session. The S/M/L complexity ratings correlate well with the estimated hours (S ≈ 0.5–1h, M ≈ 1.5–2h, L ≈ 3–4h).
- **Acceptance criteria are specific and testable.** Each task has concrete, verifiable outputs — not vague "it works" statements. Code snippets in acceptance criteria make intent unambiguous.
- **Parallel execution plans are well-designed.** Dependency analysis is mostly correct. The ASCII timeline diagrams clearly show which tasks can overlap and where merge points exist.
- **Integration checkpoints are comprehensive.** Each phase has 6–7 specific verification steps that together prove readiness for the next phase. The "Definition of Done" checklists are actionable.
- **Follows the established plan format.** Structure matches Phase 1 and Phase 3 G2 plans: Phase Goal → Prerequisites → Task Breakdown → Parallel Execution → Integration Checkpoint → Definition of Done.
- **Streaming and blocking modes are both covered.** The plan correctly identifies the need for both `sendAndWait` (simple) and event-based streaming (interactive) patterns, matching the SDK's dual-mode architecture.
- **Error handling is considered, not ignored.** `BridgeError` with structured codes, timeout handling, and error hooks show awareness that failures need design, not just try/catch.
- **Cross-phase dependency map provides a useful overview** (despite the rendering errors noted in §3.1).

---

## 6. Recommended Changes Summary

**Priority order — fix these before building:**

1. **[CRITICAL]** Add `onPermissionRequest: () => ({ decision: "allow" })` to all session creation in Phases 1–3 (§1.1)
2. **[CRITICAL]** Move BYOK `provider` config from client constructor to `createSession()` calls (§1.2)
3. **[CRITICAL]** Redesign C3.1 MCP server: drop fake tool wrappers (`copilot_terminal`, `copilot_search`), use session RPC for file ops, use `sendAndWait` for agent-mediated tasks (§1.3)
4. **[CRITICAL]** Rewrite C2.1/C2.3 to use OpenClaw's actual plugin API (`~/.openclaw/plugins/` directory, `OpenClawPlugin` interface) or MCP server registration (§1.4)
5. **[CRITICAL]** Add cycle detection / depth limits to bidirectional MCP bridge; make OpenClaw MCP server read-only (§1.5)
6. **[IMPORTANT]** Remove explicit `client.start()` call in TypeScript; use `ping()` or `getAuthStatus()` to trigger connection (§2.1)
7. **[IMPORTANT]** Add `streaming: true` to session config in `runTaskStreaming()` (§2.2)
8. **[IMPORTANT]** Redesign C2.2 streaming plugin to work within OpenClaw's actual tool return API (§2.3)
9. **[IMPORTANT]** Fix MCP transport config field names for both SDK and OpenClaw sides (§2.4)
10. **[IMPORTANT]** Add `getAuthStatus()` check before first session creation (§2.5)
11. **[IMPORTANT]** Rename `COPILOT_AUTH_TOKEN` to `COPILOT_GITHUB_TOKEN` to match SDK auto-detection (§2.6)
12. **[IMPORTANT]** Add a CI/CD setup task to Phase 1 (§2.7)
13. **[IMPORTANT]** Handle `stop()` return value (`Error[]`) — log errors, fallback to `forceStop()` (§2.8)
14. **[RISK]** Add SDK abstraction interface to isolate from API changes (§4.1)
15. **[RISK]** Add a concurrency spike before C5.2 to verify parallel session execution (§4.2)
16. **[RISK]** Use shared MCP server process instead of per-session spawning (§4.3)
17. **[MINOR]** Fix cross-phase dependency map rendering (§3.1)
18. **[MINOR]** Re-estimate C3.1 at 6–8h and consider splitting (§3.2)
19. **[MINOR]** Reassign C3.2 ownership to openclaw-development (§3.3)
20. **[MINOR]** Verify exact SDK npm package name and registry (§3.4)

---

*End of review.*
