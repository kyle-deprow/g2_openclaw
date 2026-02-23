import { describe, it, expect } from 'vitest';
import { parseFrame } from '../protocol';

describe('parseFrame', () => {
  // --- Valid inbound frames ---
  it('parses a valid status frame', () => {
    const frame = parseFrame('{"type":"status","status":"idle"}');
    expect(frame).toEqual({ type: 'status', status: 'idle' });
  });

  it('parses a valid transcription frame', () => {
    const frame = parseFrame('{"type":"transcription","text":"hello world"}');
    expect(frame).toEqual({ type: 'transcription', text: 'hello world' });
  });

  it('parses a valid assistant delta frame', () => {
    const frame = parseFrame('{"type":"assistant","delta":"chunk"}');
    expect(frame).toEqual({ type: 'assistant', delta: 'chunk' });
  });

  it('parses a valid end frame', () => {
    const frame = parseFrame('{"type":"end"}');
    expect(frame).toEqual({ type: 'end' });
  });

  it('parses a valid error frame', () => {
    const frame = parseFrame('{"type":"error","detail":"boom","code":"INTERNAL_ERROR"}');
    expect(frame).toEqual({ type: 'error', detail: 'boom', code: 'INTERNAL_ERROR' });
  });

  it('parses a valid connected frame', () => {
    const frame = parseFrame('{"type":"connected","version":"1.0.0"}');
    expect(frame).toEqual({ type: 'connected', version: '1.0.0' });
  });

  it('parses a valid ping frame', () => {
    const frame = parseFrame('{"type":"ping"}');
    expect(frame).toEqual({ type: 'ping' });
  });

  // --- Invalid JSON ---
  it('throws on invalid JSON', () => {
    expect(() => parseFrame('not json')).toThrow('Invalid JSON');
  });

  // --- Missing type ---
  it('throws when type field is missing', () => {
    expect(() => parseFrame('{"status":"idle"}')).toThrow('Frame missing "type" field');
  });

  it('throws when payload is null JSON', () => {
    expect(() => parseFrame('null')).toThrow('Frame missing "type" field');
  });

  it('throws when payload is a JSON array', () => {
    expect(() => parseFrame('[]')).toThrow('Frame missing "type" field');
  });

  // --- Unknown type ---
  it('throws on unknown frame type', () => {
    expect(() => parseFrame('{"type":"unknown"}')).toThrow('Unknown frame type: unknown');
  });

  // --- Missing required fields ---
  it('throws when status frame is missing status field', () => {
    expect(() => parseFrame('{"type":"status"}')).toThrow(
      'Frame type "status" missing required field "status"',
    );
  });

  it('throws when transcription frame is missing text field', () => {
    expect(() => parseFrame('{"type":"transcription"}')).toThrow(
      'Frame type "transcription" missing required field "text"',
    );
  });

  it('throws when assistant frame is missing delta field', () => {
    expect(() => parseFrame('{"type":"assistant"}')).toThrow(
      'Frame type "assistant" missing required field "delta"',
    );
  });

  it('throws when error frame is missing detail field', () => {
    expect(() => parseFrame('{"type":"error","code":"INTERNAL_ERROR"}')).toThrow(
      'Frame type "error" missing required field "detail"',
    );
  });

  it('throws when error frame is missing code field', () => {
    expect(() => parseFrame('{"type":"error","detail":"oops"}')).toThrow(
      'Frame type "error" missing required field "code"',
    );
  });

  it('throws when connected frame is missing version field', () => {
    expect(() => parseFrame('{"type":"connected"}')).toThrow(
      'Frame type "connected" missing required field "version"',
    );
  });

  // --- Security: field type validation ---
  it('throws when status field has wrong type', () => {
    expect(() => parseFrame('{"type":"status","status":123}')).toThrow(
      'Field "status" must be string',
    );
  });

  it('strips __proto__ key (prototype pollution guard)', () => {
    const frame = parseFrame('{"type":"end","__proto__":{"polluted":true}}');
    expect(frame.type).toBe('end');
    expect((frame as any).__proto__).toBe(Object.prototype);
  });

  // --- Extra fields are allowed (forward compatibility) ---
  it('strips extra fields on a status frame (prototype-pollution guard)', () => {
    const frame = parseFrame('{"type":"status","status":"idle","extra":"ok"}');
    expect(frame.type).toBe('status');
    expect((frame as unknown as Record<string, unknown>).extra).toBeUndefined();
  });

  it('allows extra fields on an end frame', () => {
    const frame = parseFrame('{"type":"end","reason":"done"}');
    expect(frame.type).toBe('end');
  });
});
