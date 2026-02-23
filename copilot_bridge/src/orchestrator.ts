/**
 * C5.1 — Task Decomposition Engine
 * C5.2 — Session Pool
 *
 * Orchestrates multi-step coding tasks by decomposing them via an LLM,
 * respecting dependency order, and running independent sub-tasks in parallel
 * through a concurrency-limited session pool.
 */

import type { ICopilotClient } from "./interfaces.js";
import type { CodingTaskRequest, CodingTaskResult } from "./types.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface SubTask {
	id: string;
	description: string;
	workingDir?: string;
	estimatedComplexity: "S" | "M" | "L";
	/** Tool-specific args to forward beyond the description */
	toolArgs?: Record<string, unknown>;
}

export interface TaskPlan {
	tasks: SubTask[];
	/** Map<taskId, dependsOnTaskIds[]>. Tasks not in map have no dependencies. */
	dependencies: Map<string, string[]>;
}

export interface SubTaskResult {
	id: string;
	result: CodingTaskResult;
	status: "success" | "failed" | "skipped";
	skipReason?: string;
}

export interface OrchestratedResult {
	tasks: SubTaskResult[];
	totalElapsed: number;
	summary: string;
	plan: TaskPlan;
}

export type OrchestratorEvent =
	| { type: "task_start"; taskId: string; description: string }
	| { type: "task_complete"; taskId: string; success: boolean; elapsed: number }
	| { type: "task_skipped"; taskId: string; reason: string }
	| { type: "plan_complete"; summary: string };

// ---------------------------------------------------------------------------
// Topological Sort (Kahn's algorithm)
// ---------------------------------------------------------------------------

/**
 * Returns task IDs in a valid topological execution order.
 *
 * @throws Error on circular dependencies or references to non-existent tasks.
 */
export function topologicalSort(tasks: SubTask[], dependencies: Map<string, string[]>): string[] {
	const ids = new Set(tasks.map((t) => t.id));

	// Validate all dependency references exist
	for (const [taskId, deps] of dependencies) {
		if (!ids.has(taskId)) {
			throw new Error(`Dependency map references unknown task "${taskId}"`);
		}
		for (const dep of deps) {
			if (!ids.has(dep)) {
				throw new Error(`Task "${taskId}" depends on unknown task "${dep}"`);
			}
			if (dep === taskId) {
				throw new Error(`Task "${taskId}" has a self-dependency`);
			}
		}
	}

	// Build in-degree map and adjacency list
	const inDegree = new Map<string, number>();
	const dependents = new Map<string, string[]>(); // parent → children that depend on it

	for (const id of ids) {
		inDegree.set(id, 0);
		dependents.set(id, []);
	}

	for (const [taskId, deps] of dependencies) {
		inDegree.set(taskId, deps.length);
		for (const dep of deps) {
			dependents.get(dep)?.push(taskId);
		}
	}

	// Seed queue with zero-in-degree nodes (preserve original order for stability)
	const queue: string[] = tasks.filter((t) => (inDegree.get(t.id) ?? 0) === 0).map((t) => t.id);

	const sorted: string[] = [];

	while (queue.length > 0) {
		const current = queue.shift()!;
		sorted.push(current);

		for (const child of dependents.get(current) ?? []) {
			const deg = (inDegree.get(child) ?? 1) - 1;
			inDegree.set(child, deg);
			if (deg === 0) {
				queue.push(child);
			}
		}
	}

	if (sorted.length !== ids.size) {
		throw new Error("Circular dependency detected among tasks");
	}

	return sorted;
}

// ---------------------------------------------------------------------------
// Session Pool  (C5.2)
// ---------------------------------------------------------------------------

export class SessionPool {
	private maxConcurrency: number;
	private active = 0;
	private waiting: Array<() => void> = [];
	private bridge: ICopilotClient;
	private taintedSessions = new Set<string>();

	constructor(bridge: ICopilotClient, maxConcurrency = 3) {
		this.bridge = bridge;
		this.maxConcurrency = maxConcurrency;
	}

	async execute(request: CodingTaskRequest): Promise<CodingTaskResult> {
		await this.acquire();
		try {
			const result = await this.bridge.runTask(request);
			if (!result.success) {
				this.taintedSessions.add(result.sessionId);
			}
			return result;
		} finally {
			this.release();
		}
	}

	private async acquire(): Promise<void> {
		if (this.active < this.maxConcurrency) {
			this.active++;
			return;
		}
		await new Promise<void>((resolve) => {
			this.waiting.push(resolve);
		});
		this.active++;
	}

	private release(): void {
		this.active--;
		const next = this.waiting.shift();
		if (next) {
			next();
		}
	}

	async drain(): Promise<void> {
		while (this.active > 0) {
			await new Promise<void>((resolve) => setTimeout(resolve, 50));
		}
	}

	getActiveCount(): number {
		return this.active;
	}

	getTaintedCount(): number {
		return this.taintedSessions.size;
	}
}

// ---------------------------------------------------------------------------
// Task Orchestrator  (C5.1)
// ---------------------------------------------------------------------------

export class TaskOrchestrator {
	private bridge: ICopilotClient;
	private pool: SessionPool;
	private listeners: Array<(event: OrchestratorEvent) => void> = [];

	constructor(bridge: ICopilotClient, pool: SessionPool) {
		this.bridge = bridge;
		this.pool = pool;
	}

	/** Subscribe to orchestration events. Returns an unsubscribe function. */
	on(listener: (event: OrchestratorEvent) => void): () => void {
		this.listeners.push(listener);
		return () => {
			const idx = this.listeners.indexOf(listener);
			if (idx >= 0) {
				this.listeners.splice(idx, 1);
			}
		};
	}

	private emit(event: OrchestratorEvent): void {
		for (const listener of this.listeners) {
			listener(event);
		}
	}

	// -------------------------------------------------------------------
	// Plan
	// -------------------------------------------------------------------

	async planTasks(description: string): Promise<TaskPlan> {
		const planPrompt = `You are a task decomposition engine. Break the following coding task into ordered sub-tasks.

Task: ${description}

Respond with ONLY valid JSON in this exact format:
{
  "tasks": [
    { "id": "t1", "description": "...", "estimatedComplexity": "S|M|L" },
    { "id": "t2", "description": "...", "estimatedComplexity": "S|M|L" }
  ],
  "dependencies": { "t2": ["t1"] }
}

Rules:
- Each task should be a single, clearly-scoped coding step
- Use "S" for simple changes, "M" for moderate, "L" for complex
- Dependencies mean a task must wait for listed tasks to complete
- Independent tasks will run in parallel
- Minimize dependencies to maximize parallelism`;

		try {
			const result = await this.bridge.runTask({
				prompt: planPrompt,
				timeout: 30_000,
			});
			return this.parsePlanResponse(result.content, description);
		} catch {
			return this.fallbackPlan(description);
		}
	}

	/** Visible for testing */
	parsePlanResponse(content: string, originalDescription: string): TaskPlan {
		// Strip markdown code fences if present
		let json = content.trim();
		const fenceMatch = json.match(/```(?:json)?\s*\n?([\s\S]*?)```/);
		if (fenceMatch) {
			json = fenceMatch[1].trim();
		}

		try {
			const parsed = JSON.parse(json) as {
				tasks?: Array<{
					id?: string;
					description?: string;
					estimatedComplexity?: string;
					workingDir?: string;
					toolArgs?: Record<string, unknown>;
				}>;
				dependencies?: Record<string, string[]>;
			};

			if (!Array.isArray(parsed.tasks) || parsed.tasks.length === 0) {
				return this.fallbackPlan(originalDescription);
			}

			const validComplexities = new Set(["S", "M", "L"]);
			const tasks: SubTask[] = parsed.tasks.map((t, i) => ({
				id: t.id ?? `t${i + 1}`,
				description: t.description ?? originalDescription,
				workingDir: t.workingDir,
				estimatedComplexity: validComplexities.has(t.estimatedComplexity ?? "")
					? (t.estimatedComplexity as "S" | "M" | "L")
					: "M",
				toolArgs: t.toolArgs,
			}));

			const taskIds = new Set(tasks.map((t) => t.id));
			const dependencies = new Map<string, string[]>();

			if (parsed.dependencies && typeof parsed.dependencies === "object") {
				for (const [taskId, deps] of Object.entries(parsed.dependencies)) {
					if (!taskIds.has(taskId)) continue;
					const validDeps = (Array.isArray(deps) ? deps : []).filter(
						(d) => typeof d === "string" && taskIds.has(d) && d !== taskId,
					);
					if (validDeps.length > 0) {
						dependencies.set(taskId, validDeps);
					}
				}
			}

			// Validate no cycles
			try {
				topologicalSort(tasks, dependencies);
			} catch {
				return this.fallbackPlan(originalDescription);
			}

			return { tasks, dependencies };
		} catch {
			return this.fallbackPlan(originalDescription);
		}
	}

	private fallbackPlan(description: string): TaskPlan {
		return {
			tasks: [
				{
					id: "t1",
					description,
					estimatedComplexity: "M",
				},
			],
			dependencies: new Map(),
		};
	}

	// -------------------------------------------------------------------
	// Execute
	// -------------------------------------------------------------------

	async executePlan(plan: TaskPlan): Promise<OrchestratedResult> {
		const startTime = performance.now();
		const results: SubTaskResult[] = [];
		const completed = new Set<string>();
		const failed = new Set<string>();
		const skipped = new Set<string>();

		if (plan.tasks.length === 0) {
			const summary = "0/0 tasks succeeded, 0 skipped, 0 failed";
			this.emit({ type: "plan_complete", summary });
			return {
				tasks: [],
				totalElapsed: Math.round(performance.now() - startTime),
				summary,
				plan,
			};
		}

		const taskMap = new Map(plan.tasks.map((t) => [t.id, t]));

		// Pre-compute transitive dependents for failure propagation
		const directDependents = new Map<string, string[]>();
		for (const t of plan.tasks) {
			directDependents.set(t.id, []);
		}
		for (const [taskId, deps] of plan.dependencies) {
			for (const dep of deps) {
				directDependents.get(dep)?.push(taskId);
			}
		}

		const getTransitiveDependents = (rootId: string): Set<string> => {
			const visited = new Set<string>();
			const queue = [rootId];
			while (queue.length > 0) {
				const current = queue.shift()!;
				for (const child of directDependents.get(current) ?? []) {
					if (!visited.has(child)) {
						visited.add(child);
						queue.push(child);
					}
				}
			}
			return visited;
		};

		// Execute in waves
		const remaining = new Set(plan.tasks.map((t) => t.id));

		while (remaining.size > 0) {
			// Find executable: all deps satisfied, not skipped/failed
			const executable: string[] = [];
			for (const id of remaining) {
				if (skipped.has(id) || failed.has(id)) continue;
				const deps = plan.dependencies.get(id) ?? [];
				if (deps.every((d) => completed.has(d))) {
					executable.push(id);
				}
			}

			if (executable.length === 0) {
				// No progress possible — skip all remaining
				for (const id of remaining) {
					if (!skipped.has(id) && !failed.has(id)) {
						skipped.add(id);
						const reason = "No executable path (unresolved dependencies)";
						this.emit({ type: "task_skipped", taskId: id, reason });
						results.push({
							id,
							result: this.emptyResult(id),
							status: "skipped",
							skipReason: reason,
						});
					}
				}
				break;
			}

			// Run wave in parallel
			const wavePromises = executable.map(async (id) => {
				const task = taskMap.get(id)!;
				this.emit({
					type: "task_start",
					taskId: id,
					description: task.description,
				});

				const taskStart = performance.now();
				try {
					const result = await this.pool.execute({
						prompt: task.description,
						workingDir: task.workingDir,
						sessionId: crypto.randomUUID(),
						timeout: 60_000,
					});
					const elapsed = Math.round(performance.now() - taskStart);

					if (result.success) {
						completed.add(id);
						this.emit({
							type: "task_complete",
							taskId: id,
							success: true,
							elapsed,
						});
						results.push({ id, result, status: "success" });
					} else {
						failed.add(id);
						this.emit({
							type: "task_complete",
							taskId: id,
							success: false,
							elapsed,
						});
						results.push({ id, result, status: "failed" });

						// Skip transitive dependents
						for (const depId of getTransitiveDependents(id)) {
							if (!completed.has(depId) && !failed.has(depId)) {
								skipped.add(depId);
								const reason = `Dependency "${id}" failed`;
								this.emit({
									type: "task_skipped",
									taskId: depId,
									reason,
								});
								results.push({
									id: depId,
									result: this.emptyResult(depId),
									status: "skipped",
									skipReason: reason,
								});
								remaining.delete(depId);
							}
						}
					}
				} catch (err) {
					const elapsed = Math.round(performance.now() - taskStart);
					failed.add(id);
					this.emit({
						type: "task_complete",
						taskId: id,
						success: false,
						elapsed,
					});
					results.push({
						id,
						result: {
							success: false,
							content: "",
							toolCalls: [],
							errors: [err instanceof Error ? err.message : String(err)],
							sessionId: "",
							elapsed,
						},
						status: "failed",
					});

					for (const depId of getTransitiveDependents(id)) {
						if (!completed.has(depId) && !failed.has(depId)) {
							skipped.add(depId);
							const reason = `Dependency "${id}" failed`;
							this.emit({
								type: "task_skipped",
								taskId: depId,
								reason,
							});
							results.push({
								id: depId,
								result: this.emptyResult(depId),
								status: "skipped",
								skipReason: reason,
							});
							remaining.delete(depId);
						}
					}
				}

				remaining.delete(id);
			});

			await Promise.all(wavePromises);
		}

		const successCount = results.filter((r) => r.status === "success").length;
		const failedCount = results.filter((r) => r.status === "failed").length;
		const skippedCount = results.filter((r) => r.status === "skipped").length;
		const total = plan.tasks.length;
		const summary = `${successCount}/${total} tasks succeeded, ${skippedCount} skipped, ${failedCount} failed`;

		this.emit({ type: "plan_complete", summary });

		return {
			tasks: results,
			totalElapsed: Math.round(performance.now() - startTime),
			summary,
			plan,
		};
	}

	private emptyResult(sessionId: string): CodingTaskResult {
		return {
			success: false,
			content: "",
			toolCalls: [],
			errors: [],
			sessionId,
			elapsed: 0,
		};
	}
}
