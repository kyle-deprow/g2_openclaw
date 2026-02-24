# OpenClaw Config (Repo-Managed)

This directory contains the **repo-maintained** subset of the OpenClaw
configuration used by the G2 Gateway.

## Files

| File | Purpose |
|---|---|
| `openclaw.json` | Model provider, agent defaults, session settings |
| `.env.example` | Template for the required `AZURE_OPENAI_API_KEY` secret |
| `azure-api-version-preload.cjs` | Fetch preload that injects `?api-version=` for Azure |
| `README.md` | This file |

## Cold-Start Install (Fresh Machine)

```bash
# 1. Install OpenClaw globally (Node.js 22+ required)
sudo npm install -g openclaw

# 2. Create the ~/.openclaw/ scaffold
openclaw onboard --local

# 3. Set the Azure API key (pick one)
#    Option A: fetch from Azure
az cognitiveservices account keys list \
  --name oai-ss-aisense-dev-eastus \
  --resource-group rg-ss-aisense-dev-eastus \
  --query key1 -o tsv \
  | xargs -I{} sh -c 'echo "AZURE_OPENAI_API_KEY={}" > gateway/openclaw_config/.env'
#    Option B: copy and edit
cp gateway/openclaw_config/.env.example gateway/openclaw_config/.env

# 4. Push config (merges provider, resolves API key, copies preload + SOUL.md)
bash scripts/push-openclaw-config.sh

# 5. Enable the api-version preload (persist in shell profile)
echo 'export NODE_OPTIONS="--require $HOME/.openclaw/azure-api-version-preload.cjs"' >> ~/.bashrc
source ~/.bashrc

# 6. Verify
openclaw agent --local --agent main -m "Say hello in exactly 5 words."
```

Steps 4-5 are idempotent — re-run them after any config change or key rotation.

## How It Works

The local OpenClaw installation keeps its full config at
`~/.openclaw/openclaw.json`. That file contains machine-local settings (gateway
auth tokens, wizard state, metadata timestamps) that **must not** be
overwritten.

The `openclaw.json` in this directory holds only the settings we want to
version-control — primarily the Azure OpenAI custom provider and agent
defaults. The companion push script (`scripts/push-openclaw-config.sh`) merges
these settings into the local config with `jq`, preserving everything else.

### What is managed here

- **Custom provider** `azure-oai-g2` — points at the Azure OpenAI deployment
  (`gpt-41` on `oai-ss-aisense-dev-eastus.openai.azure.com`).
- **Agent defaults** — primary model, compaction mode, concurrency limits,
  denied tools (browser, canvas, etc.).
- **Session / command settings** — DM scope, reaction scope, command modes.

### What is NOT managed here

- `gateway.auth.token` — generated locally by `openclaw onboard`.
- `wizard` / `meta` — bookkeeping written by the OpenClaw binary.
- Actual API keys — stored in `.env` (gitignored) or env vars.

## Deploying Config to the Local Machine

> **First time?** See the [Cold-Start Install](#cold-start-install-fresh-machine) section above.

### 1. Set the API key

Copy the example env file and fill in the key:

```bash
cp gateway/openclaw_config/.env.example gateway/openclaw_config/.env
# Edit .env and paste the key
```

Or retrieve it from Azure:

```bash
az cognitiveservices account keys list \
  --name oai-ss-aisense-dev-eastus \
  --resource-group rg-ss-aisense-dev-eastus \
  --query key1 -o tsv
```

### 2. Run the push script

From the repo root:

```bash
bash scripts/push-openclaw-config.sh
```

The script will:

1. Back up `~/.openclaw/openclaw.json` → `~/.openclaw/openclaw.json.bak.<timestamp>`
2. Deep-merge the repo config into the local config (local-only keys preserved)
3. Resolve `env:AZURE_OPENAI_API_KEY` → actual key value in the provider block
4. Copy `SOUL.md` and `azure-api-version-preload.cjs` to `~/.openclaw/`
5. Validate the result with `openclaw models status`

The script is idempotent — safe to run repeatedly.

### 3. Enable the api-version preload

```bash
export NODE_OPTIONS="--require $HOME/.openclaw/azure-api-version-preload.cjs"
```

Add to `~/.bashrc` or `~/.zshrc` for persistence.

## Azure OpenAI Provider Details

| Setting | Value |
|---|---|
| Endpoint | `https://oai-ss-aisense-dev-eastus.openai.azure.com/` |
| Deployment name | `gpt-41` |
| Model | GPT-4.1 (`2025-04-14`) |
| API type | `openai-completions` (OpenClaw's label for OpenAI-compatible APIs) |
| Context window | 1 047 576 tokens |
| Max output tokens | 32 768 |

OpenClaw auto-detects `*.openai.azure.com` URLs and rewrites them internally
to `<baseUrl>/openai/deployments/<modelId>`, so the deployment name must match
the model ID in the config (`gpt-41`).

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
`*.openai.azure.com`, it appends `?api-version=2024-10-21` if the parameter
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
