---
title: Idempotent Deployments
impact: HIGH
impactDescription: safe re-runs, pipeline reliability, disaster recovery confidence
tags: bicep, resources, idempotency, deployments
---

## Idempotent Deployments

Every deployment must produce the same result whether it runs once or many times.
Avoid patterns that generate unique values on each run (like `utcNow()` in
resource names) or that fail on re-deploy because a resource already exists.

**Incorrect (non-idempotent patterns):**

```bicep
// BAD: Name changes every deployment — orphans old resources
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'st${uniqueString(resourceGroup().id)}${utcNow()}'
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
}

// BAD: Role assignment with a random GUID — creates duplicates
resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: newGuid()
  properties: {
    principalId: principalId
    roleDefinitionId: readerRoleId
  }
}
```

**Correct (deterministic, re-runnable):**

```bicep
// Name is deterministic — derived from stable inputs
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'st${uniqueString(resourceGroup().id, workload, env)}'
  location: location
  sku: { name: storageSku }
  kind: 'StorageV2'
  tags: tags
}

// GUID is deterministic — derived from scope + principal + role
resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, principalId, readerRoleId)
  scope: storageAccount
  properties: {
    principalId: principalId
    roleDefinitionId: readerRoleId
    principalType: 'ServicePrincipal'
  }
}
```

Reference: [Idempotent ARM deployments — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-resource-manager/templates/overview#idempotent)
