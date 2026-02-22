---
title: Pure Kotlin Model Module
impact: MEDIUM
impactDescription: fastest builds, maximum reusability
tags: modularization, model, kotlin, no-android
---

## Pure Kotlin Model Module

The `core:model` module contains domain models as plain Kotlin data classes
with zero Android dependencies. This makes it the fastest module to compile,
testable on JVM without Robolectric, and shareable with non-Android targets
(KMP, backend).

**Incorrect (Android dependencies in model):**

```kotlin
// BAD: Room and Serialization annotations in domain model
@Entity(tableName = "topics")
@Serializable
data class Topic(
    @PrimaryKey val id: String,
    val name: String,
    @SerialName("image_url") val imageUrl: String,
)
```

**Correct (pure Kotlin — no annotations from other layers):**

```kotlin
// core:model — no Room, no Serialization, no Android imports
data class Topic(
    val id: String,
    val name: String,
    val shortDescription: String,
    val longDescription: String,
    val imageUrl: String,
)

// build.gradle.kts uses JVM library plugin, not Android
plugins {
    alias(libs.plugins.nowinandroid.jvm.library)
}
```

Reference: [NowInAndroid — core:model](https://github.com/android/nowinandroid/tree/main/core/model)
