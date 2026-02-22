---
title: Separate Model Layers with Explicit Mappers
impact: HIGH
impactDescription: decoupled layers, safe API evolution
tags: data, models, mapping, entity, network
---

## Separate Model Layers with Explicit Mappers

Maintain three distinct model types: Entity (Room), Network (Retrofit), and
Domain (pure Kotlin). Map between them with explicit extension functions. This
decouples layers — a backend API change doesn't ripple into the UI, and a DB
schema change doesn't affect the network layer.

**Incorrect (single model everywhere):**

```kotlin
// BAD: Room annotations leak into UI, network serialization tied to DB schema
@Entity(tableName = "topics")
@Serializable
data class Topic(
    @PrimaryKey val id: String,
    @SerialName("short_desc") val shortDescription: String,
    val imageUrl: String,
)
```

**Correct (separate models with mappers):**

```kotlin
// Domain model — pure Kotlin, no annotations
data class Topic(
    val id: String,
    val name: String,
    val shortDescription: String,
    val imageUrl: String,
)

// Room entity
@Entity(tableName = "topics")
data class TopicEntity(
    @PrimaryKey val id: String,
    val name: String,
    val shortDescription: String,
    val imageUrl: String,
)

// Network model
@Serializable
data class NetworkTopic(
    val id: String,
    val name: String,
    @SerialName("short_description") val shortDescription: String,
    @SerialName("image_url") val imageUrl: String,
)

// Explicit mappers
fun TopicEntity.asExternalModel() = Topic(
    id = id, name = name,
    shortDescription = shortDescription, imageUrl = imageUrl,
)

fun NetworkTopic.asEntity() = TopicEntity(
    id = id, name = name,
    shortDescription = shortDescription, imageUrl = imageUrl,
)
```

Reference: [NowInAndroid — Model mapping](https://github.com/android/nowinandroid)
