---
title: Parameter Decorators for Validation
impact: HIGH
impactDescription: self-documenting params, early validation, reduced deployment failures
tags: bicep, parameters, decorators, validation, description
---

## Parameter Decorators for Validation

Annotate every parameter with `@description()`. Use `@allowed()`, `@minValue()`,
`@maxValue()`, `@minLength()`, and `@maxLength()` to constrain inputs so that
invalid values are rejected before deployment begins.

**Incorrect (bare parameters, no docs or constraints):**

```bicep
// BAD: No descriptions, no constraints — anything goes
param location string
param sku string
param instanceCount int
param storageName string
```

**Correct (decorated parameters with constraints):**

```bicep
@description('Azure region for all resources in this deployment.')
param location string

@description('App Service Plan SKU tier.')
@allowed(['B1', 'S1', 'P1v3', 'P2v3'])
param skuName string

@description('Number of App Service Plan instances.')
@minValue(1)
@maxValue(10)
param instanceCount int

@description('Globally unique name for the storage account.')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Tags to apply to all resources.')
param tags object
```

Reference: [Bicep decorators — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/parameters#decorators)
