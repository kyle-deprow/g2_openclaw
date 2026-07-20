---
name: openclaw-improvement
description:
  Operational playbook for improving the OpenClaw autonomous research pipeline through skills, config deployment, Codex runtime diagnostics, process monitoring, memory hygiene, and failure recovery. Use when OpenClaw loops stall, produces empty output, forgets context, or needs new skills based on observed behavior.
---

# Improving OpenClaw — Lessons Learned & Optimization Playbook

## Purpose

This skill captures hard-won operational knowledge from running the OpenClaw autonomous research pipeline. When OpenClaw misbehaves, produces empty output, or the loop stalls — consult this skill for diagnosis and fixes.

**Primary improvement vector: write new skills for the OpenClaw agent** at `gateway/agent_config/skills/`. Skills are loaded on-demand and don't count against the 20,000-char bootstrap limit. Agent files (SOUL.md, AGENTS.md, TOOLS.md) should be thin — just behavioral rules and skill references.

## Philosophy: Skills Over Agent Files

| Layer | Purpose | Size discipline |
|-------|---------|-----------------|
| SOUL.md | Identity, principles, vibe | < 8k chars |
| AGENTS.md | Behavioral rules, gates, protocols | < 12k chars |
| TOOLS.md | Tool reference, quick syntax | < 4k chars |
| BOOTSTRAP.md | Config template | < 2k chars |
| Skills (`skills/*/SKILL.md`) | Deep knowledge, examples, templates | Unlimited (on-demand) |

**When you learn something new about how OpenClaw or Codex behaves, PUT IT IN A SKILL — not in AGENTS.md.** Agent files are loaded on every turn. Skills are loaded when needed. Bloated agent files get truncated at 20k chars and the agent silently loses instructions.

### When to create a new skill
- Same failure pattern occurs 2+ times → capture the fix in a skill
- A new integration point is set up (new tool, new repo, new service) → skill with usage patterns
- A complex multi-step procedure needs to be documented → skill with step-by-step

### When to update an existing skill
- A workaround becomes permanent → move from "known issue" to standard procedure
- A root cause is found for a documented symptom → update the diagnosis

### When to update agent files
- Only for behavioral rules that must apply to EVERY turn (gates, constraints, workflow steps)
- Only for skill references ("read skill X before doing Y")
- Never for examples, templates, or detailed procedures — those go in skills

## Config Deployment Checklist

Every change to `gateway/agent_config/` or `gateway/openclaw_config/`:

1. Edit source files in the repo (never `~/.openclaw/` directly)
2. Run `bash scripts/push-openclaw-config.sh` — copies config + auto-corrects per-agent models.json
3. Run `openclaw daemon restart` — picks up new config
4. Verify: `openclaw config validate`
5. Commit the source file changes to git

**If you skip step 2-3, OpenClaw runs stale config.** This is the most common "why isn't my fix working" issue.

## Bootstrap Truncation

OpenClaw loads bootstrap files (SOUL.md, AGENTS.md, TOOLS.md, BOOTSTRAP.md) on session start. Each file has a **20,000 character hard limit**. Content beyond 20k is silently dropped.

**Symptoms of truncation:**
- Agent ignores rules that are documented near the end of a file
- Agent doesn't know about recently added sections
- Sentinel template is malformed (because the template was cut mid-line)

**Diagnosis:**
```bash
wc -c gateway/agent_config/AGENTS.md  # must be < 20000
wc -c gateway/agent_config/SOUL.md
wc -c gateway/agent_config/TOOLS.md
```

**Fix:** Move detailed content to a skill. Replace with a 1-2 line reference: "Read the `X` skill for details."

## Process Monitor (replaced cron sentinels)

The gateway's built-in process monitor polls every 30s for Codex PID exit, then notifies OpenClaw via WebSocket. This replaced the earlier cron-based sentinel approach.

Monitoring is read-only unless you have confirmed an infrastructure issue that
requires operator intervention. Only the human operator or Codex touches G2,
OpenClaw orchestration, supervisor/recovery, shared monitoring, shared test
harnesses/fixtures, or shared Quantipy platform/runtime/tooling. The autonomous
PM never intervenes in those surfaces directly.

| Issue | Root cause | Fix |
|-------|-----------|-----|
| OpenClaw not notified of Codex exit | Process monitor not running (gateway down) | Ensure gateway is running: `ss -tlnp \| grep 8765` |
| Late notification (>2 min) | Death report builder timeout on long sessions | Check gateway logs for slow transcript parsing |
| Double notification | Multiple gateway restarts while Codex running | Only one gateway instance should run |

## Codex subagent Output Failures

### Orchestrator produces nothing (most common)
**Symptom:** PID exits after 5-15 min, 0 files_modified, plan.md in session-state only.
**Root cause:** Orchestrator spawns explore subagents → consumes all turns on reading → writes plan → exits before implementing.
**Evidence:** `shutdown_type: "routine"`, `lines_added: ~30`, `files_modified_count: 0` in session telemetry.
**There is NO hard turn limit** — the agent simply chooses to stop after planning.

**Fix:** Resume with `--resume=<session-id>` and explicit directive: "Skip exploration. Execute the implementation plan now." See the `codex-subagents` skill for the full resume protocol.

### Codex exits immediately (< 30s)
**Symptom:** PID appears then exits, no session created.
**Root cause:** Auth failure, model not found, or preload error.
**Diagnosis:** Check `~/.codex/logs/process-*.log` (newest file). Look for error at top.

### Codex hangs (alive > 1 hour)
**Symptom:** Sentinel keeps reporting "alive" past 1 hour.
**Diagnosis:** `ps -p <PID> -o %cpu,rss` — if CPU is 0%, it's stuck waiting for input or network.
**Fix:** Kill and re-launch with a simpler prompt. If consistent, the prompt is too complex.

## Model & Provider Gotchas

### Per-agent models.json override
`~/.openclaw/agents/claw/agent/models.json` takes priority over `openclaw.json` providers. This file is auto-generated by Azure AI Hub connections and may point to wrong endpoints.

**The push script auto-corrects this** — it rewrites baseUrl to the direct deployment URL and removes hardcoded apiKeys. If you see wrong model behavior, check:
```bash
cat ~/.openclaw/agents/claw/agent/models.json
```

### Codex runtime route
The default route is OpenClaw `openai/*` model refs through the Codex
app-server runtime. Verify with:

```bash
openclaw plugins list
openclaw models list --provider openai
openclaw models status --plain
openclaw gateway health
```

Do not silently retry through another provider if OpenAI/Codex auth fails. Fix
auth or fail the run.

### Optional Azure preload
Azure/OpenRouter are explicit non-default routes selected with
`OPENCLAW_PROVIDER`. If Azure is selected, `azure-api-version-preload.cjs`
patches Azure OpenAI requests with the required `api-version` parameter. Keep
this out of the default Codex path.

## Session & Memory tips

### OpenClaw forgets what it was doing
**Cause:** New session started (previous context lost).
**Fix:** Make the autoresearch skill self-contained — it reads
`RESEARCH_LOG.md` and rebuilds context from MemPalace readonly retrieval with
`mempalace_status`, `mempalace_diary_read`, `mempalace_search`, and
`mempalace_kg_query`. Every iteration should be independently resumable.

### OpenClaw repeats failed experiments
**Cause:** Not checking memory before proposing.
**Fix:** Enforce MemPalace readonly retrieval at the start of each new context
pass in autoresearch. Write failed experiment details to MemPalace only during
final PM decision logging after DISCARD or CRASH.

### Supervisor repeatedly reports invalid autoresearch state JSON
**Symptom:** The supervisor restarts repeatedly with `invalid autoresearch state
JSON`, then systemd start-limits the service. The authoritative state may be an
empty file while a `.quantipy-state.json.*` temp file remains nearby.

**Root cause:** A shell workflow created an empty temp output, ran
`autoresearch-advance`, and moved the temp file over authoritative state even
after advancement failed.

**Fix:** Persist directly to the authoritative path. The runner locks and
atomically replaces it:

```bash
uv run gateway-cli autoresearch-advance "$state" "$artifact" \
  --instruction-manifest-sha256 "$manifest_sha" \
  --state-reference-sha256 "$state_sha" \
  --output "$state"
```

Never use an unconditional shell `mv` to publish autoresearch state. Preserve
the invalid file as evidence, restore the latest independently validated state,
then restart supervision.

### OpenClaw invents strategies from training data
**Cause:** Violating "Research before invention" principle.
**Fix:** Tighten SOUL.md principle 5. Add negative examples. Ensure ideation is delegated to `--agent researcher` (which does web research), never done by OpenClaw directly.

## Improvement Workflow

When you observe a failure or suboptimal behavior:

1. **Diagnose** — Check logs, session telemetry, git state, sentinel output
2. **Document** — Add the failure pattern + root cause + fix to the relevant skill (or create a new one)
3. **Fix** — If it's a behavioral issue, update the skill. If it's a structural gate, update AGENTS.md (minimally).
4. **Deploy** — Push config → restart daemon
5. **Test** — Run through the scenario again to verify the fix
6. **Prune** — If the fix makes an older workaround obsolete, remove the workaround

### Ownership boundary for Quantipy work

Use this classification test before editing `/home/dev/repos/quantipy`: does the
change alter strategy logic, features, folds, models, null tests, metrics, or
research methodology behavior, or does it only repair the shared execution
substrate?

- Shared substrate only: stop the loop, repair it with an independent
  implement/review/fix cycle, verify in isolation, commit the authoritative
  shared-infrastructure patch in the authoritative checkout, then relaunch
  autoresearch.
- Strategy/methodology behavior: do not operator-edit it. Record the exact
  failure and let the autoresearch loop implement, review, and fix its own
  experiment artifacts.

Treat every alpha module, experiment notebook, experiment-specific unit test,
and research-metric/methodology behavior as autoresearch-owned even when a
dependency or runtime upgrade exposed the bug. Do not promote experiment
changes out of `INFRA_BLOCKED`, `DISCARD`, or `CRASH` disposable worktrees.
Only the human operator or Codex may promote independently reviewed
shared-infrastructure patches from the authoritative checkout. The PM never
promotes.

Compact decision tree:

1. G2/OpenClaw orchestration, task ledger, supervisor/recovery, shared Quantipy
   platform/runtime/tooling, shared data loaders, shared test
   harnesses/fixtures, dependency/runtime failures, G2 simulator, headless
   launch, or Codex/OpenClaw route/process failure: operator/Codex-owned.
2. Alpha modules, experiment notebooks, experiment-specific unit tests,
   strategy features, folds, models, null tests, metrics, validation, or
   methodology behavior: autoresearch-owned, even when exposed by dependency
   upgrades.
3. Ambiguous or risks unrelated dirty/shared state: classify as a shared-infra
   blocker, collect evidence, and wait for human/Codex operator action.

Examples:

- T36 read-only shuffle failure: autoresearch-owned because it is an
  experiment/null-test methodology failure.
- Task ledger corruption, supervisor recovery defects, G2 simulator failures,
  headless launch failures, shared loader failures, shared fixture/harness
  failures, dependency import/runtime launch failures: operator/Codex-owned.

PM/stage boundary:

- PM and stage agents report shared-infrastructure blockers with exact evidence
  and wait. Evidence should include failing command/test, path, label/session
  id, timestamp when available, and decisive log/stderr lines.
- Only human/Codex fixes, promotes, restarts, or relaunches operator-owned
  surfaces. PM never touches G2, promotes patches, edits shared infrastructure,
  or relaunches recovery for shared-infrastructure failures.
- Non-PM agents do not write MemPalace; stage agents use readonly retrieval.

Do not run concurrent `pytest` processes in the same checkout; coverage state
can corrupt. Serialize verification or use isolated worktrees.

### Autoresearch worktree storage exhaustion

**Diagnosis:** `/tmp` is a 31G tmpfs. A Quantipy disposable worktree includes
an approximately 1.5G virtual environment, so stale iteration worktrees can
fill `/tmp` and block future implementation stages.

**Prevention:** All implementation and Fix/Test worktrees belong only under
the canonical operator-controlled root
`/home/dev/.openclaw/autoresearch/worktrees`. The implementation agent creates
the parent before `git worktree add`:

```bash
mkdir -p /home/dev/.openclaw/autoresearch/worktrees
```

The autoresearch advancement boundary rejects implementation evidence, persisted
implementation evidence, and Fix/Test evidence outside that root. Fix/Test must
use the exact persisted canonical workspace and must never create a replacement
or use a legacy `/tmp` fallback.

**Safe cleanup:** First reconcile persisted loop state and active stage
processes. Do not remove an active iteration's workspace. List registered
worktrees, then remove only a confirmed stale, clean disposable worktree using
Git; do not use blind recursive deletion under `/tmp`.

```bash
git -C /home/dev/repos/quantipy worktree list --porcelain
git -C /home/dev/repos/quantipy worktree remove <confirmed-stale-worktree>
git -C /home/dev/repos/quantipy worktree prune
```

If `git worktree remove` refuses because a worktree is dirty, preserve and
reconcile the changes instead of forcing removal. Check capacity before and
after cleanup with `df -h /tmp /home/dev`.

### What makes a good skill
- **Specific** — addresses concrete scenarios, not vague philosophy
- **Actionable** — includes exact commands, templates, or decision trees
- **Self-contained** — a reader can follow it without reading 3 other files
- **Maintained** — updated when root causes change or new patterns emerge

### What doesn't belong in a skill
- Generic programming advice (use existing `.agents/skills/` for that)
- One-off debugging notes (put those in memory or commit messages)
- Aspirational features that don't exist yet (put those in the plan doc)

## Current Skills Inventory

| Skill | Location | Purpose |
|-------|----------|---------|
| codex-subagents | `gateway/agent_config/skills/codex-subagents/` | Codex invocation, background exec, sentinels, resume, debugging |
| autoresearch | `gateway/agent_config/skills/autoresearch/` | Autonomous research loop protocol |
| quantipy-methodology | `gateway/agent_config/skills/quantipy-methodology/` | Stage-agent preflight for loading live Quantipy AGENTS, skills, and Codex agent definitions |
| *(this skill)* | `.agents/skills/openclaw-improvement/` | Meta: how to improve OpenClaw itself |

When creating new skills for OpenClaw, place them at `gateway/agent_config/skills/<name>/SKILL.md`.
When creating skills for Codex in this repo, place them at `.agents/skills/<name>/SKILL.md`.
