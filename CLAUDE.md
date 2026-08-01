# G2 OpenClaw — Claude Code Instructions

Shared project rules (stack, layout, G2/OpenClaw rules, commands, guardrails) live in the root agent instructions and apply to Claude Code verbatim:

@AGENTS.md

## Claude ↔ Codex parity

This repo maintains two parallel sets of coding-agent assets. Keep them in sync when either side changes:

| Purpose | Codex (canonical) | Claude Code (mirror) |
|---------|-------------------|----------------------|
| Repo skills | `.agents/skills/<name>/SKILL.md` | `.claude/skills/<name>/SKILL.md` |
| Subagent personas | `.codex/agents/<name>.toml` | `.claude/agents/<name>.md` |
| Project instructions | `AGENTS.md` | `CLAUDE.md` (this file, imports `AGENTS.md`) |

- The `.claude/skills/` files are **distilled entry points**: frontmatter triggers plus the load-bearing rules. The full rule sets stay canonical in `.agents/skills/` — read the canonical file before non-trivial work, and edit the canonical file first when rules change, then refresh the distilled mirror.
- The `.claude/agents/` files mirror `.codex/agents/` one-to-one (underscored Codex names become kebab-case). When you change a contract in one, back-propagate it to the other.
- OpenClaw **runtime** skills (`gateway/agent_config/skills/`, deployed to `~/.openclaw/`) are a third, separate category. Never blend them into repo skills — that split is deliberate (see `AGENTS.md`).

## Model tier mapping

Production stage agents are pinned in `gateway/openclaw_config/openclaw.json`. The Claude mirrors map those tiers as follows:

| Codex pin | Claude `model:` | Agents |
|-----------|-----------------|--------|
| `gpt-5.6-sol` | `opus` | consensus-arbiter, reviewer, orchestrator |
| `gpt-5.6-terra` | `opus` | debater-data, backend-python, g2-development |
| `gpt-5.5` | `sonnet` | debater-microstructure, debater-skeptic, human-proxy, openclaw-development, azure-bicep |
| `gpt-5.4` | `sonnet` | context-curator, debater-theory, debater-implementation, implementer, fixer |

Do not silently re-tier an agent; the pinning is intentional (judge/arbiter roles get the strongest model).

## Production loop vs. development mirrors

The live autoresearch loop spawns the **Codex-native** stage agents via `spawn_agent` inside the OpenClaw/Codex runtime — the Claude stage-agent mirrors exist for development, dry runs, and protocol testing of the loop, not as the production execution path. Facts every agent working here must respect:

- Autoresearch state advances only through `gateway-cli autoresearch-next` / `autoresearch-advance` / `autoresearch-submit-stage`; stage submissions are a strict three-key envelope `{instruction_manifest_sha256, state_reference_sha256, artifact}` — extra keys or stale state fail closed.
- Memory: `memory_search`/`memory_get` are globally denied and `compaction.memoryFlush` is disabled **on purpose**. No model writes MemPalace; the sole writer is the state-derived finalizer (`gateway/mempalace_finalizer.py`) driven by the supervisor. Agents get the read-only `mempalace-readonly` MCP only.
- Sessions: G2 traffic is `agent:main:g2`; autoresearch runs only in `agent:autoresearch-pm:autoresearch:quantipy`, woken by the systemd user unit `quantipy-autoresearch-supervisor.service` (60 s poll, `BindsTo=openclaw-gateway.service`).
- Runtime tuple is pinned fail-closed: OpenClaw `2026.7.1-2`, `@openclaw/codex` `2026.7.1-1`, embedded `@openai/codex` `0.144.3`. Provider is OpenAI/Codex app-server via OAuth only — no fallback paths.
- All live-config changes go through `bash scripts/push-openclaw-config.sh` (guarded, transactional, atomic publish + rollback). Never hand-edit `~/.openclaw/`.
- Deployment checkpoint and resume checklist: `.archive/OPENCLAW_DEPLOYMENT_STATUS.md`.

## Orchestration model: Claude orchestrates, Codex workers implement

Claude (Fable, the main session) is the **orchestrator**. Implementation work is dispatched to **Codex CLI workers** using `gpt-5.6-luna` at `xhigh` reasoning effort with `--yolo`:

```bash
codex exec --yolo -m gpt-5.6-luna -c model_reasoning_effort="xhigh" \
  --cd <workdir> -o <run-dir>/last-message.txt "<strict prompt>"
```

Luna is dirt cheap but needs strict guidance: exact file lists, precise requirements, a scope fence, a verification command, and a completion sentinel — plus orchestrator review rounds before integration. The full prompt contract, review protocol, and safety boundaries are in the `codex-worker-delegation` skill (`.claude/skills/codex-worker-delegation/SKILL.md`) — load it before dispatching any worker. Verified against codex-cli 0.144.4.

## Claude Code specifics

- Subagents in `.claude/agents/` cannot spawn other subagents; the orchestrator role (delegation, review cycles) is driven from the main session — Codex CLI workers for implementation, Agent-tool personas for read/analysis roles.
- Temp/log files go to `.archive/` in the repo root (gitignored paths there are fine), matching the human-proxy convention.
- Run verification before claiming completion: `uv run pytest`, `uv run ruff check .`, `uv run mypy gateway tests`, and `cd g2_app && npm test` for TypeScript changes.
