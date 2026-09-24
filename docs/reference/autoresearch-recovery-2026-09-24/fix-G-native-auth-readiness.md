# G — native shared-auth readiness; remove credential copying

Deployment now passes the pinned doctor and real sandbox command probe, but its obsolete auth synchronization requires local main-agent OAuth rows. Installed OpenClaw 2026.9.2 reports its real shared credential store at `state/openclaw.sqlite`; the local main-agent override store is legitimately empty. Official model-status metadata reports OpenAI OAuth profiles, no unusable profiles, and no missing provider. A new login is not indicated.

Luna owns these bounded files on main, preserving all unrelated work:

- Replace `gateway/deployment/auth_sync.py` with `gateway/deployment/auth_readiness.py` (no compatibility shim).
- Replace `tests/gateway/deployment/test_auth_sync.py` with the matching readiness test module.
- `scripts/push-openclaw-config.sh` caller and directly related auth comments.
- `tests/gateway/test_openclaw_script_guarding.py` relevant fixtures/assertions only.

Before implementation, establish installed official shared-store/read-through/local-override semantics from exact 2026.9.2 sources and record source paths in the completion report. Do not invent auth semantics from a database location alone.

Requirements:

1. Remove all credential-row deletion/copy/ATTACH writes, auth JSON copying, and chmod mutations from this deployment check. No main-store fallback, portable credential duplication, login, refresh, migration, auth dump, or hand-built native schema. Preserve existing shared/local auth state unchanged.
2. Validate native shared source and each configured OpenAI agent using read-only/query-only access and existing strict regular-file/ownership/path-chain/native schema guards. Check the actual installed shared-store shape; do not accept arbitrary JSON/provider substring matches. Expose only minimal readiness metadata, never tokens/profile values. Preserve path and native agent ownership protections. No DB creation on missing paths.
3. Respect native local-override/read-through semantics; do not silently accept an incompatible local override. Profile expiry alone is not proof that refreshable OAuth is unusable. Use installed official behavior to define bounded readiness and surface uncertainty accurately; actual inference remains a separate live gate.
4. Regression tests cover legitimate shared credentials with empty local stores, missing/malformed/incompatible shared state, wrong provider/type, unsafe paths/ownership/schema, and non-mutation of pre-existing local profiles. Test that no retired synchronization/fallback is invoked. Keep all other publication gates and transactional rollback intact.
5. Run TDD, focused readiness tests and affected shell-guard tests (serialize pytest), Ruff/format/mypy/bash syntax/diff checks. Do not repeat the full slow shell suite unless necessary. Report exact tests and source evidence.

No live runtime/auth modifications, installation, deployment, Git operations, science edits, or campaign changes by workers. Sol reviews independently; root independently verifies, commits/pushes, and retries guarded publication with canonical OpenClaw and project virtualenv on PATH. A001 recovery and cap14 are already complete; campaign remains paused.
