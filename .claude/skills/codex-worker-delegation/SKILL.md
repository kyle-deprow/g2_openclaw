---
name: codex-worker-delegation
description: Delegate implementation work from Claude (orchestrator) to Codex CLI workers running gpt-5.6-luna at xhigh reasoning. Use when dispatching coding tasks to workers, scoping worker prompts, running review rounds on worker output, or debugging codex exec invocations.
---

# Codex Worker Delegation

Claude (Fable) is the **orchestrator**; implementation work is dispatched to **Codex CLI workers** running `gpt-5.6-luna` at `xhigh` reasoning effort. Luna is dirt cheap, so liberal delegation and multiple review rounds are affordable — but it requires strict guidance, explicit requirements, and reasonably scoped work. The orchestrator plans, scopes, dispatches, reviews, and integrates; it does not hand-implement work a worker can do.

**Verified against:** codex-cli `0.144.4` (smoketested 2026-08-01; `fast_mode` smoketested 2026-08-03 in this environment).

## Canonical invocation

```bash
codex exec --yolo \
  -m gpt-5.6-luna \
  -c model_reasoning_effort="xhigh" \
  -c features.fast_mode=true \
  --cd <absolute-workdir> \
  -o <run-dir>/last-message.txt \
  "<strict prompt>"
```

- `--yolo` is a hidden but working alias for `--dangerously-bypass-approvals-and-sandbox` on `codex exec` — no approval prompts, no sandbox. It works precisely because the orchestrator supplies the guardrails: tight prompt, pinned `--cd`, post-run review.
- `-c model_reasoning_effort="xhigh"` — config override, TOML-parsed value.
- `-c features.fast_mode=true` — enables codex's `fast_mode` feature (a `stable`-stage flag, see `codex features list`) for this invocation only. Pass it inline rather than `codex features enable fast_mode`, which persists to the global `~/.codex/config.toml` — that's not this delegation path's config to change.
- `--cd <dir>` pins the worker's working root. Add extra writable roots only with `--add-dir`.
- `-o <file>` captures the worker's final message for mechanical checking (e.g. a `DONE` sentinel).
- Outside a git repo add `--skip-git-repo-check`. Use `--output-schema <file.json>` when you need a structured JSON artifact instead of prose. `--ephemeral` for throwaway probes.
- Follow-up rounds: `codex exec resume --last` (or `resume <session-id>`) continues the same worker session with its context intact — use it for fix rounds instead of re-explaining.

## Stall-proof long dispatches

Harness-tracked background tasks can be killed environmentally mid-run
(concurrent Claude sessions, resource pressure) — treat a kill as noise, not
operator intent. For any worker run longer than a couple of minutes:

1. **Detach via files.** Prompt in a file, launcher in a script file, then
   `setsid nohup <launcher.sh> > <run-dir>/<run>.log 2>&1 & disown`, with
   the launcher ending in `echo "CODEX_EXIT:$?"`. Never assemble the
   detached command from inline nested quoting — a mis-quoted launcher
   wedges silently and is indistinguishable from a slow worker. Empty log +
   no `pgrep -f "codex exec"` process = wedged launcher; relaunch from
   files.
2. **Monitor the marker AND the process.** Poll the log for `CODEX_EXIT:`
   and emit a distinct event if the codex process disappears without it —
   completion-only watches are silent on crashes. A killed watcher does not
   kill detached codex: re-arm and read the log rather than restarting a
   live run.
3. **Capture logs unfiltered.** No `| tail`/`| grep` between the worker and
   its log file; filter when reading. Truncated capture windows have eaten
   the one traceback that mattered (a single `[SQL: ...]` echo can exceed
   40 lines).
4. **Pre-sync toolchains from the orchestrator** (venv/node_modules) before
   the first dispatch, and tell workers the fallback (`.venv/bin/<tool>`)
   for read-only-cache failures so they report instead of stalling.
5. **After a zero-output kill:** verify the worktree, relaunch fresh; after
   partial work, `codex exec resume --last` with "re-check on-disk state
   and finish the remaining items."

## Prompt contract (luna needs all of these)

Every dispatch prompt MUST contain:

1. **Exact file list** — which files to create/modify, by path. Name what must NOT be touched.
2. **Precise requirements** — signatures, types, behaviors, edge cases, error handling. Luna implements exactly what is written; it does not infer intent well.
3. **Scope fence** — "No other files, no README, no tests unless asked, no git commits" (or the inverse, explicitly). Without a fence luna invents extras.
4. **Verification command** — the exact command the worker must run before finishing (`uv run pytest tests/gateway/test_x.py -q`, `npm test`), and the instruction to report its real output.
5. **Completion sentinel** — "Reply DONE as your final message when complete" (checked via the `-o` file), or a `--output-schema` for structured results.

Right-size the task: one module, one bugfix, one focused refactor per dispatch. If the plan has 3+ independent chunks, dispatch parallel workers in separate `--cd` roots (or git worktrees) rather than one broad prompt.

## Planning document (multi-step or large-context tasks)

For anything spanning several files or steps where order matters, write the requirements down as a file instead of cramming them into the prompt string — luna re-reads it as needed instead of relying on everything surviving in one inline block.

- **Where:** `<workdir>/.archive/PLAN-<task>.md`, inside the pinned `--cd` root so luna can read it with its own tools.
- **Contents:** the same five prompt-contract elements above (file list, requirements, scope fence, verification command, completion sentinel), just written out per-file and in implementation order, instead of inlined.
- **The dispatch prompt shrinks to a pointer:** "Read `<path>` in full before making any changes. Implement exactly what it specifies, in the order given. Nothing outside its file list. Run `<verify cmd>` and report the real output. Reply DONE when complete."
- **State whether the plan file itself is in scope.** Default: read-only — tell luna explicitly not to edit or delete it, or it may "helpfully" rewrite it.
- **This pays off most on resume rounds:** a fix-round prompt can say "Re-read PLAN-<task>.md's requirements for `foo.py` — the reviewer found: ..." instead of re-explaining the whole task, and the plan stays as a stable reference across `codex exec resume --last` calls.
- **Skip it for single-file, single-step dispatches** — the inline prompt contract alone is enough there, and a plan file is pure overhead.

## Review protocol (mandatory)

Luna output is never integrated unreviewed:

1. **Mechanical check** — sentinel present in the `-o` file, exit code 0, `git -C <workdir> diff --stat` matches the promised file list. Any file outside the fence → reject the round.
2. **Orchestrator review** — read the diff; verify requirements point by point; run the verification commands yourself (never trust a worker's claimed test results without seeing output).
3. **Rate** 🟢 READY / 🟡 NEEDS WORK / 🔴 MAJOR ISSUES. For 🟡/🔴, resume the session (`codex exec resume --last`) with concrete, enumerated findings — file, line, what is wrong, what correct looks like. Budget 1–3 review rounds; after 3 failed rounds, re-scope the task instead of re-prompting.
4. **Integrate** — the orchestrator (not the worker) owns commits into this repo's history unless the dispatch explicitly delegates committing.

## Safety boundaries

- `--yolo` runs unsandboxed: never point a worker at `~/.openclaw/`, live runtime state, the Codex state DBs, or `scripts/push-openclaw-config.sh` execution. Deployment stays operator-owned.
- Workers get repo working trees only. For risky or parallel work, dispatch into a dedicated git worktree and merge after review.
- Do not let workers touch `/home/dev/repos/quantipy` directly — target-repo research changes flow through the autoresearch loop, not ad-hoc workers.
- This delegation path is for **repo development work** (this repo's gateway, g2_app, infra, tests, docs, agent assets). It is separate from the production autoresearch loop, which spawns its own pinned Codex stage agents (`.codex/agents/`) via the OpenClaw runtime.

## Division of labor

| Role | Who | Does |
|------|-----|------|
| Orchestrator | Claude (Fable), main session | Plan, decompose, write dispatch prompts, review diffs, run verification, integrate, commit |
| Worker | `codex exec` + `gpt-5.6-luna` xhigh | Implement exactly the scoped task, run the specified checks, report honestly |
| Review assist | `.claude/agents/reviewer.md` or a second luna pass | Optional independent adversarial read of a large diff before integration |

## This repo

- Verification commands for dispatches: `uv run pytest`, `uv run ruff check .`, `uv run mypy gateway tests`, `cd g2_app && npm test`.
- Worker scratch space: `.archive/` (repo convention) or the session scratchpad — never `/tmp` for durable artifacts.
- Related: `.claude/agents/orchestrator.md` (workflow patterns), `gateway/agent_config/skills/codex-subagents/` (the OpenClaw runtime's own Codex delegation skill — different audience, keep separate).
