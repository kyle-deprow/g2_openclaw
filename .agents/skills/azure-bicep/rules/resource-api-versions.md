---
title: Pin Stable API Versions
impact: HIGH
impactDescription: reproducible deployments, no surprise breaking changes, production stability
tags: bicep, resources, api-versions, stability
---

## Pin Stable API Versions

Always specify an explicit, stable (GA) API version for every resource. Never
use `-preview` API versions in production deployments — they can change
behaviour or be removed without notice. Update API versions deliberately during
planned maintenance.

**Incorrect (preview API or missing version awareness):**

```bicep
// BAD: Preview API version in production — subject to breaking changes
resource sqlServer 'Microsoft.Sql/servers@2024-02-01-preview' = {
  name: sqlServerName
  location: location
  properties: {
    administratorLogin: 'sqladmin'
    administratorLoginPassword: adminPassword
  }
}

// BAD: Old API version that may lack security features
resource storageAccount 'Microsoft.Storage/storageAccounts@2021-02-01' = {
  name: storageAccountName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
}
```

**Correct (explicit stable API versions):**

```bicep
// Stable GA API version — verified before adoption
resource sqlServer 'Microsoft.Sql/servers@2023-08-01-preview' = {
  // Exception: documented — no GA version available yet for required feature
  name: sqlServerName
  location: location
  properties: {
    administratorLogin: 'sqladmin'
    administratorLoginPassword: adminPassword
  }
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

// Document API version policy in a comment or ADR
// API versions last reviewed: 2025-12-15
```

Reference: [Azure resource API versions — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-providers-and-types)
