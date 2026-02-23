# Phase 0 — SDK Verification Spike Results

**SDK:** `@evenrealities/even_hub_sdk@0.0.7`  
**Date:** 2026-02-22  
**Method:** TypeScript declaration file inspection + `tsc --noEmit` type-checking  
**Spike location:** `.archive/spikes/sdk-verify/`

---

## P0.1 — Does `setPageFlip` exist?

**Status:** REJECTED

**Evidence:**

1. **Type declarations:** `setPageFlip` does not appear anywhere in `dist/index.d.ts` (1,210 lines).
2. **EvenAppMethod enum** has exactly 10 values — none related to page flipping:
   - `GetUserInfo`, `GetGlassesInfo`, `SetLocalStorage`, `GetLocalStorage`,
     `CreateStartUpPageContainer`, `RebuildPageContainer`, `UpdateImageRawData`,
     `TextContainerUpgrade`, `AudioControl`, `ShutDownPageContainer`
3. **JS source search:** `grep -i 'pageflip\|page_flip\|setpage\|flipPage'` against both `index.js` and `index.cjs` returned **zero matches**.
4. **Type-check proof:** `await bridge.setPageFlip(1)` produces:
   ```
   error TS2339: Property 'setPageFlip' does not exist on type 'EvenAppBridge'.
   ```

**Complete list of public `EvenAppBridge` methods:**

| Method | Signature |
|---|---|
| `getInstance` | `static getInstance(): EvenAppBridge` |
| `handleEvenAppMessage` | `(message: string \| EvenAppMessage): void` |
| `callEvenApp` | `(method: EvenAppMethod \| string, params?: any): Promise<any>` |
| `getUserInfo` | `(): Promise<UserInfo>` |
| `getDeviceInfo` | `(): Promise<DeviceInfo \| null>` |
| `setLocalStorage` | `(key: string, value: string): Promise<boolean>` |
| `getLocalStorage` | `(key: string): Promise<string>` |
| `createStartUpPageContainer` | `(container: CreateStartUpPageContainer): Promise<StartUpPageCreateResult>` |
| `rebuildPageContainer` | `(container: RebuildPageContainer): Promise<boolean>` |
| `updateImageRawData` | `(data: ImageRawDataUpdate): Promise<ImageRawDataUpdateResult>` |
| `textContainerUpgrade` | `(container: TextContainerUpgrade): Promise<boolean>` |
| `audioControl` | `(isOpen: boolean): Promise<boolean>` |
| `shutDownPageContainer` | `(exitMode?: number): Promise<boolean>` |
| `onDeviceStatusChanged` | `(callback: (status: DeviceStatus) => void): () => void` |
| `onEvenHubEvent` | `(callback: (event: EvenHubEvent) => void): () => void` |

**Potential alternatives for page management:**
- `rebuildPageContainer()` — replaces the entire page layout. This is the ONLY way to change page structure after initial creation.
- `textContainerUpgrade()` — updates text in-place within an existing container (more efficient for text-only changes).
- There is **no** concept of "pages" as a multi-page stack. The SDK models a single active page with up to 4 containers. "Page flipping" must be implemented by the app via `rebuildPageContainer()`.

**Impact:** Any design assuming `setPageFlip` exists must be reworked. Page transitions require calling `rebuildPageContainer()` with a new container layout. The app must manage its own page state (which page is "current") and rebuild accordingly on navigation events.

**Recommendation:** Implement a `PageManager` abstraction that:
1. Maintains a registry of named page layouts (each a `RebuildPageContainer` config).
2. Exposes a `switchPage(name)` method that calls `rebuildPageContainer()`.
3. Tracks the current page for back-navigation via `DOUBLE_CLICK_EVENT`.

---

## P0.3 — `content` vs `textContent`

**Status:** REJECTED (the field is `content`, NOT `textContent`)

**Evidence:**

1. **Type declaration for `TextContainerProperty`:**
   ```typescript
   declare class TextContainerProperty {
     // ... positioning fields ...
     /** 对应 PB：Content */
     content?: string;
     // No 'textContent' field exists
   }
   ```
2. **`TextContainerUpgrade`** also uses `content`:
   ```typescript
   declare class TextContainerUpgrade {
     content?: string;
     // ...
   }
   ```
3. **String search:** `grep -ci 'textcontent'` returns **0 matches** in both `index.d.ts` and `index.js`.
4. **`pickLoose` normalization** removes underscores and lowercases — `content` → `content`, `textContent` → `textcontent`. These do NOT match, so `pickLoose` would NOT alias `textContent` to `content`.
5. **Type-check proof:** `container.textContent` produces:
   ```
   error TS2339: Property 'textContent' does not exist on type 'TextContainerProperty'.
   ```

**Impact:** The skill documentation file `.github/skills/g2-display-ui/SKILL.md` uses `textContent` in all its examples — this is **incorrect** and will cause silent bugs (text won't render because the field name is wrong). The protobuf field name is `Content`, and the SDK maps it to camelCase `content`.

**Recommendation:**
1. Fix all `textContent` references to `content` in design docs, skill files, and any code templates.
2. In `TextContainerUpgrade`, the field is also `content` (not `textContent`).

---

## P0.4 — `audioPcm` type

**Status:** CONFIRMED

**Evidence:**

1. **Type declaration:**
   ```typescript
   type AudioEventPayload = {
     audioPcm: Uint8Array;
   };
   ```
2. **Type-check proof:** Assigning `event.audioEvent.audioPcm` to a `Uint8Array` variable compiles without error.
3. The SDK comment notes: "宿主侧 Uint8List 经 JSON 后多为 number[] 或 base64 字符串" — meaning the host sends `number[]` or base64, but the SDK normalizes it to `Uint8Array` before delivering to the web app.

**Impact:** None. The declared type matches our design assumption. Audio PCM data arrives as `Uint8Array`.

**Recommendation:** No changes needed. Use `Uint8Array` as planned.

---

## P0.5 — `createStartUpPageContainer` return type & `StartUpPageCreateResult` enum

**Status:** CONFIRMED

**Evidence:**

1. **Method signature:**
   ```typescript
   createStartUpPageContainer(container: CreateStartUpPageContainer): Promise<StartUpPageCreateResult>
   ```
2. **Enum definition:**
   ```typescript
   declare enum StartUpPageCreateResult {
     success = 0,
     invalid = 1,
     oversize = 2,
     outOfMemory = 3
   }
   ```
3. **Type-check proof:** Assigning each enum value to its expected literal type (`0`, `1`, `2`, `3`) compiles without error.
4. **Namespace utilities:** `StartUpPageCreateResult.fromInt(n)` and `StartUpPageCreateResult.normalize(raw)` exist for robust host-value handling.

**Impact:** None. The enum values match our design assumptions exactly.

**Recommendation:** No changes needed. Use `StartUpPageCreateResult` as planned. Consider using `StartUpPageCreateResult.normalize()` for defensive parsing of host responses.

---

## Summary

| Spike | Status | Action Required |
|-------|--------|-----------------|
| P0.1 — `setPageFlip` | **REJECTED** | Implement page management via `rebuildPageContainer()` + app-side state |
| P0.3 — `content` vs `textContent` | **REJECTED** | Use `content` everywhere; fix skill docs that say `textContent` |
| P0.4 — `audioPcm` type | **CONFIRMED** | None |
| P0.5 — `StartUpPageCreateResult` | **CONFIRMED** | None |

### Critical Action Items

1. **P0.1:** Design a `PageManager` that wraps `rebuildPageContainer()` for page transitions. No native multi-page support exists.
2. **P0.3:** Global find-and-replace `textContent` → `content` in all G2-related design docs and code. The skill file `.github/skills/g2-display-ui/SKILL.md` has this wrong in multiple examples.
