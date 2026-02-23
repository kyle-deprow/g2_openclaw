# EvenHub CLI — Exhaustive Developer Notes

> **Document generated:** 2026-02-21
> **Source:** <https://www.npmjs.com/package/@evenrealities/evenhub-cli> and npm registry API

---

## 1. Package Identity

| Field | Value |
|---|---|
| **Package name** | `@evenrealities/evenhub-cli` |
| **Registry** | npm (public) |
| **Version (latest)** | `0.1.5` |
| **Description** | Command-line interface for EvenHub development and app management. |
| **License** | *none* (not specified in package metadata) |
| **Unpacked size** | 1.6 MB (~1,595,766 bytes) |
| **Total files** | 9 |
| **Module entry** | `index.ts` |
| **Package type** | `"type": "module"` (ESM) |
| **Published** | 2026-01-28T10:29:15.068Z |
| **Versions count** | 1 (only `0.1.5` has been published) |
| **Node version used to publish** | 25.4.0 |
| **npm version used to publish** | 11.7.0 |
| **Built from** | `/Users/even/dev-portal/evenhub-cli/evenrealities-evenhub-cli-0.1.5.tgz` (local tgz) |
| **Weekly downloads** | ~39 (at time of research) |
| **Dependents** | 0 |

### Maintainers / Collaborators

| Name | Email |
|---|---|
| whiskee.chen | whiskee.chen@evenrealities.com |
| carson.zhu | carson.zhu@evenrealities.com |

### Binary / Executable Names

The package registers **two** CLI binary aliases:

```json
"bin": {
  "evenhub": "main.js",
  "eh": "main.js"
}
```

Both `evenhub` and `eh` invoke the same `main.js` entry point. You can use either interchangeably.

### Repository

**No repository field** is declared in the package metadata. There is no public GitHub repository linked.

---

## 2. Installation

### Global install (recommended for CLI usage)

```bash
npm install -g @evenrealities/evenhub-cli
```

### Local / project install

```bash
npm install @evenrealities/evenhub-cli
```

After local install, invoke via `npx evenhub` or `npx eh`.

### Using other package managers

```bash
# yarn
yarn global add @evenrealities/evenhub-cli

# pnpm
pnpm add -g @evenrealities/evenhub-cli
```

---

## 3. What the CLI Does

EvenHub CLI is the **developer toolchain** for the Even Realities EvenHub ecosystem. It supports:

1. **QR code generation** — Generate a QR code encoding your local dev server URL so the Even App on your phone can connect to your development server. This is the primary workflow during development.
2. **Project initialization** — Scaffold a new project with a basic `app.json` configuration file.
3. **Authentication** — Log in to the EvenHub platform with your Even Realities account.
4. **Project packaging** — Pack your built project into an `.ehpk` file for submission/distribution as an EvenHub app.

### How It Fits in the Ecosystem

The CLI is one of three main developer tools in the Even Realities ecosystem:

| Package | Purpose |
|---|---|
| `@evenrealities/evenhub-cli` | CLI for dev workflow: QR codes, init, login, packaging |
| `@evenrealities/even_hub_sdk` (v0.0.7) | TypeScript SDK for WebView ↔ Even App communication |
| `@evenrealities/evenhub-simulator` (v0.4.1) | Desktop simulator for previewing glasses UI |

**Development workflow:**
1. Use `evenhub init` to create an `app.json` project config.
2. Build your web app using the `@evenrealities/even_hub_sdk`.
3. Use `evenhub qr` to generate a QR code for your dev server.
4. Scan the QR code with the Even App on your phone → the app loads your dev server URL in its WebView → communicates with glasses via the SDK.
5. Alternatively, use `@evenrealities/evenhub-simulator` for desktop preview.
6. When ready for submission, use `evenhub login` + `evenhub pack` to produce an `.ehpk` file.

---

## 4. Commands — Complete Reference

### 4.1 `qr` — Generate QR Code

**Purpose:** Generate a QR code for your development server URL. This is the primary command for using dev mode with the Even app.

**Quick start note:** For development mode with the Even app, the `qr` command is the **only command you need** in the initial development phase.

#### Basic Usage

```bash
evenhub qr
```

The command will:
- Automatically detect your local IP address.
- Prompt you for the port and path.
- On subsequent runs, it will **remember your previous settings** (cached).

#### All Options

| Flag | Alias | Type | Default | Description |
|---|---|---|---|---|
| `--url <url>` | `-u` | `string` | — | Provide a full URL directly. **Overrides** all other options (`--ip`, `--port`, `--path`, `--https`/`--http`). |
| `--ip <ip>` | `-i` | `string` | Auto-detected local IP | Specify the IP address or hostname. |
| `--port [port]` | `-p` | `string` (optional value) | Prompted interactively | Specify the port. Leave the value empty (bare `--port`) for no port in the URL. |
| `--path <path>` | — | `string` | Prompted interactively | Specify the URL path. |
| `--https` | — | `boolean` | `false` | Use HTTPS instead of HTTP. |
| `--http` | — | `boolean` | `true` (default scheme) | Use HTTP instead of HTTPS. |
| `--external` | `-e` | `boolean` | `false` | Open the QR code in an external program (image viewer) instead of rendering in the terminal. |
| `--scale <scale>` | `-s` | `number` | `4` | Scale factor for the external QR code image. Only applies when `--external` is used. |
| `--clear` | — | `boolean` | `false` | Clear all cached settings (scheme, IP, port, and path) so the next run starts fresh. |

#### Examples

```bash
# Generate QR code with auto-detected IP (interactive prompts for port/path)
evenhub qr

# Generate QR code for a specific URL (bypasses all other options)
evenhub qr --url http://192.168.1.100:3000

# Generate QR code with specific IP and port
evenhub qr --ip 192.168.1.100 --port 3000

# Open QR code in an external image viewer
evenhub qr --external

# Use HTTPS scheme
evenhub qr --https --ip myhost.local --port 8443

# Clear cached settings
evenhub qr --clear
```

#### Behavior Details

- **Caching:** The CLI caches your scheme, IP, port, and path choices between runs. Subsequent invocations pre-fill your previous values. Use `--clear` to reset.
- **Terminal rendering:** By default, the QR code is rendered directly in the terminal using ASCII/Unicode art (via the `qrcode` library).
- **External rendering:** With `--external` / `-e`, the QR code is opened in an external program (uses the `open` npm package to launch the system's default image viewer). The `--scale` factor controls the pixel size of the generated image.

---

### 4.2 `init` — Initialize Project

**Purpose:** Initialize a new project with a basic `app.json` configuration file.

#### Usage

```bash
evenhub init [options]
```

#### All Options

| Flag | Alias | Type | Default | Description |
|---|---|---|---|---|
| `--directory <directory>` | `-d` | `string` | `./` | Directory to create the project in. |
| `--output <output>` | `-o` | `string` | `./app.json` | Output file path for the config file. **Takes precedence** over `--directory` when both are specified. |

#### Examples

```bash
# Initialize in the current directory (creates ./app.json)
evenhub init

# Initialize in a specific directory
evenhub init --directory ./my-app

# Specify an exact output path
evenhub init --output ./config/app.json
```

#### Behavior Details

- Creates an `app.json` metadata file that describes your EvenHub app.
- This `app.json` is required by the `evenhub pack` command later.
- If `--output` is specified, it takes precedence over `--directory`.

---

### 4.3 `login` — Login to EvenHub

**Purpose:** Log in using your Even Realities account (the same account used in the Even mobile app). Credentials are saved locally for future use.

#### Usage

```bash
evenhub login [options]
```

#### All Options

| Flag | Alias | Type | Default | Description |
|---|---|---|---|---|
| `--email <email>` | `-e` | `string` | Prompted interactively | Your Even Realities account email address. |

#### Examples

```bash
# Interactive login (prompts for email and password)
evenhub login

# Provide email upfront (will prompt only for password)
evenhub login --email user@example.com
```

#### Behavior Details

- Uses the `inquirer` library for interactive prompts.
- Credentials are **saved locally** so you don't need to log in again.
- The account must be the same one used in the Even mobile app.
- Login is required before using `evenhub pack --check` (to verify package ID availability).

---

### 4.4 `pack` — Pack Project

**Purpose:** Pack your project into an `.ehpk` file ready for app creation/submission to the EvenHub platform.

#### Usage

```bash
evenhub pack <json> <project> [options]
```

#### Positional Arguments

| Argument | Required | Description |
|---|---|---|
| `<json>` | Yes | Path to your `app.json` metadata file (created by `evenhub init`). |
| `<project>` | Yes | Path to your built project folder (e.g., `dist`, `build`). |

#### All Options

| Flag | Alias | Type | Default | Description |
|---|---|---|---|---|
| `--output <output>` | `-o` | `string` | `out.ehpk` | Output file name for the packed `.ehpk` file. |
| `--no-ignore` | — | `boolean` | `false` (hidden files are excluded by default) | Include hidden files (files/folders starting with `.`). By default, dotfiles are ignored. |
| `--check` | `-c` | `boolean` | `false` | Check if the package ID is available on the EvenHub platform before packing. Requires `evenhub login` first. |

#### Examples

```bash
# Basic pack: app.json + dist folder → out.ehpk
evenhub pack app.json ./dist

# Custom output filename
evenhub pack app.json ./build --output my-app.ehpk

# Check package ID availability while packing
evenhub pack app.json ./dist --check

# Include hidden files in the package
evenhub pack app.json ./dist --no-ignore
```

#### Behavior Details

- Produces an `.ehpk` file (EvenHub Package format).
- The packing uses a WASM-based packer (`ehpk_pack.js` via the `#ehpk` import map → `./ehpk/pkg/ehpk_pack.js`).
- By default, files starting with `.` (dotfiles) are excluded from the package.
- The `--check` flag communicates with the EvenHub backend to verify the package ID isn't already taken.

---

## 5. Output Formats

| Command | Output |
|---|---|
| `evenhub qr` | QR code rendered in the terminal (ASCII/Unicode) OR as an external image file (PNG). |
| `evenhub qr --external` | QR code opened in system's default image viewer. |
| `evenhub init` | Creates an `app.json` file. |
| `evenhub login` | Saves credentials locally (no file output to user). |
| `evenhub pack` | Produces an `.ehpk` file (default: `out.ehpk`). |

---

## 6. The `.ehpk` File Format

The `.ehpk` (EvenHub Package) file is produced by `evenhub pack`. It bundles:
- The `app.json` metadata file.
- The entire built project folder (e.g., `dist/` or `build/`).

The packing is handled via a WebAssembly module (`ehpk_pack.js` in the `ehpk/pkg/` directory within the package). This format is used for submitting apps to the EvenHub platform.

---

## 7. Configuration Files

### `app.json`

Created by `evenhub init`. This is the project metadata file required by `evenhub pack`. It describes the app for the EvenHub platform (app ID, name, description, etc.). The CLI uses `js-yaml` as a dependency and `zod` for validation, suggesting the config may be validated against a schema.

### Cached QR Settings

The `evenhub qr` command caches your previous scheme (HTTP/HTTPS), IP, port, and path settings between runs. The exact cache storage location is not documented, but it persists locally. Use `evenhub qr --clear` to reset cached values.

### Saved Login Credentials

The `evenhub login` command saves credentials locally for future use. The exact storage mechanism is not documented.

---

## 8. Dependencies

### Runtime Dependencies (9 total)

| Package | Version | Purpose |
|---|---|---|
| `chalk` | `^5.6.2` | Terminal string styling (colored output) |
| `commander` | `^14.0.2` | CLI framework for command/option parsing |
| `inquirer` | `^13.1.0` | Interactive command-line prompts |
| `inquirer-file-selector` | `^1.0.1` | File selection prompt for inquirer |
| `js-yaml` | `^4.1.1` | YAML parsing (likely for config files) |
| `@types/js-yaml` | `^4.0.9` | TypeScript types for js-yaml (incorrectly in dependencies instead of devDependencies) |
| `open` | `^11.0.0` | Open files/URLs in system default programs (used for `--external` QR) |
| `qrcode` | `^1.5.4` | QR code generation (terminal and image) |
| `zod` | `^4.3.2` | Schema validation (likely for `app.json` validation) |

### Dev Dependencies

| Package | Version | Purpose |
|---|---|---|
| `@types/bun` | `latest` | TypeScript types for Bun runtime |
| `@types/qrcode` | `^1.5.6` | TypeScript types for the qrcode library |

### Peer Dependencies

| Package | Version |
|---|---|
| `typescript` | `^5` |

### Internal Import Map

```json
{
  "imports": {
    "#ehpk": "./ehpk/pkg/ehpk_pack.js"
  }
}
```

The `#ehpk` import alias points to a WebAssembly-based packer module used by the `pack` command.

---

## 9. Scripts (Development)

These are the scripts in the package's `package.json` (relevant to contributors/maintainers):

```json
{
  "dev": "bun run ./index.ts",
  "build-js": "bun build.ts",
  "build-native": "bun build ./index.ts --compile --outfile evenhub",
  "prepack": "make prepack"
}
```

**Note:** The CLI is developed using **Bun** as the runtime/build tool but is distributed as Node.js-compatible JavaScript (`main.js`).

---

## 10. Environment Variables

No environment variables are explicitly documented. The CLI relies on:
- Auto-detection of local network interfaces for IP discovery.
- Interactive prompts (via `inquirer`) for missing configuration.
- Local file system for credential and settings caching.

---

## 11. Environment Setup Requirements

- **Node.js**: Required for running the CLI (published entry point is `main.js`; built with Node.js v25.4.0).
- **npm** (or yarn/pnpm): For installation.
- **Network**: A local development server must be running at the URL you generate QR codes for.
- **Even App**: The Even Realities mobile app must be installed on your phone to scan QR codes and connect to your dev server.
- **Even Realities Account**: Required for `evenhub login` and `evenhub pack --check`.

---

## 12. Related Packages in the @evenrealities Scope

| Package | Version | Description |
|---|---|---|
| `@evenrealities/evenhub-cli` | 0.1.5 | This CLI tool |
| `@evenrealities/even_hub_sdk` | 0.0.7 | TypeScript SDK for Even App ↔ WebView communication (MIT) |
| `@evenrealities/evenhub-simulator` | 0.4.1 | Desktop glasses simulator for dev/testing (MIT) |
| `@evenrealities/sim-linux-x64` | 0.4.1 | Simulator binary for Linux x64 |
| `@evenrealities/sim-darwin-arm64` | 0.4.1 | Simulator binary for macOS ARM64 |
| `@evenrealities/sim-darwin-x64` | 0.4.1 | Simulator binary for macOS x64 |
| `@evenrealities/sim-win32-x64` | 0.4.1 | Simulator binary for Windows x64 |

### Community package
| `@jappyjan/even-better-sdk` | 0.0.9 | Opinionated wrapper around the official SDK |

---

## 13. How the CLI Connects to the Simulator / SDK / Glasses

### CLI → Even App (via QR Code)

```
evenhub qr  →  generates QR code encoding dev server URL
                        ↓
Even App (phone) scans QR  →  loads URL in WebView
                        ↓
WebView uses @evenrealities/even_hub_sdk  →  communicates with glasses via EvenAppBridge
```

### CLI → Simulator

The CLI itself does **not** launch the simulator directly. They are separate tools:
1. Start your dev server.
2. Run `evenhub-simulator <your-dev-url>` (separate package).
3. OR use `evenhub qr` to connect via the Even App.

### CLI → EvenHub Platform (for login + pack)

```
evenhub login  →  authenticates with Even Realities backend
evenhub pack   →  bundles project into .ehpk
evenhub pack --check  →  verifies package ID availability on the platform
```

---

## 14. Known Limitations and Gotchas

1. **No public repository:** There is no linked GitHub or source repository. The package is published from a local tgz file.

2. **License not specified:** The `license` field is `"none"` in the npm metadata. This could mean the code is proprietary/all-rights-reserved.

3. **Only one published version:** As of 2026-02-21, only version `0.1.5` has been published. There is no version history or changelog.

4. **Bun-developed, Node-distributed:** The CLI is developed using Bun (`@types/bun` in devDependencies, `bun run` in scripts), but distributed as standard JavaScript (`main.js`) for Node.js consumption. There shouldn't be issues, but the Bun-specific development toolchain is worth noting.

5. **`@types/js-yaml` in dependencies:** The `@types/js-yaml` package is listed in `dependencies` rather than `devDependencies`. This is a minor packaging issue — it ships TypeScript type definitions to end users unnecessarily.

6. **Peer dependency on TypeScript:** The package declares `"typescript": "^5"` as a peer dependency, which may or may not be needed at runtime. If you don't have TypeScript installed, you may see peer dependency warnings.

7. **Cached settings can be confusing:** The `evenhub qr` command silently caches your previous IP/port/path/scheme. If your network changes, you may get stale values. Use `--clear` to reset.

8. **No documented `--help` or `--version` flags:** The README doesn't document global `--help` or `--version` flags, but since the CLI uses `commander` (v14), both `evenhub --help` and `evenhub --version` should work out of the box.

9. **Interactive prompts require a TTY:** The CLI uses `inquirer` for interactive prompts. Running in non-TTY environments (CI/CD pipelines) may fail unless all options are provided via flags.

10. **No global `--verbose` or `--debug` flag documented:** There's no documented way to enable verbose/debug output.

11. **EHPK format is opaque:** The `.ehpk` format is produced by a WASM module (`ehpk_pack.js`). There's no documentation about its internal structure or how to inspect/extract it.

---

## 15. Full README (Verbatim)

Below is the complete README as stored in the npm registry:

---

# EvenHub CLI

Command-line interface for EvenHub development and app management.

## Quick Start

**For development mode with the Even app, the `qr` command is the only command you need in this phase.**

## Commands

### `qr` - Generate QR Code

Generate a QR code for your development server URL. This is the primary command
for using dev mode with the Even app.

**Basic usage:**
```bash
evenhub qr
```

The command will automatically detect your local IP address and prompt you for
the port and path. On subsequent runs, it will remember your previous settings.

**Options:**
- `-u, --url <url>` - Provide a full URL directly (overrides other options)
- `-i, --ip <ip>` - Specify the IP address or hostname
- `-p, --port [port]` - Specify the port (leave empty for no port)
- `--path <path>` - Specify the URL path
- `--https` - Use HTTPS instead of HTTP
- `--http` - Use HTTP instead of HTTPS
- `-e, --external` - Open QR code in an external program instead of terminal
- `-s, --scale <scale>` - Scale factor for external QR code (default: 4)
- `--clear` - Clear cached scheme, IP, port, and path settings

**Examples:**
```bash
# Generate QR code with auto-detected IP
evenhub qr

# Generate QR code for a specific URL
evenhub qr --url http://192.168.1.100:3000

# Generate QR code with specific IP and port
evenhub qr --ip 192.168.1.100 --port 3000

# Open QR code externally
evenhub qr --external
```

### `init` - Initialize Project

Initialize a new project with a basic `app.json` configuration file.

**Usage:**
```bash
evenhub init [options]
```

**Options:**
- `-d, --directory <directory>` - Directory to create the project in (default: `./`)
- `-o, --output <output>` - Output file path (takes precedence over `--directory`, default: `./app.json`)

**Example:**
```bash
evenhub init
evenhub init --directory ./my-app
evenhub init --output ./config/app.json
```

### `login` - Login to EvenHub

Log in using your Even Realities account (same one used in app). Credentials are
saved locally for future use.

**Usage:**
```bash
evenhub login [options]
```

**Options:**
- `-e, --email <email>` - Your email address

**Example:**
```bash
evenhub login
evenhub login --email user@example.com
```

### `pack` - Pack Project

Pack your project into an `.ehpk` file ready for app creation/submit.

**Usage:**
```bash
evenhub pack <json> <project> [options]
```

**Arguments:**
- `<json>` - Path to your `app.json` metadata file
- `<project>` - Path to your built project folder (e.g., `dist`, `build`)

**Options:**
- `-o, --output <output>` - Output file name (default: `out.ehpk`)
- `--no-ignore` - Include hidden files (those starting with `.`)
- `-c, --check` - Check if the package ID is available

**Example:**
```bash
evenhub pack app.json ./dist
evenhub pack app.json ./build --output my-app.ehpk
evenhub pack app.json ./dist --check
```

---

## 16. Complete `package.json` Fields (Reconstructed)

```json
{
  "name": "@evenrealities/evenhub-cli",
  "version": "0.1.5",
  "description": "Command-line interface for EvenHub development and app management.",
  "module": "index.ts",
  "type": "module",
  "bin": {
    "evenhub": "main.js",
    "eh": "main.js"
  },
  "scripts": {
    "dev": "bun run ./index.ts",
    "build-js": "bun build.ts",
    "build-native": "bun build ./index.ts --compile --outfile evenhub",
    "prepack": "make prepack"
  },
  "imports": {
    "#ehpk": "./ehpk/pkg/ehpk_pack.js"
  },
  "dependencies": {
    "@types/js-yaml": "^4.0.9",
    "chalk": "^5.6.2",
    "commander": "^14.0.2",
    "inquirer": "^13.1.0",
    "inquirer-file-selector": "^1.0.1",
    "js-yaml": "^4.1.1",
    "open": "^11.0.0",
    "qrcode": "^1.5.4",
    "zod": "^4.3.2"
  },
  "devDependencies": {
    "@types/bun": "latest",
    "@types/qrcode": "^1.5.6"
  },
  "peerDependencies": {
    "typescript": "^5"
  }
}
```

---

## 17. Quick Reference Card

```
evenhub qr                                  # Interactive QR code generation
evenhub qr --url <url>                      # QR for specific URL
evenhub qr --ip <ip> --port <port>          # QR with specific IP/port
evenhub qr --path <path>                    # QR with specific path
evenhub qr --https                          # Use HTTPS scheme
evenhub qr --http                           # Use HTTP scheme (default)
evenhub qr --external                       # Open QR in external viewer
evenhub qr --external --scale 8             # External QR with custom scale
evenhub qr --clear                          # Clear cached settings

evenhub init                                # Create app.json in current dir
evenhub init -d ./my-app                    # Create app.json in ./my-app/
evenhub init -o ./config/app.json           # Create app.json at specific path

evenhub login                               # Interactive login
evenhub login -e user@example.com           # Login with email pre-filled

evenhub pack app.json ./dist                # Pack dist/ → out.ehpk
evenhub pack app.json ./build -o my.ehpk    # Pack build/ → my.ehpk
evenhub pack app.json ./dist --check        # Pack + check ID availability
evenhub pack app.json ./dist --no-ignore    # Include dotfiles in package
```

**Short alias:** Replace `evenhub` with `eh` in any command above.
