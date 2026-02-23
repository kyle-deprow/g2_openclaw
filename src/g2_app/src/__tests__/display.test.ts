import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { EvenAppBridge } from '@evenrealities/even_hub_sdk';

// ---------------------------------------------------------------------------
// Mock the SDK
// ---------------------------------------------------------------------------
vi.mock('@evenrealities/even_hub_sdk', () => ({
  CreateStartUpPageContainer: vi.fn().mockImplementation((args: unknown) => args),
  RebuildPageContainer: vi.fn().mockImplementation((args: unknown) => args),
  StartUpPageCreateResult: { success: 0 },
  TextContainerProperty: vi.fn().mockImplementation((args: unknown) => args),
  TextContainerUpgrade: vi.fn().mockImplementation((args: unknown) => args),
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

async function initDisplay(): Promise<{ dm: DisplayManager; bridge: ReturnType<typeof createMockBridge> }> {
  const dm = new DisplayManager();
  const bridge = createMockBridge();
  await dm.init(bridge as unknown as EvenAppBridge);
  return { dm, bridge: bridge as unknown as ReturnType<typeof createMockBridge> };
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

    it('rebuilds with thinking layout', async () => {
      const { dm, bridge } = await initDisplay();
      await dm.showThinking('test query');
      expect(asMock(bridge).rebuildPageContainer).toHaveBeenCalled();
    });
  });

  // -----------------------------------------------------------------------
  // Task 2: Delta Buffering During Layout Transitions (P3.6)
  // -----------------------------------------------------------------------
  describe('delta buffering during layout transition (P3.6)', () => {
    it('buffers deltas arriving during showStreaming rebuild and flushes them', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      // Make rebuild take time so we can inject deltas during it
      let resolveRebuild!: (v: boolean) => void;
      mock.rebuildPageContainer.mockImplementation(
        () => new Promise<boolean>((r) => { resolveRebuild = r; }),
      );

      // Start showStreaming (will block on rebuild)
      const streamingPromise = dm.showStreaming('hello');

      // While rebuild is pending, push deltas
      await dm.appendDelta('chunk1');
      await dm.appendDelta('chunk2');

      // textContainerUpgrade should NOT have been called yet (still rebuilding)
      expect(mock.textContainerUpgrade).not.toHaveBeenCalled();

      // Resolve rebuild
      resolveRebuild(true);
      await streamingPromise;

      // After showStreaming resolves, the flushed delta will be in the batch
      // Advance timers to trigger the debounce flush
      await vi.advanceTimersByTimeAsync(100);

      // Now the merged deltas should have been sent
      expect(mock.textContainerUpgrade).toHaveBeenCalled();
      // streamBuffer should contain both chunks
      expect(dm.streamBuffer).toBe('chunk1chunk2');
    });

    it('works correctly with 0 deltas during transition', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      let resolveRebuild!: (v: boolean) => void;
      mock.rebuildPageContainer.mockImplementation(
        () => new Promise<boolean>((r) => { resolveRebuild = r; }),
      );

      const streamingPromise = dm.showStreaming('test');

      // No deltas pushed during rebuild
      resolveRebuild(true);
      await streamingPromise;

      // Advance timer — nothing should crash
      await vi.advanceTimersByTimeAsync(100);

      // No textContainerUpgrade calls (no deltas to flush)
      expect(mock.textContainerUpgrade).not.toHaveBeenCalled();
      expect(dm.streamBuffer).toBe('');
    });
  });

  // -----------------------------------------------------------------------
  // Task 3: Truncation + Delta Batching (P3.7)
  // -----------------------------------------------------------------------
  describe('truncation (P3.7)', () => {
    it('triggers truncation hint when response exceeds 1800 chars', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming('test');

      // Send a large delta that exceeds the truncation limit
      const bigDelta = 'x'.repeat(1850);
      await dm.appendDelta(bigDelta);

      // Flush the debounce timer
      await vi.advanceTimersByTimeAsync(100);

      // Should have called textContainerUpgrade with truncation hint
      const calls = mock.textContainerUpgrade.mock.calls;
      expect(calls.length).toBeGreaterThan(0);

      const lastCall = calls[calls.length - 1][0];
      expect(lastCall.content).toContain('… [double-tap for more]');
    });

    it('still accumulates full text in streamBuffer after truncation', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming('test');

      // First delta exceeds limit
      const bigDelta = 'x'.repeat(1850);
      await dm.appendDelta(bigDelta);
      await vi.advanceTimersByTimeAsync(100);

      // Additional delta after truncation
      await dm.appendDelta('more');
      await vi.advanceTimersByTimeAsync(100);

      // streamBuffer should contain ALL text (not truncated)
      expect(dm.streamBuffer).toBe(bigDelta + 'more');

      // The second flush should NOT call textContainerUpgrade again
      // (already truncated — one call from first flush only)
      const upgradeCallsAfterFirstFlush = mock.textContainerUpgrade.mock.calls.length;
      // Only 1 call from the first truncation flush
      expect(upgradeCallsAfterFirstFlush).toBe(1);
    });

    it('shows normal content with cursor when under truncation limit', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming('hi');

      await dm.appendDelta('hello');
      await vi.advanceTimersByTimeAsync(100);

      const calls = mock.textContainerUpgrade.mock.calls;
      expect(calls.length).toBe(1);
      const call = calls[0][0];
      expect(call.content).toBe('hello█');
      expect(call.containerID).toBe(3); // ID_CONTENT
    });
  });

  describe('delta batching (P3.7)', () => {
    it('merges multiple rapid deltas into a single display update', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming('test');

      // Rapid-fire deltas within the 100ms window
      await dm.appendDelta('a');
      await dm.appendDelta('b');
      await dm.appendDelta('c');

      // Before timer fires, no display update
      expect(mock.textContainerUpgrade).not.toHaveBeenCalled();

      // Advance timer to trigger flush
      await vi.advanceTimersByTimeAsync(100);

      // Should be exactly one textContainerUpgrade call with merged content
      expect(mock.textContainerUpgrade).toHaveBeenCalledTimes(1);
      const call = mock.textContainerUpgrade.mock.calls[0][0];
      expect(call.content).toBe('abc█');
    });

    it('batches deltas separately across timer windows', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming('test');

      // First batch
      await dm.appendDelta('x');
      await vi.advanceTimersByTimeAsync(100);

      expect(mock.textContainerUpgrade).toHaveBeenCalledTimes(1);
      expect(mock.textContainerUpgrade.mock.calls[0][0].content).toBe('x█');

      // Second batch
      await dm.appendDelta('y');
      await vi.advanceTimersByTimeAsync(100);

      expect(mock.textContainerUpgrade).toHaveBeenCalledTimes(2);
      expect(mock.textContainerUpgrade.mock.calls[1][0].content).toBe('y█');
    });
  });

  // -----------------------------------------------------------------------
  // finaliseStream integration
  // -----------------------------------------------------------------------
  describe('finaliseStream', () => {
    it('flushes pending batch deltas before finalising', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming('test');

      // Send a delta but don't let the timer fire
      await dm.appendDelta('abc');

      // Finalise immediately — should flush the batch
      await dm.finaliseStream();

      // streamBuffer should contain the flushed delta
      expect(dm.streamBuffer).toBe('abc');

      // finaliseStream should have called textContainerUpgrade
      // (batch flush + badge + footer = 3 calls)
      expect(mock.textContainerUpgrade).toHaveBeenCalledTimes(3);

      // Validate the batch flush call sends unflushed text replacing cursor
      const contentCall = mock.textContainerUpgrade.mock.calls[0][0];
      expect(contentCall.containerID).toBe(3);
      expect(contentCall.contentOffset).toBe(0);
      expect(contentCall.contentLength).toBe(1); // the cursor
      expect(contentCall.content).toBe('abc');   // no cursor appended
    });
  });

  // -----------------------------------------------------------------------
  // Empty delta guard (M-6)
  // -----------------------------------------------------------------------
  describe('empty delta guard (M-6)', () => {
    it('ignores empty string deltas', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming('test');
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
    it('strips cursor with empty streamBuffer', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming('test');
      // No deltas — finalise immediately
      await dm.finaliseStream();

      // Should strip cursor (offset 0, length 1) + badge + footer = 3
      expect(mock.textContainerUpgrade).toHaveBeenCalledTimes(3);
      const cursorCall = mock.textContainerUpgrade.mock.calls[0][0];
      expect(cursorCall.containerID).toBe(3);
      expect(cursorCall.contentOffset).toBe(0);
      expect(cursorCall.contentLength).toBe(1);
      expect(cursorCall.content).toBe('');
    });

    it('skips cursor strip after truncation', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming('test');

      // Trigger truncation
      await dm.appendDelta('x'.repeat(1850));
      await vi.advanceTimersByTimeAsync(100);

      mock.textContainerUpgrade.mockClear();

      // Finalise — should NOT strip cursor (display is truncated)
      await dm.finaliseStream();

      // Only badge + footer updates (no cursor strip)
      expect(mock.textContainerUpgrade).toHaveBeenCalledTimes(2);
    });
  });

  // -----------------------------------------------------------------------
  // showError / showDisconnected state reset (H1)
  // -----------------------------------------------------------------------
  describe('showError state reset (H1)', () => {
    it('resets all streaming state', async () => {
      const { dm, bridge } = await initDisplay();

      await dm.showStreaming('test');
      await dm.appendDelta('hello');
      await vi.advanceTimersByTimeAsync(100);

      await dm.showError('Something failed');
      expect(dm.streamBuffer).toBe('');
    });
  });

  describe('showDisconnected state reset (H1)', () => {
    it('resets all streaming state', async () => {
      const { dm, bridge } = await initDisplay();

      await dm.showStreaming('test');
      await dm.appendDelta('hello');
      await vi.advanceTimersByTimeAsync(100);

      await dm.showDisconnected(5);
      expect(dm.streamBuffer).toBe('');
    });
  });

  // -----------------------------------------------------------------------
  // Truncation boundary (M-5)
  // -----------------------------------------------------------------------
  describe('truncation boundary (M-5)', () => {
    it('does NOT truncate at exactly 1800 chars', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming('test');
      await dm.appendDelta('x'.repeat(1800));
      await vi.advanceTimersByTimeAsync(100);

      const call = mock.textContainerUpgrade.mock.calls[0][0];
      expect(call.content).toBe('x'.repeat(1800) + '█');
      expect(call.content).not.toContain('… [double-tap for more]');
    });

    it('truncates at 1801 chars', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming('test');
      await dm.appendDelta('x'.repeat(1801));
      await vi.advanceTimersByTimeAsync(100);

      const call = mock.textContainerUpgrade.mock.calls[0][0];
      expect(call.content).toContain('… [double-tap for more]');
    });
  });

  // -----------------------------------------------------------------------
  // Offset and contentLength validation (M-4)
  // -----------------------------------------------------------------------
  describe('offset and contentLength validation (M-4)', () => {
    it('uses correct offset and contentLength for first delta', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming('test');
      await dm.appendDelta('hello');
      await vi.advanceTimersByTimeAsync(100);

      const call = mock.textContainerUpgrade.mock.calls[0][0];
      expect(call.contentOffset).toBe(0); // streamBuffer was empty
      expect(call.contentLength).toBe(1); // replacing cursor '█'
      expect(call.content).toBe('hello█');
    });

    it('uses correct offset for second delta', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming('test');
      await dm.appendDelta('abc');
      await vi.advanceTimersByTimeAsync(100);

      await dm.appendDelta('def');
      await vi.advanceTimersByTimeAsync(100);

      const call = mock.textContainerUpgrade.mock.calls[1][0];
      expect(call.contentOffset).toBe(3); // 'abc' length
      expect(call.contentLength).toBe(1); // cursor
      expect(call.content).toBe('def█');
    });

    it('truncation uses prevDisplayLen as contentLength', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming('test');
      await dm.appendDelta('x'.repeat(1850));
      await vi.advanceTimersByTimeAsync(100);

      const call = mock.textContainerUpgrade.mock.calls[0][0];
      expect(call.contentOffset).toBe(0);
      // Display had '█' (1 char) before this flush
      expect(call.contentLength).toBe(1);
    });

    it('finaliseStream with pending batch uses correct offset', async () => {
      const { dm, bridge } = await initDisplay();
      const mock = asMock(bridge);

      await dm.showStreaming('test');
      await dm.appendDelta('abc');
      await dm.finaliseStream();

      const contentCall = mock.textContainerUpgrade.mock.calls[0][0];
      expect(contentCall.containerID).toBe(3);
      expect(contentCall.contentOffset).toBe(0);
      expect(contentCall.contentLength).toBe(1);
      expect(contentCall.content).toBe('abc');
    });
  });
});
