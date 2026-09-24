# E — exact pinned doctor installation-detail schema

The current canonical publication ran the pinned embedded Codex0.153.4 doctor via OpenClaw2026.9.2. Its installation diagnostic contains `managed by Vite+` with the string value `false`. The repo's already-explicit expected managed-package diagnostic omits that key, so its exact-key validator rejects the report and publication rolls back. The expected managed-versus-global package-root difference is already modeled; do not change that policy or any runtime tuple.

Luna owns only `gateway/deployment/doctor.py` and `tests/gateway/test_openclaw_script_guarding.py` on G2 main. Preserve all other work and untracked historical documentation. Read applicable AGENTS and backend/OpenClaw config skill guidance.

Acceptance:

- Add the exact required key/value to the pinned installation-detail schema; do not accept arbitrary extra keys, truthy Vite management, missing required fields, alternate versions, or a second permissive schema.
- Update representative fixture(s), with regressions for the actual report shape, an incorrect Vite value, and a genuinely unknown extra field. Preserve all other publication/doctor checks and no-fallback behavior.
- Demonstrate the failing regression before the minimal fix. No global package upgrade/install, auth or runtime-state edit, live deployment, guard bypass, Git commit/staging, or unrelated refactor.

Verification:

```sh
uv run pytest tests/gateway/test_openclaw_script_guarding.py -k codex_doctor -q
uv run pytest tests/gateway/test_openclaw_script_guarding.py -q
uv run ruff check gateway/deployment/doctor.py tests/gateway/test_openclaw_script_guarding.py
uv run ruff format --check gateway/deployment/doctor.py tests/gateway/test_openclaw_script_guarding.py
uv run mypy gateway/deployment/doctor.py tests/gateway/test_openclaw_script_guarding.py
git diff --check
```

Report changed files, actual results, and remaining issues; DONE only if complete. Sol independently reviews; root verifies/commits/pushes and retries the guarded publication with `OPENCLAW_BIN=/home/dev/.local/bin/openclaw`.
