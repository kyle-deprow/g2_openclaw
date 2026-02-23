#!/usr/bin/env tsx
/**
 * Connectivity validation script for the CopilotBridge.
 *
 * Usage:
 *   npx tsx scripts/validate-connection.ts
 *   npx tsx scripts/validate-connection.ts --byok
 */

import { CopilotBridge } from "../src/client.js";
import { loadConfig } from "../src/config.js";

const isByok = process.argv.includes("--byok");

function step(n: number, msg: string): void {
	console.log(`\n[Step ${n}] ${msg}`);
}

function ok(msg: string): void {
	console.log(`  ✓ ${msg}`);
}

function fail(msg: string): void {
	console.error(`  ✗ ${msg}`);
}

async function main(): Promise<void> {
	try {
		// Step 1
		step(1, "Loading config...");
		const config = loadConfig();
		ok(
			`Config loaded (logLevel=${config.logLevel}, host=${config.openclawHost}:${config.openclawPort})`,
		);

		if (isByok) {
			if (!config.byokProvider) {
				fail("--byok flag set but COPILOT_BYOK_PROVIDER is not configured");
				process.exit(1);
			}
			ok(`BYOK provider: ${config.byokProvider}`);
			if (config.byokBaseUrl) ok(`BYOK base URL: ${config.byokBaseUrl}`);
			if (config.byokModel) ok(`BYOK model: ${config.byokModel}`);
		}

		// Step 2
		step(2, "Creating CopilotBridge...");
		const bridge = new CopilotBridge(config);
		ok("Bridge created");

		// Step 3
		step(3, "Triggering connection (ping)...");
		const ready = await bridge.isReady();
		if (ready) {
			ok("Ping successful");
		} else {
			fail("Ping failed — SDK agent may not be running");
			process.exit(1);
		}

		// Step 4
		step(4, "Checking auth status...");
		const status = await bridge.getStatus();
		ok(`Connected: ${status.connected}, Auth method: ${status.authMethod}`);

		if (!status.connected) {
			fail("Not connected");
			process.exit(1);
		}

		// Step 5
		step(5, "Creating session...");
		ok("Session will be created as part of the test prompt");

		// Step 6
		step(6, "Sending test prompt...");
		const result = await bridge.runTask({
			prompt: 'Reply with exactly "connection validated" and nothing else.',
			timeout: 30_000,
		});

		if (result.success) {
			// Step 7
			step(7, `Response: ${result.content.slice(0, 200)}`);
			ok(`Completed in ${result.elapsed}ms`);
		} else {
			step(7, "Response: FAILED");
			fail(`Errors: ${result.errors.join(", ")}`);
		}

		// Step 8
		step(8, "Stopping client...");
		await bridge.stop();
		ok("Client stopped");

		if (result.success) {
			console.log("\n=== Connection validation PASSED ===\n");
			process.exit(0);
		} else {
			console.log("\n=== Connection validation FAILED ===\n");
			process.exit(1);
		}
	} catch (err) {
		fail(`Unexpected error: ${err instanceof Error ? err.message : String(err)}`);
		if (err instanceof Error && err.stack) {
			console.error(err.stack);
		}
		process.exit(1);
	}
}

main();
