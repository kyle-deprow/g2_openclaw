# Phase 0 — SDK Verification Findings

**SDK:** `@evenrealities/even_hub_sdk` v0.0.7  
**Date:** 2026-02-22  
**Method:** TypeScript strict-mode compilation (`tsc --noEmit`, `skipLibCheck: false`) + direct `.d.ts` inspection  
**Result:** `npx tsc --noEmit` exited with code 0 (zero errors)

---

## Questions & Answers

### Q1: Does `setPageFlip` exist on `EvenAppBridge`?

**Answer: NO — `setPageFlip` does not exist.**

**Evidence:**
- Grep for `setPageFlip`, `pageFlip`, or `PageFlip` in `index.d.ts` returned **zero matches**.
- The `@ts-expect-error` directive on `bridge.setPageFlip(1)` in `verify.ts` correctly suppressed the "property does not exist" error. If the method *did* exist, the `@ts-expect-error` would be unused and `tsc` would report an error. Zero errors confirms the method is absent.
- The conditional type check `'setPageFlip' extends keyof EvenAppBridge ? true : false` resolved to `false` (the assignment `const hasIt: HasSetPageFlip = false` compiled).

**Complete list of public methods on `EvenAppBridge`:**

| Method | Signature |
|---|---|
| `callEvenApp` | `(method: EvenAppMethod \| string, params?: any) => Promise<any>` |
| `getUserInfo` | `() => Promise<UserInfo>` |
| `getDeviceInfo` | `() => Promise<DeviceInfo \| null>` |
| `setLocalStorage` | `(key: string, value: string) => Promise<boolean>` |
| `getLocalStorage` | `(key: string) => Promise<string>` |
| `createStartUpPageContainer` | `(container: CreateStartUpPageContainer) => Promise<StartUpPageCreateResult>` |
| `rebuildPageContainer` | `(container: RebuildPageContainer) => Promise<boolean>` |
| `updateImageRawData` | `(data: ImageRawDataUpdate) => Promise<ImageRawDataUpdateResult>` |
| `textContainerUpgrade` | `(container: TextContainerUpgrade) => Promise<boolean>` |
| `audioControl` | `(isOpen: boolean) => Promise<boolean>` |
| `shutDownPageContainer` | `(exitMode?: number) => Promise<boolean>` |
| `onDeviceStatusChanged` | `(callback: (status: DeviceStatus) => void) => () => void` |
| `onEvenHubEvent` | `(callback: (event: EvenHubEvent) => void) => () => void` |
| `handleEvenAppMessage` | `(message: string \| EvenAppMessage) => void` |

**Impact:** Page-flip must be implemented at the application layer (swapping container content via `rebuildPageContainer` or `textContainerUpgrade`), not via a native SDK method.

---

### Q2: Is `content` the correct field on `TextContainerProperty`?

**Answer: YES — the field is `content`, not `textContent`.**

**Evidence:**
- `index.d.ts` line ~388: `content?: string;` declared on `TextContainerProperty`.
- `textContent` does not appear anywhere in `index.d.ts` or `index.js` (confirmed by prior grep).
- The `@ts-expect-error` on `container.textContent` correctly suppressed the error (meaning `textContent` does NOT exist). If it did exist, `tsc` would report an unused `@ts-expect-error`.
- `container.content` compiled without error.

**Impact:** None — our docs were correct. Use `content` everywhere.

---

### Q3: Is `audioPcm` typed as `Uint8Array`?

**Answer: YES — `audioPcm` is `Uint8Array`.**

**Evidence:**
- `index.d.ts` line ~832:
  ```typescript
  type AudioEventPayload = {
      audioPcm: Uint8Array;
  };
  ```
- The assignment `const isUint8: Uint8Array = pcm;` compiled without error, confirming the type.

**Impact:** None — we can use `Uint8Array` directly. No need for `Int8Array` conversion.

---

### Q4: Does `StartUpPageCreateResult` have the expected enum values?

**Answer: YES — exact match: `success=0`, `invalid=1`, `oversize=2`, `outOfMemory=3`.**

**Evidence:**
- `index.d.ts` lines ~568-575:
  ```typescript
  declare enum StartUpPageCreateResult {
      success = 0,
      invalid = 1,
      oversize = 2,
      outOfMemory = 3
  }
  ```
- The verify.ts literal type checks (`const s: 0 = StartUpPageCreateResult.success`, etc.) all compiled.
- Namespace also provides `fromInt(value: number)` and `normalize(raw: any)` helpers.

**Impact:** None — values match expectations.

---

### Q5: Is `waitForEvenAppBridge` exported?

**Answer: YES.**

**Evidence:**
- `index.d.ts` line ~1190: `declare function waitForEvenAppBridge(): Promise<EvenAppBridge>;`
- Export list line ~1210 includes `waitForEvenAppBridge`.
- It was imported and used in `verify.ts` without error.

**Impact:** None — this is the recommended entry point for bridge initialization.

---

## Additional Discoveries

### D1: `borderRdaius` typo is real and intentional

The SDK uses `borderRdaius` (not `borderRadius`) on both `ListContainerProperty` and `TextContainerProperty`. The comment says:

> 对应 PB：Border_Rdaius（注意 PB 本身拼写为 Rdaius）

This is a known typo inherited from the protobuf definition. **We must use `borderRdaius` (misspelled) in our code.**

### D2: `ShutDownContaniner` class name typo

The class is named `ShutDownContaniner` (misspelling of "Container"). This is the actual exported name we must use.

### D3: `OsEventTypeList` enum values

```typescript
declare enum OsEventTypeList {
    CLICK_EVENT = 0,
    SCROLL_TOP_EVENT = 1,
    SCROLL_BOTTOM_EVENT = 2,
    DOUBLE_CLICK_EVENT = 3,      // NOT "DOUBLE_CLICK" — note the _EVENT suffix
    FOREGROUND_ENTER_EVENT = 4,
    FOREGROUND_EXIT_EVENT = 5,
    ABNORMAL_EXIT_EVENT = 6
}
```

All event types use `_EVENT` suffix. The ring single-tap maps to `CLICK_EVENT` (0) and double-tap to `DOUBLE_CLICK_EVENT` (3).

### D4: `EvenHubErrorCodeName` has a typo: `FAILD`

`APP_REQUEST_REBUILD_PAGE_FAILD` — note "FAILD" not "FAILED". Inherited from the PB enum.

### D5: `ImageRawDataUpdateResult` uses string values, not numbers

```typescript
declare enum ImageRawDataUpdateResult {
    success = "success",
    imageException = "imageException",
    imageSizeInvalid = "imageSizeInvalid",
    imageToGray4Failed = "imageToGray4Failed",
    sendFailed = "sendFailed"
}
```

Unlike `StartUpPageCreateResult` (which uses `0/1/2/3`), this enum uses string values. The namespace provides `valueCode()` to convert to int if needed.

### D6: `EvenHubEvent` is a type, not a class

```typescript
type EvenHubEvent = {
    listEvent?: List_ItemEvent;
    textEvent?: Text_ItemEvent;
    sysEvent?: Sys_ItemEvent;
    audioEvent?: AudioEventPayload;
    jsonData?: Record<string, any>;
};
```

Dispatch pattern: check which property is non-null to determine event type. Helper: `evenHubEventFromJson(input)`.

### D7: `DeviceModel` enum includes G2

```typescript
declare enum DeviceModel {
    G1 = "g1",
    G2 = "g2",
    Ring1 = "ring1"
}
```

With helpers: `DeviceModel.isGlasses(model)` and `DeviceModel.isRing(model)`.

### D8: Bridge is singleton + ready pattern

```typescript
EvenAppBridge.getInstance()          // get singleton
waitForEvenAppBridge()               // async wait for ready state
bridge.ready                         // boolean check
```

`waitForEvenAppBridge()` is the recommended way to get a ready bridge instance.

### D9: `EvenAppMethod` enum lists all valid method names

```typescript
declare enum EvenAppMethod {
    GetUserInfo = "getUserInfo",
    GetGlassesInfo = "getGlassesInfo",
    SetLocalStorage = "setLocalStorage",
    GetLocalStorage = "getLocalStorage",
    CreateStartUpPageContainer = "createStartUpPageContainer",
    RebuildPageContainer = "rebuildPageContainer",
    UpdateImageRawData = "updateImageRawData",
    TextContainerUpgrade = "textContainerUpgrade",
    AudioControl = "audioControl",
    ShutDownPageContainer = "shutDownPageContainer"
}
```

Note: `callEvenApp` also accepts arbitrary `string`, so undocumented methods could be called — but there's no `setPageFlip` equivalent.

### D10: JSON key compatibility

The SDK supports both camelCase and proto-style keys (e.g., `containerID` / `Container_ID`). The `pickLoose` / `normalizeLooseKey` utilities handle this. We should always use camelCase on our side.

---

## Summary Table

| # | Question | Answer | Impact |
|---|---|---|---|
| Q1 | `setPageFlip` exists? | **NO** | Must implement page-flip in app layer |
| Q2 | `content` is correct field? | **YES** | None |
| Q3 | `audioPcm` is `Uint8Array`? | **YES** | None |
| Q4 | `StartUpPageCreateResult` values? | **Match (0-3)** | None |
| Q5 | `waitForEvenAppBridge` exported? | **YES** | None |

## Typos to Remember

| SDK Identifier | Expected Spelling | Notes |
|---|---|---|
| `borderRdaius` | `borderRadius` | Must use misspelled version |
| `ShutDownContaniner` | `ShutDownContainer` | Must use misspelled version |
| `APP_REQUEST_REBUILD_PAGE_FAILD` | `...FAILED` | Must use misspelled version |

---

## Impact on Implementation Plans

1. **Page-flip (Q1):** The absence of `setPageFlip` is the only finding that affects implementation. Page navigation must be built at the application layer using `rebuildPageContainer` (full page swap) or `textContainerUpgrade` (partial text update). This was already the suspected approach and is now confirmed as the *only* approach.

2. **All other findings confirm existing assumptions.** No changes needed for content fields, audio types, or enum values.

3. **Watch the typos.** Any abstraction layer we build should use correct spelling internally, mapping to the SDK's misspelled identifiers at the boundary.
