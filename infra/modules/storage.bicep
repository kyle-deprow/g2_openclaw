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
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    publicNetworkAccess: publicNetworkAccess
    networkAcls: {
      defaultAction: publicNetworkAccess == 'Disabled' ? 'Deny' : 'Allow'
      bypass: 'AzureServices'
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

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 30
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 30
    }
  }
}

resource blobDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: '${storageAccount.name}-blob-diag'
  scope: blobService
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'StorageRead'
        enabled: true
      }
      {
        category: 'StorageWrite'
        enabled: true
      }
      {
        category: 'StorageDelete'
        enabled: true
      }
    ]
  }
}

resource lock 'Microsoft.Authorization/locks@2020-05-01' = {
  name: '${storageAccount.name}-nodelete'
  scope: storageAccount
  properties: {
    level: 'CanNotDelete'
    notes: 'Prevent accidental deletion of Storage Account'
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Resource ID of the storage account.')
output storageAccountId string = storageAccount.id

@description('Name of the storage account.')
output storageAccountName string = storageAccount.name
