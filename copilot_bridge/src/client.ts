import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { CopilotClient } from "@github/copilot-sdk";
import type { BridgeConfig } from "./config.js";
import { DEFAULT_POLICY, createHooks } from "./hooks.js";
import type { HookConfig } from "./hooks.js";
import type { ICopilotClient } from "./interfaces.js";
import type {
	CodingTaskRequest,
	CodingTaskResult,
	ProviderConfig,
	StreamingDelta,
	ToolCallRecord,
} from "./types.js";
import { BridgeError } from "./types.js";

export interface SessionMetadata {
	sessionId: string;
	task: string;
	startTime: string; // ISO 8601 UTC
	lastActivity: string; // ISO 8601 UTC
	providerType?: string;
	providerBaseUrl?: string;
	workingDir?: string;
}

function log(level: string, message: string, data?: Record<string, unknown>): void {
	const timestamp = new Date().toISOString();
	const prefix = `[${timestamp}] [copilot-bridge] [${level.toUpperCase()}]`;
	if (data) {
		console.error(`${prefix} ${message}`, data);
	} else {
		console.error(`${prefix} ${message}`);
	}
}

function buildDefaultProvider(config: BridgeConfig): ProviderConfig | undefined {
	if (!config.byokProvider) return undefined;
	return {
		type: config.byokProvider,
		baseUrl: config.byokBaseUrl,
		apiKey: config.byokApiKey,
		model: config.byokModel,
	};
}

export class CopilotBridge implements ICopilotClient {
	private client: InstanceType<typeof CopilotClient>;
	private defaultProvider: ProviderConfig | undefined;
	private config: BridgeConfig;
	private authMethod = "unknown";

	constructor(config: BridgeConfig) {
		this.config = config;
		this.defaultProvider = buildDefaultProvider(config);

		// BYOK provider is NOT passed to the CopilotClient constructor.
		// It is a session-level config passed to createSession().
		this.client = new CopilotClient({
			githubToken: config.githubToken,
			cliPath: config.cliPath,
			logLevel: config.logLevel,
			autoRestart: true,
		});
	}

	private getMcpServerPath(): string {
		const currentDir = path.dirname(fileURLToPath(import.meta.url));
		return path.join(currentDir, "mcp-openclaw.js");
	}

	async ensureReady(): Promise<void> {
		log("info", "Ensuring CopilotBridge is ready...");

		try {
			const pingResult = await this.client.ping("health");
			log("debug", "Ping successful", {
				message: pingResult.message,
				protocolVersion: pingResult.protocolVersion,
			});
		} catch (err) {
			throw new BridgeError(
				`Failed to ping Copilot SDK: ${err instanceof Error ? err.message : String(err)}`,
				"PING_FAILED",
				undefined,
				true,
			);
		}

		try {
			const auth = await this.client.getAuthStatus();
			this.authMethod =
				auth.authType ?? (auth.isAuthenticated ? "authenticated" : "unauthenticated");
			log(
				"info",
				`Auth status: authenticated=${auth.isAuthenticated}, type=${auth.authType ?? "unknown"}`,
			);

			if (!auth.isAuthenticated && !this.defaultProvider) {
				throw new BridgeError(
					`Not authenticated (${auth.statusMessage ?? "no auth"}) and no BYOK provider configured`,
					"AUTH_FAILED",
					{ isAuthenticated: auth.isAuthenticated, authType: auth.authType },
					true,
				);
			}
		} catch (err) {
			if (err instanceof BridgeError) throw err;
			throw new BridgeError(
				`Failed to check auth status: ${err instanceof Error ? err.message : String(err)}`,
				"AUTH_CHECK_FAILED",
				undefined,
				true,
			);
		}

		log("info", "CopilotBridge is ready");
	}

	async stop(): Promise<void> {
		log("info", "Stopping CopilotBridge...");

		try {
			const errors = await this.client.stop();
			if (errors && errors.length > 0) {
				log("warn", `stop() returned ${errors.length} error(s), calling forceStop()`, {
					errors: errors.map((e: Error) => e.message),
				});
				await this.client.forceStop();
			}
		} catch (err) {
			log("error", `Error during stop: ${err instanceof Error ? err.message : String(err)}`);
			await this.client.forceStop();
		}
		log("info", "CopilotBridge stopped");
	}

	async isReady(): Promise<boolean> {
		try {
			await this.client.ping("check");
			return true;
		} catch {
			return false;
		}
	}

	async getStatus(): Promise<{ connected: boolean; authMethod: string }> {
		const connected = await this.isReady();
		return { connected, authMethod: this.authMethod };
	}

	private buildHookConfig(): HookConfig {
		const moduleDir = path.dirname(fileURLToPath(import.meta.url));
		return {
			auditLogDir:
				this.config.auditLogDir ?? path.join(moduleDir, "..", ".copilot-bridge", "audit"),
			policy: this.config.permissionPolicy ?? DEFAULT_POLICY,
			projectContext: this.config.projectContext ?? "",
			maxRetries: this.config.maxRetries ?? 3,
		};
	}

	private getSessionsFilePath(): string {
		const homeDir = process.env.HOME ?? process.env.USERPROFILE ?? "/tmp";
		return path.join(homeDir, ".copilot-bridge", "sessions.json");
	}

	private async loadSessionsFile(): Promise<SessionMetadata[]> {
		try {
			const data = await fs.readFile(this.getSessionsFilePath(), "utf-8");
			return JSON.parse(data) as SessionMetadata[];
		} catch {
			return [];
		}
	}

	private async saveSessionsFile(sessions: SessionMetadata[]): Promise<void> {
		const filePath = this.getSessionsFilePath();
		await fs.mkdir(path.dirname(filePath), { recursive: true });
		await fs.writeFile(filePath, JSON.stringify(sessions, null, "\t"), "utf-8");
	}

	private async persistSession(metadata: SessionMetadata): Promise<void> {
		const sessions = await this.loadSessionsFile();
		const idx = sessions.findIndex((s) => s.sessionId === metadata.sessionId);
		if (idx >= 0) {
			sessions[idx] = metadata;
		} else {
			sessions.push(metadata);
		}
		await this.saveSessionsFile(sessions);
	}

	async runTask(request: CodingTaskRequest): Promise<CodingTaskResult> {
		const startTime = Date.now();
		const toolCalls: ToolCallRecord[] = [];
		const errors: string[] = [];
		const sessionId = request.sessionId ?? crypto.randomUUID();

		const provider = request.provider ?? this.defaultProvider;

		const hooks = createHooks(this.buildHookConfig());

		const sessionConfig: Record<string, unknown> = {
			model: request.model,
			provider,
			streaming: false,
			hooks,
			sessionId,
		};

		if (request.workingDir) {
			sessionConfig.workingDirectory = request.workingDir;
		}
		if (request.tools) {
			sessionConfig.tools = request.tools;
		}

		// Add MCP servers config — the SDK spawns them per-session
		sessionConfig.mcpServers = {
			openclaw: {
				type: "local",
				command: "node",
				args: [this.getMcpServerPath()],
			},
		};

		log("debug", "Creating session", { sessionId });
		const session = await this.client.createSession(sessionConfig);

		// Subscribe to tool execution events
		const unsubscribe = session.on((event: any) => {
			if (event.type === "tool.execution_start") {
				toolCalls.push({
					tool: event.tool ?? "unknown",
					args: event.args ?? {},
					result: "",
					timestamp: Date.now(),
				});
			} else if (event.type === "tool.execution_complete") {
				const last = [...toolCalls]
					.reverse()
					.find((tc: ToolCallRecord) => tc.tool === (event.tool ?? "unknown"));
				if (last) {
					last.result = event.result ?? "";
				}
			}
		});

		try {
			let content = "";
			const timeout = request.timeout;

			if (timeout) {
				const result = await Promise.race([
					session.sendAndWait({ prompt: request.prompt }, timeout),
					new Promise<undefined>((_, reject) =>
						setTimeout(
							() => reject(new BridgeError("Task timed out", "TIMEOUT", { timeout }, true)),
							timeout,
						),
					),
				]);
				if (result) {
					content = result.data?.content ?? "";
				}
			} else {
				const result = await session.sendAndWait({ prompt: request.prompt });
				if (result) {
					content = result.data?.content ?? "";
				}
			}

			const elapsed = Date.now() - startTime;

			if (request.persistSession) {
				await this.persistSession({
					sessionId,
					task: request.prompt,
					startTime: new Date(startTime).toISOString(),
					lastActivity: new Date().toISOString(),
					providerType: provider?.type,
					providerBaseUrl: provider?.baseUrl,
					workingDir: request.workingDir,
				});
			}

			return {
				success: true,
				content,
				toolCalls,
				errors,
				sessionId,
				elapsed,
			};
		} catch (err) {
			const elapsed = Date.now() - startTime;
			const message = err instanceof Error ? err.message : String(err);
			errors.push(message);

			return {
				success: false,
				content: "",
				toolCalls,
				errors,
				sessionId,
				elapsed,
			};
		} finally {
			unsubscribe();
			// Only destroy session if no sessionId was provided in the request
			// (caller-managed sessions are kept alive)
			if (!request.sessionId && !request.persistSession) {
				await session.destroy();
				log("debug", "Session destroyed", { sessionId });
			}
		}
	}

	async *runTaskStreaming(request: CodingTaskRequest): AsyncGenerator<StreamingDelta> {
		const provider = request.provider ?? this.defaultProvider;
		const sessionId = request.sessionId ?? crypto.randomUUID();

		const hooks = createHooks(this.buildHookConfig());

		const sessionConfig: Record<string, unknown> = {
			model: request.model,
			provider,
			streaming: true,
			hooks,
			sessionId,
		};

		if (request.workingDir) {
			sessionConfig.workingDirectory = request.workingDir;
		}
		if (request.tools) {
			sessionConfig.tools = request.tools;
		}

		// Add MCP servers config — the SDK spawns them per-session
		sessionConfig.mcpServers = {
			openclaw: {
				type: "local",
				command: "node",
				args: [this.getMcpServerPath()],
			},
		};

		log("debug", "Creating streaming session", { sessionId });
		const session = await this.client.createSession(sessionConfig);

		// Collect events into a queue that the generator consumes
		const queue: StreamingDelta[] = [];
		let resolve: (() => void) | null = null;
		let done = false;

		const unsubscribe = session.on((event: any) => {
			let delta: StreamingDelta | null = null;

			switch (event.type) {
				case "content.delta":
					delta = { type: "text", content: event.content ?? "" };
					break;
				case "tool.execution_start":
					delta = {
						type: "tool_start",
						content: event.args ? JSON.stringify(event.args) : "",
						tool: event.tool,
					};
					break;
				case "tool.execution_complete":
					delta = { type: "tool_end", content: event.result ?? "", tool: event.tool };
					break;
				case "error":
					delta = { type: "error", content: event.message ?? "Unknown error" };
					break;
				case "response.completed":
					done = true;
					break;
			}

			if (delta) {
				queue.push(delta);
				resolve?.();
			}
			if (done) {
				resolve?.();
			}
		});

		try {
			// Fire the prompt (don't await — events flow via the listener)
			const sendPromise = session.sendAndWait({ prompt: request.prompt }, request.timeout);

			// Yield deltas as they arrive
			while (!done) {
				if (queue.length > 0) {
					yield queue.shift()!;
				} else {
					await new Promise<void>((r) => {
						resolve = r;
					});
				}
			}

			// Drain remaining queue
			while (queue.length > 0) {
				yield queue.shift()!;
			}

			// Await the send to catch any final errors
			await sendPromise;

			yield { type: "done", content: "" };
		} finally {
			unsubscribe();
			if (!request.sessionId) {
				await session.destroy();
				log("debug", "Streaming session destroyed", { sessionId });
			}
		}
	}

	async resumeTask(sessionId: string, prompt: string): Promise<CodingTaskResult> {
		const sessions = await this.loadSessionsFile();
		const meta = sessions.find((s) => s.sessionId === sessionId);
		if (!meta) {
			throw new BridgeError(
				`No persisted session found with ID: ${sessionId}`,
				"SESSION_NOT_FOUND",
				{ sessionId },
				false,
			);
		}

		// Reconstruct provider from metadata + current env
		const provider = meta.providerType
			? {
					type: meta.providerType as ProviderConfig["type"],
					baseUrl: meta.providerBaseUrl,
					apiKey: this.config.byokApiKey, // API key always from current env, never persisted
					model: this.config.byokModel,
				}
			: this.defaultProvider;

		const result = await this.runTask({
			prompt,
			sessionId,
			provider,
			workingDir: meta.workingDir,
		});

		// Update lastActivity
		meta.lastActivity = new Date().toISOString();
		await this.persistSession(meta);

		return result;
	}

	async listPersistedSessions(): Promise<SessionMetadata[]> {
		const sessions = await this.loadSessionsFile();
		return sessions;
	}

	async destroyPersistedSession(sessionId: string): Promise<void> {
		const sessions = await this.loadSessionsFile();
		const filtered = sessions.filter((s) => s.sessionId !== sessionId);
		await this.saveSessionsFile(filtered);
	}

	async cleanStaleSessions(maxAgeMs = 24 * 60 * 60 * 1000): Promise<number> {
		const sessions = await this.loadSessionsFile();
		const now = Date.now();
		const fresh = sessions.filter((s) => {
			const lastActivity = new Date(s.lastActivity).getTime();
			return now - lastActivity < maxAgeMs;
		});
		const removed = sessions.length - fresh.length;
		if (removed > 0) {
			await this.saveSessionsFile(fresh);
		}
		return removed;
	}
}
