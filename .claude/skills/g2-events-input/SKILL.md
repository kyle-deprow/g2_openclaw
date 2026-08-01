---
name: g2-events-input
description: Even Realities G2 input events, event routing, audio/microphone control, and device status callbacks. Use when handling R1 ring or temple gestures, implementing onEvenHubEvent listeners, debugging events that never arrive or arrive on the wrong channel, processing PCM audio frames, or managing foreground/background lifecycle.
---

# G2 Events & Input Handling

Input events, event routing, audio capture, and device status for the G2 platform: R1 ring, temple gestures, microphone PCM streaming, and lifecycle callbacks.

**Canonical reference:** `.agents/skills/g2-events-input/SKILL.md`. This file is the distilled operating summary — read the canonical file before non-trivial work in this area.

## Core rules

- **`OsEventTypeList` values:** `CLICK_EVENT=0`, `SCROLL_TOP_EVENT=1`, `SCROLL_BOTTOM_EVENT=2`, `DOUBLE_CLICK_EVENT=3`, `FOREGROUND_ENTER_EVENT=4`, `FOREGROUND_EXIT_EVENT=5`, `ABNORMAL_EXIT_EVENT=6`. Temple touch and R1 ring produce identical event types.
- **Quirk 1 — `CLICK_EVENT = 0` deserializes to `undefined`.** The SDK's `fromJson` normalizes `0` to `undefined`. Always check `eventType === OsEventTypeList.CLICK_EVENT || eventType === undefined`; never test for `CLICK_EVENT` alone.
- **Quirk 2 — `currentSelectItemIndex` may be missing at index 0** (same 0-normalization bug). Track the selected index in your own state and fall back with `?? 0` / `?? trackedIndex`.
- **Quirk 3 — simulator sends `sysEvent` for clicks; real hardware sends `listEvent`/`textEvent`.** Always read `event.listEvent?.eventType ?? event.textEvent?.eventType ?? event.sysEvent?.eventType`.
- **Quirk 4 — throttle scroll with a 300 ms cooldown** (`SCROLL_COOLDOWN = 300` in `g2_app/src/input.ts`) to prevent duplicate page changes from one gesture.
- **Quirk 5 — `textContainerUpgrade` flashes in the simulator only**; hardware updates smoothly. Do not optimize around the simulator flicker.
- **Scroll events are BOUNDARY events**, not per-gesture: `SCROLL_TOP_EVENT`/`SCROLL_BOTTOM_EVENT` fire only when firmware's internal scroll hits the top/bottom limit; intermediate scrolling is silent.
- **Subscribe via `bridge.onEvenHubEvent(cb)`** (bridge from `waitForEvenAppBridge()`); it returns an unsubscribe function. Exactly one of `listEvent` / `textEvent` / `sysEvent` / `audioEvent` / `jsonData` is populated per `EvenHubEvent`.
- **Handle bare events (no sub-object) as clicks** — some firmware versions deliver taps with no sub-event populated; discarding them silently drops taps.
- **Routing follows `isEventCapture: 1`** — only one container should have it. List capture: firmware moves the highlight natively; clicks arrive with `currentSelectItemName`/`currentSelectItemIndex`. Text capture: firmware scrolls internally. Image containers cannot capture — pair with a hidden full-screen text container (`content: ' '`).
- **No hold/release events exist** — only tap and double-tap. Mic control is tap-to-start / tap-to-stop (walkie-talkie), never push-to-talk. Tap debounce is unnecessary — firmware distinguishes single vs double tap natively.
- **Audio control:** `await bridge.audioControl(true|false)`. PREREQUISITE: `createStartUpPageContainer` must have been called first.
- **PCM format:** 16,000 Hz, S16LE, mono. Real hardware: 10 ms / 40-byte frames. Simulator: 100 ms / 3,200-byte frames — processing must handle both. PCM arrives as `event.audioEvent.audioPcm` (`Uint8Array`, SDK normalizes number[]/base64).
- **`onMicData` is an undocumented runtime method** absent from `.d.ts` — access via `(bridge as any).onMicData(cb)`. In this project audio is delegated to the gateway instead (the app sends `start_audio`/`stop_audio` control frames over WebSocket).
- **Recording limits are server-side:** gateway auto-stops after 90 s (`_MAX_RECORDING_SECONDS` in `gateway/server.py`); audio buffer caps at 60 s (`AudioBuffer.MAX_DURATION_SECONDS` in `gateway/audio_buffer.py`).
- **`onDeviceStatusChanged` NEVER fires in the simulator** — status values are hardcoded. Test battery/wearing/charging/in-case handling on real hardware only. `DeviceConnectType`: `None`/`Connecting`/`Connected`/`Disconnected`/`ConnectionFailed`.
- **Lifecycle:** pause timers and stop the mic (`audioControl(false)`) on `FOREGROUND_EXIT_EVENT`; resume on `FOREGROUND_ENTER_EVENT`; clean up and `shutDownPageContainer(0)` on `ABNORMAL_EXIT_EVENT`. Use `shutDownPageContainer(1)` for graceful user-confirmed exit.
- **Boot lands on the session menu** (`menu` state), not idle — the session picker is the first screen. Double-tap in `idle` opens the menu; the last item is always "+ New Session".
- **Rejecting a transcription** (tap during `confirming`) fully removes the last user message via `splice()` (`removeLastUser()` in `conversation.ts`) — no marking or prefix — then refreshes with `formatReverse()`.
- **Hardware limits:** the glasses have a microphone but no camera and no speaker — audio output on the glasses is impossible.

## This repo

- **`g2_app/src/input.ts`** — `InputHandler`: event dispatch, `SCROLL_COOLDOWN = 300`, tap-to-toggle, `start_audio`/`stop_audio` frames to the gateway.
- **`g2_app/src/state.ts`** — 10-state machine (LOADING → MENU → IDLE → RECORDING → TRANSCRIBING → CONFIRMING → THINKING → STREAMING → ...).
- **`g2_app/src/conversation.ts`** — `removeLastUser()` and `formatReverse()`.
- **`gateway/server.py`** (90 s recording cap) and **`gateway/audio_buffer.py`** (60 s buffer cap) — gateway owns audio buffering and STT.
- **`docs/reference/g2-platform/evenhub_sdk.md`** — full SDK event reference.

## Repo policy overrides

- **Ignore canonical cross-references to `docs/decisions/002-tap-to-toggle.md`, `docs/implementation/`, `docs/design/g2-app.md`, and `docs/archive/`** — none of those paths exist in this repo. Reference docs live only under `docs/reference/g2-platform/`.
