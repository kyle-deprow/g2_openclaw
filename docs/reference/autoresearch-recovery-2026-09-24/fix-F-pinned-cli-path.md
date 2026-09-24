# F — bind deployment probes to the verified runtime package

After E, publication passes doctor but rolls back before the command-contract probe: the probe requires an obsolete nested dependency directory. The exact-pinned OpenClaw 2026.9.2 plugin legitimately resolves Codex 0.153.4 from its managed npm project's hoisted dependency. The existing runtime verifier already validates this identity. Do not install, upgrade, change providers, accept arbitrary CLI paths, or bypass probes.

Luna owns these files on main (preserve other work):

- `gateway/deployment/command_probe.py`
- `gateway/deployment/doctor.py`
- `scripts/push-openclaw-config.sh`
- `tests/gateway/deployment/test_command_probe.py`
- `tests/gateway/test_openclaw_script_guarding.py`

Requirements:

1. Thread the package root returned by the existing exact runtime verifier into the command probe; require the CLI to be exactly that root's `bin/codex.js`, with the existing existence and strict version/package identity checks intact. No permissive alternate suffix or independently guessed root.
2. Remove the same obsolete suffix assumption in doctor's update diagnostic; bind the reported running root to the already verified app-server root instead. Preserve exact schemas, versions, failure classifications, and all other checks.
3. Test the real hoisted layout, incorrect CLI/root authority binding, missing CLI, and update diagnostic outside the verified root. Keep the sandbox/status probe fully active and read-only.
4. TDD: demonstrate relevant regression before fix. Run focused command-probe and affected doctor/script tests, Ruff/format/mypy, and diff check. Avoid running the full slow shell suite unnecessarily or concurrently.

No live mutation/deployment, commits, auth changes, scientific changes, or runtime installation by workers. Sol independently reviews; root independently verifies the bounded change, commits/pushes, then retries guarded deployment with the canonical `OPENCLAW_BIN` override. Report exact tests and concerns before claiming DONE.
