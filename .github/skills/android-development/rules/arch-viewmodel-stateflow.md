---
title: ViewModel StateFlow with WhileSubscribed
impact: CRITICAL
impactDescription: lifecycle-safe state, prevents leaks
tags: architecture, viewmodel, stateflow, lifecycle
---

## ViewModel StateFlow with WhileSubscribed(5000)

Expose UI state as `StateFlow` using `stateIn` with
`SharingStarted.WhileSubscribed(5_000)`. The 5-second timeout keeps the
upstream alive during configuration changes (rotation) but cancels it when the
screen is truly gone, preventing resource leaks.

**Incorrect (LiveData or hot-by-default sharing):**

```kotlin
// BAD: Eagerly collects forever, even when no one observes
val uiState = repository.getData()
    .map(::Success)
    .stateIn(viewModelScope, SharingStarted.Eagerly, Loading)
```

**Correct (WhileSubscribed with 5-second timeout):**

```kotlin
@HiltViewModel
class ForYouViewModel @Inject constructor(
    getUserNewsResourcesUseCase: GetUserNewsResourcesUseCase,
) : ViewModel() {

    val uiState: StateFlow<ForYouUiState> =
        getUserNewsResourcesUseCase()
            .map(ForYouUiState::Success)
            .stateIn(
                scope = viewModelScope,
                started = SharingStarted.WhileSubscribed(5_000),
                initialValue = ForYouUiState.Loading,
            )
}
```

The `5_000` ms timeout survives configuration changes but doesn't keep the
upstream alive indefinitely.

Reference: [StateFlow and SharedFlow — Android Developers](https://developer.android.com/kotlin/flow/stateflow-and-sharedflow)
