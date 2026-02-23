// ---------------------------------------------------------------------------
// Module: Monitoring
// Deploys Log Analytics Workspace and Application Insights for diagnostics.
// ---------------------------------------------------------------------------

@description('Name of the Log Analytics workspace.')
param logAnalyticsName string

@description('Name of the Application Insights instance.')
param appInsightsName string

@description('Azure region for the monitoring resources.')
param location string

@description('Resource tags applied to all resources.')
param tags object

@description('Retention period in days for Log Analytics data.')
@minValue(30)
@maxValue(730)
param retentionInDays int = 30

@description('Allow or deny public network access. Disable for production.')
@allowed([
  'Enabled'
  'Disabled'
])
param publicNetworkAccess string = 'Enabled'

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2025-02-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionInDays
    publicNetworkAccessForIngestion: publicNetworkAccess
    publicNetworkAccessForQuery: publicNetworkAccess
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalyticsWorkspace.id
    publicNetworkAccessForIngestion: publicNetworkAccess
    publicNetworkAccessForQuery: publicNetworkAccess
  }
}

resource appInsightsLock 'Microsoft.Authorization/locks@2020-05-01' = {
  name: '${appInsights.name}-nodelete'
  scope: appInsights
  properties: {
    level: 'CanNotDelete'
    notes: 'Prevent accidental deletion of Application Insights'
  }
}

resource logAnalyticsLock 'Microsoft.Authorization/locks@2020-05-01' = {
  name: '${logAnalyticsWorkspace.name}-nodelete'
  scope: logAnalyticsWorkspace
  properties: {
    level: 'CanNotDelete'
    notes: 'Prevent accidental deletion of Log Analytics workspace'
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Resource ID of the Log Analytics workspace.')
output logAnalyticsWorkspaceId string = logAnalyticsWorkspace.id

@description('Name of the Log Analytics workspace.')
output logAnalyticsWorkspaceName string = logAnalyticsWorkspace.name

@description('Resource ID of the Application Insights instance.')
output appInsightsId string = appInsights.id

@description('Name of the Application Insights instance.')
output appInsightsName string = appInsights.name

@description('Connection string for Application Insights.')
output appInsightsConnectionString string = appInsights.properties.ConnectionString
