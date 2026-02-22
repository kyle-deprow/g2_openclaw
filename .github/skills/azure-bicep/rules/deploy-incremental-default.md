---
title: Incremental Mode by Default
impact: MEDIUM
impactDescription: safe deployments, no accidental resource deletion, predictable updates
tags: bicep, deployment, incremental, complete-mode
---

## Incremental Mode by Default

Always deploy in `Incremental` mode (the default). In incremental mode, Azure
adds or updates resources in the template without deleting resources that are
absent from it. Use `Complete` mode only with explicit documentation and
approval — it deletes any resource in the resource group that is not in the
template.

**Incorrect (complete mode without safeguards):**

```bash
# BAD: Complete mode will DELETE resources not in the template
az deployment group create \
  --resource-group rg-spinesense-prod \
  --template-file main.bicep \
  --parameters main.prod.bicepparam \
  --mode Complete
# If a resource was removed from main.bicep by mistake, it's gone from Azure
```

**Correct (incremental mode, complete mode gated):**

```bash
# Default: Incremental mode — safe for every deployment
az deployment group create \
  --resource-group rg-spinesense-prod \
  --template-file main.bicep \
  --parameters main.prod.bicepparam \
  --mode Incremental
```

```yaml
# If Complete mode is genuinely needed, gate it behind approval
- task: AzureCLI@2
  displayName: 'What-If (Complete Mode)'
  inputs:
    azureSubscription: $(serviceConnection)
    scriptType: bash
    scriptLocation: inlineScript
    inlineScript: |
      az deployment group what-if \
        --resource-group $(resourceGroup) \
        --template-file main.bicep \
        --parameters main.$(environment).bicepparam \
        --mode Complete

- task: ManualValidation@0
  displayName: 'APPROVE: Complete mode will delete unlisted resources'
  inputs:
    notifyUsers: 'platform-team@spinesense.com'
    instructions: |
      Complete mode deployment. Review what-if output.
      Resources NOT in the template WILL BE DELETED.

- task: AzureCLI@2
  displayName: 'Deploy (Complete Mode)'
  inputs:
    azureSubscription: $(serviceConnection)
    scriptType: bash
    scriptLocation: inlineScript
    inlineScript: |
      az deployment group create \
        --resource-group $(resourceGroup) \
        --template-file main.bicep \
        --parameters main.$(environment).bicepparam \
        --mode Complete
```

Reference: [ARM deployment modes — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-resource-manager/templates/deployment-modes)
