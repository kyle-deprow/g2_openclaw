export interface ProviderConfig {
	type: "openai" | "azure" | "anthropic" | "ollama";
	baseUrl?: string;
	apiKey?: string;
	model?: string;
	wireApi?: "completions" | "responses";
}

export interface CodingTaskRequest {
	prompt: string;
	workingDir?: string;
	model?: string;
	provider?: ProviderConfig;
	tools?: string[];
	sessionId?: string;
	timeout?: number;
	streaming?: boolean;
}

export interface CodingTaskResult {
	success: boolean;
	content: string;
	toolCalls: ToolCallRecord[];
	errors: string[];
	sessionId: string;
	elapsed: number;
}

export interface ToolCallRecord {
	tool: string;
	args: Record<string, unknown>;
	result: string;
	timestamp: number;
}

export interface StreamingDelta {
	type: "text" | "tool_start" | "tool_end" | "error" | "done";
	content: string;
	tool?: string;
}

export class BridgeError extends Error {
	constructor(
		message: string,
		public readonly code: string,
		public readonly details?: Record<string, unknown>,
		public readonly recoverable: boolean = false,
	) {
		super(message);
		this.name = "BridgeError";
	}
}
