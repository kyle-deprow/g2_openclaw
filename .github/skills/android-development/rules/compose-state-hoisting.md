---
title: State Hoisting
impact: HIGH
impactDescription: single source of truth, testable callers
tags: compose, state, hoisting, unidirectional
---

## State Hoisting

Hoist state to the caller. Composables that display state should not own it.
The caller provides state and receives events, keeping the composable reusable
and the data flow unidirectional.

**Incorrect (component owns and modifies its own state):**

```kotlin
@Composable
fun SearchBar() {
    // BAD: Parent can't read or control the query
    var query by remember { mutableStateOf("") }
    TextField(value = query, onValueChange = { query = it })
}
```

**Correct (state hoisted to caller):**

```kotlin
@Composable
fun SearchBar(
    query: String,
    onQueryChange: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    TextField(
        value = query,
        onValueChange = onQueryChange,
        modifier = modifier,
    )
}

// Caller owns the state
@Composable
fun SearchScreen(viewModel: SearchViewModel = hiltViewModel()) {
    val query by viewModel.query.collectAsStateWithLifecycle()
    SearchBar(query = query, onQueryChange = viewModel::updateQuery)
}
```

Reference: [Jetpack Compose — State Hoisting](https://developer.android.com/develop/ui/compose/state#state-hoisting)
