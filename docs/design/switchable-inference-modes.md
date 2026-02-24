# Switchable Inference Modes — Integration & Testing Plan

## Overview

Add three switchable inference modes to the gateway:

| Mode | Backend | Use Case |
|------|---------|----------|
| **`openclaw`** (default) | OpenClaw WebSocket Gateway | Full agent with tools, memory, persona |
| **`copilot-sdk`** | GitHub Copilot SDK via `copilot_bridge` | Copilot-native coding tasks, BYOK providers |
| **`local`** | Local model (Ollama / llama.cpp) | Offline, zero-dependency, privacy-first |

All three modes converge on the same `ResponseHandler` protocol already defined in `server.py`, so the downstream wire format (WebSocket frames to the G2 app) is identical regardless of mode.

---

## 1. Integration Points — Files Modified & Created

### 1.1 Modified Files

| File | Change |
|------|--------|
| [gateway/config.py](../../gateway/config.py) | Add `inference_mode` field (`"openclaw" \| "copilot-sdk" \| "local"`), plus `local_model_url`, `local_model_name`, `copilot_bridge_url` env vars |
| [gateway/server.py](../../gateway/server.py) | Refactor `GatewayServer.__init__` handler selection from 2-branch (`openclaw_gateway_token` → OpenClaw, else → Mock) to 4-branch dispatch via `inference_mode` config |
| [tests/gateway/test_config.py](../../tests/gateway/test_config.py) | Add tests for new config fields, validation, defaults |
| [tests/gateway/conftest.py](../../tests/gateway/conftest.py) | Add fixtures for `copilot_gateway` and `local_gateway` |
| [copilot_bridge/src/config.ts](../../copilot_bridge/src/config.ts) | No changes needed — BYOK config already covers external provider routing |

### 1.2 New Files — Python Gateway

| File | Purpose |
|------|---------|
| `gateway/inference.py` | `InferenceMode` enum, `ResponseHandler` Protocol (moved from server.py), factory function `create_handler(config) → ResponseHandler` |
| `gateway/handlers/__init__.py` | Package init, re-exports |
| `gateway/handlers/mock_handler.py` | `MockResponseHandler` (moved from server.py) |
| `gateway/handlers/openclaw_handler.py` | `OpenClawResponseHandler` (moved from server.py) |
| `gateway/handlers/copilot_handler.py` | `CopilotSDKResponseHandler` — HTTP/WebSocket bridge to copilot_bridge MCP server |
| `gateway/handlers/local_handler.py` | `LocalResponseHandler` — OpenAI-compatible HTTP client for Ollama/llama.cpp |
| `tests/gateway/test_inference.py` | Tests for factory function, mode validation |
| `tests/gateway/test_copilot_handler.py` | Unit tests for CopilotSDKResponseHandler |
| `tests/gateway/test_local_handler.py` | Unit tests for LocalResponseHandler |

### 1.3 New Files — Copilot Bridge (TypeScript)

| File | Purpose |
|------|---------|
| `copilot_bridge/src/gateway-adapter.ts` | Thin WebSocket/HTTP adapter: accepts messages from Python gateway, delegates to `CopilotBridge.runTask()`, streams deltas back |
| `copilot_bridge/tests/gateway-adapter.test.ts` | Unit tests for the adapter |

---

## 2. Data Flow for Each Inference Mode

All three paths share the same entry and exit points:

```
G2 App ──WebSocket──▶ GatewaySession._handle_text(frame)
                          │
                          ├── status:thinking ──▶ G2 App
                          │
                          ▼
                    ┌─────────────┐
                    │ handler     │  ◀── ResponseHandler Protocol
                    │  .handle()  │
                    └─────┬───────┘
                          │
               ┌──────────┼──────────┐
               ▼          ▼          ▼
          [openclaw]  [copilot]  [local]
               │          │          │
               └──────────┼──────────┘
                          │
                          ├── status:streaming ──▶ G2 App
                          ├── assistant deltas ──▶ G2 App
                          ├── end ──▶ G2 App
                          └── status:idle ──▶ G2 App
```

### 2.1 OpenClaw Mode (existing)

```
_handle_text → OpenClawResponseHandler.handle()
  → OpenClawClient.send_message(text)
    → WebSocket to OpenClaw Gateway (localhost:18789)
    → auth handshake (if not connected)
    → agent request with sessionKey
    ← event stream: assistant deltas
    ← lifecycle:end
  → send_frame(status:streaming)
  → send_frame(assistant delta) × N
  → send_frame(end)
```

### 2.2 Copilot SDK Mode (new)

```
_handle_text → CopilotSDKResponseHandler.handle()
  → HTTP POST to copilot_bridge gateway-adapter (localhost:3100)
    {prompt: text, model: config.copilot_model, streaming: true}
  → gateway-adapter delegates to CopilotBridge.runTaskStreaming()
    → @github/copilot-sdk createSession + sendAndWait
    ← StreamingDelta events from SDK
  ← SSE/WebSocket stream of deltas back to Python handler
  → send_frame(status:streaming)
  → send_frame(assistant delta) × N
  → send_frame(end)
```

Key integration point: The Python `CopilotSDKResponseHandler` communicates with the TypeScript `copilot_bridge` process via HTTP (SSE for streaming) or WebSocket. The copilot_bridge already has `CopilotBridge.runTaskStreaming()` which returns `AsyncGenerator<StreamingDelta>`.

### 2.3 Local Mode (new)

```
_handle_text → LocalResponseHandler.handle()
  → HTTP POST to Ollama/llama.cpp (localhost:11434/v1/chat/completions)
    {model: config.local_model_name, messages: [{role:"user", content: text}], stream: true}
  ← SSE stream of {choices: [{delta: {content: "..."}}]}
  → send_frame(status:streaming)
  → send_frame(assistant delta) × N
  → send_frame(end)
```

Uses the OpenAI-compatible `/v1/chat/completions` endpoint that both Ollama and llama.cpp expose. No SDK dependency — raw `aiohttp` SSE client.

### 2.4 Divergence and Reconvergence Table

| Step | OpenClaw | Copilot SDK | Local |
|------|----------|-------------|-------|
| Entry | `_handle_text` | `_handle_text` | `_handle_text` |
| Handler | `OpenClawResponseHandler` | `CopilotSDKResponseHandler` | `LocalResponseHandler` |
| Transport | WebSocket | HTTP/SSE | HTTP/SSE |
| Auth | OpenClaw token | GitHub token / BYOK key | None |
| Protocol | OpenClaw wire protocol | Copilot SDK internal | OpenAI chat completions |
| Streaming | `async for delta in stream` | `async for delta in sse_stream` | `async for chunk in sse_stream` |
| Frame output | `send_frame()` | `send_frame()` | `send_frame()` |
| Error type | `OpenClawError` | `CopilotHandlerError` | `LocalHandlerError` |
| **Reconverge** | `send_frame(end)` | `send_frame(end)` | `send_frame(end)` |

---

## 3. Unit Test Plan

### 3.1 New Test Files

#### `tests/gateway/test_inference.py`

| Test | Mocks | Asserts |
|------|-------|---------|
| `test_create_handler_openclaw_mode` | None (just config) | Returns `OpenClawResponseHandler` |
| `test_create_handler_copilot_mode` | None | Returns `CopilotSDKResponseHandler` |
| `test_create_handler_local_mode` | None | Returns `LocalResponseHandler` |
| `test_create_handler_mock_fallback` | Config with `inference_mode=None` | Returns `MockResponseHandler` |
| `test_invalid_mode_raises` | Config with `inference_mode="bogus"` | `ValueError` |
| `test_copilot_mode_without_bridge_url_raises` | Missing `copilot_bridge_url` | `ValueError` |
| `test_local_mode_without_model_url_raises` | Missing `local_model_url` | `ValueError` |

#### `tests/gateway/test_copilot_handler.py`

Pattern: mirrors `test_server_openclaw.py` — `_FakeStream` equivalent but for HTTP/SSE.

| Test | Mocks | Asserts |
|------|-------|---------|
| `test_full_flow_streams_deltas_and_end` | Mock `aiohttp.ClientSession` returning SSE chunks | `send_frame` called with streaming → deltas → end |
| `test_copilot_error_propagates` | Mock SSE stream raises error | `CopilotHandlerError` propagated |
| `test_connection_refused` | `aiohttp.ClientError` | `CopilotHandlerError` with "connection refused" |
| `test_timeout` | Slow SSE stream | Timeout error |
| `test_empty_response` | SSE stream with `done` event, no deltas | streaming → end (no deltas) |

#### `tests/gateway/test_local_handler.py`

| Test | Mocks | Asserts |
|------|-------|---------|
| `test_full_flow_ollama_compat` | Mock `aiohttp` returning OpenAI-format SSE | streaming → deltas → end |
| `test_model_not_found` | 404 response | `LocalHandlerError` with model name |
| `test_connection_refused` | `aiohttp.ClientError` | `LocalHandlerError` |
| `test_malformed_sse_chunk_skipped` | Mix of valid + invalid JSON chunks | Valid deltas forwarded, invalid skipped |
| `test_empty_delta_ignored` | Chunks with `content: ""` | No assistant frames for empty content |

#### `tests/gateway/test_config.py` (additions)

| Test | Asserts |
|------|---------|
| `test_inference_mode_default_is_none` | `cfg.inference_mode is None` |
| `test_inference_mode_from_env` | `monkeypatch.setenv("INFERENCE_MODE", "local")` → `cfg.inference_mode == "local"` |
| `test_inference_mode_validates` | `monkeypatch.setenv("INFERENCE_MODE", "bogus")` → `ValueError` |
| `test_local_model_url_from_env` | Reads `LOCAL_MODEL_URL` |
| `test_copilot_bridge_url_from_env` | Reads `COPILOT_BRIDGE_URL` |
| `test_local_model_name_default` | Default is `"llama3.2"` |

### 3.2 Existing Test Files — Required Changes

| File | Change |
|------|--------|
| [tests/gateway/test_server.py](../../tests/gateway/test_server.py) | No changes — `MockResponseHandler` tests stay as-is (regression suite) |
| [tests/gateway/test_server_openclaw.py](../../tests/gateway/test_server_openclaw.py) | Update imports if `OpenClawResponseHandler` moves to `gateway/handlers/`. `TestAutoHandlerSelection` updated to test all 4 modes |
| [tests/gateway/conftest.py](../../tests/gateway/conftest.py) | Add `copilot_gateway` and `local_gateway` fixtures |

---

## 4. Integration Test Plan

### 4.1 Full-Path Integration Tests (Python)

Create `tests/integration/test_inference_modes.py`:

| Test | Setup | Verifies |
|------|-------|----------|
| `test_openclaw_mode_e2e` | Mock OpenClaw WS server (reuse `_mock_openclaw_handler`) | text → thinking → streaming → deltas → end → idle |
| `test_copilot_mode_e2e` | Mock HTTP/SSE server returning canned deltas | Same frame sequence |
| `test_local_mode_e2e` | Mock Ollama-compatible HTTP server | Same frame sequence |
| `test_mode_switching_at_startup` | Parameterize across modes | Each mode produces valid frame sequence |
| `test_fallback_to_mock_when_no_mode_configured` | No `INFERENCE_MODE` set, no `OPENCLAW_GATEWAY_TOKEN` | MockResponseHandler canned response |

### 4.2 Copilot Bridge Integration Tests (TypeScript)

Extend `copilot_bridge/tests/integration/`:

| Test | Setup | Verifies |
|------|-------|----------|
| `test_gateway_adapter_streams_deltas` | Mock `CopilotBridge` + HTTP client | SSE stream returns deltas correctly |
| `test_gateway_adapter_handles_error` | Mock bridge throws `BridgeError` | SSE stream returns error event |

---

## 5. Copilot SDK Mocking Strategy (No Real GitHub Token)

The existing copilot_bridge tests already demonstrate the pattern:

### 5.1 TypeScript Side (Already Solved)

From [client.test.ts](../../copilot_bridge/tests/client.test.ts):

```typescript
vi.mock("@github/copilot-sdk", () => ({
    CopilotClient: vi.fn().mockImplementation(() => mockClient),
}));
```

The entire `@github/copilot-sdk` is mocked at the module level via `vi.mock`. The `mockClient` provides `.ping()`, `.getAuthStatus()`, `.createSession()` — all returning controlled responses. **No real GitHub token or SDK binary needed.**

### 5.2 Python Side (New)

The Python gateway never imports `@github/copilot-sdk` directly. It communicates with `copilot_bridge` over HTTP/SSE. So the mocking strategy is:

1. **Unit tests**: Mock `aiohttp.ClientSession` to return canned SSE responses. No real HTTP server needed.
2. **Integration tests**: Spin up a minimal `aiohttp` server in the test that mimics the copilot_bridge gateway-adapter API — returns SSE events.
3. **No GitHub token, no SDK binary, no Node.js process** needed in Python CI.

```python
# Example: mock SSE response for CopilotSDKResponseHandler
async def mock_copilot_sse_handler(request):
    response = web.StreamResponse()
    response.content_type = "text/event-stream"
    await response.prepare(request)
    for delta in ["Hello ", "from ", "Copilot."]:
        await response.write(f"data: {json.dumps({'delta': delta})}\n\n".encode())
    await response.write(b"data: [DONE]\n\n")
    return response
```

### 5.3 BYOK Fallback for CI

The copilot_bridge already supports BYOK (Bring Your Own Key) providers — `openai`, `azure`, `anthropic`, `ollama`. CI can:

1. Use the fully mocked path (no real provider) for unit/integration tests.
2. Optionally run a smoke test with `COPILOT_BYOK_PROVIDER=ollama` + a local Ollama instance (same as the `local` mode test).

---

## 6. MockResponseHandler → Provider Abstraction Mapping

### 6.1 Current Architecture

```python
# server.py — current handler selection
class ResponseHandler(Protocol):
    async def handle(self, message: str, send_frame: Callable) -> None: ...
    async def close(self) -> None: ...

class MockResponseHandler:       # canned responses
class OpenClawResponseHandler:   # real OpenClaw client

class GatewayServer:
    def __init__(self, config, handler=None):
        if handler is not None:
            self._handler = handler           # explicit (tests)
        elif config.openclaw_gateway_token:
            self._handler = OpenClawResponseHandler(...)  # prod with OpenClaw
        else:
            self._handler = MockResponseHandler()          # dev fallback
```

### 6.2 New Architecture

```python
# gateway/inference.py
class InferenceMode(StrEnum):
    OPENCLAW = "openclaw"
    COPILOT_SDK = "copilot-sdk"
    LOCAL = "local"
    MOCK = "mock"

def create_handler(config: GatewayConfig) -> ResponseHandler:
    """Factory: create the appropriate handler based on config."""
    mode = config.inference_mode

    if mode is None:
        # Backward-compat: auto-detect from existing config
        if config.openclaw_gateway_token:
            mode = InferenceMode.OPENCLAW
        else:
            mode = InferenceMode.MOCK

    match mode:
        case InferenceMode.OPENCLAW:
            return OpenClawResponseHandler(OpenClawClient(...))
        case InferenceMode.COPILOT_SDK:
            return CopilotSDKResponseHandler(config.copilot_bridge_url, ...)
        case InferenceMode.LOCAL:
            return LocalResponseHandler(config.local_model_url, config.local_model_name)
        case InferenceMode.MOCK:
            return MockResponseHandler()

# server.py — simplified
class GatewayServer:
    def __init__(self, config, handler=None):
        self._handler = handler or create_handler(config)
```

### 6.3 Key Insight

The existing `ResponseHandler` Protocol is **already the correct abstraction**. Each new mode just implements the same `handle(message, send_frame)` signature. The mock handler becomes one of four equal implementations, not a special fallback. The `GatewaySession._handle_text()` method needs **zero changes** — it already delegates entirely to `self._handler.handle()`.

---

## 7. Migration Path — Incremental, Non-Breaking

### 7.1 Phase A: Extract Handler Abstraction (no behavior change)

1. Move `ResponseHandler`, `MockResponseHandler`, `OpenClawResponseHandler` from `server.py` to `gateway/handlers/` package.
2. Add `gateway/inference.py` with `InferenceMode` enum and `create_handler()` factory.
3. `server.py` imports from new location — all existing tests pass unchanged.
4. Add `inference_mode` field to `GatewayConfig` (default `None` for backward compat).
5. `create_handler()` with `mode=None` produces the same behavior as current code.

**Risk: Zero.** Pure refactor — `handler or create_handler(config)` produces identical results.

### 7.2 Phase B: Add Local Handler

1. Add `aiohttp` to `pyproject.toml` dependencies.
2. Implement `LocalResponseHandler` — simple OpenAI-compatible SSE client.
3. Add `LOCAL_MODEL_URL`, `LOCAL_MODEL_NAME` config fields.
4. Add `test_local_handler.py` with mocked `aiohttp`.
5. Existing functionality untouched — `local` mode only activates when `INFERENCE_MODE=local`.

**Risk: Low.** New code path, completely isolated.

### 7.3 Phase C: Add Copilot SDK Handler

1. Implement `copilot_bridge/src/gateway-adapter.ts` — HTTP server that bridges to `CopilotBridge`.
2. Implement `CopilotSDKResponseHandler` in Python — HTTP/SSE client for the adapter.
3. Add `COPILOT_BRIDGE_URL` config field.
4. Add `test_copilot_handler.py` with mocked HTTP.
5. Add `gateway-adapter.test.ts` on the TypeScript side.

**Risk: Medium.** Cross-process communication (Python ↔ Node.js). Mitigated by SSE (simple, well-tested protocol) and by testing each side independently.

### 7.4 Phase D: CLI & Documentation

1. Add `--mode` flag to `gateway-cli` (`gateway/cli.py`).
2. Update `docs/design/gateway.md` and `docs/guides/getting-started.md`.
3. Add integration test that exercises mode switching.

---

## 8. Suggested Implementation Order

### PR 1: Handler Abstraction Refactor *(Phase A)*
- **Size:** S (~200 lines moved, ~100 lines new)
- **Files:** `gateway/inference.py`, `gateway/handlers/`, `tests/gateway/test_inference.py`
- **Tests:** All existing tests + new factory tests
- **Reviewer signal:** "Pure refactor — diff shows only moves and a new enum"

### PR 2: Local Inference Mode *(Phase B)*
- **Size:** M (~300 lines new)
- **Files:** `gateway/handlers/local_handler.py`, `tests/gateway/test_local_handler.py`, config changes
- **Depends on:** PR 1
- **Tests:** Full unit coverage, integration test with mock Ollama server
- **Reviewer signal:** "Can test immediately with `ollama run llama3.2`"

### PR 3: Copilot Bridge Gateway Adapter *(Phase C, TypeScript side)*
- **Size:** M (~250 lines new)
- **Files:** `copilot_bridge/src/gateway-adapter.ts`, `copilot_bridge/tests/gateway-adapter.test.ts`
- **Depends on:** None (TypeScript only, can parallel with PR 2)
- **Tests:** Vitest unit tests with mocked `CopilotBridge`

### PR 4: Copilot SDK Response Handler *(Phase C, Python side)*
- **Size:** M (~250 lines new)
- **Files:** `gateway/handlers/copilot_handler.py`, `tests/gateway/test_copilot_handler.py`
- **Depends on:** PR 1, PR 3 (for API contract)
- **Tests:** Unit tests with mocked aiohttp, integration test with mock SSE server

### PR 5: CLI, Docs, E2E Tests *(Phase D)*
- **Size:** S (~150 lines)
- **Files:** `gateway/cli.py`, docs, `tests/integration/test_inference_modes.py`
- **Depends on:** PR 1–4
- **Tests:** Parameterized integration tests across all modes

---

## 9. CI Considerations

### 9.1 What Runs in CI (Fully Mocked)

| Test Suite | CI Runner | External Deps | Notes |
|------------|-----------|---------------|-------|
| `uv run pytest tests/gateway/` | GitHub Actions | None | All handlers mocked — `aiohttp` sessions mocked, OpenClaw mocked |
| `cd copilot_bridge && npm test` | GitHub Actions | None | `@github/copilot-sdk` fully mocked via `vi.mock` |
| `uv run pytest tests/gateway/test_inference.py` | GitHub Actions | None | Factory tests, pure logic |
| `uv run mypy gateway/` | GitHub Actions | None | Type checking |
| `uv run ruff check .` | GitHub Actions | None | Linting |

### 9.2 What Runs in CI with Dependencies Available

| Test Suite | CI Runner | External Deps | Notes |
|------------|-----------|---------------|-------|
| `uv run pytest tests/integration/test_inference_modes.py -k openclaw` | GitHub Actions | None | Uses in-process mock OpenClaw WS server |
| `uv run pytest tests/integration/test_inference_modes.py -k copilot` | GitHub Actions | None | Uses in-process mock SSE server |
| `uv run pytest tests/integration/test_inference_modes.py -k local` | GitHub Actions | None | Uses in-process mock Ollama HTTP server |

### 9.3 What Requires Manual Verification

| Test | Requires | How to Run |
|------|----------|------------|
| Real OpenClaw E2E | Running OpenClaw instance | `OPENCLAW_GATEWAY_TOKEN=... INFERENCE_MODE=openclaw uv run pytest tests/integration/ -k e2e` |
| Real Copilot SDK E2E | GitHub token + `copilot-sdk` binary | `COPILOT_GITHUB_TOKEN=... INFERENCE_MODE=copilot-sdk uv run pytest tests/integration/ -k e2e` |
| Real Local Model E2E | Running Ollama with model pulled | `INFERENCE_MODE=local LOCAL_MODEL_URL=http://localhost:11434 uv run pytest tests/integration/ -k e2e` |
| G2 Glasses Display | Physical hardware or simulator | Manual: speak → verify display renders correctly for each mode |

### 9.4 CI Environment Variables

```yaml
# .github/workflows/test.yml additions
env:
  INFERENCE_MODE: ""  # empty = mock fallback in tests
  # These are never set in CI — tests mock the transport layer
  # OPENCLAW_GATEWAY_TOKEN: ""
  # COPILOT_GITHUB_TOKEN: ""
  # LOCAL_MODEL_URL: ""
```

### 9.5 New Dependencies for CI

| Package | Where | Purpose |
|---------|-------|---------|
| `aiohttp` | `pyproject.toml` dependencies | HTTP/SSE client for copilot-sdk and local handlers |

`aiohttp` is the only new Python dependency. No Node.js needed in the Python CI pipeline — the copilot_bridge process is fully mocked from the Python side.

---

## Appendix: Config Field Summary

| Env Var | Config Field | Default | Required For |
|---------|-------------|---------|-------------|
| `INFERENCE_MODE` | `inference_mode` | `None` (auto-detect) | All modes |
| `OPENCLAW_HOST` | `openclaw_host` | `127.0.0.1` | `openclaw` mode |
| `OPENCLAW_PORT` | `openclaw_port` | `18789` | `openclaw` mode |
| `OPENCLAW_GATEWAY_TOKEN` | `openclaw_gateway_token` | `None` | `openclaw` mode |
| `COPILOT_BRIDGE_URL` | `copilot_bridge_url` | `http://127.0.0.1:3100` | `copilot-sdk` mode |
| `COPILOT_MODEL` | `copilot_model` | `None` (SDK default) | `copilot-sdk` mode |
| `LOCAL_MODEL_URL` | `local_model_url` | `http://127.0.0.1:11434` | `local` mode |
| `LOCAL_MODEL_NAME` | `local_model_name` | `llama3.2` | `local` mode |
| `AGENT_TIMEOUT` | `agent_timeout` | `120` | All modes |
