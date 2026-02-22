---
title: Stateless Components
impact: HIGH
impactDescription: reusable, testable, previewable
tags: compose, components, stateless, props
---

## Stateless Components

Components receive all data and callbacks as parameters. They hold no internal
state, making them reusable, testable, and previewable with arbitrary data.

**Incorrect (stateful — manages its own data):**

```kotlin
// BAD: Component fetches its own data — can't reuse or preview
@Composable
fun TopicTag(topicId: String) {
    val topic by remember { mutableStateOf<Topic?>(null) }
    LaunchedEffect(topicId) {
        topic = repository.getTopic(topicId)
    }
    topic?.let { FilterChip(selected = it.isFollowed, onClick = { /* ??? */ }, label = { Text(it.name) }) }
}
```

**Correct (stateless — receives everything as parameters):**

```kotlin
@Composable
fun NiaTopicTag(
    text: String,
    followed: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    FilterChip(
        selected = followed,
        onClick = onClick,
        label = { Text(text) },
        modifier = modifier,
        leadingIcon = if (followed) {
            { Icon(NiaIcons.Check, contentDescription = null) }
        } else null,
    )
}
```

Reference: [Jetpack Compose — State Hoisting](https://developer.android.com/develop/ui/compose/state#state-hoisting)
