---
name: g2-sdk-bridge
description: Integrate and verify the Even Realities EvenHub JavaScript bridge in G2 OpenClaw. Use for @evenrealities/even_hub_sdk setup, bridge lifecycle, page-container calls, audio or device APIs, storage, SDK upgrades, or questions about which APIs and types actually exist.
---

# G2 SDK bridge

Build against the repository's installed SDK contract, not remembered examples or stale reference prose.

## Establish the contract first

1. Read `g2_app/package.json`, `g2_app/package-lock.json`, and `g2_app/app.json`.
2. Run `npm list @evenrealities/even_hub_sdk` from `g2_app/` when dependencies are installed.
3. Inspect `g2_app/node_modules/@evenrealities/even_hub_sdk/dist/index.d.ts` for exact signatures and field names.
4. Consult current official [Even Hub documentation](https://hub.evenrealities.com/docs) and release notes for the installed version.
5. Keep `app.json.min_sdk_version` aligned with the first SDK version required by the code.

The current lockfile resolves SDK `0.0.11`; the manifest also declares `0.0.11`. Upstream `0.0.12` has been audited, but do not use its additions until an explicit dependency and manifest migration is requested. SDK `0.0.12` adds `zOrderIndex` and page z-order validation; most other APIs described below already exist in `0.0.11`.

Use evidence in this order: installed TypeScript declarations, official docs and changelog for that version, physical-hardware behavior with recorded Even App/firmware versions, matching simulator behavior, then repository examples and `docs/reference/g2-platform/`. Never cast through `any` merely because old code or documentation does.

## Use the correct architecture

- Run the TypeScript app in the phone's Even App WebView; no application JavaScript runs on the glasses.
- Treat the glasses as a Bluetooth-connected display and input peripheral.
- Use browser `fetch`, WebSocket, and storage APIs for application networking and state.
- Use `EvenAppBridge` for Even-host and glasses capabilities.
- Keep credentials on the local gateway or another backend; packaged client assets are extractable.

## Initialize and render correctly

```ts
import { waitForEvenAppBridge } from '@evenrealities/even_hub_sdk'

const bridge = await waitForEvenAppBridge()
```

- Do not construct bridge instances. Use `EvenAppBridge.getInstance()` only after readiness is guaranteed.
- Call `createStartUpPageContainer(...)` exactly once and check `StartUpPageCreateResult.success`.
- Use `textContainerUpgrade(...)` for text-only changes.
- Use `rebuildPageContainer(...)` when layout or container types change.
- Create image placeholders first, then call `updateImageRawData(...)` sequentially.
- Exit a root page with `shutDownPageContainer(1)` so the system confirmation appears. Use mode `0` only after an internal confirmation.

SDK `0.0.11` bridge methods include:

- Page/display: `createStartUpPageContainer`, `rebuildPageContainer`, `textContainerUpgrade`, `updateImageRawData`, `shutDownPageContainer`.
- Events/device: `onEvenHubEvent`, `onDeviceStatusChanged`, `onLaunchSource`, `getDeviceInfo`, `getUserInfo`.
- Audio/IMU: `audioControl`, `imuControl`.
- Phone capabilities: app location and continuous updates, album selection, and camera capture.
- Host storage: `setLocalStorage` and `getLocalStorage`.

Do not invent `setLayout`, `setPageFlip`, `sendData`, `setNotification`, `ContainerData`, key-down/key-up events, or an `onMicData` helper. They are absent from the locked declarations.

## Use declaration-accurate spellings

- Use `borderRadius` in SDK `0.0.11` and `0.0.12`; `borderRdaius` is stale guidance.
- Use the bridge method `shutDownPageContainer`. `ShutDownContaniner` is only a legacy misspelled exported model class.
- Use `audioEvent.audioPcm`, a `Uint8Array`, and `AudioInputSource` when selecting glasses or phone audio.
- Treat any current `shutDownContaniner` call hidden behind `any` as technical debt, not a supported compatibility API.

## Verify changes

- Type-check and run `g2_app` tests.
- Exercise startup-result handling and cleanup paths.
- Test layouts and event logic in a simulator compatible with the installed SDK.
- Confirm timing, permissions, background behavior, and critical interaction paths on physical G2 hardware before release.

Repository entry points: `g2_app/src/main.ts`, `g2_app/src/display.ts`, and `g2_app/src/input.ts`.

Official references: [architecture](https://hub.evenrealities.com/docs/get-started/architecture), [page lifecycle](https://hub.evenrealities.com/docs/build/page-lifecycle), [device APIs](https://hub.evenrealities.com/docs/build/device-apis), and [versioning](https://hub.evenrealities.com/docs/reference/versioning).
