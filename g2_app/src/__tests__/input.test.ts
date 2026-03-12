// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { InputHandler } from '../input';
import { OsEventTypeList } from '@evenrealities/even_hub_sdk';
import type { EvenAppBridge } from '@evenrealities/even_hub_sdk';
import type { DisplayManager } from '../display';
import type { Gateway } from '../gateway';
import type { ConversationHistory } from '../conversation';
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
    showIdle: vi.fn().mockResolvedValue(undefined),
    showRecording: vi.fn(),
    showTranscribing: vi.fn(),
    showConfirming: vi.fn(),
    showDetailPage: vi.fn(),
    showSessionMenu: vi.fn().mockResolvedValue(undefined),
    exitMenuMode: vi.fn().mockResolvedValue(undefined),
    _streamBuffer: 'test response',
    get streamBuffer() { return this._streamBuffer; },
  };
}

function createMockGateway() {
  return {
    connect: vi.fn(),
    sendJson: vi.fn(),
    requestSessionList: vi.fn(),
    switchSession: vi.fn(),
    createNewSession: vi.fn(),
    _isConnected: true,
    get isConnected() { return this._isConnected; },
  };
}

function createMockConversation() {
  return {
    removeLastUser: vi.fn().mockReturnValue(true),
    addUser: vi.fn(),
    addAssistant: vi.fn(),
    addSystem: vi.fn(),
    clear: vi.fn(),
    format: vi.fn().mockReturnValue('Ready.'),
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
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let handlerAny: any;
  let sm: ReturnType<typeof createMockSm>;
  let display: ReturnType<typeof createMockDisplay>;
  let gateway: ReturnType<typeof createMockGateway>;
  let bridge: ReturnType<typeof createMockBridge>;
  let conversation: ReturnType<typeof createMockConversation>;

  beforeEach(() => {
    handler = new InputHandler();
    handlerAny = handler as any;
    sm = createMockSm('idle');
    display = createMockDisplay();
    gateway = createMockGateway();
    bridge = createMockBridge();
    conversation = createMockConversation();

    handler.init({
      sm: sm as unknown as StateMachine,
      display: display as unknown as DisplayManager,
      gateway: gateway as unknown as Gateway,
      bridge: bridge as unknown as EvenAppBridge,
      conversation: conversation as unknown as ConversationHistory,
    });
  });

  // ---------------------------------------------------------------------------
  // Recording flow
  // ---------------------------------------------------------------------------

  describe('recording flow', () => {
    it('tap in idle sends start_audio', () => {
      sm._current = 'idle';
      handlerAny._handleEvent(CLICK);
      expect(gateway.sendJson).toHaveBeenCalledWith({
        type: 'start_audio',
        sampleRate: 16000,
        channels: 1,
        sampleWidth: 2,
      });
    });

    it('tap in recording sends stop_audio', () => {
      sm._current = 'recording';
      handlerAny._handleEvent(CLICK);
      expect(gateway.sendJson).toHaveBeenCalledWith({ type: 'stop_audio' });
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
      handlerAny._handleEvent(CLICK);
      expect(gateway.sendJson).toHaveBeenCalledWith({
        type: 'text',
        message: 'confirmed text',
      });
      expect(handler.pendingTranscription).toBeNull();
    });

    it('double-tap in confirming rejects and returns to idle', () => {
      sm._current = 'confirming';
      handler.setPendingTranscription('rejected text');
      handlerAny._handleEvent(DOUBLE_CLICK);
      expect(conversation.removeLastUser).toHaveBeenCalled();
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
  // ttsRecord
  // ---------------------------------------------------------------------------

  describe('ttsRecord', () => {
    it('sends start_audio + stop_audio with hilText when idle', () => {
      sm._current = 'idle';
      expect(handler.ttsRecord('hello world')).toBe(true);
      expect(gateway.sendJson).toHaveBeenCalledTimes(2);
      expect(gateway.sendJson).toHaveBeenNthCalledWith(1, {
        type: 'start_audio',
        sampleRate: 16000,
        channels: 1,
        sampleWidth: 2,
      });
      expect(gateway.sendJson).toHaveBeenNthCalledWith(2, {
        type: 'stop_audio',
        hilText: 'hello world',
      });
    });

    it('returns false for empty string', () => {
      sm._current = 'idle';
      expect(handler.ttsRecord('')).toBe(false);
      expect(gateway.sendJson).not.toHaveBeenCalled();
    });

    it('returns false for whitespace-only string', () => {
      sm._current = 'idle';
      expect(handler.ttsRecord('   ')).toBe(false);
      expect(gateway.sendJson).not.toHaveBeenCalled();
    });

    it('returns false when not idle', () => {
      sm._current = 'recording';
      expect(handler.ttsRecord('hello')).toBe(false);
      expect(gateway.sendJson).not.toHaveBeenCalled();
    });

    it('trims whitespace from text', () => {
      sm._current = 'idle';
      handler.ttsRecord('  trimmed  ');
      expect(gateway.sendJson).toHaveBeenNthCalledWith(2, {
        type: 'stop_audio',
        hilText: 'trimmed',
      });
    });
  });

  // ---------------------------------------------------------------------------
  // Other tap states
  // ---------------------------------------------------------------------------

  it('tap in error dismisses', () => {
    sm._current = 'error';
    handlerAny._handleEvent(CLICK);
    expect(sm.transition).toHaveBeenCalledWith('idle');
    expect(display.showIdle).toHaveBeenCalled();
  });

  it('tap in disconnected reconnects', () => {
    sm._current = 'disconnected';
    handlerAny._handleEvent(CLICK);
    expect(gateway.connect).toHaveBeenCalled();
  });

  it('tap ignored in thinking', () => {
    sm._current = 'thinking';
    handlerAny._handleEvent(CLICK);
    expect(gateway.sendJson).not.toHaveBeenCalled();
  });

  it('tap ignored in streaming', () => {
    sm._current = 'streaming';
    handlerAny._handleEvent(CLICK);
    expect(gateway.sendJson).not.toHaveBeenCalled();
  });

  // ---------------------------------------------------------------------------
  // SDK quirks
  // ---------------------------------------------------------------------------

  it('undefined event treated as click', () => {
    sm._current = 'idle';
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    handlerAny._handleEvent(undefined);
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

  it('double tap in idle opens session menu', () => {
    sm._current = 'idle';
    handlerAny._handleEvent(DOUBLE_CLICK);
    expect(sm.transition).toHaveBeenCalledWith('menu');
    expect(gateway.requestSessionList).toHaveBeenCalled();
    expect(display.showSessionMenu).toHaveBeenCalled();
  });

  // ---------------------------------------------------------------------------
  // Cancel response (double-tap in thinking/streaming)
  // ---------------------------------------------------------------------------

  describe('cancelResponse', () => {
    it('double-tap in thinking cancels and returns to idle', () => {
      sm._current = 'thinking';
      handlerAny._handleEvent(DOUBLE_CLICK);
      expect(sm.transition).toHaveBeenCalledWith('idle');
      expect(display.showIdle).toHaveBeenCalled();
    });

    it('double-tap in streaming cancels and returns to idle', () => {
      sm._current = 'streaming';
      handlerAny._handleEvent(DOUBLE_CLICK);
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
  // Reset session (double-tap in idle)
  // ---------------------------------------------------------------------------

  describe('resetSession', () => {
    it('resetSession sends reset_session when idle', () => {
      sm._current = 'idle';
      expect(handler.resetSession()).toBe(true);
      expect(gateway.sendJson).toHaveBeenCalledWith({ type: 'reset_session' });
    });

    it('resetSession returns false when not idle', () => {
      sm._current = 'thinking';
      expect(handler.resetSession()).toBe(false);
      expect(gateway.sendJson).not.toHaveBeenCalled();
    });

    it('resetSession returns false when recording', () => {
      sm._current = 'recording';
      expect(handler.resetSession()).toBe(false);
    });
  });

  // ---------------------------------------------------------------------------
  // Lifecycle events
  // ---------------------------------------------------------------------------

  it('foreground exit is a no-op', () => {
    sm._current = 'idle';
    handlerAny._handleEvent(FG_EXIT);
    expect(sm.transition).not.toHaveBeenCalled();
  });

  it('foreground enter reconnects if disconnected', () => {
    gateway._isConnected = false;
    handlerAny._handleEvent(FG_ENTER);
    expect(gateway.connect).toHaveBeenCalled();
  });

  // ---------------------------------------------------------------------------
  // Scroll throttling
  // ---------------------------------------------------------------------------

  it('scroll throttled within 300ms', () => {
    vi.useFakeTimers();
    handlerAny._handleEvent(SCROLL_TOP);
    const firstTime = (handler as any)._lastScrollTime;
    expect(firstTime).toBeGreaterThan(0);

    vi.advanceTimersByTime(100);
    handlerAny._handleEvent(SCROLL_BOTTOM);
    expect((handler as any)._lastScrollTime).toBe(firstTime);

    vi.advanceTimersByTime(200);
    handlerAny._handleEvent(SCROLL_TOP);
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
      conversation: conversation as unknown as ConversationHistory,
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

  // ---------------------------------------------------------------------------
  // Reject transcription marks conversation
  // ---------------------------------------------------------------------------

  describe('rejectTranscription marks conversation', () => {
    it('rejectTranscription calls removeLastUser', () => {
      sm._current = 'confirming';
      handler.setPendingTranscription('some text');
      expect(handler.rejectTranscription()).toBe(true);
      expect(conversation.removeLastUser).toHaveBeenCalled();
      expect(sm.transition).toHaveBeenCalledWith('idle');
      expect(display.showIdle).toHaveBeenCalled();
    });

    it('double-tap in confirming removes entry from conversation', () => {
      sm._current = 'confirming';
      handler.setPendingTranscription('will be rejected');
      handlerAny._handleEvent(DOUBLE_CLICK);
      expect(conversation.removeLastUser).toHaveBeenCalled();
      expect(handler.pendingTranscription).toBeNull();
      expect(display.showIdle).toHaveBeenCalled();
    });
  });

  // ---------------------------------------------------------------------------
  // Session menu interactions
  // ---------------------------------------------------------------------------

  describe('session menu', () => {
    it('double-tap in idle opens session menu', () => {
      sm._current = 'idle';
      expect(handler.openSessionMenu()).toBe(true);
      expect(sm.transition).toHaveBeenCalledWith('menu');
      expect(gateway.requestSessionList).toHaveBeenCalled();
      expect(display.showSessionMenu).toHaveBeenCalled();
    });

    it('openSessionMenu returns false when not idle', () => {
      sm._current = 'recording';
      expect(handler.openSessionMenu()).toBe(false);
    });

    it('double-tap in menu closes menu', () => {
      sm._current = 'menu';
      handlerAny._handleEvent(DOUBLE_CLICK);
      expect(sm.transition).toHaveBeenCalledWith('idle');
      expect(display.exitMenuMode).toHaveBeenCalled();
    });

    it('closeSessionMenu returns false when not in menu', () => {
      sm._current = 'idle';
      expect(handler.closeSessionMenu()).toBe(false);
    });

    it('tap in menu on "New Session" (index 0) creates new session', () => {
      sm._current = 'menu';
      handler.setSessionList([
        { sessionKey: 'k1', sessionId: 'id1', label: 'Test', updatedAt: '2026-03-07T10:00:00Z', isActive: false, preview: 'Test', messageCount: 0 },
      ]);
      handlerAny._handleEvent(CLICK, {
        listEvent: { eventType: CLICK, currentSelectItemIndex: 0, currentSelectItemName: '✦ New Session' },
      });
      expect(gateway.createNewSession).toHaveBeenCalled();
    });

    it('tap in menu selects a session', () => {
      sm._current = 'menu';
      handler.setSessionList([
        { sessionKey: 'k1', sessionId: 'id1', label: 'Session 1', updatedAt: '2026-03-07T10:00:00Z', isActive: false, preview: 'Session 1', messageCount: 0 },
        { sessionKey: 'k2', sessionId: 'id2', label: 'Session 2', updatedAt: '2026-03-07T09:00:00Z', isActive: false, preview: 'Session 2', messageCount: 0 },
      ]);
      handlerAny._handleEvent(CLICK, {
        listEvent: { eventType: CLICK, currentSelectItemIndex: 2, currentSelectItemName: '  Session 2' },
      });
      expect(gateway.switchSession).toHaveBeenCalledWith('k2');
    });

    it('tap on active session closes menu instead of switching', () => {
      sm._current = 'menu';
      handler.setSessionList([
        { sessionKey: 'k1', sessionId: 'id1', label: 'Active', updatedAt: '2026-03-07T10:00:00Z', isActive: true, preview: 'Active', messageCount: 0 },
      ]);
      handlerAny._handleEvent(CLICK, {
        listEvent: { eventType: CLICK, currentSelectItemIndex: 1, currentSelectItemName: '● Active' },
      });
      expect(gateway.switchSession).not.toHaveBeenCalled();
      expect(sm.transition).toHaveBeenCalledWith('idle');
    });

    it('menu tap with undefined index (Quirk 2) falls back to trackedMenuIndex', () => {
      sm._current = 'menu';
      handler.setSessionList([
        { sessionKey: 'k1', sessionId: 'id1', label: 'Session', updatedAt: '2026-03-07T10:00:00Z', isActive: false, preview: 'Session', messageCount: 0 },
      ]);
      // trackedMenuIndex defaults to 0, so this should trigger "New Session"
      handlerAny._handleEvent(undefined, {
        listEvent: { eventType: undefined },
      });
      expect(gateway.createNewSession).toHaveBeenCalled();
    });

    it('setSessionList stores sessions', () => {
      const sessions = [
        { sessionKey: 'k1', sessionId: 'id1', label: 'Test', updatedAt: '2026-03-07T10:00:00Z', isActive: false, preview: 'Test', messageCount: 0 },
      ];
      handler.setSessionList(sessions);
      // Verify by triggering a menu tap that uses the stored list
      sm._current = 'menu';
      handlerAny._handleEvent(CLICK, {
        listEvent: { eventType: CLICK, currentSelectItemIndex: 1, currentSelectItemName: '  Test' },
      });
      expect(gateway.switchSession).toHaveBeenCalledWith('k1');
    });

    it('double-tap in confirming still rejects', () => {
      sm._current = 'confirming';
      handler.setPendingTranscription('text');
      handlerAny._handleEvent(DOUBLE_CLICK);
      expect(conversation.removeLastUser).toHaveBeenCalled();
      expect(sm.transition).toHaveBeenCalledWith('idle');
      expect(handler.pendingTranscription).toBeNull();
    });

    it('double-tap in streaming still cancels', () => {
      sm._current = 'streaming';
      handlerAny._handleEvent(DOUBLE_CLICK);
      expect(sm.transition).toHaveBeenCalledWith('idle');
      expect(display.showIdle).toHaveBeenCalled();
    });

    it('tap in menu with no session list is graceful no-op (loading guard)', () => {
      sm._current = 'menu';
      // Don't set session list — _sessionList is null
      const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
      handlerAny._handleEvent(CLICK, {
        listEvent: { eventType: CLICK, currentSelectItemIndex: 3 },
      });
      // Should not crash, and should not call any gateway method
      expect(gateway.switchSession).not.toHaveBeenCalled();
      expect(gateway.createNewSession).not.toHaveBeenCalled();
      expect(logSpy).toHaveBeenCalledWith('[Input] Menu tap ignored — session list not yet loaded');
      logSpy.mockRestore();
    });
  });
});
