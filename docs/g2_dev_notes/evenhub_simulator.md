# EvenHub Simulator — Comprehensive Developer Notes

> **Last updated:** 2026-02-21
> **Source:** https://www.npmjs.com/package/@evenrealities/evenhub-simulator

---

## 1. Package Identity

| Field              | Value                                          |
| ------------------ | ---------------------------------------------- |
| **Scoped name**    | `@evenrealities/evenhub-simulator`             |
| **Latest version** | 0.4.1                                          |
| **Description**    | EvenHub glasses app simulator                  |
| **License**        | MIT                                            |
| **Unpacked size**  | 6.34 kB (thin JS wrapper — native binaries are in optional deps) |
| **Total files**    | 3                                              |
| **Binary command**  | `evenhub-simulator` (installed via `bin/index.js`) |
| **Weekly downloads** | ~218 (as of 2026-02-21)                       |
| **First published** | 2026-02-13                                    |
| **Registry**       | https://registry.npmjs.org/@evenrealities/evenhub-simulator |
| **GitHub repo**    | https://github.com/even-realities/evenhub-simulator (currently private / 404) |
| **Maintainers**    | `whiskee.chen` (whiskee.chen@evenrealities.com), `carson.zhu` (carson.zhu@evenrealities.com) |

---

## 2. What It Is

The EvenHub Simulator is a **desktop development tool for rapid iteration and early-stage debugging of EvenHub applications**. It lets developers **preview UI layouts and logic** before running on physical Even Realities smart glasses hardware.

> **Important:** The simulator is a *supplement to*, not a replacement for, hardware testing. Inconsistencies between the simulator and real glasses can occur. Always validate on actual hardware before deployment. If you find discrepancies that affect coding logic, file a bug report (in Discord for now) so the team can reduce the differences.

The simulator receives a **target URL** (your local dev server) and renders the EvenHub app UI as if it were running on the glasses, including handling input events (Up, Down, Click, Double Click) and audio capture.

---

## 3. Installation

### Global install (recommended)

```bash
npm install -g @evenrealities/evenhub-simulator
```

This installs the `evenhub-simulator` CLI command globally.

### How the binary works

The npm package itself is a thin JavaScript wrapper (`bin/index.js`). The actual simulator is a **native binary** written in Rust (uses `lvgl-sys` v9 for display rendering). Platform-specific binaries are pulled in via **optional dependencies**:

| Optional dependency                      | Platform       |
| ---------------------------------------- | -------------- |
| `@evenrealities/sim-linux-x64`           | Linux x64      |
| `@evenrealities/sim-win32-x64`           | Windows x64    |
| `@evenrealities/sim-darwin-x64`          | macOS Intel    |
| `@evenrealities/sim-darwin-arm64`        | macOS Apple Silicon |

All optional dependency versions are pinned to match the simulator version (e.g., all at `0.4.1`).

The package has **0 regular dependencies** — only the above optional native binaries.

---

## 4. Usage / CLI

```
EvenHub simulator

Usage: evenhub-simulator [OPTIONS] [targetUrl]

Arguments:
  [targetUrl]  The URL to load on startup

Options:
  -c, --config <CONFIG>           Path to config file (use --print-config-path to see the default)
  -g, --glow                      Enable glow effect on glasses display
      --no-glow                   Disable glow effect (overrides config)
  -b, --bounce <BOUNCE>           Bounce animation type [possible values: default, spring]
      --list-audio-input-devices  List available audio input devices
      --aid <AID>                 Choose the specified audio input device instead of default
      --no-aid                    Use default audio device (overrides config)
  -V, --version                   Print version
      --print-config-path         Print the default config file path and exit
      --completions <SHELL>       Print shell completion script [possible values: bash, elvish, fish, powershell, zsh]
  -h, --help                      Print help
```

### 4.1 Arguments

| Argument       | Required | Description                        |
| -------------- | -------- | ---------------------------------- |
| `[targetUrl]`  | No       | The URL to load on startup. This is the address of your EvenHub web app dev server. |

### 4.2 Options — Full Reference

| Short | Long                          | Argument           | Description |
| ----- | ----------------------------- | ------------------ | ----------- |
| `-c`  | `--config <CONFIG>`           | Path (string)      | Path to a config file. Defaults to the OS config directory (see below). |
|       | `--print-config-path`         | *(none)*           | Print the default config file path and exit. |
| `-g`  | `--glow`                      | *(none — flag)*    | Enable the glow effect on the glasses display. |
|       | `--no-glow`                   | *(none — flag)*    | Disable the glow effect (overrides config file setting). |
| `-b`  | `--bounce <BOUNCE>`           | `default` or `spring` | Sets the bounce animation type for list scrolling. |
|       | `--list-audio-input-devices`  | *(none)*           | Lists available audio input devices and exits. |
|       | `--aid <AID>`                 | Device identifier  | Choose a specific audio input device instead of the system default. |
|       | `--no-aid`                    | *(none — flag)*    | Use the default audio device (overrides config file setting). |
| `-V`  | `--version`                   | *(none)*           | Print version and exit. |
|       | `--completions <SHELL>`       | `bash`, `elvish`, `fish`, `powershell`, or `zsh` | Print a shell completion script for the given shell. |
| `-h`  | `--help`                      | *(none)*           | Print help and exit. |

### 4.3 Config File

The simulator supports a **persistent config file** so you don't need to repeat common options on every invocation. CLI flags override config values.

#### Default config file paths

| Platform | Base directory                              | Example path                                  |
| -------- | ------------------------------------------ | --------------------------------------------- |
| Linux    | `$XDG_CONFIG_HOME` or `$HOME/.config`      | `/home/<user>/.config`                        |
| macOS    | `$HOME/Library/Application Support`        | `/Users/<user>/Library/Application Support`   |
| Windows  | `{FOLDERID_RoamingAppData}`                | `C:\Users\<user>\AppData\Roaming`             |

Use `--print-config-path` to see the exact path on your system:

```bash
evenhub-simulator --print-config-path
```

Config file options that can be set (inferred from CLI flags that have "overrides config" semantics):
- Glow effect (on/off)
- Audio input device selection
- Possibly others (bounce type, etc.)

### 4.4 Shell Completions

Generate and install shell completions. Example for **zsh**:

```bash
evenhub-simulator --completions zsh > ~/.zsh/completions/_evenhub-simulator
```

Supported shells: `bash`, `elvish`, `fish`, `powershell`, `zsh`.

---

## 5. Supported Inputs / Events

### 5.1 User Input Events

The simulator supports the following input events (simulating the glasses touchpad/button):

| Input         | Description                    |
| ------------- | ------------------------------ |
| **Up**        | Scroll / navigate up           |
| **Down**      | Scroll / navigate down         |
| **Click**     | Single tap / select            |
| **Double Click** | Double tap                  |

### 5.2 Status Events

**Not emitted** by the simulator. User profiles and device statuses are **hardcoded**. This means:
- `onDeviceStatusChanged` callbacks from the SDK will not fire in the simulator.
- Device info (model, serial number, battery, wearing status, etc.) will return hardcoded values.

### 5.3 Audio Events

The simulator **does** emit `audioEvents` (added in v0.2.0, improved in v0.2.2+). Audio specification per event:

| Parameter     | Value                              |
| ------------- | ---------------------------------- |
| Sample rate   | 16,000 Hz (16 kHz)                |
| Encoding      | Signed 16-bit little-endian PCM    |
| Frame duration | 100 ms per event                  |
| Bytes per event | 3,200 bytes                      |
| Samples per event | 1,600 samples                  |

The simulator captures audio from a real microphone on the development machine. You can select which audio input device to use with `--aid` / `--no-aid` / `--list-audio-input-devices`.

---

## 6. Caveats / Known Limitations

### 6.1 Display & UI Rendering

Due to implementation differences, overall display characteristics (such as **font rendering**) may not perfectly match the hardware. The current fidelity should still be sufficient for **layout validation** and **logic testing**.

### 6.2 List Behavior

List scrolling behavior, especially **focused-item positioning** on screen, can vary. This happens because the simulator **re-implements drawing logic** instead of sharing embedded source code directly from the glasses firmware.

### 6.3 Error Handling

Under normal conditions, the simulator behaves similarly to the hardware. **Error-response handling** (for example, invalid inputs) may still differ, but should converge as the codebase matures.

### 6.4 Image Processing

The simulator processes images **faster** and currently **does not enforce constraints** such as hardware image-size limits. Future versions may introduce stricter checks to better simulate hardware behavior.

### 6.5 Events

- **Status Events:** Not emitted; user profiles and device statuses are hardcoded.
- **Supported Inputs:** Up, Down, Click, Double Click only.

### 6.6 Audio

Audio is supported but uses the host machine's microphone, which may produce different characteristics than the glasses' built-in microphone array.

---

## 7. Architecture & Ecosystem

### 7.1 How It Connects to the SDK

The simulator is designed to work with **EvenHub web applications** built using the `@evenrealities/even_hub_sdk` (v0.0.7 as of writing). The typical development workflow is:

1. **Develop** your EvenHub app using `@evenrealities/even_hub_sdk` to build UI (list, text, and image containers) and handle events.
2. **Run** your app on a local dev server (e.g., `http://localhost:3000`).
3. **Launch** the simulator pointing at your dev server:
   ```bash
   evenhub-simulator http://localhost:3000
   ```
4. The simulator opens a window that **renders the glasses display**, processes the SDK protocol calls, and simulates user input (tap gestures mapped to Up/Down/Click/Double Click).
5. **Iterate** on your app code with hot reload, using the simulator for rapid feedback.
6. **Validate** on real hardware before deployment.

### 7.2 Related Even Realities Packages

| Package | Version | Description |
| ------- | ------- | ----------- |
| `@evenrealities/even_hub_sdk` | 0.0.7 | TypeScript SDK for EvenHub developers. Provides `EvenAppBridge` for communicating with the Even App / simulator. |
| `@evenrealities/evenhub-cli` | 0.1.5 | CLI for EvenHub development: QR code generation for dev mode, project init, login, and packaging (`evenhub qr`, `evenhub init`, `evenhub login`, `evenhub pack`). |
| `@evenrealities/sim-linux-x64` | 0.4.1 | Native simulator binary for Linux x64. |
| `@evenrealities/sim-win32-x64` | 0.4.1 | Native simulator binary for Windows x64. |
| `@evenrealities/sim-darwin-x64` | 0.4.1 | Native simulator binary for macOS Intel. |
| `@evenrealities/sim-darwin-arm64` | 0.4.1 | Native simulator binary for macOS Apple Silicon. |
| `@jappyjan/even-better-sdk` | 0.0.9 | Community wrapper around the official SDK with opinionated page composition API. |

### 7.3 Technical Implementation

- The simulator binary is written in **Rust**.
- Uses **LVGL** (Light and Versatile Graphics Library) v9 via a custom `lvgl-sys` crate for rendering the glasses display.
- The npm package acts as a **thin JS launcher** (`bin/index.js`) that detects the current platform and spawns the appropriate native binary from the optional dependencies.
- The simulator **re-implements the glasses drawing logic** (rather than sharing embedded firmware code), which explains the minor rendering differences noted in the caveats.

---

## 8. SDK API Surface (What the Simulator Must Support)

Since the simulator emulates the glasses hardware for apps built with `@evenrealities/even_hub_sdk`, the following SDK API calls are relevant to what the simulator processes:

### 8.1 Page Container Lifecycle

| SDK Method | Simulator Support | Notes |
| ---------- | ----------------- | ----- |
| `createStartUpPageContainer(container)` | ✅ Yes | Must be called once at startup. Creates up to 4 containers (list, text, image). |
| `rebuildPageContainer(container)` | ✅ Yes | Updates/rebuilds pages after initial creation. Same structure as startup. |
| `updateImageRawData(data)` | ✅ Yes | Updates image container content. Simulator processes images faster than hardware and doesn't enforce size limits. |
| `textContainerUpgrade(container)` | ✅ Yes | Updates text container content. |
| `shutDownPageContainer(exitMode?)` | ✅ Yes | Shuts down the page container. |
| `audioControl(isOpen)` | ✅ Yes | Opens/closes microphone. Simulator captures from host machine's mic. |

### 8.2 Information Methods

| SDK Method | Simulator Support | Notes |
| ---------- | ----------------- | ----- |
| `getUserInfo()` | ✅ Yes (hardcoded) | Returns hardcoded user profile. |
| `getDeviceInfo()` | ✅ Yes (hardcoded) | Returns hardcoded device info. |
| `setLocalStorage(key, value)` | ✅ Yes | Key-value storage. |
| `getLocalStorage(key)` | ✅ Yes | Key-value retrieval. |

### 8.3 Event Delivery

| Event Type | Simulator Support | Notes |
| ---------- | ----------------- | ----- |
| `listEvent` | ✅ Yes | Delivered via `onEvenHubEvent`. Contains `containerID`, `containerName`, `currentSelectItemName`, `currentSelectItemIndex`, `eventType`. |
| `textEvent` | ✅ Yes | Delivered via `onEvenHubEvent`. Contains `containerID`, `containerName`, `eventType`. |
| `sysEvent` | ✅ Yes | Delivered via `onEvenHubEvent`. Contains `eventType`. |
| `audioEvent` | ✅ Yes | Delivered via `onEvenHubEvent`. Contains `audioPcm` (Uint8Array). PCM: 16kHz, signed 16-bit LE, 100ms/event. |
| `deviceStatusChanged` | ❌ No | Not emitted; status is hardcoded. |

### 8.4 Container Property Constraints (Glasses Canvas)

The glasses canvas coordinate system has origin (0,0) at top-left, X extends right, Y extends down.

| Property | List Container | Text Container | Image Container |
| -------- | -------------- | -------------- | --------------- |
| `xPosition` | 0–576 | 0–576 | 0–576 |
| `yPosition` | 0–288 | 0–288 | 0–288 |
| `width` | 0–576 | 0–576 | 20–200 |
| `height` | 0–288 | 0–288 | 20–100 |
| `borderWidth` | 0–5 | 0–5 | N/A |
| `borderColor` | 0–15 | 0–16 | N/A |
| `borderRadius` | 0–10 | 0–10 | N/A |
| `paddingLength` | 0–32 | 0–32 | N/A |
| `containerName` | max 16 chars | max 16 chars | max 16 chars |
| `content` | N/A | max 1000 chars (startup), max 2000 chars (upgrade) | N/A |
| `itemCount` | 1–20 | N/A | N/A |
| `itemName` each | max 64 chars | N/A | N/A |
| `containerTotalNum` | max 4 total containers across all types | | |

**Note:** The simulator currently does **not** enforce hardware image-size limits, which the real glasses do.

---

## 9. Code Examples

### 9.1 Basic Launch

```bash
# Launch simulator loading a local dev server
evenhub-simulator http://localhost:3000
```

### 9.2 Launch with Options

```bash
# Enable glow effect and spring bounce animation
evenhub-simulator --glow --bounce spring http://localhost:3000

# Use a specific config file
evenhub-simulator --config ./my-config.toml http://localhost:3000

# List audio input devices
evenhub-simulator --list-audio-input-devices

# Use a specific audio input device
evenhub-simulator --aid "Built-in Microphone" http://localhost:3000
```

### 9.3 Shell Completions Setup

```bash
# Zsh
evenhub-simulator --completions zsh > ~/.zsh/completions/_evenhub-simulator

# Bash
evenhub-simulator --completions bash > /etc/bash_completion.d/evenhub-simulator

# Fish
evenhub-simulator --completions fish > ~/.config/fish/completions/evenhub-simulator.fish
```

### 9.4 Typical EvenHub App (SDK side) That Runs in the Simulator

```typescript
import {
  waitForEvenAppBridge,
  ListContainerProperty,
  TextContainerProperty,
} from '@evenrealities/even_hub_sdk';

const bridge = await waitForEvenAppBridge();

// Create startup page with a list and text container
const listContainer: ListContainerProperty = {
  xPosition: 100,
  yPosition: 50,
  width: 200,
  height: 150,
  containerID: 1,
  containerName: 'list-1',
  itemContainer: {
    itemCount: 3,
    itemName: ['Item 1', 'Item 2', 'Item 3'],
  },
  isEventCapture: 1, // Only one container can capture events
};

const textContainer: TextContainerProperty = {
  xPosition: 100,
  yPosition: 220,
  width: 200,
  height: 50,
  containerID: 2,
  containerName: 'text-1',
  content: 'Hello World',
  isEventCapture: 0,
};

const result = await bridge.createStartUpPageContainer({
  containerTotalNum: 2,
  listObject: [listContainer],
  textObject: [textContainer],
});

if (result === 0) {
  console.log('Container created successfully');
}

// Listen for events (works in simulator)
const unsubscribe = bridge.onEvenHubEvent((event) => {
  if (event.listEvent) {
    console.log('List selected:', event.listEvent.currentSelectItemName);
  } else if (event.audioEvent) {
    console.log('Audio PCM length:', event.audioEvent.audioPcm.length);
  }
});
```

---

## 10. Dependencies

### 10.1 Runtime Dependencies

**None.** The package has 0 regular dependencies.

### 10.2 Optional Dependencies (Platform Binaries)

| Package | Version | Platform |
| ------- | ------- | -------- |
| `@evenrealities/sim-linux-x64` | 0.4.1 | Linux x86_64 |
| `@evenrealities/sim-win32-x64` | 0.4.1 | Windows x86_64 |
| `@evenrealities/sim-darwin-x64` | 0.4.1 | macOS Intel x86_64 |
| `@evenrealities/sim-darwin-arm64` | 0.4.1 | macOS Apple Silicon ARM64 |

### 10.3 Peer Dependencies

**None listed.**

---

## 11. Environment Setup Requirements

1. **Node.js:** The wrapper uses Node.js to launch the native binary. The package was published with Node v25.6.0 and npm v11.8.0, but should work with recent Node LTS versions.
2. **Platform support:** Only x64 Linux, x64 Windows, x64 macOS, and ARM64 macOS are supported (via the optional binary packages). No ARM Linux or 32-bit support.
3. **Audio input:** If you want to test audio features, ensure a working microphone is available on the development machine. Use `--list-audio-input-devices` to verify.
4. **Display:** The simulator opens a graphical window — a display server (X11/Wayland on Linux, native on macOS/Windows) is required.
5. **Network:** Your EvenHub app dev server must be accessible from the machine running the simulator (typically `localhost`).

---

## 12. Version History / Changelog

| Version | Date | Changes |
| ------- | ---- | ------- |
| **0.4.1** | 2026-02-20 | Performance optimization. New `--print-config-path` flag. Fix completion command. |
| **0.4.0** | 2026-02-20 | *(Release notes not detailed on npm — likely pre-release of 0.4.1 fixes)* |
| **0.3.2** | 2026-02-18 | Fix description of config file location. |
| **0.3.1** | 2026-02-17 | Adjust audio input device listing format. |
| **0.3.0** | 2026-02-17 | Shell completion support (`--completions`). |
| **0.2.2** | *(undated)* | Adjust audio resampling logic. Ability to choose audio input device (`--aid`). Add config file support. Add more CLI flags to override config options. |
| **0.2.0** | 2026-02-16 | Upgrade `lvgl-sys` to v9 (via custom crate). Lighter CJK font. Preliminary audio event support. |
| **0.1.2** | 2026-02-13 | *(Patch release)* |
| **0.1.1** | 2026-02-13 | *(Patch release)* |
| **0.1.0** | 2026-02-13 | Initial release. |

---

## 13. Gotchas & Tips

1. **Simulator ≠ Hardware:** Always test on real glasses before shipping. Font rendering, list scrolling, error handling, and image size limits all differ.

2. **One `isEventCapture=1` per page:** When creating multiple containers, exactly one container must have `isEventCapture=1`. All others must be `0`.

3. **`createStartUpPageContainer` is one-shot:** It must be called exactly once. All subsequent page updates must use `rebuildPageContainer`.

4. **Image containers need a follow-up call:** After creating an image container, you must call `updateImageRawData` separately to display content (the create call only reserves the placeholder).

5. **No concurrent image transmission:** Image data must be sent sequentially (queue mode) — wait for the previous `updateImageRawData` to resolve before sending the next.

6. **Audio is real:** The simulator captures real audio from your machine's microphone, unlike status events which are hardcoded.

7. **Config persistence:** Use a config file to avoid repeating `--glow`, `--aid`, `--bounce` on every launch. Run `--print-config-path` to find where to put it.

8. **No status events:** `onDeviceStatusChanged` callbacks will never fire in the simulator. If your app logic depends on connection/battery/wearing status, you'll need to test that on hardware or mock it yourself.

9. **Canvas resolution:** The glasses canvas is **576×288 pixels**. Design your layouts within these constraints.

10. **Max 4 containers per page:** `containerTotalNum` maximum is 4, across all container types combined.

---

## 14. Bug Reports

File bug reports in the **Even Realities Discord** (no public GitHub Issues available as the repo is private).

---

*Document generated from NPM registry data and package README for `@evenrealities/evenhub-simulator@0.4.1`.*
