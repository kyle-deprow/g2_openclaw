---
title: Test Repository Pattern
impact: MEDIUM
impactDescription: controllable, observable, no flakiness
tags: testing, test-doubles, repository, flow
---

## Test Repository Pattern

Test repositories use `MutableSharedFlow` (replay = 1) for data and expose
`sendX()` test hooks. This gives tests full control over when and what data
the repository emits, without relying on timing or real databases.

**Pattern:**

```kotlin
class TestTopicsRepository : TopicsRepository {

    // Replay = 1 so late collectors get the last emission
    private val topicsFlow = MutableSharedFlow<List<Topic>>(replay = 1)
    private val followedTopicsFlow = MutableSharedFlow<Set<String>>(replay = 1)

    // Test hooks
    fun sendTopics(topics: List<Topic>) {
        topicsFlow.tryEmit(topics)
    }

    fun sendFollowedTopics(ids: Set<String>) {
        followedTopicsFlow.tryEmit(ids)
    }

    // Interface implementation
    override fun getTopics(): Flow<List<Topic>> = topicsFlow

    override fun getTopic(id: String): Flow<Topic> =
        topicsFlow.map { topics -> topics.first { it.id == id } }

    override suspend fun setTopicFollowed(topicId: String, followed: Boolean) {
        val current = followedTopicsFlow.replayCache.firstOrNull() ?: emptySet()
        followedTopicsFlow.emit(
            if (followed) current + topicId else current - topicId,
        )
    }
}
```

Reference: [NowInAndroid — Test doubles](https://github.com/android/nowinandroid)
