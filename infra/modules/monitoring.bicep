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
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
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
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
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

@description('Instrumentation key for Application Insights.')
output appInsightsInstrumentationKey string = appInsights.properties.InstrumentationKey

@description('Connection string for Application Insights.')
output appInsightsConnectionString string = appInsights.properties.ConnectionString
