---
name: g2-dev-toolchain
description: Build, test, sideload, package, and migrate G2 OpenClaw's Even Hub app, manifest, permissions, networking, CLI, and simulator workflow.
---

# G2 development toolchain

Read the canonical `.agents/skills/g2-dev-toolchain/SKILL.md` before non-trivial work.

- Use Node 22+ and npm in `g2_app/`. The lock resolves SDK `0.0.11` and CLI `0.1.13`; the simulator is global and must be version-checked.
- Current audited upstream is SDK `0.0.12` and simulator `0.8.0`; migrate deliberately and update `min_sdk_version` only when required.
- Keep the current `app.json` schema, declare only used permissions, maintain the network whitelist and browser CORS, and keep credentials out of client assets.
- Run `npm test`, `npm run typecheck`, and `npm run pack` in `g2_app/`; use `make sim`/`make stop` for the managed stack.
- Use a LAN IP for QR testing and keep dev-server exposure intentional.
- Use `g2-sim-automation` for the product `/_dev` API and `g2-simulator-automation` for simulator 0.8.0 native screenshots/logs/input.
- Require physical hardware for permissions, BLE timing, fonts, audio, source distinctions, backgrounding, and release-critical behavior.
- Exclude secrets, `.env`, local credentials, build output, screenshots, and `.ehpk` artifacts according to repository policy.
