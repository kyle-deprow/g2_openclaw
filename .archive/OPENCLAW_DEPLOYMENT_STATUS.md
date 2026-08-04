# OpenClaw Deployment Status

**Checkpoint:** 2026-08-01
**Repository:** `g2_openclaw`
**Branch:** `main`
**Source revision:** `0af7f03` (`origin/main`)

## Current Status

The source repository is clean and the deployment hardening work is committed and pushed. The live deployment is **not complete**. The last deployment command was interrupted before it could publish and verify the managed OpenClaw runtime, so the transaction rolled back.

Verified at this checkpoint:

- `main` matches `origin/main`.
- The latest test suite passed: `172 passed, 1 skipped`.
- Ruff, formatting, mypy, shell syntax checks, `git diff --check`, secret scanning, and commit hooks passed before the last commit.
- No `push-openclaw-config`, OpenClaw gateway, or autoresearch process is running.
- `openclaw-gateway.service` is inactive and disabled.
- The autoresearch supervisor is inactive and disabled.
- The live OpenClaw config validator reports one stale configuration key: `plugins.entries.codex.config.nativeToolSurfaceEnabled`.
- The autoresearch PM Codex state database contains `305` thread rows and `294` missing rollout files. It currently has no orphan spawn edges or orphan job assignments.

The missing rollout files and stale live configuration are runtime state, not uncommitted repository changes. They must be repaired by the guarded deployment procedure before the gateway or autoresearch loop is started.

## Change History

### `e12a8df` - Harden OpenClaw deployment transactions

- Added strict temporary-directory, ownership, path, and artifact checks.
- Added atomic publication and rollback of managed OpenClaw configuration.
- Pinned the OpenClaw/Codex runtime tuple used by the deployment.
- Made OpenClaw OAuth state the owned authentication path and rejected unexpected Codex `auth.json` state.
- Removed silent provider fallback behavior.

### `6073946` - Repair Codex deployment validation

- Tightened doctor validation for configuration, MCP, sandbox, runtime, and provider ownership.
- Made unexpected diagnostics fail closed instead of being ignored.

### `1a0cd20` - Repair stale Codex runtime state

- Added controlled repair for stale rollout references in the Codex state database.
- Added validation for rollout paths, database ownership, schema, and link safety.
- Rejected mixed valid/stale graph conditions that cannot be repaired deterministically.

### `9f715ef` - Tolerate Codex update probe timeouts

- Accepted only the known, exact update-probe DNS timeout signature.
- Continued to reject unrelated network, package, or runtime failures.

### `0af7f03` - Validate Codex state cleanup integrity

- Added foreign-key checks before and after state repair.
- Added row-count and transaction-rollback verification.
- Added cleanup and validation for stale-to-stale spawn edges.
- Preserved fail-closed behavior for orphan references and other unexpected corruption.

## What Is Not Complete

The code is ready for another guarded deployment attempt, but the machine is not yet at the desired operational state. In particular:

1. The live config must be replaced with the current repo-managed overlay so the stale `nativeToolSurfaceEnabled` key is removed.
2. The Codex state repair must complete successfully and leave all retained thread rollout paths present.
3. OpenClaw doctor, config validation, the update probe, and the authenticated endpoint probe must pass their intended checks.
4. The gateway service must be restarted, enabled, and verified healthy.
5. G2 must be the user-facing launch path for the PM session; no background agent should independently interact with the G2 app.
6. The autoresearch loop must be started only after the gateway and PM session are healthy, then observed through at least two complete implementation-capable iterations. A `no_consensus` round is not a completed loop.

Quantipy was not modified by this deployment-hardening phase. No claim is made here that its current experiments, data coverage, or strategy results have changed.

## Ideal End State

The desired end state is a reproducible, fail-closed runtime with source and live configuration in parity:

- Node, Python, OpenClaw, the Codex plugin, and the embedded Codex package match the versions pinned by the repository.
- OpenClaw uses the OpenAI/Codex app-server runtime through its authenticated OAuth profile. There is no GitHub Copilot path, alternate provider retry, or silent compatibility fallback.
- The managed OpenClaw config, MCP configuration, sandbox policy, runtime settings, and agent skills are published atomically and validated after publication.
- Codex state and log databases pass ownership, link, schema, integrity, foreign-key, and path checks. No retained thread points to a missing rollout file, and no job or spawn edge points to a missing thread.
- Any repair is narrow, transactional, auditable, and fail-closed when the state does not match a known repairable shape.
- `openclaw-gateway.service` is enabled and active, with a successful health check and authenticated provider connectivity.
- The G2 app remains a thin user interface. Only the user or the controlling Codex session operates it; PM and research subagents do not directly drive the G2 session.
- The PM agent owns research orchestration and memory writes. Research subagents receive only the tools and repository access required for their assigned work.
- The loop uses the Quantipy cache/hydration API as its external data boundary, records the full analysis window and evaluation metrics, and preserves enough experiment metadata for reproducibility.
- Autoresearch is monitored for infrastructure failures, while experiment design and alpha-module decisions remain under autoresearch ownership. Infrastructure failures stop the loop, are fixed and reviewed, and are followed by a verified restart.
- Two complete, implementation-capable iterations finish without infrastructure intervention before the loop is considered self-sustaining.

## Resume Checklist

When work resumes, use this order:

1. Run `bash scripts/push-openclaw-config.sh` and allow it to complete; do not manually edit live OpenClaw files.
2. Verify config validation, doctor output, state/log integrity, and authenticated endpoint behavior.
3. Archive the live schema-v4 autoresearch state file and re-initialize at schema v5: stop the supervisor if active, archive `~/.openclaw/autoresearch/quantipy-state.json`, run `gateway-cli autoresearch-init-state`, then `autoresearch-pin-readiness`. (Required since the G1+G2 loop-governance work; the runner refuses v4 states by design — see `docs/reference/g1g2-implementation-spec.md` §1.10. The archived file is iteration 1 with no completed iterations, so no registry history is lost.)
4. Restart and enable the gateway service, then verify its health and logs.
5. Start the autoresearch PM session through the G2 app.
6. Verify the first complete loop end to end before widening the monitoring interval. Note the loop now pauses itself at `campaign_review` after sustained non-KEEP streaks; resume via `gateway-cli autoresearch-acknowledge-campaign-review`.

## Post-checkpoint code changes (2026-08-03)

Since this checkpoint was written, the refactor and improvement plan
(`docs/reference/autoresearch-refactor-plan.md`) was fully executed on
`main`: the autoresearch runner monolith is now the `gateway/autoresearch/`
package, the push script's logic lives in strict-typed
`gateway/deployment/` modules (the script is orchestration and trap
wiring only), the supervisor's rpc/reconciliation/checkpoint seams are
extracted, and the G1+G2 loop-governance features (hypothesis registry at
state schema v5, novelty gate, negative-results ledger, campaign stall
detection with operator acknowledgement) are implemented per
`docs/reference/g1g2-implementation-spec.md`. The deployment procedure
itself is unchanged apart from step 3 above.
