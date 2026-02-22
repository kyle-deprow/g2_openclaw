// ---------------------------------------------------------------------------
// Module: Azure OpenAI
// Deploys an Azure OpenAI (Cognitive Services) account with model deployments.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// User-defined types
// ---------------------------------------------------------------------------

@description('Configuration for an OpenAI model deployment.')
type modelDeploymentConfig = {
  @description('Unique name for the deployment.')
  name: string

  @description('Model name (e.g., gpt-5).')
  modelName: string

  @description('Model version (e.g., 2025-03-01).')
  modelVersion: string

  @description('Deployment capacity in thousands of tokens per minute (TPM).')
  capacity: int

  @description('Rate limit in requests per minute (RPM).')
  rateLimitPerMinute: int
}

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Name of the Azure OpenAI account.')
param openAiAccountName string

@description('Azure region for the OpenAI account.')
param location string

@description('Resource tags applied to all resources.')
param tags object

@description('Disable local (API key) authentication. True enforces Entra-only auth.')
param disableLocalAuth bool = true

@description('Allow or deny public network access. Disable for production.')
@allowed([
  'Enabled'
  'Disabled'
])
param publicNetworkAccess string = 'Enabled'

@description('SKU name for the OpenAI account.')
@allowed([
  'S0'
])
param skuName string = 'S0'

@description('Array of model deployment configurations.')
param modelDeployments modelDeploymentConfig[]

@description('Resource ID of the Log Analytics workspace for diagnostic settings.')
param logAnalyticsWorkspaceId string

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

resource openAiAccount 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: openAiAccountName
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: {
    name: skuName
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: openAiAccountName
    disableLocalAuth: disableLocalAuth
    publicNetworkAccess: publicNetworkAccess
    networkAcls: {
      defaultAction: 'Allow'
    }
  }
}

@batchSize(1)
resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = [
  for (deployment, index) in modelDeployments: {
    parent: openAiAccount
    name: deployment.name
    sku: {
      name: 'Standard'
      capacity: deployment.capacity
    }
    properties: {
      model: {
        format: 'OpenAI'
        name: deployment.modelName
        version: deployment.modelVersion
      }
      raiPolicyName: 'Microsoft.DefaultV2'
      #disable-next-line BCP073
      rateLimits: [
        {
          key: 'request'
          renewalPeriod: 60
          count: deployment.rateLimitPerMinute
        }
      ]
    }
  }
]

resource diagnosticSettings 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: '${openAiAccount.name}-diag'
  scope: openAiAccount
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

@description('Resource ID of the Azure OpenAI account.')
output openAiAccountId string = openAiAccount.id

@description('Name of the Azure OpenAI account.')
output openAiAccountName string = openAiAccount.name

@description('Endpoint of the Azure OpenAI account.')
output openAiEndpoint string = openAiAccount.properties.endpoint

@description('Principal ID of the OpenAI account system-assigned managed identity.')
output openAiPrincipalId string = openAiAccount.identity.principalId
