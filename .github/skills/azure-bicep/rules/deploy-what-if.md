---
title: What-If Before Production Deployments
impact: MEDIUM
impactDescription: prevents accidental deletions, validates changes, builds deployment confidence
tags: bicep, deployment, what-if, safety
---

## What-If Before Production Deployments

Run `az deployment group what-if` (or the equivalent pipeline task) before every
production deployment. Review the diff to confirm only intended changes will be
applied. What-if catches accidental resource deletions, unexpected property
changes, and parameter mismatches before they hit production.

**Incorrect (deploy directly without preview):**

```bash
# BAD: Straight to production with no safety net
az deployment group create \
  --resource-group rg-spinesense-prod \
  --template-file main.bicep \
  --parameters main.prod.bicepparam
```

**Correct (what-if first, then deploy):**

```bash
# Step 1: Preview changes — review the output carefully
az deployment group what-if \
  --resource-group rg-spinesense-prod \
  --template-file main.bicep \
  --parameters main.prod.bicepparam

# Step 2: Deploy only after confirming the diff
az deployment group create \
  --resource-group rg-spinesense-prod \
  --template-file main.bicep \
  --parameters main.prod.bicepparam
```

```yaml
# In a CI/CD pipeline (Azure DevOps example)
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

- task: ManualValidation@0
  displayName: 'Approve Deployment'
  inputs:
    notifyUsers: 'platform-team@spinesense.com'
    instructions: 'Review the what-if output above before approving.'

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

Reference: [ARM what-if operation — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-resource-manager/templates/deploy-what-if)
