---
title: Module Versioning
impact: CRITICAL
impactDescription: safe upgrades, reproducible deployments, breaking change tracking
tags: bicep, modules, versioning, registry
---

## Module Versioning

Use Bicep module registries or path-based versioning for shared modules. Tag
breaking changes (parameter renames, removed outputs, behavioral changes) with a
major version bump. Consumers pin to a specific version and upgrade deliberately.

**Incorrect (unversioned shared modules):**

```bicep
// BAD: Everyone points to the same path — any change breaks all consumers
module appService '../../shared/app-service.bicep' = {
  name: 'deploy-app-service'
  params: { ... }
}
```

**Correct (registry-based versioning):**

```bicep
// Pinned to a specific version in the module registry
module appService 'br:myregistry.azurecr.io/bicep/modules/app-service:1.2.0' = {
  name: 'deploy-app-service'
  params: {
    appName: appServiceName
    location: location
    tags: tags
  }
}
```

**Correct (path-based versioning in monorepo):**

```bicep
// Version embedded in directory path
module appService 'modules/v2/app-service.bicep' = {
  name: 'deploy-app-service'
  params: {
    appName: appServiceName
    location: location
    tags: tags
  }
}
```

Reference: [Bicep module registry — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/private-module-registry)

