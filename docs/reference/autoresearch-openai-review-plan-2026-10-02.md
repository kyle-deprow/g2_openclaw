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
   owner, child, reserved task name, role, model, effort, commit and spec; reject
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
- `gateway/openclaw_config/.env.example`
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
- `tests/gateway/research/test_native_review_correlation.py` for native
  host-store and rollout fixtures; preserve relevant existing integrity and
  lifecycle regressions when retiring ACP fixtures.
- `tests/gateway/research/native_review_fixtures.py` for shared official-store
  test fixtures only.
- `tests/gateway/research/test_readiness.py` and `test_admission_wiring.py`
  for migration of shared review fixtures and native cancellation expectations.

Reuse existing exact host readers where sufficient. Native collection must
not accept ACP evidence, self-reported model identity, or an operator-retyped
verdict. Historical schema decoding is read-only, not a launch/recollection
fallback. Report any required additional file before editing it.

Runtime documentation ownership will be assigned after the new command
contract is implemented. No speculative instructions or new orchestration
framework. No installation, restart, live review, commit or push by workers.

Deployment Luna also owns the following active instruction files and their
focused documentation tests. Coordinate exact flags with Evidence Luna first;
do not invent a CLI interface or edit historical/untracked user documents.

- `gateway/agent_config/AGENTS.md`, `SOUL.md`, `TOOLS.md`
- `gateway/agent_config/README.md`, `BOOTSTRAP.md`
- `gateway/agent_config/research-orchestrator/AGENTS.md`, `SOUL.md`, `TOOLS.md`,
  `BOOTSTRAP.md`
- `gateway/agent_config/skills/autoresearch/SKILL.md`
- `gateway/agent_config/skills/codex-subagents/SKILL.md`
- `gateway/agent_config/skills/research-loop/SKILL.md`
- `.agents/skills/openclaw-codex-config/SKILL.md`
- `.agents/skills/openclaw-multi-agent/SKILL.md`
- `.claude/skills/openclaw-codex-config/SKILL.md`
- `.claude/skills/openclaw-multi-agent/SKILL.md`
- `.claude/skills/research-loop/SKILL.md`
- `.codex/agents/human-proxy.toml`
- `.claude/agents/human-proxy.md`
- `.codex/agents/openclaw-development.toml`
- `.claude/agents/openclaw-development.md`
- `tests/gateway/test_agent_config_docs.py`
- `tests/gateway/test_research_persona_docs.py`

Keep mirrors consistent without adding an active Anthropic route. Validate
updated skill frontmatter and references. Skill updates must capture only the
new review mechanism and its existing safety boundaries, not grow a separate
framework or dilute the science contract.

## Native correlation and cancellation

Installed native children are not represented by the old core task reader.
Reuse official scoped stores: the owner session.started runtime event binds
session and run to its Codex thread; the parent rollout's exact spawn call
and result, Codex thread edge, and child thread/rollout bind the native
reviewer. Verify the reserved nonce task name, role, model, effort, terminal
success and strict final verdict. A thread edge marked open is not proof of
an active child; terminal rollout evidence decides. The installed host stores
the spawn `message` only as ciphertext, so the prompt digest is reservation-side
evidence and is never host-verified (see the live-shape correction below).

The installed supported host cancellation route is chat.abort for the exact
bound owner session, agent and run; it cascades to controlled subagents only
while that owner run is active. Because the owner yields right after spawning,
an operator cannot stop a running native reviewer this way; it ends on its own
terminal evidence.
Call it at most once and confirm terminal child evidence afterward. Timeout,
transport loss, ambiguous correlation or an unconfirmed abort remains pending
with the campaign paused. Do not add a cancellation bridge or ACP fallback.

Sol identified that native completion callbacks have a distinct owner run ID
while reusing the parent Codex thread. Reservation must therefore use the
exact canonical IMPLEMENTED wake key, not an agent-supplied run/thread or the
earlier OPENED wake. Resolve only that completed delivery row, require its
attempt and state to match, then derive the thread from the official exact
session/run event. Reject pending, missing, ambiguous, wrong-state or mismatched
bindings. Implementation callbacks submit their result and stop; the next
canonical IMPLEMENTED wake alone authorizes review reservation and dispatch.
Verify the spawn belongs to the bound owner turn, not merely the shared thread.

## Sol correction round

Independent review and root's 891-test run found native-boundary defects.
The run ended with 889 passed, one skipped, and the stale-callback regression
failing. Evidence Luna must fix the following before acceptance; no waiver or
transport fallback is authorized:

- Require the official spawn call timestamp, not its output timestamp, within
  the exact owner run interval. Bind row/event run IDs and reject reused-thread
  callback or later-turn spawns, missing timestamps and conflicting identities.
- Every new reservation must derive owner identity from the exact completed
  IMPLEMENTED wake. Remove caller run/thread bypasses and active review-ack.
- Include the immutable absolute bundle path in the exact hashed prompt;
  spawn_agent has no cwd argument. Enforce the exact native argument keys,
  fork_turns=none, role, task name, prompt and returned canonical task path.
- Bind the child run separately from the owner run in row/event evidence, ACK
  and verification. The owner run is the abort target, not the child run.
- Validate the official owner and child identities, provider/model/effort,
  valid nodes and consistent paths. Reject symlinked database files and parent
  components. Use read-only database access and expected schemas.
- Derive the two native store paths from the already validated managed
  OpenClaw root implied by RESEARCH_CORE_DATABASE's exact state/openclaw.sqlite
  suffix. Use one helper for concrete wake commands and production cancellation;
  fail closed on malformed paths, missing stores, schema or identity mismatch.
  Do not add new environment variables or an arbitrary/latest-record fallback.
- Require exactly one terminal marker. A persisted terminal FAIL remains
  immutable and idempotent; later evidence cannot resurrect it as PASS.
- Restore transport-independent bundle security regressions removed during
  fixture migration: limits, symlinks, blocked/untracked/nested source, modes,
  moved files, EXCLUDED, provenance/spec/run-plan changes and frozen evidence.
- Test actual production cancellation path derivation, exact at-most-once abort
  on transport loss/malformed response, pending reconciliation and termination.

Deployment Luna updates only the already-owned runtime docs/tests for exact
output nesting, concrete managed store paths and removal of review-ack. Model,
effort and prompt_sha256 are top-level reservation output; task_name, message,
agent_type and fork_turns are nested in spawn_arguments. Sol reviews the whole
correction, then root reruns the independent suite before commit/deployment.

Root subsequently reproduced 901 passing tests and one skipped test across
research, deployment, script guarding and runtime documentation. Sol still
found five exact-boundary gaps: store-level supersession, noncanonical/stale
wake keys, conflicting official path/role fields, unvalidated interval boundary
events, and review dispatch sequencing. These remain acceptance blockers until
fixed and independently reviewed. Existing historical superseded records must
remain readable and unchanged when active supersession code is removed.

Timing evidence corrected a proposed check: the retained A003 OPENED wake was
accepted at 2026-09-25T19:43:31.544721Z, while its exact owner run started at
19:43:36.032Z. Wake sent_at is an asynchronous receipt, not an in-run timestamp.
Resolve the exact canonical wake/run initially; later require owner start <=
reservation time <= official spawn call time < validated owner end. Review
dispatch must reserve/spawn then yield; its completion callback reconciles and
collects only after the original owner turn has closed, without respawning.

## Live-shape correction (Claude operator, 2026-10-02 late)

The user moved operator implementation/review to Sonnet/Opus; the research
loop's reviewer remains native Sol. Opus checked the migration against the
live stores and found it could never correlate a real review: non-reviewer
spawns in the shared owner rollout, NULL child `threads.name`, no child
OpenClaw session rows, owner thread rotation, rollouts above 8 MiB, missing
`--bundle-dir` in docs, and an encrypted spawn `message`. The fix binds on the
host-recorded nonce task name, thread row, spawn edge, child rollout terminal
marker and the `announce:codex-native:<owner_thread>:<child_thread>:<status>`
callback run; streams rollouts (512 MiB parent, 256 MiB child); and derives the
review CLI database paths from the managed root containing `--root`. A failed
or aborted reviewer child is a terminal REVIEW_FAILED for the attempt. Opus
re-review: READY; serial suite 978 passed, 1 skipped; live read-only
correlation of real native children succeeded.

## Verification

A003 prehost correction completed on native Luna thread
`01a0fe87-8212-7fc0-afe8-303f8f584bfa`. Execution commit is
`35db8a6c8c2b10d4523333b6a68dbdca0e166ba2`; proof commit is
`d34cf8e18ef47c4f60df0be9ffe44b4944331aa9`. The callback is terminal;
Sol reviewed the three bounded fixes READY. Root independently reproduced
49 tests passing in 15.35 seconds. No operator host run or application review
has occurred yet. Private retained operator log:
`/home/dev/autoresearch-openai-20261002.Tdkdgb/a003-unit-tests.log`.

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
