import "dotenv/config";
import type { PermissionPolicy } from "./hooks.js";

export interface BridgeConfig {
	githubToken?: string;
	byokProvider?: "openai" | "azure" | "anthropic" | "ollama";
	byokApiKey?: string;
	byokBaseUrl?: string;
	byokModel?: string;
	cliPath?: string;
	logLevel: "debug" | "info" | "warning" | "error" | "none" | "all";
	openclawHost: string;
	openclawPort: number;
	openclawToken?: string;
	auditLogDir?: string;
	permissionPolicy?: PermissionPolicy;
	projectContext?: string;
	maxRetries?: number;
}

const VALID_LOG_LEVELS = new Set(["debug", "info", "warning", "error", "none", "all"]);
const VALID_BYOK_PROVIDERS = new Set(["openai", "azure", "anthropic", "ollama"]);

export function loadConfig(): BridgeConfig {
	const logLevel = process.env.COPILOT_LOG_LEVEL ?? "info";
	if (!VALID_LOG_LEVELS.has(logLevel)) {
		throw new Error(
			`Invalid COPILOT_LOG_LEVEL: "${logLevel}". Must be one of: debug, info, warning, error, none, all`,
		);
	}

	const byokProvider = process.env.COPILOT_BYOK_PROVIDER || undefined;
	if (byokProvider && !VALID_BYOK_PROVIDERS.has(byokProvider)) {
		throw new Error(
			`Invalid COPILOT_BYOK_PROVIDER: "${byokProvider}". Must be one of: openai, azure, anthropic, ollama`,
		);
	}

	const portStr = process.env.OPENCLAW_PORT ?? "18789";
	const port = Number.parseInt(portStr, 10);
	if (Number.isNaN(port) || port < 1 || port > 65535) {
		throw new Error(`Invalid OPENCLAW_PORT: "${portStr}". Must be a valid port number (1-65535)`);
	}

	const maxRetriesStr = process.env.COPILOT_MAX_RETRIES || undefined;
	let maxRetries: number | undefined;
	if (maxRetriesStr !== undefined) {
		maxRetries = Number.parseInt(maxRetriesStr, 10);
		if (Number.isNaN(maxRetries) || maxRetries < 1) {
			throw new Error(
				`Invalid COPILOT_MAX_RETRIES: "${maxRetriesStr}". Must be a positive integer`,
			);
		}
	}

	return {
		githubToken: process.env.COPILOT_GITHUB_TOKEN || undefined,
		byokProvider: byokProvider as BridgeConfig["byokProvider"],
		byokApiKey: process.env.COPILOT_BYOK_API_KEY || undefined,
		byokBaseUrl: process.env.COPILOT_BYOK_BASE_URL || undefined,
		byokModel: process.env.COPILOT_BYOK_MODEL || undefined,
		cliPath: process.env.COPILOT_CLI_PATH || undefined,
		logLevel: logLevel as BridgeConfig["logLevel"],
		openclawHost: process.env.OPENCLAW_HOST ?? "127.0.0.1",
		openclawPort: port,
		openclawToken: process.env.OPENCLAW_GATEWAY_TOKEN || undefined,
		auditLogDir: process.env.COPILOT_AUDIT_LOG_DIR || undefined,
		projectContext: process.env.COPILOT_PROJECT_CONTEXT || undefined,
		maxRetries,
	};
}
