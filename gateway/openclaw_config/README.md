# OpenClaw configuration

This directory is the source of truth for the guarded OpenClaw deployment.
Changes are reviewed in the repository and later published by the operator with
scripts/push-openclaw-config.sh. Do not hand-edit ~/.openclaw, authenticate,
restart services, or run the publish script while reviewing source changes.

## Runtime tuple and provider

The managed runtime is OpenClaw 2026.9.2, @openclaw/codex 2026.9.2, and
embedded @openai/codex 0.153.4. The default route is OpenAI/Codex app-server
through OAuth. Its managed `openai` provider uses the native
`openai-chatgpt-responses` API with
`https://chatgpt.com/backend-api/codex`, `auth: oauth`, and
`agentRuntime.id: codex`; this selects OAuth and does not add an API-key or
fallback route. Azure or OpenRouter
are explicit operator-selected routes only;
an unavailable route is a fail-closed error, never an invented alias or silent
provider switch.

The config keeps the G2 interface agent main on openai/gpt-5.4 and the bounded
research owner research-orchestrator on openai/gpt-6-astra. Native Luna is the
approved implementation/runner child and Claude Code Opus via ACP is the
review-only child. Child identity, attempt, commit, spec digest, and evidence
are bound by the research owner; no deleted debate roster or retired service
unit is configured.

## Sessions and lifecycle

G2 traffic uses agent:main:g2. Research-owner traffic uses
agent:research-orchestrator:autoresearch:quantipy-v2. The source unit
research-owner.service owns the deterministic cadence and is bound to
openclaw-gateway.service. Its source template is
gateway/openclaw_config/research-owner.service.template.

The owner loop is hypothesis equals iteration and each attempt is code → review
→ run, with at most three attempts. Astra explicitly chooses FINISH, ABANDON,
or PAUSE after exhaustion. Preserve typed admission refusals, policy-unset
pause, exhausted-attempt completion, exact cancel, owner wake, and immutable
receipts. Main controls remain read-only.

## Memory and scientific boundary

No autoresearch model writes MemPalace: every model is read-only, the built-in
memory tools memory_search and memory_get are denied, memory flush is disabled,
and the durable research records are the research store and artifact receipts.
MemPalace is optional read-only retrieval, never a control ledger or completion
gate, and this loop has no automated MemPalace writer. The managed OpenClaw 9.2
config explicitly sets `memory.search.enabled` and
`agents.defaults.compaction.memoryFlush.enabled` to `false`; do not enable
built-in memory or add a provider fallback. It also keeps
`cron.enabled=false`, sets the default heartbeat cadence to `0m`, and sets
`skills.workshop.autonomous.mode=off`; the host research-owner service is the
only intended loop owner.
The provider allowlist retains the bundled `codex`, `acpx`, and `openai`
plugins; `memory-core` remains explicitly disabled, and the managed
`plugins.slots.memory` value is `none` so the implicit memory slot cannot be
re-enabled by a machine-local entry.

The initial capability is price-panel-only and ETF-scoped. Stock work requires
trusted point-in-time earnings coverage and fails closed on unknown earnings.
A holding horizon above five sessions is refused. Trusted panel sessions,
immutable receipts, the exposure ledger, and evaluator bounds are mandatory for
scientific statements. Smoke or synthetic data does not prove installed
readiness or alpha.

## Guarded publication

The push script validates the generated JSON against the installed 9.2 schema,
the OpenAI/Codex provider, the main and research-owner agent IDs, main's
bounded tool profile, the research owner's full native profile, main-only MCP
projections, memory denial, and the research-owner unit. It publishes
atomically with rollback evidence and prunes stale installed copies using its
declared arrays. Keep those prune arrays and route checks unchanged when
editing this README.

Before a later authorized deployment, run source-only checks and inspect the
generated diff. The deployment checkpoint must prove the configured route,
workspace ownership, provider/auth invariants, service lifecycle, and clean
rollback. This document authorizes no deployment, service, auth, database,
network, or dependency action.

## Native auth-store maintenance order

The paused push publishes the configuration and managed artifacts but defers
Codex auth synchronization. On a later operator-authorized maintenance window,
complete the steps in this order: run the paused push; let the supported native
OpenClaw runtime initialize each managed agent store; then run the normal push
to synchronize the existing stores. Auth synchronization requires both the
main source store and every target `openclaw-agent.sqlite` to be existing native
stores with a valid `schema_meta` primary ownership row (`role=agent`, schema
version 19, and the expected agent ID). A missing or unowned target is a
fail-closed prerequisite failure: initialize it through the public native
OpenClaw path first. The deployment helper never creates an auth database or
inserts schema metadata manually.
