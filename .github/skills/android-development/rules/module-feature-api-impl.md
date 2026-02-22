---
title: Feature Module API/Impl Split
impact: HIGH
impactDescription: build isolation, encapsulation, parallel team work
tags: modularization, feature, api, impl, navigation
---

## Feature Module API/Impl Split

Split every feature into two submodules: `api` (public navigation contracts)
and `impl` (internal screen, ViewModel, DI). The `api` module is lightweight
and depended on by other features for cross-feature navigation. The `impl`
module is internal and only depended on by the app module.

**Incorrect (single feature module — everything public):**

```
feature/topic/
  ├── build.gradle.kts
  └── src/main/kotlin/
      ├── TopicScreen.kt       # Other features could import this
      ├── TopicViewModel.kt
      └── TopicNavigation.kt
```

**Correct (api/impl split):**

```
feature/topic/
├── api/
│   ├── build.gradle.kts        # Depends on core:model only
│   └── src/main/kotlin/
│       └── TopicNavigation.kt  # @Serializable route + NavController extension
└── impl/
    ├── build.gradle.kts        # Depends on api + core modules
    └── src/main/kotlin/
        ├── TopicScreen.kt       # internal
        ├── TopicViewModel.kt    # internal
        ├── TopicUiState.kt      # internal
        ├── TopicNavigation.kt   # NavGraphBuilder.topicScreen()
        └── di/TopicModule.kt    # Hilt bindings
```

```kotlin
// feature/topic/api — public
@Serializable
data class TopicRoute(val id: String)

fun NavController.navigateToTopic(topicId: String) {
    navigate(TopicRoute(topicId))
}

// feature/topic/impl — internal
fun NavGraphBuilder.topicScreen(
    onBackClick: () -> Unit,
    onTopicClick: (String) -> Unit,
) {
    composable<TopicRoute> {
        TopicRoute(onBackClick = onBackClick, onTopicClick = onTopicClick)
    }
}
```

Reference: [NowInAndroid — Modularization](https://github.com/android/nowinandroid/blob/main/docs/ModularizationLearningJourney.md)
