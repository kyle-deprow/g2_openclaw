# Implementation Plans — Technical Review

**Reviewer:** Senior Technical Reviewer  
**Date:** 2026-02-22  
**Documents Reviewed:** Design docs 01-05, Implementation phases 1-4, Skills (g2-display-ui, g2-sdk-bridge, g2-events-input, g2-dev-toolchain), copilot-instructions.md

---

## Grade: B+

The plans are *well-structured, thorough on happy-path coverage, and correctly phased*. Every protocol frame type, every state machine transition, every display layout from the design docs has a corresponding task. The parallel execution plans are realistic and dependency arrows are mostly correct. What pulls it down from an A: several SDK/platform discrepancies between the design docs and the actual skills files would cause silent build or runtime failures; the `setPageFlip` method underpinning the entire page 1 feature may not exist; `app.json` manifest fields are wrong; and app-side testing is completely absent.

---

## Executive Summary

The four-phase implementation plan covers ~95% of design doc requirements with correct phasing (plumbing → audio → AI → polish). Protocol frame types, state machine transitions, all 14 error scenarios, all 9 display states, and all config parameters are accounted for. However, the plans inherit several design doc assumptions that contradict the actual SDK reference: the `app.json` manifest schema uses wrong field names, the `setPageFlip()` method isn't documented in the SDK, and the `audioPcm` type is listed as `Int8Array` when the SDK delivers `Uint8Array`. These aren't cosmetic — they'll cause build failures or silent runtime bugs. Additionally, there are no TypeScript tests anywhere, no CI/CD plan, no mock OpenClaw server for offline development, and the `content` vs `textContent` field ambiguity in the SDK needs resolution before any display code ships.

---

## Section A: Coverage Gaps

### A.1 — `app.json` Manifest Schema Is Wrong (Critical)

**Design docs** (03 §9) and **P1.4** use:
```json
{"appId": "com.g2openclaw.app", "appName": "OpenClaw", "entry": "index.html", "icon": "assets/icon.bmp"}
```

The **g2-dev-toolchain skill** documents the actual schema:
```json
{"package_id": "com.g2openclaw.app", "name": "OpenClaw", "entrypoint": "index.html", "edition": "202601", ...}
```

Every field name is different: `appId` → `package_id`, `appName` → `name`, `entry` → `entrypoint`. The skill also requires additional fields (`edition`, `min_app_version`, `author`, `permissions`). The `evenhub pack` command will reject the manifest as written in the plans.

**Recommended fix:** Update P1.4 acceptance criteria with the correct `app.json` field names from the g2-dev-toolchain skill. Add all required fields.

---

### A.2 — `setPageFlip()` May Not Exist in the SDK (Critical)

P4.8 (Page 1 Detail View) and the entire page flip interaction model rely on `bridge.setPageFlip(0|1)`. This method appears in design docs 01 §5, 03, and 04 §7.4, but is **NOT listed in the g2-sdk-bridge skill's complete public method reference** (15 methods documented, none named `setPageFlip`).

The g2-display-ui skill's "Pattern 6: Page Flipping for Long Text" uses `rebuildPageContainer` to swap content — it never calls `setPageFlip`. This strongly suggests the method either doesn't exist in SDK v0.0.7, or is undocumented and unreliable.

If `setPageFlip` doesn't exist, the following plan items are broken:
- P4.8 (page 1 detail view)
- P3.7 (truncation with "double-tap for more")
- P2.5 (DOUBLE_CLICK → page flip in `displaying` state)
- All page 1 container specs from doc 04 §5

**Recommended fix:** Add a **Phase 0 spike task** (30 min) that verifies `setPageFlip` works on the simulator. If it doesn't, redesign page 1 as a `rebuildPageContainer` swap (which means you lose the native dual-page model and must manage page state yourself). Update all dependent tasks accordingly.

---

### A.3 — `audioPcm` Type Mismatch: `Int8Array` vs `Uint8Array` (Important)

Design doc 03 §6 and P2.4 reference `Int8Array` for PCM chunks from `onMicData`. Both the g2-sdk-bridge skill (`AudioEventPayload.audioPcm: Uint8Array`) and the g2-events-input skill explicitly state the type is `Uint8Array`.

This matters because `Int8Array.buffer` and `Uint8Array.buffer` return the same `ArrayBuffer`, so raw byte forwarding works either way — but TypeScript type checking will fail if the audio.ts code types the variable as `Int8Array` when the SDK delivers `Uint8Array`. The developer will either add an incorrect cast or see a type error.

**Recommended fix:** Update P2.4 to use `Uint8Array` consistently, matching the SDK type declarations.

---

### A.4 — `content` vs `textContent` Field Ambiguity (Important)

The g2-sdk-bridge skill's data model table lists the TextContainerProperty field as `content`. The g2-display-ui skill examples use `textContent`. Doc 04 §7.1 uses `content`. The SDK's `pickLoose()` normalization may accept both, but relying on undocumented magic for a critical field is risky.

**Recommended fix:** Use `content` (matching the SDK data model table and design docs). Add a note in P1.8 that `textContent` may also work due to `pickLoose()` but is not the canonical field name.

---

### A.5 — `waitForEvenAppBridge()` Not Used in Plans (Important)

P1.9 uses `EvenAppBridge.getInstance()` for initialization. The g2-sdk-bridge skill explicitly recommends `waitForEvenAppBridge()` as the reliable approach — it handles timing, DOM readiness, and bridge initialization races. Using `getInstance()` directly risks accessing the bridge before it's ready, particularly on slower devices or flaky BLE connections.

**Recommended fix:** Update P1.9 to use `await waitForEvenAppBridge()` instead of `EvenAppBridge.getInstance()`.

---

### A.6 — `createStartUpPageContainer` Result Not Checked (Minor)

The SDK returns `StartUpPageCreateResult` (0=success, 1=invalid, 2=oversize, 3=outOfMemory). P1.9 calls `createStartUpPageContainer` but none of the plans check the return value. An `invalid` or `outOfMemory` result would silently fail, and all subsequent display and audio calls would break.

**Recommended fix:** Add result checking to P1.9. If result !== `StartUpPageCreateResult.success`, display a diagnostic error in the browser console and halt initialization.

---

### A.7 — No `shutDownPageContainer` Call on App Exit (Minor)

The skills document `bridge.shutDownPageContainer()` for graceful display cleanup. Neither the design docs nor the plans call it anywhere — not on `ABNORMAL_EXIT_EVENT`, not on app teardown, not on WebSocket permanent disconnect. Failing to call it may leave stale content on the glasses display after the app is closed.

**Recommended fix:** Add `shutDownPageContainer(0)` to the `ABNORMAL_EXIT_EVENT` handler in P2.5 and to any future app cleanup path.

---

### A.8 — `onDeviceStatusChanged` Never Used (Minor)

The g2-sdk-bridge skill documents `onDeviceStatusChanged(callback)` for monitoring battery, wearing state, and connection status. The design docs mention BLE disconnect as a scenario but neither the plans nor docs use this callback. It would be useful for:
- Showing battery level in the loading/idle state
- Detecting glasses removal (pause mic/display)
- Logging connection quality

**Recommended fix:** Add a Phase 4 task to subscribe to `onDeviceStatusChanged` and log device status. Low priority but free observability.

---

### A.9 — OpenClaw Agent Configuration Missing from Plans (Important)

Review doc (05) §7.1 identifies this gap and it's still not addressed. No plan task creates:
- A G2-specific agent identity (`SOUL.md` with "keep responses under 150 words")
- Tool restrictions (browser/canvas tools are useless on glasses)
- A system prompt tailored for the constrained display

Without a concise-response system prompt, LLM answers will routinely exceed 2000 characters and every response will be truncated on page 0.

**Recommended fix:** Add a Phase 3 task (P3.2.5) to create the OpenClaw agent configuration: session key mapping, system prompt with length constraints, tool allow/deny list. This directly impacts the viability of P3.7 (truncation) — if responses are 50-150 words, truncation is rarely needed.

---

### A.10 — `EvenHubSDK` vs `EvenAppBridge` Import Confusion (Minor)

The g2-dev-toolchain skill's `src/main.ts` template uses `import { EvenHubSDK } from "@evenrealities/even_hub_sdk"`, while the g2-sdk-bridge skill uses `import { EvenAppBridge, waitForEvenAppBridge } from '@evenrealities/even_hub_sdk'`. The plans reference `EvenAppBridge`, which matches the SDK bridge skill (the authoritative reference). Just noting this to avoid confusion during implementation.

**Recommended fix:** None needed — plans use the correct import. But note the discrepancy in skills files.

---

### A.11 — SDK Import Path Inconsistency in Skills (Minor)

The three skills use different import paths: `'@evenrealities/even_hub_sdk'` (sdk-bridge), `'even-glasses'` (display-ui), `'even-app-bridge'` (events-input). Only `'@evenrealities/even_hub_sdk'` is the real npm package. The plans correctly use the real package name.

**Recommended fix:** None for plans. Skills should be updated for consistency (separate concern).

---

## Section B: Dependency & Ordering Issues

### B.1 — P2.6 Has a False Dependency on P2.4 (Minor)

P2.6 (recording + transcribing display states) is listed as depending on P2.4 (audio.ts) with rationale "needs to know audio.ts interface for level bar updates." But the recording display layout (doc 04 §4.2) renders a static Unicode block bar (`████████░░░░░░░░`). The audio level bar updates mentioned in the notes ("updated via `textContainerUpgrade` from PCM RMS") are a nice-to-have feature not actually specified anywhere in the acceptance criteria for P2.6.

**Recommended fix:** Remove private dependency. P2.6 can start immediately alongside P2.4 and P2.5, increasing parallelism in Phase 2.

---

### B.2 — P3.6 Has a False Dependency on P3.4 (Minor)

P3.6 (delta buffering during layout transitions) depends on P3.4 (thinking display) with rationale "needs thinking→streaming transition." But delta buffering is a display.ts concern that only needs to know that `rebuildPageContainer` is async. It doesn't need the thinking layout itself — it needs the buffering mechanism for *any* layout that transitions via rebuild.

**Recommended fix:** P3.6 can start in parallel with P3.4. The integration (flushing buffered deltas after the thinking→streaming rebuild) happens in P3.8 anyway.

---

### B.3 — P2.2 Could Start in Parallel with P2.1 (Minor)

P2.2 (transcriber) depends on P2.1 (audio buffer) "for numpy array format." But the numpy array format is already fully specified in doc 02 §4.4: `float32, [-1.0, 1.0], from np.int16 / 32768.0`. P2.2 doesn't need P2.1's code — it just needs to accept `np.ndarray` input.

**Recommended fix:** Start P2.2 in parallel with P2.1. This saves ~1.5 hours on the Phase 2 critical path.

---

### B.4 — No Integration Checkpoint Between P3.1 and P3.3 for `phase` vs `status` (Important)

Review doc 05 §2.3 flags that the OpenClaw lifecycle event field might be `phase` (not `status`). P3.1 acceptance criteria correctly state: "**note: field is `phase` not `status`**." But if this is wrong and the actual field IS `status`, the Gateway will never detect response completion. The plans should include a P3.1 sub-checkpoint: "Verify lifecycle event parsing against a real OpenClaw instance before proceeding to P3.3."

**Recommended fix:** Add a verification step in P3.1's acceptance criteria: "Integration test with a real OpenClaw instance confirms lifecycle field name before mocking."

---

### B.5 — Phase 1 Agent C Assignment Creates Idle Time (Minor)

The Phase 1 parallel plan shows Agent C doing P1.4 (scaffold, 1h) → P1.6 (state.ts, 1h) → P1.8 (display.ts, 2h). But P1.4 is also a prerequisite for Agent B's P1.7. If Agent C does P1.4, Agent B blocks for 1h waiting. If Agent B does P1.4, Agent C blocks.

**Recommended fix:** Assign P1.4 explicitly to one agent (it's 30-60min of scaffolding). The other agent can start on P1.6 immediately since it only depends on P1.2 (protocol types), not on the project scaffold — state.ts is pure logic with no SDK imports.

---

## Section C: Implementation Optimizations

### C.1 — Consolidate P4.5 + P4.6 + P4.7 Into One Task (Important)

P4.5 (error display), P4.6 (disconnected display), and P4.7 (loading display) are all "add a `show<State>()` method to display.ts with 4 containers matching doc 04 spec." Combined, they're ~15 lines of container definitions each. The overhead of three separate agent calls (context loading, file reading, verification) far exceeds the marginal work.

**Recommended fix:** Merge into one task: "P4.5 — Remaining Display States (error, disconnected, loading)." Saves ~2 agent calls worth of overhead.

---

### C.2 — Move P3.5 (Markdown Stripping) to Phase 1 (Minor)

`stripMarkdown()` is a pure utility with no dependencies. In Phase 1, mock responses could already contain markdown (since real LLM responses will). Having it available from the start means:
- Phase 1 integration testing is more realistic
- Phase 2 testing with echoed transcription doesn't need it (transcriptions are plain text)
- Phase 3 already has it ready when real LLM responses arrive

**Recommended fix:** Move P3.5 to Phase 1 as P1.2.5 or combine with P1.2. Zero dependency, zero risk.

---

### C.3 — Combine P1.1 + P1.3 (Gateway Protocol + Config + Scaffold) (Minor)

P1.1 (protocol.py) and P1.3 (config + scaffold) are both small backend-python tasks with no dependencies. The scaffold task creates files that protocol.py lives in. Doing them as one task avoids the overhead of a second agent call and ensures consistent project structure.

**Recommended fix:** Merge into "P1.1 — Gateway Scaffold, Protocol Types, and Config."

---

### C.4 — Extract `text()` Helper as Shared Utility Earlier (Minor)

P1.8 implements the `text()` container helper per doc 04 §7.1. But this helper is used in *every* subsequent display task (P2.6, P3.4, P4.5, P4.6, P4.7, P4.8). Extracting it into a `display-helpers.ts` or as a static method in the `Display` class during P1.8 is the plan — just confirming this is correct and no later task should reinvent it.

**Recommended fix:** Confirm in P1.8 that the `text()` and `rebuild()` helpers are exported/accessible for all subsequent display tasks. No change needed if P1.8 already does this.

---

### C.5 — Batch Delta Display Updates Should Have a Timer, Not Just a Count (Minor)

P3.7 mentions "batch 2-3 deltas per `textContainerUpgrade` call to reduce BLE traffic." But a count-based batch means: if deltas arrive slowly (1 per second), the user waits 2-3 seconds for a display update. A time-based batch (e.g., "flush every 100ms, combining all deltas received in that window") gives consistent update frequency regardless of delta arrival rate.

**Recommended fix:** Specify in P3.7 or P3.8: use a 100ms debounce timer that flushes accumulated deltas, not a fixed count. This is a small design detail but impacts perceived responsiveness.

---

## Section D: Risk Assessment

### D.1 — `setPageFlip` Non-Existence Is the Biggest Risk (Critical)

*Likelihood: HIGH. Impact: HIGH.*

If `setPageFlip` doesn't exist in the SDK, P4.8 (page 1) becomes a redesign, P3.7 (truncation UX) loses its recovery mechanism, and the double-tap interaction from `displaying` state has no target. The design docs assume this method exists but the SDK reference doesn't list it.

**Mitigation:** Phase 0 spike as described in A.2.

---

### D.2 — `app.json` Manifest Will Fail Packaging (Critical)

*Likelihood: CERTAIN (field names are provably wrong). Impact: HIGH.*

P4.12 (build & packaging) will fail at `evenhub pack` because the manifest uses design-doc field names, not the actual schema from the toolchain skill.

**Mitigation:** Fix P1.4 manifest to match g2-dev-toolchain skill schema. Verify with `evenhub pack` early in Phase 1.

---

### D.3 — BLE Bandwidth Saturation During Streaming (Important)

*Likelihood: MEDIUM. Impact: HIGH.*

Each `textContainerUpgrade` call triggers a BLE message to the glasses. During fast LLM streaming (deltas every 20-50ms), this could exceed BLE throughput and cause dropped updates, display corruption, or lag. The plans mention batching but specify no mechanism.

**Mitigation:** Implement the 100ms debounce timer from C.5. Measure actual BLE throughput on real hardware in Phase 2 (add to P2.8 acceptance criteria). The g2-display-ui skill's "textContainerUpgrade visual difference" quirk means simulator won't show this problem — only real hardware will.

---

### D.4 — No TypeScript Tests Anywhere (Important)

*Likelihood: N/A. Impact: MEDIUM.*

The Gateway has comprehensive Python tests (test_protocol, test_audio_buffer, test_transcriber, test_server, test_openclaw_client, etc.). The G2 app has **zero tests** across all 4 phases. No vitest, no jest, no test runner configured in package.json. The state machine, display manager, input handler, markdown stripper, gateway client, and audio capture module are all untested.

The state machine alone has 13+ transitions with edge cases (e.g., `displaying` ignoring `status:"idle"`). The input handler has the `CLICK_EVENT = 0 → undefined` workaround. These are exactly the kinds of logic that break silently without tests.

**Mitigation:** Add vitest to P1.4's dev dependencies. Add test files for state.ts (P1.6), input.ts (P2.5), utils.ts/stripMarkdown (P3.5), and display.ts state transitions. Even minimal unit tests catch the critical bugs.

---

### D.5 — Whisper `base.en` May Be Too Slow on Low-End Hardware (Minor)

*Likelihood: MEDIUM. Impact: MEDIUM.*

Phase 2 checkpoint says "time from `stop_audio` to `transcription` frame < 5s on CPU with `base.en`." On a modern x86 CPU with int8 quantization, base.en does ~1s for 5s audio. But on older hardware or ARM (e.g., M1 without GPU), int8 might not be supported, falling back to float32 which is 3-4x slower. The plans don't document a fallback strategy.

**Mitigation:** Add a config validation step in P2.2 that warns if `WHISPER_DEVICE=cpu` and `WHISPER_COMPUTE_TYPE=int8` but the platform doesn't support int8. Fall back to `tiny.en` automatically if first inference exceeds 5s. Log inference timing prominently.

---

### D.6 — No Mock OpenClaw Server for Development (Important)

*Likelihood: N/A. Impact: MEDIUM.*

Phase 3 requires a running OpenClaw instance. If OpenClaw isn't installed, configured, and running, no Phase 3 development or testing can happen. The plans don't provide a mock OpenClaw WebSocket server for offline development.

**Mitigation:** Add a `mock_openclaw.py` task (small, ~50 lines) as P3.0: a simple WebSocket server that accepts `connect`, accepts `agent` requests, and streams back canned responses. This lets Phase 3 G2 app development proceed without a real OpenClaw instance and makes CI testing possible.

---

## Section E: Agent Assignment Optimization

### E.1 — Agent Assignments Are Correct (No Issues)

The `backend-python` / `g2-development` split is clean:
- All Python Gateway code → backend-python ✅
- All TypeScript G2 app code → g2-development ✅
- Integration tests → both ✅

No task has the wrong agent type. The g2-development agent handles all SDK quirks (events, display, audio) which require the skills files for correctness.

---

### E.2 — P4.12 (Build & Packaging) Needs g2-dev-toolchain Skill Awareness (Important)

P4.12 produces an `.ehpk` file using `evenhub pack`. The g2-development agent must reference the g2-dev-toolchain skill for:
- Correct `app.json` schema (see A.1)
- `evenhub pack` arguments: `evenhub pack app.json dist -o openclaw.ehpk`
- QR code generation: `evenhub qr --url "http://<LAN_IP>:5173"`
- `package_id` naming rules (no hyphens, lowercase segments start with letter)

The P4.12 acceptance criteria should include: "Manifest validated against g2-dev-toolchain skill schema." Currently it references doc 03 §9 which has the wrong field names.

**Recommended fix:** Update P4.12 to reference g2-dev-toolchain skill. Ensure the agent uses the correct manifest schema.

---

### E.3 — Integration Tests Need a Coordinator Role (Minor)

P1.10, P2.8, P3.9, and Phase 4 integration checkpoint are marked "both." In practice, "both" means neither agent owns the task. Without explicit ownership, integration tests may be deprioritized.

**Recommended fix:** Assign the Python integration test scripts to `backend-python` (they're Python WS clients). Assign the simulator visual verification to `g2-development`. Mark a human checkpoint for each phase transition.

---

## Section F: Missing Items

### F.1 — No TypeScript Test Framework or Tests (Critical)

As detailed in D.4. Not a single `.test.ts` or `.spec.ts` file across all 4 phases. The Python side has excellent test coverage plans (test_protocol, test_audio_buffer, test_transcriber, test_server, test_server_audio, test_openclaw_client, test_heartbeat, test_openclaw_reconnect, test_config). The TypeScript side has zero.

**Recommended fix:** Add to P1.4: install `vitest` as dev dependency, add `"test": "vitest run"` to package.json scripts. Add test files to P1.6 (state machine), P2.5 (input handler with CLICK_EVENT bug), P3.5 (markdown stripper). These are the highest-value test targets.

---

### F.2 — No CI/CD Pipeline (Important)

No GitHub Actions, no pre-commit hooks (for the G2 app), no automated build verification. The copilot-instructions.md mentions `uv run pre-commit run --all-files` for the Python project but the G2 app has no equivalent.

**Recommended fix:** Add a Phase 4 task: CI workflow that runs `uv run pytest` for Gateway, `npx tsc --noEmit` and `npx vitest run` for app, and `npx vite build` for bundle verification. Even a single GitHub Actions file catches regressions.

---

### F.3 — No Audio Dump Mode for Debugging (Minor)

Review doc 05 §7.4 flags this. When Whisper produces bad transcriptions, there's no way to inspect the PCM data that was sent. A `DEBUG_AUDIO_DUMP=1` env var that writes each recording's PCM to `/tmp/g2_audio_<timestamp>.raw` would be invaluable for debugging.

**Recommended fix:** Add to P4.11 (structured logging): when `LOG_LEVEL=DEBUG`, write raw PCM to a temp file before Whisper inference. Include the file path in the transcription log line.

---

### F.4 — No Health Check or Status Endpoint (Minor)

There's no way to inspect Gateway state from outside — no HTTP health endpoint, no CLI status command. If the phone shows "Connecting..." and the Gateway appears to be running, there's no diagnostic tool.

**Recommended fix:** Add a minimal HTTP health endpoint (e.g., `GET /health` on port 8766 or as a query param on the WS port) returning `{"status":"ok","state":"idle","whisper":"loaded","openclaw":"connected"}`. Low effort, high debugging value.

---

### F.5 — No Rate Limiting on Gateway (Minor)

A misbehaving or malicious LAN client could send unlimited `start_audio` / `stop_audio` cycles or flood binary frames. The single-connection model limits this to one client, but that client could still trigger unlimited Whisper inferences.

**Recommended fix:** Add a minimum cooldown (e.g., 1s) between `stop_audio` and the next `start_audio`. Reject rapid-fire recordings with `INVALID_STATE`.

---

### F.6 — WSS (TLS) Not Implemented (Minor)

Doc 01 §7 recommends WSS for sensitive audio. No plan task implements TLS. For v1 on a home LAN this is acceptable, but should be documented as a known limitation.

**Recommended fix:** Add a note to P4.13 (Gateway README): "v1 uses unencrypted WebSocket. For sensitive environments, place a TLS reverse proxy (e.g., caddy, nginx) in front of the Gateway."

---

### F.7 — No `displaying → recording` Direct Transition (Minor)

Doc 04 §8 transition table notes: "transitioning from displaying to recording requires two taps: first tap dismisses (→ idle), second tap starts recording (→ recording)." This is by design but creates friction for sequential queries. Review doc 05 §7.6 flags this.

The plans correctly implement the two-tap model (P2.5 maps tap in `displaying` → dismiss → idle). This is a UX limitation, not a plan gap, but should be documented as a known v1 limitation for user feedback purposes.

**Recommended fix:** Add to P4.14 (root README) known limitations section.

---

## Consolidated Recommendations (Top 10 by Impact)

| # | Impact | Recommendation | Affects |
|---|---|---|---|
| 1 | **Critical** | **Add Phase 0 spike: verify `setPageFlip` exists in SDK.** If it doesn't, redesign page 1 as `rebuildPageContainer` swap. | P4.8, P3.7, P2.5 |
| 2 | **Critical** | **Fix `app.json` manifest field names** to match g2-dev-toolchain skill: `package_id`, `name`, `entrypoint`, `edition`, `permissions`. | P1.4, P4.12 |
| 3 | **Critical** | **Add TypeScript test framework (vitest)** and tests for state machine, input handler (CLICK_EVENT=0 bug), and markdown stripping. | P1.4, P1.6, P2.5, P3.5 |
| 4 | **Important** | **Create an OpenClaw agent config task** with concise-response system prompt (50-150 words target) and tool restrictions. Without this, every response will be truncated. | New P3.2.5 |
| 5 | **Important** | **Add a mock OpenClaw WebSocket server** for offline development and CI. ~50 lines of Python. | New P3.0 |
| 6 | **Important** | **Use `waitForEvenAppBridge()`** instead of `EvenAppBridge.getInstance()` for reliable initialization. | P1.9 |
| 7 | **Important** | **Fix `audioPcm` type** to `Uint8Array` (matching SDK type declarations) and check `createStartUpPageContainer` return value. | P2.4, P1.9 |
| 8 | **Important** | **Implement delta display batching as a 100ms debounce timer**, not a count. Measure BLE throughput on real hardware in Phase 2. | P3.7, P3.8, P2.8 |
| 9 | **Important** | **Consolidate P4.5 + P4.6 + P4.7** into one task (error + disconnected + loading display states). Saves 2 agent calls of overhead. | Phase 4 |
| 10 | **Minor** | **Move P3.5 (markdown stripping) to Phase 1** — zero dependency, useful immediately for mock response testing, reduces Phase 3 task count. | P3.5 → P1.2.5 |

---

*End of review.*
