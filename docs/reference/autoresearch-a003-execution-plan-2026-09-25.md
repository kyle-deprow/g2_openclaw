# H0006-A003 execution plan

## Authority and boundaries

The user replied `continue` on 2026-09-25 to the explicit request to raise the
campaign cap from 14 to 15 for A003. This authorizes exactly that additional
admission, not further automatic increases. Preserve closed A001/A002, their
reviews, and the retired A002 checkout unchanged.

Use Luna (`gpt-5.6-luna`, xhigh) for operator implementation and monitoring,
with independent Sol (`gpt-5.6-sol`, xhigh) review. The application loop remains
Astra → fresh native Luna implementer → one actual ACP Opus review → fresh
native Luna runner → Astra decision. Operator workers do not edit strategy
code. No provider fallback, frozen-spec changes, evidence exemptions, data
downloads, environment clones, MemPalace writes, or live trading.

## Ordered work

1. Read-only readiness and contract checks: reconcile canonical campaign and
   services, model routes, source state, and the supported target-stage launch
   boundary. Do not consume A003 while a known shared-infrastructure blocker
   remains. Record any required infrastructure patch in a bounded annex before
   delegation; its files, tests, and acceptance must be explicit.
2. Implement any necessary shared-substrate repair with Luna/Sol. Root
   independently verifies tests, integrates on main, and pushes. Deploy only
   when required, with the campaign paused and owner quiescent. Do not change
   the frozen scientific contract to accommodate the implementation.
3. Through supported controls, record the one cap increase 14→15 and resume
   exactly H0006-A003. Supply the native owner with the canonical A002 verdict
   and `/home/dev/repos/quantipy/docs/research/h0006-a002-review-failure-2026-09-24.md`.
   Native implementation must address all seven substantive findings, build
   fresh A003 proof, and leave A002 immutable. A003 is the third and final
   attempt allowed by H0006.
4. Before actual review, independently verify the full contract, not merely
   happy-path unit tests: production spec digest; complete committed proof and
   retention manifest; every ordinary/test file ≤8 MiB; exact supported launch
   command without post-builder mutation; synthetic sufficiency/status gates;
   valid cash comparator; reasons for null exposure fields. If complete proof
   cannot meet the contract, preserve a typed blocker rather than waive it.
5. Require exactly one bound, collected Opus PASS for the final A003 candidate
   before a native runner launches the real historical job. Monitor the actual
   canonical task/job identities; accepted dispatch is not execution success.
   Do not replay an unknown, failed, or completed review/run.
6. Verify the six actual historical scenarios, seven analysis reports,
   provenance, nontrivial trades/economic analysis, and Astra's explicit
   conclusion. Negative or inconclusive economics count; synthetic-only,
   process-only, or zero-trade-only output does not. Stop further admissions
   after the first useful E2E outcome.
7. Close the lifecycle deliberately. Integrate accepted work on main and push
   both repositories. Retire inactive checkouts recoverably only after
   verifying quiescence and complete evidence preservation. Do not promote
   rejected scientific code as an accepted implementation.

## Ownership and verification

- Root owns this plan, policy changes, deployment, commits, pushes, and final
  acceptance. Existing untracked user documents remain untouched.
- Readiness workers are read-only. Infrastructure file ownership is not yet
  assigned; a subsequent explicit annex is required before code edits.
- Native application workers own experiment source in the admitted A003
  checkout only; no operator edits to scientific logic or experiment tests.
- Sol reviews read-only. Serialize pytest within any shared checkout.
- Verify canonical status with `gateway-cli research status --root
  /home/dev/.openclaw/research-v2 --json`; verify Git state and actual retained
  receipts independently. Exact infrastructure verification commands belong
  in the annex if one is needed.

## Current checkpoint

Readiness confirmed the campaign remained paused at 14/14, with no A003 or
new historical job. Gateway healthy; research owner stopped; ample temporary
storage. The supported policy command has now recorded the user-authorized
cap of 15, with no automatic further extension. The campaign remains paused
while supported-launch contract checks finish; no A003 admission, restart,
or research launch has occurred in this continuation yet.

Annex A is implemented and independently reviewed READY by Sol. Root
reproduced 72 focused tests and 596 research tests, all passing; Ruff check,
format check, mypy on the three owned files, and diff checks pass. Production
defaults and the target argv contract remain unchanged. A fresh supported
provider-usage response reports OpenAI available without a provider error,
94% of the weekly allowance used (reset 2026-09-28 06:31:35 UTC). Continue with
bounded delegation; do not silently substitute models if allowance runs out.

## Annex A: supported synthetic environment construction

Read-only Sol analysis established one shared prerequisite. The builder has no
supported way to construct the required `H0006_SYNTHETIC_MODE=1` environment;
A002 mutated the returned command. Native A003 can independently repair target
imports by adding only its worktree root in its entrypoint, as the frozen
import contract permits. Do not change target argv validation or PYTHONPATH.

Luna may edit exactly:

- `gateway/research/containment.py`
- `tests/gateway/research/test_containment.py`
- `tests/gateway/research/test_worker.py` (production-default regression only)

All other source, configs, skills, RunPlan/contracts, snapshots, specs,
scientific code, and live state are out of scope. No deployment is required
for this local driver-only patch. Keep the owner stopped and campaign paused.

Add keyword-only `synthetic_env: Mapping[str, str] | None = None` explicitly to
`bwrap_argv` and `stage_plan`, forwarding through the supported builder.
`None` must preserve existing argv byte-for-byte. A supplied value must be a
mapping of exactly one entry, with a string key fully matching
`H[0-9]{4}_SYNTHETIC_MODE` and a string value exactly `"1"`. Permit it only on
the existing valid targets stage forms or exact `analysis`, never evaluator
or validation stages. Reject malformed types, empty/multiple entries,
arbitrary keys, reserved variables, and other values with `ContainmentError`.
Insert exactly one builder-owned `--setenv KEY 1` immediately before the
unchanged command suffix, after normal builder environment/mount arguments.
Do not expose synthetic mode through RunPlan, production worker controls, or
general environment overrides. Do not mutate the caller mapping.

Write failing tests first, then the smallest implementation. Cover target and
analysis construction, exact command suffix and immutable returned argv,
absence of synthetic variables by default, malformed mapping/key/value and
unsupported-stage refusals, and the actual worker's unchanged production
target path in `test_worker_rewrites_target_output_for_generated_stage_bind`.

Verification (serialize pytest):

```bash
uv run pytest tests/gateway/research/test_containment.py tests/gateway/research/test_worker.py -q
uv run ruff check gateway/research/containment.py tests/gateway/research/test_containment.py tests/gateway/research/test_worker.py
uv run ruff format --check gateway/research/containment.py tests/gateway/research/test_containment.py tests/gateway/research/test_worker.py
uv run mypy gateway/research/containment.py tests/gateway/research/test_containment.py tests/gateway/research/test_worker.py
git diff --check
```

Sol reviews read-only against this annex. Root independently reproduces the
checks before committing/pushing and resuming A003. The native owner brief
must require validator/rewriter-produced target argv and the exact returned
stage argv; the historical target contract does not accept `-P -s` in the
target argv even though evaluator and analysis stages require those flags.
Native evidence may be losslessly decomposed into complete indexed components
under the frozen per-file/aggregate limits; it may not use an exemption or
drop repeated records without retaining their exact reconstructible content.
