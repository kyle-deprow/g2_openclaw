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

  // --- Status frame with optional metadata ---
  it('parses status frame with optional metadata fields', () => {
    const frame = parseFrame(
      '{"type":"status","status":"thinking","question":"What is 2+2?","elapsedMs":1234,"phase":"Waiting for OpenClaw"}',
    );
    expect(frame).toEqual({
      type: 'status',
      status: 'thinking',
      question: 'What is 2+2?',
      elapsedMs: 1234,
      phase: 'Waiting for OpenClaw',
    });
  });

  it('parses status frame without metadata (backward compat)', () => {
    const frame = parseFrame('{"type":"status","status":"idle"}');
    expect(frame).toEqual({ type: 'status', status: 'idle' });
    expect((frame as unknown as Record<string, unknown>).question).toBeUndefined();
    expect((frame as unknown as Record<string, unknown>).elapsedMs).toBeUndefined();
    expect((frame as unknown as Record<string, unknown>).phase).toBeUndefined();
  });

  it('throws on wrong-type optional metadata on status frame', () => {
    expect(() => parseFrame(
      '{"type":"status","status":"idle","question":123,"elapsedMs":"slow","phase":true}',
    )).toThrow('Field "question" must be string');
  });

  // --- Session reset frame ---
  it('parses a valid session_reset frame', () => {
    const frame = parseFrame('{"type":"session_reset","reason":"user_request"}');
    expect(frame).toEqual({ type: 'session_reset', reason: 'user_request' });
  });

  it('parses session_reset with daily_reset reason', () => {
    const frame = parseFrame('{"type":"session_reset","reason":"daily_reset"}');
    expect(frame).toEqual({ type: 'session_reset', reason: 'daily_reset' });
  });

  it('throws when session_reset is missing reason', () => {
    expect(() => parseFrame('{"type":"session_reset"}')).toThrow(
      'Frame type "session_reset" missing required field "reason"',
    );
  });

  it('throws when session_reset reason has wrong type', () => {
    expect(() => parseFrame('{"type":"session_reset","reason":123}')).toThrow(
      'Field "reason" must be string',
    );
  });

  // --- History frame ---
  it('parses a valid history frame with entries', () => {
    const frame = parseFrame(JSON.stringify({
      type: 'history',
      entries: [
        { role: 'user', text: 'Hello', ts: 1000 },
        { role: 'assistant', text: 'Hi there', ts: 2000 },
      ],
    }));
    expect(frame).toEqual({
      type: 'history',
      entries: [
        { role: 'user', text: 'Hello', ts: 1000 },
        { role: 'assistant', text: 'Hi there', ts: 2000 },
      ],
    });
  });

  it('parses a history frame with empty entries array', () => {
    const frame = parseFrame('{"type":"history","entries":[]}');
    expect(frame).toEqual({ type: 'history', entries: [] });
  });

  it('throws when history frame is missing entries field', () => {
    expect(() => parseFrame('{"type":"history"}')).toThrow(
      'Frame type "history" missing required field "entries"',
    );
  });

  it('throws when history.entries is a string', () => {
    expect(() => parseFrame('{"type":"history","entries":"not-array"}')).toThrow(
      'Field "entries" must be object',
    );
  });

  it('throws when history.entries is a plain object (not an array)', () => {
    expect(() => parseFrame('{"type":"history","entries":{"0":"bad"}}')).toThrow(
      'history.entries must be an array',
    );
  });

  // --- Connected frame with optional session fields ---
  it('parses connected frame with optional sessionId/sessionKey/sessionStartedAt', () => {
    const frame = parseFrame(JSON.stringify({
      type: 'connected',
      version: '2.1.0',
      sessionId: 'sess-abc123',
      sessionKey: 'key-xyz',
      sessionStartedAt: '2026-03-07T10:00:00Z',
    }));
    expect(frame).toEqual({
      type: 'connected',
      version: '2.1.0',
      sessionId: 'sess-abc123',
      sessionKey: 'key-xyz',
      sessionStartedAt: '2026-03-07T10:00:00Z',
    });
  });

  it('parses connected frame without optional fields (backward compat)', () => {
    const frame = parseFrame('{"type":"connected","version":"1.0.0"}');
    expect(frame).toEqual({ type: 'connected', version: '1.0.0' });
    expect((frame as unknown as Record<string, unknown>).sessionId).toBeUndefined();
    expect((frame as unknown as Record<string, unknown>).sessionKey).toBeUndefined();
    expect((frame as unknown as Record<string, unknown>).sessionStartedAt).toBeUndefined();
  });

  it('throws on optional connected fields with wrong type', () => {
    expect(() => parseFrame(
      '{"type":"connected","version":"1.0.0","sessionId":123,"sessionKey":true}',
    )).toThrow('Field "sessionId" must be string');
  });

  // --- Status frame with optional question/elapsedMs/phase ---
  it('parses status frame with question and elapsedMs', () => {
    const frame = parseFrame(JSON.stringify({
      type: 'status',
      status: 'thinking',
      question: 'What time is it?',
      elapsedMs: 500,
    }));
    expect(frame).toEqual({
      type: 'status',
      status: 'thinking',
      question: 'What time is it?',
      elapsedMs: 500,
    });
  });

  it('parses status frame with phase only', () => {
    const frame = parseFrame(JSON.stringify({
      type: 'status',
      status: 'streaming',
      phase: 'Generating response',
    }));
    expect(frame).toEqual({
      type: 'status',
      status: 'streaming',
      phase: 'Generating response',
    });
  });

  // --- Outbound-only type rejection ---
  it('rejects text as inbound frame type', () => {
    expect(() => parseFrame('{"type":"text","message":"hi"}')).toThrow(
      'Unknown frame type: text',
    );
  });

  it('rejects pong as inbound frame type', () => {
    expect(() => parseFrame('{"type":"pong"}')).toThrow(
      'Unknown frame type: pong',
    );
  });

  it('rejects start_audio as inbound frame type', () => {
    expect(() => parseFrame('{"type":"start_audio","sampleRate":16000,"channels":1,"sampleWidth":2}')).toThrow(
      'Unknown frame type: start_audio',
    );
  });

  it('rejects reset_session as inbound frame type', () => {
    expect(() => parseFrame('{"type":"reset_session"}')).toThrow(
      'Unknown frame type: reset_session',
    );
  });

  // --- Session list frame ---
  it('parses a valid session_list frame', () => {
    const frame = parseFrame(JSON.stringify({
      type: 'session_list',
      sessions: [
        { sessionKey: 'key1', sessionId: 'id1', preview: 'First session', updatedAt: '2026-03-07T10:00:00Z', messageCount: 5, label: 'First session', isActive: true },
        { sessionKey: 'key2', sessionId: 'id2', preview: 'Second session', updatedAt: '2026-03-07T09:00:00Z', messageCount: 3, label: 'Second session', isActive: false },
      ],
      activeSessionKey: 'key1',
    }));
    expect(frame.type).toBe('session_list');
    const sl = frame as any;
    expect(sl.sessions).toHaveLength(2);
    expect(sl.sessions[0].sessionKey).toBe('key1');
    expect(sl.sessions[0].isActive).toBe(true);
    expect(sl.sessions[0].preview).toBe('First session');
    expect(sl.sessions[0].messageCount).toBe(5);
    expect(sl.sessions[0].label).toBe('First session');
    expect(sl.sessions[1].isActive).toBe(false);
  });

  it('parses session_list with empty sessions array', () => {
    const frame = parseFrame('{"type":"session_list","sessions":[],"activeSessionKey":""}');
    expect(frame).toEqual({ type: 'session_list', sessions: [], activeSessionKey: '' });
  });

  it('throws when session_list is missing sessions field', () => {
    expect(() => parseFrame('{"type":"session_list","activeSessionKey":"k"}')).toThrow(
      'Frame type "session_list" missing required field "sessions"',
    );
  });

  it('throws when session_list is missing activeSessionKey field', () => {
    expect(() => parseFrame('{"type":"session_list","sessions":[]}')).toThrow(
      'Frame type "session_list" missing required field "activeSessionKey"',
    );
  });

  it('throws when session_list.sessions is not an array', () => {
    expect(() => parseFrame('{"type":"session_list","sessions":"bad","activeSessionKey":""}')).toThrow(
      'Field "sessions" must be object',
    );
  });

  it('throws when session_list.sessions is a plain object', () => {
    expect(() => parseFrame('{"type":"session_list","sessions":{"0":"bad"},"activeSessionKey":""}')).toThrow(
      'session_list.sessions must be an array',
    );
  });

  it('throws on malformed session entries', () => {
    expect(() => parseFrame(JSON.stringify({
      type: 'session_list',
      sessions: [
        { sessionKey: 'key1', sessionId: 'id1', updatedAt: '2026-03-07T10:00:00Z', preview: 'Good', messageCount: 1, label: 'Good', isActive: false },
        { sessionKey: 123, updatedAt: '2026-03-07T09:00:00Z', preview: 'Bad key', messageCount: 0 },
      ],
      activeSessionKey: '',
    }))).toThrow('session_list.sessions[1].sessionKey must be string');
  });

  it('uses activeSessionKey to prevent stale active flags', () => {
    const frame = parseFrame(JSON.stringify({
      type: 'session_list',
      sessions: [
        { sessionKey: 'key1', sessionId: 'id1', preview: 'Test', updatedAt: '2026-03-07T10:00:00Z', messageCount: 0, label: 'Test', isActive: true },
      ],
      activeSessionKey: 'other-key',
    }));
    const sl = frame as any;
    expect(sl.sessions[0].isActive).toBe(false);
  });

  it('preserves server-provided label', () => {
    const frame = parseFrame(JSON.stringify({
      type: 'session_list',
      sessions: [
        { sessionKey: 'agent:claw:g2:abc', sessionId: 'id1', preview: 'My Chat', updatedAt: '2026-03-07T10:00:00Z', messageCount: 2, label: 'My Chat', isActive: false },
      ],
      activeSessionKey: '',
    }));
    const sl = frame as any;
    expect(sl.sessions[0].label).toBe('My Chat');
  });

  it('accepts empty preview when label is provided', () => {
    const frame = parseFrame(JSON.stringify({
      type: 'session_list',
      sessions: [
        { sessionKey: 'agent:claw:g2:abc', sessionId: 'id1', preview: '', updatedAt: '2026-03-07T10:00:00Z', messageCount: 0, label: 'abc', isActive: false },
      ],
      activeSessionKey: '',
    }));
    const sl = frame as any;
    expect(sl.sessions[0].label).toBe('abc');
  });

  it('throws when messageCount is missing', () => {
    expect(() => parseFrame(JSON.stringify({
      type: 'session_list',
      sessions: [
        { sessionKey: 'key1', sessionId: 'id1', preview: 'Test', updatedAt: '2026-03-07T10:00:00Z' },
      ],
      activeSessionKey: '',
    }))).toThrow('session_list.sessions[0].messageCount must be number');
  });

  // --- Session switched frame ---
  it('parses a valid session_switched frame with sessionId', () => {
    const frame = parseFrame(JSON.stringify({
      type: 'session_switched',
      sessionId: 'sess-123',
      sessionKey: 'key-abc',
    }));
    expect(frame).toEqual({ type: 'session_switched', sessionId: 'sess-123', sessionKey: 'key-abc' });
  });

  it('parses session_switched without optional sessionId', () => {
    const frame = parseFrame(JSON.stringify({
      type: 'session_switched',
      sessionKey: 'key-abc',
    }));
    expect(frame).toEqual({ type: 'session_switched', sessionKey: 'key-abc' });
  });

  it('parses session_switched with sessionStartedAt', () => {
    const frame = parseFrame(JSON.stringify({
      type: 'session_switched',
      sessionKey: 'key-abc',
      sessionId: 'sess-999',
      sessionStartedAt: '2026-03-07T10:00:00Z',
    }));
    expect(frame).toEqual({
      type: 'session_switched',
      sessionKey: 'key-abc',
      sessionId: 'sess-999',
      sessionStartedAt: '2026-03-07T10:00:00Z',
    });
  });

  it('throws when session_switched is missing sessionKey', () => {
    expect(() => parseFrame('{"type":"session_switched","sessionId":"id1"}')).toThrow(
      'Frame type "session_switched" missing required field "sessionKey"',
    );
  });

  it('throws when sessionId has wrong type on session_switched', () => {
    expect(() => parseFrame('{"type":"session_switched","sessionKey":"k","sessionId":123}')).toThrow(
      'Field "sessionId" must be string',
    );
  });
});
