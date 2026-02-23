/**
 * Strip common markdown formatting for plain-text display on G2 glasses.
 * Handles: bold, italic, inline code, links, headings, code fences, blockquotes.
 */
export function stripMarkdown(text: string): string {
  let result = text;
  // Code fences: ```lang\ncode\n``` → code
  result = result.replace(/```[\s\S]*?\n([\s\S]*?)```/g, '$1');
  // Inline code: `code` → code
  result = result.replace(/`([^`]+)`/g, '$1');
  // Bold+italic: ***text*** or ___text___
  result = result.replace(/\*{3}(.+?)\*{3}/g, '$1');
  result = result.replace(/_{3}(.+?)_{3}/g, '$1');
  // Bold: **text** or __text__
  result = result.replace(/\*{2}(.+?)\*{2}/g, '$1');
  result = result.replace(/_{2}(.+?)_{2}/g, '$1');
  // Italic: *text* or _text_
  result = result.replace(/\*(.+?)\*/g, '$1');
  result = result.replace(/_(.+?)_/g, '$1');
  // Strikethrough: ~~text~~
  result = result.replace(/~~(.+?)~~/g, '$1');
  // Links: [text](url) → text (images first since ![alt](url) contains [alt](url))
  result = result.replace(/!\[([^\]]*)\]\([^)]+\)/g, '$1');
  result = result.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
  // Headings: # heading → heading (all levels)
  result = result.replace(/^#{1,6}\s+/gm, '');
  // Blockquotes: > text → text
  result = result.replace(/^>\s?/gm, '');
  // Horizontal rules: --- or *** or ___
  result = result.replace(/^[-*_]{3,}\s*$/gm, '');
  // Unordered list markers: - item or * item → item
  result = result.replace(/^[\s]*[-*+]\s+/gm, '');
  // Ordered list markers: 1. item → item
  result = result.replace(/^[\s]*\d+\.\s+/gm, '');
  return result.trim();
}
