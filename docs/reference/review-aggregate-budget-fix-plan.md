# Bounded aggregate review evidence budget

## Objective and rationale

H4-A003 has a valid submitted immutable source/proof tree whose materialized
review bundle is 74,873,523 bytes: source38,586,119, exact Git diff36,132,255,
bounded test evidence115,546 and small metadata. New synthetic negative fixtures
repeat content under distinct committed paths; source and diff also duplicate
bytes by contract. Omitting/deduplicating them would violate exact committed
source/diff validation. No reviewer reservation exists and the owner is paused.

Raise the one shared aggregate/generated-patch budget from64MiB to128MiB, a
bounded factor-of-two capacity increase that accommodates this proof without
relaxing individual source/test evidence limits, security checks or scientific
review. Do not add exceptions, deduplication, archive codecs, alternate formats,
or fallback paths. This supersedes only the old aggregate value, not prior
exact-root patch hashing and strict findings-schema fixes.

## Ownership and scope

Isolated G2 repair worktree baseline50e57fead618b30e96a640a931783ea7ce731caf.
Worker owns only gateway/research/review_evidence.py and
tests/gateway/research/test_review_evidence.py; this plan is read-only and may be
committed unchanged. You are not alone; preserve other work. No Quantipy/source
or proof edits, live state/artifacts/auth/services/config/runtime skill edits,
new dependencies/environments, memory writes, or deployment actions.

## Requirements

- Set existing MAX_BUNDLE_BYTES to128*1024*1024. Preserve its existing shared use
  for aggregate budget, generated root diff.patch, Git output and validation.
- MAX_BUNDLE_FILE_BYTES and MAX_TEST_EVIDENCE_BYTES stay8MiB. Non-root
  diff.patch namesakes stay ordinary files. Exact-limit acceptance/over-limit
  refusal, aggregate accounting of every byte, regular/no-symlink checks,
  digest inputs/order, source/diff equality, immutable existing-bundle replay,
  reservation/ACK/provenance and strict review verdict parsing stay unchanged.
- Add focused red regression coverage of the new default budget and unchanged
  per-file limits, plus useful scaled exact/over-aggregate boundaries as needed.
  Do not allocate huge test fixtures or duplicate existing coverage pointlessly.
- Root/broker separately validate the actual immutable74.9MB bundle twice using
  a private read-only SQLite snapshot/copy; worker must not access live state.
- No runtime instruction file currently pins this aggregate value; do not
  expand this task into configuration deployment or change historical plans.

## Sequence and verification

Read AGENTS and applicable backend Python/TDD guidance. Red test, minimal fix,
focused checks, then commit owned2files+unchangedplan. Use existing environment:

    PYTHONPATH=. /home/dev/repos/g2_openclaw/.venv/bin/pytest tests/gateway/research/test_review_evidence.py -q
    /home/dev/repos/g2_openclaw/.venv/bin/ruff check gateway/research/review_evidence.py tests/gateway/research/test_review_evidence.py
    /home/dev/repos/g2_openclaw/.venv/bin/ruff format --check gateway/research/review_evidence.py tests/gateway/research/test_review_evidence.py
    PYTHONPATH=. /home/dev/repos/g2_openclaw/.venv/bin/mypy gateway/research/review_evidence.py
    git diff --check

Do not run parallel pytest in this worktree. Report actualred/green/check output,
changedfiles, commit/diffSHA, risks, and DONE only complete. Independent native
Luna/root verification plus actual Opus review precede root ff-only integration
and owner restart. No config/gateway restart is required for this Python-only fix.
