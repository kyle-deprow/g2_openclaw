---
name: g2-display-ui
description: Design and implement G2 OpenClaw glasses screens with the installed EvenHub container API. Use for canvas layout, text/list/image containers, transcript rendering, page rebuilds, in-place text updates, stacking, readable HUD interactions, or display validation and clipping.
---

# G2 display UI

Design for a glanceable firmware-rendered HUD, not a miniature web page.

## Enforce the locked SDK 0.0.11 contract

- Use the 576×288 canvas with origin at the top-left.
- Design in 4-bit monochrome green: 16 intensity levels; black pixels are off.
- Position containers with absolute coordinates. Glasses containers do not support CSS, DOM layout, flexbox, or arbitrary drawing.
- Allow 1–12 containers total: at most 4 image containers and 8 text/list containers.
- Give every container a unique `containerID` and a unique `containerName` of at most 16 characters.
- Set exactly one text or list container to `isEventCapture: 1`; set all other capture flags to `0`.
- Keep every rectangle within the canvas and set `containerTotalNum` to the actual total.
- Use `borderRadius`; never copy the stale `borderRdaius` spelling.

SDK `0.0.11` has no `zOrderIndex`; use declaration order for overlap. SDK `0.0.12` adds z-ordering, but only after migration: either every container on a page has a unique `zOrderIndex` or none do, and larger values render in front.

## Choose the right container

### Text

- Use `TextContainerProperty` for plain, left/top-aligned text.
- Keep startup and rebuild content at or below 1,000 characters.
- Keep `TextContainerUpgrade.content` at or below 2,000 characters.
- Expect roughly 400–500 characters in a full-screen container, depending on glyphs.
- Use `textContainerUpgrade` for frequent changes with exact matching IDs and names.
- Do not assume font family, size, weight, alignment, background fill, or animation controls exist in the declarations.

### List

- Use `ListContainerProperty` for firmware-managed selection and scrolling.
- Limit a list to 20 items and item labels to 64 characters.
- Rebuild the page to change list contents; there is no list-upgrade API.
- Do not assume per-row styling, separators, or row-height controls.

### Image

- Keep each `ImageContainerProperty` within 20–288 px wide and 20–144 px high.
- Create an empty image container, then call `updateImageRawData` after page creation.
- Queue image updates; never send them concurrently.
- Supply a supported `number[]`, `Uint8Array`, `ArrayBuffer`, or base64 payload and design for 4-bit greyscale.
- For image-first screens, put a blank text capture container behind the image.

## Prefer stable updates

- Call `createStartUpPageContainer` once.
- Use `textContainerUpgrade` for status, footer, and transcript changes; it avoids hardware flicker.
- Use `rebuildPageContainer` only for layout/type changes; it resets firmware scroll and selection.
- Check every SDK return value and serialize display operations so BLE-bound updates do not race.

## Follow this repository's UI contract

- Boot directly into the single autoresearch thread view; do not recreate a session menu.
- Keep newest transcript entries at the top because there is no programmatic scroll API.
- Preserve stable status, transcript, and footer regions from `g2_app/src/display.ts` unless a product change requires a new layout.
- Strip Markdown before display and validate non-ASCII symbols in the simulator and on hardware.
- Keep the primary action obvious and avoid rapid full-page redraws or decorative image churn.

## Verify visually

1. Unit-test counts, bounds, unique IDs/names, one capture target, text limits, and version-appropriate stacking.
2. Use simulator screenshots for layout regressions; keep native framebuffer screenshots in RGBA and inspect alpha for lit pixels.
3. Validate font fit, greyscale, scrolling, image transfer, and flicker on physical glasses.

Official references: [display system](https://hub.evenrealities.com/docs/build/display), [page lifecycle](https://hub.evenrealities.com/docs/build/page-lifecycle), and [design guidelines](https://hub.evenrealities.com/docs/build/design-guidelines).
