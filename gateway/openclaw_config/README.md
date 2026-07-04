# OpenClaw Config (Repo-Managed)

This directory contains the **repo-maintained** subset of the OpenClaw
configuration used by the G2 Gateway.

## Files

| File | Purpose |
|---|---|
| `openclaw.json` | Model providers (OpenAI/Codex + Azure + OpenRouter), agent defaults, session settings |
| `.env.example` | Template for API keys and provider selection env vars |
| `azure-api-version-preload.cjs` | Fetch preload that injects `?api-version=` for Azure |
| `README.md` | This file |

## Cold-Start Install (Fresh Machine)

```bash
# 1. Install OpenClaw globally (Node.js 22+ required)
sudo npm install -g openclaw

# 2. Create the ~/.openclaw/ scaffold
openclaw onboard --local

# 3. Authenticate OpenAI/Codex
openclaw models auth login --provider openai

# 4. Optional: copy env template if selecting Azure/OpenRouter or a non-default OpenAI model
cp gateway/openclaw_config/.env.example gateway/openclaw_config/.env

# 5. Push config + restart daemon (merges provider/runtime config and copies bootstrap files)
uv run python -m gateway push-config

# 6. Launch everything
uv run python -m gateway launch
```

Step 4 is idempotent — re-run it after any config change or key rotation.
The `launch` command handles `NODE_OPTIONS` for the OpenClaw daemon automatically.

## How It Works

The local OpenClaw installation keeps its full config at
`~/.openclaw/openclaw.json`. That file contains machine-local settings (gateway
auth tokens, wizard state, metadata timestamps) that **must not** be
overwritten.

The `openclaw.json` in this directory holds only the settings we want to
version-control — primarily the OpenAI/Codex runtime path, optional Azure and
OpenRouter providers, and agent defaults. The companion push script
(`scripts/push-openclaw-config.sh`) merges
these settings into the local config with `jq`, preserving everything else.

### What is managed here

- **Codex runtime** — enables the bundled `codex` plugin, sets the OpenAI
  provider `agentRuntime.id` to `codex`, and defaults the primary model to
  `openai/gpt-5.4`.
- **OpenAI provider** — declares `gpt-5.4` and `gpt-5-mini` model refs for
  authenticated OpenAI/Codex use.
- **Custom provider** `azure-oai-g2` — points at the Azure OpenAI GPT-5.4
  deployment (`gpt-5-4` on `oai-ss-aisense-dev-eastus2.openai.azure.com`).
- **Agent defaults** — primary model, compaction mode, concurrency limits,
  denied tools (browser, canvas, etc.).
- **Session / command settings** — DM scope, reaction scope, command modes.

### What is NOT managed here

- `gateway.auth.token` — generated locally by `openclaw onboard`.
- `wizard` / `meta` — bookkeeping written by the OpenClaw binary.
- Actual API keys — stored in `.env` (gitignored) or env vars.

## Deploying Config to the Local Machine

> **First time?** See the [Cold-Start Install](#cold-start-install-fresh-machine) section above.

### 1. Authenticate OpenAI/Codex

The default provider is `codex`, which uses OpenAI auth managed by OpenClaw:

```bash
openclaw models auth login --provider openai
```

No OpenAI key is stored in this repo. The login populates the local OpenClaw
auth store.

### 2. Optional env file

Copy the example env file when changing models or selecting Azure/OpenRouter:

```bash
cp gateway/openclaw_config/.env.example gateway/openclaw_config/.env
# Edit .env
```

For Azure, retrieve the key if your local config does not already have one:

```bash
az cognitiveservices account keys list \
  --name oai-ss-aisense-dev-eastus2 \
  --resource-group rg-ss-aisense-dev-eastus2 \
  --query key1 -o tsv
```

### 3. Run the push script

From the repo root:

```bash
# One-liner: push config and restart the daemon
uv run python -m gateway push-config

# Or push-only (no daemon restart)
uv run python -m gateway push-config --no-restart
```

Or run the underlying shell script directly:

```bash
bash scripts/push-openclaw-config.sh
```

The script will:

1. Back up `~/.openclaw/openclaw.json` → `~/.openclaw/openclaw.json.bak.<timestamp>`
2. Deep-merge the repo config into the local config (local-only keys preserved)
3. Set the selected primary model and fail if that model is not declared
4. Resolve `env:OPENROUTER_API_KEY` when OpenRouter is selected
5. Copy agent bootstrap files, repo skills, and `azure-api-version-preload.cjs`
6. Validate the result with `openclaw config validate`

The script is idempotent — safe to run repeatedly.

### 4. Enable the api-version preload for Azure only

The `gateway launch` command sets `NODE_OPTIONS` automatically when spawning the
OpenClaw daemon. If running OpenClaw standalone:

```bash
export NODE_OPTIONS="--require $HOME/.openclaw/azure-api-version-preload.cjs"
openclaw daemon
```

## Provider Selection

The push script selects the active provider via `OPENCLAW_PROVIDER`.

```bash
# Default OpenAI/Codex path
uv run python -m gateway push-config

# Use a specific configured OpenAI model
OPENCLAW_PROVIDER=codex OPENAI_MODEL=gpt-5-mini uv run python -m gateway push-config

# Use Azure explicitly
OPENCLAW_PROVIDER=azure uv run python -m gateway push-config

# Use OpenRouter explicitly
OPENCLAW_PROVIDER=openrouter OPENROUTER_MODEL=openai/gpt-4.1 uv run python -m gateway push-config
```

The default provider is `codex`. Unsupported model selections fail instead of
falling back to another provider or model.

## OpenAI/Codex Provider Details

| Setting | Value |
|---|---|
| Auth | `openclaw models auth login --provider openai` |
| Runtime | OpenAI provider `agentRuntime.id: "codex"` |
| Plugin | bundled `codex` plugin enabled |
| Default model | `openai/gpt-5.4` |
| Alternate model | `openai/gpt-5-mini` |

## Azure OpenAI Provider Details

| Setting | Value |
|---|---|
| Endpoint | `https://oai-ss-aisense-dev-eastus2.openai.azure.com/` |
| Deployment name | `gpt-5-4` |
| Model | GPT-5.4 (Azure OpenAI, eastus) |
| API type | `openai-completions` (OpenClaw's label for OpenAI-compatible APIs) |
| Context window | 1 047 576 tokens |
| Max output tokens | 32 768 |

OpenClaw auto-detects `*.openai.azure.com` URLs and rewrites them internally
to `<baseUrl>/openai/deployments/<modelId>`, so the deployment name must match
the model ID in the config (`gpt-5.4`).

## OpenRouter Provider

OpenRouter is an OpenAI-compatible router that gives access to models from
Anthropic, OpenAI, Google, and others through a single API key.

### Setup

1. Get an API key at <https://openrouter.ai/keys>
2. Add it to your `.env` file:
   ```bash
   cp gateway/openclaw_config/.env.example gateway/openclaw_config/.env
   # Edit .env and set OPENROUTER_API_KEY=sk-or-...
   ```

### Available Models

| Model ID | Name | Context | Max Tokens |
|---|---|---|---|
| `anthropic/claude-sonnet-4-20250514` | Claude Sonnet 4 | 200 000 | 16 384 |
| `openai/gpt-4.1` | GPT-4.1 | 1 047 576 | 32 768 |
| `google/gemini-2.5-flash-preview` | Gemini 2.5 Flash Preview | 1 048 576 | 65 536 |

OpenRouter does **not** need the `azure-api-version-preload.cjs` workaround —
it uses standard OpenAI-compatible endpoints.

## Azure API-Version Preload Workaround

Azure OpenAI requires every request to carry an `api-version` query parameter.
The official `AzureOpenAI` SDK client adds it automatically, but OpenClaw uses
the **regular `OpenAI` client**, which does not. Without the parameter Azure
returns **404**.

OpenClaw's config schema is validated with Zod and rejects unknown keys like
`defaultQuery`, so there is no declarative way to inject the parameter.

### How it works

`azure-api-version-preload.cjs` is a tiny CommonJS module that monkey-patches
`globalThis.fetch`. For any request whose hostname matches
`*.openai.azure.com`, it appends `?api-version=2024-12-01-preview` if the parameter
is not already present. All other requests pass through untouched.

### Enabling the preload

The push script (`scripts/push-openclaw-config.sh`) copies the file to
`~/.openclaw/`. Then export `NODE_OPTIONS` before starting the daemon:

```bash
export NODE_OPTIONS="--require $HOME/.openclaw/azure-api-version-preload.cjs"
openclaw daemon
```

You can add the export to your shell profile (`~/.bashrc`, `~/.zshrc`) so it
persists across sessions.

### Debugging

Set `AZURE_PRELOAD_DEBUG=1` to log every patched URL to stderr:

```bash
AZURE_PRELOAD_DEBUG=1 openclaw daemon
```
