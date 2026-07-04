---
title: Environment-Specific Parameter Files
impact: CRITICAL
impactDescription: clean separation, auditable configs, no conditional sprawl
tags: bicep, naming, parameters, environments, bicepparam
---

## Environment-Specific Parameter Files

Use separate `.bicepparam` files for each environment (`dev`, `stg`, `prod`).
Never use ternary expressions or long `if/else` chains inside templates to
switch behaviour by environment — it creates fragile, hard-to-audit code.

**Incorrect (inline environment conditionals):**

```bicep
// BAD: Environment logic pollutes the template
param env string

var skuName = env == 'prod' ? 'P1v3' : env == 'stg' ? 'S1' : 'B1'
var minInstances = env == 'prod' ? 3 : env == 'stg' ? 2 : 1
var enableZoneRedundancy = env == 'prod' ? true : false
var storageSku = env == 'prod' ? 'Standard_GRS' : 'Standard_LRS'

resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: 'asp-myapp-${env}'
  sku: { name: skuName }
  properties: { zoneRedundant: enableZoneRedundancy }
}
```

**Correct (separate parameter files per environment):**

```bicep
// main.bicep — clean, no env branching
param appServicePlanName string
param skuName string
param zoneRedundant bool
param location string
param tags object

resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  sku: { name: skuName }
  properties: { zoneRedundant: zoneRedundant }
  tags: tags
}
```

```bicep
// main.prod.bicepparam
using './main.bicep'

param appServicePlanName = 'asp-myapp-prod-eus2-001'
param skuName = 'P1v3'
param zoneRedundant = true
param location = 'eastus2'
param tags = { environment: 'prod', owner: 'platform-team' }
```

```bicep
// main.dev.bicepparam
using './main.bicep'

param appServicePlanName = 'asp-myapp-dev-eus2-001'
param skuName = 'B1'
param zoneRedundant = false
param location = 'eastus2'
param tags = { environment: 'dev', owner: 'platform-team' }
```

Reference: [Bicep parameter files — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/parameter-files)
