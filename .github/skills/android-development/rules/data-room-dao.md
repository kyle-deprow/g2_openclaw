---
title: Room DAO Patterns
impact: HIGH
impactDescription: correct reactive queries, efficient upserts
tags: data, room, dao, database
---

## Room DAO Patterns

Use `@Upsert` for insert-or-update operations (no manual conflict resolution).
All queries returning data must return `Flow<T>` for reactivity. Delete by ID
list for batch operations.

**Incorrect (manual insert/update logic):**

```kotlin
@Dao
interface TopicDao {
    // BAD: Caller must decide insert vs update
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertTopics(entities: List<TopicEntity>)

    // BAD: One-shot query, not reactive
    @Query("SELECT * FROM topics")
    suspend fun getTopics(): List<TopicEntity>
}
```

**Correct (Flow queries, @Upsert, batch delete):**

```kotlin
@Dao
interface TopicDao {
    // Reactive — emits on every table change
    @Query("SELECT * FROM topics")
    fun getTopicEntities(): Flow<List<TopicEntity>>

    @Query("SELECT * FROM topics WHERE id = :topicId")
    fun getTopicEntity(topicId: String): Flow<TopicEntity>

    // Upsert — inserts new, updates existing
    @Upsert
    suspend fun upsertTopics(entities: List<TopicEntity>)

    // Batch delete by ID list
    @Query("DELETE FROM topics WHERE id IN (:ids)")
    suspend fun deleteTopics(ids: List<String>)
}
```

Reference: [Room — @Upsert annotation](https://developer.android.com/reference/androidx/room/Upsert)
