// ---------------------------------------------------------------------------
// Module: Azure AI Project
// Deploys an Azure AI Project (Machine Learning workspace, kind: Project)
// linked to an AI Hub.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Name of the Azure AI Project workspace.')
param aiProjectName string

@description('Azure region for the AI Project.')
param location string

@description('Resource tags applied to all resources.')
param tags object

@description('Resource ID of the parent AI Hub workspace.')
param aiHubId string

@description('Friendly display name for the AI Project.')
param friendlyName string = 'AI Project'

@description('Description of the AI Project workspace.')
param projectDescription string = 'Azure AI Foundry Project for model experimentation and deployment.'

@description('Resource ID of the Log Analytics workspace for diagnostic settings.')
param logAnalyticsWorkspaceId string

@description('Allow or deny public network access.')
@allowed([
  'Enabled'
  'Disabled'
])
param publicNetworkAccess string = 'Enabled'

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

resource aiProject 'Microsoft.MachineLearningServices/workspaces@2024-10-01' = {
  name: aiProjectName
  location: location
  tags: tags
  kind: 'Project'
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'Basic'
    tier: 'Basic'
  }
  properties: {
    friendlyName: friendlyName
    description: projectDescription
    hubResourceId: aiHubId
    publicNetworkAccess: publicNetworkAccess
  }
}

resource diagnosticSettings 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: '${aiProject.name}-diag'
  scope: aiProject
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
  name: '${aiProject.name}-nodelete'
  scope: aiProject
  properties: {
    level: 'CanNotDelete'
    notes: 'Prevent accidental deletion of AI Project'
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Resource ID of the AI Project workspace.')
output aiProjectId string = aiProject.id

@description('Name of the AI Project workspace.')
output aiProjectName string = aiProject.name
