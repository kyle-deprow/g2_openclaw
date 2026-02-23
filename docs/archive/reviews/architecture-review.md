# G2 OpenClaw — Architecture Review

**Reviewer:** Senior Systems Architect
**Date:** 2026-02-22
**Documents Reviewed:** architecture.md, gateway.md, g2-app.md, display-layouts.md, plus openclaw_research/ (8 files)

---

## 1. Architecture Assessment

**Grade: B+**

The thin-client model is the right call. Concentrating all intelligence (Whisper, OpenClaw relay, session state, auth) on the PC is correct for three reasons: (1) the EvenHub WebView is a poor execution environment for anything complex, (2) a single WebSocket from the phone dramatically simplifies the network surface, and (3) keeping OpenClaw locked to localhost eliminates an entire class of security problems.

The component responsibilities are cleanly separated. The Gateway module architecture (server.py, transcriber.py, openclaw_client.py, audio_buffer.py, protocol.py, config.py) maps one module to one concern.  The app module structure mirrors this well. The state machines on both sides are well-defined and almost consistent with each other.

What pulls it down from an A: the protocol specification has cross-document inconsistencies that would cause real bugs (detailed below), the hold-to-talk UX model is contradicted by the SDK's actual capabilities, and several operational scenarios have no design at all.

---

## 2. Critical Issues (Must Fix Before Building)

### 2.1 Hold-to-Talk is Impossible with the G2 SDK

**Docs affected:** [architecture.md](../../design/architecture.md) §5, [g2-app.md](../../design/g2-app.md) §7, [display-layouts.md](../../design/display-layouts.md) §8

The entire design assumes hold-to-record, release-to-send. [architecture.md](../../design/architecture.md) §5 shows "HOLD button → open mic, RELEASE btn → send stop_audio." [g2-app.md](../../design/g2-app.md) §6 labels the flow as "User holds ring button" → "User releases ring button."

But [g2-app.md](../../design/g2-app.md) §7 buries the truth: **"The G2 SDK does not provide distinct HOLD/RELEASE events."** The SDK fires `CLICK_EVENT` on tap — there is no hold detection, no key-down, no key-up. The doc then pivots to a tap-to-toggle model under "Hold vs Tap Detection" but never reconciles this with the rest of the architecture.

This means: every sequence diagram, every state transition description, every UX instruction in all four docs that says "hold ring" or "release ring" is wrong. The interaction model must be rewritten as tap-to-start / tap-to-stop (toggle) across all docs, and the state machine transitions must be updated accordingly.

**Fix:** Globally replace the hold-to-record model with tap-to-toggle. Update [architecture.md](../../design/architecture.md) §4 step 6, [architecture.md](../../design/architecture.md) §5 interaction map, [gateway.md](../../design/gateway.md) §3 transition table, [g2-app.md](../../design/g2-app.md) §4 state diagram, [g2-app.md](../../design/g2-app.md) §6 step-by-step, [g2-app.md](../../design/g2-app.md) §7 event table, and [display-layouts.md](../../design/display-layouts.md) §8 transition table.

### 2.2 Protocol Field Name Conflicts Between Docs

Three field name disagreements would cause immediate JSON parse failures:

| Frame | [architecture.md](../../design/architecture.md) (Appendix A) | [gateway.md](../../design/gateway.md) (protocol.py) | Which is Correct? |
|---|---|---|---|
| `text` (phone→gw) | `{type:"text", message:"..."}` | `class TextMessage: text: str` | Must pick one |
| `error` (gw→phone) | `{type:"error", message:"...", code:"..."}` | `{type:"error", code:"...", detail:"..."}` | Must pick one — `message` vs `detail` |
| `connected` (gw→phone) | `{type:"connected", version:"1.0"}` | `class ConnectedFrame` with no `version` field | Must add `version` to protocol.py or remove from spec |

**Fix:** Canonicalize in protocol.py ([gateway.md](../../design/gateway.md)) and update [architecture.md](../../design/architecture.md) Appendix A to match. Suggested: use `message` for user text input, `detail` for error details, and include `version` in the connected frame.

### 2.3 OpenClaw Lifecycle Event Field Name May Be Wrong

[gateway.md](../../design/gateway.md) §5.6 and [architecture.md](../../design/architecture.md) Appendix B parse lifecycle events as:
```json
{"stream":"lifecycle", "status":"end"}
```

The OpenClaw research notes (02_agent_architecture_and_context.md) document the lifecycle event format as:
```
Lifecycle events → stream: "lifecycle" (phase: "start" | "end" | "error")
```

If the actual OpenClaw protocol uses `phase` instead of `status`, the PC Gateway will never detect response completion. Every voice interaction would hang in `streaming` state indefinitely.

**Fix:** Verify against the actual OpenClaw source code. If the field is `phase`, update [architecture.md](../../design/architecture.md) Appendix B, [gateway.md](../../design/gateway.md) §5.6 event parsing table, and the `openclaw_client.py` design.

### 2.4 Authentication Mechanism is Contradictory

Two different auth mechanisms are described:

- [architecture.md](../../design/architecture.md) Appendix A: `ws://<PC_IP>:8765?token=<shared_secret>` (query parameter)
- [gateway.md](../../design/gateway.md) §9 Step 5: "Phone sends `{type:"auth", token:"..."}` as its first message"

These are mutually exclusive. The `auth` frame type doesn't appear in the Phone→Gateway frame table ([architecture.md](../../design/architecture.md) Appendix A), and there's no `auth` class in protocol.py ([gateway.md](../../design/gateway.md) §2.6).

**Fix:** Pick one. Query parameter is simpler and handled at connection time before any framing. If using first-frame auth, add `AuthFrame` to protocol.py and the frame table. Recommend query param for simplicity, since WebSocket handshake rejection with a close code is cleaner than accepting the connection then closing it.

### 2.5 `cancel` Frame is Defined but Never Handled

[architecture.md](../../design/architecture.md) Appendix A defines `{type:"cancel"}` as a Phone→Gateway frame. It is never mentioned anywhere in [gateway.md](../../design/gateway.md):

- Not in the state machine transitions (§3)
- Not in the transition table
- Not parsed in protocol.py's `parse_text_frame()` return types: `StartAudio | StopAudio | TextMessage`
- No handling logic in `server.py`'s `handle_text_frame()`
- No design for how to cancel a Whisper inference in progress
- No design for how to cancel an OpenClaw agent run in progress

A cancel frame without backend support is worse than not having one — the phone thinks it cancelled but the Gateway keeps going.

**Fix:** Either (a) remove `cancel` from the protocol until properly designed, or (b) design the full cancel flow: in `transcribing` state → cancel Whisper task via `asyncio.Task.cancel()`; in `thinking`/`streaming` state → close the OpenClaw WebSocket for this run or send a stop signal. Add `CancelFrame` to protocol.py, add transitions in the state machine, and add handling in server.py.

---

## 3. Important Issues (Should Fix)

### 3.1 No WebSocket Heartbeat/Keepalive

Neither the phone→Gateway nor Gateway→OpenClaw connections define a ping/pong or heartbeat mechanism. WiFi connections can silently half-close (phone walks out of range, router drops idle TCP). Without keepalive:

- The Gateway may hold a zombie session for a phone that left 10 minutes ago.
- The phone may think it's connected while frames are silently dropped.
- The OpenClaw connection could silently die with no notification until the next agent request.

**Fix:** Enable WebSocket ping/pong at the library level. `websockets` (Python) supports `ping_interval` and `ping_timeout` params on `serve()`. On the phone side, the browser WebSocket doesn't support custom pings, so the Gateway should send application-level `{type:"ping"}` frames at 30s intervals and expect a `{type:"pong"}` response, with a 10s timeout before declaring the connection dead.

### 3.2 No Handling for "Gateway Not Ready" During Whisper Load

[gateway.md](../../design/gateway.md) §9 shows Whisper model loading takes 5–30s at startup. If the phone connects during this window, the Gateway sends `{type:"connected"}` but Whisper cannot transcribe. If the user immediately starts recording:

- `stop_audio` triggers transcription on an unloaded model → crash or silent failure
- No status frame communicates "model loading" to the phone

**Fix:** Add a `loading` status. Gateway should send `{type:"status", status:"loading"}` until the model is ready, then automatically transition to `idle`. Reject `start_audio` during `loading` with an appropriate error.

### 3.3 App-Side `displaying` State Has No Gateway Equivalent

[g2-app.md](../../design/g2-app.md) §4 and [display-layouts.md](../../design/display-layouts.md) §8 define a `displaying` (response complete) state on the app. The app enters this state when it receives `{type:"end"}`, then separately receives `{type:"status", status:"idle"}`. But the app's state machine transitions on `end` frame, not on status frames.

This creates an ambiguity: does the app map Gateway `status` frames directly to app states, or does it have its own state logic? If the app blindly obeys `status:"idle"`, receiving it after `{type:"end"}` would skip the `displaying` state and jump straight to idle — the user would never see their response.

**Fix:** Explicitly document that the app does NOT blindly transition on every `status` frame. The `displaying` state ignores `status:"idle"`. The app transitions to idle only on user input (tap to dismiss) or on starting a new recording. Document this exemption in [g2-app.md](../../design/g2-app.md) §4 and [display-layouts.md](../../design/display-layouts.md) §8.

### 3.4 2000-Character Display Limit Will Be Hit Routinely

[g2-app.md](../../design/g2-app.md) §3.4 notes: "If accumulated text exceeds 1000 chars (the `rebuildPageContainer` limit), switch to `textContainerUpgrade` which allows 2000 chars."

A typical LLM response easily exceeds 2000 characters (5–6 paragraphs). What happens when the accumulated response hits 2000 chars?

- Does `textContainerUpgrade` silently truncate?
- Does it throw?
- Can the response be paginated across pages 0 and 1?

The docs don't address this. On the 576×288 display at 18px font (~37 chars/line, ~11 visible lines), 2000 chars is about 54 lines — roughly 5 screenfuls of scrolling. This is the *maximum*, not a rare edge case.

**Fix:** Design a truncation strategy. Options: (a) truncate to 2000 chars with a "... [truncated, double-tap for more]" indicator and put the overflow on page 1; (b) strip markdown/whitespace to compress; (c) use a `textContainerUpgrade` reset technique (rebuild with the tail of the response). Document the chosen approach in [g2-app.md](../../design/g2-app.md) §3.4 and [display-layouts.md](../../design/display-layouts.md) §4.5.

### 3.5 Streaming Response Starts Before Layout Swap

[gateway.md](../../design/gateway.md) §3 transition table says: "thinking → streaming" triggers on "first assistant delta." [display-layouts.md](../../design/display-layouts.md) §8 says the transition from thinking to streaming uses `rebuildPageContainer` (full layout swap), then subsequent deltas use `textContainerUpgrade`.

The problem: the first delta triggers a `rebuildPageContainer` call (async, involves BLE round-trip to glasses), and while that's in flight, more deltas arrive. If `textContainerUpgrade` is called before `rebuildPageContainer` completes, the container doesn't exist yet. Deltas are lost or the SDK throws.

**Fix:** Buffer deltas during the thinking→streaming layout transition. The app should set a `layoutPending` flag, collect deltas in a local array, and flush them all via `textContainerUpgrade` once `rebuildPageContainer` resolves. Document this in [g2-app.md](../../design/g2-app.md) §3.4 and the streaming strategy section.

### 3.6 PCM Format Ambiguity: Int8Array vs S16LE

[g2-app.md](../../design/g2-app.md) §6 says onMicData delivers `Int8Array` chunks, and the PCM format reference says "PCM S16LE (signed 16-bit little-endian)." [gateway.md](../../design/gateway.md) §4.2 says "Sample width: 2 bytes (16-bit), Signed little-endian (PCM S16LE)."

`Int8Array` is a *byte-level view* — it contains the raw bytes of 16-bit samples, not 8-bit samples. This is an important distinction: a 40-byte Int8Array frame contains 20 S16LE samples (1.25ms at 16kHz), not 40 8-bit samples. The docs should explicitly state that the `Int8Array` is a byte view of 16-bit PCM data to avoid someone writing a sample-level parser that treats each byte as one sample.

**Fix:** Add a note in [g2-app.md](../../design/g2-app.md) §6 PCM Format Reference: "The `Int8Array` from `onMicData` is a byte-level view of 16-bit PCM S16LE data — each sample spans 2 consecutive bytes. Send the raw bytes as-is; the PC Gateway assembles them into a proper WAV."

### 3.7 No Timeout for Full Request Cycle

[gateway.md](../../design/gateway.md) specifies a 30s timeout for Whisper inference, and the OpenClaw research notes show OpenClaw's default timeout is 600s. But no end-to-end timeout is defined for the full voice-to-response cycle as seen by the phone.

If OpenClaw hangs (tool loops, model timeout, etc.), the phone sits in `thinking` or `streaming` state indefinitely with no recovery. The Gateway has no watchdog for the overall cycle time.

**Fix:** Add a Gateway-side timeout for the full agent cycle (e.g., 120s). If elapsed time from `thinking` entry exceeds the limit, send `{type:"error", code:"TIMEOUT"}` and return to idle. Make this configurable via `AGENT_TIMEOUT` env var.

---

## 4. Minor Issues (Nice to Fix)

### 4.1 Display Layout Inconsistency Between [g2-app.md](../../design/g2-app.md) and [display-layouts.md](../../design/display-layouts.md)

[g2-app.md](../../design/g2-app.md) §3.1 uses a different layout grid than [display-layouts.md](../../design/display-layouts.md) §3:

| Property | [g2-app.md](../../design/g2-app.md) Idle | [display-layouts.md](../../design/display-layouts.md) Idle |
|---|---|---|
| Title y-position | 10 | 8 |
| Title width | 556 | 460 |
| Content x-position | 10 | 8 |
| Status bar | y=258, h=22 | y=256, h=24 |
| Badge container | not present | 480, 8, 88, 32 |
| Total containers | 3 | 4 |

[display-layouts.md](../../design/display-layouts.md) introduces a consistent 4-container layout with a header badge that [g2-app.md](../../design/g2-app.md) doesn't use. [display-layouts.md](../../design/display-layouts.md) is clearly the more mature design (it adds the badge for state indication and uses a proper margin system).

**Fix:** Replace all layout specs in [g2-app.md](../../design/g2-app.md) §3.1–3.5 with references to [display-layouts.md](../../design/display-layouts.md). "See display-layouts.md for pixel-level specifications." Remove the inline layout tables from [g2-app.md](../../design/g2-app.md) to avoid drift.

### 4.2 `borderRdaius` Typo in SDK Preserved Without Comment

[display-layouts.md](../../design/display-layouts.md) §7.1 preserves the SDK's typo (`borderRdaius` instead of `borderRadius`) without noting it. The comment `// SDK typo preserved` is only visible if someone reads the code example carefully.

**Fix:** Add a bold callout: "**Note:** The SDK has a typo in the property name: `borderRdaius` (missing 'i'). This is intentional — using the correct spelling will silently fail."

### 4.3 Config Variable Naming Inconsistency

- [architecture.md](../../design/architecture.md): references `OPENCLAW_GATEWAY_TOKEN` (the OpenClaw env var name)
- [gateway.md](../../design/gateway.md) §6: defines `OPENCLAW_TOKEN` as the Gateway config variable
- OpenClaw research: uses `OPENCLAW_GATEWAY_TOKEN`

There are two different tokens: the token the Gateway uses to auth with OpenClaw, and the token the phone uses to auth with the Gateway. The name `OPENCLAW_TOKEN` for the Gateway config is ambiguous.

**Fix:** Rename to `OPENCLAW_GATEWAY_TOKEN` (for OpenClaw auth) and `GATEWAY_TOKEN` (for phone auth), matching both the established OpenClaw convention and the Gateway's own naming. Update [gateway.md](../../design/gateway.md) §6 table.

### 4.4 WAV Conversion May Be Unnecessary

[gateway.md](../../design/gateway.md) §4.4 builds a 44-byte WAV header for Whisper consumption. However, `faster-whisper` can accept raw numpy arrays or file paths. Constructing a WAV blob in memory, then having `faster-whisper` immediately decode it back to samples, is a pointless round-trip.

**Fix:** Feed `faster-whisper` a `numpy.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0` array directly. Remove the WAV header construction. This saves ~15ms of unnecessary work per transcription and removes 20 lines of struct-packing code.

### 4.5 Expo vs Vite for G2 App Build

[g2-app.md](../../design/g2-app.md) §9 uses Vite, which is correct for an EvenHub WebView app. No issue here, just confirming this is fine — EvenHub apps are plain web apps packaged as `.ehpk`. Vite's single-bundle output matches the WebView's requirements.

### 4.6 `CLICK_EVENT = 0` Workaround Could Mask Real Bugs

[g2-app.md](../../design/g2-app.md) §7 handles the SDK bug by checking `eventType === undefined`. This means *any* undefined event type will be treated as a click. If the SDK adds new event types that the app doesn't handle, they'll also fall into the click handler.

**Fix:** Check for `undefined` only as a last resort after explicitly checking all known event types. Move the undefined check to a separate code path with logging: `console.warn('Got undefined eventType — treating as click (SDK bug)')`.

---

## 5. Protocol Gaps

### 5.1 Missing Frame Types

| Missing Frame | Direction | Purpose |
|---|---|---|
| `ping` / `pong` | Bidirectional | Application-level keepalive (see §3.1) |
| `auth` | Phone→GW | First-frame auth if not using query param (see §2.4) |
| `cancel` handling | Phone→GW | Already defined but unimplemented (see §2.5) |
| `loading` status | GW→Phone | Gateway model not ready yet (see §3.2) |
| `reconnected` | GW→Phone | Distinguish first connect from reconnect — app may need to resync state |
| `session_reset` | Phone→GW | Request a new OpenClaw session (mentioned in [gateway.md](../../design/gateway.md) §5.5 as future extension but should be in v1) |
| `tool_use` | GW→Phone | OpenClaw tool events (`stream:"tool"`) — [gateway.md](../../design/gateway.md) §5.6 says "optionally forward" with no design |

### 5.2 Frame Ordering Guarantee

The protocol doesn't specify ordering guarantees. Are these guaranteed?

- `status:"transcribing"` always arrives before `transcription` ?
- `status:"streaming"` always arrives before first `assistant` delta?
- `end` always arrives after all `assistant` deltas?

TCP guarantees in-order delivery, and WebSocket inherits this. So yes, but only if the Gateway sends frames in the correct order. [gateway.md](../../design/gateway.md)'s transition table implies this ordering but never explicitly states it as a protocol invariant.

**Fix:** Add a "Protocol Invariants" section to [architecture.md](../../design/architecture.md) Appendix A that states frame ordering guarantees explicitly.

### 5.3 No Protocol Versioning Strategy

`{type:"connected", version:"1.0"}` is sent on connect, but there's no design for what happens when versions don't match. What if the Gateway is v2 and the phone app is v1? No negotiation, no feature flags, no minimum version check.

**Fix:** Define version negotiation: Gateway sends its version in `connected`, phone can check compatibility. If the phone sends a `start_audio` with fields the Gateway doesn't understand (future extensions), the Gateway should ignore unknown fields rather than error.

### 5.4 Binary Frame Ambiguity

All binary frames are raw PCM audio. If a future extension needs to send other binary data (e.g., images for display), there's no way to distinguish binary frame types. The protocol has no binary frame header.

**Fix (for later):** Reserve the first byte of binary frames as a type tag (e.g., `0x01` = PCM audio). For v1, document that all binary frames are PCM and that future binary types will use a 1-byte prefix.

---

## 6. Risk Register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | `onMicData` chunk size/format differs between real hardware (40 bytes/10ms) and simulator (3200 bytes/100ms), causing Gateway buffer assumptions to break | **High** | **Medium** | Test on real hardware early. AudioBuffer should be chunk-size-agnostic — just append bytes. |
| 2 | BLE bandwidth saturation from frequent `setLayout` / `textContainerUpgrade` calls during streaming | **Medium** | **High** | Implement throttling ([display-layouts.md](../../design/display-layouts.md) §4.5 says 15/sec but provides no mechanism). Batch 2–3 deltas per display update. Measure actual BLE throughput on hardware. |
| 3 | `textContainerUpgrade` fails silently or throws after 2000-char limit | **High** | **High** | Test with long responses early. Design truncation/pagination before building streaming display. |
| 4 | `faster-whisper` CPU inference is too slow for acceptable UX on lower-end machines (>5s for short utterances on base.en) | **Medium** | **Medium** | Default to `tiny.en`; allow GPU opt-in. Add inference timing to logs. Show elapsed time in "Transcribing..." status. |
| 5 | OpenClaw `lifecycle` event field is `phase` not `status`, causing the Gateway to never detect response completion | **High** | **Critical** | Verify against OpenClaw source code before writing any parsing code. Write an integration test that sends a real agent message. |
| 6 | iPhone kills the WebView/WKWebView background process during long OpenClaw responses, dropping the WebSocket | **Medium** | **High** | Request `UIApplication.shared.beginBackgroundTask` via Flutter bridge? Test background behavior. Reconnect logic must restore display state. |
| 7 | WiFi firewall on corporate/hotel/university networks blocks port 8765 | **Medium** | **Medium** | Document requirement for open LAN port. Consider fallback to common ports (80, 443) or config option. |
| 8 | `EvenAppBridge` singleton not initialized when events fire (race condition on app launch) | **Medium** | **Medium** | Gate all event handlers behind an `initialized` flag. Don't register `onEvenHubEvent` until after `createStartUpPageContainer` completes. |
| 9 | OpenClaw sends markdown-formatted responses (`**bold**`, `[links](url)`, `` `code` ``) that render as raw syntax on the greyscale display | **High** | **Low** | [display-layouts.md](../../design/display-layouts.md) §6 mentions markdown stripping but provides no implementation. Write a `stripMarkdown()` utility and apply to every delta before display. |
| 10 | Tap-to-toggle recording has no visual/haptic feedback on the glasses, making it unclear whether a tap started or stopped recording | **Medium** | **Medium** | Ensure the recording→idle and idle→recording transitions produce an immediate, visually distinct layout change. Consider a brief flash or color change on the badge. |

---

## 7. Missing from Plan

### 7.1 OpenClaw Agent Configuration

No design for the OpenClaw agent that backs the G2 assistant. What model? What system prompt? What tools should be enabled/disabled? The docs use a hardcoded `sessionKey: "agent:claw:g2"` but don't specify:

- The agent ID (does it use the default `claw` agent or a custom `g2` agent?)
- A `SOUL.md` / `IDENTITY.md` for the G2-specific persona ("you are a concise voice assistant rendering to a tiny display — keep responses under 200 words")
- Tool restrictions (browser and canvas tools make no sense on glasses — deny them)
- A `tools.profile` to limit to a sensible subset

### 7.2 Response Length Control

LLM responses are optimized for desktop chat — they'll routinely produce 500+ word answers. On a 576×288 display with 37 chars/line, 11 visible lines, that's 15+ pages of scrolling. The plan needs:

- A system prompt instruction to keep responses concise (50–150 words)
- A max-response-length enforcement in the Gateway (truncate after N chars with "...")
- Or a summarization step in the Gateway before forwarding to the phone

### 7.3 Testing Strategy

No testing approach is described for any component:

- How to test the Gateway without real glasses or a phone (mock WebSocket client?)
- How to test the G2 app without real glasses (EvenHub simulator?)
- How to integration-test the full pipeline
- How to test audio quality / Whisper accuracy with the G2 microphone

### 7.4 Observability and Debugging

No logging, metrics, or debugging design:

- No structured logging format
- No way to replay a voice interaction for debugging
- No audio dump mode for diagnosing Whisper accuracy issues
- No latency measurement (time from stop_audio to first delta)
- No way to inspect Gateway state from outside (health endpoint, CLI, dashboard)

### 7.5 Graceful Shutdown

What happens when the user Ctrl+C's the Gateway while a phone is connected and an agent run is in progress? No signal handling, no drain logic, no "shutting down" frame to the phone.

### 7.6 Multiple Sequential Interactions

The docs only design one request-response cycle. What about:

- Can the user start a new recording while viewing a previous response (`displaying` state)? [g2-app.md](../../design/g2-app.md) §7 says yes (ring hold from `displaying` state), but **[display-layouts.md](../../design/display-layouts.md) §8 transition table doesn't show a `displaying → recording` transition directly**. It shows `response complete → idle` via tap, then `idle → recording`. This means the user must dismiss first, then record — two gestures instead of one. The display will flash through idle unnecessarily.
- Does the previous response remain accessible on page 1 while the new query is processing?

### 7.7 First-Run / Onboarding UX

No design for:

- How the user enters the PC's IP address the first time
- What happens if the Gateway isn't running when the app launches
- How to guide the user through WiFi + BLE setup
- A "scan QR to configure" flow (hinted at in the build process but not designed as UX)

### 7.8 Offline/Degraded Mode

What happens if:

- OpenClaw is not installed or not running (Gateway starts, phone connects, but agent requests fail)
- Whisper model fails to load (corrupt download, missing CUDA drivers)

Currently these surface as generic errors. A "health check" frame from the Gateway reporting subsystem status would let the phone display actionable diagnostics: "Whisper: OK, OpenClaw: Not running."

---

## 8. Suggested Implementation Order

### Phase 1: Vertical Slice (Week 1)

**Goal:** Phone connects to Gateway, sends text, gets a response displayed on glasses.

1. **Protocol types** — Define canonical frame types in both Python (`protocol.py`) and TypeScript. Resolve all naming conflicts now.
2. **Gateway skeleton** — `server.py` with WebSocket accept, frame routing, hardcoded mock response. No Whisper, no OpenClaw.
3. **App skeleton** — `main.ts` + `gateway.ts` + `state.ts`. Connect to Gateway, send `{type:"text", message:"hello"}`, display the mock response.
4. **Display: idle + streaming + response complete** — Just these three [display-layouts.md](../../design/display-layouts.md) layouts. Prove that `rebuildPageContainer` and `textContainerUpgrade` work on the simulator.

**Rationale:** Validates the end-to-end plumbing and display rendering before touching audio or AI. If the WebSocket or display doesn't work, you find out immediately.

### Phase 2: Audio Pipeline (Week 2)

**Goal:** Speak into glasses, see transcription on display.

5. **Mic capture** — `audio.ts` with `onMicData` forwarding. Test on simulator first (3200-byte chunks), then real hardware (40-byte chunks).
6. **Audio buffer** — `audio_buffer.py` with PCM accumulation and WAV conversion (or direct numpy, per §4.4).
7. **Whisper integration** — `transcriber.py` with `faster-whisper` base.en model. Test with recorded WAV files first.
8. **End-to-end voice** — Tap to record, tap to stop, see transcription. Gateway mocks the AI response — just echo the transcription back.

**Rationale:** Audio is the highest-risk component (PCM format unknowns, BLE bandwidth, Whisper latency). Test it in isolation before adding AI complexity.

### Phase 3: OpenClaw Integration (Week 3)

**Goal:** Full voice-to-AI-response pipeline.

9. **OpenClaw client** — `openclaw_client.py` with connect, auth, agent request, delta streaming. Test against a running OpenClaw instance with CLI first.
10. **Wire it together** — Gateway transcribes audio, forwards to OpenClaw, streams deltas to phone.
11. **OpenClaw agent config** — Create the G2-specific agent with concise system prompt, restricted tools, appropriate model.

**Rationale:** By now the plumbing to/from the phone is proven. The OpenClaw integration is localhost-only and well-documented in the research notes.

### Phase 4: Polish & Edge Cases (Week 4)

12. **All 8 display states** — recording, transcribing, thinking, error, disconnected layouts.
13. **Page 1 detail view** — page flip, metadata display.
14. **Error handling** — all 13 error scenarios from [gateway.md](../../design/gateway.md) §7.2.
15. **Reconnection** — phone auto-reconnect, Gateway→OpenClaw reconnect, state recovery.
16. **Heartbeat/keepalive** — ping/pong implementation.
17. **Cancel flow** — if implementing cancel in v1.
18. **Markdown stripping** — clean LLM output for display.
19. **Response length handling** — truncation or pagination for long responses.

**Rationale:** Polish after function. Error handling is important but you can't test it well until the happy path works.

---

*End of review.*
