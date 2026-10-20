// Pure decision logic for the g2-agent-guard plugin (no OpenClaw imports).
//
// Accepted residual risk (writeRoots TOCTOU): a path is resolved when the hook runs; a
// symlink swapped in between the hook and the tool's write is not caught. The OS sandbox
// and Edit deny rules are the second barrier.
import fs from "node:fs";
import path from "node:path";
import { gateCommand } from "./cmdgate.js";

export const PLUGIN_ID = "g2-agent-guard";

// Names are compared after lowercasing and dropping "_" and "-", so OpenClaw's
// projected "notebook_edit" and Claude's "NotebookEdit" are the same tool.
export function canon(name) {
  return String(name ?? "").toLowerCase().replace(/[_-]/g, "");
}

export const FILE_WRITE_TOOLS = new Set(["write", "edit", "multiedit", "notebookedit", "apply_patch"].map(canon));
// Claude Code's Bash tool reaches OpenClaw's before_tool_call hook as "exec".
const BASH_ALIASES = new Set(["bash", "exec"]);
// Native tools denied for an agent that is not listed (or has no agentId) when agents are configured.
const NATIVE_TOOLS = new Set(
  [
    "exec", "bash", "write", "edit", "multiedit", "notebookedit", "apply_patch", "read", "glob", "grep", "ls",
    "web_fetch", "web_search", "todo_write", "agent", "task", "workflow", "monitor", "enter_worktree",
    "cron_create", "cron_list", "cron_delete", "schedule_wakeup", "send_message", "enter_plan_mode",
  ].map(canon),
);
const PATH_KEYS = ["file_path", "filePath", "path", "notebook_path", "notebookPath"];

function denyMatches(entry, tool) {
  const e = canon(entry);
  if (e === "*") return true;
  if (BASH_ALIASES.has(e)) return BASH_ALIASES.has(tool);
  return e === tool;
}

function isStringArray(v) {
  return Array.isArray(v) && v.every((x) => typeof x === "string" && x !== "");
}

/** Returns an error string for an unusable config, or null. */
export function validateConfig(config) {
  if (!config || typeof config !== "object" || Array.isArray(config)) return "config is not an object";
  const agents = config.agents;
  if (!agents || typeof agents !== "object" || Array.isArray(agents)) return "config.agents is missing";
  for (const [id, policy] of Object.entries(agents)) {
    if (!policy || typeof policy !== "object" || Array.isArray(policy)) return `policy for ${id} is not an object`;
    if (policy.denyTools !== undefined && !isStringArray(policy.denyTools)) return `denyTools for ${id} is invalid`;
    if (policy.writeRoots !== undefined && !isStringArray(policy.writeRoots)) return `writeRoots for ${id} is invalid`;
    const gate = policy.cliGate;
    if (gate !== undefined) {
      if (!gate || typeof gate !== "object" || Array.isArray(gate)) return `cliGate for ${id} is invalid`;
      if (typeof gate.cli !== "string" || !path.isAbsolute(gate.cli)) return `cliGate.cli for ${id} is not absolute`;
      if (gate.mode !== "cli-calls-only" && gate.mode !== "gated") return `cliGate.mode for ${id} is invalid`;
      if (!isStringArray(gate.allow)) return `cliGate.allow for ${id} is invalid`;
      if (gate.required_flags !== undefined) {
        const rf = gate.required_flags;
        if (!rf || typeof rf !== "object" || Array.isArray(rf) || !Object.values(rf).every(isStringArray)) {
          return `cliGate.required_flags for ${id} is invalid`;
        }
      }
      if (gate.research_root !== undefined && (typeof gate.research_root !== "string" || !path.isAbsolute(gate.research_root))) {
        return `cliGate.research_root for ${id} is invalid`;
      }
    }
  }
  return null;
}

// Fully resolves symlinks component by component: link targets are applied to the
// already-resolved parent, dangling links are followed, and a missing tail is appended.
export function realResolve(p) {
  const queue = path.resolve(p).split(path.sep).filter(Boolean);
  let cur = path.sep;
  let hops = 0;
  while (queue.length > 0) {
    const comp = queue.shift();
    if (comp === ".") continue;
    if (comp === "..") {
      cur = path.dirname(cur);
      continue;
    }
    const next = path.join(cur, comp);
    let st;
    try {
      st = fs.lstatSync(next);
    } catch (err) {
      if (err?.code === "ENOENT" || err?.code === "ENOTDIR") {
        cur = next;
        continue;
      }
      throw err;
    }
    if (st.isSymbolicLink()) {
      hops += 1;
      if (hops > 40) throw new Error("too many symlink levels");
      const target = fs.readlinkSync(next);
      if (path.isAbsolute(target)) cur = path.sep;
      queue.unshift(...target.split(path.sep).filter(Boolean));
    } else {
      cur = next;
    }
  }
  return cur;
}

function inside(child, root) {
  const rel = path.relative(root, child);
  return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel));
}

// Every file path the event would write; null means "could not determine" (fail closed).
export function targetPaths(toolName, params) {
  if (!params || typeof params !== "object" || Array.isArray(params)) return null;
  const found = [];
  const take = (obj) => {
    for (const key of PATH_KEYS) {
      if (obj[key] === undefined) continue;
      if (typeof obj[key] !== "string" || obj[key] === "") return false;
      found.push(obj[key]);
    }
    return true;
  };
  if (!take(params)) return null;
  if (params.edits !== undefined) {
    if (!Array.isArray(params.edits)) return null;
    for (const item of params.edits) {
      if (!item || typeof item !== "object" || !take(item)) return null;
    }
  }
  if (canon(toolName) === "applypatch") {
    const text = params.input ?? params.patch;
    if (text !== undefined) {
      if (typeof text !== "string") return null;
      for (const m of text.matchAll(/^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$/gm)) {
        found.push(m[1].trim());
      }
    }
  }
  return found.length > 0 ? found : null;
}

function block(toolName, agentId, why, target) {
  const where = target ? ` path ${target}` : "";
  return {
    block: true,
    blockReason: `${PLUGIN_ID}: ${toolName} is not allowed for agent ${agentId ?? "(unknown)"}${where} (${why})`,
  };
}

function decideInner(config, toolName, params, agentId) {
  const invalid = validateConfig(config);
  if (invalid) return block(toolName, agentId, `invalid guard config: ${invalid}`);
  const tool = canon(toolName);
  if (Object.keys(config.agents).length === 0) return null; // nothing configured (codex profile)
  const known = typeof agentId === "string" && Object.hasOwn(config.agents, agentId);
  if (!known) {
    return NATIVE_TOOLS.has(tool) ? block(toolName, agentId, "agent is not listed in the guard config") : null;
  }
  const policy = config.agents[agentId];
  if (policy.denyTools?.some((entry) => denyMatches(entry, tool))) {
    return block(toolName, agentId, "denied by denyTools");
  }
  const commandLike = BASH_ALIASES.has(tool) || typeof params?.command === "string";
  if (policy.cliGate && commandLike) {
    const why = gateCommand(policy.cliGate, params && typeof params === "object" ? params.command : undefined);
    if (why) return block(toolName, agentId, why);
  }
  if (policy.writeRoots && FILE_WRITE_TOOLS.has(tool)) {
    const targets = targetPaths(toolName, params);
    if (!targets) return block(toolName, agentId, "cannot determine the target path");
    const roots = policy.writeRoots.map((root) => realResolve(root));
    for (const target of targets) {
      if (!path.isAbsolute(target) || path.normalize(target) !== target) {
        return block(toolName, agentId, "path is not absolute and normalized", target);
      }
      if (!roots.some((root) => inside(realResolve(target), root))) {
        return block(toolName, agentId, "outside writeRoots", target);
      }
    }
  }
  return null;
}

/** Returns {block:true, blockReason} or null when the call may proceed. Never throws. */
export function decide(config, toolName, params, agentId) {
  try {
    return decideInner(config, toolName, params, agentId);
  } catch (err) {
    return block(toolName, agentId, `internal error: ${err?.message ?? err}`);
  }
}
