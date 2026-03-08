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
