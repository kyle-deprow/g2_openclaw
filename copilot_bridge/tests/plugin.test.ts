import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// --- Hoisted mocks ---

const { mockBridge } = vi.hoisted(() => {
	const mockBridge = {
		ensureReady: vi.fn().mockResolvedValue(undefined),
		runTask: vi.fn(),
		runTaskStreaming: vi.fn(),
		resumeTask: vi.fn(),
		listPersistedSessions: vi.fn(),
		destroyPersistedSession: vi.fn(),
		stop: vi.fn().mockResolvedValue(undefined),
		isReady: vi.fn().mockResolvedValue(true),
		getStatus: vi.fn().mockResolvedValue({ connected: true, authMethod: "user" }),
		getMostRecentSession: vi.fn(),
		getSessionTranscript: vi.fn(),
		resolveWorkingDir: vi.fn().mockImplementation(async (dir: string) => `/resolved/${dir}`),
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
		projectsRoot: "/home/test/repos",
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
		mockBridge.resumeTask.mockResolvedValue({
			success: true,
			content: "Resumed response",
			toolCalls: [],
			errors: [],
			sessionId: "sess-existing",
			elapsed: 1000,
		});
		mockBridge.listPersistedSessions.mockResolvedValue([]);
		mockBridge.destroyPersistedSession.mockResolvedValue(undefined);
		mockBridge.getMostRecentSession.mockResolvedValue(null);
		mockBridge.getSessionTranscript.mockResolvedValue([]);
		mockBridge.resolveWorkingDir.mockImplementation(async (dir: string) => `/resolved/${dir}`);
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
			expect(plugin.tools?.length).toBe(7);
		});

		it("has an onLoad function", () => {
			expect(typeof plugin.onLoad).toBe("function");
		});

		it("onLoad resolves without throwing", async () => {
			await expect(plugin.onLoad?.({})).resolves.toBeUndefined();
		});

		it("tool names match expected", () => {
			const names = plugin.tools?.map((t) => t.name);
			expect(names).toContain("copilot_code_start");
			expect(names).toContain("copilot_code_message");
			expect(names).toContain("copilot_code_verbose");
			expect(names).toContain("copilot_orchestrate");
			expect(names).toContain("copilot_code_transcript");
			expect(names).toContain("copilot_list_sessions");
			expect(names).toContain("copilot_destroy_session");
		});
	});

	describe("tool parameter schemas", () => {
		it("copilot_code_message has correct parameters", () => {
			const tool = findTool("copilot_code_message");
			expect(tool).toBeDefined();
			expect(tool?.parameters.type).toBe("object");
			expect(tool?.parameters.required).toEqual(["task"]);
			expect(Object.keys(tool?.parameters.properties)).toEqual(
				expect.arrayContaining(["task", "timeout", "sessionId"]),
			);
		});

		it("copilot_code_verbose has correct parameters", () => {
			const tool = findTool("copilot_code_verbose");
			expect(tool).toBeDefined();
			expect(tool?.parameters.type).toBe("object");
			expect(tool?.parameters.required).toEqual(["task"]);
			expect(Object.keys(tool?.parameters.properties)).toEqual(
				expect.arrayContaining(["task", "timeout", "sessionId"]),
			);
		});

		it("copilot_code_start has correct parameters", () => {
			const tool = findTool("copilot_code_start");
			expect(tool).toBeDefined();
			expect(tool?.parameters.type).toBe("object");
			expect(tool?.parameters.required).toEqual(["task", "workingDir"]);
			expect(Object.keys(tool?.parameters.properties)).toEqual(
				expect.arrayContaining(["task", "workingDir", "model", "timeout"]),
			);
		});

		it("copilot_code_transcript has correct parameters", () => {
			const tool = findTool("copilot_code_transcript");
			expect(tool).toBeDefined();
			expect(tool?.parameters.type).toBe("object");
			expect(Object.keys(tool?.parameters.properties)).toEqual(
				expect.arrayContaining(["sessionId", "count"]),
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
			mockBridge.getMostRecentSession.mockResolvedValue({
				sessionId: "sess-lazy",
				task: "task",
				startTime: "2026-02-27T10:00:00Z",
				lastActivity: "2026-02-27T11:00:00Z",
				messages: [],
			});
			const tool = findTool("copilot_code_message")!;
			await tool.execute({ task: "lazy init check" });

			// Constructor called exactly once on first use
			expect(CopilotBridge).toHaveBeenCalledTimes(1);
		});

		it("bridge is created once and reused across calls", async () => {
			mockBridge.getMostRecentSession.mockResolvedValue({
				sessionId: "sess-reuse",
				task: "task",
				startTime: "2026-02-27T10:00:00Z",
				lastActivity: "2026-02-27T11:00:00Z",
				messages: [],
			});
			const tool = findTool("copilot_code_message")!;
			await tool.execute({ task: "first call" });
			expect(CopilotBridge).toHaveBeenCalledTimes(1);

			await tool.execute({ task: "second call" });
			// Still only one construction — reused
			expect(CopilotBridge).toHaveBeenCalledTimes(1);
			expect(mockBridge.ensureReady).toHaveBeenCalledTimes(1);
			expect(mockBridge.resumeTask).toHaveBeenCalledTimes(2);
		});
	});

	describe("copilot_code_message tool", () => {
		it("auto-selects most recent session and formats result as markdown", async () => {
			mockBridge.getMostRecentSession.mockResolvedValue({
				sessionId: "sess-recent",
				task: "Previous",
				startTime: "2026-02-27T10:00:00Z",
				lastActivity: "2026-02-27T11:00:00Z",
				messages: [],
			});
			mockBridge.resumeTask.mockResolvedValue({
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
				sessionId: "sess-recent",
				elapsed: 2350,
			});

			const tool = findTool("copilot_code_message")!;
			const { result } = await tool.execute({ task: "write a reverse function" });

			expect(result).toContain("## Result");
			expect(result).toContain("def reverse(s): return s[::-1]");
			expect(result).toContain("## Tool Calls");
			expect(result).toContain("`write_file`");
			expect(result).toContain("## Stats");
			expect(result).toContain("Elapsed: 2.4s");
			expect(result).toContain("Session: sess-recent");
			expect(mockBridge.getMostRecentSession).toHaveBeenCalled();
			expect(mockBridge.resumeTask).toHaveBeenCalledWith("sess-recent", "write a reverse function", 120_000);
		});

		it("returns error when no sessions exist and no sessionId", async () => {
			mockBridge.getMostRecentSession.mockResolvedValue(null);

			const tool = findTool("copilot_code_message")!;
			const { result } = await tool.execute({ task: "do stuff" });

			expect(result).toContain("## Error");
			expect(result).toContain("No active sessions");
			expect(result).toContain("copilot_code_start");
			expect(mockBridge.resumeTask).not.toHaveBeenCalled();
		});

		it("uses provided sessionId directly", async () => {
			const tool = findTool("copilot_code_message")!;
			await tool.execute({
				task: "do stuff",
				sessionId: "sess-specific",
			});

			expect(mockBridge.resumeTask).toHaveBeenCalledWith("sess-specific", "do stuff", 120_000);
			expect(mockBridge.getMostRecentSession).not.toHaveBeenCalled();
		});

		it("uses default timeout of 120000 when not specified", async () => {
			mockBridge.getMostRecentSession.mockResolvedValue({
				sessionId: "sess-recent",
				task: "task",
				startTime: "2026-02-27T10:00:00Z",
				lastActivity: "2026-02-27T11:00:00Z",
				messages: [],
			});
			const tool = findTool("copilot_code_message")!;
			await tool.execute({ task: "do stuff" });

			expect(mockBridge.resumeTask).toHaveBeenCalledWith(
				"sess-recent", "do stuff", 120_000,
			);
		});

		it("returns error in result string on failure, never throws", async () => {
			mockBridge.getMostRecentSession.mockResolvedValue({
				sessionId: "sess-recent",
				task: "task",
				startTime: "2026-02-27T10:00:00Z",
				lastActivity: "2026-02-27T11:00:00Z",
				messages: [],
			});
			mockBridge.resumeTask.mockRejectedValue(new Error("connection refused"));

			const tool = findTool("copilot_code_message")!;
			const { result } = await tool.execute({ task: "fail" });

			expect(result).toContain("## Error");
			expect(result).toContain("connection refused");
		});

		it("returns error when getBridge fails", async () => {
			mockBridge.ensureReady.mockRejectedValue(new Error("not authenticated"));

			const tool = findTool("copilot_code_message")!;
			const { result } = await tool.execute({ task: "fail" });

			expect(result).toContain("## Error");
			expect(result).toContain("not authenticated");
		});

		it("includes errors and success status when resumeTask returns failure", async () => {
			mockBridge.getMostRecentSession.mockResolvedValue({
				sessionId: "sess-recent",
				task: "task",
				startTime: "2026-02-27T10:00:00Z",
				lastActivity: "2026-02-27T11:00:00Z",
				messages: [],
			});
			mockBridge.resumeTask.mockResolvedValue({
				success: false,
				content: "",
				toolCalls: [],
				errors: ["internal SDK error", "model overloaded"],
				sessionId: "sess-err",
				elapsed: 500,
			});

			const tool = findTool("copilot_code_message")!;
			const { result } = await tool.execute({ task: "failing task" });

			expect(result).toContain("## Errors");
			expect(result).toContain("internal SDK error");
			expect(result).toContain("model overloaded");
			expect(result).toContain("Success: false");
		});
	});

	describe("copilot_code_start tool", () => {
		it("calls runTask with persistSession: true", async () => {
			const tool = findTool("copilot_code_start")!;
			const { result } = await tool.execute({ task: "start new project", workingDir: "my-api" });

			expect(mockBridge.resolveWorkingDir).toHaveBeenCalledWith("my-api");
			expect(mockBridge.runTask).toHaveBeenCalledWith({
				prompt: "start new project",
				workingDir: "/resolved/my-api",
				model: undefined,
				timeout: 120_000,
				persistSession: true,
			});
			expect(result).toContain("## Result");
			expect(result).toContain("Hello world");
		});

		it("passes workingDir, model, and timeout", async () => {
			const tool = findTool("copilot_code_start")!;
			await tool.execute({
				task: "start",
				workingDir: "/tmp/work",
				model: "gpt-4o",
				timeout: 60000,
			});

			expect(mockBridge.resolveWorkingDir).toHaveBeenCalledWith("/tmp/work");
			expect(mockBridge.runTask).toHaveBeenCalledWith({
				prompt: "start",
				workingDir: "/resolved//tmp/work",
				model: "gpt-4o",
				timeout: 60000,
				persistSession: true,
			});
		});

		it("resolves bare project name via resolveWorkingDir", async () => {
			const tool = findTool("copilot_code_start")!;
			await tool.execute({ task: "start", workingDir: "cool-project" });

			expect(mockBridge.resolveWorkingDir).toHaveBeenCalledWith("cool-project");
			expect(mockBridge.runTask).toHaveBeenCalledWith(
				expect.objectContaining({ workingDir: "/resolved/cool-project" }),
			);
		});

		it("rejects empty workingDir with helpful error", async () => {
			const tool = findTool("copilot_code_start")!;
			const { result } = await tool.execute({ task: "start" });
			expect(result).toContain("`workingDir` is required");
			expect(mockBridge.runTask).not.toHaveBeenCalled();
		});

		it("rejects empty string workingDir", async () => {
			const tool = findTool("copilot_code_start")!;
			const { result } = await tool.execute({ task: "start", workingDir: "" });
			expect(result).toContain("`workingDir` is required");
			expect(mockBridge.runTask).not.toHaveBeenCalled();
		});

		it("returns error on failure", async () => {
			mockBridge.runTask.mockRejectedValue(new Error("connection refused"));

			const tool = findTool("copilot_code_start")!;
			const { result } = await tool.execute({ task: "fail", workingDir: "my-api" });

			expect(result).toContain("## Error");
			expect(result).toContain("connection refused");
		});

		it("rejects empty task", async () => {
			const tool = findTool("copilot_code_start")!;
			const { result } = await tool.execute({ task: "", workingDir: "my-api" });
			expect(result).toContain("`task` must be a non-empty string");
		});

		it("rejects task exceeding max length", async () => {
			const tool = findTool("copilot_code_start")!;
			const { result } = await tool.execute({ task: "x".repeat(50_001), workingDir: "my-api" });
			expect(result).toContain("`task` exceeds maximum length");
		});
	});

	describe("copilot_code_transcript tool", () => {
		it("returns transcript for given sessionId", async () => {
			mockBridge.getSessionTranscript.mockResolvedValue([
				{ role: "user", content: "Hello", timestamp: "2026-02-27T10:00:00Z" },
				{ role: "assistant", content: "Hi there", timestamp: "2026-02-27T10:00:01Z" },
			]);

			const tool = findTool("copilot_code_transcript")!;
			const { result } = await tool.execute({ sessionId: "sess-1", count: 2 });

			expect(result).toContain("## Transcript");
			expect(result).toContain("**user**");
			expect(result).toContain("Hello");
			expect(result).toContain("**assistant**");
			expect(result).toContain("Hi there");
			expect(mockBridge.getSessionTranscript).toHaveBeenCalledWith("sess-1", 2);
		});

		it("defaults to most recent session when no sessionId", async () => {
			mockBridge.getMostRecentSession.mockResolvedValue({
				sessionId: "sess-recent",
				task: "Recent",
				startTime: "2026-02-27T10:00:00Z",
				lastActivity: "2026-02-27T11:00:00Z",
				messages: [],
			});
			mockBridge.getSessionTranscript.mockResolvedValue([
				{ role: "user", content: "Test", timestamp: "2026-02-27T10:00:00Z" },
			]);

			const tool = findTool("copilot_code_transcript")!;
			const { result } = await tool.execute({});

			expect(mockBridge.getMostRecentSession).toHaveBeenCalled();
			expect(mockBridge.getSessionTranscript).toHaveBeenCalledWith("sess-recent", 2);
			expect(result).toContain("Test");
		});

		it("returns error when no sessions exist and no sessionId", async () => {
			mockBridge.getMostRecentSession.mockResolvedValue(null);

			const tool = findTool("copilot_code_transcript")!;
			const { result } = await tool.execute({});

			expect(result).toContain("## Error");
			expect(result).toContain("No active sessions");
		});

		it("returns empty message for session with no messages", async () => {
			mockBridge.getSessionTranscript.mockResolvedValue([]);

			const tool = findTool("copilot_code_transcript")!;
			const { result } = await tool.execute({ sessionId: "sess-empty" });

			expect(result).toContain("No messages in session sess-empty");
		});

		it("passes custom count parameter", async () => {
			mockBridge.getSessionTranscript.mockResolvedValue([
				{ role: "user", content: "msg", timestamp: "2026-02-27T10:00:00Z" },
			]);

			const tool = findTool("copilot_code_transcript")!;
			await tool.execute({ sessionId: "sess-1", count: 5 });

			expect(mockBridge.getSessionTranscript).toHaveBeenCalledWith("sess-1", 5);
		});

		it("returns error on failure", async () => {
			mockBridge.getSessionTranscript.mockRejectedValue(new Error("Session not found"));

			const tool = findTool("copilot_code_transcript")!;
			const { result } = await tool.execute({ sessionId: "sess-bad" });

			expect(result).toContain("## Error");
			expect(result).toContain("Session not found");
		});
	});

	describe("copilot_code_verbose tool", () => {
		it("auto-selects most recent session and uses non-streaming resumeTask", async () => {
			mockBridge.getMostRecentSession.mockResolvedValue({
				sessionId: "sess-recent",
				task: "task",
				startTime: "2026-02-27T10:00:00Z",
				lastActivity: "2026-02-27T11:00:00Z",
				messages: [],
			});

			const tool = findTool("copilot_code_verbose")!;
			const { result } = await tool.execute({ task: "verbose task" });

			expect(result).toContain("Note: Session resume uses non-streaming mode");
			expect(result).toContain("Resumed response");
			expect(mockBridge.getMostRecentSession).toHaveBeenCalled();
			expect(mockBridge.resumeTask).toHaveBeenCalledWith("sess-recent", "verbose task", 120_000);
		});

		it("uses provided sessionId directly", async () => {
			const tool = findTool("copilot_code_verbose")!;
			const { result } = await tool.execute({ task: "verbose task", sessionId: "sess-specific" });

			expect(result).toContain("Note: Session resume uses non-streaming mode");
			expect(mockBridge.resumeTask).toHaveBeenCalledWith("sess-specific", "verbose task", 120_000);
			expect(mockBridge.getMostRecentSession).not.toHaveBeenCalled();
		});

		it("returns error when no sessions exist and no sessionId", async () => {
			mockBridge.getMostRecentSession.mockResolvedValue(null);

			const tool = findTool("copilot_code_verbose")!;
			const { result } = await tool.execute({ task: "verbose task" });

			expect(result).toContain("## Error");
			expect(result).toContain("No active sessions");
			expect(mockBridge.resumeTask).not.toHaveBeenCalled();
		});

		it("returns error in result on failure, never throws", async () => {
			mockBridge.getMostRecentSession.mockResolvedValue({
				sessionId: "sess-recent",
				task: "task",
				startTime: "2026-02-27T10:00:00Z",
				lastActivity: "2026-02-27T11:00:00Z",
				messages: [],
			});
			mockBridge.resumeTask.mockRejectedValue(new Error("stream broke"));

			const tool = findTool("copilot_code_verbose")!;
			const { result } = await tool.execute({ task: "fail verbose" });

			expect(result).toContain("## Error");
			expect(result).toContain("stream broke");
		});

		it("returns error when getBridge fails", async () => {
			mockBridge.ensureReady.mockRejectedValue(new Error("config invalid"));

			const tool = findTool("copilot_code_verbose")!;
			const { result } = await tool.execute({ task: "fail" });

			expect(result).toContain("## Error");
			expect(result).toContain("config invalid");
		});

		it("rejects non-string sessionId", async () => {
			const tool = findTool("copilot_code_verbose")!;
			const { result } = await tool.execute({ task: "work", sessionId: 42 });
			expect(result).toContain("## Error");
			expect(result).toContain("`sessionId` must be a non-empty string");
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
		it("copilot_code_message rejects empty task", async () => {
			const tool = findTool("copilot_code_message")!;
			const { result } = await tool.execute({ task: "" });
			expect(result).toContain("## Error");
			expect(result).toContain("`task` must be a non-empty string");
		});

		it("copilot_code_message rejects non-string task", async () => {
			const tool = findTool("copilot_code_message")!;
			const { result } = await tool.execute({ task: 123 });
			expect(result).toContain("## Error");
			expect(result).toContain("`task` must be a non-empty string");
		});

		it("copilot_code_message rejects task exceeding max length", async () => {
			const tool = findTool("copilot_code_message")!;
			const { result } = await tool.execute({ task: "x".repeat(50_001) });
			expect(result).toContain("## Error");
			expect(result).toContain("`task` exceeds maximum length (50000 chars)");
		});

		it("copilot_code_message rejects negative timeout", async () => {
			const tool = findTool("copilot_code_message")!;
			const { result } = await tool.execute({ task: "do stuff", timeout: -1 });
			expect(result).toContain("## Error");
			expect(result).toContain("`timeout` must be a non-negative number");
		});

		it("copilot_code_message rejects non-number timeout", async () => {
			const tool = findTool("copilot_code_message")!;
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

	// ── Session persistence — copilot_code_message ─────────────────────────────────

	describe("copilot_code_message session persistence", () => {
		it("calls resumeTask with auto-resolved session when no sessionId", async () => {
			mockBridge.getMostRecentSession.mockResolvedValue({
				sessionId: "sess-recent",
				task: "Previous",
				startTime: "2026-02-27T10:00:00Z",
				lastActivity: "2026-02-27T11:00:00Z",
				messages: [],
			});
			const tool = findTool("copilot_code_message")!;
			await tool.execute({ task: "new work" });

			expect(mockBridge.getMostRecentSession).toHaveBeenCalled();
			expect(mockBridge.resumeTask).toHaveBeenCalledWith("sess-recent", "new work", 120_000);
		});

		it("calls resumeTask when sessionId is provided", async () => {
			const tool = findTool("copilot_code_message")!;
			const { result } = await tool.execute({ task: "continue work", sessionId: "sess-existing" });

			expect(mockBridge.resumeTask).toHaveBeenCalledWith("sess-existing", "continue work", 120_000);
			expect(mockBridge.getMostRecentSession).not.toHaveBeenCalled();
			expect(result).toContain("Resumed response");
			expect(result).toContain("sess-existing");
		});

		it("rejects non-string sessionId", async () => {
			const tool = findTool("copilot_code_message")!;
			const { result } = await tool.execute({ task: "work", sessionId: 123 });
			expect(result).toContain("## Error");
			expect(result).toContain("`sessionId` must be a non-empty string");
		});

		it("returns error when resumeTask throws for unknown session", async () => {
			mockBridge.resumeTask.mockRejectedValue(new Error("No persisted session found with ID: sess-gone"));
			const tool = findTool("copilot_code_message")!;
			const { result } = await tool.execute({ task: "continue", sessionId: "sess-gone" });
			expect(result).toContain("## Error");
			expect(result).toContain("No persisted session found");
		});

		it("rejects empty string sessionId", async () => {
			const tool = findTool("copilot_code_message")!;
			const { result } = await tool.execute({ task: "test", sessionId: "" });
			expect(result).toContain("## Error");
			expect(result).toContain("`sessionId` must be a non-empty string");
		});
	});

	// ── Session persistence — copilot_code_verbose ─────────────────────────

	describe("copilot_code_verbose session persistence", () => {
		it("calls resumeTask when sessionId is provided", async () => {
			const tool = findTool("copilot_code_verbose")!;
			const { result } = await tool.execute({ task: "continue verbose", sessionId: "sess-existing" });

			expect(mockBridge.resumeTask).toHaveBeenCalledWith("sess-existing", "continue verbose", 120_000);
			expect(result).toContain("Note: Session resume uses non-streaming mode");
			expect(result).toContain("Resumed response");
		});

		it("auto-selects most recent session when no sessionId provided", async () => {
			mockBridge.getMostRecentSession.mockResolvedValue({
				sessionId: "sess-recent",
				task: "task",
				startTime: "2026-02-27T10:00:00Z",
				lastActivity: "2026-02-27T11:00:00Z",
				messages: [],
			});
			const tool = findTool("copilot_code_verbose")!;
			await tool.execute({ task: "verbose work" });

			expect(mockBridge.getMostRecentSession).toHaveBeenCalled();
			expect(mockBridge.resumeTask).toHaveBeenCalledWith("sess-recent", "verbose work", 120_000);
		});

		it("rejects non-string sessionId in persistence", async () => {
			const tool = findTool("copilot_code_verbose")!;
			const { result } = await tool.execute({ task: "work", sessionId: 42 });
			expect(result).toContain("## Error");
			expect(result).toContain("`sessionId` must be a non-empty string");
		});
	});

	// ── copilot_list_sessions ──────────────────────────────────────────────

	describe("copilot_list_sessions", () => {
		it("returns formatted sessions list", async () => {
			mockBridge.listPersistedSessions.mockResolvedValue([
				{ sessionId: "sess-1", task: "Fix bugs", lastActivity: "2026-02-27T10:00:00Z" },
				{ sessionId: "sess-2", task: "Add tests", lastActivity: "2026-02-27T11:00:00Z" },
			]);

			const tool = findTool("copilot_list_sessions")!;
			const { result } = await tool.execute({});

			expect(result).toContain("## Active Sessions");
			expect(result).toContain("sess-1");
			expect(result).toContain("Fix bugs");
			expect(result).toContain("sess-2");
			expect(result).toContain("Add tests");
		});

		it("returns empty message when no sessions", async () => {
			mockBridge.listPersistedSessions.mockResolvedValue([]);

			const tool = findTool("copilot_list_sessions")!;
			const { result } = await tool.execute({});

			expect(result).toBe("No active sessions.");
		});

		it("returns error on failure", async () => {
			mockBridge.listPersistedSessions.mockRejectedValue(new Error("disk error"));

			const tool = findTool("copilot_list_sessions")!;
			const { result } = await tool.execute({});

			expect(result).toContain("## Error");
			expect(result).toContain("disk error");
		});
	});

	// ── copilot_destroy_session ────────────────────────────────────────────

	describe("copilot_destroy_session", () => {
		it("destroys session and returns confirmation", async () => {
			const tool = findTool("copilot_destroy_session")!;
			const { result } = await tool.execute({ sessionId: "sess-to-destroy" });

			expect(mockBridge.destroyPersistedSession).toHaveBeenCalledWith("sess-to-destroy");
			expect(result).toBe("Session sess-to-destroy destroyed.");
		});

		it("rejects empty sessionId", async () => {
			const tool = findTool("copilot_destroy_session")!;
			const { result } = await tool.execute({ sessionId: "" });

			expect(result).toContain("## Error");
			expect(result).toContain("`sessionId` must be a non-empty string");
		});

		it("rejects non-string sessionId", async () => {
			const tool = findTool("copilot_destroy_session")!;
			const { result } = await tool.execute({ sessionId: 123 });

			expect(result).toContain("## Error");
			expect(result).toContain("`sessionId` must be a non-empty string");
		});

		it("returns error on failure", async () => {
			mockBridge.destroyPersistedSession.mockRejectedValue(new Error("not found"));

			const tool = findTool("copilot_destroy_session")!;
			const { result } = await tool.execute({ sessionId: "sess-missing" });

			expect(result).toContain("## Error");
			expect(result).toContain("not found");
		});
	});
});
