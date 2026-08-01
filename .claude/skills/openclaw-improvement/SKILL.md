---
name: openclaw-improvement
description: Operational playbook for improving the OpenClaw autonomous research pipeline through skills, config deployment, Codex runtime diagnostics, process monitoring, and failure recovery. Use when OpenClaw loops stall, Codex produces empty output, agents forget context, autoresearch state corrupts, or observed behavior warrants a new skill.
---

# Improving OpenClaw — Operations Playbook

Hard-won operational knowledge for diagnosing and fixing OpenClaw pipeline misbehavior, plus the discipline for where fixes belong (skills vs agent files).

**Canonical reference:** `.agents/skills/openclaw-improvement/SKILL.md`. This file is the distilled operating summary — read the canonical file before non-trivial work in this area.

## Core rules

- **Primary improvement vector is new skills** at `gateway/agent_config/skills/<name>/SKILL.md` — loaded on demand, unlimited size. Agent files stay thin: SOUL.md < 8k chars, AGENTS.md < 12k, TOOLS.md < 4k, BOOTSTRAP.md < 2k.
- **20,000-character hard truncation limit per bootstrap file** — excess is silently dropped. Symptoms: agent ignores rules near end of file. Diagnose with `wc -c gateway/agent_config/AGENTS.md`; fix by moving detail into a skill and leaving a 1–2 line reference.
- **Agent files only get**: behavioral rules that must apply every turn (gates, constraints) and skill references. Never examples, templates, or procedures.
- **Skill lifecycle**: same failure 2+ times → new skill; workaround becomes permanent or root cause found → update existing skill.
- **Config deployment checklist**: edit `gateway/agent_config/` or `gateway/openclaw_config/` in-repo (never `~/.openclaw/` directly) → `bash scripts/push-openclaw-config.sh` → `systemctl --user restart openclaw-gateway.service` → `openclaw config validate` → commit. Skipping push/restart means OpenClaw runs stale config — the #1 "why isn't my fix working" cause.
- **Process monitor** polls every 30s for Codex PID exit and notifies OpenClaw via WebSocket; gateway must be running (`ss -tlnp | grep 8765`). Monitoring is read-only unless a confirmed infra issue requires operator intervention.
- **Orchestrator-produces-nothing failure** (most common): PID exits after 5–15 min, `files_modified_count: 0`, plan.md only, `shutdown_type: "routine"`. There is NO hard turn limit — the agent chooses to stop after planning. Fix: `--resume=<session-id>` with "Skip exploration. Execute the implementation plan now."
- **Codex exits < 30s**: auth failure, model not found, or preload error — check newest `~/.codex/logs/process-*.log`.
- **Codex alive > 1 hour**: `ps -p <PID> -o %cpu,rss`; 0% CPU means stuck on input/network — kill and relaunch with a simpler prompt.
- **Per-agent `~/.openclaw/agents/claw/agent/models.json` overrides `openclaw.json`** providers; the push script auto-corrects baseUrl and strips hardcoded apiKeys.
- **Never silently retry another provider** if OpenAI/Codex auth fails — fix auth or fail the run. Azure/OpenRouter are explicit non-default routes via `OPENCLAW_PROVIDER` only.
- **Verify the Codex route** with `openclaw plugins list`, `openclaw models list --provider openai`, `openclaw models status --plain`, `openclaw gateway health`.
- **Context loss / repeated failed experiments**: autoresearch must be self-contained, rebuilding context from canonical decision receipts and read-only MemPalace retrieval (`mempalace_status`, `mempalace_diary_read`, `mempalace_search`, `mempalace_kg_query`). Models never write MemPalace; only the platform finalizer persists decisions.
- **Autoresearch state publication**: never `mv` a temp file over the authoritative state. Use `uv run gateway-cli autoresearch-advance "$state" "$artifact" --instruction-manifest-sha256 ... --state-reference-sha256 ... --output "$state"` (locks and atomically replaces). Preserve invalid files as evidence.
- **Worktree root**: all implementation and Fix/Test worktrees live only under `/home/dev/.openclaw/autoresearch/worktrees`, owner-only mode `0700`, never `/tmp` (31G tmpfs; each Quantipy worktree carries ~1.5G venv). The advancement boundary rejects evidence outside that root.
- **Safe worktree cleanup**: `git -C /home/dev/repos/quantipy worktree list --porcelain` → remove only confirmed-stale clean worktrees → `git worktree prune`. Never blind recursive deletion; never force-remove dirty worktrees.
- **Ownership boundary**: shared substrate (G2, orchestration, supervisor/recovery, shared loaders/harnesses/fixtures, dependency/runtime failures) is operator/Codex-owned; alpha modules, experiment notebooks, experiment tests, strategy/methodology behavior are autoresearch-owned even when a dependency upgrade exposed the bug. The PM never promotes patches, edits shared infra, or relaunches recovery — it reports blockers with exact evidence and waits.
- **Never run concurrent `pytest` in the same checkout** — coverage state can corrupt. Serialize or use isolated worktrees.
- **Improvement workflow**: Diagnose → Document (in a skill) → Fix → Deploy (push + restart) → Test → Prune obsolete workarounds.
- **Good skills are** specific, actionable (exact commands/templates), self-contained, maintained. Not for generic programming advice, one-off debug notes, or aspirational features.

## This repo

- OpenClaw agent skills: `gateway/agent_config/skills/` (currently `autoresearch`, `codex-subagents`, `mempalace-readonly`, `quantipy-data-contract`, `quantipy-methodology`). Codex repo-skills: `.agents/skills/`.
- Deploy path: `scripts/push-openclaw-config.sh` (guarded, transactional, fail-closed); also reachable via `make push-config`.
- Supervisor/state machinery: `gateway/autoresearch_supervisor.py`, `gateway/autoresearch_runner.py`, `gateway/autoresearch_control.py`, with tests in `tests/gateway/test_autoresearch_*.py`.
- Memory finalizer: `gateway/mempalace_finalizer.py` (tests `tests/gateway/test_mempalace_finalizer.py`); read-only server `gateway/mempalace_readonly_server.py`.
- Runtime verifier: `scripts/ensure-openclaw-codex-runtime.mjs` (mandatory, fail-closed) with tests in `tests/gateway/test_openclaw_codex_runtime_patch.py`.
