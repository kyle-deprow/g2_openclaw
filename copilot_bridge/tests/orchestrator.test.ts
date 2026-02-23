import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ICopilotClient } from "../src/interfaces.js";
import {
	type OrchestratorEvent,
	SessionPool,
	type SubTask,
	TaskOrchestrator,
	type TaskPlan,
	topologicalSort,
} from "../src/orchestrator.js";
import type { CodingTaskRequest, CodingTaskResult, StreamingDelta } from "../src/types.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeResult(overrides: Partial<CodingTaskResult> = {}): CodingTaskResult {
	return {
		success: true,
		content: "done",
		toolCalls: [],
		errors: [],
		sessionId: crypto.randomUUID(),
		elapsed: 10,
		...overrides,
	};
}

function makeRequest(
	prompt: string,
	overrides: Partial<CodingTaskRequest> = {},
): CodingTaskRequest {
	return { prompt, ...overrides };
}

function createMockBridge(
	runTaskImpl?: (req: CodingTaskRequest) => Promise<CodingTaskResult>,
): ICopilotClient & { runTask: ReturnType<typeof vi.fn> } {
	const runTask = vi.fn<(req: CodingTaskRequest) => Promise<CodingTaskResult>>();

	if (runTaskImpl) {
		runTask.mockImplementation(runTaskImpl);
	} else {
		runTask.mockResolvedValue(makeResult());
	}

	return {
		ensureReady: vi.fn<() => Promise<void>>().mockResolvedValue(undefined),
		stop: vi.fn<() => Promise<void>>().mockResolvedValue(undefined),
		isReady: vi.fn<() => Promise<boolean>>().mockResolvedValue(true),
		getStatus: vi
			.fn<() => Promise<{ connected: boolean; authMethod: string }>>()
			.mockResolvedValue({ connected: true, authMethod: "token" }),
		runTask,
		runTaskStreaming: vi.fn<(req: CodingTaskRequest) => AsyncGenerator<StreamingDelta>>(),
	};
}

function makeTasks(...ids: string[]): SubTask[] {
	return ids.map((id) => ({
		id,
		description: `Task ${id}`,
		estimatedComplexity: "S" as const,
	}));
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("orchestrator", () => {
	// -----------------------------------------------------------------------
	// topologicalSort
	// -----------------------------------------------------------------------
	describe("topologicalSort()", () => {
		it("returns tasks in dependency order", () => {
			const tasks = makeTasks("t1", "t2", "t3");
			const deps = new Map([
				["t2", ["t1"]],
				["t3", ["t2"]],
			]);

			const sorted = topologicalSort(tasks, deps);

			expect(sorted.indexOf("t1")).toBeLessThan(sorted.indexOf("t2"));
			expect(sorted.indexOf("t2")).toBeLessThan(sorted.indexOf("t3"));
		});

		it("returns independent tasks in original order", () => {
			const tasks = makeTasks("a", "b", "c");
			const deps = new Map<string, string[]>();

			const sorted = topologicalSort(tasks, deps);

			expect(sorted).toEqual(["a", "b", "c"]);
		});

		it("handles empty dependencies", () => {
			const tasks = makeTasks("t1");
			const deps = new Map<string, string[]>();

			expect(topologicalSort(tasks, deps)).toEqual(["t1"]);
		});

		it("handles diamond dependencies", () => {
			//   t1
			//  /  \
			// t2  t3
			//  \  /
			//   t4
			const tasks = makeTasks("t1", "t2", "t3", "t4");
			const deps = new Map([
				["t2", ["t1"]],
				["t3", ["t1"]],
				["t4", ["t2", "t3"]],
			]);

			const sorted = topologicalSort(tasks, deps);

			expect(sorted.indexOf("t1")).toBeLessThan(sorted.indexOf("t2"));
			expect(sorted.indexOf("t1")).toBeLessThan(sorted.indexOf("t3"));
			expect(sorted.indexOf("t2")).toBeLessThan(sorted.indexOf("t4"));
			expect(sorted.indexOf("t3")).toBeLessThan(sorted.indexOf("t4"));
		});

		it("throws on circular dependencies", () => {
			const tasks = makeTasks("t1", "t2");
			const deps = new Map([
				["t1", ["t2"]],
				["t2", ["t1"]],
			]);

			expect(() => topologicalSort(tasks, deps)).toThrow("Circular dependency");
		});

		it("throws on missing dependency reference", () => {
			const tasks = makeTasks("t1");
			const deps = new Map([["t1", ["t_missing"]]]);

			expect(() => topologicalSort(tasks, deps)).toThrow('unknown task "t_missing"');
		});
	});

	// -----------------------------------------------------------------------
	// SessionPool
	// -----------------------------------------------------------------------
	describe("SessionPool", () => {
		let mockBridge: ReturnType<typeof createMockBridge>;

		beforeEach(() => {
			mockBridge = createMockBridge();
		});

		afterEach(() => {
			vi.restoreAllMocks();
		});

		it("limits concurrent executions to maxConcurrency", async () => {
			let concurrentCount = 0;
			let maxObserved = 0;

			mockBridge.runTask.mockImplementation(async () => {
				concurrentCount++;
				maxObserved = Math.max(maxObserved, concurrentCount);
				await new Promise((r) => setTimeout(r, 50));
				concurrentCount--;
				return makeResult();
			});

			const pool = new SessionPool(mockBridge, 2);
			await Promise.all([
				pool.execute(makeRequest("a")),
				pool.execute(makeRequest("b")),
				pool.execute(makeRequest("c")),
				pool.execute(makeRequest("d")),
			]);

			expect(maxObserved).toBe(2);
		});

		it("queues excess requests and processes when slot opens", async () => {
			const order: string[] = [];

			mockBridge.runTask.mockImplementation(async (req) => {
				order.push(`start:${req.prompt}`);
				await new Promise((r) => setTimeout(r, 30));
				order.push(`end:${req.prompt}`);
				return makeResult();
			});

			const pool = new SessionPool(mockBridge, 1);
			await Promise.all([pool.execute(makeRequest("first")), pool.execute(makeRequest("second"))]);

			// With concurrency 1, "second" must start after "first" ends
			expect(order.indexOf("end:first")).toBeLessThan(order.indexOf("start:second"));
		});

		it("tracks tainted sessions on failure", async () => {
			mockBridge.runTask.mockResolvedValue(makeResult({ success: false, sessionId: "tainted-1" }));

			const pool = new SessionPool(mockBridge, 3);
			await pool.execute(makeRequest("fail"));

			expect(pool.getTaintedCount()).toBe(1);
		});

		it("drain() waits for active tasks to complete", async () => {
			let finished = false;

			mockBridge.runTask.mockImplementation(async () => {
				await new Promise((r) => setTimeout(r, 100));
				finished = true;
				return makeResult();
			});

			const pool = new SessionPool(mockBridge, 3);
			// Fire-and-forget the execute
			pool.execute(makeRequest("slow"));

			// drain should wait
			await pool.drain();
			expect(finished).toBe(true);
		});

		it("getActiveCount() reflects current state", async () => {
			let resolveTask!: () => void;
			mockBridge.runTask.mockImplementation(
				() =>
					new Promise((resolve) => {
						resolveTask = () => resolve(makeResult());
					}),
			);

			const pool = new SessionPool(mockBridge, 3);
			expect(pool.getActiveCount()).toBe(0);

			const promise = pool.execute(makeRequest("x"));
			// Let the microtask queue flush so acquire() completes
			await new Promise((r) => setTimeout(r, 0));
			expect(pool.getActiveCount()).toBe(1);

			resolveTask();
			await promise;
			expect(pool.getActiveCount()).toBe(0);
		});

		it("defaults to maxConcurrency of 3", async () => {
			let concurrentCount = 0;
			let maxObserved = 0;

			mockBridge.runTask.mockImplementation(async () => {
				concurrentCount++;
				maxObserved = Math.max(maxObserved, concurrentCount);
				await new Promise((r) => setTimeout(r, 50));
				concurrentCount--;
				return makeResult();
			});

			const pool = new SessionPool(mockBridge);
			await Promise.all(Array.from({ length: 5 }, (_, i) => pool.execute(makeRequest(`t${i}`))));

			expect(maxObserved).toBe(3);
		});

		it("execute() returns result from bridge.runTask()", async () => {
			const expected = makeResult({ content: "specific-content" });
			mockBridge.runTask.mockResolvedValue(expected);

			const pool = new SessionPool(mockBridge, 1);
			const result = await pool.execute(makeRequest("q"));

			expect(result).toBe(expected);
		});
	});

	// -----------------------------------------------------------------------
	// TaskOrchestrator
	// -----------------------------------------------------------------------
	describe("TaskOrchestrator", () => {
		let mockBridge: ReturnType<typeof createMockBridge>;
		let pool: SessionPool;
		let orchestrator: TaskOrchestrator;

		beforeEach(() => {
			mockBridge = createMockBridge();
			pool = new SessionPool(mockBridge, 3);
			orchestrator = new TaskOrchestrator(mockBridge, pool);
		});

		afterEach(() => {
			vi.restoreAllMocks();
		});

		// -------------------------------------------------------------------
		// planTasks
		// -------------------------------------------------------------------
		describe("planTasks()", () => {
			it("sends decomposition prompt to bridge and parses JSON response", async () => {
				const llmResponse = JSON.stringify({
					tasks: [
						{ id: "t1", description: "Create file", estimatedComplexity: "S" },
						{ id: "t2", description: "Write tests", estimatedComplexity: "M" },
					],
					dependencies: { t2: ["t1"] },
				});

				mockBridge.runTask.mockResolvedValue(makeResult({ content: llmResponse }));

				const plan = await orchestrator.planTasks("Build a widget");

				expect(mockBridge.runTask).toHaveBeenCalledOnce();
				expect(plan.tasks).toHaveLength(2);
				expect(plan.tasks[0].id).toBe("t1");
				expect(plan.tasks[1].id).toBe("t2");
				expect(plan.dependencies.get("t2")).toEqual(["t1"]);
			});

			it("falls back to single-task plan on invalid JSON", async () => {
				mockBridge.runTask.mockResolvedValue(makeResult({ content: "This is not JSON at all!" }));

				const plan = await orchestrator.planTasks("Do something");

				expect(plan.tasks).toHaveLength(1);
				expect(plan.tasks[0].description).toBe("Do something");
				expect(plan.dependencies.size).toBe(0);
			});

			it("falls back to single-task plan on bridge error", async () => {
				mockBridge.runTask.mockRejectedValue(new Error("SDK unavailable"));

				const plan = await orchestrator.planTasks("Do something");

				expect(plan.tasks).toHaveLength(1);
				expect(plan.tasks[0].description).toBe("Do something");
			});

			it("validates dependency references exist", async () => {
				const llmResponse = JSON.stringify({
					tasks: [{ id: "t1", description: "Only task", estimatedComplexity: "S" }],
					dependencies: { t1: ["ghost"] },
				});

				mockBridge.runTask.mockResolvedValue(makeResult({ content: llmResponse }));

				const plan = await orchestrator.planTasks("Task with bad deps");

				// Invalid dep "ghost" should be filtered out
				expect(plan.tasks).toHaveLength(1);
				expect(plan.dependencies.has("t1")).toBe(false);
			});

			it("strips markdown code fences from response", async () => {
				const inner = JSON.stringify({
					tasks: [{ id: "t1", description: "Fenced task", estimatedComplexity: "L" }],
					dependencies: {},
				});
				const fenced = `\`\`\`json\n${inner}\n\`\`\``;

				mockBridge.runTask.mockResolvedValue(makeResult({ content: fenced }));

				const plan = await orchestrator.planTasks("Fenced");

				expect(plan.tasks).toHaveLength(1);
				expect(plan.tasks[0].description).toBe("Fenced task");
			});
		});

		// -------------------------------------------------------------------
		// executePlan
		// -------------------------------------------------------------------
		describe("executePlan()", () => {
			it("executes independent tasks in parallel", async () => {
				const startTimes: Record<string, number> = {};
				mockBridge.runTask.mockImplementation(async (req) => {
					startTimes[req.prompt] = Date.now();
					await new Promise((r) => setTimeout(r, 30));
					return makeResult();
				});

				const plan: TaskPlan = {
					tasks: makeTasks("t1", "t2"),
					dependencies: new Map(),
				};

				await orchestrator.executePlan(plan);

				// Both should have started nearly simultaneously
				const diff = Math.abs((startTimes["Task t1"] ?? 0) - (startTimes["Task t2"] ?? 0));
				expect(diff).toBeLessThan(25);
			});

			it("executes dependent tasks sequentially", async () => {
				const order: string[] = [];
				mockBridge.runTask.mockImplementation(async (req) => {
					order.push(req.prompt);
					await new Promise((r) => setTimeout(r, 10));
					return makeResult();
				});

				const plan: TaskPlan = {
					tasks: makeTasks("t1", "t2"),
					dependencies: new Map([["t2", ["t1"]]]),
				};

				await orchestrator.executePlan(plan);

				expect(order).toEqual(["Task t1", "Task t2"]);
			});

			it("skips dependents when a task fails", async () => {
				mockBridge.runTask.mockImplementation(async (req) => {
					if (req.prompt === "Task t1") {
						return makeResult({ success: false });
					}
					return makeResult();
				});

				const plan: TaskPlan = {
					tasks: makeTasks("t1", "t2"),
					dependencies: new Map([["t2", ["t1"]]]),
				};

				const result = await orchestrator.executePlan(plan);

				const t2 = result.tasks.find((t) => t.id === "t2");
				expect(t2?.status).toBe("skipped");
			});

			it("returns partial results on failure", async () => {
				mockBridge.runTask.mockImplementation(async (req) => {
					if (req.prompt === "Task t2") {
						return makeResult({ success: false, content: "error details" });
					}
					return makeResult({ content: "ok" });
				});

				const plan: TaskPlan = {
					tasks: makeTasks("t1", "t2", "t3"),
					dependencies: new Map([
						["t2", ["t1"]],
						["t3", ["t2"]],
					]),
				};

				const result = await orchestrator.executePlan(plan);

				expect(result.tasks.find((t) => t.id === "t1")?.status).toBe("success");
				expect(result.tasks.find((t) => t.id === "t2")?.status).toBe("failed");
				expect(result.tasks.find((t) => t.id === "t3")?.status).toBe("skipped");
			});

			it("emits task_start and task_complete events", async () => {
				const events: OrchestratorEvent[] = [];
				orchestrator.on((e) => events.push(e));

				const plan: TaskPlan = {
					tasks: makeTasks("t1"),
					dependencies: new Map(),
				};

				await orchestrator.executePlan(plan);

				const starts = events.filter((e) => e.type === "task_start");
				const completes = events.filter((e) => e.type === "task_complete");
				expect(starts).toHaveLength(1);
				expect(completes).toHaveLength(1);
				expect(starts[0].type === "task_start" && starts[0].taskId).toBe("t1");
				expect(completes[0].type === "task_complete" && completes[0].success).toBe(true);
			});

			it("emits task_skipped events for skipped tasks", async () => {
				const events: OrchestratorEvent[] = [];
				orchestrator.on((e) => events.push(e));

				mockBridge.runTask.mockResolvedValue(makeResult({ success: false }));

				const plan: TaskPlan = {
					tasks: makeTasks("t1", "t2"),
					dependencies: new Map([["t2", ["t1"]]]),
				};

				await orchestrator.executePlan(plan);

				const skippedEvents = events.filter((e) => e.type === "task_skipped");
				expect(skippedEvents).toHaveLength(1);
				expect(skippedEvents[0].type === "task_skipped" && skippedEvents[0].taskId).toBe("t2");
			});

			it("emits plan_complete with summary", async () => {
				const events: OrchestratorEvent[] = [];
				orchestrator.on((e) => events.push(e));

				const plan: TaskPlan = {
					tasks: makeTasks("t1", "t2"),
					dependencies: new Map(),
				};

				await orchestrator.executePlan(plan);

				const complete = events.find((e) => e.type === "plan_complete");
				expect(complete).toBeDefined();
				expect(complete?.type === "plan_complete" && complete.summary).toContain(
					"2/2 tasks succeeded",
				);
			});

			it("returns summary with counts", async () => {
				mockBridge.runTask.mockImplementation(async (req) => {
					if (req.prompt === "Task t1") {
						return makeResult({ success: false });
					}
					return makeResult();
				});

				const plan: TaskPlan = {
					tasks: makeTasks("t1", "t2", "t3"),
					dependencies: new Map([["t2", ["t1"]]]),
				};

				const result = await orchestrator.executePlan(plan);

				// t1 failed, t2 skipped (depends on t1), t3 succeeds (independent)
				expect(result.summary).toContain("1/3 tasks succeeded");
				expect(result.summary).toContain("1 skipped");
				expect(result.summary).toContain("1 failed");
			});

			it("handles single-task plan", async () => {
				const plan: TaskPlan = {
					tasks: makeTasks("t1"),
					dependencies: new Map(),
				};

				const result = await orchestrator.executePlan(plan);

				expect(result.tasks).toHaveLength(1);
				expect(result.tasks[0].status).toBe("success");
				expect(result.summary).toContain("1/1 tasks succeeded");
			});

			it("handles empty plan", async () => {
				const plan: TaskPlan = {
					tasks: [],
					dependencies: new Map(),
				};

				const result = await orchestrator.executePlan(plan);

				expect(result.tasks).toHaveLength(0);
				expect(result.summary).toContain("0/0 tasks succeeded");
			});
		});

		// -------------------------------------------------------------------
		// Event listener unsubscribe
		// -------------------------------------------------------------------
		describe("on()", () => {
			it("returns unsubscribe function that removes listener", async () => {
				const events: OrchestratorEvent[] = [];
				const unsub = orchestrator.on((e) => events.push(e));

				unsub();

				const plan: TaskPlan = {
					tasks: makeTasks("t1"),
					dependencies: new Map(),
				};
				await orchestrator.executePlan(plan);

				expect(events).toHaveLength(0);
			});
		});
	});
});
