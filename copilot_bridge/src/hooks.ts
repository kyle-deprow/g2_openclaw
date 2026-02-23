import fs from "node:fs/promises";
import path from "node:path";

// ─── SDK-compatible hook input types ────────────────────────────────────────

export interface PreToolUseInput {
	timestamp: number;
	cwd: string;
	toolName: string;
	toolArgs: Record<string, unknown>;
}

export interface PostToolUseInput {
	timestamp: number;
	cwd: string;
	toolName: string;
	toolArgs: Record<string, unknown>;
	toolResult?: string;
}

export interface PromptInput {
	timestamp: number;
	cwd: string;
	prompt: string;
}

export interface SessionStartInput {
	timestamp: number;
	cwd: string;
	source?: string;
	initialPrompt?: string;
}

export interface SessionEndInput {
	timestamp: number;
	cwd: string;
	reason?: string;
	finalMessage?: string;
	error?: string;
}

export interface ErrorInput {
	timestamp: number;
	cwd: string;
	error: string;
	errorContext?: string;
	recoverable: boolean;
}

// ─── SDK-compatible hook result types ───────────────────────────────────────

export interface PreToolUseResult {
	permissionDecision: "allow" | "deny" | "ask";
	permissionDecisionReason?: string;
	modifiedArgs?: Record<string, unknown>;
	additionalContext?: string;
	suppressOutput?: boolean;
}

export interface PostToolUseResult {
	modifiedResult?: string;
	additionalContext?: string;
	suppressOutput?: boolean;
}

export interface PromptResult {
	modifiedPrompt?: string;
	additionalContext?: string;
	suppressOutput?: boolean;
}

export interface SessionStartResult {
	additionalContext?: string;
	modifiedConfig?: Record<string, unknown>;
}

export interface SessionEndResult {
	suppressOutput?: boolean;
	sessionSummary?: string;
}

export interface ErrorResult {
	suppressOutput?: boolean;
	errorHandling: "retry" | "skip" | "abort";
	retryCount?: number;
	userNotification?: string;
}

// ─── Domain types ───────────────────────────────────────────────────────────

export interface AuditEntry {
	timestamp: string;
	sessionId: string;
	hookType: string;
	toolName?: string;
	input: unknown;
	output: unknown;
	elapsed: number;
}

export interface PermissionPolicy {
	allowedTools: string[];
	blockedTools: string[];
	askTools: string[];
	blockedPatterns: RegExp[];
}

export interface HookConfig {
	auditLogDir: string;
	policy: PermissionPolicy;
	projectContext: string;
	maxRetries: number;
}

export interface SessionHooks {
	onPreToolUse: (
		input: PreToolUseInput,
		invocation: { sessionId: string },
	) => Promise<PreToolUseResult | null>;
	onPostToolUse: (
		input: PostToolUseInput,
		invocation: { sessionId: string },
	) => Promise<PostToolUseResult | null>;
	onUserPromptSubmitted: (
		input: PromptInput,
		invocation: { sessionId: string },
	) => Promise<PromptResult | null>;
	onSessionStart: (
		input: SessionStartInput,
		invocation: { sessionId: string },
	) => Promise<SessionStartResult | null>;
	onSessionEnd: (
		input: SessionEndInput,
		invocation: { sessionId: string },
	) => Promise<SessionEndResult | null>;
	onErrorOccurred: (
		input: ErrorInput,
		invocation: { sessionId: string },
	) => Promise<ErrorResult | null>;
}

// ─── Constants ──────────────────────────────────────────────────────────────

export const DEFAULT_POLICY: PermissionPolicy = {
	allowedTools: [],
	blockedTools: [],
	askTools: [],
	blockedPatterns: [],
};

const SECRET_PATTERN =
	/(sk-|ghp_|gho_|ghs_|ghu_|github_pat_|AKIA[A-Z0-9]|DefaultEndpointsProtocol=|-----BEGIN|password\s*=\s*"?|api_key\s*=\s*"?|token\s*=\s*"?|secret\s*=\s*"?|Bearer\s+|eyJ[A-Za-z0-9_-]{10,})[^\s"]+/gi;
const MAX_RESULT_LENGTH = 10_000;
const PATH_ARG_KEYS = [
	"path",
	"file",
	"filePath",
	"directory",
	"dir",
	"destination",
	"target",
	"outputPath",
	"inputPath",
];

const RATE_LIMIT_PATTERN = /rate.?limit/i;
const TRANSIENT_PATTERN = /timeout|ECONNRESET|ETIMEDOUT|network/i;
const FILE_ERROR_PATTERN = /file not found|ENOENT/i;

// ─── AuditLogger ────────────────────────────────────────────────────────────

export class AuditLogger {
	private buffer: AuditEntry[] = [];
	private dirCreated = false;

	constructor(private readonly logDir: string) {}

	getLogPath(): string {
		const date = new Date().toISOString().split("T")[0];
		return path.join(this.logDir, `audit-${date}.jsonl`);
	}

	async write(entry: AuditEntry): Promise<void> {
		this.buffer.push(entry);
	}

	async flush(): Promise<void> {
		if (this.buffer.length === 0) return;

		if (!this.dirCreated) {
			await fs.mkdir(this.logDir, { recursive: true });
			this.dirCreated = true;
		}

		const logPath = this.getLogPath();
		const entries = this.buffer;
		this.buffer = [];

		const lines = `${entries.map((e) => JSON.stringify(e)).join("\n")}\n`;

		try {
			await fs.appendFile(logPath, lines, "utf-8");
		} catch (err) {
			// Restore entries that failed to persist
			this.buffer = entries.concat(this.buffer);
			throw err;
		}
	}

	/** Visible for testing only. */
	getPendingCount(): number {
		return this.buffer.length;
	}
}

// ─── Pure helpers ───────────────────────────────────────────────────────────

export function redactSecrets(text: string): string {
	return text.replace(SECRET_PATTERN, "[REDACTED]");
}

export function evaluatePermission(
	toolName: string,
	toolArgs: Record<string, unknown>,
	policy: PermissionPolicy,
): { decision: "allow" | "deny" | "ask"; reason?: string } {
	// 1. Blocked tools → deny
	if (policy.blockedTools.includes(toolName)) {
		return { decision: "deny", reason: `Tool "${toolName}" is blocked by policy` };
	}

	// 2. Blocked patterns → deny
	const serialised = JSON.stringify(toolArgs);
	for (const pattern of policy.blockedPatterns) {
		// Reset lastIndex for global patterns
		pattern.lastIndex = 0;
		if (pattern.test(serialised)) {
			return {
				decision: "deny",
				reason: `Tool arguments matched blocked pattern: ${pattern.source}`,
			};
		}
	}

	// 3. Ask tools → ask
	if (policy.askTools.includes(toolName)) {
		return { decision: "ask", reason: `Tool "${toolName}" requires approval` };
	}

	// 4. Allowed tools allowlist (non-empty = only these allowed)
	if (policy.allowedTools.length > 0 && !policy.allowedTools.includes(toolName)) {
		return { decision: "deny", reason: `Tool "${toolName}" is not in the allowed list` };
	}

	// 5. Default → allow
	return { decision: "allow" };
}

// ─── Path restriction ───────────────────────────────────────────────────────

async function validatePathArg(
	argValue: unknown,
	cwd: string,
): Promise<{ valid: true } | { valid: false; reason: string }> {
	if (typeof argValue !== "string" || argValue.length === 0) {
		return { valid: true };
	}

	const resolved = path.resolve(cwd, argValue);
	const normCwd = path.resolve(cwd);

	if (!resolved.startsWith(normCwd + path.sep) && resolved !== normCwd) {
		return { valid: false, reason: `Path "${argValue}" resolves outside workspace` };
	}

	// Defence-in-depth: resolve symlinks
	try {
		const real = await fs.realpath(resolved);
		const realCwd = await fs.realpath(normCwd);
		if (!real.startsWith(realCwd + path.sep) && real !== realCwd) {
			return {
				valid: false,
				reason: `Path "${argValue}" resolves outside workspace via symlink`,
			};
		}
	} catch {
		// File doesn't exist yet — string check was sufficient
	}

	return { valid: true };
}

// ─── Audit entry helper ────────────────────────────────────────────────────

function makeAuditEntry(
	sessionId: string,
	hookType: string,
	input: unknown,
	output: unknown,
	elapsed: number,
	toolName?: string,
): AuditEntry {
	return {
		timestamp: new Date().toISOString(),
		sessionId,
		hookType,
		toolName,
		input,
		output,
		elapsed,
	};
}

// ─── createHooks factory ───────────────────────────────────────────────────

export function createHooks(config: HookConfig): SessionHooks {
	const logger = new AuditLogger(config.auditLogDir);
	const maxRetries = Math.max(1, Math.floor(config.maxRetries));

	// ── C4.2 Pre-Tool-Use ─────────────────────────────────────────────────
	const onPreToolUse = async (
		input: PreToolUseInput,
		invocation: { sessionId: string },
	): Promise<PreToolUseResult | null> => {
		try {
			const start = Date.now();

			const redactedInput = {
				...input,
				toolArgs: JSON.parse(redactSecrets(JSON.stringify(input.toolArgs))),
			};

			// Evaluate permission policy
			const { decision, reason } = evaluatePermission(
				input.toolName,
				input.toolArgs,
				config.policy,
			);

			if (decision === "deny" || decision === "ask") {
				const result: PreToolUseResult = {
					permissionDecision: decision,
					permissionDecisionReason: reason,
				};
				await logger.write(
					makeAuditEntry(
						invocation.sessionId,
						"pre_tool_use",
						redactedInput,
						result,
						Date.now() - start,
						input.toolName,
					),
				);
				await logger.flush();
				return result;
			}

			// Path restriction for file-related args
			for (const key of PATH_ARG_KEYS) {
				const argValue = input.toolArgs[key];
				if (argValue !== undefined) {
					const check = await validatePathArg(argValue, input.cwd);
					if (!check.valid) {
						const denyResult: PreToolUseResult = {
							permissionDecision: "deny",
							permissionDecisionReason: check.reason,
						};
						await logger.write(
							makeAuditEntry(
								invocation.sessionId,
								"pre_tool_use",
								redactedInput,
								denyResult,
								Date.now() - start,
								input.toolName,
							),
						);
						await logger.flush();
						return denyResult;
					}
				}
			}

			const result: PreToolUseResult = {
				permissionDecision: "allow",
			};

			await logger.write(
				makeAuditEntry(
					invocation.sessionId,
					"pre_tool_use",
					redactedInput,
					result,
					Date.now() - start,
					input.toolName,
				),
			);
			await logger.flush();
			return result;
		} catch (err) {
			// Fail closed — deny on internal error
			return {
				permissionDecision: "deny",
				permissionDecisionReason: `Internal hook error: ${err instanceof Error ? err.message : String(err)}`,
			};
		}
	};

	// ── C4.3 Post-Tool-Use ────────────────────────────────────────────────
	const onPostToolUse = async (
		input: PostToolUseInput,
		invocation: { sessionId: string },
	): Promise<PostToolUseResult | null> => {
		try {
			const start = Date.now();
			let modifiedResult: string | undefined;

			if (typeof input.toolResult === "string") {
				const redacted = redactSecrets(input.toolResult);
				const changed = redacted !== input.toolResult;

				if (redacted.length > MAX_RESULT_LENGTH) {
					modifiedResult = `${redacted.slice(0, MAX_RESULT_LENGTH)}\n[truncated]`;
				} else if (changed) {
					modifiedResult = redacted;
				}
			}

			const result: PostToolUseResult | null =
				modifiedResult !== undefined ? { modifiedResult } : null;

			await logger.write(
				makeAuditEntry(
					invocation.sessionId,
					"post_tool_use",
					{
						toolName: input.toolName,
						toolArgs: JSON.parse(redactSecrets(JSON.stringify(input.toolArgs))),
						resultLength: input.toolResult?.length ?? 0,
					},
					result,
					Date.now() - start,
					input.toolName,
				),
			);
			await logger.flush();
			return result;
		} catch {
			// Swallow — don't let audit failures crash the session
			return null;
		}
	};

	// ── C4.4 Prompt ───────────────────────────────────────────────────────
	const onUserPromptSubmitted = async (
		input: PromptInput,
		invocation: { sessionId: string },
	): Promise<PromptResult | null> => {
		try {
			const start = Date.now();
			const sanitised = redactSecrets(input.prompt);
			const modified = sanitised !== input.prompt;

			const result: PromptResult = {
				modifiedPrompt: modified ? sanitised : undefined,
				additionalContext: config.projectContext || undefined,
			};

			await logger.write(
				makeAuditEntry(
					invocation.sessionId,
					"prompt",
					{ prompt: sanitised },
					result,
					Date.now() - start,
				),
			);
			await logger.flush();

			// If nothing was modified and no context to inject, return null
			if (!modified && !config.projectContext) {
				return null;
			}

			return result;
		} catch {
			// Swallow — don't let audit failures crash the session
			return null;
		}
	};

	// ── C4.4 Session Start ────────────────────────────────────────────────
	const onSessionStart = async (
		input: SessionStartInput,
		invocation: { sessionId: string },
	): Promise<SessionStartResult | null> => {
		try {
			const start = Date.now();

			const result: SessionStartResult = {
				additionalContext: config.projectContext || undefined,
			};

			await logger.write(
				makeAuditEntry(invocation.sessionId, "session_start", input, result, Date.now() - start),
			);
			await logger.flush();

			return config.projectContext ? result : null;
		} catch {
			// Swallow — don't let audit failures crash the session
			return null;
		}
	};

	// ── C4.4 Session End ──────────────────────────────────────────────────
	const onSessionEnd = async (
		input: SessionEndInput,
		invocation: { sessionId: string },
	): Promise<SessionEndResult | null> => {
		try {
			const start = Date.now();

			const summary = `Session ended: ${input.reason ?? "unknown"}`;
			const result: SessionEndResult = { sessionSummary: summary };

			await logger.write(
				makeAuditEntry(invocation.sessionId, "session_end", input, result, Date.now() - start),
			);
			await logger.flush();

			return result;
		} catch {
			// Swallow — don't let audit failures crash the session
			return null;
		}
	};

	// ── C4.5 Error Handling ───────────────────────────────────────────────
	const onErrorOccurred = async (
		input: ErrorInput,
		invocation: { sessionId: string },
	): Promise<ErrorResult | null> => {
		try {
			const start = Date.now();
			let result: ErrorResult;

			if (RATE_LIMIT_PATTERN.test(input.error)) {
				result = {
					errorHandling: "retry",
					retryCount: maxRetries,
					userNotification: "Rate limited. Retrying...",
				};
			} else if (input.recoverable && TRANSIENT_PATTERN.test(input.error)) {
				result = {
					errorHandling: "retry",
					retryCount: Math.min(maxRetries, 2),
				};
			} else if (FILE_ERROR_PATTERN.test(input.error)) {
				result = {
					errorHandling: "skip",
					userNotification: `Skipping: ${input.error}`,
				};
			} else {
				result = {
					errorHandling: "abort",
					userNotification: `Unrecoverable error: ${input.error}`,
				};
			}

			await logger.write(
				makeAuditEntry(invocation.sessionId, "error", input, result, Date.now() - start),
			);
			await logger.flush();

			return result;
		} catch {
			// Swallow — don't let audit failures crash the session
			return null;
		}
	};

	return {
		onPreToolUse,
		onPostToolUse,
		onUserPromptSubmitted,
		onSessionStart,
		onSessionEnd,
		onErrorOccurred,
	};
}
