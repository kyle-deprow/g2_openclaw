# OpenClaw — Multi-Agent & Subagents

## Overview

OpenClaw supports **multi-agent architectures** where multiple agents can coexist, communicate, and spawn sub-agents. Each agent has its own identity, tools, skills, and session context.

## Agent Configuration

### Defining Multiple Agents

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
        "model": "sonnet",
        "skills": ["general", "coding"],
        "description": "General-purpose assistant"
      },
      "researcher": {
        "name": "Atlas",
        "emoji": "🦉",
        "model": "opus",
        "skills": ["web-research", "deep-research"],
        "description": "Deep research and analysis"
      },
      "devops": {
        "name": "Forge",
        "emoji": "⚒️",
        "model": "sonnet",
        "skills": ["coding", "git", "docker"],
        "description": "Infrastructure and deployment"
      },
      "writer": {
        "name": "Quill",
        "emoji": "✍️",
        "model": "opus",
        "skills": ["writing", "editing"],
        "description": "Content writing and editing"
      }
    }
  }
}
```

### Per-Agent Properties

| Property | Description |
|---|---|
| `name` | Display name |
| `emoji` | Agent emoji for chat |
| `model` | LLM model override |
| `thinking` | Enable/disable extended thinking |
| `skills` | List of skill names to load |
| `tools` | Tool profile/allow/deny overrides |
| `description` | Agent purpose (used for routing) |
| `channels` | Channel bindings |
| `session` | Session configuration overrides |

---

## Session Tools (Inter-Agent Communication)

### `sessions_list`

List all active sessions:

```
Tool: sessions_list
Input: {}
Output: [
  { "key": "agent:claw:main", "id": "abc-123", "updatedAt": "...", "agent": "claw" },
  { "key": "agent:researcher:main", "id": "def-456", "updatedAt": "...", "agent": "researcher" },
  ...
]
```

### `sessions_history`

Read the conversation history of a session:

```
Tool: sessions_history
Input: { "sessionKey": "agent:researcher:main", "limit": 20 }
Output: [
  { "role": "user", "content": "Research quantum computing advances..." },
  { "role": "assistant", "content": "Here's what I found..." },
  ...
]
```

### `sessions_send`

Send a message to another agent's session (ping-pong communication):

```
Tool: sessions_send
Input: {
  "sessionKey": "agent:researcher:main",
  "message": "Can you look up the latest papers on transformer architectures?",
  "waitForReply": true
}
Output: {
  "reply": "I found several recent papers on transformer architectures..."
}
```

#### Send Behavior Flags

| Flag | Behavior |
|---|---|
| `waitForReply` | Block until the target agent responds |
| `REPLY_SKIP` | Send without expecting a reply |
| `ANNOUNCE_SKIP` | Skip channel announcement of the send |

#### Send Policy

Control which agents can send to which:

```json
{
  "session": {
    "sendPolicy": {
      "allow": ["claw->researcher", "claw->devops"],
      "deny": ["writer->devops"]
    }
  }
}
```

### `sessions_spawn`

Spawn a sub-agent for a specific task:

```
Tool: sessions_spawn
Input: {
  "agent": "researcher",
  "prompt": "Research the top 5 Python web frameworks in 2024 and compare their performance benchmarks.",
  "waitForComplete": true,
  "announceResult": true
}
Output: {
  "sessionKey": "agent:claw:subagent:uuid-here",
  "result": "Here's my analysis of the top 5 Python web frameworks..."
}
```

#### Spawn Options

| Option | Description |
|---|---|
| `agent` | Which agent to spawn (defaults to same agent) |
| `prompt` | The task/instruction for the subagent |
| `waitForComplete` | Wait for the sub-agent run to finish |
| `announceResult` | Send the result back to the spawning chat |
| `threadBound` | Keep the sub-agent in a thread context |
| `model` | Override model for the sub-agent run |
| `thinking` | Override thinking for the sub-agent run |
| `tools` | Override tool access for the sub-agent |
| `timeout` | Custom timeout |

---

## Subagent Architecture

### Session Key Model

When an agent spawns a sub-agent, the sub-agent gets its own session:

```
agent:claw:main  (parent)
  └── agent:claw:subagent:uuid-1  (child)
        └── agent:researcher:subagent:uuid-2  (grandchild)
```

### Subagent Tool Access

By default, subagents inherit the parent's tool configuration. This can be overridden:

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

Per-spawn overrides:

```
Tool: sessions_spawn
Input: {
  "agent": "researcher",
  "prompt": "...",
  "tools": {
    "allow": ["web_search", "web_fetch", "memory_search"],
    "deny": ["exec"]
  }
}
```

### Sandbox Session Visibility

Controls what sessions a subagent can see via `sessions_list` and `sessions_history`:

```json
{
  "session": {
    "sandbox": {
      "visibility": "own"
    }
  }
}
```

| Visibility | Behavior |
|---|---|
| **`own`** (default) | Subagent sees only its own session |
| **`all`** | Subagent can see all sessions (including parent) |
| **`none`** | No session tools available to subagent |

### Thread-Bound Mode

When `threadBound: true`, the sub-agent's context is tied to a thread in the parent's chat:

- Sub-agent messages appear as thread replies
- Parent can see the sub-agent's progress in real-time
- Useful for collaborative workflows

### Announce Flow

When `announceResult: true`:

1. Sub-agent completes its run
2. Result is formatted as a message
3. Message is sent to the original channel where the spawn was triggered
4. Parent agent can then process the result

---

## Agent Routing

### Channel-Agent Bindings

Route specific channels to specific agents:

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
      "devops": {
        "channels": {
          "slack": true,
          "discord": "devops-channel-id"
        }
      }
    }
  }
}
```

### Explicit Agent Commands

Users can switch agents in chat:

```
/agent researcher
/agent claw
/agents              # List available agents
```

### Agent Allowlists

Restrict which agents can spawn or communicate with others:

```json
{
  "agents": {
    "agents": {
      "claw": {
        "allowSpawn": ["researcher", "devops"],
        "allowSend": ["researcher", "devops", "writer"]
      },
      "writer": {
        "allowSpawn": [],
        "allowSend": ["claw"]
      }
    }
  }
}
```

### `agents_list` Tool

The agent can discover available agents:

```
Tool: agents_list
Input: {}
Output: [
  { "id": "claw", "name": "Claw", "emoji": "🦞", "description": "General-purpose assistant" },
  { "id": "researcher", "name": "Atlas", "emoji": "🦉", "description": "Deep research and analysis" },
  ...
]
```

---

## Multi-Agent Patterns

### Delegation Pattern

Primary agent delegates specific tasks to specialists:

```
User → Claw: "Research quantum computing and write a blog post"
Claw → sessions_spawn(researcher): "Research quantum computing advances in 2024"
Researcher → (searches web, reads papers, compiles findings)
Researcher → Claw: "Here are the findings..."
Claw → sessions_spawn(writer): "Write a blog post based on these findings: ..."
Writer → (drafts, edits, formats)
Writer → Claw: "Here's the blog post..."
Claw → User: "Here's your blog post on quantum computing!"
```

### Supervisor Pattern

One agent monitors and manages others:

```json
{
  "cron": {
    "jobs": {
      "team-check": {
        "schedule": { "every": "1h" },
        "agent": "claw",
        "prompt": "Check on all agent sessions. Report any stuck or errored runs."
      }
    }
  }
}
```

### Pipeline Pattern

Sequential processing through multiple agents:

```
Data → Agent A (extract) → Agent B (analyze) → Agent C (report) → User
```

Implemented via chained `sessions_send` or `sessions_spawn` calls.

### Collaborative Pattern

Multiple agents contributing to a shared context:

```
User asks complex question
  → Claw spawns Researcher for data gathering
  → Claw spawns DevOps for infrastructure check
  → Claw synthesizes both responses
  → Claw responds to User
```

---

## Agent Discovery & Management

### CLI Commands

```bash
openclaw agents list           # List all configured agents
openclaw agent <id> "prompt"   # Send prompt to specific agent
openclaw sessions list         # List all active sessions
openclaw sessions history <key> # View session history
```

### RPC Methods

| Method | Description |
|---|---|
| `agents.list` | List agents |
| `agent` | Run agent with params |
| `sessions.list` | List sessions |
| `sessions.history` | Get session history |
| `sessions.send` | Send message to session |
| `sessions.spawn` | Spawn subagent |

## References

- [Session Tools](https://docs.openclaw.ai/concepts/session-tool)
- [Agent Runtime](https://docs.openclaw.ai/concepts/agent)
- [Session Management](https://docs.openclaw.ai/concepts/session)
- [Configuration](https://docs.openclaw.ai/reference/configuration)
