---
name: g2-display-ui
description:
  Even Realities G2 glasses display system and UI container architecture. Use when building or debugging glasses layouts, positioning containers, rendering text/list/image content, working within greyscale and resolution constraints, or implementing UI patterns like fake buttons, progress bars, and page flipping. Triggers on tasks involving G2 canvas, container properties, display rendering, or visual layout.
---

# G2 Display & UI System

Principles, constraints, and patterns for building user interfaces on the Even
Realities G2 AR glasses. There is no CSS, no DOM, and no flexbox — all layout
is pixel-positioned containers rendered by firmware on a dual micro-LED display.

## When to Apply

Reference these guidelines when:

- Creating or modifying page layouts for the G2 glasses display
- Positioning text, list, or image containers on the 576×288 canvas
- Implementing UI patterns such as fake buttons, progress bars, or page flipping
- Working with `TextContainerProperty`, `ListContainerProperty`, or `ImageContainerProperty`
- Debugging clipping, overflow, tiling, or scroll behaviour on the glasses
- Converting images to 4-bit greyscale for display
- Handling touch/scroll events routed through `isEventCapture` containers

## Rule Categories by Priority

| Priority | Category              | Impact   | Prefix       |
| -------- | --------------------- | -------- | ------------ |
| 1        | Canvas Fundamentals   | CRITICAL | `canvas-`    |
| 2        | Container Model       | CRITICAL | `container-` |
| 3        | Text Containers       | HIGH     | `text-`      |
| 4        | List Containers       | HIGH     | `list-`      |
| 5        | Image Containers      | HIGH     | `image-`     |
| 6        | Page Lifecycle        | HIGH     | `lifecycle-` |
| 7        | UI Patterns           | HIGH     | `pattern-`   |
| 8        | Unsupported Features  | MEDIUM   | `nosup-`     |

---

## 1. Canvas Fundamentals (CRITICAL)

The G2 display is a pair of micro-LED panels — one per eye — connected by a
physical FPC cable that keeps them in perfect sync. All rendering is driven by
firmware; the host (phone/watch) sends container descriptions, not pixels.

- **Resolution:** 576 × 288 pixels per eye.
- **Coordinate system:** Origin `(0, 0)` is the **top-left** corner. X increases
  rightward, Y increases downward.
- **Display technology:** Dual micro-LED panels emitting **green** light only.
- **Colour model:** 4-bit greyscale = **16 shades of green** (values 0–15).
  - White pixels (`0xF`) appear as **bright green**.
  - Black pixels (`0x0`) are **off** (transparent/invisible).
- **No web primitives.** There is no CSS, no flexbox, no DOM. All positioning is
  absolute pixel coordinates within the 576×288 canvas.

```
┌──────────────────────────────── 576 px ────────────────────────────────┐
│ (0,0)                                                        (575,0)  │
│                                                                       │
│                         G2 Canvas Per Eye                             │
│                                                                       │
│ (0,287)                                                    (575,287)  │
└───────────────────────────────────────────────────────────────────────┘
  Origin top-left · X → right · Y ↓ down · 4-bit greyscale (green)
```

---

## 2. Container Model (CRITICAL)

Every piece of visible content lives inside a **container**. Containers are the
only unit of layout. You cannot draw arbitrary pixels — only declare containers
with properties.

### Rules

- **Maximum 4 containers per page.** Mixed types (text, list, image) are allowed.
- **Exactly ONE container must have `isEventCapture: 1`.** This container
  receives scroll/touch events from the firmware. If zero or more than one
  container has this flag, behaviour is undefined.
- **`containerTotalNum` must match** the actual number of containers you send.
  A mismatch causes layout corruption or crashes.
- **Overlap is allowed.** Later containers in the array draw on top of earlier
  ones. There is no `z-index` property — declaration order is the only control.
- **Container types:** `text`, `list`, `image`.

### Shared Container Properties

Every container — regardless of type — has these common fields:

| Property          | Type   | Range                          | Notes                           |
| ----------------- | ------ | ------------------------------ | ------------------------------- |
| `xPosition`       | number | 0–576                          | Left edge of the container      |
| `yPosition`       | number | 0–288                          | Top edge of the container       |
| `width`           | number | 0–576 (images: 20–200)         | Container width in pixels       |
| `height`          | number | 0–288 (images: 20–100)         | Container height in pixels      |
| `containerID`     | number | any                            | Must be unique within the page  |
| `containerName`   | string | max 16 chars                   | Must be unique within the page  |
| `isEventCapture`  | number | 0 or 1                         | Exactly one container must be 1 |

> † `isEventCapture` applies to text and list containers only — image containers do not support this property.

### Border & Decoration (text and list containers only — NOT images)

| Property        | Range                         | Notes                                      |
| --------------- | ----------------------------- | ------------------------------------------ |
| `borderWidth`   | 0–5                           | 0 = no border                              |
| `borderColor`   | 0–15 (list), 0–16 (text)      | Greyscale level                            |
| `borderRdaius`  | 0–10                          | **SDK typo preserved from protobuf** — use `borderRdaius` NOT `borderRadius` |
| `paddingLength` | 0–32                          | Uniform padding on all four sides          |

> **Important:** There is no background colour and no fill colour. The only
> visual decoration available is the border. The interior of a container is
> always transparent (black/off).

> **Known SDK typos (preserved from protobuf):** Use the misspelled names in
> code — the SDK does not accept the corrected spellings.
> - `borderRdaius` (not `borderRadius`)
> - `ShutDownContaniner` (not `ShutDownContainer`)
> - `APP_REQUEST_REBUILD_PAGE_FAILD` (not `APP_REQUEST_REBUILD_PAGE_FAILED`)

---

## 3. Text Containers — `TextContainerProperty` (HIGH)

Text containers render plain monospaced-ish text on the canvas. They are the
most versatile container type and the building block for most UI patterns.

### Rules

- **Alignment:** Left-aligned, top-aligned only. There is no centre, right-align,
  justify, or vertical-centre option.
- **Font:** A single fixed-width-ish font. No size, bold, italic, underline, or
  font-family selection.
- **Content limits:**
  - At `createStartUpPageContainer` or `rebuildPageContainer`: **1000 characters**.
  - At `textContainerUpgrade`: **2000 characters**.
- **Line breaks:** `\n` works. Unicode characters are supported (▲, ━, ─, →, ←).
- **Full-screen capacity:** Approximately **400–500 characters** fill a 576×288
  container before overflow.
- **Overflow behaviour:**
  - With `isEventCapture: 1`: firmware handles internal scrolling. You receive
    `SCROLL_TOP_EVENT` and `SCROLL_BOTTOM_EVENT` only at the **boundaries** (not
    during intermediate scroll).
  - Without `isEventCapture: 1`: overflowing text is **clipped** silently.
- **Centering text:** Manually pad with spaces to approximate horizontal centering.

### Example: Creating a Text Container

```typescript
import { TextContainerProperty } from "@evenrealities/even_hub_sdk";

const headerContainer: TextContainerProperty = {
  containerID: 1,
  containerName: "header",
  xPosition: 0,
  yPosition: 0,
  width: 576,
  height: 40,
  borderWidth: 0,
  borderColor: 0,
  borderRdaius: 0, // SDK typo — must use this spelling
  paddingLength: 4,
  isEventCapture: 0,
  content: "SpineSense — Patient Overview",
};

const bodyContainer: TextContainerProperty = {
  containerID: 2,
  containerName: "body",
  xPosition: 0,
  yPosition: 40,
  width: 576,
  height: 248,
  borderWidth: 1,
  borderColor: 10,
  borderRdaius: 2,
  paddingLength: 4,
  isEventCapture: 1, // This container receives scroll events
  content: "Loading patient data...",
};
```

### Partial Text Updates — `TextContainerUpgrade`

`textContainerUpgrade` updates text **in-place** without a full page rebuild.
This is significantly faster than `rebuildPageContainer` and avoids flicker.

#### Rules

- The `containerID` and `containerName` **must match** the existing container.
- `contentOffset` specifies the character position to start replacing.
- `contentLength` specifies how many characters to replace.
- Maximum **2000 characters** during an upgrade.
- On the simulator, updates cause a full visual redraw. On real hardware, the
  update is smooth and partial.

#### Example: Partial Text Update

```typescript
// Update the body container's text without rebuilding the whole page
await glassesManager.textContainerUpgrade({
  containerID: 2,
  containerName: "body",
  content: "Name: Jane Doe\nAge: 34\nCondition: L4-L5 Herniation\n\nNext appointment: March 15",
  contentOffset: 0,
  contentLength: 23, // Length of "Loading patient data..."
});
```

To **append** text instead of replacing:

```typescript
const existingLength = currentText.length;
const appendText = "\n--- New Entry ---\nPain level: 4/10";

await glassesManager.textContainerUpgrade({
  containerID: 2,
  containerName: "body",
  content: appendText,
  contentOffset: existingLength,
  contentLength: 0, // 0 = insert, don't replace
});
```

> **Markdown stripping:** LLM/AI responses typically contain Markdown formatting
> (bold, italic, code, links, headings) that the G2 display cannot render. Strip
> Markdown before display. See `stripMarkdown()` in `g2_app/src/display.ts`.

> **Caution (`fontSize` / `fontColor`):** The protobuf schema defines `fontSize`
> and `fontColor` on text containers, but these are **absent from the published
> `.d.ts`**. Access requires `(container as any).fontSize = n`. Behavior on real
> hardware is unverified. See `g2_app/src/display.ts` for usage.

---

## 4. List Containers — `ListContainerProperty` (HIGH)

List containers render a native scrollable list. The firmware handles scroll
highlighting — you do not manually manage selection state.

### Rules

- **Items:** `itemCount` ranges from 1 to 20.
- **`itemWidth`:** Set to 0 for auto-width (fills container). Non-zero values
  set an explicit pixel width.
- **`isItemSelectBorderEn`:** 0 or 1. When 1, firmware draws a highlight border
  around the currently selected item.
- **`itemName`:** Each item is a plain-text string, max **64 characters**.
- **No custom per-item styling.** No icons, no secondary text, no colour per item.
- **Item height:** Auto-calculated as `containerHeight / itemCount`.
- **Single line per item.** Text that exceeds the item width is clipped.
- **No in-place update.** To change list items, you must call
  `rebuildPageContainer` with the new list.
- **Scroll events:** When a list has `isEventCapture: 1`, events arrive as
  `listEvent` (not `textEvent`). The event payload includes the selected item
  index.

### Example: Creating a List Container

```typescript
import { ListContainerProperty, ListItemContainerProperty } from "@evenrealities/even_hub_sdk";

const menuItems: ListItemContainerProperty = {
  itemCount: 5,
  itemWidth: 0, // auto
  isItemSelectBorderEn: 1,
  itemName: [
    "Dashboard",
    "Patient List",
    "Scan Results",
    "Settings",
    "About",
  ],
};

const menuList: ListContainerProperty = {
  containerID: 1,
  containerName: "menu",
  xPosition: 0,
  yPosition: 0,
  width: 576,
  height: 288,
  borderWidth: 0,
  borderColor: 0,
  borderRdaius: 0,
  paddingLength: 2,
  isEventCapture: 1, // List events arrive as listEvent
  itemContainer: menuItems,
};
```

---

## 5. Image Containers — `ImageContainerProperty` (HIGH)

Image containers display raster images converted to 4-bit greyscale.

### Rules & Constraints

- **Width:** 20–200 pixels. **Height:** 20–100 pixels.
- **Cannot cover the full 576×288 canvas** — constrained to the ranges above.
- Images are converted to 4-bit greyscale by the host using `imageToGray4`.
- **Accepted data formats:** `number[]`, `Uint8Array`, `ArrayBuffer`, base64 string.
- **Startup restriction:** You **cannot** send image data during
  `createStartUpPageContainer`. Instead, create a placeholder image container
  first, then call `updateImageRawData` after the page is built.
- **No concurrent sends.** Image data must be sent sequentially — queue updates
  if you have multiple images.
- **Tiling trap:** If the image is **smaller than the container**, the hardware
  **tiles (repeats)** the image to fill the space. Always match image dimensions
  to container dimensions exactly.
- **Do NOT** perform 1-bit dithering on the host. The firmware's 4-bit
  downsampling produces better results.

### Best Practice: Image Preparation Pipeline

1. Resize the source image to fit within 20–200 × 20–100.
2. Centre on a black canvas matching the exact container dimensions.
3. Convert to greyscale using BT.601: `0.299R + 0.587G + 0.114B`.
4. Encode as PNG.
5. Send as `number[]` via `updateImageRawData`.

### Example: Image Container Flow

```typescript
import { ImageContainerProperty } from "@evenrealities/even_hub_sdk";

// Step 1: Define the image container (placeholder — no image data yet)
const iconContainer: ImageContainerProperty = {
  containerID: 3,
  containerName: "icon",
  xPosition: 238, // Centred: (576 - 100) / 2
  yPosition: 94,  // Centred: (288 - 100) / 2
  width: 100,
  height: 100,
  isEventCapture: 0, // Images cannot capture events
};

// Step 2: Build the page with the placeholder
await glassesManager.createStartUpPageContainer({
  containerTotalNum: 2,
  textObject: [eventCaptureContainer], // Hidden text for events
  imageObject: [iconContainer],
});

// Step 3: After page is built, send the actual image data
const imageData: number[] = await prepareGreyscaleImage(
  sourceImageBuffer,
  100,  // Must match container width
  100   // Must match container height
);

await glassesManager.updateImageRawData({
  containerID: 3,
  containerName: "icon",
  imageData: imageData,
});
```

### Image Preparation Tips

Use `sharp` or Canvas to: resize → greyscale (BT.601) → raw pixels → map 8-bit to 4-bit (`Math.round(pixel / 17)`).

---

## 6. Page Lifecycle (HIGH)

The SDK provides five methods for managing page state. Understanding when to
use each is critical for smooth UX.

### `createStartUpPageContainer()`

Called **exactly once** at app startup to create the initial page layout. Returns
`StartUpPageCreateResult`:
- `0` — success
- `1` — invalid parameters
- `2` — oversize (containers exceed canvas)
- `3` — out of memory

You **cannot** send image data during this call — create placeholder image
containers and follow up with `updateImageRawData` after the page is built.

### `rebuildPageContainer()`

Used for **all subsequent layout changes** after startup. Returns
`Promise<boolean>`.

> **Flicker warning:** `rebuildPageContainer` performs a full redraw — all
> containers are destroyed and recreated. This causes a brief flicker on real
> hardware. Scroll state is lost. Delta buffering may be needed if data arrives
> while a rebuild is in-flight.

Prefer `textContainerUpgrade` when only text content changes — it avoids the
full rebuild and is flicker-free.

### `textContainerUpgrade()`

In-place text update — the **preferred** method when only text content changes.
Returns `Promise<boolean>`. Does not destroy or recreate containers, so there
is no flicker and scroll state is preserved.

See [Text Containers § Partial Text Updates](#partial-text-updates--textcontainerupgrade)
for usage details.

### `updateImageRawData()`

Sends image pixel data to an existing image container. Returns
`ImageRawDataUpdateResult`. Calls **must be sequential** — `await` each call
before sending the next image.

### `shutDownPageContainer()`

Exits the current page. Takes a mode parameter:
- `0` — immediate shutdown
- `1` — shows a confirmation dialog before shutting down

---

## 7. UI Patterns (HIGH)

The G2 display has no buttons, checkboxes, radio groups, or form controls.
Every interactive UI element must be **faked** using the primitives above.

### Pattern 1: Fake "Buttons"

Append action labels to a text container's content and track the current
selection with a `>` prefix. Use `textContainerUpgrade` to move the cursor.

```typescript
function renderMenu(items: string[], selectedIndex: number): string {
  return items
    .map((item, i) => (i === selectedIndex ? `> ${item}` : `  ${item}`))
    .join("\n");
}

// On scroll event, update selection
let currentIndex = 0;

function onScrollDown() {
  currentIndex = Math.min(currentIndex + 1, menuItems.length - 1);
  const newText = renderMenu(menuItems, currentIndex);
  glassesManager.textContainerUpgrade({
    containerID: 2,
    containerName: "body",
    content: newText,
    contentOffset: 0,
    contentLength: previousText.length,
  });
}
```

### Pattern 2: Selection Highlight via Border

Toggle `borderWidth` between 0 (unselected) and 1–3 (selected) on text
containers to visually indicate focus.

### Pattern 3: Multi-Slot Layout

Use multiple text containers as "rows". For example, three containers of height
96 each stack to fill 288px vertically. Each can have its own border state.

```typescript
// 3 equal slots filling the full height
const slots = [0, 1, 2].map((i) => ({
  containerID: i + 1,
  containerName: `slot${i}`,
  xPosition: 0,
  yPosition: i * 96,
  width: 576,
  height: 96,
  borderWidth: i === 0 ? 2 : 0, // First slot selected
  borderColor: 12,
  borderRdaius: 3,
  paddingLength: 4,
  isEventCapture: i === 0 ? 1 : 0,
    content: slotContents[i],
}));
```

### Pattern 4: Progress Bars

Use Unicode block characters in a text container to simulate progress:

```typescript
function progressBar(current: number, total: number, barWidth: number = 30): string {
  const filled = Math.round((current / total) * barWidth);
  const empty = barWidth - filled;
  return "━".repeat(filled) + "─".repeat(empty) + ` ${Math.round((current / total) * 100)}%`;
}

// Output: "━━━━━━━━━━━━───────────────────── 40%"
```

### Pattern 5: Event Capture for Image Apps

Image containers **do not support `isEventCapture`**. To receive events in an
image-focused app, place a **hidden full-screen text container** behind the
image container. The text container holds `isEventCapture: 1` and a single
space `' '` as content. Events arrive as `textEvent`.

> **Warning:** A 1×1 list container does NOT work as an event proxy — it cannot
> generate scroll events. Use a text container.

```typescript
// Hidden full-screen text container for event capture
const eventProxy: TextContainerProperty = {
  containerID: 1,
  containerName: "events",
  xPosition: 0,
  yPosition: 0,
  width: 576,
  height: 288,
  borderWidth: 0,
  borderColor: 0,
  borderRdaius: 0,
  paddingLength: 0,
  isEventCapture: 1, // Captures all touch/scroll events
  content: " ",  // Single space — invisible but required
};

// Image container drawn ON TOP of the event proxy
const displayImage: ImageContainerProperty = {
  containerID: 2,
  containerName: "photo",
  xPosition: 188, // Centred for 200px wide image
  yPosition: 94,  // Centred for 100px tall image
  width: 200,
  height: 100,
  isEventCapture: 0,
};

await glassesManager.createStartUpPageContainer({
  containerTotalNum: 2,
  textObject: [eventProxy],   // Declared first → drawn behind
  imageObject: [displayImage], // Declared second → drawn on top
});

// Events arrive as textEvent, not imageEvent
glassesManager.on("textEvent", (event) => {
  if (event.type === "SCROLL_BOTTOM_EVENT") {
    showNextImage();
  }
});
```

### Pattern 6: Page Flipping for Long Text

Pre-paginate text into chunks of ~400–500 characters. Track a `pageIndex` and
rebuild the page on `SCROLL_BOTTOM_EVENT` / `SCROLL_TOP_EVENT`. Optionally
show a page indicator in a small header or footer container.

```typescript
let pageIndex = 0;
const pages = paginateText(longArticleText); // split at ~450 chars, prefer newline breaks

function showPage(index: number) {
  bridge.rebuildPageContainer(new RebuildPageContainer({
    containerTotalNum: 3,
    textObject: [
      new TextContainerProperty({ containerID: 1, containerName: 'header',
        xPosition: 0, yPosition: 0, width: 576, height: 30,
        content: `Page ${index + 1}/${pages.length}`, isEventCapture: 0 }),
      new TextContainerProperty({ containerID: 2, containerName: 'body',
        xPosition: 0, yPosition: 30, width: 576, height: 230,
        content: pages[index], isEventCapture: 1, paddingLength: 4 }),
      new TextContainerProperty({ containerID: 3, containerName: 'footer',
        xPosition: 0, yPosition: 260, width: 576, height: 28,
        content: `${index > 0 ? '▲ Prev' : ''}    ${index < pages.length - 1 ? '▼ Next' : ''}`,
        isEventCapture: 0 }),
    ],
  }));
}

bridge.onEvenHubEvent((event) => {
  const et = event.textEvent?.eventType;
  if (et === OsEventTypeList.SCROLL_BOTTOM_EVENT && pageIndex < pages.length - 1) showPage(++pageIndex);
  if (et === OsEventTypeList.SCROLL_TOP_EVENT && pageIndex > 0) showPage(--pageIndex);
});
showPage(0);
```

---

## 8. What the Display System Does NOT Support

Understanding the boundaries prevents wasted effort and impossible designs.

- **No arbitrary pixel drawing.** You cannot address individual pixels — only
  declare containers.
- **No text alignment options.** No centre, right-align, or justify. Text is
  always left-aligned, top-aligned.
- **No font customization.** No font size, weight (bold), style (italic), or
  font-family selection.
- **No background colour or fill.** Container interiors are always transparent
  (black/off). Only border decoration is available.
- **No per-item list styling.** All list items look identical — same font, same
  size, same colour.
- **No programmatic scroll control.** You cannot set scroll position from code.
  Firmware manages scroll state internally.
- **No animations or transitions.** No fade, slide, or scale effects.
  Containers appear or disappear instantly on rebuild.
- **No z-index.** Drawing order is determined solely by declaration order in the
  container array.

---

## 9. Key Constants Quick Reference

| Constant                     | Value           |
| ---------------------------- | --------------- |
| Canvas resolution            | 576 × 288 px   |
| Max containers per page      | 4               |
| Text at startup/rebuild      | 1000 chars max  |
| Text at upgrade              | 2000 chars max  |
| Full-screen text capacity    | ~400–500 chars  |
| List items                   | 1–20            |
| Item name length             | 64 chars max    |
| Container name length        | 16 chars max    |
| Image width                  | 20–200 px       |
| Image height                 | 20–100 px       |
| Border width                 | 0–5             |
| Border radius (`borderRdaius`) | 0–10          |
| Padding                      | 0–32            |
| Greyscale depth              | 4-bit (16 shades) |
| Greyscale range              | 0–15            |

---

## Checklist Before Sending a Page

1. ☐ Total container count ≤ 4
2. ☐ `containerTotalNum` matches actual container array length
3. ☐ Exactly ONE container has `isEventCapture: 1`
4. ☐ All `containerID` values are unique within the page
5. ☐ All `containerName` values are unique and ≤ 16 chars
6. ☐ Text content ≤ 1000 chars (startup/rebuild) or ≤ 2000 chars (upgrade)
7. ☐ Image dimensions match container dimensions exactly (no tiling)
8. ☐ No image data in `createStartUpPageContainer` — use `updateImageRawData` after
9. ☐ Border properties use `borderRdaius` (SDK typo), not `borderRadius`
10. ☐ Image containers have no border properties

---

## Cross-References

- [docs/reference/g2-platform/evenhub_sdk.md](docs/reference/g2-platform/evenhub_sdk.md) — Full SDK container reference
- [docs/reference/g2-platform/g2_reference_guide.md](docs/reference/g2-platform/g2_reference_guide.md) — Hardware display reference
- [docs/design/display-layouts.md](docs/design/display-layouts.md) — Display layout design for this project
- [docs/archive/spikes/phase0-sdk-findings.md](docs/archive/spikes/phase0-sdk-findings.md) — SDK verification findings
- [g2_app/src/display.ts](g2_app/src/display.ts) — Display implementation
