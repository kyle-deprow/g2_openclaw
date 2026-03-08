import { describe, it, expect, beforeEach } from 'vitest';
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

});
