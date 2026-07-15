/**
 * dev-api.ts — Vite plugin providing HTTP automation API endpoints.
 *
 * Lets an external tool or curl control the G2 app running in the
 * simulator's webview via a simple command-queue-and-poll pattern:
 *
 *   External caller  ──POST /_dev/cmd──►  Vite middleware  ◄──GET /_dev/poll──  Browser
 *                     ◄─GET /_dev/result/{id}──              ──POST /_dev/result──►
 *
 * Server-side: configureServer middleware on the Vite dev server (port 5173).
 * Browser-side: polling script injected via transformIndexHtml.
 */

import type { Plugin } from 'vite';
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { AddressInfo } from 'node:net';
import { randomUUID } from 'node:crypto';

export const SIMULATOR_LOOPBACK_HOST = '127.0.0.1';

// ── Types ──────────────────────────────────────────────────────────────────

interface PendingCommand {
  id: string;
  cmd: string;
  args: unknown[];
}

interface StoredResult {
  result: unknown;
  error?: string;
  ts: number;
}

/** Return the only browser origins allowed to use simulator automation. */
export function allowedDevOrigins(port: number): readonly string[] {
  return [
    `http://127.0.0.1:${port}`,
    `http://localhost:${port}`,
  ];
}

/**
 * Permit same-host tools that do not send Origin, but reject every supplied
 * origin except the exact loopback dev-server origins for the active port.
 */
export function isAllowedDevOrigin(origin: string | undefined, port: number): boolean {
  return origin === undefined || allowedDevOrigins(port).includes(origin);
}

/** The automation plugin is intentionally IPv4-loopback-only. */
export function isApprovedSimulatorHost(host: string | boolean | undefined): boolean {
  return host === SIMULATOR_LOOPBACK_HOST;
}

function actualDevPort(server: { httpServer?: { address(): string | AddressInfo | null } }, fallback: number): number {
  const address = server.httpServer?.address();
  return address && typeof address !== 'string' ? address.port : fallback;
}

function requestOrigin(req: IncomingMessage): string | undefined {
  const origin = req.headers.origin;
  return typeof origin === 'string' ? origin : undefined;
}

function setAllowedOriginHeaders(res: ServerResponse, origin: string | undefined): void {
  if (origin === undefined) return;
  res.setHeader('Access-Control-Allow-Origin', origin);
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  res.setHeader('Vary', 'Origin');
}

// ── Plugin ─────────────────────────────────────────────────────────────────

export function apiPlugin(): Plugin {
  const pending: PendingCommand[] = [];
  const results = new Map<string, StoredResult>();

  const RESULT_TTL_MS = 60_000;
  const WAIT_TIMEOUT_MS = 30_000;
  const WAIT_POLL_MS = 50;

  /** Purge expired results. */
  function gc(): void {
    const now = Date.now();
    for (const [id, entry] of results) {
      if (now - entry.ts > RESULT_TTL_MS) results.delete(id);
    }
  }

  /** Read the full request body as a string. */
  function readBody(req: IncomingMessage): Promise<string> {
    return new Promise((resolve) => {
      let body = '';
      req.on('data', (chunk: Buffer) => { body += chunk.toString(); });
      req.on('end', () => resolve(body));
      req.on('error', () => resolve(''));
    });
  }

  /** Standard JSON response helper. CORS headers are set only after origin validation. */
  function json(res: ServerResponse, status: number, data: unknown): void {
    res.writeHead(status, {
      'Content-Type': 'application/json',
    });
    res.end(JSON.stringify(data));
  }

  /** Enqueue a command and wait for its result (up to 30 s). */
  function enqueueAndWait(
    cmd: string,
    args: unknown[],
    res: ServerResponse,
  ): void {
    const id = randomUUID();
    pending.push({ id, cmd, args });

    const deadline = Date.now() + WAIT_TIMEOUT_MS;
    const timer = setInterval(() => {
      if (results.has(id)) {
        clearInterval(timer);
        const entry = results.get(id)!;
        results.delete(id);
        json(res, 200, { id, ...entry });
      } else if (Date.now() > deadline) {
        clearInterval(timer);
        json(res, 408, { id, error: 'timeout' });
      }
    }, WAIT_POLL_MS);
    res.on('close', () => clearInterval(timer));
  }

  return {
    name: 'api',
    apply: 'serve',

    configResolved(config) {
      if (!isApprovedSimulatorHost(config.server.host)) {
        throw new Error(
          `Simulator automation requires Vite to bind ${SIMULATOR_LOOPBACK_HOST}; received ${String(config.server.host)}`,
        );
      }
    },

    configureServer(server) {
      // Periodic GC every 30 s
      const gcTimer = setInterval(gc, 30_000);
      server.httpServer?.on('close', () => clearInterval(gcTimer));

      // ── Origin gate and OPTIONS preflight ───────────────────────────
      server.middlewares.use((req, res, next) => {
        if (!req.url?.startsWith('/_dev/')) {
          next();
          return;
        }

        const origin = requestOrigin(req);
        const port = actualDevPort(server, server.config.server.port ?? 5173);
        if (!isAllowedDevOrigin(origin, port)) {
          json(res, 403, { error: 'origin not allowed' });
          return;
        }

        setAllowedOriginHeaders(res, origin);
        if (req.method === 'OPTIONS') {
          res.writeHead(204);
          res.end();
          return;
        }
        next();
      });

      // ── GET /_dev/health ───────────────────────────────────────────
      server.middlewares.use('/_dev/health', ((req: IncomingMessage, res: ServerResponse, next: () => void) => {
        if (req.method !== 'GET') { next(); return; }
        json(res, 200, { ok: true });
      }) as any);

      // ── POST /_dev/cmd ─────────────────────────────────────────────
      server.middlewares.use('/_dev/cmd', (async (req: IncomingMessage, res: ServerResponse, next: () => void) => {
        if (req.method !== 'POST') { next(); return; }
        const body = await readBody(req);
        let parsed: { cmd: string; args?: unknown[] };
        try {
          parsed = JSON.parse(body);
        } catch {
          json(res, 400, { error: 'invalid json' });
          return;
        }
        if (!parsed.cmd) {
          json(res, 400, { error: 'missing cmd' });
          return;
        }
        const id = randomUUID();
        pending.push({ id, cmd: parsed.cmd, args: parsed.args ?? [] });
        gc();
        json(res, 200, { id });
      }) as any);

      // ── GET /_dev/poll ─────────────────────────────────────────────
      server.middlewares.use('/_dev/poll', ((req: IncomingMessage, res: ServerResponse, next: () => void) => {
        if (req.method !== 'GET') { next(); return; }
        const cmd = pending.shift() ?? null;
        json(res, 200, cmd ?? { cmd: null });
      }) as any);

      // ── POST /_dev/result ──────────────────────────────────────────
      server.middlewares.use('/_dev/result', (async (req: IncomingMessage, res: ServerResponse, next: () => void) => {
        if (req.method !== 'POST') { next(); return; }
        const body = await readBody(req);
        try {
          const data = JSON.parse(body) as { id?: string; result?: unknown; error?: string };
          if (data.id) {
            results.set(data.id, { result: data.result, error: data.error, ts: Date.now() });
          }
        } catch { json(res, 400, { error: 'invalid json' }); return; }
        json(res, 200, { ok: true });
      }) as any);

      // ── GET /_dev/result/:id ───────────────────────────────────────
      // Must be registered AFTER the POST handler above. Connect's
      // path-prefix matching strips '/_dev/result' so req.url is '/:id'.
      // We differentiate by method (GET vs POST handled above).
      // NOTE: Because connect's .use('/_dev/result', ...) matches both
      // POST and GET, we handle both in a single middleware and branch.
      // The POST handler above will catch POSTs first (registered earlier).
      // For GET, we re-register with a separate middleware that only
      // fires for GET and extracts the id from the URL.
      server.middlewares.use((req: IncomingMessage, res: ServerResponse, next: () => void) => {
        if (req.method !== 'GET') { next(); return; }
        const match = req.url?.match(/^\/_dev\/result\/([a-f0-9-]+)/);
        if (!match) { next(); return; }
        const id = match[1];

        // If result already available, return immediately
        if (results.has(id)) {
          const entry = results.get(id)!;
          results.delete(id);
          json(res, 200, { id, ...entry });
          return;
        }

        // Otherwise poll until result arrives or timeout
        const deadline = Date.now() + WAIT_TIMEOUT_MS;
        const timer = setInterval(() => {
          if (results.has(id)) {
            clearInterval(timer);
            const entry = results.get(id)!;
            results.delete(id);
            json(res, 200, { id, ...entry });
          } else if (Date.now() > deadline) {
            clearInterval(timer);
            json(res, 408, { id, error: 'timeout' });
          }
        }, WAIT_POLL_MS);
        res.on('close', () => clearInterval(timer));
      });

      // ── GET /_dev/state (convenience) ──────────────────────────────
      server.middlewares.use('/_dev/state', ((req: IncomingMessage, res: ServerResponse, next: () => void) => {
        if (req.method !== 'GET') { next(); return; }
        enqueueAndWait('getState', [], res);
      }) as any);

      // ── GET /_dev/conversation (convenience) ───────────────────────
      server.middlewares.use('/_dev/conversation', ((req: IncomingMessage, res: ServerResponse, next: () => void) => {
        if (req.method !== 'GET') { next(); return; }
        enqueueAndWait('getConversation', [], res);
      }) as any);

      // ── GET /_dev/display (convenience) ─────────────────────────────
      // Returns the exact text currently rendered on the G2 glasses display.
      server.middlewares.use('/_dev/display', ((req: IncomingMessage, res: ServerResponse, next: () => void) => {
        if (req.method !== 'GET') { next(); return; }
        enqueueAndWait('getDisplayText', [], res);
      }) as any);

      // ── POST /_dev/sendText (convenience) ──────────────────────────
      server.middlewares.use('/_dev/sendText', (async (req: IncomingMessage, res: ServerResponse, next: () => void) => {
        if (req.method !== 'POST') { next(); return; }
        const body = await readBody(req);
        let text: string;
        try {
          const parsed = JSON.parse(body);
          text = parsed.text ?? parsed.message ?? '';
        } catch {
          json(res, 400, { error: 'invalid json' });
          return;
        }
        if (!text) {
          json(res, 400, { error: 'missing text' });
          return;
        }
        enqueueAndWait('sendText', [text], res);
      }) as any);

      // ── POST /_dev/ttsRecord (convenience) ─────────────────────────
      server.middlewares.use('/_dev/ttsRecord', (async (req: IncomingMessage, res: ServerResponse, next: () => void) => {
        if (req.method !== 'POST') { next(); return; }
        const body = await readBody(req);
        let text: string;
        try {
          const parsed = JSON.parse(body);
          text = parsed.text ?? '';
        } catch {
          json(res, 400, { error: 'invalid json' });
          return;
        }
        if (!text) {
          json(res, 400, { error: 'missing text' });
          return;
        }
        enqueueAndWait('ttsRecord', [text], res);
      }) as any);
    },

    // ── Browser-side polling script ──────────────────────────────────────
    transformIndexHtml(html) {
      const script = `
<script>
// Automation API polling (injected by apiPlugin)
(function() {
  var POLL_INTERVAL = 200;

  function poll() {
    fetch('/_dev/poll').then(function(r) { return r.json(); }).then(function(cmd) {
      if (!cmd || !cmd.cmd || cmd.cmd === null) return;
      exec(cmd);
    }).catch(function() { /* ignore */ });
  }

  function exec(cmd) {
    var api = window.__g2Api;
    if (!api) {
      postResult(cmd.id, undefined, 'app not ready');
      return;
    }

    var result, error;
    try {
      var args = cmd.args || [];
      switch (cmd.cmd) {
        case 'getState':
          result = api.getState();
          break;
        case 'getConversation':
          result = api.getConversation();
          break;
        case 'getDisplayText':
          result = api.getDisplayText();
          break;
        case 'getGatewayConnected':
          result = api.getGatewayConnected();
          break;
        case 'sendText':
          result = api.sendText(args[0]);
          break;
        case 'ttsRecord':
          result = api.ttsRecord(args[0]);
          break;
        case 'startRecording':
          result = api.startRecording();
          break;
        case 'stopRecording':
          result = api.stopRecording(args[0]);
          break;
        case 'confirmTranscription':
          result = api.confirmTranscription();
          break;
        case 'rejectTranscription':
          result = api.rejectTranscription();
          break;
        case 'cancelResponse':
          result = api.cancelResponse();
          break;
        case 'forceStop':
          result = api.forceStop();
          break;
        case 'getSessionList':
          result = api.getSessionList();
          break;
        case 'openSessionMenu':
          result = api.openSessionMenu();
          break;
        case 'closeSessionMenu':
          result = api.closeSessionMenu();
          break;
        case 'selectSession':
          result = api.selectSession(args[0]);
          break;
        case 'tap':
          result = api.tap();
          break;
        case 'doubleTap':
          result = api.doubleTap();
          break;
        case 'resetSession':
          result = api.resetSession();
          break;
        case 'getSessionId':
          result = api.getSessionId();
          break;
        case 'getPendingTranscription':
          result = api.getPendingTranscription();
          break;
        default:
          error = 'unknown command: ' + cmd.cmd;
      }
    } catch (e) {
      error = e && e.message || String(e);
    }

    postResult(cmd.id, result, error);
  }

  function postResult(id, result, error) {
    fetch('/_dev/result', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: id, result: result, error: error }),
    }).catch(function() { /* ignore */ });
  }

  setInterval(poll, POLL_INTERVAL);
})();
<\/script>`;

      return html.replace('</body>', script + '\n</body>');
    },
  };
}
