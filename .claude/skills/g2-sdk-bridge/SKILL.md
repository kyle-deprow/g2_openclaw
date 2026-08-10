---
name: g2-sdk-bridge
description: Integrate and verify G2 OpenClaw's installed EvenAppBridge SDK contract. Use for bridge lifecycle, page calls, audio/device APIs, storage, or SDK migrations.
---

# G2 SDK bridge

Read the canonical `.agents/skills/g2-sdk-bridge/SKILL.md` before non-trivial work.

- Inspect `g2_app/package.json`, lockfile, manifest, and installed `dist/index.d.ts`; declarations outrank remembered examples and `docs/reference/g2-platform/`.
- The lockfile and manifest use SDK `0.0.11`. Upstream `0.0.12` is audited but its z-order additions require an explicit dependency/manifest migration.
- Initialize with `waitForEvenAppBridge()`. Call `createStartUpPageContainer()` once, then use text upgrades or rebuilds.
- Use `borderRadius` and bridge method `shutDownPageContainer`; `borderRdaius` and `shutDownContaniner` are stale/nonexistent calls.
- SDK `0.0.11` already includes event values 0-8, input sources, `AudioInputSource`, IMU, location, album, camera, host storage, and launch/device callbacks.
- Do not invent `setLayout`, `setPageFlip`, `ContainerData`, key-down/up, `sendData`, `setNotification`, or `onMicData`.
- Serialize display/image operations, check return values, and use root `shutDownPageContainer(1)` for cancellable system exit.
- Confirm permissions, timing, audio, lifecycle, and critical paths on physical hardware.

Repository entry points: `g2_app/src/main.ts`, `g2_app/src/display.ts`, and `g2_app/src/input.ts`.
