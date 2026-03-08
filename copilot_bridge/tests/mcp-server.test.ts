import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// ─── Hoisted mocks ──────────────────────────────────────────────────────────

const { mockBridge, mockSession, mockRpc, mockToolFn } = vi.hoisted(() => {
	const mockRpc: Record<string, ReturnType<typeof vi.fn>> = {
		"workspace.readFile": vi.fn(),
		"workspace.createFile": vi.fn(),
		"workspace.listFiles": vi.fn(),
	};
	const mockSession = {
		sendAndWait: vi.fn(),
		on: vi.fn().mockReturnValue(vi.fn()),
		destroy: vi.fn().mockResolvedValue(undefined),
		rpc: mockRpc,
	};
	const mockBridge = {
		ensureReady: vi.fn().mockResolvedValue(undefined),
		runTask: vi.fn(),
		stop: vi.fn().mockResolvedValue(undefined),
		isReady: vi.fn().mockResolvedValue(true),
		getStatus: vi.fn().mockResolvedValue({ connected: true }),
		resolveWorkingDir: vi.fn().mockImplementation(async (d: string) => `/resolved/${d}`),
		listSessions: vi.fn().mockReturnValue([]),
		destroySession: vi.fn().mockResolvedValue(true),
		destroyAllSessions: vi.fn().mockResolvedValue(0),
	};
	const mockToolFn = vi.fn();
	return { mockBridge, mockSession, mockRpc, mockToolFn };
});

vi.mock("@github/copilot-sdk", () => ({
	CopilotClient: vi.fn().mockImplementation(() => ({
		ping: vi.fn().mockResolvedValue({ message: "ok" }),
		getAuthStatus: vi.fn().mockResolvedValue({ isAuthenticated: true, authType: "user" }),
		stop: vi.fn().mockResolvedValue([]),
		forceStop: vi.fn(),
		createSession: vi.fn().mockResolvedValue(mockSession),
	})),
}));

vi.mock("@modelcontextprotocol/sdk/server/mcp.js", () => ({
	McpServer: vi.fn().mockImplementation(() => ({
		tool: mockToolFn,
		connect: vi.fn().mockResolvedValue(undefined),
	})),
}));

vi.mock("@modelcontextprotocol/sdk/server/stdio.js", () => ({
	StdioServerTransport: vi.fn(),
}));

vi.mock("../src/config.js", () => ({
	loadConfig: vi.fn().mockReturnValue({
		githubToken: "ghu_test",
		logLevel: "warning",
		openclawHost: "127.0.0.1",
		openclawPort: 18789,
		projectsRoot: "/home/test/repos",
	}),
}));

vi.mock("../src/hooks.js", () => ({
	DEFAULT_POLICY: { allowedTools: [], blockedTools: [], askTools: [], blockedPatterns: [] },
	createHooks: vi.fn().mockReturnValue({}),
}));

vi.mock("../src/client.js", () => ({
	CopilotBridge: vi.fn().mockImplementation(() => mockBridge),
}));

// ─── Import module under test ───────────────────────────────────────────────

const {
	createServer,
	ensureInitialized,
	shutdown,
	_resetState,
	_resetMutex,
	checkDepth,
	MAX_CALL_DEPTH,
	acquireMutex,
	formatResult,
} = await import("../src/mcp-server.js");

// ─── Helpers ────────────────────────────────────────────────────────────────

function getToolCallback(toolName: string) {
	const call = mockToolFn.mock.calls.find((c: unknown[]) => c[0] === toolName);
	if (!call) throw new Error(`Tool "${toolName}" not registered`);
	// server.tool(name, description, schema, callback) — callback is at index 3
	return call[3] as Function;
}

function resetMockDefaults() {
	mockBridge.ensureReady.mockResolvedValue(undefined);
	mockBridge.runTask.mockReset();
	mockBridge.stop.mockResolvedValue(undefined);
	mockBridge.isReady.mockResolvedValue(true);
	mockBridge.getStatus.mockResolvedValue({ connected: true });
	mockBridge.resolveWorkingDir.mockImplementation(async (d: string) => `/resolved/${d}`);
	mockBridge.listSessions.mockReturnValue([]);
	mockBridge.destroySession.mockResolvedValue(true);
	mockBridge.destroyAllSessions.mockResolvedValue(0);
	mockSession.destroy.mockResolvedValue(undefined);
	mockSession.on.mockReturnValue(vi.fn());
	mockRpc["workspace.readFile"].mockReset();
	mockRpc["workspace.createFile"].mockReset();
	mockRpc["workspace.listFiles"].mockReset();
}

// ─── Tests ──────────────────────────────────────────────────────────────────

describe("MCP Server", () => {
	beforeEach(() => {
		_resetState();
		_resetMutex();
		vi.clearAllMocks();
		resetMockDefaults();
		createServer();
	});

	afterEach(async () => {
		await shutdown();
	});

	// ── Server creation ─────────────────────────────────────────────────────

	describe("createServer", () => {
		it("registers exactly 6 tools", () => {
			const toolNames = mockToolFn.mock.calls.map((c: unknown[]) => c[0]);
			expect(toolNames).toEqual([
				"copilot_read_file",
				"copilot_create_file",
				"copilot_list_files",
				"copilot",
				"copilot_sessions",
				"copilot_session_destroy",
			]);
		});

		it("returns an McpServer instance", () => {
			// createServer was already called in beforeEach; calling again is fine
			_resetState();
			vi.clearAllMocks();
			const server = createServer();
			expect(server).toBeDefined();
			expect(server.tool).toBe(mockToolFn);
		});
	});

	// ── copilot_read_file ───────────────────────────────────────────────────

	describe("copilot_read_file", () => {
		it("reads file content (happy path)", async () => {
			mockRpc["workspace.readFile"].mockResolvedValue("file contents here");
			const cb = getToolCallback("copilot_read_file");
			const result = await cb({ path: "src/index.ts" });
			expect(result).toEqual({
				content: [{ type: "text", text: "file contents here" }],
			});
			expect(mockRpc["workspace.readFile"]).toHaveBeenCalledWith({ path: "src/index.ts" });
		});

		it("JSON-stringifies non-string RPC results", async () => {
			mockRpc["workspace.readFile"].mockResolvedValue({ lines: ["a", "b"] });
			const cb = getToolCallback("copilot_read_file");
			const result = await cb({ path: "data.json" });
			expect(result.content[0].text).toBe(JSON.stringify({ lines: ["a", "b"] }, null, 2));
		});

		it("rejects absolute paths", async () => {
			const cb = getToolCallback("copilot_read_file");
			const result = await cb({ path: "/etc/passwd" });
			expect(result).toEqual({
				content: [{ type: "text", text: "Error: Absolute paths are not allowed" }],
				isError: true,
			});
			expect(mockRpc["workspace.readFile"]).not.toHaveBeenCalled();
		});

		it("rejects path traversal", async () => {
			const cb = getToolCallback("copilot_read_file");
			const result = await cb({ path: "../secret.txt" });
			expect(result).toEqual({
				content: [{ type: "text", text: "Error: Path traversal is not allowed" }],
				isError: true,
			});
		});

		it("rejects null bytes", async () => {
			const cb = getToolCallback("copilot_read_file");
			const result = await cb({ path: "file\0.txt" });
			expect(result).toEqual({
				content: [{ type: "text", text: "Error: Null bytes are not allowed" }],
				isError: true,
			});
		});

		it("returns error on RPC failure", async () => {
			mockRpc["workspace.readFile"].mockRejectedValue(new Error("File not found"));
			const cb = getToolCallback("copilot_read_file");
			const result = await cb({ path: "missing.ts" });
			expect(result).toEqual({
				content: [{ type: "text", text: "Error reading file: File not found" }],
				isError: true,
			});
		});

		it("returns cycle detection error when _depth >= MAX_CALL_DEPTH", async () => {
			const cb = getToolCallback("copilot_read_file");
			const result = await cb({ path: "file.ts", _depth: 3 });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Maximum call depth exceeded");
			expect(mockRpc["workspace.readFile"]).not.toHaveBeenCalled();
		});
	});

	// ── copilot_create_file ─────────────────────────────────────────────────

	describe("copilot_create_file", () => {
		it("creates a file (happy path)", async () => {
			mockRpc["workspace.createFile"].mockResolvedValue(undefined);
			const cb = getToolCallback("copilot_create_file");
			const result = await cb({ path: "src/new.ts", content: "export {}" });
			expect(result).toEqual({
				content: [{ type: "text", text: "File created: src/new.ts" }],
			});
			expect(mockRpc["workspace.createFile"]).toHaveBeenCalledWith({
				path: "src/new.ts",
				content: "export {}",
			});
		});

		it("rejects absolute path", async () => {
			const cb = getToolCallback("copilot_create_file");
			const result = await cb({ path: "/tmp/evil.sh", content: "rm -rf /" });
			expect(result).toEqual({
				content: [{ type: "text", text: "Error: Absolute paths are not allowed" }],
				isError: true,
			});
			expect(mockRpc["workspace.createFile"]).not.toHaveBeenCalled();
		});

		it("rejects path traversal", async () => {
			const cb = getToolCallback("copilot_create_file");
			const result = await cb({ path: "../../escape.txt", content: "bad" });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Path traversal is not allowed");
		});

		it("returns error on RPC failure", async () => {
			mockRpc["workspace.createFile"].mockRejectedValue(new Error("Permission denied"));
			const cb = getToolCallback("copilot_create_file");
			const result = await cb({ path: "src/new.ts", content: "data" });
			expect(result).toEqual({
				content: [{ type: "text", text: "Error creating file: Permission denied" }],
				isError: true,
			});
		});

		it("returns cycle detection error when _depth >= MAX_CALL_DEPTH", async () => {
			const cb = getToolCallback("copilot_create_file");
			const result = await cb({ path: "file.ts", content: "x", _depth: 5 });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Maximum call depth exceeded");
		});
	});

	// ── copilot_list_files ──────────────────────────────────────────────────

	describe("copilot_list_files", () => {
		it("lists files at workspace root when no directory given", async () => {
			mockRpc["workspace.listFiles"].mockResolvedValue(["a.ts", "b.ts"]);
			const cb = getToolCallback("copilot_list_files");
			const result = await cb({});
			expect(result.content[0].text).toBe(JSON.stringify(["a.ts", "b.ts"], null, 2));
			expect(mockRpc["workspace.listFiles"]).toHaveBeenCalledWith({ directory: undefined });
		});

		it("lists files in a specific directory", async () => {
			mockRpc["workspace.listFiles"].mockResolvedValue("file1.ts\nfile2.ts");
			const cb = getToolCallback("copilot_list_files");
			const result = await cb({ directory: "src" });
			expect(result).toEqual({
				content: [{ type: "text", text: "file1.ts\nfile2.ts" }],
			});
			expect(mockRpc["workspace.listFiles"]).toHaveBeenCalledWith({ directory: "src" });
		});

		it("rejects absolute directory path", async () => {
			const cb = getToolCallback("copilot_list_files");
			const result = await cb({ directory: "/etc" });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Absolute paths are not allowed");
		});

		it("returns error on RPC failure", async () => {
			mockRpc["workspace.listFiles"].mockRejectedValue(new Error("Access denied"));
			const cb = getToolCallback("copilot_list_files");
			const result = await cb({});
			expect(result).toEqual({
				content: [{ type: "text", text: "Error listing files: Access denied" }],
				isError: true,
			});
		});

		it("returns cycle detection error when _depth >= MAX_CALL_DEPTH", async () => {
			const cb = getToolCallback("copilot_list_files");
			const result = await cb({ _depth: 3 });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Maximum call depth exceeded");
		});
	});

	// ── copilot tool ────────────────────────────────────────────────────────

	describe("copilot", () => {
		const taskResult = {
			success: true,
			content: "Done",
			toolCalls: [],
			errors: [],
			sessionId: "sess-1",
			elapsed: 2000,
		};

		it("executes a coding task (happy path)", async () => {
			mockBridge.runTask.mockResolvedValue(taskResult);
			const cb = getToolCallback("copilot");
			const result = await cb({ prompt: "fix the bug", workingDir: "myproject", timeout: 120000 });
			expect(result.content[0].text).toContain("Done");
			expect(result.content[0].text).toContain("Success: true");
			expect(mockBridge.resolveWorkingDir).toHaveBeenCalledWith("myproject");
			expect(mockBridge.runTask).toHaveBeenCalledWith({
				prompt: "fix the bug",
				workingDir: "/resolved/myproject",
				timeout: 120000,
			});
		});

		it("prepends persona with separator", async () => {
			mockBridge.runTask.mockResolvedValue(taskResult);
			const cb = getToolCallback("copilot");
			await cb({
				prompt: "do the thing",
				persona: "You are an expert",
				workingDir: "proj",
				timeout: 60000,
			});
			expect(mockBridge.runTask).toHaveBeenCalledWith(
				expect.objectContaining({
					prompt: "You are an expert\n\n---\n\ndo the thing",
				}),
			);
		});

		it("uses prompt only when no persona provided", async () => {
			mockBridge.runTask.mockResolvedValue(taskResult);
			const cb = getToolCallback("copilot");
			await cb({ prompt: "just do it", workingDir: "proj", timeout: 120000 });
			expect(mockBridge.runTask).toHaveBeenCalledWith(
				expect.objectContaining({
					prompt: "just do it",
				}),
			);
		});

		it("forwards custom timeout", async () => {
			mockBridge.runTask.mockResolvedValue(taskResult);
			const cb = getToolCallback("copilot");
			await cb({ prompt: "task", workingDir: "proj", timeout: 300000 });
			expect(mockBridge.runTask).toHaveBeenCalledWith(
				expect.objectContaining({ timeout: 300000 }),
			);
		});

		it("passes sessionId to bridge.runTask", async () => {
			mockBridge.runTask.mockResolvedValue(taskResult);
			const cb = getToolCallback("copilot");
			await cb({ prompt: "continue", workingDir: "proj", timeout: 120000, sessionId: "sess-abc" });
			expect(mockBridge.runTask).toHaveBeenCalledWith(
				expect.objectContaining({ sessionId: "sess-abc" }),
			);
		});

		it("sessionId is undefined when omitted", async () => {
			mockBridge.runTask.mockResolvedValue(taskResult);
			const cb = getToolCallback("copilot");
			await cb({ prompt: "new task", workingDir: "proj", timeout: 120000 });
			expect(mockBridge.runTask).toHaveBeenCalledWith(
				expect.objectContaining({ sessionId: undefined }),
			);
		});

		it("returns cycle detection error", async () => {
			const cb = getToolCallback("copilot");
			const result = await cb({ prompt: "task", workingDir: "proj", timeout: 120000, _depth: 3 });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Maximum call depth exceeded");
			expect(mockBridge.runTask).not.toHaveBeenCalled();
		});

		it("returns error on bridge failure", async () => {
			mockBridge.runTask.mockRejectedValue(new Error("Bridge timeout"));
			const cb = getToolCallback("copilot");
			const result = await cb({ prompt: "task", workingDir: "proj", timeout: 120000 });
			expect(result).toEqual({
				content: [{ type: "text", text: "Error: Bridge timeout" }],
				isError: true,
			});
		});

		it("handles non-Error thrown values", async () => {
			mockBridge.runTask.mockRejectedValue("string error");
			const cb = getToolCallback("copilot");
			const result = await cb({ prompt: "task", workingDir: "proj", timeout: 120000 });
			expect(result.content[0].text).toBe("Error: string error");
			expect(result.isError).toBe(true);
		});
	});
	// ── copilot_sessions ───────────────────────────────────────────────────

	describe("copilot_sessions", () => {
		it("returns 'No active sessions.' when empty", async () => {
			mockBridge.listSessions.mockReturnValue([]);
			const cb = getToolCallback("copilot_sessions");
			const result = await cb({});
			expect(result.content[0].text).toBe("No active sessions.");
		});

		it("returns markdown table when sessions exist", async () => {
			mockBridge.listSessions.mockReturnValue([
				{ sessionId: "s1", workingDir: "/proj", createdAt: "2026-03-07T00:00:00Z", messageCount: 2 },
			]);
			const cb = getToolCallback("copilot_sessions");
			const result = await cb({});
			expect(result.content[0].text).toContain("| Session ID |");
			expect(result.content[0].text).toContain("s1");
			expect(result.content[0].text).toContain("/proj");
		});

		it("returns cycle detection error when _depth >= MAX_CALL_DEPTH", async () => {
			const cb = getToolCallback("copilot_sessions");
			const result = await cb({ _depth: 3 });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Maximum call depth exceeded");
		});
	});

	// ── copilot_session_destroy ───────────────────────────────────────────

	describe("copilot_session_destroy", () => {
		it("destroys session and returns confirmation", async () => {
			mockBridge.destroySession.mockResolvedValue(true);
			const cb = getToolCallback("copilot_session_destroy");
			const result = await cb({ sessionId: "dead-sess" });
			expect(result.content[0].text).toContain("dead-sess");
			expect(result.content[0].text).toContain("destroyed");
			expect(mockBridge.destroySession).toHaveBeenCalledWith("dead-sess");
		});

		it("returns 'not found' for unknown session", async () => {
			mockBridge.destroySession.mockResolvedValue(false);
			const cb = getToolCallback("copilot_session_destroy");
			const result = await cb({ sessionId: "unknown" });
			expect(result.content[0].text).toContain("not found");
		});

		it("returns cycle detection error when _depth >= MAX_CALL_DEPTH", async () => {
			const cb = getToolCallback("copilot_session_destroy");
			const result = await cb({ sessionId: "x", _depth: 3 });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Maximum call depth exceeded");
		});

		it("returns error on bridge failure", async () => {
			mockBridge.destroySession.mockRejectedValue(new Error("destroy failed"));
			const cb = getToolCallback("copilot_session_destroy");
			const result = await cb({ sessionId: "sess-x" });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("destroy failed");
		});
	});
	// ── checkDepth ──────────────────────────────────────────────────────────

	describe("checkDepth", () => {
		it("returns null when depth is undefined", () => {
			expect(checkDepth(undefined)).toBeNull();
		});

		it("returns null when depth is 0", () => {
			expect(checkDepth(0)).toBeNull();
		});

		it("returns null when depth < MAX_CALL_DEPTH", () => {
			expect(checkDepth(MAX_CALL_DEPTH - 1)).toBeNull();
		});

		it("returns error when depth equals MAX_CALL_DEPTH", () => {
			const result = checkDepth(MAX_CALL_DEPTH);
			expect(result).not.toBeNull();
			expect(result!.isError).toBe(true);
			expect(result!.content[0].text).toContain("Maximum call depth exceeded");
			expect(result!.content[0].text).toContain(`Depth: ${MAX_CALL_DEPTH}`);
		});

		it("returns error when depth exceeds MAX_CALL_DEPTH", () => {
			const result = checkDepth(MAX_CALL_DEPTH + 5);
			expect(result).not.toBeNull();
			expect(result!.isError).toBe(true);
		});
	});

	// ── formatResult ────────────────────────────────────────────────────────

	describe("formatResult", () => {
		it("formats a full result with content, tool calls, errors, and stats", () => {
			const result = formatResult({
				success: true,
				content: "All good",
				toolCalls: [
					{ tool: "read", args: { path: "a.ts" }, result: "ok", timestamp: 0 },
					{ tool: "write", args: { path: "b.ts" }, result: "done", timestamp: 1 },
				],
				errors: ["warning: lint issue"],
				sessionId: "sess-123",
				elapsed: 1500,
			});
			expect(result).toContain("All good");
			expect(result).toContain("Tool Calls:");
			expect(result).toContain('- read({"path":"a.ts"}) → ok');
			expect(result).toContain('- write({"path":"b.ts"}) → done');
			expect(result).toContain("Errors:");
			expect(result).toContain("- warning: lint issue");
			expect(result).toContain("Success: true | Elapsed: 1.5s | Session: sess-123");
		});

		it("omits tool calls section when empty", () => {
			const result = formatResult({
				success: true,
				content: "Done",
				toolCalls: [],
				errors: [],
				sessionId: "sess-1",
				elapsed: 500,
			});
			expect(result).not.toContain("Tool Calls:");
			expect(result).toContain("Done");
			expect(result).toContain("Success: true | Elapsed: 0.5s | Session: sess-1");
		});

		it("omits errors section when empty", () => {
			const result = formatResult({
				success: false,
				content: "Partial",
				toolCalls: [{ tool: "t", args: {}, result: "r", timestamp: 0 }],
				errors: [],
				sessionId: "s-2",
				elapsed: 3000,
			});
			expect(result).not.toContain("Errors:");
			expect(result).toContain("Tool Calls:");
			expect(result).toContain("Success: false | Elapsed: 3.0s | Session: s-2");
		});

		it("includes both sections when both have entries", () => {
			const result = formatResult({
				success: true,
				content: "output",
				toolCalls: [{ tool: "x", args: {}, result: "y", timestamp: 0 }],
				errors: ["e1"],
				sessionId: "s",
				elapsed: 100,
			});
			expect(result).toContain("Tool Calls:");
			expect(result).toContain("Errors:");
		});
	});

	// ── mutex ───────────────────────────────────────────────────────────────

	describe("acquireMutex", () => {
		it("serializes concurrent calls", async () => {
			const order: number[] = [];

			const run = async (id: number, delayMs: number) => {
				const release = await acquireMutex();
				try {
					order.push(id);
					await new Promise((resolve) => setTimeout(resolve, delayMs));
				} finally {
					release();
				}
			};

			// Start two concurrent tasks — the second should wait for the first
			const p1 = run(1, 50);
			const p2 = run(2, 10);

			await Promise.all([p1, p2]);

			expect(order).toEqual([1, 2]);
		});

		it("releases mutex even when task throws", async () => {
			const release = await acquireMutex();
			// Simulate a task that errors but still releases
			await expect(
				(async () => {
					try {
						throw new Error("boom");
					} finally {
						release();
					}
				})(),
			).rejects.toThrow("boom");

			// Should be able to acquire again after error
			const release2 = await acquireMutex();
			release2();
		});
	});

	// ── MAX_CALL_DEPTH constant ─────────────────────────────────────────────

	describe("MAX_CALL_DEPTH", () => {
		it("is 3", () => {
			expect(MAX_CALL_DEPTH).toBe(3);
		});
	});
});
