# Generated review patch size consistency repair

## Objective

Make review bundle hashing use the same existing generated-patch size bound as
bundle construction and validation. H0004-A001 has a valid immutable 10,188,380
byte root diff.patch inside a 23,603,827 byte bundle. Construction and validation
allow a generated patch up to the existing 64 MiB bundle cap, but hashing wrongly
uses the ordinary 8 MiB file cap. No scientific implementation or evidence changes.

## Ownership and scope

The implementation worker owns only gateway/research/review_evidence.py and
tests/gateway/research/test_review_evidence.py. This plan is read-only and may be
included unchanged in the repair commit. Work only in the isolated G2 worktree
based on 9cfc806e30742aea8bd1a7352784138205b8898c. Other agents are active; preserve
their work and all unrelated changes. Do not touch Quantipy, live research state,
review artifacts, auth, models, config, services, or runtime skills.

## Required behavior

- Only the exact root-relative path diff.patch uses MAX_BUNDLE_BYTES when read
  inside _bundle_digest. This matches the already-existing build and validation
  rules; do not introduce a larger cap or change constants.
- All other files, including source/diff.patch and nested namesakes, retain
  MAX_BUNDLE_FILE_BYTES. The separate test evidence limit remains unchanged.
- The existing cumulative MAX_BUNDLE_BYTES check remains enforced, counting the
  patch and every other file. No excluded bytes or skipped hashing.
- Preserve exact digest inputs/order, regular-file and no-symlink checks,
  immutability, committed-source/diff comparison, reservation/ACK semantics, and
  strict actual Opus model/verdict collection. No fallback or bypass.
- An already-published immutable bundle that failed only this inconsistent
  hashing limit must be revalidatable through the normal existing path without
  replacement, deletion, or mutation.

## Implementation sequence

1. Read the repository instructions and relevant Python/TDD skill references.
2. Add focused red tests demonstrating the mismatch, with scaled monkeypatched
   limits where practical to avoid large allocations. Cover a root generated
   patch above the ordinary limit but within aggregate budget, oversized patch,
   aggregate overflow, and ordinary/nested namesake file rejection. Include
   normal bundle construction/revalidation coverage, not only a helper test.
3. Apply the smallest exact-path fix. Do not refactor adjacent lifecycle code.
4. Run focused tests and proportional lint/type checks, then commit the two
   owned files and this unchanged plan. Return exact commit and diff hashes.
5. Independent root/Luna verification and actual Opus review precede integration.
   The root/broker will separately rehearse validation of the real immutable H4
   bundle using a private database copy; workers must not operate on live state.

## Verification

Use existing /home/dev/repos/g2_openclaw/.venv binaries with PYTHONPATH set to the
isolated worktree; never create an environment or install dependencies.

    PYTHONPATH=. /home/dev/repos/g2_openclaw/.venv/bin/pytest tests/gateway/research/test_review_evidence.py -q
    /home/dev/repos/g2_openclaw/.venv/bin/ruff check gateway/research/review_evidence.py tests/gateway/research/test_review_evidence.py
    /home/dev/repos/g2_openclaw/.venv/bin/ruff format --check gateway/research/review_evidence.py tests/gateway/research/test_review_evidence.py
    PYTHONPATH=. /home/dev/repos/g2_openclaw/.venv/bin/mypy gateway/research/review_evidence.py
    git diff --check

Do not run the broad environment-dependent script suite or parallel pytest in
this worktree. Record actual red/green outputs and any genuine environment issue;
do not claim unexecuted checks. Final report lists changed files, checks, risks,
commit/hash, and ends DONE only when the bounded implementation is complete.
