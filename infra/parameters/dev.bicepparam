// ---------------------------------------------------------------------------
// Dev Environment Parameters
// ---------------------------------------------------------------------------
using '../main.bicep'

param prefix = 'ss'
param workload = 'aisense'
param environment = 'dev'
param location = 'eastus2'

param tags = {
  environment: 'dev'
  owner: 'platform-team'
  costCenter: 'CC-1234'
  project: 'SpineSense'
}

param publicNetworkAccess = 'Enabled'
param storageSkuName = 'Standard_LRS'
param logRetentionInDays = 30

param openAiUserPrincipalId = 'ae13c0e4-93db-4d6a-ac32-3f1d529a9d32'

param modelDeployments = [
  {
    name: 'gpt-5-4'
    modelName: 'gpt-5.4'
    modelVersion: '2026-03-05'
    capacity: 500
    rateLimitPerMinute: 500
    skuName: 'GlobalStandard'
  }
]
