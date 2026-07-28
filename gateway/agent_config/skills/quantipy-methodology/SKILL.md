---
name: quantipy-methodology
description: Deterministic source-of-truth routing for Quantipy autoresearch stages.
version: 2.0.0
---

# Quantipy Methodology

This skill routes native Codex stage agents to the minimum current Quantipy
methodology needed for their task. Every stage also uses
`quantipy-data-contract`; that compact runtime skill and the injected readiness
receipt define platform capabilities, so stages do not rediscover them.

Quantipy remains the source of truth. Do not copy its `AGENTS.md`, repo skills,
or Codex agent definitions into G2 OpenClaw.

## Required Preflight

Before context, debate, consensus, implementation, review, or fix work:

1. Read every file listed in the runner's `instruction_source_manifest` from
   its absolute canonical path when the current methodology content is needed.
2. Treat the manifest digest as a dispatch identity, not a live-file freshness
   gate. Do not reject the stage solely because mutable live state, readiness,
   or methodology files have advanced after dispatch; the runner verifies
   persisted-state and envelope compatibility before accepting artifacts.
   Missing or unreadable files whose content is required for the stage are
   operator-owned blockers.
3. Read `/home/dev/repos/quantipy/AGENTS.md` completely when listed or routed.
4. Load `quantipy-data-contract` and consume the injected
   `PLATFORM_READINESS_CAPABILITIES` receipt.
5. Read only the target-repo skills and rule files routed below or clearly
   required by the candidate task.
6. Read the routed Codex agent definitions.
7. Record the exact Quantipy source paths loaded in the structured stage
   artifact and return the exact `source_manifest_sha256` as
   `instruction_manifest_sha256` in the production artifact envelope. The
   digest is bound to the phase, expected artifact type, ordered target agent
   IDs, canonical target repo root, and sorted source receipts.

If a required path is missing, report that exact path as an operator-owned
blocker. Do not reconstruct missing methodology or capabilities from memory.
Never emit a raw unwrapped artifact; every production file passed to
`autoresearch-advance` uses the strict envelope and stays under the 64 KiB local
artifact-file budget. The next-action prompt remains bounded separately.

## Stage Routing

Every row assumes `AGENTS.md`, `quantipy-data-contract`, and the readiness
receipt are already loaded.

| Stage agent | Additional Quantipy sources |
|-------------|-----------------------------|
| `context-curator` | `experiment-data`; `data-querying` only to interpret existing universe receipts; `explorer.toml`, `researcher.toml` |
| `debater-microstructure` | `backtesting`, `experiment-data`; `theorist.toml`, `researcher.toml` |
| `debater-data` | `data-querying` including its price and security-master rules, `experiment-data`, `backtesting`; `researcher.toml` |
| `debater-skeptic` | `experiment-data`, `backtesting`; `contrarian.toml`, `reviewer.toml` |
| `debater-theory` | `backtesting`, `experiment-data`; `theorist.toml`, `researcher.toml` |
| `debater-implementation` | `backend-python`, `backtesting`, `data-querying`, `experiment-data`; `backend-python.toml`, `orchestrator.toml` |
| `consensus-arbiter` | Candidate-governing skills cited by debaters; `contrarian.toml`, `theorist.toml`, `reviewer.toml` |
| `implementer` | `backend-python`, `backtesting`, `data-querying`, `experiment-data`; `backend-python.toml`, `orchestrator.toml` |
| `reviewer` | `experiment-data`, `backtesting`, `data-querying`; `reviewer.toml`, `contrarian.toml` |
| `fixer` | `backend-python` and the skill governing the accepted defect; `backend-python.toml`, `orchestrator.toml`, `reviewer.toml` |

## Execution Rules

- Read routed target-repo methodology in the active stage turn. Treat
  capabilities as current only from the runner receipt.
- At consensus, preserve only canonical universe plan inputs: profile identity
  and digest, sorted selection schedule, maximum members per date, and execution
  policy. Consensus stores no batch boundaries; the runner mechanically derives
  deterministic contiguous batch boundaries from those inputs. Add snapshot,
  summary, and member-union materialization identities and digests, including
  each batch's contract digest, only to verification receipts after each history
  batch is materialized. Include the compact external canonical member-union
  manifest path/SHA receipt required by `quantipy-data-contract`. Do not replace
  either artifact with narrative capability claims or symbol lists.
- Use shell reads from the OpenClaw workspace when needed to inspect Quantipy.
- Do not change Quantipy methodology unless the accepted experiment task
  explicitly owns that change.
- If Quantipy methodology conflicts with autoresearch orchestration, report the
  conflict to the PM.
- `DATA_INFRA_G0` repairs data/provenance/folds and emits a gate outcome; it
  never validates alpha. `ALPHA_RESEARCH` requires valid readiness, universe,
  and coverage receipts before a performance conclusion.
- `ALPHA_RESEARCH` requires only compact `DynamicUniverseCoverageReceipt` for
  coverage. Legacy per-symbol `CoverageReceipt` and aggregate
  `AggregateCoverageReceipt` are explicitly `DATA_INFRA_G0`-only; when G0 uses
  them, their ranges, intersections, day counts, percentages, missing reasons,
  and fold counts must agree.
- Reject a burned theory family unless the alpha submission provides materially
  new evidence. Reviewer methodology `PASS` is distinct from an alpha KEEP
  decision.
