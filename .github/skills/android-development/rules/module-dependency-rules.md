---
title: Module Dependency Rules
impact: HIGH
impactDescription: prevents circular deps, enforces encapsulation
tags: modularization, dependencies, architecture, boundaries
---

## Module Dependency Rules

Strict dependency rules prevent circular dependencies, enforce encapsulation,
and keep build times fast. Violations cascade into unmaintainable code.

**Allowed dependencies:**

```
app           → feature:*:impl, feature:*:api, core:*
feature:impl  → feature:*:api (other features), core:*
feature:api   → core:model (only)
core:data     → core:database, core:network, core:model, core:datastore
core:database → core:model
core:network  → core:model
core:ui       → core:model, core:designsystem
core:model    → (nothing — pure Kotlin)
```

**Forbidden dependencies:**

| Forbidden | Reason |
|-----------|--------|
| feature:impl → feature:impl | Features communicate via navigation, not direct calls |
| core → feature | Core must be feature-agnostic |
| core → app | Core must not depend on app module |
| model → anything | Model is pure Kotlin data classes |

**Incorrect:**

```kotlin
// feature/settings/impl/build.gradle.kts
// BAD: Directly depending on another feature's implementation
dependencies {
    implementation(projects.feature.topic.impl)
}
```

**Correct:**

```kotlin
// feature/settings/impl/build.gradle.kts
// GOOD: Depend on api module for navigation only
dependencies {
    implementation(projects.feature.topic.api)
}
```

Reference: [NowInAndroid — Module graph](https://github.com/android/nowinandroid/blob/main/docs/ModularizationLearningJourney.md)
