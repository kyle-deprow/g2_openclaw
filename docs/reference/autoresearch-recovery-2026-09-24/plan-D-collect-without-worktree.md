# Plan D — review collection must not depend on the disposable worktree, and a verified transcript verdict must supersede a host-verification-failure record

You are the implementation worker for the g2_openclaw repository (Python 3.13, uv, pytest, ruff, mypy strict).
You are NOT alone in the repository. Do NOT commit. Do NOT run `git add`, `git commit`, `git stash`, or touch git state.
Do NOT run `scripts/push-openclaw-config.sh`, `systemctl`, or any `gateway-cli` command against a real research root.
Do NOT modify any file outside the "Files you may change" list.

## Background (verified facts, do not re-derive)

`gateway/research/review_evidence.py::verify_review` reads the exact Claude transcript, parses the bare-JSON verdict, and THEN calls
`_validate_bundle(store, attempt_id, bundle_dir, expected_digest=reservation.bundle_sha256)`. `_validate_bundle` re-derives the expected
`source/` tree and `diff.patch` from the attempt's disposable git worktree (`attempt.worktree_path`) via `_tracked_source`/`_git_diff`.
In production the H0006-A001 worktree was deleted after the review completed; `_validate_bundle` raised a `BundleError` from git, and
`verify_review` converted that into `ReviewEvidenceError("bundle_mutated")` → `_failed_verification` → the store now durably holds
`review` = `{"verdict": "FAIL", "findings": ["bundle_mutated"], ...}` and `review_host_evidence` with `"reason": "bundle_mutated"`,
`"transcript_path": ""`, `"transcript_sha256": ""`. The bundle directory itself is intact: its `_bundle_digest` still equals the
reservation digest `e9d4a249…`, and its entries are exactly the allowed set. The real Opus verdict (FAIL with four findings) was parsed
successfully before the bundle step. `collect_review` currently replays any stored `review_host_evidence` unconditionally, so the wrong
record can never be corrected through the supported path. The attempt is in state REVIEW_FAILED, not CLOSED.

Trust model: same-user host metadata; detect wrong routing, stale identity, mutation, and model substitution; not hostile-root security.
Never retype a verdict: the ONLY source of a verdict/findings is the Claude transcript bound to the reservation and ACK.

## Deliverable

### 1. `gateway/research/review_evidence.py`

a. Add `_verify_reserved_bundle(root: Path, expected_digest: str) -> str`: require an absolute, non-symlink, immutable directory
   (`_require_immutable`), the exact allowed top-level entry set used by `_validate_bundle` (factor that set into a module constant
   `_BUNDLE_ENTRIES` shared by both functions), no symlinks anywhere under it, every regular file immutable, and
   `_bundle_digest(root) == expected_digest`. It must not touch git or the worktree. Raise `BundleError` with a specific message per
   failure; use the message "reserved review bundle was modified" for the digest mismatch.
b. In `verify_review`, replace the `_validate_bundle(...)` call with `_verify_reserved_bundle(Path(reservation.bundle_dir),
   reservation.bundle_sha256)`. Map a digest mismatch to `ReviewEvidenceError("bundle_mutated")` and any other `BundleError` to
   `ReviewEvidenceError(f"bundle_invalid: {exc}")`.
c. `_failed_verification`: add keyword-only optional parameters `transcript_path: str = ""`, `transcript_sha256: str = ""`,
   `verdict_json: str = ""`, `assistant_events: int = 0`, and fill the corresponding host fields from them. In `verify_review`, when the
   transcript was already read (and the verdict possibly parsed) before a `ReviewEvidenceError`, pass what is available so a failed
   verification still binds the transcript bytes it saw. Also add `"task_source": task.source if task else "unresolved"` to the failed
   host payload (the passing path already records `task_source`).
d. `collect_review`: when stored `review_host_evidence` exists, parse it. If it has a non-empty `"reason"` key (a host-verification
   failure) AND the current attempt state is `REVIEW_FAILED` (not CLOSED), run `verify_review` again. If the new verification's host
   payload has NO `"reason"` key (a transcript-verified verdict), call `store.supersede_failed_review(attempt_id, verification.review,
   verification.host_payload)` and return its result. If the new verification is itself a failure (has `"reason"`), do not change
   anything: return the current attempt (idempotent replay, no new evidence rows, no new events). If the stored host evidence has no
   `"reason"`, keep today's replay behavior exactly.

### 2. `gateway/research/store.py`

Add `supersede_failed_review(self, attempt_id: str, record: ReviewEvidence, host_payload: str) -> Attempt`:
- Same validation as `collect_review_evidence` (binding equality between host payload and record; record matches attempt commit and
  frozen spec digest).
- Inside one `BEGIN IMMEDIATE` transaction: load the attempt row; require state `REVIEW_FAILED` (StoreConflict otherwise); load stored
  `review` and `review_host_evidence` rows; require the stored host payload to contain a non-empty `"reason"` (StoreConflict "stored
  review is transcript-verified and cannot be superseded"); require the new host payload to contain no `"reason"` key, a non-empty
  `transcript_sha256`, and `acp_session_uuid` equal to the stored one (StoreConflict otherwise); require that no
  `review_superseded` / `review_host_evidence_superseded` rows exist yet (StoreConflict "review evidence was already superseded once").
  Then insert the OLD payloads under kinds `review_superseded` and `review_host_evidence_superseded` (with their digests), UPDATE the
  `review` and `review_host_evidence` rows to the new payloads/digests, apply `submit_review(current, record, hypothesis.spec_sha256,
  now_utc())` to compute the new attempt state/verdict fields, UPDATE the attempts row exactly as `collect_review_evidence` does, and
  record ONE event `review_recollected` with detail `{"verdict": record.verdict, "superseded_reason": <old reason>,
  "superseded_review_sha256": <old digest>, "transcript_sha256": <new>}` (actor "driver"). Commit.
- After commit, rewrite the `review.json` and `review_host_evidence.json` projections and write `review_superseded.json` and
  `review_host_evidence_superseded.json` projections next to them (use the existing `_projection` helper).
- Check the `attempt_evidence` table's primary key/uniqueness in `initialize()` before writing and use whatever the schema requires;
  do not change the schema.

### 3. Tests
- `tests/gateway/research/test_review_evidence.py`:
  (a) reproduce the production scenario end to end: reserve + ACK, build host fixture with a PASS or FAIL transcript, then DELETE the
      attempt worktree directory (`shutil.rmtree`) and ALSO delete the `task_runs` row leaving a matching `subagent_runs` row (reuse the
      fixture helpers added earlier in this file); `collect_review` must succeed with the transcript's verdict and findings, and the host
      payload must have `task_source == "subagent_runs"` and no `reason`.
  (b) a bundle whose bytes were changed after reservation (chmod +w, edit `instructions.md`, chmod -w) → `collect_review` records FAIL with
      findings `("bundle_mutated",)` and host `reason == "bundle_mutated"`, `transcript_sha256` non-empty.
  (c) supersession: first produce a failed-verification record (e.g. monkeypatch `_verify_reserved_bundle` to raise `BundleError("x")`
      → findings `("bundle_invalid: x",)`), then remove the monkeypatch and call `collect_review` again → attempt now carries the
      transcript verdict/findings, `review_superseded` and `review_host_evidence_superseded` evidence rows and projections exist, exactly
      one `review_recollected` event; calling `collect_review` a third time is a no-op replay (no new events).
  (d) a transcript-verified record is never superseded: after (a), monkeypatch to force a failure and call `collect_review` → attempt
      unchanged, no new events. (e) `supersede_failed_review` raises StoreConflict on CLOSED attempts, on a stored verified record, on
      acp_session_uuid mismatch, and on a second supersession.
- `tests/gateway/research/test_store.py`: unit tests for `supersede_failed_review` preconditions listed in (e).

## Files you may change
- gateway/research/review_evidence.py
- gateway/research/store.py (only the new method and, if needed, a tiny shared helper it extracts from `collect_review_evidence`)
- tests/gateway/research/test_review_evidence.py
- tests/gateway/research/test_store.py

## Files you must not change
- Everything else, including host_records.py, cli.py, wake.py, contracts.py, containment.py, worker.py, any agent_config or skill file,
  and anything under /home/dev/repos/quantipy.

## Verification (run these yourself and paste real output)
```
uv run pytest tests/gateway/research -q
uv run ruff check gateway tests
uv run ruff format --check gateway tests
uv run mypy gateway tests
```
Tests that launch real bubblewrap/systemd-run stages may fail inside your sandbox with `containment_unavailable` / `Operation not
permitted`; report exactly which and do not "fix" them. Everything else must pass.

## Completion report
List changed files, the exact verification output (last 8 lines of each command), sandbox-environmental failures by name, and
unresolved issues. Reply with the single line `PLAN_D_DONE` as the very last line only if every acceptance criterion above is met.
