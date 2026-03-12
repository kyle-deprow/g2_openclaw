# OpenRouter Provider — Implementation Plan

## Status: IMPLEMENTED

## Overview

Add **OpenRouter** as an alternative model provider alongside the existing Azure OpenAI provider. OpenRouter exposes an OpenAI-compatible API at `https://openrouter.ai/api/v1`, making it a drop-in replacement for the custom provider mechanism already used by OpenClaw.

This touches **two integration surfaces**:

1. **OpenClaw daemon** — add an `openrouter` custom provider in `openclaw.json` so the OpenClaw agent uses OpenRouter for inference.
2. **Push script** — resolve the `env:OPENROUTER_API_KEY` placeholder, same pattern as Azure.

No gateway Python code changes are needed — the gateway talks to OpenClaw over WebSocket; it doesn't call the LLM directly. The provider swap is entirely within the OpenClaw configuration layer.

---

## How Azure Hooks Up Today

```
G2 Glasses ──BLE──▶ PC Gateway ──WS──▶ OpenClaw daemon ──HTTPS──▶ Azure OpenAI
                     (Python)           (Node.js)                  (model-router)
```

1. **Azure Bicep** (`infra/main.bicep`) deploys an Azure AI Services resource with a `model-router` deployment.
2. **`gateway/openclaw_config/openclaw.json`** defines a custom provider `azure-oai-g2` pointing at the Azure endpoint, using `"apiKey": "env:AZURE_AI_SERVICES_API_KEY"`.
3. **`scripts/push-openclaw-config.sh`** deep-merges the repo config into `~/.openclaw/openclaw.json`, resolving `env:` API key placeholders to real values from `gateway/openclaw_config/.env`.
4. **`azure-api-version-preload.cjs`** monkey-patches `globalThis.fetch` to inject `?api-version=` on Azure requests (required because OpenClaw uses the standard OpenAI SDK, not AzureOpenAI).
5. The gateway's `cli.py` launch command sets `NODE_OPTIONS="--require ...preload.cjs"` when spawning the OpenClaw daemon.
6. Agent defaults set `"primary": "azure-oai-g2/model-router"` — all inference flows through Azure.

---

## What Changes for OpenRouter

### Key Insight

OpenRouter is **OpenAI-compatible** (`openai-completions` API type) and does NOT require:
- An `api-version` query parameter (no preload hack needed)
- Azure URL detection / rewriting
- Any Bicep infrastructure

It's purely a config + API key addition.

---

## 1. Files Modified

| File | Change |
|------|--------|
| `gateway/openclaw_config/openclaw.json` | Add `openrouter` provider block; update agent default model |
| `gateway/openclaw_config/.env.example` | Add `OPENROUTER_API_KEY=` |
| `gateway/openclaw_config/README.md` | Document OpenRouter setup, model selection |
| `scripts/push-openclaw-config.sh` | Resolve `env:OPENROUTER_API_KEY` same as Azure key |

### No files created, no files deleted.

---

## 2. Config Changes

### 2.1 `openclaw.json` — Add OpenRouter Provider

```json
{
  "models": {
    "providers": {
      "azure-oai-g2": { ... },
      "openrouter": {
        "baseUrl": "https://openrouter.ai/api/v1",
        "api": "openai-completions",
        "apiKey": "env:OPENROUTER_API_KEY",
        "models": [
          {
            "id": "anthropic/claude-sonnet-4-20250514",
            "name": "Claude Sonnet 4 (via OpenRouter)",
            "contextWindow": 200000,
            "maxTokens": 16384,
            "input": ["text"],
            "cost": { "input": 3.0, "output": 15.0, "cacheRead": 0.3, "cacheWrite": 3.75 },
            "reasoning": false
          },
          {
            "id": "openai/gpt-4.1",
            "name": "GPT-4.1 (via OpenRouter)",
            "contextWindow": 1047576,
            "maxTokens": 32768,
            "input": ["text"],
            "cost": { "input": 2.0, "output": 8.0, "cacheRead": 0.5, "cacheWrite": 0 },
            "reasoning": false
          },
          {
            "id": "google/gemini-2.5-flash-preview",
            "name": "Gemini 2.5 Flash Preview (via OpenRouter)",
            "contextWindow": 1048576,
            "maxTokens": 65536,
            "input": ["text"],
            "cost": { "input": 0.15, "output": 0.6, "cacheRead": 0.0375, "cacheWrite": 0 },
            "reasoning": false
          }
        ]
      }
    }
  }
}
```

### 2.2 Agent Default Model — Switchable

The `agents.defaults.model.primary` field controls which provider+model the agent uses. To switch between Azure and OpenRouter:

```json
// Azure (current)
"primary": "azure-oai-g2/model-router"

// OpenRouter — Claude
"primary": "openrouter/anthropic/claude-sonnet-4-20250514"

// OpenRouter — GPT-4.1
"primary": "openrouter/openai/gpt-4.1"
```

**Design decision:** We do NOT change the default in `openclaw.json`. The push script will accept an optional `--provider` flag (or env var) to select which provider becomes the active primary at push time. Azure remains the default.

### 2.3 `.env.example` Addition

```dotenv
# OpenRouter API key (alternative to Azure AI Services).
# Get one at: https://openrouter.ai/keys
OPENROUTER_API_KEY=
```

### 2.4 Push Script — Resolve OpenRouter Key

The existing `push-openclaw-config.sh` already has a pattern for resolving `env:` references. Add the same for `OPENROUTER_API_KEY`:

```bash
# After the Azure key resolution block:
if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
  MERGED=$(echo "${MERGED}" | jq --arg key "${OPENROUTER_API_KEY}" '
    (.models.providers // {}) |= with_entries(
      if .value.apiKey == "env:OPENROUTER_API_KEY" then
        .value.apiKey = $key
      else . end
    )
  ')
  echo "Resolved env:OPENROUTER_API_KEY (${#OPENROUTER_API_KEY} chars)."
fi
```

### 2.5 Push Script — Optional `--provider` Flag

Add a provider selection mechanism. When `OPENCLAW_PROVIDER=openrouter` is set (or `--provider openrouter` is passed), the push script overrides `agents.defaults.model.primary`:

```bash
# Provider selection (after merge, before write)
PROVIDER="${OPENCLAW_PROVIDER:-azure}"
case "${PROVIDER}" in
  azure)
    MODEL_PRIMARY="azure-oai-g2/model-router"
    ;;
  openrouter)
    MODEL_PRIMARY="openrouter/${OPENROUTER_MODEL:-anthropic/claude-sonnet-4-20250514}"
    ;;
  *)
    echo "ERROR: Unknown provider '${PROVIDER}'. Use 'azure' or 'openrouter'." >&2
    exit 1
    ;;
esac

MERGED=$(echo "${MERGED}" | jq --arg primary "${MODEL_PRIMARY}" '
  .agents.defaults.model.primary = $primary
')
echo "Active provider: ${PROVIDER} → ${MODEL_PRIMARY}"
```

---

## 3. No Gateway Python Changes Required

The Python gateway (`server.py`, `openclaw_client.py`) communicates with OpenClaw over WebSocket. It sends user text, receives streamed deltas. It never constructs LLM API calls directly. The provider swap is transparent to the gateway — OpenClaw handles all LLM routing internally.

**Verification:** The `ResponseHandler.handle()` → `OpenClawClient.send_message()` → WebSocket path is provider-agnostic. The only provider-aware code is in:
- `openclaw.json` (which provider config is active)
- `push-openclaw-config.sh` (which API keys get resolved)
- `azure-api-version-preload.cjs` (Azure-specific, harmless no-op for OpenRouter URLs)

---

## 4. No Bicep / Infrastructure Changes

OpenRouter is a third-party SaaS — no Azure resources to deploy. The existing Azure infrastructure remains intact for users who prefer it.

---

## 5. Testing

### 5.1 Push Script Validation

```bash
# Test: push with OpenRouter selected
OPENCLAW_PROVIDER=openrouter \
OPENROUTER_API_KEY=sk-or-test-xxx \
bash scripts/push-openclaw-config.sh

# Verify: check the merged config
jq '.agents.defaults.model.primary' ~/.openclaw/openclaw.json
# → "openrouter/anthropic/claude-sonnet-4-20250514"

jq '.models.providers.openrouter.apiKey' ~/.openclaw/openclaw.json
# → "sk-or-test-xxx"  (not "env:OPENROUTER_API_KEY")
```

### 5.2 Runtime Validation

```bash
# After push, verify OpenClaw sees the provider
openclaw models status
# Should list openrouter models

# Send a test message
openclaw chat "Hello, which model are you?"
# Should respond identifying as the OpenRouter model
```

### 5.3 Gateway E2E

```bash
# Launch the full stack with OpenRouter
OPENCLAW_PROVIDER=openrouter make sim

# In the simulator: tap to record → speak → verify response comes from OpenRouter model
```

---

## 6. OpenRouter-Specific Considerations

### 6.1 HTTP Headers

OpenRouter recommends sending `HTTP-Referer` and `X-Title` headers for app attribution and priority routing. OpenClaw uses the standard OpenAI SDK which doesn't add these by default. Two options:

- **Option A (recommended):** Ignore for now — OpenRouter works fine without them. Add later if needed for rate limit priority.
- **Option B:** Extend the `azure-api-version-preload.cjs` (rename to `fetch-preload.cjs`) to also inject OpenRouter headers. Low priority.

### 6.2 Model ID Format

OpenRouter model IDs use the format `provider/model-name` (e.g., `anthropic/claude-sonnet-4-20250514`). In the OpenClaw config, the model is referenced as `openrouter/<model-id>`, making the full reference `openrouter/anthropic/claude-sonnet-4-20250514`. This is consistent with how OpenClaw derives provider-prefixed model references.

### 6.3 Cost Tracking

The `cost` fields in the model config are informational (used by OpenClaw for session cost estimates). OpenRouter costs are per-million-tokens. Update these when switching models.

### 6.4 Rate Limits

OpenRouter has per-key rate limits. For development, the free tier is sufficient. For production, ensure the key has adequate credits.

---

## 7. Implementation Order

### Single PR — Size: S (~80 lines changed)

1. Add `openrouter` provider block to `openclaw.json`
2. Add `OPENROUTER_API_KEY=` to `.env.example`
3. Add OpenRouter key resolution to `push-openclaw-config.sh`
4. Add `OPENCLAW_PROVIDER` / `OPENROUTER_MODEL` env var support to push script
5. Update `gateway/openclaw_config/README.md` with OpenRouter docs
6. Test with real key

**No Python or TypeScript code changes. No new dependencies. No infrastructure changes.**

---

## 8. Usage After Implementation

```bash
# One-time setup
echo "OPENROUTER_API_KEY=sk-or-v1-xxxxx" >> gateway/openclaw_config/.env

# Push config with OpenRouter as active provider
OPENCLAW_PROVIDER=openrouter uv run python -m gateway push-config

# Or use a specific model
OPENCLAW_PROVIDER=openrouter OPENROUTER_MODEL=openai/gpt-4.1 uv run python -m gateway push-config

# Switch back to Azure
OPENCLAW_PROVIDER=azure uv run python -m gateway push-config

# Launch as normal
make sim
```
