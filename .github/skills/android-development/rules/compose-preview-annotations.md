---
title: Multi-Preview Annotations
impact: MEDIUM
impactDescription: consistent preview coverage across themes and devices
tags: compose, previews, annotations, theme
---

## Multi-Preview Annotations

Create custom multi-preview annotations so every component preview
automatically covers light/dark themes and multiple device sizes with a single
annotation.

**Incorrect (manual duplicate previews):**

```kotlin
// BAD: Repetitive, easy to forget one variant
@Preview(name = "Light") @Composable fun PreviewLight() { NiaTheme { MyScreen(/*...*/) } }
@Preview(name = "Dark", uiMode = UI_MODE_NIGHT_YES) @Composable fun PreviewDark() { NiaTheme(darkTheme = true) { MyScreen(/*...*/) } }
```

**Correct (multi-preview annotation):**

```kotlin
@Preview(name = "Light")
@Preview(name = "Dark", uiMode = Configuration.UI_MODE_NIGHT_YES)
annotation class ThemePreviews

@Preview(name = "Phone", device = Devices.PHONE)
@Preview(name = "Tablet", device = Devices.TABLET)
annotation class DevicePreviews

// Single annotation covers both themes
@ThemePreviews
@Composable
private fun TopicScreenPreview() {
    NiaTheme {
        TopicScreen(
            uiState = TopicUiState.Success(previewTopic, previewNews),
            onBackClick = {},
            onTopicClick = {},
            onFollowClick = {},
        )
    }
}
```

Reference: [Compose Previews — Multi-preview annotations](https://developer.android.com/develop/ui/compose/tooling/previews/multipreview-annotations)
