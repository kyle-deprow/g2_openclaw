---
title: Single Responsibility Modules
impact: CRITICAL
impactDescription: maintainable modules, predictable blast radius, reusable components
tags: bicep, modules, architecture, single-responsibility
---

## Single Responsibility Modules

Each Bicep module should deploy one logical resource or a tightly coupled group
of resources (e.g., a storage account and its private endpoint). Bundling
unrelated resources makes modules impossible to reuse and increases the blast
radius of changes.

**Incorrect (kitchen-sink module):**

```bicep
// BAD: One module deploys database, app service, AND key vault
module everything 'everything.bicep' = {
  name: 'deploy-everything'
  params: {
    sqlServerName: sqlServerName
    appServiceName: appServiceName
    keyVaultName: keyVaultName
    location: location
  }
}
```

**Correct (focused modules composed by parent):**

```bicep
// main.bicep orchestrates focused modules
module sqlServer 'modules/sql-server.bicep' = {
  name: 'deploy-sql-server'
  params: {
    serverName: sqlServerName
    location: location
    adminGroupObjectId: adminGroupObjectId
    tags: tags
  }
}

module appService 'modules/app-service.bicep' = {
  name: 'deploy-app-service'
  params: {
    appName: appServiceName
    location: location
    appServicePlanId: appServicePlan.outputs.planId
    tags: tags
  }
}

module keyVault 'modules/key-vault.bicep' = {
  name: 'deploy-key-vault'
  params: {
    vaultName: keyVaultName
    location: location
    tags: tags
  }
}
```

Reference: [Bicep modules — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/modules)

