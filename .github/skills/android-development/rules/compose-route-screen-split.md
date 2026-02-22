---
title: Route-Screen Split
impact: HIGH
impactDescription: testable screens, Hilt-free previews
tags: compose, architecture, route, screen, separation
---

## Route-Screen Split

Separate the Route composable (ViewModel injection, navigation callbacks) from
the Screen composable (pure UI). The Screen receives all data and callbacks as
parameters, making it testable and previewable without Hilt or a ViewModel.

**Incorrect (ViewModel coupled to UI):**

```kotlin
// BAD: Screen directly injects ViewModel — can't preview or test in isolation
@Composable
fun TopicScreen(viewModel: TopicViewModel = hiltViewModel()) {
    val uiState by viewModel.uiState.collectAsStateWithLifecycle()
    when (uiState) {
        is TopicUiState.Loading -> LoadingIndicator()
        is TopicUiState.Success -> Text((uiState as TopicUiState.Success).topic.name)
    }
}
```

**Correct (Route handles DI, Screen is pure):**

```kotlin
// Route: Handles ViewModel and navigation
@Composable
internal fun TopicRoute(
    onBackClick: () -> Unit,
    onTopicClick: (String) -> Unit,
    modifier: Modifier = Modifier,
    viewModel: TopicViewModel = hiltViewModel(),
) {
    val uiState by viewModel.uiState.collectAsStateWithLifecycle()
    TopicScreen(
        uiState = uiState,
        onBackClick = onBackClick,
        onTopicClick = onTopicClick,
        onFollowClick = viewModel::followTopic,
        modifier = modifier,
    )
}

// Screen: Pure UI — testable, previewable
@Composable
internal fun TopicScreen(
    uiState: TopicUiState,
    onBackClick: () -> Unit,
    onTopicClick: (String) -> Unit,
    onFollowClick: (Boolean) -> Unit,
    modifier: Modifier = Modifier,
) {
    when (uiState) {
        TopicUiState.Loading -> LoadingState(modifier)
        is TopicUiState.Error -> ErrorState(uiState.message, modifier)
        is TopicUiState.Success -> TopicContent(
            topic = uiState.topic,
            onBackClick = onBackClick,
            onTopicClick = onTopicClick,
            onFollowClick = onFollowClick,
            modifier = modifier,
        )
    }
}
```

Reference: [NowInAndroid — Screen Architecture](https://github.com/android/nowinandroid)
