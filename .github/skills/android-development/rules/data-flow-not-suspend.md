---
title: Expose Data as Flow, Not Suspend
impact: HIGH
impactDescription: reactive updates, eliminates manual refresh
tags: data, flow, reactive, suspend
---

## Expose Data as Flow, Not Suspend

All data in the data layer must be exposed as `Flow<T>`. Never use
`suspend fun getX(): T` for data that changes over time. Flow-based access
ensures the UI automatically updates when the underlying data changes (e.g.,
after a sync or local write).

**Incorrect (suspend — one-shot, stale data):**

```kotlin
// BAD: Caller gets a snapshot; must manually re-call to see updates
interface NewsRepository {
    suspend fun getNews(): List<NewsResource>
}
```

**Correct (Flow — reactive, auto-updating):**

```kotlin
// GOOD: Callers automatically receive updates when data changes
interface NewsRepository {
    fun getNewsResources(): Flow<List<NewsResource>>
    fun getNewsResource(id: String): Flow<NewsResource>
}

// Room DAO returns Flow — emits on every table change
@Dao
interface NewsResourceDao {
    @Query("SELECT * FROM news_resources ORDER BY publish_date DESC")
    fun getNewsResources(): Flow<List<NewsResourceEntity>>
}
```

Reference: [Room — Write async DAO queries](https://developer.android.com/training/data-storage/room/async-queries)
