import { CopilotBridge } from "./client.js";
import { loadConfig } from "./config.js";
import { SessionPool, TaskOrchestrator } from "./orchestrator.js";
import type { OrchestratedResult, SubTaskResult, TaskPlan } from "./orchestrator.js";
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

function formatOrchestratedResult(result: OrchestratedResult): string {
	const planSection = `## Task Plan\n${result.plan.tasks.map((t) => `- **${t.id}**: ${t.description} [${t.estimatedComplexity}]`).join("\n")}`;

	const taskResults = result.tasks
		.map((tr: SubTaskResult) => {
			const icon = tr.status === "success" ? "✅" : tr.status === "failed" ? "❌" : "⏭️";
			const detail =
				tr.status === "skipped"
					? `Skipped: ${tr.skipReason ?? "dependency failed"}`
					: tr.result.success
						? `${tr.result.content.slice(0, 500)}${tr.result.content.length > 500 ? "..." : ""}`
						: `Error: ${tr.result.errors.join(", ")}`;
			return `### ${icon} ${tr.id}\n${detail}`;
		})
		.join("\n\n");

	const statsSection = `## Summary\n${result.summary}\n- Total elapsed: ${(result.totalElapsed / 1000).toFixed(1)}s`;

	return `${planSection}\n\n## Results\n${taskResults}\n\n${statsSection}`;
}

function summariseArgs(args: Record<string, unknown> | undefined): string {
	if (!args) return "{}";
	const keys = Object.keys(args);
	if (keys.length === 0) return "{}";
	return keys.join(", ");
}

// --- Tool definitions ---

const copilotCodeMessageTool: OpenClawToolDef = {
	name: "copilot_code_message",
	description:
		"Message into an existing Copilot session. If sessionId is omitted, messages into the most recent session. Use copilot_code_start to begin a new session.",
	parameters: {
		type: "object",
		properties: {
			task: { type: "string", description: "The message to send to the Copilot session" },
			timeout: {
				type: "number",
				description: "Timeout in milliseconds (default 120000)",
			},
			sessionId: {
				type: "string",
				description: "Session ID to continue a previous conversation. Omit to use the most recent session.",
			},
		},
		required: ["task"],
	},
	async execute(args: Record<string, any>): Promise<{ result: string }> {
		if (typeof args.task !== "string" || args.task.length === 0) {
			return { result: "## Error\n\n`task` must be a non-empty string" };
		}
		if (args.task.length > 50_000) {
			return { result: "## Error\n\n`task` exceeds maximum length (50000 chars)" };
		}
		if (args.timeout !== undefined && (typeof args.timeout !== "number" || args.timeout < 0)) {
			return { result: "## Error\n\n`timeout` must be a non-negative number" };
		}
		if (args.sessionId !== undefined && (typeof args.sessionId !== "string" || args.sessionId.length === 0 || args.sessionId.length > 200)) {
			return { result: "## Error\n\n`sessionId` must be a non-empty string (max 200 chars)" };
		}
		try {
			const bridge = await getBridge();
			let targetSessionId = args.sessionId as string | undefined;
			if (!targetSessionId) {
				const recent = await bridge.getMostRecentSession();
				if (!recent) {
					return { result: "## Error\n\nNo active sessions. Use copilot_code_start to begin a new session." };
				}
				targetSessionId = recent.sessionId;
			}
			const result = await bridge.resumeTask(targetSessionId, args.task as string, (args.timeout as number | undefined) ?? 120_000);
			return { result: formatResult(result) };
		} catch (err) {
			return { result: formatError(err) };
		}
	},
};

const copilotCodeVerboseTool: OpenClawToolDef = {
	name: "copilot_code_verbose",
	description:
		"Message into an existing Copilot session with verbose step-by-step logging. If sessionId is omitted, uses the most recent session. Use copilot_code_start to begin a new session.",
	parameters: {
		type: "object",
		properties: {
			task: { type: "string", description: "The message to send to the Copilot session" },
			timeout: {
				type: "number",
				description: "Timeout in milliseconds (default 120000)",
			},
			sessionId: {
				type: "string",
				description: "Session ID to continue a previous conversation. Omit to use the most recent session.",
			},
		},
		required: ["task"],
	},
	async execute(args: Record<string, any>): Promise<{ result: string }> {
		if (typeof args.task !== "string" || args.task.length === 0) {
			return { result: "## Error\n\n`task` must be a non-empty string" };
		}
		if (args.task.length > 50_000) {
			return { result: "## Error\n\n`task` exceeds maximum length (50000 chars)" };
		}
		if (args.timeout !== undefined && (typeof args.timeout !== "number" || args.timeout < 0)) {
			return { result: "## Error\n\n`timeout` must be a non-negative number" };
		}
		if (args.sessionId !== undefined && (typeof args.sessionId !== "string" || args.sessionId.length === 0 || args.sessionId.length > 200)) {
			return { result: "## Error\n\n`sessionId` must be a non-empty string (max 200 chars)" };
		}
		try {
			const bridge = await getBridge();

			// Resolve target session
			let targetSessionId = args.sessionId as string | undefined;
			if (!targetSessionId) {
				const recent = await bridge.getMostRecentSession();
				if (!recent) {
					return { result: "## Error\n\nNo active sessions. Use copilot_code_start to begin a new session." };
				}
				targetSessionId = recent.sessionId;
			}

			// Session resume uses non-streaming resumeTask
			const result = await bridge.resumeTask(targetSessionId, args.task as string, (args.timeout as number | undefined) ?? 120_000);
			return { result: `> Note: Session resume uses non-streaming mode; step-by-step log unavailable.\n\n${formatResult(result)}` };
		} catch (err) {
			return { result: formatError(err) };
		}
	},
};

const copilotOrchestrateTool: OpenClawToolDef = {
	name: "copilot_orchestrate",
	description:
		"Break a complex coding task into sub-tasks, execute them in parallel where possible, and return synthesized results. Use for multi-file or multi-step coding tasks.",
	parameters: {
		type: "object",
		properties: {
			task: {
				type: "string",
				description: "High-level coding task description to decompose and execute",
			},
			maxConcurrency: {
				type: "number",
				description: "Maximum parallel sessions (default 3)",
			},
			timeout: {
				type: "number",
				description: "Timeout per sub-task in milliseconds (default 120000)",
			},
		},
		required: ["task"],
	},
	async execute(args: Record<string, unknown>): Promise<{ result: string }> {
		if (typeof args.task !== "string" || (args.task as string).length === 0) {
			return { result: "## Error\n\n`task` must be a non-empty string" };
		}
		if ((args.task as string).length > 50_000) {
			return { result: "## Error\n\n`task` exceeds maximum length (50000 chars)" };
		}
		try {
			const bridge = await getBridge();
			const rawConcurrency = (typeof args.maxConcurrency === "number" && Number.isFinite(args.maxConcurrency)) ? args.maxConcurrency : 3;
			const maxConcurrency = Math.min(Math.max(1, rawConcurrency), 10);
			const pool = new SessionPool(bridge, maxConcurrency);

			const orchestrator = new TaskOrchestrator(bridge, pool);
			const plan = await orchestrator.planTasks(args.task as string);
			const result = await orchestrator.executePlan(plan);

			await pool.drain();

			return { result: formatOrchestratedResult(result) };
		} catch (err) {
			return { result: formatError(err) };
		}
	},
};

// --- Plugin default export ---

const copilotCodeStartTool: OpenClawToolDef = {
	name: "copilot_code_start",
	description: "Start a new Copilot coding session with an initial message. Creates a persistent session for follow-up messages via copilot_code_message.",
	parameters: {
		type: "object",
		properties: {
			task: { type: "string", description: "Initial message to start the session" },
			workingDir: { type: "string", description: "Project name or path. Bare names (e.g. 'my-api') resolve to ~/repos/my-api. Absolute paths used as-is. Created if missing." },
			model: { type: "string", description: "Model to use (optional)" },
			timeout: { type: "number", description: "Timeout in milliseconds (default 120000)" },
		},
		required: ["task", "workingDir"],
	},
	async execute(args: Record<string, any>): Promise<{ result: string }> {
		if (typeof args.task !== "string" || args.task.length === 0) {
			return { result: "## Error\n\n`task` must be a non-empty string" };
		}
		if (args.task.length > 50_000) {
			return { result: "## Error\n\n`task` exceeds maximum length (50000 chars)" };
		}
		if (typeof args.workingDir !== "string" || args.workingDir.length === 0) {
			return { result: "## Error\n\n`workingDir` is required. Pass a project name (e.g. 'my-api') or absolute path." };
		}
		if (args.timeout !== undefined && (typeof args.timeout !== "number" || args.timeout < 0)) {
			return { result: "## Error\n\n`timeout` must be a non-negative number" };
		}
		try {
			const bridge = await getBridge();
			const resolvedDir = await bridge.resolveWorkingDir(args.workingDir as string);
			const result = await bridge.runTask({
				prompt: args.task as string,
				workingDir: resolvedDir,
				model: args.model as string | undefined,
				timeout: (args.timeout as number | undefined) ?? 120_000,
				persistSession: true,
			});
			return { result: formatResult(result) };
		} catch (err) {
			return { result: formatError(err) };
		}
	},
};

const copilotCodeTranscriptTool: OpenClawToolDef = {
	name: "copilot_code_transcript",
	description: "Get the recent transcript of a Copilot session. Returns the last N messages (default 2).",
	parameters: {
		type: "object",
		properties: {
			sessionId: { type: "string", description: "Session ID to get transcript for. Omit to use the most recent session." },
			count: { type: "number", description: "Number of recent messages to return (default 2)" },
		},
	},
	async execute(args: Record<string, any>): Promise<{ result: string }> {
		if (args.sessionId !== undefined && (typeof args.sessionId !== "string" || args.sessionId.length === 0)) {
			return { result: "## Error\n\n`sessionId` must be a non-empty string" };
		}
		if (args.count !== undefined && (typeof args.count !== "number" || args.count < 1)) {
			return { result: "## Error\n\n`count` must be a positive number" };
		}
		try {
			const bridge = await getBridge();
			let targetSessionId = args.sessionId as string | undefined;
			if (!targetSessionId) {
				const recent = await bridge.getMostRecentSession();
				if (!recent) {
					return { result: "## Error\n\nNo active sessions. Use copilot_code_start to begin a new session." };
				}
				targetSessionId = recent.sessionId;
			}
			const count = (args.count as number | undefined) ?? 2;
			const messages = await bridge.getSessionTranscript(targetSessionId, count);
			if (messages.length === 0) {
				return { result: `No messages in session ${targetSessionId}.` };
			}
			const text = messages.map(m => `**${m.role}** (${m.timestamp}):\n${m.content}`).join("\n\n---\n\n");
			return { result: `## Transcript\n\n${text}` };
		} catch (err) {
			return { result: formatError(err) };
		}
	},
};

const copilotListSessionsTool: OpenClawToolDef = {
	name: "copilot_list_sessions",
	description: "List active Copilot sessions with their IDs, task summaries, and timestamps.",
	parameters: {
		type: "object",
		properties: {},
	},
	async execute(): Promise<{ result: string }> {
		try {
			const bridge = await getBridge();
			const sessions = await bridge.listPersistedSessions();
			if (sessions.length === 0) {
				return { result: "No active sessions." };
			}
			const lines = sessions.map(s =>
				`- **${s.sessionId}** | Task: ${s.task} | Last: ${s.lastActivity}`
			);
			return { result: `## Active Sessions\n${lines.join("\n")}` };
		} catch (err) {
			return { result: formatError(err) };
		}
	},
};

const copilotDestroySessionTool: OpenClawToolDef = {
	name: "copilot_destroy_session",
	description: "Destroy a Copilot session to end a conversation and free resources.",
	parameters: {
		type: "object",
		properties: {
			sessionId: { type: "string", description: "Session ID to destroy" },
		},
		required: ["sessionId"],
	},
	async execute(args: Record<string, any>): Promise<{ result: string }> {
		if (typeof args.sessionId !== "string" || args.sessionId.length === 0) {
			return { result: "## Error\n\n`sessionId` must be a non-empty string" };
		}
		try {
			const bridge = await getBridge();
			await bridge.destroyPersistedSession(args.sessionId);
			return { result: `Session ${args.sessionId} destroyed.` };
		} catch (err) {
			return { result: formatError(err) };
		}
	},
};

// --- Plugin default export ---

const plugin: OpenClawPlugin = {
	name: "copilot-bridge",
	version: "1.0.0",
	tools: [copilotCodeStartTool, copilotCodeMessageTool, copilotCodeVerboseTool, copilotOrchestrateTool, copilotCodeTranscriptTool, copilotListSessionsTool, copilotDestroySessionTool],
	async onLoad() {
		console.log("[copilot-bridge] Plugin loaded");
	},
};

export default plugin;
