---
title: Managed Identities for Authentication
impact: HIGH
impactDescription: eliminates credential leakage, automatic rotation, zero-secret deployments
tags: bicep, security, managed-identity, authentication
---

## Managed Identities for Authentication

Use system-assigned or user-assigned managed identities for service-to-service
authentication. Never embed connection strings with passwords, store service
principal secrets in templates, or pass credentials as deployment parameters.

**Incorrect (embedded credentials):**

```bicep
// BAD: Password in the template — leaked in deployment history
param sqlAdminPassword string

resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  properties: {
    siteConfig: {
      appSettings: [
        {
          name: 'SQL_CONNECTION'
          value: 'Server=${sqlServer.properties.fullyQualifiedDomainName};User=admin;Password=${sqlAdminPassword}'
        }
      ]
    }
  }
}
```

**Correct (managed identity with RBAC):**

```bicep
resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    siteConfig: {
      appSettings: [
        {
          name: 'SQL_CONNECTION'
          value: 'Server=${sqlServer.properties.fullyQualifiedDomainName};Authentication=Active Directory Managed Identity;Database=${databaseName}'
        }
      ]
    }
  }
}

// Grant the web app's identity access to the SQL database
resource sqlRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: sqlDatabase
  name: guid(sqlDatabase.id, webApp.id, sqlContributorRoleId)
  properties: {
    principalId: webApp.identity.principalId
    roleDefinitionId: sqlContributorRoleId
    principalType: 'ServicePrincipal'
  }
}
```

Reference: [Managed identities — Microsoft Learn](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/overview)
