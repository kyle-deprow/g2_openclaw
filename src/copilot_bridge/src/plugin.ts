import { CopilotBridge } from "./client.js";
import { loadConfig } from "./config.js";
import type { CodingTaskResult, StreamingDelta } from "./types.js";

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

function summariseArgs(args: Record<string, unknown> | undefined): string {
	if (!args) return "{}";
	const keys = Object.keys(args);
	if (keys.length === 0) return "{}";
	return keys.join(", ");
}

// --- Tool definitions ---

const copilotCodeTool: OpenClawToolDef = {
	name: "copilot_code",
	description:
		"Delegate a coding task to GitHub Copilot. Returns the result as markdown including tool calls and stats.",
	parameters: {
		type: "object",
		properties: {
			task: { type: "string", description: "The coding task prompt to send to Copilot" },
			workingDir: {
				type: "string",
				description: "Working directory for the task (optional)",
			},
			model: { type: "string", description: "Model to use (optional)" },
			timeout: {
				type: "number",
				description: "Timeout in milliseconds (default 120000)",
			},
		},
		required: ["task"],
	},
	async execute(args: Record<string, any>): Promise<{ result: string }> {
		try {
			const bridge = await getBridge();
			const result = await bridge.runTask({
				prompt: args.task as string,
				workingDir: args.workingDir as string | undefined,
				model: args.model as string | undefined,
				timeout: (args.timeout as number | undefined) ?? 120_000,
			});
			return { result: formatResult(result) };
		} catch (err) {
			return { result: formatError(err) };
		}
	},
};

const copilotCodeVerboseTool: OpenClawToolDef = {
	name: "copilot_code_verbose",
	description:
		"Delegate a coding task to GitHub Copilot with verbose step-by-step logging of every tool call.",
	parameters: {
		type: "object",
		properties: {
			task: { type: "string", description: "The coding task prompt to send to Copilot" },
			workingDir: {
				type: "string",
				description: "Working directory for the task (optional)",
			},
			model: { type: "string", description: "Model to use (optional)" },
			timeout: {
				type: "number",
				description: "Timeout in milliseconds (default 120000)",
			},
		},
		required: ["task"],
	},
	async execute(args: Record<string, any>): Promise<{ result: string }> {
		try {
			const bridge = await getBridge();
			const startTime = Date.now();

			const logEntries: string[] = [];
			let stepNum = 0;
			let aggregatedText = "";

			const stream = bridge.runTaskStreaming({
				prompt: args.task as string,
				workingDir: args.workingDir as string | undefined,
				model: args.model as string | undefined,
				timeout: (args.timeout as number | undefined) ?? 120_000,
				streaming: true,
			});

			let currentTool: string | undefined;
			let currentToolArgs: string | undefined;

			for await (const delta of stream) {
				switch (delta.type) {
					case "text":
						aggregatedText += delta.content;
						break;
					case "tool_start":
						currentTool = delta.tool ?? "unknown";
						currentToolArgs = delta.content || undefined;
						break;
					case "tool_end": {
						stepNum++;
						const toolName = delta.tool ?? currentTool ?? "unknown";
						let argsSummary = "";
						if (currentToolArgs) {
							try {
								argsSummary = summariseArgs(JSON.parse(currentToolArgs));
							} catch {
								argsSummary = currentToolArgs;
							}
						}
						const resultLen = delta.content?.length ?? 0;
						logEntries.push(
							`${stepNum}. 🔧 Called \`${toolName}\`(${argsSummary}) — ${resultLen} chars result`,
						);
						currentTool = undefined;
						currentToolArgs = undefined;
						break;
					}
					case "error":
						stepNum++;
						logEntries.push(`${stepNum}. ❌ Error: ${delta.content}`);
						break;
					case "done": {
						const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
						stepNum++;
						logEntries.push(`${stepNum}. ✅ Complete (${elapsed}s)`);
						break;
					}
				}
			}

			const logSection = `## Execution Log\n${logEntries.join("\n")}`;
			const resultSection = `\n\n## Result\n${aggregatedText}`;
			return { result: `${logSection}${resultSection}` };
		} catch (err) {
			return { result: formatError(err) };
		}
	},
};

// --- Plugin default export ---

const plugin: OpenClawPlugin = {
	name: "copilot-bridge",
	version: "1.0.0",
	tools: [copilotCodeTool, copilotCodeVerboseTool],
	async onLoad() {
		console.log("[copilot-bridge] Plugin loaded");
	},
};

export default plugin;
