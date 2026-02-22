```skill
---
name: copilot-sdk-client
description:
  GitHub Copilot SDK client lifecycle, process management, authentication, and
  BYOK provider configuration. Use when initializing CopilotClient, configuring
  auth methods, setting up BYOK with OpenAI/Azure/Anthropic/Ollama, managing
  CLI process spawning and restart, or debugging connection and protocol issues.
  Triggers on tasks involving CopilotClient constructor, start/stop, auth tokens,
  provider config, CLI server mode, process management, or protocol versions.
---

# Copilot SDK Client & Authentication

Initialize, configure, authenticate, and manage the Copilot SDK client lifecycle.
Covers process management, all auth methods, BYOK provider setup, and connection
health.

## When to Apply

Reference these guidelines when:

- Creating and configuring a CopilotClient instance
- Choosing and configuring authentication method
- Setting up BYOK providers (OpenAI, Azure, Anthropic, Ollama)
- Managing CLI process lifecycle (start, stop, restart)
- Connecting to an external CLI server (TCP mode)
- Debugging connection failures, protocol mismatches, or auth errors
- Configuring log levels and diagnostic tools
- Querying status, auth state, or available models

## Rule Categories by Priority

| Priority | Category              | Impact   | Prefix      |
| -------- | --------------------- | -------- | ----------- |
| 1        | Client Lifecycle      | CRITICAL | `client-`   |
| 2        | Authentication        | CRITICAL | `auth-`     |
| 3        | BYOK Providers        | HIGH     | `byok-`     |
| 4        | Process Management    | HIGH     | `proc-`     |
| 5        | Diagnostics           | MEDIUM   | `diag-`     |

---

## 1. Client Lifecycle (CRITICAL)

### `client-constructor-options`
CopilotClient accepts optional configuration at construction:

**TypeScript:**
```typescript
import { CopilotClient } from "@github/copilot-sdk";

const client = new CopilotClient({
  githubToken: process.env.GH_TOKEN,     // Explicit token
  useLoggedInUser: true,                  // Use stored CLI credentials (default)
  cliPath: "/usr/local/bin/copilot",     // Override CLI path
  logLevel: "info",                       // "debug" | "info" | "warn" | "error"
  autoRestart: true,                      // Reconnect on crash (default)
  cliArgs: ["--log-dir", "/tmp/logs"],   // Extra CLI args
});
```

**Python:**
```python
from copilot import CopilotClient

client = CopilotClient(options={
    "github_token": os.environ.get("GH_TOKEN"),
    "use_logged_in_user": True,
    "cli_path": "/usr/local/bin/copilot",
    "log_level": "info",
    "auto_restart": True,
})
```

### `client-start-stop`
Always pair `start()` with `stop()`. Python supports context managers:

**TypeScript:**
```typescript
const client = new CopilotClient();
// start() is implicit — first RPC call triggers connection

// When done:
const errors = await client.stop();  // Returns array of session destruction errors
// OR
await client.forceStop();  // SIGKILL, no cleanup
```

**Python:**
```python
# Explicit lifecycle
client = CopilotClient()
await client.start()  # Must call explicitly in Python
# ... work ...
errors = await client.stop()

# OR use context manager (recommended)
async with CopilotClient() as client:
    session = await client.create_session()
    # auto-stops on exit
```

### `client-stop-vs-force-stop`
Know when to use each:

| Method | Behavior | Use When |
|--------|----------|----------|
| `stop()` | Destroys all sessions (3 retries), disposes connection, terminates process | Normal shutdown |
| `forceStop()` | Clears sessions immediately, SIGKILL, no retries | Hung process, timeout exceeded |

`stop()` returns `Error[]` — check for session destruction failures. Use
`forceStop()` only as an escape hatch when `stop()` hangs.

### `client-state-machine`
Track connection state with `getState()`:

| State | Meaning |
|-------|---------|
| `"disconnected"` | Not connected to CLI |
| `"connecting"` | Connection in progress |
| `"connected"` | Ready for RPC calls |
| `"error"` | Connection failed |

Check state before sending commands. React to state changes for UIs.

### `client-lifecycle-events`
Subscribe to session lifecycle events on the client:

```typescript
const unsub = client.on("session.created", (event) => {
  console.log("New session:", event.sessionId);
});

// All lifecycle event types:
// "session.created" | "session.deleted" | "session.updated"
// "session.foreground" | "session.background"
```

### `client-session-management`
Client-level session operations for multi-session apps:

```typescript
// List all sessions
const sessions = await client.listSessions({ status: "active" });

// Get last modified session
const lastId = await client.getLastSessionId();

// Delete session from storage permanently
await client.deleteSession("session-id");

// Foreground management (for TUI apps)
await client.setForegroundSessionId("session-id");
const fgId = await client.getForegroundSessionId();
```

---

## 2. Authentication (CRITICAL)

### `auth-method-priority`
The SDK evaluates auth in this order — first match wins:

| Priority | Method | Config |
|----------|--------|--------|
| 1 | Explicit token | `githubToken` option |
| 2 | HMAC key | `CAPI_HMAC_KEY` / `COPILOT_HMAC_KEY` env |
| 3 | Direct API token | `GITHUB_COPILOT_API_TOKEN` + `COPILOT_API_URL` env |
| 4 | SDK auth env | `COPILOT_GITHUB_TOKEN` env |
| 5 | GH CLI env | `GH_TOKEN` env |
| 6 | GitHub env | `GITHUB_TOKEN` env |
| 7 | Stored OAuth | `copilot auth login` credentials in keychain |
| 8 | GH CLI credentials | `gh auth` stored tokens |

### `auth-choose-method`
Match auth method to deployment scenario:

| Scenario | Method | Notes |
|----------|--------|-------|
| Local development | Stored OAuth (default) | Run `copilot auth login` once |
| CI/CD pipelines | Environment variables | Set `COPILOT_GITHUB_TOKEN` |
| Web app with user login | OAuth GitHub App | Pass `gho_`/`ghu_` token |
| Enterprise / custom models | BYOK | No GitHub auth needed |
| Server-to-server | Environment variables | Service account token |

### `auth-oauth-github-app`
For apps that authenticate users through GitHub OAuth:

```typescript
const client = new CopilotClient({
  githubToken: userAccessToken,  // gho_, ghu_, or github_pat_ prefix
  useLoggedInUser: false,        // Don't fall back to stored CLI creds
});
```

The token must be from an OAuth app with Copilot scope. User must have a
Copilot subscription.

### `auth-check-status`
Verify auth before creating sessions:

```typescript
const authStatus = await client.getAuthStatus();
// Returns: { status: "signed-in" | "signed-out" | ... }

if (authStatus.status !== "signed-in") {
  console.error("Not authenticated. Run: copilot auth login");
}
```

### `auth-env-variable-pattern`
For automation, set tokens via environment:

```bash
# Preferred — SDK-specific variable
export COPILOT_GITHUB_TOKEN="gho_xxxxxxxxxxxx"

# Also works — GH CLI compatibility
export GH_TOKEN="gho_xxxxxxxxxxxx"
```

Never hardcode tokens. Use environment variables or secret managers.

---

## 3. BYOK Providers (HIGH)

### `byok-provider-config`
BYOK bypasses GitHub auth entirely. Configure via `provider` in session config:

```typescript
interface ProviderConfig {
  type?: "openai" | "azure" | "anthropic";  // default: "openai"
  wireApi?: "completions" | "responses";     // default: "completions"
  baseUrl: string;                           // Required
  apiKey?: string;                           // Optional (Ollama needs none)
  bearerToken?: string;                      // Takes precedence over apiKey
  azure?: { apiVersion?: string };           // Default: "2024-10-21"
}
```

### `byok-provider-recipes`
Tested configurations for each provider:

**OpenAI:**
```typescript
provider: {
  type: "openai",
  baseUrl: "https://api.openai.com/v1",
  apiKey: process.env.OPENAI_API_KEY,
}
```

**Azure AI Foundry:**
```typescript
provider: {
  type: "openai",
  baseUrl: "https://your-resource.openai.azure.com/openai/v1/",
  apiKey: process.env.AZURE_API_KEY,
  wireApi: "responses",  // Use "completions" for older models
}
```

**Azure OpenAI (classic):**
```typescript
provider: {
  type: "azure",
  baseUrl: "https://your-resource.openai.azure.com",
  apiKey: process.env.AZURE_API_KEY,
  azure: { apiVersion: "2024-10-21" },
}
```

**Anthropic:**
```typescript
provider: {
  type: "anthropic",
  baseUrl: "https://api.anthropic.com",
  apiKey: process.env.ANTHROPIC_API_KEY,
}
```

**Ollama (local):**
```typescript
provider: {
  type: "openai",
  baseUrl: "http://localhost:11434/v1",
  // No apiKey needed
}
```

**LiteLLM / vLLM / other OpenAI-compatible:**
```typescript
provider: {
  type: "openai",
  baseUrl: "http://localhost:4000/v1",
  apiKey: process.env.LITELLM_KEY,
}
```

### `byok-limitations`
Know the constraints:

- **No managed identity.** Entra ID, federated identity not supported. Static keys only.
- **API keys not persisted on resume.** Must re-provide `provider` config when calling `resumeSession()`.
- **Bearer token precedence.** If both `apiKey` and `bearerToken` set, `bearerToken` wins.
- **Wire API matters.** `"responses"` (newer) vs `"completions"` (older) — wrong choice causes silent failures. Use `"responses"` for Azure AI Foundry, `"completions"` for everything else unless you know the model supports the Responses API.

### `byok-model-names`
With BYOK, the `model` field maps to the provider's model name or deployment:

```typescript
// OpenAI
{ model: "gpt-4.1", provider: { type: "openai", ... } }

// Azure — use deployment name
{ model: "my-gpt4-deployment", provider: { type: "azure", ... } }

// Ollama — use local model name
{ model: "llama3.1", provider: { type: "openai", baseUrl: "http://localhost:11434/v1" } }
```

---

## 4. Process Management (HIGH)

### `proc-cli-resolution`
The SDK resolves the CLI binary in this order:

1. Explicit `cliPath` option
2. Bundled `@github/copilot` npm package (Node.js)
3. Error: "Copilot CLI not found"

Always install the CLI separately or bundle it. The SDK does not install it.

### `proc-spawn-flags`
The CLI is spawned with these flags:

```
copilot --headless --no-auto-update --stdio [--log-level <level>] [custom args]
```

- `--headless`: No TUI, JSON-RPC only
- `--no-auto-update`: Prevents version changes mid-run
- `--stdio`: JSON-RPC over stdin/stdout (default)
- Auth flags added automatically based on token config

### `proc-tcp-mode`
Connect to an external CLI server instead of spawning:

```bash
# Start CLI in server mode externally:
copilot --server --port 3333

# Connect from SDK:
const client = new CopilotClient({
  port: 3333,
  host: "127.0.0.1",  // default
});
```

Use TCP for: shared CLI instances, Docker deployments, remote debugging.

### `proc-auto-restart`
When `autoRestart: true` (default), the SDK reconnects if the CLI process exits
unexpectedly. This handles transient crashes but won't help with persistent failures.

Disable for: controlled shutdown scenarios, one-shot scripts, debugging.

### `proc-stderr-capture`
CLI stderr output is prefixed and forwarded to the parent process stderr:

```
[CLI subprocess] Loading configuration...
[CLI subprocess] Warning: Model not found, falling back to default
```

Monitor stderr during development for diagnostic messages.

---

## 5. Diagnostics (MEDIUM)

### `diag-health-check`
Use `ping()` to verify the connection and protocol version:

```typescript
const { message, timestamp, protocolVersion } = await client.ping("hello");
// protocolVersion must match SDK's expected version
```

### `diag-status-check`
Get CLI version and capabilities:

```typescript
const status = await client.getStatus();
// { cliVersion: "1.x.x", protocolVersion: 2 }
```

### `diag-model-listing`
List available models (cached after first call):

```typescript
const models = await client.listModels();
// [{ id: "gpt-4.1", name: "GPT-4.1", ... }, ...]
```

### `diag-debug-logging`
Enable verbose logging for troubleshooting:

```typescript
const client = new CopilotClient({
  logLevel: "debug",
  cliArgs: ["--log-dir", "/tmp/copilot-logs"],
});
```

Check `/tmp/copilot-logs/` for detailed CLI-side logs. SDK-side logs go to
stderr with `[CLI subprocess]` prefix.

### `diag-protocol-version`
SDK enforces protocol compatibility. If versions mismatch:

```
Error: Protocol version mismatch. SDK expects 2, CLI reports 3.
Update the SDK: npm install @github/copilot-sdk@latest
```

Always keep SDK and CLI versions in sync. Pin both in CI.

---

## Anti-Patterns

| Anti-Pattern | Why It's Wrong | Correct Approach |
|---|---|---|
| No `stop()` call | CLI process leaks | Always call `stop()` or use context manager |
| Hardcoded auth tokens | Credential exposure | Use env variables or secret managers |
| Ignoring `stop()` error array | Silent session leaks | Log and handle destruction errors |
| Wrong wireApi for BYOK | Silent model failures | `"responses"` for Foundry, `"completions"` otherwise |
| Multi-client without locking | Session state races | Implement app-level session locking |
| Skipping `getAuthStatus()` check | Confusing runtime errors | Verify auth before `createSession()` |
| Mismatched SDK/CLI versions | Protocol errors | Pin and sync both versions |
| `forceStop()` as default shutdown | Dirty state, lost sessions | Use `stop()` first, `forceStop()` as fallback |

## References

- SDK README: `.archive/copilot-sdk/README.md`
- Getting Started: `.archive/copilot-sdk/docs/getting-started.md`
- Auth docs: `.archive/copilot-sdk/docs/auth/index.md`
- BYOK docs: `.archive/copilot-sdk/docs/auth/byok.md`
- Debugging: `.archive/copilot-sdk/docs/debugging.md`
```
