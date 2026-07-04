---
title: RBAC Least-Privilege Access
impact: HIGH
impactDescription: minimised blast radius, compliance-ready, principle of least privilege
tags: bicep, security, rbac, role-assignments, least-privilege
---

## RBAC Least-Privilege Access

Assign the minimum role at the narrowest possible scope. Prefer built-in roles
over custom roles. Never grant Contributor or Owner at the subscription level
when a role at the resource group or resource scope suffices.

**Incorrect (over-privileged, wide scope):**

```bicep
// BAD: Contributor on the entire subscription — far too broad
var contributorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'b24988ac-6180-42a0-ab88-20f7382dd24c'
)

resource overPrivilegedRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().subscriptionId, principalId, contributorRoleId)
  properties: {
    principalId: principalId
    roleDefinitionId: contributorRoleId
    principalType: 'ServicePrincipal'
  }
}
```

**Correct (narrow scope, specific built-in role):**

```bicep
// Storage Blob Data Reader — read-only, scoped to one storage account
var storageBlobDataReaderRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
)

resource storageBlobReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name: guid(storageAccount.id, webApp.id, storageBlobDataReaderRoleId)
  properties: {
    principalId: webApp.identity.principalId
    roleDefinitionId: storageBlobDataReaderRoleId
    principalType: 'ServicePrincipal'
  }
}
```

Reference: [Azure RBAC best practices — Microsoft Learn](https://learn.microsoft.com/en-us/azure/role-based-access-control/best-practices)
