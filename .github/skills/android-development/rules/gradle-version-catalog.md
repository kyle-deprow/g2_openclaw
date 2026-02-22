---
title: Version Catalog in libs.versions.toml
impact: MEDIUM
impactDescription: single source of truth for dependency versions
tags: gradle, version-catalog, dependencies, configuration
---

## Version Catalog in libs.versions.toml

Centralize all dependency versions in `gradle/libs.versions.toml`. Reference
libraries and plugins via type-safe accessors (`libs.hilt.android`,
`libs.plugins.nowinandroid.hilt`). This prevents version conflicts, makes
upgrades atomic, and enables IDE autocomplete.

**Incorrect (hardcoded versions in build files):**

```kotlin
// BAD: Version scattered across modules, easy to get out of sync
dependencies {
    implementation("com.google.dagger:hilt-android:2.50")
    ksp("com.google.dagger:hilt-android-compiler:2.50")
}
```

**Correct (version catalog):**

```toml
# gradle/libs.versions.toml
[versions]
hilt = "2.50"
room = "2.6.1"
compose-bom = "2024.02.00"

[libraries]
hilt-android = { group = "com.google.dagger", name = "hilt-android", version.ref = "hilt" }
hilt-android-compiler = { group = "com.google.dagger", name = "hilt-android-compiler", version.ref = "hilt" }
room-runtime = { group = "androidx.room", name = "room-runtime", version.ref = "room" }
androidx-compose-bom = { group = "androidx.compose", name = "compose-bom", version.ref = "compose-bom" }

[plugins]
hilt = { id = "com.google.dagger.hilt.android", version.ref = "hilt" }
```

```kotlin
// Module build.gradle.kts — type-safe accessors
dependencies {
    implementation(libs.hilt.android)
    ksp(libs.hilt.android.compiler)
}
```

Reference: [Gradle Version Catalogs](https://docs.gradle.org/current/userguide/platforms.html)
