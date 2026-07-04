---
title: Mandatory Resource Tags
impact: HIGH
impactDescription: cost tracking, ownership clarity, operational visibility
tags: bicep, resources, tagging, governance
---

## Mandatory Resource Tags

Every resource must carry at minimum: `environment`, `owner`, `costCenter`, and
`project`. Pass tags as an object parameter and apply them uniformly. Untagged
resources become orphans that no one can trace, budget, or decommission.

**Incorrect (no tags or inconsistent tagging):**

```bicep
// BAD: No tags — who owns this? Which project? What cost centre?
resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  sku: { name: 'P1v3' }
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  tags: {
    env: 'production' // Inconsistent key — should be 'environment'
  }
}
```

**Correct (mandatory tags applied consistently):**

```bicep
@description('Tags applied to every resource. Must include environment, owner, costCenter, project.')
param tags object

// Validate required keys via a user-defined type
type requiredTagsType = {
  environment: string
  owner: string
  costCenter: string
  project: string
}

resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  sku: { name: skuName }
  tags: tags
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: { name: storageSku }
  kind: 'StorageV2'
  tags: tags
}
```

```bicep
// main.prod.bicepparam — tags defined once, applied everywhere
param tags = {
  environment: 'prod'
  owner: 'platform-team'
  costCenter: 'CC-4200'
  project: 'SpineSense'
}
```

Reference: [Azure tagging strategy — Microsoft Learn](https://learn.microsoft.com/en-us/azure/cloud-adoption-framework/ready/azure-best-practices/resource-tagging)
