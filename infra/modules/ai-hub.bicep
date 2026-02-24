// ---------------------------------------------------------------------------
// Module: Azure AI Hub
// Deploys an Azure AI Hub (Machine Learning workspace, kind: Hub) with
// system-assigned managed identity and RBAC role assignment to OpenAI.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Name of the Azure AI Hub workspace.')
param aiHubName string

@description('Azure region for the AI Hub.')
param location string

@description('Resource tags applied to all resources.')
param tags object

@description('Resource ID of the Storage Account linked to the AI Hub.')
param storageAccountId string

@description('Resource ID of the Key Vault linked to the AI Hub.')
param keyVaultId string

@description('Resource ID of the Application Insights instance linked to the AI Hub.')
param appInsightsId string

@description('Resource ID of the Azure OpenAI account for RBAC assignment.')
param openAiAccountId string

@description('Friendly display name for the AI Hub.')
param friendlyName string = 'AI Hub'

@description('Description of the AI Hub workspace.')
param hubDescription string = 'Azure AI Foundry Hub for centralised AI resource management.'

@description('Resource ID of the Log Analytics workspace for diagnostic settings.')
param logAnalyticsWorkspaceId string

@description('Allow or deny public network access.')
@allowed([
  'Enabled'
  'Disabled'
])
param publicNetworkAccess string = 'Enabled'

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

// Built-in role: Cognitive Services OpenAI User
var cognitiveServicesOpenAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

resource aiHub 'Microsoft.MachineLearningServices/workspaces@2024-10-01' = {
  name: aiHubName
  location: location
  tags: tags
  kind: 'Hub'
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'Basic'
    tier: 'Basic'
  }
  properties: {
    friendlyName: friendlyName
    description: hubDescription
    storageAccount: storageAccountId
    keyVault: keyVaultId
    applicationInsights: appInsightsId
    publicNetworkAccess: publicNetworkAccess
  }
}

resource openAiRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(openAiAccountId, aiHub.id, cognitiveServicesOpenAiUserRoleId)
  scope: openAiResource
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAiUserRoleId)
    principalId: aiHub.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Reference to existing OpenAI account for scoping the role assignment
resource openAiResource 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: last(split(openAiAccountId, '/'))
}

resource diagnosticSettings 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: '${aiHub.name}-diag'
  scope: aiHub
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
  name: '${aiHub.name}-nodelete'
  scope: aiHub
  properties: {
    level: 'CanNotDelete'
    notes: 'Prevent accidental deletion of AI Hub'
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Resource ID of the AI Hub workspace.')
output aiHubId string = aiHub.id

@description('Name of the AI Hub workspace.')
output aiHubName string = aiHub.name

@description('Principal ID of the AI Hub system-assigned managed identity.')
output aiHubPrincipalId string = aiHub.identity.principalId
