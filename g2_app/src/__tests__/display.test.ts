import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { EvenAppBridge } from '@evenrealities/even_hub_sdk';
import { ConversationHistory } from '../conversation';

// ---------------------------------------------------------------------------
// Mock the SDK
// ---------------------------------------------------------------------------
vi.mock('@evenrealities/even_hub_sdk', () => ({
  CreateStartUpPageContainer: vi.fn().mockImplementation((args: unknown) => args),
  RebuildPageContainer: vi.fn().mockImplementation((args: unknown) => args),
  StartUpPageCreateResult: { success: 0 },
  TextContainerProperty: vi.fn().mockImplementation((args: unknown) => args),
  TextContainerUpgrade: vi.fn().mockImplementation((args: unknown) => args),
  ListContainerProperty: vi.fn().mockImplementation((args: unknown) => args),
  ListItemContainerProperty: vi.fn().mockImplementation((args: unknown) => args),
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
// Mock stripMarkdown — identity function for predictable testing
// ---------------------------------------------------------------------------
vi.mock('../utils', () => ({
  stripMarkdown: (s: string) => s,
}));

import { DisplayManager } from '../display';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function createMockBridge() {
  return {
    createStartUpPageContainer: vi.fn().mockResolvedValue(0), // success
    rebuildPageContainer: vi.fn().mockResolvedValue(true),
    textContainerUpgrade: vi.fn().mockResolvedValue(true),
    audioControl: vi.fn(),
    onMicData: vi.fn(),
    onEvenHubEvent: vi.fn(),
  } as unknown as EvenAppBridge;
}

async function initDisplay(): Promise<{ dm: DisplayManager; bridge: ReturnType<typeof createMockBridge>; conversation: ConversationHistory }> {
  const dm = new DisplayManager();
  const bridge = createMockBridge();
  const conversation = new ConversationHistory();
  await dm.init(bridge as unknown as EvenAppBridge, conversation);
  return { dm, bridge: bridge as unknown as ReturnType<typeof createMockBridge>, conversation };
}

// Cast to get access to mock methods on the bridge
function asMock(bridge: unknown) {
  return bridge as {
    createStartUpPageContainer: ReturnType<typeof vi.fn>;
    rebuildPageContainer: ReturnType<typeof vi.fn>;
    textContainerUpgrade: ReturnType<typeof vi.fn>;
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('DisplayManager', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  // -----------------------------------------------------------------------
  // Task 1: showThinking exists
  // -----------------------------------------------------------------------
  describe('showThinking (P3.4)', () => {
    it('exists and is callable', async () => {
      const { dm } = await initDisplay();
      expect(typeof dm.showThinking).toBe('function');
    });

    it('updates status and footer for thinking layout', async () => {
      const { dm, bridge } = await initDisplay();
      await dm.showThinking();
      expect(asMock(bridge).textContainerUpgrade).toHaveBeenCalled();
    });
  });

  // -----------------------------------------------------------------------
  // Task 2: Delta Buffering During Layout Transitions (P3.6)
  // -----------------------------------------------------------------------
  describe('delta buffering during layout transition (P3.6)', () => {
    it('buffers deltas arriving during showStreaming rebuild and flushes them', async () => {
      const { dm, bridge, conversation } = await initDisplay();
      const mock = asMock(bridge);

      // Make rebuild take time so we can inject deltas during it
      let resolveRebuild!: (v: boolean) => void;
      mock.rebuildPageContainer.mockImplementation(
        () => new Promise<boolean>((r) => { resolveRebuild = r; }),
      );

      // Start showStreaming (will block on rebuild)
      const streamingPromise = dm.showStreaming();
      await streamingPromise;

      // Push deltas after streaming is set up
      conversation.startAssistantStream();
      conversation.appendToLastAssistant('chunk1');
      await dm.appendDelta('chunk1');
      conversation.appendToLastAssistant('chunk2');
      await dm.appendDelta('chunk2');

      // Advance timers to trigger the debounce flush
      await vi.advanceTimersByTimeAsync(100);

      // Now the merged deltas should have been sent
      expect(mock.textContainerUpgrade).toHaveBeenCalled();
      // streamBuffer should contain both chunks via conversation
      expect(dm.streamBuffer).toBe('chunk1chunk2');
    });

    it('works correctly with 0 deltas during transition', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();

      // Advance timer — nothing should crash
      await vi.advanceTimersByTimeAsync(100);

      // streamBuffer is empty (no conversation entries)
      expect(dm.streamBuffer).toBe('');
    });
  });

  // -----------------------------------------------------------------------
  // Task 3: Truncation + Delta Batching (P3.7)
  // -----------------------------------------------------------------------
  describe('truncation (P3.7)', () => {
    it('triggers rebuild when cumulative deltas exceed budget', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();
      mock.textContainerUpgrade.mockClear();
      mock.rebuildPageContainer.mockClear();

      // Send a large delta that exceeds the upgrade budget
      // New budget: _transcriptLen + delta > UPGRADE_CHAR_LIMIT - 100 (1900)
      const bigDelta = 'x'.repeat(1870);
      await dm.appendDelta(bigDelta);

      // Flush the debounce timer
      await vi.advanceTimersByTimeAsync(100);

      // Should have triggered a rebuild (appendToTranscript falls back)
      expect(mock.rebuildPageContainer).toHaveBeenCalled();
    });

    it('appends normally when under budget limit', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();
      mock.textContainerUpgrade.mockClear();

      await dm.appendDelta('hello');
      await vi.advanceTimersByTimeAsync(100);

      const calls = mock.textContainerUpgrade.mock.calls;
      expect(calls.length).toBe(1);
      const call = calls[0][0];
      expect(call.content).toBe('hello');
      expect(call.containerID).toBe(3); // ID_TRANSCRIPT
    });
  });

  describe('delta batching (P3.7)', () => {
    it('merges multiple rapid deltas into a single display update', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();
      mock.textContainerUpgrade.mockClear();

      // Rapid-fire deltas within the 100ms window
      await dm.appendDelta('a');
      await dm.appendDelta('b');
      await dm.appendDelta('c');

      // Before timer fires, no additional display update
      expect(mock.textContainerUpgrade).not.toHaveBeenCalled();

      // Advance timer to trigger flush
      await vi.advanceTimersByTimeAsync(100);

      // Should be exactly one textContainerUpgrade call with merged content
      expect(mock.textContainerUpgrade).toHaveBeenCalledTimes(1);
      const call = mock.textContainerUpgrade.mock.calls[0][0];
      expect(call.content).toBe('abc');
    });

    it('batches deltas separately across timer windows', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();
      mock.textContainerUpgrade.mockClear();

      // First batch
      await dm.appendDelta('x');
      await vi.advanceTimersByTimeAsync(100);

      expect(mock.textContainerUpgrade).toHaveBeenCalledTimes(1);
      expect(mock.textContainerUpgrade.mock.calls[0][0].content).toBe('x');

      // Second batch
      await dm.appendDelta('y');
      await vi.advanceTimersByTimeAsync(100);

      expect(mock.textContainerUpgrade).toHaveBeenCalledTimes(2);
      expect(mock.textContainerUpgrade.mock.calls[1][0].content).toBe('y');
    });
  });

  // -----------------------------------------------------------------------
  // finaliseStream integration
  // -----------------------------------------------------------------------
  describe('finaliseStream', () => {
    it('flushes pending batch deltas before finalising', async () => {
      const { dm, bridge, conversation } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();
      mock.textContainerUpgrade.mockClear();

      // Send a delta but don't let the timer fire
      conversation.startAssistantStream();
      conversation.appendToLastAssistant('abc');
      await dm.appendDelta('abc');

      // Finalise immediately — should flush the batch
      await dm.finaliseStream();

      // streamBuffer should contain the flushed delta via conversation
      expect(dm.streamBuffer).toBe('abc');

      // finaliseStream should have called textContainerUpgrade
      // (batch flush + replaceTranscript + status + footer = 4 calls)
      expect(mock.textContainerUpgrade).toHaveBeenCalledTimes(4);

      // Validate the batch flush call appends text
      const contentCall = mock.textContainerUpgrade.mock.calls[0][0];
      expect(contentCall.containerID).toBe(3);
      expect(contentCall.content).toBe('abc');
    });
  });

  // -----------------------------------------------------------------------
  // Empty delta guard (M-6)
  // -----------------------------------------------------------------------
  describe('empty delta guard (M-6)', () => {
    it('ignores empty string deltas', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();
      mock.textContainerUpgrade.mockClear();

      await dm.appendDelta('');
      await vi.advanceTimersByTimeAsync(100);

      expect(mock.textContainerUpgrade).not.toHaveBeenCalled();
      expect(dm.streamBuffer).toBe('');
    });
  });

  // -----------------------------------------------------------------------
  // finaliseStream edge cases (M-5)
  // -----------------------------------------------------------------------
  describe('finaliseStream edge cases (M-5)', () => {
    it('finalises with empty streamBuffer (no deltas)', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();
      mock.textContainerUpgrade.mockClear();

      // No deltas — finalise immediately
      await dm.finaliseStream();

      // Should update replaceTranscript + status + footer = 3 calls
      expect(mock.textContainerUpgrade).toHaveBeenCalledTimes(3);
    });

    it('finalises after large delta that triggered rebuild', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();

      // Trigger rebuild via large delta
      await dm.appendDelta('x'.repeat(1870));
      await vi.advanceTimersByTimeAsync(100);

      mock.textContainerUpgrade.mockClear();

      // Finalise
      await dm.finaliseStream();

      // replaceTranscript + status + footer = 3 calls
      expect(mock.textContainerUpgrade).toHaveBeenCalledTimes(3);
    });
  });

  // -----------------------------------------------------------------------
  // showError / showDisconnected state reset (H1)
  // -----------------------------------------------------------------------
  describe('showError state reset (H1)', () => {
    it('clears delta timer on error', async () => {
      const { dm, bridge } = await initDisplay();

      await dm.showStreaming();
      await dm.appendDelta('hello');
      await vi.advanceTimersByTimeAsync(100);

      await dm.showError('Something failed');
      // streamBuffer delegates to conversation — should be empty since no conversation entry
      expect(dm.streamBuffer).toBe('');
    });
  });

  describe('showDisconnected state reset (H1)', () => {
    it('clears delta timer on disconnect', async () => {
      const { dm, bridge } = await initDisplay();

      await dm.showStreaming();
      await dm.appendDelta('hello');
      await vi.advanceTimersByTimeAsync(100);

      await dm.showDisconnected();
      expect(dm.streamBuffer).toBe('');
    });
  });

  // -----------------------------------------------------------------------
  // Truncation boundary (M-5)
  // -----------------------------------------------------------------------
  describe('truncation boundary (M-5)', () => {
    it('does NOT trigger rebuild at budget boundary (1867 chars)', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();
      mock.textContainerUpgrade.mockClear();
      mock.rebuildPageContainer.mockClear();

      await dm.appendDelta('x'.repeat(1867));
      await vi.advanceTimersByTimeAsync(100);

      // Should append normally, not rebuild
      expect(mock.textContainerUpgrade).toHaveBeenCalled();
      expect(mock.rebuildPageContainer).not.toHaveBeenCalled();
    });

    it('triggers rebuild at 1868 chars (exceeds budget)', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();
      mock.rebuildPageContainer.mockClear();

      await dm.appendDelta('x'.repeat(1868));
      await vi.advanceTimersByTimeAsync(100);

      expect(mock.rebuildPageContainer).toHaveBeenCalled();
    });
  });

  // -----------------------------------------------------------------------
  // Offset and contentLength validation (M-4)
  // -----------------------------------------------------------------------
  describe('offset and contentLength validation (M-4)', () => {
    it('appends first delta at end of transcript', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();
      mock.textContainerUpgrade.mockClear();

      await dm.appendDelta('hello');
      await vi.advanceTimersByTimeAsync(100);

      const call = mock.textContainerUpgrade.mock.calls[0][0];
      expect(call.contentLength).toBe(0); // appending (not replacing)
      expect(call.content).toBe('hello');
    });

    it('appends second delta at correct offset', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();
      mock.textContainerUpgrade.mockClear();

      await dm.appendDelta('abc');
      await vi.advanceTimersByTimeAsync(100);

      const firstOffset = mock.textContainerUpgrade.mock.calls[0][0].contentOffset;

      await dm.appendDelta('def');
      await vi.advanceTimersByTimeAsync(100);

      const call = mock.textContainerUpgrade.mock.calls[1][0];
      expect(call.contentOffset).toBe(firstOffset + 3); // after 'abc'
      expect(call.contentLength).toBe(0); // appending
      expect(call.content).toBe('def');
    });

    it('finaliseStream with pending batch flushes correctly', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();
      mock.textContainerUpgrade.mockClear();

      await dm.appendDelta('abc');
      await dm.finaliseStream();

      const contentCall = mock.textContainerUpgrade.mock.calls[0][0];
      expect(contentCall.containerID).toBe(3);
      expect(contentCall.content).toBe('abc');
    });
  });
});
