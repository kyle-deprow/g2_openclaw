---
title: Secure Parameters for Secrets
impact: HIGH
impactDescription: secrets hidden from logs and deployment history, compliance-safe
tags: bicep, parameters, secure, secrets
---

## Secure Parameters for Secrets

Mark any parameter that carries a secret with `@secure()`. Bicep will mask
the value in deployment logs and the Azure portal. Never output a `@secure()`
parameter or derive an output from one — it defeats the protection.

**Incorrect (secret without @secure, or output leaking it):**

```bicep
// BAD: Password visible in deployment history and logs
@description('Database admin password.')
param adminPassword string

resource sqlServer 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: sqlServerName
  location: location
  properties: {
    administratorLogin: 'sqladmin'
    administratorLoginPassword: adminPassword
  }
}

// ALSO BAD: Leaking the secret as an output
output password string = adminPassword
```

**Correct (@secure parameter, no secret outputs):**

```bicep
@secure()
@description('Database admin password — sourced from Key Vault at deployment.')
param adminPassword string

@description('SQL Server name.')
param sqlServerName string

@description('Azure region.')
param location string

resource sqlServer 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: sqlServerName
  location: location
  properties: {
    administratorLogin: 'sqladmin'
    administratorLoginPassword: adminPassword
  }
}

// Output the resource ID — safe; never output the password
@description('Resource ID of the SQL Server.')
output sqlServerId string = sqlServer.id
```

Reference: [Bicep secure parameters — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/parameters#secure-parameters)
