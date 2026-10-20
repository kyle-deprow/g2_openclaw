# Tools

## Allowed use

- Read existing research status/control surfaces and their durable evidence.
- Use only the approved implementer and runner plus the read-only reviewer
  (role table and spawn mechanics below). Reserve the immutable review bundle
  before the one reviewer run; reconcile the official child run only after the
  original owner turn ends, binding task identity, commit, spec digest, and
  verdict.
- Use the exact cancel operation once, after rereading the current task. Keep
  an unknown response pending and pause; do not repeat it or invent a result.
- Return every refusal and outcome to Astra through the owner wake. Main
  controls remain read-only.

## Command evidence

Use the installed CLI's live help to discover research operations.
Start with:

`@GATEWAY_CLI@ research --help`

The top-level help enumerates subcommands. Before invoking one, run the
installed CLI's research <subcommand> --help form and use only its listed
flags. The frozen appendix is not an exhaustive CLI
allowlist. Review collection through the existing review-collect operation
is authorized; consult its live help before using the invocation below.
Live help defines syntax only and grants no additional permission. Owner actions
remain limited to the operations authorized by this contract (including
review-collect); policy, ledger, runtime-registration,
run-release-unstarted, run-reverify, and operator-note mutations remain
operator-only. Do not invent flags, providers, routes, or command names. No
command launches work unless the operator policy and route are proven.

`pause --root ROOT --owner --reason TEXT` is owner-authorized for the campaign charter's stop
condition (for example after consecutive underpowered `hypothesis-create` refusals): pause
with a summary instead of retrying, and never resume yourself. Use `--owner` for any
owner-initiated pause. A refused create is recorded
and re-wakes you once with the reason and count; an operator `resume` resets that count.

Freeze sequence (`compute-probe` first, then `hypothesis-set-compute`, then freeze): see the
autoresearch skill.

Submission: the implementer writes a `research-submission-input-v1` JSON.
`submission-build ATTEMPT_ID --root ROOT --input FILE --out-dir DIR` derives the record,
run plan, and provenance index and runs `submission-preflight`; both must PASS (read-only,
`--json`) before `implementation-submit`. Never hand-compute digests or argv.

New hypotheses use contract `research-hypothesis-v2` with a `power` block; fill
it with the read-only `power-check --events N --sd-bps S`. The SD must come
from outside the scored test period; create refuses designs whose MDE exceeds
a plausible effect.

Stock specs need `hypothesis-create --earnings FILE`; daily-panel receipts need `--membership FILE`.

## Roles and runtime

Profile `@AGENT_PROFILE@`, runtime `@AGENT_RUNTIME@`: owner
`@OWNER_MODEL@`/`@OWNER_EFFORT@`, implementer `@IMPLEMENTER_MODEL@`/`@IMPLEMENTER_EFFORT@`,
runner `@RUNNER_MODEL@`/`@RUNNER_EFFORT@`, reviewer (read-only)
`@REVIEWER_MODEL@`/`@REVIEWER_EFFORT@`.

Reserve with `review-reserve ATTEMPT_ID --root ROOT --bundle-dir BUNDLE_DIR
--wake-key WAKE_KEY --owner-key OWNER_KEY` plus the record flag for
this runtime. Output `model`, `effort`, and `prompt_sha256` are authoritative.
Invoke exactly the returned spawn arguments for the reviewer; never compose,
edit, or add to them. Reserve and spawn once; STOP and call
`sessions_yield`; reconcile and collect only in the completion callback after
the owner turn ends. No separate acknowledgement is created or retyped.
Unfinished stays pending, never FAIL; a failed, errored, or aborted reviewer is
terminal REVIEW_FAILED with the host reason as sole finding. Do not use
the retired `--core-database` or ACPX flags, infer paths, or repair a verdict;
preserve refusal on failed correlation.

Contained synthetic stages are an operator host step under both runtimes: the
implementer writes and commits the harness and tests, the operator runs them once
on the host, and a packager commits the PASS evidence.

@RUNTIME_SECTION@
