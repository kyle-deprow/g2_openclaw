---
title: Explicit Inputs and Outputs
impact: CRITICAL
impactDescription: self-documenting modules, no hidden coupling, testable contracts
tags: bicep, modules, parameters, outputs, typing
---

## Explicit Inputs and Outputs

Every module declares typed `param` inputs and `output` values. Nothing
environment-specific is hardcoded inside a module — names, SKUs, locations, and
feature flags all come from parameters. Outputs expose values that consuming
modules or deployment scripts need.

**Incorrect (hardcoded values inside module):**

```bicep
// BAD: Module has hidden assumptions baked in
param location string = 'eastus2' // Default hides the decision
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stspinesenseprod001' // Hardcoded name — can't reuse for dev
  location: location
  sku: {
    name: 'Standard_GRS' // Hardcoded SKU — can't use LRS for dev
  }
  kind: 'StorageV2'
}
// No outputs — consumers can't reference the account
```

**Correct (fully parameterized with outputs):**

```bicep
@description('Name of the storage account.')
param storageAccountName string

@description('Azure region for the resource.')
param location string

@description('Storage SKU name.')
@allowed(['Standard_LRS', 'Standard_GRS', 'Standard_ZRS', 'Standard_RAGRS'])
param skuName string

@description('Tags to apply to the resource.')
param tags object

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: skuName
  }
  kind: 'StorageV2'
  tags: tags
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

@description('Resource ID of the storage account.')
output storageAccountId string = storageAccount.id

@description('Name of the storage account.')
output storageAccountName string = storageAccount.name
```

Reference: [Bicep parameters — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/parameters)

