# OpenClaw — Automation, Scheduling & Webhooks

## Cron Jobs (Scheduled Tasks)

OpenClaw has a built-in cron system that lets agents run tasks on a schedule — autonomously, without user interaction.

### Schedule Types

| Type | Syntax | Example |
|---|---|---|
| **`at`** | Time string | `"at": "09:00"` — daily at 9 AM |
| **`every`** | Duration | `"every": "2h"` — every 2 hours |
| **`cron`** | Cron expression | `"cron": "0 */6 * * *"` — every 6 hours |

### Cron Job Definition

```json
{
  "cron": {
    "jobs": {
      "morning-briefing": {
        "schedule": { "at": "08:00" },
        "prompt": "Good morning! Give me a weather update and today's calendar summary.",
        "agent": "claw",
        "execution": "isolated",
        "delivery": "announce",
        "model": "haiku",
        "thinking": false
      },
      "backup-check": {
        "schedule": { "every": "6h" },
        "prompt": "Check if backups ran successfully. Alert me only if there are issues.",
        "execution": "main",
        "delivery": "webhook",
        "webhook": "https://hooks.slack.com/..."
      },
      "weekly-review": {
        "schedule": { "cron": "0 17 * * 5" },
        "prompt": "Summarize this week's activity from memory files.",
        "execution": "isolated",
        "delivery": "announce"
      }
    }
  }
}
```

### Execution Modes

| Mode | Session Key | Behavior |
|---|---|---|
| **`main`** | `agent:<agentId>:main` | Runs within the main session (shares context with normal chat) |
| **`isolated`** (default) | `cron:<jobId>` | Mints a fresh session per run (clean context each time) |

### Delivery Modes

| Mode | Behavior |
|---|---|
| **`announce`** | Sends the result to the agent's default channel (chat) |
| **`webhook`** | POSTs the result to a configured webhook URL |
| **`none`** | Runs silently, result only in session transcript |

### Overrides per Job

Each cron job can override:
- **`model`**: Use a different/cheaper model for scheduled tasks
- **`thinking`**: Enable/disable extended thinking
- **`agent`**: Bind to a specific agent in multi-agent setups
- **`timeout`**: Custom timeout for long-running tasks

### Agent Tools for Cron

The agent itself can manage cron jobs programmatically:

| Tool | Description |
|---|---|
| `cron_create` | Create a new cron job with schedule, prompt, and options |
| `cron_list` | List all active cron jobs |
| `cron_delete` | Remove a cron job by ID |

### CLI & RPC for Cron

```bash
openclaw cron list              # List all jobs
openclaw cron create ...        # Create a job
openclaw cron delete <id>       # Delete a job
openclaw cron run <id>          # Manually trigger a job
```

RPC methods: `cron.list`, `cron.create`, `cron.delete`, `cron.run`

---

## Hooks (Event-Driven Automation)

Hooks are event-driven handlers that fire on specific Gateway and agent lifecycle events.

### Hook Structure

Each hook is a directory containing:

```
hooks/
  my-hook/
    HOOK.md         # Hook manifest (describes the hook)
    handler.ts      # TypeScript handler executed on event
```

### HOOK.md Format

```markdown
# My Hook

## Description
What this hook does.

## Events
- command:slash
- agent:end
- message:received

## Priority
100

## Enabled
true
```

### Event Categories

#### Command Events
| Event | When |
|---|---|
| `command:slash` | Any slash command is invoked |
| `command:new` | `/new` command (session reset) |
| `command:reset` | `/reset` command |
| `command:stop` | `/stop` command (abort current run) |

#### Agent Events
| Event | When |
|---|---|
| `agent:start` | Agent run begins |
| `agent:end` | Agent run completes |
| `agent:error` | Agent run errors |
| `agent:bootstrap` | Bootstrap files being assembled |
| `agent:compaction` | Compaction triggered |

#### Gateway Events
| Event | When |
|---|---|
| `gateway:start` | Gateway daemon starts |
| `gateway:stop` | Gateway daemon stops |
| `gateway:client_connect` | Client connects via WebSocket |
| `gateway:client_disconnect` | Client disconnects |

#### Message Events
| Event | When |
|---|---|
| `message:received` | Inbound message from any channel |
| `message:sending` | Outbound message about to be sent |
| `message:sent` | Outbound message successfully sent |

### Bundled Hooks

| Hook | Description |
|---|---|
| **`session-memory`** | Indexes session transcripts into memory after each conversation |
| **`bootstrap-extra-files`** | Loads additional files into bootstrap context |
| **`command-logger`** | Logs all slash commands for debugging |
| **`boot-md`** | Processes BOOT.md files at startup |

### Hook Packs (npm packages)

Hooks can be distributed as npm packages:

```bash
npm install @openclaw/hook-session-memory
```

### Hook Discovery Order

1. **Workspace hooks**: `~/.openclaw/hooks/`
2. **Managed hooks**: `~/.openclaw/managed/hooks/`
3. **Bundled hooks**: Ships with OpenClaw

### Custom Hook Example

```typescript
// hooks/auto-label/handler.ts
import type { HookHandler } from '@openclaw/sdk';

export default {
  async handle(event, context) {
    if (event.type === 'message:received') {
      const text = event.payload.text;
      if (text.includes('urgent') || text.includes('ASAP')) {
        // Inject priority context into the agent's bootstrap
        context.addBootstrapNote('⚠️ User marked this as urgent. Prioritize accordingly.');
      }
    }
  }
} satisfies HookHandler;
```

---

## Webhooks (HTTP Endpoints)

OpenClaw exposes HTTP webhook endpoints that external services can call to trigger agent actions.

### Endpoints

#### `POST /hooks/wake`

Wake up the agent with a message from an external system:

```bash
curl -X POST http://localhost:18789/hooks/wake \
  -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "New deployment completed for production",
    "sessionKey": "hook:deploy-notify",
    "agent": "claw"
  }'
```

#### `POST /hooks/agent`

Trigger a full agent run (not just a message):

```bash
curl -X POST http://localhost:18789/hooks/agent \
  -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Analyze the latest error logs and summarize findings",
    "sessionKey": "hook:error-analysis",
    "agent": "claw",
    "model": "sonnet",
    "thinking": true,
    "wait": true
  }'
```

### Session Key Policy for Webhooks

| Policy | Behavior |
|---|---|
| **`unique`** (default) | Each webhook call gets `hook:<uuid>` — fresh context |
| **`provided`** | Use the `sessionKey` from the request body — share context across calls |
| **`mapped`** | Map to a pre-configured session key based on webhook ID |

### Mapped Hooks

Pre-configure webhook-to-session mappings:

```json
{
  "webhooks": {
    "mappedHooks": {
      "deploy-notify": {
        "sessionKey": "agent:claw:deploys",
        "agent": "claw",
        "prompt": "A deployment event occurred: {{body.message}}"
      },
      "github-pr": {
        "sessionKey": "agent:claw:github",
        "agent": "claw"
      }
    }
  }
}
```

### Authentication

- Bearer token auth via `OPENCLAW_GATEWAY_TOKEN`
- If no token is set, webhooks are open (not recommended for production)

### Gmail Pub/Sub Integration

OpenClaw has built-in support for Gmail push notifications:

```json
{
  "webhooks": {
    "gmail": {
      "enabled": true,
      "topicName": "projects/my-project/topics/gmail-push",
      "subscriptionName": "projects/my-project/subscriptions/openclaw-gmail",
      "labels": ["inbox"],
      "agent": "claw",
      "prompt": "New email received. Process and respond if needed."
    }
  }
}
```

---

## Heartbeats

Heartbeats are periodic nudges from the Gateway to the agent runtime.

### Configuration

```json
{
  "agents": {
    "defaults": {
      "heartbeat": {
        "enabled": true,
        "intervalMinutes": 30,
        "prompt": "Check if there's anything pending or if the user needs follow-up."
      }
    }
  }
}
```

### Behavior

- Fires at the configured interval while the Gateway is running
- Runs in the main session (has full context)
- Agent can decide to take action or be silent
- Useful for proactive notifications, reminders, monitoring

---

## Automation Patterns

### CI/CD Integration

```json
{
  "webhooks": {
    "mappedHooks": {
      "ci-complete": {
        "sessionKey": "agent:claw:ci",
        "prompt": "CI pipeline {{body.status}} for {{body.repo}}@{{body.branch}}. {{body.message}}"
      }
    }
  }
}
```

### Monitoring & Alerting

```json
{
  "cron": {
    "jobs": {
      "health-check": {
        "schedule": { "every": "5m" },
        "prompt": "Run health checks on all services. Alert only on failures.",
        "model": "haiku",
        "delivery": "none"
      }
    }
  }
}
```

### Daily Summaries

```json
{
  "cron": {
    "jobs": {
      "daily-summary": {
        "schedule": { "at": "18:00" },
        "prompt": "Review today's memory files and conversations. Compile a daily summary.",
        "execution": "isolated",
        "delivery": "announce"
      }
    }
  }
}
```

### External Service Setup (Example: GitHub Webhooks)

1. Configure OpenClaw webhook endpoint
2. Set up GitHub webhook to POST to `http://your-server:18789/hooks/wake`
3. Map the webhook to a session and agent
4. Agent receives GitHub events and can respond, create issues, etc.

## References

- [Cron Jobs](https://docs.openclaw.ai/concepts/cron)
- [Hooks](https://docs.openclaw.ai/concepts/hooks)
- [Webhooks](https://docs.openclaw.ai/concepts/webhook)
- [Configuration](https://docs.openclaw.ai/reference/configuration)
