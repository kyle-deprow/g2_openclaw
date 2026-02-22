---
name: g2-sdk-bridge
description:
  Even Realities G2 EvenAppBridge SDK API, data models, enums, and communication protocol. Use when integrating the @evenrealities/even_hub_sdk, calling bridge methods, constructing container objects, handling return types, or debugging SDK communication. Triggers on tasks involving EvenAppBridge, SDK imports, data model classes, enum values, or bridge initialization.
---

# G2 SDK Bridge — Complete Reference

## Architecture Overview

Web apps for the G2 glasses run inside an iPhone WebView (`flutter_inappwebview`). The communication path is:

```
[Your Web App Server] <--HTTPS--> [iPhone WebView] <--BLE--> [G2 Glasses]
```

Key architectural facts:

- The SDK injects an `EvenAppBridge` singleton into the `window` object.
- The bridge uses `flutter_inappwebview.callHandler('evenAppMessage', ...)` — this is **native WebView message passing**, not HTTP.
- **Web → Glasses:** `bridge.callEvenApp(method, params)` → Flutter host receives the call → relays the command over BLE to the glasses.
- **Glasses → Web:** Glasses send BLE data → Flutter host receives it → calls `window._listenEvenAppMessage(...)` which pushes the event into the WebView.
- **No code runs on the glasses themselves.** The G2 is a display + input peripheral only. All logic lives in your web app.

The SDK (`@evenrealities/even_hub_sdk`) is a TypeScript wrapper that:
1. Manages the bridge singleton lifecycle.
2. Serializes/deserializes method calls and events.
3. Provides typed data models and enums for all container types.

## Bridge Initialization

### Singleton Pattern

`EvenAppBridge` is a singleton obtained via `getInstance()`:

```typescript
import { EvenAppBridge } from '@evenrealities/even_hub_sdk';

const bridge = EvenAppBridge.getInstance();
```

### Recommended: waitForEvenAppBridge

The recommended approach is the async helper that handles timing:

```typescript
import { waitForEvenAppBridge } from '@evenrealities/even_hub_sdk';

async function init() {
  const bridge = await waitForEvenAppBridge();
  // Bridge is guaranteed ready here
  const user = await bridge.getUserInfo();
  console.log('User:', user.name);
}
init();
```

### Initialization Flow (internal)

1. `getInstance()` creates the singleton (if not already created).
2. Exposes the instance to `window.EvenAppBridge`.
3. Listens for DOM `'DOMContentLoaded'` (or fires immediately if already loaded).
4. Exposes `window._evenAppHandleMessage` as the inbound message handler.
5. Sets internal `_ready = true`.
6. Dispatches a `CustomEvent('evenAppBridgeReady')` on `window`.

### waitForEvenAppBridge() resolution strategy

1. Checks if the bridge is already ready → resolves immediately.
2. Listens for the `'evenAppBridgeReady'` event → resolves on fire.
3. Fallback: retries every 100ms polling for readiness.

## Public Methods — Complete Reference

### 1. `static getInstance(): EvenAppBridge`

Returns the singleton bridge instance. Creates it on first call.

```typescript
const bridge = EvenAppBridge.getInstance();
```

### 2. `handleEvenAppMessage(message: string | EvenAppMessage): void`

Processes an incoming message from the Flutter host. Normally called internally by the bridge via `window._evenAppHandleMessage`. You should not need to call this directly unless simulating events in tests.

### 3. `callEvenApp(method: EvenAppMethod | string, params?: any): Promise<any>`

Generic escape hatch for calling any method on the Flutter host. All typed methods delegate to this internally.

```typescript
const result = await bridge.callEvenApp(EvenAppMethod.GetUserInfo);
```

### 4–7. Info & Storage Methods

```typescript
const user = await bridge.getUserInfo();       // → UserInfo { uid, name, avatar, country }
const device = await bridge.getDeviceInfo();   // → DeviceInfo | null
await bridge.setLocalStorage('key', 'value');  // → boolean (host-side storage, NOT browser localStorage)
const val = await bridge.getLocalStorage('key'); // → string (empty string if not found)
```

### 8. `createStartUpPageContainer(container: CreateStartUpPageContainer): Promise<StartUpPageCreateResult>`

**MUST be called exactly once.** Returns: 0=success, 1=invalid, 2=oversize, 3=outOfMemory.

```typescript
const result = await bridge.createStartUpPageContainer(new CreateStartUpPageContainer({
  containerTotalNum: 1,
  textObject: [new TextContainerProperty({
    containerID: 1, containerName: 'main', content: 'Hello G2!',
    xPosition: 0, yPosition: 0, width: 480, height: 136, isEventCapture: 1,
  })],
}));
if (result === StartUpPageCreateResult.success) { /* ready */ }
```

### 9. `rebuildPageContainer(container: RebuildPageContainer): Promise<boolean>`

**ALL subsequent page updates** use this. Same container structure as startup.

```typescript
await bridge.rebuildPageContainer(new RebuildPageContainer({ containerTotalNum: 1, textObject: [/*...*/] }));
```

### 10. `updateImageRawData(data: ImageRawDataUpdate): Promise<ImageRawDataUpdateResult>`

Sends raw image data to an image container on the glasses. **Sequential only** — do not call concurrently; wait for each promise to resolve before sending the next.

```typescript
import { ImageRawDataUpdate } from '@evenrealities/even_hub_sdk';

const update = new ImageRawDataUpdate({
  containerID: 2,
  containerName: 'icon',
  imageData: myUint8Array, // number[] | string | Uint8Array | ArrayBuffer
});

const imgResult = await bridge.updateImageRawData(update);
if (imgResult.isSuccess()) {
  console.log('Image sent');
}
```

### 11. `textContainerUpgrade(container: TextContainerUpgrade): Promise<boolean>`

In-place text update for an existing text container. More efficient than `rebuildPageContainer` for text-only changes.

```typescript
import { TextContainerUpgrade } from '@evenrealities/even_hub_sdk';

const upgrade = new TextContainerUpgrade({
  containerID: 1,
  containerName: 'main_text',
  contentOffset: 0,
  contentLength: 12,
  content: 'New text here',
});

await bridge.textContainerUpgrade(upgrade);
```

### 12. `audioControl(isOpen: boolean): Promise<boolean>`

Toggles audio streaming from the glasses microphone. **Requires `createStartUpPageContainer` to have been called first.**

```typescript
await bridge.audioControl(true);  // start
// ... later
await bridge.audioControl(false); // stop
```

### 13. `shutDownPageContainer(exitMode?: number): Promise<boolean>`

Shuts down the display container on the glasses.

- `0` — immediate shutdown (default)
- `1` — shows user confirmation dialog on glasses first

```typescript
await bridge.shutDownPageContainer(0);
```

### 14. `onDeviceStatusChanged(callback: (status: DeviceStatus) => void): () => void`

Subscribes to device status changes (connection, battery, wearing state). Returns an **unsubscribe function**.

```typescript
const unsub = bridge.onDeviceStatusChanged((status) => {
  console.log(`Connected: ${status.isConnected()}, Battery: ${status.batteryLevel}%`);
});

// Later: clean up
unsub();
```

### 15. `onEvenHubEvent(callback: (event: EvenHubEvent) => void): () => void`

Subscribes to all events from the glasses (list selection, text events, system events, audio). Returns an **unsubscribe function**.

```typescript
const unsub = bridge.onEvenHubEvent((event) => {
  if (event.listEvent) {
    console.log(`Selected: ${event.listEvent.currentSelectItemName}`);
  }
  if (event.audioEvent) {
    const pcm: Uint8Array = event.audioEvent.audioPcm;
    // process audio data
  }
});

unsub();
```

## Data Models — Complete Reference

All 17 data model classes share these common patterns:
- `constructor(data?: Partial<T>)` — partial construction with defaults
- `toJson(): JsonRecord` — instance serialization
- `static fromJson(json: any): T` — deserialization
- `static toJson(model?: T): JsonRecord | undefined` — static serialization helper

### UserInfo

| Property  | Type     | Notes                 |
|-----------|----------|-----------------------|
| `uid`     | `number` | User ID               |
| `name`    | `string` | Display name          |
| `avatar`  | `string` | Avatar URL            |
| `country` | `string` | Country code          |

### DeviceInfo

| Property | Type           | Notes                                              |
|----------|----------------|----------------------------------------------------|
| `model`  | `DeviceModel`  | **readonly** — G1, G2, or Ring1                    |
| `sn`     | `string`       | **readonly** — serial number                       |
| `status` | `DeviceStatus` | Current status                                     |

Methods:
- `updateStatus(status: DeviceStatus): void` — updates status **only when `status.sn` matches `this.sn`**.
- `isGlasses(): boolean` — true for G1/G2 models.
- `isRing(): boolean` — true for Ring1.

### DeviceStatus

| Property        | Type                | Notes            |
|-----------------|---------------------|------------------|
| `sn`            | `string`            | **readonly**     |
| `connectType`   | `DeviceConnectType` | Connection state |
| `isWearing`     | `boolean?`          | Optional         |
| `batteryLevel`  | `number?`           | 0-100            |
| `isCharging`    | `boolean?`          | Optional         |
| `isInCase`      | `boolean?`          | Optional         |

Methods:
- `isConnected(): boolean`
- `isConnecting(): boolean`
- `isDisconnected(): boolean`
- `isConnectionFailed(): boolean`
- `isNone(): boolean`

### ListContainerProperty

All layout positioning properties (`xPosition`, `yPosition`, `width`, `height`, `containerID`, `containerName`, etc.) plus:

| Property          | Type                        | Notes             |
|-------------------|-----------------------------|-------------------|
| `itemContainer`   | `ListItemContainerProperty` | Child item config |
| `isEventCapture`  | `number`                    | 0 or 1            |

Layout properties include: `borderRdaius` (note: typo is intentional — from protobuf), `borderColor`, `backgroundColor`, `fontColor`, `fontSize`, etc.

### ListItemContainerProperty

| Property                | Type       | Notes                             |
|-------------------------|------------|-----------------------------------|
| `itemCount`             | `number`   | 1–20                              |
| `itemWidth`             | `number`   | 0 = auto width                    |
| `isItemSelectBorderEn`  | `number`   | 0 or 1                            |
| `itemName`              | `string[]` | Max 64 chars per item name        |

### TextContainerProperty

All layout positioning properties plus:

| Property         | Type     | Notes                  |
|------------------|----------|------------------------|
| `content`        | `string` | Max 1000 chars         |
| `isEventCapture` | `number` | 0 or 1                 |

### TextContainerUpgrade

| Property        | Type     | Notes          |
|-----------------|----------|----------------|
| `containerID`   | `number` |                |
| `containerName` | `string` |                |
| `contentOffset` | `number` | Start position |
| `contentLength` | `number` | Replace length |
| `content`       | `string` | Max 2000 chars |

### ImageContainerProperty

| Property        | Type     | Notes            |
|-----------------|----------|------------------|
| `xPosition`     | `number` |                  |
| `yPosition`     | `number` |                  |
| `width`         | `number` | 20–200           |
| `height`        | `number` | 20–100           |
| `containerID`   | `number` |                  |
| `containerName` | `string` |                  |

### ImageRawDataUpdate

| Property        | Type                                            | Notes                  |
|-----------------|-------------------------------------------------|------------------------|
| `containerID`   | `number`                                        |                        |
| `containerName` | `string`                                        |                        |
| `imageData`     | `number[] \| string \| Uint8Array \| ArrayBuffer` | Raw image bytes     |

### ImageRawDataUpdateFields (low-level / reserved)

Fields: `mapSessionId`, `mapTotalSize`, `compressMode`, `mapFragmentIndex`, `mapFragmentPacketSize`, `mapRawData`

### CreateStartUpPageContainer / RebuildPageContainer

Both share the same structure:

| Property            | Type                         | Notes                   |
|---------------------|------------------------------|-------------------------|
| `containerTotalNum` | `number`                     | Max 4 containers total  |
| `listObject`        | `ListContainerProperty[]?`   | Optional                |
| `textObject`        | `TextContainerProperty[]?`   | Optional                |
| `imageObject`       | `ImageContainerProperty[]?`  | Optional                |

### ShutDownContaniner

> **Note:** The class name `ShutDownContaniner` is a typo in the SDK for "ShutDownContainer". Use the typo as-is.

| Property   | Type     | Notes                         |
|------------|----------|-------------------------------|
| `exitMode` | `number` | 0 = immediate, 1 = confirm    |

### Event Models

- `List_ItemEvent` — `containerID`, `containerName`, `currentSelectItemName`, `currentSelectItemIndex`, `eventType`
- `Text_ItemEvent` — `containerID`, `containerName`, `eventType`
- `Sys_ItemEvent` — `eventType` only

## Enums — Complete Reference

### EvenAppMethod

```typescript
enum EvenAppMethod {
  GetUserInfo = 'getUserInfo',
  GetGlassesInfo = 'getGlassesInfo',
  SetLocalStorage = 'setLocalStorage',
  GetLocalStorage = 'getLocalStorage',
  CreateStartUpPageContainer = 'createStartUpPageContainer',
  RebuildPageContainer = 'rebuildPageContainer',
  UpdateImageRawData = 'updateImageRawData',
  TextContainerUpgrade = 'textContainerUpgrade',
  AudioControl = 'audioControl',
  ShutDownPageContainer = 'shutDownPageContainer',
}
```

### EvenAppMessageType

```typescript
enum EvenAppMessageType {
  CallEvenAppMethod = 'call_even_app_method',
  ListenEvenAppNotify = 'listen_even_app_data',
}
```

### BridgeEvent

```typescript
enum BridgeEvent {
  BridgeReady = 'evenAppBridgeReady',
  DeviceStatusChanged = 'deviceStatusChanged',
  EvenHubEvent = 'evenHubEvent',
}
```

### DeviceConnectType

```typescript
enum DeviceConnectType {
  None = 'none',
  Connecting = 'connecting',
  Connected = 'connected',
  Disconnected = 'disconnected',
  ConnectionFailed = 'connectionFailed',
}
```

Helper: `DeviceConnectType.fromString(value: string): DeviceConnectType`

### DeviceModel

```typescript
enum DeviceModel {
  G1 = 'g1',
  G2 = 'g2',
  Ring1 = 'ring1',
}
```

Helpers:
- `DeviceModel.fromString(value: string): DeviceModel` — **defaults to `G1` for unknown strings!**
- `DeviceModel.isGlasses(model: DeviceModel): boolean`
- `DeviceModel.isRing(model: DeviceModel): boolean`

### StartUpPageCreateResult

```typescript
enum StartUpPageCreateResult {
  success = 0,
  invalid = 1,
  oversize = 2,
  outOfMemory = 3,
}
```

Helpers: `fromInt(value: number)`, `normalize(value: any)`

### ImageRawDataUpdateResult

Values: `success`, `imageException`, `imageSizeInvalid`, `imageToGray4Failed`, `sendFailed`.  
Helpers: `valueCode()`, `fromInt(value)`, `normalize(value)`, `isSuccess()`

### EvenHubErrorCodeName

All `APP_REQUEST_*` codes. Notable typos: `INVAILD` (for INVALID), `FAILD` (for FAILED). Covers create, rebuild, shutdown, text upgrade, image update, and audio control success/failure codes.

### EvenHubEventType

Values: `listEvent`, `textEvent`, `sysEvent`, `audioEvent`, `notSet`

### OsEventTypeList

Values: `CLICK_EVENT=0`, `SCROLL_TOP_EVENT=1`, `SCROLL_BOTTOM_EVENT=2`, `DOUBLE_CLICK_EVENT=3`, `FOREGROUND_ENTER_EVENT=4`, `FOREGROUND_EXIT_EVENT=5`, `ABNORMAL_EXIT_EVENT=6`.  
Helper: `OsEventTypeList.fromJson(value): OsEventTypeList`

## Type Aliases

```typescript
/** Union event object from glasses — at most one field is populated */
type EvenHubEvent = {
  listEvent?: List_ItemEvent;
  textEvent?: Text_ItemEvent;
  sysEvent?: Sys_ItemEvent;
  audioEvent?: AudioEventPayload;
  jsonData?: any;
};

/** Raw audio PCM data from glasses microphone */
type AudioEventPayload = {
  audioPcm: Uint8Array;
};

/** Union of all possible event payloads */
type EvenHubEventPayload = List_ItemEvent | Text_ItemEvent | Sys_ItemEvent | AudioEventPayload;

/** Error code from the hub — may arrive as enum, number, or string */
type EvenHubErrorCode = EvenHubErrorCodeName | number | string;

/** Generic JSON record */
type JsonRecord = Record<string, any>;
```

## JSON Compatibility

The SDK handles multiple key casing conventions transparently via `pickLoose()`:

- **camelCase:** `containerName`
- **PascalCase:** `ContainerName`
- **Proto-style / snake_case:** `container_name`

`pickLoose()` normalizes keys by removing underscores and lowercasing before matching.

Event data from the Flutter host may arrive in several shapes. The SDK accepts all of these:

```typescript
// Shape 1: typed object
{ type: 'listEvent', jsonData: { containerID: 1, currentSelectItemName: 'Option A' } }

// Shape 2: snake_case type with data key
{ type: 'list_event', data: { containerID: 1, currentSelectItemName: 'Option A' } }

// Shape 3: array format
['list_event', { containerID: 1, currentSelectItemName: 'Option A' }]
```

## Utility Functions (13 exported)

- `waitForEvenAppBridge()` — async bridge init helper
- `createDefaultEvenHubEvent()` / `evenHubEventFromJson(json)` — event construction/parsing
- `isObjectRecord(v)` — type guard for plain objects
- `pick(obj, key)` / `pickLoose(obj, key)` — exact and case-insensitive key lookup
- `normalizeLooseKey(key)` — strip underscores + lowercase
- `toNumber(v)` / `toString(v)` — safe coercion
- `readNumber(obj, key)` / `readString(obj, key)` — loose-pick + coerce
- `toObjectRecord(v)` / `bytesToJson(bytes)` — object coercion and UTF-8 decode

## SDK Known Typos & Gotchas

| Issue | Detail |
|---|---|
| `borderRdaius` | Typo from protobuf definition for `borderRadius`. Use the typo in code. |
| `ShutDownContaniner` | Class name typo for `ShutDownContainer`. Import and use as `ShutDownContaniner`. |
| `APP_REQUEST_REBUILD_PAGE_FAILD` | Enum typo for `FAILED`. Match the typo when comparing error codes. |
| `APP_REQUEST_CREATE_INVAILD_CONTAINER` | Enum typo for `INVALID`. Match the typo when comparing error codes. |
| `DeviceModel.fromString()` defaults to G1 | Any unrecognized string maps to `DeviceModel.G1` — not `null` or `undefined`. |
| Dist code is obfuscated | Only `.d.ts` type declarations are human-readable. Do not attempt to read compiled JS. |
| `updateImageRawData` is sequential | Concurrent calls cause data corruption. Always `await` each call. |
| `createStartUpPageContainer` once only | Calling it a second time will fail. Use `rebuildPageContainer` for updates. |
| `audioControl` requires startup | Must call `createStartUpPageContainer` before `audioControl(true)`. |
| `containerTotalNum` max is 4 | Cannot exceed 4 containers across list + text + image combined. |
| `ListItemContainerProperty.itemCount` range | Must be 1–20. |
| `TextContainerProperty.content` limit | Max 1000 characters. |
| `TextContainerUpgrade.content` limit | Max 2000 characters. |
| `ImageContainerProperty.width` range | 20–200 pixels. |
| `ImageContainerProperty.height` range | 20–100 pixels. |
| `ListItemContainerProperty.itemName` char limit | Max 64 characters per item name. |

## Complete Import Reference

```typescript
// Core bridge
import {
  EvenAppBridge,
  waitForEvenAppBridge,
} from '@evenrealities/even_hub_sdk';

// Data models
import {
  UserInfo,
  DeviceInfo,
  DeviceStatus,
  ListContainerProperty,
  ListItemContainerProperty,
  TextContainerProperty,
  TextContainerUpgrade,
  ImageContainerProperty,
  ImageRawDataUpdate,
  ImageRawDataUpdateFields,
  CreateStartUpPageContainer,
  RebuildPageContainer,
  ShutDownContaniner,
  List_ItemEvent,
  Text_ItemEvent,
  Sys_ItemEvent,
} from '@evenrealities/even_hub_sdk';

// Enums
import {
  EvenAppMethod,
  EvenAppMessageType,
  BridgeEvent,
  DeviceConnectType,
  DeviceModel,
  StartUpPageCreateResult,
  ImageRawDataUpdateResult,
  EvenHubErrorCodeName,
  EvenHubEventType,
  OsEventTypeList,
} from '@evenrealities/even_hub_sdk';

// Type aliases
import type {
  EvenHubEvent,
  AudioEventPayload,
  EvenHubEventPayload,
  EvenHubErrorCode,
  JsonRecord,
} from '@evenrealities/even_hub_sdk';

// Utility functions
import {
  createDefaultEvenHubEvent,
  evenHubEventFromJson,
  isObjectRecord,
  pick,
  pickLoose,
  normalizeLooseKey,
  toNumber,
  toString,
  readNumber,
  readString,
  toObjectRecord,
  bytesToJson,
} from '@evenrealities/even_hub_sdk';
```
