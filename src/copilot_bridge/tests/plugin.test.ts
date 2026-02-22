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
			expect(plugin.tools?.length).toBe(2);
		});

		it("has an onLoad function", () => {
			expect(typeof plugin.onLoad).toBe("function");
		});

		it("onLoad resolves without throwing", async () => {
			await expect(plugin.onLoad!({})).resolves.toBeUndefined();
		});

		it("tool names match expected", () => {
			const names = plugin.tools?.map((t) => t.name);
			expect(names).toContain("copilot_code");
			expect(names).toContain("copilot_code_verbose");
		});
	});

	describe("tool parameter schemas", () => {
		it("copilot_code has correct parameters", () => {
			const tool = findTool("copilot_code");
			expect(tool).toBeDefined();
			expect(tool!.parameters.type).toBe("object");
			expect(tool!.parameters.required).toEqual(["task"]);
			expect(Object.keys(tool!.parameters.properties)).toEqual(
				expect.arrayContaining(["task", "workingDir", "model", "timeout"]),
			);
		});

		it("copilot_code_verbose has correct parameters", () => {
			const tool = findTool("copilot_code_verbose");
			expect(tool).toBeDefined();
			expect(tool!.parameters.type).toBe("object");
			expect(tool!.parameters.required).toEqual(["task"]);
			expect(Object.keys(tool!.parameters.properties)).toEqual(
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
});
