# OpenAI research review migration

The user requested OpenAI models instead of Anthropic on October 2, 2026.
Use Astra as owner, native Luna for implementation and execution, and native
Sol (`gpt-5.6-sol`, `xhigh`) for review. This replaces the active Claude route;
it is not a fallback. Historical review evidence stays unchanged and readable.

H0006-A003 remains OPENED without a review reservation or historical job.
The campaign was paused through the supported CLI before this work. Its
frozen scientific specification, admission count, and candidate are unchanged.
Do not increase the cap beyond 15 or authorize another attempt.

## Order and acceptance

1. Reconcile actor quiescence. Independently review the outstanding A003
   correction before any operator host integration.
2. Luna replaces deployment configuration and removes the active ACP reviewer
   launcher. Sol reviews the diff before deployment.
3. Luna replaces review correlation with exact native task and official Codex
   rollout evidence. Keep immutable bundle and strict verdict checks. Bind
   owner, child, reserved prompt, role, model, effort, commit and spec; reject
   ambiguous, stale, nonterminal, mismatched or altered evidence. Requested
   service tier is not observed service tier. No fabricated verification.
4. Keep stop/cancel fail-closed using supported native controls. Resolve the
   actual host cancellation capability before claiming production readiness;
   do not invent an RPC, retain ACP cancellation, or blindly repeat dispatch.
5. Update active runtime instructions to match the implemented CLI. Retain
   historical prose and records truthfully. Root reproduces focused and full
   research tests, reviews the final diff, commits and pushes main.
6. Deploy through the guarded push script while paused and idle, restart the
   gateway, validate configuration and health, and verify the native role.
7. Complete A003 through its canonical owner: one operator host test after
   prehost READY, fresh native proof packaging, one actual native Sol review,
   native historical run after PASS, and Astra's economic decision. Monitor
   exact task identities with Luna; no duplicated unknown stages.
8. Preserve useful economic results (negative/inconclusive is valid), integrate
   accepted work on main, push both repos, and retire only inactive verified
   checkouts recoverably. Synthetic-only output is not E2E completion.

## Ownership

Root owns this plan, live controls, deployment, Git integration and acceptance.
Workers are not alone and must preserve unrelated changes. No worker edits
scientific source, immutable evidence, auth, installed configuration, or live
databases. Use apply_patch, TDD, and serialize pytest per checkout.

Deployment Luna owns only:

- `.codex/agents/reviewer.toml`
- `.codex/agent-configs/reviewer.toml`
- `.claude/agents/reviewer.md` (source mirror only, not an active Claude route)
- `gateway/deployment/codex_agents.py`
- `gateway/deployment/config_merge.py`
- `gateway/openclaw_config/openclaw.json`
- `gateway/openclaw_config/README.md`
- `scripts/push-openclaw-config.sh`
- `scripts/research-reviewer-cli.py` (remove)
- `tests/gateway/deployment/test_codex_agents.py`
- `tests/gateway/deployment/test_config_merge.py`
- `tests/gateway/test_openclaw_script_guarding.py`
- `tests/gateway/test_research_reviewer_cli.py` (remove obsolete tests)

Require a scoped read-only reviewer role without child spawning or repair;
register Sol in the native model allowlist. Sanitize inherited deployed
Claude/ACP/ACPX reviewer wiring as well as the source overlay. Do not expand
main's tool access or introduce another provider. Remove active Anthropic
catalog/default selections and fail closed on an explicitly selected
non-OpenAI model where applicable. Preserve unrelated operator state.

Evidence Luna owns only:

- `gateway/research/host_records.py`
- `gateway/research/review_evidence.py`
- `gateway/research/contracts.py`
- `gateway/research/store.py`
- `gateway/research/machine.py`
- `gateway/research/cli.py`
- `gateway/research/control.py`
- `gateway/research/wake.py`
- Corresponding `tests/gateway/research/test_host_records.py`,
  `test_review_evidence.py`, `test_store.py`, `test_machine.py`,
  `test_status_control.py`, `test_cli_scenario.py`, `test_wake.py`.

Reuse existing exact host readers where sufficient. Native collection must
not accept ACP evidence, self-reported model identity, or an operator-retyped
verdict. Historical schema decoding is read-only, not a launch/recollection
fallback. Report any required additional file before editing it.

Runtime documentation ownership will be assigned after the new command
contract is implemented. No speculative instructions or new orchestration
framework. No installation, restart, live review, commit or push by workers.

## Native correlation and cancellation

Installed native children are not represented by the old core task reader.
Reuse official scoped stores: the owner session.started runtime event binds
session and run to its Codex thread; the parent rollout's exact spawn call
and result, Codex thread edge, and child thread/rollout bind the native
reviewer. Verify the generated prompt digest, role, model, effort, terminal
success and strict final verdict. A thread edge marked open is not proof of
an active child; terminal rollout evidence decides.

The installed supported host cancellation route is chat.abort for the exact
bound owner session, agent and run; it cascades to controlled subagents.
Call it at most once and confirm terminal child evidence afterward. Timeout,
transport loss, ambiguous correlation or an unconfirmed abort remains pending
with the campaign paused. Do not add a cancellation bridge or ACP fallback.

## Verification

Run focused owned-file pytest first; root then reproduces:

```bash
uv run pytest tests/gateway/research tests/gateway/deployment tests/gateway/test_openclaw_script_guarding.py tests/gateway/test_agent_config_docs.py tests/gateway/test_research_persona_docs.py -q
uv run ruff check gateway/research gateway/deployment tests/gateway/research tests/gateway/deployment
uv run ruff format --check gateway/research gateway/deployment tests/gateway/research tests/gateway/deployment
uv run mypy gateway/research gateway/deployment
bash -n scripts/push-openclaw-config.sh
git diff --check
```

Sol reviews read-only for correctness, security, regressions, scope, typing,
negative tests, cancellation and evidence integrity. Root independently
checks test results and retained live receipts; accepted dispatch does not
prove completion. Do not parallelize pytest in this checkout.
