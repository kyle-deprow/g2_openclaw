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
import type { OpenClawPlugin } from "../src/plugin.js";

let plugin: OpenClawPlugin;
let _resetBridge: () => Promise<void>;

beforeEach(async () => {
	const mod = await import("../src/plugin.js");
	plugin = mod.default;
	_resetBridge = mod._resetBridge;

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
});

// ─── Plugin shape ───────────────────────────────────────────────────────────

describe("plugin shape", () => {
	it("has expected name and version", () => {
		expect(plugin.name).toBe("copilot-bridge");
		expect(plugin.version).toBe("1.0.0");
	});

	it("exposes exactly one tool", () => {
		expect(plugin.tools).toHaveLength(1);
		expect(plugin.tools?.[0].name).toBe("copilot");
	});

	it("has an onLoad hook", () => {
		expect(typeof plugin.onLoad).toBe("function");
	});
});

// ─── Tool parameter schema ──────────────────────────────────────────────────

describe("tool parameter schema", () => {
	it("requires prompt and workingDir", () => {
		const tool = plugin.tools![0];
		expect(tool.parameters.required).toEqual(["prompt", "workingDir"]);
	});

	it("defines prompt, persona, workingDir, and timeout properties", () => {
		const props = plugin.tools![0].parameters.properties;
		expect(Object.keys(props).sort()).toEqual(
			["persona", "prompt", "timeout", "workingDir"].sort(),
		);
		expect(props.prompt.type).toBe("string");
		expect(props.persona.type).toBe("string");
		expect(props.workingDir.type).toBe("string");
		expect(props.timeout.type).toBe("number");
	});
});

// ─── Bridge singleton ───────────────────────────────────────────────────────

describe("bridge singleton", () => {
	it("lazily initialises the bridge on first execute", async () => {
		const tool = plugin.tools![0];
		expect(vi.mocked(CopilotBridge)).not.toHaveBeenCalled();

		await tool.execute({ prompt: "do stuff", workingDir: "my-app" });

		expect(vi.mocked(CopilotBridge)).toHaveBeenCalledTimes(1);
		expect(mockBridge.ensureReady).toHaveBeenCalledTimes(1);
	});

	it("reuses the bridge across calls", async () => {
		const tool = plugin.tools![0];

		await tool.execute({ prompt: "first", workingDir: "my-app" });
		await tool.execute({ prompt: "second", workingDir: "my-app" });

		expect(vi.mocked(CopilotBridge)).toHaveBeenCalledTimes(1);
		expect(mockBridge.ensureReady).toHaveBeenCalledTimes(1);
	});
});

// ─── Happy path ─────────────────────────────────────────────────────────────

describe("happy path", () => {
	it("prompt only — forwards prompt, default timeout, and resolvedDir as sessionId", async () => {
		const tool = plugin.tools![0];
		const { result } = await tool.execute({ prompt: "hello", workingDir: "proj" });

		expect(mockBridge.resolveWorkingDir).toHaveBeenCalledWith("proj");
		expect(mockBridge.runTask).toHaveBeenCalledWith({
			prompt: "hello",
			workingDir: "/resolved/proj",
			timeout: 120_000,
			sessionId: "/resolved/proj",
			systemMessage: undefined,
		});
		expect(result).toContain("## Result");
		expect(result).toContain("Hello world");
	});

	it("prompt with persona — passes persona as systemMessage, not in prompt", async () => {
		const tool = plugin.tools![0];
		const persona = "You are a senior engineer.";
		const prompt = "refactor module X";

		await tool.execute({ prompt, persona, workingDir: "proj" });

		expect(mockBridge.runTask).toHaveBeenCalledWith(
			expect.objectContaining({
				prompt,
				systemMessage: persona,
			}),
		);
	});

	it("systemMessage is undefined when no persona provided", async () => {
		const tool = plugin.tools![0];
		await tool.execute({ prompt: "do stuff", workingDir: "proj" });

		const call = mockBridge.runTask.mock.calls[0][0];
		expect(call.prompt).toBe("do stuff");
		expect(call.systemMessage).toBeUndefined();
	});

	it("custom timeout is forwarded", async () => {
		const tool = plugin.tools![0];
		await tool.execute({ prompt: "go", workingDir: "proj", timeout: 60_000 });

		expect(mockBridge.runTask).toHaveBeenCalledWith(
			expect.objectContaining({ timeout: 60_000 }),
		);
	});

});

// ─── Result formatting ──────────────────────────────────────────────────────

describe("result formatting", () => {
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

		const tool = plugin.tools![0];
		const { result } = await tool.execute({ prompt: "go", workingDir: "proj" });

		expect(result).toContain("## Tool Calls");
		expect(result).toContain("`readFile`");
		expect(result).toContain("`editFile`");
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

		const tool = plugin.tools![0];
		const { result } = await tool.execute({ prompt: "go", workingDir: "proj" });

		expect(result).toContain("## Errors");
		expect(result).toContain("lint failed");
		expect(result).toContain("type error");
	});

	it("includes stats section with success, elapsed, and session", async () => {
		const tool = plugin.tools![0];
		const { result } = await tool.execute({ prompt: "go", workingDir: "proj" });

		expect(result).toContain("## Stats");
		expect(result).toContain("Success: true");
		expect(result).toContain("Elapsed: 1.5s");
		expect(result).toContain("Session: sess-123");
	});
});

// ─── Error handling ─────────────────────────────────────────────────────────

describe("error handling", () => {
	it("returns ## Error on bridge init failure — never throws", async () => {
		mockBridge.ensureReady.mockRejectedValue(new Error("auth failed"));

		const tool = plugin.tools![0];
		const { result } = await tool.execute({ prompt: "go", workingDir: "proj" });

		expect(result).toContain("## Error");
		expect(result).toContain("auth failed");
	});

	it("returns ## Error on runTask rejection — never throws", async () => {
		mockBridge.runTask.mockRejectedValue(new Error("timeout exceeded"));

		const tool = plugin.tools![0];
		const { result } = await tool.execute({ prompt: "go", workingDir: "proj" });

		expect(result).toContain("## Error");
		expect(result).toContain("timeout exceeded");
	});

	it("allows retry after bridge init failure", async () => {
		mockBridge.ensureReady
			.mockRejectedValueOnce(new Error("transient"))
			.mockResolvedValueOnce(undefined);

		const tool = plugin.tools![0];

		const first = await tool.execute({ prompt: "go", workingDir: "proj" });
		expect(first.result).toContain("## Error");

		const second = await tool.execute({ prompt: "go", workingDir: "proj" });
		expect(second.result).toContain("## Result");
	});
});

// ─── Input validation ───────────────────────────────────────────────────────

describe("input validation", () => {
	const exec = (args: Record<string, any>) => plugin.tools![0].execute(args);

	it("rejects empty prompt", async () => {
		const { result } = await exec({ prompt: "", workingDir: "proj" });
		expect(result).toContain("## Error");
		expect(result).toContain("`prompt` must be a non-empty string");
	});

	it("rejects non-string prompt", async () => {
		const { result } = await exec({ prompt: 42, workingDir: "proj" });
		expect(result).toContain("## Error");
		expect(result).toContain("`prompt` must be a non-empty string");
	});

	it("rejects too-long prompt (>500000 chars)", async () => {
		const { result } = await exec({ prompt: "x".repeat(500_001), workingDir: "proj" });
		expect(result).toContain("## Error");
		expect(result).toContain("exceeds maximum length");
	});

	it("rejects empty workingDir", async () => {
		const { result } = await exec({ prompt: "go", workingDir: "" });
		expect(result).toContain("## Error");
		expect(result).toContain("`workingDir` is required");
	});

	it("rejects missing workingDir", async () => {
		const { result } = await exec({ prompt: "go" });
		expect(result).toContain("## Error");
		expect(result).toContain("`workingDir` is required");
	});

	it("rejects negative timeout", async () => {
		const { result } = await exec({ prompt: "go", workingDir: "proj", timeout: -1 });
		expect(result).toContain("## Error");
		expect(result).toContain("`timeout` must be a non-negative number");
	});

	it("rejects non-number timeout", async () => {
		const { result } = await exec({ prompt: "go", workingDir: "proj", timeout: "fast" });
		expect(result).toContain("## Error");
		expect(result).toContain("`timeout` must be a non-negative number");
	});

	it("rejects non-string persona", async () => {
		const { result } = await exec({ prompt: "go", workingDir: "proj", persona: 123 });
		expect(result).toContain("## Error");
		expect(result).toContain("`persona` must be a string");
	});

	it("rejects too-long persona (>50000 chars)", async () => {
		const { result } = await exec({
			prompt: "go",
			workingDir: "proj",
			persona: "x".repeat(50_001),
		});
		expect(result).toContain("## Error");
		expect(result).toContain("`persona` must be a string");
	});

});


