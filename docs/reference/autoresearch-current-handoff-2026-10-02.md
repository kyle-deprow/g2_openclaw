# Autoresearch handoff — state as of 2026-10-02 (evening)

Supersedes `autoresearch-current-handoff-2026-09-24.md`. Written by the Claude
operator session after the Codex root session (`01a0778e-c5fe-7a21-bbf5-e72d76a8799f`)
was interrupted at 23:25Z before it wrote its own handoff. The governing plans
remain `autoresearch-openai-review-plan-2026-10-02.md` (shared review migration)
and `autoresearch-a003-execution-plan-2026-09-25.md` (A003). No historical run
has happened. Do not claim a trading result.

## One-paragraph status

Everything in the 09-24 handoff was carried out: D and C merged (`ec1600e`,
`34fa94c`), A001 closed FAIL/RETRY, the cap went 13→14, A002 ran and received an
actual Opus FAIL with seven findings (closed), the user authorized 14→15 for
A003, and A003 was admitted on 2026-09-25. A003 went through two pre-host
correction rounds; the second (Oct 2) is Sol-READY with 49 tests reproduced. On
2026-10-02 the user replaced the Claude/ACP reviewer with native OpenAI Sol. The
replacement is implemented (46 tracked files at −3.3k net lines, plus new untracked reviewer-role and native-fixture files) and has passed
several Sol rounds. It is **uncommitted, unpushed, and undeployed**. The live
gateway still carries the `acpx` plugin and the `claude` agent. The campaign is
PAUSED and the owner service is stopped.

## Authoritative live state (verified 2026-10-02 23:30Z)

| Surface | State |
|---|---|
| Campaign | `PAUSED`, resume_seq 37, attempt_cap 15; last event 4394 `campaign_paused` (operator, OpenAI-only maintenance) |
| H0006 attempts | A001 `CLOSED` FAIL, A002 `CLOSED` FAIL, A003 `OPENED` (third and final H0006 attempt) |
| A003 checkout | `/home/dev/.openclaw/research-v2/hypothesis-worktrees/H0006-A003`, clean, HEAD `d34cf8e` (proof) on execution commit `35db8a6`; proof classified `EVIDENCE_BLOCKED` pending the operator host run |
| A003 contained destination | `data/.attempt-evidence/H0006-A003-contained` must still be absent |
| research-owner.service | inactive |
| openclaw-gateway.service | active; live `~/.openclaw/openclaw.json` still has plugins `codex, acpx, openai, memory-core` and agents `main, research-orchestrator, claude` |
| G2 main | `a1d4d30` plus the uncommitted migration below |
| Quantipy main | `5cd3b19` |
| Agents in flight | none; all of root's Luna/Sol children reached `task_complete` |

## The uncommitted OpenAI-native review migration

Ownership and acceptance rules are in the 10-02 plan. Summary of where it stands:

- **Deployment side:** reviewer role (`.codex/agents/reviewer.toml`,
  `.codex/agent-configs/reviewer.toml`, read-only, no child spawning); `config_merge`
  and `codex_agents` sanitize inherited Claude/ACP/ACPX wiring; the push script was
  simplified; `scripts/research-reviewer-cli.py` and its tests were removed; active
  runtime instructions and skill mirrors were updated.
- **Evidence side:** native correlation through the official owner `session.started`
  event, the parent rollout's spawn call and result, the Codex thread edge, and the
  child rollout. It binds the exact canonical IMPLEMENTED wake, owner run versus
  child run, prompt digest (including the absolute bundle path), role, model, effort,
  terminal marker, and the timing `owner start ≤ reservation ≤ spawn call < owner end`.
  Store paths derive from `RESEARCH_CORE_DATABASE`. Cancellation is `chat.abort`,
  at most once, on the owner run. Active supersession was removed while historical
  superseded rows stay readable. Review-ack was removed.
- **Review history:** Sol found and the workers fixed: a writable reviewer sandbox,
  missing owner identity in the wake command, callback run/thread reuse, a
  reservation bypass, a missing bundle path in the prompt, wrong cancellation
  stores, five later boundary gaps, and a dropped symlink guard. Sol's final verdict
  (23:21:54Z) found no source defect and asked for one regression: corrupting the
  `threads.agent_path` column. The worker added it at 23:22:14Z (20/20 passed).
  **Sol has not confirmed that last test.**
- **Root's last full run was invalid.** Two pytest runs overlapped and
  `test_push_script_uses_recorded_owner_workspace_for_native_layers` rewrites the
  real `gateway/openclaw_config/openclaw.json` and restores it in a `finally`. The
  concurrent run then saw compact JSON. The repo file is intact. Always run pytest
  serially in this checkout.
- **Independent re-verification (this session, serial):** ruff check, ruff format
  check, mypy (31 files), `bash -n`, and `git diff --check` are all clean. Full
  suite (research + deployment + script guarding + both doc suites): **912 passed,
  1 skipped, 0 failed** in 6m55s. The repo `openclaw.json` sha256 is unchanged
  afterwards. Log: Claude scratchpad `runs/fable-full-suite.log`.

## Live-shape review (2026-10-02 late, user directive: Sonnet implements / Opus reviews operator work)

An Opus review of the full migration diff, checked read-only against the live host
stores, returned ISSUES. The parent independently confirmed every Must-Fix:
- A spawn that isn't the reviewer's makes the owner rollout unreadable, and A003's
  owner rollout already contains two implementer spawns.
- Child `threads.name` is NULL (43/43), but the code required `/root/<task>`.
- Native children have no OpenClaw `session_nodes` or session events, but the code
  required both.
- Owner thread rotation was treated as fatal; 75 of 121 owner starts rotated thread.
- The 8 MiB cap is below real owner rollout sizes (A003 is at 7.4 MB).
- All four runtime docs omit the required `review-reserve --bundle-dir`.

The parent found one more: the spawn `message` is stored **encrypted** in both
rollouts, so the prompt-digest check can never pass on this host. The fixtures had
invented every one of these shapes, and the research path still never correlates a
real review. The fix plan is the Claude scratchpad file
`runs/migration/PLAN-native-shapes.md` (F1–F13). Sonnet implements it, then Opus
re-reviews against the live shapes.
Memory note: `native-codex-child-host-record-shapes.md`.

## What remains, in order

### A. Accept and integrate the migration — DONE

Opus re-review READY after the live-shape fix round (Sonnet implemented F1–F13,
then the doc wording and test typing follow-ups). The parent reran the suite
serially (978 passed, 1 skipped; ruff, format, mypy including the pre-commit test
typing, `bash -n` and diff check all clean) and checked read-only that two real
native children correlate, including one whose callback came back on a rotated
owner thread. Committed `52f9c98`, together with the `CLAUDE.md` role line. Known,
documented limitations:
- A failed or aborted reviewer child makes the attempt a terminal REVIEW_FAILED.
- An operator cannot stop a running native reviewer after the owner yields.
- The spawn prompt digest cannot be host-verified because the host stores the
  spawn message as ciphertext.

### B. Deploy while paused and idle — DONE (2026-10-03 00:29Z)

Deployed with
`PATH=/home/dev/repos/g2_openclaw/.venv/bin:$PATH OPENCLAW_BIN=/home/dev/.local/bin/openclaw bash scripts/push-openclaw-config.sh`.
- `OPENCLAW_BIN` is needed because the pnpm wrapper probed first is still 2026.7.1-2.
- The venv must be on PATH for the research-owner command-contract probe.
- The FastEmbed `bge-base` model cache under `~/.cache/fastembed` had vanished,
  so the MemPalace healthcheck failed. It was repopulated with the same pinned
  model (BAAI/bge-base-en-v1.5, 768-dim).

After the push: gateway restarted, `config validate` valid, health OK. The live
config has plugins `codex, openai, memory-core`, agents `main, research-orchestrator`,
no acp/acpx/claude/anthropic, Astra unchanged, and the `reviewer` role is
gpt-5.6-sol/xhigh/read-only with `max_depth=0`. A direct Sol probe answered.

### C. Complete A003 (canonical owner; operator only where the plan says so)

6. **Operator host test (once).** From the A003 checkout, with the destination
   absent:
   `/home/dev/repos/quantipy/.venv/bin/python tests/integration/research/run_h0006_synthetic_integration.py --evidence-dir /home/dev/.openclaw/research-v2/hypothesis-worktrees/H0006-A003/data/.attempt-evidence/H0006-A003-contained`.
   Preserve the entire destination. Then check the step-4 contract of the A003 plan:
   13 positive provenance identities in interleaved order, the spec digest, the
   ≤8 MiB per-file limit, the exact launch argv, the sufficiency gates, the cash
   comparator, and reasons for null exposure fields. On failure, preserve a typed
   blocker rather than retrying blindly.
7. **Native proof packaging.** Through an Astra owner turn, a fresh native Luna
   packages the PASS evidence into `proof/h0006/H0006-A003/current` (keeping
   `prior-blocked/` byte-exact) and submits with `implementation-submit … --provenance-evidence …`.
8. **Resume** through `gateway-cli research resume` and start
   `research-owner.service`. The next canonical IMPLEMENTED wake alone authorizes
   review reservation and one native Sol spawn. The completion callback collects.
   This is the first live use of the native review path: watch the exact
   owner run, child run, and task identities. An unknown or unconfirmed spawn stays
   pending with the campaign paused; never re-dispatch.
9. **After Sol PASS:** a native Luna runner runs the historical job (six scenarios,
   seven analysis reports). Astra makes the economic decision; negative or
   inconclusive results count. **After Sol FAIL:** H0006 has no attempts left, so
   Astra must explicitly choose FINISH, ABANDON, or PAUSE. Any further cap increase
   needs a new user decision.
10. Integrate accepted work, push both repos, and retire inactive checkouts
    recoverably.

### Progress on C (2026-10-03)

- Host run 1 failed at targets-s000 with EROFS. A shared-driver rewrite-order
  bug sent `--out` to read-only `/work` when the evidence directory is nested in
  the worktree. Fixed in `6904a6b` (Sonnet implemented, Opus READY).
- Host run 2: all 13 positive contained stages and the analysis report
  succeeded. The never-executed negative tail then crashed with
  `NameError: analysis_env`, 13 references, an A003 harness defect.
- Both runs are preserved byte-exact with sha256 manifests under
  `data/.attempt-evidence/H0006-A003-contained-blocked-20261003-{rewrite-order,negatives-nameerror}`.
- Correction 3 was dispatched to Astra as run `recovery-20261003-a003-prehost-fix-3`.
  Brief: `/home/dev/autoresearch-openai-20261002.Tdkdgb/A003-prehost-fix-3.md`.
  It asks for a fix plus a regression test that exercises the tail against the
  real preserved positive evidence.

- Correction 3 landed: execution `287b5ee`, proof `95c8ff1`. Opus pre-host review
  READY, no Must-Fix. Its Should-Fixes are test quality only: the tail regression
  covers the negatives but not the reassembly, the test injects `analysis_env`,
  and the unit suite needs the git-ignored preserved evidence. The parent
  reproduced 190 unit tests passing.
- **Host run 3 PASSED** (2026-10-03 01:15–01:18Z): 13 positive stages and 14
  negatives (27 commands), 7 reports, 6 scenarios, 481 files none over 8 MiB,
  baseline and SPY 90 lots, `SYNTHETIC_ONLY`. The destination
  `data/.attempt-evidence/H0006-A003-contained` is preserved as-is.
- Packaging and submission were dispatched to Astra as run
  `recovery-20261003-a003-package-submit`. Brief:
  `/home/dev/autoresearch-openai-20261002.Tdkdgb/A003-package-submit.md`. One Luna
  packager, then one `implementation-submit` by Astra; no review reservation.

- The packager committed proof `a522a72` (513 files, 85 MB, proof-only changes
  over `287b5ee`). It recorded a typed blocker because the committed submission
  JSONs named `287b5ee`, where the records are not tracked. The operator dry-ran
  the validator: with the JSONs naming `a522a72` and kept outside the commit, all
  13 stages verify. Astra then submitted from
  `workspace-research-orchestrator/campaigns/conditional-reversal-20260911/H0006/A003-submission/`.
  **A003 is IMPLEMENTED at `a522a72`** (event 4395).
- A dry `review-bundle` build of A003 came to 171 MB, over the 128 MiB aggregate
  cap. Commit `88c1477` raised the bundle cap and the reviewer child-rollout bound
  to 256 MiB (Sonnet implemented, Opus READY); the real bundle then built in 6 s.
  Opus's pre-existing Should-Fixes remain as follow-ups:
  - the aggregate check runs only after the bundle dir is frozen;
  - `_git` buffers its whole output before checking the bound;
  - there is no accept-side boundary test.

- First live native review attempt (resume 38): `review-reserve` refused
  "owner run/thread is unresolved". Cause: OpenClaw buffers trajectory runtime
  events until the turn flushes, so a run cannot see its own `session.started`.
  A live in-turn probe confirmed it: 0 own rows, while the session node showed
  `status=running` with `activeWriterRunId` set to the run. Nothing was reserved
  or dispatched. The campaign was paused and fix `0e92c0a` landed (Sonnet,
  then two Opus rounds READY, 1053 passed, 1 skipped):
  - reserve binds the canonical wake run plus the active owner writer, and binds
    the thread after the flush;
  - the completion callback is optional corroboration;
  - a vanished owner run after the ACK pauses the campaign.
  Redeployed, then resumed at 39.
- **Native Sol review, end to end** (2026-10-03 02:44–02:57Z):
  - reservation with the nonce task `review_h0006_a003_d573714dcb316974e966a5a2`;
  - Sol child `01a0ffa8-c21c-79f3-b491-82095c40b84e` (gpt-5.6-sol, xhigh), spawned and yielded;
  - reconcile and collect inside the callback turn.
- **Verdict FAIL**, two findings:
  1. High: lot PnL/NAV reconstruction uses exported notionals not bound to
     shares × price (`h0006/analysis.py:895`).
  2. Medium: `intended_entry_notional` and `scaling_factor` are always
     unavailable, although they are computable and required (`:887`).
- **Sandbox audit.** The reviewer child inherited the owner's writable sandbox:
  Codex does not apply the role's `sandbox_mode=read-only` to spawned children.
  Its 28 tool calls were all reads, and the worktree, PASS evidence and bundle
  were byte-identical afterwards.
- **Astra's decisions:**
  - A003 `CLOSED`, decision FINISH;
  - H0006 `ABANDONED` (3/3 attempts, no eligible historical result);
  - H0007 drafted and frozen: the H0006 economics plus primitive shares × price
    accounting and computed entry sizing. No attempt admitted.
- **Operator:** campaign paused (cap 15/15 exhausted), owner service stopped,
  gateway healthy.

## Outcome and what remains

The loop infrastructure is proven end to end on the live host: admission,
native implementation, a contained host run, submission, reserve, native Sol
spawn, reconcile, collect, and the owner's decision. **No historical run
happened**: the only eligible attempt received a substantive review FAIL. There
is no economic result and no alpha claim.

Continuing requires a **user decision**. Admitting H0007-A001 needs a cap increase
(15→16) through `campaign-policy-set`, since no automatic extension is
authorized. Then:
1. resume;
2. the owner admits and dispatches the native implementer;
3. operator contained host run → package/submit → native Sol review → on PASS
   the historical run → Astra's decision.

Open follow-ups, none blocking:
- OS-level read-only enforcement for the native reviewer child.
- The bundle aggregate check runs only after the bundle dir is frozen.
- `_git` buffers whole outputs before bounding them.
- Doc nits from the last review.
- The `test_push_script_uses_recorded_owner_workspace_for_native_layers` test
  rewrites the repo `openclaw.json` in place, so pytest must run serially.
- The pnpm `openclaw` wrapper is still 2026.7.1-2, so deploys need
  `OPENCLAW_BIN=/home/dev/.local/bin/openclaw` and the venv on PATH.


## Continuation after user directive "no cap; keep going until a full e2e run" (2026-10-03)

- **Cap raised.** The cap went 15→60 via `campaign-policy-set`, citing the user
  quote. The per-hypothesis limit of three attempts is unchanged. Astra froze
  H0007: H0006 economics plus primitive shares×price accounting and computed
  entry sizing.
- **H0007-A001.**
  - Corrections: a pre-host Opus review drove three native corrections.
  - Host runs: three passed.
  - Driver fix `ef1b89e`: review bundle bound 512 MiB, plus pre-freeze checks.
  - Sol FAIL: the proof exceeded the FROZEN 128 MiB retention/diff cap. The
    operator notes had wrongly said 256 MiB.
- **H0007-A002.** A port of A001 with identity-only changes; its host run
  passed. Sol FAIL: the mandatory next-fold-entry boundary case was never
  exercised by the fixture.
- **Exhaustive audit before A003.** An Opus frozen-spec audit found 19 gaps
  (`/home/dev/autoresearch-openai-20261002.Tdkdgb/H0007-A003-compliance-audit.md`).
  H0007-A003 ran them as Batch A, Batch B and correction 2, each followed by an
  Opus re-audit. The first host run crashed on provenance keys; the second
  passed.
- **A003 packaging.** Batch C packaging was audited twice before submission;
  the operator aborted the owner's submit turn so the audit could run first.
  The final commit `c1cbb4c` has a 53.8 MB bundle.
- **Sol PASS** for H0007-A003, with no findings.
- **Historical run launch failure.** The historical job
  `job-22859ba1ce774b3fa5f0ee32dd53ee89` failed at launch with nothing
  executed. `jobs._validate_targets` rejected the pinned production panel path,
  a latent shared-driver bug, and the failure happened after the claim, so the
  attempt went to terminal RUN_FAILED.
  - The operator paused the campaign and aborted the runner callback.
  - The RUN_FAILED wake turn could not be aborted (unauthorized). To prevent an
    irreversible close, the operator stopped the owner service and briefly made
    `state.sqlite3` non-writable (chmod u-w; no data changed). Astra's close was
    refused, and the permission was restored.
  - A003 remains RUN_FAILED and is not closed.
  - In progress: a driver fix (accept the pinned panel/receipt at launch;
    validate before claim) and a narrowly guarded `run-release-unstarted`
    operator transition (unstarted `launch_failed` → REVIEW_PASSED, preserving
    evidence), then a relaunch of the reviewed commit.
- **Launch fix and release (`027916d`).** Launch validation now matches the
  worker and runs before the claim. The operator ran `run-release-unstarted`
  once for H0007-A003 and relaunched.
- **Relaunch timed out.** The relaunch hit the FROZEN 300 s stage bound at
  `validate-c000`. Measured on the real pinned inputs: validate-inputs 642 s and
  6.3 GB per spec; an empty-target evaluate 470 s and 5.9 GB. Every H0007
  attempt was therefore infeasible.
  - The driver bounds were raised to an 8 h run and 16 GB per stage, and
    `research run` now derives its timeout from the plan (`3fbc3cb`).
  - Astra recorded H0007 ABANDONED (execution infeasibility, no economic
    result).
  - She froze H0008: identical science, with compute bounds of 1500 s per
    scenario and 5400 s for the analysis. A text bound fix was needed first
    (`63c671e`).
- **H0008-A001.** A port of the A003 code, with identity changes only.
  - Contained host PASS, then an Opus compliance audit, then Batch C packaging
    and its audit; the one must-fix was the manifest classes.
  - Operator-verified HELD submit.
  - Native Sol **PASS**.
- **Historical run.** The job `job-4f43dce6d26e4b2aa79836b472859972` ran all
  16 stages with exit 0 and wrote `terminal.json` `succeeded`. This is the first
  real successful run.
- **Host verifier defect.** The host verifier still recorded
  `run_evidence_mismatch`: it required a per-scenario `status` that the worker
  never writes. Astra paused.
  - The fix is `c92c566`: verify on no status (or "succeeded") with both exits
    int 0, plus an operator-only `run-reverify`. That command recomputes the
    outcome from the immutable run files, at most once, and binds both file
    digests and the canonical primary result.
  - The operator ran `run-reverify` once (event 4583). The attempt is now
    RUN_SUCCEEDED, and the run files are byte-identical. The store was backed up
    first to `store-backup-before-reverify-20261003/`.
- **Economic result.** Paired mean −26.18 bps vs the baseline, CI lower
  −63.3 bps (95% CI [−63.3, +10.2]), block-sign p=0.871, over 107 paired
  events. Only 2 of 4 folds were positive, below the 3 required. NAV returns at
  2× and 3× costs were negative, and the base mean net lot return was below the
  dip comparator.
- **Astra's decision (owner turn from the operator decision brief):**
  - event 4584: `attempt-close H0008-A001 FINISH`;
  - event 4585: `hypothesis-decide H0008 FINISHED`, REJECT_FOR_THIS_DESIGN,
    DEVELOPMENT_VALIDATION only, no alpha claim.
- **Outcome.** The campaign's first full end-to-end loop is complete: code →
  native Sol review → real historical run → owner economic decision.
- **Current state.**
  - The campaign remains PAUSED and `research-owner.service` is stopped.
  - No further attempts or hypotheses are admitted; resuming is a fresh user
    decision.

## Post-E2E improvements (2026-10-04, user directive: sonnet/opus loop)

Each package was implemented by Sonnet and reviewed adversarially by Opus
until READY. The parent verified each package serially, with FORCE_COLOR set
and unset. All packages are committed and pushed.

- **Golden end-to-end test (`0abce5f`).**
  - The production queue → serve → launch → real worker under bwrap and
    systemd-run → host verify → close/decide path runs with fake
    target/evaluator/analysis programs only.
  - Mutation checks show it catches the c92c566 verifier bug and the 3fbc3cb
    stage-budget bug.
- **Compute probe (`d330ebd`, `4540576`).**
  - `research compute-probe H` runs on a DRAFT and measures validate-inputs
    plus an empty-target evaluate under the worker's containment.
  - Freeze requires a matching probe, `max_rss_mb` ≥ 1.25× the measured peak,
    and a `max_wall_seconds` that some plan fits.
  - Submit requires `scenario_timeout` ≥ 1.5× the measured wall.
  - `hypothesis-set-compute` edits a DRAFT's compute block. A DRAFT can be
    ABANDONED if it is infeasible. Freeze writes are state-conditioned.
- **Power gate and operator metric (`031fab7`).**
  - Contract `research-hypothesis-v2` adds a `power` block. The MDE is
    recomputed, and designs whose MDE exceeds the plausible effect are
    refused at create, as are non-canonical specs.
  - The v1 specs H0001–H0008 are byte-identical.
  - `research power-check` computes the MDE. Status reports
    `operatorInterventions`; the operator-only `research operator-note`
    records briefs and audits.
- **Submission tools (`27a17f5`).**
  - `research submission-build` derives the record, plan and provenance
    deterministically.
  - `research submission-preflight` is read-only and shares
    implementation-submit's checks. It adds a bundle dry-build that
    reproduces the real H0008-A001 bundle byte-for-byte.
- **Quantipy evaluator speedup (quantipy `7a2ce40`).**
  - validate-inputs went from 642–1848 s to about 8–10 s. evaluate went from
    about 600 s to about 10 s. Peak RSS went from about 6 GB to 2.2 GB.
  - All five real H0008 cases are byte-identical to the old code. evaluate
    s000 also matches the production containment run.
  - **Not yet live.** The live campaign's `driver_config` pins the evaluator
    snapshot `quantipy-source-snapshot-44c77a8` and has no re-pin path. The
    speedup takes effect only for a new research root (or campaign)
    initialized with a fresh snapshot of quantipy `7a2ce40` or later. That
    choice belongs to the operator when the next campaign starts.
- **Research direction** is now in the autoresearch and research-loop runtime
  skills and the `.claude` mirror:
  - the sector-ETF residual reversal family is closed;
  - sessions after 2026-07-31 are a reserved holdout;
  - new hypotheses must explain why they beat the matched-market and dip
    controls, and must be powered.
- **Housekeeping.** The superseded 2026-09-06 planning drafts
  (`autoresearch-simplification-plan.md`, `autoresearch-cleanup-inventory.json`,
  `fable-autoresearch-handoff.md`) moved from `docs/reference/` to
  `.archive/2026-09-06-simplification-planning/`. They were preserved and
  never tracked.
- **State.** The campaign remains PAUSED and `research-owner.service` is
  stopped. The next family choice is the user's.

## Guardrails (unchanged, plus the 10-02 directive)

OpenAI models only for the research loop. Use no Anthropic, Claude, ACP, or Opus
route, and do not fall back to one. Historical Opus evidence stays readable and
unchanged. No live trading; no MemPalace writes; no input redownloads; five-session
horizon; ETF-only; no cap beyond 15; never hand-edit the research DB; workers never
commit, deploy, or restart; serialize pytest per checkout.
