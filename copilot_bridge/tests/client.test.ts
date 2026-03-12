import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { approveAll } from "@github/copilot-sdk";
import type { BridgeConfig } from "../src/config.js";
import type { CodingTaskRequest } from "../src/types.js";

// --- SDK Mock (vi.hoisted ensures these exist before vi.mock factory runs) ---

const { mockClient, mockSession, mockFs, mockExistsSync, mockHomedir } = vi.hoisted(() => {
	const mockHomedir = vi.fn().mockReturnValue("/home/testuser");
	const mockSession = {
		sendAndWait: vi.fn(),
		on: vi.fn().mockReturnValue(vi.fn()), // returns unsubscribe fn
		destroy: vi.fn().mockResolvedValue(undefined),
		rpc: {},
		getMessages: vi.fn().mockResolvedValue([]),
	};

	const mockClient = {
		start: vi.fn().mockResolvedValue(undefined),
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

	const mockExistsSync = vi.fn().mockReturnValue(false);

	return { mockClient, mockSession, mockFs, mockExistsSync, mockHomedir };
});

vi.mock("@github/copilot-sdk", () => ({
	CopilotClient: vi.fn().mockImplementation(function () { return mockClient; }),
	approveAll: vi.fn(),
}));

vi.mock("node:fs/promises", () => ({
	default: mockFs,
	...mockFs,
}));

vi.mock("node:fs", () => ({
	existsSync: mockExistsSync,
}));

vi.mock("node:os", () => ({
	default: { homedir: mockHomedir },
	homedir: mockHomedir,
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
		mockExistsSync.mockReturnValue(false);

		mockClient.start.mockResolvedValue(undefined);
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

		it("expands tilde to home directory", async () => {
			const config = makeConfig({ projectsRoot: "/home/user/repos" });
			bridge = new CopilotBridge(config);
			const resolved = await bridge.resolveWorkingDir("~/repos/weather");
			expect(resolved).toBe("/home/testuser/repos/weather");
			expect(mockFs.mkdir).toHaveBeenCalledWith("/home/testuser/repos/weather", { recursive: true });
		});

		it("expands bare tilde to home directory", async () => {
			const config = makeConfig({ projectsRoot: "/home/user/repos" });
			bridge = new CopilotBridge(config);
			const resolved = await bridge.resolveWorkingDir("~");
			expect(resolved).toBe("/home/testuser");
			expect(mockFs.mkdir).toHaveBeenCalledWith("/home/testuser", { recursive: true });
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
		it("creates session with hooks and onPermissionRequest approveAll", async () => {
			const result = await bridge.runTask(makeRequest());

			expect(mockClient.createSession).toHaveBeenCalledTimes(1);
			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;

			// Both hooks and onPermissionRequest must be present
			expect(sessionConfig.onPermissionRequest).toBe(approveAll);
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

		it("uses byokModel as session model when BYOK provider is configured", async () => {
			const byokBridge = new CopilotBridge(
				makeConfig({
					byokProvider: "azure",
					byokApiKey: "sk-test",
					byokBaseUrl: "https://my.azure.com",
					byokModel: "model-router",
					model: "claude-opus-4.6",
				}),
			);

			await byokBridge.runTask(makeRequest());

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			expect(sessionConfig.model).toBe("model-router");
		});

		it("falls back to config.model when no BYOK provider is configured", async () => {
			const plainBridge = new CopilotBridge(
				makeConfig({
					model: "claude-opus-4.6",
				}),
			);

			await plainBridge.runTask(makeRequest());

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			expect(sessionConfig.model).toBe("claude-opus-4.6");
		});

		it("request-level model overrides byokModel", async () => {
			const byokBridge = new CopilotBridge(
				makeConfig({
					byokProvider: "azure",
					byokApiKey: "sk-test",
					byokBaseUrl: "https://my.azure.com",
					byokModel: "model-router",
					model: "claude-opus-4.6",
				}),
			);

			await byokBridge.runTask(makeRequest({ model: "gpt-4o-mini" }));

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			expect(sessionConfig.model).toBe("gpt-4o-mini");
		});

		it("sends prompt via sendAndWait()", async () => {
			await bridge.runTask(makeRequest({ prompt: "test prompt" }));

			expect(mockSession.sendAndWait).toHaveBeenCalledWith({ prompt: "test prompt" });
		});

		it("passes systemMessage to createSession as append config", async () => {
			await bridge.runTask(makeRequest({ systemMessage: "You are a planner." }));

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			expect(sessionConfig.systemMessage).toEqual({
				type: "append",
				content: "You are a planner.",
			});
		});

		it("omits systemMessage from session config when not provided", async () => {
			await bridge.runTask(makeRequest());

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			expect(sessionConfig.systemMessage).toBeUndefined();
		});

		it("sets skillDirectories when .github/skills/ exists in workingDir", async () => {
			mockExistsSync.mockReturnValue(true);

			await bridge.runTask(makeRequest({ workingDir: "/home/user/repos/my-app" }));

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			expect(sessionConfig.skillDirectories).toEqual(["/home/user/repos/my-app/.github/skills"]);
			expect(mockExistsSync).toHaveBeenCalledWith("/home/user/repos/my-app/.github/skills");
		});

		it("does not set skillDirectories when .github/skills/ does not exist", async () => {
			mockExistsSync.mockReturnValue(false);

			await bridge.runTask(makeRequest({ workingDir: "/home/user/repos/no-skills" }));

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			expect(sessionConfig.skillDirectories).toBeUndefined();
		});

		it("does not set skillDirectories when no workingDir provided", async () => {
			await bridge.runTask(makeRequest());

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			expect(sessionConfig.skillDirectories).toBeUndefined();
			expect(mockExistsSync).not.toHaveBeenCalled();
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

		it("passes a UUID as sessionId to createSession, not the request sessionId", async () => {
			const dirPath = "/home/user/repos/weather";
			const result = await bridge.runTask(makeRequest({ sessionId: dirPath }));

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			const sdkSessionId = sessionConfig.sessionId as string;

			// SDK should receive a UUID, not the dir path
			expect(sdkSessionId).not.toBe(dirPath);
			expect(sdkSessionId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);

			// But the returned sessionId should be the original sessionKey (dir path)
			expect(result.sessionId).toBe(dirPath);
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
			expect(a!.lastAccessedAt).toBeTruthy();

			const b = sessions.find(s => s.sessionId === "sess-b");
			expect(b).toBeDefined();
			expect(b!.workingDir).toBe("/home/b");
			expect(b!.lastAccessedAt).toBeTruthy();
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

		it("lastAccessedAt is updated on session reuse", async () => {
			await bridge.runTask(makeRequest({ sessionId: "access-test" }));
			const sessions1 = bridge.listSessions();
			const first = sessions1.find(s => s.sessionId === "access-test")!;
			const firstAccess = first.lastAccessedAt;

			// Small delay to ensure different timestamp
			await new Promise(r => setTimeout(r, 10));

			await bridge.runTask(makeRequest({ sessionId: "access-test", prompt: "follow up" }));
			const sessions2 = bridge.listSessions();
			const second = sessions2.find(s => s.sessionId === "access-test")!;

			expect(new Date(second.lastAccessedAt).getTime()).toBeGreaterThanOrEqual(
				new Date(firstAccess).getTime()
			);
		});

		it("LRU eviction destroys oldest session when maxSessions reached", async () => {
			const smallBridge = new CopilotBridge(makeConfig({ maxSessions: 2 }));

			// Create two sessions at capacity
			await smallBridge.runTask(makeRequest({ sessionId: "old-sess", prompt: "first" }));
			// Small delay so timestamps differ
			await new Promise(r => setTimeout(r, 10));
			await smallBridge.runTask(makeRequest({ sessionId: "mid-sess", prompt: "second" }));
			expect(smallBridge.listSessions()).toHaveLength(2);

			// Adding a third should evict "old-sess" (oldest lastAccessedAt)
			await smallBridge.runTask(makeRequest({ sessionId: "new-sess", prompt: "third" }));
			const sessions = smallBridge.listSessions();
			expect(sessions).toHaveLength(2);
			expect(sessions.map(s => s.sessionId).sort()).toEqual(["mid-sess", "new-sess"]);
			// session.destroy should have been called for eviction
			expect(mockSession.destroy).toHaveBeenCalled();
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
			expect(sessionConfig.onPermissionRequest).toBe(approveAll);
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

		it("uses byokModel as session model in streaming when BYOK is configured", async () => {
			mockSession.on.mockImplementation((callback: (event: any) => void) => {
				setTimeout(() => callback({ type: "response.completed" }), 10);
				return vi.fn();
			});
			mockSession.sendAndWait.mockResolvedValue({ data: { content: "" } });

			const byokBridge = new CopilotBridge(
				makeConfig({
					byokProvider: "azure",
					byokApiKey: "sk-test",
					byokBaseUrl: "https://my.azure.com",
					byokModel: "model-router",
					model: "claude-opus-4.6",
				}),
			);

			const gen = byokBridge.runTaskStreaming(makeRequest());
			for await (const _ of gen) {
				/* drain */
			}

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			expect(sessionConfig.model).toBe("model-router");
		});

		it("falls back to config.model in streaming when no BYOK configured", async () => {
			mockSession.on.mockImplementation((callback: (event: any) => void) => {
				setTimeout(() => callback({ type: "response.completed" }), 10);
				return vi.fn();
			});
			mockSession.sendAndWait.mockResolvedValue({ data: { content: "" } });

			const plainBridge = new CopilotBridge(
				makeConfig({ model: "claude-opus-4.6" }),
			);

			const gen = plainBridge.runTaskStreaming(makeRequest());
			for await (const _ of gen) {
				/* drain */
			}

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			expect(sessionConfig.model).toBe("claude-opus-4.6");
		});

		it("request-level model overrides byokModel in streaming", async () => {
			mockSession.on.mockImplementation((callback: (event: any) => void) => {
				setTimeout(() => callback({ type: "response.completed" }), 10);
				return vi.fn();
			});
			mockSession.sendAndWait.mockResolvedValue({ data: { content: "" } });

			const byokBridge = new CopilotBridge(
				makeConfig({
					byokProvider: "azure",
					byokApiKey: "sk-test",
					byokBaseUrl: "https://my.azure.com",
					byokModel: "model-router",
					model: "claude-opus-4.6",
				}),
			);

			const gen = byokBridge.runTaskStreaming(makeRequest({ model: "gpt-4o-mini" }));
			for await (const _ of gen) {
				/* drain */
			}

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			expect(sessionConfig.model).toBe("gpt-4o-mini");
		});

		it("passes systemMessage to streaming session config", async () => {
			mockSession.on.mockImplementation((callback: (event: any) => void) => {
				setTimeout(() => callback({ type: "response.completed" }), 10);
				return vi.fn();
			});
			mockSession.sendAndWait.mockResolvedValue({ data: { content: "" } });

			const gen = bridge.runTaskStreaming(makeRequest({ systemMessage: "You are a planner." }));
			for await (const _ of gen) {
				/* drain */
			}

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			expect(sessionConfig.systemMessage).toEqual({
				type: "append",
				content: "You are a planner.",
			});
		});

		it("sets skillDirectories in streaming when .github/skills/ exists", async () => {
			mockExistsSync.mockReturnValue(true);
			mockSession.on.mockImplementation((callback: (event: any) => void) => {
				setTimeout(() => callback({ type: "response.completed" }), 10);
				return vi.fn();
			});
			mockSession.sendAndWait.mockResolvedValue({ data: { content: "" } });

			const gen = bridge.runTaskStreaming(makeRequest({ workingDir: "/home/user/repos/my-app" }));
			for await (const _ of gen) {
				/* drain */
			}

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			expect(sessionConfig.skillDirectories).toEqual(["/home/user/repos/my-app/.github/skills"]);
		});

		it("does not set skillDirectories in streaming when .github/skills/ missing", async () => {
			mockExistsSync.mockReturnValue(false);
			mockSession.on.mockImplementation((callback: (event: any) => void) => {
				setTimeout(() => callback({ type: "response.completed" }), 10);
				return vi.fn();
			});
			mockSession.sendAndWait.mockResolvedValue({ data: { content: "" } });

			const gen = bridge.runTaskStreaming(makeRequest({ workingDir: "/home/user/repos/no-skills" }));
			for await (const _ of gen) {
				/* drain */
			}

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			expect(sessionConfig.skillDirectories).toBeUndefined();
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
