# Verified autoresearch worktree cleanup — 2026-09-24

The user authorized cleanup of both repositories' worktrees. All 16 secondary worktrees were removed only after full recoverable archiving. Their abandoned/diverged source was not promoted onto main.

Private recovery root: `/home/dev/autoresearch-recovery-20260924.DSU7GA` (mode0700).

Preserved evidence:

- `worktree-archives/`: full tar.zst snapshots including tracked, dirty, untracked, ignored files, symlinks, ACLs and extended attributes; original/final HEAD and status; diffs and index; process checks; comparison logs and SHA-256 receipts.
- `g2-before-cleanup.bundle` and `quantipy-before-cleanup.bundle`: verified complete Git-history bundles. Archived HEADs also remain reachable through `refs/archive/worktrees/20260924/<worktree-name>` in their authoritative repository.
- `worktrees-removed.tsv`: exact target, recovery archive, HEAD and removal time for every removed tree.
- `research-before-recollection.sqlite3`: consistent research database backup before live verdict recovery. No research database, review bundle, transcript, campaign input, or research archive ref was deleted.

Removed G2 secondary trees: fixA, fixB, fixC, fixD, owner-auth-sync-native-guard-20260910, owner-deployment-data-only-assembly-20260910, owner-readiness-assembly-20260910, owner-remove-codex-db-repair-20260910, research-interface-v2, research-persona-status-v2, research-routes-v2, and sentiment-steer.

Removed Quantipy secondary trees: t10-erd, t10-wst, t11-cqr, and t11-hpp.

The first fixC archive failed comparison/checksum verification around a shared CUDA library and was NOT used for removal. It remains preserved as a failed artifact. A separately named `g2_openclaw-worktrees__fixC-retry1` archive passed complete comparison and checksum checks; the removal receipt points to that verified replacement.

Before every removal the operator independently rechecked exact source HEAD/status, absence of a process using that worktree as its working directory, the archive checksum, and a full tar comparison. `git worktree remove --force` was used only for those exact approved targets after proving their dirty/ignored bytes recoverable. Both repositories now list only their main checkout.

For recovery, restore into a new directory/worktree from the archived HEAD and verified tar, never over main. The archived `.git` pointer refers to the removed registration: retain a fresh registration's `.git` pointer instead of restoring the old one. Full restoration metadata and the original index/diffs are alongside each archive. Archives are private and are not uploaded to origin.

This cleanup is operational only. It establishes neither scientific acceptance nor a historical research outcome. Future active research worktrees must not be removed before their terminal evidence and commits are durably preserved.
