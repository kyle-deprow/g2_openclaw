# Autoresearch recovery execution plan — 2026-09-24

Authority: user requests implementation of the 2026-09-24 handoff, accepted changes on both repositories' main branches, frequent origin pushes, safe worktree cleanup, and continued Luna/Sol implementation and monitoring until a useful historical end-to-end result. This plan supplements the current handoff; it does not alter frozen research science or the application's Opus review gate.

## Verified progress

- `ec1600e`: worktree-independent review collection and one-time host-failure supersession, including atomic trigger migration. Luna fix/Sol READY; parent 557 research tests and Ruff/format/mypy passed; pushed to origin/main.
- `34fa94c`: in-sandbox provenance gate and historical reserved-bundle integration. Luna fix/Sol READY; parent 581 research tests including real containment and Ruff/format/mypy passed; pushed to origin/main.
- Quantipy main `44c77a8` pushed to origin/main; no scientific source changes promoted by the operator.
- All 16 registered secondary worktrees were archived, compared against their originals, checksum-verified again immediately before removal, and removed. Only the two main checkouts remain registered. Recovery details: `autoresearch-cleanup-2026-09-24.md`.
- `153f876`: exact pinned doctor report schema now includes `managed by Vite+ = "false"`. Luna's 207 shell-guard tests passed, Sol READY, parent 36 targeted tests plus Ruff/format/mypy passed; pushed to origin/main. The managed-versus-global npm diagnostic policy was already correct and was not relaxed.
- Deployment is not yet accepted: default resolution selects stale OpenClaw2026.7.1-2; the supported `OPENCLAW_BIN=/home/dev/.local/bin/openclaw` override selects installed2026.9.2. After E, both doctor checks pass, but command-probe validation assumes an obsolete nested dependency path instead of the exact verified managed npm package root. Publication rolled back safely. Fix F is delegated with explicit path-authority propagation, no runtime guard bypass or provider change.
- Campaign remains paused33/cap13; no historical job or trading result yet. Three supplied historical planning documents remain untracked and unchanged rather than being published without a separate content review.

## Ordered work and acceptance

1. Reconcile actual worktrees/processes against the handoff. Preserve all dirty work, immutable research evidence, and archive refs. Do not restart an existing live worker.
2. Review completed fixD with independent Sol. Luna fixes concrete findings if needed. Integrate onto G2 main only after READY and independent tests.
3. Verify fixC round 3 with real containment, review independently with Sol, and use bounded Luna fixes as required. Integrate after D, resolving only overlapping intended changes. Never skip failed containment tests.
4. Independently run research tests, Ruff checks/format verification, and mypy on the integrated main tree. Commit D and C separately, push accepted main changes to origin, and deploy runtime skill changes through the repository push script and gateway restart while the campaign remains paused.
5. Canonically recollect the existing H0006-A001 Opus verdict, preserving the synthetic host-failure record as superseded. Verify four real findings, one supersession, and no duplicate review. Close A001 RETRY.
6. Authorize only the handoff's explicit cap13→14 admission for clean H0006-A002; resume the Astra/Luna/Opus/Luna loop. Capture missing provenance mechanically before review; do not carry A001's proof tree into A002.
7. Luna monitors durable stage transitions and failures with bounded tools and file-based evidence. Sol independently reviews implementation fixes. Root handles consequential decisions, integration/deployment, and direct final verification, not repeated transcript exploration.
8. After actual Opus PASS, complete the pinned real historical job, collect artifacts, and obtain Astra's honest economic decision. Negative/inconclusive results are valid; synthetic/process/zero-trade-only success is insufficient.
9. Preserve accepted source/documentation on main, push both repos, and remove only reconciled inactive worktrees. Preserve unmerged/dirty work via durable recoverable archives before any authorized cleanup; never force-delete unknown work or research evidence.

## File ownership and scope

- fixD Luna may change only `gateway/research/review_evidence.py`, `gateway/research/store.py`, `tests/gateway/research/test_review_evidence.py`, `tests/gateway/research/test_store.py` in `/home/dev/repos/g2_openclaw-worktrees/fixD`.
- fixC Luna may change only its existing handoff diff: research CLI/containment/provenance/worker/store/review-evidence/wake modules, the new `gateway/research/containment_provenance/` recorder, corresponding `tests/gateway/research/` tests, and the three already-modified runtime/mirror skill docs in `/home/dev/repos/g2_openclaw-worktrees/fixC`. Exact changed-file inventory and original scratchpad C plans govern each bounded round.
- Sol reviewers are read-only. Workers must not commit, push, deploy, edit live state, change campaign caps, alter frozen contracts, or modify unrelated files. Root owns integration and Git operations.
- Read the phase/fix plans fully before editing, plus applicable AGENTS/skills. Exact scratchpad-plan copies are preserved in `docs/reference/autoresearch-recovery-2026-09-24/`; use those durable copies if the temporary scratchpad disappears. Report missing requirements rather than inventing them.
- Preserve existing main-checkout untracked documentation. No live trading, new providers/fallbacks, MemPalace writes, data downloads, environment clones, gate waivers, or automatic cap extensions beyond14.

## Verification

In the relevant checkout, serialize pytest and use its established environment:

```sh
uv run pytest tests/gateway/research -q
uv run ruff check gateway/research tests/gateway/research
uv run ruff format --check gateway/research tests/gateway/research
uv run mypy gateway/research tests/gateway/research
```

Use original phase plans for any additional exact targets. Real systemd/bwrap tests must actually execute successfully from an operator-host context; denial is a blocker, not a skip or substitute. After deployment validate OpenClaw configuration and gateway health. Never print auth material.

Definition of done: committed reviewed attempt with projected actual Opus PASS, completed real historical job and retained six-scenario/seven-report evidence, verified five-session rules/costs/bindings, final Astra decision, deliberately closed lifecycle with no unresolved children, main branches pushed, and reconciled cleanup documented. Until then report only the actual completed stage.
