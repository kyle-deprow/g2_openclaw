// ---------------------------------------------------------------------------
// Module: Key Vault
// Deploys an Azure Key Vault with RBAC authorization for use by AI Hub.
// ---------------------------------------------------------------------------

@description('Name of the Key Vault. Must be globally unique, 3-24 characters.')
@minLength(3)
@maxLength(24)
param keyVaultName string

@description('Azure region for the Key Vault.')
param location string

@description('Resource tags applied to all resources.')
param tags object

@description('Enable RBAC authorization model for the Key Vault.')
param enableRbacAuthorization bool = true

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

resource keyVault 'Microsoft.KeyVault/vaults@2024-11-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: tenant().tenantId
    enableRbacAuthorization: enableRbacAuthorization
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    publicNetworkAccess: publicNetworkAccess
    networkAcls: {
      defaultAction: publicNetworkAccess == 'Disabled' ? 'Deny' : 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource diagnosticSettings 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: '${keyVault.name}-diag'
  scope: keyVault
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

resource lock 'Microsoft.Authorization/locks@2020-05-01' = {
  name: '${keyVault.name}-nodelete'
  scope: keyVault
  properties: {
    level: 'CanNotDelete'
    notes: 'Prevent accidental deletion of Key Vault'
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Resource ID of the Key Vault.')
output keyVaultId string = keyVault.id

@description('Name of the Key Vault.')
output keyVaultName string = keyVault.name

@description('URI of the Key Vault.')
output keyVaultUri string = keyVault.properties.vaultUri
