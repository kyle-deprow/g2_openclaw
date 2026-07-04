---
title: Consistent Resource Naming Convention
impact: CRITICAL
impactDescription: predictable names, no collisions, easy identification across environments
tags: bicep, naming, conventions, resources
---

## Consistent Resource Naming Convention

Follow the pattern `{prefix}-{workload}-{env}-{region}-{instance}` for all
resource names. Build names from parameters so they stay consistent across
environments and regions. Never hardcode resource names — it prevents reuse
and causes collisions.

**Incorrect (hardcoded names):**

```bicep
// BAD: Names are baked in — can't deploy to another env or region
resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: 'my-app-plan'
  location: 'eastus2'
  sku: {
    name: 'P1v3'
  }
}

resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: 'myapp-production'
  location: 'eastus2'
  properties: {
    serverFarmId: appServicePlan.id
  }
}
```

**Correct (parameterized naming convention):**

```bicep
@description('Abbreviated prefix for the organisation or project.')
param prefix string

@description('Workload or application identifier.')
param workload string

@description('Environment short name.')
@allowed(['dev', 'stg', 'prod'])
param env string

@description('Azure region short name (e.g., eus2, wus3).')
param regionShort string

@description('Azure region for the resource.')
param location string

@description('Optional instance number for uniqueness.')
param instance string = '001'

var baseName = '${prefix}-${workload}-${env}-${regionShort}-${instance}'

resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: 'asp-${baseName}'
  location: location
  sku: {
    name: 'P1v3'
  }
}

resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: 'app-${baseName}'
  location: location
  properties: {
    serverFarmId: appServicePlan.id
  }
}
```

Reference: [Azure naming conventions — Microsoft Learn](https://learn.microsoft.com/en-us/azure/cloud-adoption-framework/ready/azure-best-practices/resource-naming)
