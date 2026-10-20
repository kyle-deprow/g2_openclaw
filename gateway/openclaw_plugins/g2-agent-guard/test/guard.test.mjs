import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test, { after } from "node:test";
import plugin, { decide } from "../index.js";
import { canon, targetPaths, realResolve } from "../guard.js";

const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "guard-")));
after(() => fs.rmSync(tmp, { recursive: true, force: true }));
const root = path.join(tmp, "wt");
const outside = path.join(tmp, "outside");
fs.mkdirSync(root);
fs.mkdirSync(path.join(outside, "sub"), { recursive: true });
fs.symlinkSync(outside, path.join(root, "linkout"));
fs.symlinkSync(path.join(outside, "dangle"), path.join(root, "dangling"));
fs.symlinkSync(path.join(outside, "sub"), path.join(root, "L"));
fs.symlinkSync("../NEW", path.join(outside, "sub", "d"));
fs.symlinkSync(root, path.join(tmp, "rootlink"));

const config = {
  agents: {
    rev: { denyTools: ["Bash", "WRITE", "edit", "multiedit", "notebookedit", "apply_patch"] },
    star: { denyTools: ["*"] },
    impl: { writeRoots: [root] },
    both: { denyTools: ["bash"], writeRoots: [root] },
    empty: {},
  },
};
const w = (agent, tool, params) => decide(config, tool, params, agent);

test("unlisted or missing agents: native tools blocked, other tools pass", () => {
  for (const id of ["other", undefined, "constructor", "__proto__"]) {
    assert.equal(w(id, "write", { file_path: "/etc/x" }).block, true, String(id));
    assert.equal(w(id, "exec", { command: "ls" }).block, true);
    assert.equal(w(id, "Read", { file_path: "/etc/x" }).block, true);
    assert.equal(w(id, "sessions_list", {}), null);
  }
  assert.equal(w("empty", "write", { file_path: "/etc/x" }), null);
});

test("an empty agents map is a no-op", () => {
  assert.equal(decide({ agents: {} }, "write", { file_path: "/x" }, "anyone"), null);
});

test("missing or invalid config blocks every tool and never throws", () => {
  for (const bad of [undefined, null, {}, "x", [], { agents: [] }, { agents: null }, { agents: { a: 1 } },
    { agents: { a: { denyTools: "write" } } }, { agents: { a: { cliGate: { cli: "rel", mode: "gated", allow: [] } } } },
    { agents: { a: { cliGate: { cli: "/c", mode: "bogus", allow: [] } } } },
    { agents: { a: { cliGate: { cli: "/c", mode: "gated", allow: [], research_root: "rel" } } } }]) {
    const r = decide(bad, "sessions_list", {}, "a");
    assert.equal(r?.block, true, JSON.stringify(bad));
    assert.match(r.blockReason, /invalid guard config/);
  }
});

test("exceptions inside the decision are blocked", () => {
  const evil = { agents: { a: { writeRoots: [root] } } };
  const params = { get file_path() { throw new Error("boom"); } };
  const r = decide(evil, "write", params, "a");
  assert.equal(r.block, true);
  assert.match(r.blockReason, /internal error/);
});

test("denyTools blocks case-insensitively, names plugin tool and agent", () => {
  for (const tool of ["write", "Write", "EDIT", "multi_edit", "notebook_edit", "NotebookEdit", "apply_patch"]) {
    const r = w("rev", tool, { file_path: "/x" });
    assert.equal(r.block, true, tool);
    assert.match(r.blockReason, /g2-agent-guard/);
    assert.match(r.blockReason, new RegExp(tool));
    assert.match(r.blockReason, /rev/);
  }
  assert.equal(w("rev", "read", { file_path: "/x" }), null);
});

test("bash and exec are the same tool", () => {
  assert.equal(w("rev", "exec", { command: "ls" }).block, true);
  assert.equal(w("rev", "Bash", { command: "ls" }).block, true);
  assert.equal(w("both", "exec", { command: "ls" }).block, true);
  assert.equal(w("impl", "exec", { command: "ls" }), null);
  assert.equal(decide({ agents: { a: { denyTools: ["exec"] } } }, "bash", {}, "a").block, true);
});

test("star blocks every tool", () => {
  for (const tool of ["write", "exec", "read", "sessions_list"]) assert.equal(w("star", tool, {}).block, true);
});

test("writeRoots allows inside, blocks outside", () => {
  assert.equal(w("impl", "write", { file_path: path.join(root, "a.txt"), path: path.join(root, "a.txt") }), null);
  assert.equal(w("impl", "write", { file_path: path.join(root, "sub", "deep", "a.txt") }), null);
  assert.equal(w("impl", "write", { file_path: root }), null);
  const r = w("impl", "Edit", { file_path: path.join(outside, "a.txt") });
  assert.equal(r.block, true);
  assert.match(r.blockReason, /outside writeRoots/);
  assert.match(r.blockReason, new RegExp(path.join(outside, "a.txt")));
  assert.equal(w("impl", "write", { file_path: root + "-sibling/a.txt" }).block, true);
});

test("writeRoots does not affect non-file tools", () => {
  assert.equal(w("impl", "read", { file_path: "/etc/passwd" }), null);
  assert.equal(w("impl", "exec", { command: "echo > /etc/x" }), null);
});

test("non-normalized and relative targets are rejected", () => {
  assert.equal(w("impl", "write", { file_path: `${root}/../outside/a.txt` }).block, true);
  assert.equal(w("impl", "write", { file_path: `${root}/x/../ok.txt` }).block, true);
  assert.equal(w("impl", "write", { file_path: `${root}//a.txt` }).block, true);
  assert.equal(w("impl", "write", { file_path: `${root}/./a.txt` }).block, true);
  assert.equal(w("impl", "write", { file_path: "a.txt" }).block, true);
});

test("symlink escapes are blocked", () => {
  assert.equal(w("impl", "write", { file_path: path.join(root, "linkout", "a.txt") }).block, true);
  assert.equal(w("impl", "write", { file_path: path.join(root, "linkout", "new", "a.txt") }).block, true);
  assert.equal(w("impl", "write", { file_path: path.join(root, "dangling") }).block, true);
});

test("dangling link reached through a link to outside (review case) is blocked", () => {
  // L -> <outside>/sub ; L/d -> ../NEW  => resolves to <outside>/NEW
  assert.equal(realResolve(path.join(root, "L", "d")), path.join(outside, "NEW"));
  assert.equal(w("impl", "write", { file_path: path.join(root, "L", "d") }).block, true);
});

test("a root reached through a symlink is resolved", () => {
  const linked = { agents: { a: { writeRoots: [path.join(tmp, "rootlink")] } } };
  assert.equal(decide(linked, "write", { file_path: path.join(root, "a.txt") }, "a"), null);
  assert.equal(decide(linked, "write", { file_path: path.join(outside, "a.txt") }, "a").block, true);
});

test("every path in the event must be inside", () => {
  const good = path.join(root, "a");
  const bad = path.join(outside, "a");
  assert.equal(w("impl", "write", { file_path: good, path: bad }).block, true);
  assert.equal(w("impl", "edit", { file_path: good, edits: [{ file_path: good }, { file_path: bad }] }).block, true);
  assert.equal(w("impl", "multiedit", { file_path: good, edits: [{ file_path: good }] }), null);
  assert.equal(w("impl", "notebook_edit", { notebook_path: bad }).block, true);
  assert.equal(w("impl", "notebook_edit", { notebook_path: good }), null);
});

test("apply_patch targets come from the patch text", () => {
  const ok = `*** Begin Patch\n*** Add File: ${path.join(root, "a")}\n+x\n*** End Patch`;
  const bad = `${ok}\n*** Update File: ${path.join(outside, "b")}`;
  assert.equal(w("impl", "apply_patch", { input: ok }), null);
  assert.equal(w("impl", "apply_patch", { input: bad }).block, true);
  assert.equal(w("impl", "apply_patch", { input: "*** Begin Patch\n*** End Patch" }).block, true);
});

test("malformed events fail closed under writeRoots", () => {
  for (const params of [undefined, null, "x", [], {}, { content: "x" }, { file_path: "" }, { file_path: 3 },
    { file_path: path.join(root, "a"), path: 7 }, { file_path: path.join(root, "a"), edits: "no" },
    { file_path: path.join(root, "a"), edits: [null] }, { edits: [{}] }, { file_path: "relative.txt" }]) {
    assert.equal(w("impl", "write", params)?.block, true, JSON.stringify(params));
  }
});

test("targetPaths and canon", () => {
  assert.equal(canon("Notebook_Edit"), "notebookedit");
  assert.deepEqual(targetPaths("write", { file_path: "/a", path: "/b" }), ["/a", "/b"]);
  assert.equal(targetPaths("write", {}), null);
});

test("plugin registers before_tool_call", () => {
  const handlers = {};
  plugin.register({ pluginConfig: config, on: (name, fn) => { handlers[name] = fn; } });
  assert.equal(plugin.id, "g2-agent-guard");
  assert.equal(handlers.before_tool_call({ toolName: "write", params: {} }, { agentId: "empty" }), undefined);
  assert.equal(handlers.before_tool_call({ toolName: "exec", params: {} }, { agentId: "rev" }).block, true);
  assert.equal(handlers.before_tool_call(undefined, undefined), undefined);
  const broken = {};
  const err = console.error;
  const logged = [];
  console.error = (m) => logged.push(m);
  try {
    plugin.register({ pluginConfig: undefined, on: (name, fn) => { broken[name] = fn; } });
  } finally {
    console.error = err;
  }
  assert.match(logged[0], /invalid config/);
  assert.equal(broken.before_tool_call({ toolName: "sessions_list", params: {} }, { agentId: "x" }).block, true);
});

test("subagent and scheduling tools are blocked by name", () => {
  const names = ["agent", "task", "workflow", "monitor", "enter_worktree", "cron_create", "cron_list", "cron_delete",
    "schedule_wakeup", "send_message", "enter_plan_mode"];
  const c = { agents: { a: { denyTools: names, writeRoots: [root] } } };
  for (const tool of [...names, "Agent", "Task", "EnterPlanMode", "ScheduleWakeup"]) {
    assert.equal(decide(c, tool, {}, "a").block, true, tool);
  }
  assert.equal(decide(c, "write", { file_path: path.join(root, "x") }, "a"), null);
});

test("manifest declares a strict schema with required agents", () => {
  const dir = path.dirname(new URL("../openclaw.plugin.json", import.meta.url).pathname);
  const m = JSON.parse(fs.readFileSync(path.join(dir, "openclaw.plugin.json"), "utf8"));
  assert.equal(m.id, "g2-agent-guard");
  assert.equal(m.activation.onStartup, true);
  assert.equal(m.configSchema.additionalProperties, false);
  assert.deepEqual(m.configSchema.required, ["agents"]);
  const policy = m.configSchema.properties.agents.additionalProperties;
  assert.equal(policy.additionalProperties, false);
  assert.equal(policy.properties.cliGate.additionalProperties, false);
  assert.deepEqual(policy.properties.cliGate.required, ["cli", "mode", "allow"]);
  const pkg = JSON.parse(fs.readFileSync(path.join(dir, "package.json"), "utf8"));
  assert.equal(pkg.type, "module");
  assert.deepEqual(pkg.openclaw.extensions, ["./index.js"]);
});
