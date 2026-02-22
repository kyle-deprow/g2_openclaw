---
title: Unidirectional Data Flow
impact: CRITICAL
impactDescription: prevents state bugs, enforces predictable architecture
tags: architecture, udf, mvvm, state-management
---

## Unidirectional Data Flow

Events flow down (UI → Domain → Data), data flows up (Data → Domain → UI).
This eliminates circular dependencies and makes state changes predictable.

**Incorrect (bidirectional — UI reads and writes state directly):**

```kotlin
@Composable
fun TopicScreen(viewModel: TopicViewModel = hiltViewModel()) {
    // BAD: Screen directly modifies repository state
    val topics = viewModel.repository.getTopicsSnapshot()
    Button(onClick = {
        viewModel.repository.updateTopic(topics.first())
    }) { Text("Update") }
}
```

**Correct (unidirectional — events down, data up):**

```kotlin
// ViewModel exposes state as read-only Flow, handles events
@HiltViewModel
class TopicViewModel @Inject constructor(
    private val topicsRepository: TopicsRepository,
) : ViewModel() {

    val uiState: StateFlow<TopicUiState> = topicsRepository
        .getTopics()
        .map(TopicUiState::Success)
        .stateIn(
            scope = viewModelScope,
            started = SharingStarted.WhileSubscribed(5_000),
            initialValue = TopicUiState.Loading,
        )

    fun onAction(action: TopicAction) {
        when (action) {
            is TopicAction.FollowTopic -> viewModelScope.launch {
                topicsRepository.setTopicFollowed(action.id, action.followed)
            }
        }
    }
}

// Screen receives state and sends events — never touches data layer directly
@Composable
fun TopicScreen(
    uiState: TopicUiState,
    onAction: (TopicAction) -> Unit,
) {
    when (uiState) {
        TopicUiState.Loading -> CircularProgressIndicator()
        is TopicUiState.Success -> TopicList(uiState.topics, onAction)
        is TopicUiState.Error -> ErrorMessage(uiState.message)
    }
}
```

Reference: [Google Architecture Guide — UI Layer](https://developer.android.com/topic/architecture/ui-layer)
