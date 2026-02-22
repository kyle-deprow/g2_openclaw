---
title: Domain Use Cases Are Optional
impact: MEDIUM
impactDescription: reduces boilerplate when logic is simple
tags: architecture, domain, use-case, clean-architecture
---

## Domain Use Cases Are Optional

The domain layer exists to encapsulate reusable business logic shared across
multiple ViewModels. If logic is only used in one ViewModel, keep it there.
Don't create a use case just for the sake of layering.

**Incorrect (wrapper use case with no added logic):**

```kotlin
// BAD: Passthrough use case adds complexity without value
class GetTopicsUseCase @Inject constructor(
    private val topicsRepository: TopicsRepository,
) {
    operator fun invoke(): Flow<List<Topic>> = topicsRepository.getTopics()
}
```

**Correct (use case that combines or transforms multiple sources):**

```kotlin
// GOOD: Combines data from two repositories — reused by multiple ViewModels
class GetUserNewsResourcesUseCase @Inject constructor(
    private val newsRepository: NewsRepository,
    private val userDataRepository: UserDataRepository,
) {
    operator fun invoke(): Flow<List<UserNewsResource>> =
        newsRepository.getNewsResources()
            .combine(userDataRepository.userData) { newsResources, userData ->
                newsResources.mapToUserNewsResources(userData)
            }
}
```

**When to create a use case:**

- Logic is reused by 2+ ViewModels
- Complex transformations combining multiple data sources
- Business rules that don't belong in UI or Data layer

Reference: [Google Architecture Guide — Domain Layer](https://developer.android.com/topic/architecture/domain-layer)
