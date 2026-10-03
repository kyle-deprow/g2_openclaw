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

## Guardrails (unchanged, plus the 10-02 directive)

OpenAI models only for the research loop. Use no Anthropic, Claude, ACP, or Opus
route, and do not fall back to one. Historical Opus evidence stays readable and
unchanged. No live trading; no MemPalace writes; no input redownloads; five-session
horizon; ETF-only; no cap beyond 15; never hand-edit the research DB; workers never
commit, deploy, or restart; serialize pytest per checkout.
