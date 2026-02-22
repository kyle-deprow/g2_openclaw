/// <reference types="vite/client" />
import { parseFrame, type InboundFrame, type OutboundFrame } from './protocol';

export type GatewayEvent = 'connected' | 'disconnected' | 'reconnecting';
type FrameCallback = (frame: InboundFrame) => void;
type EventCallback = (event: GatewayEvent) => void;

const MAX_BACKOFF = 30_000;
const INITIAL_BACKOFF = 1_000;

export class Gateway {
  private ws: WebSocket | null = null;
  private url: string = '';
  private frameCallbacks: FrameCallback[] = [];
  private eventCallbacks: EventCallback[] = [];
  private backoff = INITIAL_BACKOFF;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private intentionalClose = false;

  /** Resolve Gateway URL from hash > query > localStorage > env */
  resolveUrl(): string {
    // Hash: #ws://192.168.1.100:8765?token=abc
    const hash = window.location.hash.slice(1);
    if (hash && hash.startsWith('ws')) return hash;

    // Query: ?gateway=ws://...
    const params = new URLSearchParams(window.location.search);
    const qGateway = params.get('gateway');
    if (qGateway) return qGateway;

    // localStorage
    const stored = localStorage.getItem('g2_gateway_url');
    if (stored) return stored;

    // Build-time env
    const envUrl = import.meta.env?.VITE_GATEWAY_URL;
    if (envUrl) return envUrl;

    // Fallback
    return 'ws://localhost:8765';
  }

  connect(url?: string): void {
    this.url = url ?? this.resolveUrl();
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
      console.log(`[Gateway] Connected to ${this.url}`);
      this.backoff = INITIAL_BACKOFF;
      // Persist URL on first successful connection
      try { localStorage.setItem('g2_gateway_url', this.url); } catch { /* noop */ }
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
