import type { EvenAppBridge } from '@evenrealities/even_hub_sdk';
import { OsEventTypeList } from '@evenrealities/even_hub_sdk';
import type { DisplayManager } from './display';
import type { Gateway } from './gateway';
import type { StateMachine } from './state';

/**
 * InputHandler — routes R1 ring and temple gesture events to app actions.
 *
 * HIL recording flow:
 *   idle → tap → start recording (send start_audio)
 *   recording → tap → stop recording (send stop_audio with hilText)
 *   confirming → tap → confirm (send text frame)
 *   confirming → double-tap → reject (back to idle)
 */
export class InputHandler {
  private sm!: StateMachine;
  private display!: DisplayManager;
  private gateway!: Gateway;
  private bridge!: EvenAppBridge;
  private _lastScrollTime = 0;
  private _initialised = false;
  private _pendingTranscription: string | null = null;
  private static readonly SCROLL_COOLDOWN = 300; // ms

  init(deps: {
    sm: StateMachine;
    display: DisplayManager;
    gateway: Gateway;
    bridge: EvenAppBridge;
  }): void {
    if (this._initialised) {
      console.warn('[Input] Already initialised — ignoring duplicate init()');
      return;
    }
    this._initialised = true;
    this.sm = deps.sm;
    this.display = deps.display;
    this.gateway = deps.gateway;
    this.bridge = deps.bridge;

    deps.bridge.onEvenHubEvent((event) => {
      console.log('[Input] Event received:', JSON.stringify(event));

      // Audio events are ignored in HIL mode.
      if (event.audioEvent) {
        return;
      }

      const hasEvent = event.textEvent || event.listEvent || event.sysEvent;
      if (!hasEvent) {
        console.log('[Input] Bare event (no sub-object) — treating as click');
        this._handleEvent(undefined);
        return;
      }

      const eventType =
        event.textEvent?.eventType ??
        event.listEvent?.eventType ??
        event.sysEvent?.eventType;

      this._handleEvent(eventType);
    });
  }

  /** Store transcription text for the confirming state. */
  setPendingTranscription(text: string): void {
    this._pendingTranscription = text;
  }

  /** Get the current pending transcription (if any). */
  get pendingTranscription(): string | null {
    return this._pendingTranscription;
  }

  /** Send a text message to the gateway. Does not touch the DOM. */
  sendText(message: string): boolean {
    const trimmed = message.trim();
    if (!trimmed) return false;
    if (this.sm.current !== 'idle' && this.sm.current !== 'confirming') {
      console.warn('[Input] Cannot send — state is', this.sm.current);
      return false;
    }
    this.gateway.sendJson({ type: 'text', message: trimmed });
    return true;
  }

  /** Start a recording session — sends start_audio to gateway. */
  startRecording(): boolean {
    if (this.sm.current !== 'idle') {
      console.warn('[Input] Cannot start recording — state is', this.sm.current);
      return false;
    }
    this.gateway.sendJson({
      type: 'start_audio',
      sampleRate: 16000,
      channels: 1,
      sampleWidth: 2,
    });
    return true;
  }

  /** Stop recording — sends stop_audio to gateway with optional hilText. */
  stopRecording(hilText?: string): boolean {
    if (this.sm.current !== 'recording') {
      console.warn('[Input] Cannot stop recording — state is', this.sm.current);
      return false;
    }
    const frame: { type: 'stop_audio'; hilText?: string } = { type: 'stop_audio' };
    if (hilText?.trim()) {
      frame.hilText = hilText.trim();
    }
    this.gateway.sendJson(frame);
    return true;
  }

  /** Confirm the pending transcription — sends text frame to gateway. */
  confirmTranscription(): boolean {
    if (this.sm.current !== 'confirming') {
      console.warn('[Input] Cannot confirm — state is', this.sm.current);
      return false;
    }
    if (!this._pendingTranscription) {
      console.warn('[Input] No pending transcription to confirm');
      return false;
    }
    this.gateway.sendJson({ type: 'text', message: this._pendingTranscription });
    this._pendingTranscription = null;
    return true;
  }

  /** Reject the pending transcription — return to idle. */
  rejectTranscription(): boolean {
    if (this.sm.current !== 'confirming') {
      console.warn('[Input] Cannot reject — state is', this.sm.current);
      return false;
    }
    this._pendingTranscription = null;
    this.sm.transition('idle');
    this.display.showIdle().catch(err => console.error('[Input] Display error:', err));
    return true;
  }

  private _handleEvent(eventType: number | undefined): void {
    // CLICK_EVENT = 0 bug: SDK deserializes 0 as undefined
    if (eventType === OsEventTypeList.CLICK_EVENT || eventType === undefined) {
      if (eventType === undefined) {
        console.warn('[Input] Received undefined eventType — treating as CLICK_EVENT (SDK bug)');
      }
      this._handleTap();
      return;
    }

    if (eventType === OsEventTypeList.DOUBLE_CLICK_EVENT) {
      this._handleDoubleTap();
      return;
    }

    if (eventType === OsEventTypeList.FOREGROUND_EXIT_EVENT) {
      return;
    }

    if (eventType === OsEventTypeList.FOREGROUND_ENTER_EVENT) {
      if (!this.gateway.isConnected) {
        this.gateway.connect();
      }
      return;
    }

    if (eventType === OsEventTypeList.ABNORMAL_EXIT_EVENT) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (this.bridge as any).shutDownContaniner(0);
      return;
    }

    // Scroll events with throttling
    if (
      eventType === OsEventTypeList.SCROLL_TOP_EVENT ||
      eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT
    ) {
      const now = Date.now();
      if (now - this._lastScrollTime < InputHandler.SCROLL_COOLDOWN) return;
      this._lastScrollTime = now;
      return;
    }
  }

  private _handleTap(): void {
    console.log('[Input] _handleTap() — current state:', this.sm.current);
    const state = this.sm.current;
    switch (state) {
      case 'idle':
        // Tap in idle starts recording
        this.startRecording();
        break;
      case 'recording':
        // Tap during recording stops it
        this.stopRecording(this._getHilText());
        break;
      case 'confirming':
        // Tap in confirming state confirms the transcription
        this.confirmTranscription();
        break;
      case 'error':
        this.sm.transition('idle');
        this.display.showIdle().catch(err => console.error('[Input] Display error:', err));
        break;
      case 'disconnected':
        this.gateway.connect();
        break;
      default:
        // loading, thinking, streaming, transcribing — ignored
        break;
    }
  }

  private _handleDoubleTap(): void {
    const state = this.sm.current;
    if (state === 'idle') {
      this.resetSession();
    } else if (state === 'confirming') {
      this.rejectTranscription();
    } else if (state === 'thinking' || state === 'streaming') {
      this.cancelResponse();
    }
  }

  /** Cancel an in-progress AI response — return to idle. */
  cancelResponse(): boolean {
    if (this.sm.current !== 'thinking' && this.sm.current !== 'streaming') {
      console.warn('[Input] Cannot cancel — state is', this.sm.current);
      return false;
    }
    console.log('[Input] Cancelling response');
    this.sm.transition('idle');
    this.display.showIdle().catch(err => console.error('[Input] Display error:', err));
    return true;
  }

  /** Request a session reset from the gateway. */
  resetSession(): boolean {
    if (this.sm.current !== 'idle') {
      console.warn('[Input] Cannot reset session — state is', this.sm.current);
      return false;
    }
    this.gateway.sendJson({ type: 'reset_session' });
    console.log('[Input] Session reset requested');
    return true;
  }

  /** Read the HIL text input value (if present in DOM). */
  private _getHilText(): string | undefined {
    const input = document.getElementById('hil-text') as HTMLInputElement | null;
    return input?.value?.trim() || undefined;
  }
}
