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

export interface SessionInfo {
	sessionId: string;
	workingDir?: string;
	createdAt: string; // ISO timestamp
	messageCount: number;
}

interface StoredSession {
	session: any;
	unsubscribe: () => void;
	workingDir?: string;
	createdAt: string;
	messageCount: number;
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
	private sessions = new Map<string, StoredSession>();

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

		await this.destroyAllSessions();

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

	/**
	 * Resolve a workingDir value to an absolute path.
	 * - If absolute, use as-is.
	 * - If relative/bare name, resolve against projectsRoot.
	 * - Creates the directory if it doesn't exist (mkdir -p).
	 * Returns the resolved absolute path.
	 */
	async resolveWorkingDir(workingDir: string): Promise<string> {
		const { mkdir } = await import("node:fs/promises");
		const resolved = path.isAbsolute(workingDir)
			? workingDir
			: path.join(this.config.projectsRoot, workingDir);
		await mkdir(resolved, { recursive: true });
		log("info", `Resolved workingDir: "${workingDir}" → "${resolved}"`);
		return resolved;
	}

	async runTask(request: CodingTaskRequest): Promise<CodingTaskResult> {
		const startTime = Date.now();
		const toolCalls: ToolCallRecord[] = [];
		const errors: string[] = [];
		const sessionId = request.sessionId ?? crypto.randomUUID();

		const provider = request.provider ?? this.defaultProvider;

		// Check if we can reuse an existing stored session
		const stored = this.sessions.get(sessionId);
		let session: any;
		let isNewSession = false;

		if (stored) {
			// Reuse existing session
			session = stored.session;
			log("debug", "Reusing stored session", { sessionId, messageCount: stored.messageCount });
		} else {
			// Create a new session
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
			session = await this.client.createSession(sessionConfig);
			isNewSession = true;
		}

		// Subscribe to tool execution events for this call
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

			// Store the session (create or update)
			if (isNewSession) {
				this.sessions.set(sessionId, {
					session,
					unsubscribe: () => {}, // per-session unsub is a no-op; per-call unsub is in finally
					workingDir: request.workingDir,
					createdAt: new Date().toISOString(),
					messageCount: 1,
				});
			} else if (stored) {
				stored.messageCount++;
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

			// Still store the session even on error so it can be retried
			if (isNewSession) {
				this.sessions.set(sessionId, {
					session,
					unsubscribe: () => {},
					workingDir: request.workingDir,
					createdAt: new Date().toISOString(),
					messageCount: 1,
				});
			} else if (stored) {
				stored.messageCount++;
			}

			return {
				success: false,
				content: "",
				toolCalls,
				errors,
				sessionId,
				elapsed,
			};
		} finally {
			// Only unsubscribe the per-call event listener — sessions are long-lived
			unsubscribe();
		}
	}

	/** List all active sessions with metadata */
	listSessions(): SessionInfo[] {
		return Array.from(this.sessions.entries()).map(([id, stored]) => ({
			sessionId: id,
			workingDir: stored.workingDir,
			createdAt: stored.createdAt,
			messageCount: stored.messageCount,
		}));
	}

	/** Destroy a specific session by ID */
	async destroySession(sessionId: string): Promise<boolean> {
		const stored = this.sessions.get(sessionId);
		if (!stored) return false;
		try {
			await stored.session.destroy();
		} catch (err) {
			log("warn", `Error destroying session ${sessionId}: ${err instanceof Error ? err.message : String(err)}`);
		}
		this.sessions.delete(sessionId);
		log("debug", "Session destroyed", { sessionId });
		return true;
	}

	/** Destroy all sessions */
	async destroyAllSessions(): Promise<number> {
		const count = this.sessions.size;
		const destroyPromises = Array.from(this.sessions.entries()).map(async ([id, stored]) => {
			try {
				await stored.session.destroy();
			} catch (err) {
				log("warn", `Error destroying session ${id}: ${err instanceof Error ? err.message : String(err)}`);
			}
		});
		await Promise.all(destroyPromises);
		this.sessions.clear();
		log("debug", `Destroyed ${count} session(s)`);
		return count;
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

}
