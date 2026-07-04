---
title: Descriptive Symbolic Names
impact: CRITICAL
impactDescription: readable templates, self-documenting resources, easier reviews
tags: bicep, naming, symbolic-names, readability
---

## Descriptive Symbolic Names

Use descriptive camelCase symbolic names that clearly convey a resource's purpose.
Avoid abbreviations, numbered suffixes, or generic labels. When someone reads
`sqlDatabase` they understand the resource immediately — `db1` tells them nothing.

**Incorrect (cryptic symbolic names):**

```bicep
// BAD: Symbolic names give no hint of purpose
resource asp1 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  sku: { name: skuName }
}

resource app1 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  properties: { serverFarmId: asp1.id }
}

resource db 'Microsoft.Sql/servers/databases@2023-08-01-preview' = {
  name: databaseName
  parent: srv
  location: location
}

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
}
```

**Correct (descriptive symbolic names):**

```bicep
resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  sku: { name: skuName }
}

resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  properties: { serverFarmId: appServicePlan.id }
}

resource sqlDatabase 'Microsoft.Sql/servers/databases@2023-08-01-preview' = {
  name: databaseName
  parent: sqlServer
  location: location
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
}
```

Reference: [Bicep best practices — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/best-practices#resource-definitions)
