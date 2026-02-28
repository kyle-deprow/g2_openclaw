import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { InputHandler } from '../input';
import { OsEventTypeList } from '@evenrealities/even_hub_sdk';
import type { EvenAppBridge } from '@evenrealities/even_hub_sdk';
import type { AudioCapture } from '../audio';
import type { DisplayManager } from '../display';
import type { Gateway } from '../gateway';
import type { StateMachine } from '../state';

// ---------------------------------------------------------------------------
// Event type constants (matching SDK enum values, aliased for readability)
// ---------------------------------------------------------------------------
const CLICK        = OsEventTypeList.CLICK_EVENT;            // 0
const DOUBLE_CLICK = OsEventTypeList.DOUBLE_CLICK_EVENT;     // 3
const SCROLL_TOP   = OsEventTypeList.SCROLL_TOP_EVENT;       // 1
const SCROLL_BOTTOM = OsEventTypeList.SCROLL_BOTTOM_EVENT;   // 2
const FG_ENTER     = OsEventTypeList.FOREGROUND_ENTER_EVENT; // 4
const FG_EXIT      = OsEventTypeList.FOREGROUND_EXIT_EVENT;  // 5

// ---------------------------------------------------------------------------
// Mock factories
// ---------------------------------------------------------------------------

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
    showDetailPage: vi.fn(),
    _streamBuffer: 'test response',
    get streamBuffer() { return this._streamBuffer; },
  };
}

function createMockAudio() {
  return {
    start: vi.fn(),
    stop: vi.fn(),
    _isRecording: false,
    get isRecording() { return this._isRecording; },
  };
}

function createMockGateway() {
  return {
    connect: vi.fn(),
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

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('InputHandler', () => {
  let handler: InputHandler;
  let sm: ReturnType<typeof createMockSm>;
  let display: ReturnType<typeof createMockDisplay>;
  let audio: ReturnType<typeof createMockAudio>;
  let gateway: ReturnType<typeof createMockGateway>;
  let bridge: ReturnType<typeof createMockBridge>;

  beforeEach(() => {
    handler = new InputHandler();
    sm = createMockSm('idle');
    display = createMockDisplay();
    audio = createMockAudio();
    gateway = createMockGateway();
    bridge = createMockBridge();

    handler.init({
      sm: sm as unknown as StateMachine,
      display: display as unknown as DisplayManager,
      audio: audio as unknown as AudioCapture,
      gateway: gateway as unknown as Gateway,
      bridge: bridge as unknown as EvenAppBridge,
    });
  });

  // -----------------------------------------------------------------------
  // Tap behaviour per state
  // -----------------------------------------------------------------------

  it('tap in idle starts recording', () => {
    sm._current = 'idle';
    handler._handleEvent(CLICK);
    expect(audio.start).toHaveBeenCalled();
    expect(sm.transition).toHaveBeenCalledWith('recording');
    expect(display.showRecording).toHaveBeenCalled();
  });

  it('tap in recording stops audio', () => {
    sm._current = 'recording';
    handler._handleEvent(CLICK);
    expect(audio.stop).toHaveBeenCalled();
  });

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
    expect(audio.start).not.toHaveBeenCalled();
    expect(audio.stop).not.toHaveBeenCalled();
    expect(sm.transition).not.toHaveBeenCalled();
    expect(display.showIdle).not.toHaveBeenCalled();
  });

  it('tap ignored in streaming', () => {
    sm._current = 'streaming';
    handler._handleEvent(CLICK);
    expect(audio.start).not.toHaveBeenCalled();
    expect(audio.stop).not.toHaveBeenCalled();
    expect(sm.transition).not.toHaveBeenCalled();
    expect(display.showIdle).not.toHaveBeenCalled();
  });

  // -----------------------------------------------------------------------
  // SDK CLICK_EVENT = 0 → undefined quirk
  // -----------------------------------------------------------------------

  it('undefined event treated as click', () => {
    sm._current = 'idle';
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    handler._handleEvent(undefined);

    expect(audio.start).toHaveBeenCalled();
    expect(sm.transition).toHaveBeenCalledWith('recording');
    expect(warnSpy).toHaveBeenCalledWith(
      '[Input] Received undefined eventType — treating as CLICK_EVENT (SDK bug)',
    );
    warnSpy.mockRestore();
  });

  // -----------------------------------------------------------------------
  // Double-tap
  // -----------------------------------------------------------------------

  it('double tap is a no-op (phase 4 placeholder)', () => {
    sm._current = 'idle';
    handler._handleEvent(DOUBLE_CLICK);
    // No action expected — double-tap is a placeholder for Phase 4
    expect(display.showIdle).not.toHaveBeenCalled();
  });

  // -----------------------------------------------------------------------
  // Lifecycle events
  // -----------------------------------------------------------------------

  it('foreground exit stops recording', () => {
    sm._current = 'recording';
    audio._isRecording = true;
    handler._handleEvent(FG_EXIT);
    expect(audio.stop).toHaveBeenCalled();
    // Should NOT transition to idle — server drives the state flow
    expect(sm.transition).not.toHaveBeenCalled();
    expect(display.showIdle).not.toHaveBeenCalled();
  });

  it('foreground enter reconnects if disconnected', () => {
    gateway._isConnected = false;
    handler._handleEvent(FG_ENTER);
    expect(gateway.connect).toHaveBeenCalled();
  });

  // -----------------------------------------------------------------------
  // Scroll throttling
  // -----------------------------------------------------------------------

  it('scroll throttled within 300ms', () => {
    vi.useFakeTimers();

    // First scroll at t=0 — should pass through
    handler._handleEvent(SCROLL_TOP);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const firstTime = (handler as any)._lastScrollTime;
    expect(firstTime).toBeGreaterThan(0);

    // Second scroll 100ms later — should be throttled
    vi.advanceTimersByTime(100);
    handler._handleEvent(SCROLL_BOTTOM);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((handler as any)._lastScrollTime).toBe(firstTime);

    // Third scroll 200ms later (total 300ms) — should pass through
    vi.advanceTimersByTime(200);
    handler._handleEvent(SCROLL_TOP);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((handler as any)._lastScrollTime).toBeGreaterThan(firstTime);

    vi.useRealTimers();
  });

  // -----------------------------------------------------------------------
  // Double-init guard
  // -----------------------------------------------------------------------

  it('double init is ignored with warning', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    // handler was already init'd in beforeEach — call again
    handler.init({
      sm: sm as unknown as StateMachine,
      display: display as unknown as DisplayManager,
      audio: audio as unknown as AudioCapture,
      gateway: gateway as unknown as Gateway,
      bridge: bridge as unknown as EvenAppBridge,
    });

    expect(warnSpy).toHaveBeenCalledWith('[Input] Already initialised — ignoring duplicate init()');
    // onEvenHubEvent should only have been registered once (from beforeEach)
    expect(bridge.onEvenHubEvent).toHaveBeenCalledTimes(1);
    warnSpy.mockRestore();
  });

  // -----------------------------------------------------------------------
  // Bare event (no sub-object) treated as click
  // -----------------------------------------------------------------------

  it('bare event (no sub-object) treated as click', () => {
    sm._current = 'idle';

    // Retrieve the onEvenHubEvent callback registered during init
    const eventCallback = bridge.onEvenHubEvent.mock.calls[0][0];

    // Fire an event with no textEvent, listEvent, sysEvent, or audioEvent
    eventCallback({});

    // Should be treated as a click → starts recording in idle state
    expect(audio.start).toHaveBeenCalled();
    expect(sm.transition).toHaveBeenCalledWith('recording');
  });

  // -----------------------------------------------------------------------
  // Audio event NOT treated as click
  // -----------------------------------------------------------------------

  it('audio event is not treated as a click', () => {
    sm._current = 'recording';

    // Retrieve the onEvenHubEvent callback registered during init
    const eventCallback = bridge.onEvenHubEvent.mock.calls[0][0];

    // Fire an event with only audioEvent populated (simulates PCM frame)
    eventCallback({ audioEvent: { audioPcm: new Uint8Array(3200) } });

    // Should NOT stop recording or trigger any state change
    expect(audio.stop).not.toHaveBeenCalled();
    expect(sm.transition).not.toHaveBeenCalled();
    expect(display.showIdle).not.toHaveBeenCalled();
  });
});
