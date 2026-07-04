---
title: Composition Over Monoliths
impact: CRITICAL
impactDescription: reusable building blocks, manageable complexity, parallel deployments
tags: bicep, modules, composition, architecture
---

## Composition Over Monoliths

Parent templates orchestrate by calling child modules. Children own their
resources. Never nest modules deeper than two levels — it makes dependency
tracking and debugging difficult.

**Incorrect (deeply nested modules):**

```bicep
// BAD: main → networking → subnets → nsg → rules (4 levels deep)
module networking 'modules/networking.bicep' = {
  name: 'deploy-networking'
  params: { ... }
  // Inside networking.bicep, it calls subnets.bicep,
  // which calls nsg.bicep, which calls nsgRules.bicep
}
```

**Correct (flat composition with two levels max):**

```bicep
// main.bicep — orchestrator (level 0)
module vnet 'modules/virtual-network.bicep' = {
  name: 'deploy-vnet'
  params: {
    vnetName: vnetName
    addressPrefix: vnetAddressPrefix
    subnets: subnetConfigurations // Subnets defined as param, not a nested module
    location: location
    tags: tags
  }
}

module nsg 'modules/network-security-group.bicep' = {
  name: 'deploy-nsg'
  params: {
    nsgName: nsgName
    securityRules: nsgRules
    location: location
    tags: tags
  }
}

// Associate NSG to subnet via a separate, focused module
module nsgAssociation 'modules/nsg-subnet-association.bicep' = {
  name: 'deploy-nsg-association'
  params: {
    subnetId: vnet.outputs.appSubnetId
    nsgId: nsg.outputs.nsgId
  }
}
```

Reference: [Bicep modules — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/modules)

