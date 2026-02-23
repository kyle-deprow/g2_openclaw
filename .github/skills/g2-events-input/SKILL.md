---
name: g2-events-input
description:
  Even Realities G2 input event system, event routing, interaction patterns, and audio/microphone control. Use when handling user input from the R1 ring or temple gestures, implementing event listeners, debugging event routing issues, processing audio streams, or managing device status callbacks. Triggers on tasks involving onEvenHubEvent, OsEventTypeList, event capture, audio PCM, or input handling.
---

# G2 Events & Input Handling

Complete reference for input events, event routing, audio capture, and device
status on the Even Realities G2 AR glasses platform. Covers the R1 ring,
temple gestures, microphone PCM streaming, and lifecycle callbacks.

## When to Apply

Reference these guidelines when:

- Handling user input from the R1 ring or temple touch gestures
- Implementing `onEvenHubEvent` listeners or processing `EvenHubEvent` payloads
- Debugging why events are not arriving or are arriving with unexpected shapes
- Working with microphone audio (opening, closing, processing PCM frames)
- Managing device status callbacks (`onDeviceStatusChanged`)
- Implementing foreground/background lifecycle handling
- Setting `isEventCapture` on containers to control event routing

## Rule Categories by Priority

| Priority | Category              | Impact   | Prefix      |
| -------- | --------------------- | -------- | ----------- |
| 1        | Event Quirks          | CRITICAL | `quirk-`    |
| 2        | Event Routing         | CRITICAL | `routing-`  |
| 3        | Audio System          | HIGH     | `audio-`    |
| 4        | Input Best Practices  | HIGH     | `input-`    |
| 5        | Device Status         | MEDIUM   | `status-`   |

---

## 1. Hardware Input Sources

The G2 platform has two physical input devices:

- **G2 Glasses** — capacitive touch strips on the temple tips. Support tap,
  double-tap, and swipe (scroll) gestures.
- **R1 Ring** — a separate BLE-connected ring worn on the finger. Supports tap,
  double-tap, and scroll gestures via a small trackpad.

Both devices produce the **same event types** through the SDK. Your code does
not need to distinguish which device generated the event — the `OsEventTypeList`
value is identical regardless of source.

**Important hardware constraints:**

- The G2 glasses have **no camera** and **no speaker**.
- Audio output is not possible on the glasses themselves.
- The glasses have a **microphone** for voice input (see Audio System below).

---

## 2. Event Types — `OsEventTypeList` Enum

Every input and system event is identified by a numeric value from the
`OsEventTypeList` enum:

| Event                    | Value | Source(s)                        | Description                                        |
| ------------------------ | ----- | -------------------------------- | -------------------------------------------------- |
| `CLICK_EVENT`            | 0     | Ring tap, temple tap             | Single tap / select action                         |
| `SCROLL_TOP_EVENT`       | 1     | Scroll gesture                   | Internal scroll reached the **top** boundary       |
| `SCROLL_BOTTOM_EVENT`    | 2     | Scroll gesture                   | Internal scroll reached the **bottom** boundary    |
| `DOUBLE_CLICK_EVENT`     | 3     | Ring double-tap, temple double-tap | Double tap action                                |
| `FOREGROUND_ENTER_EVENT` | 4     | System                           | App comes to foreground                            |
| `FOREGROUND_EXIT_EVENT`  | 5     | System                           | App goes to background                             |
| `ABNORMAL_EXIT_EVENT`    | 6     | System                           | Unexpected disconnect or crash                     |

### CRITICAL: Scroll events are BOUNDARY events

`SCROLL_TOP_EVENT` and `SCROLL_BOTTOM_EVENT` are **NOT** raw gesture events.
They fire **only** when the firmware's internal scroll position reaches the top
or bottom boundary of the scrollable content. They do **not** fire on every
individual scroll gesture.

This means:

- If a list has 5 items and the user scrolls down from item 0 → 1, no
  `SCROLL_BOTTOM_EVENT` fires.
- When the user scrolls past the last item, `SCROLL_BOTTOM_EVENT` fires once.
- When the user scrolls back past the first item, `SCROLL_TOP_EVENT` fires once.
- Between boundaries, the firmware handles scroll navigation silently — your
  app receives no scroll events.

```typescript
// You will NOT see events for every scroll step.
// You only see boundary hits:
//   SCROLL_TOP_EVENT    → user tried to scroll past the top
//   SCROLL_BOTTOM_EVENT → user tried to scroll past the bottom
```

---

## 3. Event Delivery System

### Subscribing to Events

All events arrive through a single callback registered via
`bridge.onEvenHubEvent()`. The function returns an unsubscribe handle.

```typescript
import { waitForEvenAppBridge, OsEventTypeList } from '@evenrealities/even_hub_sdk';

async function setupEventListener() {
  const bridge = await waitForEvenAppBridge();

  const unsubscribe = bridge.onEvenHubEvent((event) => {
    // event is an EvenHubEvent object
    console.log('Received event:', JSON.stringify(event));

    // Determine which sub-event is populated
    if (event.listEvent) {
      handleListEvent(event.listEvent);
    } else if (event.textEvent) {
      handleTextEvent(event.textEvent);
    } else if (event.sysEvent) {
      handleSysEvent(event.sysEvent);
    } else if (event.audioEvent) {
      handleAudioEvent(event.audioEvent);
    }
  });

  // Later, to stop listening:
  // unsubscribe();
  return unsubscribe;
}
```

### `EvenHubEvent` Shape

The callback receives an `EvenHubEvent` object. Exactly **one** of the
following fields will be populated per event:

| Field          | Type                     | When populated                         |
| -------------- | ------------------------ | -------------------------------------- |
| `listEvent`    | `List_ItemEvent`         | Input on a list container              |
| `textEvent`    | `Text_ItemEvent`         | Input on a text container              |
| `sysEvent`     | `Sys_ItemEvent`          | System-level events                    |
| `audioEvent`   | `AudioEventPayload`      | Microphone PCM data frames             |
| `jsonData`     | `Record<string, any>`    | Raw payload (useful for debugging)     |

---

## 4. Event Data Models

### `List_ItemEvent`

Sent when the user interacts with a list container that has event capture.

| Field                      | Type                           | Description                          |
| -------------------------- | ------------------------------ | ------------------------------------ |
| `containerID`              | `number \| undefined`          | Numeric ID of the list container     |
| `containerName`            | `string \| undefined`          | Name of the list container           |
| `currentSelectItemName`    | `string \| undefined`          | Display text of selected item        |
| `currentSelectItemIndex`   | `number \| undefined`          | 0-based index of selected item       |
| `eventType`                | `OsEventTypeList \| undefined` | Which event occurred                 |

```typescript
function handleListEvent(listEvent: List_ItemEvent) {
  const { containerID, containerName, currentSelectItemName,
          currentSelectItemIndex, eventType } = listEvent;

  console.log(`List "${containerName}" (ID: ${containerID})`);
  console.log(`Selected: "${currentSelectItemName}" at index ${currentSelectItemIndex}`);
  console.log(`Event type: ${eventType}`);
}
```

### `Text_ItemEvent`

Sent when the user interacts with a text container that has event capture.

| Field            | Type               | Description                        |
| ---------------- | ------------------ | ---------------------------------- |
| `containerID`    | `number`           | Numeric ID of the text container   |
| `containerName`  | `string`           | Name of the text container         |
| `eventType`      | `OsEventTypeList`  | Which event occurred               |

```typescript
function handleTextEvent(textEvent: Text_ItemEvent) {
  const { containerID, containerName, eventType } = textEvent;
  console.log(`Text "${containerName}" (ID: ${containerID}), event: ${eventType}`);
}
```

### `Sys_ItemEvent`

System-level events that are not tied to a specific container.

| Field       | Type               | Description            |
| ----------- | ------------------ | ---------------------- |
| `eventType` | `OsEventTypeList`  | Which event occurred   |

```typescript
function handleSysEvent(sysEvent: Sys_ItemEvent) {
  const { eventType } = sysEvent;
  console.log(`System event: ${eventType}`);
}
```

---

## 5. Event Routing Rules (CRITICAL)

Event routing is determined by which container has `isEventCapture: 1` set.
This is the single most important configuration for input handling.

### Rule: Only ONE container should have `isEventCapture: 1` at a time.

### Routing by container type:

**When a List container has capture (`isEventCapture: 1`):**

- Scroll gestures are handled **natively by the firmware** — the visible
  selection highlight moves up/down automatically.
- Your app receives `listEvent` with `SCROLL_TOP_EVENT` or
  `SCROLL_BOTTOM_EVENT` only when the user hits a boundary.
- Click and double-click events arrive as `listEvent` with the currently
  selected item's name and index.

**When a Text container has capture (`isEventCapture: 1`):**

- The firmware scrolls text content internally.
- Boundary events arrive as `textEvent`.
- Click and double-click events arrive as `textEvent`.

**Image containers:**

- Image containers do **NOT** have an `isEventCapture` property.
- They cannot receive events directly.
- To handle input on a screen with only images, pair images with a hidden text
  or list container that has `isEventCapture: 1`.

### Routing example:

```typescript
import {
  waitForEvenAppBridge,
  OsEventTypeList,
  type EvenHubEvent,
} from '@evenrealities/even_hub_sdk';

// --- Scenario A: List container has capture ---
// Firmware handles scroll highlighting automatically.
// You respond to CLICK and boundary events only.
function handleListRouting(event: EvenHubEvent) {
  if (!event.listEvent) return;

  const { eventType, currentSelectItemIndex, currentSelectItemName } = event.listEvent;

  if (eventType === OsEventTypeList.CLICK_EVENT || eventType === undefined) {
    console.log(`User selected: "${currentSelectItemName}" at ${currentSelectItemIndex}`);
    // Navigate or act on selection
  }
  if (eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT) {
    console.log('Reached bottom of list — load more or wrap around');
  }
  if (eventType === OsEventTypeList.SCROLL_TOP_EVENT) {
    console.log('Reached top of list — wrap or ignore');
  }
}

// --- Scenario B: Text container has capture ---
// Firmware scrolls text internally.
// You respond to CLICK and boundary events.
function handleTextRouting(event: EvenHubEvent) {
  if (!event.textEvent) return;

  const { eventType } = event.textEvent;

  if (eventType === OsEventTypeList.CLICK_EVENT || eventType === undefined) {
    console.log('User clicked while viewing text');
  }
  if (eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT) {
    console.log('Text fully scrolled — show next page or exit');
  }
}
```

---

## 6. Event Quirks (CRITICAL)

These are known SDK behaviors and bugs that **will** cause issues if not
handled. Every G2 app must account for all five.

### Quirk 1: `CLICK_EVENT = 0` becomes `undefined`

The SDK's `fromJson` deserialization normalizes the numeric value `0` to
`undefined`. Since `CLICK_EVENT` has value `0`, click events will have
`eventType === undefined` instead of `eventType === 0`.

**Always use this pattern:**

```typescript
if (eventType === OsEventTypeList.CLICK_EVENT || eventType === undefined) {
  // Handle click — this covers both real hardware and the SDK bug
}
```

**Never** write `if (eventType === OsEventTypeList.CLICK_EVENT)` alone — it
will miss clicks when `eventType` is `undefined`.

### Quirk 2: Missing `currentSelectItemIndex` at index 0

The simulator (and occasionally real hardware) omits
`currentSelectItemIndex` when the selected item is at index 0. The field
may be `undefined` or missing entirely.

**Mitigation:** Always track the selected index in your own application state.
Do not rely solely on `currentSelectItemIndex` from the event payload.

```typescript
let selectedIndex = 0; // Track in your own state

function handleListClick(listEvent: List_ItemEvent) {
  // Use event index if available, fall back to tracked state
  const index = listEvent.currentSelectItemIndex ?? selectedIndex;
  console.log(`Acting on item at index: ${index}`);
}
```

### Quirk 3: Simulator vs Real Device event source

The simulator and real hardware send events through **different** sub-event
fields:

- **Simulator:** Sends button clicks as `sysEvent`
- **Real hardware:** Sends clicks as `listEvent` or `textEvent` depending
  on which container has `isEventCapture: 1`

**You MUST handle ALL THREE event sources** to work in both environments:

```typescript
bridge.onEvenHubEvent((event) => {
  const eventType =
    event.listEvent?.eventType ??
    event.textEvent?.eventType ??
    event.sysEvent?.eventType;

  if (eventType === OsEventTypeList.CLICK_EVENT || eventType === undefined) {
    handleClick();
  } else if (eventType === OsEventTypeList.DOUBLE_CLICK_EVENT) {
    handleDoubleClick();
  } else if (eventType === OsEventTypeList.SCROLL_TOP_EVENT) {
    handleScrollTop();
  } else if (eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT) {
    handleScrollBottom();
  }
});
```

### Quirk 4: Swipe / Scroll throttling

Scroll events can fire in rapid succession when the user swipes quickly.
Without throttling, your app may process multiple page changes or list jumps
from a single physical gesture.

**Use a 300ms cooldown:**

```typescript
let lastScrollTime = 0;

function handleScroll(eventType: OsEventTypeList) {
  const now = Date.now();
  if (now - lastScrollTime < 300) return; // Discard rapid duplicates
  lastScrollTime = now;

  if (eventType === OsEventTypeList.SCROLL_TOP_EVENT) {
    // Navigate to previous page or wrap
  } else if (eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT) {
    // Navigate to next page or wrap
  }
}
```

### Quirk 5: `textContainerUpgrade` visual difference

Simulator causes a full redraw flash; real hardware updates smoothly. Do not optimize around the simulator flicker.

---

## 7. Audio System

### Opening and Closing the Microphone

```typescript
await bridge.audioControl(true);   // open mic — starts PCM stream
await bridge.audioControl(false);  // close mic — stops PCM stream
// PREREQUISITE: createStartUpPageContainer must be called first
```

### PCM Format Specification

| Parameter     | Value                                     |
| ------------- | ----------------------------------------- |
| Sample rate   | 16,000 Hz (16 kHz)                        |
| Frame length  | 10 ms (dtUs: 10,000 µs)                   |
| Bytes / frame | 40 bytes                                  |
| Encoding      | PCM S16LE (signed 16-bit little-endian)   |
| Channels      | Mono                                      |

### Simulator vs Real Hardware Frame Sizes

| Environment     | Frame duration | Bytes per frame |
| --------------- | -------------- | --------------- |
| Real hardware   | 10 ms          | 40 bytes        |
| Simulator       | 100 ms         | 3,200 bytes     |

The simulator uses the host machine's microphone at 16 kHz, signed 16-bit LE,
but delivers frames 10× larger (100 ms per event with 3,200 bytes) compared to
real hardware (10 ms per event with 40 bytes). Your audio processing must handle
**both** frame sizes.

### Receiving Audio Data

```typescript
const bridge = await waitForEvenAppBridge();

// Ensure startup container exists
await bridge.createStartUpPageContainer(/* ... */);

// Open microphone
await bridge.audioControl(true);

const unsubscribe = bridge.onEvenHubEvent((event) => {
  if (event.audioEvent) {
    const pcm: Uint8Array = event.audioEvent.audioPcm;
    // 40 bytes per frame on real hardware, 3200 on simulator
    processAudioFrame(pcm);
  }
});

// Cleanup when done
async function stopAudio() {
  await bridge.audioControl(false);
  unsubscribe();
}
```

### Audio Data from Host

Audio data from the host application arrives in one of these formats:

- `audioPcm` as a `number[]` inside `audioEvent`
- Base64-encoded audio inside `jsonData`

The SDK normalizes both formats into a `Uint8Array` accessible via
`event.audioEvent.audioPcm`. You do not need to decode manually.

### `onMicData` Convenience Method

The bridge exposes an undocumented `onMicData` method at runtime that provides
a cleaner API for audio-only listeners. It is absent from the SDK `.d.ts` type
definitions, so you must cast through `any`:

```typescript
// Undocumented convenience wrapper — available at runtime but absent from .d.ts
// Provides cleaner API for audio-only listeners
(bridge as any).onMicData((data: { audioPcm: Uint8Array }) => {
  processFrame(data.audioPcm);
});
```

> **Note:** This is used in `g2_app/src/audio.ts` as the primary audio capture
> mechanism. Prefer `onMicData` over filtering `onEvenHubEvent` for audio-only
> use cases.

---

## 8. Device Status Monitoring

Use `onDeviceStatusChanged` to monitor the physical state of the glasses:

```typescript
const bridge = await waitForEvenAppBridge();

const unsubscribe = bridge.onDeviceStatusChanged((status) => {
  console.log('Connection:', status.connectType); // DeviceConnectType enum
  console.log('Battery:',    status.batteryLevel); // 0-100
  console.log('Wearing:',    status.isWearing);    // boolean
  console.log('Charging:',   status.isCharging);   // boolean
  console.log('In Case:',    status.isInCase);     // boolean
});

// Later, to stop monitoring:
// unsubscribe();
```

### Status fields

| Field          | Type                 | Description                            |
| -------------- | -------------------- | -------------------------------------- |
| `connectType`  | `DeviceConnectType`  | Current connection state               |
| `batteryLevel` | `number`             | Battery percentage (0–100)             |
| `isWearing`    | `boolean`            | Whether glasses are on the user's face |
| `isCharging`   | `boolean`            | Whether the glasses are charging       |
| `isInCase`     | `boolean`            | Whether the glasses are in their case  |

### `DeviceConnectType` Enum Values

| Value               | String               | Description                    |
| ------------------- | -------------------- | ------------------------------ |
| `None`              | `'none'`             | No connection state            |
| `Connecting`        | `'connecting'`       | Connection in progress         |
| `Connected`         | `'connected'`        | Successfully connected         |
| `Disconnected`      | `'disconnected'`     | Connection lost                |
| `ConnectionFailed`  | `'connectionFailed'` | Connection attempt failed      |

### SIMULATOR WARNING

`onDeviceStatusChanged` **NEVER fires** in the simulator. Device status values
are hardcoded and static. You must test device status handling on real hardware.

---

## 9. Foreground / Background Lifecycle

Three system events manage app lifecycle:

| Event                    | Value | Meaning                                 |
| ------------------------ | ----- | --------------------------------------- |
| `FOREGROUND_ENTER_EVENT` | 4     | App comes to foreground (user switched to it) |
| `FOREGROUND_EXIT_EVENT`  | 5     | App goes to background (user switched away)   |
| `ABNORMAL_EXIT_EVENT`    | 6     | Unexpected disconnect or crash                |

Use these events to pause and resume expensive operations:

```typescript
bridge.onEvenHubEvent((event) => {
  const eventType =
    event.sysEvent?.eventType ??
    event.textEvent?.eventType ??
    event.listEvent?.eventType;

  switch (eventType) {
    case OsEventTypeList.FOREGROUND_ENTER_EVENT:
      console.log('App entered foreground');
      resumeTimers();
      resumeAudioIfNeeded();
      break;

    case OsEventTypeList.FOREGROUND_EXIT_EVENT:
      console.log('App moved to background');
      pauseTimers();
      pauseAudio();
      break;

    case OsEventTypeList.ABNORMAL_EXIT_EVENT:
      console.log('Abnormal exit — cleaning up');
      emergencyCleanup();
      bridge.shutDownPageContainer(0);  // Note: SDK class name is "ShutDownContaniner" (typo)
      break;
  }
});
```

**Best practice:** Always stop the microphone (`audioControl(false)`) and clear
intervals/timeouts on `FOREGROUND_EXIT_EVENT`. Restart them on
`FOREGROUND_ENTER_EVENT`.

---

## 10. Input Handling Best Practices

1. **Guard against empty/malformed events BEFORE checking `eventType`:**
   ```typescript
   // CRITICAL: Guard against empty/malformed events BEFORE checking eventType
   const hasEvent = event.textEvent || event.listEvent || event.sysEvent;
   if (!hasEvent) return; // Skip — not a user input event

   // Only THEN check for click (safe because we know a sub-event exists)
   const et = event.textEvent?.eventType ?? event.listEvent?.eventType ?? event.sysEvent?.eventType;
   if (et === OsEventTypeList.CLICK_EVENT || et === undefined) { /* ... */ }
   ```
   Without this guard, empty events would be misclassified as clicks due to Quirk 1 (`CLICK_EVENT = 0 → undefined`).
2. **Handle clicks from ALL event sources** — never assume one channel:
   ```typescript
   const et = event.listEvent?.eventType ?? event.textEvent?.eventType ?? event.sysEvent?.eventType;
   ```
3. **Account for CLICK_EVENT = 0 → undefined:** `if (et === OsEventTypeList.CLICK_EVENT || et === undefined)`
4. **Throttle scrolls at 300ms** to prevent duplicate actions from rapid gestures.
5. **Track selected index yourself** — `currentSelectItemIndex` may be missing at index 0.
6. **Test on real hardware** — simulator event sources and frame sizes differ significantly.
7. **Use `shutDownPageContainer(1)`** for graceful exit with user confirmation.
8. **Pair image containers with hidden text** — images lack `isEventCapture`; use a full-screen text container (`content: ' '`, `isEventCapture: 1`) behind the image.

---

## 11. Tap-to-Toggle Pattern

The G2 SDK provides **no hold/release events** — only `CLICK_EVENT` (single tap)
and `DOUBLE_CLICK_EVENT` (double tap). This means microphone control cannot
use a push-to-talk model. Instead, the system uses **tap-to-start / tap-to-stop**
(walkie-talkie model):

1. **First tap** → start recording (open mic, begin streaming PCM)
2. **Second tap** → stop recording (close mic, send audio for processing)

### 300ms Tap Debounce

A debounce window of **300 ms** after each tap prevents accidental
double-tap from being interpreted as start-then-immediately-stop:

```typescript
let lastTapTime = 0;

function handleTap() {
  const now = Date.now();
  if (now - lastTapTime < 300) return; // Debounce — ignore rapid re-tap
  lastTapTime = now;

  if (isRecording) {
    stopRecording();
  } else {
    startRecording();
  }
}
```

### 30s Server-Side Silence Timeout

The gateway enforces a **30-second silence timeout** for orphaned recordings.
If the user starts recording but never taps again to stop, the server
automatically closes the audio session after 30 s of inactivity.

> **Reference:** See `docs/decisions/002-tap-to-toggle.md` for the full
> architectural decision record.

---

## 12. Compatible Event Data Formats

The SDK normalizes multiple host payload shapes into `EvenHubEvent`. For debugging:
`{ type: 'listEvent', jsonData: {...} }`, `{ type: 'list_event', data: {...} }`,
or `['list_event', {...}]`. Audio: `{ type: 'audioEvent', jsonData: { audioPcm: [...] } }`.

---

## Quick Reference Card

```
INPUT SOURCES:       G2 temple touch, R1 ring (both produce same events)
HARDWARE LIMITS:     No camera, no speaker on glasses

EVENT VALUES:        CLICK=0  SCROLL_TOP=1  SCROLL_BOTTOM=2  DOUBLE_CLICK=3
                     FG_ENTER=4  FG_EXIT=5  ABNORMAL=6

SUBSCRIBE:           const unsub = bridge.onEvenHubEvent(cb)
UNSUBSCRIBE:         unsub()

ROUTING:             isEventCapture: 1 → that container gets events
                     Image containers: pair with text/list for events

MUST-HANDLE QUIRKS:  eventType 0 → undefined (SDK bug)
                     index 0 → missing currentSelectItemIndex
                     Simulator sends sysEvent, hardware sends list/textEvent
                     Throttle scroll at 300ms
                     textContainerUpgrade flashes in simulator only

AUDIO:               bridge.audioControl(true/false)
                     PCM: 16kHz, S16LE, mono, 40 bytes/frame (3200 in sim)
                     Requires createStartUpPageContainer first

DEVICE STATUS:       bridge.onDeviceStatusChanged(cb)
                     Battery, wearing, charging, inCase, connectType
                     NEVER fires in simulator

LIFECYCLE:           FG_ENTER → resume, FG_EXIT → pause, ABNORMAL → cleanup
```

---

## Cross-References

- `g2_app/src/input.ts` — Input handler implementation
- `g2_app/src/audio.ts` — Audio capture implementation
- `g2_app/src/state.ts` — State machine
- `docs/decisions/002-tap-to-toggle.md` — Tap-to-toggle ADR
- `docs/implementation/phase-2-audio-pipeline.md` — Audio pipeline implementation plan
- `docs/archive/spikes/phase0-sdk-findings.md` — SDK spike findings
- `docs/design/g2-app.md` — G2 app design (event×state dispatch table in §7)
- `docs/reference/g2-platform/evenhub_sdk.md` — Full SDK reference
