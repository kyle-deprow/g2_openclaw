import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BridgeConfig } from "../src/config.js";
import type { CodingTaskRequest } from "../src/types.js";

// --- SDK Mock (vi.hoisted ensures these exist before vi.mock factory runs) ---

const { mockClient, mockSession, mockFs } = vi.hoisted(() => {
	const mockSession = {
		sendAndWait: vi.fn(),
		on: vi.fn().mockReturnValue(vi.fn()), // returns unsubscribe fn
		destroy: vi.fn().mockResolvedValue(undefined),
		rpc: {},
		getMessages: vi.fn().mockResolvedValue([]),
	};

	const mockClient = {
		ping: vi.fn(),
		getAuthStatus: vi.fn(),
		stop: vi.fn(),
		forceStop: vi.fn(),
		createSession: vi.fn().mockResolvedValue(mockSession),
	};

	const mockFs = {
		readFile: vi.fn(),
		writeFile: vi.fn().mockResolvedValue(undefined),
		mkdir: vi.fn().mockResolvedValue(undefined),
		realpath: vi.fn().mockImplementation(async (p: string) => p),
		appendFile: vi.fn().mockResolvedValue(undefined),
	};

	return { mockClient, mockSession, mockFs };
});

vi.mock("@github/copilot-sdk", () => ({
	CopilotClient: vi.fn().mockImplementation(() => mockClient),
}));

vi.mock("node:fs/promises", () => ({
	default: mockFs,
	...mockFs,
}));

const { CopilotClient: MockedCopilotClient } = await import("@github/copilot-sdk");
const { CopilotBridge } = await import("../src/client.js");

// --- Helpers ---

function makeConfig(overrides: Partial<BridgeConfig> = {}): BridgeConfig {
	return {
		logLevel: "warning",
		openclawHost: "127.0.0.1",
		openclawPort: 18789,
		projectsRoot: "/home/test/repos",
		...overrides,
	};
}

function makeRequest(overrides: Partial<CodingTaskRequest> = {}): CodingTaskRequest {
	return {
		prompt: "Hello, world!",
		...overrides,
	};
}

// --- Tests ---

describe("CopilotBridge", () => {
	let bridge: CopilotBridge;

	beforeEach(() => {
		vi.clearAllMocks();

		// Re-establish mock implementations after clearAllMocks
		mockSession.sendAndWait.mockResolvedValue({ data: { content: "response text" } });
		mockSession.on.mockReturnValue(vi.fn());
		mockSession.destroy.mockResolvedValue(undefined);
		mockSession.getMessages.mockResolvedValue([]);

		mockClient.ping.mockResolvedValue({
			message: "health",
			timestamp: Date.now(),
			protocolVersion: "1.0",
		});
		mockClient.getAuthStatus.mockResolvedValue({ isAuthenticated: true, authType: "user" });
		mockClient.stop.mockResolvedValue([]);
		mockClient.forceStop.mockResolvedValue(undefined);
		mockClient.createSession.mockResolvedValue(mockSession);

		bridge = new CopilotBridge(makeConfig());
	});

	describe("resolveWorkingDir", () => {
		it("resolves bare project name against projectsRoot", async () => {
			const config = makeConfig({ projectsRoot: "/home/user/repos" });
			bridge = new CopilotBridge(config);
			const resolved = await bridge.resolveWorkingDir("my-api");
			expect(resolved).toBe("/home/user/repos/my-api");
			expect(mockFs.mkdir).toHaveBeenCalledWith("/home/user/repos/my-api", { recursive: true });
		});

		it("uses absolute path as-is", async () => {
			const config = makeConfig({ projectsRoot: "/home/user/repos" });
			bridge = new CopilotBridge(config);
			const resolved = await bridge.resolveWorkingDir("/opt/projects/special");
			expect(resolved).toBe("/opt/projects/special");
			expect(mockFs.mkdir).toHaveBeenCalledWith("/opt/projects/special", { recursive: true });
		});

		it("resolves nested relative path against projectsRoot", async () => {
			const config = makeConfig({ projectsRoot: "/home/user/repos" });
			bridge = new CopilotBridge(config);
			const resolved = await bridge.resolveWorkingDir("org/my-api");
			expect(resolved).toBe("/home/user/repos/org/my-api");
			expect(mockFs.mkdir).toHaveBeenCalledWith("/home/user/repos/org/my-api", { recursive: true });
		});

		it("creates directory if it doesn't exist", async () => {
			const config = makeConfig({ projectsRoot: "/home/user/repos" });
			bridge = new CopilotBridge(config);
			await bridge.resolveWorkingDir("new-project");
			expect(mockFs.mkdir).toHaveBeenCalledWith("/home/user/repos/new-project", { recursive: true });
		});

		it("uses default projectsRoot from config if not overridden", async () => {
			const config = makeConfig({ projectsRoot: "/default/repos" });
			bridge = new CopilotBridge(config);
			const resolved = await bridge.resolveWorkingDir("test-project");
			expect(resolved).toBe("/default/repos/test-project");
		});
	});

	describe("constructor", () => {
		it("creates CopilotClient without BYOK provider fields", () => {
			const config = makeConfig({
				githubToken: "ghp_test",
				byokProvider: "openai",
				byokApiKey: "sk-test",
				byokBaseUrl: "https://api.openai.com",
				byokModel: "gpt-4o",
				cliPath: "/usr/bin/gh",
			});

			new CopilotBridge(config);

			expect(MockedCopilotClient).toHaveBeenCalledWith({
				githubToken: "ghp_test",
				cliPath: "/usr/bin/gh",
				logLevel: "warning",
				autoRestart: true,
			});

			// Verify BYOK fields are NOT in the constructor call
			const ctorArgs = vi.mocked(MockedCopilotClient).mock.calls[1]?.[0] as Record<string, unknown>;
			expect(ctorArgs).not.toHaveProperty("provider");
			expect(ctorArgs).not.toHaveProperty("byokProvider");
			expect(ctorArgs).not.toHaveProperty("byokApiKey");
			expect(ctorArgs).not.toHaveProperty("byokBaseUrl");
			expect(ctorArgs).not.toHaveProperty("byokModel");
		});
	});

	describe("ensureReady()", () => {
		it("calls ping() then getAuthStatus()", async () => {
			await bridge.ensureReady();

			expect(mockClient.ping).toHaveBeenCalledWith("health");
			expect(mockClient.getAuthStatus).toHaveBeenCalled();

			// ping called before getAuthStatus
			const pingOrder = mockClient.ping.mock.invocationCallOrder[0]!;
			const authOrder = mockClient.getAuthStatus.mock.invocationCallOrder[0]!;
			expect(pingOrder).toBeLessThan(authOrder);
		});

		it("throws BridgeError if ping fails", async () => {
			mockClient.ping.mockRejectedValue(new Error("connection refused"));

			await expect(bridge.ensureReady()).rejects.toThrow("Failed to ping Copilot SDK");
		});

		it("throws BridgeError if not signed in and no BYOK configured", async () => {
			mockClient.getAuthStatus.mockResolvedValue({ isAuthenticated: false });

			await expect(bridge.ensureReady()).rejects.toThrow("Not authenticated");
		});

		it("succeeds if not signed in but BYOK is configured", async () => {
			mockClient.getAuthStatus.mockResolvedValue({ isAuthenticated: false });
			const byokBridge = new CopilotBridge(
				makeConfig({
					byokProvider: "openai",
					byokApiKey: "sk-test",
				}),
			);

			await expect(byokBridge.ensureReady()).resolves.toBeUndefined();
		});
	});

	describe("stop()", () => {
		it("calls client.stop() and completes when no errors", async () => {
			mockClient.stop.mockResolvedValue([]);

			await bridge.stop();

			expect(mockClient.stop).toHaveBeenCalled();
			expect(mockClient.forceStop).not.toHaveBeenCalled();
		});

		it("calls forceStop() when stop() returns errors", async () => {
			mockClient.stop.mockResolvedValue([new Error("cleanup failed")]);
			mockClient.forceStop.mockResolvedValue(undefined);

			await bridge.stop();

			expect(mockClient.stop).toHaveBeenCalled();
			expect(mockClient.forceStop).toHaveBeenCalled();
		});

		it("calls forceStop() when stop() throws", async () => {
			mockClient.stop.mockRejectedValue(new Error("unexpected"));
			mockClient.forceStop.mockResolvedValue(undefined);

			await bridge.stop();

			expect(mockClient.forceStop).toHaveBeenCalled();
		});
	});

	describe("isReady()", () => {
		it("returns true when ping succeeds", async () => {
			expect(await bridge.isReady()).toBe(true);
		});

		it("returns false when ping fails", async () => {
			mockClient.ping.mockRejectedValue(new Error("down"));
			expect(await bridge.isReady()).toBe(false);
		});
	});

	describe("getStatus()", () => {
		it("returns connected and authMethod", async () => {
			await bridge.ensureReady(); // sets authMethod
			const status = await bridge.getStatus();
			expect(status).toEqual({ connected: true, authMethod: "user" });
		});
	});

	describe("runTask()", () => {
		it("creates session with hooks instead of onPermissionRequest", async () => {
			const result = await bridge.runTask(makeRequest());

			expect(mockClient.createSession).toHaveBeenCalledTimes(1);
			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;

			// hooks MUST be present, onPermissionRequest MUST NOT
			expect(sessionConfig).not.toHaveProperty("onPermissionRequest");
			expect(sessionConfig).toHaveProperty("hooks");
			const hooks = sessionConfig.hooks as Record<string, unknown>;
			expect(typeof hooks.onPreToolUse).toBe("function");
			expect(typeof hooks.onPostToolUse).toBe("function");
			expect(typeof hooks.onUserPromptSubmitted).toBe("function");
			expect(typeof hooks.onSessionStart).toBe("function");
			expect(typeof hooks.onSessionEnd).toBe("function");
			expect(typeof hooks.onErrorOccurred).toBe("function");

			// streaming should be false for non-streaming
			expect(sessionConfig.streaming).toBe(false);

			expect(result.success).toBe(true);
			expect(result.content).toBe("response text");
		});

		it("default config produces permissive hooks (backward compatible)", async () => {
			await bridge.runTask(makeRequest());

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			const hooks = sessionConfig.hooks as Record<string, unknown>;

			// With default policy, all tools should be allowed
			const onPreToolUse = hooks.onPreToolUse as (
				...args: unknown[]
			) => Promise<Record<string, unknown>>;
			const preToolResult = await onPreToolUse(
				{ timestamp: Date.now(), cwd: "/tmp", toolName: "anything", toolArgs: {} },
				{ sessionId: "test" },
			);
			expect(preToolResult.permissionDecision).toBe("allow");
		});

		it("custom policy is passed through to hooks", async () => {
			const customBridge = new CopilotBridge(
				makeConfig({
					permissionPolicy: {
						allowedTools: [],
						blockedTools: ["dangerous_tool"],
						askTools: [],
						blockedPatterns: [],
					},
				}),
			);

			await customBridge.runTask(makeRequest());

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			const hooks = sessionConfig.hooks as Record<string, unknown>;

			const onPreToolUse = hooks.onPreToolUse as (
				...args: unknown[]
			) => Promise<Record<string, unknown>>;
			const preToolResult = await onPreToolUse(
				{ timestamp: Date.now(), cwd: "/tmp", toolName: "dangerous_tool", toolArgs: {} },
				{ sessionId: "test" },
			);
			expect(preToolResult.permissionDecision).toBe("deny");
		});

		it("passes provider from request to createSession, not constructor", async () => {
			const requestProvider = {
				type: "anthropic" as const,
				apiKey: "sk-ant",
				model: "claude-3",
			};

			await bridge.runTask(makeRequest({ provider: requestProvider }));

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			expect(sessionConfig.provider).toEqual(requestProvider);
		});

		it("uses default BYOK provider when request has none", async () => {
			const byokBridge = new CopilotBridge(
				makeConfig({
					byokProvider: "openai",
					byokApiKey: "sk-test",
					byokBaseUrl: "https://api.openai.com",
					byokModel: "gpt-4o",
				}),
			);

			await byokBridge.runTask(makeRequest());

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			expect(sessionConfig.provider).toEqual({
				type: "openai",
				apiKey: "sk-test",
				baseUrl: "https://api.openai.com",
				model: "gpt-4o",
			});
		});

		it("sends prompt via sendAndWait()", async () => {
			await bridge.runTask(makeRequest({ prompt: "test prompt" }));

			expect(mockSession.sendAndWait).toHaveBeenCalledWith({ prompt: "test prompt" });
		});

		it("destroys session after task when no sessionId provided", async () => {
			await bridge.runTask(makeRequest());

			// Sessions are now stored — no longer destroyed in runTask
			expect(mockSession.destroy).not.toHaveBeenCalled();
		});

		it("does NOT destroy session when sessionId is provided in request", async () => {
			await bridge.runTask(makeRequest({ sessionId: "persistent-session" }));

			expect(mockSession.destroy).not.toHaveBeenCalled();
		});

		it("returns errors on failure", async () => {
			mockSession.sendAndWait.mockRejectedValue(new Error("SDK failure"));

			const result = await bridge.runTask(makeRequest());

			expect(result.success).toBe(false);
			expect(result.errors).toContain("SDK failure");
		});

		it("handles timeout and cleans up event listener", async () => {
			mockSession.sendAndWait.mockImplementation(
				() =>
					new Promise((resolve) => setTimeout(() => resolve({ data: { content: "late" } }), 5000)),
			);

			const result = await bridge.runTask(makeRequest({ timeout: 50 }));

			expect(result.success).toBe(false);
			expect(result.errors.some((e) => e.includes("timed out"))).toBe(true);
			// Session is stored even on error (for retry)
		});
	});

	describe("session persistence", () => {
		it("stores session after first runTask and reuses on second call with same sessionId", async () => {
			const result1 = await bridge.runTask(makeRequest({ sessionId: "reuse-me" }));
			expect(result1.sessionId).toBe("reuse-me");
			expect(mockClient.createSession).toHaveBeenCalledTimes(1);

			const result2 = await bridge.runTask(makeRequest({ sessionId: "reuse-me", prompt: "follow up" }));
			expect(result2.sessionId).toBe("reuse-me");
			// createSession should NOT be called again
			expect(mockClient.createSession).toHaveBeenCalledTimes(1);
			// sendAndWait should have been called twice
			expect(mockSession.sendAndWait).toHaveBeenCalledTimes(2);
		});

		it("runTask without sessionId creates a new session and stores it", async () => {
			const result = await bridge.runTask(makeRequest());
			expect(result.sessionId).toBeTruthy();

			const sessions = bridge.listSessions();
			expect(sessions).toHaveLength(1);
			expect(sessions[0].sessionId).toBe(result.sessionId);
			expect(sessions[0].messageCount).toBe(1);
		});

		it("increments messageCount on session reuse", async () => {
			await bridge.runTask(makeRequest({ sessionId: "counter-test" }));
			await bridge.runTask(makeRequest({ sessionId: "counter-test", prompt: "msg 2" }));
			await bridge.runTask(makeRequest({ sessionId: "counter-test", prompt: "msg 3" }));

			const sessions = bridge.listSessions();
			const session = sessions.find(s => s.sessionId === "counter-test");
			expect(session?.messageCount).toBe(3);
		});

		it("listSessions returns correct metadata", async () => {
			await bridge.runTask(makeRequest({ sessionId: "sess-a", workingDir: "/home/a" }));
			await bridge.runTask(makeRequest({ sessionId: "sess-b", workingDir: "/home/b" }));

			const sessions = bridge.listSessions();
			expect(sessions).toHaveLength(2);

			const a = sessions.find(s => s.sessionId === "sess-a");
			expect(a).toBeDefined();
			expect(a!.workingDir).toBe("/home/a");
			expect(a!.messageCount).toBe(1);
			expect(a!.createdAt).toBeTruthy();

			const b = sessions.find(s => s.sessionId === "sess-b");
			expect(b).toBeDefined();
			expect(b!.workingDir).toBe("/home/b");
		});

		it("destroySession removes session from store and calls session.destroy()", async () => {
			await bridge.runTask(makeRequest({ sessionId: "to-destroy" }));
			expect(bridge.listSessions()).toHaveLength(1);

			const destroyed = await bridge.destroySession("to-destroy");
			expect(destroyed).toBe(true);
			expect(mockSession.destroy).toHaveBeenCalled();
			expect(bridge.listSessions()).toHaveLength(0);
		});

		it("destroySession returns false for unknown session", async () => {
			const destroyed = await bridge.destroySession("nonexistent");
			expect(destroyed).toBe(false);
		});

		it("destroyAllSessions clears all sessions", async () => {
			await bridge.runTask(makeRequest({ sessionId: "s1" }));
			await bridge.runTask(makeRequest({ sessionId: "s2" }));
			await bridge.runTask(makeRequest({ sessionId: "s3" }));
			expect(bridge.listSessions()).toHaveLength(3);

			const count = await bridge.destroyAllSessions();
			expect(count).toBe(3);
			expect(bridge.listSessions()).toHaveLength(0);
			expect(mockSession.destroy).toHaveBeenCalledTimes(3);
		});

		it("stop() destroys all sessions before stopping client", async () => {
			await bridge.runTask(makeRequest({ sessionId: "stop-test" }));
			await bridge.stop();

			expect(mockSession.destroy).toHaveBeenCalled();
			expect(bridge.listSessions()).toHaveLength(0);
			expect(mockClient.stop).toHaveBeenCalled();
		});
	});

	describe("runTaskStreaming()", () => {
		it("creates session with streaming: true and hooks", async () => {
			// Set up session.on to simulate events
			mockSession.on.mockImplementation((callback: (event: any) => void) => {
				// Simulate events asynchronously
				setTimeout(() => {
					callback({ type: "content.delta", content: "Hello" });
					callback({ type: "response.completed" });
				}, 10);
				return vi.fn(); // unsubscribe
			});
			mockSession.sendAndWait.mockResolvedValue({ data: { content: "Hello" } });

			const deltas: Array<{ type: string; content: string }> = [];
			for await (const delta of bridge.runTaskStreaming(makeRequest())) {
				deltas.push(delta);
				if (delta.type === "done") break;
			}

			expect(mockClient.createSession).toHaveBeenCalledTimes(1);
			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			expect(sessionConfig.streaming).toBe(true);
			expect(sessionConfig).not.toHaveProperty("onPermissionRequest");
			expect(sessionConfig).toHaveProperty("hooks");
			const hooks = sessionConfig.hooks as Record<string, unknown>;
			expect(typeof hooks.onPreToolUse).toBe("function");
			expect(typeof hooks.onPostToolUse).toBe("function");
			expect(typeof hooks.onUserPromptSubmitted).toBe("function");
			expect(typeof hooks.onSessionStart).toBe("function");
			expect(typeof hooks.onSessionEnd).toBe("function");
			expect(typeof hooks.onErrorOccurred).toBe("function");

			// Should have at least text delta and done
			expect(deltas.some((d) => d.type === "text")).toBe(true);
			expect(deltas[deltas.length - 1]?.type).toBe("done");
		});

		it("request provider overrides default in streaming mode", async () => {
			mockSession.on.mockImplementation((callback: (event: any) => void) => {
				setTimeout(() => callback({ type: "response.completed" }), 10);
				return vi.fn();
			});
			mockSession.sendAndWait.mockResolvedValue({ data: { content: "" } });

			const requestProvider = { type: "azure" as const, baseUrl: "https://my.azure.com" };

			const gen = bridge.runTaskStreaming(makeRequest({ provider: requestProvider }));
			for await (const _ of gen) {
				/* drain */
			}

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			expect(sessionConfig.provider).toEqual(requestProvider);
		});

		it("uses default BYOK provider when request has none in streaming mode", async () => {
			mockSession.on.mockImplementation((callback: (event: any) => void) => {
				setTimeout(() => callback({ type: "response.completed" }), 10);
				return vi.fn();
			});
			mockSession.sendAndWait.mockResolvedValue({ data: { content: "" } });

			const byokBridge = new CopilotBridge(
				makeConfig({
					byokProvider: "openai",
					byokApiKey: "sk-test",
					byokBaseUrl: "https://api.openai.com",
					byokModel: "gpt-4o",
				}),
			);

			const gen = byokBridge.runTaskStreaming(makeRequest());
			for await (const _ of gen) {
				/* drain */
			}

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			expect(sessionConfig.provider).toEqual({
				type: "openai",
				apiKey: "sk-test",
				baseUrl: "https://api.openai.com",
				model: "gpt-4o",
			});
		});
	});

	describe("MCP server lifecycle", () => {
		it("runTask() always includes mcpServers in session config", async () => {
			await bridge.runTask(makeRequest());

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			const mcpServers = sessionConfig.mcpServers as Record<string, Record<string, unknown>>;
			expect(mcpServers).toBeDefined();
			expect(mcpServers.openclaw).toBeDefined();
			expect(mcpServers.openclaw.type).toBe("local");
			expect(mcpServers.openclaw.command).toBe("node");
			expect(mcpServers.openclaw.args).toEqual([expect.stringContaining("mcp-openclaw")]);
		});

		it("runTaskStreaming() always includes mcpServers in session config", async () => {
			mockSession.on.mockImplementation((callback: (event: any) => void) => {
				setTimeout(() => callback({ type: "response.completed" }), 10);
				return vi.fn();
			});
			mockSession.sendAndWait.mockResolvedValue({ data: { content: "" } });

			const gen = bridge.runTaskStreaming(makeRequest());
			for await (const _ of gen) {
				/* drain */
			}

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			const mcpServers = sessionConfig.mcpServers as Record<string, Record<string, unknown>>;
			expect(mcpServers).toBeDefined();
			expect(mcpServers.openclaw.type).toBe("local");
		});

		it("mcpServers config uses correct structure", async () => {
			await bridge.runTask(makeRequest());

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			const mcpServers = sessionConfig.mcpServers as Record<string, Record<string, unknown>>;

			expect(mcpServers).toEqual({
				openclaw: {
					type: "local",
					command: "node",
					args: [expect.stringContaining("mcp-openclaw")],
				},
			});
		});
	});
});
