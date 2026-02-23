#!/usr/bin/env tsx
/**
 * Register (or unregister) the OpenClaw Memory MCP server configuration.
 *
 * Usage:
 *   npx tsx scripts/register-mcp.ts              # register
 *   npx tsx scripts/register-mcp.ts --unregister  # unregister
 *   npx tsx scripts/register-mcp.ts --help        # show help
 *
 * Merges the MCP server entry from openclaw-mcp-config.json into
 * ~/.openclaw/config so that the Gateway discovers and spawns the server.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const __scriptDir = import.meta.dirname ?? path.dirname(new URL(import.meta.url).pathname);
const CONFIG_JSON_PATH = path.resolve(__scriptDir, "..", "openclaw-mcp-config.json");
const OPENCLAW_CONFIG_DIR = path.join(os.homedir(), ".openclaw");
const OPENCLAW_CONFIG_PATH = path.join(OPENCLAW_CONFIG_DIR, "config");
const SERVER_KEY = "copilot";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function info(msg: string): void {
	console.log(`\x1b[36mℹ\x1b[0m ${msg}`);
}

function success(msg: string): void {
	console.log(`\x1b[32m✅\x1b[0m ${msg}`);
}

function error(msg: string): void {
	console.error(`\x1b[31m❌\x1b[0m ${msg}`);
}

interface OpenClawConfig {
	mcp?: {
		servers?: Record<string, unknown>;
	};
	[key: string]: unknown;
}

function readOpenClawConfig(): OpenClawConfig {
	try {
		const raw = fs.readFileSync(OPENCLAW_CONFIG_PATH, "utf-8");
		try {
			return JSON.parse(raw) as OpenClawConfig;
		} catch (parseErr) {
			throw new Error(
				`Failed to parse ${OPENCLAW_CONFIG_PATH}: ${parseErr instanceof Error ? parseErr.message : String(parseErr)}`,
			);
		}
	} catch (err) {
		if ((err as NodeJS.ErrnoException).code === "ENOENT") {
			return {};
		}
		throw err;
	}
}

function writeOpenClawConfig(config: OpenClawConfig): void {
	fs.mkdirSync(OPENCLAW_CONFIG_DIR, { recursive: true });
	const tmpPath = `${OPENCLAW_CONFIG_PATH}.tmp.${process.pid}`;
	fs.writeFileSync(tmpPath, `${JSON.stringify(config, null, "\t")}\n`, "utf-8");
	fs.renameSync(tmpPath, OPENCLAW_CONFIG_PATH);
}

function readMcpConfigJson(): OpenClawConfig {
	if (!fs.existsSync(CONFIG_JSON_PATH)) {
		error(`MCP config not found at ${CONFIG_JSON_PATH}`);
		process.exit(1);
	}
	const raw = fs.readFileSync(CONFIG_JSON_PATH, "utf-8");
	try {
		return JSON.parse(raw) as OpenClawConfig;
	} catch (parseErr) {
		error(
			`Failed to parse ${CONFIG_JSON_PATH}: ${parseErr instanceof Error ? parseErr.message : String(parseErr)}`,
		);
		process.exit(1);
		throw parseErr; // unreachable, satisfies TypeScript
	}
}

// ---------------------------------------------------------------------------
// Register
// ---------------------------------------------------------------------------

function register(): void {
	const mcpConfig = readMcpConfigJson();
	const serverEntry = mcpConfig.mcp?.servers?.[SERVER_KEY];

	if (!serverEntry) {
		error(`No "mcp.servers.${SERVER_KEY}" entry found in openclaw-mcp-config.json`);
		process.exit(1);
	}

	const existing = readOpenClawConfig();

	// Deep merge: ensure mcp.servers exists
	if (!existing.mcp) {
		existing.mcp = {};
	}
	if (!existing.mcp.servers) {
		existing.mcp.servers = {};
	}

	// Check if already registered with same config
	const currentEntry = existing.mcp.servers[SERVER_KEY];
	if (currentEntry && JSON.stringify(currentEntry) === JSON.stringify(serverEntry)) {
		success("MCP server already registered with matching configuration.");
		return;
	}

	if (currentEntry) {
		info(`Updating existing "${SERVER_KEY}" MCP server entry.`);
	}

	existing.mcp.servers[SERVER_KEY] = serverEntry;
	writeOpenClawConfig(existing);

	success("MCP server registered.");
	console.log("");
	info("Restart the Gateway to pick up changes:");
	console.log("  openclaw gateway restart");
	console.log("");
	info("The following MCP tools will be available:");
	console.log("  - copilot_read_file");
	console.log("  - copilot_create_file");
	console.log("  - copilot_list_files");
	console.log("  - copilot_code_task");
}

// ---------------------------------------------------------------------------
// Unregister
// ---------------------------------------------------------------------------

function unregister(): void {
	let existing: OpenClawConfig;
	try {
		existing = readOpenClawConfig();
	} catch {
		info("Nothing to unregister — no OpenClaw config found.");
		return;
	}

	if (!existing.mcp?.servers?.[SERVER_KEY]) {
		info(`Nothing to unregister — no "${SERVER_KEY}" MCP server entry found.`);
		return;
	}

	Reflect.deleteProperty(existing.mcp.servers, SERVER_KEY);

	// Clean up empty structures
	if (Object.keys(existing.mcp.servers).length === 0) {
		existing.mcp.servers = undefined;
	}
	if (existing.mcp && Object.keys(existing.mcp).length === 0) {
		existing.mcp = undefined;
	}

	writeOpenClawConfig(existing);
	success(`MCP server "${SERVER_KEY}" unregistered.`);
	console.log("");
	info("Restart the Gateway: openclaw gateway restart");
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

function main(): void {
	const args = process.argv.slice(2);

	if (args.includes("--help") || args.includes("-h")) {
		console.log("Usage:");
		console.log("  npx tsx scripts/register-mcp.ts              # register MCP server");
		console.log("  npx tsx scripts/register-mcp.ts --unregister  # remove registration");
		console.log("");
		console.log("Merges the copilot MCP server entry from openclaw-mcp-config.json");
		console.log("into ~/.openclaw/config for Gateway discovery.");
		return;
	}

	if (args.includes("--unregister")) {
		unregister();
	} else {
		register();
	}
}

main();
