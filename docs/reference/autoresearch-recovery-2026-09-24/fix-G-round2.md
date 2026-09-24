# G round 2 — preserve readiness guard coverage

Sol found native source semantics correct, but required safety regressions were lost when replacing the auth-sync suite. Use the same bounded G file ownership; preserve all other work. No runtime changes, commits, or deployment.

- Add missing-local/no-creation, symlinked/directory/corrupt local database cases and parameterized wrong shared/local schema role/version/app-version/agent identity cases. Verify rejection without writes.
- In success and incompatible-local-override cases, snapshot shared and all local DBs plus relevant existing profile/state artifacts, then assert byte-for-byte preservation. Do not expose credential material in logs.
- Remove the now-unused `openclaw_bin` positional argument throughout readiness caller/parser/tests, with no compatibility shim.
- Print overall readiness success only after all managed agents pass; a later local failure must not first claim success.
- Run focused readiness tests, the affected publication tests, Ruff/format/mypy/bash syntax/diff checks. Root's first-round tests are complete; no concurrent pytest.

Sol independently reviews the final diff. Existing native OAuth eligibility (access OR refresh, expiry not equivalent to unusability) remains unchanged; this round must not expand into an auth system rewrite.
