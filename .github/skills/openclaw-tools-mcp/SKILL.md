---
name: openclaw-tools-mcp
description:
  OpenClaw tool system, MCP server integration, plugin development, and skills authoring. Use when configuring tool profiles, connecting MCP servers, building custom plugins, writing SKILL.md manifests, managing tool allow/deny policies, or debugging tool execution issues. Triggers on tasks involving tool groups, MCP transport config, plugin TypeScript modules, ClawHub skills, tool streaming, or provider-specific tool policies.
---

# OpenClaw Tools, MCP & Plugins

Configure, extend, and author the tool ecosystem — built-in tools, MCP servers,
TypeScript plugins, and ClawHub skills.

## When to Apply

Reference these guidelines when:

- Configuring tool profiles (minimal, coding, messaging, full)
- Setting up allow/deny policies for tool access control
- Connecting MCP servers via stdio, SSE, or Streamable HTTP
- Writing custom TypeScript plugins for tools, channels, or hooks
- Authoring SKILL.md manifests for reusable skill packages
- Publishing or installing skills from ClawHub
- Debugging tool execution failures, timeouts, or permission errors
- Configuring per-agent or per-subagent tool access
- Setting up browser automation via CDP

## Rule Categories by Priority

| Priority | Category            | Impact   | Prefix      |
| -------- | ------------------- | -------- | ----------- |
| 1        | Tool Profiles       | CRITICAL | `tools-`    |
| 2        | MCP Integration     | CRITICAL | `mcp-`      |
| 3        | Plugin Development  | HIGH     | `plugin-`   |
| 4        | Skills Authoring    | HIGH     | `skill-`    |
| 5        | Browser Automation  | MEDIUM   | `browser-`  |

---

## 1. Tool Profiles & Policies (CRITICAL)

### `tools-profile-selection`
Start with the right profile, then refine with allow/deny:

| Profile      | Includes                                      | Use Case              |
| ------------ | --------------------------------------------- | --------------------- |
| `minimal`    | Basic chat tools only                         | Chatbot, no actions   |
| `coding`     | File system, exec, process, apply_patch       | Development assistant |
| `messaging`  | Message, sessions tools                       | Communication bot     |
| `full`       | Everything (default)                          | General-purpose agent |

```json
// ✅ Start restrictive, expand as needed
{ "agents": { "defaults": { "tools": { "profile": "coding" } } } }

// ❌ Full profile when the agent only needs file access
{ "agents": { "defaults": { "tools": { "profile": "full" } } } }
```

### `tools-least-privilege`
Grant the minimum tool set the agent needs. Extra tools increase:
- System prompt size (tool descriptions consume tokens)
- Attack surface (exec + browser = arbitrary code + web access)
- Decision complexity (agent spends thinking tokens choosing tools)

```json
// ✅ Only what's needed
{
  "tools": {
    "profile": "coding",
    "deny": ["process", "apply_patch"]
  }
}

// ❌ Full profile with no restrictions for a simple Q&A agent
```

### `tools-allow-deny-order`
Allow and deny lists are evaluated after the profile:
1. Profile determines the base set
2. `allow` narrows to only these tools (if specified)
3. `deny` removes specific tools from the remaining set

```json
// Only exec and file reading — nothing else
{
  "tools": {
    "profile": "full",
    "allow": ["exec", "Read", "Glob", "Grep"],
    "deny": []
  }
}
```

### `tools-provider-policy`
Different LLM providers may handle certain tools poorly. Use provider-specific
policies to compensate:

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

This prevents tool calls that a provider's model can't handle well, without
affecting other providers.

### `tools-groups-fine-control`
Tool groups provide category-level control without listing individual tools:

```json
{
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
```

Use groups for broad category toggles; use allow/deny for individual tools.

---

## 2. MCP Integration (CRITICAL)

### `mcp-transport-choice`
Choose the right MCP transport for your server:

| Transport        | Config Key                          | When to Use                     |
| ---------------- | ----------------------------------- | ------------------------------- |
| **stdio**        | `command` + `args`                  | npm packages, local scripts     |
| **SSE**          | `url` (ending in /sse)             | Remote servers, long-lived      |
| **Streamable HTTP** | `url` + `transport: "streamable-http"` | Modern HTTP-based servers  |

```json
{
  "mcp": {
    "servers": {
      "github": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": { "GITHUB_TOKEN": "env:GITHUB_TOKEN" }
      },
      "database": {
        "url": "http://localhost:3001/mcp",
        "transport": "streamable-http"
      }
    }
  }
}
```

### `mcp-env-references`
Never hardcode secrets in MCP config. Use `env:VAR_NAME` syntax:

```json
// ✅ Environment variable reference
{ "env": { "API_KEY": "env:MY_SERVICE_API_KEY" } }

// ❌ Hardcoded secret
{ "env": { "API_KEY": "sk-1234567890abcdef" } }
```

The `env:` prefix resolves at runtime from the Gateway's environment.

### `mcp-tool-naming`
MCP tools appear with their server name as prefix: `github_create_issue`,
`filesystem_read_file`. Choose short, descriptive server names:

```json
// ✅ Clean prefixes
{ "mcp": { "servers": { "gh": {}, "db": {}, "search": {} } } }

// ❌ Long prefixes that waste tokens
{ "mcp": { "servers": { "my-github-integration": {}, "postgres-database": {} } } }
```

### `mcp-respect-policies`
MCP tools follow the same allow/deny policies as built-in tools. You can deny
specific MCP tools:

```json
{
  "tools": {
    "deny": ["gh_delete_repo", "db_drop_table"]
  }
}
```

### `mcp-disable-not-remove`
To temporarily disable an MCP server without removing its config, use `disabled`:

```json
{
  "mcp": {
    "servers": {
      "experimental": {
        "command": "npx",
        "args": ["-y", "@my/experimental-server"],
        "disabled": true
      }
    }
  }
}
```

### `mcp-skills-bundle`
Skills can bundle their own MCP servers. When a skill is active, its MCP servers
are automatically started. When the skill is deactivated, they stop:

```markdown
# SKILL.md
## MCP Servers
- github: @modelcontextprotocol/server-github
```

This keeps MCP lifecycle tied to skill activation, not global config.

---

## 3. Plugin Development (HIGH)

### `plugin-structure`
Every plugin is a TypeScript module loaded by jiti (just-in-time compilation):

```
plugins/
  my-plugin/
    index.ts        # Main entry — exports the plugin object
    package.json    # Optional, for external dependencies
```

### `plugin-api-surface`
Plugins can register across seven extension points:

| Extension    | Purpose                                   | Example                       |
| ------------ | ----------------------------------------- | ----------------------------- |
| `tools`      | Custom tool definitions                   | Domain-specific actions       |
| `services`   | Background services alongside Gateway     | Monitoring, indexing           |
| `channels`   | Custom messaging channels                 | Proprietary chat systems      |
| `hooks`      | Event handlers for lifecycle events       | Audit logging, notifications  |
| `providers`  | Custom LLM providers                      | Private model endpoints       |
| `rpcMethods` | Custom Gateway RPC methods                | Dashboard API extensions      |
| `cliCommands`| Custom CLI subcommands                    | Workflow shortcuts             |

### `plugin-tool-definition`
Define tools with full JSON Schema parameters:

```typescript
import type { OpenClawPlugin } from '@openclaw/sdk';

export default {
  name: 'jira-tools',
  version: '1.0.0',
  tools: [
    {
      name: 'jira_create_ticket',
      description: 'Create a JIRA ticket with title, description, and priority',
      parameters: {
        type: 'object',
        properties: {
          title: { type: 'string', description: 'Ticket title' },
          description: { type: 'string', description: 'Detailed description' },
          priority: {
            type: 'string',
            enum: ['low', 'medium', 'high', 'critical'],
            description: 'Ticket priority'
          }
        },
        required: ['title', 'description']
      },
      async execute({ title, description, priority = 'medium' }) {
        // Implementation here
        return { ticketId: 'PROJ-123', url: '...' };
      }
    }
  ]
} satisfies OpenClawPlugin;
```

### `plugin-discovery-precedence`
Plugins are discovered from three locations (highest priority first):

1. **Workspace**: `~/.openclaw/plugins/` — user-created
2. **Managed**: `~/.openclaw/managed/plugins/` — installed via CLI
3. **Bundled**: Ships with OpenClaw — lowest priority

A workspace plugin with the same name overrides a managed or bundled one.

### `plugin-hooks-lifecycle`
Use plugin hooks for cross-cutting concerns:

```typescript
{
  hooks: {
    async before_tool_call(toolName, params) {
      if (toolName === 'exec' && params.command.includes('rm')) {
        console.warn(`Destructive command detected: ${params.command}`);
      }
    },
    async after_tool_call(toolName, params, result) {
      auditLog.write({ tool: toolName, params, result, timestamp: Date.now() });
    },
    async message_received(message) {
      metrics.increment('messages_received', { channel: message.channel });
    }
  }
}
```

### `plugin-error-isolation`
Plugin errors must not crash the Gateway. Wrap plugin code in try/catch and
log errors rather than re-throwing. The Gateway will skip a failing plugin and
continue with the rest.

---

## 4. Skills Authoring (HIGH)

### `skill-manifest-format`
Every skill needs a SKILL.md manifest:

```markdown
# Web Research

## Description
Deep web research with source verification and citation.

## Instructions
When this skill is active, the agent should:
- Always cite sources with URLs
- Cross-reference claims across multiple sources
- Distinguish between facts and opinions
- Provide confidence levels for findings

## Tools
- web_search: Search the web for current information
- web_fetch: Fetch and parse a specific URL

## MCP Servers
- search: @my/search-mcp-server

## Environment
- `SEARCH_API_KEY`: API key for search provider (required)

## Gating
- Requires: web_search, web_fetch
- Platforms: macos, linux, windows
```

### `skill-gating-requirements`
Skills declare what they need. If requirements aren't met, the skill silently
doesn't load — no errors, no partial state:

- **Tool requirements**: Skill only loads if listed tools are available
- **Platform requirements**: Only loads on specified platforms
- **Feature flags**: Based on configuration values

```markdown
## Gating
- Requires: exec, browser
- Platforms: macos, linux
```

### `skill-three-tiers`
Skills come from three sources with descending priority:

| Tier        | Location                        | Managed By     |
| ----------- | ------------------------------- | -------------- |
| Workspace   | `~/.openclaw/skills/`           | User (manual)  |
| Managed     | `~/.openclaw/managed/skills/`   | ClawHub CLI    |
| Bundled     | Ships with OpenClaw             | OpenClaw team  |

A workspace skill overrides a managed skill with the same name. Use workspace
for customization, managed for community packages.

### `skill-per-agent`
Assign skills per agent to create specialists:

```json
{
  "agents": {
    "agents": {
      "researcher": { "skills": ["web-research", "deep-research", "arxiv"] },
      "coder": { "skills": ["coding", "git", "code-review"] },
      "writer": { "skills": ["writing", "editing", "grammar"] }
    }
  }
}
```

### `skill-environment-injection`
Skills can require environment variables. Inject them per-skill:

```json
{
  "skills": {
    "web-research": {
      "env": { "SEARCH_API_KEY": "env:SEARCH_API_KEY" }
    }
  }
}
```

### `skill-clawhub-install`
Install community skills from ClawHub:

```bash
openclaw skills install web-research     # Install
openclaw skills list                     # List installed
openclaw skills search "database"        # Search registry
openclaw skills update web-research      # Update
openclaw skills remove web-research      # Uninstall
```

---

## 5. Browser Automation (MEDIUM)

### `browser-cdp-default`
The browser tool launches a local Chromium instance by default via CDP:

```json
{
  "tools": {
    "browser": {
      "enabled": true,
      "headless": true,
      "viewport": { "width": 1280, "height": 720 },
      "timeout": 30000
    }
  }
}
```

### `browser-remote-connect`
Connect to an existing Chrome instance instead of launching one:

```bash
export BROWSER_CDP_URL="http://localhost:9222"
```

Use this in Docker/CI environments where Chromium is pre-installed, or to reuse
a browser with logged-in sessions.

### `browser-domain-restrict`
In production, restrict which domains the browser can access:

```json
{
  "tools": {
    "browser": {
      "allowedDomains": ["*.example.com", "docs.openclaw.ai"],
      "blockedDomains": ["*.malware.example.com"]
    }
  }
}
```

### `browser-disable-by-default`
The browser tool is powerful but costly (tokens for screenshots, execution time).
Disable by default and enable per-agent only where needed:

```json
{
  "agents": {
    "defaults": { "tools": { "groups": { "browser": false } } },
    "agents": {
      "researcher": { "tools": { "groups": { "browser": true } } }
    }
  }
}
```

---

## Tool Execution Flow

Understanding the execution pipeline helps debug issues:

```
1. Agent decides to call a tool
2. Tool call enters the tool queue
3. before_tool_call hook fires (plugins can modify params)
4. Tool executes with timeout enforcement
5. after_tool_call hook fires (plugins can modify results)
6. tool_result_persist hook fires (transform before transcript write)
7. Result streamed back as toolResult message
8. Agent processes result and continues reasoning
```

Hook interception points (3, 5, 6) are where plugins add audit logging,
parameter validation, result filtering, or cost tracking.

---

## Anti-Patterns

| Anti-Pattern | Why It's Wrong | Correct Approach |
| --- | --- | --- |
| Full profile for limited-use agents | Token waste, decision fatigue | Use minimal/coding profile + allow list |
| Hardcoded secrets in MCP config | Credential exposure in version control | Use env:VAR_NAME references |
| Long MCP server names | Wasted tokens in tool descriptions | Short, descriptive prefixes (gh, db) |
| Plugin errors crashing Gateway | Full agent downtime | try/catch in all plugin code |
| No gating on skills | Skills load on platforms that can't use them | Declare platform + tool requirements |
| Browser enabled globally | Unnecessary cost and security surface | Disable by default, enable per-agent |
| Ignoring tool_result_persist hook | Large tool outputs bloat transcripts | Filter/summarize before persistence |

## References

- https://docs.openclaw.ai/concepts/tools
- https://docs.openclaw.ai/concepts/skills
- https://docs.openclaw.ai/concepts/browser
- https://docs.openclaw.ai/concepts/plugins
- https://docs.openclaw.ai/reference/configuration
