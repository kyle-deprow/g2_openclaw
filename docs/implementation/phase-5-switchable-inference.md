# Phase 5: Switchable Inference Modes

## Phase Goal

Enable switching between LLM inference backends — Direct API (Azure/OpenAI/Anthropic), GitHub Copilot SDK, and Local (Ollama) — via a repo-committed JSON config file. Users with a Copilot subscription can route inference through the Copilot SDK instead of maintaining separate API keys and endpoints.

## Prerequisites

- Phase 3 complete: OpenClaw pipeline working end-to-end (voice → transcription → OpenClaw → response → display)
- `gateway/server.py` `ResponseHandler` protocol established
- `copilot_bridge/` operational with `@github/copilot-sdk` integration
- Understanding of OpenClaw plugin `providers` extension point

---

## Architecture

### Current State

```
GatewayServer.__init__:
  if OPENCLAW_GATEWAY_TOKEN → OpenClawResponseHandler (real OpenClaw)
  else                      → MockResponseHandler     (canned responses)
```

### Target State

```
inference_config.json  ──→  config.py (resolve active mode)
                               │
                               ▼
                        server.py handler factory
                        ┌──────┼──────────┐
                        ▼      ▼          ▼
                    Direct   Copilot    Local
                     API      SDK      (Ollama)
                   Handler   Handler   Handler
                        │      │          │
                        └──────┼──────────┘
                               ▼
                     same phone protocol frames
```

The `ResponseHandler` protocol is the abstraction point. Each inference mode implements `handle(message, send_frame)`. Everything downstream (Whisper, phone protocol, display rendering) is unchanged.

### Key Design Decisions

1. **Python Gateway never imports `@github/copilot-sdk`** — the copilot_bridge exposes an HTTP/SSE adapter, Gateway consumes it as a standard SSE client. Clean language boundary.
2. **Restart-required for mode switch** (not hot-swap) — consistent with existing single-daemon pattern. Future `/control` RPC can add hot-swap.
3. **Secrets stay in env vars** — referenced via `"env:VAR_NAME"` syntax in JSON, resolved at runtime. Never committed.
4. **Fallback chain** — if selected mode is unavailable (missing key, unreachable endpoint), automatically tries next mode. Falls back to `MockResponseHandler` as last resort.

---

## Config Schema

### File: `gateway/inference_config.json`

```jsonc
{
  "version": "1.0",

  // Which mode is active when INFERENCE_MODE env var is not set
  "defaultMode": "direct-azure",

  "modes": {
    "direct-azure": {
      "type": "direct",
      "enabled": true,
      "provider": "azure",
      "model": "gpt-41",
      "endpoint": "https://your-endpoint.openai.azure.com/",
      "apiKeyRef": "env:AZURE_OPENAI_API_KEY",
      "apiVersion": "2025-01-01-preview",
      "contextWindow": 1047576,
      "maxTokens": 32768,
      "options": { "temperature": 0.7 }
    },

    "copilot-sdk": {
      "type": "copilot-sdk",
      "enabled": true,
      "provider": "copilot",
      "model": "gpt-4o",
      "apiKeyRef": "env:COPILOT_GITHUB_TOKEN",
      "contextWindow": 128000,
      "maxTokens": 16384,
      "options": {}
    },

    "local-ollama": {
      "type": "local",
      "enabled": true,
      "provider": "ollama",
      "model": "codellama",
      "endpoint": "http://localhost:11434",
      "apiKeyRef": null,
      "contextWindow": 16384,
      "maxTokens": 4096,
      "options": { "temperature": 0.3 }
    },

    "direct-anthropic": {
      "type": "direct",
      "enabled": false,
      "provider": "anthropic",
      "model": "claude-sonnet-4-20250514",
      "endpoint": "https://api.anthropic.com",
      "apiKeyRef": "env:ANTHROPIC_API_KEY",
      "contextWindow": 200000,
      "maxTokens": 8192,
      "options": {}
    }
  },

  // Ordered list of mode IDs to try when the selected mode is unavailable
  "fallbackChain": ["direct-azure", "copilot-sdk", "local-ollama"]
}
```

### Mode Field Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `"direct" \| "copilot-sdk" \| "local"` | Yes | Inference routing strategy |
| `enabled` | `bool` | Yes | Whether mode can be selected |
| `provider` | `"openai" \| "azure" \| "anthropic" \| "ollama" \| "copilot"` | Yes | Provider identifier |
| `model` | `string` | Yes | Model identifier |
| `endpoint` | `string` | Required for `direct`/`local` | Base URL for API |
| `apiKeyRef` | `string \| null` | No | Env var reference (`"env:VAR_NAME"`) |
| `apiVersion` | `string` | No | For Azure OpenAI deployments |
| `contextWindow` | `int` | No | Max context tokens |
| `maxTokens` | `int` | No | Max output tokens per response |
| `options` | `object` | No | Provider-specific pass-through |

### Runtime Selection

Active mode is resolved as: `INFERENCE_MODE` env var → `defaultMode` in config → first enabled mode in `fallbackChain`.

---

## Copilot SDK as Inference Backend

### Two Code Paths Within `copilot-sdk` Mode

| | SDK Path (native Copilot auth) | BYOK-Direct Path |
|---|---|---|
| **When** | `provider: "copilot"` | `provider: "openai"` (etc.) with BYOK config |
| **System prompt** | Flattened into single prompt with role markers | Full `messages[]` array control |
| **Overhead** | Agent loop (tool discovery, potential tool calls) | Direct HTTP, no overhead |
| **Streaming** | `content.delta` events → mapped to OpenClaw deltas | Standard SSE → trivial mapping |
| **Models** | GPT-4o (unlimited on Pro), Claude/o1 (premium-limited) | Whatever the provider offers |
| **ToS risk** | Medium (non-coding use) | None (your own API key) |

### SDK Limitations for General Inference

| Limitation | Severity | Mitigation |
|---|---|---|
| No explicit `messages[]` array | High | Flatten system+conversation into single prompt; inject systemPrompt via `additionalContext` hook |
| Agent loop overhead on every prompt | Medium | Pass `tools: []` to disable tool discovery |
| No conversation history control | High | Multiple `sendAndWait` calls accumulate context internally; can't inject arbitrary history |
| Model selection opacity | Medium | Pass `model` in session config; available models depend on subscription tier |
| Rate limits (premium models) | Medium | Handle 429s reactively; base models unlimited on Pro+ |

### Streaming Delta Mapping

| Source Event | → | OpenClaw Event |
|---|---|---|
| Copilot `content.delta` | → | `{"stream":"assistant","delta":"..."}` |
| Copilot `response.completed` | → | `{"stream":"lifecycle","phase":"end"}` |
| Copilot `error` | → | `{"stream":"lifecycle","phase":"error","error":"..."}` |
| SSE `choices[0].delta.content` | → | `{"stream":"assistant","delta":"..."}` |
| SSE `[DONE]` | → | `{"stream":"lifecycle","phase":"end"}` |

---

## Config Propagation Path

```
inference_config.json + INFERENCE_MODE env var
        │
        ▼
  gateway/config.py
  InferenceMode dataclass + load_inference_config()
        │
        ▼
  gateway/server.py
  create_handler(mode) factory dispatches on mode.type:
    "direct"      → DirectAPIResponseHandler(mode)
    "copilot-sdk" → CopilotBridgeResponseHandler(mode)
    "local"       → DirectAPIResponseHandler(mode)   # same handler, OpenAI-compat
    (fallback)    → MockResponseHandler
        │
        ▼  (for type="copilot-sdk")
  copilot_bridge/src/gateway-adapter.ts
  HTTP/SSE endpoint wrapping CopilotBridge sessions
        │
        ▼  (for type="direct" or "local")
  aiohttp SSE to OpenAI-compatible /v1/chat/completions
```

### Integration with Copilot Bridge BYOK

| Inference mode field | BridgeConfig field | Notes |
|---------------------|-------------------|-------|
| `type: "copilot-sdk"` + `provider: "copilot"` | `githubToken` | SDK uses Copilot subscription directly |
| `type: "copilot-sdk"` + `provider: "openai"` | `byokProvider: "openai"` | SDK routes through user's API key |
| `provider` | `byokProvider` | Direct mapping |
| `apiKeyRef` (resolved) | `byokApiKey` | Secret resolved by Gateway before passing |
| `endpoint` | `byokBaseUrl` | Direct mapping |
| `model` | `byokModel` | Direct mapping |

---

## Fallback Behavior

| Scenario | Behavior |
|----------|----------|
| API key env var unset | Try next mode in `fallbackChain`. Log warning. |
| Endpoint unreachable at startup | Try next mode in `fallbackChain`. Log error. |
| All modes fail | Fall back to `MockResponseHandler`. Gateway stays up for health checks and transcription. |
| Mid-session API error | Caught by existing `OpenClawError` → `ErrorCode.OPENCLAW_ERROR` path. No automatic mode switch mid-session. |
| Copilot token expired | `BridgeError("AUTH_FAILED")` mapped to `OpenClawError`. |

### Startup Validation

```python
def resolve_active_mode(config_path: Path, env_override: str | None) -> InferenceMode | None:
    cfg = json.loads(config_path.read_text())
    requested = env_override or cfg["defaultMode"]

    for mode_id in [requested, *cfg["fallbackChain"]]:
        mode = cfg["modes"].get(mode_id)
        if mode is None or not mode.get("enabled", True):
            continue
        if not _validate_mode(mode):
            logger.warning("Mode %s unavailable, trying next", mode_id)
            continue
        if mode_id != requested:
            logger.warning("Fell back from %s to %s", requested, mode_id)
        return InferenceMode(**mode)

    logger.error("All inference modes unavailable — using mock handler")
    return None
```

---

## Validation Rules

### Config load time (fatal errors)

- `inference_config.json` is valid JSON
- `defaultMode` exists in `modes`
- Every mode has required fields (`type`, `provider`, `model`)
- `type` is one of `"direct"`, `"copilot-sdk"`, `"local"`
- `provider` is one of `"openai"`, `"azure"`, `"anthropic"`, `"ollama"`, `"copilot"`
- `contextWindow` and `maxTokens` are positive integers (if set)
- `fallbackChain` entries all exist in `modes`
- No duplicate mode IDs

### Mode activation time (warnings, try fallback)

- `apiKeyRef` env var is set and non-empty (for `direct` type)
- `endpoint` is a valid URL (for `direct` and `local` type)
- GitHub token resolves (for `copilot-sdk` type)
- Ollama endpoint responds to `GET /api/tags` (for `local` type)

---

## Task Breakdown

### PR 1 — Handler Factory Refactor (Gateway Python)

- **Description:** Extract handler creation from `GatewayServer.__init__` into a `create_handler(config)` factory function. Pure refactor, zero behavior change.
- **Owner:** backend-python
- **Dependencies:** None
- **Complexity:** S
- **Files modified:**
  - `gateway/server.py` — extract factory
- **Files created:**
  - None
- **Acceptance criteria:**
  - Existing `MockResponseHandler` / `OpenClawResponseHandler` selection works exactly as before
  - All existing tests pass unchanged
  - Factory function is unit-testable

### PR 2 — Config Schema + Validation (Gateway Python)

- **Description:** Add `inference_config.json` schema, `InferenceMode` Pydantic model, `load_inference_config()` function, and `validate-config` CLI command with full test suite.
- **Owner:** backend-python
- **Dependencies:** PR 1
- **Complexity:** M
- **Files created:**
  - `gateway/inference_config.json` — mode definitions
  - `tests/gateway/test_inference_config.py` — validation and fallback tests
- **Files modified:**
  - `gateway/config.py` — add `InferenceMode` dataclass, `load_inference_config()`
  - `gateway/cli.py` — add `validate-config` command
- **Acceptance criteria:**
  - `InferenceMode` validates all fields with Pydantic
  - `load_inference_config()` resolves active mode from env + JSON
  - Fallback chain tested (3+ scenarios)
  - `uv run python -m gateway validate-config` reports selected mode and validation errors
  - Invalid configs raise clear error messages

### PR 3 — Local Mode / Ollama (Gateway Python)

- **Description:** Implement `DirectAPIResponseHandler` using `aiohttp` SSE client against OpenAI-compatible `/v1/chat/completions`. Works for Ollama, vLLM, LM Studio, and direct OpenAI/Azure.
- **Owner:** backend-python
- **Dependencies:** PR 2
- **Complexity:** M
- **Files modified:**
  - `gateway/server.py` — add `DirectAPIResponseHandler`, wire into factory
- **Files created:**
  - `tests/gateway/test_direct_handler.py` — unit tests with mocked HTTP
- **Acceptance criteria:**
  - Sends system prompt + user message to `/v1/chat/completions` with `stream: true`
  - Parses SSE `data:` lines, maps `choices[0].delta.content` → `assistant` deltas
  - Handles `[DONE]` → sends `end` frame
  - Handles 401, 429, 500 errors with appropriate error frames
  - Azure variant adds `api-key` header and `/openai/deployments/{model}/` URL
  - Works against Ollama `http://localhost:11434` with no API key

### PR 4 — Copilot SDK Adapter (TypeScript + Python)

- **Description:** Create `gateway-adapter.ts` HTTP/SSE endpoint in copilot_bridge exposing Copilot SDK sessions for general inference. Add `CopilotBridgeResponseHandler` in Python as an aiohttp SSE client. Each side tested independently.
- **Owner:** backend-python (Python handler), copilot-bridge (TypeScript adapter)
- **Dependencies:** PR 2 (config schema); parallel with PR 3
- **Complexity:** L
- **Files created:**
  - `copilot_bridge/src/gateway-adapter.ts` — HTTP server with `/v1/inference` SSE endpoint
  - `copilot_bridge/tests/gateway-adapter.test.ts` — unit tests
  - `tests/gateway/test_copilot_handler.py` — Python handler tests with mocked HTTP
- **Files modified:**
  - `gateway/server.py` — add `CopilotBridgeResponseHandler`, wire into factory
  - `copilot_bridge/package.json` — add `adapter` script entry
- **Acceptance criteria:**
  - `gateway-adapter.ts` accepts POST `/v1/inference` with `{ systemPrompt, messages, model, streaming }` body
  - Creates Copilot SDK session with `tools: []`, no MCP servers
  - Streams SSE events: `data: {"delta":"token"}` per content chunk, `data: [DONE]` on complete
  - Python `CopilotBridgeResponseHandler` consumes SSE, maps to phone protocol frames
  - Handles auth failure (no token) with clear error
  - Mutex prevents concurrent session creation
  - Works with both native Copilot auth and BYOK provider config

### PR 5 — Fallback Chain + `switch-mode` CLI

- **Description:** Wire up fallback chain resolution at startup, add `switch-mode` CLI command, add Anthropic direct API support (Messages API format differs from OpenAI).
- **Owner:** backend-python
- **Dependencies:** PR 3, PR 4
- **Complexity:** M
- **Files modified:**
  - `gateway/server.py` — fallback chain in factory
  - `gateway/config.py` — `resolve_active_mode()` with chain logic
  - `gateway/cli.py` — add `switch-mode <mode-id>` command
- **Files created:**
  - `tests/gateway/test_fallback_chain.py` — fallback scenario tests
- **Acceptance criteria:**
  - If selected mode's API key is unset, falls to next in chain
  - If endpoint is unreachable, falls to next in chain
  - If all fail, logs error and uses `MockResponseHandler`
  - `switch-mode` writes `INFERENCE_MODE=<id>` to `.env` and prints restart instructions
  - Anthropic Messages API format handled (different auth header, different SSE format)

---

## New/Modified File Summary

| File | Change | PR |
|------|--------|----|
| `gateway/server.py` | Refactor handler factory; add `DirectAPIResponseHandler`, `CopilotBridgeResponseHandler` | 1, 3, 4 |
| `gateway/config.py` | Add `InferenceMode` Pydantic model, `load_inference_config()`, `resolve_active_mode()` | 2, 5 |
| `gateway/cli.py` | Add `validate-config`, `switch-mode` commands | 2, 5 |
| `gateway/inference_config.json` | New — mode definitions | 2 |
| `copilot_bridge/src/gateway-adapter.ts` | New — HTTP/SSE adapter for Copilot SDK inference | 4 |
| `copilot_bridge/package.json` | Add `adapter` script | 4 |
| `copilot_bridge/scripts/setup-byok.ts` | Add `--from-inference-config` flag | 5 |
| `tests/gateway/test_inference_config.py` | New — config validation tests | 2 |
| `tests/gateway/test_direct_handler.py` | New — DirectAPI handler tests | 3 |
| `tests/gateway/test_copilot_handler.py` | New — CopilotBridge handler tests | 4 |
| `tests/gateway/test_fallback_chain.py` | New — fallback scenario tests | 5 |
| `copilot_bridge/tests/gateway-adapter.test.ts` | New — adapter unit tests | 4 |

---

## Testing Strategy

### Unit Tests (CI — no external services)

- **Config validation:** Valid/invalid JSON, missing fields, unknown types, fallback resolution
- **DirectAPIResponseHandler:** Mock `aiohttp.ClientSession` → assert correct HTTP request shape, SSE parsing, error handling
- **CopilotBridgeResponseHandler:** Mock HTTP → assert SSE consumption, delta mapping
- **gateway-adapter.ts:** Mock `@github/copilot-sdk` via `vi.mock` (existing pattern), assert SSE output shape

### Integration Tests (Manual — requires running services)

- **Ollama:** Start `ollama serve`, set `INFERENCE_MODE=local-ollama`, send voice query, verify response
- **Copilot SDK:** Set `COPILOT_GITHUB_TOKEN`, set `INFERENCE_MODE=copilot-sdk`, verify end-to-end
- **Direct Azure:** Set `AZURE_OPENAI_API_KEY`, set `INFERENCE_MODE=direct-azure`, verify end-to-end
- **Fallback:** Unset all keys, verify falls to mock handler with warning logs

### CI Considerations

- All unit tests run with mocked HTTP — no tokens or external services
- `validate-config` command tested with fixture JSON files
- TypeScript adapter tests use `vi.mock('@github/copilot-sdk')` (existing pattern in `client.test.ts`)

---

## Copilot Subscription Model Reference

| Tier | Base Models (unlimited) | Premium Requests/month | Notes |
|------|------------------------|----------------------|-------|
| Free | GPT-4o-mini | 50 | Very limited |
| Pro | GPT-4o, GPT-4.1, Gemini Flash | 300 | Good for personal use |
| Pro+ | Same as Pro | 1,500 | Higher premium quota |
| Business | Same as Pro | Per-seat | Admin-managed |

Premium models (Claude, o1, o3-mini) are counted against the monthly premium quota. Base model calls are effectively unlimited on paid tiers. For G2 voice queries (~1 request per interaction), rate limiting is unlikely to be an issue on Pro or above.

---

## Open Questions

1. **OpenClaw `providers` plugin API** — The extension point is documented but no code sample exists. The interface shape (streaming `AsyncGenerator<ChatCompletionChunk>`, `countTokens()`, `listModels()`) is inferred from patterns. Needs validation against a running OpenClaw instance.
2. **Copilot ToS for non-coding inference** — Using Copilot SDK for general Q&A (not coding) may be outside intended use. BYOK-Direct path avoids this entirely.
3. **`wireApi` format** — `IProviderConfig.wireApi` in copilot_bridge hints at `completions` vs `responses` OpenAI API formats. Only `completions` is implemented initially; `responses` can be added later.

---

## Review Findings

_Three reviewer agents examined this plan against OpenClaw architecture, Copilot SDK internals, and Gateway Python engineering patterns. Consolidated findings below._

### Critical Issues (must address before implementation)

#### R1. Two-tier architecture not acknowledged

The plan treats all four modes (OpenClaw, Direct, Copilot SDK, Local) as equivalent `ResponseHandler` swaps. They are not:

| Tier | Mode | Capabilities |
|------|------|-------------|
| **Full agent** | `openclaw` | Sessions, tools, memory, persona, compaction, pruning |
| **Raw LLM** | `direct`, `copilot-sdk`, `local` | Stateless text completion only — no tools, no memory, no persona |

Switching from OpenClaw to a raw LLM mode is a **capability cliff**, not a graceful degradation. The fallback chain (`direct-azure` → `copilot-sdk` → `local`) should be restricted to same-tier modes. Falling from `openclaw` to `local-ollama` should require explicit opt-in, not silent fallback.

**Action:** Reframe config schema with explicit tier labels. Restrict fallback chain to same-tier. Add mode indicator to the `connected` phone protocol frame.

#### R2. `handle(message: str, ...)` provides no conversation history

The `ResponseHandler.handle()` receives only the latest user utterance. `OpenClawResponseHandler` doesn't need history (OpenClaw manages sessions server-side). But `DirectAPIResponseHandler` calling `/v1/chat/completions` **requires a `messages[]` array** — without it, every query is context-free.

**Action:** Either extend `handle()` to accept `list[dict]` of messages, or make `DirectAPIResponseHandler` stateful with an internal conversation buffer that `GatewaySession` feeds. Decide before PR 3.

#### R3. No system prompt in bypass modes

For OpenClaw mode, `SOUL.md` is loaded by OpenClaw's bootstrap. For Direct/Local/Copilot modes, nobody loads a system prompt. Responses will be generic, not G2-display-optimized (under 150 words, no markdown, etc.).

**Action:** Add `systemPrompt` field to `InferenceMode` config, or auto-load `gateway/agent_config/SOUL.md` at startup and prepend to every direct API request. Required for PR 3.

#### R4. Config duplicates `openclaw.json` provider definitions

The proposed `inference_config.json` re-declares the same Azure OpenAI provider already in `openclaw.json` (`models.providers.azure-oai-g2`). When the endpoint or model changes, both files must be updated.

**Action:** For OpenClaw mode, reference `openclaw.json` as the source of truth (don't duplicate). `inference_config.json` only stores non-OpenClaw mode parameters and the `activeMode` / `fallbackChain` top-level fields.

#### R5. `ResponseHandler` protocol missing `startup()` lifecycle method

No `async def startup()` counterpart to `close()`. `DirectAPIResponseHandler` needs to create an `aiohttp.ClientSession` once (not per request). `CopilotBridgeResponseHandler` needs to discover/spawn the adapter process.

**Action:** Add `startup()` to the protocol in PR 1. Existing handlers no-op.

### High Issues (should address)

#### R6. Copilot SDK `tools: []` behavior is unverified

The plan assumes `tools: []` disables all tool discovery. The SDK may still perform internal tool calls (e.g., `thinking`, `web_search` on certain models). Additionally, if `mcpServers` is passed, tool discovery still occurs regardless of `tools: []`.

**Action:** The gateway adapter must pass **neither** `tools` nor `mcpServers`. Add logging in the adapter for any unexpected `tool.execution_start` events.

#### R7. Copilot SDK has no `messages[]` array — prompt flattening is lossy

`session.sendAndWait({ prompt })` takes a flat string, not a conversation history array. Multi-turn conversations must be flattened with role markers, losing structural boundaries. The SDK also accumulates context internally with no reset API.

**Action:** Document the flattening strategy. Add `flattenMessages()` as a tested pure function. Add session TTL / max-requests config to prevent context overflow.

#### R8. `aiohttp` not in dependencies; SSE parsing is manual

`aiohttp` is not in `pyproject.toml`. Neither `aiohttp` nor `httpx` parse SSE natively.

**Action:** Add `httpx` + `httpx-sse` to `pyproject.toml` (lighter than aiohttp, cleaner SSE API). Extract an `async def iter_sse_events()` utility in `gateway/sse.py` shared by both direct and Copilot handlers.

#### R9. Anthropic Messages API underestimated

Anthropic uses a structurally different API: `system` is a top-level parameter, content blocks are arrays, streaming uses `content_block_delta` events, auth header is `x-api-key`. This is not a "parameter tweak to DirectAPIResponseHandler" — it's a distinct handler variant.

**Action:** Extract Anthropic into its own PR (new PR 6). Don't bundle with PR 5.

#### R10. `__main__.py` CLI routing not updated

The `_cli_commands` set in `__main__.py` hard-codes `{"init-env"}`. New CLI commands (`validate-config`, `switch-mode`) will silently fail unless this set is extended.

**Action:** Update `_cli_commands` in PR 2.

#### R11. `InferenceMode` should use `@dataclass(frozen=True)`, not Pydantic

Existing `GatewayConfig` is a frozen dataclass. Pydantic is not in `pyproject.toml` dependencies. Using Pydantic for `InferenceMode` alone is inconsistent and adds weight.

**Action:** Use `@dataclass(frozen=True)` with procedural validation in `load_inference_config()`.

### Medium Issues (nice to have)

#### R12. Copilot adapter process lifecycle unspecified

Who starts `gateway-adapter.ts`? The plan is silent. For v1, document manual startup (`npm run adapter`). Add a health endpoint (`GET /health`). Use `adapterUrl` in config (default `http://localhost:3001`).

#### R13. No mode indicator in phone protocol

The G2 app has no way to show which inference mode is active. Add the mode to the `connected` frame: `{"type":"connected","version":"1.0","inferenceMode":"local-ollama"}`.

#### R14. Backward compatibility when no config file exists

If `inference_config.json` is absent and `INFERENCE_MODE` is unset, behavior must be identical to today (OpenClaw if token present, Mock otherwise). Add an explicit test for this path in PR 2.

#### R15. PR 4 should be split into 4a (TypeScript) + 4b (Python)

PR 4 spans two languages and is marked Complexity: L. Splitting enables parallel review and smaller blast radius.

#### R16. `apiKeyRef` should align with OpenClaw convention

OpenClaw uses `apiKey: "env:VAR_NAME"`. The plan uses `apiKeyRef`. Align to `apiKey` for consistency.

### Revised PR Plan

| PR | Scope | Changes from original |
|----|-------|-----------------------|
| **PR 0** | Add `startup()` to `ResponseHandler` protocol | **New** — unblocks clean lifecycle in PR 3/4 |
| **PR 1** | Handler factory refactor | As planned + add `create_handler()` unit tests |
| **PR 2** | Config schema + validation | Use dataclass not Pydantic; update `__main__.py`; no OpenClaw provider duplication; backward-compat test |
| **PR 3** | Direct/Local handler | Add system prompt loading, conversation history buffer, `gateway/sse.py` utility; add `httpx`+`httpx-sse` deps |
| **PR 4a** | Copilot adapter (TypeScript) | Separate from Python handler; new `InferenceAdapter` class (not reuse `CopilotBridge`); health endpoint; session TTL |
| **PR 4b** | Copilot Python handler | `CopilotBridgeResponseHandler` consuming adapter SSE |
| **PR 5** | Fallback chain + `switch-mode` CLI | Tier-aware fallback (no cross-tier silent degradation) |
| **PR 6** | Anthropic handler | **New** — separate PR for structurally different API |
