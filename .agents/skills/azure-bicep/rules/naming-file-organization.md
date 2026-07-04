---
title: File Organisation — One Resource Type per File
impact: CRITICAL
impactDescription: navigable structure, isolated changes, clear ownership
tags: bicep, naming, file-structure, organisation
---

## File Organisation — One Resource Type per File

Each `.bicep` file should contain a single resource type or a tightly related
pair (e.g., a storage account and its blob service). `main.bicep` acts as the
orchestrator, calling into module files. Dumping everything into one file makes
reviews painful and diffs unintelligible.

**Incorrect (single file with everything):**

```bicep
// BAD: main.bicep contains inline resources for VNet, SQL, App Service, Key Vault
param location string
param env string

resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = { ... }
resource sqlServer 'Microsoft.Sql/servers@2023-08-01-preview' = { ... }
resource sqlDatabase 'Microsoft.Sql/servers/databases@2023-08-01-preview' = { ... }
resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = { ... }
resource webApp 'Microsoft.Web/sites@2023-12-01' = { ... }
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = { ... }
// 300+ lines, impossible to review
```

**Correct (modular file structure):**

```text
├── main.bicep                  # Orchestrator — calls modules only
├── main.prod.bicepparam        # Production parameter values
├── main.dev.bicepparam         # Development parameter values
└── modules/
    ├── virtual-network.bicep   # VNet + subnets
    ├── sql-server.bicep        # SQL Server + database
    ├── app-service.bicep       # App Service Plan + Web App
    └── key-vault.bicep         # Key Vault + access policies
```

```bicep
// main.bicep — orchestrates modules, declares no inline resources
module vnet 'modules/virtual-network.bicep' = {
  name: 'deploy-vnet'
  params: { vnetName: vnetName, location: location, tags: tags }
}

module sql 'modules/sql-server.bicep' = {
  name: 'deploy-sql'
  params: { serverName: sqlServerName, location: location, tags: tags }
}
```

Reference: [Bicep best practices — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/best-practices)
