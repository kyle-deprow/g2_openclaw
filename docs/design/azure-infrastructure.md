# Azure Infrastructure — Design Document

## 1. Overview

The G2 OpenClaw project deploys an **Azure AI Foundry** stack to host OpenAI models and support the end-to-end AI pipeline — from G2 glasses audio capture through the PC gateway to LLM inference and back.

The infrastructure is defined entirely in **Bicep** (IaC), deployed at subscription scope, and managed through a Python CLI wrapper. A single `main.bicep` orchestrator creates a resource group and delegates to six focused child modules.

## 2. Resource Architecture

```
Subscription-scope deployment
│
└── Resource Group (rg-{prefix}-{workload}-{env}-{location})
    │
    ├── 1. Monitoring
    │       ├── Log Analytics Workspace   (log-…)
    │       └── Application Insights      (appi-…)
    │
    ├── 2. Storage Account                (st…)       ← diag → Log Analytics
    │
    ├── 3. Key Vault                      (kv-…)      ← diag → Log Analytics
    │
    ├── 4. Azure OpenAI                   (oai-…)     ← diag → Log Analytics
    │       └── Model Deployment: gpt-4.1
    │
    ├── 5. AI Hub                         (aihub-…)   ← diag → Log Analytics
    │       ├── links → Storage, Key Vault, App Insights
    │       └── RBAC  → Cognitive Services OpenAI User on OpenAI account
    │
    └── 6. AI Project                     (aiproj-…)  ← diag → Log Analytics
            └── child of AI Hub
```

Every resource sends diagnostic logs and/or metrics to the shared Log Analytics workspace.

## 3. Module Reference

| # | Module | File | Resources Created | Key Config |
|---|--------|------|-------------------|------------|
| 1 | **Monitoring** | `modules/monitoring.bicep` | Log Analytics Workspace, Application Insights | PerGB2018 SKU, configurable retention (30–730 days) |
| 2 | **Storage** | `modules/storage.bicep` | Storage Account (StorageV2) | TLS 1.2 enforced, no public blob access, Hot tier |
| 3 | **Key Vault** | `modules/keyvault.bicep` | Key Vault | RBAC auth, soft-delete (90 days), purge protection |
| 4 | **OpenAI** | `modules/openai.bicep` | Cognitive Services account + model deployments | S0 SKU, system-assigned identity, configurable model array |
| 5 | **AI Hub** | `modules/ai-hub.bicep` | ML workspace (kind: Hub) + RBAC role assignment | System-assigned identity, Basic SKU, links all upstream resources |
| 6 | **AI Project** | `modules/ai-project.bicep` | ML workspace (kind: Project) | Child of AI Hub, Basic SKU, system-assigned identity |

## 4. Naming Convention

All resources follow the pattern:

```
{prefix}-{workload}-{environment}-{location}
```

with a resource-type prefix prepended:

| Resource | Prefix | Example (dev) |
|----------|--------|---------------|
| Resource Group | `rg-` | `rg-ss-aisense-dev-eastus` |
| Log Analytics | `log-` | `log-ss-aisense-dev-eastus` |
| App Insights | `appi-` | `appi-ss-aisense-dev-eastus` |
| Storage Account | `st` | `stssaisensedev` (24-char alphanumeric limit) |
| Key Vault | `kv-` | `kv-ss-aisense-dev-eastu` (24-char limit) |
| Azure OpenAI | `oai-` | `oai-ss-aisense-dev-eastus` |
| AI Hub | `aihub-` | `aihub-ss-aisense-dev-eastus` |
| AI Project | `aiproj-` | `aiproj-ss-aisense-dev-eastus` |

## 5. Security Configuration

| Area | Setting |
|------|---------|
| **Key Vault auth** | RBAC authorization (`enableRbacAuthorization: true`), no access policies |
| **Key Vault protection** | Soft-delete enabled (90-day retention), purge protection on |
| **Storage** | HTTPS-only, TLS 1.2 minimum, blob public access disabled |
| **OpenAI auth** | `disableLocalAuth: false` in dev (API keys allowed); set to `true` for prod |
| **Managed identities** | System-assigned on OpenAI account, AI Hub, and AI Project |
| **RBAC role grants** | AI Hub → `Cognitive Services OpenAI User` on the OpenAI account |
| **Network ACLs** | `defaultAction: Allow` with `bypass: AzureServices` (Key Vault); `publicNetworkAccess` parameterised |
| **Bicep linting** | `bicepconfig.json` enforces: no hardcoded URLs/locations, no unused params/vars, secure param defaults, no unnecessary dependsOn |

## 6. Parameters

Parameters are defined in `main.bicep` and supplied via `.bicepparam` files.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `prefix` | `string` (2–6 chars) | — (required) | Organisation abbreviation for naming |
| `workload` | `string` | `'aisense'` | Workload identifier |
| `environment` | `string` | — (required) | `dev` / `staging` / `prod` |
| `location` | `string` | `'eastus'` | Azure region |
| `tags` | `object` | — (required) | Resource tags (`environment`, `owner`, `costCenter`, `project`) |
| `modelDeployments` | `modelDeploymentConfig[]` | — (required) | Array of model deployment specs (name, model, version, capacity, RPM) |
| `publicNetworkAccess` | `string` | `'Enabled'` | `Enabled` or `Disabled` |
| `storageSkuName` | `string` | `'Standard_LRS'` | `Standard_LRS`, `Standard_GRS`, or `Standard_ZRS` |
| `logRetentionInDays` | `int` | `30` | Log Analytics retention (30–730) |

### `modelDeploymentConfig` type

```
{ name, modelName, modelVersion, capacity, rateLimitPerMinute }
```

## 7. Infra CLI

A Typer-based Python CLI in `infra/` wraps `az` CLI commands. All commands require `az` CLI and the Bicep extension.

### Commands

| Command | Description | Key Flags |
|---------|-------------|-----------|
| `deploy` | Validate → what-if → deploy (subscription-scope) | `--subscription-id`, `--location`, `--param-file`, `--dry-run`, `--confirm/--no-confirm` |
| `what-if` | Preview infrastructure changes without deploying | `--subscription-id`, `--location`, `--param-file` |
| `validate` | Validate Bicep templates only | `--subscription-id`, `--location`, `--param-file` |
| `lint` | Run Bicep linter (`az bicep build`) | `--template-path` (default: `infra/main.bicep`) |
| `destroy` | Delete a resource group | `--subscription-id`, `--resource-group`, `--confirm/--no-confirm` |

### Usage Examples

```bash
# Full deployment to dev
python -m infra deploy \
  --subscription-id $AZURE_SUBSCRIPTION_ID \
  --location eastus \
  --param-file infra/parameters/dev.bicepparam

# Dry run (validate + what-if, no changes)
python -m infra deploy \
  --subscription-id $AZURE_SUBSCRIPTION_ID \
  --param-file infra/parameters/dev.bicepparam \
  --dry-run

# Preview changes
python -m infra what-if \
  --subscription-id $AZURE_SUBSCRIPTION_ID

# Lint only
python -m infra lint

# Tear down
python -m infra destroy \
  --subscription-id $AZURE_SUBSCRIPTION_ID \
  --resource-group rg-ss-aisense-dev-eastus
```

The CLI uses Rich for formatted output — colour-coded success/error messages, progress spinners, and panelled deployment results.

## 8. Environment Configurations

| Environment | Param File | Status |
|-------------|------------|--------|
| **dev** | `infra/parameters/dev.bicepparam` | Active |
| staging | — | Not yet created |
| prod | — | Not yet created |

**Dev configuration highlights:**

- Prefix: `ss`, workload: `aisense`, region: `eastus`
- Public network access enabled (for development convenience)
- Single model deployment: `gpt-4.1` (version `2025-04-14`), 15K TPM capacity, 100 RPM
- Standard LRS storage, 30-day log retention
- Tags: `environment=dev`, `owner=platform-team`, `costCenter=CC-1234`, `project=SpineSense`

## 9. Relation to G2 OpenClaw

The Azure infrastructure supports the G2 OpenClaw real-time AI pipeline:

```
G2 Glasses  ──BLE──▶  PC Gateway  ──HTTPS──▶  Azure OpenAI (gpt-4.1)
                        │                          │
                        │ telemetry                 │ inference
                        ▼                          ▼
                   App Insights              AI Hub / AI Project
                   (appi-…)                  (model management)
                        │
                   Key Vault
                   (secrets: API keys, connection strings)
```

| Azure Resource | Gateway Usage |
|----------------|---------------|
| **Azure OpenAI** | The gateway's `openclaw_client` sends LLM requests to the OpenAI endpoint for real-time inference (chat completions from transcribed audio) |
| **Application Insights** | Telemetry sink for gateway operations — request latency, error rates, audio session metrics |
| **Key Vault** | Stores secrets consumed by the gateway at runtime — OpenAI API keys, App Insights connection strings |
| **AI Hub + AI Project** | Centralised model management and experimentation within Azure AI Foundry; the AI Hub's managed identity authenticates to OpenAI via RBAC |

### Deployment Outputs

`main.bicep` emits these outputs for downstream configuration:

| Output | Value |
|--------|-------|
| `resourceGroupName` | Name of the created resource group |
| `openAiEndpoint` | Azure OpenAI endpoint URL |
| `openAiAccountName` | OpenAI account name |
| `appInsightsConnectionString` | App Insights connection string |
| `keyVaultUri` | Key Vault URI |
| `aiHubId` | AI Hub resource ID |
| `aiProjectId` | AI Project resource ID |
