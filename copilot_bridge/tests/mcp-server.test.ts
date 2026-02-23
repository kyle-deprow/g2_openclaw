import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// ─── Hoisted mocks (available before vi.mock factories run) ─────────────────

const { mockSession, mockSdkClient, mockBridge, mockToolFn } = vi.hoisted(() => {
	const mockSession = {
		rpc: {
			"workspace.readFile": vi.fn(),
			"workspace.createFile": vi.fn(),
			"workspace.listFiles": vi.fn(),
		} as Record<string, ReturnType<typeof vi.fn>>,
		destroy: vi.fn().mockResolvedValue(undefined),
		sendAndWait: vi.fn(),
		on: vi.fn().mockReturnValue(vi.fn()),
		getMessages: vi.fn().mockResolvedValue([]),
	};

	const mockSdkClient = {
		createSession: vi.fn().mockResolvedValue(mockSession),
		stop: vi.fn().mockResolvedValue([]),
		forceStop: vi.fn().mockResolvedValue(undefined),
	};

	const mockBridge = {
		ensureReady: vi.fn().mockResolvedValue(undefined),
		runTask: vi.fn(),
		stop: vi.fn().mockResolvedValue(undefined),
		isReady: vi.fn().mockResolvedValue(true),
	};

	const mockToolFn = vi.fn();

	return { mockSession, mockSdkClient, mockBridge, mockToolFn };
});

// ─── Module mocks ───────────────────────────────────────────────────────────

vi.mock("@github/copilot-sdk", () => ({
	CopilotClient: vi.fn().mockImplementation(() => mockSdkClient),
}));

vi.mock("../src/client.js", () => ({
	CopilotBridge: vi.fn().mockImplementation(() => mockBridge),
}));

vi.mock("../src/config.js", () => ({
	loadConfig: vi.fn().mockReturnValue({
		githubToken: "test-token",
		cliPath: undefined,
		logLevel: "warning",
		openclawHost: "127.0.0.1",
		openclawPort: 18789,
	}),
}));

vi.mock("@modelcontextprotocol/sdk/server/mcp.js", () => ({
	McpServer: vi.fn().mockImplementation(() => ({
		tool: mockToolFn,
		connect: vi.fn().mockResolvedValue(undefined),
	})),
}));

vi.mock("@modelcontextprotocol/sdk/server/stdio.js", () => ({
	StdioServerTransport: vi.fn().mockImplementation(() => ({})),
}));

// ─── Import module under test (after mocks are in place) ────────────────────

const {
	createServer,
	ensureInitialized,
	shutdown,
	_resetState,
	acquireMutex,
	_resetMutex,
	formatResult,
	checkDepth,
	MAX_CALL_DEPTH,
} = await import("../src/mcp-server.js");

const { CopilotBridge } = await import("../src/client.js");
const { CopilotClient } = await import("@github/copilot-sdk");

// ─── Helpers ────────────────────────────────────────────────────────────────

interface ToolResult {
	content: Array<{ type: string; text: string }>;
	isError?: boolean;
}

type ToolCallback = (args: Record<string, unknown>, extra?: unknown) => Promise<ToolResult>;

function getToolCallback(name: string): ToolCallback {
	const call = mockToolFn.mock.calls.find((c: unknown[]) => c[0] === name);
	if (!call) {
		throw new Error(
			`Tool "${name}" not found. Registered: ${mockToolFn.mock.calls.map((c: unknown[]) => c[0]).join(", ")}`,
		);
	}
	// server.tool(name, description, schema, callback) — callback is arg index 3
	return call[3] as ToolCallback;
}

// ─── Tests ──────────────────────────────────────────────────────────────────

describe("MCP Server", () => {
	beforeEach(() => {
		vi.clearAllMocks();
		_resetState();
		_resetMutex();

		// Re-establish default mock implementations
		mockSession.rpc["workspace.readFile"].mockResolvedValue("file content here");
		mockSession.rpc["workspace.createFile"].mockResolvedValue(undefined);
		mockSession.rpc["workspace.listFiles"].mockResolvedValue(["file1.ts", "file2.ts"]);
		mockSession.destroy.mockResolvedValue(undefined);
		mockSdkClient.createSession.mockResolvedValue(mockSession);
		mockSdkClient.stop.mockResolvedValue([]);
		mockBridge.ensureReady.mockResolvedValue(undefined);
		mockBridge.runTask.mockResolvedValue({
			success: true,
			content: "Generated code output",
			toolCalls: [],
			errors: [],
			sessionId: "sess-abc",
			elapsed: 2000,
		});
		mockBridge.stop.mockResolvedValue(undefined);
	});

	afterEach(() => {
		_resetState();
		_resetMutex();
	});

	// ── Tool discovery ──────────────────────────────────────────────────────

	describe("tool discovery", () => {
		it("registers exactly 4 tools", () => {
			createServer();
			expect(mockToolFn).toHaveBeenCalledTimes(4);
		});

		it("registers tools with the expected names", () => {
			createServer();
			const names = mockToolFn.mock.calls.map((c: unknown[]) => c[0]);
			expect(names).toEqual(
				expect.arrayContaining([
					"copilot_read_file",
					"copilot_create_file",
					"copilot_list_files",
					"copilot_code_task",
				]),
			);
		});

		it("registers tools with descriptions and _depth in schema", () => {
			createServer();
			for (const call of mockToolFn.mock.calls as unknown[][]) {
				const description = call[1] as string;
				expect(typeof description).toBe("string");
				expect(description.length).toBeGreaterThan(0);

				const schema = call[2] as Record<string, unknown>;
				expect(schema).toHaveProperty("_depth");
			}
		});
	});

	// ── copilot_read_file ───────────────────────────────────────────────────

	describe("copilot_read_file", () => {
		it("calls workspace.readFile with the provided path", async () => {
			createServer();
			const callback = getToolCallback("copilot_read_file");

			const result = await callback({ path: "src/main.ts" });

			expect(mockSession.rpc["workspace.readFile"]).toHaveBeenCalledWith({
				path: "src/main.ts",
			});
			expect(result).toEqual({
				content: [{ type: "text", text: "file content here" }],
			});
		});

		it("handles non-string results by JSON-serializing them", async () => {
			mockSession.rpc["workspace.readFile"].mockResolvedValue({
				data: "binary",
			});
			createServer();
			const callback = getToolCallback("copilot_read_file");

			const result = await callback({ path: "data.bin" });

			expect(result.content[0].text).toContain('"data": "binary"');
		});

		it("returns isError when readFile throws", async () => {
			mockSession.rpc["workspace.readFile"].mockRejectedValue(new Error("File not found"));
			createServer();
			const callback = getToolCallback("copilot_read_file");

			const result = await callback({ path: "nonexistent.ts" });

			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Error reading file");
			expect(result.content[0].text).toContain("File not found");
		});
	});

	// ── copilot_create_file ─────────────────────────────────────────────────

	describe("copilot_create_file", () => {
		it("calls workspace.createFile with path and content", async () => {
			createServer();
			const callback = getToolCallback("copilot_create_file");

			const result = await callback({
				path: "src/new.ts",
				content: "export const x = 1;",
			});

			expect(mockSession.rpc["workspace.createFile"]).toHaveBeenCalledWith({
				path: "src/new.ts",
				content: "export const x = 1;",
			});
			expect(result).toEqual({
				content: [{ type: "text", text: "File created: src/new.ts" }],
			});
		});

		it("returns isError when createFile throws", async () => {
			mockSession.rpc["workspace.createFile"].mockRejectedValue(new Error("Permission denied"));
			createServer();
			const callback = getToolCallback("copilot_create_file");

			const result = await callback({
				path: "etc/secret",
				content: "data",
			});

			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Error creating file");
			expect(result.content[0].text).toContain("Permission denied");
		});
	});

	// ── copilot_list_files ──────────────────────────────────────────────────

	describe("copilot_list_files", () => {
		it("calls workspace.listFiles with the given directory", async () => {
			createServer();
			const callback = getToolCallback("copilot_list_files");

			const result = await callback({ directory: "src" });

			expect(mockSession.rpc["workspace.listFiles"]).toHaveBeenCalledWith({
				directory: "src",
			});
			expect(result.content[0].text).toContain("file1.ts");
			expect(result.content[0].text).toContain("file2.ts");
		});

		it("passes undefined directory for workspace root", async () => {
			createServer();
			const callback = getToolCallback("copilot_list_files");

			await callback({ directory: undefined });

			expect(mockSession.rpc["workspace.listFiles"]).toHaveBeenCalledWith({
				directory: undefined,
			});
		});

		it("returns isError when listFiles throws", async () => {
			mockSession.rpc["workspace.listFiles"].mockRejectedValue(new Error("Access denied"));
			createServer();
			const callback = getToolCallback("copilot_list_files");

			const result = await callback({ directory: "restricted" });

			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Error listing files");
			expect(result.content[0].text).toContain("Access denied");
		});
	});

	// ── copilot_code_task ───────────────────────────────────────────────────

	describe("copilot_code_task", () => {
		it("calls bridge.runTask and returns formatted result", async () => {
			createServer();
			const callback = getToolCallback("copilot_code_task");

			const result = await callback({
				prompt: "Fix the bug",
				workingDir: "/project",
				timeout: 60_000,
			});

			expect(mockBridge.runTask).toHaveBeenCalledWith({
				prompt: "Fix the bug",
				workingDir: "/project",
				timeout: 60_000,
			});
			expect(result.content[0].text).toContain("Generated code output");
			expect(result.content[0].text).toContain("Success: true");
			expect(result.content[0].text).toContain("2.0s");
			expect(result.content[0].text).toContain("sess-abc");
		});

		it("returns isError when runTask throws", async () => {
			mockBridge.runTask.mockRejectedValue(new Error("Copilot unavailable"));
			createServer();
			const callback = getToolCallback("copilot_code_task");

			const result = await callback({
				prompt: "Do something",
				timeout: 120_000,
			});

			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Copilot unavailable");
		});

		it("includes tool calls and errors in formatted output", async () => {
			mockBridge.runTask.mockResolvedValue({
				success: false,
				content: "Partial result",
				toolCalls: [
					{
						tool: "readFile",
						args: { path: "x.ts" },
						result: "ok",
						timestamp: 0,
					},
				],
				errors: ["Something went wrong"],
				sessionId: "sess-xyz",
				elapsed: 5000,
			});
			createServer();
			const callback = getToolCallback("copilot_code_task");

			const result = await callback({
				prompt: "Complex task",
				timeout: 120_000,
			});

			expect(result.content[0].text).toContain("readFile");
			expect(result.content[0].text).toContain("Something went wrong");
			expect(result.content[0].text).toContain("Success: false");
			expect(result.content[0].text).toContain("5.0s");
		});
	});

	// ── Lazy initialization ─────────────────────────────────────────────────

	describe("lazy initialization", () => {
		it("does not create bridge or session at import time", () => {
			// After _resetState in beforeEach, nothing should be constructed
			expect(CopilotBridge).not.toHaveBeenCalled();
			expect(CopilotClient).not.toHaveBeenCalled();
		});

		it("creates bridge and session on first tool call", async () => {
			createServer();
			const callback = getToolCallback("copilot_read_file");

			await callback({ path: "test.ts" });

			expect(CopilotBridge).toHaveBeenCalledTimes(1);
			expect(mockBridge.ensureReady).toHaveBeenCalledTimes(1);
			expect(CopilotClient).toHaveBeenCalledTimes(1);
			expect(mockSdkClient.createSession).toHaveBeenCalledTimes(1);
		});

		it("reuses bridge and session across subsequent calls", async () => {
			createServer();
			const readFile = getToolCallback("copilot_read_file");
			const createFile = getToolCallback("copilot_create_file");

			await readFile({ path: "a.ts" });
			await createFile({ path: "b.ts", content: "x" });

			// Only one construction — reused across calls
			expect(CopilotBridge).toHaveBeenCalledTimes(1);
			expect(CopilotClient).toHaveBeenCalledTimes(1);
			expect(mockSdkClient.createSession).toHaveBeenCalledTimes(1);
		});

		it("retries initialization after a failure", async () => {
			mockBridge.ensureReady
				.mockRejectedValueOnce(new Error("Connection refused"))
				.mockResolvedValue(undefined);

			createServer();
			const callback = getToolCallback("copilot_read_file");

			// First call fails during init
			const result1 = await callback({ path: "test.ts" });
			expect(result1.isError).toBe(true);
			expect(result1.content[0].text).toContain("Connection refused");

			// Second call retries init and succeeds
			const result2 = await callback({ path: "test.ts" });
			expect(result2.isError).toBeUndefined();
			expect(CopilotBridge).toHaveBeenCalledTimes(2);
		});

		it("retries initialization after createSession failure", async () => {
			mockSdkClient.createSession
				.mockRejectedValueOnce(new Error("Session creation failed"))
				.mockResolvedValue(mockSession);

			createServer();
			const callback = getToolCallback("copilot_read_file");

			const result1 = await callback({ path: "test.ts" });
			expect(result1.isError).toBe(true);
			expect(result1.content[0].text).toContain("Session creation failed");

			const result2 = await callback({ path: "test.ts" });
			expect(result2.isError).toBeUndefined();
			expect(result2.content[0].text).toBe("file content here");
		});

		it("cleans up bridge on createSession failure", async () => {
			mockSdkClient.createSession.mockRejectedValueOnce(new Error("createSession failed"));

			createServer();
			const callback = getToolCallback("copilot_read_file");

			const result = await callback({ path: "test.ts" });
			expect(result.isError).toBe(true);

			// Bridge.stop should have been called as cleanup
			expect(mockBridge.stop).toHaveBeenCalledTimes(1);
		});
	});

	// ── Mutex ───────────────────────────────────────────────────────────────

	describe("mutex", () => {
		it("serializes concurrent copilot_code_task calls", async () => {
			const executionOrder: number[] = [];
			let callCount = 0;

			mockBridge.runTask.mockImplementation(async () => {
				const myOrder = ++callCount;
				// Simulate async work
				await new Promise((resolve) => setTimeout(resolve, 50));
				executionOrder.push(myOrder);
				return {
					success: true,
					content: `Result ${myOrder}`,
					toolCalls: [],
					errors: [],
					sessionId: `sess-${myOrder}`,
					elapsed: 50,
				};
			});

			createServer();
			const callback = getToolCallback("copilot_code_task");

			// Launch two concurrent calls
			const p1 = callback({ prompt: "Task 1", timeout: 120_000 });
			const p2 = callback({ prompt: "Task 2", timeout: 120_000 });

			await Promise.all([p1, p2]);

			// Both executed, but sequentially (1 before 2)
			expect(executionOrder).toEqual([1, 2]);
		});

		it("releases mutex when copilot_code_task fails", async () => {
			mockBridge.runTask.mockRejectedValueOnce(new Error("first call fails")).mockResolvedValue({
				success: true,
				content: "recovered",
				toolCalls: [],
				errors: [],
				sessionId: "sess-recover",
				elapsed: 100,
			});
			createServer();
			const callback = getToolCallback("copilot_code_task");

			const result1 = await callback({ prompt: "Fail", timeout: 120_000 });
			expect(result1.isError).toBe(true);

			// Second call should proceed (mutex was released in finally block)
			const result2 = await callback({ prompt: "Succeed", timeout: 120_000 });
			expect(result2.isError).toBeUndefined();
			expect(result2.content[0].text).toContain("recovered");
		});

		it("acquireMutex returns a working release function", async () => {
			const order: string[] = [];

			const release1 = await acquireMutex();
			order.push("acquired-1");

			// Second acquire should not resolve until release1 is called
			let acquired2 = false;
			const p2 = acquireMutex().then((release2) => {
				acquired2 = true;
				order.push("acquired-2");
				release2();
			});

			// Give microtasks a chance to run
			await new Promise((resolve) => setTimeout(resolve, 10));
			expect(acquired2).toBe(false);

			release1();
			await p2;

			expect(acquired2).toBe(true);
			expect(order).toEqual(["acquired-1", "acquired-2"]);
		});
	});

	// ── Shutdown ────────────────────────────────────────────────────────────

	describe("shutdown", () => {
		it("destroys session, stops bridge, and stops SDK client in order", async () => {
			createServer();
			const callback = getToolCallback("copilot_read_file");
			await callback({ path: "test.ts" });

			await shutdown();

			expect(mockSession.destroy).toHaveBeenCalledTimes(1);
			expect(mockBridge.stop).toHaveBeenCalledTimes(1);
			expect(mockSdkClient.stop).toHaveBeenCalledTimes(1);

			// Verify order: session destroyed → bridge stopped → client stopped
			const destroyOrder = mockSession.destroy.mock.invocationCallOrder[0]!;
			const bridgeOrder = mockBridge.stop.mock.invocationCallOrder[0]!;
			const clientOrder = mockSdkClient.stop.mock.invocationCallOrder[0]!;
			expect(destroyOrder).toBeLessThan(bridgeOrder);
			expect(bridgeOrder).toBeLessThan(clientOrder);
		});

		it("handles errors during shutdown gracefully", async () => {
			mockSession.destroy.mockRejectedValue(new Error("destroy failed"));
			mockBridge.stop.mockRejectedValue(new Error("stop failed"));
			mockSdkClient.stop.mockRejectedValue(new Error("client stop failed"));

			// Initialize
			createServer();
			const callback = getToolCallback("copilot_read_file");
			await callback({ path: "test.ts" });

			// Should not throw despite all shutdown steps failing
			await expect(shutdown()).resolves.toBeUndefined();
		});

		it("is safe to call when not initialized", async () => {
			await expect(shutdown()).resolves.toBeUndefined();
		});

		it("clears state so next tool call re-initializes", async () => {
			createServer();
			const callback = getToolCallback("copilot_read_file");
			await callback({ path: "test.ts" });

			expect(CopilotBridge).toHaveBeenCalledTimes(1);

			await shutdown();

			// Making another call should trigger re-initialization
			await callback({ path: "test2.ts" });
			expect(CopilotBridge).toHaveBeenCalledTimes(2);
		});
	});

	// ── formatResult ────────────────────────────────────────────────────────

	describe("formatResult", () => {
		it("formats a successful result with no tool calls or errors", () => {
			const text = formatResult({
				success: true,
				content: "Hello world",
				toolCalls: [],
				errors: [],
				sessionId: "sess-1",
				elapsed: 1500,
			});

			expect(text).toContain("Hello world");
			expect(text).toContain("Success: true");
			expect(text).toContain("1.5s");
			expect(text).toContain("sess-1");
			expect(text).not.toContain("Tool Calls:");
			expect(text).not.toContain("Errors:");
		});

		it("includes tool calls section when present", () => {
			const text = formatResult({
				success: true,
				content: "Done",
				toolCalls: [
					{
						tool: "readFile",
						args: { path: "x.ts" },
						result: "content",
						timestamp: 0,
					},
				],
				errors: [],
				sessionId: "sess-2",
				elapsed: 2000,
			});

			expect(text).toContain("Tool Calls:");
			expect(text).toContain("readFile");
			expect(text).toContain("x.ts");
		});

		it("includes errors section when present", () => {
			const text = formatResult({
				success: false,
				content: "",
				toolCalls: [],
				errors: ["Something broke", "Another issue"],
				sessionId: "sess-3",
				elapsed: 500,
			});

			expect(text).toContain("Errors:");
			expect(text).toContain("Something broke");
			expect(text).toContain("Another issue");
			expect(text).toContain("Success: false");
		});
	});

	// ── Path validation ─────────────────────────────────────────────────────

	describe("path validation", () => {
		it("rejects absolute paths in copilot_read_file", async () => {
			createServer();
			const callback = getToolCallback("copilot_read_file");
			const result = await callback({ path: "/etc/passwd" });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Absolute paths are not allowed");
		});

		it("rejects path traversal in copilot_read_file", async () => {
			createServer();
			const callback = getToolCallback("copilot_read_file");
			const result = await callback({ path: "../../etc/passwd" });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Path traversal is not allowed");
		});

		it("rejects normalized traversal in copilot_read_file", async () => {
			createServer();
			const callback = getToolCallback("copilot_read_file");
			const result = await callback({ path: "foo/../../etc/passwd" });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Path traversal is not allowed");
		});

		it("rejects absolute paths in copilot_create_file", async () => {
			createServer();
			const callback = getToolCallback("copilot_create_file");
			const result = await callback({ path: "/tmp/evil.ts", content: "x" });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Absolute paths are not allowed");
		});

		it("rejects path traversal in copilot_create_file", async () => {
			createServer();
			const callback = getToolCallback("copilot_create_file");
			const result = await callback({ path: "../outside.ts", content: "x" });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Path traversal is not allowed");
		});

		it("rejects absolute paths in copilot_list_files", async () => {
			createServer();
			const callback = getToolCallback("copilot_list_files");
			const result = await callback({ directory: "/etc" });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Absolute paths are not allowed");
		});

		it("rejects path traversal in copilot_list_files", async () => {
			createServer();
			const callback = getToolCallback("copilot_list_files");
			const result = await callback({ directory: "../../etc" });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Path traversal is not allowed");
		});

		it("allows valid relative paths in copilot_read_file", async () => {
			createServer();
			const callback = getToolCallback("copilot_read_file");
			const result = await callback({ path: "src/main.ts" });
			expect(result.isError).toBeUndefined();
			expect(result.content[0].text).toBe("file content here");
		});

		it("rejects null bytes in paths", async () => {
			createServer();
			const callback = getToolCallback("copilot_read_file");
			const result = await callback({ path: "src/main.ts\0.jpg" });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Null bytes are not allowed");
		});
	});

	// ── Cycle detection ─────────────────────────────────────────────────────

	describe("cycle detection", () => {
		it("checkDepth returns error at depth >= MAX_CALL_DEPTH", () => {
			const result = checkDepth(3);
			expect(result).not.toBeNull();
			expect(result?.isError).toBe(true);
			expect(result?.content[0].text).toContain("Maximum call depth exceeded");
			expect(result?.content[0].text).toContain("cycle detected");
		});

		it("checkDepth returns null at depth < MAX_CALL_DEPTH", () => {
			expect(checkDepth(0)).toBeNull();
			expect(checkDepth(1)).toBeNull();
			expect(checkDepth(2)).toBeNull();
		});

		it("checkDepth defaults to 0 when undefined", () => {
			expect(checkDepth(undefined)).toBeNull();
		});

		it("MAX_CALL_DEPTH is 3", () => {
			expect(MAX_CALL_DEPTH).toBe(3);
		});

		it("rejects copilot_read_file at depth >= MAX_CALL_DEPTH", async () => {
			createServer();
			const callback = getToolCallback("copilot_read_file");

			const result = await callback({ path: "/test.ts", _depth: 3 });

			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Maximum call depth exceeded");
			// Should NOT have called ensureInitialized / session
			expect(mockSession.rpc["workspace.readFile"]).not.toHaveBeenCalled();
		});

		it("rejects copilot_code_task at depth >= MAX_CALL_DEPTH", async () => {
			createServer();
			const callback = getToolCallback("copilot_code_task");

			const result = await callback({
				prompt: "Do something",
				timeout: 120_000,
				_depth: 5,
			});

			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Maximum call depth exceeded");
			expect(mockBridge.runTask).not.toHaveBeenCalled();
		});

		it("allows calls at depth < MAX_CALL_DEPTH", async () => {
			createServer();
			const callback = getToolCallback("copilot_read_file");

			const result = await callback({ path: "test.ts", _depth: 2 });

			expect(result.isError).toBeUndefined();
			expect(result.content[0].text).toBe("file content here");
		});

		it("defaults _depth to 0 when not provided", async () => {
			createServer();
			const callback = getToolCallback("copilot_read_file");

			const result = await callback({ path: "test.ts" });

			expect(result.isError).toBeUndefined();
			expect(result.content[0].text).toBe("file content here");
		});
	});
});
