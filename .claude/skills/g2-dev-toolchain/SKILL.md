---
name: g2-dev-toolchain
description: Even Realities G2 developer toolchain covering the EvenHub CLI, simulator, app scaffolding, app.json manifests, and .ehpk packaging. Use when setting up a G2 dev environment, generating QR codes, running the simulator, authoring manifests, packaging apps, or troubleshooting simulator-vs-hardware differences.
---

# G2 Developer Toolchain

CLI utilities, simulator, scaffolding, manifest authoring, and packaging workflow for building, testing, and shipping G2 apps.

**Canonical reference:** `.agents/skills/g2-dev-toolchain/SKILL.md`. This file is the distilled operating summary — read the canonical file before non-trivial work in this area.

## Core rules

- **Pinned versions (root AGENTS.md):** SDK `@evenrealities/even_hub_sdk` 0.0.11, CLI `@evenrealities/evenhub-cli` 0.1.13, simulator `@evenrealities/evenhub-simulator` 0.7.3. Node.js 22+; `g2_app/` is npm-managed.
- **CLI binary aliases:** both `evenhub` and `eh` work.
- **`evenhub qr` is the primary dev command** — flags: `--url/-u` (overrides everything), `--ip/-i`, `--port/-p`, `--path`, `--https`/`--http`, `--external/-e`, `--scale/-s` (default 4), `--clear` (reset cached settings). Settings (scheme/IP/port/path) are cached between runs.
- **Use the machine's local network IP (192.168.x.x), NOT localhost, in QR URLs** — the phone must reach the dev server over the network.
- **`evenhub init`** scaffolds `app.json` (`--directory/-d`, `--output/-o` default `./app.json`). **`evenhub login`** (`--email/-e`) uses the same account as the Even mobile app and is required before `pack --check`.
- **`evenhub pack <json> <project>`** produces an `.ehpk` via an internal WASM packer — e.g. `evenhub pack app.json dist -o myapp.ehpk`; `--no-ignore` includes dotfiles, `--check/-c` verifies package ID availability. Add `*.ehpk` to `.gitignore`.
- **`app.json` validation:** `package_id` must match `^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$` (reverse-domain, min 2 segments, no hyphens); `edition` must be exactly `"202601"`; `version` must be semver; `min_app_version` required (CLI default `"2.0.0"`); `min_sdk_version` required (use installed SDK version); `supported_languages` from `en`, `de`, `fr`, `es`, `it`, `zh`, `ja`, `ko`; unknown fields rejected by the 0.1.13 Zod schema.
- **Valid permission names:** `g2-microphone`, `phone-microphone`, `album`, `location`, `network`, `camera`. Each permission object needs `name` and `desc` (1-300 chars); `network` may include `whitelist`.
- **Simulator:** native Rust binary (LVGL v9). Key flags: positional `[targetUrl]`, `-c/--config`, `-g/--glow`/`--no-glow`, `-b/--bounce default|spring`, `--list-audio-input-devices`, `--aid <device>`/`--no-aid`, `--print-config-path`.
- **Simulator screenshot (v0.5.0+):** button in the simulator window saves an RGBA PNG to the CWD with a timestamp filename; path logged to stdout. Not affected by `--glow`.
- **Simulator differences vs hardware:** clicks arrive as `sysEvent` (hardware: `textEvent`/`listEvent`); `CLICK_EVENT=0` deserializes as `undefined`; `currentSelectItemIndex` missing at index 0; audio 100 ms/3,200-byte frames (hardware: 10 ms/40 bytes); status events never fire; image size limits not enforced; `textContainerUpgrade` does a full redraw; x/y are i32 (hardware u32); user/device info hardcoded.
- **WebView build must be a single JS bundle** — no code splitting; set Vite `rollupOptions.output.inlineDynamicImports: true`.
- **Vite dev server on port 5173**; `npm run dev` binds localhost only, `npm run dev:network` (`vite --host 0.0.0.0`) for phone access.
- **Generic dev loop:** `npm run dev` → `npx evenhub qr --url "http://<local-ip>:5173"` → scan with the Even App → HMR works live; or preview on desktop with `evenhub-simulator http://localhost:5173`.
- **This project's stack lifecycle is `make sim` / `make stop`** (see below) — prefer it over manual startup during development.
- **Production packaging:** `npm run build` then `npx evenhub pack app.json dist -o myapp.ehpk`.
- **Browser settings pages (not glasses UI)** can use `@jappyjan/even-realities-ui` (React 19, Tailwind-based; import `@jappyjan/even-realities-ui/styles.css`).

## This repo

- **`make sim`** (alias `make restart`) — kill running services, then start OpenClaw daemon, the gateway WebSocket server (port 8765), Vite (port 5173), and `evenhub-simulator`; also brings up the OTel observability stack (`otel-up` dependency in the Makefile). **`make stop`** stops everything; **`make launch`** starts the gateway without killing first.
- **Python CLI equivalents:** `uv run python -m gateway launch [--restart|--no-simulator]`, `uv run python -m gateway stop`.
- **`g2_app/`** — the TypeScript thin client (npm-managed, Vite, port 5173); OpenClaw daemon listens on port 18789.
- **`docs/reference/g2-platform/evenhub_cli.md`**, **`evenhub_simulator.md`**, **`g2_reference_guide.md`** — full CLI, simulator, and platform references.

## Repo policy overrides

- **Ignore canonical cross-references to `docs/guides/`, `docs/design/`, and `docs/archive/`** — those directories do not exist in this repo. Reference docs live only under `docs/reference/g2-platform/`.
