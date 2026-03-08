#!/usr/bin/env node
/**
 * Copilot Bridge MCP Server
 *
 * Exposes GitHub Copilot SDK capabilities as MCP tools for OpenClaw
 * to consume via stdio transport.
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { CopilotBridge } from "./client.js";
import { loadConfig } from "./config.js";
import type { CodingTaskResult } from "./types.js";

// ─── Cycle detection ────────────────────────────────────────────────────────

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

// ─── Logging (stderr — stdout is reserved for MCP protocol) ────────────────

function log(level: string, message: string, data?: Record<string, unknown>): void {
	const timestamp = new Date().toISOString();
	const prefix = `[${timestamp}] [mcp-server] [${level.toUpperCase()}]`;
	if (data) {
		console.error(`${prefix} ${message}`, JSON.stringify(data));
	} else {
		console.error(`${prefix} ${message}`);
	}
}

// ─── Lazy-init singleton state ──────────────────────────────────────────────

let bridge: CopilotBridge | null = null;
let initPromise: Promise<void> | null = null;

export interface InitializedState {
	bridge: CopilotBridge;
}

export async function ensureInitialized(): Promise<InitializedState> {
	if (bridge) return { bridge };

	if (!initPromise) {
		initPromise = (async () => {
			log("info", "Initializing Copilot Bridge...");
			const config = loadConfig();

			if (!config.githubToken) {
				throw new Error(
					"COPILOT_GITHUB_TOKEN is required. Set it in .env or as an environment variable.",
				);
			}

			let localBridge: CopilotBridge | null = null;

			try {
				localBridge = new CopilotBridge(config);
				await localBridge.ensureReady();

				bridge = localBridge;
				log("info", "Initialization complete");
			} catch (err) {
				// Clean up partially-created resources
				if (localBridge) {
					try {
						await localBridge.stop();
					} catch {
						/* best effort */
					}
				}
				bridge = null;
				throw err;
			}
		})();
	}

	try {
		await initPromise;
	} catch (err) {
		initPromise = null; // Allow retry on failure
		throw err;
	}

	return { bridge: bridge! };
}

export async function shutdown(): Promise<void> {
	log("info", "Shutting down...");

	const currentBridge = bridge;

	bridge = null;
	initPromise = null;

	if (currentBridge) {
		try {
			await currentBridge.stop();
		} catch (err) {
			log("warn", `Error stopping bridge: ${err instanceof Error ? err.message : String(err)}`);
		}
	}

	log("info", "Shutdown complete");
}

/** Reset all internal state — for testing only. */
export function _resetState(): void {
	bridge = null;
	initPromise = null;
}

// ─── Promise-based mutex for serializing agent-mediated calls ───────────────

let mutexChain: Promise<void> = Promise.resolve();

export function acquireMutex(): Promise<() => void> {
	let release!: () => void;
	const gate = new Promise<void>((resolve) => {
		release = resolve;
	});
	const ticket = mutexChain.then(() => release);
	mutexChain = gate;
	return ticket;
}

/** Reset mutex state — for testing only. */
export function _resetMutex(): void {
	mutexChain = Promise.resolve();
}

// ─── Result formatting ─────────────────────────────────────────────────────

export function formatResult(result: CodingTaskResult): string {
	const parts: string[] = [result.content];

	if (result.toolCalls.length > 0) {
		const calls = result.toolCalls
			.map((tc) => `- ${tc.tool}(${JSON.stringify(tc.args)}) → ${tc.result}`)
			.join("\n");
		parts.push(`\nTool Calls:\n${calls}`);
	}

	if (result.errors.length > 0) {
		const errors = result.errors.map((e) => `- ${e}`).join("\n");
		parts.push(`\nErrors:\n${errors}`);
	}

	parts.push(
		`\nSuccess: ${result.success} | Elapsed: ${(result.elapsed / 1000).toFixed(1)}s` +
			` | Session: ${result.sessionId}`,
	);

	return parts.join("\n");
}

// ─── MCP Server factory ────────────────────────────────────────────────────

export function createServer(): McpServer {
	const server = new McpServer({ name: "copilot-bridge", version: "1.0.0" });

	// ── copilot ────────────────────────────────────────────────────────────

	server.tool(
		"copilot",
		"Execute a coding task via GitHub Copilot. OpenClaw constructs the full prompt including any persona directives, task context, and instructions. Copilot handles planning, implementation, review, and fixes autonomously.",
		{
			prompt: z.string().max(500_000).describe("The full task prompt. Include all context, constraints, and instructions."),
			persona: z.string().max(50_000).optional().describe("System-level instructions appended to Copilot's system prompt. Applied when a new session starts for this workingDir. Use for role, constraints, output format directives."),
			workingDir: z.string().max(1000).describe("Project name or absolute path. Bare names resolve to ~/repos/<name>."),
			timeout: z.number().optional().default(120_000).describe("Timeout in milliseconds"),
			_depth: z.number().optional().describe("Call depth for cycle detection"),
		},
		async ({ prompt, persona, workingDir, timeout, _depth }) => {
			const depthError = checkDepth(_depth);
			if (depthError) return depthError;
			const release = await acquireMutex();
			try {
				const { bridge } = await ensureInitialized();
				const resolvedDir = await bridge.resolveWorkingDir(workingDir);
				const result = await bridge.runTask({ prompt, workingDir: resolvedDir, timeout, sessionId: resolvedDir, systemMessage: persona || undefined });
				return { content: [{ type: "text" as const, text: formatResult(result) }] };
			} catch (err) {
				const message = err instanceof Error ? err.message : String(err);
				return { content: [{ type: "text" as const, text: `Error: ${message}` }], isError: true };
			} finally {
				release();
			}
		},
	);

	// ── copilot_sessions ─────────────────────────────────────────────────────

	server.tool(
		"copilot_sessions",
		"List or destroy Copilot coding sessions. Sessions are keyed by project directory and retain full conversation context. Destroy a session to start fresh in that project.",
		{
			action: z.enum(["list", "destroy"]).describe("'list' shows all active sessions. 'destroy' removes a session (next copilot call to that project starts fresh)."),
			project: z.string().max(1000).optional().describe("Project name or path to destroy. Required for action='destroy'. Omit with action='list'."),
			_depth: z.number().optional().describe("Call depth for cycle detection"),
		},
		async ({ action, project, _depth }) => {
			const depthError = checkDepth(_depth);
			if (depthError) return depthError;
			try {
				const { bridge } = await ensureInitialized();
				if (action === "list") {
					const sessions = bridge.listSessions();
					if (sessions.length === 0) {
						return { content: [{ type: "text" as const, text: "No active Copilot sessions." }] };
					}
					const lines = sessions.map(s =>
						`- ${s.workingDir ?? "unknown"} (messages: ${s.messageCount}, created: ${s.createdAt})`
					);
					return { content: [{ type: "text" as const, text: `Active sessions:\n${lines.join("\n")}` }] };
				} else {
					// action === "destroy"
					if (!project) {
						return { content: [{ type: "text" as const, text: "Error: 'project' is required for action='destroy'." }], isError: true };
					}
					const resolved = await bridge.resolveWorkingDir(project);
					const destroyed = await bridge.destroySession(resolved);
					if (destroyed) {
						return { content: [{ type: "text" as const, text: `Session for ${project} destroyed. Next copilot call will start a fresh session.` }] };
					}
					return { content: [{ type: "text" as const, text: `No active session for ${project}.` }] };
				}
			} catch (err) {
				const message = err instanceof Error ? err.message : String(err);
				return { content: [{ type: "text" as const, text: `Error: ${message}` }], isError: true };
			}
		},
	);

	return server;
}

// ─── Main ───────────────────────────────────────────────────────────────────

async function main(): Promise<void> {
	const server = createServer();

	process.on("SIGINT", async () => {
		await shutdown();
		process.exit(0);
	});

	process.on("SIGTERM", async () => {
		await shutdown();
		process.exit(0);
	});

	const transport = new StdioServerTransport();
	await server.connect(transport);
	log("info", "Copilot Bridge MCP server running on stdio");
}

// Only run main when executed directly (not imported for testing)
if (import.meta.url === `file://${process.argv[1]}`) {
	main().catch((err) => {
		console.error("Fatal error:", err);
		process.exit(1);
	});
}
