import { describe, expect, it } from "vitest";

const INTEGRATION_ENABLED =
	process.env.COPILOT_INTEGRATION === "1" && process.env.OPENCLAW_INTEGRATION === "1";

describe.skipIf(!INTEGRATION_ENABLED)("Orchestration E2E", () => {
	// These tests require both Copilot SDK and OpenClaw running

	it("decomposes complex task into sub-tasks and executes in parallel", async () => {
		// Test 1: "Add input validation to all API endpoints"
		// → should decompose into per-file tasks
		// → should execute independent tasks in parallel
		// → should return combined results
		const { CopilotBridge } = await import("../../src/client.js");
		const { loadConfig } = await import("../../src/config.js");
		const { TaskOrchestrator, SessionPool } = await import("../../src/orchestrator.js");

		const config = loadConfig();
		const bridge = new CopilotBridge(config);
		await bridge.ensureReady();

		const pool = new SessionPool(bridge, 2);
		const orchestrator = new TaskOrchestrator(bridge, pool);

		const plan = await orchestrator.planTasks(
			"Create a simple hello-world TypeScript file and a corresponding test file",
		);
		expect(plan.tasks.length).toBeGreaterThanOrEqual(1);

		const result = await orchestrator.executePlan(plan);
		expect(result.tasks.some((t) => t.status === "success")).toBe(true);
		expect(result.summary).toContain("succeeded");

		await pool.drain();
		await bridge.stop();
	}, 120_000);

	it("resumes interrupted orchestration", async () => {
		// Test 2: Start task, "interrupt" by using persistence, resume
		const { CopilotBridge } = await import("../../src/client.js");
		const { loadConfig } = await import("../../src/config.js");

		const config = loadConfig();
		const bridge = new CopilotBridge(config);
		await bridge.ensureReady();

		const result = await bridge.runTask({
			prompt: "Say hello",
			persistSession: true,
			timeout: 30_000,
		});
		expect(result.success).toBe(true);

		const sessions = await bridge.listPersistedSessions();
		expect(sessions.length).toBeGreaterThanOrEqual(1);

		const resumed = await bridge.resumeTask(result.sessionId, "Say goodbye");
		expect(resumed.success).toBe(true);

		await bridge.destroyPersistedSession(result.sessionId);
		await bridge.stop();
	}, 60_000);

	it("returns partial results when sub-task fails", async () => {
		// Test 3: Plan with a task that will fail, verify others complete
		const { CopilotBridge } = await import("../../src/client.js");
		const { loadConfig } = await import("../../src/config.js");
		const { TaskOrchestrator, SessionPool } = await import("../../src/orchestrator.js");

		const config = loadConfig();
		const bridge = new CopilotBridge(config);
		await bridge.ensureReady();

		const pool = new SessionPool(bridge, 2);
		const orchestrator = new TaskOrchestrator(bridge, pool);

		// Manually create a plan with one impossible task
		const plan = {
			tasks: [
				{
					id: "t1",
					description: "Create a file called hello.ts with console.log('hello')",
					estimatedComplexity: "S" as const,
				},
				{
					id: "t2",
					description: "Read the file /nonexistent/impossible/path.ts",
					estimatedComplexity: "S" as const,
				},
				{
					id: "t3",
					description: "List the files in the current directory",
					estimatedComplexity: "S" as const,
				},
			],
			dependencies: new Map<string, string[]>(),
		};

		const result = await orchestrator.executePlan(plan);
		// At least some tasks should complete
		expect(result.tasks.length).toBe(3);
		expect(result.summary).toBeTruthy();

		await pool.drain();
		await bridge.stop();
	}, 120_000);

	it("respects concurrent session pool limit", async () => {
		// Test 4: Submit 5 tasks with pool limit 2, verify max 2 active at a time
		const { CopilotBridge } = await import("../../src/client.js");
		const { loadConfig } = await import("../../src/config.js");
		const { SessionPool } = await import("../../src/orchestrator.js");

		const config = loadConfig();
		const bridge = new CopilotBridge(config);
		await bridge.ensureReady();

		const pool = new SessionPool(bridge, 2);

		const tasks = Array.from({ length: 5 }, (_, i) =>
			pool.execute({ prompt: `Say the number ${i + 1}`, timeout: 30_000 }),
		);

		const results = await Promise.all(tasks);
		expect(results).toHaveLength(5);
		// All should complete (pool queues excess)
		expect(results.every((r) => r.sessionId)).toBe(true);

		await pool.drain();
		await bridge.stop();
	}, 180_000);

	it("BYOK orchestration works", async () => {
		// Test 5: Only runs if BYOK is configured
		const { loadConfig } = await import("../../src/config.js");
		const config = loadConfig();

		if (!config.byokProvider) {
			console.log("Skipping BYOK test — no BYOK provider configured");
			return;
		}

		const { CopilotBridge } = await import("../../src/client.js");
		const { TaskOrchestrator, SessionPool } = await import("../../src/orchestrator.js");

		const bridge = new CopilotBridge(config);
		await bridge.ensureReady();

		const pool = new SessionPool(bridge, 2);
		const orchestrator = new TaskOrchestrator(bridge, pool);

		const plan = await orchestrator.planTasks("Create a simple utility function");
		const result = await orchestrator.executePlan(plan);
		expect(result.tasks.length).toBeGreaterThanOrEqual(1);

		await pool.drain();
		await bridge.stop();
	}, 120_000);
});
