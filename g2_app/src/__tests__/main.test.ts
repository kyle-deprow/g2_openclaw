import { describe, it, expect, vi, beforeEach } from 'vitest';

// ---------------------------------------------------------------------------
// Mock the SDK — must be before any import that transitively references it
// ---------------------------------------------------------------------------
const mockBridge = {
  audioControl: vi.fn(),
  onMicData: vi.fn(),
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

const mockDisplay = {
  init: vi.fn(() => Promise.resolve()),
  showLoading: vi.fn(() => Promise.resolve()),
  showIdle: vi.fn(),
  showRecording: vi.fn(),
  showThinking: vi.fn(),
  showTranscribing: vi.fn(),
  showStreaming: vi.fn(),
  showDisconnected: vi.fn(),
  showError: vi.fn(),
  appendDelta: vi.fn(),
  finaliseStream: vi.fn(),
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

const mockAudio = {
  init: vi.fn(),
  start: vi.fn(),
  stop: vi.fn(),
  isRecording: false,
};
vi.mock('../audio', () => ({
  AudioCapture: vi.fn(() => mockAudio),
}));

const mockInput = {
  init: vi.fn(),
  _handleEvent: vi.fn(),
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
    // Bust the module cache so boot() runs fresh each time
    vi.resetModules();

    // Re-apply mocks after resetModules
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
    vi.doMock('../display', () => ({
      DisplayManager: vi.fn(() => mockDisplay),
    }));
    vi.doMock('../gateway', () => ({
      Gateway: vi.fn(() => mockGateway),
    }));
    vi.doMock('../state', () => ({
      StateMachine: vi.fn(() => mockSm),
    }));
    vi.doMock('../audio', () => ({
      AudioCapture: vi.fn(() => mockAudio),
    }));
    vi.doMock('../input', () => ({
      InputHandler: vi.fn(() => mockInput),
    }));

    await import('../main');
    // Flush all pending microtasks (boot() is async)
    await vi.dynamicImportSettled?.() ?? new Promise((r) => setTimeout(r, 50));
  }

  it('initialises AudioCapture with bridge and gateway during boot', async () => {
    await runBoot();

    expect(mockAudio.init).toHaveBeenCalledOnce();
    expect(mockAudio.init).toHaveBeenCalledWith(mockBridge, mockGateway);
  });

  it('initialises InputHandler with all dependencies during boot', async () => {
    await runBoot();

    expect(mockInput.init).toHaveBeenCalledOnce();
    expect(mockInput.init).toHaveBeenCalledWith({
      sm: mockSm,
      display: mockDisplay,
      audio: mockAudio,
      gateway: mockGateway,
      bridge: mockBridge,
    });
  });

  it('initialises audio before input handler', async () => {
    await runBoot();

    const audioOrder = mockAudio.init.mock.invocationCallOrder[0];
    const inputOrder = mockInput.init.mock.invocationCallOrder[0];
    expect(audioOrder).toBeLessThan(inputOrder);
  });

  it('connects gateway before initialising audio', async () => {
    await runBoot();

    const connectOrder = mockGateway.connect.mock.invocationCallOrder[0];
    const audioOrder = mockAudio.init.mock.invocationCallOrder[0];
    expect(connectOrder).toBeLessThan(audioOrder);
  });
});
