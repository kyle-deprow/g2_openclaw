# Phase 2: Audio Pipeline

## Phase Goal

Speak into the G2 glasses microphone, see the transcription appear on the glasses display — proving the full audio capture → PCM relay → Whisper transcription pipeline before adding AI.

## Prerequisites

- Phase 1 complete: WebSocket plumbing, protocol types, basic display, state machine all working
- `faster-whisper` model downloadable (internet access for first model download)
- G2 simulator for PCM simulation (3200-byte chunks) or real hardware (40-byte chunks)
- Gateway mock response still used for AI — transcription is echoed back as the "response"

## Task Breakdown

### P2.1 — Audio Buffer (`audio_buffer.py`)

- **Description:** Implement PCM accumulation buffer per [gateway.md](../design/gateway.md) §2.4 and §4. Accept raw bytes, enforce 60-second max duration limit, convert to float32 numpy array for Whisper. Buffer is chunk-size-agnostic (works with both 40-byte hardware chunks and 3200-byte simulator chunks per review §6 Risk #1).
- **Owner:** backend-python
- **Dependencies:** P1.3 (gateway scaffold exists)
- **Complexity:** M
- **Files created:**
  - `gateway/audio_buffer.py`
  - `tests/gateway/test_audio_buffer.py`
- **Acceptance criteria:**
  - `AudioBuffer.__init__(sample_rate, channels, sample_width)` — accepts format params from `start_audio`
  - `append(chunk: bytes)` — appends bytes; raises `BufferOverflow` if accumulated bytes exceed `MAX_DURATION × byte_rate` ([gateway.md](../design/gateway.md) §4.3: 60s × 32000 B/s = 1,920,000 bytes)
  - `to_numpy() -> np.ndarray` — returns float32 array normalized to [-1.0, 1.0] via `np.frombuffer(bytes, dtype=np.int16).astype(np.float32) / 32768.0` ([gateway.md](../design/gateway.md) §4.4)
  - `reset()` — clears buffer for next recording
  - `duration_seconds` property — estimated from byte count and format
  - `is_empty` property — true when no bytes accumulated
  - Tests: append small chunks, append large chunks, verify numpy output shape and range, test overflow exception, test reset

### P2.2 — Transcriber (`transcriber.py`)

- **Description:** Implement Whisper integration wrapper per [gateway.md](../design/gateway.md) §2.2 and §4.5-4.7. Load model at init, expose async `transcribe()` that runs inference in a thread pool executor. Use greedy decoding, VAD filter, per [gateway.md](../design/gateway.md) §4.6 inference parameters.
- **Owner:** backend-python
- **Dependencies:** P1.3 (gateway scaffold)
- **Complexity:** M
- **Files created:**
  - `gateway/transcriber.py`
  - `tests/gateway/test_transcriber.py`
- **Acceptance criteria:**
  - `Transcriber.__init__(model_name, device, compute_type)` — loads `faster-whisper` model ([gateway.md](../design/gateway.md) §4.5, default `base.en` / `cpu` / `int8`)
  - `async transcribe(audio: np.ndarray, language: str = "en") -> str` — runs `model.transcribe()` in `asyncio.get_event_loop().run_in_executor()` per [gateway.md](../design/gateway.md) §11 threading model
  - Inference parameters match [gateway.md](../design/gateway.md) §4.6: `beam_size=1`, `best_of=1`, `temperature=0.0`, `condition_on_previous_text=False`, `vad_filter=True`, `language="en"`
  - Handles empty transcription → raises `TranscriptionError`
  - 30-second timeout via `asyncio.wait_for()` per [gateway.md](../design/gateway.md) §4.7
  - Tests: mock `faster-whisper` model, verify correct params passed, test timeout handling, test empty result handling
  - Integration test with real model: transcribe a known WAV file and verify output contains expected words

### P2.3 — Gateway Audio State Machine Integration

- **Description:** Extend `server.py` to handle the full audio flow: `start_audio` → create buffer → accumulate binary frames → `stop_audio` → transcribe → send transcription → mock AI response. Implement state transitions per [gateway.md](../design/gateway.md) §3: `idle → recording → transcribing → thinking → streaming → idle`. Handle all audio error scenarios from [gateway.md](../design/gateway.md) §4.7 and §7.2 (#1-5).
- **Owner:** backend-python
- **Dependencies:** P1.5 (server skeleton), P2.1 (audio buffer), P2.2 (transcriber)
- **Complexity:** L
- **Files created/modified:**
  - `gateway/server.py` (modify — add audio handling)
  - `gateway/config.py` (modify — add Whisper config fields)
  - `tests/gateway/test_server_audio.py`
- **Acceptance criteria:**
  - On `start_audio` (in `idle` state): create `AudioBuffer` with params from frame, transition to `recording`, send `status:recording`
  - On binary frame (in `recording` state): call `buffer.append(data)`
  - On `stop_audio` (in `recording` state): transition to `transcribing`, send `status:transcribing`, call `transcriber.transcribe(buffer.to_numpy())`, send `{type:"transcription", text:"..."}`, transition to `thinking`
  - On `start_audio` in non-idle state: send `{type:"error", code:"INVALID_STATE"}`
  - Handle `BufferOverflow`: send error, reset buffer, return to idle
  - Handle empty buffer on `stop_audio`: send error `TRANSCRIPTION_FAILED`, return to idle
  - Handle Whisper timeout: send error, return to idle
  - Handle empty transcription: send error, return to idle
  - After transcription, mock AI response (same as Phase 1 — echo transcription back as 3 deltas)
  - Loading state: start WS server immediately, load Whisper in background, send `status:loading` to early connections, transition to `idle` when model ready ([gateway.md](../design/gateway.md) §9, review §3.2)
  - Config extended with `whisper_model`, `whisper_device`, `whisper_compute_type` per [gateway.md](../design/gateway.md) §6
  - Tests: mock transcriber, verify full state flow, verify error handling for each scenario

### P2.4 — Mic Capture (`audio.ts`)

- **Description:** Implement microphone capture per [g2-app.md](../design/g2-app.md) §6. Open/close mic via `bridge.audioControl()`. Forward each `onMicData` PCM chunk immediately as a binary WebSocket frame — zero buffering on the phone. Handle the `Uint8Array` byte-level view correctly ([g2-app.md](../design/g2-app.md) §6 PCM Format Clarification — SDK delivers `Uint8Array`, not `Int8Array`).
- **Owner:** g2-development
- **Dependencies:** P1.7 (gateway.ts), P1.9 (main.ts)
- **Complexity:** M
- **Files created:**
  - `g2_app/src/audio.ts`
  - `g2_app/src/__tests__/audio.test.ts`
- **Acceptance criteria:**
  - `AudioCapture` class with `start(bridge, gateway)` and `stop()` methods per [g2-app.md](../design/g2-app.md) §2
  - `start()`: sends `{type:"start_audio", sampleRate:16000, channels:1, sampleWidth:2}` to gateway, then calls `bridge.audioControl(true)`
  - Audio event handler: on `event.audioEvent`, forwards `audioPcm.buffer` (`Uint8Array.buffer` — SDK delivers `Uint8Array`, NOT `Int8Array` per review A.3) via `gateway.send()` immediately — no buffering ([g2-app.md](../design/g2-app.md) §6 critical constraint)
  - `stop()`: calls `bridge.audioControl(false)`, sends `{type:"stop_audio"}` to gateway
  - Prerequisite enforced: `audioControl()` only called after `createStartUpPageContainer` ([g2-app.md](../design/g2-app.md) §6)
  - Handles both 40-byte (real hardware) and 3200-byte (simulator) chunks transparently

### P2.5 — Input Handler (`input.ts`)

- **Description:** Implement full event routing per [g2-app.md](../design/g2-app.md) §7. Handle the `CLICK_EVENT = 0 / undefined` SDK bug. Route taps based on current state (tap-to-toggle model). Handle all event types from `OsEventTypeList`. Implement scroll throttling (300ms cooldown per [g2-app.md](../design/g2-app.md) §7).
- **Owner:** g2-development
- **Dependencies:** P1.6 (state.ts), P2.4 (audio.ts)
- **Complexity:** M
- **Files created:**
  - `g2_app/src/input.ts`
- **Acceptance criteria:**
  - `InputHandler` class with `setup(bridge)` per [g2-app.md](../design/g2-app.md) §2
  - Registers `bridge.onEvenHubEvent()` callback
  - Checks all three event sources: `event.textEvent?.eventType`, `event.listEvent?.eventType`, `event.sysEvent?.eventType` ([g2-app.md](../design/g2-app.md) §7)
  - `CLICK_EVENT` bug workaround: `eventType === OsEventTypeList.CLICK_EVENT || eventType === undefined` with `console.warn` on undefined (review §4.6)
  - Tap-to-toggle mapping per [g2-app.md](../design/g2-app.md) §7 complete event table:
    - `idle` → start recording
    - `recording` → stop recording
    - `displaying` → dismiss (return to idle)
    - `error` → dismiss
    - `disconnected` → force reconnect
    - `loading`, `transcribing`, `thinking`, `streaming` → ignored
  - `DOUBLE_CLICK_EVENT` in `displaying` → page flip via `rebuildPageContainer` (SDK has no `setPageFlip` method)
  - `FOREGROUND_EXIT_EVENT` while recording → stop mic, send `stop_audio`
  - `FOREGROUND_ENTER_EVENT` → reconnect WS if needed
  - `ABNORMAL_EXIT_EVENT` → cleanup: call `bridge.shutDownContaniner(0)` (note: SDK typo is intentional — method misspells "Container")
  - Scroll throttling: 300ms cooldown on `SCROLL_TOP/BOTTOM_EVENT`
  - Unit tests in `g2_app/src/__tests__/input.test.ts` covering CLICK_EVENT=0/undefined workaround, tap routing per state, scroll throttling (review F.1)

### P2.6 — Display: Recording + Transcribing States

- **Description:** Add recording ([display-layouts.md](../design/display-layouts.md) §4.2) and transcribing ([display-layouts.md](../design/display-layouts.md) §4.3) layouts to `display.ts`. Recording shows audio level bar with Unicode blocks. Transcribing shows animated dots.
- **Owner:** g2-development
- **Dependencies:** P1.8 (display.ts base)
- **Complexity:** S
- **Files created/modified:**
  - `g2_app/src/display.ts` (modify — add `showRecording()`, `showTranscribing()`)
- **Acceptance criteria:**
  - `showRecording()` — 4 containers matching [display-layouts.md](../design/display-layouts.md) §4.2: badge `"● Recording"` in accent (0xC), content `"Listening...\n\n████████░░░░░░░░"`, footer `"Tap to stop"`
  - `showTranscribing()` — 4 containers matching [display-layouts.md](../design/display-layouts.md) §4.3: badge `"● Transcribing"` in accent (0xC), content `"Transcribing audio..."`, footer `"Processing speech"`
  - Recording → full `rebuildPageContainer` on entry and exit
  - Transcribing → `rebuildPageContainer` on entry
  - Animated dots on transcribing via 500ms `textContainerUpgrade` cycle ([display-layouts.md](../design/display-layouts.md) §4.3 notes)

### P2.7 — Wiring Audio into Main App Loop

- **Description:** Integrate `audio.ts`, `input.ts`, and new display states into `main.ts`. Wire tap events to audio start/stop. Wire gateway status frames to display transitions for recording and transcribing states.
- **Owner:** g2-development
- **Dependencies:** P2.4, P2.5, P2.6, P1.9
- **Complexity:** M
- **Files created/modified:**
  - `g2_app/src/main.ts` (modify — integrate audio + input)
- **Acceptance criteria:**
  - Tap from idle → `audio.start()` → display shows recording → state = `recording`
  - Tap from recording → `audio.stop()` → display shows transcribing → state = `transcribing`
  - Gateway `status:transcribing` → display update
  - Gateway `transcription` frame → store text for display in thinking state
  - Gateway `status:thinking` → display shows thinking with "You: {text}"
  - Mock response streams and displays correctly (same as Phase 1 but triggered by voice)
  - `FOREGROUND_EXIT_EVENT` during recording correctly stops mic

### P2.8 — End-to-End Audio Verification

- **Description:** Full pipeline test: tap to record on simulator → speak (simulator sends mock PCM) → tap to stop → Gateway transcribes → mock response displayed. Also test on real hardware if available.
- **Owner:** both
- **Dependencies:** P2.3, P2.7 (all Phase 2 tasks)
- **Complexity:** M
- **Files created:**
  - `tests/integration/test_audio_pipeline.py`
- **Acceptance criteria:**
  - Simulator: tap → "Recording" display → tap → "Transcribing" → transcription appears → mock response streams
  - Gateway logs show: PCM bytes received, buffer duration, Whisper inference time, transcription text
  - Error case: very short recording (<0.5s) → "Transcription failed" error displayed
  - Error case: send `stop_audio` without `start_audio` → error displayed
  - Error case: send `start_audio` while in recording state → error
  - Loading state: restart Gateway → app shows "Starting up..." → model loads → "Ready"
  - Automated test: Python WS client sends `start_audio` + PCM bytes + `stop_audio`, verifies `transcription` frame returned

## Parallel Execution Plan

```
── Time →

Agent A (backend-python):
  [P2.1 audio_buffer.py] ──┐
        ~1.5h               ├──→ [P2.3 server.py audio FSM] ──→ [P2.8 e2e test]
  [P2.2 transcriber.py]  ──┘              ~3h                        ~1h
        ~2h                  (B.3: P2.2 needs only the numpy array spec, not P2.1 code)

Agent B (g2-development):
  [P2.4 audio.ts]  ──────┐
       ~1.5h              │
  [P2.5 input.ts]  ──────┼──→ [P2.7 wire audio into main.ts] ──→ [P2.8 e2e test]
       ~2h                │              ~2h                           ~1h
  [P2.6 display states] ─┘
       ~1h  (B.1: starts immediately — no P2.4 dependency; audio bar is static Unicode)
```

**Parallelization notes:**
- P2.1 (buffer), P2.2 (transcriber), P2.4 (mic capture), P2.5 (input handler) all start immediately in parallel — no interdependencies (review B.3: P2.2 doesn't need P2.1's code, just the numpy array spec from [gateway.md](../design/gateway.md) §4.4)
- P2.6 (display states) starts after P1.8 only, no dependency on P2.4 (review B.1: audio level bar is static Unicode, not driven by PCM RMS)
- P2.3 (server audio FSM) depends on P2.1 + P2.2
- P2.7 (wiring) depends on P2.4 + P2.5 + P2.6 but can start once interfaces are defined
- P2.8 needs everything — joint verification

## Integration Checkpoint

Before moving to Phase 3, verify:

1. **Audio buffer handles both chunk sizes** — 40-byte and 3200-byte chunks produce valid numpy arrays
2. **Whisper transcribes correctly** — test with known audio, verify output
3. **Full audio state machine** — `idle → recording → transcribing → thinking` transitions fire correctly with proper status frames
4. **Mic capture works on simulator** — `onMicData` chunks forwarded as binary WS frames
5. **Input handler routes correctly** — tap-to-toggle works, all event types handled, SDK bug workaround active
6. **Loading state works** — early connection gets `status:loading`, transitions to `idle` when model ready
7. **Error scenarios handled** — empty buffer, overflow, Whisper timeout, empty transcription all send proper error frames
8. **End-to-end latency acceptable** — time from `stop_audio` to `transcription` frame < 5s on CPU with `base.en`

## Definition of Done

- [x] `uv run pytest tests/gateway/` — all tests pass including audio buffer, transcriber, and server audio tests
- [x] Gateway starts, loads Whisper model, and accepts audio input
- [ ] Simulator tap-to-record produces PCM chunks that arrive at Gateway *(needs real simulator/hardware)*
- [x] Gateway transcribes audio and returns `{type:"transcription"}` frame
- [x] App displays "Recording" → "Transcribing" → "Thinking" → mock streamed response → "Displaying"
- [x] All 5 audio error scenarios ([gateway.md](../design/gateway.md) §4.7) produce correct error frames and recovery
- [x] Loading state shown during Whisper model initialization
- [x] Input handler correctly processes all `OsEventTypeList` events including undefined CLICK bug
- [x] `FOREGROUND_EXIT_EVENT` during recording stops mic and sends `stop_audio`
