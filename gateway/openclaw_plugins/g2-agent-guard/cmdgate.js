// Conservative shell-command gate for the trusted research CLI (no OpenClaw imports).
// Anything that cannot be parsed confidently while mentioning the CLI is blocked.
import fs from "node:fs";
import path from "node:path";

export class Unparseable extends Error {}

const MAX_DEPTH = 8;
const RESERVED = new Set(["{", "}", "!", "if", "then", "elif", "else", "fi", "while", "until", "do", "done", "for", "select", "time", "coproc"]);
const SHELLS = new Set(["sh", "bash", "zsh", "dash", "ksh", "ash", "fish", "busybox"]);
const INTERPRETERS = new Set(["perl", "ruby", "node", "nodejs", "php", "lua", "tclsh", "awk", "gawk", "mawk", "osascript"]);
// Commands that run another command given in their arguments.
const WRAPPERS = new Set([
  "env", "command", "builtin", "exec", "nohup", "setsid", "time", "sudo", "doas", "nice", "ionice", "stdbuf",
  "timeout", "xargs", "unbuffer", "chroot", "flock", "script", "watch", "strace", "ltrace", "taskset", "chrt",
  "uv", "uvx", "pipx", "poetry", "pdm", "hatch", "npx", "pnpm", "npm", "yarn", "make",
]);
const FLAGS_WITH_ARG = new Set(["-n", "-u", "-g", "-C", "-s", "-k", "-t", "-S", "-c", "-p", "-d", "-w", "-e", "-P", "-i"]);
const UV_FLAGS_WITH_ARG = new Set(["--with", "--project", "--directory", "-p", "--python", "--package", "--extra", "--group", "--env-file", "--with-requirements", "--index", "--cache-dir", "--config-file"]);

// ---------------------------------------------------------------- tokenizer

function isNameStart(c) {
  return c !== undefined && /[A-Za-z_]/.test(c);
}

function ansiC(s) {
  return s.replace(/\\(x[0-9a-fA-F]{1,2}|[0-7]{1,3}|[uU][0-9a-fA-F]{1,8}|.)/g, (_, e) => {
    if (e[0] === "x") return String.fromCharCode(parseInt(e.slice(1), 16));
    if (e[0] === "u" || e[0] === "U") {
      const cp = parseInt(e.slice(1), 16);
      return cp <= 0x10ffff ? String.fromCodePoint(cp) : "";
    }
    if (/^[0-7]+$/.test(e)) return String.fromCharCode(parseInt(e, 8));
    return { n: "\n", t: "\t", r: "\r", a: "\x07", b: "\b", e: "\x1b", f: "\f", v: "\v" }[e] ?? e;
  });
}

/** Returns every simple command (argv of {t, dyn, glob}) found, nested ones included. */
export function parseCommands(text) {
  const out = [];
  const s = String(text);
  parseList(s, 0, null, 0, out);
  return out;
}

function matchClose(s, i, open, close) {
  // s[i-1] is the opener; returns index of the matching closer, honouring quotes.
  let depth = 1;
  for (; i < s.length; i += 1) {
    const c = s[i];
    if (c === "\\") i += 1;
    else if (c === "'") {
      const j = s.indexOf("'", i + 1);
      if (j < 0) throw new Unparseable("unterminated quote");
      i = j;
    } else if (c === '"') {
      i += 1;
      while (i < s.length && s[i] !== '"') {
        if (s[i] === "\\") i += 1;
        i += 1;
      }
      if (i >= s.length) throw new Unparseable("unterminated quote");
    } else if (c === open) depth += 1;
    else if (c === close) {
      depth -= 1;
      if (depth === 0) return i;
    }
  }
  throw new Unparseable(`unterminated ${open}`);
}

function parseList(s, start, closer, depth, out) {
  if (depth > MAX_DEPTH) throw new Unparseable("nesting too deep");
  let i = start;
  let argv = [];
  let buf = "";
  let has = false;
  let dyn = false;
  let glob = false;
  let dropNext = false;

  const endWord = () => {
    if (has) {
      if (dropNext) dropNext = false;
      else argv.push({ t: buf, dyn, glob });
    }
    buf = "";
    has = false;
    dyn = false;
    glob = false;
  };
  const endCommand = () => {
    endWord();
    if (argv.length > 0) out.push(argv);
    argv = [];
  };
  const nested = (inner) => {
    parseList(inner, 0, null, depth + 1, out);
  };
  // Handles "$" at s[i] (outside single quotes); returns new i (index of last consumed char).
  const dollar = (inDouble) => {
    const n = s[i + 1];
    if (n === "(") {
      if (s[i + 2] === "(") {
        const j = matchClose(s, i + 2, "(", ")");
        dyn = true;
        has = true;
        return j;
      }
      const j = matchClose(s, i + 2, "(", ")");
      nested(s.slice(i + 2, j));
      dyn = true;
      has = true;
      return j;
    }
    if (n === "{") {
      const j = matchClose(s, i + 2, "{", "}");
      nested(s.slice(i + 2, j));
      dyn = true;
      has = true;
      return j;
    }
    if (!inDouble && n === "'") return i; // handled by caller
    if (isNameStart(n) || (n !== undefined && /[0-9@*#?!$-]/.test(n))) {
      dyn = true;
      has = true;
      return i + (isNameStart(n) ? s.slice(i + 1).match(/^[A-Za-z0-9_]+/)[0].length : 1);
    }
    buf += "$";
    has = true;
    return i;
  };

  while (i < s.length) {
    const c = s[i];
    if (c === " " || c === "\t") {
      endWord();
    } else if (c === "\n" || c === "\r") {
      endCommand();
    } else if (c === "\\") {
      if (s[i + 1] === "\n") i += 1;
      else if (i + 1 < s.length) {
        buf += s[i + 1];
        has = true;
        i += 1;
      } else throw new Unparseable("trailing backslash");
    } else if (c === "'") {
      const j = s.indexOf("'", i + 1);
      if (j < 0) throw new Unparseable("unterminated quote");
      buf += s.slice(i + 1, j);
      has = true;
      i = j;
    } else if (c === '"') {
      has = true;
      i += 1;
      for (;;) {
        if (i >= s.length) throw new Unparseable("unterminated quote");
        const d = s[i];
        if (d === '"') break;
        if (d === "\\") {
          const e = s[i + 1];
          if (e === undefined) throw new Unparseable("unterminated quote");
          if (!/["\\$`\n]/.test(e)) buf += "\\";
          if (e !== "\n") buf += e;
          i += 2;
          continue;
        }
        if (d === "$") i = dollar(true) + 1;
        else if (d === "`") {
          const j = findBacktick(s, i + 1);
          nested(s.slice(i + 1, j).replace(/\\`/g, "`"));
          dyn = true;
          i = j + 1;
        } else {
          buf += d;
          i += 1;
        }
      }
    } else if (c === "$" && s[i + 1] === "'") {
      const j = (() => {
        let k = i + 2;
        while (k < s.length && s[k] !== "'") k += s[k] === "\\" ? 2 : 1;
        if (k >= s.length) throw new Unparseable("unterminated quote");
        return k;
      })();
      buf += ansiC(s.slice(i + 2, j));
      has = true;
      i = j;
    } else if (c === "$" && s[i + 1] === '"') {
      i += 0; // $"..." behaves as "..."; drop the $ and let the quote branch run
      s = s.slice(0, i) + s.slice(i + 1);
      continue;
    } else if (c === "$") {
      i = dollar(false);
    } else if (c === "`") {
      const j = findBacktick(s, i + 1);
      nested(s.slice(i + 1, j).replace(/\\`/g, "`"));
      dyn = true;
      has = true;
      i = j;
    } else if (c === "#" && !has) {
      while (i < s.length && s[i] !== "\n") i += 1;
      continue;
    } else if (c === ";" || c === "|" || c === "&") {
      if (c === "&" && s[i + 1] === ">") {
        endWord();
        dropNext = true;
        i += s[i + 2] === ">" ? 2 : 1;
      } else {
        endCommand();
        if (s[i + 1] === c || (c === "|" && s[i + 1] === "&") || (c === ";" && s[i + 1] === "&")) i += 1;
      }
    } else if (c === "<" || c === ">") {
      if (s[i + 1] === "(") {
        const j = matchClose(s, i + 2, "(", ")");
        nested(s.slice(i + 2, j));
        i = j;
      } else {
        if (has && /^[0-9]+$/.test(buf) && !dyn) {
          buf = "";
          has = false;
        } else endWord();
        if (c === "<" && s[i + 1] === "<") throw new Unparseable("heredoc or here-string");
        dropNext = true;
        if (s[i + 1] === c || s[i + 1] === "&") i += 1;
      }
    } else if (c === "(") {
      if (has || argv.length > 0) throw new Unparseable("unexpected (");
      const j = matchClose(s, i + 1, "(", ")");
      parseList(s.slice(i + 1, j), 0, null, depth + 1, out);
      i = j;
    } else if (c === ")") {
      throw new Unparseable("unmatched )");
    } else {
      if (c === "*" || c === "?" || c === "[") glob = true;
      if (c === "{" && has) dyn = true;
      buf += c;
      has = true;
    }
    i += 1;
  }
  if (closer !== null) throw new Unparseable("unterminated group");
  endCommand();
  return i;
}

function findBacktick(s, i) {
  for (; i < s.length; i += 1) {
    if (s[i] === "\\") i += 1;
    else if (s[i] === "`") return i;
  }
  throw new Unparseable("unterminated backtick");
}

// ---------------------------------------------------------------- analysis

function stripPrefix(argv) {
  let k = 0;
  for (;;) {
    const w = argv[k];
    if (!w) break;
    if (/^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=/.test(w.t) || (RESERVED.has(w.t) && !w.dyn)) k += 1;
    else break;
  }
  return argv.slice(k);
}

function skipFlags(args, { positional = 0, valueFlags = FLAGS_WITH_ARG } = {}) {
  let k = 0;
  while (k < args.length) {
    const a = args[k];
    if (a.dyn) throw new Unparseable("dynamic wrapper argument");
    if (a.t === "--") {
      k += 1;
      break;
    }
    if (a.t.startsWith("-") && a.t.length > 1) {
      k += valueFlags.has(a.t) ? 2 : 1;
    } else if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(a.t)) {
      k += 1;
    } else break;
  }
  return args.slice(k + positional);
}

function isPython(base) {
  return /^(python|pypy)[0-9.]*$/.test(base);
}

/**
 * Resolve one simple command to the gated invocations it performs.
 * Returns an array of {kind, args} where args are the words after the CLI/module.
 * Throws Unparseable when it cannot tell.
 */
function resolveCommand(argv, ctx, depth = 0) {
  if (depth > MAX_DEPTH) throw new Unparseable("wrapper nesting too deep");
  const found = [];
  const words = stripPrefix(argv);
  if (words.length === 0) return found;
  const head = words[0];
  if (head.dyn || head.glob) throw new Unparseable("dynamic command word");
  const base = path.basename(head.t);
  const rest = words.slice(1);

  if (base === "gateway-cli" || isCliPath(head.t, ctx)) {
    found.push({ kind: "cli", args: rest });
    return found;
  }
  if (isPython(base)) {
    let k = 0;
    for (; k < rest.length; k += 1) {
      const a = rest[k].t;
      if (a === "-m" || (a.startsWith("-m") && a.length > 2 && !a.startsWith("--"))) {
        const mod = a === "-m" ? rest[k + 1] : { t: a.slice(2), dyn: rest[k].dyn };
        if (!mod || mod.dyn) throw new Unparseable("dynamic python module");
        const tail = rest.slice(a === "-m" ? k + 2 : k + 1);
        if (mod.t === "gateway" || mod.t.startsWith("gateway.")) {
          found.push({ kind: "module", module: mod.t, args: tail });
        } else if (rest.slice(k).some((w) => ctx.mentions(w.t))) {
          throw new Unparseable("python module run mentions the CLI");
        }
        return found;
      }
      if (a === "-c" || (/^-[A-Za-z]*c$/.test(a) && !a.startsWith("--"))) {
        const code = rest[k + 1];
        if (!code || code.dyn || /gateway/.test(code.t)) throw new Unparseable("python -c code");
        return found;
      }
      if (a === "-") throw new Unparseable("python reading stdin");
      if (!a.startsWith("-")) {
        if (rest.slice(k).some((w) => /gateway/.test(w.t))) throw new Unparseable("python script argument mentions gateway");
        return found;
      }
    }
    throw new Unparseable("python reading stdin");
  }
  if (SHELLS.has(base)) {
    let k = 0;
    for (; k < rest.length; k += 1) {
      const a = rest[k];
      if (a.dyn) throw new Unparseable("dynamic shell argument");
      if (/^-[A-Za-z]*c[A-Za-z]*$/.test(a.t) || a.t === "-c") {
        const script = rest[k + 1];
        if (!script || script.dyn) throw new Unparseable("dynamic shell script");
        const sub = [];
        parseList(script.t, 0, null, depth + 1, sub);
        for (const cmd of sub) found.push(...resolveCommand(cmd, ctx, depth + 1));
        return found;
      }
      if (a.t === "--" || (a.t.startsWith("-") && a.t.length > 1) || a.t.startsWith("+")) continue;
      // `sh script.sh args`: the script body is not visible; fall through to the argument scan.
      if (rest.slice(k).some((w) => ctx.mentions(w.t))) throw new Unparseable("shell script argument mentions the CLI");
      return found;
    }
    throw new Unparseable("shell reading stdin");
  }
  if (base === "eval") {
    if (rest.some((w) => w.dyn)) throw new Unparseable("dynamic eval");
    const sub = [];
    parseList(rest.map((w) => w.t).join(" "), 0, null, depth + 1, sub);
    for (const cmd of sub) found.push(...resolveCommand(cmd, ctx, depth + 1));
    return found;
  }
  if (base === "source" || base === ".") {
    if (rest.some((w) => ctx.mentions(w.t))) throw new Unparseable("source mentions the CLI");
    return found;
  }
  if (INTERPRETERS.has(base)) {
    if (rest.some((w) => w.dyn || ctx.mentions(w.t) || /gateway/.test(w.t))) throw new Unparseable("interpreter mentions the CLI");
    return found;
  }
  if (WRAPPERS.has(base)) {
    let inner;
    if (base === "uv" || base === "uvx" || base === "pipx" || base === "poetry" || base === "pdm" || base === "hatch" || base === "npx" || base === "pnpm" || base === "npm" || base === "yarn") {
      let args = rest;
      if (base === "uv" || base === "poetry" || base === "pdm" || base === "hatch" || base === "pipx" || base === "pnpm" || base === "npm" || base === "yarn") {
        if (!args[0] || !/^(run|exec|x|dlx)$/.test(args[0].t)) {
          if (rest.some((w) => ctx.mentions(w.t))) throw new Unparseable("package manager mentions the CLI");
          return found;
        }
        args = args.slice(1);
      }
      inner = skipFlags(args, { valueFlags: UV_FLAGS_WITH_ARG });
    } else if (base === "make") {
      if (rest.some((w) => w.dyn || ctx.mentions(w.t))) throw new Unparseable("make mentions the CLI");
      return found;
    } else {
      inner = skipFlags(rest, { positional: base === "timeout" || base === "flock" || base === "watch" ? 1 : 0 });
    }
    if (inner.length > 0) found.push(...resolveCommand(inner, ctx, depth + 1));
    // Flags with unknown arity may hide the real command: scan the whole tail too.
    for (let k = 0; k < rest.length; k += 1) {
      const w = rest[k];
      if (w.dyn) continue;
      if (w.t === "gateway-cli" || path.basename(w.t) === "gateway-cli" || isCliPath(w.t, ctx)) {
        found.push({ kind: "cli", args: rest.slice(k + 1) });
      }
    }
    return found;
  }
  return found;
}

function isCliPath(text, ctx) {
  if (!ctx.cli || !text.includes("/")) return false;
  const abs = path.resolve(text);
  if (abs === ctx.cli) return true;
  try {
    return ctx.realCli !== null && fs.realpathSync(abs) === ctx.realCli;
  } catch {
    return false;
  }
}

function makeContext(cli) {
  const abs = path.resolve(cli);
  let realCli = null;
  try {
    realCli = fs.realpathSync(abs);
  } catch {
    realCli = null;
  }
  const venvRoot = path.join(path.dirname(path.dirname(path.dirname(abs))), ".venv");
  const needles = ["gateway-cli", "gateway.research", "gateway.cli", abs, venvRoot].map((n) => n.toLowerCase());
  return {
    cli: abs,
    realCli,
    mentions(text) {
      const plain = String(text).toLowerCase();
      const squashed = plain.replace(/['"\\`${}]/g, "");
      return needles.some((n) => plain.includes(n) || squashed.includes(n)) || /-m\s*gateway/.test(squashed);
    },
  };
}

const PLAIN_WORD = /^[A-Za-z0-9_./=:@,+%-]+$/;

/**
 * Strict form: the whole string is ONE simple command made only of plain literal words
 * (unquoted [A-Za-z0-9_./=:@,+%-]+ or a single-quoted word without "'"). Returns argv or null.
 * This is the only form that can ever be ALLOWED; everything else is judged by detectors that only block.
 */
export function strictArgv(command) {
  if (typeof command !== "string" || /[\n\r\0]/.test(command)) return null;
  const argv = [];
  let i = 0;
  const n = command.length;
  const isSep = (c) => c === " " || c === "\t";
  for (;;) {
    while (i < n && isSep(command[i])) i += 1;
    if (i >= n) break;
    if (command[i] === "'") {
      const j = command.indexOf("'", i + 1);
      if (j < 0 || (j + 1 < n && !isSep(command[j + 1]))) return null;
      argv.push(command.slice(i + 1, j));
      i = j + 1;
    } else {
      let j = i;
      while (j < n && !isSep(command[j])) j += 1;
      const word = command.slice(i, j);
      if (!PLAIN_WORD.test(word)) return null;
      argv.push(word);
      i = j;
    }
  }
  return argv.length > 0 ? argv : null;
}

function rootMatches(value, root) {
  if (value === root) return true;
  if (!path.isAbsolute(value) || !path.isAbsolute(root)) return false;
  try {
    return fs.realpathSync(value) === fs.realpathSync(root);
  } catch {
    return false;
  }
}

function checkStrictCall(gate, argv) {
  if (argv[1] !== "research") return "only `research` subcommands of the CLI are allowed";
  const allow = new Set(Array.isArray(gate.allow) ? gate.allow : []);
  const sub = argv[2];
  if (sub === undefined || !allow.has(sub)) return `research CLI command "${sub ?? "(none)"}" is not allowed`;
  const required = gate.required_flags?.[sub];
  if (Array.isArray(required)) {
    for (const flag of required) {
      if (!argv.slice(3).includes(flag)) return `research CLI command "${sub}" requires ${flag}`;
    }
  }
  if (typeof gate.research_root === "string") {
    for (let k = 3; k < argv.length; k += 1) {
      let value = null;
      if (argv[k] === "--root") value = argv[k + 1] ?? "";
      else if (argv[k].startsWith("--root=")) value = argv[k].slice(7);
      if (value !== null && !rootMatches(value, gate.research_root)) {
        return `--root ${value} is not the active research root`;
      }
    }
  }
  return null;
}

/**
 * Returns null if the command may run, or a short reason string if it must be blocked.
 * gate: {cli, mode: "cli-calls-only"|"gated", allow: [subcommands], research_root?}
 */
export function gateCommand(gate, command) {
  if (typeof command !== "string" || command.trim() === "") return "missing command text";
  const ctx = makeContext(gate.cli);
  const argv = strictArgv(command);
  if (argv !== null && argv[0] === gate.cli) return checkStrictCall(gate, argv);
  if (gate.mode !== "gated") return "only exact `<cli> research <subcommand> ...` calls are allowed for this agent";
  if (ctx.mentions(command)) return "command mentions the research CLI but is not an allowed exact call";
  try {
    for (const cmd of parseCommands(command)) {
      if (resolveCommand(cmd, ctx).length > 0) return "command invokes the research CLI";
    }
  } catch (err) {
    if (!(err instanceof Unparseable)) return "cannot analyse command";
    if (err.message === "dynamic command word") return "dynamic command word (cannot prove it is not the research CLI)";
    return null; // unparseable and no mention: not a research CLI call
  }
  return null;
}
