---
title: Secrets in Key Vault
impact: HIGH
impactDescription: centralised secret management, audit trail, automatic rotation support
tags: bicep, security, key-vault, secrets, getSecret
---

## Secrets in Key Vault

Store all secrets, certificates, and connection strings in Azure Key Vault.
Reference secrets at deployment time using `getSecret()` rather than passing
them as plain-text parameters. Never output secret values from modules.

**Incorrect (secrets passed as plain parameters):**

```bicep
// BAD: Secret flows through deployment history in plain text
@description('SQL admin password.')
param sqlAdminPassword string

resource sqlServer 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: sqlServerName
  location: location
  properties: {
    administratorLogin: 'sqladmin'
    administratorLoginPassword: sqlAdminPassword
  }
}

// ALSO BAD: Outputting a secret value
output adminPassword string = sqlAdminPassword
```

**Correct (reference secrets from Key Vault):**

```bicep
// main.bicep — fetch the secret at deployment time
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
  scope: resourceGroup(keyVaultResourceGroup)
}

module sqlServer 'modules/sql-server.bicep' = {
  name: 'deploy-sql-server'
  params: {
    sqlServerName: sqlServerName
    location: location
    adminLogin: 'sqladmin'
    adminPassword: keyVault.getSecret('sql-admin-password')
    tags: tags
  }
}
```

```bicep
// modules/sql-server.bicep — accepts a @secure() param
@secure()
@description('SQL administrator password from Key Vault.')
param adminPassword string

resource sqlServer 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: sqlServerName
  location: location
  properties: {
    administratorLogin: adminLogin
    administratorLoginPassword: adminPassword
  }
}
// No output for adminPassword — never expose secrets
```

Reference: [Key Vault secret reference — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/key-vault-parameter)
