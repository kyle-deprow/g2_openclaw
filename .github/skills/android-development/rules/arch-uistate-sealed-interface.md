---
title: UiState as Sealed Interface
impact: CRITICAL
impactDescription: exhaustive when handling, no impossible states
tags: architecture, uistate, sealed-interface, state
---

## UiState as Sealed Interface

Model every screen's state as a sealed interface with explicit variants
(Loading, Success, Error). The compiler forces exhaustive handling via `when`,
preventing forgotten states and making impossible states unrepresentable.

**Incorrect (nullable booleans — impossible states possible):**

```kotlin
// BAD: isLoading=true AND error!=null is a possible but meaningless state
data class TopicUiState(
    val isLoading: Boolean = true,
    val topics: List<Topic> = emptyList(),
    val error: String? = null,
)
```

**Correct (sealed interface — each state is distinct):**

```kotlin
sealed interface TopicUiState {
    data object Loading : TopicUiState

    data class Success(
        val topics: List<Topic>,
        val isRefreshing: Boolean = false,
    ) : TopicUiState

    data class Error(
        val message: String,
    ) : TopicUiState
}

// Compiler enforces handling every state
when (uiState) {
    TopicUiState.Loading -> LoadingIndicator()
    is TopicUiState.Success -> TopicList(uiState.topics)
    is TopicUiState.Error -> ErrorMessage(uiState.message)
}
```

Reference: [NowInAndroid — ForYouUiState](https://github.com/android/nowinandroid)
