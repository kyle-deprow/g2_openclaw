import type { EvenAppBridge } from '@evenrealities/even_hub_sdk';
import { OsEventTypeList } from '@evenrealities/even_hub_sdk';
import type { AudioCapture } from './audio';
import type { DisplayManager } from './display';
import type { Gateway } from './gateway';
import type { StateMachine } from './state';

/**
 * InputHandler — routes R1 ring and temple gesture events to app actions.
 *
 * Tap-to-toggle model:
 *   idle → start recording
 *   recording → stop recording
 *   displaying → dismiss (return to idle)
 *   error → dismiss
 *   disconnected → force reconnect
 *   loading/transcribing/thinking/streaming → ignored
 */
export class InputHandler {
  private sm!: StateMachine;
  private display!: DisplayManager;
  private audio!: AudioCapture;
  private gateway!: Gateway;
  private bridge!: EvenAppBridge;
  private _lastScrollTime = 0;
  private static readonly SCROLL_COOLDOWN = 300; // ms

  init(deps: {
    sm: StateMachine;
    display: DisplayManager;
    audio: AudioCapture;
    gateway: Gateway;
    bridge: EvenAppBridge;
  }): void {
    this.sm = deps.sm;
    this.display = deps.display;
    this.audio = deps.audio;
    this.gateway = deps.gateway;
    this.bridge = deps.bridge;

    deps.bridge.onEvenHubEvent((event) => {
      // Check all three event sources
      const eventType =
        event.textEvent?.eventType ??
        event.listEvent?.eventType ??
        event.sysEvent?.eventType;

      this._handleEvent(eventType);
    });
  }

  _handleEvent(eventType: number | undefined): void {
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
      // App going to background — stop mic if recording
      if (this.audio.isRecording) {
        this.audio.stop();
        this.sm.transition('idle');
        this.display.showIdle();
      }
      return;
    }

    if (eventType === OsEventTypeList.FOREGROUND_ENTER_EVENT) {
      // App returning to foreground — reconnect if needed
      if (!this.gateway.isConnected) {
        this.gateway.connect();
      }
      return;
    }

    if (eventType === OsEventTypeList.ABNORMAL_EXIT_EVENT) {
      // Cleanup — use stored bridge reference (SDK typo is intentional)
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
      // TODO: Phase 4 — implement scroll in displaying state
      return;
    }
  }

  private _handleTap(): void {
    const state = this.sm.current;
    switch (state) {
      case 'idle':
        this.audio.start();
        this.sm.transition('recording');
        this.display.showRecording();
        break;
      case 'recording':
        this.audio.stop();
        // Server will send status:transcribing
        break;
      case 'displaying':
        this.sm.transition('idle');
        this.display.showIdle();
        break;
      case 'error':
        this.sm.transition('idle');
        this.display.showIdle();
        break;
      case 'disconnected':
        this.gateway.connect();
        break;
      default:
        // loading, transcribing, thinking, streaming — ignored
        break;
    }
  }

  private _handleDoubleTap(): void {
    if (this.sm.current === 'displaying') {
      // Toggle detail page via rebuildPageContainer (no setPageFlip)
      this.display.showDetailPage(this.display.streamBuffer, {
        model: 'mock',
        elapsed: 0,
        session: 'local',
      });
    }
  }
}
