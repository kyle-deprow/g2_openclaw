---
title: No Hardcoded Environment-Specific Values
impact: HIGH
impactDescription: reusable templates, environment parity, single source of truth
tags: bicep, parameters, hardcoding, reusability
---

## No Hardcoded Environment-Specific Values

Everything that varies between environments — names, SKUs, capacities, feature
flags, IP ranges — must be a parameter or derived from one. Hardcoded values
create templates that work only in the author's environment and silently break
everywhere else.

**Incorrect (hardcoded environment values):**

```bicep
// BAD: Only works in prod eastus2 — useless for dev or other regions
resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: 'asp-spinesense-prod-eus2-001'
  location: 'eastus2'
  sku: {
    name: 'P1v3'
    capacity: 3
  }
  tags: {
    environment: 'prod'
  }
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stspinesenseprod001'
  location: 'eastus2'
  sku: { name: 'Standard_GRS' }
  kind: 'StorageV2'
}
```

**Correct (fully parameterized):**

```bicep
@description('App Service Plan name.')
param appServicePlanName string

@description('Azure region for all resources.')
param location string

@description('App Service Plan SKU.')
@allowed(['B1', 'S1', 'P1v3'])
param skuName string

@description('Number of worker instances.')
@minValue(1)
@maxValue(10)
param capacity int

@description('Storage account name.')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Storage SKU.')
@allowed(['Standard_LRS', 'Standard_GRS'])
param storageSku string

@description('Tags to apply to all resources.')
param tags object

resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  sku: { name: skuName, capacity: capacity }
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

Reference: [Bicep parameters — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/parameters)
