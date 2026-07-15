import type { AddressInfo } from 'node:net';
import { readFile } from 'node:fs/promises';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { createServer, type Plugin, type ViteDevServer } from 'vite';

import { apiPlugin, SIMULATOR_LOOPBACK_HOST } from './dev-api';
import { createViteConfig } from './vite.config';

function pluginNames(plugins: unknown[] | undefined): string[] {
  return (plugins ?? []).map((plugin) => (plugin as Plugin).name);
}

describe('simulator automation mode', () => {
  it('activates the API and control panels only in simulator mode', async () => {
    const simulator = createViteConfig({ command: 'serve', mode: 'simulator' });
    const defaultDev = createViteConfig({ command: 'serve', mode: 'development' });
    const packageJson = JSON.parse(
      await readFile(new URL('./package.json', import.meta.url), 'utf8'),
    ) as { scripts: Record<string, string> };

    expect(simulator.server?.host).toBe(SIMULATOR_LOOPBACK_HOST);
    expect(pluginNames(simulator.plugins as unknown[])).toEqual([
      'api',
      'input-bar',
      'telemetry-panel',
      'session-panel',
    ]);
    expect(pluginNames(defaultDev.plugins as unknown[])).toEqual([]);
    expect(packageJson.scripts.dev).toBe('vite');
    expect(packageJson.scripts['dev:network']).toBe('vite --host 0.0.0.0');
    expect(packageJson.scripts['dev:network']).not.toContain('simulator');
  });

  it('does not register control routes for network delivery', async () => {
    const config = createViteConfig({ command: 'serve', mode: 'development' });
    const server = await createServer({
      ...config,
      server: { ...config.server, host: SIMULATOR_LOOPBACK_HOST, port: 45124, strictPort: false },
    });

    try {
      await server.listen();
      const address = server.httpServer?.address() as AddressInfo;
      const response = await fetch(`http://127.0.0.1:${address.port}/_dev/health`);

      expect(response.status).toBe(200);
      expect(response.headers.get('content-type')).toContain('text/html');
      expect(await response.text()).toMatch(/<!doctype html>/i);
    } finally {
      await server.close();
    }
  });
});

describe('simulator automation origin gate', () => {
  let server: ViteDevServer;
  let baseUrl: string;

  beforeEach(async () => {
    server = await createServer({
      configFile: false,
      plugins: [apiPlugin()],
      server: { host: SIMULATOR_LOOPBACK_HOST, port: 45123, strictPort: false, cors: false },
    });
    await server.listen();
    const address = server.httpServer?.address() as AddressInfo;
    baseUrl = `http://127.0.0.1:${address.port}`;
  });

  it('fails closed when resolved Vite configuration overrides the loopback host', async () => {
    await expect(createServer({
      configFile: false,
      plugins: [apiPlugin()],
      server: { host: '0.0.0.0', port: 45125, strictPort: false, cors: false },
    })).rejects.toThrow(`requires Vite to bind ${SIMULATOR_LOOPBACK_HOST}`);
  });

  it('accepts the approved loopback host during resolved configuration', () => {
    expect(SIMULATOR_LOOPBACK_HOST).toBe('127.0.0.1');
    expect(server.config.server.host).toBe(SIMULATOR_LOOPBACK_HOST);
  });

  afterEach(async () => {
    await server.close();
  });

  it('accepts exact loopback origins and returns explicit CORS headers', async () => {
    const port = new URL(baseUrl).port;

    for (const origin of [`http://127.0.0.1:${port}`, `http://localhost:${port}`]) {
      const response = await fetch(`${baseUrl}/_dev/health`, { headers: { Origin: origin } });

      expect(response.status).toBe(200);
      expect(response.headers.get('access-control-allow-origin')).toBe(origin);
      expect(response.headers.get('vary')).toBe('Origin');
      await expect(response.json()).resolves.toEqual({ ok: true });
    }
  });

  it('allows an absent Origin for same-host tooling without emitting wildcard CORS', async () => {
    const response = await fetch(`${baseUrl}/_dev/health`);

    expect(response.status).toBe(200);
    expect(response.headers.get('access-control-allow-origin')).toBeNull();
  });

  it('rejects foreign origins, including OPTIONS preflights', async () => {
    const origin = 'https://example.test';
    const response = await fetch(`${baseUrl}/_dev/health`, { headers: { Origin: origin } });
    const options = await fetch(`${baseUrl}/_dev/health`, {
      method: 'OPTIONS',
      headers: { Origin: origin },
    });

    expect(response.status).toBe(403);
    await expect(response.json()).resolves.toEqual({ error: 'origin not allowed' });
    expect(response.headers.get('access-control-allow-origin')).toBeNull();
    expect(options.status).toBe(403);
  });

  it('handles allowed OPTIONS preflights with the same origin policy', async () => {
    const origin = `http://127.0.0.1:${new URL(baseUrl).port}`;
    const response = await fetch(`${baseUrl}/_dev/health`, {
      method: 'OPTIONS',
      headers: { Origin: origin },
    });

    expect(response.status).toBe(204);
    expect(response.headers.get('access-control-allow-origin')).toBe(origin);
    expect(response.headers.get('access-control-allow-methods')).toBe('GET, POST, OPTIONS');
  });
});
