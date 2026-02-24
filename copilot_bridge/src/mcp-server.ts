#!/usr/bin/env node
/**
 * Copilot Bridge MCP Server
 *
 * Exposes GitHub Copilot SDK capabilities as MCP tools for OpenClaw
 * to consume via stdio transport.
 */
import nodePath from "node:path";
import { fileURLToPath } from "node:url";
import { CopilotClient } from "@github/copilot-sdk";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { CopilotBridge } from "./client.js";
import { loadConfig } from "./config.js";
import { DEFAULT_POLICY, createHooks } from "./hooks.js";
import type { HookConfig } from "./hooks.js";
import type { ICopilotSession } from "./interfaces.js";
import type { CodingTaskResult } from "./types.js";

// ─── Path validation ────────────────────────────────────────────────────────

function validateMcpPath(filePath: string): { valid: true } | { valid: false; reason: string } {
	if (filePath.includes("\0")) {
		return { valid: false, reason: "Null bytes are not allowed" };
	}
	if (nodePath.isAbsolute(filePath)) {
		return { valid: false, reason: "Absolute paths are not allowed" };
	}
	const normalized = nodePath.normalize(filePath);
	if (normalized.startsWith("..") || normalized.includes(`..${nodePath.sep}`)) {
		return { valid: false, reason: "Path traversal is not allowed" };
	}
	return { valid: true };
}

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
let session: ICopilotSession | null = null;
let sdkClient: InstanceType<typeof CopilotClient> | null = null;
let initPromise: Promise<void> | null = null;

export interface InitializedState {
	bridge: CopilotBridge;
	session: ICopilotSession;
}

export async function ensureInitialized(): Promise<InitializedState> {
	if (bridge && session) return { bridge, session };

	if (!initPromise) {
		initPromise = (async () => {
			log("info", "Initializing Copilot Bridge...");
			const config = loadConfig();

			const hookConfig: HookConfig = {
				auditLogDir:
					config.auditLogDir ??
				nodePath.join(nodePath.dirname(fileURLToPath(import.meta.url)), "..", ".copilot-bridge", "audit"),
				policy: config.permissionPolicy ?? DEFAULT_POLICY,
				projectContext: config.projectContext ?? "",
				maxRetries: config.maxRetries ?? 3,
			};

			if (!config.githubToken) {
				throw new Error(
					"COPILOT_GITHUB_TOKEN is required. Set it in .env or as an environment variable.",
				);
			}

			let localBridge: CopilotBridge | null = null;
			let localClient: InstanceType<typeof CopilotClient> | null = null;

			try {
				localBridge = new CopilotBridge(config);
				await localBridge.ensureReady();

				localClient = new CopilotClient({
					githubToken: config.githubToken,
					cliPath: config.cliPath,
					logLevel: config.logLevel,
					autoRestart: true,
				});

				session = (await localClient.createSession({
					hooks: createHooks(hookConfig) as unknown as Record<string, unknown>,
					streaming: false,
				})) as unknown as ICopilotSession;

				bridge = localBridge;
				sdkClient = localClient;
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
				if (localClient) {
					try {
						await localClient.stop();
					} catch {
						/* best effort */
					}
				}
				bridge = null;
				session = null;
				sdkClient = null;
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

	return { bridge: bridge!, session: session! };
}

export async function shutdown(): Promise<void> {
	log("info", "Shutting down...");

	const currentSession = session;
	const currentBridge = bridge;
	const currentClient = sdkClient;

	session = null;
	bridge = null;
	sdkClient = null;
	initPromise = null;

	if (currentSession) {
		try {
			await currentSession.destroy();
		} catch (err) {
			log("warn", `Error destroying session: ${err instanceof Error ? err.message : String(err)}`);
		}
	}

	if (currentBridge) {
		try {
			await currentBridge.stop();
		} catch (err) {
			log("warn", `Error stopping bridge: ${err instanceof Error ? err.message : String(err)}`);
		}
	}

	if (currentClient) {
		try {
			await currentClient.stop();
		} catch (err) {
			log("warn", `Error stopping client: ${err instanceof Error ? err.message : String(err)}`);
		}
	}

	log("info", "Shutdown complete");
}

/** Reset all internal state — for testing only. */
export function _resetState(): void {
	bridge = null;
	session = null;
	sdkClient = null;
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

	// ── copilot_read_file ──────────────────────────────────────────────────

	server.tool(
		"copilot_read_file",
		"Read a file from the workspace via GitHub Copilot",
		{
			path: z.string().max(1000).describe("File path to read"),
			_depth: z.number().optional().describe("Call depth for cycle detection"),
		},
		async ({ path, _depth }) => {
			const depthError = checkDepth(_depth);
			if (depthError) return depthError;
			const pathCheck = validateMcpPath(path);
			if (!pathCheck.valid) {
				return { content: [{ type: "text" as const, text: `Error: ${pathCheck.reason}` }], isError: true };
			}
			try {
				const { session } = await ensureInitialized();
				const result: unknown = await session.rpc["workspace.readFile"]({ path });
				const text = typeof result === "string" ? result : JSON.stringify(result, null, 2);
				return { content: [{ type: "text" as const, text }] };
			} catch (err) {
				const message = err instanceof Error ? err.message : String(err);
				return {
					content: [{ type: "text" as const, text: `Error reading file: ${message}` }],
					isError: true,
				};
			}
		},
	);

	// ── copilot_create_file ────────────────────────────────────────────────

	server.tool(
		"copilot_create_file",
		"Create a file in the workspace via GitHub Copilot",
		{
			path: z.string().max(1000).describe("File path to create"),
			content: z.string().max(500_000).describe("File content to write"),
			_depth: z.number().optional().describe("Call depth for cycle detection"),
		},
		async ({ path, content, _depth }) => {
			const depthError = checkDepth(_depth);
			if (depthError) return depthError;
			const pathCheck = validateMcpPath(path);
			if (!pathCheck.valid) {
				return { content: [{ type: "text" as const, text: `Error: ${pathCheck.reason}` }], isError: true };
			}
			try {
				const { session } = await ensureInitialized();
				await session.rpc["workspace.createFile"]({ path, content });
				return {
					content: [{ type: "text" as const, text: `File created: ${path}` }],
				};
			} catch (err) {
				const message = err instanceof Error ? err.message : String(err);
				return {
					content: [{ type: "text" as const, text: `Error creating file: ${message}` }],
					isError: true,
				};
			}
		},
	);

	// ── copilot_list_files ─────────────────────────────────────────────────

	server.tool(
		"copilot_list_files",
		"List files in a workspace directory via GitHub Copilot",
		{
			directory: z.string().max(1000).optional().describe("Directory to list, defaults to workspace root"),
			_depth: z.number().optional().describe("Call depth for cycle detection"),
		},
		async ({ directory, _depth }) => {
			const depthError = checkDepth(_depth);
			if (depthError) return depthError;
			if (directory) {
				const dirCheck = validateMcpPath(directory);
				if (!dirCheck.valid) {
					return { content: [{ type: "text" as const, text: `Error: ${dirCheck.reason}` }], isError: true };
				}
			}
			try {
				const { session } = await ensureInitialized();
				const result: unknown = await session.rpc["workspace.listFiles"]({
					directory,
				});
				const text = typeof result === "string" ? result : JSON.stringify(result, null, 2);
				return { content: [{ type: "text" as const, text }] };
			} catch (err) {
				const message = err instanceof Error ? err.message : String(err);
				return {
					content: [{ type: "text" as const, text: `Error listing files: ${message}` }],
					isError: true,
				};
			}
		},
	);

	// ── copilot_code_task ──────────────────────────────────────────────────

	server.tool(
		"copilot_code_task",
		"Agent-mediated coding task. Results are non-deterministic. " +
			"Sends a natural language prompt to GitHub Copilot and returns the response.",
		{
			prompt: z.string().max(500_000).describe("Natural language coding task prompt"),
			workingDir: z.string().max(1000).optional().describe("Working directory for the task"),
			timeout: z.number().optional().default(120_000).describe("Timeout in milliseconds"),
			_depth: z.number().optional().describe("Call depth for cycle detection"),
		},
		async ({ prompt, workingDir, timeout, _depth }) => {
			const depthError = checkDepth(_depth);
			if (depthError) return depthError;
			const release = await acquireMutex();
			try {
				const { bridge } = await ensureInitialized();
				const result = await bridge.runTask({ prompt, workingDir, timeout });
				return {
					content: [{ type: "text" as const, text: formatResult(result) }],
				};
			} catch (err) {
				const message = err instanceof Error ? err.message : String(err);
				return {
					content: [{ type: "text" as const, text: `Error: ${message}` }],
					isError: true,
				};
			} finally {
				release();
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
