---
name: openclaw-automation
description:
  OpenClaw automation through cron jobs, hooks, webhooks, and heartbeats. Use when scheduling recurring agent tasks, building event-driven handlers, configuring HTTP webhook endpoints, integrating external services via hooks, or setting up proactive agent behaviors. Triggers on tasks involving cron schedules, hook event handlers, webhook mapped hooks, Gmail Pub/Sub, heartbeat nudges, or CI/CD agent integration.
---

# OpenClaw Automation & Event System

Schedule tasks, react to events, and integrate external systems through cron
jobs, hooks, webhooks, and heartbeats.

## When to Apply

Reference these guidelines when:

- Creating cron jobs for scheduled agent tasks
- Choosing between main and isolated cron execution modes
- Building custom hooks for event-driven automation
- Configuring webhook endpoints for external service integration
- Setting up Gmail Pub/Sub or CI/CD agent triggers
- Designing heartbeat behaviors for proactive agent actions
- Choosing delivery modes for scheduled task output
- Debugging missed cron runs, webhook failures, or hook errors

## Rule Categories by Priority

| Priority | Category       | Impact   | Prefix       |
| -------- | -------------- | -------- | ------------ |
| 1        | Cron Jobs      | CRITICAL | `cron-`      |
| 2        | Hooks          | HIGH     | `hook-`      |
| 3        | Webhooks       | HIGH     | `webhook-`   |
| 4        | Heartbeats     | MEDIUM   | `heartbeat-` |
| 5        | Patterns       | MEDIUM   | `pattern-`   |

---

## 1. Cron Jobs (CRITICAL)

### `cron-schedule-types`
Three schedule formats, choose based on need:

| Type     | Syntax              | Example                  | Use When                    |
| -------- | ------------------- | ------------------------ | --------------------------- |
| `at`     | `"HH:MM"`           | `"at": "09:00"`          | Fixed daily time            |
| `every`  | Duration string      | `"every": "2h"`          | Regular interval            |
| `cron`   | Standard expression  | `"cron": "0 */6 * * *"` | Complex schedules           |

```json
{
  "cron": {
    "jobs": {
      "morning-brief": { "schedule": { "at": "08:00" } },
      "health-check": { "schedule": { "every": "5m" } },
      "weekly-review": { "schedule": { "cron": "0 17 * * 5" } }
    }
  }
}
```

Use `at` for human-timed tasks (daily briefings). Use `every` for monitoring.
Use `cron` for complex schedules (weekdays only, specific hours).

### `cron-execution-mode`
Choose execution mode based on whether the job needs conversation context:

| Mode         | Session Key             | Context                       | Use When                      |
| ------------ | ----------------------- | ----------------------------- | ----------------------------- |
| `isolated`   | `cron:<jobId>`          | Fresh each run                | Independent tasks, reports    |
| `main`       | `agent:<agentId>:main`  | Shares with normal chat       | Follow-ups, context-aware work|

```json
// ✅ Isolated — clean context for a standalone task
{
  "morning-brief": {
    "schedule": { "at": "08:00" },
    "execution": "isolated",
    "prompt": "Give me today's weather and calendar summary."
  }
}

// ✅ Main — job can see recent chat context
{
  "follow-up": {
    "schedule": { "every": "4h" },
    "execution": "main",
    "prompt": "Check if there are any pending tasks from our conversation."
  }
}
```

Default is `isolated`. Use `main` sparingly — it can clutter conversational flow.

### `cron-delivery-mode`
Choose where job output goes:

| Mode        | Behavior                                     | Use When                    |
| ----------- | -------------------------------------------- | --------------------------- |
| `announce`  | Sends result to the agent's default channel  | User-facing summaries       |
| `webhook`   | POSTs result to a URL                        | Integration with Slack, etc.|
| `none`      | Silent — output only in transcript           | Background monitoring       |

```json
{
  "health-check": {
    "schedule": { "every": "5m" },
    "delivery": "none",
    "prompt": "Run health checks. Alert me only on failures."
  }
}
```

Use `none` for frequent checks — announce only when there's something to report.
Let the agent decide when to escalate by instructing it in the prompt.

### `cron-model-override`
Use cheaper models for routine scheduled tasks:

```json
{
  "daily-digest": {
    "schedule": { "at": "18:00" },
    "model": "haiku",
    "thinking": false,
    "prompt": "Summarize today's activity from memory files."
  }
}
```

Haiku without thinking is ideal for summarization and monitoring cron jobs.
Reserve Sonnet/Opus for jobs requiring complex reasoning.

### `cron-agent-binding`
Bind cron jobs to specific agents in multi-agent setups:

```json
{
  "code-review": {
    "schedule": { "cron": "0 9 * * 1-5" },
    "agent": "coder",
    "prompt": "Review yesterday's git commits for issues."
  },
  "research-digest": {
    "schedule": { "at": "08:00" },
    "agent": "researcher",
    "prompt": "Search for new papers in our research areas."
  }
}
```

### `cron-management-tools`
The agent can self-manage cron jobs via tools:

| Tool             | Purpose                           |
| ---------------- | --------------------------------- |
| `cron_create`    | Create a new scheduled job        |
| `cron_list`      | List all active jobs              |
| `cron_delete`    | Remove a job by ID                |

CLI equivalents: `openclaw cron list`, `openclaw cron create`, `openclaw cron delete`,
`openclaw cron run <id>` (manual trigger).

---

## 2. Hooks — Event-Driven Handlers (HIGH)

### `hook-structure`
Each hook is a directory with a manifest and handler:

```
hooks/
  my-hook/
    HOOK.md         # Manifest: events, priority, enabled
    handler.ts      # TypeScript handler
```

### `hook-manifest-format`
HOOK.md declares what events the hook listens to:

```markdown
# Audit Logger

## Description
Logs all agent actions and tool calls for compliance auditing.

## Events
- agent:start
- agent:end
- agent:error
- command:slash

## Priority
100

## Enabled
true
```

### `hook-event-categories`
Four event categories, each with specific events:

**Command Events** — User-initiated slash commands:
| Event           | Trigger                     |
| --------------- | --------------------------- |
| `command:slash`  | Any slash command invoked   |
| `command:new`    | `/new` — session reset     |
| `command:reset`  | `/reset` — full reset      |
| `command:stop`   | `/stop` — abort current run|

**Agent Events** — Agent runtime lifecycle:
| Event              | Trigger                         |
| ------------------ | ------------------------------- |
| `agent:start`      | Agent run begins                |
| `agent:end`        | Agent run completes             |
| `agent:error`      | Agent run errors                |
| `agent:bootstrap`  | Bootstrap files being assembled |
| `agent:compaction`  | Compaction triggered            |

**Gateway Events** — Gateway process lifecycle:
| Event                      | Trigger                  |
| -------------------------- | ------------------------ |
| `gateway:start`            | Gateway daemon starts    |
| `gateway:stop`             | Gateway daemon stops     |
| `gateway:client_connect`   | Client WS connection     |
| `gateway:client_disconnect`| Client WS disconnection  |

**Message Events** — Inbound and outbound messages:
| Event              | Trigger                         |
| ------------------ | ------------------------------- |
| `message:received` | Inbound from any channel        |
| `message:sending`  | Outbound about to be sent       |
| `message:sent`     | Outbound successfully sent      |

### `hook-handler-implementation`
Handlers receive the event object and a context API:

```typescript
import type { HookHandler } from '@openclaw/sdk';

export default {
  async handle(event, context) {
    if (event.type === 'agent:end') {
      const duration = event.payload.endedAt - event.payload.startedAt;
      if (duration > 60000) {
        context.addBootstrapNote('Previous run took over 60s. Consider simpler approaches.');
      }
    }

    if (event.type === 'message:received') {
      const text = event.payload.text.toLowerCase();
      if (text.includes('urgent') || text.includes('asap')) {
        context.addBootstrapNote('User marked this as urgent. Prioritize.');
      }
    }
  }
} satisfies HookHandler;
```

### `hook-bundled-essentials`
OpenClaw ships with these hooks — understand what they do before building custom:

| Hook                      | Purpose                                          |
| ------------------------- | ------------------------------------------------ |
| `session-memory`          | Indexes transcripts into vector memory           |
| `bootstrap-extra-files`   | Loads additional files into bootstrap context     |
| `command-logger`          | Logs all slash commands for debugging             |
| `boot-md`                 | Processes BOOT.md files at startup               |

### `hook-discovery-order`
Hooks load from three locations (highest priority first):

1. Workspace: `~/.openclaw/hooks/`
2. Managed: `~/.openclaw/managed/hooks/`
3. Bundled: ships with OpenClaw

### `hook-packs-npm`
Distribute hooks as npm packages for team sharing:

```bash
npm install @my-org/openclaw-hook-audit-logger
```

---

## 3. Webhooks — HTTP Endpoints (HIGH)

### `webhook-two-endpoints`
OpenClaw exposes two webhook endpoints:

| Endpoint             | Purpose                          | Response                    |
| -------------------- | -------------------------------- | --------------------------- |
| `POST /hooks/wake`   | Send a message to trigger agent  | Message queued              |
| `POST /hooks/agent`  | Trigger a full agent run         | Run result (if wait: true)  |

### `webhook-wake-vs-agent`
Use `/hooks/wake` for lightweight notifications. Use `/hooks/agent` for tasks
that need a full agent run with tools:

```bash
# Wake — lightweight notification
curl -X POST http://localhost:18789/hooks/wake \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "Deploy completed for production"}'

# Agent — full run with tools
curl -X POST http://localhost:18789/hooks/agent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Analyze error logs and summarize", "wait": true}'
```

### `webhook-session-key-policy`
Control how webhook calls are scoped to sessions:

| Policy      | Behavior                                   | Use When                    |
| ----------- | ------------------------------------------ | --------------------------- |
| `unique`    | `hook:<uuid>` per call (default)           | Independent events          |
| `provided`  | Use sessionKey from request body           | Shared context across calls |
| `mapped`    | Pre-configured mapping by hook ID          | Known integrations          |

### `webhook-mapped-hooks`
Pre-configure webhook-to-session mappings for known integrations:

```json
{
  "webhooks": {
    "mappedHooks": {
      "deploy-notify": {
        "sessionKey": "agent:claw:deploys",
        "agent": "claw",
        "prompt": "Deployment event: {{body.message}}"
      },
      "github-pr": {
        "sessionKey": "agent:claw:github",
        "agent": "claw"
      }
    }
  }
}
```

Mapped hooks with stable session keys let the agent build context across
related events (e.g., all deploys share one session).

### `webhook-auth-required`
Always authenticate webhooks in production:

```bash
export OPENCLAW_GATEWAY_TOKEN="$(openssl rand -hex 32)"
```

External services must include `Authorization: Bearer <token>` in requests.
Without this, anyone with network access can trigger agent runs.

### `webhook-gmail-pubsub`
Built-in Gmail push notification support:

```json
{
  "webhooks": {
    "gmail": {
      "enabled": true,
      "topicName": "projects/my-project/topics/gmail-push",
      "labels": ["inbox"],
      "agent": "claw",
      "prompt": "New email received. Triage and respond if needed."
    }
  }
}
```

---

## 4. Heartbeats (MEDIUM)

### `heartbeat-proactive-nudge`
Heartbeats are periodic nudges from Gateway to agent. The agent can act or stay
silent:

```json
{
  "agents": {
    "defaults": {
      "heartbeat": {
        "enabled": true,
        "intervalMinutes": 30,
        "prompt": "Check for pending tasks or needed follow-ups."
      }
    }
  }
}
```

### `heartbeat-main-session`
Heartbeats run in the main session — they have full conversation context. This
makes them ideal for:
- Reminders about tasks mentioned in conversation
- Follow-ups on unanswered questions
- Proactive status updates

### `heartbeat-vs-cron`
Know when to use each:

| Feature       | Heartbeat                 | Cron Job                    |
| ------------- | ------------------------- | --------------------------- |
| Session       | Main (shared context)     | Main or isolated            |
| Schedule      | Fixed interval only       | at, every, or cron expr     |
| Tools         | Full access               | Full access                 |
| Best for      | Proactive follow-ups      | Scheduled independent tasks |

---

## 5. Automation Patterns (MEDIUM)

### `pattern-ci-cd-integration`
Connect CI/CD pipelines to agent webhooks:

```json
{
  "webhooks": {
    "mappedHooks": {
      "ci-pipeline": {
        "sessionKey": "agent:claw:ci",
        "prompt": "CI pipeline {{body.status}} for {{body.repo}}@{{body.branch}}."
      }
    }
  }
}
```

### `pattern-monitor-and-alert`
Frequent silent monitoring with selective alerting:

```json
{
  "cron": {
    "jobs": {
      "service-health": {
        "schedule": { "every": "5m" },
        "model": "haiku",
        "delivery": "none",
        "prompt": "Check service endpoints. Only alert if something is down."
      }
    }
  }
}
```

### `pattern-daily-summary`
End-of-day summary from memory and conversation history:

```json
{
  "cron": {
    "jobs": {
      "daily-summary": {
        "schedule": { "at": "18:00" },
        "execution": "isolated",
        "delivery": "announce",
        "prompt": "Review today's memory files. Compile a concise daily summary."
      }
    }
  }
}
```

### `pattern-escalation-chain`
Combine hooks and webhooks for escalation workflows:

```
1. Cron job detects issue (delivery: none)
2. Agent writes finding to memory
3. Agent sends message to Slack channel via webhook
4. If unresolved after 30 min, heartbeat re-checks
5. If still unresolved, agent escalates via sessions_send to another agent
```

---

## Anti-Patterns

| Anti-Pattern | Why It's Wrong | Correct Approach |
| --- | --- | --- |
| Cron with delivery: announce every 5m | Spam the user's chat channel | Use delivery: none for frequent checks |
| All cron jobs using Opus model | Expensive for routine tasks | Use Haiku for summaries, Sonnet for reasoning |
| Cron in main session for independent tasks | Pollutes conversational context | Use isolated execution for standalone jobs |
| No webhook auth in production | Arbitrary agent triggering | Set OPENCLAW_GATEWAY_TOKEN always |
| Unique session key for related events | Agent loses context between events | Use mapped hooks with stable session keys |
| Plugin hooks without error handling | Hook crash kills the event pipeline | Wrap all hook handlers in try/catch |
| Heartbeat interval under 5 minutes | CPU and token waste | 15-30 min minimum for heartbeats |

## References

- https://docs.openclaw.ai/concepts/cron
- https://docs.openclaw.ai/concepts/hooks
- https://docs.openclaw.ai/concepts/webhook
- https://docs.openclaw.ai/reference/configuration
