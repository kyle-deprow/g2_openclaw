---
name: android-development
description:
  Android development with Kotlin, Jetpack Compose, and Google's official architecture guidance (NowInAndroid patterns). Use when building Android apps, feature modules, ViewModels, repositories, Room databases, Hilt DI, Compose screens, Gradle convention plugins, or multi-module projects. Triggers on tasks involving Android architecture, Kotlin coroutines/Flow, or Material 3 theming.
---

# Android Development

Build production-quality Android applications following Google's official
architecture guidance, as demonstrated in the NowInAndroid reference app.

## When to Apply

Reference these guidelines when:

- Creating new Android projects or feature modules
- Building Jetpack Compose screens with MVVM/UDF patterns
- Setting up data layer with Room, Retrofit, and offline-first sync
- Configuring multi-module Gradle builds with convention plugins
- Writing tests using test doubles (no mocking libraries)
- Implementing Hilt dependency injection
- Designing navigation with type-safe Navigation 2.8+

## Visual Design

Avoid generic "AI slop" aesthetics. Make distinctive, creative Android UIs.

- **Typography:** Choose distinctive font families via Google Fonts or bundled assets. Never default to Roboto alone. Vary choices across projects — avoid converging on common AI picks.
- **Color & Theme:** Go beyond default Material 3 dynamic color. Commit to a cohesive palette with dominant colors and sharp accents. Draw from cultural aesthetics and bold design systems. Vary between light and dark themes.
- **Motion:** Use Compose animation APIs for orchestrated screen transitions — staggered `AnimatedVisibility` reveals with `animationSpec` delays create more delight than scattered micro-interactions.
- **Avoid:** Stock Material defaults with no customization, predictable layouts, cookie-cutter component patterns, and any design that lacks context-specific character. Every screen should feel uniquely designed.

## Rule Categories by Priority

| Priority | Category         | Impact   | Prefix            |
| -------- | ---------------- | -------- | ----------------- |
| 1        | Architecture     | CRITICAL | `arch-`           |
| 2        | Compose UI       | HIGH     | `compose-`        |
| 3        | Data Layer       | HIGH     | `data-`           |
| 4        | Modularization   | HIGH     | `module-`         |
| 5        | Testing          | MEDIUM   | `testing-`        |
| 6        | Build Config     | MEDIUM   | `gradle-`         |

## Quick Reference

### 1. Architecture (CRITICAL)

- `arch-unidirectional-data-flow` - Events flow down (UI → Data), data flows up (Data → UI)
- `arch-offline-first` - Local database is source of truth; sync with remote
- `arch-uistate-sealed-interface` - Model screen state as a sealed interface (Loading, Success, Error)
- `arch-viewmodel-stateflow` - Expose UI state as StateFlow with WhileSubscribed(5000)
- `arch-use-cases-optional` - Add domain use cases only for reuse across ViewModels

### 2. Compose UI (HIGH)

- `compose-route-screen-split` - Separate Route (ViewModel + nav) from Screen (pure UI)
- `compose-stateless-components` - Components receive all data and callbacks as parameters
- `compose-state-hoisting` - Hoist state to callers; screens are stateless
- `compose-lifecycle-collection` - Use collectAsStateWithLifecycle for Flow collection
- `compose-preview-annotations` - Create multi-preview annotations (ThemePreviews, DevicePreviews)
- `compose-lazy-list-keys` - Always provide stable keys to LazyColumn/LazyRow items

### 3. Data Layer (HIGH)

- `data-repository-pattern` - Single repository interface per domain; offline-first implementation
- `data-flow-not-suspend` - Expose data as Flow<T>, never as suspend getX()
- `data-room-dao` - Use @Upsert, emit Flow from queries, delete by ID list
- `data-model-mapping` - Separate Entity, Network, and Domain models with explicit mappers
- `data-sync-worker` - Use WorkManager CoroutineWorker for background sync

### 4. Modularization (HIGH)

- `module-feature-api-impl` - Split features into api (navigation) and impl (screen + ViewModel)
- `module-core-types` - Use core modules (model, data, database, network, ui, designsystem, testing)
- `module-dependency-rules` - Features never depend on other feature impls; core never depends on features
- `module-model-pure-kotlin` - core:model has zero Android dependencies

### 5. Testing (MEDIUM)

- `testing-no-mocks` - Use test doubles implementing interfaces, not mocking libraries
- `testing-test-repository` - TestRepository with MutableSharedFlow + test hooks
- `testing-viewmodel-pattern` - Inject test doubles, assert on UiState transitions
- `testing-dispatcher-rule` - Use TestDispatcherRule to control coroutine timing
- `testing-compose-ui` - Set content with theme, assert on nodes, verify callbacks

### 6. Build Configuration (MEDIUM)

- `gradle-convention-plugins` - Extract shared config into build-logic convention plugins
- `gradle-version-catalog` - Centralize versions in libs.versions.toml
- `gradle-feature-plugin` - AndroidFeatureConventionPlugin auto-applies library + compose + hilt

## How to Use

Read individual rule files for detailed explanations and code examples:

```
rules/arch-unidirectional-data-flow.md
rules/compose-route-screen-split.md
```

Each rule file contains:

- Brief explanation of why it matters
- Incorrect code example with explanation
- Correct code example with explanation
- Additional context and references
