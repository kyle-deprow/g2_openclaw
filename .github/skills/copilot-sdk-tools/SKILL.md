```skill
---
name: copilot-sdk-tools
description:
  GitHub Copilot SDK custom tool definitions, MCP server integration, skills
  system, and tool access control. Use when defining custom tools with
  defineTool, configuring MCP servers (local/HTTP), loading skill directories,
  managing tool allow/deny lists, or debugging tool execution. Triggers on tasks
  involving defineTool, define_tool, JSON Schema parameters, Pydantic/Zod tool
  schemas, MCP stdio/HTTP transport, skill manifests, availableTools,
  excludedTools, or tool result handling.
---

# Copilot SDK Tools, MCP & Skills

Define custom tools, connect MCP servers, load skill packages, and control which
tools the agent can access. The extensibility layer of the Copilot SDK.

## When to Apply

Reference these guidelines when:

- Defining custom tools with `defineTool` (TS) or `@define_tool` (Python)
- Writing tool parameter schemas (JSON Schema, Zod, Pydantic)
- Handling tool invocation results and errors
- Connecting MCP servers (local/stdio or remote HTTP/SSE)
- Loading skills from directories
- Controlling tool access with availableTools/excludedTools
- Debugging tool execution failures or missing tool calls
- Understanding the tool invocation pipeline

## Rule Categories by Priority

| Priority | Category              | Impact   | Prefix      |
| -------- | --------------------- | -------- | ----------- |
| 1        | Custom Tools          | CRITICAL | `tool-`     |
| 2        | MCP Integration       | CRITICAL | `mcp-`      |
| 3        | Skills System         | HIGH     | `skill-`    |
| 4        | Tool Access Control   | HIGH     | `access-`   |
| 5        | Tool Pipeline         | MEDIUM   | `pipe-`     |

---

## 1. Custom Tool Definitions (CRITICAL)

### `tool-define-typescript`
Define tools with `defineTool`:

```typescript
import { defineTool } from "@github/copilot-sdk";

const myTool = defineTool("tool_name", {
  description: "What the tool does — be specific, the LLM reads this",
  parameters: {
    type: "object",
    properties: {
      query: { type: "string", description: "Search query" },
      limit: { type: "number", description: "Max results" },
    },
    required: ["query"],
  },
  handler: async (args, invocation) => {
    // args: { query: string, limit?: number }
    // invocation: { sessionId, toolCallId, toolName, arguments }
    return { results: ["item1", "item2"] };
  },
});

// Pass to session:
const session = await client.createSession({
  tools: [myTool],
});
```

### `tool-define-python`
Use the `@define_tool` decorator with Pydantic models:

```python
from copilot.tools import define_tool
from pydantic import BaseModel, Field

class SearchParams(BaseModel):
    query: str = Field(description="Search query")
    limit: int = Field(default=10, description="Max results")

@define_tool(description="Search the knowledge base")
async def search_kb(params: SearchParams) -> dict:
    return {"results": ["item1", "item2"]}

# Pass to session:
session = await client.create_session({
    "tools": [search_kb],
})
```

### `tool-python-signature-variants`
Python auto-detects handler signatures:

```python
# No params, no invocation
@define_tool(description="Get current time")
async def get_time() -> str:
    return datetime.now().isoformat()

# Invocation only (access session context)
@define_tool(description="Get session info")
async def session_info(invocation) -> dict:
    return {"session": invocation.session_id}

# Params only (most common)
@define_tool(description="Search")
async def search(params: SearchParams) -> dict:
    return {"results": []}

# Both params and invocation
@define_tool(description="Search with context")
async def search_ctx(params: SearchParams, invocation) -> dict:
    return {"results": [], "session": invocation.session_id}

# Custom name override
@define_tool(name="custom_name", description="Custom tool")
async def my_handler(params: MyParams) -> dict:
    return {}
```

### `tool-zod-schemas`
TypeScript tools support Zod schemas (auto-detected via `toJSONSchema()`):

```typescript
import { z } from "zod";
import { defineTool } from "@github/copilot-sdk";

const schema = z.object({
  city: z.string().describe("City name"),
  units: z.enum(["celsius", "fahrenheit"]).default("celsius"),
});

const weather = defineTool("get_weather", {
  description: "Get weather for a city",
  parameters: schema,  // Zod auto-converted to JSON Schema
  handler: async (args) => {
    return { temperature: 72, unit: args.units };
  },
});
```

### `tool-json-schema-format`
Standard JSON Schema for parameters:

```json
{
  "type": "object",
  "properties": {
    "name": {
      "type": "string",
      "description": "The item name"
    },
    "count": {
      "type": "integer",
      "minimum": 1,
      "maximum": 100,
      "description": "Number of items"
    },
    "tags": {
      "type": "array",
      "items": { "type": "string" },
      "description": "Category tags"
    },
    "options": {
      "type": "object",
      "properties": {
        "verbose": { "type": "boolean" }
      }
    }
  },
  "required": ["name"]
}
```

Good descriptions are critical — the LLM uses them to decide when and how to
call the tool.

### `tool-result-types`
Tool handlers can return various types. The SDK normalizes them:

| Return Value | Normalized To | `resultType` |
|---|---|---|
| `string` | `{textResultForLlm: str}` | `"success"` |
| `object` / `dict` | JSON stringified → `textResultForLlm` | `"success"` |
| `null` / `undefined` / `None` | `"Tool returned no result"` | `"failure"` |
| `ToolResultObject` | Pass-through | As specified |
| Thrown error | Error message (sanitized) | `"failure"` |

### `tool-result-object`
For fine-grained control, return a `ToolResultObject`:

```typescript
handler: async (args) => {
  if (!args.query) {
    return {
      textResultForLlm: "No query provided. Please specify a search term.",
      resultType: "failure",
    };
  }

  const results = await search(args.query);
  return {
    textResultForLlm: JSON.stringify(results),
    resultType: "success",
    sessionLog: `Searched for: ${args.query}, found ${results.length} results`,
    toolTelemetry: { queryLength: args.query.length, resultCount: results.length },
  };
},
```

| Field | Purpose |
|-------|---------|
| `textResultForLlm` | Text sent to the LLM (required) |
| `binaryResultsForLlm` | Binary data (images, files) |
| `resultType` | `"success"` \| `"failure"` \| `"rejected"` \| `"denied"` |
| `error` | Error message (logged, NOT sent to LLM) |
| `sessionLog` | Logged to transcript but not sent to LLM |
| `toolTelemetry` | Arbitrary metrics for analytics |

### `tool-error-security`
Tool errors are sanitized before reaching the LLM:

```typescript
// ❌ Raw error with stack trace → LLM sees sanitized version
handler: async () => { throw new Error("DB connection failed at 10.0.0.5:5432"); }
// LLM sees: "Invoking this tool produced an error..."

// ✅ Explicit error result with controlled message
handler: async () => ({
  textResultForLlm: "Database temporarily unavailable. Try again in a few minutes.",
  resultType: "failure",
  error: "DB connection failed at 10.0.0.5:5432",  // Logged only
})
```

Never leak internal details through tool results. Use `error` field for
debugging; `textResultForLlm` for what the agent should know.

---

## 2. MCP Integration (CRITICAL)

### `mcp-transport-choice`
Two transport types:

**Local/stdio — for npm packages and local scripts:**
```typescript
mcpServers: {
  "filesystem": {
    type: "local",          // or "stdio"
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    env: { DEBUG: "true" },  // Optional environment variables
    cwd: "./servers",        // Optional working directory
    tools: ["*"],            // "*" = all, [] = none, ["tool1"] = specific
    timeout: 30000,          // Optional, milliseconds
  }
}
```

**Remote HTTP/SSE — for shared and cloud services:**
```typescript
mcpServers: {
  "github": {
    type: "http",           // or "sse"
    url: "https://api.githubcopilot.com/mcp/",
    headers: { "Authorization": "Bearer ${TOKEN}" },
    tools: ["*"],
    timeout: 30000,
  }
}
```

### `mcp-python-config`
Python uses snake_case:

```python
session = await client.create_session({
    "mcp_servers": {
        "filesystem": {
            "type": "local",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            "tools": ["*"],
        },
        "github": {
            "type": "http",
            "url": "https://api.githubcopilot.com/mcp/",
            "headers": {"Authorization": f"Bearer {token}"},
            "tools": ["*"],
        },
    }
})
```

### `mcp-tool-filtering`
Control which MCP tools are exposed:

```typescript
mcpServers: {
  "github": {
    type: "http",
    url: "https://api.githubcopilot.com/mcp/",
    tools: ["create_issue", "list_issues"],  // Only these tools
  },
  "dangerous-server": {
    type: "local",
    command: "node",
    args: ["./server.js"],
    tools: [],  // No tools exposed (effectively disabled)
  }
}
```

### `mcp-on-resume`
MCP servers can be reconfigured when resuming sessions:

```typescript
const session = await client.resumeSession("my-session", {
  mcpServers: {
    "filesystem": {
      type: "local",
      command: "npx",
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/new/path"],
      tools: ["*"],
    }
  }
});
```

The CLI manages the MCP server lifecycle — starting, connecting, discovering
tools. MCP tools appear alongside custom and built-in tools.

### `mcp-env-values`
The SDK sends `envValueMode: "direct"` to the CLI, meaning environment values
are literal strings, not references to shell variables:

```typescript
// ✅ Value is sent directly
env: { "API_KEY": process.env.MY_API_KEY }

// ❌ This sends the literal string "MY_API_KEY", not the env var value
env: { "API_KEY": "MY_API_KEY" }
```

Resolve environment variables in your application code before passing them.

### `mcp-debugging`
When MCP tools don't appear or aren't called:

1. Verify the MCP server starts correctly (check stderr output)
2. Confirm `tools` is set to `["*"]` or lists the correct tool names
3. For HTTP servers, verify the URL is accessible and auth headers are correct
4. Add `timeout` to diagnose slow-starting servers
5. Use `logLevel: "debug"` on the client for detailed RPC logs
6. Check `tool.execution_start` events to see if tools are being invoked

---

## 3. Skills System (HIGH)

### `skill-directory-structure`
Skills are directories containing prompts and tool definitions:

```
skills/
└── code-review/
    ├── skill.json         # Manifest (required)
    ├── prompts/
    │   ├── system.md      # System prompt additions
    │   └── examples.md    # Few-shot examples
    └── tools/
        └── lint.json      # Tool definitions
```

### `skill-manifest`
The `skill.json` manifest declares the skill:

```json
{
  "name": "code-review",
  "displayName": "Code Review Assistant",
  "description": "Specialized code review capabilities",
  "version": "1.0.0",
  "author": "Your Team",
  "prompts": ["prompts/system.md"],
  "tools": ["tools/lint.json"]
}
```

### `skill-loading`
Load skills at session creation:

```typescript
const session = await client.createSession({
  skillDirectories: [
    "./skills/code-review",
    "./skills/documentation",
    "~/.copilot/skills",     // User-level skills
  ],
  disabledSkills: ["noisy-skill"],  // Opt-out specific skills
});
```

Skills are loaded in order. Later directories override same-name tools.
The `skill.invoked` event fires when a skill is loaded.

### `skill-prompts-as-context`
Skill prompt files are injected as additional context. Use them for:
- Domain-specific instructions ("Always use TypeScript strict mode")
- Code conventions ("Follow our naming convention: camelCase for functions")
- Few-shot examples of desired behavior
- Safety rules ("Never modify files in /production/")

---

## 4. Tool Access Control (HIGH)

### `access-available-tools`
Allowlist — only these tools can be called:

```typescript
const session = await client.createSession({
  availableTools: ["read_file", "write_file", "search"],
  // Only these 3 tools available, everything else blocked
});
```

### `access-excluded-tools`
Blocklist — everything except these:

```typescript
const session = await client.createSession({
  excludedTools: ["exec", "shell", "delete_file"],
  // All tools available except these 3
});
```

### `access-allow-deny-interaction`
- `availableTools` alone → only listed tools available
- `excludedTools` alone → all tools except listed ones
- Both → `availableTools` applied first, then `excludedTools` removes from that set
- Neither → all tools available (built-in + custom + MCP)

### `access-custom-agents`
Define sub-agents with their own tool scopes:

```typescript
const session = await client.createSession({
  customAgents: [{
    name: "researcher",
    description: "Web research specialist",
    tools: ["web_search", "web_fetch"],  // Restricted tool set
  }],
});
```

Custom agents fire `subagent.started`, `subagent.completed`, `subagent.failed`
events.

---

## 5. Tool Execution Pipeline (MEDIUM)

### `pipe-invocation-flow`
Understanding the full pipeline helps debug issues:

```
1. LLM decides to call a tool
2. CLI checks permissions (onPermissionRequest or hook-based)
3. preToolUse hook fires (can modify args or deny)
4. Tool handler executes with timeout
5. Result normalized to ToolResultObject
6. postToolUse hook fires (can modify result)
7. tool.execution_complete event emitted
8. Result sent back to LLM for next reasoning step
```

### `pipe-permission-flow`
Permission requests are checked before tool execution:

```typescript
// Via session config callback
onPermissionRequest: async (req) => {
  // req: { toolName, toolArgs, description }
  return { decision: "allow" };  // or "deny" with reason
}

// Via preToolUse hook (more powerful — can modify args)
hooks: {
  onPreToolUse: async (input) => {
    return { permissionDecision: "allow" };
  }
}
```

Without either, **tool calls are denied by default**.

### `pipe-tool-list`
Query available tools:

```typescript
const tools = await client.rpc["tools.list"]();
// Returns: [{ name, description, namespacedName?, ... }]
```

MCP tools have `namespacedName` with server prefix (e.g. `"playwright/navigate"`).

### `pipe-timeout-behavior`
Tool handlers that exceed the timeout produce a failure result. Set appropriate
timeouts for long-running tools:

```typescript
// MCP server timeout
mcpServers: { "slow-server": { timeout: 120000 } }  // 2 minutes

// Custom tool — implement your own timeout
handler: async (args) => {
  const result = await Promise.race([
    longOperation(args),
    new Promise((_, reject) => setTimeout(() => reject(new Error("Timeout")), 60000)),
  ]);
  return result;
}
```

---

## Anti-Patterns

| Anti-Pattern | Why It's Wrong | Correct Approach |
|---|---|---|
| Vague tool descriptions | LLM can't decide when to use the tool | Specific, action-oriented descriptions |
| Leaking internal errors to LLM | Security risk, confusing agent behavior | Use `error` field for logging, `textResultForLlm` for agent |
| No permission handler with tools | All tool calls silently denied | Always provide `onPermissionRequest` or `preToolUse` hook |
| `tools: []` on MCP without realizing | No MCP tools exposed | Use `tools: ["*"]` for all tools |
| Shell env `"MY_VAR"` instead of value | CLI receives literal string | Resolve env vars in app code: `process.env.MY_VAR` |
| Returning `null` from tool handlers | Treated as failure | Return explicit success result |
| Too many tools in one session | Decision fatigue for the LLM | Use `availableTools` to scope per task |
| No timeout on custom tools | Hangs indefinitely | Implement application-level timeouts |
| Same-name tools across MCP servers | Shadowing, unpredictable behavior | Use distinct tool names or MCP name filters |

## References

- Getting Started (tools section): `.archive/copilot-sdk/docs/getting-started.md`
- MCP Overview: `.archive/copilot-sdk/docs/mcp/overview.md`
- MCP Debugging: `.archive/copilot-sdk/docs/mcp/debugging.md`
- Skills Guide: `.archive/copilot-sdk/docs/guides/skills.md`
```
