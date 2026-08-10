---
name: g2-events-input
description: Implement and verify G2 OpenClaw touch/ring events, state routing, microphone PCM, IMU, device status, exit, and lifecycle handling.
---

# G2 events and input

Read the canonical `.agents/skills/g2-events-input/SKILL.md` before non-trivial work.

- SDK `0.0.11` event values are 0-8, including system exit and IMU reports; `eventSource` distinguishes right temple, ring, and left temple.
- Protobuf zero values may be omitted: handle click as `CLICK_EVENT` or `undefined`, and use `currentSelectItemIndex ?? 0`.
- Text capture routes swipes through `textEvent` and presses through `sysEvent`; list capture routes selection through `listEvent` and double press through `sysEvent`.
- Do not model input as key-down/up, and do not treat arbitrary empty events as clicks without hardware evidence.
- Preserve state-aware press/double-press behavior while keeping a reachable root `shutDownPageContainer(1)` flow.
- Do not stop resources before the cancellable exit dialog resolves; clean up on system/abnormal exit.
- Consume `audioEvent.audioPcm` as arbitrary-boundary 16 kHz S16LE mono. Use `AudioInputSource.Glasses`; never add `onMicData` casts or hardcode 40-byte packets.
- The current `shutDownContaniner` any-cast and bare-event click fallback in `g2_app/src/input.ts` are compatibility debt, not SDK truth.
- Test source distinctions, permissions, PCM transfer, lifecycle, timing, and exit on hardware.

Repository implementation: `g2_app/src/input.ts`, `g2_app/src/state.ts`, and gateway audio handling.
