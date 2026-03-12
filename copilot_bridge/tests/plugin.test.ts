import { describe, it, expect, vi, beforeEach } from "vitest";

const { mockBridge } = vi.hoisted(() => {
const mockBridge = {
ensureReady: vi.fn().mockResolvedValue(undefined),
runTask: vi.fn(),
stop: vi.fn().mockResolvedValue(undefined),
isReady: vi.fn().mockResolvedValue(true),
getStatus: vi.fn().mockResolvedValue({ connected: true, authMethod: "user" }),
resolveWorkingDir: vi.fn().mockImplementation(async (dir: string) => `/resolved/${dir}`),
listSessions: vi.fn().mockReturnValue([]),
destroySession: vi.fn().mockResolvedValue(true),
destroyAllSessions: vi.fn().mockResolvedValue(0),
};
return { mockBridge };
});

vi.mock("../src/config.js", () => ({
loadConfig: vi.fn().mockReturnValue({
githubToken: "ghu_test",
editorVersion: "vscode/1.90.0",
}),
}));

vi.mock("../src/client.js", () => ({
CopilotBridge: vi.fn().mockImplementation(function () { return mockBridge; }),
}));

import { CopilotBridge } from "../src/client.js";
import registerPlugin, { _resetBridge } from "../src/plugin.js";

// --- Mock OpenClawPluginApi ---

type RegisteredTool = { name: string; execute: (id: string, params: any) => Promise<any>; [k: string]: any };

function createMockApi() {
const tools: RegisteredTool[] = [];
return {
tools,
api: {
id: "copilot-bridge",
name: "Copilot Bridge",
source: "test",
config: {},
runtime: { state: { resolveStateDir: () => "/tmp" }, config: { loadConfig: () => ({}) } },
logger: { info: vi.fn(), warn: vi.fn(), error: vi.fn() },
registerTool: vi.fn((tool: RegisteredTool) => { tools.push(tool); }),
registerHook: vi.fn(),
registerHttpRoute: vi.fn(),
registerChannel: vi.fn(),
registerGatewayMethod: vi.fn(),
registerCli: vi.fn(),
registerService: vi.fn(),
registerProvider: vi.fn(),
registerCommand: vi.fn(),
resolvePath: vi.fn((p: string) => p),
on: vi.fn(),
} as any,
};
}

let copilotTool: RegisteredTool;
let sessionsTool: RegisteredTool;

beforeEach(async () => {
await _resetBridge();
vi.clearAllMocks();

mockBridge.ensureReady.mockResolvedValue(undefined);
mockBridge.stop.mockResolvedValue(undefined);
mockBridge.resolveWorkingDir.mockImplementation(async (dir: string) => `/resolved/${dir}`);
mockBridge.listSessions.mockReturnValue([]);
mockBridge.destroySession.mockResolvedValue(true);
mockBridge.destroyAllSessions.mockResolvedValue(0);
mockBridge.runTask.mockResolvedValue({
success: true,
content: "Hello world",
toolCalls: [],
errors: [],
sessionId: "sess-123",
elapsed: 1500,
});

const { api, tools } = createMockApi();
registerPlugin(api);
copilotTool = tools.find(t => t.name === "copilot")!;
sessionsTool = tools.find(t => t.name === "copilot_sessions")!;
});

// Helper: execute tool and extract text from result
async function execCopilot(params: Record<string, any>): Promise<string> {
const result = await copilotTool.execute("call-1", params);
return result.content[0].text;
}

async function execSessions(params: Record<string, any>): Promise<string> {
const result = await sessionsTool.execute("call-1", params);
return result.content[0].text;
}

// --- Plugin registration ---

describe("plugin registration", () => {
it("registers exactly two tools", () => {
const { api, tools } = createMockApi();
registerPlugin(api);
expect(tools).toHaveLength(2);
expect(tools[0].name).toBe("copilot");
expect(tools[1].name).toBe("copilot_sessions");
});

it("logs after registration", () => {
const { api } = createMockApi();
registerPlugin(api);
expect(api.logger.info).toHaveBeenCalledWith(expect.stringContaining("copilot-bridge"));
});
});

// --- Bridge singleton ---

describe("bridge singleton", () => {
it("lazily initialises the bridge on first execute", async () => {
expect(vi.mocked(CopilotBridge)).not.toHaveBeenCalled();
await execCopilot({ prompt: "do stuff", workingDir: "my-app" });
expect(vi.mocked(CopilotBridge)).toHaveBeenCalledTimes(1);
expect(mockBridge.ensureReady).toHaveBeenCalledTimes(1);
});

it("reuses the bridge across calls", async () => {
await execCopilot({ prompt: "first", workingDir: "my-app" });
await execCopilot({ prompt: "second", workingDir: "my-app" });
expect(vi.mocked(CopilotBridge)).toHaveBeenCalledTimes(1);
expect(mockBridge.ensureReady).toHaveBeenCalledTimes(1);
});
});

// --- Copilot tool happy path ---

describe("copilot tool - happy path", () => {
it("forwards prompt, default timeout, and resolvedDir as sessionId", async () => {
const text = await execCopilot({ prompt: "hello", workingDir: "proj" });
expect(mockBridge.resolveWorkingDir).toHaveBeenCalledWith("proj");
expect(mockBridge.runTask).toHaveBeenCalledWith({
prompt: "hello",
workingDir: "/resolved/proj",
timeout: 900_000,
sessionId: "/resolved/proj",
systemMessage: undefined,
});
const parsed = JSON.parse(text);
expect(parsed.text).toContain("## Result");
expect(parsed.text).toContain("Hello world");
});

it("passes persona as systemMessage", async () => {
await execCopilot({ prompt: "refactor module X", persona: "You are a senior engineer.", workingDir: "proj" });
expect(mockBridge.runTask).toHaveBeenCalledWith(
expect.objectContaining({ systemMessage: "You are a senior engineer." }),
);
});

it("systemMessage is undefined when no persona provided", async () => {
await execCopilot({ prompt: "do stuff", workingDir: "proj" });
const call = mockBridge.runTask.mock.calls[0][0];
expect(call.systemMessage).toBeUndefined();
});

it("custom timeout is forwarded", async () => {
await execCopilot({ prompt: "go", workingDir: "proj", timeout: 60_000 });
expect(mockBridge.runTask).toHaveBeenCalledWith(
expect.objectContaining({ timeout: 60_000 }),
);
});
});

// --- Copilot tool result formatting ---

describe("copilot tool - result formatting", () => {
it("includes tool calls section when present", async () => {
mockBridge.runTask.mockResolvedValue({
success: true,
content: "Done",
toolCalls: [
{ tool: "readFile", args: { path: "a.ts" }, result: "ok" },
{ tool: "editFile", args: { path: "b.ts" }, result: "ok" },
],
errors: [],
sessionId: "sess-abc",
elapsed: 2000,
});
const text = await execCopilot({ prompt: "go", workingDir: "proj" });
const parsed = JSON.parse(text);
expect(parsed.text).toContain("## Tool Calls");
expect(parsed.text).toContain("`readFile`");
});

it("includes errors section when present", async () => {
mockBridge.runTask.mockResolvedValue({
success: false,
content: "Partial",
toolCalls: [],
errors: ["lint failed", "type error"],
sessionId: "sess-err",
elapsed: 3000,
});
const text = await execCopilot({ prompt: "go", workingDir: "proj" });
const parsed = JSON.parse(text);
expect(parsed.text).toContain("## Errors");
expect(parsed.text).toContain("lint failed");
});

it("includes stats section", async () => {
const text = await execCopilot({ prompt: "go", workingDir: "proj" });
const parsed = JSON.parse(text);
expect(parsed.text).toContain("## Stats");
expect(parsed.text).toContain("Success: true");
expect(parsed.text).toContain("Elapsed: 1.5s");
expect(parsed.text).toContain("Session: sess-123");
});
});

// --- Copilot tool error handling ---

describe("copilot tool - error handling", () => {
it("returns error on bridge init failure", async () => {
mockBridge.ensureReady.mockRejectedValue(new Error("auth failed"));
const text = await execCopilot({ prompt: "go", workingDir: "proj" });
expect(JSON.parse(text).error).toContain("auth failed");
});

it("returns error on runTask rejection", async () => {
mockBridge.runTask.mockRejectedValue(new Error("timeout exceeded"));
const text = await execCopilot({ prompt: "go", workingDir: "proj" });
expect(JSON.parse(text).error).toContain("timeout exceeded");
});

it("allows retry after bridge init failure", async () => {
mockBridge.ensureReady
.mockRejectedValueOnce(new Error("transient"))
.mockResolvedValueOnce(undefined);

const first = await execCopilot({ prompt: "go", workingDir: "proj" });
expect(JSON.parse(first).error).toBeDefined();

const second = await execCopilot({ prompt: "go", workingDir: "proj" });
expect(JSON.parse(second).text).toContain("## Result");
});
});

// --- Copilot tool input validation ---

describe("copilot tool - input validation", () => {
it("rejects empty prompt", async () => {
const text = await execCopilot({ prompt: "", workingDir: "proj" });
expect(JSON.parse(text).error).toContain("`prompt` must be a non-empty string");
});

it("rejects too-long prompt", async () => {
const text = await execCopilot({ prompt: "x".repeat(500_001), workingDir: "proj" });
expect(JSON.parse(text).error).toContain("exceeds maximum length");
});

it("rejects empty workingDir", async () => {
const text = await execCopilot({ prompt: "go", workingDir: "" });
expect(JSON.parse(text).error).toContain("`workingDir` is required");
});

it("rejects negative timeout", async () => {
const text = await execCopilot({ prompt: "go", workingDir: "proj", timeout: -1 });
expect(JSON.parse(text).error).toContain("`timeout` must be a non-negative number");
});

it("rejects too-long persona", async () => {
const text = await execCopilot({ prompt: "go", workingDir: "proj", persona: "x".repeat(50_001) });
expect(JSON.parse(text).error).toContain("`persona` must be a string");
});
});

// --- Sessions tool ---

describe("copilot_sessions tool", () => {
it("lists sessions", async () => {
mockBridge.listSessions.mockReturnValue([
{ workingDir: "/home/dev/repos/proj", messageCount: 5, createdAt: "2026-01-01" },
]);
const text = await execSessions({ action: "list" });
const parsed = JSON.parse(text);
expect(parsed.sessions).toHaveLength(1);
expect(parsed.sessions[0].workingDir).toBe("/home/dev/repos/proj");
});

it("lists empty sessions", async () => {
const text = await execSessions({ action: "list" });
expect(JSON.parse(text).sessions).toHaveLength(0);
});

it("destroys a session", async () => {
const text = await execSessions({ action: "destroy", project: "my-app" });
const parsed = JSON.parse(text);
expect(parsed.destroyed).toBe(true);
expect(mockBridge.resolveWorkingDir).toHaveBeenCalledWith("my-app");
expect(mockBridge.destroySession).toHaveBeenCalledWith("/resolved/my-app");
});

it("rejects invalid action", async () => {
const text = await execSessions({ action: "nope" });
expect(JSON.parse(text).error).toContain("`action` must be 'list' or 'destroy'");
});

it("rejects destroy without project", async () => {
const text = await execSessions({ action: "destroy" });
expect(JSON.parse(text).error).toContain("`project` is required");
});
});
