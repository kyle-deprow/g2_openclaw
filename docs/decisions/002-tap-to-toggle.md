# ADR-002: Tap-to-Toggle (not Hold-to-Talk)

**Date:** 2026-02-22
**Status:** Accepted

---

## Context

The original architecture documents (01 §5, 03 §6–§7) assumed a
**hold-to-record, release-to-send** interaction model: the user holds
the R1 ring button to open the microphone, and releases it to signal
end-of-speech. Sequence diagrams, state machine transitions, and UX
instructions were all written around this model.

The Phase 0 SDK verification spike (`phase0-sdk-findings.md`) and the
subsequent architecture review (§2.1) established that hold-to-talk is
**impossible** with the G2 SDK. The Even Realities `EvenAppBridge`
(v0.0.7) fires only discrete event types via `onEvenHubEvent`:

- `OsEventType.CLICK_EVENT` — single tap
- `OsEventType.DOUBLE_CLICK_EVENT` — double tap

There is no `HOLD_EVENT`, no `KEY_DOWN` / `KEY_UP`, and no way to detect
press duration. The SDK simply does not surface hold or release signals.

## Decision

We adopt a **tap-to-start / tap-to-stop toggle** model for voice
recording:

1. **First tap** (`CLICK_EVENT`) → transition from `idle` to `recording`.
   The app sends `{type:"start_audio"}` to the PC Gateway and opens the
   microphone via `bridge.audioControl(true)`.
2. **Second tap** (`CLICK_EVENT`) → transition from `recording` to
   `transcribing`. The app sends `{type:"stop_audio"}` to the PC Gateway
   and closes the microphone via `bridge.audioControl(false)`.

All state machines, sequence diagrams, protocol definitions, and UX copy
use this toggle model exclusively. References to "hold ring" or "release
ring" are removed from all documentation.

## Consequences

**Benefits:**

- Aligns the design with the actual SDK capabilities — no fictional
  events or workarounds required.
- Simpler event handling: one event type (`CLICK_EVENT`) drives both
  transitions.
- Toggle model is familiar to users (walkie-talkie / voice-memo pattern).

**Risks and mitigations:**

- **Accidental double-tap** could start and immediately stop a recording,
  producing an empty or near-empty audio buffer. Mitigated by state
  machine guards: ignore a `CLICK_EVENT` arriving within 300 ms of the
  previous one (debounce).
- **User confusion** — without haptic or audio feedback on state change
  (G2 has no speaker), the user must rely on the glasses display status
  indicator to confirm recording state. The app renders a visible status
  line ("Recording…" / "Idle") on every transition.
- **Orphaned recording** — if the user forgets to tap again, the mic
  stays open indefinitely. Mitigated by a server-side silence timeout
  (configurable, default 30 s) that auto-sends a stop signal.
