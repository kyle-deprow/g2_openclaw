---
name: g2-simulator-automation
description: Run and automate the official EvenHub Simulator for G2 OpenClaw. Use for simulator 0.8.0 launch, native HTTP input injection, framebuffer or WebView screenshots, console inspection, CI smoke tests, and simulator-versus-hardware diagnosis.
---

# G2 official simulator automation

Use the official simulator `0.8.0` HTTP control plane for native display, input, and console evidence. Use the separate `g2-sim-automation` skill for G2 OpenClaw's product-specific `/_dev` API and gateway journeys.

## Start explicitly

Run the G2 OpenClaw Vite simulator server, then launch a simulator `0.8.0` process with automation enabled:

```bash
cd g2_app
npm run dev:sim
evenhub-simulator http://localhost:5173 --automation-port 9898
```

Keep automation bound to `127.0.0.1`. Supervise and terminate only the exact child/PID; use `make stop` for the repository-managed stack and never broad `pkill` patterns.

## Wait for readiness

Input sent before the first event-capturing container exists is silently dropped.

1. Preserve startup logs and poll `GET /api/console` without `since_id` first.
2. Track the latest ID and then poll `GET /api/console?since_id=<last-id>` with a non-negative ID.
3. Wait for the existing `[Display] _createStartup complete` marker instead of relying only on a fixed sleep.
4. Allow roughly four seconds for initial startup, then use the marker and framebuffer state.

## Use native endpoints

- `GET /api/ping`: health check.
- `GET /api/screenshot/glasses`: 576×288 RGBA framebuffer PNG.
- `GET /api/screenshot/webview`: host WebView PNG.
- `GET /api/console?since_id=N`: incremental console/error/fetch logs.
- `DELETE /api/console`: clear logs only after startup evidence is saved.
- `POST /api/input` with `{ "action": "click" | "double_click" | "up" | "down" }`: inject glasses input.

Consult `evenhub-simulator --help` before depending on any additional option or endpoint.

## Assert behavior

- Decode glasses screenshots as RGBA and use alpha (`alpha > 0`) to detect lit pixels.
- Compare relevant regions or semantic states instead of PNG file size.
- Inspect logs for uncaught errors, rejected promises, failed fetches, and SDK validation failures.
- Capture before/after frames around each injected action.
- Pair native evidence with `g2-sim-automation` when a test also needs exact product state, conversation data, or gateway controls.

Minimum smoke journey:

1. Boot and observe the startup marker.
2. Assert lit framebuffer pixels and no startup errors.
3. Inject the primary click and verify the intended state/display change.
4. Exercise double-click behavior for the current app state and separately verify a reachable system-exit-confirmation path.
5. Save logs and screenshots on failure.

## Respect simulator limits

- Treat the simulator as layout and logic tooling, not a hardware emulator.
- Do not trust it for pixel-perfect fonts/greyscale, BLE timing, compressed image transfer, real status events, production permissions, or background lifecycle.
- Simulator `0.8.0` uses host audio and emits 100 ms PCM events (3,200 bytes at 16 kHz S16LE mono); application code must remain chunk-agnostic.
- Its `eventSource` is fixed to right-glasses input; validate ring and left-temple distinctions on hardware.

Official reference: [EvenHub Simulator](https://hub.evenrealities.com/docs/test/simulator).
