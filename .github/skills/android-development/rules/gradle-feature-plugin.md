---
title: Feature Convention Plugin
impact: MEDIUM
impactDescription: consistent feature setup, zero boilerplate
tags: gradle, convention-plugins, feature, hilt, compose
---

## Feature Convention Plugin

Create an `AndroidFeatureConventionPlugin` that auto-applies library + compose
+ hilt plugins and adds common feature dependencies (core:ui, core:designsystem,
navigation, lifecycle). New feature modules need only apply this single plugin.

**Pattern:**

```kotlin
class AndroidFeatureConventionPlugin : Plugin<Project> {
    override fun apply(target: Project) {
        with(target) {
            pluginManager.apply {
                apply("nowinandroid.android.library")
                apply("nowinandroid.android.library.compose")
                apply("nowinandroid.hilt")
            }

            extensions.configure<LibraryExtension> {
                defaultConfig {
                    testInstrumentationRunner =
                        "com.example.core.testing.AppTestRunner"
                }
            }

            dependencies {
                add("implementation", project(":core:ui"))
                add("implementation", project(":core:designsystem"))
                add("implementation",
                    libs.findLibrary("androidx-hilt-navigation-compose").get())
                add("implementation",
                    libs.findLibrary("androidx-lifecycle-runtime-compose").get())
                add("implementation",
                    libs.findLibrary("androidx-lifecycle-viewmodel-compose").get())
            }
        }
    }
}
```

**Result — feature module build file is minimal:**

```kotlin
plugins {
    alias(libs.plugins.nowinandroid.android.feature)
}
android { namespace = "com.example.feature.topic.impl" }
dependencies {
    api(projects.feature.topic.api)
    implementation(projects.core.data)
}
```

Reference: [NowInAndroid — AndroidFeatureConventionPlugin](https://github.com/android/nowinandroid/tree/main/build-logic)
