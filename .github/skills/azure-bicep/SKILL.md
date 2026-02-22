---
name: azure-bicep
description:
  Azure Bicep and infrastructure-as-code expertise for ARM deployments, module design, security hardening, and cloud resource patterns. Use when authoring Bicep templates, designing module hierarchies, configuring Azure resources, managing deployments, or reviewing IaC for security and compliance. Triggers on tasks involving .bicep files, .bicepparam files, Azure resource definitions, ARM template conversions, or cloud infrastructure design.
---

# Azure Bicep & Infrastructure

Design, deploy, and maintain Azure infrastructure using Bicep with
security-first defaults, composable modules, and production-grade patterns.

## When to Apply

Reference these guidelines when:

- Authoring or modifying `.bicep` templates
- Designing module hierarchies for Azure resource deployments
- Creating or reviewing `.bicepparam` environment parameter files
- Configuring Azure resources (networking, compute, storage, identity, monitoring)
- Setting up CI/CD pipelines for infrastructure deployments
- Reviewing infrastructure code for security, compliance, or cost optimization
- Converting ARM JSON templates to Bicep
- Configuring `bicepconfig.json` linter rules

## Rule Categories by Priority

| Priority | Category              | Impact   | Prefix      |
| -------- | --------------------- | -------- | ----------- |
| 1        | Module Design         | CRITICAL | `module-`   |
| 2        | Naming & Conventions  | CRITICAL | `naming-`   |
| 3        | Security & Compliance | HIGH     | `security-` |
| 4        | Parameters & Types    | HIGH     | `param-`    |
| 5        | Resource Patterns     | HIGH     | `resource-` |
| 6        | Deployment & Testing  | MEDIUM   | `deploy-`   |

## Quick Reference

### 1. Module Design (CRITICAL)

- `module-single-responsibility` - Each module deploys one logical resource or tightly coupled group
- `module-inputs-outputs` - Explicit typed params and outputs; no hardcoded values
- `module-composition` - Parent orchestrates, children own resources; max two nesting levels
- `module-versioning` - Version modules via registry or path; tag breaking changes

### 2. Naming & Conventions (CRITICAL)

- `naming-resource-names` - Consistent naming: `{prefix}-{workload}-{env}-{region}-{instance}`
- `naming-file-organization` - One resource type per file; main.bicep orchestrates
- `naming-parameter-files` - Environment-specific `.bicepparam` files; no inline env conditionals
- `naming-symbolic-names` - Descriptive symbolic names matching resource purpose

### 3. Security & Compliance (HIGH)

- `security-managed-identity` - Managed identities for auth; never embed credentials
- `security-rbac-least-privilege` - Minimum role at narrowest scope; prefer built-in roles
- `security-network-isolation` - Private endpoints + NSGs by default; no public endpoints in prod
- `security-secrets-keyvault` - All secrets in Key Vault; reference via getSecret()
- `security-encryption` - Encryption at rest and in transit; CMK for sensitive workloads

### 4. Parameters & Types (HIGH)

- `param-decorators` - @description on all params; use @allowed, @minValue, @maxValue
- `param-no-hardcoded-values` - Parameterize everything environment-specific
- `param-user-defined-types` - User-defined types for complex shapes; no loose object params
- `param-secure-params` - @secure() for secrets; never output secret values

### 5. Resource Patterns (HIGH)

- `resource-api-versions` - Pin explicit stable API versions; no -preview in production
- `resource-dependencies` - Implicit deps via property references; avoid dependsOn
- `resource-idempotency` - Deployments must be re-runnable with identical results
- `resource-tags` - Mandatory tags: environment, owner, costCenter, project
- `resource-diagnostics` - Enable diagnostic settings and logging for all supported resources

### 6. Deployment & Testing (MEDIUM)

- `deploy-what-if` - Run what-if before production deployments
- `deploy-parameterize-environments` - Separate param files per environment
- `deploy-incremental-default` - Incremental mode by default; document complete mode usage
- `deploy-linting` - az bicep build + lint in CI; fail on warnings

## How to Use

Read individual rule files for detailed explanations and code examples:

```
rules/module-single-responsibility.md
rules/security-managed-identity.md
```

Each rule file contains:

- Brief explanation of why it matters
- Incorrect code example with explanation
- Correct code example with explanation
- Additional context and references

