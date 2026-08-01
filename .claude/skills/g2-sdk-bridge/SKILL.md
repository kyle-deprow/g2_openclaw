---
name: g2-sdk-bridge
description: Even Realities G2 EvenAppBridge SDK API, data models, enums, and WebView communication protocol. Use when integrating @evenrealities/even_hub_sdk, calling bridge methods, constructing container objects, handling return types, or debugging SDK communication and bridge initialization.
---

# G2 SDK Bridge

API reference for `@evenrealities/even_hub_sdk`: the `EvenAppBridge` singleton that connects the WebView app to the Even App host and, over BLE, to the glasses.

**Canonical reference:** `.agents/skills/g2-sdk-bridge/SKILL.md`. This file is the distilled operating summary — read the canonical file before non-trivial work in this area.

## Core rules

- **Architecture:** web app runs in an iPhone WebView (`flutter_inappwebview`); the SDK injects an `EvenAppBridge` singleton on `window`. Web → glasses via `bridge.callEvenApp(method, params)`; glasses → web via `window._listenEvenAppMessage`. **No code runs on the glasses** — they are a display + input peripheral.
- **Always initialize with `await waitForEvenAppBridge()`** — it resolves when ready (immediate check → `'evenAppBridgeReady'` event → 100 ms polling fallback). `EvenAppBridge.getInstance()` exists but does not guarantee readiness.
- **`createStartUpPageContainer()` MUST be called exactly once** — a second call fails. Returns `StartUpPageCreateResult`: `success=0`, `invalid=1`, `oversize=2`, `outOfMemory=3`.
- **All subsequent page updates use `rebuildPageContainer()`** (same structure: `containerTotalNum` + `listObject`/`textObject`/`imageObject`). Full redraw — scroll and selection state lost, flicker on hardware. Prefer `textContainerUpgrade` for text-only changes.
- **`textContainerUpgrade()`** does in-place text edits via `containerID`, `containerName`, `contentOffset`, `contentLength`, `content` (max 2000 chars).
- **`updateImageRawData()` is sequential-only** — concurrent calls corrupt data; `await` each call. `imageData` accepts `number[] | string | Uint8Array | ArrayBuffer`. Check success with `result.isSuccess()`.
- **`audioControl(true|false)` requires `createStartUpPageContainer` first.** PCM: 16 kHz S16LE mono; 10 ms/40-byte frames on hardware, 100 ms/3,200-byte frames in the simulator.
- **`shutDownPageContainer(exitMode)`:** `0` = immediate (default), `1` = confirmation dialog on glasses.
- **Subscriptions return unsubscribe functions:** `onEvenHubEvent(cb)` for all glasses events; `onDeviceStatusChanged(cb)` for connection/battery/wearing (never fires in the simulator).
- **Storage methods are host-side:** `setLocalStorage`/`getLocalStorage` go through the Flutter host, NOT browser localStorage; `getLocalStorage` returns an empty string when the key is absent.
- **Hard limits:** max 4 containers total (`containerTotalNum`); `TextContainerProperty.content` max 1000 chars; list `itemCount` 1–20; `itemName` max 64 chars each; image width 20–200 px, height 20–100 px.
- **Use SDK typos verbatim:** `borderRdaius`, class `ShutDownContaniner`, enum values `APP_REQUEST_REBUILD_PAGE_FAILD` and `APP_REQUEST_CREATE_INVAILD_CONTAINER`. Corrected spellings do not exist in the SDK.
- **`CLICK_EVENT = 0` deserializes to `undefined`** — always check `eventType === OsEventTypeList.CLICK_EVENT || eventType === undefined`; the same 0-bug can drop `currentSelectItemIndex` at index 0.
- **Simulator sends `sysEvent` for interactions; hardware sends `textEvent`/`listEvent`** — handle all three paths. Scroll events are boundary-only; throttle at 300 ms.
- **`DeviceModel.fromString()` defaults to `G1`** for any unrecognized string — never `null`/`undefined`.
- **Undocumented protobuf fields need `as any` casts:** `fontSize`, `fontColor` (0–15) on containers, and `(bridge as any).onMicData(cb)` — all absent from `.d.ts`, hardware behaviour unverified.
- **All 17 data models share** `constructor(data?: Partial<T>)`, `toJson()`, `static fromJson(json)`. The SDK's `pickLoose()` accepts camelCase, PascalCase, and snake_case keys, and event payloads in three shapes (typed object, snake_case `{type, data}`, or `['list_event', {...}]` array).
- **Key enums:** `EvenAppMethod` (string method names), `DeviceConnectType` (`none`/`connecting`/`connected`/`disconnected`/`connectionFailed`), `OsEventTypeList` (`CLICK_EVENT=0` ... `ABNORMAL_EXIT_EVENT=6`), `EvenHubEventType` (`listEvent`/`textEvent`/`sysEvent`/`audioEvent`/`notSet`), `BridgeEvent.BridgeReady = 'evenAppBridgeReady'`.
- **Dist code is obfuscated** — only the `.d.ts` declarations are readable; do not attempt to read the compiled JS.
- **Pinned version in this repo:** SDK `@evenrealities/even_hub_sdk` 0.0.11 (see root AGENTS.md stack pins).

## This repo

- **`g2_app/src/main.ts`** — app bootstrap using `waitForEvenAppBridge()`.
- **`g2_app/src/display.ts`** — container construction, `fontSize`/`fontColor` casts, page lifecycle calls.
- **`g2_app/src/input.ts`** — `onEvenHubEvent` handling, CLICK-0 quirk workarounds, audio control frames.
- **`g2_app/src/conversation.ts`** and **`g2_app/src/utils.ts`** — rendering and helpers used with the SDK containers.
- **`docs/reference/g2-platform/evenhub_sdk.md`** — full SDK analysis; **`docs/reference/g2-platform/g2_reference_guide.md`** — comprehensive G2 reference.

## Repo policy overrides

- **Ignore canonical cross-references to `docs/design/g2-app.md` and `docs/archive/spikes/`** — those paths do not exist in this repo. Reference docs live only under `docs/reference/g2-platform/`.
