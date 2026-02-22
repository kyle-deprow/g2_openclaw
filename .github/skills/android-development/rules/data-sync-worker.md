---
title: WorkManager for Background Sync
impact: MEDIUM
impactDescription: reliable sync, survives process death
tags: data, sync, workmanager, background
---

## WorkManager for Background Sync

Use WorkManager with `CoroutineWorker` for background data synchronization.
WorkManager survives process death, respects battery optimizations, and
supports constraints (network required). Each repository's `syncWith` method
handles its own sync logic.

**Incorrect (manual sync in ViewModel):**

```kotlin
// BAD: Dies with the ViewModel; no retry, no constraints
class MyViewModel : ViewModel() {
    init {
        viewModelScope.launch {
            repository.fetchFromNetworkAndSave()
        }
    }
}
```

**Correct (WorkManager CoroutineWorker):**

```kotlin
@HiltWorker
class SyncWorker @AssistedInject constructor(
    @Assisted context: Context,
    @Assisted params: WorkerParameters,
    private val newsRepository: NewsRepository,
    private val topicsRepository: TopicsRepository,
) : CoroutineWorker(context, params), Synchronizer {

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val syncedSuccessfully = listOf(
            newsRepository.syncWith(this@SyncWorker),
            topicsRepository.syncWith(this@SyncWorker),
        ).all { it }

        if (syncedSuccessfully) Result.success()
        else Result.retry()
    }
}

// Enqueue with constraints
val syncRequest = OneTimeWorkRequestBuilder<SyncWorker>()
    .setConstraints(
        Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()
    )
    .build()
WorkManager.getInstance(context).enqueue(syncRequest)
```

Reference: [WorkManager — Android Developers](https://developer.android.com/topic/libraries/architecture/workmanager)
