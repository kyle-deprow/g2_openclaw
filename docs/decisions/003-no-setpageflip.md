# ADR-003: Manual Page Management via rebuildPageContainer

**Date:** 2026-02-22
**Status:** Accepted

---

## Context

The original display-layout design assumed `setPageFlip(0|1)` as a native
SDK method for switching between two pre-loaded pages on the G2 glasses'
576×288 micro-LED display. Page 0 would show the "idle / recording" UI
and page 1 the streamed AI response, with hardware-accelerated flipping
between them.

The Phase 0 SDK verification spike (`phase0-sdk-findings.md`, Q1;
`phase-0-results.md`, P0.1) proved conclusively that **`setPageFlip`
does not exist** in `@evenrealities/even_hub_sdk` v0.0.7:

- Grep for `setPageFlip`, `pageFlip`, `page_flip`, or `flipPage` across
  `index.d.ts`, `index.js`, and `index.cjs` returned zero matches.
- `EvenAppMethod` enum contains exactly 10 values — none related to
  page flipping.
- `tsc --noEmit` confirms: `Property 'setPageFlip' does not exist on
  type 'EvenAppBridge'`.

The SDK models a **single active page** with up to 4 containers. There
is no built-in concept of a multi-page stack.

## Decision

Implement page management manually at the application layer:

1. The app maintains a `PageManager` abstraction that holds a registry of
   named page layouts (each a `RebuildPageContainer` configuration).
2. To switch pages, the app calls `bridge.rebuildPageContainer()` with
   the target layout — a full container replacement over BLE.
3. The current page name is tracked in app state for back-navigation.
4. `DOUBLE_CLICK_EVENT` triggers page-back (e.g., from response view
   back to idle view).

Text-only updates within the current page use `textContainerUpgrade()`
for efficiency; structural page transitions use `rebuildPageContainer()`.

## Consequences

**Benefits:**

- Design is grounded in verified SDK capabilities — no reliance on
  undocumented or non-existent methods.
- `PageManager` pattern provides a clean abstraction that can be
  extended if Even Realities adds native page-flip support in a
  future SDK release.

**Trade-offs:**

- **More complex app-side code.** The app must maintain page state,
  named layouts, and transition logic that `setPageFlip` would have
  handled natively.
- **No hardware-accelerated flip.** Each page switch involves a full
  `rebuildPageContainer()` call with a BLE round-trip, introducing a
  brief visual transition gap.
- **Delta buffering required.** During a `rebuildPageContainer()` call
  (async, awaits BLE acknowledgement), incoming assistant deltas must
  be buffered locally. The app sets a `layoutPending` flag and flushes
  buffered deltas via `textContainerUpgrade()` once the rebuild
  resolves. Without this guard, deltas targeting a not-yet-created
  container would be silently lost.
