#!/usr/bin/env node
/**
 * OpenClaw Memory MCP Server (Read-Only)
 *
 * Exposes OpenClaw's memory/context as read-only MCP tools for Copilot SDK
 * sessions to consume. No agent-triggering tools — all tools are data lookups.
 *
 * Tools:
 *   - openclaw_memory_search: Vector similarity search over memory entries
 *   - openclaw_memory_read:   Read specific memory files
 *   - openclaw_user_prefs:    Read USER.md preferences
 */

import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { loadConfig } from "./config.js";
import type { BridgeConfig } from "./config.js";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const MAX_CALL_DEPTH = 3;

export function checkDepth(
	depth: number | undefined,
): { content: Array<{ type: "text"; text: string }>; isError: true } | null {
	const d = depth ?? 0;
	if (d >= MAX_CALL_DEPTH) {
		return {
			content: [
				{
					type: "text" as const,
					text: `Maximum call depth exceeded (cycle detected). Depth: ${d}, max: ${MAX_CALL_DEPTH}`,
				},
			],
			isError: true,
		};
	}
	return null;
}

const MEMORY_DIR = path.join(os.homedir(), ".openclaw", "memory");
const USER_MD_PATH = path.join(os.homedir(), ".openclaw", "USER.md");

// ---------------------------------------------------------------------------
// Path Validation
// ---------------------------------------------------------------------------

/**
 * Validate that a resolved path stays within the allowed base directory.
 * Prevents directory traversal attacks.
 */
export async function validateMemoryPath(file: string, baseDir: string): Promise<string> {
	// Reject obviously malicious patterns before resolving
	if (file.includes("\0")) {
		throw new Error("Invalid file path: null bytes not allowed");
	}

	const resolved = path.resolve(baseDir, file);
	const normalizedBase = path.resolve(baseDir);

	if (!resolved.startsWith(normalizedBase + path.sep) && resolved !== normalizedBase) {
		throw new Error(`Path traversal detected: "${file}" escapes memory directory`);
	}

	// Follow symlinks and re-check containment
	try {
		const realResolved = await fs.realpath(resolved);
		const realBase = await fs.realpath(baseDir);
		if (!realResolved.startsWith(realBase + path.sep) && realResolved !== realBase) {
			throw new Error(`Path traversal detected: "${file}" escapes memory directory (via symlink)`);
		}
		return realResolved;
	} catch (err) {
		// If file doesn't exist yet, realpath throws ENOENT — return the lexically-validated path
		if ((err as NodeJS.ErrnoException).code === "ENOENT") {
			return resolved;
		}
		throw err;
	}
}

// ---------------------------------------------------------------------------
// Memory Search (fallback: file-based grep)
// ---------------------------------------------------------------------------

async function searchMemoryFiles(query: string, limit: number): Promise<string[]> {
	const results: Array<{ file: string; line: string; score: number }> = [];
	const lowerQuery = query.toLowerCase();
	const queryTerms = lowerQuery.split(/\s+/).filter(Boolean);

	let files: string[];
	try {
		files = await fs.readdir(MEMORY_DIR);
	} catch {
		return [`No memory directory found at ${MEMORY_DIR}`];
	}

	const mdFiles = files.filter((f) => f.endsWith(".md"));

	for (const file of mdFiles) {
		try {
			const filePath = path.join(MEMORY_DIR, file);
			const content = await fs.readFile(filePath, "utf-8");
			const lines = content.split("\n");

			for (const line of lines) {
				if (line.trim().length === 0) continue;
				const lowerLine = line.toLowerCase();
				const matchCount = queryTerms.filter((term) => lowerLine.includes(term)).length;
				if (matchCount > 0) {
					results.push({
						file,
						line: line.trim(),
						score: matchCount / queryTerms.length,
					});
				}
			}
		} catch {
			// Skip unreadable files
		}
	}

	// Sort by score descending, take top N
	results.sort((a, b) => b.score - a.score);
	const top = results.slice(0, limit);

	if (top.length === 0) {
		return [`No memory entries found matching "${query}"`];
	}

	return top.map((r) => `[${r.file}] (score: ${r.score.toFixed(2)}) ${r.line}`);
}

// ---------------------------------------------------------------------------
// Tool Handlers
// ---------------------------------------------------------------------------

export async function handleMemorySearch(args: {
	query: string;
	limit: number;
	_depth?: number;
}): Promise<{ content: Array<{ type: "text"; text: string }>; isError?: boolean }> {
	const depthError = checkDepth(args._depth);
	if (depthError) return depthError;
	try {
		const results = await searchMemoryFiles(args.query, args.limit);
		return {
			content: [{ type: "text", text: results.join("\n") }],
		};
	} catch (err) {
		return {
			content: [
				{
					type: "text",
					text: `Memory search failed: ${err instanceof Error ? err.message : String(err)}`,
				},
			],
			isError: true,
		};
	}
}

export async function handleMemoryRead(args: {
	file?: string;
	_depth?: number;
}): Promise<{ content: Array<{ type: "text"; text: string }>; isError?: boolean }> {
	const depthError = checkDepth(args._depth);
	if (depthError) return depthError;
	const file = args.file ?? "MEMORY.md";

	try {
		const resolvedPath = await validateMemoryPath(file, MEMORY_DIR);
		const content = await fs.readFile(resolvedPath, "utf-8");
		return {
			content: [{ type: "text", text: content }],
		};
	} catch (err) {
		const message = err instanceof Error ? err.message : String(err);
		if (message.includes("traversal") || message.includes("null bytes")) {
			return {
				content: [{ type: "text", text: `Access denied: ${message}` }],
				isError: true,
			};
		}
		return {
			content: [
				{
					type: "text",
					text: `Failed to read memory file "${file}": ${message}`,
				},
			],
			isError: true,
		};
	}
}

export async function handleUserPrefs(args?: {
	_depth?: number;
}): Promise<{
	content: Array<{ type: "text"; text: string }>;
	isError?: boolean;
}> {
	const depthError = checkDepth(args?._depth);
	if (depthError) return depthError;
	try {
		const content = await fs.readFile(USER_MD_PATH, "utf-8");
		return {
			content: [{ type: "text", text: content }],
		};
	} catch (err) {
		const message = err instanceof Error ? err.message : String(err);
		if (message.includes("ENOENT")) {
			return {
				content: [
					{
						type: "text",
						text:
							"USER.md not found at ~/.openclaw/USER.md. " +
							"Create this file to store user preferences that OpenClaw can reference.",
					},
				],
				isError: true,
			};
		}
		return {
			content: [{ type: "text", text: `Failed to read USER.md: ${message}` }],
			isError: true,
		};
	}
}

// ---------------------------------------------------------------------------
// Server Factory
// ---------------------------------------------------------------------------

export function createServer(_config?: BridgeConfig): McpServer {
	const server = new McpServer({
		name: "openclaw-memory",
		version: "1.0.0",
	});

	// Tool 1: openclaw_memory_search
	server.tool(
		"openclaw_memory_search",
		"Search OpenClaw agent memory using keyword matching. Returns relevant memory entries.",
		{
			query: z.string().describe("Search query for memory"),
			limit: z.number().optional().default(5),
			_depth: z.number().optional().describe("Call depth for cycle detection"),
		},
		async (args) => handleMemorySearch(args),
	);

	// Tool 2: openclaw_memory_read
	server.tool(
		"openclaw_memory_read",
		"Read OpenClaw memory files. Defaults to MEMORY.md (consolidated memory).",
		{
			file: z.string().optional().describe("Memory file to read, defaults to MEMORY.md"),
			_depth: z.number().optional().describe("Call depth for cycle detection"),
		},
		async (args) => handleMemoryRead(args),
	);

	// Tool 3: openclaw_user_prefs
	server.tool(
		"openclaw_user_prefs",
		"Read user preferences from OpenClaw's USER.md file.",
		{
			_depth: z.number().optional().describe("Call depth for cycle detection"),
		},
		async (args) => handleUserPrefs(args),
	);

	return server;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
	const config = loadConfig();
	const server = createServer(config);
	const transport = new StdioServerTransport();

	// Clean shutdown
	const shutdown = () => {
		process.exit(0);
	};
	process.on("SIGTERM", shutdown);
	process.on("SIGINT", shutdown);

	await server.connect(transport);
}

// Only run main when executed directly (not imported for testing)
const isDirectExecution =
	typeof process !== "undefined" &&
	process.argv[1] &&
	(process.argv[1].endsWith("mcp-openclaw.js") || process.argv[1].endsWith("mcp-openclaw.ts"));

if (isDirectExecution) {
	main().catch((err) => {
		console.error("Fatal:", err);
		process.exit(1);
	});
}
