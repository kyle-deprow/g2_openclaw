#!/usr/bin/env tsx
/**
 * Register (or unregister) the copilot-bridge plugin for OpenClaw discovery.
 *
 * Usage:
 *   npx tsx scripts/register-plugin.ts              # register
 *   npx tsx scripts/register-plugin.ts --unregister  # unregister
 *
 * Creates a symlink at ~/.openclaw/plugins/copilot-bridge/index.ts pointing
 * to the plugin source so that OpenClaw's jiti loader picks it up at runtime.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const PLUGIN_NAME = "copilot-bridge";
const PLUGIN_DIR = path.join(os.homedir(), ".openclaw", "plugins", PLUGIN_NAME);
const SYMLINK_PATH = path.join(PLUGIN_DIR, "index.ts");
const __scriptDir = import.meta.dirname ?? path.dirname(new URL(import.meta.url).pathname);
const PLUGIN_SOURCE = path.resolve(__scriptDir, "..", "src", "plugin.ts");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function info(msg: string): void {
	console.log(msg);
}

function error(msg: string): void {
	console.error(`❌ ${msg}`);
}

/** Return the symlink target if `p` is a symlink, or `null` otherwise. */
function readSymlink(p: string): string | null {
	try {
		return fs.readlinkSync(p);
	} catch {
		return null;
	}
}

/** Resolve a possibly-relative symlink target against its containing dir. */
function resolveSymlink(p: string): string | null {
	const target = readSymlink(p);
	if (target === null) return null;
	if (path.isAbsolute(target)) return target;
	return path.resolve(path.dirname(p), target);
}

// ---------------------------------------------------------------------------
// Register
// ---------------------------------------------------------------------------

function register(): void {
	// Ensure the plugin source file actually exists
	if (!fs.existsSync(PLUGIN_SOURCE)) {
		error(`Plugin source not found at ${PLUGIN_SOURCE}`);
		process.exit(1);
	}

	// Check if symlink already exists and points to the correct target
	const existingTarget = resolveSymlink(SYMLINK_PATH);
	if (existingTarget !== null) {
		if (fs.realpathSync(existingTarget) === fs.realpathSync(PLUGIN_SOURCE)) {
			info("✅ Already registered — symlink is correct.");
			return;
		}
		// Symlink exists but points elsewhere — remove it
		info(`⚠️  Existing symlink points to ${existingTarget} — replacing.`);
		try {
			fs.unlinkSync(SYMLINK_PATH);
		} catch (err) {
			error(`Failed to remove stale symlink: ${(err as Error).message}`);
			process.exit(1);
		}
	} else if (fs.existsSync(SYMLINK_PATH)) {
		// Path exists but is not a symlink (e.g. a regular file) — bail out
		error(`${SYMLINK_PATH} exists and is not a symlink. Please remove it manually.`);
		process.exit(1);
	}

	// Create the plugin directory tree
	try {
		fs.mkdirSync(PLUGIN_DIR, { recursive: true });
	} catch (err) {
		error(`Failed to create directory ${PLUGIN_DIR}: ${(err as Error).message}`);
		process.exit(1);
	}

	// Compute a relative path from the symlink's directory to the source
	const relativeTarget = path.relative(PLUGIN_DIR, PLUGIN_SOURCE);

	// Create the symlink
	try {
		fs.symlinkSync(relativeTarget, SYMLINK_PATH);
	} catch (err) {
		error(`Failed to create symlink: ${(err as Error).message}`);
		process.exit(1);
	}

	// Validate the symlink is readable
	try {
		fs.accessSync(SYMLINK_PATH, fs.constants.R_OK);
	} catch (err) {
		error(`Symlink created but not accessible: ${(err as Error).message}`);
		process.exit(1);
	}

	info(`✅ Plugin registered at ~/.openclaw/plugins/${PLUGIN_NAME}/index.ts`);
	info("");
	info("Next steps:");
	info("  1. Restart the Gateway:  openclaw gateway restart");
	info('  2. Verify tools:         openclaw agent --message "List your tools"');
	info("");
	info("Expected tools: copilot_code, copilot_code_verbose");
}

// ---------------------------------------------------------------------------
// Unregister
// ---------------------------------------------------------------------------

function unregister(): void {
	if (!fs.existsSync(SYMLINK_PATH) && !fs.existsSync(PLUGIN_DIR)) {
		info("Nothing to unregister — plugin is not registered.");
		return;
	}

	// Remove the symlink
	if (fs.existsSync(SYMLINK_PATH)) {
		try {
			fs.unlinkSync(SYMLINK_PATH);
			info("Removed symlink.");
		} catch (err) {
			error(`Failed to remove symlink: ${(err as Error).message}`);
			process.exit(1);
		}
	}

	// Remove the directory (only if empty)
	if (fs.existsSync(PLUGIN_DIR)) {
		try {
			fs.rmSync(PLUGIN_DIR, { recursive: false });
			info("Removed plugin directory.");
		} catch {
			info("Plugin directory not empty — leaving in place.");
		}
	}

	info(`✅ Plugin "${PLUGIN_NAME}" unregistered.`);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

function main(): void {
	const args = process.argv.slice(2);

	if (args.includes("--help") || args.includes("-h")) {
		info("Usage:");
		info("  npx tsx scripts/register-plugin.ts              # register plugin");
		info("  npx tsx scripts/register-plugin.ts --unregister  # remove registration");
		return;
	}

	if (args.includes("--unregister")) {
		unregister();
	} else {
		register();
	}
}

main();
