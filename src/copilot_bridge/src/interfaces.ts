import type {
	CodingTaskRequest,
	CodingTaskResult,
	ProviderConfig,
	StreamingDelta,
} from "./types.js";

export interface ICopilotClient {
	ensureReady(): Promise<void>;
	stop(): Promise<void>;
	isReady(): Promise<boolean>;
	getStatus(): Promise<{ connected: boolean; authMethod: string }>;
	runTask(request: CodingTaskRequest): Promise<CodingTaskResult>;
	runTaskStreaming(request: CodingTaskRequest): AsyncGenerator<StreamingDelta>;
}

export interface ICopilotSession {
	sendAndWait(
		params: { prompt: string },
		timeout?: number,
	): Promise<{ data: { content: string } } | undefined>;
	on(eventType: string, callback: (event: any) => void): () => void;
	on(callback: (event: any) => void): () => void;
	destroy(): Promise<void>;
	rpc: Record<string, (args?: any) => Promise<any>>;
	getMessages(): Promise<any[]>;
}

export interface IPermissionRequest {
	toolName: string;
	toolArgs: Record<string, unknown>;
	description?: string;
}

export interface IPermissionResponse {
	decision: "allow" | "deny" | "ask";
	reason?: string;
}

export type IPermissionHandler = (request: IPermissionRequest) => Promise<IPermissionResponse>;

export interface IProviderConfig {
	type?: "openai" | "azure" | "anthropic";
	wireApi?: "completions" | "responses";
	baseUrl: string;
	apiKey?: string;
	bearerToken?: string;
	azure?: { apiVersion?: string };
}
