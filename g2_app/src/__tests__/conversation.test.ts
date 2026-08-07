import { describe, it, expect, beforeEach, vi } from 'vitest';
import { ConversationHistory } from '../conversation';

describe('ConversationHistory', () => {
  let conv: ConversationHistory;

  beforeEach(() => {
    conv = new ConversationHistory();
  });

  // -----------------------------------------------------------------------
  // replayHistory
  // -----------------------------------------------------------------------
  describe('replayHistory', () => {
    it('populates conversation from valid entries', () => {
      conv.replayHistory([
        { role: 'user', text: 'Hello', ts: 1000 },
        { role: 'assistant', text: 'Hi there', ts: 2000 },
      ]);

      expect(conv.length).toBe(2);
      expect(conv.lastEntry?.role).toBe('assistant');
      expect(conv.lastEntry?.text).toBe('Hi there');
      expect(conv.format()).toContain('» Hello');
      expect(conv.format()).toContain('Hi there');
    });

    it('results in empty conversation when given empty array', () => {
      // Pre-populate so we can verify clear behaviour
      conv.addUser('existing');
      expect(conv.length).toBe(1);

      conv.replayHistory([]);

      expect(conv.length).toBe(0);
      expect(conv.format()).toBe('Ready.');
    });

    it('replaces existing entries', () => {
      conv.addUser('old message');
      conv.addAssistant('old response');
      expect(conv.length).toBe(2);

      conv.replayHistory([
        { role: 'user', text: 'new question', ts: 3000 },
        { role: 'assistant', text: 'new answer', ts: 4000 },
        { role: 'user', text: 'follow up', ts: 5000 },
      ]);

      expect(conv.length).toBe(3);
      expect(conv.format()).not.toContain('old message');
      expect(conv.format()).toContain('» new question');
      expect(conv.format()).toContain('new answer');
      expect(conv.format()).toContain('» follow up');
    });

    it('preserves existing feed entries while replacing local history', () => {
      conv.setFeedEntries([
        { role: 'assistant', text: 'autoresearch update', ts: 2500 },
      ]);

      conv.replayHistory([
        { role: 'user', text: 'new question', ts: 3000 },
        { role: 'assistant', text: 'new answer', ts: 4000 },
      ]);

      expect(conv.length).toBe(3);
      expect(conv.format()).toContain('◆ autoresearch update');
      expect(conv.format()).toContain('» new question');
      expect(conv.format()).toContain('new answer');
    });

    it('trims to MAX_ENTRIES when replaying large history', () => {
      const entries = Array.from({ length: 120 }, (_, i) => ({
        role: 'user' as const,
        text: `msg-${i}`,
        ts: i * 1000,
      }));

      conv.replayHistory(entries);

      expect(conv.length).toBe(100);
      // Should keep the last 100 entries (indices 20–119)
      expect(conv.format()).toContain('msg-119');
      expect(conv.format()).not.toContain('msg-0');
    });
  });

  // -----------------------------------------------------------------------
  // formatReverse
  // -----------------------------------------------------------------------
  describe('formatReverse', () => {
    it('returns "Ready." for empty conversation', () => {
      expect(conv.formatReverse(500)).toBe('Ready.');
    });

    it('formats a single user entry', () => {
      conv.addUser('Hello');
      expect(conv.formatReverse(500)).toBe('» Hello');
    });

    it('formats a single assistant entry with stripMarkdown', () => {
      conv.addAssistant('World');
      expect(conv.formatReverse(500)).toBe('World');
    });

    it('formats a single system entry', () => {
      conv.addSystem('Connected');
      expect(conv.formatReverse(500)).toBe('[Connected]');
    });

    it('shows empty assistant entry as "..."', () => {
      conv.startAssistantStream();
      expect(conv.formatReverse(500)).toBe('...');
    });

    it('returns entries in reverse order (newest first)', () => {
      conv.addUser('First');
      conv.addAssistant('Second');
      conv.addUser('Third');
      const result = conv.formatReverse(2000);
      const lines = result.split('\n');
      // Newest entry (Third) should appear before oldest (First)
      const thirdIdx = result.indexOf('» Third');
      const firstIdx = result.indexOf('» First');
      expect(thirdIdx).toBeLessThan(firstIdx);
    });

    it('includes separator between entries', () => {
      conv.addUser('A');
      conv.addAssistant('B');
      const result = conv.formatReverse(2000);
      expect(result).toContain('─ ─ ─ ─ ─ ─ ─ ─');
    });

    it('returns full text when under char limit', () => {
      conv.addUser('Hello');
      conv.addAssistant('World');
      const full = conv.formatReverse(2000);
      expect(full).not.toContain('…');
    });

    it('trims oldest entries (end of reversed text) when over budget', () => {
      conv.addUser('Old message that is fairly long');
      conv.addAssistant('Another old response with some detail');
      conv.addUser('Newest question');
      // Use a small char limit so trimming kicks in
      const result = conv.formatReverse(40);
      // Newest entry should be preserved at the start
      expect(result).toContain('» Newest question');
      // Should end with ellipsis marker
      expect(result).toContain('…');
    });

    it('handles single-line overflow gracefully', () => {
      conv.addUser('x'.repeat(100));
      const result = conv.formatReverse(50);
      // Should have ellipsis since it overflows
      expect(result).toContain('…');
    });
  });

  // -----------------------------------------------------------------------
  // setFeedEntries
  // -----------------------------------------------------------------------
  describe('setFeedEntries', () => {
    it('replaces the previous feed entries', () => {
      conv.setFeedEntries([
        { role: 'assistant', text: 'old feed', ts: 1000 },
      ]);
      conv.setFeedEntries([
        { role: 'assistant', text: 'new feed', ts: 2000 },
      ]);

      expect(conv.format()).toContain('new feed');
      expect(conv.format()).not.toContain('old feed');
      expect(conv.length).toBe(1);
    });

    it('interleaves feed entries with local entries by timestamp', () => {
      conv.replayHistory([
        { role: 'user', text: 'local old', ts: 2000 },
        { role: 'assistant', text: 'local new', ts: 4000 },
      ]);
      conv.setFeedEntries([
        { role: 'assistant', text: 'feed old', ts: 1000 },
        { role: 'assistant', text: 'feed new', ts: 3000 },
      ]);

      const entries = conv.getEntries();
      expect(entries.map(entry => entry.text)).toEqual(['feed old', 'local old', 'feed new', 'local new']);
    });

    it('preserves equal-timestamp insertion order across local and feed entries', () => {
      conv.replayHistory([
        { role: 'user', text: 'local first', ts: 1000 },
        { role: 'assistant', text: 'local second', ts: 1000 },
      ]);
      conv.setFeedEntries([
        { role: 'assistant', text: 'feed third', ts: 1000 },
      ]);

      expect(conv.getEntries().map(entry => entry.text)).toEqual([
        'local first',
        'local second',
        'feed third',
      ]);
    });

    it('is idempotent when setting identical feed entries twice', () => {
      const feed = [
        { role: 'assistant', text: 'first update', ts: 1000 },
        { role: 'assistant', text: 'second update', ts: 2000 },
      ];
      conv.setFeedEntries(feed);
      const firstLength = conv.length;
      const firstFormat = conv.formatReverse(1000);

      conv.setFeedEntries(feed);

      expect(conv.length).toBe(firstLength);
      expect(conv.formatReverse(1000)).toBe(firstFormat);
    });

    it('keeps only the last 10 incoming feed entries', () => {
      conv.setFeedEntries(Array.from({ length: 15 }, (_, i) => ({
        role: 'assistant',
        text: `feed-${i}`,
        ts: i,
      })));

      expect(conv.length).toBe(10);
      expect(conv.format()).not.toContain('feed-0');
      expect(conv.format()).not.toContain('feed-4');
      expect(conv.format()).toContain('feed-5');
      expect(conv.format()).toContain('feed-14');
    });

    it('uses the feed prefix only for feed entries in both formats', () => {
      conv.addAssistant('local assistant');
      conv.addUser('local user');
      conv.setFeedEntries([
        { role: 'assistant', text: 'feed assistant', ts: Date.now() + 1 },
      ]);

      expect(conv.format()).toContain('◆ feed assistant');
      expect(conv.format()).toContain('local assistant');
      expect(conv.format()).not.toContain('◆ local assistant');
      expect(conv.formatReverse(1000)).toContain('◆ feed assistant');
      expect(conv.formatReverse(1000)).not.toContain('◆ local assistant');
    });

    it('keeps the newest 100 entries after merging feed entries', () => {
      conv.replayHistory(Array.from({ length: 100 }, (_, i) => ({
        role: 'user' as const,
        text: `local-${i}`,
        ts: i,
      })));
      conv.setFeedEntries(Array.from({ length: 15 }, (_, i) => ({
        role: 'assistant',
        text: `feed-${i}`,
        ts: 100 + i,
      })));

      expect(conv.length).toBe(100);
      expect(conv.format()).not.toContain('local-0');
      expect(conv.format()).toContain('local-10');
      expect(conv.format()).toContain('feed-5');
      expect(conv.format()).toContain('feed-14');
    });

    it('does not append to a trailing feed entry', () => {
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
      conv.setFeedEntries([{ role: 'assistant', text: 'feed response', ts: 1000 }]);

      conv.appendToLastAssistant(' dropped');

      expect(conv.lastEntry?.text).toBe('feed response');
      expect(warn).toHaveBeenCalled();
      warn.mockRestore();
    });
  });

  // -----------------------------------------------------------------------
  // removeLastUser
  // -----------------------------------------------------------------------
  describe('removeLastUser', () => {
    it('removes the last user entry entirely', () => {
      conv.addUser('Hello');
      conv.addAssistant('Hi');
      conv.addUser('Goodbye');
      expect(conv.removeLastUser()).toBe(true);
      expect(conv.format()).not.toContain('Goodbye');
      // Earlier user entry should be untouched
      expect(conv.format()).toContain('» Hello');
      expect(conv.length).toBe(2);
    });

    it('returns false when no user entries exist', () => {
      conv.addAssistant('only assistant');
      expect(conv.removeLastUser()).toBe(false);
    });

    it('returns false on empty conversation', () => {
      expect(conv.removeLastUser()).toBe(false);
    });

    it('removes the only user entry', () => {
      conv.addUser('solo');
      expect(conv.removeLastUser()).toBe(true);
      expect(conv.length).toBe(0);
    });
  });

});
