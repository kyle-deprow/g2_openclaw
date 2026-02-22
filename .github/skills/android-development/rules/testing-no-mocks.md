---
title: Test Doubles Over Mocking Libraries
impact: MEDIUM
impactDescription: less brittle tests, exercises more production code
tags: testing, test-doubles, no-mocks, interfaces
---

## Test Doubles Over Mocking Libraries

Create hand-written test doubles that implement the same interfaces as
production code. Do not use Mockito, MockK, or other mocking libraries.
Test doubles provide realistic implementations with test hooks, resulting in
less brittle tests that exercise more of the production code path.

**Incorrect (mocking library — brittle, tests implementation details):**

```kotlin
// BAD: Tightly coupled to implementation; breaks if method signature changes
val mockRepo = mockk<TopicsRepository>()
every { mockRepo.getTopics() } returns flowOf(testTopics)
```

**Correct (test double — implements interface):**

```kotlin
class TestTopicsRepository : TopicsRepository {

    private val topicsFlow = MutableSharedFlow<List<Topic>>(replay = 1)

    // Test hook — control what data the repository emits
    fun sendTopics(topics: List<Topic>) {
        topicsFlow.tryEmit(topics)
    }

    // Interface implementation
    override fun getTopics(): Flow<List<Topic>> = topicsFlow

    override fun getTopic(id: String): Flow<Topic> =
        topicsFlow.map { topics -> topics.first { it.id == id } }

    override suspend fun syncWith(synchronizer: Synchronizer): Boolean = true
}
```

Place test doubles in `core:testing` so they're reusable across all modules.

Reference: [NowInAndroid — Testing strategy](https://github.com/android/nowinandroid)
