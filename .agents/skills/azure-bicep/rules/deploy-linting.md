---
title: Lint and Build in CI
impact: MEDIUM
impactDescription: early error detection, consistent style, prevented misconfigurations
tags: bicep, deployment, linting, ci, build
---

## Lint and Build in CI

Run `az bicep build` and `az bicep lint` (via `bicepconfig.json`) in every CI
pipeline. Fail the build on warnings so that issues are caught before they reach
any environment. Linting enforces naming rules, security baselines, and best
practices automatically.

**Incorrect (no CI validation):**

```yaml
# BAD: Skip straight to deployment — errors surface in production
- task: AzureCLI@2
  displayName: 'Deploy'
  inputs:
    azureSubscription: $(serviceConnection)
    scriptType: bash
    scriptLocation: inlineScript
    inlineScript: |
      az deployment group create \
        --resource-group $(resourceGroup) \
        --template-file main.bicep \
        --parameters main.$(environment).bicepparam
```

**Correct (lint + build gate before deploy):**

```json
// bicepconfig.json — enable recommended rules as errors
{
  "analyzers": {
    "core": {
      "enabled": true,
      "rules": {
        "no-hardcoded-env-urls": { "level": "error" },
        "no-unused-params": { "level": "error" },
        "no-unused-vars": { "level": "error" },
        "prefer-interpolation": { "level": "warning" },
        "secure-parameter-default": { "level": "error" },
        "simplify-interpolation": { "level": "warning" },
        "use-stable-resource-identifiers": { "level": "error" }
      }
    }
  }
}
```

```yaml
# CI pipeline: lint → build → what-if → deploy
- task: AzureCLI@2
  displayName: 'Bicep Lint & Build'
  inputs:
    azureSubscription: $(serviceConnection)
    scriptType: bash
    scriptLocation: inlineScript
    inlineScript: |
      set -euo pipefail
      echo "=== Building all Bicep files ==="
      find . -name '*.bicep' -not -path './modules/*' | while read f; do
        echo "Building $f"
        az bicep build --file "$f" --stdout > /dev/null
      done
      echo "=== Lint passed, build succeeded ==="

- task: AzureCLI@2
  displayName: 'What-If Preview'
  inputs:
    azureSubscription: $(serviceConnection)
    scriptType: bash
    scriptLocation: inlineScript
    inlineScript: |
      az deployment group what-if \
        --resource-group $(resourceGroup) \
        --template-file main.bicep \
        --parameters main.$(environment).bicepparam

- task: AzureCLI@2
  displayName: 'Deploy'
  inputs:
    azureSubscription: $(serviceConnection)
    scriptType: bash
    scriptLocation: inlineScript
    inlineScript: |
      az deployment group create \
        --resource-group $(resourceGroup) \
        --template-file main.bicep \
        --parameters main.$(environment).bicepparam
```

Reference: [Bicep linter — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/linter)
