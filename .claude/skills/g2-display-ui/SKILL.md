---
name: g2-display-ui
description: Even Realities G2 glasses display system and UI container architecture for the 576×288 4-bit greyscale canvas. Use when building or debugging glasses layouts, positioning text/list/image containers, implementing UI patterns like fake buttons or page flipping, or debugging clipping, tiling, flicker, or scroll behaviour.
---

# G2 Display & UI System

Principles and constraints for building UIs on the G2 AR glasses: no CSS, no DOM, no flexbox — only pixel-positioned containers rendered by firmware on a dual micro-LED display.

**Canonical reference:** `.agents/skills/g2-display-ui/SKILL.md`. This file is the distilled operating summary — read the canonical file before non-trivial work in this area.

## Core rules

- **Canvas is 576×288 px per eye**, origin `(0,0)` top-left, X right, Y down. 4-bit greyscale = 16 shades of green (0–15); `0xF` = bright green, `0x0` = off/transparent.
- **No web primitives.** No CSS, flexbox, DOM, or arbitrary pixel drawing — all layout is absolute-positioned containers (`text`, `list`, `image`).
- **Maximum 4 containers per page**, mixed types allowed. `containerTotalNum` must match the actual container count or layout corrupts.
- **Exactly ONE container must have `isEventCapture: 1`** (text/list only — image containers do not support the property). Zero or multiple = undefined behaviour.
- **Draw order = declaration order.** Later containers draw on top; there is no z-index.
- **Use SDK typos as-is:** `borderRdaius` (not `borderRadius`), `ShutDownContaniner`, `APP_REQUEST_REBUILD_PAGE_FAILD` — corrected spellings are rejected.
- **No background/fill colour.** Container interiors are always transparent; the only decoration is the border (`borderWidth` 0–5, `borderRdaius` 0–10, `paddingLength` 0–32).
- **Text is left/top aligned only**, single fixed font, no size/bold/italic. Limits: 1000 chars at `createStartUpPageContainer`/`rebuildPageContainer`, 2000 chars at `textContainerUpgrade`; ~400–500 chars fill a full 576×288 container.
- **Text overflow:** with `isEventCapture: 1` firmware scrolls internally and fires `SCROLL_TOP_EVENT`/`SCROLL_BOTTOM_EVENT` only at boundaries; without it, text clips silently.
- **Lists:** `itemCount` 1–20, `itemName` max 64 chars each, `itemWidth: 0` = auto, `isItemSelectBorderEn` for firmware highlight. No in-place update — changing items requires `rebuildPageContainer`. Events arrive as `listEvent`.
- **Images:** width 20–200 px, height 20–100 px — cannot cover the full canvas. Convert via BT.601 greyscale; do NOT dither to 1-bit on the host.
- **No image data at startup.** `createStartUpPageContainer` cannot carry image bytes — create a placeholder container, then call `updateImageRawData` after the page is built. Image sends must be sequential (`await` each).
- **Tiling trap:** an image smaller than its container tiles to fill it — match image and container dimensions exactly.
- **Lifecycle:** `createStartUpPageContainer()` exactly once (returns `StartUpPageCreateResult`: 0 success, 1 invalid, 2 oversize, 3 out of memory); `rebuildPageContainer()` for all later layout changes; `shutDownPageContainer(0|1)` to exit (1 = confirm dialog).
- **Prefer `textContainerUpgrade` over rebuild** when only text changes — in-place, flicker-free, preserves scroll. `rebuildPageContainer` destroys/recreates everything: flicker on hardware, scroll state lost.
- **No programmatic scroll control.** Chat UIs must use reverse chronological order (newest at top). Use `formatReverse()` and call `replaceTranscript(formatReverse(...))` on each streaming delta flush — never append.
- **Event capture for image apps:** place a hidden full-screen text container (`content: ' '`, `isEventCapture: 1`) behind the image; events arrive as `textEvent`. A 1×1 list container does NOT work as an event proxy.
- **Dual-mode display:** switching between transcript (text container) and menu (list container) requires a full rebuild. Error/disconnect states must call `exitMenuMode()` first, or error text goes to a list container that cannot show it.
- **Strip Markdown before display** — LLM output formatting cannot be rendered. Use `stripMarkdown()` in `g2_app/src/utils.ts`.
- **`fontSize`/`fontColor` exist in protobuf but are absent from the published `.d.ts`** — require `(container as any)` casts; hardware behaviour unverified.
- **Container names max 16 chars**; `containerID` and `containerName` must be unique within the page.
- **`xPosition`/`yPosition` are signed i32 in the simulator (since 0.7.3)** but u32 on hardware.
- **Fake interactivity with primitives:** cursor-prefix menus via `textContainerUpgrade`, selection via `borderWidth` toggling, progress bars via Unicode blocks (`━`/`─`), page flipping via pre-paginated ~450-char chunks on boundary scroll events.

## This repo

- **`g2_app/src/display.ts`** — `DisplayManager` implementation (dual transcript/menu modes, `fontSize`/`fontColor` casts).
- **`g2_app/src/conversation.ts`** — reverse-chronological conversation rendering (`formatReverse()`, newest first — no scroll API exists).
- **`g2_app/src/utils.ts`** — `stripMarkdown()` and helpers.
- **`docs/reference/g2-platform/evenhub_sdk.md`** — full SDK container reference; **`docs/reference/g2-platform/g2_reference_guide.md`** — hardware display reference.
- **`make sim`** starts the full stack (gateway + Vite + simulator) to see rendering live; `make stop` tears it down.

## Repo policy overrides

- **User message prefix is `»`, not `> `.** The canonical Pattern 7 shows `> ` as the user prefix, but `g2_app/src/conversation.ts` actually renders `» ${entry.text}`. Follow the code.
- **Ignore canonical cross-references to `docs/design/` and `docs/archive/`** — those directories do not exist in this repo. Reference docs live only under `docs/reference/g2-platform/`.
