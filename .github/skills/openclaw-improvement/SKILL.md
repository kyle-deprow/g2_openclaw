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

**When you learn something new about how OpenClaw or Copilot behaves, PUT IT IN A SKILL — not in AGENTS.md.** Agent files are loaded on every turn. Skills are loaded when needed. Bloated agent files get truncated at 20k chars and the agent silently loses instructions.

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

The gateway's built-in process monitor polls every 30s for Copilot PID exit, then notifies OpenClaw via WebSocket. This replaced the earlier cron-based sentinel approach.

| Issue | Root cause | Fix |
|-------|-----------|-----|
| OpenClaw not notified of Copilot exit | Process monitor not running (gateway down) | Ensure gateway is running: `ss -tlnp \| grep 8765` |
| Late notification (>2 min) | Death report builder timeout on long sessions | Check gateway logs for slow transcript parsing |
| Double notification | Multiple gateway restarts while Copilot running | Only one gateway instance should run |

## Copilot CLI Output Failures

### Orchestrator produces nothing (most common)
**Symptom:** PID exits after 5-15 min, 0 files_modified, plan.md in session-state only.
**Root cause:** Orchestrator spawns explore subagents → consumes all turns on reading → writes plan → exits before implementing.
**Evidence:** `shutdown_type: "routine"`, `lines_added: ~30`, `files_modified_count: 0` in session telemetry.
**There is NO hard turn limit** — the agent simply chooses to stop after planning.

**Fix:** Resume with `--resume=<session-id>` and explicit directive: "Skip exploration. Execute the implementation plan now." See the `copilot-cli` skill for the full resume protocol.

### Copilot exits immediately (< 30s)
**Symptom:** PID appears then exits, no session created.
**Root cause:** Auth failure, model not found, or preload error.
**Diagnosis:** Check `~/.copilot/logs/process-*.log` (newest file). Look for error at top.

### Copilot hangs (alive > 1 hour)
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

### Azure preload
`gateway/openclaw_config/azure-api-version-preload.cjs` patches `globalThis.fetch` to:
1. Inject correct api-version for Azure OpenAI
2. Cap `max_tokens` for specific models (gpt-5-mini → 16384)
3. Replace Entra ID tokens with correct ones

The preload is loaded via `NODE_OPTIONS="--require ..."` in the systemd service. Debug with `AZURE_PRELOAD_DEBUG=1`.

**Never cap max_tokens for your main model** (GPT-5.4 should stay at 128000). Only cap mini models used for sentinels.

## Session & Memory tips

### OpenClaw forgets what it was doing
**Cause:** New session started (previous context lost).
**Fix:** Make the autoresearch skill self-contained — it reads RESEARCH_LOG.md and `memory_search` to rebuild context. Every iteration should be independently resumable.

### OpenClaw repeats failed experiments
**Cause:** Not checking memory before proposing.
**Fix:** Enforce `memory_search` at start of every Phase 1 (REVIEW) in autoresearch. Add failed experiment details to memory immediately after DISCARD.

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

### What makes a good skill
- **Specific** — addresses concrete scenarios, not vague philosophy
- **Actionable** — includes exact commands, templates, or decision trees
- **Self-contained** — a reader can follow it without reading 3 other files
- **Maintained** — updated when root causes change or new patterns emerge

### What doesn't belong in a skill
- Generic programming advice (use existing `.github/skills/` for that)
- One-off debugging notes (put those in memory or commit messages)
- Aspirational features that don't exist yet (put those in the plan doc)

## Current Skills Inventory

| Skill | Location | Purpose |
|-------|----------|---------|
| copilot-cli | `gateway/agent_config/skills/copilot-cli/` | Copilot invocation, background exec, sentinels, resume, debugging |
| autoresearch | `gateway/agent_config/skills/autoresearch/` | Autonomous research loop protocol |
| *(this skill)* | `.github/skills/openclaw-improvement/` | Meta: how to improve OpenClaw itself |

When creating new skills for OpenClaw, place them at `gateway/agent_config/skills/<name>/SKILL.md`.
When creating skills for Copilot in this repo, place them at `.github/skills/<name>/SKILL.md`.
