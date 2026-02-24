/**
 * azure-api-version-preload.cjs
 *
 * Node.js CommonJS preload module that monkey-patches globalThis.fetch to
 * inject `api-version=2024-10-21` into Azure OpenAI requests.
 *
 * WHY: OpenClaw uses the standard OpenAI SDK client (not AzureOpenAI).
 * The standard client doesn't append the mandatory `api-version` query
 * parameter that Azure requires — Azure returns 404 without it.
 * OpenClaw's strict Zod config schema rejects `defaultQuery`, so we
 * can't configure it declaratively. This preload is the workaround.
 *
 * USAGE:
 *   NODE_OPTIONS="--require /path/to/azure-api-version-preload.cjs" openclaw daemon
 *
 * DEBUG:
 *   AZURE_PRELOAD_DEBUG=1  — logs every patched URL to stderr
 */

"use strict";

const AZURE_API_VERSION = "2024-10-21";
const AZURE_HOST_PATTERN = /\.openai\.azure\.com$/i;
const debug = process.env.AZURE_PRELOAD_DEBUG === "1";

const originalFetch = globalThis.fetch;

/**
 * Attempt to inject `api-version` into an Azure OpenAI URL.
 * Returns the original input unchanged if it isn't an Azure URL or if
 * parsing fails.
 */
function maybeInjectApiVersion(input) {
  try {
    const urlStr = typeof input === "string" ? input : input instanceof URL ? input.href : null;
    if (!urlStr) return input;

    const url = new URL(urlStr);
    if (!AZURE_HOST_PATTERN.test(url.hostname)) return input;
    if (url.searchParams.has("api-version")) return input;

    url.searchParams.set("api-version", AZURE_API_VERSION);

    if (debug) {
      process.stderr.write(`[azure-preload] ${urlStr} → ${url.href}\n`);
    }

    // If the caller passed a Request object, rebuild it with the new URL
    // so that headers / method / body are preserved.
    if (typeof input === "object" && typeof input.url === "string") {
      return new Request(url.href, input);
    }

    return url.href;
  } catch (_err) {
    // URL parsing failed — pass through untouched.
    return input;
  }
}

globalThis.fetch = function patchedFetch(input, init) {
  return originalFetch.call(this, maybeInjectApiVersion(input), init);
};
