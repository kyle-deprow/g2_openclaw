---
title: Core Module Types
impact: HIGH
impactDescription: clear boundaries, reusable shared code
tags: modularization, core, modules, structure
---

## Core Module Types

Use a standard set of core modules with well-defined responsibilities. Each
module has a single concern and clear dependency direction.

**Module catalog:**

| Module | Purpose | Key Contents |
|--------|---------|-------------|
| `core:model` | Domain models (pure Kotlin) | `Topic`, `NewsResource`, `UserData` |
| `core:data` | Repositories, data coordination | `TopicsRepository`, `OfflineFirstTopicsRepository` |
| `core:database` | Room database, DAOs, entities | `AppDatabase`, `TopicDao`, `TopicEntity` |
| `core:network` | Retrofit API, network models | `RetrofitApi`, `NetworkTopic` |
| `core:datastore` | DataStore preferences | `UserPreferencesDataSource` |
| `core:common` | Shared utilities | `NiaDispatchers`, `Result` |
| `core:ui` | Reusable Compose components | `NewsFeed`, `NewsResourceCard` |
| `core:designsystem` | Theme, icons, base components | `AppTheme`, `AppIcons`, `AppButton` |
| `core:testing` | Test utilities, test doubles | `TestDispatcherRule`, `TestRepository` |

**Dependency direction:**

```
core:data → core:database, core:network, core:datastore, core:model
core:database → core:model
core:network → core:model
core:ui → core:model, core:designsystem
core:designsystem → (no core deps)
core:model → (no deps — pure Kotlin)
```

Reference: [NowInAndroid — Modularization](https://github.com/android/nowinandroid/blob/main/docs/ModularizationLearningJourney.md)
