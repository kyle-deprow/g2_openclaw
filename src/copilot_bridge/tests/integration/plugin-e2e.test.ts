import { afterAll, beforeAll, describe, expect, it } from "vitest";

const INTEGRATION = !!process.env.COPILOT_INTEGRATION && !!process.env.OPENCLAW_INTEGRATION;

describe.skipIf(!INTEGRATION)("Plugin E2E Integration Tests", () => {
	let plugin: typeof import("../../src/plugin.js").default;
	let resetBridge: typeof import("../../src/plugin.js")._resetBridge;

	beforeAll(async () => {
		const mod = await import("../../src/plugin.js");
		plugin = mod.default;
		resetBridge = mod._resetBridge;
	});

	afterAll(async () => {
		await resetBridge();
	});

	function findTool(name: string) {
		return plugin.tools?.find((t) => t.name === name);
	}

	it("copilot_code: write a Python reverse function", async () => {
		const tool = findTool("copilot_code")!;
		const { result } = await tool.execute({
			task: "Write a Python function that reverses a string",
			timeout: 60_000,
		});

		expect(result).toContain("## Result");
		// LLM should produce a Python function — check for def keyword (not inside another word)
		expect(result).toMatch(/\bdef\b/);
	}, 90_000);

	it("copilot_code_verbose: returns step-by-step log", async () => {
		const tool = findTool("copilot_code_verbose")!;
		const { result } = await tool.execute({
			task: "Write a Python function that adds two numbers",
			timeout: 60_000,
		});

		expect(result).toContain("## Execution Log");
		expect(result).toContain("## Result");
		// Verify at least one numbered step appears in the log
		expect(result).toMatch(/\d+\.\s/);
		expect(result).toContain("✅ Complete");
	}, 90_000);

	it("copilot_code: timeout produces error in result, not exception", async () => {
		const tool = findTool("copilot_code")!;
		const { result } = await tool.execute({
			task: "Write a very long and detailed essay about the history of computing. At least 50000 words.",
			timeout: 2000, // Short enough to trigger timeout, long enough for bridge init
		});

		// Should not throw — result should contain error info or partial output
		expect(typeof result).toBe("string");
		expect(result.length).toBeGreaterThan(0);
		// Should indicate some kind of result structure
		expect(result).toMatch(/## (Result|Error)/);
	}, 30_000);

	it("copilot_code: refactor inline code", async () => {
		const tool = findTool("copilot_code")!;
		const { result } = await tool.execute({
			task: "Refactor this Python code to use list comprehension:\n\nresult = []\nfor i in range(10):\n    if i % 2 == 0:\n        result.append(i * 2)",
			timeout: 60_000,
		});

		expect(result).toContain("## Result");
		// Should contain list comprehension syntax
		expect(result).toMatch(/\[.*for.*in.*\]/s);
	}, 90_000);
});
