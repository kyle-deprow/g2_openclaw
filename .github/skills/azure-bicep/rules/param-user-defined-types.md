---
title: User-Defined Types for Complex Parameter Shapes
impact: HIGH
impactDescription: type-safe configs, editor autocomplete, validated structure at compile time
tags: bicep, parameters, user-defined-types, typing
---

## User-Defined Types for Complex Parameter Shapes

Define custom types for complex parameter shapes instead of using loose `object`
parameters. User-defined types give you compile-time validation, editor
autocomplete, and self-documenting contracts. Untyped objects hide expected
structure and allow silent misconfiguration.

**Incorrect (loose object parameter):**

```bicep
// BAD: Consumers have no idea what shape 'networkConfig' expects
@description('Network configuration.')
param networkConfig object

resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: networkConfig.vnetName     // Fails at deploy time if key is missing
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: networkConfig.addressPrefixes
    }
  }
}
```

**Correct (user-defined type with explicit shape):**

```bicep
@description('Configuration for a subnet within the VNet.')
type subnetConfigType = {
  @description('Subnet name.')
  name: string

  @description('Subnet CIDR prefix, e.g. 10.0.1.0/24.')
  addressPrefix: string

  @description('Whether to delegate the subnet to a service.')
  delegation: string?
}

@description('Configuration for the virtual network.')
type vnetConfigType = {
  @description('Name of the virtual network.')
  vnetName: string

  @description('Address space CIDR prefixes.')
  addressPrefixes: string[]

  @description('List of subnet configurations.')
  subnets: subnetConfigType[]
}

@description('Virtual network configuration.')
param networkConfig vnetConfigType

resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: networkConfig.vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: networkConfig.addressPrefixes
    }
    subnets: [for subnet in networkConfig.subnets: {
      name: subnet.name
      properties: {
        addressPrefix: subnet.addressPrefix
      }
    }]
  }
}
```

Reference: [Bicep user-defined types — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/user-defined-data-types)
