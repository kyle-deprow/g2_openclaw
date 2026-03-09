import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// ─── Hoisted mocks ──────────────────────────────────────────────────────────

const { mockBridge, mockToolFn } = vi.hoisted(() => {
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
	return { mockBridge, mockToolFn };
});

vi.mock("@github/copilot-sdk", () => ({
	CopilotClient: vi.fn().mockImplementation(function () {
		return {
			ping: vi.fn().mockResolvedValue({ message: "ok" }),
			getAuthStatus: vi.fn().mockResolvedValue({ isAuthenticated: true, authType: "user" }),
			stop: vi.fn().mockResolvedValue([]),
			forceStop: vi.fn(),
		};
	}),
}));

vi.mock("@modelcontextprotocol/sdk/server/mcp.js", () => ({
	McpServer: vi.fn().mockImplementation(function () {
		return {
			tool: mockToolFn,
			connect: vi.fn().mockResolvedValue(undefined),
		};
	}),
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
	CopilotBridge: vi.fn().mockImplementation(function () { return mockBridge; }),
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
		it("registers exactly 2 tools", () => {
			const toolNames = mockToolFn.mock.calls.map((c: unknown[]) => c[0]);
			expect(toolNames).toEqual([
				"copilot",
				"copilot_sessions",
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
				sessionId: "/resolved/myproject",
				systemMessage: undefined,
			});
		});

		it("passes persona as systemMessage, not in prompt", async () => {
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
					prompt: "do the thing",
					systemMessage: "You are an expert",
				}),
			);
		});

		it("systemMessage is undefined when no persona provided", async () => {
			mockBridge.runTask.mockResolvedValue(taskResult);
			const cb = getToolCallback("copilot");
			await cb({ prompt: "just do it", workingDir: "proj", timeout: 120000 });
			const call = mockBridge.runTask.mock.calls[0][0];
			expect(call.prompt).toBe("just do it");
			expect(call.systemMessage).toBeUndefined();
		});

		it("forwards custom timeout", async () => {
			mockBridge.runTask.mockResolvedValue(taskResult);
			const cb = getToolCallback("copilot");
			await cb({ prompt: "task", workingDir: "proj", timeout: 300000 });
			expect(mockBridge.runTask).toHaveBeenCalledWith(
				expect.objectContaining({ timeout: 300000 }),
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

		it("passes resolvedDir as sessionId — same dir = same session key", async () => {
			mockBridge.runTask.mockResolvedValue(taskResult);
			const cb = getToolCallback("copilot");
			await cb({ prompt: "first", workingDir: "proj", timeout: 120000 });
			await cb({ prompt: "second", workingDir: "proj", timeout: 120000 });
			const calls = mockBridge.runTask.mock.calls;
			expect(calls).toHaveLength(2);
			const id1 = calls[0][0].sessionId;
			const id2 = calls[1][0].sessionId;
			expect(id1).toBe("/resolved/proj");
			expect(id1).toBe(id2);
		});

		it("different workingDirs produce different sessionIds", async () => {
			mockBridge.runTask.mockResolvedValue(taskResult);
			const cb = getToolCallback("copilot");
			await cb({ prompt: "first", workingDir: "proj-a", timeout: 120000 });
			await cb({ prompt: "second", workingDir: "proj-b", timeout: 120000 });
			const calls = mockBridge.runTask.mock.calls;
			expect(calls[0][0].sessionId).toBe("/resolved/proj-a");
			expect(calls[1][0].sessionId).toBe("/resolved/proj-b");
		});
	});

	// ── copilot_sessions tool ──────────────────────────────────────────────────

	describe("copilot_sessions", () => {
		it("list with no sessions returns empty message", async () => {
			mockBridge.listSessions.mockReturnValue([]);
			const cb = getToolCallback("copilot_sessions");
			const result = await cb({ action: "list" });
			expect(result.content[0].text).toBe("No active Copilot sessions.");
			expect(result.isError).toBeUndefined();
		});

		it("list formats sessions correctly", async () => {
			mockBridge.listSessions.mockReturnValue([
				{ sessionId: "/home/test/repos/proj-a", workingDir: "/home/test/repos/proj-a", createdAt: "2026-03-07T00:00:00.000Z", lastAccessedAt: "2026-03-07T00:01:00.000Z", messageCount: 3 },
				{ sessionId: "/home/test/repos/proj-b", workingDir: "/home/test/repos/proj-b", createdAt: "2026-03-07T00:02:00.000Z", lastAccessedAt: "2026-03-07T00:03:00.000Z", messageCount: 1 },
			]);
			const cb = getToolCallback("copilot_sessions");
			const result = await cb({ action: "list" });
			expect(result.content[0].text).toContain("Active sessions:");
			expect(result.content[0].text).toContain("/home/test/repos/proj-a");
			expect(result.content[0].text).toContain("messages: 3");
			expect(result.content[0].text).toContain("/home/test/repos/proj-b");
			expect(result.content[0].text).toContain("messages: 1");
		});

		it("destroy without project returns error", async () => {
			const cb = getToolCallback("copilot_sessions");
			const result = await cb({ action: "destroy" });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("'project' is required");
		});

		it("destroy with project calls bridge.destroySession with resolved path", async () => {
			mockBridge.destroySession.mockResolvedValue(true);
			const cb = getToolCallback("copilot_sessions");
			const result = await cb({ action: "destroy", project: "my-app" });
			expect(mockBridge.resolveWorkingDir).toHaveBeenCalledWith("my-app");
			expect(mockBridge.destroySession).toHaveBeenCalledWith("/resolved/my-app");
			expect(result.content[0].text).toContain("destroyed");
			expect(result.content[0].text).toContain("my-app");
		});

		it("destroy returns 'no active session' when session doesn't exist", async () => {
			mockBridge.destroySession.mockResolvedValue(false);
			const cb = getToolCallback("copilot_sessions");
			const result = await cb({ action: "destroy", project: "nonexistent" });
			expect(result.content[0].text).toContain("No active session");
		});

		it("returns cycle detection error", async () => {
			const cb = getToolCallback("copilot_sessions");
			const result = await cb({ action: "list", _depth: 3 });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Maximum call depth exceeded");
		});

		it("returns error on bridge failure", async () => {
			mockBridge.listSessions.mockImplementation(() => { throw new Error("Bridge down"); });
			const cb = getToolCallback("copilot_sessions");
			const result = await cb({ action: "list" });
			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Bridge down");
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
