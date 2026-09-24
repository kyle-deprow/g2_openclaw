# D round 3 — atomic migration and direct guard coverage

Read the parent execution plan, plan-D-collect-without-worktree.md, and fix-D-round2.md in this directory. Existing ownership and guardrails remain. This round owns only `gateway/research/store.py` and `tests/gateway/research/test_store.py` in the fixD worktree; preserve all other pending changes.

Independent Sol review found:

1. Blocking: initialize's old-trigger detection and DROP/CREATE run without an explicit SQLite transaction. Make detection and replacement atomic under an explicit migration write lock/transaction; no concurrent writer may observe an unprotected evidence table. Preserve initialization compatibility, rollback behavior, and existing connection/transaction boundaries. Add a regression that exercises the atomicity guarantee rather than only inspecting the SQL string.
2. The transcript-verified-record rejection test currently uses PASS and trips the state guard. Use a transcript-verified FAIL so it exercises the no-reason protection.
3. The second-supersession test likewise uses PASS and does not reach its intended guard. Exercise FAIL→FAIL and the required already-superseded rejection. Order explicit precondition checks consistently so the documented one-time refusal is demonstrable without weakening transcript-verification protection.
4. The key-change test changes attempt_id to a nonexistent row and can pass through a foreign-key failure. Use a second valid attempt identity and assert the immutability-trigger error, proving that key changes remain forbidden.

Write the failing regression first, implement the minimum fix, and run:

```sh
uv run pytest tests/gateway/research -q
uv run ruff check gateway tests
uv run ruff format --check gateway tests
uv run mypy gateway tests
git diff --check
```

No commits, Git staging, live database mutations, deployments, unrelated refactors, schema weakening, provider changes, or new dependencies. Preserve the exact prior review archive and one-time supersession semantics. Return changed files, actual check results, unresolved risks, and DONE only when complete. Sol reviews this round before integration.
