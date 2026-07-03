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
import type { SessionListEntry } from './protocol';

// ---------------------------------------------------------------------------
// Container IDs (stable across all rebuilds)
// ---------------------------------------------------------------------------
const ID_STATUS         = 1;
const ID_TRANSCRIPT     = 2;
const ID_FOOTER         = 3;
const ID_MENU_LIST      = 4; // reuses slot 4 (menu mode replaces transcript mode)

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
  private _mode: 'transcript' | 'menu' = 'transcript';

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
    this._mode = 'transcript';
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
    if (this._mode !== 'transcript') return;
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
    if (this._mode !== 'transcript') return;
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
    if (this._mode !== 'transcript') return;
    return this.enqueue(() => this._doReplace(transcriptText));
  }

  private async _doReplace(transcriptText: string): Promise<void> {
    if (transcriptText.length > UPGRADE_CHAR_LIMIT - 100) {
      await this._doRebuild('idle', 'Tap to interact');
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
    if (this._mode !== 'transcript') return;
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
  }

  // -----------------------------------------------------------------------
  // Streaming delta batching (100ms debounce)
  // -----------------------------------------------------------------------

  async appendDelta(delta: string): Promise<void> {
    if (!delta) return;
    if (this._mode !== 'transcript') return;
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
    if (this._deltaTimer) {
      clearTimeout(this._deltaTimer);
      this._deltaTimer = null;
    }
    if (this._deltaBatch.length > 0) {
      this._deltaBatch = [];
      const transcript = this.conversation
        ? this.conversation.formatReverse(UPGRADE_CHAR_LIMIT - 100)
        : '';
      await this.replaceTranscript(transcript);
    }
  }

  // -----------------------------------------------------------------------
  // High-level state display methods
  // -----------------------------------------------------------------------

  async showIdle(): Promise<void> {
    this.clearDeltaTimer();
    this.clearResetTimer();
    const transcript = this.conversation
      ? this.conversation.formatReverse(UPGRADE_CHAR_LIMIT - 100)
      : 'Ready.';

    // Detect task status from latest system message
    const statusLabel = this._resolveIdleStatus();

    if (transcript.length > UPGRADE_CHAR_LIMIT - 100) {
      await this.rebuildTranscript(statusLabel, 'Tap to interact');
    } else {
      await this.replaceTranscript(transcript);
      await this.updateStatus(statusLabel);
      await this.updateFooter('Tap to interact');
    }
  }

  /** Check the latest system message for a [TASK:*] marker and return an appropriate status label. */
  private _resolveIdleStatus(): string {
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

  async showRecording(): Promise<void> {
    this.clearResetTimer();
    await this.updateStatus('Recording');
    await this.updateFooter('Tap to stop');
  }

  async showTranscribing(): Promise<void> {
    this.clearResetTimer();
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
    await this.updateStatus('Thinking');
    await this.updateFooter('Waiting for response...');
  }

  async showStreaming(): Promise<void> {
    this.clearResetTimer();
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
    await this.flushRemainingDeltas();
    const transcript = this.conversation ? this.conversation.formatReverse(UPGRADE_CHAR_LIMIT - 100) : '';
    await this.replaceTranscript(transcript);
    await this.updateStatus(this._resolveIdleStatus());
    await this.updateFooter('Tap to interact');
  }

  async showError(message: string, hint = 'Tap to continue'): Promise<void> {
    this.clearDeltaTimer();
    this.clearResetTimer();
    if (this._mode === 'menu') {
      await this.exitMenuMode();
    }
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
    if (this._mode === 'menu') {
      await this.exitMenuMode();
    }
    await this.updateStatus('Offline');
    await this.updateFooter('Reconnecting...');
  }

  async showLoading(): Promise<void> {
    await this.updateStatus('Loading');
    await this.updateFooter('Initialising speech model');
  }

  async showSessionReset(label: string): Promise<void> {
    this.clearDeltaTimer();
    this.clearResetTimer();
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
  // Session menu (list mode)
  // -----------------------------------------------------------------------

  /**
   * Build the list item names for the session menu.
   * First item is always "✦ New Session", followed by up to 19 sessions.
   */
  private _buildSessionListItems(sessions: SessionListEntry[]): string[] {
    if (sessions.length === 0) {
      return ['Loading sessions...'];
    }
    const items: string[] = ['✦ New Session'];
    for (const s of sessions.slice(0, 19)) {
      const prefix = s.isActive ? '● ' : '  ';
      const label = s.label.slice(0, 60);
      items.push(`${prefix}${label}`);
    }
    return items;
  }

  /**
   * Rebuild the display in menu (list) mode.
   * Replaces the transcript text container with a ListContainer.
   */
  async showSessionMenu(sessions: SessionListEntry[]): Promise<void> {
    this.clearDeltaTimer();
    this.clearResetTimer();
    this._mode = 'menu';

    const b = this.requireBridge();
    const items = this._buildSessionListItems(sessions);

    const statusText = 'OpenClaw  ● Sessions';
    const footerText = 'Tap to select · 2×tap back';

    await b.rebuildPageContainer(
      new RebuildPageContainer({
        containerTotalNum: 3,
        textObject: [
          text(ID_STATUS, 'status', 8, 2, 560, 24, statusText),
          text(ID_FOOTER, 'footer', 8, 256, 560, 26, footerText),
        ],
        listObject: [
          new ListContainerProperty({
            xPosition: 8,
            yPosition: 34,
            width: 560,
            height: 212,
            borderWidth: 0,
            borderColor: 0,
            borderRadius: 0,
            paddingLength: 0,
            containerID: ID_MENU_LIST,
            containerName: 'menu-list',
            isEventCapture: 1,
            itemContainer: new ListItemContainerProperty({
              itemCount: items.length,
              itemWidth: 560,
              isItemSelectBorderEn: 1,
              itemName: items,
            }),
          }),
        ],
      }),
    );

    this._statusLen = statusText.length;
    this._footerLen = footerText.length;
  }

  /**
   * Return to transcript mode from menu mode.
   * Triggers a full rebuild to swap list → text containers.
   */
  async exitMenuMode(): Promise<void> {
    if (this._mode !== 'menu') return;
    this._mode = 'transcript';

    const b = this.requireBridge();
    const statusText = 'OpenClaw  ● Idle';
    const transcriptText = this.conversation
      ? this.conversation.formatReverse(REBUILD_CHAR_LIMIT - 50)
      : 'Ready.';
    const footerText = 'Tap to interact';

    await b.rebuildPageContainer(
      new RebuildPageContainer({
        containerTotalNum: 3,
        textObject: [
          text(ID_STATUS,     'status',     8,   2, 560,  24, statusText),
          text(ID_TRANSCRIPT, 'transcript', 8,  34, 560, 212, transcriptText, 1),
          text(ID_FOOTER,     'footer',     8, 256, 560,  26, footerText),
        ],
      }),
    );

    this._statusLen = statusText.length;
    this._transcriptLen = transcriptText.length;
    this._footerLen = footerText.length;
  }

  /** Get the current display mode. */
  get mode(): 'transcript' | 'menu' {
    return this._mode;
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
