#!/usr/bin/env tsx
/**
 * C5.0 — Concurrency Spike
 *
 * Diagnostic script that measures whether the Copilot SDK processes
 * concurrent sessions in parallel or serializes them.
 *
 * Usage:  npx tsx scripts/concurrency-spike.ts
 */

import { CopilotBridge } from "../src/client.js";
import { loadConfig } from "../src/config.js";
import type { CodingTaskResult } from "../src/types.js";

const SIMPLE_PROMPT = "Reply with exactly: PING";
const CONCURRENCY = 3;
const PARALLEL_THRESHOLD = 1.5;

interface SpikeResult {
	singleMs: number;
	parallelMs: number;
	ratio: number;
	verdict: "parallel" | "serialized" | "unknown";
	error?: string;
}

async function measureSingle(bridge: CopilotBridge): Promise<number> {
	const start = performance.now();
	await bridge.runTask({ prompt: SIMPLE_PROMPT, timeout: 30_000 });
	return performance.now() - start;
}

async function measureParallel(bridge: CopilotBridge, n: number): Promise<number> {
	const start = performance.now();
	const tasks: Promise<CodingTaskResult>[] = [];
	for (let i = 0; i < n; i++) {
		tasks.push(
			bridge.runTask({
				prompt: SIMPLE_PROMPT,
				sessionId: crypto.randomUUID(),
				timeout: 30_000,
			}),
		);
	}
	await Promise.all(tasks);
	return performance.now() - start;
}

async function run(): Promise<void> {
	let result: SpikeResult;

	try {
		const config = loadConfig();
		const bridge = new CopilotBridge(config);
		await bridge.ensureReady();

		const singleMs = await measureSingle(bridge);
		const parallelMs = await measureParallel(bridge, CONCURRENCY);
		const ratio = parallelMs / singleMs;
		const verdict = ratio < PARALLEL_THRESHOLD ? "parallel" : "serialized";

		result = {
			singleMs: Math.round(singleMs),
			parallelMs: Math.round(parallelMs),
			ratio: Math.round(ratio * 100) / 100,
			verdict,
		};

		await bridge.stop();
	} catch (err) {
		const message = err instanceof Error ? err.message : String(err);
		result = {
			singleMs: 0,
			parallelMs: 0,
			ratio: 0,
			verdict: "unknown",
			error: message,
		};
	}

	console.log(JSON.stringify(result, null, 2));
	console.log(`\nRESULT: ${result.verdict}`);
}

// Only run when executed directly
const isMain =
	typeof process !== "undefined" &&
	process.argv[1] &&
	import.meta.url.endsWith(process.argv[1].replace(/\\/g, "/"));

if (isMain) {
	run();
}
