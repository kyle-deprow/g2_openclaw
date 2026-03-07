// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { InputHandler } from '../input';
import { OsEventTypeList } from '@evenrealities/even_hub_sdk';
import type { EvenAppBridge } from '@evenrealities/even_hub_sdk';
import type { DisplayManager } from '../display';
import type { Gateway } from '../gateway';
import type { StateMachine } from '../state';

const CLICK        = OsEventTypeList.CLICK_EVENT;
const DOUBLE_CLICK = OsEventTypeList.DOUBLE_CLICK_EVENT;
const SCROLL_TOP   = OsEventTypeList.SCROLL_TOP_EVENT;
const SCROLL_BOTTOM = OsEventTypeList.SCROLL_BOTTOM_EVENT;
const FG_ENTER     = OsEventTypeList.FOREGROUND_ENTER_EVENT;
const FG_EXIT      = OsEventTypeList.FOREGROUND_EXIT_EVENT;

function createMockSm(initial = 'idle') {
  const obj = {
    _current: initial,
    get current() { return this._current; },
    transition: vi.fn(function (this: typeof obj, s: string) {
      obj._current = s;
      return true;
    }),
    onChange: vi.fn(),
    reset: vi.fn(),
  };
  return obj;
}

function createMockDisplay() {
  return {
    showIdle: vi.fn(),
    showRecording: vi.fn(),
    showTranscribing: vi.fn(),
    showConfirming: vi.fn(),
    showDetailPage: vi.fn(),
    _streamBuffer: 'test response',
    get streamBuffer() { return this._streamBuffer; },
  };
}

function createMockGateway() {
  return {
    connect: vi.fn(),
    sendJson: vi.fn(),
    _isConnected: true,
    get isConnected() { return this._isConnected; },
  };
}

function createMockBridge() {
  return {
    onEvenHubEvent: vi.fn(),
    shutDownContaniner: vi.fn(),
  };
}

describe('InputHandler', () => {
  let handler: InputHandler;
  let sm: ReturnType<typeof createMockSm>;
  let display: ReturnType<typeof createMockDisplay>;
  let gateway: ReturnType<typeof createMockGateway>;
  let bridge: ReturnType<typeof createMockBridge>;

  beforeEach(() => {
    handler = new InputHandler();
    sm = createMockSm('idle');
    display = createMockDisplay();
    gateway = createMockGateway();
    bridge = createMockBridge();

    handler.init({
      sm: sm as unknown as StateMachine,
      display: display as unknown as DisplayManager,
      gateway: gateway as unknown as Gateway,
      bridge: bridge as unknown as EvenAppBridge,
    });
  });

  // ---------------------------------------------------------------------------
  // Recording flow
  // ---------------------------------------------------------------------------

  describe('recording flow', () => {
    it('tap in idle sends start_audio', () => {
      sm._current = 'idle';
      handler._handleEvent(CLICK);
      expect(gateway.sendJson).toHaveBeenCalledWith({
        type: 'start_audio',
        sampleRate: 16000,
        channels: 1,
        sampleWidth: 2,
      });
    });

    it('tap in recording sends stop_audio', () => {
      sm._current = 'recording';
      handler._handleEvent(CLICK);
      expect(gateway.sendJson).toHaveBeenCalledWith({ type: 'stop_audio' });
    });

    it('tap in recording sends stop_audio with hilText from DOM', () => {
      sm._current = 'recording';
      const input = document.createElement('input');
      input.id = 'hil-text';
      input.value = 'hello world';
      document.body.appendChild(input);

      handler._handleEvent(CLICK);
      expect(gateway.sendJson).toHaveBeenCalledWith({
        type: 'stop_audio',
        hilText: 'hello world',
      });

      input.remove();
    });

    it('startRecording returns false when not idle', () => {
      sm._current = 'recording';
      expect(handler.startRecording()).toBe(false);
    });

    it('stopRecording returns false when not recording', () => {
      sm._current = 'idle';
      expect(handler.stopRecording()).toBe(false);
    });
  });

  // ---------------------------------------------------------------------------
  // Confirming flow
  // ---------------------------------------------------------------------------

  describe('confirming flow', () => {
    it('setPendingTranscription stores text', () => {
      handler.setPendingTranscription('hello');
      expect(handler.pendingTranscription).toBe('hello');
    });

    it('tap in confirming sends text frame with pending transcription', () => {
      sm._current = 'confirming';
      handler.setPendingTranscription('confirmed text');
      handler._handleEvent(CLICK);
      expect(gateway.sendJson).toHaveBeenCalledWith({
        type: 'text',
        message: 'confirmed text',
      });
      expect(handler.pendingTranscription).toBeNull();
    });

    it('double-tap in confirming rejects and returns to idle', () => {
      sm._current = 'confirming';
      handler.setPendingTranscription('rejected text');
      handler._handleEvent(DOUBLE_CLICK);
      expect(sm.transition).toHaveBeenCalledWith('idle');
      expect(display.showIdle).toHaveBeenCalled();
      expect(handler.pendingTranscription).toBeNull();
    });

    it('confirmTranscription returns false without pending text', () => {
      sm._current = 'confirming';
      expect(handler.confirmTranscription()).toBe(false);
    });

    it('confirmTranscription returns false when not confirming', () => {
      sm._current = 'idle';
      handler.setPendingTranscription('text');
      expect(handler.confirmTranscription()).toBe(false);
    });

    it('rejectTranscription returns false when not confirming', () => {
      sm._current = 'idle';
      expect(handler.rejectTranscription()).toBe(false);
    });
  });

  // ---------------------------------------------------------------------------
  // sendText
  // ---------------------------------------------------------------------------

  describe('sendText', () => {
    it('sends text frame when idle', () => {
      sm._current = 'idle';
      expect(handler.sendText('hello')).toBe(true);
      expect(gateway.sendJson).toHaveBeenCalledWith({ type: 'text', message: 'hello' });
    });

    it('sends text frame when confirming', () => {
      sm._current = 'confirming';
      expect(handler.sendText('hello')).toBe(true);
      expect(gateway.sendJson).toHaveBeenCalledWith({ type: 'text', message: 'hello' });
    });

    it('returns false for empty string', () => {
      sm._current = 'idle';
      expect(handler.sendText('')).toBe(false);
    });

    it('returns false when thinking', () => {
      sm._current = 'thinking';
      expect(handler.sendText('hello')).toBe(false);
    });

    it('trims whitespace', () => {
      sm._current = 'idle';
      handler.sendText('  trimmed  ');
      expect(gateway.sendJson).toHaveBeenCalledWith({ type: 'text', message: 'trimmed' });
    });
  });

  // ---------------------------------------------------------------------------
  // Other tap states
  // ---------------------------------------------------------------------------

  it('tap in error dismisses', () => {
    sm._current = 'error';
    handler._handleEvent(CLICK);
    expect(sm.transition).toHaveBeenCalledWith('idle');
    expect(display.showIdle).toHaveBeenCalled();
  });

  it('tap in disconnected reconnects', () => {
    sm._current = 'disconnected';
    handler._handleEvent(CLICK);
    expect(gateway.connect).toHaveBeenCalled();
  });

  it('tap ignored in thinking', () => {
    sm._current = 'thinking';
    handler._handleEvent(CLICK);
    expect(gateway.sendJson).not.toHaveBeenCalled();
  });

  it('tap ignored in streaming', () => {
    sm._current = 'streaming';
    handler._handleEvent(CLICK);
    expect(gateway.sendJson).not.toHaveBeenCalled();
  });

  // ---------------------------------------------------------------------------
  // SDK quirks
  // ---------------------------------------------------------------------------

  it('undefined event treated as click', () => {
    sm._current = 'idle';
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    handler._handleEvent(undefined);
    expect(gateway.sendJson).toHaveBeenCalledWith({
      type: 'start_audio',
      sampleRate: 16000,
      channels: 1,
      sampleWidth: 2,
    });
    warnSpy.mockRestore();
  });

  // ---------------------------------------------------------------------------
  // Double-tap (non-confirming)
  // ---------------------------------------------------------------------------

  it('double tap in idle is a no-op', () => {
    sm._current = 'idle';
    handler._handleEvent(DOUBLE_CLICK);
    expect(display.showIdle).not.toHaveBeenCalled();
  });

  // ---------------------------------------------------------------------------
  // Cancel response (double-tap in thinking/streaming)
  // ---------------------------------------------------------------------------

  describe('cancelResponse', () => {
    it('double-tap in thinking cancels and returns to idle', () => {
      sm._current = 'thinking';
      handler._handleEvent(DOUBLE_CLICK);
      expect(sm.transition).toHaveBeenCalledWith('idle');
      expect(display.showIdle).toHaveBeenCalled();
    });

    it('double-tap in streaming cancels and returns to idle', () => {
      sm._current = 'streaming';
      handler._handleEvent(DOUBLE_CLICK);
      expect(sm.transition).toHaveBeenCalledWith('idle');
      expect(display.showIdle).toHaveBeenCalled();
    });

    it('cancelResponse returns false when not in thinking/streaming', () => {
      sm._current = 'idle';
      expect(handler.cancelResponse()).toBe(false);
    });

    it('cancelResponse returns true when thinking', () => {
      sm._current = 'thinking';
      expect(handler.cancelResponse()).toBe(true);
      expect(sm.transition).toHaveBeenCalledWith('idle');
      expect(display.showIdle).toHaveBeenCalled();
    });

    it('cancelResponse returns true when streaming', () => {
      sm._current = 'streaming';
      expect(handler.cancelResponse()).toBe(true);
      expect(sm.transition).toHaveBeenCalledWith('idle');
      expect(display.showIdle).toHaveBeenCalled();
    });
  });

  // ---------------------------------------------------------------------------
  // Lifecycle events
  // ---------------------------------------------------------------------------

  it('foreground exit is a no-op', () => {
    sm._current = 'idle';
    handler._handleEvent(FG_EXIT);
    expect(sm.transition).not.toHaveBeenCalled();
  });

  it('foreground enter reconnects if disconnected', () => {
    gateway._isConnected = false;
    handler._handleEvent(FG_ENTER);
    expect(gateway.connect).toHaveBeenCalled();
  });

  // ---------------------------------------------------------------------------
  // Scroll throttling
  // ---------------------------------------------------------------------------

  it('scroll throttled within 300ms', () => {
    vi.useFakeTimers();
    handler._handleEvent(SCROLL_TOP);
    const firstTime = (handler as any)._lastScrollTime;
    expect(firstTime).toBeGreaterThan(0);

    vi.advanceTimersByTime(100);
    handler._handleEvent(SCROLL_BOTTOM);
    expect((handler as any)._lastScrollTime).toBe(firstTime);

    vi.advanceTimersByTime(200);
    handler._handleEvent(SCROLL_TOP);
    expect((handler as any)._lastScrollTime).toBeGreaterThan(firstTime);

    vi.useRealTimers();
  });

  // ---------------------------------------------------------------------------
  // Double-init guard
  // ---------------------------------------------------------------------------

  it('double init is ignored with warning', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    handler.init({
      sm: sm as unknown as StateMachine,
      display: display as unknown as DisplayManager,
      gateway: gateway as unknown as Gateway,
      bridge: bridge as unknown as EvenAppBridge,
    });
    expect(warnSpy).toHaveBeenCalledWith('[Input] Already initialised — ignoring duplicate init()');
    expect(bridge.onEvenHubEvent).toHaveBeenCalledTimes(1);
    warnSpy.mockRestore();
  });

  // ---------------------------------------------------------------------------
  // Bare event and audio event
  // ---------------------------------------------------------------------------

  it('bare event treated as click', () => {
    sm._current = 'idle';
    const eventCallback = bridge.onEvenHubEvent.mock.calls[0][0];
    eventCallback({});
    expect(gateway.sendJson).toHaveBeenCalledWith({
      type: 'start_audio',
      sampleRate: 16000,
      channels: 1,
      sampleWidth: 2,
    });
  });

  it('audio event is ignored', () => {
    sm._current = 'idle';
    const eventCallback = bridge.onEvenHubEvent.mock.calls[0][0];
    eventCallback({ audioEvent: { audioPcm: new Uint8Array(3200) } });
    expect(gateway.sendJson).not.toHaveBeenCalled();
  });
});
