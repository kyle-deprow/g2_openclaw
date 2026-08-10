---
name: g2-dev-toolchain
description: Configure, build, test, sideload, package, and upgrade the G2 OpenClaw Even Hub app. Use for Node/Vite setup, dependency pins, app.json, permissions and network whitelists, CLI commands, QR testing, simulator setup, .ehpk packaging, or release-readiness checks.
---

# G2 development toolchain

Use repeatable builds and validate through progressively more realistic environments.

## Respect the repository baseline

- Node.js: 22 or newer.
- `g2_app/package-lock.json` currently resolves `@evenrealities/even_hub_sdk` `0.0.11` and `@evenrealities/evenhub-cli` `0.1.13`.
- `g2_app/app.json.min_sdk_version` is `0.0.11`.
- The simulator is installed globally by the bootstrap workflow rather than locked in `g2_app`; inspect `evenhub-simulator --version` before relying on version-specific behavior.
- Current audited upstream versions are SDK `0.0.12`, CLI `0.1.13`, and simulator `0.8.0`.

Do not use `latest` in committed dependencies. Before upgrading, read intervening changelogs, inspect the new `.d.ts`, update `min_sdk_version` only when justified, and rerun simulator plus real-device tests. SDK `0.0.12` adds z-order support and validation; it is not silently available to the locked `0.0.11` app.

Use `npm` in `g2_app/` and commit `package-lock.json`. Build to `g2_app/dist/`. Keep application and SDK code browser-compatible; no Node-only modules belong in the WebView bundle.

## Maintain a valid manifest

- `package_id`: lowercase reverse-domain identifier; each segment starts with a letter; no hyphens.
- `edition`: current schema value `"202601"`.
- `name`: at most 20 characters.
- `version`: three-part semver.
- `min_app_version` and `min_sdk_version`: required strings.
- `entrypoint`: must exist inside `dist/`.
- `permissions`: objects with a non-empty `name` and `desc`.
- `supported_languages`: values from the current supported set.

Declare only used capabilities. Current permission names include `network`, `location`, `g2-microphone`, `phone-microphone`, `album`, and `camera`.

For `network`, list each allowed origin in `whitelist`. The whitelist is an Even-side permission gate, not a CORS bypass; the gateway must also return valid browser CORS headers. Use HTTPS/WSS outside controlled local development and never ship credentials in URLs or the bundle.

## Use repository commands

```bash
cd g2_app
npm install
npm test
npm run typecheck
npm run dev:sim

# From repository root
make sim
make stop

# Phone sideloading and package
cd g2_app
npm run dev:network
npx evenhub qr --url "http://<lan-ip>:5173"
npm run pack
```

Use the machine's LAN IP for QR sideloading, not `localhost` or `0.0.0.0`. Keep server exposure intentional and firewall-aware.

## Choose the simulator control layer

- Use `g2-sim-automation` for G2 OpenClaw's local `/_dev` API, exact display text, gateway state, and product journeys.
- Use `g2-simulator-automation` with simulator `0.8.0` for native framebuffer/WebView screenshots, injected glasses input, and console logs.
- These layers complement each other. Do not expose the app-level `/_dev` API to the LAN or replace it merely because the official simulator has a native control plane.

## Test in layers

1. Run TypeScript type-checks and unit tests.
2. Run the current product simulator workflow for state and gateway integration.
3. Use native simulator automation for repeatable layout and input smoke checks when simulator `0.8.0` is available.
4. QR-sideload to physical G2 hardware for permissions, BLE behavior, fonts, audio, lifecycle, and timing.
5. Test a private `.ehpk` build before release.

The simulator is not a hardware emulator. Do not accept simulator-only evidence for visual QA, image transfer, device status, background suspension, BLE timing, or production permissions.

## Package safely

- Ensure `dist/index.html` matches `app.json.entrypoint`.
- Exclude secrets, `.env` files, local credentials, and source maps unless intentional.
- Keep `dist/`, `node_modules/`, screenshots, and `*.ehpk` ignored unless artifact policy says otherwise.
- Validate that the root interaction model exposes the system exit confirmation.
- Record tested SDK, CLI, simulator, Even App, firmware, and hardware versions for releases.

Official references: [quickstart](https://hub.evenrealities.com/docs/get-started/quickstart/index), [packaging](https://hub.evenrealities.com/docs/ship/packaging), [CLI](https://hub.evenrealities.com/docs/reference/cli), [networking](https://hub.evenrealities.com/docs/build/networking), and [versioning](https://hub.evenrealities.com/docs/reference/versioning).
