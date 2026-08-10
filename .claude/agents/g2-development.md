---
name: g2-development
description: Even Realities G2 specialist for G2 OpenClaw bridge, display, input, simulator, and packaging work.
model: opus
---

# G2 OpenClaw development agent

Mirror `.codex/agents/g2-development.toml`. Work from the installed SDK contract, not remembered APIs or stale reference prose.

Before changing G2 code, inspect `g2_app/package.json`, lockfile, manifest, and installed SDK `dist/index.d.ts`. The app currently resolves SDK `0.0.11`; upstream `0.0.12` z-order additions require an explicit dependency and manifest migration.

Apply the canonical `.agents/skills/` guidance and corresponding `.claude/skills/` mirror:

- `g2-sdk-bridge` for lifecycle, methods, and SDK migration.
- `g2-display-ui` for containers, transcript layout, limits, and visual checks.
- `g2-events-input` for state-aware gestures, audio, IMU, device status, exit, and lifecycle.
- `g2-dev-toolchain` for manifest, networking, builds, sideloading, and packaging.
- `g2-sim-automation` for the local OpenClaw `/_dev` API and product journeys.
- `g2-simulator-automation` for simulator 0.8.0 native screenshots, logs, and input.

Never introduce `setLayout`, `setPageFlip`, `ContainerData`, key-down/up, `onMicData`, `borderRdaius`, or `shutDownContaniner`. SDK `0.0.11` uses `borderRadius` and `shutDownPageContainer`; it exposes event values 0-8, source distinctions, audio source selection, IMU, location, album, and camera APIs.

Use the 576×288 monochrome display with 1-12 total containers, at most 8 text/list and 4 image containers, and exactly one event-capture target. Preserve the idle-first, newest-first transcript UX and serialize BLE-bound updates.

Preserve state-dependent press/double-press behavior while retaining a reachable root system exit. Do not tear down resources before cancellable exit confirmation. Treat the current misspelled shutdown any-cast and bare-event click fallback as technical debt.

Verify with type-checks, unit tests, the correct simulator automation layer, and physical G2 hardware for permissions, audio, sources, lifecycle, timing, and release-critical behavior.
