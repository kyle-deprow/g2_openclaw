---
title: Separate Parameter Files per Environment
impact: MEDIUM
impactDescription: auditable configs, clean promotions, eliminates env drift
tags: bicep, deployment, environments, parameters, bicepparam
---

## Separate Parameter Files per Environment

Maintain one `.bicepparam` file per environment. The template stays the same
across `dev`, `stg`, and `prod` — only the parameter values change. This ensures
every environment is deployed from the identical template, reducing
configuration drift.

**Incorrect (one param file with manual edits per deploy):**

```bicep
// BAD: Single file manually edited before each deployment
// main.bicepparam — "remember to change skuName before prod deploy"
using './main.bicep'

param appServicePlanName = 'asp-myapp-dev-001'  // ← change to prod name
param skuName = 'B1'                             // ← change to P1v3
param location = 'eastus2'
```

**Correct (dedicated file per environment):**

```text
├── main.bicep
├── main.dev.bicepparam
├── main.stg.bicepparam
└── main.prod.bicepparam
```

```bicep
// main.dev.bicepparam
using './main.bicep'

param appServicePlanName = 'asp-spinesense-dev-eus2-001'
param skuName = 'B1'
param capacity = 1
param location = 'eastus2'
param tags = {
  environment: 'dev'
  owner: 'platform-team'
  costCenter: 'CC-4200'
  project: 'SpineSense'
}
```

```bicep
// main.prod.bicepparam
using './main.bicep'

param appServicePlanName = 'asp-spinesense-prod-eus2-001'
param skuName = 'P1v3'
param capacity = 3
param location = 'eastus2'
param tags = {
  environment: 'prod'
  owner: 'platform-team'
  costCenter: 'CC-4200'
  project: 'SpineSense'
}
```

```bash
# CI/CD selects the correct param file by environment variable
az deployment group create \
  --resource-group "rg-spinesense-${ENV}" \
  --template-file main.bicep \
  --parameters "main.${ENV}.bicepparam"
```

Reference: [Bicep parameter files — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/parameter-files)
