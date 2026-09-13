# Safe retry after an implementation cannot enter review

## Objective

H5-A001 is immutable IMPLEMENTED, but its review bundle was refused before
reservation because a source artifact exceeds the existing 8 MiB limit. No
review reservation, ACK, verdict, job or run exists. Existing close_attempt
rejects IMPLEMENTED and thereby prevents any bounded retry. Fix this lifecycle
gap without changing evidence limits, frozen research, review/run gates or APIs.

Baseline b2adf171be575de0390055a14031f124285b029f in the isolated G2 repair
worktree. Campaign PAUSED21 and owner stopped; all tasks terminal. Worker must
not access or mutate live state, artifacts, services, auth or Quantipy science.
You are not alone; preserve other changes. This plan is read-only and may be
committed unchanged. No new dependencies, fallback, CLI, schema or migration.

## Owned files

- gateway/research/machine.py
- gateway/research/store.py
- tests/gateway/research/test_machine.py
- tests/gateway/research/test_store.py
- tests/gateway/research/test_review_evidence.py only if needed for race/replay coverage

## Required behavior

1. Extend the existing close operation only for IMPLEMENTED + RETRY. Continue
   refusing IMPLEMENTED + FINISH/PAUSE and all other currently invalid states.
   Existing reviewed/run-terminal closure behavior remains unchanged. Pure
   machine transition enforces decision/state; store owns evidence/job guards.
2. Store pre-review RETRY closure must run in one BEGIN IMMEDIATE transaction,
   rereading canonical state there. Require exactly IMPLEMENTED, null run_job_id,
   no jobs row for this attempt (any status), no review lifecycle evidence
   (reservation, ACK, verdict/host evidence, cancel), and no dispatch-indicating
   review lifecycle event suggesting an unknown in-flight review. Refuse rather
   than guess if those guards conflict. Do not query external runtime from store.
3. Preserve commit, implementation/run-plan/admission evidence, frozen hypothesis,
   all files and earlier refusal events. Update only normal closure fields and
   append the existing attempt_closed event, actor astra, decision RETRY/reason.
   Do not pause/resume campaign or auto-open another attempt as a side effect.
4. Close versus reserve must be serialized. In insert_review_evidence, require
   current state IMPLEMENTED inside the insertion transaction before a NEW
   review_reservation is inserted; refuse new lifecycle evidence on CLOSED.
   Preserve exact existing-payload idempotent replay/projection repair semantics
   even after later lifecycle transitions. No changes to successful collection.
   Reserve wins: close sees evidence and refuses. Close wins: reserve insertion
   refuses. A losing build may leave an immutable bundle; never delete it.
5. A2 can open normally after guarded A1 closure, subject to unchanged per-
   hypothesis and global admission caps. No unreviewed run or false success.
6. Keep the patch minimal; no wholesale store refactor or generic abort framework.

## Tests and sequence

Read AGENTS, backend-python/TDD and fail-fast guidance in full. Write focused
red tests, implement smallest fix, run focused green tests serially. Cover:

- IMPLEMENTED RETRY positive; FINISH/PAUSE and other state rejections remain.
- Store preserves immutable evidence/commit and records exactly the normal close.
- Any reservation/review lifecycle evidence, job row or run_job_id blocks retry.
- Both race orderings and new reservation after CLOSED refuse safely.
- Exact old lifecycle-payload replay/projection repair still works.
- Next attempt can open; frozen attempt/campaign caps remain enforced.
- Existing review PASS requirement and per-file/aggregate limits remain unchanged.

Use existing environment, no parallel pytest in the worktree:

    PYTHONPATH=. /home/dev/repos/g2_openclaw/.venv/bin/pytest tests/gateway/research/test_machine.py tests/gateway/research/test_store.py tests/gateway/research/test_review_evidence.py -q
    /home/dev/repos/g2_openclaw/.venv/bin/ruff check gateway/research/machine.py gateway/research/store.py tests/gateway/research/test_machine.py tests/gateway/research/test_store.py tests/gateway/research/test_review_evidence.py
    /home/dev/repos/g2_openclaw/.venv/bin/ruff format --check gateway/research/machine.py gateway/research/store.py tests/gateway/research/test_machine.py tests/gateway/research/test_store.py tests/gateway/research/test_review_evidence.py
    PYTHONPATH=. /home/dev/repos/g2_openclaw/.venv/bin/mypy gateway/research/machine.py gateway/research/store.py
    git diff --check

Commit owned changes plus unchanged plan. Report red/green/check results, files,
commit/diff SHA, risks and DONE. Root independently runs the research suite;
separate Luna and actual Opus review precede ff-only integration. A private
SQLite backup may rehearse actual H5A1 closure without touching live records.
Root then resumes normal Astra closure/retry. Python-only: no config push or
gateway restart is needed, and no live closure is performed by workers.
