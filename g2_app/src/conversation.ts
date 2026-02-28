/**
 * conversation.ts — Persistent conversation transcript model.
 *
 * Stores all user/assistant exchanges within a session so the G2 display
 * can render a scrollable chat-style transcript.
 */

import { stripMarkdown } from './utils';

export interface ConversationEntry {
  role: 'user' | 'assistant' | 'system';
  text: string;
  timestamp: number;
}

const SEPARATOR = '\n─ ─ ─ ─ ─ ─ ─ ─\n';

export class ConversationHistory {
  private entries: ConversationEntry[] = [];
  private static readonly MAX_ENTRIES = 100;

  /** Add a user message. */
  addUser(text: string): void {
    this.entries.push({ role: 'user', text, timestamp: Date.now() });
    this._trim();
  }

  /** Begin a new empty assistant entry (called when streaming starts). */
  startAssistantStream(): void {
    this.entries.push({ role: 'assistant', text: '', timestamp: Date.now() });
  }

  /** Append a delta to the most recent assistant entry. */
  appendToLastAssistant(delta: string): void {
    const last = this.entries[this.entries.length - 1];
    if (last && last.role === 'assistant') {
      last.text += delta;
    } else {
      console.warn('[Conversation] appendToLastAssistant: no assistant entry found — dropping delta');
    }
  }

  /** Add a complete assistant response (non-streaming path). */
  addAssistant(text: string): void {
    this.entries.push({ role: 'assistant', text, timestamp: Date.now() });
    this._trim();
  }

  /** Add a system/status message (errors, connection status, etc.). */
  addSystem(text: string): void {
    this.entries.push({ role: 'system', text, timestamp: Date.now() });
    this._trim();
  }

  /** Trim entries to cap if exceeded. */
  private _trim(): void {
    if (this.entries.length > ConversationHistory.MAX_ENTRIES) {
      this.entries = this.entries.slice(-ConversationHistory.MAX_ENTRIES);
    }
  }

  /** Format the entire transcript for display. */
  format(): string {
    if (this.entries.length === 0) {
      return 'Ready.\n\nTap ring to ask anything.';
    }
    return this.entries.map((entry) => {
      switch (entry.role) {
        case 'user':
          return `» ${entry.text}`;
        case 'assistant':
          return stripMarkdown(entry.text) || '...';
        case 'system':
          return `[${entry.text}]`;
      }
    }).join(SEPARATOR);
  }

  /**
   * Format the tail of the transcript that fits within a character budget.
   * Works backwards so the most recent messages are always visible.
   */
  formatTail(charLimit: number): string {
    const full = this.format();
    if (full.length <= charLimit) return full;
    const tail = full.slice(-charLimit);
    const firstNewline = tail.indexOf('\n');
    if (firstNewline <= 0) return '…' + tail;
    let start = firstNewline + 1;
    // Skip past any separator characters at the boundary
    while (start < tail.length && '─ \n'.includes(tail[start])) start++;
    return '…\n' + tail.slice(start);
  }

  /** Get the formatted text for the last assistant entry only. */
  get lastAssistantText(): string {
    const last = this.entries[this.entries.length - 1];
    return last && last.role === 'assistant' ? stripMarkdown(last.text) : '';
  }

  /** Get the text of the last user entry. */
  get lastUserText(): string {
    for (let i = this.entries.length - 1; i >= 0; i--) {
      if (this.entries[i].role === 'user') return this.entries[i].text;
    }
    return '';
  }

  get length(): number {
    return this.entries.length;
  }

  get lastEntry(): ConversationEntry | undefined {
    return this.entries[this.entries.length - 1];
  }

  clear(): void {
    this.entries = [];
  }
}
