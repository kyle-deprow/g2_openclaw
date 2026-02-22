---
title: ViewModel Testing Pattern
impact: MEDIUM
impactDescription: validates state transitions, catches regressions
tags: testing, viewmodel, stateflow, coroutines
---

## ViewModel Testing Pattern

Inject test doubles into the ViewModel, trigger actions, and assert on
`UiState` transitions. Use `TestDispatcherRule` to control coroutine timing
and Turbine for `Flow` assertion.

**Pattern:**

```kotlin
class TopicViewModelTest {

    @get:Rule
    val dispatcherRule = TestDispatcherRule()

    private val topicsRepository = TestTopicsRepository()
    private val userDataRepository = TestUserDataRepository()
    private lateinit var viewModel: TopicViewModel

    @Before
    fun setup() {
        viewModel = TopicViewModel(
            savedStateHandle = SavedStateHandle(mapOf("topicId" to "test-1")),
            topicsRepository = topicsRepository,
            userDataRepository = userDataRepository,
        )
    }

    @Test
    fun `uiState is Loading when initialized`() = runTest {
        assertEquals(TopicUiState.Loading, viewModel.uiState.value)
    }

    @Test
    fun `uiState is Success when topic data is available`() = runTest {
        topicsRepository.sendTopics(listOf(testTopic))
        userDataRepository.setUserData(testUserData)

        val uiState = viewModel.uiState.first { it is TopicUiState.Success }
        assertTrue(uiState is TopicUiState.Success)
    }

    @Test
    fun `followTopic updates repository`() = runTest {
        topicsRepository.sendTopics(listOf(testTopic))
        viewModel.followTopic(true)

        val userData = userDataRepository.userData.first()
        assertTrue("test-1" in userData.followedTopics)
    }
}
```

Reference: [Testing coroutines — Android Developers](https://developer.android.com/kotlin/coroutines/test)
