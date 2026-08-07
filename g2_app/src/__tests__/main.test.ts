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
  setFeedEntries: vi.fn(),
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

  describe('autoresearch frame routing', () => {
    const statusFrame = (overrides: Record<string, unknown> = {}) => ({
      type: 'autoresearch_status',
      running: true,
      phase: 'cycling',
      iteration: 4,
      suspended: false,
      campaignReviewRequired: false,
      ...overrides,
    });

    it('formats and routes running, stopped, and not-running headers', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();

      routeFrame(statusFrame());
      routeFrame(statusFrame({ running: false, phase: 'paused', iteration: 7 }));
      routeFrame(statusFrame({ running: false, phase: 'not running', iteration: 0 }));

      expect(mockDisplay.setAutoresearchHeader).toHaveBeenNthCalledWith(1, 'AR cycling it4');
      expect(mockDisplay.setAutoresearchHeader).toHaveBeenNthCalledWith(2, 'AR stopped · paused it7');
      expect(mockDisplay.setAutoresearchHeader).toHaveBeenNthCalledWith(3, 'AR not running');
    });

    it('formats outcome, suspended, and review suffixes', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();

      routeFrame(statusFrame({
        supervisorOutcome: 'continue',
        suspended: true,
        campaignReviewRequired: true,
      }));

      expect(mockDisplay.setAutoresearchHeader).toHaveBeenCalledWith(
        'AR cycling it4 · continue ⏸ ⚠rev',
      );
    });

    it('truncates a long phase while preserving the autoresearch suffix', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();

      routeFrame(statusFrame({
        phase: '1234567890123456789012345678901234567890',
        supervisorOutcome: 'continue',
        suspended: true,
        campaignReviewRequired: true,
      }));

      const header = (mockDisplay.setAutoresearchHeader.mock.calls[0] as unknown as [string])[0];
      const phaseEnd = header.indexOf(' it4');
      expect(header.length).toBeLessThanOrEqual(50);
      expect(header.slice(3, phaseEnd).endsWith('…')).toBe(true);
      expect(header.endsWith(' ⏸ ⚠rev')).toBe(true);
    });

    it('applies an autoresearch feed immediately while idle', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();
      const entries = [{ role: 'assistant', text: 'update', ts: 10 }];
      mockSm._current = 'idle';

      routeFrame({ type: 'autoresearch_feed', entries });

      expect(mockConversation.setFeedEntries).toHaveBeenCalledWith(entries);
      expect(mockDisplay.showIdle).toHaveBeenCalledOnce();
    });

    it('defers streaming feeds and applies only the latest feed on idle', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();
      const first = [{ role: 'assistant', text: 'first', ts: 10 }];
      const latest = [{ role: 'assistant', text: 'latest', ts: 20 }];
      mockSm._current = 'streaming';

      routeFrame({ type: 'autoresearch_feed', entries: first });
      routeFrame({ type: 'autoresearch_feed', entries: latest });

      expect(mockConversation.setFeedEntries).not.toHaveBeenCalled();
      expect(mockDisplay.showIdle).not.toHaveBeenCalled();

      mockSm.transition('idle');

      expect(mockConversation.setFeedEntries).toHaveBeenCalledOnce();
      expect(mockConversation.setFeedEntries).toHaveBeenCalledWith(latest);
      expect(mockDisplay.showIdle).toHaveBeenCalledOnce();
      mockSm.transition('idle');
      expect(mockConversation.setFeedEntries).toHaveBeenCalledOnce();
    });

    it('drops a stashed feed when disconnected before reconnecting', async () => {
      await runBoot();
      const routeFrame = getRouteFrame();
      const routeEvent = getRouteEvent();
      const stale = [{ role: 'assistant', text: 'stale', ts: 10 }];
      mockSm._current = 'streaming';

      routeFrame({ type: 'autoresearch_feed', entries: stale });
      routeEvent('disconnected');
      routeFrame({ type: 'connected', version: '1.0' });

      expect(mockConversation.setFeedEntries).not.toHaveBeenCalledWith(stale);
    });
  });
});
