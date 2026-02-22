---
title: Network Isolation by Default
impact: HIGH
impactDescription: reduced attack surface, defence in depth, compliance with data residency
tags: bicep, security, networking, private-endpoints, nsg
---

## Network Isolation by Default

Deploy private endpoints and Network Security Groups (NSGs) for all PaaS
resources that support them. Production workloads must not expose public
endpoints. Deny public network access explicitly and route traffic through
a VNet.

**Incorrect (public endpoints in production):**

```bicep
// BAD: Storage account is wide open to the internet
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
    }
  }
}
```

**Correct (private endpoint + public access denied):**

```bicep
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
    }
  }
}

resource storagePrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-01-01' = {
  name: 'pe-${storageAccountName}'
  location: location
  properties: {
    subnet: { id: privateEndpointSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'storage-connection'
        properties: {
          privateLinkServiceId: storageAccount.id
          groupIds: [ 'blob' ]
        }
      }
    ]
  }
}
```

Reference: [Private endpoints — Microsoft Learn](https://learn.microsoft.com/en-us/azure/private-link/private-endpoint-overview)
