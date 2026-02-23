# Phase 0: SDK Verification Spikes

## Phase Goal

Verify critical SDK assumptions before any implementation begins. These spikes resolve uncertainties that would cause rework if discovered mid-build.

## Prerequisites

- Node.js ≥ 18 installed
- `@evenrealities/even_hub_sdk` installable via npm
- `@evenrealities/evenhub-cli` installed globally
- EvenHub simulator (optional but preferred for visual verification)

## Spike Breakdown

### P0.1 — Verify `setPageFlip()` Exists (Critical — review A.2, D.1)

**Risk:** The entire page 1 feature (P4.8), truncation UX (P3.7), and double-tap interaction depend on `bridge.setPageFlip(0|1)`. This method appears in design docs but is NOT listed in the SDK's 15 documented public methods.

**Method:**
1. Create a minimal test app with `@evenrealities/even_hub_sdk`
2. Initialize `EvenAppBridge` via `waitForEvenAppBridge()`
3. Call `createStartUpPageContainer` with 2 pages of containers
4. Attempt `bridge.setPageFlip(1)` and `bridge.setPageFlip(0)`
5. Check: does the method exist? Does it throw? Does it actually flip pages?
6. Also check TypeScript types: does the SDK export `setPageFlip` in its type declarations?

**Expected outputs:**
- `setPageFlip` exists and works → proceed with current page 1 design
- `setPageFlip` exists but is undocumented → proceed but add a note about SDK version coupling
- `setPageFlip` does NOT exist → redesign page 1 as `rebuildPageContainer` swap (Pattern 6 from g2-display-ui skill), remove all `setPageFlip` references from docs

**Owner:** g2-development  
**Complexity:** S (~30 min)

### P0.2 — Verify `app.json` Manifest Schema (Critical — review A.1, D.2)

**Risk:** Design docs use `appId`, `appName`, `entry`. The g2-dev-toolchain skill documents `package_id`, `name`, `entrypoint`. Wrong fields = `evenhub pack` fails.

**Method:**
1. Create a minimal `app.json` with the g2-dev-toolchain skill schema:
   ```json
   {"package_id": "com.g2openclaw.spike", "name": "Spike", "entrypoint": "index.html", "edition": "202601", "min_app_version": "1.0.0", "author": "test", "permissions": []}
   ```
2. Create a minimal `index.html`
3. Run `evenhub pack app.json . -o spike.ehpk`
4. Record which fields are required, which are optional, what errors appear for wrong fields
5. Try with the old schema (`appId`, `appName`, `entry`) and record the error

**Expected outputs:**
- Confirmed required fields for `app.json`
- Confirmed `evenhub pack` command syntax
- Document any additional required fields not in either source

**Owner:** g2-development  
**Complexity:** S (~15 min)

### P0.3 — Verify `content` vs `textContent` Field Name (Important — review A.4)

**Risk:** SDK data model table says `content`. Display skill examples use `textContent`. Wrong field = blank display.

**Method:**
1. In the spike app from P0.1, create a `TextContainerProperty` with `content: "test"`
2. Call `rebuildPageContainer`
3. Check: does text appear on simulator?
4. Change to `textContent: "test"` — does that also work?
5. Check SDK TypeScript type declarations for the canonical field name

**Expected outputs:**
- Confirmed canonical field name
- Whether both work (via `pickLoose()` normalization) or only one works

**Owner:** g2-development  
**Complexity:** S (~10 min, combined with P0.1)

### P0.4 — Verify `audioPcm` Type (Important — review A.3)

**Risk:** Design docs say `Int8Array`. SDK bridge skill says `Uint8Array`. Wrong type = TypeScript errors.

**Method:**
1. In the spike app, register `bridge.onEvenHubEvent()` and call `bridge.audioControl(true)`
2. In the event handler, log `typeof event.audioEvent?.audioPcm` and `event.audioEvent?.audioPcm?.constructor?.name`
3. On simulator: verify the logged type
4. Check SDK TypeScript type declarations

**Expected outputs:**
- Confirmed runtime type of `audioPcm`
- Confirmed TypeScript declared type

**Owner:** g2-development  
**Complexity:** S (~10 min, combined with P0.1)

### P0.5 — Verify `createStartUpPageContainer` Return Value Checking (Minor — review A.6)

**Method:**
1. In the spike app, call `createStartUpPageContainer` with valid containers
2. Log the return value — confirm it returns `StartUpPageCreateResult.success` (0)
3. Call with invalid containers (>2000 bytes, wrong structure) — log return value
4. Confirm all enum values exist: `success`, `invalid`, `oversize`, `outOfMemory`

**Expected outputs:**
- Confirmed return value is a number matching `StartUpPageCreateResult` enum
- Documented what triggers each non-success value

**Owner:** g2-development  
**Complexity:** S (~10 min, combined with P0.1)

## Parallel Execution Plan

All spikes use the same test app scaffold. P0.2 is independent. P0.1/P0.3/P0.4/P0.5 share one app.

```
── Time →

Agent A (g2-development):
  [P0.1 + P0.3 + P0.4 + P0.5 spike app] ──→ results
              ~45 min

Agent B (g2-development):
  [P0.2 app.json manifest + evenhub pack] ──→ results
              ~15 min
```

## Integration Checkpoint

Before Phase 1, all of the following must be resolved:

- [ ] `setPageFlip` status: exists / doesn't exist → if not, update design docs 01-04 to use `rebuildPageContainer` swap
- [ ] `app.json` canonical schema documented with all required fields
- [ ] `content` vs `textContent` canonical field name confirmed
- [ ] `audioPcm` runtime type confirmed (`Uint8Array` expected)
- [ ] `createStartUpPageContainer` return values confirmed
- [ ] All findings written to `.archive/implementation/phase-0-results.md`
