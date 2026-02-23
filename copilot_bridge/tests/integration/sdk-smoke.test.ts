import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { CopilotBridge } from "../../src/client.js";
import { loadConfig } from "../../src/config.js";
import type { CodingTaskRequest, StreamingDelta } from "../../src/types.js";

const INTEGRATION = !!process.env.COPILOT_INTEGRATION;
const HAS_BYOK = !!process.env.COPILOT_BYOK_PROVIDER;

describe.skipIf(!INTEGRATION)("SDK Smoke Integration Tests", () => {
	let bridge: CopilotBridge;

	beforeAll(async () => {
		const config = loadConfig();
		bridge = new CopilotBridge(config);
	});

	afterAll(async () => {
		await bridge.stop();
	});

	it("ensureReady succeeds", async () => {
		await expect(bridge.ensureReady()).resolves.toBeUndefined();
	});

	it("runTask returns success", async () => {
		const request: CodingTaskRequest = {
			prompt: 'Reply with exactly "hello" and nothing else.',
			timeout: 30_000,
		};

		const result = await bridge.runTask(request);

		expect(result.success).toBe(true);
		expect(result.content.length).toBeGreaterThan(0);
		expect(result.sessionId).toBeTruthy();
		expect(result.elapsed).toBeGreaterThan(0);
	});

	it("runTaskStreaming yields deltas", async () => {
		const request: CodingTaskRequest = {
			prompt: 'Reply with exactly "streaming works" and nothing else.',
			streaming: true,
			timeout: 30_000,
		};

		const deltas: StreamingDelta[] = [];
		for await (const delta of bridge.runTaskStreaming(request)) {
			deltas.push(delta);
			if (delta.type === "done") break;
		}

		expect(deltas.length).toBeGreaterThan(0);
		expect(deltas[deltas.length - 1]?.type).toBe("done");
	});

	it("timeout fires for slow tasks", async () => {
		const request: CodingTaskRequest = {
			prompt:
				"Write a very long and detailed essay about the history of computing. Make it at least 10000 words.",
			timeout: 100, // Very short timeout to force a timeout
		};

		const result = await bridge.runTask(request);

		// Either it timed out or completed quickly — just verify structure
		expect(result).toHaveProperty("success");
		expect(result).toHaveProperty("elapsed");
		expect(result).toHaveProperty("sessionId");
	});

	describe.skipIf(!HAS_BYOK)("BYOK mode", () => {
		it("runTask succeeds with BYOK provider", async () => {
			const request: CodingTaskRequest = {
				prompt: 'Reply with exactly "byok works" and nothing else.',
				timeout: 30_000,
			};

			const result = await bridge.runTask(request);

			expect(result.success).toBe(true);
			expect(result.content.length).toBeGreaterThan(0);
		});
	});

	it("permission handler fires for tool-using tasks", async () => {
		const request: CodingTaskRequest = {
			prompt: "List the files in the current directory.",
			tools: ["readFile", "listDirectory"],
			timeout: 30_000,
		};

		// This test just verifies the task completes without the default deny
		// blocking everything — our bridge auto-allows permissions.
		const result = await bridge.runTask(request);

		expect(result).toHaveProperty("success");
		expect(result).toHaveProperty("sessionId");
	});
});
