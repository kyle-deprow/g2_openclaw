---
name: openclaw-multi-agent
description:
  OpenClaw multi-agent orchestration — spawning subagents, inter-agent communication, session tools, agent routing, and delegation patterns. Use when designing multi-agent architectures, configuring agent routing across channels, setting up subagent tool access and sandbox visibility, implementing delegation/supervisor/pipeline patterns, or debugging inter-agent communication. Triggers on tasks involving sessions_spawn, sessions_send, agent allowlists, sandbox visibility, channel-agent bindings, or multi-agent config.
---

# OpenClaw Multi-Agent Orchestration

Design, configure, and operate multi-agent systems — from simple delegation to
complex collaborative workflows.

## When to Apply

Reference these guidelines when:

- Designing multi-agent architectures with specialist agents
- Configuring agent routing to map channels to agents
- Spawning subagents for delegated tasks
- Setting up inter-agent communication via sessions_send
- Configuring subagent tool access and sandbox visibility
- Implementing delegation, supervisor, or pipeline patterns
- Setting up agent allowlists and send policies
- Debugging inter-agent communication failures or routing issues

## Rule Categories by Priority

| Priority | Category                | Impact   | Prefix       |
| -------- | ----------------------- | -------- | ------------ |
| 1        | Agent Design            | CRITICAL | `agent-`     |
| 2        | Session Tools           | CRITICAL | `st-`        |
| 3        | Subagent Configuration  | HIGH     | `sub-`       |
| 4        | Routing & Bindings      | HIGH     | `route-`     |
| 5        | Orchestration Patterns  | MEDIUM   | `orch-`      |

---

## 1. Agent Design (CRITICAL)

### `agent-orthogonal-specialists`
Design agents as orthogonal specialists. Each agent should have a distinct role
that doesn't overlap with others:

```json
{
  "agents": {
    "agents": {
      "claw": {
        "name": "Claw",
        "emoji": "🦞",
        "model": "sonnet",
        "skills": ["general"],
        "description": "General coordinator — routes and synthesizes"
      },
      "researcher": {
        "name": "Atlas",
        "emoji": "🦉",
        "model": "opus",
        "skills": ["web-research", "deep-research"],
        "description": "Deep research and source verification"
      },
      "coder": {
        "name": "Forge",
        "emoji": "⚒️",
        "model": "sonnet",
        "skills": ["coding", "git", "code-review"],
        "description": "Code writing, review, and infrastructure"
      },
      "writer": {
        "name": "Quill",
        "emoji": "✍️",
        "model": "opus",
        "skills": ["writing", "editing"],
        "description": "Content creation and editing"
      }
    }
  }
}
```

**Bad:** Two agents that both "write code and research topics" — they'll confuse
routing and waste resources duplicating effort.

### `agent-model-per-role`
Match model to role complexity:

| Role                    | Recommended Model | Reasoning                     |
| ----------------------- | ----------------- | ----------------------------- |
| General coordinator     | Sonnet            | Fast routing, light synthesis |
| Deep researcher         | Opus              | Complex analysis, long context|
| Coder                   | Sonnet            | Fast, accurate code generation|
| Routine summarizer      | Haiku             | Cheap, sufficient for digests |
| Creative writer         | Opus              | Nuance, style, creativity    |

### `agent-per-agent-identity`
Each agent should have its own IDENTITY.md and SOUL.md overrides:

```
~/.openclaw/
  SOUL.md                    # Shared base (if any)
  agents/
    researcher/
      IDENTITY.md            # Atlas the Owl
      SOUL.md                # Scholarly, thorough, citation-focused
    coder/
      IDENTITY.md            # Forge the Hammer
      SOUL.md                # Precise, efficient, test-driven
```

Agents must feel distinct in conversation. Users and other agents should
immediately recognize who they're talking to.

### `agent-defaults-then-override`
Configure shared defaults, then override per agent:

```json
{
  "agents": {
    "defaults": {
      "model": "sonnet",
      "thinking": true,
      "timeoutSeconds": 600,
      "tools": { "profile": "full" }
    },
    "agents": {
      "researcher": {
        "model": "opus",
        "tools": { "profile": "full", "deny": ["exec", "process"] }
      },
      "coder": {
        "tools": { "profile": "coding" }
      }
    }
  }
}
```

---

## 2. Session Tools — Inter-Agent Communication (CRITICAL)

### `st-four-tools`
Four session tools enable inter-agent communication:

| Tool               | Purpose                                    | Blocking     |
| ------------------ | ------------------------------------------ | ------------ |
| `sessions_list`    | List all active sessions                   | No           |
| `sessions_history` | Read conversation history of a session     | No           |
| `sessions_send`    | Send a message to another agent's session  | Optional     |
| `sessions_spawn`   | Spawn a subagent for a delegated task      | Optional     |

### `st-send-vs-spawn`
Know when to use each:

| Scenario                        | Use `sessions_send`      | Use `sessions_spawn`     |
| ------------------------------- | ------------------------ | ------------------------ |
| Quick question to another agent | ✅ Ping-pong reply       |                          |
| Delegated task with full run    |                          | ✅ Independent execution |
| Share information (no reply)    | ✅ With REPLY_SKIP       |                          |
| Parallel tasks                  |                          | ✅ Multiple spawns       |
| Need result in parent context   |                          | ✅ waitForComplete       |

### `st-sessions-send-patterns`
`sessions_send` is ping-pong: send a message, optionally wait for reply.

```
// Ask another agent a question and wait for answer
Tool: sessions_send
Input: {
  "sessionKey": "agent:researcher:main",
  "message": "What are the latest findings on transformer architectures?",
  "waitForReply": true
}

// Notify without expecting a reply
Tool: sessions_send
Input: {
  "sessionKey": "agent:coder:main",
  "message": "FYI: Research complete. Findings saved in memory/2026-02-21.md",
  "flags": ["REPLY_SKIP"]
}
```

### `st-sessions-spawn-patterns`
`sessions_spawn` creates a fresh subagent session for independent work:

```
// Delegate and wait for result
Tool: sessions_spawn
Input: {
  "agent": "researcher",
  "prompt": "Research top 5 React frameworks. Compare performance benchmarks.",
  "waitForComplete": true,
  "announceResult": true
}

// Fire and forget
Tool: sessions_spawn
Input: {
  "agent": "coder",
  "prompt": "Run the full test suite and report failures.",
  "waitForComplete": false,
  "announceResult": true
}
```

### `st-send-policy`
Control which agents can communicate:

```json
{
  "session": {
    "sendPolicy": {
      "allow": ["claw->researcher", "claw->coder", "claw->writer"],
      "deny": ["writer->coder"]
    }
  }
}
```

The coordinator (claw) can reach everyone. Specialists communicate through the
coordinator, not directly. This prevents circular communication loops.

---

## 3. Subagent Configuration (HIGH)

### `sub-tool-access`
Subagents inherit the parent's tool profile by default. Override for security:

```json
{
  "tools": {
    "subagents": {
      "tools": {
        "profile": "coding",
        "allow": ["exec", "Read", "Write", "Grep", "Glob"],
        "deny": ["browser", "message", "cron_create"]
      }
    }
  }
}
```

Principle: subagents should have **fewer** tools than the parent, not more.
They execute specific tasks — they don't need the full tool suite.

### `sub-per-spawn-override`
Override tool access per spawn for task-specific scoping:

```
Tool: sessions_spawn
Input: {
  "agent": "researcher",
  "prompt": "Search the web for...",
  "tools": {
    "allow": ["web_search", "web_fetch", "memory_search"],
    "deny": ["exec", "Write"]
  }
}
```

### `sub-sandbox-visibility`
Control which sessions a subagent can discover:

| Visibility | Subagent sees           | Use When                       |
| ---------- | ----------------------- | ------------------------------ |
| `own`      | Only its own session    | Default, isolated tasks        |
| `all`      | All sessions            | Collaborative workflows        |
| `none`     | No session tools at all | Pure computation, no comms     |

```json
{
  "session": {
    "sandbox": {
      "visibility": "own"
    }
  }
}
```

Default to `own`. Only use `all` when the subagent explicitly needs to read
other sessions' history for context.

### `sub-session-key-model`
Subagent sessions follow a nesting pattern:

```
agent:claw:main                        (parent)
  └── agent:claw:subagent:uuid-1       (child spawned by claw)
        └── agent:researcher:subagent:uuid-2  (grandchild)
```

Avoid deep nesting (>2 levels). If you need three agents in a chain, use
sequential spawns from the coordinator rather than nested spawns.

### `sub-announce-flow`
When `announceResult: true`, the subagent's result is posted to the originating
chat channel. The parent agent can see and process this announcement.

Use `announceResult: true` when:
- The user should see the subagent's output directly
- The parent needs to post-process the result

Use `announceResult: false` when:
- The task is a background check with no user-visible output
- The parent will synthesize multiple subagent results before responding

---

## 4. Routing & Bindings (HIGH)

### `route-channel-agent-bindings`
Map specific channels to specific agents:

```json
{
  "agents": {
    "agents": {
      "claw": {
        "channels": {
          "telegram": true,
          "whatsapp": true
        }
      },
      "coder": {
        "channels": {
          "slack": true
        }
      },
      "researcher": {
        "channels": {
          "discord": "research-channel-id"
        }
      }
    }
  }
}
```

Channel bindings route incoming messages to the right agent without the user
needing to specify.

### `route-explicit-commands`
Users can switch agents explicitly in chat:

```
/agent researcher      ← Switch to researcher
/agent claw           ← Switch back to coordinator
/agents               ← List available agents
```

### `route-allowlists`
Restrict which agents can spawn or send to others:

```json
{
  "agents": {
    "agents": {
      "claw": {
        "allowSpawn": ["researcher", "coder", "writer"],
        "allowSend": ["researcher", "coder", "writer"]
      },
      "researcher": {
        "allowSpawn": [],
        "allowSend": ["claw"]
      },
      "coder": {
        "allowSpawn": [],
        "allowSend": ["claw"]
      }
    }
  }
}
```

Hub-and-spoke: coordinator can reach all agents; specialists report back to
coordinator only. Prevents runaway inter-agent loops.

### `route-agents-list-tool`
The `agents_list` tool lets agents discover available peers:

```
Tool: agents_list
Input: {}
Output: [
  { "id": "claw", "name": "Claw", "emoji": "🦞", "description": "General coordinator" },
  { "id": "researcher", "name": "Atlas", "emoji": "🦉", "description": "Research" },
  ...
]
```

The coordinator uses this to decide which agent to delegate to based on the
user's request.

---

## 5. Orchestration Patterns (MEDIUM)

### `orch-delegation`
The primary agent delegates specific tasks to specialists:

```
User → Claw: "Research X and write a blog post"
Claw → spawn(researcher): "Research X comprehensively"
  Researcher → (web_search, web_fetch, memory) → findings
Claw → spawn(writer): "Write blog post from findings: ..."
  Writer → (drafts, edits) → blog post
Claw → User: "Here's your blog post"
```

Best for: clear task decomposition with sequential dependencies.

### `orch-parallel-fan-out`
Spawn multiple subagents simultaneously for independent subtasks:

```
User → Claw: "Compare these three frameworks"
Claw → spawn(researcher, "Research framework A")  ──┐
Claw → spawn(researcher, "Research framework B")  ──┼── parallel
Claw → spawn(researcher, "Research framework C")  ──┘
Claw waits for all three, synthesizes comparison
Claw → User: "Here's the comparison"
```

Best for: independent subtasks that can run concurrently.

### `orch-supervisor`
One agent monitors and manages others via cron:

```json
{
  "cron": {
    "jobs": {
      "team-health": {
        "schedule": { "every": "1h" },
        "agent": "claw",
        "prompt": "Check all agent sessions via sessions_list. Report stuck runs."
      }
    }
  }
}
```

Best for: monitoring long-running agent deployments.

### `orch-pipeline`
Sequential processing through a chain of specialists:

```
Data → Agent A (extract) → Agent B (analyze) → Agent C (report) → User
```

Implement with chained sessions_send or sequential spawns. Each agent's output
becomes the next agent's input.

### `orch-escalation`
Agents escalate to more capable peers when stuck:

```
Haiku agent attempts task
  → Fails or is uncertain
  → sessions_send to Sonnet agent with context
  → Sonnet resolves
  → Result returned to conversation
```

Use model cost as the escalation dimension: Haiku → Sonnet → Opus.

---

## Anti-Patterns

| Anti-Pattern | Why It's Wrong | Correct Approach |
| --- | --- | --- |
| Overlapping agent responsibilities | Confused routing, duplicated work | Orthogonal specialist roles |
| Subagents with full tool access | Overprivileged, security risk | Minimal tools per spawn |
| Deep spawn nesting (3+ levels) | Hard to debug, timeout risk | Sequential spawns from coordinator |
| No send policy | Agents communicate in loops | Hub-and-spoke with allowlists |
| All agents using Opus | Expensive for routine tasks | Match model to role complexity |
| Sandbox visibility: all by default | Subagents reading unrelated sessions | Default: own |
| No agent descriptions | Coordinator can't route intelligently | Clear, distinct descriptions per agent |
| Direct specialist-to-specialist comms | Bypasses coordinator, loses oversight | Route through the hub agent |

---

## Quick Config Template

```json
{
  "agents": {
    "defaults": {
      "model": "sonnet",
      "thinking": true,
      "timeoutSeconds": 600
    },
    "agents": {
      "claw": {
        "name": "Claw",
        "emoji": "🦞",
        "description": "General coordinator",
        "skills": ["general"],
        "allowSpawn": ["researcher", "coder"],
        "allowSend": ["researcher", "coder"]
      },
      "researcher": {
        "name": "Atlas",
        "emoji": "🦉",
        "model": "opus",
        "description": "Deep research and analysis",
        "skills": ["web-research"],
        "allowSpawn": [],
        "allowSend": ["claw"]
      },
      "coder": {
        "name": "Forge",
        "emoji": "⚒️",
        "description": "Code and infrastructure",
        "skills": ["coding", "git"],
        "tools": { "profile": "coding" },
        "allowSpawn": [],
        "allowSend": ["claw"]
      }
    }
  },
  "session": {
    "sandbox": { "visibility": "own" },
    "sendPolicy": {
      "allow": ["claw->researcher", "claw->coder", "researcher->claw", "coder->claw"]
    }
  },
  "tools": {
    "subagents": {
      "tools": { "deny": ["cron_create", "cron_delete", "message"] }
    }
  }
}
```

## References

- https://docs.openclaw.ai/concepts/session-tool
- https://docs.openclaw.ai/concepts/agent
- https://docs.openclaw.ai/concepts/session
- https://docs.openclaw.ai/reference/configuration
```
