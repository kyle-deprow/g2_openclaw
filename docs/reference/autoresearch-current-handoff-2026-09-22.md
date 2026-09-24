# Autoresearch handoff — current state and recovery path

Status date: 2026-09-22 (America/Chicago)

## Executive summary

The simplified research loop exists and its intended model split is in place:

1. GPT-6 Astra owns the hypothesis, attempt lifecycle, and final decision.
2. GPT-5.6 Luna at xhigh implements the attempt.
3. Claude Code Opus 5 at high effort reviews the immutable committed bundle.
4. GPT-5.6 Luna at xhigh runs the historical experiment after review PASS.
5. Astra decides whether the hypothesis is supported, rejected, inconclusive, or blocked.

The loop has **not completed one historical experiment**. Thirteen admissions were consumed across H0001-H0006, but all work stopped before a real historical job. The most advanced attempt, H0006-A001, passed a substantial synthetic containment run and reached a real Opus review. Opus returned **FAIL** because required source provenance was not captured from inside the sandboxes and because evidence retention/source-binding was not sufficiently self-contained.

The durable research store does not yet contain that verdict. It still shows H0006-A001 as `IMPLEMENTED` with no review verdict because host correlation/collection failed after the ACP review completed. The campaign is `PAUSED` at resume sequence 33, the research owner service is inactive, and the OpenClaw gateway is currently healthy.

Do not claim a trading result or alpha. There is no historical outcome to interpret.

## Authoritative current state

| Surface | Current state |
|---|---|
| Campaign | `PAUSED`, resume sequence 33 |
| Campaign admissions | cap 13; all 13 consumed |
| H0001-H0005 | `DECIDED`; all 12 attempts closed without a historical run |
| H0006 | `FROZEN` |
| H0006-A001 | `IMPLEMENTED`; no projected review verdict, run, or decision |
| Research owner service | inactive |
| OpenClaw gateway | active and healthy on 2026-09-22 |
| Last driver failure | 2026-09-16: connection refused / opening-handshake timeout, followed by fatal campaign pause |
| G2 OpenClaw main | `e4ce0c1aa8f7e856a04baf10ee2505b78df001ba` |
| Quantipy main | `44c77a8656d2444885340de2c1b9b9fd87c24c47` |
| Historical jobs completed | 0 |

Canonical state:

- Research root: `/home/dev/.openclaw/research-v2`
- Research database: `/home/dev/.openclaw/research-v2/state.sqlite3`
- OpenClaw task database: `/home/dev/.openclaw/state/openclaw.sqlite`
- Owner key: `agent:research-orchestrator:autoresearch:quantipy-v2`
- Campaign policy reference is stored in the `campaign` row. It authorizes the 12-to-13 extension only; it explicitly forbids automatic further extension.

The G2 checkout has three pre-existing untracked documents that must be preserved:

- `docs/reference/autoresearch-cleanup-inventory.json`
- `docs/reference/autoresearch-simplification-plan.md`
- `docs/reference/fable-autoresearch-handoff.md`

Quantipy main is clean. Several unrelated Quantipy worktrees remain registered; do not remove or force-clean them without reconciling their ownership and state.

## The research hypothesis

The retained research proposal is documented in Quantipy at:

`/home/dev/repos/quantipy/docs/research/conditional-short-term-reversal.md`

H0006 tests a simple conditional sector-ETF reversal strategy:

- Fixed universe: SPY plus 11 liquid, unlevered US sector ETFs; SPY is the market reference/control rather than a traded sector candidate.
- Data window: 2021-10-01 through 2026-07-31, using 1,212 XNYS sessions.
- Scored window: 2023-01-03 onward, split into four chronological folds of 250, 252, 250, and 145 sessions (897 total).
- Signal: a negative open-to-close sector return whose 126-session market-residual z-score is at most -2.
- Entry: decide at the regular close and enter at the next regular open.
- Exit: fifth trading-session close, with entry counted as session one.
- Portfolio: one long sector position or cash, sized to 1/11 of NAV; no leverage or overlapping positions.
- Controls: unconditional dip-buying, cash, and matched-date SPY.
- Costs: 5, 10, and 15 basis points per side.
- Classification: `DEVELOPMENT_VALIDATION` with `UNKNOWN_HISTORY`; this is not a pristine holdout.

The individual-stock rule forbidding holdings across earnings is not exercised because the current universe contains ETFs only. The frozen contract explicitly does **not** claim that constituent earnings exposure has been removed. Moving to stocks requires trusted point-in-time earnings data and must fail closed when earnings status is unknown.

Reddit sentiment and ML were deliberately deferred. They should only be added as separately admitted hypotheses after the price-only baseline runs and after their point-in-time data contracts are proven. Missing news or Reddit data must never be interpreted as a lack of information.

Pinned inputs remain under `/home/dev/autoresearch-reversal-20260911.9AbPOA/inputs` and `/home/dev/autoresearch-reversal-20260911.9AbPOA/receipts`. The frozen H0006 spec contains their exact SHA-256 bindings. Do not redownload, rehydrate, or silently replace them.

## What H0006-A001 accomplished

Final reviewed implementation commit:

`abe15398e7438d2888018e7d48c8bb205fae6c30`

The disposable H0006-A001 worktree no longer exists, but the final commit is durably reachable in the authoritative Quantipy repository through:

`refs/heads/archive/research-v2-H0006-A001`

The earlier complete proof generation is also retained at:

`refs/research-archive/H0006-A001-prior-proof-0279aaaa`

which points to:

`0279aaaa73df4ed8a0f963759c53051fbe06d81b`

The immutable review bundle remains at:

`/home/dev/.openclaw/research-v2/review-bundles/H0006-A001`

Reservation identity:

- Label: `H0006-A001-45649b4f7fad4cc02f5cd397`
- Bundle SHA-256: `e9d4a2491d381fb505682e29a46ff67088a451aa189ae8cbd79664ae8a5544be`
- Frozen spec SHA-256: `709dacefe3729fbf1a34320ac5f5172dc22f4c6a9650a13b0fd3d6046c15aa45`
- ACP run ID: `41f57bfe-92f1-4a73-9d7d-7b092753f2a7`
- ACP child session: `agent:claude:acp:f33ea5d9-03ea-48aa-b096-f40e4c1dd5e0`

The attempt produced a genuine operator-host synthetic integration run with:

- all six contained target stages;
- all six contained evaluator stages;
- the contained analysis stage;
- 14 intended negative cases;
- 307 artifact checks;
- 13 focused unit tests;
- unchanged pre/post tested source bytes and Git objects.

That result proves substantial software behavior only. It is not historical economic evidence.

The proof package was reduced from an invalid 165,466,637-byte aggregate to approximately 86.3 MB without raising the 128 MiB limit. A stale `EvaluationSpecSet` aggregate digest was also corrected before submission. These packaging corrections did not change the strategy or execution source.

## The actual Opus result

Claude Code Opus 5, configured at high effort, completed its read-only ACP review on 2026-09-15. The exact terminal JSON is preserved in:

`/home/dev/.openclaw/workspace/state/sessions/agent%3Aclaude%3Aacp%3Af33ea5d9-03ea-48aa-b096-f40e4c1dd5e0%3Aoneshot%3Af7035cf8-49a7-4172-844c-d195eb843134.json`

The verdict was `FAIL`, with four findings:

1. **High — missing in-sandbox import provenance.** Target, evaluator, and analysis artifacts do not record actual module `__file__` paths and hashes from inside the containment namespaces. Nothing proves that shared Quantipy modules resolved to `/snapshot/src/quantipy` during each real stage.
2. **High — host hashes are insufficient.** The integration collector hashes worktree and snapshot files from the host. That proves host bytes, not which code the sandboxed processes imported. `ENTRYPOINT_SOURCE_PATH` exists in the implementation but is never emitted.
3. **Medium — prior evidence is not self-contained in the bundle.** A 334-file superseded generation was replaced by Git/tmp pointers. The frozen retention language says prior generations remain under `prior-blocked`; the reviewer could not verify those bytes from the immutable review bundle.
4. **Low — source-equivalence language is ambiguous.** The final proof commit contains proof files absent from the tested commit. The bundle asserted the intermediate chain rather than including sufficient parent/diff evidence and explicitly limiting equality to execution-affecting files.

The first two findings are unquestionably valid: H0006's frozen `import_contract` and `integration_gate` explicitly require provenance captured inside containment. The retention finding also exposes a real contract ambiguity. A prior preflight considered an immutable Git archive pointer sufficient, while Opus interpreted “lives under prior-blocked” as requiring the bytes in the review bundle. Do not rely on the pointer interpretation in the next attempt.

## Why the store still says the review is unresolved

The review reservation and acknowledgement were persisted, but strict host collection failed. The child notification was truncated and the owner could not correlate the terminal ACP record through the host task ledger. The campaign was paused with:

`review_unresolved:H0006-A001`

The operator blocker is recorded at:

`/home/dev/.openclaw/workspace-research-orchestrator/campaigns/conditional-reversal-20260911/H0006/H0006-A001-review-collection-blocker.json`

This is a bookkeeping failure after a real review, not an absent review and not permission to dispatch a duplicate review. The exact FAIL must be collected or reconciled into the research store before closing A001.

## What went wrong at the program level

The core scientific idea is reasonable and deliberately simple. The execution program became dominated by proof construction and lifecycle recovery:

- 13 admissions and many synthetic/package iterations produced zero historical backtests.
- Requirements that should have been standard runner outputs—especially in-sandbox import provenance—were discovered at final review rather than enforced by an early preflight.
- Evidence was repeatedly copied, rebound, and repackaged, creating size and retention conflicts.
- The owner spent long turns rereading already-validated evidence, increasing callback timeouts and context use.
- A completed ACP verdict could not be recovered automatically from its acknowledged run/session identity.
- The owner service continued producing timeout status events while the campaign was paused and ultimately stopped on a transport handshake failure.

The loop architecture is now small enough; the evidence protocol around it is still overengineered and insufficiently mechanical. The next work should simplify and standardize evidence production, not add agents or more scientific gates.

## Required recovery sequence

### Phase 1 — reconcile H0006-A001 without rerunning it

1. Keep the campaign paused and the owner service stopped.
2. Preserve the review bundle, ACP session files, archive refs, research database, and campaign records.
3. Run the canonical review reconciliation/collection path against the existing reservation and ACK. Never dispatch a second review.
4. Project the exact Opus `FAIL` and its four findings into H0006-A001.
5. Close A001 with `RETRY` only after the verdict is durably projected.

Supported commands are:

```bash
.venv/bin/gateway-cli research review-reconcile H0006-A001 \
  --root /home/dev/.openclaw/research-v2 \
  --core-database /home/dev/.openclaw/state/openclaw.sqlite

.venv/bin/gateway-cli research review-collect H0006-A001 \
  --root /home/dev/.openclaw/research-v2 \
  --core-database /home/dev/.openclaw/state/openclaw.sqlite \
  --acpx-sessions /home/dev/.openclaw/workspace/state/sessions \
  --claude-projects /home/dev/.claude/projects
```

If those commands cannot recover the already-acknowledged terminal record, fix the shared G2/OpenClaw collector. Do not hand-edit the research database or manufacture a review receipt.

### Phase 2 — repair the shared loop before spending another admission

This is operator-owned infrastructure work and should use an independent implementation/review/fix cycle:

1. Make review collection deterministically recover an ACP terminal response using the persisted reservation label, run ID, child session key, and immutable session record—even if task-ledger delivery failed or a notification was truncated.
2. Ensure a paused campaign idles without launching repeated owner turns or accumulating one-minute timeout events.
3. Make native result delivery durable enough that an owner can resume from a completed child record rather than redoing a long verification turn.
4. Add a pre-review mechanical gate for every frozen proof requirement. In particular, reject a bundle that lacks in-sandbox source provenance before paying for Opus.
5. Keep normal monitoring in a cheap Luna worker and notify the top-level orchestrator only on stage transitions or genuine failures.

Deploy any repo-managed OpenClaw changes with `bash scripts/push-openclaw-config.sh`, restart the gateway, validate configuration, and test the exact failed recovery scenario. Do not add provider fallbacks or legacy paths.

### Phase 3 — explicitly authorize one clean H0006-A002 admission

All 13 admissions are consumed. Opening A002 requires an explicit operator policy change from 13 to 14. Do not reset counters, reopen A001, or infer this authority from the old 12-to-13 extension.

If authorized, use exactly one additional admission and retain the same frozen H0006 economic design. H0006's intrinsic attempt budget permits A002, but the campaign-wide cap currently does not.

### Phase 4 — build A002 cleanly

Do not carry A001's bulky proof tree into A002. A001 remains preserved as a closed historical attempt through its archive branch, review bundle, ACP transcript, and research records. A002 should begin with one clean `current` proof generation.

Before the synthetic run, design and test the following evidence contract mechanically:

- Each target sandbox records its actual entrypoint and imported shared-module paths plus SHA-256 hashes from inside that namespace.
- Each evaluator sandbox proves the Quantipy modules it actually loads resolve beneath `/snapshot/src/quantipy`, with hashes captured inside containment rather than recomputed only on the host.
- The analysis sandbox emits the same provenance for `h0006.analysis` and all required shared modules.
- The host collector binds those in-sandbox attestations to the mounted snapshot, tested Git commit, exact command, environment, and mount plan.
- The final review bundle contains the tested commit, final proof commit, parent chain/diffs, and an explicit manifest distinguishing execution-affecting bytes from proof-only bytes.
- If a proof generation is superseded within A002, preserve its required bytes under A002's committed `prior-blocked` tree. Do not substitute a pointer unless the frozen contract is explicitly amended before admission and the reviewer instructions agree.

Run one contained synthetic validation, package once, perform the mechanical preflight, and then dispatch one Opus review. Avoid proof-only commit churn after the bound run where possible.

### Phase 5 — complete the real loop

Only after actual Opus PASS:

1. Luna runs the historical job against the pinned October 2021–July 2026 inputs.
2. The job produces all six evaluator scenarios and seven declared analysis artifacts.
3. Astra validates identities, receipts, data bindings, costs, folds, sample counts, null tests, and economic gates.
4. Astra records a useful terminal decision: support, reject, inconclusive, or evidence-blocked.
5. Preserve the negative result if negative. Do not tune thresholds or add Reddit/ML variants to rescue it.

The first end-to-end completion is successful if it produces an honest, review-accepted historical conclusion. It does not need to find alpha.

## Definition of done

This effort is not done until all of the following exist in durable state:

- projected Opus PASS for a committed attempt;
- one real historical job with retained artifacts and no runner repair;
- validated five-session execution and fixed cost scenarios;
- a final Astra decision with economic results and limitations;
- no unprojected child/review/job state;
- campaign and attempt closed deliberately;
- concise operator-facing summary pointing to the immutable evidence.

Synthetic PASS, process success, an API response, or a zero-trade run is not completion.

## Non-negotiable guardrails

- No live trading or order placement.
- No alternate providers, compatibility shims, or silent fallbacks.
- No MemPalace writes by research agents.
- No data redownloads or environment clones.
- No holding horizon above five trading sessions.
- No individual-stock position may cross earnings; stocks remain disabled until point-in-time earnings coverage exists.
- No automatic attempt-cap extension.
- No duplicate Opus review for A001.
- No deletion of review bundles, ACP sessions, research records, archive refs, or unrelated dirty worktrees.
- No claim of alpha from synthetic evidence or exposed development history.

## Immediate operator decision

The next operator should first reconcile A001's existing FAIL and fix the shared collector/paused-loop behavior. After that, the only material decision required is whether to authorize campaign cap 14 for one clean H0006-A002 attempt. If that admission is not authorized, the campaign should be closed honestly as an engineering failure to reach historical evaluation, with no market-strategy conclusion.
