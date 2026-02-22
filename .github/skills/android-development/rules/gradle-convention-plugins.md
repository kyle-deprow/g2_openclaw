---
title: Convention Plugins in build-logic
impact: MEDIUM
impactDescription: DRY build config, consistent module setup
tags: gradle, convention-plugins, build-logic, configuration
---

## Convention Plugins in build-logic

Extract shared Gradle configuration into convention plugins under
`build-logic/convention/`. Each plugin encapsulates a concern (Android library,
Compose, Hilt, feature). Module `build.gradle.kts` files become tiny — just
apply plugins and declare dependencies.

**Incorrect (copy-paste config in every module):**

```kotlin
// BAD: Every module repeats compileSdk, jvmTarget, compose config
plugins {
    id("com.android.library")
    id("org.jetbrains.kotlin.android")
}
android {
    compileSdk = 34
    defaultConfig { minSdk = 24 }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    buildFeatures { compose = true }
    // ... 20 more lines
}
```

**Correct (convention plugin — one-liner):**

```kotlin
// build-logic/convention/src/main/kotlin/AndroidLibraryConventionPlugin.kt
class AndroidLibraryConventionPlugin : Plugin<Project> {
    override fun apply(target: Project) {
        with(target) {
            with(pluginManager) {
                apply("com.android.library")
                apply("org.jetbrains.kotlin.android")
            }
            extensions.configure<LibraryExtension> {
                configureKotlinAndroid(this)
                defaultConfig.targetSdk = 34
            }
        }
    }
}

// Module build.gradle.kts — clean and minimal
plugins {
    alias(libs.plugins.nowinandroid.android.library)
    alias(libs.plugins.nowinandroid.hilt)
}
android { namespace = "com.example.core.data" }
dependencies {
    api(projects.core.model)
    implementation(projects.core.database)
}
```

Reference: [NowInAndroid — build-logic](https://github.com/android/nowinandroid/tree/main/build-logic)
