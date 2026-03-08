import { describe, it, expect, vi, beforeEach } from 'vitest';

// ---------------------------------------------------------------------------
// Mock the SDK — must be before any import that transitively references it
// ---------------------------------------------------------------------------
const mockBridge = {
  onEvenHubEvent: vi.fn(),
};

vi.mock('@evenrealities/even_hub_sdk', () => ({
  waitForEvenAppBridge: vi.fn(() => Promise.resolve(mockBridge)),
  OsEventTypeList: {
    CLICK_EVENT: 0,
    SCROLL_TOP_EVENT: 1,
    SCROLL_BOTTOM_EVENT: 2,
    DOUBLE_CLICK_EVENT: 3,
    FOREGROUND_ENTER_EVENT: 4,
    FOREGROUND_EXIT_EVENT: 5,
    ABNORMAL_EXIT_EVENT: 6,
  },
}));

// ---------------------------------------------------------------------------
// Mock internal modules
// ---------------------------------------------------------------------------

const mockConversation = {
  clear: vi.fn(),
  addUser: vi.fn(),
  addAssistant: vi.fn(),
  addSystem: vi.fn(),
  startAssistantStream: vi.fn(),
  appendToLastAssistant: vi.fn(),
  replayHistory: vi.fn(),
  formatReverse: vi.fn().mockReturnValue('Ready.'),
  format: vi.fn().mockReturnValue('Ready.'),
  get length() { return 0; },
};
vi.mock('../conversation', () => ({
  ConversationHistory: vi.fn(() => mockConversation),
}));

const mockDisplay = {
  init: vi.fn(() => Promise.resolve()),
  showLoading: vi.fn(() => Promise.resolve()),
  showIdle: vi.fn(() => Promise.resolve()),
  showRecording: vi.fn(() => Promise.resolve()),
  showThinking: vi.fn(() => Promise.resolve()),
  showTranscribing: vi.fn(() => Promise.resolve()),
  showStreaming: vi.fn(() => Promise.resolve()),
  showDisconnected: vi.fn(() => Promise.resolve()),
  showError: vi.fn(() => Promise.resolve()),
  showConfirming: vi.fn(() => Promise.resolve()),
  showSessionMenu: vi.fn(() => Promise.resolve()),
  showSessionReset: vi.fn(() => Promise.resolve()),
  exitMenuMode: vi.fn(() => Promise.resolve()),
  appendDelta: vi.fn(() => Promise.resolve()),
  finaliseStream: vi.fn(() => Promise.resolve()),
};
vi.mock('../display', () => ({
  DisplayManager: vi.fn(() => mockDisplay),
}));

const mockGateway = {
  connect: vi.fn(),
  onMessage: vi.fn(),
  onEvent: vi.fn(),
  send: vi.fn(),
  sendJson: vi.fn(),
  requestStatus: vi.fn(),
  requestSessionList: vi.fn(),
  isConnected: true,
};
vi.mock('../gateway', () => ({
  Gateway: vi.fn(() => mockGateway),
}));

const mockSm = {
  _current: 'loading',
  get current() {
    return this._current;
  },
  transition: vi.fn(function (this: typeof mockSm, s: string) {
    this._current = s;
    return true;
  }),
  onChange: vi.fn(),
  reset: vi.fn(),
};
vi.mock('../state', () => ({
  StateMachine: vi.fn(() => mockSm),
}));

const mockInput = {
  init: vi.fn(),
  sendTextFromInput: vi.fn(),
  _handleEvent: vi.fn(),
  setSessionList: vi.fn(),
  closeSessionMenu: vi.fn(function () {
    mockSm._current = 'idle';
    return true;
  }),
  setPendingTranscription: vi.fn(),
  get pendingTranscription() { return null; },
};
vi.mock('../input', () => ({
  InputHandler: vi.fn(() => mockInput),
}));

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('main.ts boot()', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSm._current = 'loading';
  });

  /**
   * Dynamically import main.ts to trigger boot().
   * Each test should call this then flush microtasks.
   */
  async function runBoot() {
    vi.resetModules();

    vi.doMock('@evenrealities/even_hub_sdk', () => ({
      waitForEvenAppBridge: vi.fn(() => Promise.resolve(mockBridge)),
      OsEventTypeList: {
        CLICK_EVENT: 0,
        SCROLL_TOP_EVENT: 1,
        SCROLL_BOTTOM_EVENT: 2,
        DOUBLE_CLICK_EVENT: 3,
        FOREGROUND_ENTER_EVENT: 4,
        FOREGROUND_EXIT_EVENT: 5,
        ABNORMAL_EXIT_EVENT: 6,
      },
    }));
    vi.doMock('../conversation', () => ({
      ConversationHistory: vi.fn(() => mockConversation),
    }));
    vi.doMock('../display', () => ({
      DisplayManager: vi.fn(() => mockDisplay),
    }));
    vi.doMock('../gateway', () => ({
      Gateway: vi.fn(() => mockGateway),
    }));
    vi.doMock('../state', () => ({
      StateMachine: vi.fn(() => mockSm),
    }));
    vi.doMock('../input', () => ({
      InputHandler: vi.fn(() => mockInput),
    }));

    await import('../main');
    await vi.dynamicImportSettled?.() ?? new Promise((r) => setTimeout(r, 50));
  }

  /** Get the routeFrame callback registered on the gateway. */
  function getRouteFrame(): (frame: Record<string, unknown>) => void {
    return mockGateway.onMessage.mock.calls[0][0];
  }

  it('initialises InputHandler with dependencies (no audio) during boot', async () => {
    await runBoot();

    expect(mockInput.init).toHaveBeenCalledOnce();
    expect(mockInput.init).toHaveBeenCalledWith({
      sm: mockSm,
      display: mockDisplay,
      gateway: mockGateway,
      bridge: mockBridge,
      conversation: mockConversation,
    });
  });

  it('connects gateway before initialising input handler', async () => {
    await runBoot();

    const connectOrder = mockGateway.connect.mock.invocationCallOrder[0];
    const inputOrder = mockInput.init.mock.invocationCallOrder[0];
    expect(connectOrder).toBeLessThan(inputOrder);
  });

  // -----------------------------------------------------------------------
  // Session frame routing (P2-12)
  // -----------------------------------------------------------------------
  describe('session frame routing', () => {
    it('session_list frame in menu state calls display.showSessionMenu', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();
      mockSm._current = 'menu';

      const sessions = [
        { sessionKey: 'k1', sessionId: 'id1', label: 'Chat 1', updatedAt: '2026-03-07T10:00:00Z', isActive: true, preview: 'Chat 1', messageCount: 2 },
      ];

      routeFrame({
        type: 'session_list',
        sessions,
        activeSessionKey: 'k1',
      });

      expect(mockInput.setSessionList).toHaveBeenCalledWith(sessions);
      expect(mockDisplay.showSessionMenu).toHaveBeenCalledWith(sessions);
    });

    it('session_list frame outside menu state stores list but does not show menu', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();
      mockSm._current = 'idle';

      routeFrame({
        type: 'session_list',
        sessions: [],
        activeSessionKey: '',
      });

      expect(mockInput.setSessionList).toHaveBeenCalledWith([]);
      expect(mockDisplay.showSessionMenu).not.toHaveBeenCalled();
    });

    it('session_switched frame from menu clears conversation, closes menu, skips showIdle', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();
      mockSm._current = 'menu';

      routeFrame({
        type: 'session_switched',
        sessionKey: 'key-new',
        sessionId: 'sess-new',
      });

      expect(mockConversation.clear).toHaveBeenCalled();
      expect(mockInput.closeSessionMenu).toHaveBeenCalled();
      // showIdle should NOT be called — exitMenuMode already rebuilt the layout
      expect(mockDisplay.showIdle).not.toHaveBeenCalled();
    });

    it('session_switched frame from non-menu calls showIdle', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();
      mockSm._current = 'idle';

      routeFrame({
        type: 'session_switched',
        sessionKey: 'key-abc',
      });

      expect(mockConversation.clear).toHaveBeenCalled();
      expect(mockInput.closeSessionMenu).not.toHaveBeenCalled();
      expect(mockDisplay.showIdle).toHaveBeenCalled();
    });

    it('ignores status:idle while in menu state', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();
      mockSm._current = 'menu';

      routeFrame({ type: 'status', status: 'idle' });

      // State should remain menu — the guard prevents the transition
      expect(mockSm.current).toBe('menu');
      expect(mockDisplay.showIdle).not.toHaveBeenCalled();
    });

    it('session_switched without sessionId does not crash localStorage', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();
      mockSm._current = 'idle';

      // Should not throw even though sessionId is missing
      expect(() => {
        routeFrame({
          type: 'session_switched',
          sessionKey: 'key-only',
        });
      }).not.toThrow();

      expect(mockConversation.clear).toHaveBeenCalled();
    });
  });
});
