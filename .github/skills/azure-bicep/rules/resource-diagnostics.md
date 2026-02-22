---
title: Diagnostic Settings and Logging
impact: HIGH
impactDescription: observability, incident response, compliance audit trails
tags: bicep, resources, diagnostics, logging, monitoring
---

## Diagnostic Settings and Logging

Enable diagnostic settings on every resource that supports them. Send logs and
metrics to a Log Analytics workspace (and optionally a Storage Account for
long-term retention). Without diagnostics, incidents become black boxes.

**Incorrect (no diagnostic settings):**

```bicep
// BAD: Key Vault deployed without any logging — no audit trail
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
  }
  tags: tags
}
// No diagnosticSettings — secret access is invisible
```

**Correct (diagnostic settings sending all logs to Log Analytics):**

```bicep
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
  }
  tags: tags
}

resource keyVaultDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: '${keyVaultName}-diag'
  scope: keyVault
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
        retentionPolicy: { enabled: true, days: 90 }
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
        retentionPolicy: { enabled: true, days: 90 }
      }
    ]
  }
}
```

Reference: [Azure diagnostic settings — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-monitor/essentials/diagnostic-settings)
