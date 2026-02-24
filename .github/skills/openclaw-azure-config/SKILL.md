---
name: openclaw-azure-config
description:
  OpenClaw Azure OpenAI provider configuration, cold-start setup, API key management, and the api-version fetch preload workaround. Use when setting up OpenClaw with Azure OpenAI from scratch, debugging 401/404 inference errors, managing the push-config workflow, or configuring the NODE_OPTIONS preload. Triggers on tasks involving openclaw.json provider config, push-openclaw-config.sh, azure-api-version-preload.cjs, Azure OpenAI deployment endpoints, env:VAR_NAME apiKey resolution, or cold-start OpenClaw installation.
---

# OpenClaw Azure OpenAI Configuration

Set up and maintain the OpenClaw → Azure OpenAI integration. Covers cold-start
install, the repo-managed config overlay, API key resolution, and the
api-version preload workaround.

## When to Apply

Reference these guidelines when:

- Setting up OpenClaw on a fresh machine (cold start)
- Configuring a custom Azure OpenAI provider in OpenClaw
- Debugging HTTP 401 (auth) or 404 (missing api-version) errors from Azure
- Running or modifying `scripts/push-openclaw-config.sh`
- Working with `gateway/openclaw_config/` files
- Changing the Azure OpenAI deployment (model, region, capacity)
- Rotating the Azure API key
- Configuring `NODE_OPTIONS` for the api-version preload

## Rule Categories by Priority

| Priority | Category                | Impact   | Prefix      |
| -------- | ----------------------- | -------- | ----------- |
| 1        | Cold-Start Install      | CRITICAL | `install-`  |
| 2        | Provider Config         | CRITICAL | `provider-` |
| 3        | API Key Management      | CRITICAL | `auth-`     |
| 4        | API-Version Preload     | HIGH     | `preload-`  |
| 5        | Push Script             | MEDIUM   | `push-`     |

---

## 1. Cold-Start Install (CRITICAL)

### `install-prerequisites`
Before configuring the provider, OpenClaw must be installed and onboarded:

| Tool         | Version | Install                               |
| ------------ | ------- | ------------------------------------- |
| Node.js      | ≥ 22    | `nvm install 22`                      |
| OpenClaw CLI | latest  | `sudo npm install -g openclaw`        |
| jq           | any     | `sudo apt install jq` / `brew install jq` |
| Azure CLI    | any     | Only if fetching the API key from Azure |

### `install-onboard-first`
`openclaw onboard --local` **must** run before the push script. It creates the
`~/.openclaw/` directory structure, generates gateway auth tokens, and writes
the base `openclaw.json` that the push script merges into.

```bash
openclaw onboard --local
```

### `install-cold-start-sequence`
The complete cold-start sequence from a freshly cloned repo:

```bash
# 1. Install & onboard OpenClaw
sudo npm install -g openclaw
openclaw onboard --local

# 2. Set the Azure API key
#    Option A: fetch from Azure
az cognitiveservices account keys list \
  --name oai-ss-aisense-dev-eastus \
  --resource-group rg-ss-aisense-dev-eastus \
  --query key1 -o tsv \
  | xargs -I{} sh -c 'echo "AZURE_OPENAI_API_KEY={}" > gateway/openclaw_config/.env'

#    Option B: copy from .env.example and paste manually
cp gateway/openclaw_config/.env.example gateway/openclaw_config/.env
# then edit gateway/openclaw_config/.env

# 3. Push repo config (merges provider, resolves API key, copies preload + SOUL.md)
bash scripts/push-openclaw-config.sh

# 4. Enable the api-version preload (persist in shell profile)
echo 'export NODE_OPTIONS="--require $HOME/.openclaw/azure-api-version-preload.cjs"' >> ~/.bashrc
source ~/.bashrc

# 5. Verify
openclaw agent --local --agent main -m "Say hello in exactly 5 words."
```

### `install-idempotent-push`
The push script is idempotent. Re-run it any time the repo config changes or
after rotating an API key. It backs up the local config before every merge.

---

## 2. Provider Config (CRITICAL)

### `provider-repo-managed-overlay`
The repo config at `gateway/openclaw_config/openclaw.json` is a **partial overlay**,
not a complete OpenClaw config. It contains only version-controlled settings:
- Custom provider `azure-oai-g2` with model definitions
- Agent defaults (primary model, compaction, concurrency)
- Session / command settings

Machine-local settings (`gateway.auth.token`, `wizard`, `meta`) live only in
`~/.openclaw/openclaw.json` and are preserved by the jq deep-merge.

### `provider-baseurl-format`
For Azure OpenAI, the `baseUrl` must include the full deployment path:

```
https://<resource>.openai.azure.com/openai/deployments/<deployment-name>
```

OpenClaw uses the regular OpenAI SDK client (not `AzureOpenAI`), so it won't
prepend `/openai/deployments/<model>` automatically. The deployment name in the
URL must match the model `id` in the config.

**Current value:**
```
https://oai-ss-aisense-dev-eastus.openai.azure.com/openai/deployments/gpt-41
```

### `provider-api-type`
Use `"api": "openai-completions"` for Azure OpenAI chat-completions endpoints.
This tells OpenClaw to use the standard OpenAI SDK `chat.completions.create()`.
The `openai-responses` API type also works but targets the newer responses
endpoint.

### `provider-model-id-matches-deployment`
The model `id` field (e.g., `gpt-41`) must exactly match the Azure deployment
name. OpenClaw uses this ID when constructing API requests.

### `provider-config-schema-constraints`
OpenClaw validates provider config with a strict Zod schema. Only these keys
are accepted on a provider object:

| Key          | Type                     | Required |
| ------------ | ------------------------ | -------- |
| `baseUrl`    | `string` (min 1)         | **Yes**  |
| `apiKey`     | `string`                 | No       |
| `auth`       | enum                     | No       |
| `api`        | enum                     | No       |
| `headers`    | `Record<string, string>` | No       |
| `authHeader` | `boolean`                | No       |
| `models`     | `ModelDefinition[]`      | **Yes**  |

**Rejected keys** that cause validation errors: `defaultQuery`, `defaultHeaders`,
`apiVersion`, or any other key not listed above.

---

## 3. API Key Management (CRITICAL)

### `auth-env-placeholder`
The repo config uses `"apiKey": "env:AZURE_OPENAI_API_KEY"` as a placeholder.
**OpenClaw does NOT resolve `env:` prefixes** for custom provider apiKey fields.
The push script substitutes the actual value at merge time.

### `auth-push-resolves-key`
`scripts/push-openclaw-config.sh` loads the key from
`gateway/openclaw_config/.env` (or the shell environment) and replaces any
`"env:AZURE_OPENAI_API_KEY"` value in provider configs with the real key.
The actual key is written to `~/.openclaw/openclaw.json` — never committed.

### `auth-bearer-works`
Azure OpenAI accepts both `api-key: <key>` and `Authorization: Bearer <key>`
headers. The standard OpenAI SDK sends `Authorization: Bearer`, which works.

### `auth-rotate-key`
To rotate the API key:

```bash
# 1. Get the new key
az cognitiveservices account keys list \
  --name oai-ss-aisense-dev-eastus \
  --resource-group rg-ss-aisense-dev-eastus \
  --query key1 -o tsv

# 2. Update .env
echo "AZURE_OPENAI_API_KEY=<new-key>" > gateway/openclaw_config/.env

# 3. Re-push
bash scripts/push-openclaw-config.sh
```

---

## 4. API-Version Preload (HIGH)

### `preload-why`
Azure OpenAI requires `?api-version=2024-10-21` on every request. The standard
OpenAI SDK client doesn't add it. OpenClaw's config schema rejects
`defaultQuery`. Without the parameter, Azure returns **HTTP 404**.

### `preload-mechanism`
`gateway/openclaw_config/azure-api-version-preload.cjs` is a CommonJS module
that monkey-patches `globalThis.fetch`. For requests to `*.openai.azure.com`,
it appends `?api-version=2024-10-21` if not already present. All other
requests pass through unchanged.

### `preload-activation`
The preload must be loaded via `NODE_OPTIONS` before any OpenClaw process:

```bash
export NODE_OPTIONS="--require $HOME/.openclaw/azure-api-version-preload.cjs"
```

Add to `~/.bashrc` or `~/.zshrc` for persistence. The push script copies the
file to `~/.openclaw/` automatically.

### `preload-debug`
Set `AZURE_PRELOAD_DEBUG=1` to log every patched URL to stderr:

```bash
AZURE_PRELOAD_DEBUG=1 openclaw agent --local --agent main -m "test"
# [azure-preload] https://...com/openai/deployments/gpt-41/chat/completions → ...?api-version=2024-10-21
```

### `preload-version-bump`
If Azure deprecates `2024-10-21`, update the `AZURE_API_VERSION` constant in
`azure-api-version-preload.cjs` and re-push.

---

## 5. Push Script (MEDIUM)

### `push-what-it-does`
`scripts/push-openclaw-config.sh` performs these steps in order:

1. Backs up `~/.openclaw/openclaw.json` with a UTC timestamp suffix
2. Deep-merges the repo config over the local config (jq `*` operator)
3. Cleans stale model entries that don't match the repo's primary model
4. Resolves `env:AZURE_OPENAI_API_KEY` → actual key value
5. Writes the merged config
6. Copies `SOUL.md` to `~/.openclaw/`
7. Copies `azure-api-version-preload.cjs` to `~/.openclaw/`
8. Runs `openclaw models status` to validate

### `push-prerequisites`
- `jq` must be installed
- `openclaw onboard --local` must have been run (creates `~/.openclaw/`)
- `AZURE_OPENAI_API_KEY` must be set (env or `.env` file)

### `push-safe-to-rerun`
The script is idempotent. It creates a timestamped backup before every merge.
Safe to run on config changes, key rotations, or just to verify state.

---

## Troubleshooting

### HTTP 404 from Azure
**Cause:** Missing `?api-version` query parameter.
**Fix:** Ensure `NODE_OPTIONS` is set with the preload before running OpenClaw:
```bash
export NODE_OPTIONS="--require $HOME/.openclaw/azure-api-version-preload.cjs"
```

### HTTP 401 from Azure
**Cause:** API key not resolved — the literal string `env:AZURE_OPENAI_API_KEY`
was sent as the Bearer token.
**Fix:** Re-run the push script with the key available:
```bash
source gateway/openclaw_config/.env
bash scripts/push-openclaw-config.sh
```

### "Unrecognized key" config validation error
**Cause:** A key not in the Zod schema was added to a provider config (e.g.,
`defaultQuery`, `apiVersion`).
**Fix:** Remove the offending key. See `provider-config-schema-constraints`
for the full list of accepted keys.

### `openclaw onboard` not run
**Symptom:** Push script fails with "Local OpenClaw config not found."
**Fix:** Run `openclaw onboard --local` first.

---

## File Reference

| File | Purpose |
| ---- | ------- |
| `gateway/openclaw_config/openclaw.json` | Repo-managed config overlay (version-controlled) |
| `gateway/openclaw_config/.env.example` | Template for `AZURE_OPENAI_API_KEY` |
| `gateway/openclaw_config/.env` | Actual API key (gitignored) |
| `gateway/openclaw_config/azure-api-version-preload.cjs` | Fetch preload for api-version injection |
| `gateway/openclaw_config/README.md` | Detailed README for this directory |
| `gateway/agent_config/SOUL.md` | Agent persona / system prompt |
| `scripts/push-openclaw-config.sh` | Merge + deploy script |
| `~/.openclaw/openclaw.json` | Live OpenClaw config (local, not committed) |
| `~/.openclaw/azure-api-version-preload.cjs` | Deployed copy of the preload |
