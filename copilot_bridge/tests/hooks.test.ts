import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// ─── Mock node:fs/promises ──────────────────────────────────────────────────

const mockAppendFile = vi.hoisted(() => vi.fn().mockResolvedValue(undefined));
const mockMkdir = vi.hoisted(() => vi.fn().mockResolvedValue(undefined));
const mockRealpath = vi.hoisted(() => vi.fn());

vi.mock("node:fs/promises", () => ({
	default: {
		appendFile: mockAppendFile,
		mkdir: mockMkdir,
		realpath: mockRealpath,
	},
}));

const { AuditLogger, DEFAULT_POLICY, createHooks, evaluatePermission, redactSecrets } =
	await import("../src/hooks.js");

type PermissionPolicy = import("../src/hooks.js").PermissionPolicy;
type HookConfig = import("../src/hooks.js").HookConfig;

// ─── Helpers ────────────────────────────────────────────────────────────────

function makeHookConfig(overrides: Partial<HookConfig> = {}): HookConfig {
	return {
		auditLogDir: "/tmp/audit",
		policy: { ...DEFAULT_POLICY },
		projectContext: "",
		maxRetries: 3,
		...overrides,
	};
}

const SESSION = { sessionId: "test-session-1" };
const CWD = "/home/user/project";
const NOW = Date.now();

// ─── Tests ──────────────────────────────────────────────────────────────────

describe("hooks", () => {
	beforeEach(() => {
		vi.clearAllMocks();
		mockRealpath.mockImplementation(async (p: string) => p);
	});

	// ── C4.1 — Hook Infrastructure & Types ─────────────────────────────────

	describe("DEFAULT_POLICY", () => {
		it("has empty arrays for all fields", () => {
			expect(DEFAULT_POLICY).toEqual({
				allowedTools: [],
				blockedTools: [],
				askTools: [],
				blockedPatterns: [],
			});
		});
	});

	describe("AuditLogger", () => {
		it("getLogPath() returns date-formatted path", () => {
			const logger = new AuditLogger("/tmp/logs");
			const logPath = logger.getLogPath();

			expect(logPath).toMatch(/\/tmp\/logs\/audit-\d{4}-\d{2}-\d{2}\.jsonl$/);
		});

		it("write() buffers entries", async () => {
			const logger = new AuditLogger("/tmp/logs");
			const entry = {
				timestamp: new Date().toISOString(),
				sessionId: "s1",
				hookType: "test",
				input: {},
				output: {},
				elapsed: 10,
			};

			await logger.write(entry);

			expect(logger.getPendingCount()).toBe(1);
			expect(mockAppendFile).not.toHaveBeenCalled();
		});

		it("flush() writes buffered entries as JSONL", async () => {
			const logger = new AuditLogger("/tmp/logs");
			const entry1 = {
				timestamp: "2025-01-01T00:00:00.000Z",
				sessionId: "s1",
				hookType: "test",
				input: {},
				output: null,
				elapsed: 5,
			};
			const entry2 = {
				timestamp: "2025-01-01T00:00:01.000Z",
				sessionId: "s1",
				hookType: "test2",
				input: { x: 1 },
				output: { y: 2 },
				elapsed: 3,
			};

			await logger.write(entry1);
			await logger.write(entry2);
			await logger.flush();

			expect(mockMkdir).toHaveBeenCalledWith("/tmp/logs", { recursive: true });
			expect(mockAppendFile).toHaveBeenCalledTimes(1);

			const written = mockAppendFile.mock.calls[0]?.[1] as string;
			const lines = written.trim().split("\n");
			expect(lines).toHaveLength(2);
			expect(JSON.parse(lines[0]!)).toEqual(entry1);
			expect(JSON.parse(lines[1]!)).toEqual(entry2);
		});

		it("flush() is a no-op when buffer is empty", async () => {
			const logger = new AuditLogger("/tmp/logs");
			await logger.flush();

			expect(mockMkdir).not.toHaveBeenCalled();
			expect(mockAppendFile).not.toHaveBeenCalled();
		});

		it("flush() creates directory only once", async () => {
			const logger = new AuditLogger("/tmp/logs");
			await logger.write({
				timestamp: "",
				sessionId: "s1",
				hookType: "a",
				input: null,
				output: null,
				elapsed: 0,
			});
			await logger.flush();
			await logger.write({
				timestamp: "",
				sessionId: "s1",
				hookType: "b",
				input: null,
				output: null,
				elapsed: 0,
			});
			await logger.flush();

			expect(mockMkdir).toHaveBeenCalledTimes(1);
			expect(mockAppendFile).toHaveBeenCalledTimes(2);
		});

		it("flush() restores buffer entries on appendFile failure", async () => {
			const logger = new AuditLogger("/tmp/logs");
			const entry = {
				timestamp: "2025-01-01T00:00:00.000Z",
				sessionId: "s1",
				hookType: "test",
				input: {},
				output: null,
				elapsed: 5,
			};

			await logger.write(entry);
			mockAppendFile.mockRejectedValueOnce(new Error("disk full"));

			await expect(logger.flush()).rejects.toThrow("disk full");
			expect(logger.getPendingCount()).toBe(1);

			// Verify it can succeed on retry
			mockAppendFile.mockResolvedValueOnce(undefined);
			await logger.flush();
			expect(logger.getPendingCount()).toBe(0);
			expect(mockAppendFile).toHaveBeenCalledTimes(2);
		});

		it("flush() restores buffer entries on mkdir failure", async () => {
			const logger = new AuditLogger("/tmp/logs");
			const entry = {
				timestamp: "2025-01-01T00:00:00.000Z",
				sessionId: "s1",
				hookType: "test",
				input: {},
				output: null,
				elapsed: 5,
			};

			await logger.write(entry);
			mockMkdir.mockRejectedValueOnce(new Error("permission denied"));

			await expect(logger.flush()).rejects.toThrow("permission denied");
			expect(logger.getPendingCount()).toBe(1);
		});
	});

	describe("createHooks()", () => {
		it("returns an object with all 6 hook functions", () => {
			const hooks = createHooks(makeHookConfig());

			expect(typeof hooks.onPreToolUse).toBe("function");
			expect(typeof hooks.onPostToolUse).toBe("function");
			expect(typeof hooks.onUserPromptSubmitted).toBe("function");
			expect(typeof hooks.onSessionStart).toBe("function");
			expect(typeof hooks.onSessionEnd).toBe("function");
			expect(typeof hooks.onErrorOccurred).toBe("function");
		});
	});

	// ── evaluatePermission (pure) ─────────────────────────────────────────

	describe("evaluatePermission()", () => {
		it("returns allow with empty policy", () => {
			const result = evaluatePermission("read_file", {}, DEFAULT_POLICY);
			expect(result).toEqual({ decision: "allow" });
		});

		it("denies blocked tool", () => {
			const policy: PermissionPolicy = {
				...DEFAULT_POLICY,
				blockedTools: ["exec"],
			};
			const result = evaluatePermission("exec", {}, policy);
			expect(result.decision).toBe("deny");
			expect(result.reason).toContain("exec");
			expect(result.reason).toContain("blocked");
		});

		it("denies when args match blocked pattern", () => {
			const policy: PermissionPolicy = {
				...DEFAULT_POLICY,
				blockedPatterns: [/rm\s+-rf/],
			};
			const result = evaluatePermission("exec", { command: "rm -rf /" }, policy);
			expect(result.decision).toBe("deny");
			expect(result.reason).toContain("blocked pattern");
		});

		it("handles global regex patterns correctly (lastIndex reset)", () => {
			const policy: PermissionPolicy = {
				...DEFAULT_POLICY,
				blockedPatterns: [/secret/gi],
			};
			// First call
			evaluatePermission("tool1", { x: "secret" }, policy);
			// Second call — should still match (lastIndex reset)
			const result = evaluatePermission("tool2", { x: "secret" }, policy);
			expect(result.decision).toBe("deny");
		});

		it("returns ask for askTools", () => {
			const policy: PermissionPolicy = {
				...DEFAULT_POLICY,
				askTools: ["write_file"],
			};
			const result = evaluatePermission("write_file", {}, policy);
			expect(result.decision).toBe("ask");
			expect(result.reason).toContain("approval");
		});

		it("denies tool not in allowedTools allowlist", () => {
			const policy: PermissionPolicy = {
				...DEFAULT_POLICY,
				allowedTools: ["read_file", "search"],
			};
			const result = evaluatePermission("exec", {}, policy);
			expect(result.decision).toBe("deny");
			expect(result.reason).toContain("not in the allowed list");
		});

		it("allows tool in allowedTools allowlist", () => {
			const policy: PermissionPolicy = {
				...DEFAULT_POLICY,
				allowedTools: ["read_file", "search"],
			};
			const result = evaluatePermission("read_file", {}, policy);
			expect(result.decision).toBe("allow");
		});

		it("blockedTools checked before askTools", () => {
			const policy: PermissionPolicy = {
				...DEFAULT_POLICY,
				blockedTools: ["dangerous"],
				askTools: ["dangerous"],
			};
			const result = evaluatePermission("dangerous", {}, policy);
			expect(result.decision).toBe("deny");
		});

		it("blockedTools checked before allowedTools", () => {
			const policy: PermissionPolicy = {
				...DEFAULT_POLICY,
				blockedTools: ["tool_a"],
				allowedTools: ["tool_a"],
			};
			const result = evaluatePermission("tool_a", {}, policy);
			expect(result.decision).toBe("deny");
		});

		it("allows empty toolName with default policy", () => {
			const result = evaluatePermission("", {}, DEFAULT_POLICY);
			expect(result.decision).toBe("allow");
		});

		it("denies when multiple blocked patterns exist and second matches", () => {
			const policy: PermissionPolicy = {
				...DEFAULT_POLICY,
				blockedPatterns: [/DROP TABLE/i, /rm\s+-rf/],
			};
			const result = evaluatePermission("exec", { cmd: "rm -rf /" }, policy);
			expect(result.decision).toBe("deny");
		});
	});

	// ── redactSecrets ─────────────────────────────────────────────────────

	describe("redactSecrets()", () => {
		it("redacts sk- prefixed tokens", () => {
			expect(redactSecrets("key is sk-abc123xyz")).toBe("key is [REDACTED]");
		});

		it("redacts ghp_ prefixed tokens", () => {
			expect(redactSecrets("token ghp_abcdef123456")).toBe("token [REDACTED]");
		});

		it("redacts gho_ prefixed tokens", () => {
			expect(redactSecrets("token gho_org_token_here")).toBe("token [REDACTED]");
		});

		it("redacts password= patterns", () => {
			expect(redactSecrets('password="my-secret-pass"')).toBe('[REDACTED]"');
		});

		it("leaves clean text unchanged", () => {
			const clean = "This is a normal message with no secrets.";
			expect(redactSecrets(clean)).toBe(clean);
		});

		it("redacts multiple secrets in one string", () => {
			const text = "keys: sk-aaa ghp_bbb gho_ccc";
			const result = redactSecrets(text);
			expect(result).not.toContain("sk-aaa");
			expect(result).not.toContain("ghp_bbb");
			expect(result).not.toContain("gho_ccc");
		});

		it("redacts AWS access key IDs", () => {
			expect(redactSecrets("key is AKIAIOSFODNN7EXAMPLE")).toBe("key is [REDACTED]");
		});

		it("redacts api_key= patterns", () => {
			expect(redactSecrets("api_key=mysecretkey123")).toBe("[REDACTED]");
		});

		it("redacts Bearer tokens", () => {
			expect(redactSecrets("Authorization: Bearer eyJhbGciOiJI.payload.sig")).toBe(
				"Authorization: [REDACTED]",
			);
		});

		it("redacts unquoted password= patterns", () => {
			expect(redactSecrets("password=mysecret123")).toBe("[REDACTED]");
		});

		it("redacts token= patterns", () => {
			expect(redactSecrets("token=abc123def456")).toBe("[REDACTED]");
		});

		it("redacts secret= patterns", () => {
			expect(redactSecrets("secret=shh_dont_tell")).toBe("[REDACTED]");
		});
	});

	// ── C4.2 — Pre-Tool-Use: Permission Enforcement ──────────────────────

	describe("onPreToolUse", () => {
		it("allows tool with default policy", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onPreToolUse(
				{ timestamp: NOW, cwd: CWD, toolName: "read_file", toolArgs: {} },
				SESSION,
			);
			expect(result?.permissionDecision).toBe("allow");
		});

		it("denies blocked tool", async () => {
			const hooks = createHooks(
				makeHookConfig({
					policy: { ...DEFAULT_POLICY, blockedTools: ["exec"] },
				}),
			);
			const result = await hooks.onPreToolUse(
				{ timestamp: NOW, cwd: CWD, toolName: "exec", toolArgs: {} },
				SESSION,
			);
			expect(result?.permissionDecision).toBe("deny");
			expect(result?.permissionDecisionReason).toContain("blocked");
		});

		it("denies when args match blocked pattern", async () => {
			const hooks = createHooks(
				makeHookConfig({
					policy: { ...DEFAULT_POLICY, blockedPatterns: [/rm\s+-rf/] },
				}),
			);
			const result = await hooks.onPreToolUse(
				{ timestamp: NOW, cwd: CWD, toolName: "exec", toolArgs: { command: "rm -rf /" } },
				SESSION,
			);
			expect(result?.permissionDecision).toBe("deny");
		});

		it("asks for askTools", async () => {
			const hooks = createHooks(
				makeHookConfig({
					policy: { ...DEFAULT_POLICY, askTools: ["write_file"] },
				}),
			);
			const result = await hooks.onPreToolUse(
				{ timestamp: NOW, cwd: CWD, toolName: "write_file", toolArgs: {} },
				SESSION,
			);
			expect(result?.permissionDecision).toBe("ask");
		});

		it("denies absolute path outside workspace", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onPreToolUse(
				{
					timestamp: NOW,
					cwd: CWD,
					toolName: "read_file",
					toolArgs: { path: "/etc/passwd" },
				},
				SESSION,
			);
			expect(result?.permissionDecision).toBe("deny");
			expect(result?.permissionDecisionReason).toContain("outside workspace");
		});

		it("denies relative path that escapes workspace", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onPreToolUse(
				{
					timestamp: NOW,
					cwd: CWD,
					toolName: "read_file",
					toolArgs: { path: "../../etc/passwd" },
				},
				SESSION,
			);
			expect(result?.permissionDecision).toBe("deny");
			expect(result?.permissionDecisionReason).toContain("outside workspace");
		});

		it("allows valid relative path", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onPreToolUse(
				{
					timestamp: NOW,
					cwd: CWD,
					toolName: "read_file",
					toolArgs: { path: "src/index.ts" },
				},
				SESSION,
			);
			expect(result?.permissionDecision).toBe("allow");
		});

		it("checks file arg as well as path arg", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onPreToolUse(
				{
					timestamp: NOW,
					cwd: CWD,
					toolName: "read_file",
					toolArgs: { file: "/etc/shadow" },
				},
				SESSION,
			);
			expect(result?.permissionDecision).toBe("deny");
		});

		it("checks filePath arg", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onPreToolUse(
				{
					timestamp: NOW,
					cwd: CWD,
					toolName: "write_file",
					toolArgs: { filePath: "/root/.ssh/id_rsa" },
				},
				SESSION,
			);
			expect(result?.permissionDecision).toBe("deny");
		});

		it("writes audit entry on every call", async () => {
			const hooks = createHooks(makeHookConfig());
			await hooks.onPreToolUse(
				{ timestamp: NOW, cwd: CWD, toolName: "read_file", toolArgs: {} },
				SESSION,
			);

			// Logger.flush is called, which triggers appendFile
			expect(mockAppendFile).toHaveBeenCalledTimes(1);
			const written = mockAppendFile.mock.calls[0]?.[1] as string;
			const entry = JSON.parse(written.trim());
			expect(entry.hookType).toBe("pre_tool_use");
			expect(entry.sessionId).toBe("test-session-1");
		});

		it("allows non-string path args (number, array, null)", async () => {
			const hooks = createHooks(makeHookConfig());

			for (const value of [42, ["/etc/passwd"], null, true]) {
				const result = await hooks.onPreToolUse(
					{
						timestamp: NOW,
						cwd: CWD,
						toolName: "read_file",
						toolArgs: { path: value },
					},
					SESSION,
				);
				expect(result?.permissionDecision).toBe("allow");
			}
		});

		it("allows path '.' (current directory)", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onPreToolUse(
				{
					timestamp: NOW,
					cwd: CWD,
					toolName: "list_files",
					toolArgs: { path: "." },
				},
				SESSION,
			);
			expect(result?.permissionDecision).toBe("allow");
		});

		it("denies path that resolves outside workspace via symlink", async () => {
			// Simulate: resolved path is inside workspace string-wise, but realpath reveals outside
			mockRealpath.mockImplementation(async (p: string) => {
				if (p === path.resolve(CWD, "link")) return "/etc/secret";
				if (p === path.resolve(CWD)) return path.resolve(CWD);
				return p;
			});

			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onPreToolUse(
				{
					timestamp: NOW,
					cwd: CWD,
					toolName: "read_file",
					toolArgs: { path: "link" },
				},
				SESSION,
			);
			expect(result?.permissionDecision).toBe("deny");
			expect(result?.permissionDecisionReason).toContain("symlink");
		});

		it("checks directory, dir, destination, target, outputPath, inputPath args", async () => {
			const hooks = createHooks(makeHookConfig());
			const outsidePath = "/etc/passwd";

			for (const key of ["directory", "dir", "destination", "target", "outputPath", "inputPath"]) {
				const result = await hooks.onPreToolUse(
					{
						timestamp: NOW,
						cwd: CWD,
						toolName: "some_tool",
						toolArgs: { [key]: outsidePath },
					},
					SESSION,
				);
				expect(result?.permissionDecision, `expected deny for key "${key}"`).toBe("deny");
			}
		});
	});

	// ── C4.3 — Post-Tool-Use: Audit Logging & Result Filtering ──────────

	describe("onPostToolUse", () => {
		it("returns null for clean short result", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onPostToolUse(
				{
					timestamp: NOW,
					cwd: CWD,
					toolName: "read_file",
					toolArgs: {},
					toolResult: "clean text",
				},
				SESSION,
			);
			expect(result).toBeNull();
		});

		it("redacts secrets in tool result", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onPostToolUse(
				{
					timestamp: NOW,
					cwd: CWD,
					toolName: "read_file",
					toolArgs: {},
					toolResult: "key=sk-secret123 done",
				},
				SESSION,
			);
			expect(result).not.toBeNull();
			expect(result?.modifiedResult).toContain("[REDACTED]");
			expect(result?.modifiedResult).not.toContain("sk-secret123");
		});

		it("truncates results over 10000 chars", async () => {
			const hooks = createHooks(makeHookConfig());
			const longResult = "x".repeat(15_000);
			const result = await hooks.onPostToolUse(
				{
					timestamp: NOW,
					cwd: CWD,
					toolName: "read_file",
					toolArgs: {},
					toolResult: longResult,
				},
				SESSION,
			);
			expect(result).not.toBeNull();
			expect(result?.modifiedResult).toContain("[truncated]");
			expect(result?.modifiedResult?.length).toBeLessThan(15_000);
		});

		it("handles undefined toolResult gracefully", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onPostToolUse(
				{
					timestamp: NOW,
					cwd: CWD,
					toolName: "exec",
					toolArgs: {},
					toolResult: undefined,
				},
				SESSION,
			);
			expect(result).toBeNull();
		});

		it("writes audit entry with tool info", async () => {
			const hooks = createHooks(makeHookConfig());
			await hooks.onPostToolUse(
				{
					timestamp: NOW,
					cwd: CWD,
					toolName: "search",
					toolArgs: { query: "test" },
					toolResult: "found it",
				},
				SESSION,
			);

			expect(mockAppendFile).toHaveBeenCalled();
			const written = mockAppendFile.mock.calls[0]?.[1] as string;
			const entry = JSON.parse(written.trim());
			expect(entry.hookType).toBe("post_tool_use");
			expect(entry.toolName).toBe("search");
		});

		it("redacts secrets before truncating", async () => {
			const hooks = createHooks(makeHookConfig());
			// Create a string with a secret near the beginning and length > 10000
			const longResult = `key=sk-supersecret123 ${"x".repeat(15_000)}`;
			const result = await hooks.onPostToolUse(
				{
					timestamp: NOW,
					cwd: CWD,
					toolName: "exec",
					toolArgs: {},
					toolResult: longResult,
				},
				SESSION,
			);
			expect(result).not.toBeNull();
			expect(result?.modifiedResult).toContain("[REDACTED]");
			expect(result?.modifiedResult).not.toContain("sk-supersecret123");
			expect(result?.modifiedResult).toContain("[truncated]");
		});
	});

	// ── C4.4 — Prompt & Session Lifecycle Hooks ──────────────────────────

	describe("onUserPromptSubmitted", () => {
		it("returns null with no context and clean prompt", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onUserPromptSubmitted(
				{ timestamp: NOW, cwd: CWD, prompt: "hello world" },
				SESSION,
			);
			expect(result).toBeNull();
		});

		it("redacts credentials from prompt", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onUserPromptSubmitted(
				{ timestamp: NOW, cwd: CWD, prompt: "use key ghp_abc123def456 please" },
				SESSION,
			);
			expect(result).not.toBeNull();
			expect(result?.modifiedPrompt).toContain("[REDACTED]");
			expect(result?.modifiedPrompt).not.toContain("ghp_abc123def456");
		});

		it("injects projectContext as additionalContext", async () => {
			const hooks = createHooks(makeHookConfig({ projectContext: "SpineSense Python backend" }));
			const result = await hooks.onUserPromptSubmitted(
				{ timestamp: NOW, cwd: CWD, prompt: "fix the bug" },
				SESSION,
			);
			expect(result?.additionalContext).toBe("SpineSense Python backend");
		});

		it("both redacts and injects context when needed", async () => {
			const hooks = createHooks(makeHookConfig({ projectContext: "My project" }));
			const result = await hooks.onUserPromptSubmitted(
				{ timestamp: NOW, cwd: CWD, prompt: "key is sk-mysecret123" },
				SESSION,
			);
			expect(result?.modifiedPrompt).toContain("[REDACTED]");
			expect(result?.additionalContext).toBe("My project");
		});
	});

	describe("onSessionStart", () => {
		it("returns context when projectContext is set", async () => {
			const hooks = createHooks(makeHookConfig({ projectContext: "SpineSense" }));
			const result = await hooks.onSessionStart({ timestamp: NOW, cwd: CWD }, SESSION);
			expect(result).not.toBeNull();
			expect(result?.additionalContext).toBe("SpineSense");
		});

		it("returns null when no projectContext", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onSessionStart({ timestamp: NOW, cwd: CWD }, SESSION);
			expect(result).toBeNull();
		});

		it("writes session_start audit entry", async () => {
			const hooks = createHooks(makeHookConfig({ projectContext: "test" }));
			await hooks.onSessionStart({ timestamp: NOW, cwd: CWD }, SESSION);

			expect(mockAppendFile).toHaveBeenCalled();
			const written = mockAppendFile.mock.calls[0]?.[1] as string;
			const entry = JSON.parse(written.trim());
			expect(entry.hookType).toBe("session_start");
		});
	});

	describe("onSessionEnd", () => {
		it("returns session summary with reason", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onSessionEnd(
				{ timestamp: NOW, cwd: CWD, reason: "user_requested" },
				SESSION,
			);
			expect(result?.sessionSummary).toBe("Session ended: user_requested");
		});

		it("handles missing reason", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onSessionEnd({ timestamp: NOW, cwd: CWD }, SESSION);
			expect(result?.sessionSummary).toBe("Session ended: unknown");
		});

		it("writes session_end audit entry", async () => {
			const hooks = createHooks(makeHookConfig());
			await hooks.onSessionEnd({ timestamp: NOW, cwd: CWD, reason: "destroyed" }, SESSION);

			expect(mockAppendFile).toHaveBeenCalled();
			const written = mockAppendFile.mock.calls[0]?.[1] as string;
			const entry = JSON.parse(written.trim());
			expect(entry.hookType).toBe("session_end");
		});

		it("flushes audit logger", async () => {
			const hooks = createHooks(makeHookConfig());
			await hooks.onSessionEnd({ timestamp: NOW, cwd: CWD, reason: "done" }, SESSION);

			// appendFile was called which means flush happened
			expect(mockAppendFile).toHaveBeenCalledTimes(1);
		});
	});

	// ── C4.5 — Error Handling Hook ───────────────────────────────────────

	describe("onErrorOccurred", () => {
		it("returns retry for rate limit errors", async () => {
			const hooks = createHooks(makeHookConfig({ maxRetries: 5 }));
			const result = await hooks.onErrorOccurred(
				{
					timestamp: NOW,
					cwd: CWD,
					error: "429 rate limit exceeded",
					recoverable: true,
				},
				SESSION,
			);
			expect(result?.errorHandling).toBe("retry");
			expect(result?.retryCount).toBe(5);
		});

		it("returns retry with capped count for transient + recoverable", async () => {
			const hooks = createHooks(makeHookConfig({ maxRetries: 5 }));
			const result = await hooks.onErrorOccurred(
				{
					timestamp: NOW,
					cwd: CWD,
					error: "ECONNRESET: connection reset",
					recoverable: true,
				},
				SESSION,
			);
			expect(result?.errorHandling).toBe("retry");
			expect(result?.retryCount).toBe(2);
		});

		it("returns retry for timeout errors when recoverable", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onErrorOccurred(
				{
					timestamp: NOW,
					cwd: CWD,
					error: "Request timeout after 30s",
					recoverable: true,
				},
				SESSION,
			);
			expect(result?.errorHandling).toBe("retry");
		});

		it("returns skip for file not found errors", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onErrorOccurred(
				{
					timestamp: NOW,
					cwd: CWD,
					error: "ENOENT: no such file or directory",
					recoverable: false,
				},
				SESSION,
			);
			expect(result?.errorHandling).toBe("skip");
		});

		it("returns skip for 'file not found' text", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onErrorOccurred(
				{
					timestamp: NOW,
					cwd: CWD,
					error: "file not found: /tmp/missing.txt",
					recoverable: false,
				},
				SESSION,
			);
			expect(result?.errorHandling).toBe("skip");
		});

		it("returns abort for unrecoverable errors", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onErrorOccurred(
				{
					timestamp: NOW,
					cwd: CWD,
					error: "FATAL: internal state corruption",
					recoverable: false,
				},
				SESSION,
			);
			expect(result?.errorHandling).toBe("abort");
			expect(result?.userNotification).toContain("Unrecoverable");
		});

		it("does not retry transient errors when not recoverable", async () => {
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onErrorOccurred(
				{
					timestamp: NOW,
					cwd: CWD,
					error: "ECONNRESET but fatal",
					recoverable: false,
				},
				SESSION,
			);
			// Contains ECONNRESET but recoverable=false → not transient retry → abort
			expect(result?.errorHandling).toBe("abort");
		});

		it("writes error audit entry", async () => {
			const hooks = createHooks(makeHookConfig());
			await hooks.onErrorOccurred(
				{
					timestamp: NOW,
					cwd: CWD,
					error: "something broke",
					recoverable: false,
				},
				SESSION,
			);

			expect(mockAppendFile).toHaveBeenCalled();
			const written = mockAppendFile.mock.calls[0]?.[1] as string;
			const entry = JSON.parse(written.trim());
			expect(entry.hookType).toBe("error");
		});

		it("rate limit takes priority over transient patterns", async () => {
			const hooks = createHooks(makeHookConfig({ maxRetries: 4 }));
			const result = await hooks.onErrorOccurred(
				{
					timestamp: NOW,
					cwd: CWD,
					error: "rate limit timeout",
					recoverable: true,
				},
				SESSION,
			);
			// "rate limit" matches first, so maxRetries (4) not capped at 2
			expect(result?.errorHandling).toBe("retry");
			expect(result?.retryCount).toBe(4);
		});
	});

	// ── Hook Error Isolation ────────────────────────────────────────────

	describe("hook error isolation", () => {
		it("onPreToolUse returns deny on internal error", async () => {
			mockAppendFile.mockRejectedValueOnce(new Error("disk full"));
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onPreToolUse(
				{ timestamp: NOW, cwd: CWD, toolName: "read_file", toolArgs: {} },
				SESSION,
			);
			expect(result?.permissionDecision).toBe("deny");
			expect(result?.permissionDecisionReason).toContain("Internal hook error");
		});

		it("onPostToolUse returns null on internal error", async () => {
			mockAppendFile.mockRejectedValueOnce(new Error("disk full"));
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onPostToolUse(
				{
					timestamp: NOW,
					cwd: CWD,
					toolName: "read_file",
					toolArgs: {},
					toolResult: "data",
				},
				SESSION,
			);
			expect(result).toBeNull();
		});

		it("onSessionStart returns null on internal error", async () => {
			mockAppendFile.mockRejectedValueOnce(new Error("disk full"));
			const hooks = createHooks(makeHookConfig({ projectContext: "test" }));
			const result = await hooks.onSessionStart({ timestamp: NOW, cwd: CWD }, SESSION);
			expect(result).toBeNull();
		});

		it("onSessionEnd returns null on internal error", async () => {
			mockAppendFile.mockRejectedValueOnce(new Error("disk full"));
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onSessionEnd(
				{ timestamp: NOW, cwd: CWD, reason: "done" },
				SESSION,
			);
			expect(result).toBeNull();
		});

		it("onErrorOccurred returns null on internal error", async () => {
			mockAppendFile.mockRejectedValueOnce(new Error("disk full"));
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onErrorOccurred(
				{
					timestamp: NOW,
					cwd: CWD,
					error: "some error",
					recoverable: false,
				},
				SESSION,
			);
			expect(result).toBeNull();
		});

		it("onUserPromptSubmitted returns null on internal error", async () => {
			mockAppendFile.mockRejectedValueOnce(new Error("disk full"));
			const hooks = createHooks(makeHookConfig());
			const result = await hooks.onUserPromptSubmitted(
				{ timestamp: NOW, cwd: CWD, prompt: "hello" },
				SESSION,
			);
			expect(result).toBeNull();
		});
	});
});
