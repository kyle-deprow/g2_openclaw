---
title: Stable Keys for Lazy Lists
impact: HIGH
impactDescription: prevents unnecessary recomposition, smooth scrolling
tags: compose, lazy-list, keys, performance
---

## Stable Keys for Lazy Lists

Always provide a stable `key` to `items()` in `LazyColumn`/`LazyRow`. Without
keys, Compose treats list items as positionally identified—insertions or
removals cause all items to recompose. With stable keys, only changed items
recompose, and item state (scroll position, animations) is preserved.

**Incorrect (no keys — positional identity):**

```kotlin
LazyColumn {
    // BAD: Inserting at position 0 recomposes every item
    items(feedState.feed) { resource ->
        NewsResourceCard(resource)
    }
}
```

**Correct (stable keys — content identity):**

```kotlin
LazyColumn(
    contentPadding = PaddingValues(16.dp),
    verticalArrangement = Arrangement.spacedBy(16.dp),
) {
    items(
        items = feedState.feed,
        key = { it.id },
    ) { resource ->
        NewsResourceCard(resource)
    }
}
```

Reference: [Lazy lists — Item keys](https://developer.android.com/develop/ui/compose/lists#item-keys)
