/**
 * G2 OpenClaw — Display Manager (Conversation Transcript Mode)
 *
 * Renders a scrollable conversation transcript on the G2 576×288 micro-LED
 * display.  The layout is fixed: status bar + transcript + footer.  State
 * changes only update the status bar and footer text in-place via
 * textContainerUpgrade — no full rebuild except when the transcript
 * content exceeds the upgrade character limit and needs a fresh page.
 *
 * Greyscale palette (4-bit, renders as shades of green on hardware):
 *   0x0 = Background (black/off)
 *   0x6 = Muted (hints, placeholders)
 *   0xA = Secondary (labels, status)
 *   0xC = Accent (active-state indicators)
 *   0xF = Primary (main content, headings)
 */

import type { EvenAppBridge } from '@evenrealities/even_hub_sdk';
import {
  CreateStartUpPageContainer,
  ListContainerProperty,
  ListItemContainerProperty,
  RebuildPageContainer,
  StartUpPageCreateResult,
  TextContainerProperty,
  TextContainerUpgrade,
} from '@evenrealities/even_hub_sdk';
import type { ConversationHistory } from './conversation';

// ---------------------------------------------------------------------------
// Greyscale constants
// ---------------------------------------------------------------------------
const MUTED   = 0x6;
const SECOND  = 0xA;
const PRIMARY = 0xF;

// ---------------------------------------------------------------------------
// Container IDs (stable across all rebuilds)
// ---------------------------------------------------------------------------
const ID_STATUS         = 1;
const ID_TRANSCRIPT     = 3;
const ID_FOOTER         = 4;
const ID_EVENT_CAPTURE  = 5;

// ---------------------------------------------------------------------------
// Character limits
// ---------------------------------------------------------------------------
/** Max chars for text content at createStartUp / rebuild */
const REBUILD_CHAR_LIMIT = 1000;
/** Max chars for textContainerUpgrade */
const UPGRADE_CHAR_LIMIT = 2000;

// ---------------------------------------------------------------------------
// Helper: build a TextContainerProperty
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
// Helper: invisible list container for event capture (simulator compat)
// ---------------------------------------------------------------------------
function eventCaptureList(): ListContainerProperty {
  return new ListContainerProperty({
    xPosition: 0,
    yPosition: 0,
    width: 1,
    height: 1,
    borderWidth: 0,
    borderColor: 0,
    borderRdaius: 0, // SDK typo — intentional
    paddingLength: 0,
    containerID: ID_EVENT_CAPTURE,
    containerName: 'capture',
    isEventCapture: 1,
    itemContainer: new ListItemContainerProperty({
      itemCount: 1,
      itemWidth: 1,
      isItemSelectBorderEn: 0,
      itemName: ['tap'],
    }),
  });
}

// ---------------------------------------------------------------------------
// DisplayManager
// ---------------------------------------------------------------------------
export class DisplayManager {
  private bridge: EvenAppBridge | null = null;
  private _started = false;

  /** Reference to the shared conversation model */
  private conversation: ConversationHistory | null = null;

  /** Tracked lengths for in-place textContainerUpgrade */
  private _statusLen = 0;
  private _footerLen = 0;
  private _transcriptLen = 0;

  /** Total chars appended since last rebuild (to decide when a rebuild is needed) */
  private _transcriptCharBudgetUsed = 0;

  /** Debounce for streaming delta flushes */
  private _deltaBatch: string[] = [];
  private _deltaTimer: ReturnType<typeof setTimeout> | null = null;
  private static readonly DELTA_FLUSH_MS = 100;
  private _opQueue: Promise<void> = Promise.resolve();

  // -----------------------------------------------------------------------
  // Lifecycle
  // -----------------------------------------------------------------------

  async init(bridge: EvenAppBridge, conv: ConversationHistory): Promise<void> {
    this.bridge = bridge;
    this.conversation = conv;
    await this._createStartup();
  }

  private async _createStartup(): Promise<void> {
    const b = this.bridge;
    if (!b) throw new Error('[Display] Bridge not set');

    const statusText = 'OpenClaw  ● Loading';
    const transcriptText = 'Starting up...';
    const footerText = 'Initialising';

    const result = await b.createStartUpPageContainer(
      new CreateStartUpPageContainer({
        containerTotalNum: 4,
        textObject: [
          text(ID_STATUS,     'status',     8,   4, 560,  22, 16, SECOND, statusText),
          text(ID_TRANSCRIPT, 'transcript', 8,  30, 560, 228, 18, PRIMARY, transcriptText, 1),
          text(ID_FOOTER,     'footer',     8, 262, 560,  22, 14, MUTED,  footerText),
        ],
        listObject: [eventCaptureList()],
      }),
    );

    if (result !== StartUpPageCreateResult.success) {
      throw new Error(`[Display] createStartUpPageContainer failed (code ${result})`);
    }

    this._started = true;
    this._statusLen = statusText.length;
    this._transcriptLen = transcriptText.length;
    this._footerLen = footerText.length;
    this._transcriptCharBudgetUsed = 0;
  }

  private requireBridge(): EvenAppBridge {
    if (!this.bridge || !this._started) {
      throw new Error('[Display] Bridge not initialised — call init() first');
    }
    return this.bridge;
  }

  private enqueue(fn: () => Promise<void>): Promise<void> {
    this._opQueue = this._opQueue.then(fn, fn);
    return this._opQueue;
  }

  // -----------------------------------------------------------------------
  // Full rebuild (used sparingly — causes flicker & scroll reset)
  // -----------------------------------------------------------------------

  async rebuildTranscript(statusLabel: string, footerHint: string): Promise<void> {
    return this.enqueue(() => this._doRebuild(statusLabel, footerHint));
  }

  private async _doRebuild(statusLabel: string, footerHint: string): Promise<void> {
    this.clearDeltaTimer();
    const b = this.requireBridge();

    const statusText = `OpenClaw  ● ${statusLabel}`;
    const transcriptText = this.conversation
      ? this.conversation.formatTail(REBUILD_CHAR_LIMIT - 50)
      : 'Ready.\n\nTap ring to ask anything.';

    await b.rebuildPageContainer(
      new RebuildPageContainer({
        containerTotalNum: 4,
        textObject: [
          text(ID_STATUS,     'status',     8,   4, 560,  22, 16, SECOND, statusText),
          text(ID_TRANSCRIPT, 'transcript', 8,  30, 560, 228, 18, PRIMARY, transcriptText, 1),
          text(ID_FOOTER,     'footer',     8, 262, 560,  22, 14, MUTED,  footerHint),
        ],
        listObject: [eventCaptureList()],
      }),
    );

    this._statusLen = statusText.length;
    this._transcriptLen = transcriptText.length;
    this._footerLen = footerHint.length;
    this._transcriptCharBudgetUsed = 0;
  }

  // -----------------------------------------------------------------------
  // In-place updates (preferred — no flicker, preserves scroll)
  // -----------------------------------------------------------------------

  async updateStatus(label: string): Promise<void> {
    const b = this.requireBridge();
    const newText = `OpenClaw  ● ${label}`;
    await b.textContainerUpgrade(
      new TextContainerUpgrade({
        containerID: ID_STATUS,
        containerName: 'status',
        contentOffset: 0,
        contentLength: this._statusLen,
        content: newText,
      }),
    );
    this._statusLen = newText.length;
  }

  async updateFooter(hint: string): Promise<void> {
    const b = this.requireBridge();
    await b.textContainerUpgrade(
      new TextContainerUpgrade({
        containerID: ID_FOOTER,
        containerName: 'footer',
        contentOffset: 0,
        contentLength: this._footerLen,
        content: hint,
      }),
    );
    this._footerLen = hint.length;
  }

  /**
   * Replace the entire transcript content in-place.
   * Falls back to a full rebuild if the text is too long.
   */
  async replaceTranscript(transcriptText: string): Promise<void> {
    return this.enqueue(() => this._doReplace(transcriptText));
  }

  private async _doReplace(transcriptText: string): Promise<void> {
    if (transcriptText.length > UPGRADE_CHAR_LIMIT - 100) {
      await this._doRebuild('idle', 'Tap to speak');
      return;
    }
    const b = this.requireBridge();
    await b.textContainerUpgrade(
      new TextContainerUpgrade({
        containerID: ID_TRANSCRIPT,
        containerName: 'transcript',
        contentOffset: 0,
        contentLength: this._transcriptLen,
        content: transcriptText,
      }),
    );
    this._transcriptLen = transcriptText.length;
    this._transcriptCharBudgetUsed = 0;
  }

  /**
   * Append text to the end of the transcript (for streaming deltas).
   * Triggers a rebuild if the cumulative appended text is too large.
   */
  async appendToTranscript(appendText: string): Promise<void> {
    return this.enqueue(() => this._doAppend(appendText));
  }

  private async _doAppend(appendText: string): Promise<void> {
    if (!appendText) return;

    if (this._transcriptLen + appendText.length > UPGRADE_CHAR_LIMIT - 100) {
      await this._doRebuild('Streaming', 'Streaming...');
      return;
    }

    const b = this.requireBridge();
    await b.textContainerUpgrade(
      new TextContainerUpgrade({
        containerID: ID_TRANSCRIPT,
        containerName: 'transcript',
        contentOffset: this._transcriptLen,
        contentLength: 0,
        content: appendText,
      }),
    );
    this._transcriptLen += appendText.length;
    this._transcriptCharBudgetUsed += appendText.length;
  }

  // -----------------------------------------------------------------------
  // Streaming delta batching (100ms debounce)
  // -----------------------------------------------------------------------

  async appendDelta(delta: string): Promise<void> {
    if (!delta) return;
    this._deltaBatch.push(delta);
    if (!this._deltaTimer) {
      this._deltaTimer = setTimeout(() => {
        this._flushDeltas().catch((err) =>
          console.error('[Display] delta flush failed:', err),
        );
      }, DisplayManager.DELTA_FLUSH_MS);
    }
  }

  private async _flushDeltas(): Promise<void> {
    this._deltaTimer = null;
    if (this._deltaBatch.length === 0) return;
    const merged = this._deltaBatch.join('');
    this._deltaBatch = [];
    await this.appendToTranscript(merged);
  }

  clearDeltaTimer(): void {
    if (this._deltaTimer) {
      clearTimeout(this._deltaTimer);
      this._deltaTimer = null;
    }
    this._deltaBatch = [];
  }

  async flushRemainingDeltas(): Promise<void> {
    if (this._deltaTimer) {
      clearTimeout(this._deltaTimer);
      this._deltaTimer = null;
    }
    if (this._deltaBatch.length > 0) {
      const merged = this._deltaBatch.join('');
      this._deltaBatch = [];
      await this.appendToTranscript(merged);
    }
  }

  // -----------------------------------------------------------------------
  // High-level state display methods
  // -----------------------------------------------------------------------

  async showIdle(): Promise<void> {
    this.clearDeltaTimer();
    const transcript = this.conversation
      ? this.conversation.format()
      : 'Ready.\n\nTap ring to ask anything.';

    if (transcript.length > UPGRADE_CHAR_LIMIT - 100) {
      await this.rebuildTranscript('Idle', 'Tap to speak · Scroll to read');
    } else {
      await this.replaceTranscript(transcript);
      await this.updateStatus('Idle');
      await this.updateFooter('Tap to speak · Scroll to read');
    }
  }

  async showRecording(): Promise<void> {
    await this.updateStatus('Recording');
    await this.updateFooter('Tap to stop');
  }

  async showTranscribing(): Promise<void> {
    await this.updateStatus('Transcribing');
    await this.updateFooter('Processing speech...');
  }

  async showTranscription(): Promise<void> {
    const transcript = this.conversation ? this.conversation.format() : '';
    if (transcript.length > UPGRADE_CHAR_LIMIT - 100) {
      await this.rebuildTranscript('Transcribing', 'Sending to OpenClaw...');
    } else {
      await this.replaceTranscript(transcript);
    }
    await this.updateFooter('Sending to OpenClaw...');
  }

  async showThinking(): Promise<void> {
    await this.updateStatus('Thinking');
    await this.updateFooter('Waiting for response...');
  }

  async showStreaming(): Promise<void> {
    await this.updateStatus('Streaming');
    await this.updateFooter('Streaming...');
    const transcript = this.conversation ? this.conversation.format() : '';
    if (transcript.length > UPGRADE_CHAR_LIMIT - 200) {
      await this.rebuildTranscript('Streaming', 'Streaming...');
    } else {
      await this.replaceTranscript(transcript);
    }
  }

  async finaliseStream(): Promise<void> {
    await this.flushRemainingDeltas();
    const transcript = this.conversation ? this.conversation.format() : '';
    await this.replaceTranscript(transcript);
    await this.updateStatus('Idle');
    await this.updateFooter('Tap to speak · Scroll to read');
  }

  async showError(message: string, hint = 'Tap to continue'): Promise<void> {
    this.clearDeltaTimer();
    await this.updateStatus('Error');
    const transcript = this.conversation ? this.conversation.format() : '';
    if (transcript.length > UPGRADE_CHAR_LIMIT - 100) {
      await this.rebuildTranscript('Error', hint);
    } else {
      await this.replaceTranscript(transcript);
      await this.updateFooter(hint);
    }
  }

  async showDisconnected(): Promise<void> {
    this.clearDeltaTimer();
    await this.updateStatus('Offline');
    await this.updateFooter('Reconnecting...');
  }

  async showLoading(): Promise<void> {
    await this.updateStatus('Loading');
    await this.updateFooter('Initialising speech model');
  }

  // -----------------------------------------------------------------------
  // Accessors (backward compat for InputHandler)
  // -----------------------------------------------------------------------

  get streamBuffer(): string {
    return this.conversation ? this.conversation.lastAssistantText : '';
  }

  get query(): string {
    if (!this.conversation) return '';
    return this.conversation.lastUserText;
  }
}
