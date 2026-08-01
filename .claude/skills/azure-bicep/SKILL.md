---
name: azure-bicep
description: Azure Bicep and infrastructure-as-code expertise for ARM deployments, module design, and security hardening. Use when authoring .bicep or .bicepparam files, configuring Azure resources, running infra deployments, or reviewing IaC for security and compliance.
---

# Azure Bicep & Infrastructure

Design, deploy, and maintain Azure infrastructure using Bicep with security-first defaults, composable modules, and production-grade patterns.

**Canonical reference:** `.agents/skills/azure-bicep/SKILL.md` and `rules/*.md`. This file is the distilled operating summary — read the canonical file (and the specific rules/*.md files) before non-trivial work in this area.

## Core rules

- **One logical resource (or tightly coupled group) per module**; explicit typed params and outputs, no hardcoded values.
- **Parent orchestrates, children own resources**; maximum two nesting levels. Version modules via registry or path; tag breaking changes.
- **Naming convention**: `{prefix}-{workload}-{env}-{region}-{instance}`; one resource type per file with `main.bicep` orchestrating; descriptive symbolic names matching resource purpose.
- **Environment-specific `.bicepparam` files** — no inline environment conditionals in templates.
- **Managed identities for all service-to-service auth**; never embed connection strings with passwords, service-principal secrets, or credentials as deployment parameters — they leak in deployment history. Grant access via `roleAssignments` with `principalId`.
- **RBAC least privilege**: minimum role at narrowest scope; prefer built-in roles.
- **Network isolation by default**: private endpoints + NSGs; no public endpoints in prod.
- **All secrets in Key Vault**, referenced via `getSecret()`; mark secret params `@secure()` and never output secret values.
- **Encryption at rest and in transit**; CMK for sensitive workloads.
- **`@description` on every param**; constrain with `@allowed`, `@minValue`, `@maxValue`; user-defined types for complex shapes — no loose `object` params.
- **Pin explicit stable (GA) API versions**; no `-preview` in production (documented exception only when no GA version supports a required feature). Update versions deliberately during planned maintenance.
- **Implicit dependencies via property references**; avoid `dependsOn`.
- **Deployments must be idempotent** — re-runnable with identical results.
- **Mandatory tags**: environment, owner, costCenter, project. Enable diagnostic settings/logging for all supported resources.
- **Run what-if before production deployments**; incremental mode by default (document any complete-mode usage).
- **`az bicep build` + lint in CI; fail on warnings.**
- **Rule files** live at `.agents/skills/azure-bicep/rules/<prefix>-<name>.md` with incorrect/correct examples; prefixes: `module-`, `naming-`, `security-`, `param-`, `resource-`, `deploy-`.

## This repo

- Bicep sources: `infra/main.bicep` orchestrating modules in `infra/modules/` (`ai-hub.bicep`, `ai-project.bicep`, `keyvault.bicep`, `monitoring.bicep`, `openai.bicep`, `storage.bicep`).
- Environment parameters: `infra/parameters/dev.bicepparam`; linter config: `infra/bicepconfig.json`.
- Deployment is driven by a Typer CLI at `infra/main.py` (with `infra/azure_ops.py`, `infra/console.py`) — use it rather than raw `az deployment` invocations.
- Makefile targets: `make infra-validate`, `make infra-lint`, `make infra-deploy`, `make infra-destroy`.
