// ---------------------------------------------------------------------------
// Main Orchestrator — Subscription-scope deployment
// Creates a resource group and deploys all Azure AI Foundry resources via
// focused child modules.
// ---------------------------------------------------------------------------
targetScope = 'subscription'

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

@description('Short prefix used in resource names (e.g., org abbreviation).')
@minLength(2)
@maxLength(6)
param prefix string

@description('Workload identifier used in resource names.')
param workload string = 'aisense'

@description('Deployment environment. Used in naming and tags.')
@allowed([
  'dev'
  'staging'
  'prod'
])
param environment string

@description('Azure region for all resources.')
param location string = 'eastus'

@description('Mandatory resource tags applied to every resource.')
param tags object

@description('Array of OpenAI model deployment configurations.')
param modelDeployments modelDeploymentConfig[]

@description('Allow or deny public network access on applicable resources.')
@allowed([
  'Enabled'
  'Disabled'
])
param publicNetworkAccess string = 'Enabled'

@description('SKU for the Storage Account.')
@allowed([
  'Standard_LRS'
  'Standard_GRS'
  'Standard_ZRS'
])
param storageSkuName string = 'Standard_LRS'

@description('Retention period in days for Log Analytics data.')
@minValue(30)
@maxValue(730)
param logRetentionInDays int = 30

@description('Azure region for the AI Services account (model-router). Must differ from primary location when model is region-restricted.')
param aiServicesLocation string = 'eastus2'

@description('Deployment capacity for the model-router (tokens per minute, thousands).')
param modelRouterCapacity int = 100

// ---------------------------------------------------------------------------
// Variables — Naming convention: {prefix}-{workload}-{env}-{region}-{instance}
// ---------------------------------------------------------------------------

var baseName = '${prefix}-${workload}-${environment}-${location}'
var resourceGroupName = 'rg-${baseName}'

// Storage accounts have a 24-char alphanumeric limit — derive a compliant name
var storageAccountName = take(replace('st${prefix}${workload}${environment}', '-', ''), 24)

var keyVaultName = take('kv-${baseName}', 24)
var logAnalyticsName = 'log-${baseName}'
var appInsightsName = 'appi-${baseName}'
var openAiAccountName = 'oai-${baseName}'
var aiHubName = 'aihub-${baseName}'
var aiProjectName = 'aiproj-${baseName}'
var aiServicesName = 'aisvc-${prefix}-${workload}-${environment}-${aiServicesLocation}'

// ---------------------------------------------------------------------------
// Resource Group
// ---------------------------------------------------------------------------

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

// ---------------------------------------------------------------------------
// Module Deployments — ordered by dependency chain
// ---------------------------------------------------------------------------

// 1. Monitoring — no upstream dependencies
module monitoring 'modules/monitoring.bicep' = {
  name: 'deploy-monitoring'
  scope: resourceGroup
  params: {
    logAnalyticsName: logAnalyticsName
    appInsightsName: appInsightsName
    location: location
    tags: tags
    retentionInDays: logRetentionInDays
    publicNetworkAccess: publicNetworkAccess
  }
}

// 2. Storage Account — depends on monitoring (diagnostic settings)
module storage 'modules/storage.bicep' = {
  name: 'deploy-storage'
  scope: resourceGroup
  params: {
    #disable-next-line BCP334
    storageAccountName: storageAccountName
    location: location
    tags: tags
    skuName: storageSkuName
    publicNetworkAccess: publicNetworkAccess
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
  }
}

// 3. Key Vault — depends on monitoring (diagnostic settings)
module keyVault 'modules/keyvault.bicep' = {
  name: 'deploy-keyvault'
  scope: resourceGroup
  params: {
    keyVaultName: keyVaultName
    location: location
    tags: tags
    enableRbacAuthorization: true
    publicNetworkAccess: publicNetworkAccess
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
  }
}

// 4. Azure OpenAI — depends on monitoring (diagnostic settings)
module openAi 'modules/openai.bicep' = {
  name: 'deploy-openai'
  scope: resourceGroup
  params: {
    openAiAccountName: openAiAccountName
    location: location
    tags: tags
    disableLocalAuth: false
    publicNetworkAccess: publicNetworkAccess
    modelDeployments: modelDeployments
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
  }
}

// 5. AI Services (model-router) — depends on monitoring (diagnostic settings)
module aiServices 'modules/ai-services.bicep' = {
  name: 'deploy-ai-services'
  scope: resourceGroup
  params: {
    aiServicesAccountName: aiServicesName
    location: aiServicesLocation
    tags: tags
    disableLocalAuth: false
    publicNetworkAccess: publicNetworkAccess
    modelRouterCapacity: modelRouterCapacity
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
  }
}

// 6. AI Hub — depends on storage, keyVault, monitoring, openAi
module aiHub 'modules/ai-hub.bicep' = {
  name: 'deploy-ai-hub'
  scope: resourceGroup
  params: {
    aiHubName: aiHubName
    location: location
    tags: tags
    storageAccountId: storage.outputs.storageAccountId
    keyVaultId: keyVault.outputs.keyVaultId
    appInsightsId: monitoring.outputs.appInsightsId
    openAiAccountId: openAi.outputs.openAiAccountId
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
    publicNetworkAccess: publicNetworkAccess
  }
}

// 7. AI Project — depends on aiHub
module aiProject 'modules/ai-project.bicep' = {
  name: 'deploy-ai-project'
  scope: resourceGroup
  params: {
    aiProjectName: aiProjectName
    location: location
    tags: tags
    aiHubId: aiHub.outputs.aiHubId
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
    publicNetworkAccess: publicNetworkAccess
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Name of the deployed resource group.')
output resourceGroupName string = resourceGroup.name

@description('Resource ID of the AI Hub workspace.')
output aiHubId string = aiHub.outputs.aiHubId

@description('Resource ID of the AI Project workspace.')
output aiProjectId string = aiProject.outputs.aiProjectId

@description('Endpoint of the Azure OpenAI account.')
output openAiEndpoint string = openAi.outputs.openAiEndpoint

@description('Name of the Azure OpenAI account.')
output openAiAccountName string = openAi.outputs.openAiAccountName

@description('Primary API key for the Azure OpenAI account — treat as secret.')
output openAiApiKey string = openAi.outputs.openAiApiKey

@description('Endpoint of the Azure AI Services account (model-router).')
output aiServicesEndpoint string = aiServices.outputs.aiServicesEndpoint

@description('Primary API key for the Azure AI Services account — treat as secret.')
output aiServicesApiKey string = aiServices.outputs.aiServicesApiKey


