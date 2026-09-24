# C round 4 — integrate after D without invalidating reserved evidence

Read the execution plan and all C phase/fix plans. Independent Sol review accepted C's provenance behavior but found that its new-bundle schema would reject the already-reserved H0006-A001 bundle during canonical recollection. Root will first land independently accepted D, then mechanically apply C onto main. This round resolves that integration; it must not edit frozen research records or manufacture a verdict.

## Ownership

After root's explicit dispatch on the integrated main checkout, Luna owns only:

- `gateway/research/review_evidence.py`
- `gateway/research/store.py` (only overlapping D/C integration, preserving both reviewed behaviors)
- `tests/gateway/research/test_review_evidence.py`
- `tests/gateway/research/test_store.py` (only overlapping D/C fixture/integration changes)

Do not change other files or Git state. Preserve all existing dirty documentation and the remainder of the mechanically applied C diff.

## Requirements

- Preserve D's worktree-independent immutable reserved-bundle validation, transcript correlation, atomic migration, and one-time supersession.
- New implementation submissions and new bundle creation/reservation must still require validated containment provenance and the exact new bundle schema. Missing or malformed provenance is refused, never synthesized/defaulted.
- Recover historical already-reserved evidence according to its canonical stored evidence and exact reserved digest. A001 has no containment-provenance evidence row/file; its existing immutable reservation must remain verifiable without a disposable worktree. Distinguish this explicit historical-evidence case from new submissions. Do not add a runtime/provider fallback, relax immutability/digest/identity checks, accept arbitrary bundle entries, or retry alternate schemas after validation failure.
- Add an integrated regression covering a pre-provenance reserved bundle with its original digest, absent worktree, and host-failure supersession to the exact transcript's four-finding FAIL. Verify old evidence retained once and idempotent replay. Do not run another external review.
- Add/retain negative coverage: new bundle without provenance refused; reserved bundle mutated or with unexpected files refused; new provenance-bearing reserved bundle verifies after worktree removal.
- No actual live store mutation during tests. Root later runs the canonical CLI against the real records.

## Verification

```sh
uv run pytest tests/gateway/research -q
uv run ruff check gateway tests
uv run ruff format --check gateway tests
uv run mypy gateway tests
git diff --check
```

No test skips, new dependencies, external writes, commits, pushes, deployment, scientific changes, cap changes, or source promotion. Report changed files, actual results, remaining concerns, and DONE only on completion. Independent Sol reviews the integrated C diff afterward.
