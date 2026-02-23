# G2 OpenClaw — Display Layouts

Pixel-level layout specification for every screen rendered on the G2 576×288
micro-LED display. Covers all 9 application states, container construction
code, and state transition logic. For hardware details see
[architecture.md](architecture.md).

## 1. Design Principles

1. **Readability first** — minimum 18px font, high contrast on always-black background
2. **Minimal chrome** — maximise content area, no decorative borders
3. **Status always visible** — header badge shows current state at all times
4. **Intentional greyscale** — bright for primary, dim for secondary, near-off for structure
5. **Consistent positions** — same zone coordinates across all states for spatial memory
6. **Smooth transitions** — prefer `textContainerUpgrade` over `rebuildPageContainer` when only text changes

## 2. Colour Palette (Greyscale)

4-bit values (0–15). On G2 hardware these render as shades of green.

| Name | Hex | Decimal | Use |
|---|---|---|---|
| Background | `0x0` | 0 | Canvas background (always black / off) |
| Primary Text | `0xF` | 15 | Main content, headings, critical info |
| Secondary Text | `0xA` | 10 | Subtitles, labels, timestamps |
| Muted Text | `0x6` | 6 | Hints, disabled text, placeholders |
| Accent | `0xC` | 12 | Status indicators, active-state dots |
| Border | `0x3` | 3 | Dividers, container outlines (subtle) |
| Highlight BG | `0x2` | 2 | Selected-item background in lists |

## 3. Layout Grid

A three-zone grid provides consistent structure:

```
┌─────────────────────────────── 576 px ──────────────────────────────┐
│ M=8                                                                  │
│  ┌─── HEADER ZONE ────────────────────────────────── y=8 ────────┐  │
│  │  x=8  w=560  h=32                                             │  │
│  │  Status bar / title / state indicator                         │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                          8px gap                     │
│  ┌─── CONTENT ZONE ──────────────────────────────── y=48 ────────┐  │  288 px
│  │  x=8  w=560  h=200                                            │  │
│  │  Primary content area: response text, prompts, messages       │  │
│  │  Scrollable when isEventCapture=1                             │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                          8px gap                     │
│  ┌─── FOOTER ZONE ───────────────────────────────── y=256 ───────┐  │
│  │  x=8  w=560  h=24                                             │  │
│  │  Hint text, status indicator, interaction prompts             │  │
│  └───────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

The 8px margin keeps text clear of the optical edge. The header is split into
a title (x=8, w=460) and badge (x=480, w=88), using 2 of 4 containers.

## 4. State Layouts

### 4.1 Idle State

```
┌──────────────────────────────────────────────────────────────┐
│  ┌─────────────────────────────────┐ ┌────────────────────┐  │
│  │  OpenClaw                       │ │           ● idle   │  │  HEADER
│  └─────────────────────────────────┘ └────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │                                                        │  │
│  │            Tap ring to speak                           │  │  CONTENT
│  │                                                        │  │
│  └────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Ready                                                 │  │  FOOTER
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

**Container specification:**

| # | ID | Name | Type | x | y | w | h | fontSize | fontColor | content | scrollable | eventCapture |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | `title` | text | 8 | 8 | 460 | 32 | 24 | 0xF | `"OpenClaw"` | no | 0 |
| 2 | 2 | `badge` | text | 480 | 8 | 88 | 32 | 18 | 0x6 | `"● idle"` | no | 0 |
| 3 | 3 | `content` | text | 8 | 100 | 560 | 80 | 24 | 0xA | `"Tap ring to speak"` | no | 1 |
| 4 | 4 | `footer` | text | 8 | 256 | 560 | 24 | 18 | 0x6 | `"Ready"` | no | 0 |

**Notes:** Content centred at y=100 for visual balance; Secondary (`0xA`) for
instructional text. Badge Muted (`0x6`). Container 3 has `isEventCapture: 1`.

### 4.2 Recording State

Microphone is active. High contrast to clearly signal "you are being heard."

```
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│  ┌─────────────────────────────────┐ ┌────────────────────┐  │
│  │  OpenClaw                       │ │     ● Recording    │  │  HEADER
│  └─────────────────────────────────┘ └────────────────────┘  │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │                                                        │  │
│  │                                                        │  │
│  │         🎙  Listening...                               │  │  CONTENT
│  │                                                        │  │
│  │         ████████░░░░░░░░                               │  │
│  │                                                        │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Tap to stop                                           │  │  FOOTER
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

**Container specification:**

| # | ID | Name | Type | x | y | w | h | fontSize | fontColor | content | scrollable | eventCapture |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | `title` | text | 8 | 8 | 460 | 32 | 24 | 0xF | `"OpenClaw"` | no | 0 |
| 2 | 2 | `badge` | text | 480 | 8 | 88 | 32 | 18 | 0xC | `"● Recording"` | no | 0 |
| 3 | 3 | `content` | text | 8 | 80 | 560 | 160 | 24 | 0xF | `"Listening...\n\n████████░░░░░░░░"` | no | 1 |
| 4 | 4 | `footer` | text | 8 | 256 | 560 | 24 | 18 | 0xA | `"Tap to stop"` | no | 0 |

**Notes:** Badge Accent (`0xC`) for active recording. Audio bar uses Unicode
blocks (U+2588/U+2591), updated via `textContainerUpgrade` from PCM RMS.
Content full-brightness (`0xF`). Full `rebuildPageContainer` in and out.

### 4.3 Transcribing State

```
┌──────────────────────────────────────────────────────────────┐
│  ┌─────────────────────────────────┐ ┌────────────────────┐  │
│  │  OpenClaw                       │ │  ● Transcribing    │  │  HEADER
│  └─────────────────────────────────┘ └────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │                                                        │  │
│  │         Transcribing audio...                          │  │  CONTENT
│  │                                                        │  │
│  └────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Processing speech                                     │  │  FOOTER
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

**Container specification:**

| # | ID | Name | Type | x | y | w | h | fontSize | fontColor | content | scrollable | eventCapture |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | `title` | text | 8 | 8 | 460 | 32 | 24 | 0xF | `"OpenClaw"` | no | 0 |
| 2 | 2 | `badge` | text | 480 | 8 | 88 | 32 | 18 | 0xC | `"● Transcribing"` | no | 0 |
| 3 | 3 | `content` | text | 8 | 100 | 560 | 80 | 24 | 0xA | `"Transcribing audio..."` | no | 1 |
| 4 | 4 | `footer` | text | 8 | 256 | 560 | 24 | 18 | 0x6 | `"Processing speech"` | no | 0 |

**Notes:** Animated dots via 500ms `textContainerUpgrade` cycle. Badge stays
Accent (`0xC`). Typically brief (1–3s with faster-whisper on GPU).

### 4.4 Thinking State

```
┌──────────────────────────────────────────────────────────────┐
│  ┌─────────────────────────────────┐ ┌────────────────────┐  │
│  │  OpenClaw                       │ │    ● Thinking      │  │  HEADER
│  └─────────────────────────────────┘ └────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  You: What is the capital of                           │  │
│  │  France?                                               │  │  CONTENT
│  │  ─────────────────────────────                         │  │
│  │  Thinking...                                           │  │
│  └────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Waiting for response                                  │  │  FOOTER
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

**Container specification:**

| # | ID | Name | Type | x | y | w | h | fontSize | fontColor | content | scrollable | eventCapture |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | `title` | text | 8 | 8 | 460 | 32 | 24 | 0xF | `"OpenClaw"` | no | 0 |
| 2 | 2 | `badge` | text | 480 | 8 | 88 | 32 | 18 | 0xC | `"● Thinking"` | no | 0 |
| 3 | 3 | `content` | text | 8 | 48 | 560 | 200 | 18 | 0xF | `"You: {query}\n\n━━━━━━━━━━━━━━━\nThinking..."` | no | 1 |
| 4 | 4 | `footer` | text | 8 | 256 | 560 | 24 | 18 | 0x6 | `"Waiting for response"` | no | 0 |

**Notes:** User query shown as `"You: {text}"` for confirmation (font 18 to
fit longer queries). Unicode `━` divider. Animated dots same as transcribing.
Truncate query at ~3 lines — full text on page 1.

### 4.5 Streaming State

```
┌──────────────────────────────────────────────────────────────┐
│  ┌─────────────────────────────────┐ ┌────────────────────┐  │
│  │  You: What is the capital...    │ │   ● Streaming      │  │  HEADER
│  └─────────────────────────────────┘ └────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  The capital of France is Paris. It is the            │  │
│  │  largest city in France and serves as the              │  │  CONTENT
│  │  country's political, economic, and cultural            │  │  (scrollable)
│  │  center. Paris is located on the Seine█               │  │
│  └────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Streaming...                                          │  │  FOOTER
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

**Container specification:**

| # | ID | Name | Type | x | y | w | h | fontSize | fontColor | content | scrollable | eventCapture |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | `title` | text | 8 | 8 | 460 | 32 | 18 | 0xA | `"You: {query_truncated}"` | no | 0 |
| 2 | 2 | `badge` | text | 480 | 8 | 88 | 32 | 18 | 0xC | `"● Streaming"` | no | 0 |
| 3 | 3 | `content` | text | 8 | 48 | 560 | 200 | 18 | 0xF | `"{accumulated_response}"` | yes | 1 |
| 4 | 4 | `footer` | text | 8 | 256 | 560 | 24 | 18 | 0x6 | `"Streaming..."` | no | 0 |

**Notes:** Header shows truncated query (~25 chars) at Secondary (`0xA`).
Content scrollable via `isEventCapture: 1`. Deltas appended with
`textContainerUpgrade` (offset=len, length=0). Cursor `█` shown during stream,
removed on completion. Batch 2–3 deltas to reduce BLE traffic.

**Delta buffering:** When transitioning from thinking to streaming, the app performs a `rebuildPageContainer` call to swap to the streaming layout. During this BLE round-trip (~50–100ms), incoming deltas must be buffered in memory. Once the rebuild completes, flush all buffered deltas via a single `textContainerUpgrade` call.

### 4.6 Response Complete (Displaying) State

```
┌──────────────────────────────────────────────────────────────┐
│  ┌─────────────────────────────────┐ ┌────────────────────┐  │
│  │  You: What is the capital...    │ │           ● Done   │  │  HEADER
│  └─────────────────────────────────┘ └────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  The capital of France is Paris. It is the            │  │
│  │  largest city in France and serves as the              │  │  CONTENT
│  │  country's political, economic, and cultural            │  │  (scrollable)
│  │  center. Paris is located on the Seine River           │  │
│  └────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Tap to dismiss · Double-tap for more                  │  │  FOOTER
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

**Container specification:**

| # | ID | Name | Type | x | y | w | h | fontSize | fontColor | content | scrollable | eventCapture |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | `title` | text | 8 | 8 | 460 | 32 | 18 | 0xA | `"You: {query_truncated}"` | no | 0 |
| 2 | 2 | `badge` | text | 480 | 8 | 88 | 32 | 18 | 0x6 | `"● Done"` | no | 0 |
| 3 | 3 | `content` | text | 8 | 48 | 560 | 200 | 18 | 0xF | `"{full_response}"` | yes | 1 |
| 4 | 4 | `footer` | text | 8 | 256 | 560 | 24 | 18 | 0x6 | `"Tap to dismiss · Double-tap for more"` | no | 0 |

**Notes:** Badge returns to Muted (`0x6`). Cursor `█` stripped. Content
scrollable. **Tap** → idle. **Double-tap** → page 1. Transition via
`textContainerUpgrade` on badge + footer (no full rebuild from streaming).

**Character limit:** The `textContainerUpgrade` method supports up to 2000 characters. If the response exceeds ~1800 characters, truncate the visible text on page 0 with `'… [double-tap for more]'` and make the full response available on page 1.

**Ignoring idle status:** The displaying state ignores `status:"idle"` from the Gateway. This prevents the display from resetting while the user is still reading the response. The transition to idle only occurs via an explicit user tap.

### 4.7 Error State

```
┌──────────────────────────────────────────────────────────────┐
│  ┌─────────────────────────────────┐ ┌────────────────────┐  │
│  │  OpenClaw                       │ │         ✕ Error    │  │  HEADER
│  └─────────────────────────────────┘ └────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Error                                                 │  │
│  │  Whisper transcription failed:                         │  │  CONTENT
│  │  Audio too short (< 0.5s)                              │  │
│  │  Try speaking longer.                                  │  │
│  └────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Tap to dismiss                                        │  │  FOOTER
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

**Container specification:**

| # | ID | Name | Type | x | y | w | h | fontSize | fontColor | content | scrollable | eventCapture |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | `title` | text | 8 | 8 | 460 | 32 | 24 | 0xF | `"OpenClaw"` | no | 0 |
| 2 | 2 | `badge` | text | 480 | 8 | 88 | 32 | 18 | 0xF | `"✕ Error"` | no | 0 |
| 3 | 3 | `content` | text | 8 | 48 | 560 | 200 | 18 | 0xF | `"Error\n\n{error_message}\n\n{recovery_hint}"` | yes | 1 |
| 4 | 4 | `footer` | text | 8 | 256 | 560 | 24 | 18 | 0xA | `"Tap to dismiss"` | no | 0 |

**Notes:** Badge Primary (`0xF`) for maximum error visibility. Content
scrollable. Hint varies by error type (Whisper → "Try speaking longer",
OpenClaw timeout → "Check that OpenClaw is running", unknown → "Try again").
**Tap** → idle.

### 4.8 Disconnected State

```
┌──────────────────────────────────────────────────────────────┐
│  ┌─────────────────────────────────┐ ┌────────────────────┐  │
│  │  OpenClaw                       │ │     ○ Offline      │  │  HEADER
│  └─────────────────────────────────┘ └────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Connecting to Gateway...                              │  │
│  │  ─────────────────────────────                         │  │  CONTENT
│  │  Make sure your PC is on and                           │  │
│  │  connected to the same WiFi network.                   │  │
│  └────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Retry in 5s...                                        │  │  FOOTER
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

**Container specification:**

| # | ID | Name | Type | x | y | w | h | fontSize | fontColor | content | scrollable | eventCapture |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | `title` | text | 8 | 8 | 460 | 32 | 24 | 0xF | `"OpenClaw"` | no | 0 |
| 2 | 2 | `badge` | text | 480 | 8 | 88 | 32 | 18 | 0x6 | `"○ Offline"` | no | 0 |
| 3 | 3 | `content` | text | 8 | 60 | 560 | 180 | 18 | 0xA | `"Connecting to Gateway...\n\n━━━━━━━━━━━━━━━\n\nMake sure your PC is on\nand connected to the same\nWiFi network."` | no | 1 |
| 4 | 4 | `footer` | text | 8 | 256 | 560 | 24 | 18 | 0x6 | `"Retry in {n}s..."` | no | 0 |

**Notes:** Hollow `○` contrasts filled `●` of active states. Footer countdown
via `textContainerUpgrade` each second. Exponential backoff: 2s → 4s → 8s →
max 30s. **Tap** → force immediate retry. Reconnect → idle.

### 4.9 Loading State

The Gateway is initializing the Whisper model. Recording is disabled.

```
┌──────────────────────────────────────────────────────────────┐
│  ┌─────────────────────────────────┐ ┌────────────────────┐  │
│  │  OpenClaw                       │ │       ● loading    │  │  HEADER
│  └─────────────────────────────────┘ └────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │                                                        │  │
│  │          Starting up...                                │  │  CONTENT
│  │                                                        │  │
│  └────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Initializing speech model                             │  │  FOOTER
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

**Container specification:**

| # | ID | Name | Type | x | y | w | h | fontSize | fontColor | content | scrollable | eventCapture |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | `title` | text | 8 | 8 | 460 | 32 | 24 | 0xF | `"OpenClaw"` | no | 0 |
| 2 | 2 | `badge` | text | 480 | 8 | 88 | 32 | 18 | 0xC | `"● loading"` | no | 0 |
| 3 | 3 | `content` | text | 8 | 100 | 560 | 80 | 24 | 0xA | `"Starting up..."` | no | 1 |
| 4 | 4 | `footer` | text | 8 | 256 | 560 | 24 | 18 | 0x6 | `"Initializing speech model"` | no | 0 |

**Notes:** Badge Accent (`0xC`). Content Secondary (`0xA`). Footer Muted (`0x6`).
Ring taps are ignored while in the loading state — recording cannot start until
the Whisper model is ready. Transition to idle occurs automatically when the
Gateway sends `status:"idle"` after model initialization completes.

## 5. Page 1 Layout (Detail View)

Accessed via **double-tap** from response complete. Shows extended content + metadata.

```
┌──────────────────────────────────────────────────────────────┐
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Response Details                           [Page 2]   │  │  HEADER
│  └────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  {full_response_text continued}                        │  │
│  │  ─────────────────────────────                         │  │  CONTENT
│  │  Model: openclaw-default                               │  │  (scrollable)
│  │  Time: 2.3s  ·  Session: agent:claw:g2                  │  │
│  └────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Double-tap: back · Tap: dismiss                       │  │  FOOTER
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

**Container specification:**

| # | ID | Name | Type | x | y | w | h | fontSize | fontColor | content | scrollable | eventCapture |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | `p1_header` | text | 8 | 8 | 560 | 32 | 24 | 0xF | `"Response Details         [Page 2]"` | no | 0 |
| 2 | 2 | `p1_content` | text | 8 | 48 | 560 | 200 | 18 | 0xF | `"{response}\n\n━━━━━━━━━━━━━━━\nModel: {model}\nTime: {elapsed}s\nSession: {session}"` | yes | 1 |
| 3 | 3 | `p1_footer` | text | 8 | 256 | 560 | 24 | 18 | 0x6 | `"Double-tap: back · Tap: dismiss"` | no | 0 |

**Notes:** Only 3 containers (single full-width header). Full response +
metadata below divider. **Double-tap** → `rebuildPageContainer` (swap back to page 0 summary layout). **Tap** → idle.

> **⚠️ `setPageFlip` does NOT exist in the SDK.** Page switching is implemented at the app layer by maintaining a `currentPage` variable (0 or 1) and calling `rebuildPageContainer` with the alternate page's containers on each double-tap.

**Overflow text:** When a response is truncated on page 0 (due to the ~1800 character limit), page 1 shows the continuation of the truncated response above the metadata divider. The `{response}` field in the `p1_content` container contains the full untruncated response text, allowing the user to scroll through the complete answer.

## 6. Text Rendering Rules

| Font Size | ~Chars/Line (560px) | Use |
|---|---|---|
| 24 | ~28 | Titles, prompts, idle/recording messages |
| 18 | ~37 | Body text, response content, metadata |

- **Word wrap:** firmware handles automatically; use explicit `\n` only for dividers
- **Scrollable** (`isEventCapture: 1`): firmware scrolls, fires `SCROLL_TOP/BOTTOM_EVENT` at boundaries
- **Non-scrollable** (`isEventCapture: 0`): text **clipped silently** beyond container height
- **Truncation:** for non-scrollable containers, truncate and append `"..."`
- **Markdown stripping:** LLM responses often contain markdown formatting (`**bold**`, `*italic*`, `` `code` ``, `[links](url)`, `# headings`). Apply a `stripMarkdown()` function to each delta before appending to display text. The glasses cannot render rich text — raw markdown syntax degrades readability on the constrained display. At minimum, strip: `**bold**` → `bold`, `*italic*` → `italic`, `` `code` `` → `code`, `[text](url)` → `text`, `# heading` → `heading`.

## 7. Container Construction (Code Examples)

All examples use the real SDK types from `@evenrealities/even_hub_sdk`.
A `text()` helper reduces boilerplate.

### 7.1 Helper + Rebuild Wrapper

```typescript
import {
  EvenAppBridge, RebuildPageContainer,
  TextContainerProperty, TextContainerUpgrade,
} from '@evenrealities/even_hub_sdk';

function text(
  id: number, name: string,
  x: number, y: number, w: number, h: number,
  fontSize: number, fontColor: number, content: string,
  eventCapture = 0,
): TextContainerProperty {
  return {
    containerID: id, containerName: name,
    xPosition: x, yPosition: y, width: w, height: h,
    fontSize, fontColor, content,
    borderWidth: 0, borderColor: 0, borderRdaius: 0, // SDK typo preserved
    paddingLength: 0, isEventCapture: eventCapture,
  };
}

async function rebuild(bridge: EvenAppBridge, containers: TextContainerProperty[]) {
  await bridge.rebuildPageContainer(
    new RebuildPageContainer({ containerTotalNum: containers.length, textObject: containers }),
  );
}
```

> **⚠️ Note: `borderRdaius` is a typo in the SDK (missing 'i'). This is intentional — using the correct spelling `borderRadius` will silently fail.**

### 7.2 State Render Examples

```typescript
// Idle
async function renderIdle(b: EvenAppBridge) {
  await rebuild(b, [
    text(1, 'title',   8,   8, 460, 32, 24, 0xF, 'OpenClaw'),
    text(2, 'badge', 480,   8,  88, 32, 18, 0x6, '● idle'),
    text(3, 'content', 8, 100, 560, 80, 24, 0xA, 'Tap ring to speak', 1),
    text(4, 'footer',  8, 256, 560, 24, 18, 0x6, 'Ready'),
  ]);
}

// Recording
async function renderRecording(b: EvenAppBridge) {
  await rebuild(b, [
    text(1, 'title',   8,   8, 460,  32, 24, 0xF, 'OpenClaw'),
    text(2, 'badge', 480,   8,  88,  32, 18, 0xC, '● Recording'),
    text(3, 'content', 8,  80, 560, 160, 24, 0xF, 'Listening...\n\n████████░░░░░░░░', 1),
    text(4, 'footer',  8, 256, 560,  24, 18, 0xA, 'Tap to stop'),
  ]);
}

// Error
async function renderError(b: EvenAppBridge, msg: string, hint: string) {
  await rebuild(b, [
    text(1, 'title',   8,   8, 460,  32, 24, 0xF, 'OpenClaw'),
    text(2, 'badge', 480,   8,  88,  32, 18, 0xF, '✕ Error'),
    text(3, 'content', 8,  48, 560, 200, 18, 0xF, `Error\n\n${msg}\n\n${hint}`, 1),
    text(4, 'footer',  8, 256, 560,  24, 18, 0xA, 'Tap to dismiss'),
  ]);
}
```

### 7.3 Streaming — Append Delta

```typescript
let streamedText = '';
async function appendDelta(b: EvenAppBridge, delta: string) {
  const offset = streamedText.length;
  streamedText += delta;
  await b.textContainerUpgrade(new TextContainerUpgrade({
    containerID: 3, containerName: 'content',
    contentOffset: offset, contentLength: 0, content: delta,
  }));
}
```

### 7.4 Page Toggle (App-Layer)

> **`setPageFlip` does NOT exist in the SDK.** Page navigation is managed
> entirely in the app by tracking a `currentPage` variable and calling
> `rebuildPageContainer` to swap between page 0 (summary) and page 1 (detail).

```typescript
let currentPage: 0 | 1 = 0;

/**
 * Toggle between page 0 (summary) and page 1 (detail).
 * Called on DOUBLE_CLICK_EVENT while in the `displaying` state.
 */
async function togglePage(
  b: EvenAppBridge,
  query: string,
  response: string,
  meta: { model: string; elapsed: number; session: string },
) {
  currentPage = currentPage === 0 ? 1 : 0;

  if (currentPage === 1) {
    // Page 1 — detail view: full response + metadata
    await rebuild(b, [
      text(1, 'p1_header', 8, 8, 560, 32, 24, 0xF, 'Response Details         [Page 2]'),
      text(2, 'p1_content', 8, 48, 560, 200, 18, 0xF,
        `${response}\n\n━━━━━━━━━━━━━━━\nModel: ${meta.model}\nTime: ${meta.elapsed}s\nSession: ${meta.session}`,
        1),
      text(3, 'p1_footer', 8, 256, 560, 24, 18, 0x6, 'Double-tap: back · Tap: dismiss'),
    ]);
  } else {
    // Page 0 — summary view: truncated response
    const queryTrunc = query.length > 25 ? query.slice(0, 22) + '...' : query;
    const displayText = response.length > 1800
      ? response.slice(0, 1800) + '… [double-tap for more]'
      : response;
    await rebuild(b, [
      text(1, 'title',   8,   8, 460,  32, 18, 0xA, `You: ${queryTrunc}`),
      text(2, 'badge', 480,   8,  88,  32, 18, 0x6, '● Done'),
      text(3, 'content', 8,  48, 560, 200, 18, 0xF, displayText, 1),
      text(4, 'footer',  8, 256, 560,  24, 18, 0x6, 'Tap to dismiss · Double-tap for more'),
    ]);
  }
}
```

## 8. Transition Table

| From | To | Trigger | Display Action | Method |
|---|---|---|---|---|
| **loading** | **idle** | Whisper model loaded (`status:"idle"` from Gateway) | Full layout swap to idle state | `rebuildPageContainer` |
| **idle** | **recording** | Tap (from idle) → `audioControl(true)` + `start_audio` sent | Full layout swap to recording state | `rebuildPageContainer` |
| **recording** | **transcribing** | Tap (from recording) → `audioControl(false)` + `stop_audio` sent; Gateway responds `status:"transcribing"` | Swap to transcribing layout | `rebuildPageContainer` |
| **transcribing** | **thinking** | Gateway sends `transcription` frame + `status:"thinking"` | Show user query text + thinking indicator | `rebuildPageContainer` |
| **thinking** | **streaming** | First `assistant` delta frame received from Gateway | Swap to streaming layout; begin appending deltas | `rebuildPageContainer` (initial), then `textContainerUpgrade` (deltas) |
| **streaming** | **displaying** | Gateway sends `end` frame | Strip cursor, update badge + footer text | `textContainerUpgrade` (badge, footer) |
| **displaying** | **page 1** | Double-tap (`DOUBLE_CLICK_EVENT`) | Swap to detail view (toggle `currentPage` to 1, call `rebuildPageContainer` with page 1 containers) | `rebuildPageContainer` |
| **page 1** | **displaying** | Double-tap (`DOUBLE_CLICK_EVENT`) | Swap back to summary view (toggle `currentPage` to 0, call `rebuildPageContainer` with page 0 containers) | `rebuildPageContainer` |
| **displaying** | **idle** | Tap (`CLICK_EVENT`) | Full layout swap back to idle | `rebuildPageContainer` |
| **any** | **error** | Gateway `error` frame received | Full layout swap to error state | `rebuildPageContainer` |
| **any** | **disconnected** | WebSocket `close` / `error` event | Full layout swap to disconnected state | `rebuildPageContainer` |
| **error** | **idle** | Tap (`CLICK_EVENT`) | Return to idle layout | `rebuildPageContainer` |
| **disconnected** | **idle** | WebSocket reconnect succeeds | Return to idle layout | `rebuildPageContainer` |
| **disconnected** | **disconnected** | Retry timer fires, still no connection | Update footer countdown | `textContainerUpgrade` |

**Notes:**
- In tap-to-toggle mode, transitioning from displaying to recording requires two taps: first tap dismisses (→ idle), second tap starts recording (→ recording).
- The **displaying** state ignores `status:"idle"` from Gateway to prevent premature transition while the user is still reading the response.
- The **loading** state appears during initial Gateway startup while the Whisper model is being loaded into memory. Recording is disabled until loading completes.

### Transition flow diagram

```
                    ┌──────────────┐
          ┌────────►│     idle     │◄─────────────────────┐
          │         └──────┬───────┘                      │
          │                │ tap                           │ tap
          │                ▼                               │
          │         ┌──────────────┐                ┌─────┴────────┐
          │         │  recording   │                │    error      │
          │         └──────┬───────┘                └──────▲────────┘
          │                │ tap                           │
          │                ▼                               │ error frame
          │         ┌──────────────┐                       │ (from any)
          │         │ transcribing │                       │
          │         └──────┬───────┘                ┌─────┴────────┐
          │                │ transcript              │ disconnected │
          │                ▼                         └──────▲───────┘
          │         ┌──────────────┐                       │
          │         │   thinking   │                       │ WS close
          │         └──────┬───────┘                       │ (from any)
          │                │ first delta                    │
          │                ▼                               │
          │         ┌──────────────┐                       │
          │         │  streaming   │───────────────────────┘
          │         └──────┬───────┘
          │                │ end frame
          │                ▼
          │         ┌──────────────────┐    double-tap    ┌────────────┐
          └─────────│    displaying    │◄───────────────►│  page 1    │
             tap    └──────────────────┘                  └────────────┘

          Startup:  loading ──model loaded──► idle
```


