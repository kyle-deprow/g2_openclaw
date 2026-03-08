import { CopilotBridge } from "./client.js";
import { loadConfig } from "./config.js";
import type { CodingTaskResult } from "./types.js";

// --- Local type definitions (since @openclaw/sdk may not be installable) ---

interface OpenClawToolDef {
	name: string;
	description: string;
	parameters: {
		type: "object";
		properties: Record<string, { type: string; description: string }>;
		required?: string[];
	};
	execute(args: Record<string, any>): Promise<{ result: string }>;
}

export interface OpenClawPlugin {
	name: string;
	version: string;
	tools?: OpenClawToolDef[];
	hooks?: Record<string, (...args: any[]) => Promise<void>>;
	onLoad?(api: any): Promise<void>;
}

// --- Shared bridge singleton ---

let sharedBridge: CopilotBridge | null = null;
let bridgeInitPromise: Promise<CopilotBridge> | null = null;

async function getBridge(): Promise<CopilotBridge> {
	if (sharedBridge) return sharedBridge;
	if (!bridgeInitPromise) {
		bridgeInitPromise = (async () => {
			const config = loadConfig();
			const bridge = new CopilotBridge(config);
			await bridge.ensureReady();
			sharedBridge = bridge;
			return bridge;
		})();
	}
	try {
		return await bridgeInitPromise;
	} catch (err) {
		bridgeInitPromise = null; // allow retry on failure
		throw err;
	}
}

/** Reset the singleton — exposed for testing only. */
export async function _resetBridge(): Promise<void> {
	const bridge = sharedBridge;
	sharedBridge = null;
	bridgeInitPromise = null;
	if (bridge) {
		await bridge.stop();
	}
}

// --- Result formatting helpers ---

function formatToolCalls(toolCalls: CodingTaskResult["toolCalls"]): string {
	if (toolCalls.length === 0) return "";
	const lines = toolCalls.map((tc) => {
		const argsStr = JSON.stringify(tc.args);
		return `- \`${tc.tool}\`(${argsStr}) → ${tc.result}`;
	});
	return `\n\n## Tool Calls\n${lines.join("\n")}`;
}

function formatResult(result: CodingTaskResult): string {
	const toolCalls = formatToolCalls(result.toolCalls);
	const errorsSection =
		result.errors.length > 0
			? `\n\n## Errors\n${result.errors.map((e) => `- ${e}`).join("\n")}`
			: "";
	return `## Result\n${result.content}${toolCalls}${errorsSection}\n\n## Stats\n- Success: ${result.success}\n- Elapsed: ${(result.elapsed / 1000).toFixed(1)}s\n- Session: ${result.sessionId}`;
}

function formatError(err: unknown): string {
	const message = err instanceof Error ? err.message : String(err);
	return `## Error\n\n${message}`;
}

// --- Tool definition ---

const copilotTool: OpenClawToolDef = {
	name: "copilot",
	description:
		"Execute a coding task via GitHub Copilot. OpenClaw constructs the full prompt including any persona directives, task context, and instructions. Copilot handles planning, implementation, review, and fixes autonomously.",
	parameters: {
		type: "object",
		properties: {
			prompt: {
				type: "string",
				description: "The full task prompt. Include all context, constraints, and instructions.",
			},
			persona: {
				type: "string",
				description:
					"System-level instructions appended to Copilot's system prompt. Applied when a new session starts for this workingDir.",
			},
			workingDir: {
				type: "string",
				description: "Project name or absolute path. Bare names resolve to ~/repos/<name>.",
			},
			timeout: {
				type: "number",
				description: "Timeout in milliseconds (default 120000)",
			},
		},
		required: ["prompt", "workingDir"],
	},
	async execute(args: Record<string, any>): Promise<{ result: string }> {
		if (typeof args.prompt !== "string" || args.prompt.length === 0) {
			return { result: "## Error\n\n`prompt` must be a non-empty string" };
		}
		if (args.prompt.length > 500_000) {
			return { result: "## Error\n\n`prompt` exceeds maximum length (500000 chars)" };
		}
		if (typeof args.workingDir !== "string" || args.workingDir.length === 0) {
			return {
				result:
					"## Error\n\n`workingDir` is required. Pass a project name (e.g. 'my-api') or absolute path.",
			};
		}
		if (args.timeout !== undefined && (typeof args.timeout !== "number" || args.timeout < 0)) {
			return { result: "## Error\n\n`timeout` must be a non-negative number" };
		}
		if (
			args.persona !== undefined &&
			(typeof args.persona !== "string" || args.persona.length > 50_000)
		) {
			return {
				result: "## Error\n\n`persona` must be a string (max 50000 chars)",
			};
		}
		try {
			const bridge = await getBridge();
			const resolvedDir = await bridge.resolveWorkingDir(args.workingDir as string);

			const result = await bridge.runTask({
				prompt: args.prompt as string,
				workingDir: resolvedDir,
				timeout: (args.timeout as number | undefined) ?? 120_000,
				sessionId: resolvedDir,
				systemMessage: (args.persona as string | undefined) || undefined,
			});
			return { result: formatResult(result) };
		} catch (err) {
			return { result: formatError(err) };
		}
	},
};

// --- Plugin default export ---

const plugin: OpenClawPlugin = {
	name: "copilot-bridge",
	version: "1.0.0",
	tools: [copilotTool],
	async onLoad() {
		console.log("[copilot-bridge] Plugin loaded");
	},
};

export default plugin;
