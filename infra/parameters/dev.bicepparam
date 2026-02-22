// ---------------------------------------------------------------------------
// Dev Environment Parameters
// ---------------------------------------------------------------------------
using '../main.bicep'

param prefix = 'ss'
param workload = 'aisense'
param environment = 'dev'
param location = 'eastus'

param tags = {
  environment: 'dev'
  owner: 'platform-team'
  costCenter: 'CC-1234'
  project: 'SpineSense'
}

param publicNetworkAccess = 'Enabled'
param storageSkuName = 'Standard_LRS'
param logRetentionInDays = 30

param modelDeployments = [
  {
    name: 'gpt-41'
    modelName: 'gpt-4.1'
    modelVersion: '2025-04-14'
    capacity: 15
    rateLimitPerMinute: 100
  }
]
