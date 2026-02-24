/// <reference types="vite/client" />
import { parseFrame, type InboundFrame, type OutboundFrame } from './protocol';

export type GatewayEvent = 'connected' | 'disconnected' | 'reconnecting';
type FrameCallback = (frame: InboundFrame) => void;
type EventCallback = (event: GatewayEvent) => void;

const MAX_BACKOFF = 30_000;
const INITIAL_BACKOFF = 1_000;

/** Check if hostname is a literal private IP or localhost */
function isPrivateHost(hostname: string): boolean {
  if (hostname === 'localhost' || hostname === '::1') return true;
  const parts = hostname.split('.');
  if (parts.length !== 4 || parts.some(p => !/^\d{1,3}$/.test(p))) return false;
  const [a, b] = parts.map(Number);
  if (a === 127 || a === 10) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 192 && b === 168) return true;
  return false;
}

/** Validate that a gateway URL targets a plausible host */
function isAllowedUrl(url: string): boolean {
  try {
    const parsed = new URL(url);
    const proto = parsed.protocol;
    if (proto !== 'ws:' && proto !== 'wss:') return false;
    // Allow ws:// only for localhost/private IPs; require wss:// for public hosts
    if (proto === 'ws:' && !isPrivateHost(parsed.hostname)) return false;
    return true;
  } catch {
    return false;
  }
}

/** Strip query parameters (e.g., ?token=...) from a URL string */
function stripQueryParams(url: string): string {
  try {
    const parsed = new URL(url);
    parsed.search = '';
    return parsed.toString();
  } catch {
    return url;
  }
}

/** Extract token from URL query params, if present */
function extractToken(url: string): string | undefined {
  try {
    const parsed = new URL(url);
    return parsed.searchParams.get('token') ?? undefined;
  } catch {
    return undefined;
  }
}

/** Remove token from URL query params, returning clean URL */
function stripToken(url: string): string {
  try {
    const parsed = new URL(url);
    parsed.searchParams.delete('token');
    return parsed.toString();
  } catch {
    return url;
  }
}

export class Gateway {
  private ws: WebSocket | null = null;
  private url: string = '';
  private frameCallbacks: FrameCallback[] = [];
  private eventCallbacks: EventCallback[] = [];
  private backoff = INITIAL_BACKOFF;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private intentionalClose = false;
  private token: string | undefined;

  /** Resolve Gateway URL from hash > query > localStorage > env */
  resolveUrl(): string {
    // Hash: #ws://192.168.1.100:8765?token=abc
    const hash = window.location.hash.slice(1);
    if (hash && hash.startsWith('ws') && isAllowedUrl(hash)) {
      // Clear sensitive URL fragment from browser history
      window.history?.replaceState(null, '', window.location.pathname);
      return hash;
    }

    // Query: ?gateway=ws://...
    const params = new URLSearchParams(window.location.search);
    const qGateway = params.get('gateway');
    if (qGateway && isAllowedUrl(qGateway)) {
      // Clear sensitive URL params from browser history
      window.history?.replaceState(null, '', window.location.pathname);
      return qGateway;
    }

    // localStorage
    const stored = localStorage.getItem('g2_gateway_url');
    if (stored && isAllowedUrl(stored)) return stored;

    // Build-time env
    const envUrl = import.meta.env?.VITE_GATEWAY_URL;
    if (envUrl && isAllowedUrl(envUrl)) return envUrl;

    // Fallback
    return 'ws://localhost:8765';
  }

  connect(url?: string): void {
    const resolved = url ?? this.resolveUrl();
    this.token = extractToken(resolved);
    this.url = stripToken(resolved);
    this.intentionalClose = false;
    this._connect();
  }

  private _connect(): void {
    try {
      this.ws = new WebSocket(this.url);
    } catch (e) {
      console.error('WebSocket creation failed:', e);
      this._scheduleReconnect();
      return;
    }

    this.ws.binaryType = 'arraybuffer';

    this.ws.onopen = () => {
      console.log('[Gateway] Connected');
      this.backoff = INITIAL_BACKOFF;
      // Send auth handshake if token available
      if (this.token) {
        this.ws!.send(JSON.stringify({ type: 'auth', token: this.token }));
      }
      // Persist URL WITHOUT credentials
      try { localStorage.setItem('g2_gateway_url', stripQueryParams(this.url)); } catch { /* noop */ }
      this._emit('connected');
    };

    this.ws.onmessage = (event: MessageEvent) => {
      if (typeof event.data === 'string') {
        try {
          const frame = parseFrame(event.data);
          // Respond to ping immediately
          if (frame.type === 'ping') {
            this.sendJson({ type: 'pong' });
            return;
          }
          for (const cb of this.frameCallbacks) cb(frame);
        } catch (e) {
          console.error('[Gateway] Frame parse error:', e);
        }
      } else {
        console.warn('[Gateway] Binary frame received — ignoring');
      }
    };

    this.ws.onclose = () => {
      this.ws = null;
      if (!this.intentionalClose) {
        this._emit('disconnected');
        this._scheduleReconnect();
      }
    };

    this.ws.onerror = (e) => {
      console.error('[Gateway] WebSocket error:', e);
      // onclose will fire after onerror
    };
  }

  private _scheduleReconnect(): void {
    this._emit('reconnecting');
    const jitter = this.backoff * (0.8 + Math.random() * 0.4); // ±20%
    console.log(`[Gateway] Reconnecting in ${Math.round(jitter)}ms`);
    this.reconnectTimer = setTimeout(() => {
      this.backoff = Math.min(this.backoff * 2, MAX_BACKOFF);
      this._connect();
    }, jitter);
  }

  /** Send raw binary data (PCM audio chunks) */
  send(data: ArrayBuffer): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(data);
    }
  }

  /** Send a JSON frame */
  sendJson(frame: OutboundFrame): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(frame));
    }
  }

  onMessage(cb: FrameCallback): void {
    this.frameCallbacks.push(cb);
  }

  onEvent(cb: EventCallback): void {
    this.eventCallbacks.push(cb);
  }

  private _emit(event: GatewayEvent): void {
    for (const cb of this.eventCallbacks) cb(event);
  }

  disconnect(): void {
    this.intentionalClose = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
  }

  get isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }
}
