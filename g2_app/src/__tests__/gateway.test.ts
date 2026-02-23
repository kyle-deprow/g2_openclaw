import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { Gateway, type GatewayEvent } from '../gateway';
import type { InboundFrame } from '../protocol';

// ---------------------------------------------------------------------------
// Minimal MockWebSocket
// ---------------------------------------------------------------------------
type WSListener = ((ev: unknown) => void) | null;

class MockWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  readonly CONNECTING = 0;
  readonly OPEN = 1;
  readonly CLOSING = 2;
  readonly CLOSED = 3;

  readyState: number = MockWebSocket.CONNECTING;
  binaryType: string = '';
  url: string;

  onopen: WSListener = null;
  onmessage: WSListener = null;
  onclose: WSListener = null;
  onerror: WSListener = null;

  sent: unknown[] = [];
  closed = false;

  constructor(url: string) {
    this.url = url;
    // Capture the latest instance for test access
    MockWebSocket.last = this;
  }

  send(data: unknown): void {
    this.sent.push(data);
  }

  close(): void {
    this.closed = true;
    this.readyState = MockWebSocket.CLOSED;
  }

  // --- Test helpers ---
  static last: MockWebSocket | null = null;

  /** Simulate server opening the connection */
  simulateOpen(): void {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.({});
  }

  /** Simulate receiving a text message */
  simulateMessage(data: string): void {
    this.onmessage?.({ data } as unknown);
  }

  /** Simulate receiving a binary message */
  simulateBinaryMessage(data: ArrayBuffer): void {
    this.onmessage?.({ data } as unknown);
  }

  /** Simulate connection close */
  simulateClose(): void {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.({});
  }
}

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------
let gateway: Gateway;
let mockLocalStorage: {
  getItem: ReturnType<typeof vi.fn>;
  setItem: ReturnType<typeof vi.fn>;
  removeItem: ReturnType<typeof vi.fn>;
  store: Record<string, string>;
};

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('WebSocket', MockWebSocket);
  MockWebSocket.last = null;

  const store: Record<string, string> = {};
  mockLocalStorage = {
    store,
    getItem: vi.fn((k: string) => store[k] ?? null),
    setItem: vi.fn((k: string, v: string) => { store[k] = v; }),
    removeItem: vi.fn((k: string) => { delete store[k]; }),
  };

  vi.stubGlobal('window', {
    location: { hash: '', search: '' },
    localStorage: mockLocalStorage,
  });
  vi.stubGlobal('localStorage', mockLocalStorage);

  gateway = new Gateway();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
describe('Gateway', () => {
  describe('resolveUrl()', () => {
    it('returns fallback when no sources available', () => {
      expect(gateway.resolveUrl()).toBe('ws://localhost:8765');
    });

    it('prefers hash over everything', () => {
      window.location.hash = '#ws://hash:1234';
      expect(gateway.resolveUrl()).toBe('ws://hash:1234');
    });

    it('prefers query param over localStorage', () => {
      window.location.search = '?gateway=ws://query:5678';
      expect(gateway.resolveUrl()).toBe('ws://query:5678');
    });

    it('uses localStorage when no hash/query', () => {
      mockLocalStorage.getItem.mockReturnValue('ws://stored:9999');
      expect(gateway.resolveUrl()).toBe('ws://stored:9999');
    });
  });

  describe('connect + onopen emits connected', () => {
    it('emits connected event on ws open', () => {
      const events: GatewayEvent[] = [];
      gateway.onEvent((e) => events.push(e));
      gateway.connect('ws://test:1234');

      MockWebSocket.last!.simulateOpen();
      expect(events).toContain('connected');
    });
  });

  describe('sendJson', () => {
    it('sends JSON string when connected', () => {
      gateway.connect('ws://test:1234');
      const ws = MockWebSocket.last!;
      ws.simulateOpen();

      gateway.sendJson({ type: 'pong' });
      expect(ws.sent).toContain('{"type":"pong"}');
    });

    it('is no-op when disconnected', () => {
      gateway.connect('ws://test:1234');
      // Don't open the socket — readyState stays CONNECTING
      gateway.sendJson({ type: 'pong' });
      expect(MockWebSocket.last!.sent).toHaveLength(0);
    });
  });

  describe('disconnect()', () => {
    it('clears reconnect timer and closes ws', () => {
      gateway.connect('ws://test:1234');
      const ws = MockWebSocket.last!;
      ws.simulateOpen();

      gateway.disconnect();
      expect(ws.closed).toBe(true);
      expect(gateway.isConnected).toBe(false);
    });

    it('prevents reconnect after intentional close', () => {
      const events: GatewayEvent[] = [];
      gateway.onEvent((e) => events.push(e));
      gateway.connect('ws://test:1234');
      const ws = MockWebSocket.last!;
      ws.simulateOpen();

      gateway.disconnect();
      // simulate close firing after disconnect
      ws.simulateClose();
      // Should NOT have scheduled reconnect
      expect(events).not.toContain('reconnecting');
    });
  });

  describe('onMessage callbacks receive parsed frames', () => {
    it('delivers parsed frames to callbacks', () => {
      const frames: InboundFrame[] = [];
      gateway.onMessage((f) => frames.push(f));
      gateway.connect('ws://test:1234');
      MockWebSocket.last!.simulateOpen();

      MockWebSocket.last!.simulateMessage('{"type":"status","status":"idle"}');
      expect(frames).toHaveLength(1);
      expect(frames[0]).toEqual({ type: 'status', status: 'idle' });
    });

    it('auto-replies pong to ping and does not forward to callbacks', () => {
      const frames: InboundFrame[] = [];
      gateway.onMessage((f) => frames.push(f));
      gateway.connect('ws://test:1234');
      const ws = MockWebSocket.last!;
      ws.simulateOpen();

      ws.simulateMessage('{"type":"ping"}');
      expect(frames).toHaveLength(0);
      expect(ws.sent).toContain('{"type":"pong"}');
    });
  });

  describe('onEvent callbacks receive lifecycle events', () => {
    it('emits disconnected and reconnecting on unexpected close', () => {
      const events: GatewayEvent[] = [];
      gateway.onEvent((e) => events.push(e));
      gateway.connect('ws://test:1234');
      MockWebSocket.last!.simulateOpen();

      MockWebSocket.last!.simulateClose();
      expect(events).toContain('disconnected');
      expect(events).toContain('reconnecting');
    });
  });

  describe('binary message handling', () => {
    it('warns on binary message', () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      gateway.connect('ws://test:1234');
      MockWebSocket.last!.simulateOpen();

      MockWebSocket.last!.simulateBinaryMessage(new ArrayBuffer(8));
      expect(warnSpy).toHaveBeenCalledWith('[Gateway] Binary frame received — ignoring');
    });
  });
});
