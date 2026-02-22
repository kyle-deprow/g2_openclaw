# OpenClaw — Configuration Reference

## Overview

OpenClaw is configured via a JSON configuration file and environment variables. The configuration controls agents, models, tools, sessions, memory, cron, webhooks, plugins, and more.

## Configuration File Location

```
~/.openclaw/config.json           # Primary config
~/.openclaw/config.local.json     # Local overrides (gitignored)
```

Settings are merged: `config.local.json` overrides `config.json`.

---

## Top-Level Structure

```json
{
  "agents": { ... },
  "mcp": { ... },
  "tools": { ... },
  "memory": { ... },
  "session": { ... },
  "cron": { ... },
  "webhooks": { ... },
  "channels": { ... },
  "providers": { ... },
  "plugins": { ... },
  "gateway": { ... }
}
```

---

## Agents Configuration

```json
{
  "agents": {
    "defaults": {
      "model": "sonnet",
      "thinking": true,
      "verbose": false,
      "timeoutSeconds": 600,
      "tools": {
        "profile": "full",
        "groups": { ... },
        "allow": [],
        "deny": []
      },
      "compaction": {
        "reserveTokensFloor": 20000,
        "memoryFlush": {
          "enabled": true,
          "softThresholdTokens": 4000,
          "systemPrompt": "...",
          "prompt": "..."
        }
      },
      "heartbeat": {
        "enabled": false,
        "intervalMinutes": 30,
        "prompt": "..."
      },
      "contextPruning": {
        "mode": "off",
        "ttl": "5m",
        "keepLastAssistants": 3,
        "softTrimRatio": 0.3,
        "hardClearRatio": 0.5
      }
    },
    "agents": {
      "<agentId>": {
        "name": "...",
        "emoji": "...",
        "model": "...",
        "thinking": true,
        "skills": ["..."],
        "tools": { ... },
        "channels": { ... },
        "session": { ... },
        "description": "...",
        "allowSpawn": ["..."],
        "allowSend": ["..."]
      }
    }
  }
}
```

---

## Model & Provider Configuration

### Supported Providers

| Provider | Models |
|---|---|
| **Anthropic** | Claude Opus, Sonnet, Haiku (all generations) |
| **OpenAI** | GPT-4o, GPT-4, o1, o3, etc. |
| **Google** | Gemini Pro, Flash, Ultra |
| **Groq** | Llama, Mixtral variants |
| **Ollama** | Any locally hosted model |
| **Custom** | Any OpenAI-compatible API |

### Provider Configuration

```json
{
  "providers": {
    "anthropic": {
      "apiKey": "env:ANTHROPIC_API_KEY",
      "defaultModel": "claude-sonnet-4-20250514"
    },
    "openai": {
      "apiKey": "env:OPENAI_API_KEY",
      "defaultModel": "gpt-4o"
    },
    "google": {
      "apiKey": "env:GOOGLE_AI_API_KEY"
    },
    "ollama": {
      "baseUrl": "http://localhost:11434"
    },
    "custom": {
      "baseUrl": "https://my-proxy.example.com/v1",
      "apiKey": "env:CUSTOM_API_KEY",
      "defaultModel": "my-model"
    }
  }
}
```

### Model Aliases

Shorthand aliases for common models:

| Alias | Resolves To |
|---|---|
| `sonnet` | `claude-sonnet-4-20250514` |
| `opus` | `claude-opus-4-20250514` |
| `haiku` | `claude-haiku-3-5-20241022` |
| `gpt4o` | `gpt-4o` |
| `flash` | `gemini-2.0-flash` |
| `pro` | `gemini-2.5-pro` |

---

## Session Configuration

```json
{
  "session": {
    "dmScope": "main",
    "dailyResetHour": 4,
    "idleMinutes": null,
    "resetByType": {
      "direct": { "dailyResetHour": 4 },
      "group": { "dailyResetHour": null, "idleMinutes": 60 },
      "thread": { "idleMinutes": 30 }
    },
    "resetByChannel": {
      "slack": { "idleMinutes": 120 }
    },
    "identityLinks": {
      "alice": ["telegram:123", "discord:456"]
    },
    "sandbox": {
      "visibility": "own"
    },
    "sendPolicy": {
      "allow": [],
      "deny": []
    }
  }
}
```

---

## MCP Configuration

```json
{
  "mcp": {
    "servers": {
      "<serverName>": {
        "command": "npx",
        "args": ["-y", "@package/mcp-server"],
        "env": { "KEY": "env:VAR" },
        "disabled": false
      },
      "<serverName>": {
        "url": "http://localhost:3001/mcp",
        "transport": "streamable-http"
      }
    }
  }
}
```

---

## Memory Configuration

```json
{
  "memory": {
    "enabled": true,
    "vectorSearch": {
      "enabled": true,
      "backend": "sqlite",
      "sqliteVec": false,
      "embeddingProvider": "local",
      "embeddingModel": "default",
      "chunkSize": 512,
      "chunkOverlap": 50,
      "hybridWeight": 0.7,
      "topK": 20,
      "mmr": {
        "enabled": true,
        "lambda": 0.7,
        "fetchMultiplier": 3
      },
      "temporalDecay": {
        "enabled": true,
        "halfLifeDays": 30,
        "weight": 0.2
      }
    },
    "sessionMemory": {
      "enabled": false,
      "indexTranscripts": false
    }
  }
}
```

---

## Cron Configuration

```json
{
  "cron": {
    "jobs": {
      "<jobId>": {
        "schedule": {
          "at": "HH:MM",
          "every": "<duration>",
          "cron": "<expression>"
        },
        "prompt": "...",
        "agent": "<agentId>",
        "execution": "isolated",
        "delivery": "announce",
        "model": "...",
        "thinking": false,
        "timeout": 300,
        "webhook": "https://..."
      }
    }
  }
}
```

---

## Webhook Configuration

```json
{
  "webhooks": {
    "sessionKeyPolicy": "unique",
    "mappedHooks": {
      "<hookId>": {
        "sessionKey": "...",
        "agent": "<agentId>",
        "prompt": "..."
      }
    },
    "gmail": {
      "enabled": false,
      "topicName": "...",
      "subscriptionName": "...",
      "labels": ["inbox"],
      "agent": "<agentId>",
      "prompt": "..."
    }
  }
}
```

---

## Channel Configuration

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "env:TELEGRAM_BOT_TOKEN",
      "allowedUsers": ["user1", "user2"]
    },
    "whatsapp": {
      "enabled": true,
      "phoneNumberId": "env:WHATSAPP_PHONE_ID",
      "accessToken": "env:WHATSAPP_TOKEN",
      "verifyToken": "env:WHATSAPP_VERIFY_TOKEN"
    },
    "slack": {
      "enabled": true,
      "botToken": "env:SLACK_BOT_TOKEN",
      "appToken": "env:SLACK_APP_TOKEN",
      "signingSecret": "env:SLACK_SIGNING_SECRET"
    },
    "discord": {
      "enabled": true,
      "token": "env:DISCORD_TOKEN",
      "applicationId": "env:DISCORD_APP_ID"
    },
    "imessage": {
      "enabled": true
    },
    "signal": {
      "enabled": true,
      "number": "env:SIGNAL_NUMBER"
    },
    "matrix": {
      "enabled": true,
      "homeserver": "https://matrix.org",
      "accessToken": "env:MATRIX_TOKEN"
    },
    "googlechat": {
      "enabled": true,
      "serviceAccountKey": "env:GOOGLE_CHAT_SA_KEY"
    },
    "msteams": {
      "enabled": true,
      "appId": "env:TEAMS_APP_ID",
      "appPassword": "env:TEAMS_APP_PASSWORD"
    }
  }
}
```

---

## Gateway Configuration

```json
{
  "gateway": {
    "port": 18789,
    "host": "127.0.0.1",
    "token": "env:OPENCLAW_GATEWAY_TOKEN",
    "cors": {
      "allowedOrigins": ["http://localhost:3000"]
    },
    "webchat": {
      "enabled": true,
      "port": 3000
    }
  }
}
```

---

## Environment Variables

### Core

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key (required for Claude models) |
| `OPENAI_API_KEY` | OpenAI API key |
| `GOOGLE_AI_API_KEY` | Google AI API key |
| `OPENCLAW_GATEWAY_TOKEN` | Auth token for Gateway WS & webhook APIs |
| `OPENCLAW_HOME` | Override default workspace dir (default: `~/.openclaw`) |
| `OPENCLAW_AGENT` | Default agent ID |

### Channel-Specific

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from BotFather |
| `WHATSAPP_PHONE_ID` | WhatsApp Business phone number ID |
| `WHATSAPP_TOKEN` | WhatsApp access token |
| `SLACK_BOT_TOKEN` | Slack bot OAuth token |
| `SLACK_APP_TOKEN` | Slack app-level token |
| `DISCORD_TOKEN` | Discord bot token |
| `SIGNAL_NUMBER` | Signal phone number |

### Tool-Specific

| Variable | Description |
|---|---|
| `BROWSER_CDP_URL` | Connect to existing Chrome instance |
| `BROWSER_HEADLESS` | Run browser in headless mode |
| `SEARCH_API_KEY` | API key for web search provider |

---

## Supported Messaging Channels

| Channel | Protocol | Status |
|---|---|---|
| **WhatsApp** | Cloud API | Stable |
| **Telegram** | Bot API | Stable |
| **Slack** | Socket Mode / Events API | Stable |
| **Discord** | Gateway + REST | Stable |
| **iMessage** | macOS native | macOS only |
| **Signal** | signal-cli | Stable |
| **Matrix** | Matrix Client-Server API | Stable |
| **Google Chat** | Chat API | Stable |
| **MS Teams** | Bot Framework | Stable |
| **WebChat** | Built-in web UI | Stable |
| **CLI** | Terminal | Stable |

---

## Directory Structure

```
~/.openclaw/
├── config.json              # Main configuration
├── config.local.json        # Local overrides (gitignored)
├── AGENTS.md                # Agent behavior rules
├── SOUL.md                  # Agent personality
├── IDENTITY.md              # Agent identity card
├── USER.md                  # User knowledge
├── TOOLS.md                 # Tool guidelines
├── BOOTSTRAP.md             # Custom bootstrap context
├── memory/
│   ├── MEMORY.md            # Long-term memory
│   └── 2024-01-17.md        # Daily memories
├── agents/
│   └── <agentId>/
│       └── sessions/
│           ├── sessions.json  # Session store
│           └── <id>.jsonl     # Session transcripts
├── skills/                  # Workspace skills
├── hooks/                   # Workspace hooks
├── plugins/                 # Workspace plugins
├── managed/                 # ClawHub-installed packages
│   ├── skills/
│   ├── hooks/
│   └── plugins/
└── cache/
    └── embeddings/          # Embedding cache
```

## References

- [Configuration Reference](https://docs.openclaw.ai/reference/configuration)
- [Getting Started](https://docs.openclaw.ai/getting-started)
- [Architecture](https://docs.openclaw.ai/concepts/architecture)
- [All Docs](https://docs.openclaw.ai)
