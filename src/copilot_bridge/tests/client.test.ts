import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BridgeConfig } from "../src/config.js";
import type { CodingTaskRequest } from "../src/types.js";

// --- SDK Mock (vi.hoisted ensures these exist before vi.mock factory runs) ---

const { mockClient, mockSession } = vi.hoisted(() => {
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

	return { mockClient, mockSession };
});

vi.mock("@github/copilot-sdk", () => ({
	CopilotClient: vi.fn().mockImplementation(() => mockClient),
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
		it("creates session with correct config including onPermissionRequest", async () => {
			const result = await bridge.runTask(makeRequest());

			expect(mockClient.createSession).toHaveBeenCalledTimes(1);
			const sessionConfig = mockClient.createSession.mock.calls[0]?.[0] as Record<string, unknown>;

			// onPermissionRequest MUST be present
			expect(sessionConfig).toHaveProperty("onPermissionRequest");
			expect(typeof sessionConfig.onPermissionRequest).toBe("function");

			// streaming should be false for non-streaming
			expect(sessionConfig.streaming).toBe(false);

			expect(result.success).toBe(true);
			expect(result.content).toBe("response text");
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

		it("handles timeout", async () => {
			mockSession.sendAndWait.mockImplementation(
				() =>
					new Promise((resolve) => setTimeout(() => resolve({ data: { content: "late" } }), 5000)),
			);

			const result = await bridge.runTask(makeRequest({ timeout: 50 }));

			expect(result.success).toBe(false);
			expect(result.errors.some((e) => e.includes("timed out"))).toBe(true);
		});
	});

	describe("runTaskStreaming()", () => {
		it("creates session with streaming: true and onPermissionRequest", async () => {
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
			expect(sessionConfig).toHaveProperty("onPermissionRequest");
			expect(typeof sessionConfig.onPermissionRequest).toBe("function");

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
	});
});
