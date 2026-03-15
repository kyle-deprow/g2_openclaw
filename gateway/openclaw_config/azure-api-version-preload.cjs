/**
 * azure-api-version-preload.cjs
 *
 * Node.js CommonJS preload that monkey-patches globalThis.fetch to:
 *   1. Inject `api-version=2024-12-01-preview` into Azure OpenAI requests.
 *   2. Acquire an Entra ID (Azure AD) bearer token via `az account
 *      get-access-token` and attach it as an Authorization header.
 *
 * WHY: OpenClaw uses the standard OpenAI SDK client (not AzureOpenAI).
 * The standard client doesn't append the mandatory `api-version` query
 * parameter, and it sends an `api-key` header instead of a Bearer token.
 * Azure OpenAI with `disableLocalAuth: true` requires Entra tokens.
 * OpenClaw's strict Zod config schema rejects `defaultQuery`, so we
 * can't configure either concern declaratively.
 *
 * USAGE:
 *   NODE_OPTIONS="--require /path/to/azure-api-version-preload.cjs" openclaw daemon
 *
 * DEBUG:
 *   AZURE_PRELOAD_DEBUG=1  — logs every patched URL + token refresh to stderr
 */

"use strict";

const { execSync } = require("child_process");

const AZURE_API_VERSION = "2024-12-01-preview";
const AZURE_HOST_PATTERN = /\.openai\.azure\.com$/i;
const TOKEN_RESOURCE = "https://cognitiveservices.azure.com";
const TOKEN_REFRESH_MARGIN_S = 300; // refresh 5 min before expiry
const debug = process.env.AZURE_PRELOAD_DEBUG === "1";

const originalFetch = globalThis.fetch;

// ── Token cache ──────────────────────────────────────────────────────
let cachedToken = null; // { accessToken, expiresOn }

function getEntraToken() {
  if (cachedToken) {
    const nowEpoch = Math.floor(Date.now() / 1000);
    if (cachedToken.expiresOn - nowEpoch > TOKEN_REFRESH_MARGIN_S) {
      return cachedToken.accessToken;
    }
    if (debug) {
      process.stderr.write(
        `[azure-preload] token expiring in ${cachedToken.expiresOn - nowEpoch}s, refreshing\n`
      );
    }
  }

  try {
    const raw = execSync(
      `az account get-access-token --resource ${TOKEN_RESOURCE} --output json`,
      { encoding: "utf-8", timeout: 15_000 }
    );
    const parsed = JSON.parse(raw);
    // Use expires_on (epoch seconds) — expiresOn is local-time string
    const expiresOn = Number(parsed.expires_on);
    cachedToken = { accessToken: parsed.accessToken, expiresOn };

    if (debug) {
      const ttl = expiresOn - Math.floor(Date.now() / 1000);
      process.stderr.write(`[azure-preload] acquired Entra token, TTL ${ttl}s\n`);
    }

    return cachedToken.accessToken;
  } catch (err) {
    process.stderr.write(
      `[azure-preload] ERROR acquiring Entra token: ${err.message}\n`
    );
    // Return stale token if we have one, otherwise null
    return cachedToken?.accessToken ?? null;
  }
}

// ── URL patching ─────────────────────────────────────────────────────

function isAzureUrl(urlStr) {
  try {
    return AZURE_HOST_PATTERN.test(new URL(urlStr).hostname);
  } catch {
    return false;
  }
}

function maybeInjectApiVersion(input) {
  try {
    const urlStr =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : null;
    if (!urlStr) return input;

    const url = new URL(urlStr);
    if (!AZURE_HOST_PATTERN.test(url.hostname)) return input;
    if (url.searchParams.has("api-version")) return input;

    url.searchParams.set("api-version", AZURE_API_VERSION);

    if (debug) {
      process.stderr.write(`[azure-preload] ${urlStr} → ${url.href}\n`);
    }

    // Rebuild Request if caller passed one, preserving headers/method/body
    if (typeof input === "object" && typeof input.url === "string") {
      return new Request(url.href, input);
    }

    return url.href;
  } catch {
    return input;
  }
}

// ── Fetch patch ──────────────────────────────────────────────────────

globalThis.fetch = function patchedFetch(input, init) {
  const patched = maybeInjectApiVersion(input);

  // Determine the URL string for Azure detection
  const urlStr =
    typeof patched === "string"
      ? patched
      : patched instanceof Request
        ? patched.url
        : null;

  if (urlStr && isAzureUrl(urlStr)) {
    const token = getEntraToken();
    if (token) {
      // Merge all headers: from Request object + from init
      const merged = new Headers();

      // 1. Headers baked into a Request object
      if (patched instanceof Request) {
        for (const [k, v] of patched.headers.entries()) {
          merged.set(k, v);
        }
      }

      // 2. Headers from init (override Request headers)
      if (init?.headers) {
        const initH =
          init.headers instanceof Headers
            ? init.headers
            : new Headers(init.headers);
        for (const [k, v] of initH.entries()) {
          merged.set(k, v);
        }
      }

      // 3. Remove any existing authorization (case-insensitive via Headers API)
      merged.delete("authorization");

      // 4. Set Entra bearer token
      merged.set("Authorization", `Bearer ${token}`);

      // Rebuild: strip headers from the Request so merged takes precedence
      const newInit = { ...init, headers: merged };
      if (patched instanceof Request) {
        // Reconstruct Request without its baked-in headers
        const freshRequest = new Request(patched.url, {
          method: patched.method,
          body: patched.body,
          signal: patched.signal,
        });
        return originalFetch.call(this, freshRequest, newInit);
      }
      return originalFetch.call(this, patched, newInit);
    }
  }

  return originalFetch.call(this, patched, init);
};
