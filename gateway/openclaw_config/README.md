# OpenClaw Config (Repo-Managed)

This directory contains the **repo-maintained** subset of the OpenClaw
configuration used by the G2 Gateway.

## Files

| File | Purpose |
|---|---|
| `openclaw.json` | Model providers (OpenAI/Codex + Azure + OpenRouter), agent defaults, session settings |
| `.env.example` | Template for API keys and provider selection env vars |
| `azure-api-version-preload.cjs` | Fetch preload that injects `?api-version=` for Azure |
| `../mempalace_readonly_server.py` | Repo-managed read-only MemPalace MCP wrapper for non-PM agents |
| `README.md` | This file |

## Cold-Start Install (Fresh Machine)

```bash
# 1. Install OpenClaw globally (Node.js 22+ required)
sudo npm install -g openclaw

# 2. Create the ~/.openclaw/ scaffold
openclaw onboard --local

# 3. Install/enable the Codex plugin, then authenticate OpenAI/Codex
openclaw plugins install @openclaw/codex
openclaw models auth login --provider openai

# 4. Install/upgrade required MemPalace MCP server
make mempalace-install

# 5. Optional: copy env template if selecting Azure/OpenRouter or a non-default OpenAI model
cp gateway/openclaw_config/.env.example gateway/openclaw_config/.env

# 6. Push config + restart daemon (merges provider/runtime config and copies bootstrap files)
uv run python -m gateway push-config

# 7. Launch everything
uv run python -m gateway launch
```

Steps 4 and 6 are idempotent. Re-run the push step after any config change or
key rotation.
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

- **Codex runtime** — enables the `codex` plugin, sets the OpenAI provider
  `agentRuntime.id` to `codex`, defaults bounded execution stages to
  `openai/gpt-5.4`, keeps `main` as a G2 interface agent, and pins
  high-judgment PM/review/consensus stages to `openai/gpt-5.6-sol` high.
- **OpenAI provider** — declares `gpt-5.4`, `gpt-5.5`, `gpt-5.6-sol`,
  `gpt-5.6-terra`, and `gpt-5-mini` model refs for authenticated OpenAI/Codex
  use.
- **MemPalace-only research memory** — disables built-in OpenClaw memory search
  and memory flush, denies `memory_search`/`memory_get`, and requires the
  MemPalace MCP server split: full `mempalace` for `autoresearch-pm`, no
  MemPalace access for `main`, and filtered `mempalace-readonly` for every
  read-only stage agent.
- **Custom provider** `azure-oai-g2` — points at the Azure OpenAI GPT-5.4
  deployment (`gpt-5-4` on `oai-ss-aisense-dev-eastus2.openai.azure.com`).
- **Agent roster** — exact `main` interface agent, `autoresearch-pm`, and
  autoresearch stage-agent IDs, model assignments, high reasoning, MemPalace
  skill split, Quantipy methodology loading, tool denies, disabled built-in
  memory search/flush, and concurrency limits.
- **Managed bootstrap files** — `AGENTS.md`, `SOUL.md`, `TOOLS.md`, and
  `BOOTSTRAP.md` are copied to every configured OpenClaw agent workspace derived
  from `agents.list`; `main` uses `~/.openclaw/workspace`, `autoresearch-pm`
  and stage agents default to `~/.openclaw/workspace-{id}` unless `.workspace`
  is set, and local files such as `USER.md` and `IDENTITY.md` are left
  untouched.
- **Session / command settings** — DM scope, reaction scope, command modes.

### What is NOT managed here

- `gateway.auth.token` — generated locally by `openclaw onboard`.
- `wizard` / `meta` — bookkeeping written by the OpenClaw binary.
- Actual API keys — stored in `.env` (gitignored) or env vars.

## Deploying Config to the Local Machine

> **First time?** See the [Cold-Start Install](#cold-start-install-fresh-machine) section above.

### 1. Install the Codex plugin and authenticate OpenAI/Codex

The default provider is `codex`, which uses OpenAI auth managed by OpenClaw:

```bash
openclaw plugins install @openclaw/codex
openclaw models auth login --provider openai
```

No OpenAI key is stored in this repo. The login populates the local OpenClaw
auth store.

### 2. Install/upgrade required MemPalace MCP

```bash
make mempalace-install
```

The push script fails closed if MemPalace is missing or the MCP module is not
runnable.

### 3. Optional env file

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

### 4. Run the push script

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
3. Set the selected default model, force the repo-managed agent roster, and
   fail if any pinned agent model is not declared
4. Install the repo-managed MemPalace read-only wrapper, then resolve the full
   and read-only MemPalace MCP commands plus palace path
5. Resolve `env:OPENROUTER_API_KEY` when OpenRouter is selected
6. Validate repo-managed invariants for `main` interface isolation,
   `autoresearch-pm`, exact stage-agent models, high reasoning, memory policy,
   and the full/read-only MemPalace MCP server split
7. Copy managed agent bootstrap files to every configured agent workspace, copy
   repo skills after validating the referenced skill directories, and copy
   `azure-api-version-preload.cjs`
8. Validate the result with `openclaw config validate`

The script is idempotent — safe to run repeatedly.

### 5. Enable the api-version preload for Azure only

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

# Use a specific configured OpenAI model for unspecified/default agents
OPENCLAW_PROVIDER=codex OPENAI_MODEL=gpt-5-mini uv run python -m gateway push-config

# Use Azure explicitly
OPENCLAW_PROVIDER=azure uv run python -m gateway push-config

# Use OpenRouter explicitly
OPENCLAW_PROVIDER=openrouter OPENROUTER_MODEL=openai/gpt-4.1 uv run python -m gateway push-config
```

The default provider is `codex`. Unsupported model selections fail instead of
falling back to another provider or model.

Autoresearch stage-agent models are not selected by environment variables.
Change them in `gateway/openclaw_config/openclaw.json` and run the push script;
the script validates the exact model matrix before writing local OpenClaw
config.

## G2 Interface and PM Session

G2 traffic routes to `main`, the human-facing interface agent. It does not load
`mempalace` or `autoresearch`, has no stage-agent allowlist, and may only hand
human start/status/stop requests to deterministic control commands. Autonomous
research runs in `agent:autoresearch-pm:autoresearch:quantipy`; supervisors and
control commands communicate with OpenClaw there directly.

## OpenAI/Codex Provider Details

| Setting | Value |
|---|---|
| Auth | `openclaw models auth login --provider openai` |
| Runtime | OpenAI provider `agentRuntime.id: "codex"` |
| Plugin | `codex` plugin enabled; install with `openclaw plugins install @openclaw/codex` if needed |
| Default stage model | `openai/gpt-5.4` |
| G2 interface model | `openai/gpt-5.4` for `main` |
| Autoresearch PM model | `openai/gpt-5.6-sol` for `autoresearch-pm` |
| Frontier judgment model | `openai/gpt-5.6-sol` for PM, skeptic, consensus, and review |
| Data debate model | `openai/gpt-5.6-terra` |
| Microstructure debate model | `openai/gpt-5.5` |
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
