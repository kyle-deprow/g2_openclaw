# Even Realities EvenHub SDK — Comprehensive Developer Notes

> **Source:** [`@evenrealities/even_hub_sdk`](https://www.npmjs.com/package/@evenrealities/even_hub_sdk)
> **Fetched:** 2026-02-21
> **NPM Version at time of analysis:** 0.0.7 (published ~11 days prior)
> **README states "Current Version":** 0.0.6 (slightly behind — the NPM sidebar shows 0.0.7)

---

## 1. Package Metadata

| Field | Value |
|---|---|
| **Package name** | `@evenrealities/even_hub_sdk` |
| **Version** | `0.0.7` |
| **Description** | TypeScript SDK for WebView developers to communicate with Even App |
| **License** | MIT |
| **Author** | Whiskee (`whiskee.chen@evenrealities.com`) |
| **Collaborators** | `whiskee.chen`, `carson.zhu` |
| **Unpacked Size** | 347 kB |
| **Total Files** | 8 |
| **Module type** | ESM (`"type": "module"`) |
| **Main (CJS)** | `dist/index.cjs` |
| **Module (ESM)** | `dist/index.js` |
| **Types** | `dist/index.d.ts` |
| **Side Effects** | `false` |
| **Node.js Requirement** | `^20.0.0 \|\| >=22.0.0` |
| **TypeScript Support** | Full — built-in `.d.ts` type declarations |
| **Repository** | Not listed in package.json (no public GitHub repo linked) |
| **Published files** | `dist/`, `README.md`, `README.zh-CN.md`, `LICENSE` |

---

## 2. Installation

```bash
# npm
npm install @evenrealities/even_hub_sdk

# yarn
yarn add @evenrealities/even_hub_sdk

# pnpm
pnpm add @evenrealities/even_hub_sdk
```

---

## 3. Dependencies

### Runtime Dependencies

**None.** The package has 0 runtime dependencies.

### Dev Dependencies (build-time only)

| Package | Version |
|---|---|
| `javascript-obfuscator` | `^4.1.0` |
| `rimraf` | `^6.0.1` |
| `tsup` | `^8.3.6` |
| `typescript` | `^5.7.3` |
| `vitest` | `^3.0.2` |

### Peer Dependencies

**None listed.**

> **Note:** The dist output is obfuscated with `javascript-obfuscator` (compact, control-flow-flattening, dead-code-injection, string-array with base64 encoding). The `.d.ts` files are **not** obfuscated.

---

## 4. Architecture Overview

### What the SDK Does

`@evenrealities/even_hub_sdk` is a **TypeScript bridge SDK** for building web applications that run inside the **Even App's WebView**. It enables bidirectional communication between:

- **Web pages** (your code, running in WebView)
- **Even App** (the native host application on mobile — built with Flutter/Dart)

The SDK communicates with the host app via `flutter_inappwebview`'s `callHandler` mechanism. The host app registers a JavaScript handler named `'evenAppMessage'`, and the SDK posts messages to it. The host can push notifications back to the web via `window._listenEvenAppMessage(...)` or `window._evenAppHandleMessage(...)`.

### Key Capabilities

| Capability | Description |
|---|---|
| **Unified Bridge** | `EvenAppBridge` singleton for type-safe Web ↔ App communication |
| **Device Management** | Get device info, monitor connection status, battery level, wearing status |
| **User Information** | Retrieve current logged-in user info |
| **Local Storage** | Key-value storage persisted on the App side |
| **EvenHub Protocol** | Full support for core EvenHub protocol calls (JSON field mapping to protobuf-aligned models) |
| **Event Listening** | Real-time push notifications for device status changes and EvenHub events |
| **Audio Control** | Microphone open/close and PCM audio data streaming |
| **Display/UI** | Create and manage containers (list, text, image) on the glasses display |
| **Type Safety** | Complete TypeScript type definitions |
| **Auto Initialization** | SDK automatically initializes the bridge; no manual configuration needed |

### Communication Flow

```
┌──────────────┐    flutter_inappwebview    ┌──────────────┐     BLE/Protocol     ┌──────────────┐
│   Web Page   │ ◄─────────────────────────► │   Even App   │ ◄──────────────────► │   Glasses    │
│  (WebView)   │    callHandler /           │  (Flutter)   │                      │  (G1/G2)     │
│              │    _evenAppHandleMessage    │              │                      │              │
└──────────────┘                            └──────────────┘                      └──────────────┘
        │                                          │
        │  EvenAppBridge.postMessage()             │  Host processes + forwards
        │  ────────────────────────────►           │  to glasses via BLE
        │                                          │
        │  window._listenEvenAppMessage()          │  Glasses events pushed back
        │  ◄────────────────────────────           │  via host
```

### Message Format

All communication uses `EvenAppMessage`:

```typescript
interface EvenAppMessage {
  type: string;    // 'call_even_app_method' | 'listen_even_app_data'
  method: string;  // e.g. 'getUserInfo', 'deviceStatusChanged', 'evenHubEvent'
  data?: any;      // message payload
  payload?: any;   // backward compat alias for data
}
```

### Coordinate System

The glasses canvas uses a coordinate system with:
- **Origin (0, 0):** Top-left corner
- **X-axis:** Extends rightward (positive values increase to the right)
- **Y-axis:** Extends downward (positive values increase downward)
- **Canvas size:** 576 × 288 pixels (based on property ranges)

---

## 5. Getting Started — Quick Start

### Basic Usage

```typescript
import { waitForEvenAppBridge } from '@evenrealities/even_hub_sdk';

// Wait for bridge initialization (recommended)
const bridge = await waitForEvenAppBridge();

// Get user information
const user = await bridge.getUserInfo();
console.log('User:', user.name);

// Get device information
const device = await bridge.getDeviceInfo();
console.log('Device Model:', device?.model);

// Local storage operations
await bridge.setLocalStorage('theme', 'dark');
const theme = await bridge.getLocalStorage('theme');
```

### Device Status Monitoring

```typescript
import { waitForEvenAppBridge, DeviceConnectType } from '@evenrealities/even_hub_sdk';

const bridge = await waitForEvenAppBridge();

const unsubscribe = bridge.onDeviceStatusChanged((status) => {
  if (status.connectType === DeviceConnectType.Connected) {
    console.log('Device connected!', status.batteryLevel);
  }
});

// Later: unsubscribe();
```

### Creating Glasses UI

> **⚠️ Important:** You must call `createStartUpPageContainer` first before any other UI operations.

```typescript
import {
  waitForEvenAppBridge,
  CreateStartUpPageContainer,
  ListContainerProperty,
  TextContainerProperty,
} from '@evenrealities/even_hub_sdk';

const bridge = await waitForEvenAppBridge();

const listContainer: ListContainerProperty = {
  xPosition: 100,
  yPosition: 50,
  width: 200,
  height: 150,
  containerID: 1,
  containerName: 'list-1',
  itemContainer: {
    itemCount: 3,
    itemName: ['Item 1', 'Item 2', 'Item 3'],
  },
  isEventCapture: 1, // Only ONE container can have isEventCapture=1
};

const textContainer: TextContainerProperty = {
  xPosition: 100,
  yPosition: 220,
  width: 200,
  height: 50,
  containerID: 2,
  containerName: 'text-1',
  content: 'Hello World',
  isEventCapture: 0,
};

const result = await bridge.createStartUpPageContainer({
  containerTotalNum: 2, // Maximum: 4
  listObject: [listContainer],
  textObject: [textContainer],
});

if (result === 0) {
  console.log('Container created successfully');
}
```

### Event Listening

```typescript
const bridge = await waitForEvenAppBridge();

const unsubscribe = bridge.onEvenHubEvent((event) => {
  if (event.listEvent) {
    console.log('List selected:', event.listEvent.currentSelectItemName);
  } else if (event.textEvent) {
    console.log('Text event:', event.textEvent);
  } else if (event.sysEvent) {
    console.log('System event:', event.sysEvent.eventType);
  } else if (event.audioEvent) {
    console.log('Audio PCM length:', event.audioEvent.audioPcm.length);
  }
});

// Later: unsubscribe();
```

### Audio Control

```typescript
const bridge = await waitForEvenAppBridge();

// Open microphone (start receiving audio)
await bridge.audioControl(true);

// Listen for audio events
// PCM params: dtUs 10000 µs (frame length), srHz 16kHz (sample rate),
//             40 bytes per frame, little-endian byte order
const unsubscribe = bridge.onEvenHubEvent((event) => {
  if (event.audioEvent) {
    const pcm = event.audioEvent.audioPcm; // Uint8Array
    console.log('Audio PCM length:', pcm.length);
    // Process PCM with Web Audio API
  }
});

// Close microphone when done
await bridge.audioControl(false);
unsubscribe();
```

---

## 6. Full API Reference — `EvenAppBridge` Class

The main bridge class. Uses the **singleton pattern**.

### Obtaining an Instance

```typescript
import { EvenAppBridge, waitForEvenAppBridge } from '@evenrealities/even_hub_sdk';

// Method 1: Wait for bridge ready (RECOMMENDED)
const bridge = await waitForEvenAppBridge();

// Method 2: Get singleton directly (ensure it's initialized)
const bridge = EvenAppBridge.getInstance();
```

### Properties

| Property | Type | Description |
|---|---|---|
| `_ready` | `boolean` | Whether the bridge has completed initialization. `true` = ready, `false` = still initializing. |
| `ready` (getter) | `boolean` | Public getter for `_ready`. |

### Private Methods (documented for understanding)

| Method | Description |
|---|---|
| `private constructor()` | Private — singleton pattern. Use `getInstance()`. |
| `private init()` | Auto-called. Exposes bridge to `window.EvenAppBridge`, listens for DOM load, exposes `window._evenAppHandleMessage`. |
| `private dispatchReadyEvent()` | Triggers `BridgeEvent.BridgeReady` (`'evenAppBridgeReady'`) custom event on `window`. |
| `private postMessage(message: EvenAppMessage): Promise<any>` | Sends message to Even App via `flutter_inappwebview.callHandler('evenAppMessage', ...)`. Returns the host's response. |
| `private handleGlassesStatusChanged(data: any): void` | Processes device status push from host. Updates internal state and dispatches `'deviceStatusChanged'` event. |
| `private handleEvenHubEventChanged(data: any): void` | Processes EvenHub event push from host. Parses multiple input formats. |

### Public Methods

---

#### `static getInstance(): EvenAppBridge`

Get the singleton instance. Creates it if not yet created.

**Returns:** `EvenAppBridge`

---

#### `handleEvenAppMessage(message: string | EvenAppMessage): void`

Handle a message from Even App. This is the core method that processes all incoming messages.

**Parameters:**
- `message: string | EvenAppMessage` — Can be a JSON string or already-parsed object.

**Supported message types:**
- `listen_even_app_data` — Even App push notifications (e.g., `deviceStatusChanged`, `evenHubEvent`)

---

#### `callEvenApp(method: EvenAppMethod | string, params?: any): Promise<any>`

Generic method for calling Even App native functionality.

**Parameters:**
- `method: EvenAppMethod | string` — Method name (can use enum or string)
- `params?: any` — Method parameters (optional)

**Returns:** `Promise<any>` — Even App method execution result

```typescript
import { EvenAppMethod } from '@evenrealities/even_hub_sdk';

// Using enum
const result = await bridge.callEvenApp(EvenAppMethod.GetUserInfo);

// Using string
const result = await bridge.callEvenApp('getUserInfo');
```

---

#### `getUserInfo(): Promise<UserInfo>`

Get current logged-in user information.

**Returns:** `Promise<UserInfo>`

```typescript
const user = await bridge.getUserInfo();
console.log(user.name);
console.log(user.uid);
console.log(user.avatar);
console.log(user.country);
```

---

#### `getDeviceInfo(): Promise<DeviceInfo | null>`

Get device information (glasses/ring information). Returns `null` if no device info available.

**Returns:** `Promise<DeviceInfo | null>`

```typescript
const device = await bridge.getDeviceInfo();
if (device) {
  console.log('Model:', device.model);
  console.log('SN:', device.sn);
  console.log('Status:', device.status);
}
```

---

#### `setLocalStorage(key: string, value: string): Promise<boolean>`

Set a local storage value. Data is persisted on the App side.

**Parameters:**
- `key: string` — Storage key name
- `value: string` — Storage value

**Returns:** `Promise<boolean>` — Whether the operation succeeded

```typescript
await bridge.setLocalStorage('theme', 'dark');
await bridge.setLocalStorage('language', 'en-US');
```

---

#### `getLocalStorage(key: string): Promise<string>`

Get a local storage value.

**Parameters:**
- `key: string` — Storage key name

**Returns:** `Promise<string>` — Stored value; returns empty string if not found

```typescript
const theme = await bridge.getLocalStorage('theme');
const language = await bridge.getLocalStorage('language');
```

---

#### `createStartUpPageContainer(container: CreateStartUpPageContainer): Promise<StartUpPageCreateResult>`

Create the startup page container. **Must be called exactly once** when first starting the glasses UI. Cannot be called again afterwards (subsequent calls have no effect). All further page updates must use `rebuildPageContainer`.

**Parameters:**
- `container: CreateStartUpPageContainer` — Container configuration object

**Returns:** `Promise<StartUpPageCreateResult>` — Creation result enum (0=success, 1=invalid, 2=oversize, 3=outOfMemory)

**Important Rules:**
- When creating multiple containers, **exactly one** container must have `isEventCapture=1` (all others must be `0`)
- `containerTotalNum` has a **maximum value of 4** — at most 4 containers (single or mixed types)
- Image containers require calling `updateImageRawData` after creation to display actual content
- Image content **cannot** be transmitted during the startup stage (too large)

```typescript
import {
  CreateStartUpPageContainer,
  ListContainerProperty,
  TextContainerProperty,
  ImageContainerProperty,
} from '@evenrealities/even_hub_sdk';

const listContainer: ListContainerProperty = {
  xPosition: 100,
  yPosition: 50,
  width: 200,
  height: 150,
  borderWidth: 2,
  borderColor: 5,
  borderRdaius: 5,
  paddingLength: 10,
  containerID: 1,
  containerName: 'list-1',
  itemContainer: {
    itemCount: 3,
    itemWidth: 0, // 0 = auto fill
    isItemSelectBorderEn: 1,
    itemName: ['Item 1', 'Item 2', 'Item 3'],
  },
  isEventCapture: 1,
};

const textContainer: TextContainerProperty = {
  xPosition: 100,
  yPosition: 220,
  width: 200,
  height: 50,
  borderWidth: 1,
  borderColor: 0,
  borderRdaius: 3,
  paddingLength: 5,
  containerID: 2,
  containerName: 'text-1',
  content: 'Hello World',
  isEventCapture: 0,
};

const imageContainer: ImageContainerProperty = {
  xPosition: 320,
  yPosition: 50,
  width: 100,
  height: 80,
  containerID: 3,
  containerName: 'img-1',
};

const container: CreateStartUpPageContainer = {
  containerTotalNum: 3,
  listObject: [listContainer],
  textObject: [textContainer],
  imageObject: [imageContainer],
};

const result = await bridge.createStartUpPageContainer(container);
if (result === 0) {
  console.log('Container created successfully');

  // Image containers need updateImageRawData after creation
  await bridge.updateImageRawData({
    containerID: 3,
    containerName: 'img-1',
    imageData: [/* image data as number[] */],
  });
} else {
  console.error('Failed to create container:', result);
}
```

---

#### `rebuildPageContainer(container: RebuildPageContainer): Promise<boolean>`

Rebuild page container. Used to update the current page or create a new page. Functionally identical to `createStartUpPageContainer` with the same parameter structure, but this is the method to use for **all subsequent updates** after the initial startup.

**Parameters:**
- `container: RebuildPageContainer` — Container configuration object (same structure as `CreateStartUpPageContainer`)

**Returns:** `Promise<boolean>` — Whether the operation succeeded

```typescript
import { RebuildPageContainer } from '@evenrealities/even_hub_sdk';

const container: RebuildPageContainer = {
  containerTotalNum: 2,
  listObject: [/* ... ListContainerProperty[] */],
  textObject: [/* ... TextContainerProperty[] */],
  imageObject: [/* ... ImageContainerProperty[] */],
};

const success = await bridge.rebuildPageContainer(container);
if (success) {
  // If there are image containers, call updateImageRawData after rebuild
  await bridge.updateImageRawData({
    containerID: 3,
    containerName: 'img-1',
    imageData: [/* image data */],
  });
}
```

---

#### `updateImageRawData(data: ImageRawDataUpdate): Promise<ImageRawDataUpdateResult>`

Update image raw data for an image container.

**Parameters:**
- `data: ImageRawDataUpdate` — Image data update object

**Returns:** `Promise<ImageRawDataUpdateResult>` — Update result enum

**Important Notes:**
1. Images should preferably use **simple, single-color schemes**
2. Image transmission must **not be sent concurrently** — use a queue mode, ensuring the previous transmission returns successfully before sending the next one
3. Due to limited memory on the glasses, **avoid sending images too frequently**
4. `imageData` is recommended as `number[]`. If `Uint8Array` or `ArrayBuffer` is passed, the SDK auto-converts to `number[]`. You can also pass a base64 string.

```typescript
import { ImageRawDataUpdate } from '@evenrealities/even_hub_sdk';

const raw: Uint8Array = new Uint8Array([1, 2, 3]);

const data: ImageRawDataUpdate = {
  containerID: 1,
  containerName: 'img-1',
  imageData: raw, // SDK auto-converts Uint8Array/ArrayBuffer to number[]
};

const result = await bridge.updateImageRawData(data);
```

---

#### `textContainerUpgrade(container: TextContainerUpgrade): Promise<boolean>`

Update text content in an existing text container.

**Parameters:**
- `container: TextContainerUpgrade` — Text container upgrade configuration

**Returns:** `Promise<boolean>` — Whether the operation succeeded

**Parameter Requirements:**
- `containerName`: Maximum 16 characters
- `content`: Maximum 2000 characters

```typescript
import { TextContainerUpgrade } from '@evenrealities/even_hub_sdk';

const container: TextContainerUpgrade = {
  containerID: 1,
  containerName: 'text-1', // max 16 characters
  contentOffset: 0,
  contentLength: 100,
  content: 'Your text content here', // max 2000 characters
};

const success = await bridge.textContainerUpgrade(container);
```

---

#### `audioControl(isOpen: boolean): Promise<boolean>`

EvenHub MIC control (audio control). Opens or closes the microphone.

> **Prerequisite:** You must call `createStartUpPageContainer` successfully **before** opening or closing the microphone.

**Parameters:**
- `isOpen: boolean` — `true` to open the microphone, `false` to close it

**Returns:** `Promise<boolean>` — `true` on success, `false` on failure

```typescript
// Open microphone
await bridge.audioControl(true);

// Close microphone
await bridge.audioControl(false);
```

---

#### `shutDownPageContainer(exitMode?: number): Promise<boolean>`

Shut down (close) the page container.

**Parameters:**
- `exitMode?: number` — Exit mode (optional, defaults to 0)
  - `0` = Exit immediately
  - `1` = Show foreground interaction layer, let user decide whether to exit

**Returns:** `Promise<boolean>` — Whether the operation succeeded

```typescript
// Exit immediately
await bridge.shutDownPageContainer(0);

// Show interaction layer for user decision
await bridge.shutDownPageContainer(1);
```

---

#### `onDeviceStatusChanged(callback: (status: DeviceStatus) => void): () => void`

Listen to device status change events.

**Parameters:**
- `callback: (status: DeviceStatus) => void` — Callback function when status changes

**Returns:** `() => void` — Unsubscribe function

```typescript
const unsubscribe = bridge.onDeviceStatusChanged((status) => {
  console.log('Connect Type:', status.connectType);
  console.log('Battery Level:', status.batteryLevel);
  console.log('Is Wearing:', status.isWearing);
  console.log('Is Charging:', status.isCharging);
});

// Unsubscribe when done
unsubscribe();
```

---

#### `onEvenHubEvent(callback: (event: EvenHubEvent) => void): () => void`

Listen to EvenHub event pushes from the glasses OS.

**Parameters:**
- `callback: (event: EvenHubEvent) => void` — Event callback function

**Returns:** `() => void` — Unsubscribe function

```typescript
const unsubscribe = bridge.onEvenHubEvent((event) => {
  if (event.listEvent) {
    // Handle list event
  } else if (event.textEvent) {
    // Handle text event
  } else if (event.sysEvent) {
    // Handle system event
  } else if (event.audioEvent) {
    // Handle audio event (PCM bytes from host)
    const pcm = event.audioEvent.audioPcm; // Uint8Array
  }
});

// Unsubscribe when done
unsubscribe();
```

---

## 7. Top-Level Exported Function

#### `waitForEvenAppBridge(): Promise<EvenAppBridge>`

Helper function to wait for the bridge to be ready.

**Returns:** `Promise<EvenAppBridge>` — The ready bridge instance

**How it works:**
1. First checks if the bridge already exists and is ready — if so, returns immediately
2. If not ready, listens for the `'evenAppBridgeReady'` custom event
3. If bridge exists but hasn't been marked ready, waits 100ms then re-checks (fault tolerance)

```typescript
const bridge = await waitForEvenAppBridge();
const userInfo = await bridge.getUserInfo();
console.log('User Info:', userInfo);
```

---

## 8. Data Models (Classes)

### `UserInfo`

User information model.

**Properties:**

| Property | Type | Description |
|---|---|---|
| `uid` | `number` | User ID |
| `name` | `string` | Username |
| `avatar` | `string` | User avatar URL |
| `country` | `string` | User country |

**Constructor:**
```typescript
new UserInfo(params: { uid: number; name: string; avatar: string; country: string })
```

**Instance Methods:**
- `toJson(): Record<string, any>` — Convert to JSON object

**Static Methods:**
- `fromJson(json: any): UserInfo` — Create UserInfo instance from JSON
- `createDefault(): UserInfo` — Create default UserInfo instance

---

### `DeviceInfo`

Device information model. `model` and `sn` are **immutable** after creation; only `status` can be updated.

**Properties:**

| Property | Type | Description |
|---|---|---|
| `readonly model` | `DeviceModel` | Device model (read-only) |
| `readonly sn` | `string` | Device serial number (read-only) |
| `status` | `DeviceStatus` | Device status (mutable) |

**Constructor:**
```typescript
new DeviceInfo(params: { model: DeviceModel; sn: string; status?: DeviceStatus })
```

**Instance Methods:**
- `updateStatus(status: DeviceStatus): void` — Update device status. **Only updates when `status.sn === device.sn`**; mismatches are silently ignored.
- `isGlasses(): boolean` — Check if device is glasses
- `isRing(): boolean` — Check if device is ring
- `toJson(): Record<string, any>` — Convert to JSON object

**Static Methods:**
- `fromJson(json: any): DeviceInfo` — Create DeviceInfo instance from JSON

---

### `DeviceStatus`

Device status model.

**Properties:**

| Property | Type | Description |
|---|---|---|
| `readonly sn` | `string` | Device serial number (read-only) |
| `connectType` | `DeviceConnectType` | Connection status |
| `isWearing?` | `boolean` | Whether wearing |
| `batteryLevel?` | `number` | Battery level (0-100) |
| `isCharging?` | `boolean` | Whether charging |
| `isInCase?` | `boolean` | Whether in charging case |

**Constructor:**
```typescript
new DeviceStatus(params: {
  sn: string;
  connectType?: DeviceConnectType;
  isWearing?: boolean;
  batteryLevel?: number;
  isCharging?: boolean;
  isInCase?: boolean;
})
```

**Instance Methods:**
- `toJson(): Record<string, any>` — Convert to JSON object
- `isNone(): boolean` — Check if status is not initialized
- `isConnected(): boolean` — Check if device is connected
- `isConnecting(): boolean` — Check if device is connecting
- `isDisconnected(): boolean` — Check if device is disconnected
- `isConnectionFailed(): boolean` — Check if connection failed

**Static Methods:**
- `fromJson(json: any): DeviceStatus` — Create DeviceStatus instance from JSON
- `createDefault(sn?: string): DeviceStatus` — Create default DeviceStatus instance

---

### `ListContainerProperty`

List container configuration. Aligned with PB: `ListContainerProperty`.

**Properties:**

| Property | Type | Range/Constraint | Description |
|---|---|---|---|
| `xPosition?` | `number` | 0–576 | X position |
| `yPosition?` | `number` | 0–288 | Y position |
| `width?` | `number` | 0–576 | Width |
| `height?` | `number` | 0–288 | Height |
| `borderWidth?` | `number` | 0–5 | Border width |
| `borderColor?` | `number` | 0–15 | Border color |
| `borderRdaius?` | `number` | 0–10 | Border radius (note: PB spelling is "Rdaius") |
| `paddingLength?` | `number` | 0–32 | Padding length |
| `containerID?` | `number` | (random) | Container ID |
| `containerName?` | `string` | max 16 chars | Container name |
| `itemContainer?` | `ListItemContainerProperty` | — | Item container configuration |
| `isEventCapture?` | `number` | 0 or 1 | Event capture flag (only one container per page can be 1) |

**Constructor:**
```typescript
new ListContainerProperty(data?: Partial<ListContainerProperty>)
```

**Instance Methods:**
- `toJson(): Record<string, any>`

**Static Methods:**
- `fromJson(json: any): ListContainerProperty` — Auto-compatible with camelCase and protoName keys
- `toJson(model?: ListContainerProperty | Record<string, any>): Record<string, any>`

---

### `ListItemContainerProperty`

List item container configuration. Aligned with PB: `List_ItemContainerProperty`.

**Properties:**

| Property | Type | Range/Constraint | Description |
|---|---|---|---|
| `itemCount?` | `number` | 1–20 | Item count |
| `itemWidth?` | `number` | 0 = auto fill, other = fixed | Item width |
| `isItemSelectBorderEn?` | `number` | 0 or 1 | Item select border enable (1 = show outer border when selected, 0 = hidden) |
| `itemName?` | `string[]` | max 20 items, max 64 chars each | Item names |

**Constructor:**
```typescript
new ListItemContainerProperty(data?: Partial<ListItemContainerProperty>)
```

**Instance Methods:**
- `toJson(): Record<string, any>`

**Static Methods:**
- `fromJson(json: any): ListItemContainerProperty`
- `toJson(model?: ListItemContainerProperty | Record<string, any>): Record<string, any>`

---

### `TextContainerProperty`

Text container configuration. Aligned with PB: `TextContainerProperty`.

**Properties:**

| Property | Type | Range/Constraint | Description |
|---|---|---|---|
| `xPosition?` | `number` | 0–576 | X position |
| `yPosition?` | `number` | 0–288 | Y position |
| `width?` | `number` | 0–576 | Width |
| `height?` | `number` | 0–288 | Height |
| `borderWidth?` | `number` | 0–5 | Border width |
| `borderColor?` | `number` | 0–16 | Border color |
| `borderRdaius?` | `number` | 0–10 | Border radius |
| `paddingLength?` | `number` | 0–32 | Padding length |
| `containerID?` | `number` | (random) | Container ID |
| `containerName?` | `string` | max 16 chars | Container name |
| `isEventCapture?` | `number` | 0 or 1 | Event capture flag. On a page, only one container can handle events. |
| `content?` | `string` | max 1000 chars | Content text. During startup phase, minimize data length for transmission efficiency. |

**Constructor:**
```typescript
new TextContainerProperty(data?: Partial<TextContainerProperty>)
```

**Instance Methods:**
- `toJson(): Record<string, any>`

**Static Methods:**
- `fromJson(json: any): TextContainerProperty`
- `toJson(model?: TextContainerProperty | Record<string, any>): Record<string, any>`

---

### `TextContainerUpgrade`

Text container upgrade configuration. Aligned with PB: `TextContainerUpgrade`.

**Properties:**

| Property | Type | Constraint | Description |
|---|---|---|---|
| `containerID?` | `number` | — | Container ID |
| `containerName?` | `string` | max 16 chars | Container name |
| `contentOffset?` | `number` | — | Content offset |
| `contentLength?` | `number` | — | Content length |
| `content?` | `string` | max 2000 chars | Content text |

**Constructor:**
```typescript
new TextContainerUpgrade(data?: Partial<TextContainerUpgrade>)
```

**Instance Methods:**
- `toJson(): Record<string, any>`

**Static Methods:**
- `fromJson(json: any): TextContainerUpgrade`
- `toJson(model?: TextContainerUpgrade | Record<string, any>): Record<string, any>`

---

### `ImageContainerProperty`

Image container configuration. Aligned with PB: `ImageContainerProperty`.

**Properties:**

| Property | Type | Range/Constraint | Description |
|---|---|---|---|
| `xPosition?` | `number` | 0–576 | X position |
| `yPosition?` | `number` | 0–288 | Y position |
| `width?` | `number` | 20–200 | Width |
| `height?` | `number` | 20–100 | Height |
| `containerID?` | `number` | — | Container ID |
| `containerName?` | `string` | max 16 chars | Container name |

> **Note:** Image data volume is too large; image content **cannot** be transmitted during the startup stage. Create the container first, then call `updateImageRawData`.

**Constructor:**
```typescript
new ImageContainerProperty(data?: Partial<ImageContainerProperty>)
```

**Instance Methods:**
- `toJson(): Record<string, any>`

**Static Methods:**
- `fromJson(json: any): ImageContainerProperty`
- `toJson(model?: ImageContainerProperty | Record<string, any>): Record<string, any>`

---

### `ImageRawDataUpdate`

Image raw data update model. Aligned with host Dart: `EvenHubImageContainer`.

**Properties:**

| Property | Type | Description |
|---|---|---|
| `containerID?` | `number` | Container ID |
| `containerName?` | `string` | Container name |
| `imageData?` | `number[] \| string \| Uint8Array \| ArrayBuffer` | Image data. Recommended: `number[]`. Also accepts base64 string, Uint8Array, or ArrayBuffer. |

> If `Uint8Array` or `ArrayBuffer` is passed, the SDK auto-converts to `number[]` during serialization via `toJson()`.

**Constructor:**
```typescript
new ImageRawDataUpdate(data?: Partial<ImageRawDataUpdate>)
```

**Instance Methods:**
- `toJson(): Record<string, any>`

**Static Methods:**
- `fromJson(json: any): ImageRawDataUpdate`
- `toJson(model?: ImageRawDataUpdate | Record<string, any>): Record<string, any>`
- `normalizeImageData(raw: any): number[] | string | undefined`

---

### `ImageRawDataUpdateFields`

Low-level image raw data update fields. Aligned with PB: `ImageRawDataUpdate`. (Noted as "reserved, not currently used" but available.)

**Properties:**

| Property | Type | Description |
|---|---|---|
| `containerID?` | `number` | Container ID |
| `containerName?` | `string` | Container name |
| `mapSessionId?` | `number` | Session ID for image transfer |
| `mapTotalSize?` | `number` | Total size of the image data |
| `compressMode?` | `number` | Compression mode |
| `mapFragmentIndex?` | `number` | Fragment index |
| `mapFragmentPacketSize?` | `number` | Fragment packet size |
| `mapRawData?` | `number[] \| string \| Uint8Array \| ArrayBuffer` | Raw bytes (recommended: `number[]` or base64 string) |

**Constructor:**
```typescript
new ImageRawDataUpdateFields(data?: Partial<ImageRawDataUpdateFields>)
```

**Instance Methods:**
- `toJson(): Record<string, any>`

**Static Methods:**
- `fromJson(json: any): ImageRawDataUpdateFields`
- `toJson(model?: ImageRawDataUpdateFields | Record<string, any>): Record<string, any>`

---

### `CreateStartUpPageContainer`

Startup page container creation model. Aligned with PB: `CreateStartUpPageContainer`.

**Properties:**

| Property | Type | Description |
|---|---|---|
| `containerTotalNum?` | `number` | Total number of containers (max 4) |
| `listObject?` | `ListContainerProperty[]` | List of list containers |
| `textObject?` | `TextContainerProperty[]` | List of text containers |
| `imageObject?` | `ImageContainerProperty[]` | List of image containers |

**Constructor:**
```typescript
new CreateStartUpPageContainer(data?: Partial<CreateStartUpPageContainer>)
```

**Instance Methods:**
- `toJson(): Record<string, any>`

**Static Methods:**
- `fromJson(json: any): CreateStartUpPageContainer` — Auto-compatible with `List_Object` protoName keys
- `toJson(model?: CreateStartUpPageContainer | Record<string, any>): Record<string, any>`

---

### `RebuildPageContainer`

Page container rebuild model. Same structure as `CreateStartUpPageContainer`.

**Properties:**

| Property | Type | Description |
|---|---|---|
| `containerTotalNum?` | `number` | Total number of containers (max 4) |
| `listObject?` | `ListContainerProperty[]` | List of list containers |
| `textObject?` | `TextContainerProperty[]` | List of text containers |
| `imageObject?` | `ImageContainerProperty[]` | List of image containers |

**Constructor:**
```typescript
new RebuildPageContainer(data?: Partial<RebuildPageContainer>)
```

**Instance Methods:**
- `toJson(): Record<string, any>`

**Static Methods:**
- `fromJson(json: any): RebuildPageContainer`
- `toJson(model?: RebuildPageContainer | Record<string, any>): Record<string, any>`

---

### `ShutDownContaniner`

Shutdown container model (note: the typo "Contaniner" is in the SDK itself).

**Properties:**

| Property | Type | Description |
|---|---|---|
| `exitMode` | `number` | 0 = exit immediately; 1 = pop up foreground layer for user decision |
| `[key: string]` | `any` | Allows additional arbitrary properties |

**Constructor:**
```typescript
new ShutDownContaniner(data?: Partial<ShutDownContaniner>)
```

**Instance Methods:**
- `toJson(): Record<string, any>`

**Static Methods:**
- `fromJson(json: any): ShutDownContaniner`
- `toJson(model?: ShutDownContaniner | Record<string, any>): Record<string, any>`

---

### `List_ItemEvent`

List item event model. Typical scenario: OS list component fires click/scroll events, reports currently selected item.

**Properties:**

| Property | Type | Description |
|---|---|---|
| `containerID?` | `number` | Container ID |
| `containerName?` | `string` | Container name |
| `currentSelectItemName?` | `string` | Currently selected item name |
| `currentSelectItemIndex?` | `number` | Currently selected item index |
| `eventType?` | `OsEventTypeList` | Event type |

**Constructor:**
```typescript
new List_ItemEvent(data?: Partial<List_ItemEvent>)
```

**Instance Methods:**
- `toJson(): Record<string, any>`

**Static Methods:**
- `fromJson(input: any): List_ItemEvent` — Compatible with camelCase and protoName keys; handles string→number conversion
- `toJson(model?: List_ItemEvent | Record<string, any>): Record<string, any>`

---

### `Text_ItemEvent`

Text item event model. Typical scenario: OS text component fires click/foreground-enter/foreground-exit events.

**Properties:**

| Property | Type | Description |
|---|---|---|
| `containerID?` | `number` | Container ID |
| `containerName?` | `string` | Container name |
| `eventType?` | `OsEventTypeList` | Event type |

**Constructor:**
```typescript
new Text_ItemEvent(data?: Partial<Text_ItemEvent>)
```

**Instance Methods:**
- `toJson(): Record<string, any>`

**Static Methods:**
- `fromJson(input: any): Text_ItemEvent`
- `toJson(model?: Text_ItemEvent | Record<string, any>): Record<string, any>`

---

### `Sys_ItemEvent`

System item event model. Typical scenario: OS system-level events (foreground enter, foreground exit, etc.).

**Properties:**

| Property | Type | Description |
|---|---|---|
| `eventType?` | `OsEventTypeList` | Event type |

**Constructor:**
```typescript
new Sys_ItemEvent(data?: Partial<Sys_ItemEvent>)
```

**Instance Methods:**
- `toJson(): Record<string, any>`

**Static Methods:**
- `fromJson(input: any): Sys_ItemEvent`
- `toJson(model?: Sys_ItemEvent | Record<string, any>): Record<string, any>`

> **Note:** Image events (`imgEvent`) are defined in the protocol but are **not yet included** in the current version's type definitions.

---

## 9. Enum Types

### `EvenAppMethod`

Even App method enum — defines all methods that Web pages can call on Even App.

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

---

### `EvenAppMessageType`

Message type enum for Web ↔ App communication.

```typescript
enum EvenAppMessageType {
  CallEvenAppMethod = 'call_even_app_method',     // Web calls Even App
  ListenEvenAppNotify = 'listen_even_app_data',   // Web listens to Even App notifications
}
```

---

### `BridgeEvent`

Bridge-triggered custom event names.

```typescript
enum BridgeEvent {
  BridgeReady = 'evenAppBridgeReady',          // Bridge initialization complete
  DeviceStatusChanged = 'deviceStatusChanged', // Device status changed
  EvenHubEvent = 'evenHubEvent',               // EvenHub (PB) event pushed
}
```

---

### `DeviceConnectType`

Device connection status enum. Not tightly coupled to BLE — named generically.

```typescript
enum DeviceConnectType {
  None = 'none',                       // Default, uninitialized
  Connecting = 'connecting',           // Connecting
  Connected = 'connected',             // Connected successfully
  Disconnected = 'disconnected',       // Disconnected
  ConnectionFailed = 'connectionFailed', // Connection failed
}
```

**Namespace helper:**
- `DeviceConnectType.fromString(value: string): DeviceConnectType` — Parse from string; returns `None` if no match.

---

### `DeviceModel`

Even device model enum.

```typescript
enum DeviceModel {
  G1 = 'g1',       // G1 glasses
  G2 = 'g2',       // G2 glasses
  Ring1 = 'ring1',  // Ring1 ring
}
```

**Namespace helpers:**
- `DeviceModel.fromString(value: string): DeviceModel` — Parse from string; defaults to `G1` if no match.
- `DeviceModel.isGlasses(model: DeviceModel): boolean` — Check if the model is glasses type.
- `DeviceModel.isRing(model: DeviceModel): boolean` — Check if the model is ring type.

---

### `StartUpPageCreateResult`

Startup page creation result enum. Aligned with host (Dart) `EhStartUpPageCreateResult`.

```typescript
enum StartUpPageCreateResult {
  success = 0,
  invalid = 1,
  oversize = 2,
  outOfMemory = 3,
}
```

**Namespace helpers:**
- `StartUpPageCreateResult.fromInt(value: number): StartUpPageCreateResult` — Convert host int; out-of-range defaults to `invalid`.
- `StartUpPageCreateResult.normalize(raw: any): StartUpPageCreateResult` — Best-effort normalization; handles `number`, `"0"`–`"3"` strings.

---

### `ImageRawDataUpdateResult`

Image send result enum. Aligned with host (Dart) `BleEhSendImageResult`.

```typescript
enum ImageRawDataUpdateResult {
  success = 'success',
  imageException = 'imageException',
  imageSizeInvalid = 'imageSizeInvalid',
  imageToGray4Failed = 'imageToGray4Failed',
  sendFailed = 'sendFailed',
}
```

**Namespace helpers:**
- `ImageRawDataUpdateResult.valueCode(value: ImageRawDataUpdateResult): number` — Convert enum to host int code.
- `ImageRawDataUpdateResult.fromInt(value: number): ImageRawDataUpdateResult` — Convert host int (note: 2 maps to `imageSizeInvalid`, host may also mean `imageToGray4Failed`).
- `ImageRawDataUpdateResult.normalize(raw: any): ImageRawDataUpdateResult` — Best-effort; handles number, numeric string, enum string (e.g., `"BleEhSendImageResult.success"` or `"success"`).
- `ImageRawDataUpdateResult.isSuccess(value): boolean`
- `ImageRawDataUpdateResult.isImageException(value): boolean`
- `ImageRawDataUpdateResult.isImageSizeInvalid(value): boolean`
- `ImageRawDataUpdateResult.isImageToGray4Failed(value): boolean`
- `ImageRawDataUpdateResult.isSendFailed(value): boolean`

---

### `EvenHubErrorCodeName`

Error code names aligned with PB: `EvenHub_ErrorCode_List`. Host (Flutter) may return enum name (string) or value (number).

```typescript
enum EvenHubErrorCodeName {
  APP_REQUEST_CREATE_PAGE_SUCCESS = 'APP_REQUEST_CREATE_PAGE_SUCCESS',
  APP_REQUEST_CREATE_INVAILD_CONTAINER = 'APP_REQUEST_CREATE_INVAILD_CONTAINER',
  APP_REQUEST_CREATE_OVERSIZE_RESPONSE_CONTAINER = 'APP_REQUEST_CREATE_OVERSIZE_RESPONSE_CONTAINER',
  APP_REQUEST_CREATE_OUTOFMEMORY_CONTAINER = 'APP_REQUEST_CREATE_OUTOFMEMORY_CONTAINER',
  APP_REQUEST_UPGRADE_IMAGE_RAW_DATA_SUCCESS = 'APP_REQUEST_UPGRADE_IMAGE_RAW_DATA_SUCCESS',
  APP_REQUEST_UPGRADE_IMAGE_RAW_DATA_FAILED = 'APP_REQUEST_UPGRADE_IMAGE_RAW_DATA_FAILED',
  APP_REQUEST_REBUILD_PAGE_SUCCESS = 'APP_REQUEST_REBUILD_PAGE_SUCCESS',
  APP_REQUEST_REBUILD_PAGE_FAILD = 'APP_REQUEST_REBUILD_PAGE_FAILD',
  APP_REQUEST_UPGRADE_TEXT_DATA_SUCCESS = 'APP_REQUEST_UPGRADE_TEXT_DATA_SUCCESS',
  APP_REQUEST_UPGRADE_TEXT_DATA_FAILED = 'APP_REQUEST_UPGRADE_TEXT_DATA_FAILED',
  APP_REQUEST_UPGRADE_SHUTDOWN_SUCCESS = 'APP_REQUEST_UPGRADE_SHUTDOWN_SUCCESS',
  APP_REQUEST_UPGRADE_SHUTDOWN_FAILED = 'APP_REQUEST_UPGRADE_SHUTDOWN_FAILED',
  APP_REQUEST_UPGRADE_HEARTBEAT_PACKET_SUCCESS = 'APP_REQUEST_UPGRADE_HEARTBEAT_PACKET_SUCCESS',
}
```

---

### `EvenHubEventType`

EvenHub event type enum.

```typescript
enum EvenHubEventType {
  listEvent = 'listEvent',
  textEvent = 'textEvent',
  sysEvent = 'sysEvent',
  audioEvent = 'audioEvent',
  notSet = 'notSet',
}
```

---

### `OsEventTypeList`

OS event type list (PB-aligned enum).

```typescript
enum OsEventTypeList {
  CLICK_EVENT = 0,
  SCROLL_TOP_EVENT = 1,
  SCROLL_BOTTOM_EVENT = 2,
  DOUBLE_CLICK_EVENT = 3,
  FOREGROUND_ENTER_EVENT = 4,
  FOREGROUND_EXIT_EVENT = 5,
  ABNORMAL_EXIT_EVENT = 6,
}
```

**Namespace helper:**
- `OsEventTypeList.fromJson(raw: any): OsEventTypeList | undefined` — Normalizes from number (0–6), string (`'CLICK_EVENT'`), or shorthand (`'CLICK'`).

---

## 10. Type Aliases

### `EvenHubEvent`

The unified event structure pushed from Flutter to Web.

```typescript
type EvenHubEvent = {
  listEvent?: List_ItemEvent;
  textEvent?: Text_ItemEvent;
  sysEvent?: Sys_ItemEvent;
  audioEvent?: AudioEventPayload;
  jsonData?: Record<string, any>;  // Raw JSON for debugging/replay
};
```

---

### `AudioEventPayload`

Audio event payload: host pushes PCM bytes via `onAudioData`.

```typescript
type AudioEventPayload = {
  audioPcm: Uint8Array;
};
```

**PCM Parameters:**
- `dtUs`: 10000 µs (frame length = 10 ms)
- `srHz`: 16000 Hz (sample rate = 16 kHz)
- Frame size: 40 bytes per frame
- Byte order: little-endian

---

### `EvenHubEventPayload`

Union type for all possible event payloads.

```typescript
type EvenHubEventPayload =
  | List_ItemEvent
  | Text_ItemEvent
  | Sys_ItemEvent
  | AudioEventPayload
  | Record<string, any>;
```

---

### `EvenHubErrorCode`

Flexible error code type (host may return string name or numeric value).

```typescript
type EvenHubErrorCode = EvenHubErrorCodeName | number | string;
```

---

### `JsonRecord`

Convenience alias used internally.

```typescript
type JsonRecord = Record<string, any>;
```

---

## 11. Utility Functions (Exported)

These are helper functions for JSON field mapping, primarily for compatibility with the host's protobuf-style naming conventions.

### `waitForEvenAppBridge(): Promise<EvenAppBridge>`

(Documented above in Section 7.)

### `createDefaultEvenHubEvent(): EvenHubEvent`

Creates a default empty `EvenHubEvent` (used for bridge initialization cache value).

### `evenHubEventFromJson(input: any): EvenHubEvent`

Parse an `EvenHubEvent` from host-sent JSON. Compatible with multiple input formats:
- `{ type: 'listEvent', jsonData: {...} }`
- `{ type: 'list_event', data: {...} }`
- `[ 'list_event', {...} ]`

**Strategy:** Normalize the `type`, unify the data field to `jsonData`, then parse into the corresponding event object.

### `isObjectRecord(v: any): v is JsonRecord`

Check if a value is a plain object (non-array). `null`/`undefined` → `false`, `[]` → `false`, `{}` → `true`.

### `pick<T = any>(obj: any, ...keys: string[]): T | undefined`

Read the first existing key from an object by priority.

```typescript
pick(json, 'containerID', 'ContainerID', 'Container_ID')
```

### `pickLoose<T = any>(obj: any, ...keys: string[]): T | undefined`

Loose field reading: first tries exact key match, then falls back to "remove underscores + ignore case" matching. Useful for compatibility with:
- `containerID`
- `ContainerID`
- `Container_ID`

### `normalizeLooseKey(key: string): string`

Normalize a key for loose matching: remove `_` and lowercase.
- `Container_ID` → `containerid`
- `CurrentSelect_ItemIndex` → `currentselectitemindex`

### `toNumber(v: any): number | undefined`

Best-effort conversion to number. Handles string numbers from host (e.g., `"12"`).

### `toString(v: any): string | undefined`

Best-effort conversion to string. Numbers/booleans are `String(...)` converted.

### `readNumber(obj: any, ...keys: string[]): number | undefined`

Read a number from an object with loose key matching and string→number conversion.

### `readString(obj: any, ...keys: string[]): string | undefined`

Read a string from an object with loose key matching and number→string conversion.

### `toObjectRecord(v: any): JsonRecord`

Convert any value to a Record. Non-object/array values return `{}`.

### `bytesToJson(v: any): number[] | string | undefined`

Convert bytes-like data to JSON-friendly payload:
- `string` → returned as-is (typically base64)
- `Uint8Array` / `ArrayBuffer` → converted to `number[]`
- `number[]` → normalized to 0–255

> **Note:** This is NOT protobuf encoding/decoding; it's for JSON.stringify compatibility.

---

## 12. Display Capabilities — Detailed

### Container Types

The glasses support 3 container types, up to **4 containers per page**:

| Type | Class | Description |
|---|---|---|
| **List** | `ListContainerProperty` | Scrollable list with selectable items |
| **Text** | `TextContainerProperty` | Static/dynamic text display |
| **Image** | `ImageContainerProperty` | Image display (loaded after container creation) |

### Display Canvas

- **Resolution:** 576 × 288 pixels (based on property ranges)
- **Coordinate origin:** Top-left (0, 0)
- **X-axis:** Left to right (0–576)
- **Y-axis:** Top to bottom (0–288)

### Container Common Properties

| Property | Range | Description |
|---|---|---|
| `xPosition` | 0–576 | Horizontal position |
| `yPosition` | 0–288 | Vertical position |
| `width` | 0–576 (images: 20–200) | Container width |
| `height` | 0–288 (images: 20–100) | Container height |
| `borderWidth` | 0–5 | Border thickness |
| `borderColor` | 0–15 (text: 0–16) | Border color index |
| `borderRdaius` | 0–10 | Border corner radius |
| `paddingLength` | 0–32 | Internal padding |
| `containerID` | any number | Unique container identifier |
| `containerName` | max 16 chars | Container name |
| `isEventCapture` | 0 or 1 | **Only one container per page** can have `1` |

### Image Constraints

- Width range: **20–200** pixels
- Height range: **20–100** pixels
- Images should use **simple, single-color schemes**
- Must **not** send images concurrently — use a **queue** (wait for previous to succeed)
- Avoid sending images **too frequently** (limited glasses memory)
- Image data **cannot** be sent during startup phase — create container first, then `updateImageRawData`
- Images are converted to **gray4** (4-bit grayscale) on the host side

### Text Constraints

- Content during startup: max **1000 characters** (minimize for transmission efficiency)
- Content during upgrade (`textContainerUpgrade`): max **2000 characters**

### List Constraints

- Max **20 items** per list
- Max **64 characters** per item name
- Item width: `0` = auto fill, other = fixed user-defined width

### Page Lifecycle

1. **`createStartUpPageContainer`** — Called **exactly once** at startup to create the initial page
2. **`updateImageRawData`** — Called after creation for any image containers
3. **`textContainerUpgrade`** — Update text content dynamically
4. **`rebuildPageContainer`** — All subsequent page changes (update or new page)
5. **`shutDownPageContainer`** — Close/exit the page

---

## 13. Audio / Microphone Details

### Opening the Microphone

```typescript
await bridge.audioControl(true);
```

> **Prerequisite:** `createStartUpPageContainer` must have been called successfully first.

### Receiving Audio Data

Audio data arrives as `audioEvent` in the `onEvenHubEvent` callback:

```typescript
bridge.onEvenHubEvent((event) => {
  if (event.audioEvent) {
    const pcm: Uint8Array = event.audioEvent.audioPcm;
    // Process PCM data
  }
});
```

### PCM Format

| Parameter | Value |
|---|---|
| Frame duration (`dtUs`) | 10,000 µs (10 ms) |
| Sample rate (`srHz`) | 16,000 Hz (16 kHz) |
| Frame size | 40 bytes |
| Byte order | Little-endian |

### Data Format from Host

The host sends `audioPcm` as `number[]` or base64 string inside `jsonData`. The SDK parses it into `Uint8Array`.

### Closing the Microphone

```typescript
await bridge.audioControl(false);
```

---

## 14. Event System — Detailed

### Push Message Format (App → Web)

#### Device Status Changes

```json
{
  "type": "listen_even_app_data",
  "method": "deviceStatusChanged",
  "data": {
    "sn": "DEVICE_SN",
    "connectType": "connected",
    "isWearing": true,
    "batteryLevel": 80,
    "isCharging": false,
    "isInCase": false
  }
}
```

#### EvenHub Events

```json
{
  "type": "listen_even_app_data",
  "method": "evenHubEvent",
  "data": {
    "type": "listEvent",
    "jsonData": {
      "containerID": 1,
      "currentSelectItemName": "item1"
    }
  }
}
```

### Compatible Event Data Formats

The SDK is compatible with multiple formats from the host:

1. `data: { type: 'listEvent', jsonData: {...} }`
2. `data: { type: 'list_event', data: {...} }`
3. `data: [ 'list_event', {...} ]`
4. `data: { type: 'audioEvent', jsonData: { audioPcm: [...] } }`

### Event Type Mapping

| Event Type String | Parsed Property | Model |
|---|---|---|
| `listEvent` / `list_event` | `event.listEvent` | `List_ItemEvent` |
| `textEvent` / `text_event` | `event.textEvent` | `Text_ItemEvent` |
| `sysEvent` / `sys_event` | `event.sysEvent` | `Sys_ItemEvent` |
| `audioEvent` / `audio_event` | `event.audioEvent` | `{ audioPcm: Uint8Array }` |

### OS Event Types (`OsEventTypeList`)

| Value | Name | Description |
|---|---|---|
| 0 | `CLICK_EVENT` | Single click/tap |
| 1 | `SCROLL_TOP_EVENT` | Scrolled to top |
| 2 | `SCROLL_BOTTOM_EVENT` | Scrolled to bottom |
| 3 | `DOUBLE_CLICK_EVENT` | Double click/tap |
| 4 | `FOREGROUND_ENTER_EVENT` | App/container entered foreground |
| 5 | `FOREGROUND_EXIT_EVENT` | App/container exited foreground |
| 6 | `ABNORMAL_EXIT_EVENT` | Abnormal/unexpected exit |

---

## 15. Bridge Initialization Flow

1. SDK creates `EvenAppBridge` singleton via `new EvenAppBridge()` in constructor
2. `init()` is called automatically:
   - Exposes instance to `window.EvenAppBridge`
   - Listens for DOM readiness (`DOMContentLoaded` or checks `document.readyState`)
   - Exposes `window._evenAppHandleMessage` for the host to call
   - Marks `_ready = true` when DOM is loaded
3. `dispatchReadyEvent()` fires `BridgeEvent.BridgeReady` (`'evenAppBridgeReady'`) as a custom `Event` on `window`
4. `waitForEvenAppBridge()`:
   - If bridge exists and is ready → returns immediately
   - If not ready → listens for `'evenAppBridgeReady'` event
   - Fallback: if bridge exists but not marked ready, waits 100ms and retries

### Message Reception

The host communicates via:
- **Primary:** `window._listenEvenAppMessage(message)` — preferred path
- **Secondary:** `window._evenAppHandleMessage(message)` — exposed during init

Both routes end up at `handleEvenAppMessage()`.

### Message Sending

Web → App uses `flutter_inappwebview.callHandler('evenAppMessage', message)`. The host registers a handler named `'evenAppMessage'` and returns results synchronously (Promise-based on the JS side).

---

## 16. All Exported Symbols

The complete list of exports from the package (from the `.d.ts` file):

### Classes
- `EvenAppBridge`
- `UserInfo`
- `DeviceInfo`
- `DeviceStatus`
- `ListContainerProperty`
- `ListItemContainerProperty`
- `TextContainerProperty`
- `TextContainerUpgrade`
- `ImageContainerProperty`
- `ImageRawDataUpdate`
- `ImageRawDataUpdateFields`
- `CreateStartUpPageContainer`
- `RebuildPageContainer`
- `ShutDownContaniner`
- `List_ItemEvent`
- `Text_ItemEvent`
- `Sys_ItemEvent`

### Enums
- `EvenAppMethod`
- `EvenAppMessageType`
- `BridgeEvent`
- `DeviceConnectType`
- `DeviceModel`
- `StartUpPageCreateResult`
- `ImageRawDataUpdateResult`
- `EvenHubErrorCodeName`
- `EvenHubEventType`
- `OsEventTypeList`

### Type Aliases
- `EvenHubEvent`
- `EvenHubEventPayload`
- `AudioEventPayload`
- `EvenHubErrorCode`
- `JsonRecord`

### Functions
- `waitForEvenAppBridge()`
- `createDefaultEvenHubEvent()`
- `evenHubEventFromJson(input: any)`
- `isObjectRecord(v: any)`
- `pick<T>(obj, ...keys)`
- `pickLoose<T>(obj, ...keys)`
- `normalizeLooseKey(key: string)`
- `toNumber(v: any)`
- `toString(v: any)`
- `readNumber(obj, ...keys)`
- `readString(obj, ...keys)`
- `toObjectRecord(v: any)`
- `bytesToJson(v: any)`

---

## 17. Platform & Environment

### Runtime Environment

This SDK is designed to run **inside the Even App's WebView** (powered by `flutter_inappwebview`). It relies on:
- `window` global object (browser/WebView environment)
- `window.flutter_inappwebview.callHandler()` for posting messages to the host app
- Custom DOM events (`CustomEvent`) for bridge readiness
- `window._evenAppHandleMessage` / `window._listenEvenAppMessage` for host-to-web communication

### Supported Devices

| Device | Model Enum | Type |
|---|---|---|
| G1 Glasses | `DeviceModel.G1` | Glasses |
| G2 Glasses | `DeviceModel.G2` | Glasses |
| Ring1 | `DeviceModel.Ring1` | Ring |

### Node.js Support

The `engines` field specifies `^20.0.0 || >=22.0.0`, but this SDK is fundamentally a **browser/WebView SDK**. Node.js is required only for the build toolchain.

---

## 18. Known Limitations & Gotchas

1. **`createStartUpPageContainer` can only be called once.** All subsequent page updates must use `rebuildPageContainer`. Calling it again has no effect.

2. **Maximum 4 containers per page.** `containerTotalNum` must not exceed 4.

3. **Only one container per page can have `isEventCapture=1`.** All others must be `0`.

4. **Image data cannot be sent during startup.** Create the image container first, then call `updateImageRawData` after `createStartUpPageContainer` succeeds.

5. **Image transmissions must be sequential (queue mode).** Do not send multiple images concurrently — wait for each to return before sending the next.

6. **Limited glasses memory.** Avoid sending images too frequently.

7. **Images should be simple, single-color schemes.** Complex images may not render well (converted to gray4).

8. **`borderRdaius` is a typo** in the SDK (should be "radius"). This matches the protobuf definition, so it's intentional.

9. **`ShutDownContaniner` is a typo** (should be "Container"). This is the class name in the SDK.

10. **`APP_REQUEST_REBUILD_PAGE_FAILD`** is a typo (should be "FAILED"). Matches the PB definition.

11. **`APP_REQUEST_CREATE_INVAILD_CONTAINER`** is a typo (should be "INVALID"). Matches the PB definition.

12. **Image events (`imgEvent`) are defined in the protocol but not yet included** in the current SDK's type definitions.

13. **Audio `audioControl` requires `createStartUpPageContainer` to be called first.** Opening/closing the microphone will fail otherwise.

14. **Text content limits differ by context:**
    - During startup (`TextContainerProperty.content`): max 1000 characters
    - During upgrade (`TextContainerUpgrade.content`): max 2000 characters

15. **The `DeviceModel.fromString()` defaults to `G1`** if the string doesn't match any known model.

16. **Host may return enum values as either strings or numbers.** The SDK's `normalize()` and `fromInt()` helpers handle both, but be aware of this inconsistency.

17. **The dist code is obfuscated** with `javascript-obfuscator`. You cannot read the implementation source from the npm package — only `.d.ts` type definitions are unobfuscated.

18. **No public GitHub repository** is linked. The package has no `repository` field in `package.json`.

19. **Chinese README** (`README.zh-CN.md`) is included in the package at `node_modules/@evenrealities/even_hub_sdk/README.zh-CN.md` but not displayed on the npm website.

20. **EvenHub API uses camelCase naming** but is also compatible with different naming conventions (e.g., `List_Object`, `Container_ID`) from the protobuf definitions.

---

## 19. Changelog

### 0.0.1
- Initial release
- Implemented basic bridge functionality
- Support for device information, user information, local storage
- Support for EvenHub protocol core interfaces
- Support for event listening mechanism

### 0.0.2 – 0.0.7
- No detailed changelog published for these versions (5 total published versions through 0.0.7)

---

## 20. Contact & Contributing

- **Author:** Whiskee
- **Email:** whiskee.chen@evenrealities.com
- **Collaborators:** whiskee.chen, carson.zhu
- **Contributing:** Issues and Pull Requests are welcome (though no public repo is linked)
- **License:** [MIT](https://opensource.org/licenses/MIT)

---

## 21. Complete Import Reference

```typescript
// Core bridge
import {
  EvenAppBridge,
  waitForEvenAppBridge,
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

// Data Models
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

// Types
import type {
  EvenHubEvent,
  EvenHubEventPayload,
  AudioEventPayload,
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

---

*Document generated from NPM package analysis on 2026-02-21. Package version: 0.0.7.*
