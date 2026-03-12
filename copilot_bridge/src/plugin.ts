import { Type } from "@sinclair/typebox";
// OpenClawPluginApi is provided at runtime by the OpenClaw daemon plugin loader.
// We declare a minimal interface here to avoid a hard dependency on the SDK package.
interface OpenClawPluginApi {
	logger: { info: (msg: string) => void; warn: (msg: string) => void; error: (msg: string) => void };
	registerTool: (tool: { name: string; label: string; description: string; parameters: unknown; execute: (toolCallId: string, params: Record<string, unknown>) => Promise<unknown> }) => void;
}
import { CopilotBridge } from "./client.js";
import { loadConfig } from "./config.js";
import type { CodingTaskResult } from "./types.js";

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

function jsonResult(payload: unknown) {
	return {
		content: [{ type: "text" as const, text: JSON.stringify(payload, null, 2) }],
		details: payload,
	};
}

// --- Parameter schemas ---

const CopilotToolSchema = Type.Object({
	prompt: Type.String({ description: "The full task prompt. Include all context, constraints, and instructions." }),
	workingDir: Type.String({ description: "Project name or absolute path. Bare names resolve to ~/repos/<name>." }),
	persona: Type.Optional(Type.String({ description: "System-level instructions appended to Copilot's system prompt. Applied when a new session starts for this workingDir." })),
	timeout: Type.Optional(Type.Number({ description: "Timeout in milliseconds (default 900000). Set 0 for no timeout." })),
});

const CopilotSessionsToolSchema = Type.Object({
	action: Type.Union([Type.Literal("list"), Type.Literal("destroy")], { description: "'list' shows all active sessions. 'destroy' removes a session." }),
	project: Type.Optional(Type.String({ description: "Project name or path to destroy. Required for action='destroy'." })),
});

// --- Plugin registration ---

export default function register(api: OpenClawPluginApi) {
	api.registerTool({
		name: "copilot",
		label: "Copilot",
		description:
			"Execute a coding task via GitHub Copilot. OpenClaw constructs the full prompt including any persona directives, task context, and instructions. Copilot handles planning, implementation, review, and fixes autonomously.",
		parameters: CopilotToolSchema,
		async execute(_toolCallId, params) {
			const prompt = typeof params?.prompt === "string" ? params.prompt : "";
			const workingDir = typeof params?.workingDir === "string" ? params.workingDir : "";
			const persona = typeof params?.persona === "string" ? params.persona : undefined;
			const timeout = typeof params?.timeout === "number" ? params.timeout : 900_000;

			if (!prompt) return jsonResult({ error: "`prompt` must be a non-empty string" });
			if (prompt.length > 500_000) return jsonResult({ error: "`prompt` exceeds maximum length (500000 chars)" });
			if (!workingDir) return jsonResult({ error: "`workingDir` is required. Pass a project name (e.g. 'my-api') or absolute path." });
			if (timeout < 0) return jsonResult({ error: "`timeout` must be a non-negative number" });
			if (persona !== undefined && persona.length > 50_000) return jsonResult({ error: "`persona` must be a string (max 50000 chars)" });

			try {
				const bridge = await getBridge();
				const resolvedDir = await bridge.resolveWorkingDir(workingDir);
				const result = await bridge.runTask({
					prompt,
					workingDir: resolvedDir,
					timeout,
					sessionId: resolvedDir,
					systemMessage: persona || undefined,
				});
				return jsonResult({ text: formatResult(result), success: result.success });
			} catch (err) {
				return jsonResult({ error: formatError(err) });
			}
		},
	});

	api.registerTool({
		name: "copilot_sessions",
		label: "Copilot Sessions",
		description:
			"List or destroy Copilot coding sessions. Sessions are keyed by project directory and retain full conversation context. Destroy a session to start fresh in that project.",
		parameters: CopilotSessionsToolSchema,
		async execute(_toolCallId, params) {
			const action = params?.action as string;
			if (action !== "list" && action !== "destroy") {
				return jsonResult({ error: "`action` must be 'list' or 'destroy'" });
			}
			try {
				const bridge = await getBridge();
				if (action === "list") {
					const sessions = bridge.listSessions();
					return jsonResult({ sessions });
				}
				const project = typeof params?.project === "string" ? params.project : "";
				if (!project) return jsonResult({ error: "`project` is required for action='destroy'." });
				const resolved = await bridge.resolveWorkingDir(project);
				const destroyed = await bridge.destroySession(resolved);
				return jsonResult({ destroyed, project });
			} catch (err) {
				return jsonResult({ error: formatError(err) });
			}
		},
	});

	api.logger.info("copilot-bridge: registered copilot + copilot_sessions tools");
}
