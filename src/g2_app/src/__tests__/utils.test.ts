import { describe, it, expect } from 'vitest';
import { stripMarkdown } from '../utils';

describe('stripMarkdown', () => {
  it('strips bold', () => {
    expect(stripMarkdown('**bold**')).toBe('bold');
    expect(stripMarkdown('__bold__')).toBe('bold');
  });
  it('strips italic', () => {
    expect(stripMarkdown('*italic*')).toBe('italic');
    expect(stripMarkdown('_italic_')).toBe('italic');
  });
  it('strips bold+italic', () => {
    expect(stripMarkdown('***both***')).toBe('both');
  });
  it('strips inline code', () => {
    expect(stripMarkdown('`code`')).toBe('code');
  });
  it('strips code fences', () => {
    expect(stripMarkdown('```js\nconsole.log("hi")\n```')).toBe('console.log("hi")');
  });
  it('strips links', () => {
    expect(stripMarkdown('[text](http://example.com)')).toBe('text');
  });
  it('strips images', () => {
    expect(stripMarkdown('![alt](http://img.png)')).toBe('alt');
  });
  it('strips headings', () => {
    expect(stripMarkdown('# Heading 1')).toBe('Heading 1');
    expect(stripMarkdown('## Heading 2')).toBe('Heading 2');
    expect(stripMarkdown('### Heading 3')).toBe('Heading 3');
  });
  it('strips blockquotes', () => {
    expect(stripMarkdown('> quoted text')).toBe('quoted text');
  });
  it('strips strikethrough', () => {
    expect(stripMarkdown('~~struck~~')).toBe('struck');
  });
  it('preserves normal text', () => {
    expect(stripMarkdown('Hello, world!')).toBe('Hello, world!');
  });
  it('preserves URLs in plain text', () => {
    expect(stripMarkdown('Visit http://example.com today')).toBe('Visit http://example.com today');
  });
  it('handles mixed markdown', () => {
    const input = '# Title\n\n**Bold** and *italic* with `code` and [link](url)';
    const expected = 'Title\n\nBold and italic with code and link';
    expect(stripMarkdown(input)).toBe(expected);
  });
});
