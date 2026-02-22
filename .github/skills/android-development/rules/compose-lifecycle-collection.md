---
title: Lifecycle-Aware Flow Collection
impact: HIGH
impactDescription: prevents wasted work when app is backgrounded
tags: compose, lifecycle, stateflow, collection
---

## Lifecycle-Aware Flow Collection

Always use `collectAsStateWithLifecycle()` to collect Flows in Compose. It
automatically stops collection when the lifecycle drops below STARTED (app
backgrounded), preventing wasted work and potential crashes from updating UI
that isn't visible.

**Incorrect (collectAsState — ignores lifecycle):**

```kotlin
@Composable
fun TopicRoute(viewModel: TopicViewModel = hiltViewModel()) {
    // BAD: Keeps collecting even when app is in background
    val uiState by viewModel.uiState.collectAsState()
    TopicScreen(uiState)
}
```

**Correct (collectAsStateWithLifecycle — lifecycle-aware):**

```kotlin
@Composable
fun TopicRoute(viewModel: TopicViewModel = hiltViewModel()) {
    // GOOD: Stops collection when app is backgrounded
    val uiState by viewModel.uiState.collectAsStateWithLifecycle()
    TopicScreen(uiState)
}
```

Requires dependency: `androidx.lifecycle:lifecycle-runtime-compose`

Reference: [Collect flows lifecycle-aware — Android Developers](https://developer.android.com/develop/ui/compose/side-effects#collectAsStateWithLifecycle)
