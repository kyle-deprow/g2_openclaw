// ---------------------------------------------------------------------------
// Module: Storage Account
// Deploys a Storage Account for use by Azure AI Hub.
// ---------------------------------------------------------------------------

@description('Name of the storage account. Must be globally unique, 3-24 lowercase alphanumeric.')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Azure region for the storage account.')
param location string

@description('Resource tags applied to all resources.')
param tags object

@description('Storage account SKU name.')
@allowed([
  'Standard_LRS'
  'Standard_GRS'
  'Standard_ZRS'
])
param skuName string = 'Standard_LRS'

@description('Allow or deny public network access. Disable for production.')
@allowed([
  'Enabled'
  'Disabled'
])
param publicNetworkAccess string = 'Enabled'

@description('Resource ID of the Log Analytics workspace for diagnostic settings.')
param logAnalyticsWorkspaceId string

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

resource storageAccount 'Microsoft.Storage/storageAccounts@2025-01-01' = {
  name: storageAccountName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: skuName
  }
  properties: {
    accessTier: 'Hot'
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    publicNetworkAccess: publicNetworkAccess
    networkAcls: {
      defaultAction: 'Allow'
    }
  }
}

resource diagnosticSettings 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: '${storageAccount.name}-diag'
  scope: storageAccount
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    metrics: [
      {
        category: 'Transaction'
        enabled: true
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Resource ID of the storage account.')
output storageAccountId string = storageAccount.id

@description('Name of the storage account.')
output storageAccountName string = storageAccount.name
