import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionMetadata } from "../src/client.js";
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
}));

const { CopilotClient: MockedCopilotClient } = await import("@github/copilot-sdk");
const { CopilotBridge } = await import("../src/client.js");

// --- Helpers ---

function makeConfig(overrides: Partial<BridgeConfig> = {}): BridgeConfig {
	return {
		logLevel: "warning",
		openclawHost: "127.0.0.1",
		openclawPort: 18789,
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

			expect(mockSession.destroy).toHaveBeenCalled();
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

		it("handles timeout and still destroys session", async () => {
			mockSession.sendAndWait.mockImplementation(
				() =>
					new Promise((resolve) => setTimeout(() => resolve({ data: { content: "late" } }), 5000)),
			);

			const result = await bridge.runTask(makeRequest({ timeout: 50 }));

			expect(result.success).toBe(false);
			expect(result.errors.some((e) => e.includes("timed out"))).toBe(true);
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

	describe("session persistence", () => {
		const sampleSession: SessionMetadata = {
			sessionId: "sess-123",
			task: "Fix the bug",
			startTime: "2026-01-01T00:00:00.000Z",
			lastActivity: "2026-01-01T01:00:00.000Z",
			providerType: "openai",
			providerBaseUrl: "https://api.openai.com",
			workingDir: "/tmp/project",
		};

		beforeEach(() => {
			mockFs.readFile.mockRejectedValue(new Error("ENOENT"));
			mockFs.writeFile.mockResolvedValue(undefined);
			mockFs.mkdir.mockResolvedValue(undefined);
		});

		it("loadSessionsFile returns empty array when file doesn't exist", async () => {
			const sessions = await bridge.listPersistedSessions();
			expect(sessions).toEqual([]);
		});

		it("persistSession saves metadata to sessions.json", async () => {
			await bridge.runTask(makeRequest({ persistSession: true }));

			expect(mockFs.writeFile).toHaveBeenCalledTimes(1);
			const writtenData = JSON.parse(
				mockFs.writeFile.mock.calls[0]?.[1] as string,
			) as SessionMetadata[];
			expect(writtenData).toHaveLength(1);
			expect(writtenData[0]?.task).toBe("Hello, world!");
		});

		it("resumeTask loads session and sends prompt", async () => {
			mockFs.readFile.mockResolvedValue(JSON.stringify([sampleSession]));

			const result = await bridge.resumeTask("sess-123", "Continue working");

			expect(result.success).toBe(true);
			expect(mockSession.sendAndWait).toHaveBeenCalledWith({
				prompt: "Continue working",
			});
		});

		it("resumeTask throws BridgeError for unknown session", async () => {
			mockFs.readFile.mockResolvedValue(JSON.stringify([]));

			await expect(bridge.resumeTask("nonexistent", "hello")).rejects.toThrow(
				"No persisted session found with ID: nonexistent",
			);
		});

		it("resumeTask reconstructs BYOK provider from metadata + env", async () => {
			const byokBridge = new CopilotBridge(
				makeConfig({
					byokApiKey: "sk-current",
					byokModel: "gpt-4o",
				}),
			);
			mockFs.readFile.mockResolvedValue(JSON.stringify([sampleSession]));

			await byokBridge.resumeTask("sess-123", "Continue");

			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
			const provider = sessionConfig.provider as Record<string, unknown>;
			expect(provider.type).toBe("openai");
			expect(provider.baseUrl).toBe("https://api.openai.com");
			expect(provider.apiKey).toBe("sk-current");
			expect(provider.model).toBe("gpt-4o");
		});

		it("listPersistedSessions returns all saved sessions", async () => {
			const sessions = [sampleSession, { ...sampleSession, sessionId: "sess-456" }];
			mockFs.readFile.mockResolvedValue(JSON.stringify(sessions));

			const result = await bridge.listPersistedSessions();

			expect(result).toHaveLength(2);
			expect(result[0]?.sessionId).toBe("sess-123");
			expect(result[1]?.sessionId).toBe("sess-456");
		});

		it("destroyPersistedSession removes session from file", async () => {
			const sessions = [sampleSession, { ...sampleSession, sessionId: "sess-456" }];
			mockFs.readFile.mockResolvedValue(JSON.stringify(sessions));

			await bridge.destroyPersistedSession("sess-123");

			expect(mockFs.writeFile).toHaveBeenCalledTimes(1);
			const writtenData = JSON.parse(
				mockFs.writeFile.mock.calls[0]?.[1] as string,
			) as SessionMetadata[];
			expect(writtenData).toHaveLength(1);
			expect(writtenData[0]?.sessionId).toBe("sess-456");
		});

		it("cleanStaleSessions removes sessions older than 24h", async () => {
			const staleSession = {
				...sampleSession,
				lastActivity: new Date(Date.now() - 25 * 60 * 60 * 1000).toISOString(),
			};
			mockFs.readFile.mockResolvedValue(JSON.stringify([staleSession]));

			const removed = await bridge.cleanStaleSessions();

			expect(removed).toBe(1);
			const writtenData = JSON.parse(
				mockFs.writeFile.mock.calls[0]?.[1] as string,
			) as SessionMetadata[];
			expect(writtenData).toHaveLength(0);
		});

		it("cleanStaleSessions keeps fresh sessions", async () => {
			const freshSession = {
				...sampleSession,
				lastActivity: new Date().toISOString(),
			};
			mockFs.readFile.mockResolvedValue(JSON.stringify([freshSession]));

			const removed = await bridge.cleanStaleSessions();

			expect(removed).toBe(0);
			expect(mockFs.writeFile).not.toHaveBeenCalled();
		});

		it("runTask with persistSession=true saves metadata and keeps session alive", async () => {
			const result = await bridge.runTask(makeRequest({ persistSession: true }));

			expect(result.success).toBe(true);
			// Session should NOT be destroyed
			expect(mockSession.destroy).not.toHaveBeenCalled();
			// Metadata should be saved
			expect(mockFs.writeFile).toHaveBeenCalledTimes(1);
		});
	});
});
