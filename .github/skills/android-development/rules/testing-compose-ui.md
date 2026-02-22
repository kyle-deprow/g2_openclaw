---
title: Compose UI Testing
impact: MEDIUM
impactDescription: verifies rendering and interactions without device
tags: testing, compose, ui-testing, assertions
---

## Compose UI Testing

Test Screen composables by setting content with the app theme, asserting on
semantic nodes, and verifying callback invocations. Because screens are
stateless (Route-Screen split), you pass known `UiState` values directly —
no ViewModel or Hilt needed.

**Pattern:**

```kotlin
class TopicScreenTest {

    @get:Rule
    val composeTestRule = createComposeRule()

    @Test
    fun `loading state shows progress indicator`() {
        composeTestRule.setContent {
            AppTheme {
                TopicScreen(
                    uiState = TopicUiState.Loading,
                    onBackClick = {},
                    onTopicClick = {},
                    onFollowClick = {},
                )
            }
        }

        composeTestRule
            .onNodeWithTag("loadingIndicator")
            .assertIsDisplayed()
    }

    @Test
    fun `success state shows topic name`() {
        composeTestRule.setContent {
            AppTheme {
                TopicScreen(
                    uiState = TopicUiState.Success(
                        topic = testTopic,
                        newsResources = testNewsResources,
                    ),
                    onBackClick = {},
                    onTopicClick = {},
                    onFollowClick = {},
                )
            }
        }

        composeTestRule
            .onNodeWithText(testTopic.name)
            .assertIsDisplayed()
    }

    @Test
    fun `clicking follow triggers callback`() {
        var followClicked = false

        composeTestRule.setContent {
            AppTheme {
                TopicScreen(
                    uiState = TopicUiState.Success(testTopic, emptyList()),
                    onBackClick = {},
                    onTopicClick = {},
                    onFollowClick = { followClicked = true },
                )
            }
        }

        composeTestRule
            .onNodeWithContentDescription("Follow")
            .performClick()

        assertTrue(followClicked)
    }
}
```

Reference: [Testing Compose layouts — Android Developers](https://developer.android.com/develop/ui/compose/testing)
