---
title: Encryption at Rest and in Transit
impact: HIGH
impactDescription: data protection, compliance, defence against interception and exfiltration
tags: bicep, security, encryption, tls, cmk
---

## Encryption at Rest and in Transit

Enforce TLS 1.2+ for all traffic and enable encryption at rest on every resource.
For sensitive or regulated workloads, use Customer-Managed Keys (CMK) stored in
Key Vault instead of relying solely on platform-managed keys.

**Incorrect (weak TLS, no explicit encryption):**

```bicep
// BAD: Allows older TLS and relies on implicit defaults
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    supportsHttpsTrafficOnly: false // Allows HTTP
    // No minimumTlsVersion — defaults may vary
  }
}
```

**Correct (enforced TLS 1.2 + CMK encryption):**

```bicep
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    encryption: {
      keySource: 'Microsoft.Keyvault'
      keyvaultproperties: {
        keyname: encryptionKeyName
        keyvaulturi: keyVaultUri
      }
      services: {
        blob: { enabled: true }
        file: { enabled: true }
        queue: { enabled: true }
        table: { enabled: true }
      }
    }
  }
}

resource sqlServer 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: sqlServerName
  location: location
  properties: {
    minimalTlsVersion: '1.2'
  }
}
```

Reference: [Azure encryption overview — Microsoft Learn](https://learn.microsoft.com/en-us/azure/security/fundamentals/encryption-overview)
