---
name: g2-events-input
description: Implement G2 OpenClaw touch/ring input, microphone, IMU, device-status, and lifecycle handling with the installed SDK. Use for onEvenHubEvent, OsEventTypeList, event capture, audio PCM, source distinctions, app-state routing, cleanup, or background recovery.
---

# G2 events and input

Use one typed event boundary and route by payload, source, and application state.

## Subscribe and clean up

```ts
import { OsEventTypeList } from '@evenrealities/even_hub_sdk'

const unsubscribe = bridge.onEvenHubEvent(event => {
  const eventType =
    event.textEvent?.eventType ??
    event.listEvent?.eventType ??
    event.sysEvent?.eventType

  if (eventType === OsEventTypeList.CLICK_EVENT || eventType === undefined) {
    // Handle press. Protobuf zero/default values can normalize to undefined.
  }
})
```

Retain every unsubscribe function. Stop audio, IMU, location updates, timers, and sockets when their owning feature is torn down.

## Route the locked SDK 0.0.11 event contract

SDK `0.0.11` defines:

- `CLICK_EVENT = 0`
- `SCROLL_TOP_EVENT = 1`
- `SCROLL_BOTTOM_EVENT = 2`
- `DOUBLE_CLICK_EVENT = 3`
- `FOREGROUND_ENTER_EVENT = 4`
- `FOREGROUND_EXIT_EVENT = 5`
- `ABNORMAL_EXIT_EVENT = 6`
- `SYSTEM_EXIT_EVENT = 7`
- `IMU_DATA_REPORT = 8`

Event routing depends on the active capture container:

- With text capture, swipes arrive as `textEvent`, while single and double presses arrive as `sysEvent`.
- With list capture, firmware handles swipe navigation internally; a single press arrives as `listEvent`, while a double press arrives as `sysEvent`.
- Lifecycle and IMU data use `sysEvent`; microphone buffers use `audioEvent`.

- Handle `CLICK_EVENT` and `undefined` together because protobuf value zero may be omitted.
- Treat top/bottom values as firmware navigation or boundary events, not key-down/key-up events.
- Use `sysEvent.eventSource` when behavior must distinguish right temple, left temple, and R1 ring. SDK `0.0.11` exposes all three sources.
- Track list selection in app state and use `currentSelectItemIndex ?? 0` for the first item.
- Do not classify an entirely empty/malformed event as a click without documented firmware evidence. The current bare-event fallback in `g2_app/src/input.ts` is a compatibility path that needs hardware evidence before reuse.

## Preserve product routing and safe exit

G2 OpenClaw routes a press by state: start/stop recording, confirm transcription, recover errors, or reconnect. Double press rejects confirmation, cancels an active response, or recovers an error. Preserve those state-dependent behaviors unless product requirements change.

Every root interaction model must still provide a reachable system exit flow:

- Request it with `await bridge.shutDownPageContainer(1)`.
- Do not unsubscribe or stop hardware before requesting mode `1`; the user can cancel the dialog.
- Clean up after `SYSTEM_EXIT_EVENT` or `ABNORMAL_EXIT_EVENT`.
- Use mode `0` only after an internal confirmation.
- Never call the nonexistent bridge method `shutDownContaniner`; the current `any`-cast occurrence in `g2_app/src/input.ts` is technical debt.

## Capture audio correctly

```ts
import { AudioInputSource } from '@evenrealities/even_hub_sdk'

await bridge.audioControl(true, AudioInputSource.Glasses)

const unsubscribe = bridge.onEvenHubEvent(event => {
  if (!event.audioEvent) return
  const pcm = event.audioEvent.audioPcm // Uint8Array
  consumePcm(pcm, event.audioEvent.source)
})
```

- Declare `g2-microphone` for glasses audio and create the startup page before opening it.
- Treat audio as 16 kHz, signed 16-bit little-endian, mono.
- Process arbitrary chunk boundaries; do not hardcode obsolete 40-byte hardware packets.
- Copy a typed-array slice before transferring its `ArrayBuffer` when the view may not cover the whole backing buffer.
- Apply backpressure or batching when forwarding PCM to the gateway.
- Use `event.audioEvent.audioPcm`; do not add undocumented `onMicData` casts.

## Handle device streams and backgrounding

- Use `imuControl(true, ImuReportPace.*)` and accept `IMU_DATA_REPORT` through `sysEvent.imuData`; pacing values are protocol codes, not Hz.
- Use `onDeviceStatusChanged` for battery, wearing, charging, and connection state. Current simulators do not emit these changes.
- Use `onLaunchSource` if launch behavior differs between app and glasses menus.
- Persist important state eagerly and assume Android may reclaim the WebView, sockets, and streams.
- Rehydrate state and reconnect or re-arm required streams on foreground or cold launch.

## Verify interactions

- Unit-test state-dependent press and double-press behavior, zero-value normalization, and text/list capture paths.
- Test source-aware behavior for temple and ring inputs when used.
- Exercise simulator click, double-click, up, and down actions.
- Verify permissions, arbitrary audio chunks, background recovery, and exit flow on physical G2 hardware.

Official references: [device APIs](https://hub.evenrealities.com/docs/build/device-apis), [background lifecycle](https://hub.evenrealities.com/docs/build/background-lifecycle), and [page lifecycle](https://hub.evenrealities.com/docs/build/page-lifecycle).
