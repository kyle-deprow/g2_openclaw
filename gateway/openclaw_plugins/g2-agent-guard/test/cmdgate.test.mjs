import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test, { after } from "node:test";
import { decide } from "../index.js";
import { gateCommand, strictArgv } from "../cmdgate.js";

const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "cmdgate-")));
after(() => fs.rmSync(tmp, { recursive: true, force: true }));
const repo = path.join(tmp, "repo");
const CLI = path.join(repo, ".venv", "bin", "gateway-cli");
const ROOT = path.join(tmp, "research-root");
fs.mkdirSync(path.dirname(CLI), { recursive: true });
fs.mkdirSync(ROOT);
fs.writeFileSync(CLI, "#!/bin/sh\n");
fs.chmodSync(CLI, 0o755);
fs.symlinkSync(CLI, path.join(tmp, "alias"));
fs.symlinkSync(ROOT, path.join(tmp, "root-link"));

const OWNER_ALLOW = ["status", "run", "pause", "attempt-open", "review-reserve"];
const owner = { cli: CLI, mode: "gated", allow: OWNER_ALLOW, research_root: ROOT, required_flags: { pause: ["--owner"] } };
const runner = { cli: CLI, mode: "cli-calls-only", allow: ["run"], research_root: ROOT };
const impl = { cli: CLI, mode: "gated", allow: [] };
const none = { cli: CLI, mode: "cli-calls-only", allow: [] };
const g = (gate, cmd) => gateCommand(gate, cmd);

test("strictArgv accepts only plain literal single commands", () => {
  assert.deepEqual(strictArgv("a b/c 'd e' f=g:h@i,j+k%l-m"), ["a", "b/c", "d e", "f=g:h@i,j+k%l-m"]);
  assert.deepEqual(strictArgv("  a\tb  "), ["a", "b"]);
  for (const bad of ["", "   ", "a $X", "a `x`", "a {b,c}", "a *", "a ?", "a [x]", "~/x", "a ~", "a;b", "a&b", "a&&b",
    "a||b", "a|b", "a\nb", "a\rb", "a > b", "a >| b", "a <> b", "a<b", "{fd}>x a", "a &> b", "a 2>&1", "a $'x'",
    "a <<EOF", "a # c", "A=1 a", "a \"b\"", "a 'b'c", "a b'c d'", "a 'b", "a\\ b", "a (b)", "a !b", "a ^b", "a é"]) {
    if (bad === "A=1 a") continue; // an assignment is a plain word; rejected later because argv[0] != CLI
    assert.equal(strictArgv(bad), null, JSON.stringify(bad));
  }
});

test("allowed exact calls", () => {
  for (const cmd of [
    `${CLI} research status`,
    `${CLI} research status --root ${ROOT}`,
    `${CLI} research status --root=${ROOT}`,
    `${CLI} research status --root ${path.join(tmp, "root-link")}`,
    `  ${CLI}   research   status  --help `,
    `${CLI}\tresearch\tstatus`,
    `'${CLI}' 'research' 'status' --reason 'costs $5; fine'`,
    `${CLI} research pause --owner --reason because`,
  ]) assert.equal(g(owner, cmd), null, cmd);
  assert.equal(g(runner, `${CLI} research run H0001-A1 --root ${ROOT}`), null);
});

test("exact-call failures", () => {
  for (const cmd of [
    `${CLI} research`,
    `${CLI} research --help`,
    `${CLI} research -h`,
    `${CLI} research resume`,
    `${CLI} research init`,
    `${CLI} research serve`,
    `${CLI} research operator-note`,
    `${CLI} research Status`,
    `${CLI} research status-x`,
    `${CLI} --help`,
    `${CLI}`,
    `${CLI} research-status`,
    `${CLI} other status`,
    `${CLI} -x research status`,
    `${repo}/.venv/bin/../bin/gateway-cli research status`,
    `${repo}/.venv/bin//gateway-cli research status`,
    `./gateway-cli research status`,
    `gateway-cli research status`,
    `${path.join(tmp, "alias")} research status`,
    `FOO=1 ${CLI} research status`,
    `env ${CLI} research status`,
  ]) assert.ok(g(owner, cmd), cmd);
});

test("required flags: pause needs --owner", () => {
  assert.match(g(owner, `${CLI} research pause --root ${ROOT} --reason x`) ?? "", /requires --owner/);
  assert.equal(g(owner, `${CLI} research pause --root ${ROOT} --owner --reason x`), null);
  assert.ok(g(owner, `${CLI} research pause --root ${ROOT} --owner-x --reason x`));
  assert.equal(g(owner, `${CLI} research status --root ${ROOT}`), null);
});

test("ansi-c \\U escapes are decoded by the detector", () => {
  assert.ok(g(impl, `$'\\U00000067ateway-cli' research status`));
  assert.equal(g(impl, `echo $'\\U0001f600' $'\\UFFFFFFFF'`), null); // no crash, argument only
});

test("--root pinning", () => {
  for (const cmd of [
    `${CLI} research status --root /etc`,
    `${CLI} research status --root=${ROOT}x`,
    `${CLI} research status --root ${ROOT}/..`,
    `${CLI} research status --root`,
    `${CLI} research status --root ${ROOT} --root /other`,
  ]) {
    assert.match(g(owner, cmd) ?? "", /active research root/, cmd);
    assert.ok(g(runner, cmd.replace("status", "run")), cmd);
  }
  assert.equal(g({ ...owner, research_root: undefined }, `${CLI} research status --root /anything`), null);
});

test("the four must-fix bypass strings are blocked", () => {
  for (const gate of [owner, runner, impl]) {
    for (const cmd of [
      `${CLI} research {resume,} --help`,
      `${CLI} {fd}>x research resume`,
      `${CLI} >| x research resume`,
      `${CLI} research $'\\U00000072esume'`,
      `${CLI} research $'\\x72esume' --root ${ROOT}`,
    ]) assert.ok(g(gate, cmd), `${cmd} / ${gate.mode}`);
  }
});

test("chains, wrappers and obfuscation that touch the CLI are blocked in gated mode", () => {
  const wrapped = (c) => [
    c, `env A=1 ${c}`, `sh -c '${c}'`, `bash -lc "${c}"`, `echo hi; ${c}`, `true && ${c}`, `false || ${c}`, `echo | ${c}`,
    `echo $(${c})`, `echo \`${c}\``, `echo\n${c}`, `(${c})`, `{ ${c}; }`, `timeout 5 ${c}`, `nohup ${c} &`, `eval "${c}"`,
    `uv run ${c}`, `sudo -u root ${c}`, `nice -n 5 ${c}`, `if true; then ${c}; fi`, `command ${c}`, `exec ${c}`,
    `xargs ${c}`, `time -p ${c}`, `f() { ${c}; }; f`, `find . -exec ${c} \;`, `env -S '${c}'`, `script -qc '${c}' /dev/null`,
    `${c} > out.txt`, `${c} 2>&1`, `${c} | cat`, `cd /tmp && ${c}`, `FOO=1 ${c}`, `echo '${c}' | sh`,
  ];
  for (const gate of [owner, impl]) {
    for (const cmd of wrapped(`${CLI} research resume --root ${ROOT}`)) assert.ok(g(gate, cmd), cmd);
  }
  // even allowed subcommands are blocked when not an exact call
  for (const cmd of wrapped(`${CLI} research status --root ${ROOT}`).slice(1)) assert.ok(g(owner, cmd), cmd);
  for (const cmd of [
    `g''ateway-cli research status`, `"gateway-"cli research status`, `gateway\\-cli research status`,
    `$'\\x67ateway-cli' research status`, `GATEWAY-CLI research status`, `${CLI.toUpperCase()} research status`,
    `.venv/bin/gateway-cli research status`, `python -m gateway.cli research status`, `python3 -mgateway.cli research status`,
    `${repo}/.venv/bin/python -m gateway.research.cli status`, `python -c "import gateway.research.cli"`,
    `${repo}/.venv/bin/python x.py`, `cat ${repo}/.venv/bin/activate`, `echo gateway.research`,
    `perl -e 'system("gateway-cli research status")'`, `bash <<EOF\n${CLI} research status\nEOF`,
    `f() { ${CLI} research status; }; f`, `${path.join(tmp, "alias")} research status`,
  ]) assert.ok(g(owner, cmd), cmd);
});

test("dynamic command words are refused in gated mode", () => {
  for (const cmd of [`$X research status`, `c=gateway; \${c}-cli research status`, `$(echo x) research status`, `/x/gateway-c* a`]) {
    assert.ok(g(impl, cmd), cmd);
  }
});

test("gated mode lets unrelated commands through", () => {
  for (const cmd of [`ls -la`, `git status && pytest -q`, `grep -rn foo src | head`, `python3 -m pytest tests -q`, `cat <<EOF\nhello\nEOF`,
    `echo "unterminated`, `echo $HOME`, `git add gateway/research/x.py`, `.venv/bin/pytest -q`, `uv run pytest -q`, `echo hi > out.txt`]) {
    assert.equal(g(owner, cmd), null, cmd);
    assert.equal(g(impl, cmd), null, cmd);
  }
});

test("cli-calls-only mode blocks every non-CLI command", () => {
  for (const gate of [runner, none]) {
    for (const cmd of [`echo hi`, `ls`, `true`, `echo hi > x`, `sh -c 'echo hi'`, `python3 -c 'print(1)'`, ``, `   `, `cat /etc/passwd`,
      `git status`, `echo $(date)`]) assert.ok(g(gate, cmd), JSON.stringify(cmd));
  }
  assert.ok(g(runner, `${CLI} research status`));
  assert.ok(g(none, `${CLI} research run A`));
  assert.ok(g(runner, `${CLI} research run A; echo hi`));
  assert.ok(g(runner, `${CLI} research run A && echo hi`));
  assert.ok(g(runner, `${CLI} research run A | tee x`));
});

test("unknown mode fails closed", () => {
  assert.ok(g({ cli: CLI, allow: ["status"] }, `echo hi`));
});

test("decide() applies the gate to any event with a string command", () => {
  const cfg = { agents: { a: { cliGate: runner }, b: { denyTools: ["write"] }, i: { cliGate: impl } } };
  assert.equal(decide(cfg, "exec", { command: `${CLI} research run A --root ${ROOT}` }, "a"), null);
  const r = decide(cfg, "Bash", { command: `${CLI} research status` }, "a");
  assert.equal(r.block, true);
  assert.match(r.blockReason, /g2-agent-guard/);
  assert.match(r.blockReason, /agent a/);
  assert.match(r.blockReason, /"status"/);
  assert.equal(decide(cfg, "some_other_tool", { command: `${CLI} research resume` }, "i").block, true);
  assert.equal(decide(cfg, "some_other_tool", { command: `ls` }, "i"), null);
  assert.equal(decide(cfg, "read", { file_path: "/x" }, "a"), null);
  assert.equal(decide(cfg, "exec", { command: `${CLI} research status` }, "b"), null);
  assert.equal(decide(cfg, "exec", {}, "a").block, true);
  assert.equal(decide(cfg, "exec", undefined, "a").block, true);
  assert.equal(decide(cfg, "exec", { command: 7 }, "a").block, true);
});
