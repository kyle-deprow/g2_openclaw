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

let savedViteGatewayUrl: string | undefined;
const TEST_URL = 'ws://127.0.0.1:1234?token=test-token';

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('WebSocket', MockWebSocket);
  MockWebSocket.last = null;

  // Clear build-time env so .env.local doesn't override test expectations
  savedViteGatewayUrl = import.meta.env?.VITE_GATEWAY_URL;
  import.meta.env.VITE_GATEWAY_URL = '';

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
  // Restore build-time env
  if (savedViteGatewayUrl !== undefined) {
    import.meta.env.VITE_GATEWAY_URL = savedViteGatewayUrl;
  }
  vi.restoreAllMocks();
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
describe('Gateway', () => {
  describe('resolveUrl()', () => {
    it('throws when no sources are available', () => {
      expect(() => gateway.resolveUrl()).toThrow('Gateway URL is not configured');
    });

    it('prefers hash over everything', () => {
      window.location.hash = '#ws://192.168.1.100:1234?token=abc';
      expect(gateway.resolveUrl()).toBe('ws://192.168.1.100:1234?token=abc');
    });

    it('prefers query param over localStorage', () => {
      window.location.search = '?gateway=ws://10.0.0.5:5678?token=abc';
      expect(gateway.resolveUrl()).toBe('ws://10.0.0.5:5678?token=abc');
    });

    it('uses localStorage when no hash/query', () => {
      mockLocalStorage.getItem.mockReturnValue('ws://127.0.0.1:9999');
      expect(gateway.resolveUrl()).toBe('ws://127.0.0.1:9999');
    });
  });

  describe('URL security', () => {
    it('rejects ws:// to public hostnames', () => {
      window.location.hash = '#ws://evil.com:8765';
      expect(() => gateway.resolveUrl()).toThrow('Gateway URL is not configured');
    });

    it('rejects ws:// to DNS names starting with private IP prefix', () => {
      window.location.hash = '#ws://10.evil.com:8765';
      expect(() => gateway.resolveUrl()).toThrow('Gateway URL is not configured');
    });

    it('accepts wss:// to public hostnames', () => {
      window.location.hash = '#wss://api.example.com:443';
      expect(gateway.resolveUrl()).toBe('wss://api.example.com:443');
    });

    it('rejects non-ws protocols', () => {
      window.location.hash = '#http://localhost:8765';
      expect(() => gateway.resolveUrl()).toThrow('Gateway URL is not configured');
    });

    it('accepts ws:// to Tailscale CGNAT IPs (100.64-127.x.x)', () => {
      window.location.hash = '#ws://100.100.50.1:8765';
      expect(gateway.resolveUrl()).toBe('ws://100.100.50.1:8765');
    });

    it('accepts ws:// to Tailscale lower-bound IP (100.64.0.1)', () => {
      window.location.hash = '#ws://100.64.0.1:8765';
      expect(gateway.resolveUrl()).toBe('ws://100.64.0.1:8765');
    });

    it('rejects ws:// to non-Tailscale 100.x IPs outside CGNAT range', () => {
      window.location.hash = '#ws://100.63.0.1:8765';
      expect(() => gateway.resolveUrl()).toThrow('Gateway URL is not configured');
    });

    it('rejects ws:// to 100.128.x.x (above CGNAT range)', () => {
      window.location.hash = '#ws://100.128.0.1:8765';
      expect(() => gateway.resolveUrl()).toThrow('Gateway URL is not configured');
    });
  });

  describe('connect + onopen emits connected', () => {
    it('emits connected event on ws open', () => {
      const events: GatewayEvent[] = [];
      gateway.onEvent((e) => events.push(e));
      gateway.connect(TEST_URL);

      MockWebSocket.last!.simulateOpen();
      expect(events).toContain('connected');
    });
  });

  describe('sendJson', () => {
    it('sends JSON string when connected', () => {
      gateway.connect(TEST_URL);
      const ws = MockWebSocket.last!;
      ws.simulateOpen();

      gateway.sendJson({ type: 'pong' });
      expect(ws.sent).toContain('{"type":"pong"}');
    });

    it('is no-op when disconnected', () => {
      gateway.connect(TEST_URL);
      // Don't open the socket — readyState stays CONNECTING
      gateway.sendJson({ type: 'pong' });
      expect(MockWebSocket.last!.sent).toHaveLength(0);
    });
  });

  describe('disconnect()', () => {
    it('clears reconnect timer and closes ws', () => {
      gateway.connect(TEST_URL);
      const ws = MockWebSocket.last!;
      ws.simulateOpen();

      gateway.disconnect();
      expect(ws.closed).toBe(true);
      expect(gateway.isConnected).toBe(false);
    });

    it('prevents reconnect after intentional close', () => {
      const events: GatewayEvent[] = [];
      gateway.onEvent((e) => events.push(e));
      gateway.connect(TEST_URL);
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
      gateway.connect(TEST_URL);
      MockWebSocket.last!.simulateOpen();

      MockWebSocket.last!.simulateMessage('{"type":"status","status":"idle"}');
      expect(frames).toHaveLength(1);
      expect(frames[0]).toEqual({ type: 'status', status: 'idle' });
    });

    it('auto-replies pong to ping and does not forward to callbacks', () => {
      const frames: InboundFrame[] = [];
      gateway.onMessage((f) => frames.push(f));
      gateway.connect(TEST_URL);
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
      gateway.connect(TEST_URL);
      MockWebSocket.last!.simulateOpen();

      MockWebSocket.last!.simulateClose();
      expect(events).toContain('disconnected');
      expect(events).toContain('reconnecting');
    });
  });

  describe('binary message handling', () => {
    it('warns on binary message', () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      gateway.connect(TEST_URL);
      MockWebSocket.last!.simulateOpen();

      MockWebSocket.last!.simulateBinaryMessage(new ArrayBuffer(8));
      expect(warnSpy).toHaveBeenCalledWith('[Gateway] Binary frame received — ignoring');
    });
  });

  describe('control methods', () => {
    it('sendForceStop sends the force_stop frame', () => {
      gateway.connect(TEST_URL);
      MockWebSocket.last!.simulateOpen();
      gateway.sendForceStop();
      expect(MockWebSocket.last!.sent).toContain('{"type":"force_stop"}');
    });

    it('requestStatus sends the status_request frame', () => {
      gateway.connect(TEST_URL);
      MockWebSocket.last!.simulateOpen();
      gateway.requestStatus();
      expect(MockWebSocket.last!.sent).toContain('{"type":"status_request"}');
    });
  });
});
