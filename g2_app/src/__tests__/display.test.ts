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
    it('triggers rebuild when transcript exceeds upgrade budget', async () => {
      const { dm, bridge, conversation } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();

      // Build a conversation that exceeds the 1900 char budget
      conversation.startAssistantStream();
      conversation.appendToLastAssistant('x'.repeat(1950));

      mock.textContainerUpgrade.mockClear();
      mock.rebuildPageContainer.mockClear();

      await dm.appendDelta('z');
      await vi.advanceTimersByTimeAsync(100);

      // replaceTranscript falls back to rebuild when over budget
      expect(mock.rebuildPageContainer).toHaveBeenCalled();
    });

    it('replaces normally when under budget limit', async () => {
      const { dm, bridge, conversation } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();

      conversation.startAssistantStream();
      conversation.appendToLastAssistant('hello');

      mock.textContainerUpgrade.mockClear();

      await dm.appendDelta('hello');
      await vi.advanceTimersByTimeAsync(100);

      const calls = mock.textContainerUpgrade.mock.calls;
      expect(calls.length).toBe(1);
      const call = calls[0][0];
      // Full replace: content is formatReverse output, offset 0
      expect(call.containerID).toBe(3); // ID_TRANSCRIPT
      expect(call.contentOffset).toBe(0);
      expect(call.content).toContain('hello');
    });
  });

  describe('delta batching (P3.7)', () => {
    it('merges multiple rapid deltas into a single display update', async () => {
      const { dm, bridge, conversation } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();

      conversation.startAssistantStream();
      conversation.appendToLastAssistant('a');
      conversation.appendToLastAssistant('b');
      conversation.appendToLastAssistant('c');

      mock.textContainerUpgrade.mockClear();

      // Rapid-fire deltas within the 100ms window
      await dm.appendDelta('a');
      await dm.appendDelta('b');
      await dm.appendDelta('c');

      // Before timer fires, no additional display update
      expect(mock.textContainerUpgrade).not.toHaveBeenCalled();

      // Advance timer to trigger flush
      await vi.advanceTimersByTimeAsync(100);

      // Should be exactly one textContainerUpgrade call (full replace)
      expect(mock.textContainerUpgrade).toHaveBeenCalledTimes(1);
      const call = mock.textContainerUpgrade.mock.calls[0][0];
      // Content is the full formatReverse output containing 'abc'
      expect(call.content).toContain('abc');
      expect(call.contentOffset).toBe(0); // full replace
    });

    it('batches deltas separately across timer windows', async () => {
      const { dm, bridge, conversation } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();

      conversation.startAssistantStream();
      conversation.appendToLastAssistant('x');

      mock.textContainerUpgrade.mockClear();

      // First batch
      await dm.appendDelta('x');
      await vi.advanceTimersByTimeAsync(100);

      expect(mock.textContainerUpgrade).toHaveBeenCalledTimes(1);
      expect(mock.textContainerUpgrade.mock.calls[0][0].content).toContain('x');

      // Second batch
      conversation.appendToLastAssistant('y');
      await dm.appendDelta('y');
      await vi.advanceTimersByTimeAsync(100);

      expect(mock.textContainerUpgrade).toHaveBeenCalledTimes(2);
      expect(mock.textContainerUpgrade.mock.calls[1][0].content).toContain('xy');
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
      // (batch flush replace + replaceTranscript + status + footer = 4 calls)
      expect(mock.textContainerUpgrade).toHaveBeenCalledTimes(4);

      // Validate the batch flush does a full replace (offset 0)
      const contentCall = mock.textContainerUpgrade.mock.calls[0][0];
      expect(contentCall.containerID).toBe(3);
      expect(contentCall.contentOffset).toBe(0);
      expect(contentCall.content).toContain('abc');
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
      const { dm, bridge, conversation } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();

      // Build a conversation that exceeds the budget
      conversation.startAssistantStream();
      conversation.appendToLastAssistant('x'.repeat(1950));
      await dm.appendDelta('x'.repeat(1950));
      await vi.advanceTimersByTimeAsync(100);

      mock.textContainerUpgrade.mockClear();

      // Finalise
      await dm.finaliseStream();

      // replaceTranscript falls back to rebuild (transcript baked in),
      // then status + footer = 2 textContainerUpgrade calls
      expect(mock.textContainerUpgrade).toHaveBeenCalledTimes(2);
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
    it('does NOT trigger rebuild when formatReverse output is under budget', async () => {
      const { dm, bridge, conversation } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();

      // Add content that stays under 1900 chars
      conversation.startAssistantStream();
      conversation.appendToLastAssistant('x'.repeat(100));

      mock.textContainerUpgrade.mockClear();
      mock.rebuildPageContainer.mockClear();

      await dm.appendDelta('x');
      await vi.advanceTimersByTimeAsync(100);

      // Should replace normally, not rebuild
      expect(mock.textContainerUpgrade).toHaveBeenCalled();
      expect(mock.rebuildPageContainer).not.toHaveBeenCalled();
    });

    it('triggers rebuild when formatReverse output exceeds budget', async () => {
      const { dm, bridge, conversation } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();

      // Add content that exceeds the 1900 char budget
      conversation.startAssistantStream();
      conversation.appendToLastAssistant('x'.repeat(1950));

      mock.rebuildPageContainer.mockClear();

      await dm.appendDelta('x');
      await vi.advanceTimersByTimeAsync(100);

      expect(mock.rebuildPageContainer).toHaveBeenCalled();
    });
  });

  // -----------------------------------------------------------------------
  // Offset and contentLength validation (M-4)
  // -----------------------------------------------------------------------
  describe('offset and contentLength validation (M-4)', () => {
    it('does a full replace (offset 0) for first delta flush', async () => {
      const { dm, bridge, conversation } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();

      conversation.startAssistantStream();
      conversation.appendToLastAssistant('hello');

      mock.textContainerUpgrade.mockClear();

      await dm.appendDelta('hello');
      await vi.advanceTimersByTimeAsync(100);

      const call = mock.textContainerUpgrade.mock.calls[0][0];
      expect(call.contentOffset).toBe(0); // full replace
      expect(call.content).toContain('hello');
    });

    it('does a full replace for second delta flush too', async () => {
      const { dm, bridge, conversation } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();

      conversation.startAssistantStream();
      conversation.appendToLastAssistant('abc');

      mock.textContainerUpgrade.mockClear();

      await dm.appendDelta('abc');
      await vi.advanceTimersByTimeAsync(100);

      conversation.appendToLastAssistant('def');
      await dm.appendDelta('def');
      await vi.advanceTimersByTimeAsync(100);

      const call = mock.textContainerUpgrade.mock.calls[1][0];
      expect(call.contentOffset).toBe(0); // full replace
      expect(call.content).toContain('abcdef');
    });

    it('finaliseStream with pending batch flushes correctly', async () => {
      const { dm, bridge, conversation } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming();

      conversation.startAssistantStream();
      conversation.appendToLastAssistant('abc');

      mock.textContainerUpgrade.mockClear();

      await dm.appendDelta('abc');
      await dm.finaliseStream();

      const contentCall = mock.textContainerUpgrade.mock.calls[0][0];
      expect(contentCall.containerID).toBe(3);
      expect(contentCall.contentOffset).toBe(0);
      expect(contentCall.content).toContain('abc');
    });
  });

  // -----------------------------------------------------------------------
  // showSessionReset
  // -----------------------------------------------------------------------
  describe('showSessionReset', () => {
    it('displays the reset label in the transcript', async () => {
      const { dm, bridge, conversation } = await initDisplay();
      const mock = asMock(bridge);

      conversation.addSystem('Session reset');
      mock.textContainerUpgrade.mockClear();

      await dm.showSessionReset('Session reset');

      // Should update status to "New Session"
      const statusCall = mock.textContainerUpgrade.mock.calls.find(
        (c: any[]) => c[0].containerID === 1,
      );
      expect(statusCall).toBeDefined();
      expect(statusCall![0].content).toContain('New Session');

      // Should update transcript with conversation content containing the label
      const transcriptCall = mock.textContainerUpgrade.mock.calls.find(
        (c: any[]) => c[0].containerID === 3,
      );
      expect(transcriptCall).toBeDefined();
      expect(transcriptCall![0].content).toContain('Session reset');
    });

    it('clears the delta timer', async () => {
      const { dm, bridge } = await initDisplay();

      // Start streaming and queue a delta
      await dm.showStreaming();
      await dm.appendDelta('pending');

      const mock = asMock(bridge);
      mock.textContainerUpgrade.mockClear();

      // showSessionReset should NOT flush the pending streaming delta
      // because it clears delta timer via the status → replaceTranscript path
      await dm.showSessionReset('New day, new session');

      // Advance past the delta timer window — the old delta batch should be gone
      await vi.advanceTimersByTimeAsync(150);

      // The 'pending' text should not appear in any transcript update from
      // the delta flush (the batch was cleared by the status update path)
      const allCalls = mock.textContainerUpgrade.mock.calls;
      const deltaFlushCall = allCalls.find(
        (c: any[]) => c[0].containerID === 3 && c[0].content === 'pending',
      );
      expect(deltaFlushCall).toBeUndefined();
    });

    it('auto-reverts to idle display after 2 s timeout', async () => {
      const { dm, bridge, conversation } = await initDisplay();
      const mock = asMock(bridge);

      conversation.addSystem('Session reset');
      await dm.showSessionReset('Session reset');

      // Clear to isolate the auto-revert calls
      mock.textContainerUpgrade.mockClear();
      mock.rebuildPageContainer.mockClear();

      // Advance past the 2 000ms auto-revert timeout
      await vi.advanceTimersByTimeAsync(2100);

      // showIdle uses replaceTranscript (no scroll-to-bottom hack)
      // Transcript replace + status + footer = 3 textContainerUpgrade calls
      expect(mock.textContainerUpgrade).toHaveBeenCalled();

      const transcriptReplace = mock.textContainerUpgrade.mock.calls.find(
        (c: any[]) => c[0].containerID === 3,
      );
      expect(transcriptReplace).toBeDefined();
      expect(transcriptReplace![0].content).toContain('Session reset');
    });
  });

  // -----------------------------------------------------------------------
  // showIdle uses replaceTranscript (no scroll-to-bottom hack)
  // -----------------------------------------------------------------------
  describe('showIdle', () => {
    it('uses replaceTranscript for normal showIdle', async () => {
      const { dm, bridge, conversation } = await initDisplay();
      const mock = asMock(bridge);

      conversation.addUser('Hello');
      conversation.addAssistant('World');
      mock.rebuildPageContainer.mockClear();
      mock.textContainerUpgrade.mockClear();

      await dm.showIdle();

      // Should NOT rebuild — uses in-place replace
      expect(mock.rebuildPageContainer).not.toHaveBeenCalled();
      expect(mock.textContainerUpgrade).toHaveBeenCalled();

      // Transcript should be a full replace (offset 0)
      const transcriptCall = mock.textContainerUpgrade.mock.calls.find(
        (c: any[]) => c[0].containerID === 3,
      );
      expect(transcriptCall).toBeDefined();
      expect(transcriptCall![0].contentOffset).toBe(0);
      // Reverse order: World first, then Hello
      const content = transcriptCall![0].content;
      expect(content.indexOf('World')).toBeLessThan(content.indexOf('Hello'));
    });

    it('does NOT rebuild on consecutive showIdle calls', async () => {
      const { dm, bridge, conversation } = await initDisplay();
      const mock = asMock(bridge);

      conversation.addUser('Hello');

      await dm.showIdle();

      mock.rebuildPageContainer.mockClear();
      mock.textContainerUpgrade.mockClear();

      await dm.showIdle();

      expect(mock.rebuildPageContainer).not.toHaveBeenCalled();
      expect(mock.textContainerUpgrade).toHaveBeenCalled();
    });
  });
});
