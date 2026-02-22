/**
 * G2 OpenClaw — Display Manager
 *
 * Renders application state layouts on the G2 576×288 micro-LED display.
 * Uses the EvenAppBridge SDK for all display operations.
 *
 * Greyscale palette (4-bit, renders as shades of green on hardware):
 *   0x0 = Background (black/off)
 *   0x3 = Border (subtle dividers)
 *   0x6 = Muted (hints, placeholders, inactive badges)
 *   0xA = Secondary (subtitles, labels, instructional text)
 *   0xC = Accent (active-state indicators)
 *   0xF = Primary (main content, headings)
 */

import type { EvenAppBridge } from '@evenrealities/even_hub_sdk';
import {
  CreateStartUpPageContainer,
  RebuildPageContainer,
  StartUpPageCreateResult,
  TextContainerProperty,
  TextContainerUpgrade,
} from '@evenrealities/even_hub_sdk';
import { stripMarkdown } from './utils';

// ---------------------------------------------------------------------------
// Greyscale constants
// ---------------------------------------------------------------------------
const BG      = 0x0; // Background (black / off)
const MUTED   = 0x6; // Hints, disabled, placeholders
const SECOND  = 0xA; // Secondary text, labels
const ACCENT  = 0xC; // Active-state indicators
const PRIMARY = 0xF; // Main content, headings

// ---------------------------------------------------------------------------
// Container ID constants (consistent across all layouts)
// ---------------------------------------------------------------------------
const ID_TITLE   = 1;
const ID_BADGE   = 2;
const ID_CONTENT = 3;
const ID_FOOTER  = 4;

// ---------------------------------------------------------------------------
// Helper: build a TextContainerProperty
//
// UNDOCUMENTED PROTOBUF FIELDS — fontSize & fontColor
// ---------------------------------------------------
// The official SDK .d.ts does NOT expose `fontSize` or `fontColor` on
// TextContainerProperty.  However, both fields exist in the underlying
// protobuf schema and are serialised by `toJson()`.  We patch them onto
// the constructed instance via `as any`.
//
// ⚠  Behaviour on real G2 hardware is UNVERIFIED.  If the glasses firmware
//    ignores these fields, all text will render at the default size and
//    colour — visual hierarchy will be flat, but the content remains
//    readable on the 576×136-per-eye micro-LED display.
// ---------------------------------------------------------------------------
function text(
  id: number,
  name: string,
  x: number,
  y: number,
  w: number,
  h: number,
  fontSize: number,
  fontColor: number,
  content: string,
  eventCapture: 0 | 1 = 0,
): TextContainerProperty {
  const c = new TextContainerProperty({
    containerID: id,
    containerName: name,
    content,
    xPosition: x,
    yPosition: y,
    width: w,
    height: h,
    borderWidth: 0,
    borderColor: 0,
    borderRdaius: 0, // SDK typo — intentional
    paddingLength: 0,
    isEventCapture: eventCapture,
  });
  // fontSize / fontColor exist in the protobuf but are absent from the .d.ts
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const obj = c as any;
  obj.fontSize = fontSize;
  obj.fontColor = fontColor;
  return c;
}

// ---------------------------------------------------------------------------
// Helper: full-page rebuild
// ---------------------------------------------------------------------------
async function rebuild(
  bridge: EvenAppBridge,
  containers: TextContainerProperty[],
): Promise<boolean> {
  return bridge.rebuildPageContainer(
    new RebuildPageContainer({
      containerTotalNum: containers.length,
      textObject: containers,
    }),
  );
}

// ---------------------------------------------------------------------------
// Truncate a query string for the header (≤25 chars)
// ---------------------------------------------------------------------------
function truncateQuery(query: string, maxLen = 25): string {
  return query.length > maxLen ? query.slice(0, maxLen - 3) + '...' : query;
}

// ---------------------------------------------------------------------------
// DisplayManager
// ---------------------------------------------------------------------------
export class DisplayManager {
  private bridge: EvenAppBridge | null = null;

  /** Whether createStartUpPageContainer has been called successfully */
  private _started = false;

  /** Accumulated response text during streaming */
  private _streamBuffer = '';

  /** Current query (stored for layout reuse) */
  private _query = '';

  /** Last footer text length (for accurate in-place upgrades) */
  private _footerLen = 0;

  // -----------------------------------------------------------------------
  // Lifecycle
  // -----------------------------------------------------------------------

  /**
   * Store bridge reference and register the startup page container.
   * Must be called once after waitForEvenAppBridge().
   * The SDK requires createStartUpPageContainer() to be called exactly once
   * before rebuildPageContainer or audioControl will work.
   */
  async init(bridge: EvenAppBridge): Promise<void> {
    this.bridge = bridge;
    await this.createStartup();
  }

  /**
   * Call createStartUpPageContainer with a minimal loading layout.
   * Sets `_started` on success; throws on failure.
   */
  private async createStartup(): Promise<void> {
    const b = this.bridge;
    if (!b) throw new Error('[Display] Bridge not set before createStartup()');

    const result = await b.createStartUpPageContainer(
      new CreateStartUpPageContainer({
        containerTotalNum: 4,
        textObject: [
          text(ID_TITLE,   'title',   8,   8, 460, 32, 24, PRIMARY, 'OpenClaw'),
          text(ID_BADGE,   'badge', 480,   8,  88, 32, 18, ACCENT,  '● loading'),
          text(ID_CONTENT, 'content', 8, 100, 560, 80, 24, SECOND,  'Starting up...', 1),
          text(ID_FOOTER,  'footer',  8, 256, 560, 24, 18, MUTED,   'Initializing speech model'),
        ],
      }),
    );

    if (result !== StartUpPageCreateResult.success) {
      throw new Error(`[Display] createStartUpPageContainer failed (code ${result})`);
    }
    this._started = true;
  }

  private requireBridge(): EvenAppBridge {
    if (!this.bridge || !this._started) {
      throw new Error('[Display] Bridge not initialised — call init() first');
    }
    return this.bridge;
  }

  // -----------------------------------------------------------------------
  // Layout: Loading (§4.9)
  // -----------------------------------------------------------------------

  async showLoading(): Promise<void> {
    const b = this.requireBridge();
    await rebuild(b, [
      text(ID_TITLE,   'title',   8,   8, 460, 32, 24, PRIMARY, 'OpenClaw'),
      text(ID_BADGE,   'badge', 480,   8,  88, 32, 18, ACCENT,  '● loading'),
      text(ID_CONTENT, 'content', 8, 100, 560, 80, 24, SECOND,  'Starting up...', 1),
      text(ID_FOOTER,  'footer',  8, 256, 560, 24, 18, MUTED,   'Initializing speech model'),
    ]);
  }

  // -----------------------------------------------------------------------
  // Layout: Idle (§4.1)
  // -----------------------------------------------------------------------

  async showIdle(): Promise<void> {
    const b = this.requireBridge();
    this._streamBuffer = '';
    this._query = '';
    await rebuild(b, [
      text(ID_TITLE,   'title',   8,   8, 460, 32, 24, PRIMARY, 'OpenClaw'),
      text(ID_BADGE,   'badge', 480,   8,  88, 32, 18, MUTED,   '● idle'),
      text(ID_CONTENT, 'content', 8, 100, 560, 80, 24, SECOND,  'Tap ring to speak', 1),
      text(ID_FOOTER,  'footer',  8, 256, 560, 24, 18, MUTED,   'Ready'),
    ]);
  }

  // -----------------------------------------------------------------------
  // Layout: Recording (§4.2)
  // -----------------------------------------------------------------------

  async showRecording(): Promise<void> {
    const b = this.requireBridge();
    await rebuild(b, [
      text(ID_TITLE,   'title',   8,   8, 460,  32, 24, PRIMARY, 'OpenClaw'),
      text(ID_BADGE,   'badge', 480,   8,  88,  32, 18, ACCENT,  '● Recording'),
      text(ID_CONTENT, 'content', 8,  80, 560, 160, 24, PRIMARY, 'Listening...\n\n████████░░░░░░░░', 1),
      text(ID_FOOTER,  'footer',  8, 256, 560,  24, 18, SECOND,  'Tap to stop'),
    ]);
  }

  // -----------------------------------------------------------------------
  // Layout: Transcribing (§4.3)
  // TODO: Phase 4 — animated dots via 500ms textContainerUpgrade cycle
  // -----------------------------------------------------------------------

  async showTranscribing(): Promise<void> {
    const b = this.requireBridge();
    await rebuild(b, [
      text(ID_TITLE,   'title',   8,   8, 460, 32, 24, PRIMARY, 'OpenClaw'),
      text(ID_BADGE,   'badge', 480,   8,  88, 32, 18, ACCENT,  '● Transcribing'),
      text(ID_CONTENT, 'content', 8, 100, 560, 80, 24, SECOND,  'Transcribing audio...', 1),
      text(ID_FOOTER,  'footer',  8, 256, 560, 24, 18, MUTED,   'Processing speech'),
    ]);
  }

  // -----------------------------------------------------------------------
  // Layout: Thinking (§4.4)
  // -----------------------------------------------------------------------

  async showThinking(query: string): Promise<void> {
    const b = this.requireBridge();
    this._query = query;
    // H4: template overhead is ~40 chars; keep total content ≤ 1000
    const maxQueryLen = 960;
    const safeQuery = query.length > maxQueryLen
      ? query.slice(0, maxQueryLen - 1) + '…'
      : query;
    await rebuild(b, [
      text(ID_TITLE,   'title',   8,   8, 460,  32, 24, PRIMARY, 'OpenClaw'),
      text(ID_BADGE,   'badge', 480,   8,  88,  32, 18, ACCENT,  '● Thinking'),
      text(ID_CONTENT, 'content', 8,  48, 560, 200, 18, PRIMARY,
        `You: ${safeQuery}\n\n━━━━━━━━━━━━━━━\nThinking...`, 1),
      text(ID_FOOTER,  'footer',  8, 256, 560,  24, 18, MUTED,   'Waiting for response'),
    ]);
  }

  // -----------------------------------------------------------------------
  // Layout: Streaming (§4.5)
  // -----------------------------------------------------------------------

  async showStreaming(query: string): Promise<void> {
    const b = this.requireBridge();
    this._query = query;
    this._streamBuffer = '';
    const queryTrunc = truncateQuery(query);
    await rebuild(b, [
      text(ID_TITLE,   'title',   8,   8, 460,  32, 18, SECOND, `You: ${queryTrunc}`),
      text(ID_BADGE,   'badge', 480,   8,  88,  32, 18, ACCENT, '● Streaming'),
      text(ID_CONTENT, 'content', 8,  48, 560, 200, 18, PRIMARY, '█', 1),
      text(ID_FOOTER,  'footer',  8, 256, 560,  24, 18, MUTED,  'Streaming...'),
    ]);
  }

  // -----------------------------------------------------------------------
  // Streaming: append delta via textContainerUpgrade
  // -----------------------------------------------------------------------

  async appendDelta(delta: string): Promise<void> {
    const b = this.requireBridge();

    // M10: strip markdown from incoming delta before buffering
    const cleanDelta = stripMarkdown(delta);

    // H3: guard against unbounded buffer growth (SDK content max ~2000)
    if (this._streamBuffer.length + cleanDelta.length > 1900) {
      await this.showResponse(this._query, this._streamBuffer);
      return;
    }

    const offset = this._streamBuffer.length;
    this._streamBuffer += cleanDelta;

    // Replace everything from the start to show accumulated text + cursor
    await b.textContainerUpgrade(
      new TextContainerUpgrade({
        containerID: ID_CONTENT,
        containerName: 'content',
        contentOffset: offset,
        contentLength: 1, // replace the trailing cursor character
        content: cleanDelta + '█',
      }),
    );
  }

  // -----------------------------------------------------------------------
  // Layout: Response Complete / Displaying (§4.6)
  // -----------------------------------------------------------------------

  async showResponse(query: string, response: string): Promise<void> {
    const b = this.requireBridge();
    this._query = query;
    this._streamBuffer = response;
    const queryTrunc = truncateQuery(query);

    // M10: strip markdown before display
    const clean = stripMarkdown(response);

    // C2: SDK TextContainerProperty.content max is 1000 chars at rebuild.
    // The suffix '… [double-tap for more]' is 22 chars → 978 + 22 = 1000.
    const displayText = clean.length > 978
      ? clean.slice(0, 978) + '… [double-tap for more]'
      : clean;

    await rebuild(b, [
      text(ID_TITLE,   'title',   8,   8, 460,  32, 18, SECOND, `You: ${queryTrunc}`),
      text(ID_BADGE,   'badge', 480,   8,  88,  32, 18, MUTED,  '● Done'),
      text(ID_CONTENT, 'content', 8,  48, 560, 200, 18, PRIMARY, displayText, 1),
      text(ID_FOOTER,  'footer',  8, 256, 560,  24, 18, MUTED,  'Tap to dismiss · Double-tap for more'),
    ]);
  }

  /**
   * Transition from streaming → displaying in-place (badge + footer only).
   * Strips the cursor and updates badge/footer without a full rebuild.
   *
   * NOTE (M8): The badge text shrinks from '● Streaming' (11 chars) to
   * '● Done' (6 chars). We assume the SDK handles shorter replacement text
   * correctly via textContainerUpgrade. If this produces rendering artefacts
   * on hardware, replace this method body with a full `rebuild()` call.
   */
  async finaliseStream(): Promise<void> {
    const b = this.requireBridge();

    // Strip the trailing cursor from content
    const cursorLen = '█'.length;
    await b.textContainerUpgrade(
      new TextContainerUpgrade({
        containerID: ID_CONTENT,
        containerName: 'content',
        contentOffset: this._streamBuffer.length,
        contentLength: cursorLen,
        content: '', // remove cursor
      }),
    );

    // Update badge: Streaming → Done
    await b.textContainerUpgrade(
      new TextContainerUpgrade({
        containerID: ID_BADGE,
        containerName: 'badge',
        contentOffset: 0,
        contentLength: '● Streaming'.length,
        content: '● Done',
      }),
    );

    // Update footer
    const footerContent = 'Tap to dismiss · Double-tap for more';
    await b.textContainerUpgrade(
      new TextContainerUpgrade({
        containerID: ID_FOOTER,
        containerName: 'footer',
        contentOffset: 0,
        contentLength: this._footerLen || 'Streaming...'.length,
        content: footerContent,
      }),
    );
    this._footerLen = footerContent.length;
  }

  // -----------------------------------------------------------------------
  // Layout: Error (§4.7)
  // -----------------------------------------------------------------------

  async showError(message: string, hint = 'Try again'): Promise<void> {
    const b = this.requireBridge();
    await rebuild(b, [
      text(ID_TITLE,   'title',   8,   8, 460,  32, 24, PRIMARY, 'OpenClaw'),
      text(ID_BADGE,   'badge', 480,   8,  88,  32, 18, PRIMARY, '✕ Error'),
      text(ID_CONTENT, 'content', 8,  48, 560, 200, 18, PRIMARY,
        `Error\n\n${message}\n\n${hint}`, 1),
      text(ID_FOOTER,  'footer',  8, 256, 560,  24, 18, SECOND,  'Tap to dismiss'),
    ]);
  }

  // -----------------------------------------------------------------------
  // Layout: Disconnected (§4.8)
  // -----------------------------------------------------------------------

  async showDisconnected(retrySeconds?: number): Promise<void> {
    const b = this.requireBridge();
    const footerText = retrySeconds !== undefined
      ? `Retry in ${retrySeconds}s...`
      : 'Reconnecting...';

    await rebuild(b, [
      text(ID_TITLE,   'title',   8,   8, 460,  32, 24, PRIMARY, 'OpenClaw'),
      text(ID_BADGE,   'badge', 480,   8,  88,  32, 18, MUTED,   '○ Offline'),
      text(ID_CONTENT, 'content', 8,  60, 560, 180, 18, SECOND,
        'Connecting to Gateway...\n\n━━━━━━━━━━━━━━━\n\nMake sure your PC is on\nand connected to the same\nWiFi network.', 1),
      text(ID_FOOTER,  'footer',  8, 256, 560,  24, 18, MUTED,   footerText),
    ]);
    // M5: track actual footer length for in-place upgrades
    this._footerLen = footerText.length;
  }

  /**
   * Update disconnected footer countdown in-place.
   */
  async updateRetryCountdown(seconds: number): Promise<void> {
    const b = this.requireBridge();
    const newText = `Retry in ${seconds}s...`;
    // M5: use tracked footer length instead of hardcoded value
    await b.textContainerUpgrade(
      new TextContainerUpgrade({
        containerID: ID_FOOTER,
        containerName: 'footer',
        contentOffset: 0,
        contentLength: this._footerLen || newText.length,
        content: newText,
      }),
    );
    this._footerLen = newText.length;
  }

  // -----------------------------------------------------------------------
  // Page 1: Detail View (§5)
  // -----------------------------------------------------------------------

  async showDetailPage(
    response: string,
    meta: { model: string; elapsed: number; session: string },
  ): Promise<void> {
    const b = this.requireBridge();
    await rebuild(b, [
      text(1, 'p1_header',  8,   8, 560,  32, 24, PRIMARY,
        'Response Details         [Page 2]'),
      text(2, 'p1_content', 8,  48, 560, 200, 18, PRIMARY,
        `${response}\n\n━━━━━━━━━━━━━━━\nModel: ${meta.model}\nTime: ${meta.elapsed}s\nSession: ${meta.session}`, 1),
      text(3, 'p1_footer',  8, 256, 560,  24, 18, MUTED,
        'Double-tap: back · Tap: dismiss'),
    ]);
  }

  // -----------------------------------------------------------------------
  // Accessors for external use (e.g., page toggle logic)
  // -----------------------------------------------------------------------

  /** Get the accumulated stream buffer content */
  get streamBuffer(): string {
    return this._streamBuffer;
  }

  /** Get the current query */
  get query(): string {
    return this._query;
  }
}
