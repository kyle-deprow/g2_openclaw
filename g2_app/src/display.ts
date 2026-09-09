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
  RebuildPageContainer,
  StartUpPageCreateResult,
  TextContainerProperty,
  TextContainerUpgrade,
} from '@evenrealities/even_hub_sdk';
import type { ConversationHistory } from './conversation';

// ---------------------------------------------------------------------------
// Container IDs (stable across all rebuilds)
// ---------------------------------------------------------------------------
const ID_STATUS         = 1;
const ID_TRANSCRIPT     = 2;
const ID_FOOTER         = 3;

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
  content: string,
  eventCapture: 0 | 1 = 0,
): TextContainerProperty {
  return new TextContainerProperty({
    containerID: id,
    containerName: name,
    content,
    xPosition: x,
    yPosition: y,
    width: w,
    height: h,
    borderWidth: 0,
    borderColor: 0,
    borderRadius: 0,
    paddingLength: 0,
    isEventCapture: eventCapture,
  });
}

// ---------------------------------------------------------------------------
// DisplayManager
// ---------------------------------------------------------------------------
export class DisplayManager {
  private bridge: EvenAppBridge | null = null;
  private _started = false;
  private _arHeader: string | null = null;
  private _arFooter: string | null = null;
  private _idleStatusShown = false;

  /** Reference to the shared conversation model */
  private conversation: ConversationHistory | null = null;

  /** Tracked lengths for in-place textContainerUpgrade */
  private _statusLen = 0;
  private _footerLen = 0;
  private _transcriptLen = 0;



  /** Debounce for streaming delta flushes */
  private _deltaBatch: string[] = [];
  private _deltaTimer: ReturnType<typeof setTimeout> | null = null;
  private static readonly DELTA_FLUSH_MS = 100;
  private _opQueue: Promise<void> = Promise.resolve();

  /** Timer for auto-reverting the session-reset banner */
  private _resetTimer: ReturnType<typeof setTimeout> | null = null;

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
    console.log('[Display] _createStartup: bridge OK');

    const statusText = 'OpenClaw  ● Loading';
    const transcriptText = 'Starting up...';
    const footerText = 'Initialising';

    console.log('[Display] Building container objects...');
    const textObjects = [
      text(ID_STATUS,     'status',     8,   2, 560,  24, statusText),
      text(ID_TRANSCRIPT, 'transcript', 8,  34, 560, 212, transcriptText, 1),
      text(ID_FOOTER,     'footer',     8, 256, 560,  26, footerText),
    ];
    console.log('[Display] textObjects: %d', textObjects.length);

    const payload = new CreateStartUpPageContainer({
      containerTotalNum: 3,
      textObject: textObjects,
    });
    console.log('[Display] Payload built, calling createStartUpPageContainer...');

    let result: StartUpPageCreateResult;
    try {
      result = await b.createStartUpPageContainer(payload);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      console.error('[Display] createStartUpPageContainer threw:', msg, typeof e, e);
      throw e;
    }

    console.log('[Display] createStartUpPageContainer result:', result, typeof result);
    if (result !== StartUpPageCreateResult.success) {
      throw new Error(`[Display] createStartUpPageContainer failed (code ${result})`);
    }

    this._started = true;
    this._statusLen = statusText.length;
    this._transcriptLen = transcriptText.length;
    this._footerLen = footerText.length;
    console.log('[Display] _createStartup complete');
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
      ? this.conversation.formatReverse(REBUILD_CHAR_LIMIT - 50)
      : 'Ready.';

    await b.rebuildPageContainer(
      new RebuildPageContainer({
        containerTotalNum: 3,
        textObject: [
          text(ID_STATUS,     'status',     8,   2, 560,  24, statusText),
          text(ID_TRANSCRIPT, 'transcript', 8,  34, 560, 212, transcriptText, 1),
          text(ID_FOOTER,     'footer',     8, 256, 560,  26, footerHint),
        ],
      }),
    );

    this._statusLen = statusText.length;
    this._transcriptLen = transcriptText.length;
    this._footerLen = footerHint.length;
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
      const footer = this._idleStatusShown ? (this._arFooter ?? 'Tap to interact') : 'Tap to interact';
      await this._doRebuild(this._resolveIdleStatus(), footer);
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
      this._idleStatusShown = false;
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
    this._deltaBatch = [];
    // Reverse mode: re-render from conversation model so newest
    // (currently streaming) entry stays at the top.
    const transcript = this.conversation
      ? this.conversation.formatReverse(UPGRADE_CHAR_LIMIT - 100)
      : '';
    await this.replaceTranscript(transcript);
  }

  clearDeltaTimer(): void {
    if (this._deltaTimer) {
      clearTimeout(this._deltaTimer);
      this._deltaTimer = null;
    }
    this._deltaBatch = [];
  }

  async flushRemainingDeltas(): Promise<void> {
    return this.enqueue(() => this._flushRemainingDeltas());
  }

  private async _flushRemainingDeltas(): Promise<void> {
    if (this._deltaTimer) {
      clearTimeout(this._deltaTimer);
      this._deltaTimer = null;
    }
    if (this._deltaBatch.length > 0) {
      this._deltaBatch = [];
      const transcript = this.conversation
        ? this.conversation.formatReverse(UPGRADE_CHAR_LIMIT - 100)
        : '';
      await this._doReplace(transcript);
    }
  }

  // -----------------------------------------------------------------------
  // High-level state display methods
  // -----------------------------------------------------------------------

  async showIdle(): Promise<void> {
    this.clearDeltaTimer();
    this.clearResetTimer();
    this._idleStatusShown = true;

    // Keep the complete idle repaint in one serialized operation.  A local
    // gesture flips _idleStatusShown synchronously while a bridge call is in
    // flight; every follow-up repaint checks it before touching the display.
    await this.enqueue(async () => {
      if (!this._idleStatusShown) return;
      const transcript = this.conversation
        ? this.conversation.formatReverse(UPGRADE_CHAR_LIMIT - 100)
        : 'Ready.';
      const statusLabel = this._resolveIdleStatus();
      const footerHint = this._arFooter ?? 'Tap to interact';

      if (transcript.length > UPGRADE_CHAR_LIMIT - 100) {
        await this._doRebuild(statusLabel, footerHint);
      } else {
        await this._doReplace(transcript);
      }
      if (!this._idleStatusShown) return;
      await this.updateStatus(statusLabel);
      if (!this._idleStatusShown) return;
      await this.updateFooter(footerHint);
    });
  }

  /** Check the latest system message for a [TASK:*] marker and return an appropriate status label. */
  private _resolveIdleStatus(): string {
    if (this._arHeader !== null) return this._arHeader;
    if (!this.conversation) return 'Idle';
    const entries = this.conversation.getEntries();
    // Walk backwards to find the most recent system entry
    for (let i = entries.length - 1; i >= 0; i--) {
      const e = entries[i];
      if (e.role !== 'system') continue;
      if (e.text.startsWith('RUNNING:')) return '● Task Running';
      if (e.text.startsWith('COMPLETE:')) return '✓ Task Done';
      if (e.text.startsWith('FAILED:')) return '✗ Task Failed';
      break; // Only check the most recent system message
    }
    return 'Idle';
  }

  async setAutoresearchHeader(header: string | null): Promise<void> {
    this._arHeader = header;
    if (this._idleStatusShown) {
      await this.enqueue(async () => {
        if (!this._idleStatusShown) return;
        await this.updateStatus(this._resolveIdleStatus());
      });
    }
  }

  /**
   * Cache research status footer text, but only paint it while the normal
   * idle layout is still visible.  Status polling can race a user gesture;
   * rechecking inside the serialized operation keeps a queued update from
   * overwriting confirmation, recording, or streaming affordances.
   */
  async setAutoresearchFooter(footer: string | null): Promise<void> {
    this._arFooter = footer;
    if (!this._idleStatusShown) return;
    await this.enqueue(async () => {
      if (!this._idleStatusShown) return;
      await this.updateFooter(this._arFooter ?? 'Tap to interact');
    });
  }

  async showRecording(): Promise<void> {
    this.clearResetTimer();
    this._idleStatusShown = false;
    await this.updateStatus('Recording');
    await this.updateFooter('Tap to stop');
  }

  async showTranscribing(): Promise<void> {
    this.clearResetTimer();
    this._idleStatusShown = false;
    await this.updateStatus('Transcribing');
    await this.updateFooter('Processing speech...');
  }

  async showTranscription(): Promise<void> {
    const transcript = this.conversation ? this.conversation.formatReverse(UPGRADE_CHAR_LIMIT - 100) : '';
    if (transcript.length > UPGRADE_CHAR_LIMIT - 100) {
      await this.rebuildTranscript('Transcribing', 'Sending to OpenClaw...');
    } else {
      await this.replaceTranscript(transcript);
    }
    await this.updateFooter('Sending to OpenClaw...');
  }

  async showConfirming(transcriptionText: string): Promise<void> {
    this.clearResetTimer();
    this._idleStatusShown = false;
    const transcript = this.conversation ? this.conversation.formatReverse(UPGRADE_CHAR_LIMIT - 100) : transcriptionText;
    if (transcript.length > UPGRADE_CHAR_LIMIT - 100) {
      await this.rebuildTranscript('Confirm?', 'Tap = send · 2×tap = reject');
    } else {
      await this.replaceTranscript(transcript);
      await this.updateStatus('Confirm?');
      await this.updateFooter('Tap = send · 2×tap = reject');
    }
  }

  async showThinking(): Promise<void> {
    this.clearResetTimer();
    this._idleStatusShown = false;
    await this.updateStatus('Thinking');
    await this.updateFooter('Waiting for response...');
  }

  async showStreaming(): Promise<void> {
    this.clearResetTimer();
    this._idleStatusShown = false;
    await this.updateStatus('Streaming');
    await this.updateFooter('Streaming...');
    const transcript = this.conversation ? this.conversation.formatReverse(UPGRADE_CHAR_LIMIT - 100) : '';
    if (transcript.length > UPGRADE_CHAR_LIMIT - 100) {
      await this.rebuildTranscript('Streaming', 'Streaming...');
    } else {
      await this.replaceTranscript(transcript);
    }
  }

  async finaliseStream(): Promise<void> {
    this._idleStatusShown = true;
    await this.enqueue(async () => {
      if (!this._idleStatusShown) return;
      await this._flushRemainingDeltas();
      if (!this._idleStatusShown) return;

      const transcript = this.conversation
        ? this.conversation.formatReverse(UPGRADE_CHAR_LIMIT - 100)
        : '';
      await this._doReplace(transcript);
      if (!this._idleStatusShown) return;

      await this.updateStatus(this._resolveIdleStatus());
      if (!this._idleStatusShown) return;
      await this.updateFooter(this._arFooter ?? 'Tap to interact');
    });
  }

  async showError(message: string, hint = 'Tap to continue'): Promise<void> {
    this.clearDeltaTimer();
    this.clearResetTimer();
    this._idleStatusShown = false;
    await this.updateStatus('Error');
    const transcript = this.conversation ? this.conversation.formatReverse(UPGRADE_CHAR_LIMIT - 100) : '';
    const displayText = transcript || message || 'Error';
    if (displayText.length > UPGRADE_CHAR_LIMIT - 100) {
      await this.rebuildTranscript('Error', hint);
    } else {
      await this.replaceTranscript(displayText);
      await this.updateFooter(hint);
    }
  }

  async showDisconnected(): Promise<void> {
    this.clearDeltaTimer();
    this.clearResetTimer();
    this._idleStatusShown = false;
    await this.updateStatus('Offline');
    await this.updateFooter('Reconnecting...');
  }

  async showLoading(): Promise<void> {
    this._idleStatusShown = false;
    await this.updateStatus('Loading');
    await this.updateFooter('Initialising speech model');
  }

  async showSessionReset(label: string): Promise<void> {
    this.clearDeltaTimer();
    this.clearResetTimer();
    this._idleStatusShown = false;
    await this.updateStatus('New Session');
    const transcript = this.conversation ? this.conversation.formatReverse(UPGRADE_CHAR_LIMIT - 100) : label;
    if (transcript.length > UPGRADE_CHAR_LIMIT - 100) {
      await this.rebuildTranscript('New Session', 'Session cleared · Tap to talk');
    } else {
      await this.replaceTranscript(transcript);
      await this.updateFooter('Session cleared · Tap to talk');
    }
    // After 2 seconds, revert to normal idle display
    this._resetTimer = setTimeout(() => {
      this._resetTimer = null;
      this.showIdle().catch(err => console.error('[Display] Error reverting from reset:', err));
    }, 2000);
  }

  private clearResetTimer(): void {
    if (this._resetTimer) {
      clearTimeout(this._resetTimer);
      this._resetTimer = null;
    }
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
