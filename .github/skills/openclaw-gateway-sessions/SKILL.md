```skill
---
name: openclaw-gateway-sessions
description:
  OpenClaw Gateway architecture, session lifecycle, context management, and
  pruning strategies. Use when configuring the Gateway daemon, designing session
  scoping, managing context windows, tuning pruning or compaction, or
  troubleshooting session resets and transcript storage. Triggers on tasks
  involving session keys, DM scoping, context pruning, compaction config,
  Gateway wire protocol, or WebSocket control plane issues.
---

# OpenClaw Gateway & Sessions

Architecture, session lifecycle, and context management for the OpenClaw Gateway.
Keep sessions predictable, context within budget, and transcripts durable.

## When to Apply

Reference these guidelines when:

- Configuring the Gateway daemon (port, auth, CORS, WebChat)
- Designing session scoping for single-user or multi-user deployments
- Setting up DM scope modes (`main`, `per-peer`, `per-channel-peer`)
- Configuring identity links to unify a person across channels
- Tuning context pruning to stay within token budgets
- Configuring compaction and pre-compaction memory flush
- Debugging session resets, stale context, or transcript corruption
- Working with the WebSocket wire protocol or RPC methods
- Setting up session reset schedules (daily, idle, per-type, per-channel)

## Rule Categories by Priority

| Priority | Category              | Impact   | Prefix       |
| -------- | --------------------- | -------- | ------------ |
| 1        | Gateway Architecture  | CRITICAL | `gw-`        |
| 2        | Session Lifecycle     | CRITICAL | `session-`   |
| 3        | Context Management    | HIGH     | `context-`   |
| 4        | Pruning & Compaction  | HIGH     | `pruning-`   |
| 5        | Wire Protocol         | MEDIUM   | `wire-`      |

---

## 1. Gateway Architecture (CRITICAL)

### `gw-single-daemon`
The Gateway is a **single long-lived process** that owns all channel connections
and serves as the control plane. Never run multiple Gateway instances against the
same `~/.openclaw` directory — they will corrupt session state.

```json
// ✅ One Gateway, one workspace
{ "gateway": { "port": 18789, "host": "127.0.0.1" } }

// ❌ Two Gateways sharing ~/.openclaw → race conditions on session files
```

### `gw-auth-always`
Always set `OPENCLAW_GATEWAY_TOKEN` in production. Without it, the WebSocket API
and webhook endpoints are open to unauthenticated access.

```bash
# ✅ Production
export OPENCLAW_GATEWAY_TOKEN="$(openssl rand -hex 32)"

# ❌ No token → anyone on the network can send agent commands
```

### `gw-component-roles`
Understand the four client types that connect to the Gateway:

| Role       | Purpose                                    | Examples              |
| ---------- | ------------------------------------------ | --------------------- |
| `client`   | Interactive UI, sends requests             | CLI, macOS app, WebUI |
| `node`     | Device commands (screenshot, camera, GPS)  | iOS app, Android app  |
| `agent`    | Internal — the Pi runtime                  | Embedded runtime      |
| `webhook`  | Inbound HTTP triggers                      | CI/CD, external APIs  |

Never expose `agent` or `node` roles to untrusted networks.

### `gw-cors-restrict`
In production, restrict CORS to known origins. The default allows localhost only.

```json
// ✅ Explicit origins
{ "gateway": { "cors": { "allowedOrigins": ["https://my-dashboard.example.com"] } } }

// ❌ Wide open
{ "gateway": { "cors": { "allowedOrigins": ["*"] } } }
```

---

## 2. Session Lifecycle (CRITICAL)

### `session-key-anatomy`
Every session is identified by a structured key. Know the patterns:

| Type           | Pattern                                        | Use Case                     |
| -------------- | ---------------------------------------------- | ---------------------------- |
| Main DM        | `agent:<agentId>:main`                         | Default direct conversation  |
| Per-peer DM    | `agent:<agentId>:dm:<peerId>`                  | Isolated per sender          |
| Channel-peer   | `agent:<agentId>:<channel>:dm:<peerId>`        | Isolated per channel+sender  |
| Group          | `agent:<agentId>:<channel>:group:<id>`         | Group chat                   |
| Cron           | `cron:<jobId>`                                 | Scheduled task               |
| Webhook        | `hook:<uuid>`                                  | Inbound HTTP trigger         |
| Subagent       | `agent:<agentId>:subagent:<uuid>`              | Spawned child agent          |

Never construct session keys manually in application code — use the SDK helpers.

### `session-dm-scope-choose`
Choose DM scope based on deployment model:

| Scope                      | When to use                                           |
| -------------------------- | ----------------------------------------------------- |
| `main`                     | Single-user personal assistant (default, recommended) |
| `per-peer`                 | Multi-user but same person across channels            |
| `per-channel-peer`         | Multi-user with channel isolation (teams, orgs)       |
| `per-account-channel-peer` | Multi-account enterprise deployments                  |

```json
// ✅ Single user — all DMs share one session for continuity
{ "session": { "dmScope": "main" } }

// ✅ Multi-user team bot — isolate per channel and person
{ "session": { "dmScope": "per-channel-peer" } }

// ❌ Using per-channel-peer for a single user → fragmented context
```

### `session-identity-links`
When the same person uses multiple channels, link their identities so they share
one DM session:

```json
{
  "session": {
    "identityLinks": {
      "alice": ["telegram:123456789", "discord:987654321012345"]
    }
  }
}
```

Without identity links and `per-peer` scoping, the same person gets separate
sessions per channel, losing conversational continuity.

### `session-reset-schedule`
Configure session resets deliberately. Defaults to daily at 4 AM local time.

```json
{
  "session": {
    "dailyResetHour": 4,
    "idleMinutes": null,
    "resetByType": {
      "direct": { "dailyResetHour": 4 },
      "group": { "dailyResetHour": null, "idleMinutes": 60 },
      "thread": { "idleMinutes": 30 }
    },
    "resetByChannel": {
      "slack": { "idleMinutes": 120 }
    }
  }
}
```

- Direct chats: daily reset preserves a "fresh morning" feel
- Groups: idle reset avoids stale context in bursty channels
- Threads: short idle timeout keeps threads focused

### `session-storage-aware`
Sessions live at `~/.openclaw/agents/<agentId>/sessions/`:
- `sessions.json` — session key → session ID mapping
- `<sessionId>.jsonl` — full transcript (append-only)

Back up the `sessions/` directory for durability. Transcript JSONL files are the
source of truth — the in-memory session can always be rebuilt from them.

---

## 3. Context Management (HIGH)

### `context-prompt-assembly-order`
The system prompt is assembled in this order:

1. OpenClaw base prompt (internal)
2. Skills list (compact XML summary of available tools/skills)
3. Bootstrap files: AGENTS.md → SOUL.md → IDENTITY.md → USER.md → TOOLS.md → BOOTSTRAP.md
4. Per-run overrides

Keep bootstrap files concise — every token counts against the context window.
Combined bootstrap files should stay under 2,000 tokens for optimal headroom.

### `context-budget-awareness`
Each model has a context limit. OpenClaw enforces `compaction.reserveTokensFloor`
(default 20,000 tokens) to keep space for the model's response.

```
Effective context = Model limit - reserveTokensFloor - system prompt tokens
```

For long-running sessions, prefer aggressive pruning or lower reserveTokensFloor
only if the agent produces short responses.

### `context-steer-while-streaming`
When queue mode is `steer`, inbound messages are injected into the current run.
The queue is checked after each tool call — remaining tool calls from the current
assistant turn are skipped.

Use `steer` mode for interactive sessions where the user needs to redirect mid-
thought. Use `collect` for batch/cron work where interruption is undesirable.

---

## 4. Pruning & Compaction (HIGH)

### `pruning-mode-selection`
Choose pruning mode based on usage pattern:

| Mode        | Behavior                                | Best for                  |
| ----------- | --------------------------------------- | ------------------------- |
| `off`       | No pruning (default)                    | Short sessions, cheap     |
| `cache-ttl` | Prune when Anthropic cache is stale     | Long sessions, Anthropic  |

```json
{
  "agent": {
    "contextPruning": {
      "mode": "cache-ttl",
      "ttl": "5m",
      "keepLastAssistants": 3
    }
  }
}
```

### `pruning-what-gets-trimmed`
Only `toolResult` messages are pruned. User and assistant messages are **never**
modified. The last N assistant messages (default 3) are fully protected.
Tool results containing image blocks are skipped entirely.

### `pruning-soft-vs-hard`
- **Soft-trim** (softTrimRatio 0.3): Keeps head + tail of tool result, inserts
  `...` separator, appends note with original size. Use when partial tool output
  is still useful.
- **Hard-clear** (hardClearRatio 0.5): Replaces entire tool result with a
  placeholder. Use for large file reads or command outputs that lose value fast.

Apply hard-clear at ≥50% context utilization, soft-trim at ≥30%.

### `pruning-compaction-flush`
Before auto-compaction, enable the memory flush to avoid losing critical context:

```json
{
  "compaction": {
    "memoryFlush": {
      "enabled": true,
      "softThresholdTokens": 4000,
      "systemPrompt": "Session nearing compaction. Store durable memories now.",
      "prompt": "Write lasting notes to memory/YYYY-MM-DD.md; reply NO_REPLY if nothing."
    }
  }
}
```

The flush gives the agent one silent turn to persist important information before
the session is summarized. Without it, nuanced context is lost to compaction.

### `pruning-manual-compact`
Users can trigger compaction manually with `/compact` (optional instructions).
Use this when the session feels sluggish or the model starts repeating itself —
both are signs of context saturation.

---

## 5. Wire Protocol (MEDIUM)

### `wire-connect-first`
The first frame on any WebSocket connection **must** be a `connect` frame with
role and optional auth token. Any other frame first will be rejected.

```json
{ "type": "connect", "params": { "role": "client", "auth": { "token": "..." } } }
```

### `wire-request-response`
All RPC follows request-response:

```json
// Request
{ "type": "req", "id": "unique-id", "method": "agent", "params": {} }

// Response
{ "type": "res", "id": "unique-id", "ok": true, "payload": {} }
```

Always use unique, client-generated `id` values. Use UUIDs or monotonic counters.

### `wire-idempotency`
Side-effecting methods (`send`, `agent`) require idempotency keys. Retries with
the same key are safe. Without idempotency, network retries can duplicate agent
runs or messages.

### `wire-events-subscribe`
Events are pushed by the Gateway without a request:

```json
{ "type": "event", "event": "agent", "payload": {}, "seq": 42 }
```

Event types: `agent`, `chat`, `presence`, `health`, `heartbeat`, `cron`.
Use `seq` to detect missed events and request replay if needed.

---

## Anti-Patterns

| Anti-Pattern | Why It's Wrong | Correct Approach |
| --- | --- | --- |
| Multiple Gateways sharing one workspace | Session file corruption | One Gateway per ~/.openclaw directory |
| No Gateway token in production | Open WebSocket and webhook access | Always set OPENCLAW_GATEWAY_TOKEN |
| per-channel-peer for single user | Fragmented context across channels | Use `main` for single-user deployments |
| Ignoring compaction memory flush | Context lost on auto-compaction | Enable memoryFlush.enabled: true |
| Manual session key construction | Fragile, breaks on format changes | Use SDK helpers or let Gateway assign |
| pruning.mode off on long sessions | Context overflow, degraded output | Use cache-ttl for sessions >30 minutes |
| Wide CORS origins in production | XSS-style attacks on Gateway | Restrict to known dashboard origins |

---

## Quick Config Template

```json
{
  "gateway": {
    "port": 18789,
    "host": "127.0.0.1",
    "token": "env:OPENCLAW_GATEWAY_TOKEN",
    "cors": { "allowedOrigins": ["https://dashboard.example.com"] },
    "webchat": { "enabled": true, "port": 3000 }
  },
  "session": {
    "dmScope": "main",
    "dailyResetHour": 4,
    "idleMinutes": null,
    "identityLinks": {}
  },
  "agents": {
    "defaults": {
      "timeoutSeconds": 600,
      "contextPruning": {
        "mode": "cache-ttl",
        "ttl": "5m",
        "keepLastAssistants": 3,
        "softTrimRatio": 0.3,
        "hardClearRatio": 0.5
      },
      "compaction": {
        "reserveTokensFloor": 20000,
        "memoryFlush": { "enabled": true, "softThresholdTokens": 4000 }
      }
    }
  }
}
```

## References

- https://docs.openclaw.ai/concepts/architecture
- https://docs.openclaw.ai/concepts/session
- https://docs.openclaw.ai/concepts/session-pruning
- https://docs.openclaw.ai/concepts/agent-loop
- https://docs.openclaw.ai/concepts/streaming
```
