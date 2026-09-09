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
  appendHistory: vi.fn(),
  formatReverse: vi.fn().mockReturnValue('Ready.'),
  format: vi.fn().mockReturnValue('Ready.'),
  get length() { return 0; },
};
vi.mock('../conversation', () => ({
  ConversationHistory: vi.fn(function () { return mockConversation; }),
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
  setAutoresearchHeader: vi.fn(() => Promise.resolve()),
  setAutoresearchFooter: vi.fn(() => Promise.resolve()),
  showSessionReset: vi.fn(() => Promise.resolve()),
  appendDelta: vi.fn(() => Promise.resolve()),
  finaliseStream: vi.fn(() => Promise.resolve()),
};
vi.mock('../display', () => ({
  DisplayManager: vi.fn(function () { return mockDisplay; }),
}));

const mockGateway = {
  connect: vi.fn(),
  onMessage: vi.fn(),
  onEvent: vi.fn(),
  send: vi.fn(),
  sendJson: vi.fn(),
  requestStatus: vi.fn(),
  isConnected: true,
};
vi.mock('../gateway', () => ({
  Gateway: vi.fn(function () { return mockGateway; }),
}));

const mockSm = {
  _current: 'loading',
  _callbacks: [] as Array<(newState: string, oldState: string) => void>,
  get current() {
    return this._current;
  },
  transition: vi.fn(function (this: typeof mockSm, s: string) {
    const old = this._current;
    this._current = s;
    for (const cb of this._callbacks) cb(s, old);
    return true;
  }),
  onChange: vi.fn((cb: (newState: string, oldState: string) => void) => {
    mockSm._callbacks.push(cb);
  }),
  reset: vi.fn(),
};
vi.mock('../state', () => ({
  StateMachine: vi.fn(function () { return mockSm; }),
}));

const mockInput = {
  init: vi.fn(),
  sendTextFromInput: vi.fn(),
  _handleEvent: vi.fn(),
  setPendingTranscription: vi.fn(),
  get pendingTranscription() { return null; },
};
vi.mock('../input', () => ({
  InputHandler: vi.fn(function () { return mockInput; }),
}));

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('main.ts boot()', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSm._current = 'loading';
    mockSm._callbacks = [];
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
      ConversationHistory: vi.fn(function () { return mockConversation; }),
    }));
    vi.doMock('../display', () => ({
      DisplayManager: vi.fn(function () { return mockDisplay; }),
    }));
    vi.doMock('../gateway', () => ({
      Gateway: vi.fn(function () { return mockGateway; }),
    }));
    vi.doMock('../state', () => ({
      StateMachine: vi.fn(function () { return mockSm; }),
    }));
    vi.doMock('../input', () => ({
      InputHandler: vi.fn(function () { return mockInput; }),
    }));

    await import('../main');
    await vi.dynamicImportSettled?.() ?? new Promise((r) => setTimeout(r, 50));
  }

  /** Get the routeFrame callback registered on the gateway. */
  function getRouteFrame(): (frame: Record<string, unknown>) => void {
    return mockGateway.onMessage.mock.calls[0][0];
  }

  function getRouteEvent(): (event: string) => void {
    return mockGateway.onEvent.mock.calls[0][0];
  }

  it('initialises InputHandler with dependencies (no audio) during boot', async () => {
    await runBoot();

    expect(mockInput.init).toHaveBeenCalledOnce();
    expect(mockInput.init).toHaveBeenCalledWith(expect.objectContaining({
      sm: mockSm,
      display: mockDisplay,
      gateway: mockGateway,
      bridge: mockBridge,
      conversation: mockConversation,
      applyPendingResearchOwnerHistory: expect.any(Function),
    }));
  });

  it('connects gateway before initialising input handler', async () => {
    await runBoot();

    const connectOrder = mockGateway.connect.mock.invocationCallOrder[0];
    const inputOrder = mockInput.init.mock.invocationCallOrder[0];
    expect(connectOrder).toBeLessThan(inputOrder);
  });

  // -----------------------------------------------------------------------
  // Connection and status frame routing
  // -----------------------------------------------------------------------
  it('connected frame transitions to idle and shows the idle display', async () => {
    await runBoot();
    const routeFrame = getRouteFrame();

    routeFrame({ type: 'connected', version: '1.0' });

    expect(mockSm.current).toBe('idle');
    expect(mockSm.transition).toHaveBeenCalledWith('idle');
    expect(mockDisplay.showIdle).toHaveBeenCalledOnce();
  });

  describe('status frame routing', () => {
    it('recovers error to idle on an authoritative gateway idle status', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();

      routeFrame({ type: 'error', detail: 'temporary gateway failure', code: 'GATEWAY_ERROR' });
      routeFrame({ type: 'status', status: 'idle' });

      expect(mockSm.current).toBe('idle');
      expect(mockSm.transition).toHaveBeenNthCalledWith(1, 'error');
      expect(mockSm.transition).toHaveBeenNthCalledWith(2, 'idle');
      expect(mockDisplay.showError).toHaveBeenCalledWith('temporary gateway failure');
      expect(mockDisplay.showIdle).toHaveBeenCalledOnce();
      expect(mockConversation.addSystem).toHaveBeenCalledWith('Error: temporary gateway failure');
      expect(mockConversation.clear).not.toHaveBeenCalled();
    });

    it('keeps an error visible until the gateway sends a subsequent idle status', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();

      routeFrame({ type: 'error', detail: 'temporary gateway failure', code: 'GATEWAY_ERROR' });

      expect(mockSm.current).toBe('error');
      expect(mockDisplay.showError).toHaveBeenCalledWith('temporary gateway failure');
      expect(mockDisplay.showIdle).not.toHaveBeenCalled();
    });

    it('continues to ignore status:idle while confirming', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();
      mockSm._current = 'confirming';

      routeFrame({ type: 'status', status: 'idle' });

      expect(mockSm.current).toBe('confirming');
      expect(mockDisplay.showIdle).not.toHaveBeenCalled();
    });

  });

  describe('history frame routing', () => {
    const entries = [{ role: 'assistant' as const, text: 'Astra update', ts: 3000 }];

    it('replaces conversation only for an initial/reconnect history frame', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();

      routeFrame({
        type: 'history',
        entries: [
          { role: 'user', text: 'spoken question', ts: 1000 },
          { role: 'assistant', text: 'spoken answer', ts: 2000 },
        ],
      });

      expect(mockConversation.replayHistory).toHaveBeenCalledWith([
        { role: 'user', text: 'spoken question', ts: 1000 },
        { role: 'assistant', text: 'spoken answer', ts: 2000 },
      ]);
      expect(mockConversation.appendHistory).not.toHaveBeenCalled();
      expect(mockDisplay.showIdle).toHaveBeenCalled();
    });

    it('applies owner deltas while idle and repaints the newest-first transcript', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();
      mockSm._current = 'idle';

      routeFrame({ type: 'history', historyKind: 'research_owner_delta', entries });

      expect(mockConversation.appendHistory).toHaveBeenCalledWith(entries);
      expect(mockConversation.replayHistory).not.toHaveBeenCalled();
      expect(mockDisplay.showIdle).toHaveBeenCalledOnce();
    });

    it('queues owner deltas while confirming and drains them on a later idle delta', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();
      const laterEntries = [{ role: 'assistant' as const, text: 'Later update', ts: 4000 }];
      mockSm._current = 'confirming';
      vi.clearAllMocks();

      routeFrame({ type: 'history', historyKind: 'research_owner_delta', entries });
      mockSm._current = 'idle';
      routeFrame({
        type: 'history',
        historyKind: 'research_owner_delta',
        entries: laterEntries,
      });

      expect(mockConversation.appendHistory).toHaveBeenCalledOnce();
      expect(mockConversation.appendHistory).toHaveBeenCalledWith([
        ...entries,
        ...laterEntries,
      ]);
    });

    it('drains queued owner deltas on a same-state idle status', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();
      mockSm._current = 'recording';
      vi.clearAllMocks();

      routeFrame({ type: 'history', historyKind: 'research_owner_delta', entries });
      mockSm._current = 'idle';
      routeFrame({ type: 'status', status: 'idle' });

      expect(mockConversation.appendHistory).toHaveBeenCalledWith(entries);
      expect(mockDisplay.showIdle).toHaveBeenCalledOnce();
    });

    it('drains queued owner deltas when a streaming end frame returns idle', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();
      mockSm._current = 'streaming';
      vi.clearAllMocks();

      routeFrame({ type: 'history', historyKind: 'research_owner_delta', entries });
      routeFrame({ type: 'end' });

      expect(mockConversation.appendHistory).toHaveBeenCalledWith(entries);
      expect(mockDisplay.finaliseStream).toHaveBeenCalledOnce();
    });

    it.each(['recording', 'confirming', 'transcribing', 'thinking', 'streaming'] as const)(
      'keeps the %s interaction visible when an owner delta arrives',
      async (state) => {
        await runBoot();
        const routeFrame = getRouteFrame();
        vi.clearAllMocks();
        mockSm._current = state;

        routeFrame({ type: 'history', historyKind: 'research_owner_delta', entries });

        expect(mockConversation.appendHistory).not.toHaveBeenCalled();
        expect(mockDisplay.showIdle).not.toHaveBeenCalled();

        if (state === 'confirming') {
          routeFrame({ type: 'status', status: 'thinking' });
        }
        routeFrame({ type: 'status', status: 'idle' });
        expect(mockConversation.appendHistory).toHaveBeenCalledWith(entries);
        expect(mockDisplay.showIdle).toHaveBeenCalledOnce();
      },
    );
  });

  describe('autoresearch frame routing', () => {
    const statusFrame = (overrides: Record<string, unknown> = {}) => ({
      type: 'autoresearch_status',
      hypothesisId: 'H0001',
      hypothesisState: 'FROZEN',
      attemptId: 'H0001-A001',
      attemptState: 'RUNNING',
      stage: 'running',
      lastAstraDecision: null,
      campaignStatus: 'ACTIVE',
      boundaryFailure: null,
      lastEventAt: null,
      ownerState: 'active',
      updatedAt: '2026-09-06T00:00:00Z',
      available: true,
      unavailableReason: null,
      ...overrides,
    });

    it('maps hypothesis, attempt, and stage into the header', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();

      routeFrame(statusFrame());
      routeFrame(statusFrame({ hypothesisId: null, attemptId: null, stage: 'idle' }));
      routeFrame(statusFrame({ available: false, unavailableReason: 'missing database' }));

      expect(mockDisplay.setAutoresearchHeader).toHaveBeenNthCalledWith(1, 'H:H0001 A:H0001-A001 running');
      expect(mockDisplay.setAutoresearchHeader).toHaveBeenNthCalledWith(2, 'H:— A:— idle');
      expect(mockDisplay.setAutoresearchHeader).toHaveBeenNthCalledWith(3, 'Research unavailable');
      expect(mockDisplay.setAutoresearchFooter).toHaveBeenNthCalledWith(1, 'campaign ACTIVE · owner active');
      expect(mockDisplay.setAutoresearchFooter).toHaveBeenNthCalledWith(3, 'Unavailable: missing database');
    });

    it('includes boundary failures in the footer', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();

      routeFrame(statusFrame({
        boundaryFailure: 'review_failed',
      }));

      expect(mockDisplay.setAutoresearchFooter).toHaveBeenCalledWith(
        'campaign ACTIVE · owner active · failure review_failed',
      );
    });
  });
});
