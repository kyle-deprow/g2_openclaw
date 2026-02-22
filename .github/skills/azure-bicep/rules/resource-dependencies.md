---
title: Implicit Dependencies over dependsOn
impact: HIGH
impactDescription: simpler templates, automatic ordering, fewer stale dependency bugs
tags: bicep, resources, dependencies, dependsOn
---

## Implicit Dependencies over dependsOn

Let Bicep infer deployment order from property references (implicit
dependencies). Only use `dependsOn` when there is no property-level reference
but a deployment ordering requirement still exists. Explicit `dependsOn` is
fragile — it is easy to forget to update and hides the actual relationship.

**Incorrect (unnecessary dependsOn):**

```bicep
// BAD: dependsOn is redundant — serverFarmId already creates the dependency
resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  sku: { name: 'P1v3' }
}

resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  properties: {
    serverFarmId: appServicePlan.id // This reference IS the dependency
  }
  dependsOn: [
    appServicePlan // Redundant — clutters the template
  ]
}
```

**Correct (implicit dependency via property reference):**

```bicep
resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  sku: { name: 'P1v3' }
}

resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  properties: {
    serverFarmId: appServicePlan.id  // Implicit dependency — Bicep handles ordering
  }
}

// Use dependsOn only when there is no property reference
resource diagnosticSetting 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'send-to-law'
  scope: webApp
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [ { categoryGroup: 'allLogs', enabled: true } ]
  }
  dependsOn: [
    webApp // Required — no property reference to webApp exists above
  ]
}
```

Reference: [Bicep resource dependencies — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/resource-dependencies)
