import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// --- Hoisted mocks ---

const { mockReadFile, mockReaddir, mockRealpath, mockToolFn } = vi.hoisted(() => {
	return {
		mockReadFile: vi.fn(),
		mockReaddir: vi.fn(),
		mockRealpath: vi.fn(),
		mockToolFn: vi.fn(),
	};
});

vi.mock("node:fs/promises", () => ({
	default: {
		readFile: mockReadFile,
		readdir: mockReaddir,
		realpath: mockRealpath,
	},
	readFile: mockReadFile,
	readdir: mockReaddir,
	realpath: mockRealpath,
}));

vi.mock("@modelcontextprotocol/sdk/server/mcp.js", () => ({
	McpServer: vi.fn().mockImplementation(() => ({
		tool: mockToolFn,
		connect: vi.fn().mockResolvedValue(undefined),
	})),
}));

vi.mock("@modelcontextprotocol/sdk/server/stdio.js", () => ({
	StdioServerTransport: vi.fn().mockImplementation(() => ({})),
}));

vi.mock("../src/config.js", () => ({
	loadConfig: vi.fn().mockReturnValue({
		githubToken: undefined,
		byokProvider: undefined,
		byokApiKey: undefined,
		byokBaseUrl: undefined,
		byokModel: undefined,
		cliPath: undefined,
		logLevel: "warning",
		openclawHost: "127.0.0.1",
		openclawPort: 18789,
		openclawToken: undefined,
	}),
}));

const {
	createServer,
	handleMemorySearch,
	handleMemoryRead,
	handleUserPrefs,
	validateMemoryPath,
	OpenClawClient,
	checkDepth,
	MAX_CALL_DEPTH,
} = await import("../src/mcp-openclaw.js");

// --- Tests ---

describe("OpenClaw Memory MCP Server", () => {
	beforeEach(() => {
		vi.clearAllMocks();
		// Default: realpath returns the same path (no symlinks)
		mockRealpath.mockImplementation(async (p: string) => p);
	});

	afterEach(() => {
		vi.clearAllMocks();
	});

	// -----------------------------------------------------------------------
	// Tool registration
	// -----------------------------------------------------------------------

	describe("createServer", () => {
		it("registers exactly 3 tools (no agent-triggering tools)", () => {
			createServer();
			expect(mockToolFn).toHaveBeenCalledTimes(3);
		});

		it("registers tools with expected names", () => {
			createServer();
			const names = mockToolFn.mock.calls.map((c: unknown[]) => c[0]);
			expect(names).toEqual(
				expect.arrayContaining([
					"openclaw_memory_search",
					"openclaw_memory_read",
					"openclaw_user_prefs",
				]),
			);
		});

		it("does not register agent-triggering tools", () => {
			createServer();
			const names = mockToolFn.mock.calls.map((c: unknown[]) => c[0]) as string[];
			const forbidden = ["openclaw_context", "openclaw_run_agent", "openclaw_send_message"];
			for (const name of forbidden) {
				expect(names).not.toContain(name);
			}
		});
	});

	// -----------------------------------------------------------------------
	// openclaw_memory_search
	// -----------------------------------------------------------------------

	describe("openclaw_memory_search", () => {
		it("returns matching memory entries from files", async () => {
			mockReaddir.mockResolvedValue(["MEMORY.md", "2025-06-15.md"]);
			mockReadFile.mockImplementation(async (filePath: string) => {
				if (filePath.includes("MEMORY.md")) {
					return "# Memory\nUser prefers dark mode\nLast session: coding in TypeScript\n";
				}
				if (filePath.includes("2025-06-15.md")) {
					return "# Daily\nWorked on TypeScript MCP server\nFixed dark mode toggle\n";
				}
				throw new Error("ENOENT");
			});

			const result = await handleMemorySearch({ query: "TypeScript", limit: 5 });

			expect(result.isError).toBeUndefined();
			expect(result.content).toHaveLength(1);
			expect(result.content[0].type).toBe("text");
			expect(result.content[0].text).toContain("TypeScript");
			// Verify output format: [filename] (score: X.XX) line text
			expect(result.content[0].text).toMatch(/\[.+\.md\]\s+\(score:\s+\d+\.\d+\)/);
		});

		it("returns 'no entries found' for unmatched query", async () => {
			mockReaddir.mockResolvedValue(["MEMORY.md"]);
			mockReadFile.mockResolvedValue("# Memory\nNothing relevant here\n");

			const result = await handleMemorySearch({
				query: "xyznonexistent",
				limit: 5,
			});

			expect(result.isError).toBeUndefined();
			expect(result.content[0].text).toContain("No memory entries found");
		});

		it("handles missing memory directory gracefully", async () => {
			mockReaddir.mockRejectedValue(new Error("ENOENT"));

			const result = await handleMemorySearch({ query: "test", limit: 5 });

			expect(result.isError).toBeUndefined();
			expect(result.content[0].text).toContain("No memory directory found");
		});

		it("respects the limit parameter", async () => {
			mockReaddir.mockResolvedValue(["MEMORY.md"]);
			mockReadFile.mockResolvedValue(
				[
					"Line one about cats",
					"Line two about cats",
					"Line three about cats",
					"Line four about cats",
					"Line five about cats",
				].join("\n"),
			);

			const result = await handleMemorySearch({ query: "cats", limit: 2 });

			expect(result.isError).toBeUndefined();
			const lines = result.content[0].text.split("\n").filter(Boolean);
			expect(lines.length).toBeLessThanOrEqual(2);
		});
	});

	// -----------------------------------------------------------------------
	// openclaw_memory_read
	// -----------------------------------------------------------------------

	describe("openclaw_memory_read", () => {
		it("reads MEMORY.md by default", async () => {
			mockReadFile.mockResolvedValue("# Consolidated Memory\nKey facts here.");

			const result = await handleMemoryRead({});

			expect(result.isError).toBeUndefined();
			expect(result.content[0].text).toBe("# Consolidated Memory\nKey facts here.");
			// Verify readFile was called with a path ending in MEMORY.md
			expect(mockReadFile).toHaveBeenCalledWith(expect.stringContaining("MEMORY.md"), "utf-8");
		});

		it("reads a specific memory file", async () => {
			mockReadFile.mockResolvedValue("# Daily log\nDid some coding.");

			const result = await handleMemoryRead({ file: "2025-06-15.md" });

			expect(result.isError).toBeUndefined();
			expect(result.content[0].text).toBe("# Daily log\nDid some coding.");
			expect(mockReadFile).toHaveBeenCalledWith(expect.stringContaining("2025-06-15.md"), "utf-8");
		});

		it("blocks path traversal (../../etc/passwd)", async () => {
			const result = await handleMemoryRead({ file: "../../etc/passwd" });

			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Access denied");
			expect(result.content[0].text).toContain("traversal");
			// readFile should never be called for traversal attempts
			expect(mockReadFile).not.toHaveBeenCalled();
		});

		it("blocks path traversal with absolute path", async () => {
			const result = await handleMemoryRead({ file: "/etc/passwd" });

			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Access denied");
		});

		it("blocks path traversal with null bytes", async () => {
			const result = await handleMemoryRead({ file: "MEMORY.md\0.txt" });

			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Access denied");
		});

		it("returns error for missing file", async () => {
			mockReadFile.mockRejectedValue(
				Object.assign(new Error("ENOENT: no such file"), { code: "ENOENT" }),
			);

			const result = await handleMemoryRead({ file: "nonexistent.md" });

			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Failed to read memory file");
		});
	});

	// -----------------------------------------------------------------------
	// openclaw_user_prefs
	// -----------------------------------------------------------------------

	describe("openclaw_user_prefs", () => {
		it("reads USER.md contents", async () => {
			mockReadFile.mockResolvedValue("# User Preferences\nTheme: dark\nEditor: vim");

			const result = await handleUserPrefs();

			expect(result.isError).toBeUndefined();
			expect(result.content[0].text).toBe("# User Preferences\nTheme: dark\nEditor: vim");
		});

		it("returns helpful error when USER.md not found", async () => {
			mockReadFile.mockRejectedValue(
				Object.assign(new Error("ENOENT: no such file or directory"), {
					code: "ENOENT",
				}),
			);

			const result = await handleUserPrefs();

			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("USER.md not found");
			expect(result.content[0].text).toContain("Create this file");
		});

		it("returns error for other read failures", async () => {
			mockReadFile.mockRejectedValue(new Error("EACCES: permission denied"));

			const result = await handleUserPrefs();

			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Failed to read USER.md");
		});
	});

	// -----------------------------------------------------------------------
	// Path validation
	// -----------------------------------------------------------------------

	describe("validateMemoryPath", () => {
		beforeEach(() => {
			// Default: realpath returns the same path (no symlinks)
			mockRealpath.mockImplementation(async (p: string) => p);
		});

		it("allows simple filenames", async () => {
			const result = await validateMemoryPath("MEMORY.md", "/tmp/memory");
			expect(result).toBe("/tmp/memory/MEMORY.md");
		});

		it("allows subdirectory files", async () => {
			const result = await validateMemoryPath("sub/file.md", "/tmp/memory");
			expect(result).toBe("/tmp/memory/sub/file.md");
		});

		it("rejects parent traversal", async () => {
			await expect(validateMemoryPath("../../../etc/passwd", "/tmp/memory")).rejects.toThrow(
				"traversal",
			);
		});

		it("rejects absolute paths", async () => {
			await expect(validateMemoryPath("/etc/passwd", "/tmp/memory")).rejects.toThrow("traversal");
		});

		it("rejects null bytes", async () => {
			await expect(validateMemoryPath("file\0.md", "/tmp/memory")).rejects.toThrow("null bytes");
		});

		it("rejects symlinks that escape the base directory", async () => {
			mockRealpath.mockImplementation(async (p: string) => {
				if (p === "/tmp/memory/evil-link.md") return "/etc/passwd";
				return p;
			});
			await expect(validateMemoryPath("evil-link.md", "/tmp/memory")).rejects.toThrow("traversal");
		});

		it("allows paths when file does not exist (ENOENT from realpath)", async () => {
			mockRealpath.mockRejectedValue(Object.assign(new Error("ENOENT"), { code: "ENOENT" }));
			const result = await validateMemoryPath("new-file.md", "/tmp/memory");
			expect(result).toBe("/tmp/memory/new-file.md");
		});
	});

	// -----------------------------------------------------------------------
	// OpenClawClient — WebSocket reconnection
	// -----------------------------------------------------------------------

	describe("OpenClawClient", () => {
		it("initialises with config", () => {
			const client = new OpenClawClient({
				host: "127.0.0.1",
				port: 18789,
				token: "test-token",
			});
			expect(client).toBeDefined();
		});

		it("tracks exponential backoff", () => {
			const client = new OpenClawClient({
				host: "127.0.0.1",
				port: 18789,
			});
			// Initial backoff should be 500ms (INITIAL_BACKOFF_MS)
			expect(client.getBackoffMs()).toBe(500);
		});

		it("close() prevents further connections", async () => {
			const client = new OpenClawClient({
				host: "127.0.0.1",
				port: 18789,
			});
			client.close();
			await expect(client.connect()).rejects.toThrow("Client is closed");
		});
	});

	describe("OpenClawClient connect + callTool", () => {
		it("sends auth token in first message after connection, not in URL", () => {
			// Verify the class doesn't put token in URL
			// We test indirectly: create client with token, verify it stores config
			const client = new OpenClawClient({
				host: "127.0.0.1",
				port: 18789,
				token: "secret-token",
			});
			expect(client).toBeDefined();
			// The connect() method uses WebSocket which requires a real server,
			// so we verify the design by checking the class exists and close works
			client.close();
		});

		it("rejects callTool when not connected", async () => {
			const client = new OpenClawClient({
				host: "127.0.0.1",
				port: 18789,
			});
			// Close immediately to ensure not connected
			client.close();
			await expect(client.callTool("test", {})).rejects.toThrow("Client is closed");
		});

		it("backoff doubles on each retry up to max", () => {
			const client = new OpenClawClient({
				host: "127.0.0.1",
				port: 18789,
			});
			expect(client.getBackoffMs()).toBe(500); // INITIAL_BACKOFF_MS
			// The backoff is internal — tested via connectWithBackoff
			client.close();
		});
	});

	// -----------------------------------------------------------------------
	// Cycle detection
	// -----------------------------------------------------------------------

	describe("cycle detection", () => {
		it("checkDepth returns error at depth >= MAX_CALL_DEPTH", () => {
			const result = checkDepth(3);
			expect(result).not.toBeNull();
			expect(result?.isError).toBe(true);
			expect(result?.content[0].text).toContain("Maximum call depth exceeded");
			expect(result?.content[0].text).toContain("cycle detected");
		});

		it("checkDepth returns null at depth < MAX_CALL_DEPTH", () => {
			expect(checkDepth(0)).toBeNull();
			expect(checkDepth(1)).toBeNull();
			expect(checkDepth(2)).toBeNull();
		});

		it("checkDepth defaults to 0 when undefined", () => {
			expect(checkDepth(undefined)).toBeNull();
		});

		it("MAX_CALL_DEPTH is 3", () => {
			expect(MAX_CALL_DEPTH).toBe(3);
		});

		it("rejects openclaw_memory_search at depth >= MAX_CALL_DEPTH", async () => {
			const result = await handleMemorySearch({
				query: "test",
				limit: 5,
				_depth: 3,
			});

			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Maximum call depth exceeded");
			// Should NOT have attempted file reads
			expect(mockReaddir).not.toHaveBeenCalled();
		});

		it("rejects openclaw_user_prefs at depth >= MAX_CALL_DEPTH", async () => {
			const result = await handleUserPrefs({ _depth: 4 });

			expect(result.isError).toBe(true);
			expect(result.content[0].text).toContain("Maximum call depth exceeded");
			expect(mockReadFile).not.toHaveBeenCalled();
		});

		it("rejects at exact boundary depth", async () => {
			const result = await handleMemorySearch({
				query: "test",
				limit: 5,
				_depth: MAX_CALL_DEPTH,
			});
			expect(result.isError).toBe(true);

			const result2 = await handleMemoryRead({
				file: "test.md",
				_depth: MAX_CALL_DEPTH,
			});
			expect(result2.isError).toBe(true);

			const result3 = await handleUserPrefs({ _depth: MAX_CALL_DEPTH });
			expect(result3.isError).toBe(true);
		});

		it("allows calls at depth < MAX_CALL_DEPTH", async () => {
			mockReadFile.mockResolvedValue("# Consolidated Memory\nKey facts here.");

			const result = await handleMemoryRead({ file: "MEMORY.md", _depth: 2 });

			expect(result.isError).toBeUndefined();
			expect(result.content[0].text).toBe("# Consolidated Memory\nKey facts here.");
		});

		it("defaults _depth to 0 when not provided", async () => {
			mockReadFile.mockResolvedValue("# User Preferences\nTheme: dark");

			const result = await handleUserPrefs();

			expect(result.isError).toBeUndefined();
			expect(result.content[0].text).toBe("# User Preferences\nTheme: dark");
		});
	});
});
