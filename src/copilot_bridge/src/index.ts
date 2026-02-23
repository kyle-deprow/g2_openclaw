// Barrel exports for copilot-bridge

export { loadConfig } from "./config.js";
export type { BridgeConfig } from "./config.js";

export { CopilotBridge } from "./client.js";
export type { SessionMetadata } from "./client.js";

export { TaskOrchestrator, SessionPool, topologicalSort } from "./orchestrator.js";
export type {
	SubTask,
	TaskPlan,
	SubTaskResult,
	OrchestratedResult,
	OrchestratorEvent,
} from "./orchestrator.js";

export type {
	ICopilotClient,
	ICopilotSession,
	IPermissionHandler,
	IPermissionRequest,
	IPermissionResponse,
	IProviderConfig,
} from "./interfaces.js";

export { BridgeError } from "./types.js";
export type {
	CodingTaskRequest,
	CodingTaskResult,
	ProviderConfig,
	StreamingDelta,
	ToolCallRecord,
} from "./types.js";

export { default as plugin } from "./plugin.js";
export type { OpenClawPlugin } from "./plugin.js";

export {
	createHooks,
	AuditLogger,
	evaluatePermission,
	redactSecrets,
	DEFAULT_POLICY,
} from "./hooks.js";
export type {
	HookConfig,
	PermissionPolicy,
	SessionHooks,
	AuditEntry,
	PreToolUseInput,
	PreToolUseResult,
	PostToolUseInput,
	PostToolUseResult,
	PromptInput,
	PromptResult,
	SessionStartInput,
	SessionStartResult,
	SessionEndInput,
	SessionEndResult,
	ErrorInput,
	ErrorResult,
} from "./hooks.js";
