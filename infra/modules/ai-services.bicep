// ---------------------------------------------------------------------------
// Module: Azure AI Services (model-router)
// Deploys an AIServices (Cognitive Services) account with a model-router
// deployment using GlobalStandard SKU.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Name of the Azure AI Services account.')
param aiServicesAccountName string

@description('Azure region for the AI Services account.')
param location string

@description('Resource tags applied to all resources.')
param tags object

@description('Disable local (API key) authentication. True enforces Entra-only auth.')
param disableLocalAuth bool = false

@description('Allow or deny public network access. Disable for production.')
@allowed([
  'Enabled'
  'Disabled'
])
param publicNetworkAccess string = 'Enabled'

@description('SKU name for the AI Services account.')
@allowed([
  'S0'
])
param skuName string = 'S0'

@description('Deployment capacity for the model-router (tokens per minute, thousands).')
param modelRouterCapacity int = 100

@description('Model version for the model-router deployment.')
param modelRouterVersion string = '2025-11-18'

@description('Resource ID of the Log Analytics workspace for diagnostic settings.')
param logAnalyticsWorkspaceId string

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

resource aiServicesAccount 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: aiServicesAccountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: skuName
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: aiServicesAccountName
    disableLocalAuth: disableLocalAuth
    publicNetworkAccess: publicNetworkAccess
    networkAcls: {
      defaultAction: publicNetworkAccess == 'Disabled' ? 'Deny' : 'Allow'
    }
  }
}

resource lock 'Microsoft.Authorization/locks@2020-05-01' = {
  name: '${aiServicesAccount.name}-nodelete'
  scope: aiServicesAccount
  properties: {
    level: 'CanNotDelete'
    notes: 'Prevent accidental deletion of Azure AI Services account'
  }
}

resource modelRouterDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiServicesAccount
  name: 'model-router'
  sku: {
    name: 'GlobalStandard'
    capacity: modelRouterCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'model-router'
      version: modelRouterVersion
    }
  }
}

resource diagnosticSettings 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: '${aiServicesAccount.name}-diag'
  scope: aiServicesAccount
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

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Resource ID of the Azure AI Services account.')
output aiServicesAccountId string = aiServicesAccount.id

@description('Name of the Azure AI Services account.')
output aiServicesAccountName string = aiServicesAccount.name

@description('Endpoint of the Azure AI Services account.')
output aiServicesEndpoint string = aiServicesAccount.properties.endpoint

@description('Principal ID of the AI Services account system-assigned managed identity.')
output aiServicesPrincipalId string = aiServicesAccount.identity.principalId

@description('Primary API key — treat as secret.')
#disable-next-line outputs-should-not-contain-secrets
output aiServicesApiKey string = aiServicesAccount.listKeys().key1
