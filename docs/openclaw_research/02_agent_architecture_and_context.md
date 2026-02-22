# OpenClaw — Agent Architecture & Context Management

## Architecture Overview

OpenClaw uses a **Gateway-centric architecture** where a single long-lived Gateway process owns all messaging surfaces and acts as the control plane.

```
WhatsApp / Telegram / Slack / Discord / Google Chat / Signal / iMessage / MS Teams / Matrix / WebChat
               │
               ▼
┌───────────────────────────────┐
│            Gateway            │
│       (control plane)         │
│     ws://127.0.0.1:18789      │
└──────────────┬────────────────┘
               │
               ├─ Pi agent (RPC)
               ├─ CLI (openclaw …)
               ├─ WebChat UI
               ├─ macOS app
               └─ iOS / Android nodes
```

### Components

| Component | Role |
|---|---|
| **Gateway (daemon)** | Maintains provider connections, exposes typed WS API, validates frames via JSON Schema, emits events (`agent`, `chat`, `presence`, `health`, `heartbeat`, `cron`) |
| **Clients** (macOS app / CLI / web UI) | One WS connection per client, send requests, subscribe to events |
| **Nodes** (macOS / iOS / Android / headless) | Connect with `role: node`, expose device commands (canvas, camera, screen record, location) |
| **WebChat** | Static UI using Gateway WS API for chat history and sends |

### Wire Protocol

- **Transport**: WebSocket, text frames with JSON payloads
- **First frame**: Must be `connect`
- **Request**: `{type:"req", id, method, params}` → `{type:"res", id, ok, payload|error}`
- **Events**: `{type:"event", event, payload, seq?, stateVersion?}`
- **Auth**: If `OPENCLAW_GATEWAY_TOKEN` is set, `connect.params.auth.token` must match
- **Idempotency**: Required for side-effecting methods (`send`, `agent`)

## Agent Runtime

OpenClaw runs a **single embedded agent runtime** derived from pi-mono. It runs in RPC mode with tool streaming and block streaming.

### The Agent Loop (End-to-End)

1. **`agent` RPC** validates params, resolves session (sessionKey/sessionId), persists session metadata → returns `{ runId, acceptedAt }`
2. **`agentCommand`** runs the agent:
   - Resolves model + thinking/verbose defaults
   - Loads skills snapshot
   - Calls `runEmbeddedPiAgent` (pi-agent-core runtime)
   - Emits lifecycle end/error if the embedded loop does not
3. **`runEmbeddedPiAgent`**:
   - Serializes runs via per-session + global queues
   - Resolves model + auth profile and builds the pi session
   - Subscribes to pi events and streams assistant/tool deltas
   - Enforces timeout → aborts run if exceeded
4. **`subscribeEmbeddedPiSession`** bridges pi-agent-core events to OpenClaw streams:
   - Tool events → `stream: "tool"`
   - Assistant deltas → `stream: "assistant"`
   - Lifecycle events → `stream: "lifecycle"` (`phase: "start" | "end" | "error"`)
5. **`agent.wait`** waits for lifecycle end/error for `runId` → returns `{ status: ok|error|timeout }`

### Entry Points

- **Gateway RPC**: `agent` and `agent.wait`
- **CLI**: `agent` command

### Queueing & Concurrency

- Runs are **serialized per session key** (session lane) and optionally through a global lane
- Prevents tool/session races and keeps session history consistent
- Queue modes: `collect`, `steer`, `followup`

### Streaming

- **Block streaming**: Sends completed assistant blocks as soon as they finish (off by default)
- **Reasoning streaming**: Can be emitted as separate stream or block replies
- Assistant deltas buffered into chat `delta` messages; `final` emitted on lifecycle end/error

### Timeouts

- `agent.wait` default: 30s (just the wait)
- Agent runtime: `agents.defaults.timeoutSeconds` default 600s

## Context Management

### Session Model

OpenClaw treats one direct-chat session per agent as primary. Session state is owned by the Gateway.

#### Session Key Types

| Session Type | Key Pattern |
|---|---|
| **Direct chat (main)** | `agent:<agentId>:<mainKey>` (default key: `main`) |
| **Per-peer DM** | `agent:<agentId>:dm:<peerId>` |
| **Per-channel-peer DM** | `agent:<agentId>:<channel>:dm:<peerId>` |
| **Group chat** | `agent:<agentId>:<channel>:group:<id>` |
| **Cron job** | `cron:<job.id>` |
| **Webhook** | `hook:<uuid>` |
| **Node session** | `node-<nodeId>` |
| **Sub-agent** | `agent:<agentId>:subagent:<uuid>` |

#### DM Scope Options

| Scope | Behavior |
|---|---|
| `main` (default) | All DMs share the main session for continuity |
| `per-peer` | Isolate by sender id across channels |
| `per-channel-peer` | Isolate by channel + sender (recommended for multi-user) |
| `per-account-channel-peer` | Isolate by account + channel + sender |

#### Identity Links

Map provider-prefixed peer ids to a canonical identity so the same person shares a DM session across channels:

```json
{
  "session": {
    "identityLinks": {
      "alice": ["telegram:123456789", "discord:987654321012345678"]
    }
  }
}
```

### Where State Lives

- **Store file**: `~/.openclaw/agents/<agentId>/sessions/sessions.json`
- **Transcripts**: `~/.openclaw/agents/<agentId>/sessions/<SessionId>.jsonl`
- The store is a map `sessionKey -> { sessionId, updatedAt, ... }`

### Session Lifecycle

- **Daily reset**: Defaults to 4:00 AM local time on gateway host
- **Idle reset** (optional): `idleMinutes` sliding window
- **Per-type overrides**: `resetByType` for `direct`, `group`, `thread`
- **Per-channel overrides**: `resetByChannel`
- **Reset triggers**: `/new` or `/reset` commands
- **Isolated cron jobs**: Always mint a fresh sessionId per run

### Session Pruning (Context Trimming)

Session pruning trims old tool results from the in-memory context right before each LLM call. It does **not** rewrite on-disk history (`.jsonl`).

#### Modes

- **`off`** (default): No pruning
- **`cache-ttl`**: Prunes when last Anthropic call is older than TTL (default 5m)

#### What Gets Pruned

- Only `toolResult` messages
- User + assistant messages are **never** modified
- Last `keepLastAssistants` (default 3) assistant messages are protected
- Tool results with image blocks are skipped

#### Pruning Types

- **Soft-trim**: Keeps head + tail, inserts `...`, appends note with original size
- **Hard-clear**: Replaces entire tool result with placeholder

```json
{
  "agent": {
    "contextPruning": {
      "mode": "cache-ttl",
      "ttl": "5m",
      "keepLastAssistants": 3,
      "softTrimRatio": 0.3,
      "hardClearRatio": 0.5
    }
  }
}
```

### Compaction

Separate from pruning. Compaction summarizes and persists context when sessions get long.

- **Auto-compaction** emits `compaction` stream events and can trigger a retry
- **Pre-compaction memory flush**: When near auto-compaction, runs a silent memory flush turn to remind the model to write durable notes to disk
- **Chat command**: `/compact` (optional instructions)

```json
{
  "agents": {
    "defaults": {
      "compaction": {
        "reserveTokensFloor": 20000,
        "memoryFlush": {
          "enabled": true,
          "softThresholdTokens": 4000,
          "systemPrompt": "Session nearing compaction. Store durable memories now.",
          "prompt": "Write any lasting notes to memory/YYYY-MM-DD.md; reply with NO_REPLY if nothing to store."
        }
      }
    }
  }
}
```

### Prompt Assembly

System prompt is built from:
1. OpenClaw's base prompt
2. Skills prompt (compact XML list of available skills)
3. Bootstrap context files (`AGENTS.md`, `SOUL.md`, `TOOLS.md`, `IDENTITY.md`, `USER.md`, `BOOTSTRAP.md`)
4. Per-run overrides

Model-specific limits and compaction reserve tokens are enforced.

### Steering While Streaming

When queue mode is `steer`, inbound messages are injected into the current run. The queue is checked after each tool call; remaining tool calls from the current assistant message are skipped.

## Hook Points

### Internal Hooks (Gateway Hooks)

- `agent:bootstrap`: Runs while building bootstrap files before system prompt is finalized
- Command hooks: `/new`, `/reset`, `/stop`

### Plugin Hooks (Agent + Gateway Lifecycle)

| Hook | When |
|---|---|
| `before_model_resolve` | Pre-session, override provider/model |
| `before_prompt_build` | After session load, inject context/system prompt |
| `agent_end` | Inspect final message list after completion |
| `before_compaction` / `after_compaction` | Observe compaction cycles |
| `before_tool_call` / `after_tool_call` | Intercept tool params/results |
| `tool_result_persist` | Transform tool results before transcript write |
| `message_received` / `message_sending` / `message_sent` | Inbound + outbound hooks |
| `session_start` / `session_end` | Session lifecycle boundaries |
| `gateway_start` / `gateway_stop` | Gateway lifecycle events |

## References

- [Architecture](https://docs.openclaw.ai/concepts/architecture)
- [Agent Runtime](https://docs.openclaw.ai/concepts/agent)
- [Agent Loop](https://docs.openclaw.ai/concepts/agent-loop)
- [Session Management](https://docs.openclaw.ai/concepts/session)
- [Session Pruning](https://docs.openclaw.ai/concepts/session-pruning)
- [Compaction](https://docs.openclaw.ai/concepts/compaction)
- [System Prompt](https://docs.openclaw.ai/concepts/system-prompt)
- [Streaming](https://docs.openclaw.ai/concepts/streaming)
