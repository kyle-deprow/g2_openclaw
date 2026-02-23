# OpenClaw — Tools, Plugins & MCP

## Tool System Overview

OpenClaw provides the agent with a rich set of built-in tools, a plugin system for extending functionality, and a skills system for packaging related capabilities.

## Built-in Tools

### Core Tool Categories

| Category | Tools | Description |
|---|---|---|
| **File System** | `Read`, `Write`, `Edit`, `MultiEdit`, `LS`, `Glob`, `Grep`, `BatchTool` | File operations in the workspace |
| **Execution** | `exec` | Run shell commands with timeout and working directory |
| **Process** | `process` | Manage long-running background processes |
| **Browser** | `browser` | CDP-based browser automation |
| **Canvas** | `canvas` | macOS/iOS native canvas for drawing, diagrams |
| **Nodes** | `nodes` | Device-specific actions (screenshot, camera, location) |
| **Messaging** | `message` | Send messages to channels |
| **Cron** | `cron_create`, `cron_list`, `cron_delete` | Schedule recurring tasks |
| **Gateway** | `gateway` | Query Gateway state and config |
| **Image** | `image_generate` | Generate images via configured provider |
| **Web** | `web_search`, `web_fetch` | Search the web, fetch URLs |
| **Memory** | `memory_search`, `memory_get` | Search and retrieve memory files |
| **Session** | `sessions_list`, `sessions_history`, `sessions_send`, `sessions_spawn` | Multi-session/agent management |
| **Agents** | `agents_list` | List available agents |
| **Patch** | `apply_patch` | Apply unified diffs to files |

### Tool Profiles

Tools are organized into **profiles** that control which tools are available:

| Profile | What's Included |
|---|---|
| `minimal` | Basic chat tools only |
| `coding` | File system, exec, process, apply_patch |
| `messaging` | Message, sessions tools |
| `full` | Everything (default) |

```json
{
  "agents": {
    "defaults": {
      "tools": {
        "profile": "full"
      }
    }
  }
}
```

### Tool Groups

Fine-grained control via tool groups:

```json
{
  "agents": {
    "defaults": {
      "tools": {
        "groups": {
          "filesystem": true,
          "exec": true,
          "browser": false,
          "canvas": false,
          "messaging": true,
          "cron": true,
          "web": true,
          "memory": true,
          "sessions": true
        }
      }
    }
  }
}
```

### Allow/Deny Policies

Explicit allow and deny lists for fine-grained tool control:

```json
{
  "agents": {
    "defaults": {
      "tools": {
        "allow": ["exec", "Read", "Write", "web_search"],
        "deny": ["browser", "canvas", "image_generate"]
      }
    }
  }
}
```

### Provider-Specific Tool Policy

Control tool availability per model provider:

```json
{
  "tools": {
    "providerPolicy": {
      "anthropic": { "allow": ["*"] },
      "openai": { "deny": ["browser", "canvas"] },
      "google": { "deny": ["exec"] }
    }
  }
}
```

---

## Browser Tool

OpenClaw includes a built-in browser tool powered by **Chrome DevTools Protocol (CDP)**.

### Capabilities

- Navigate to URLs
- Click elements, type text, scroll
- Take screenshots
- Extract page content
- Execute JavaScript
- Fill forms
- Wait for selectors/navigation

### Configuration

```json
{
  "tools": {
    "browser": {
      "enabled": true,
      "headless": true,
      "viewport": { "width": 1280, "height": 720 },
      "timeout": 30000,
      "allowedDomains": ["*"],
      "blockedDomains": ["malware.example.com"]
    }
  }
}
```

### CDP Connection

- Launches a local Chromium instance by default
- Can connect to an existing Chrome instance via `BROWSER_CDP_URL`
- Supports remote debugging port connections

---

## MCP (Model Context Protocol)

OpenClaw has first-class MCP support, allowing it to connect to external MCP tool servers.

### Configuration

MCP servers are configured in the OpenClaw config file:

```json
{
  "mcp": {
    "servers": {
      "github": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {
          "GITHUB_TOKEN": "env:GITHUB_TOKEN"
        }
      },
      "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/allowed/dir"]
      },
      "postgres": {
        "url": "http://localhost:3001/mcp",
        "transport": "streamable-http"
      }
    }
  }
}
```

### Transport Types

| Transport | Config Key | Description |
|---|---|---|
| **stdio** | `command` + `args` | Spawns a child process, communicates via stdin/stdout |
| **SSE** | `url` (with `/sse`) | Server-Sent Events transport |
| **Streamable HTTP** | `url` + `transport: "streamable-http"` | HTTP-based streaming |

### MCP Tool Access

- All MCP tools are automatically available to the agent
- Tools appear with their server name as prefix: `github_create_issue`, `filesystem_read_file`
- MCP tools respect the same allow/deny policies as built-in tools
- MCP resources and prompts are also supported

### MCP in Skills

Skills can bundle their own MCP server configurations:

```markdown
# SKILL.md
...
## MCP Servers
- github: @modelcontextprotocol/server-github
```

### Environment Variable References

MCP configs support `env:VAR_NAME` syntax to reference environment variables without hardcoding secrets:

```json
{
  "env": {
    "API_KEY": "env:MY_SERVICE_API_KEY"
  }
}
```

---

## Plugin System

Plugins are TypeScript modules that extend OpenClaw's functionality at a deeper level than tools or skills.

### Plugin Structure

```
plugins/
  my-plugin/
    index.ts      # Main entry point
    package.json  # Optional, for dependencies
```

### Plugin API

Plugins are loaded via **jiti** (Just-In-Time TypeScript compilation) and can register:

| Extension Point | Description |
|---|---|
| **Tools** | Register custom tool definitions |
| **Services** | Background services that run alongside the Gateway |
| **Channels** | Custom messaging channels |
| **Hooks** | Event handlers for agent/gateway lifecycle |
| **Providers** | Custom LLM providers |
| **RPC Methods** | Custom Gateway RPC methods |
| **CLI Commands** | Custom CLI subcommands |

### Plugin Discovery & Precedence

Plugins are discovered from (highest priority first):
1. **Workspace plugins**: `~/.openclaw/plugins/`
2. **Managed plugins**: `~/.openclaw/managed/plugins/`
3. **Bundled plugins**: Ships with OpenClaw

### Plugin Example

```typescript
import type { OpenClawPlugin } from '@openclaw/sdk';

export default {
  name: 'my-plugin',
  version: '1.0.0',

  tools: [
    {
      name: 'my_custom_tool',
      description: 'Does something custom',
      parameters: {
        type: 'object',
        properties: {
          input: { type: 'string', description: 'Input text' }
        },
        required: ['input']
      },
      async execute({ input }) {
        return { result: `Processed: ${input}` };
      }
    }
  ],

  hooks: {
    async message_received(message) {
      console.log('Received:', message.text);
    }
  },

  async onLoad(api) {
    console.log('Plugin loaded!');
  }
} satisfies OpenClawPlugin;
```

---

## Skills System

Skills package related tools, prompts, and configurations into reusable modules.

### Skill Structure

```
skills/
  my-skill/
    SKILL.md       # Skill manifest and instructions
    tools/         # Custom tools for this skill
    prompts/       # Additional prompts
    config.json    # Skill-specific configuration
```

### SKILL.md Format

```markdown
# My Skill

## Description
What this skill does.

## Instructions
Detailed instructions for the agent when this skill is active.

## Tools
- tool_name: Description of tool

## MCP Servers
- server-name: @package/mcp-server

## Environment
- `API_KEY`: Required API key for the service

## Gating
- Requires: exec
- Platforms: macos, linux
```

### Skill Discovery & Precedence

1. **Workspace skills**: `~/.openclaw/skills/`
2. **Managed skills** (installed from ClawHub): `~/.openclaw/managed/skills/`
3. **Bundled skills**: Ships with OpenClaw

### ClawHub Registry

Skills can be published to and installed from **ClawHub** (the OpenClaw skill registry):

```bash
openclaw skills install web-research
openclaw skills install code-review
openclaw skills list
openclaw skills search "database"
```

### Per-Agent Skills

Skills can be assigned per agent:

```json
{
  "agents": {
    "agents": {
      "researcher": {
        "skills": ["web-research", "deep-research", "arxiv"]
      },
      "coder": {
        "skills": ["coding", "git", "code-review"]
      }
    }
  }
}
```

### Skill Gating

Skills can declare requirements that must be met:

- **Tool requirements**: Skill only loads if specified tools are available
- **Platform requirements**: Only loads on specified platforms (macos, linux, windows)
- **Feature flags**: Based on configuration values

### Environment Injection

Skills can inject environment variables that their tools need:

```json
{
  "skills": {
    "my-skill": {
      "env": {
        "API_KEY": "sk-..."
      }
    }
  }
}
```

---

## Subagent Tool Access

When spawning subagents via `sessions_spawn`, the subagent's tool access is configurable:

### Default Behavior

- Subagents inherit the spawning agent's tool profile by default
- Subagents run in their own session with independent context

### Custom Tool Access

```json
{
  "tools": {
    "subagents": {
      "tools": {
        "profile": "coding",
        "allow": ["exec", "Read", "Write"],
        "deny": ["browser", "message"]
      }
    }
  }
}
```

### Sandbox Session Visibility

Control which sessions a subagent can see and communicate with:

```json
{
  "session": {
    "sandbox": {
      "visibility": "own"  // "own" | "all" | "none"
    }
  }
}
```

| Visibility | Behavior |
|---|---|
| `own` | Subagent sees only its own session |
| `all` | Subagent can see all sessions |
| `none` | Subagent has no session tools |

---

## Tool Execution Flow

1. Agent decides to use a tool based on conversation context
2. Tool call enters the **tool queue**
3. `before_tool_call` hook fires (plugins can modify params)
4. Tool executes with timeout enforcement
5. `after_tool_call` hook fires (plugins can modify results)
6. `tool_result_persist` hook fires (transform before transcript write)
7. Result streamed back to the agent as `toolResult` message
8. Agent processes result and continues reasoning

## References

- [Tools Overview](https://docs.openclaw.ai/concepts/tools)
- [Skills](https://docs.openclaw.ai/concepts/skills)
- [Browser](https://docs.openclaw.ai/concepts/browser)
- [Plugins](https://docs.openclaw.ai/concepts/plugins)
- [Configuration](https://docs.openclaw.ai/reference/configuration)
