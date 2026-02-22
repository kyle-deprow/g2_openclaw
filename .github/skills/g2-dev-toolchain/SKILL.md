---
name: g2-dev-toolchain
description:
  Even Realities G2 developer toolchain covering the EvenHub CLI, simulator, app scaffolding, packaging, and deployment workflow. Use when setting up a G2 dev environment, generating QR codes, running the simulator, structuring an EvenHub app, creating app.json manifests, or packaging .ehpk files. Triggers on tasks involving evenhub CLI, evenhub-simulator, app.json, .ehpk packaging, QR code generation, or G2 development workflow.
---

# G2 Developer Toolchain

Complete reference for the Even Realities G2 AR glasses developer toolchain:
CLI utilities, simulator, app scaffolding, manifest authoring, packaging, and
deployment. Covers every command, flag, and workflow step needed to build, test,
and ship G2 apps.

## When to Apply

Reference these guidelines when:

- Setting up a new G2 development environment from scratch
- Generating QR codes to load apps on glasses via the Even App
- Scaffolding a new EvenHub app project
- Authoring or modifying `app.json` manifest files
- Running the desktop simulator for previewing glasses UI
- Packaging `.ehpk` files for distribution
- Configuring Vite or other dev servers for G2 development
- Troubleshooting differences between simulator and real hardware

---

## Ecosystem Overview

| Package | Purpose | Version |
|---|---|---|
| `@evenrealities/even_hub_sdk` | TypeScript SDK for WebView ↔ Even App communication | 0.0.7 |
| `@evenrealities/evenhub-cli` | CLI for dev workflow: QR codes, init, login, packaging | 0.1.5 |
| `@evenrealities/evenhub-simulator` | Desktop simulator for previewing glasses UI | 0.4.1 |
| `@jappyjan/even-realities-ui` | React component library for browser settings pages | community |

---

## Installation

```bash
# SDK (project dependency)
npm install @evenrealities/even_hub_sdk

# CLI (global or dev dependency)
npm install -g @evenrealities/evenhub-cli
# or local: npm install -D @evenrealities/evenhub-cli

# Simulator (global)
npm install -g @evenrealities/evenhub-simulator
```

---

## CLI Tool (`evenhub` / `eh`)

Binary aliases: both `evenhub` and `eh` work. Built with Commander.js, uses inquirer for prompts.

### `evenhub qr` — Generate QR Code (PRIMARY dev command)

#### All Options

| Flag | Alias | Type | Default | Description |
|---|---|---|---|---|
| `--url <url>` | `-u` | string | — | Full URL, overrides all other options |
| `--ip <ip>` | `-i` | string | auto-detected | IP address or hostname |
| `--port [port]` | `-p` | string | prompted | Port (empty = no port in URL) |
| `--path <path>` | — | string | prompted | URL path |
| `--https` | — | boolean | false | Use HTTPS scheme |
| `--http` | — | boolean | true | Use HTTP scheme (default) |
| `--external` | `-e` | boolean | false | Open QR in external image viewer |
| `--scale <scale>` | `-s` | number | 4 | Scale factor for external QR image |
| `--clear` | — | boolean | false | Clear cached settings before prompting |

**IMPORTANT:** Use machine's local network IP (192.168.x.x), NOT localhost — phone must reach dev server over network.

**Caching:** CLI caches scheme, IP, port, path between runs. Use `--clear` to reset.

#### Examples

```bash
# Basic — interactive prompts for IP, port, path
evenhub qr

# Full URL override (skips all prompts)
evenhub qr --url "http://192.168.1.42:5173"

# Specify IP and port directly
evenhub qr --ip 192.168.1.42 --port 5173

# Short alias with explicit port
eh qr -i 192.168.1.42 -p 5173

# Open QR in external viewer (useful for large screens / screen sharing)
evenhub qr --url "http://192.168.1.42:5173" --external --scale 8

# Clear cached settings and start fresh
evenhub qr --clear
```

---

### `evenhub init` — Initialize Project

#### Options

| Flag | Alias | Type | Default | Description |
|---|---|---|---|---|
| `--directory <dir>` | `-d` | string | `./` | Target directory for the generated file |
| `--output <output>` | `-o` | string | `./app.json` | Output file path (takes precedence over `--directory`) |

---

### `evenhub login` — Authenticate

#### Options

| Flag | Alias | Type | Default | Description |
|---|---|---|---|---|
| `--email <email>` | `-e` | string | prompted | Account email address |

- Same account as Even mobile app
- Credentials saved locally
- Required before `evenhub pack --check`

---

### `evenhub pack` — Package App

Produces `.ehpk` file using internal WASM packer.

#### Arguments and Options

| Argument/Flag | Type | Default | Description |
|---|---|---|---|
| `<json>` (positional) | string | required | Path to `app.json` manifest |
| `<project>` (positional) | string | required | Path to built output folder (e.g. `dist`) |
| `--output <output>` / `-o` | string | `out.ehpk` | Output `.ehpk` filename |
| `--no-ignore` | boolean | false | Include dotfiles in the package |
| `--check` / `-c` | boolean | false | Check package ID availability (requires login) |

#### Examples

```bash
evenhub pack app.json dist                          # basic (produces out.ehpk)
evenhub pack app.json dist -o myapp.ehpk             # custom output
evenhub pack app.json dist -o myapp.ehpk --check     # check ID availability
```

---

## Simulator (`evenhub-simulator`)

Native Rust binary using LVGL v9 for rendering. Thin Node.js wrapper.

### CLI Options

| Flag | Description |
|---|---|
| `[targetUrl]` | URL to load on startup (positional argument) |
| `-c, --config <path>` | Path to config file |
| `-g, --glow` / `--no-glow` | Enable/disable glow effect |
| `-b, --bounce <type>` | Bounce animation: `default` or `spring` |
| `--list-audio-input-devices` | List available audio input devices |
| `--aid <device>` / `--no-aid` | Choose specific audio device / use default |
| `--print-config-path` | Print the config file path |
| `--completions <shell>` | Print shell completions (bash/elvish/fish/powershell/zsh) |

Config file paths: Linux `~/.config`, macOS `~/Library/Application Support`, Windows `AppData/Roaming`.

Platform binaries (optional deps): `@evenrealities/sim-linux-x64`, `@evenrealities/sim-win32-x64`, `@evenrealities/sim-darwin-x64`, `@evenrealities/sim-darwin-arm64`.

### Simulator Limitations vs Real Hardware

| Feature | Simulator | Real Hardware |
|---|---|---|
| Font rendering | Different (desktop fonts) | Native firmware fonts |
| List scrolling | Re-implemented in sim | Firmware native scrolling |
| Image size limits | Not enforced | Enforced (will reject oversize) |
| Status events | Hardcoded values, never fire | Real events from hardware |
| Audio delivery | 100ms/event (3200 bytes) | 10ms/event (40 bytes) |
| Event source | `sysEvent` for clicks | `textEvent`/`listEvent` |
| `textContainerUpgrade` | Full visual redraw | Smooth in-place update |
| User/device info | Hardcoded placeholder values | Real user and device data |

---

## App Structure

Minimal file structure:

```
my-app/
  index.html          ← entry point loaded by glasses WebView
  package.json         ← dependencies and scripts
  vite.config.ts       ← dev server configuration
  app.json             ← app metadata manifest (for packaging)
  src/
    main.ts            ← app bootstrap
    styles.css         ← stylesheet
```

### `index.html`

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>My G2 App</title>
</head>
<body>
  <script type="module" src="/src/main.ts"></script>
</body>
</html>
```

### `package.json`

```json
{
  "name": "my-g2-app",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "dev": "vite --host 0.0.0.0 --port 5173",
    "build": "vite build",
    "qr": "evenhub qr --http --port 5173",
    "pack": "npm run build && evenhub pack app.json dist -o myapp.ehpk"
  },
  "dependencies": {
    "@evenrealities/even_hub_sdk": "^0.0.7"
  },
  "devDependencies": {
    "typescript": "^5.5.0",
    "vite": "^6.0.0",
    "@evenrealities/evenhub-cli": "^0.1.5"
  }
}
```

### `vite.config.ts`

```typescript
import { defineConfig } from "vite";

export default defineConfig({
  server: {
    host: true,    // bind to 0.0.0.0 so phone can reach the server
    port: 5173,
  },
});
```

### `src/main.ts`

```typescript
import { EvenHubSDK } from "@evenrealities/even_hub_sdk";

const sdk = new EvenHubSDK();

// Your app logic here
console.log("G2 app started");
```

### `src/styles.css`

```css
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}
```

---

## `app.json` Manifest (CRITICAL)

### Full Schema

```json
{
  "package_id": "com.example.myapp",
  "edition": "202601",
  "name": "My app",
  "version": "1.0.0",
  "min_app_version": "0.1.0",
  "tagline": "Short description",
  "description": "Longer description of what the app does",
  "author": "Your Name",
  "entrypoint": "index.html",
  "permissions": {
    "network": ["api.example.com"],
    "fs": ["./assets"]
  }
}
```

**`package_id` rules:** Reverse-domain, each segment starts with lowercase letter, only lowercase letters + numbers. No hyphens. `com.myname.myapp` = valid, `com.my-name.my-app` = invalid.

**`permissions.network`:** List domains. Use `["*"]` for unrestricted.

---

## Development Workflow

```
1. npm install @evenrealities/even_hub_sdk
2. Code your web app (any framework — just HTML + TS + SDK)
3. npm run dev (Vite on port 5173 with --host 0.0.0.0)
4. Terminal 2: npx evenhub qr --url "http://<local-ip>:5173"
5. Scan QR with Even App on iPhone
6. App loads on glasses; Vite HMR works for live changes
7. OR: evenhub-simulator http://localhost:5173 (desktop preview)
```

---

## Production Packaging

```bash
npm run build
npx evenhub pack app.json dist -o myapp.ehpk
```

Add `*.ehpk` to `.gitignore`.

---

## Recommended npm Scripts

```json
{
  "scripts": {
    "dev": "vite --host 0.0.0.0 --port 5173",
    "build": "vite build",
    "qr": "evenhub qr --http --port 5173",
    "pack": "npm run build && evenhub pack app.json dist -o myapp.ehpk"
  }
}
```

---

## even-dev Shared Environment

Repository: https://github.com/BxNxM/even-dev

Handles app discovery, dep installation, Vite config, simulator launch.

- `./start-even.sh` prompts for app selection
- `APP_NAME=demo ./start-even.sh` or `APP_PATH=../my-app ./start-even.sh`
- External apps registered in `apps.json` (git URLs auto-cloned)

---

## Browser UI Library (`@jappyjan/even-realities-ui`)

React 19 component library for settings/config WebView pages (NOT glasses display).

- Components: Button, IconButton, Card, Text, Input, Textarea, Select, Checkbox, Radio, Switch, Badge, Chip, Divider
- 90+ icons (hardware, battery, navigation, actions, features, settings)
- Tailwind-based styling, import `@jappyjan/even-realities-ui/styles.css`
- Design tokens as CSS custom properties (colors, typography, spacing)

---

## Reference Apps

| App | Description | Link |
|---|---|---|
| chess | Full app, tests, modular architecture | github.com/dmyster145/EvenChess |
| reddit | Clean app, API proxy, packaging | github.com/fuutott/rdt-even-g2-rddit-client |
| weather | Settings UI, plain CSS | github.com/nickustinov/weather-even-g2 |
| tesla | Image rendering, backend integration | github.com/nickustinov/tesla-even-g2 |
| pong | Canvas game, image container | github.com/nickustinov/pong-even-g2 |
| snake | Canvas game, image container | github.com/nickustinov/snake-even-g2 |

---

## Tips

- Use inline styles or plain CSS — avoid Tailwind requiring build plugins
- Keep apps standalone — should work with just `npm run dev`
- If app needs backend, put in `server/` with own `package.json` (even-dev auto-detects)
- Use `@jappyjan/even-realities-ui` for browser settings page consistency
