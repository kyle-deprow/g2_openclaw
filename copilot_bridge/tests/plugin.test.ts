import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// --- Hoisted mocks ---

const { mockBridge } = vi.hoisted(() => {
	const mockBridge = {
		ensureReady: vi.fn().mockResolvedValue(undefined),
		runTask: vi.fn(),
		runTaskStreaming: vi.fn(),
		stop: vi.fn().mockResolvedValue(undefined),
		isReady: vi.fn().mockResolvedValue(true),
		getStatus: vi.fn().mockResolvedValue({ connected: true, authMethod: "user" }),
	};
	return { mockBridge };
});

vi.mock("../src/config.js", () => ({
	loadConfig: vi.fn().mockReturnValue({
		githubToken: undefined,
		byokProvider: undefined,
		byokApiKey: undefined,
		byokBaseUrl: undefined,
		byokModel: undefined,
		cliPath: undefined,
		logLevel: "warning",
		openclawHost: "127.0.0.1",
		openclawPort: 18789,
		openclawToken: undefined,
	}),
}));

vi.mock("../src/client.js", () => ({
	CopilotBridge: vi.fn().mockImplementation(() => mockBridge),
}));

const { mockOrchestrator, mockPool } = vi.hoisted(() => {
	const mockOrchestrator = {
		planTasks: vi.fn(),
		executePlan: vi.fn(),
		on: vi.fn().mockReturnValue(vi.fn()),
	};
	const mockPool = {
		drain: vi.fn().mockResolvedValue(undefined),
		execute: vi.fn(),
		getActiveCount: vi.fn().mockReturnValue(0),
	};
	return { mockOrchestrator, mockPool };
});

vi.mock("../src/orchestrator.js", () => ({
	TaskOrchestrator: vi.fn().mockImplementation(() => mockOrchestrator),
	SessionPool: vi.fn().mockImplementation(() => mockPool),
}));

const { default: plugin, _resetBridge } = await import("../src/plugin.js");
const { CopilotBridge } = await import("../src/client.js");

// --- Helpers ---

function findTool(name: string) {
	return plugin.tools?.find((t) => t.name === name);
}

async function* fakeStream(
	deltas: Array<import("../src/types.js").StreamingDelta>,
): AsyncGenerator<import("../src/types.js").StreamingDelta> {
	for (const d of deltas) {
		yield d;
	}
}

// --- Tests ---

describe("OpenClaw Plugin", () => {
	beforeEach(async () => {
		vi.clearAllMocks();
		await _resetBridge();

		mockBridge.ensureReady.mockResolvedValue(undefined);
		mockBridge.runTask.mockResolvedValue({
			success: true,
			content: "Hello world",
			toolCalls: [],
			errors: [],
			sessionId: "sess-123",
			elapsed: 1500,
		});
		mockBridge.runTaskStreaming.mockImplementation(() =>
			fakeStream([
				{ type: "text", content: "streamed content" },
				{ type: "done", content: "" },
			]),
		);
	});

	afterEach(async () => {
		await _resetBridge();
	});

	describe("plugin shape", () => {
		it("exports name, version, and tools array", () => {
			expect(plugin.name).toBe("copilot-bridge");
			expect(plugin.version).toBe("1.0.0");
			expect(Array.isArray(plugin.tools)).toBe(true);
			expect(plugin.tools?.length).toBe(3);
		});

		it("has an onLoad function", () => {
			expect(typeof plugin.onLoad).toBe("function");
		});

		it("onLoad resolves without throwing", async () => {
			await expect(plugin.onLoad?.({})).resolves.toBeUndefined();
		});

		it("tool names match expected", () => {
			const names = plugin.tools?.map((t) => t.name);
			expect(names).toContain("copilot_code");
			expect(names).toContain("copilot_code_verbose");
			expect(names).toContain("copilot_orchestrate");
		});
	});

	describe("tool parameter schemas", () => {
		it("copilot_code has correct parameters", () => {
			const tool = findTool("copilot_code");
			expect(tool).toBeDefined();
			expect(tool?.parameters.type).toBe("object");
			expect(tool?.parameters.required).toEqual(["task"]);
			expect(Object.keys(tool?.parameters.properties)).toEqual(
				expect.arrayContaining(["task", "workingDir", "model", "timeout"]),
			);
		});

		it("copilot_code_verbose has correct parameters", () => {
			const tool = findTool("copilot_code_verbose");
			expect(tool).toBeDefined();
			expect(tool?.parameters.type).toBe("object");
			expect(tool?.parameters.required).toEqual(["task"]);
			expect(Object.keys(tool?.parameters.properties)).toEqual(
				expect.arrayContaining(["task", "workingDir", "model", "timeout"]),
			);
		});
	});

	describe("shared bridge singleton", () => {
		it("bridge is lazy-initialized on first tool call, not at import time", async () => {
			// After reset, no tool call has been made yet, so bridge should not exist
			// Use a fresh execution: reset + verify no construction before first execute
			await _resetBridge();
			vi.mocked(CopilotBridge).mockClear();

			// Before any tool call, constructor should not have been called
			expect(CopilotBridge).not.toHaveBeenCalled();

			// Now trigger a tool call
			const tool = findTool("copilot_code")!;
			await tool.execute({ task: "lazy init check" });

			// Constructor called exactly once on first use
			expect(CopilotBridge).toHaveBeenCalledTimes(1);
		});

		it("bridge is created once and reused across calls", async () => {
			const tool = findTool("copilot_code")!;
			await tool.execute({ task: "first call" });
			expect(CopilotBridge).toHaveBeenCalledTimes(1);

			await tool.execute({ task: "second call" });
			// Still only one construction — reused
			expect(CopilotBridge).toHaveBeenCalledTimes(1);
			expect(mockBridge.ensureReady).toHaveBeenCalledTimes(1);
			expect(mockBridge.runTask).toHaveBeenCalledTimes(2);
		});
	});

	describe("copilot_code tool", () => {
		it("calls runTask and formats result as markdown", async () => {
			mockBridge.runTask.mockResolvedValue({
				success: true,
				content: "def reverse(s): return s[::-1]",
				toolCalls: [
					{
						tool: "write_file",
						args: { path: "main.py" },
						result: "ok",
						timestamp: 1000,
					},
				],
				errors: [],
				sessionId: "session-abc",
				elapsed: 2350,
			});

			const tool = findTool("copilot_code")!;
			const { result } = await tool.execute({ task: "write a reverse function" });

			expect(result).toContain("## Result");
			expect(result).toContain("def reverse(s): return s[::-1]");
			expect(result).toContain("## Tool Calls");
			expect(result).toContain("`write_file`");
			expect(result).toContain("## Stats");
			expect(result).toContain("Elapsed: 2.4s");
			expect(result).toContain("Session: session-abc");
		});

		it("passes workingDir, model, and timeout to runTask", async () => {
			const tool = findTool("copilot_code")!;
			await tool.execute({
				task: "do stuff",
				workingDir: "/tmp/work",
				model: "gpt-4o",
				timeout: 60000,
			});

			expect(mockBridge.runTask).toHaveBeenCalledWith({
				prompt: "do stuff",
				workingDir: "/tmp/work",
				model: "gpt-4o",
				timeout: 60000,
			});
		});

		it("uses default timeout of 120000 when not specified", async () => {
			const tool = findTool("copilot_code")!;
			await tool.execute({ task: "do stuff" });

			expect(mockBridge.runTask).toHaveBeenCalledWith(
				expect.objectContaining({ timeout: 120_000 }),
			);
		});

		it("returns error in result string on failure, never throws", async () => {
			mockBridge.runTask.mockRejectedValue(new Error("connection refused"));

			const tool = findTool("copilot_code")!;
			const { result } = await tool.execute({ task: "fail" });

			expect(result).toContain("## Error");
			expect(result).toContain("connection refused");
		});

		it("returns error when getBridge fails", async () => {
			mockBridge.ensureReady.mockRejectedValue(new Error("not authenticated"));

			const tool = findTool("copilot_code")!;
			const { result } = await tool.execute({ task: "fail" });

			expect(result).toContain("## Error");
			expect(result).toContain("not authenticated");
		});

		it("includes errors and success status when runTask returns failure", async () => {
			mockBridge.runTask.mockResolvedValue({
				success: false,
				content: "",
				toolCalls: [],
				errors: ["internal SDK error", "model overloaded"],
				sessionId: "sess-err",
				elapsed: 500,
			});

			const tool = findTool("copilot_code")!;
			const { result } = await tool.execute({ task: "failing task" });

			expect(result).toContain("## Errors");
			expect(result).toContain("internal SDK error");
			expect(result).toContain("model overloaded");
			expect(result).toContain("Success: false");
		});

		it("formats tool calls even when content is empty", async () => {
			mockBridge.runTask.mockResolvedValue({
				success: true,
				content: "",
				toolCalls: [
					{ tool: "create_file", args: { path: "out.py" }, result: "created", timestamp: 1000 },
				],
				errors: [],
				sessionId: "sess-empty",
				elapsed: 800,
			});

			const tool = findTool("copilot_code")!;
			const { result } = await tool.execute({ task: "file-only task" });

			expect(result).toContain("## Tool Calls");
			expect(result).toContain("`create_file`");
			expect(result).toContain("## Result");
		});
	});

	describe("copilot_code_verbose tool", () => {
		it("calls runTaskStreaming and aggregates log", async () => {
			mockBridge.runTaskStreaming.mockReturnValue(
				fakeStream([
					{ type: "tool_start", content: "", tool: "read_file" },
					{ type: "tool_end", content: "file contents here", tool: "read_file" },
					{ type: "text", content: "Here is the result" },
					{ type: "done", content: "" },
				]),
			);

			const tool = findTool("copilot_code_verbose")!;
			const { result } = await tool.execute({ task: "verbose task" });

			expect(result).toContain("## Execution Log");
			expect(result).toContain("🔧 Called `read_file`");
			expect(result).toContain("18 chars result");
			expect(result).toContain("✅ Complete");
			expect(result).toContain("## Result");
			expect(result).toContain("Here is the result");
		});

		it("passes streaming: true to runTaskStreaming", async () => {
			const tool = findTool("copilot_code_verbose")!;
			await tool.execute({ task: "verbose task" });

			expect(mockBridge.runTaskStreaming).toHaveBeenCalledWith(
				expect.objectContaining({ streaming: true }),
			);
		});

		it("returns error in result on failure, never throws", async () => {
			mockBridge.runTaskStreaming.mockImplementation(() => {
				throw new Error("stream broke");
			});

			const tool = findTool("copilot_code_verbose")!;
			const { result } = await tool.execute({ task: "fail verbose" });

			expect(result).toContain("## Error");
			expect(result).toContain("stream broke");
		});

		it("handles error deltas in the stream", async () => {
			mockBridge.runTaskStreaming.mockReturnValue(
				fakeStream([
					{ type: "error", content: "something went wrong" },
					{ type: "done", content: "" },
				]),
			);

			const tool = findTool("copilot_code_verbose")!;
			const { result } = await tool.execute({ task: "error in stream" });

			expect(result).toContain("❌ Error: something went wrong");
			expect(result).toContain("✅ Complete");
		});

		it("returns error when getBridge fails", async () => {
			mockBridge.ensureReady.mockRejectedValue(new Error("config invalid"));

			const tool = findTool("copilot_code_verbose")!;
			const { result } = await tool.execute({ task: "fail" });

			expect(result).toContain("## Error");
			expect(result).toContain("config invalid");
		});

		it("shows argument summary when tool_start carries JSON args", async () => {
			mockBridge.runTaskStreaming.mockReturnValue(
				fakeStream([
					{ type: "tool_start", content: '{"path":"main.py","lang":"python"}', tool: "write_file" },
					{ type: "tool_end", content: "ok", tool: "write_file" },
					{ type: "done", content: "" },
				]),
			);

			const tool = findTool("copilot_code_verbose")!;
			const { result } = await tool.execute({ task: "args test" });

			expect(result).toContain("path, lang");
		});

		it("falls back to raw string when tool_start content is not valid JSON", async () => {
			mockBridge.runTaskStreaming.mockReturnValue(
				fakeStream([
					{ type: "tool_start", content: "not json", tool: "broken_tool" },
					{ type: "tool_end", content: "result", tool: "broken_tool" },
					{ type: "done", content: "" },
				]),
			);

			const tool = findTool("copilot_code_verbose")!;
			const { result } = await tool.execute({ task: "bad json args" });

			expect(result).toContain("not json");
			expect(result).not.toContain("## Error");
		});
	});

	describe("copilot_orchestrate", () => {
		const mockPlan = {
			tasks: [
				{ id: "t1", description: "Create utils", estimatedComplexity: "S" },
				{ id: "t2", description: "Add tests", estimatedComplexity: "M" },
			],
			dependencies: new Map([["t2", ["t1"]]]),
		};

		const mockOrchestratedResult = {
			tasks: [
				{
					id: "t1",
					result: {
						success: true,
						content: "Utils created",
						toolCalls: [],
						errors: [],
						sessionId: "s1",
						elapsed: 1000,
					},
					status: "success" as const,
				},
				{
					id: "t2",
					result: {
						success: true,
						content: "Tests added",
						toolCalls: [],
						errors: [],
						sessionId: "s2",
						elapsed: 2000,
					},
					status: "success" as const,
				},
			],
			totalElapsed: 3000,
			summary: "2/2 tasks succeeded, 0 skipped, 0 failed",
			plan: mockPlan,
		};

		beforeEach(() => {
			mockOrchestrator.planTasks.mockResolvedValue(mockPlan);
			mockOrchestrator.executePlan.mockResolvedValue(mockOrchestratedResult);
		});

		it("has correct tool schema", () => {
			const tool = findTool("copilot_orchestrate");
			expect(tool).toBeDefined();
			expect(tool?.parameters.type).toBe("object");
			expect(tool?.parameters.required).toEqual(["task"]);
			expect(Object.keys(tool?.parameters.properties)).toEqual(
				expect.arrayContaining(["task", "maxConcurrency", "timeout"]),
			);
		});

		it("decomposes task, executes plan, and returns formatted result", async () => {
			const tool = findTool("copilot_orchestrate")!;
			const { result } = await tool.execute({ task: "build a REST API" });

			expect(mockOrchestrator.planTasks).toHaveBeenCalledWith("build a REST API");
			expect(mockOrchestrator.executePlan).toHaveBeenCalledWith(mockPlan);

			expect(result).toContain("## Task Plan");
			expect(result).toContain("**t1**: Create utils [S]");
			expect(result).toContain("**t2**: Add tests [M]");
			expect(result).toContain("## Results");
			expect(result).toContain("✅ t1");
			expect(result).toContain("Utils created");
			expect(result).toContain("✅ t2");
			expect(result).toContain("Tests added");
			expect(result).toContain("## Summary");
			expect(result).toContain("2/2 tasks succeeded");
			expect(result).toContain("Total elapsed: 3.0s");
		});

		it("uses custom maxConcurrency when provided", async () => {
			const { SessionPool } = await import("../src/orchestrator.js");

			const tool = findTool("copilot_orchestrate")!;
			await tool.execute({ task: "parallel work", maxConcurrency: 5 });

			expect(SessionPool).toHaveBeenCalledWith(expect.anything(), 5);
		});

		it("drains pool after execution", async () => {
			const tool = findTool("copilot_orchestrate")!;
			await tool.execute({ task: "drain test" });

			expect(mockPool.drain).toHaveBeenCalledTimes(1);
		});

		it("returns error message on orchestrator failure", async () => {
			mockOrchestrator.executePlan.mockRejectedValue(new Error("orchestrator crashed"));

			const tool = findTool("copilot_orchestrate")!;
			const { result } = await tool.execute({ task: "will fail" });

			expect(result).toContain("## Error");
			expect(result).toContain("orchestrator crashed");
		});

		it("falls back gracefully when plan fails", async () => {
			mockOrchestrator.planTasks.mockRejectedValue(new Error("planning failed: task too vague"));

			const tool = findTool("copilot_orchestrate")!;
			const { result } = await tool.execute({ task: "do something" });

			expect(result).toContain("## Error");
			expect(result).toContain("planning failed: task too vague");
		});
	});

	describe("input validation", () => {
		it("copilot_code rejects empty task", async () => {
			const tool = findTool("copilot_code")!;
			const { result } = await tool.execute({ task: "" });
			expect(result).toContain("## Error");
			expect(result).toContain("`task` must be a non-empty string");
		});

		it("copilot_code rejects non-string task", async () => {
			const tool = findTool("copilot_code")!;
			const { result } = await tool.execute({ task: 123 });
			expect(result).toContain("## Error");
			expect(result).toContain("`task` must be a non-empty string");
		});

		it("copilot_code rejects task exceeding max length", async () => {
			const tool = findTool("copilot_code")!;
			const { result } = await tool.execute({ task: "x".repeat(50_001) });
			expect(result).toContain("## Error");
			expect(result).toContain("`task` exceeds maximum length (50000 chars)");
		});

		it("copilot_code rejects non-string workingDir", async () => {
			const tool = findTool("copilot_code")!;
			const { result } = await tool.execute({ task: "do stuff", workingDir: 42 });
			expect(result).toContain("## Error");
			expect(result).toContain("`workingDir` must be a string");
		});

		it("copilot_code rejects negative timeout", async () => {
			const tool = findTool("copilot_code")!;
			const { result } = await tool.execute({ task: "do stuff", timeout: -1 });
			expect(result).toContain("## Error");
			expect(result).toContain("`timeout` must be a non-negative number");
		});

		it("copilot_code rejects non-number timeout", async () => {
			const tool = findTool("copilot_code")!;
			const { result } = await tool.execute({ task: "do stuff", timeout: "fast" });
			expect(result).toContain("## Error");
			expect(result).toContain("`timeout` must be a non-negative number");
		});

		it("copilot_code_verbose rejects empty task", async () => {
			const tool = findTool("copilot_code_verbose")!;
			const { result } = await tool.execute({ task: "" });
			expect(result).toContain("## Error");
			expect(result).toContain("`task` must be a non-empty string");
		});

		it("copilot_code_verbose rejects task exceeding max length", async () => {
			const tool = findTool("copilot_code_verbose")!;
			const { result } = await tool.execute({ task: "x".repeat(50_001) });
			expect(result).toContain("## Error");
			expect(result).toContain("`task` exceeds maximum length (50000 chars)");
		});

		it("copilot_orchestrate rejects empty task", async () => {
			const tool = findTool("copilot_orchestrate")!;
			const { result } = await tool.execute({ task: "" });
			expect(result).toContain("## Error");
			expect(result).toContain("`task` must be a non-empty string");
		});

		it("copilot_orchestrate rejects task exceeding max length", async () => {
			const tool = findTool("copilot_orchestrate")!;
			const { result } = await tool.execute({ task: "x".repeat(50_001) });
			expect(result).toContain("## Error");
			expect(result).toContain("`task` exceeds maximum length (50000 chars)");
		});

		it("copilot_orchestrate treats NaN maxConcurrency as default", async () => {
			const { SessionPool } = await import("../src/orchestrator.js");

			const tool = findTool("copilot_orchestrate")!;
			await tool.execute({ task: "nan test", maxConcurrency: Number.NaN });

			expect(SessionPool).toHaveBeenCalledWith(expect.anything(), 3);
		});
	});
});
