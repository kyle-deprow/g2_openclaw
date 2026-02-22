---
title: Offline-First Architecture
impact: CRITICAL
impactDescription: reliable UX on poor networks, data consistency
tags: architecture, offline-first, room, sync
---

## Offline-First Architecture

Local database is the source of truth. The network is a sync mechanism, not a
primary data source. Users see data immediately from Room; sync runs in the
background via WorkManager.

**Incorrect (network-first — blocks UI on network call):**

```kotlin
class TopicsRepository(private val api: TopicsApi) {
    // BAD: UI waits on network; fails without connectivity
    suspend fun getTopics(): List<Topic> = api.fetchTopics()
}
```

**Correct (offline-first — Room is source of truth):**

```kotlin
internal class OfflineFirstTopicsRepository @Inject constructor(
    private val topicDao: TopicDao,
    private val network: NiaNetworkDataSource,
) : TopicsRepository {

    // Data always comes from local database as a Flow
    override fun getTopics(): Flow<List<Topic>> =
        topicDao.getTopicEntities()
            .map { entities -> entities.map(TopicEntity::asExternalModel) }

    // Sync updates local database; Room Flow auto-emits changes
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

Reference: [Google Architecture Guide — Data Layer](https://developer.android.com/topic/architecture/data-layer/offline-first)
