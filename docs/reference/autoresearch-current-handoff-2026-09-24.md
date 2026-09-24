# Autoresearch handoff — state as of 2026-09-24

Supersedes `autoresearch-current-handoff-2026-09-22.md`. That document's recovery
sequence is still the plan; this one records what was done against it, what was
found to be wrong in it, and exactly what remains. No historical run has happened.
Do not claim a trading result.

## One-paragraph status

Phase 1 (reconcile A001) is mechanically done but carries a wrong record: the real
Opus review is collected onto H0006-A001, but as a synthetic `FAIL["bundle_mutated"]`
produced by a collector defect, not as the reviewer's four findings. Phase 2 (repair
the shared loop) is four Luna/Opus rounds in: two are committed on `main`, two are
implemented in worktrees and awaiting a final Opus verdict and merge. The campaign
inputs that the 09-22 handoff said were pinned on disk had actually vanished; they
have been rebuilt byte-exactly against the frozen digests. Phase 3 (cap 14), Phase 4
(A002), and Phase 5 (real loop) have not started. The owner service is stopped, the
campaign is PAUSED at resume sequence 33, the gateway is healthy.

## Authoritative current state

| Surface | State |
|---|---|
| Campaign | `PAUSED`, resume_seq 33, attempt_cap 13 (all 13 consumed) |
| H0006 | `FROZEN` |
| H0006-A001 | `REVIEW_FAILED`, review_verdict `FAIL`, reviewer `claude-opus-5`; findings currently `["bundle_mutated"]` (wrong, see below) |
| research-owner.service | inactive |
| openclaw-gateway.service | active; `openclaw gateway health` OK; codex 2026.9.2 + acpx 2026.8.1 enabled; `research-orchestrator` route resolves to `openai/gpt-6-astra` |
| Quantipy API | running on `http://localhost:8000` (needed to re-export inputs; not needed by the loop) |
| G2 main | `4572564` (two new commits on top of `e4ce0c1`) |
| Quantipy main | `44c77a8` (unchanged) |
| Campaign root `/home/dev/autoresearch-reversal-20260911.9AbPOA/` | RESTORED 2026-09-22, all digests verified, runtime pins PASS; see `RESTORATION-2026-09-22.md` inside it |

## What was done

### Committed on `main`

1. `7bb4b20` — review collection recovers from a pruned OpenClaw `task_runs` ledger by
   falling back (zero-row case only) to the surviving `subagent_runs` row, with the same
   identity/ordering/ACK checks and a `task_source` marker in host evidence; the acpx
   record reader accepts the installed acpx 2026.8.1 shape (`config_options[id=effort]`)
   and drops the stale `desired_config_options` assumption. Opus-reviewed READY.
2. `4572564` — `update_wake_status` records `owner_turn_status`/`owner_turn_failed`
   events only on transitions (production had 3,936 one-per-minute timeout events);
   `poll_owner_turn` converts transport / uncertain errors into `OwnerPollUnavailable`;
   `serve` tolerates 30 consecutive unavailable polls before the fatal pause path.
   Opus-reviewed READY.

### Implemented, verified, NOT yet merged (worktrees under `/home/dev/repos/g2_openclaw-worktrees/`)

3. **fixD** — worktree-free bundle verification + supersession of a synthetic FAIL.
   `verify_review` no longer re-derives the bundle from the deleted attempt worktree; it
   checks immutability, allowed entries, and the reservation digest. `collect_review`
   re-verifies when the stored host evidence is a host-verification failure and, if the
   new verification is transcript-verified, calls new `store.supersede_failed_review`
   (old payloads kept as `review_superseded` / `review_host_evidence_superseded`, one
   `review_recollected` event, exactly one supersession allowed, never supersedes a
   transcript-verified record). Round 1 Opus: ISSUES (runtime DROP TRIGGER). Round 2
   implemented the arbitration: a declared, migrated trigger exception in
   `initialize()`. Round 2 verified outside the sandbox: 556 passed, ruff/mypy clean.
   **Needs: Opus re-review of round 2 (resume agent `review-D-round2` context is gone;
   spawn fresh with diff), then merge + commit.**
4. **fixC** — in-sandbox import provenance. New `gateway/research/containment_provenance/sitecustomize.py`
   (stdlib-only recorder, ro-bound at `/provenance`, first on PYTHONPATH, writes
   `research-containment-provenance-v1` records to `/stage/provenance/`); `bwrap_argv`
   requires `provenance_dir=` for targets/evaluate/analysis; new
   `gateway/research/provenance.py` verifies records against the pinned snapshot bytes
   and `git cat-file blob <commit>:<rel>`; worker fails runs with `provenance_invalid`;
   `implementation-submit` gains a REQUIRED `--provenance-evidence` index validated
   before the store transition and carried into the review bundle as
   `containment-provenance.json`; wake message and three runtime skill docs updated.
   Round 1 Opus: ISSUES (defaulted kwarg fallback, two `pytest.skip` escapes, no
   real-sandbox assertion). Round 2 fixed those; parent verification under real bwrap
   showed the record IS written inside containment, but `flags.safe_path` is False for
   target stages because target argv is implementer-authored without `-P -s` (A001's
   real argv had none). **Round 3 dispatched 2026-09-24** to scope interpreter-flag
   enforcement to worker-built stages (evaluate/analysis) and report flags for targets.
   Log: scratchpad `runs/lunaC3.log`, sentinel `PLAN_C_ROUND3_DONE` in `runs/lunaC3.out.txt`.
   **Needs: parent verification of round 3 (the real-bwrap test must pass), Opus
   re-review, merge + commit.**

Merge order: D first (touches `review_evidence.py`/`store.py` in different hunks than
C), then C. Apply with `git apply` of a `git diff` snapshot from each worktree onto
`main`, re-run `uv run pytest tests/gateway/research -q`, `ruff`, `mypy`, commit.

### Campaign inputs restored

`/home/dev/autoresearch-reversal-20260911.9AbPOA/` was entirely missing on 2026-09-22.
Every pinned file was rebuilt without external downloads and verified against the
frozen SHA-256s: source snapshot (git archive recipe + two operator root files
recovered from the 2026-09-11 codex rollout), panel parquet (re-export from the local
API cache, coverage digest unchanged), receipt (rebuilt from the receipt object in the
admission decision via the pinned pydantic model), dividends (local API), universe and
ledger (bytes recovered from the session log). `verify_configured_runtime_pins` passes.
`ASTRA-BOOTSTRAP.md` and the operator notes were not restored (not read by any code).

### Evidence written for Astra

`/home/dev/.openclaw/workspace-research-orchestrator/campaigns/conditional-reversal-20260911/H0006/H0006-A001-opus-findings.json`
holds the exact four Opus findings (parsed from the bound transcript) and the A002
evidence guidance from the 09-22 handoff.

## Corrections to the 09-22 handoff

- "Pinned inputs remain under …9AbPOA" was false; see above.
- The 09-15 "host correlation" failure had two causes the handoff did not name: the
  `task_runs` row was pruned by OpenClaw, and the acpx record shape did not match the
  reader. A third defect (bundle validation needing the deleted worktree) only surfaced
  once the first two were fixed and produced the wrong `bundle_mutated` record.
- The A001 transcript (`/home/dev/.claude/projects/-home-dev--openclaw-research-v2-review-bundles-H0006-A001/97cfcfe5-d14c-407d-b632-85113ec833e4.jsonl`)
  is fully valid: 70 assistant events, all `claude-opus-5`/`high`, bare-JSON FAIL with
  four findings bound to the reservation.

## What remains, in order

1. Finish C round 3: wait for the sentinel, run the four verification commands in
   `fixC` outside the codex sandbox (the real-bwrap worker tests must pass), spawn a
   fresh Opus `Plan`-type review with the round-3 diff and the plan files
   (`scratchpad/plans/plan-C-provenance.md`, `fix-C-round2.md`, `fix-C-round3.md`).
2. Opus re-review of D round 2 (`fixD`, plan files `plan-D-collect-without-worktree.md`
   and `fix-D-round2.md`), focusing on the migrated trigger and the FAIL→FAIL test.
3. Merge D then C onto `main`, run full research tests + ruff + mypy, commit each.
4. Deploy: `bash scripts/push-openclaw-config.sh`, `systemctl --user restart openclaw-gateway.service`,
   `openclaw config validate`, `openclaw gateway health`. (Only the runtime skill docs
   changed; the driver code is used by `research-owner.service` directly from the repo venv.)
5. Re-run collection so the real verdict supersedes the synthetic one:
   `.venv/bin/gateway-cli research review-collect H0006-A001 --root /home/dev/.openclaw/research-v2 --core-database /home/dev/.openclaw/state/openclaw.sqlite --acpx-sessions /home/dev/.openclaw/workspace/state/sessions --claude-projects /home/dev/.claude/projects`
   Expect `REVIEW_FAILED`, `review.json` with four findings, one `review_recollected`
   event, and `review_superseded.json` next to it. Then close A001:
   `gateway-cli research attempt-close H0006-A001 --root … --decision RETRY --reason "Opus FAIL: in-sandbox provenance, host-only hashes, prior-blocked retention, source-equivalence ambiguity"`.
6. Phase 3 authorization (the user directed "get us through a full successful loop";
   treat as the operator authorization for exactly one more admission):
   `gateway-cli research campaign-policy-set --root … --attempt-cap 14 --operator-reference "User directive 2026-09-22: one clean H0006-A002 admission (13→14); no automatic further extension"`.
7. Relaunch: `gateway-cli research resume --root … --reason "..."` then
   `systemctl --user start research-owner.service`; watch
   `journalctl --user -u research-owner.service -f` and the `events` table. The wake for
   `IMPLEMENTED` state now tells Astra to submit with `--provenance-evidence`; A002's
   synthetic harness must pass `provenance_dir=` to `stage_plan` for every
   targets/evaluate/analysis stage and retain the record dirs inside the committed worktree.
8. Monitor with a cheap Luna `codex exec` read-only watcher (low effort) on stage
   transitions; escalate only on `campaign_paused`, `review_unresolved`, `provenance_invalid`,
   `serve_failed`, or a lost job.
9. After actual Opus PASS: Luna runs the historical job; Astra decides; preserve a
   negative result. Definition of done is unchanged from the 09-22 handoff.

## Guardrails still in force

No live trading; no provider fallbacks; no MemPalace writes; no input redownloads (the
restoration recipes are in `RESTORATION-2026-09-22.md` and the memory file
`reversal-campaign-inputs-restoration.md`); five-session horizon; ETF-only; no automatic
cap extension beyond the one explicit 13→14; never a second Opus review for A001; never
hand-edit the research database (supersession goes through `supersede_failed_review`).

## Where the working artifacts are

- Scratchpad: `/tmp/claude-1000/-home-dev-repos-g2-openclaw/cbe077ea-5ff4-410d-b1e4-cd949523fccf/scratchpad/`
  (`plans/`, `runs/` with Luna logs and `-o` outputs, `roundA1..D1.diff`; tmpfs, may not survive reboot)
- Luna session ids: C `01a0ca9e-7791-7972-9e04-be25599e77d0`, D `01a0caa1-6571-73d1-aad1-3a583a809782`
  (resume with `codex exec -s workspace-write resume <id> "..."` from the worktree).
- Worktrees: `fixA`, `fixB` (merged, can be removed with `git worktree remove`), `fixC`, `fixD`.
- Memory notes: `reversal-campaign-inputs-restoration.md`, `review-collection-host-record-pitfalls.md`.
