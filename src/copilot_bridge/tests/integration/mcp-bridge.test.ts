import { afterAll, beforeAll, describe, expect, it } from "vitest";

const INTEGRATION = !!process.env.COPILOT_INTEGRATION && !!process.env.OPENCLAW_INTEGRATION;

describe.skipIf(!INTEGRATION)(
	"MCP Bridge Integration Tests",
	() => {
		// Import modules dynamically in beforeAll
		let mcpServer: typeof import("../../src/mcp-server.js");
		let mcpOpenclaw: typeof import("../../src/mcp-openclaw.js");
		let bridge: InstanceType<typeof import("../../src/client.js").CopilotBridge>;

		beforeAll(async () => {
			// Dynamic imports to avoid loading when skipped
			mcpServer = await import("../../src/mcp-server.js");
			mcpOpenclaw = await import("../../src/mcp-openclaw.js");
			const { CopilotBridge } = await import("../../src/client.js");
			const { loadConfig } = await import("../../src/config.js");

			const config = loadConfig();
			bridge = new CopilotBridge(config);
			await bridge.ensureReady();
		});

		afterAll(async () => {
			await mcpServer.shutdown();
			await bridge.stop();
		});

		// Test 1: Deterministic file read via direct session RPC
		// (tests the underlying SDK capability; MCP tool wrapper tested in unit tests)
		it("copilot_read_file reads a file deterministically", async () => {
			const { session } = await mcpServer.ensureInitialized();
			// Read a known file (the package.json of the copilot_bridge itself)
			const result = await session.rpc["workspace.readFile"]({ path: "package.json" });
			expect(result).toBeDefined();
			const text = typeof result === "string" ? result : JSON.stringify(result);
			expect(text).toContain("copilot_bridge");
		}, 30_000);

		// Test 2: Agent-mediated code task
		it("copilot_code_task returns LLM response", async () => {
			const result = await bridge.runTask({
				prompt: "What is 2 + 2? Reply with just the number.",
				timeout: 60_000,
			});
			expect(result.success).toBe(true);
			expect(result.content).toBeTruthy();
			expect(result.content).toMatch(/4|four/i);
		}, 90_000);

		// Test 3: OpenClaw memory search (reads local memory files)
		it("openclaw_memory_search returns results or no-match message", async () => {
			const result = await mcpOpenclaw.handleMemorySearch({
				query: "test",
				limit: 5,
			});
			expect(result.content).toHaveLength(1);
			expect(result.content[0].type).toBe("text");
			// Either returns matches or "No memory entries found" — both are valid
			expect(result.content[0].text).toBeTruthy();
		}, 15_000);

		// Test 4: Combined — bridge task + memory read (concurrent)
		it("bridge can run task while memory tools are available", async () => {
			const [prefs, task] = await Promise.all([
				mcpOpenclaw.handleUserPrefs(),
				bridge.runTask({
					prompt: "Say hello",
					timeout: 30_000,
				}),
			]);
			expect(prefs.content[0].text).toBeTruthy();
			expect(task.content).toBeDefined();
		}, 60_000);

		// Test 5: Soft recovery (simulates lazy-init re-entry after state reset,
		// not a real process crash — see unit tests for process lifecycle)
		it("recovers after MCP server state reset", async () => {
			// Simulate crash by resetting state
			mcpServer._resetState();
			mcpServer._resetMutex();

			// Next call should re-initialize
			const { session } = await mcpServer.ensureInitialized();
			expect(session).toBeTruthy();
		}, 30_000);

		// Test 6: Cycle detection
		it("rejects calls at max depth", () => {
			const depthError = mcpServer.checkDepth(3);
			expect(depthError).not.toBeNull();
			expect(depthError?.isError).toBe(true);
			expect(depthError?.content[0].text).toContain("Maximum call depth exceeded");

			const noError = mcpServer.checkDepth(2);
			expect(noError).toBeNull();

			// Also check openclaw side
			const openclawError = mcpOpenclaw.checkDepth(3);
			expect(openclawError).not.toBeNull();
			expect(openclawError?.content[0].text).toContain("cycle detected");
		});
	},
	90_000,
);
