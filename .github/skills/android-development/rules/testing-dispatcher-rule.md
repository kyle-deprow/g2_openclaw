---
title: TestDispatcherRule
impact: MEDIUM
impactDescription: deterministic coroutine tests, no flakiness
tags: testing, coroutines, dispatcher, rule
---

## TestDispatcherRule

Use a JUnit `TestWatcher` rule that replaces `Dispatchers.Main` with a
`TestDispatcher` before each test and resets it after. This makes coroutine
timing deterministic and prevents `Dispatchers.Main` missing errors in unit
tests.

**Pattern:**

```kotlin
// Place in core:testing module
class TestDispatcherRule(
    private val testDispatcher: TestDispatcher = UnconfinedTestDispatcher(),
) : TestWatcher() {

    override fun starting(description: Description) {
        Dispatchers.setMain(testDispatcher)
    }

    override fun finished(description: Description) {
        Dispatchers.resetMain()
    }
}

// Usage in tests
class MyViewModelTest {
    @get:Rule
    val dispatcherRule = TestDispatcherRule()

    // viewModelScope.launch now uses the test dispatcher
}
```

`UnconfinedTestDispatcher` runs coroutines eagerly (good for most ViewModel
tests). Use `StandardTestDispatcher` when you need explicit time control with
`advanceTimeBy()`.

Reference: [Testing coroutines — Android Developers](https://developer.android.com/kotlin/coroutines/test)
