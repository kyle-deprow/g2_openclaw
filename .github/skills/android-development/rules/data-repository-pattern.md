---
title: Repository Pattern with Flow
impact: HIGH
impactDescription: consistent data access, offline-first by default
tags: data, repository, flow, interface
---

## Repository Pattern with Flow

Define a public repository interface per domain area. The implementation is
`internal` and uses offline-first strategy: Room as source of truth, network
for sync. All data is exposed as `Flow<T>`, never as one-shot `suspend` getters.

**Incorrect (suspend getter — no reactivity):**

```kotlin
// BAD: Callers must manually re-fetch to see changes
interface TopicsRepository {
    suspend fun getTopics(): List<Topic>
}
```

**Correct (Flow — reactive, offline-first):**

```kotlin
// Public interface in core:data
interface TopicsRepository {
    fun getTopics(): Flow<List<Topic>>
    fun getTopic(id: String): Flow<Topic>
    suspend fun syncWith(synchronizer: Synchronizer): Boolean
}

// Internal implementation
internal class OfflineFirstTopicsRepository @Inject constructor(
    private val topicDao: TopicDao,
    private val network: NiaNetworkDataSource,
) : TopicsRepository {

    override fun getTopics(): Flow<List<Topic>> =
        topicDao.getTopicEntities()
            .map { entities -> entities.map(TopicEntity::asExternalModel) }

    override fun getTopic(id: String): Flow<Topic> =
        topicDao.getTopicEntity(id)
            .map(TopicEntity::asExternalModel)

    override suspend fun syncWith(synchronizer: Synchronizer): Boolean =
        synchronizer.changeListSync(
            versionReader = ChangeListVersions::topicVersion,
            changeListFetcher = { network.getTopicChangeList(after = it) },
            modelUpdater = { changedIds ->
                val networkTopics = network.getTopics(ids = changedIds)
                topicDao.upsertTopics(networkTopics.map(NetworkTopic::asEntity))
            },
        )
}
```

Reference: [Google Architecture Guide — Data Layer](https://developer.android.com/topic/architecture/data-layer)
