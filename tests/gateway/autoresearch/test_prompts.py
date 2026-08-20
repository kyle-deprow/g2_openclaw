from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import cast

import gateway.autoresearch.artifacts as autoresearch_artifacts
import gateway.autoresearch.engine as autoresearch_engine
import gateway.autoresearch.prompts as autoresearch_prompts
import gateway.autoresearch.transitions as autoresearch_transitions
import pytest
from gateway.autoresearch.artifacts import (
    SetupContextArtifact,
)
from gateway.autoresearch.constants import (
    DEFAULT_AUTORESEARCH_STATE_PATH,
    DEFAULT_AUTORESEARCH_WORKTREE_ROOT,
    INSTRUCTION_SOURCE_MANIFEST_DIGEST_DOMAIN,
    INSTRUCTION_SOURCE_MANIFEST_VERSION,
    MAX_ARTIFACT_FILE_BYTES,
)
from gateway.autoresearch.engine import (
    next_action,
)
from gateway.autoresearch.enums import (
    ArtifactType,
    FixTriggerPhase,
    Phase,
    ResearchMode,
    VerificationStatus,
)
from gateway.autoresearch.errors import (
    AutoresearchReceiptError,
    AutoresearchValidationError,
)
from gateway.autoresearch.lifecycle import (
    start_next_iteration,
)
from gateway.autoresearch.manifest import (
    SourceReceipt,
)
from gateway.autoresearch.manifest_runtime import (
    QUANTIPY_RECEIPT_PATHS,
    build_instruction_source_manifest,
    build_receipt_catalog,
    expected_instruction_manifest_sha256,
    instruction_source_manifest_sha256,
)
from gateway.autoresearch.persistence import (
    load_artifact_file,
    persist_next_iteration_state,
)
from gateway.autoresearch.policy import (
    AutoresearchPolicy,
    ReceiptCatalog,
)
from gateway.autoresearch.state import (
    AutoresearchState,
)
from gateway.autoresearch_readiness import (
    PlatformReadinessManifest,
)

from tests.gateway.autoresearch.builders import (
    GitWorktree,
    _final_decision,
    _fix_result,
    _implementation_artifact,
    _implementation_result,
    _majority_consensus,
    _setup_artifact,
    _state_to_consensus,
    _state_to_decision,
    _state_to_review,
    _verification_result,
    advance_state,
)


def test_every_stage_prompt_has_one_compact_canonical_capabilities_block(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = AutoresearchState(platform_readiness=platform_readiness.identity())

    prompt = next_action(state, policy, receipts, platform_readiness).prompt_text

    marker = "PLATFORM_READINESS_CAPABILITIES="
    assert prompt.count(marker) == 1
    line = next(line for line in prompt.splitlines() if line.startswith(marker))
    payload = json.loads(line.removeprefix(marker))
    assert payload["capabilities"] == platform_readiness.to_dict()["capabilities"]
    assert set(payload["evidence"]) == {
        "quantipy_data_contract",
        "xnys_trading_calendar",
    }
    assert all(isinstance(item, str) for item in payload["evidence"].values())
    assert payload["contract_identity"] == {
        "manifest_id": platform_readiness.manifest_id,
        "snapshot_id": platform_readiness.snapshot_id,
    }
    assert "content" not in line
    assert "tickers" not in line
    assert "members" not in line
    assert prompt.count(platform_readiness.manifest_id) == 1
    assert prompt.count(platform_readiness.snapshot_id) == 1
    for evidence in platform_readiness.evidence.values():
        assert evidence.path is not None
        assert evidence.path not in prompt


def test_instruction_source_manifest_digest_is_canonical_and_deterministic(
    receipts: ReceiptCatalog,
) -> None:
    required_receipts = receipts.require(tuple(QUANTIPY_RECEIPT_PATHS))
    state = AutoresearchState()

    first = build_instruction_source_manifest(
        phase=Phase.SETUP_CONTEXT,
        expected_artifact_type=ArtifactType.SETUP,
        target_agent_ids=("autoresearch-pm",),
        target_repo_root=Path("/home/dev/repos/quantipy"),
        state=state,
        receipts=required_receipts,
    )
    second = build_instruction_source_manifest(
        phase=Phase.SETUP_CONTEXT,
        expected_artifact_type=ArtifactType.SETUP,
        target_agent_ids=("autoresearch-pm",),
        target_repo_root=Path("/home/dev/repos/quantipy"),
        state=state,
        receipts=tuple(reversed(required_receipts)),
    )

    assert first.canonical_json() == second.canonical_json()
    assert first.sha256() == second.sha256()
    assert first.sha256() == instruction_source_manifest_sha256(
        phase=Phase.SETUP_CONTEXT,
        expected_artifact_type=ArtifactType.SETUP,
        target_agent_ids=("autoresearch-pm",),
        target_repo_root=Path("/home/dev/repos/quantipy"),
        state=state,
        receipts=required_receipts,
    )
    assert [source.receipt_id for source in first.sources] == sorted(
        receipt.receipt_id for receipt in required_receipts
    )


def test_instruction_source_manifest_rejects_duplicate_receipt_ids(
    receipts: ReceiptCatalog,
) -> None:
    receipt = receipts.require(("quantipy.agents",))[0]
    state = AutoresearchState()

    with pytest.raises(AutoresearchReceiptError, match="duplicate instruction source"):
        build_instruction_source_manifest(
            phase=Phase.SETUP_CONTEXT,
            expected_artifact_type=ArtifactType.SETUP,
            target_agent_ids=("autoresearch-pm",),
            target_repo_root=Path("/home/dev/repos/quantipy"),
            state=state,
            receipts=(
                receipt,
                SourceReceipt(
                    receipt_id=receipt.receipt_id,
                    path=receipt.path,
                    sha256=receipt.sha256,
                ),
            ),
        )


def test_instruction_source_manifest_digest_is_bound_to_dispatch_context(
    receipts: ReceiptCatalog,
) -> None:
    required_receipts = receipts.require(tuple(QUANTIPY_RECEIPT_PATHS))
    state = AutoresearchState()
    baseline = build_instruction_source_manifest(
        phase=Phase.SETUP_CONTEXT,
        expected_artifact_type=ArtifactType.SETUP,
        target_agent_ids=("autoresearch-pm",),
        target_repo_root=Path("/home/dev/repos/quantipy"),
        state=state,
        receipts=required_receipts,
    ).sha256()

    variants = (
        build_instruction_source_manifest(
            phase=Phase.DEBATE,
            expected_artifact_type=ArtifactType.SETUP,
            target_agent_ids=("autoresearch-pm",),
            target_repo_root=Path("/home/dev/repos/quantipy"),
            state=state,
            receipts=required_receipts,
        ).sha256(),
        build_instruction_source_manifest(
            phase=Phase.SETUP_CONTEXT,
            expected_artifact_type=ArtifactType.CONTEXT_PACKET,
            target_agent_ids=("autoresearch-pm",),
            target_repo_root=Path("/home/dev/repos/quantipy"),
            state=state,
            receipts=required_receipts,
        ).sha256(),
        build_instruction_source_manifest(
            phase=Phase.SETUP_CONTEXT,
            expected_artifact_type=ArtifactType.SETUP,
            target_agent_ids=("context_curator",),
            target_repo_root=Path("/home/dev/repos/quantipy"),
            state=state,
            receipts=required_receipts,
        ).sha256(),
        build_instruction_source_manifest(
            phase=Phase.SETUP_CONTEXT,
            expected_artifact_type=ArtifactType.SETUP,
            target_agent_ids=("autoresearch-pm",),
            target_repo_root=Path("/home/dev/repos/quantipy-alt"),
            state=state,
            receipts=required_receipts,
        ).sha256(),
    )

    assert all(variant != baseline for variant in variants)


def test_next_action_exposes_compact_instruction_manifest_without_source_bytes(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = AutoresearchState(platform_readiness=platform_readiness.identity())

    action = next_action(state, policy, receipts, platform_readiness)
    payload = action.to_dict()

    assert action.source_manifest_sha256 == action.instruction_source_manifest.sha256()
    assert action.source_manifest_sha256 == expected_instruction_manifest_sha256(
        state, policy, receipts
    )
    assert len(action.source_manifest_sha256) == 64
    assert (
        action.state_reference_sha256 == action.instruction_source_manifest.state_reference.sha256()
    )
    assert "fixture for" not in action.prompt_text
    assert "content" not in json.dumps(payload, sort_keys=True)
    required = payload["required_receipts"]
    manifest = payload["instruction_source_manifest"]
    assert isinstance(required, list)
    assert isinstance(manifest, dict)
    assert manifest["version"] == INSTRUCTION_SOURCE_MANIFEST_VERSION
    assert manifest["digest_domain"] == INSTRUCTION_SOURCE_MANIFEST_DIGEST_DOMAIN
    assert manifest["phase"] == "setup_context"
    assert manifest["expected_artifact_type"] == "setup_context"
    assert manifest["target_agent_ids"] == ["autoresearch-pm"]
    assert manifest["target_repo_root"] == str(Path("/home/dev/repos/quantipy").resolve())
    assert set(manifest) == {
        "version",
        "digest_domain",
        "phase",
        "expected_artifact_type",
        "target_agent_ids",
        "target_repo_root",
        "state_reference",
        "sources",
    }
    state_reference = manifest["state_reference"]
    assert isinstance(state_reference, dict)
    assert state_reference["path"] == str(DEFAULT_AUTORESEARCH_STATE_PATH)
    assert state_reference["phase"] == state.phase.value
    assert state_reference["iteration"] == state.iteration
    assert (
        state_reference["state_sha256"]
        == action.instruction_source_manifest.state_reference.state_sha256
    )
    assert [source["receipt_id"] for source in manifest["sources"]] == sorted(
        receipt["receipt_id"] for receipt in required
    )
    for receipt in required:
        assert set(receipt) == {"receipt_id", "path", "sha256"}
        assert Path(receipt["path"]).is_absolute()
        assert re.fullmatch(r"[0-9a-f]{64}", receipt["sha256"])


def test_context_prompt_requires_flat_typed_schema_and_ignores_stale_context_files(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = AutoresearchState(
        setup=_setup_artifact(),
        platform_readiness=platform_readiness.identity(),
    )

    prompt = next_action(state, policy, receipts, platform_readiness).prompt_text

    expected_field_types = {
        "baseline_metric": "string",
        "current_best_metric": "string",
        "recent_experiment_outcomes": "array[string]",
        "prior_findings": "array[string]",
        "open_proposals": "array[string]",
        "hard_constraints": "array[string]",
        "available_data_sources": "array[string]",
        "loaded_quantipy_sources": "array[string]",
        "research_mode": "enum[alpha_research,data_infra_g0]",
        "mode_rationale": "string",
        "burned_theory_families": "array[string]",
        "contested_methodology_families": "array[string]",
    }
    contract = autoresearch_artifacts.ARTIFACT_CONTRACTS[ArtifactType.CONTEXT_PACKET]

    assert contract["field_types"] == expected_field_types
    assert set(cast(list[str], contract["required_fields"])) == set(expected_field_types)
    assert "Do not use nested objects in the context_packet artifact" in prompt
    assert "exactly the listed keys and no extra keys" in prompt
    assert "standalone iteration context files are non-authoritative residue" in prompt
    assert "alpha_research or data_infra_g0" in prompt
    assert "If the live state no longer matches STATE_REF, do not emit an artifact" in prompt


def test_build_receipt_catalog_fails_closed_when_required_source_is_missing(
    quantipy_root: Path,
) -> None:
    missing = quantipy_root / QUANTIPY_RECEIPT_PATHS["quantipy.agents"]
    missing.unlink()

    with pytest.raises(AutoresearchReceiptError, match="missing required receipt source"):
        build_receipt_catalog(quantipy_root)


def test_load_artifact_file_rejects_missing_bad_and_legacy_instruction_envelopes(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
) -> None:
    state = AutoresearchState()
    digest = expected_instruction_manifest_sha256(state, policy, receipts)
    artifact = _setup_artifact().to_dict()
    cases = [
        artifact,
        {"artifact": artifact},
        {"instruction_manifest_sha256": "0" * 64, "artifact": artifact},
        {
            "instruction_manifest_sha256": digest,
            "artifact": artifact,
            "legacy_extra": True,
        },
    ]

    for index, payload in enumerate(cases):
        artifact_path = tmp_path / f"bad-artifact-{index}.json"
        artifact_path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(AutoresearchValidationError):
            load_artifact_file(
                artifact_path,
                state,
                policy,
                instruction_manifest_sha256=digest,
            )


def test_load_artifact_file_accepts_exact_instruction_envelope(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
) -> None:
    state = AutoresearchState()
    state_path = tmp_path / "custom-state.json"
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    digest = expected_instruction_manifest_sha256(
        state,
        policy,
        receipts,
        state_path=state_path,
    )
    state_reference_sha256 = autoresearch_transitions.build_authoritative_state_reference(
        state,
        state_path=state_path,
    ).sha256()
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(
        json.dumps(
            {
                "instruction_manifest_sha256": digest,
                "state_reference_sha256": state_reference_sha256,
                "artifact": _setup_artifact().to_dict(),
            }
        ),
        encoding="utf-8",
    )

    artifact = load_artifact_file(
        artifact_path,
        state,
        policy,
        instruction_manifest_sha256=digest,
        state_path=state_path,
    )

    assert isinstance(artifact, SetupContextArtifact)
    assert artifact.metric_name == "OOS Sharpe net"


def test_load_artifact_file_rejects_oversized_envelope_before_json_parse(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
) -> None:
    state = AutoresearchState()
    artifact_path = tmp_path / "oversized-artifact.json"
    artifact_path.write_bytes(b"{" + (b'"x":' + b'"' + b"a" * MAX_ARTIFACT_FILE_BYTES + b'"'))
    digest = expected_instruction_manifest_sha256(state, policy, receipts)

    with pytest.raises(AutoresearchValidationError, match="artifact file exceeds hard byte budget"):
        load_artifact_file(
            artifact_path,
            state,
            policy,
            instruction_manifest_sha256=digest,
        )


def test_missing_receipt_file_fails_fast(tmp_path: Path) -> None:
    for receipt_id, relative_path in QUANTIPY_RECEIPT_PATHS.items():
        if receipt_id == "quantipy.skill.data_querying":
            continue
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture\n", encoding="utf-8")

    with pytest.raises(AutoresearchReceiptError, match=r"data-querying/SKILL.md"):
        build_receipt_catalog(tmp_path)


def test_repeat_prompt_requires_standardized_mempalace_kg_facts_from_verified_state(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_decision(policy, platform_readiness)
    state = advance_state(state, _final_decision(), policy)
    prompt = next_action(state, policy, receipts, platform_readiness).prompt_text

    assert "derive the exact standardized predicate/object pairs" in prompt
    assert "STATE_REF=" in prompt
    assert "alpha_decision_metric" not in prompt


def test_next_iteration_recomputes_instruction_manifest_under_state_lock(
    tmp_path: Path,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    quantipy_root: Path,
    completed_memory_written_state: AutoresearchState,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state_path = tmp_path / "quantipy-state.json"
    state_path.write_text(json.dumps(completed_memory_written_state.to_dict()), encoding="utf-8")
    instruction_digest = expected_instruction_manifest_sha256(
        completed_memory_written_state,
        policy,
        receipts,
        state_path=state_path,
    )
    drift_path = quantipy_root / QUANTIPY_RECEIPT_PATHS["quantipy.agents"]
    drift_path.write_text("changed after dispatch\n", encoding="utf-8")
    next_state = start_next_iteration(completed_memory_written_state, readiness=platform_readiness)

    with pytest.raises(AutoresearchValidationError, match="instruction manifest is stale"):
        persist_next_iteration_state(
            state_path,
            state_path,
            completed_memory_written_state,
            next_state,
            instruction_manifest_sha256=instruction_digest,
            policy=policy,
            receipt_catalog_factory=lambda: build_receipt_catalog(quantipy_root),
        )

    persisted = AutoresearchState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
    assert persisted == completed_memory_written_state
    assert not (state_path.parent / "decision-receipts").exists()


def test_implementation_prompt_contains_workspace_isolation_contract(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)

    prompt = next_action(state, policy, receipts, platform_readiness).prompt_text

    assert "Workspace isolation contract" in prompt
    assert "disposable isolated clone" in prompt
    assert json.dumps(str(DEFAULT_AUTORESEARCH_WORKTREE_ROOT)) in prompt
    assert "umask 077" in prompt
    assert "mkdir -p /home/dev/.openclaw/autoresearch/model-workspaces" in prompt
    assert "chmod 700" in prompt
    assert "mode 0700" in prompt
    assert "working_directory" in prompt
    assert "authoritative target checkout" in prompt
    assert "Never use /tmp" in prompt
    assert "31G tmpfs" in prompt
    assert "Commit all accepted implementation changes" in prompt
    assert "workspace_path" in prompt
    assert "commit_sha" in prompt


def test_alpha_campaign_directive_is_present_only_for_alpha_stage_prompts(
    policy: AutoresearchPolicy,
) -> None:
    alpha_state = _state_to_consensus(policy)
    alpha_prompt = autoresearch_engine._phase_instruction(
        alpha_state,
        Phase.CONSENSUS,
        ArtifactType.CONSENSUS_RESULT,
        ("agent",),
        state_path=Path("/tmp/state.json"),
    )
    g0_prompt = autoresearch_engine._phase_instruction(
        replace(alpha_state, mode=ResearchMode.DATA_INFRA_G0),
        Phase.CONSENSUS,
        ArtifactType.CONSENSUS_RESULT,
        ("agent",),
        state_path=Path("/tmp/state.json"),
    )

    for phrase in (
        "any holding period",
        "overnight carry is forbidden",
        "1.0 trades/day",
        "A permanently burned family may be re-proposed",
    ):
        assert phrase in alpha_prompt
        assert phrase not in g0_prompt
    for stale_phrase in ("scalping", "short-holding-period", "at least 2 trades per day"):
        assert stale_phrase not in alpha_prompt

    # The injected directive is a second source of truth alongside SKILL.md section 8
    # and has drifted from it before. Pin the universe rule in both directions so the
    # prompt cannot keep asserting a frozen panel after the directive is relaxed.
    # Mode scoping is already covered above; this block pins the rule's CONTENT.
    # `universe_plan` also appears in the shared consensus artifact contract, so it
    # is deliberately not asserted absent from the DATA_INFRA_G0 prompt.
    for phrase in (
        "pre-registered panel",
        "universe_plan",
        "mechanical, data-independent selection rule",
        "may never use out-of-sample returns",
        "post-hoc member substitution is forbidden",
        "trades per day per instrument",
    ):
        assert phrase in alpha_prompt
    for stale_universe_phrase in (
        "five-ETF panel",
        "frozen five-ETF universe",
        "no member substitution, the",
    ):
        assert stale_universe_phrase not in alpha_prompt

    for phrase in (
        "group closely related proposals into theory-family clusters",
        "in both rounds",
        "pre-registered deterministic tie-break",
        "most votes",
        "least-explored family",
        "lexicographically smallest normalized family name",
        "losing clusters in dissent_summary",
        "enumerate data_requirements",
        "exactly price_panel",
        "receipt-bound 1-minute price data",
        "operator-precondition consensus",
        "never authorize it for implementation",
    ):
        assert phrase in alpha_prompt
    assert len(autoresearch_engine.CONSENSUS_DATA_REQUIREMENTS_INSTRUCTION.encode()) <= 500


def test_zero_trade_alpha_gate_pass_tuple_is_accepted_by_artifact_validator() -> None:
    candidate = replace(
        _verification_result(VerificationStatus.PASS),
        is_walk_forward_sharpe_net=0.0,
        oos_sharpe_net=0.0,
        max_drawdown_pct=0.0,
        win_rate=0.0,
        trade_count=0,
        trades_per_day=0.0,
    )

    parsed = autoresearch_artifacts.VerificationResultArtifact.from_dict(
        candidate.to_dict(),
        mode=ResearchMode.ALPHA_RESEARCH,
    )

    assert parsed.status is VerificationStatus.PASS
    assert parsed.tests_passed is True
    assert parsed.bug_signals == ()
    assert parsed.trade_count == 0
    assert parsed.trades_per_day == 0.0
    assert parsed.is_walk_forward_sharpe_net == 0.0
    assert parsed.oos_sharpe_net == 0.0
    assert parsed.max_drawdown_pct == 0.0
    assert parsed.win_rate == 0.0


def test_fix_test_prompt_contains_private_workspace_and_cwd_contract(
    git_worktree: GitWorktree,
) -> None:
    state = AutoresearchState(
        phase=Phase.FIX_TEST,
        implementation_result=_implementation_artifact(git_worktree),
    )

    prompt = autoresearch_engine._workspace_isolation_contract(state, Phase.FIX_TEST)

    assert "owned non-symlink" in prompt
    assert "mode 0700" in prompt
    assert "working_directory and spawned process cwd" in prompt
    assert "authoritative target checkout" in prompt


def test_implementation_target_is_always_implementation_result(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    """Brief wording must never force the blocker decision as the target.

    Keying the expectation on transport terms in the brief text made the
    controller reject valid implementation_result submissions for three
    consecutive iterations once briefs began declaring price_panel.
    """
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    assert state.latest_consensus is not None
    state = replace(
        state,
        consensus_history=(
            replace(
                state.latest_consensus,
                implementation_brief=(
                    "The approved brief requires an ExperimentManifest transport contract "
                    "the platform does not provide."
                ),
            ),
        ),
    )

    target = autoresearch_prompts._select_phase_target(state, policy)

    assert target.artifact_type is ArtifactType.IMPLEMENTATION_RESULT


def test_fix_prompt_and_validator_reuse_persisted_implementation_workspace(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    implementation = _implementation_result()
    state = advance_state(state, implementation, policy)
    state = advance_state(state, _verification_result(VerificationStatus.TEST_FAILURE), policy)

    prompt = next_action(state, policy, receipts, platform_readiness).prompt_text
    fixed = advance_state(state, _fix_result(FixTriggerPhase.VERIFICATION), policy)

    assert "reuse the exact persisted implementation worktree" in prompt
    assert implementation.workspace_path not in prompt
    assert implementation.commit_sha not in prompt
    assert "Never create another worktree" in prompt
    assert (
        "Any notebook, hydrate, backtest, or similarly long test command MUST be launched "
        "through /home/dev/repos/g2_openclaw/scripts/run-long-task.sh"
    ) in prompt
    assert "direct foreground execution is invalid" in prompt
    assert "without emitting a fix_result" in prompt
    assert fixed.implementation_result is not None
    assert fixed.implementation_result.workspace_path == implementation.workspace_path


def test_implementation_prompt_does_not_direct_reuse_of_a_prior_workspace(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)

    prompt = next_action(state, policy, receipts, platform_readiness).prompt_text

    assert "Create and use a disposable isolated clone" in prompt
    assert "Reuse the exact persisted implementation worktree" not in prompt


def test_verification_prompt_uses_recorded_workspace(
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    platform_readiness: PlatformReadinessManifest,
) -> None:
    state = _state_to_consensus(policy, platform_readiness)
    state = advance_state(state, _majority_consensus(round_number=1, policy=policy), policy)
    state = advance_state(state, _implementation_result(), policy)

    prompt = next_action(state, policy, receipts, platform_readiness).prompt_text

    assert "implementation_result.workspace_path" in prompt
    assert "implementation_result.commit_sha" in prompt


def test_phase_instructions_require_feasibility_telemetry_and_projected_timeout(
    policy: AutoresearchPolicy,
) -> None:
    implementation = autoresearch_engine._phase_instruction(
        _state_to_consensus(policy),
        Phase.IMPLEMENTATION,
        ArtifactType.IMPLEMENTATION_RESULT,
        ("agent",),
        state_path=Path("/tmp/state.json"),
    )
    assert "encoded_feature_columns" in implementation
    assert "calibration_fit_seconds" in implementation
    assert "projected_model_seconds" in implementation
    assert "reporting-only, not an admission gate" in implementation
    assert (
        "A succeeded prewarm run bound to the current state reference MUST be reused"
        in implementation
    )
    assert "this is a reporting duty, not a mechanical gate" in implementation
    for instruction in (implementation,):
        assert "4096-character cap" in instruction
        assert "serialized length <= 3800 locally" in instruction
        assert "top-K by |magnitude| with K named" in instruction
        assert "~6 significant figures" in instruction
        assert "prefixes stripped" in instruction
        assert "comparators as scalars" in instruction
        assert "bulk detail in the run artifact" in instruction
    assert (
        "Experiment packages are built ONLY in the persisted experiment workspace, never in the "
        "authoritative quantipy worktree" in implementation
    )
    assert (
        "If implementation reveals the approved brief requires a data or runtime contract the "
        "platform does not provide, do not work around it or edit shared platform code; submit "
        "a FINAL_DECISION with INFRA_BLOCKED, reviewer_verdict NOT_RUN, null metric, "
        "memory_write_required false, and an infra_rationale naming the exact missing contract."
        in implementation
    )

    verification = autoresearch_engine._phase_instruction(
        _state_to_review(policy),
        Phase.VERIFICATION,
        ArtifactType.VERIFICATION_RESULT,
        ("agent",),
        state_path=Path("/tmp/state.json"),
    )
    assert "projected_model_seconds reported in implementation_result.summary" in verification
    assert (
        "timeout_seconds = min(max(3 * projected_model_seconds + pre_model_seconds, 1800), "
        "43200)" in verification
    )
    assert (
        "timeout_seconds = min(max(3 * projected_model_seconds + pre_model_seconds, 1800), "
        "21600)" in verification
    )
    assert "default 28800s for gpu or mixed" in verification
    assert "the default 14400s for cpu or none" in verification
    assert "Measured values still drive the timeout" in verification
    assert "ceiling is only a cap, not permission to skip projection" in verification
    assert (
        "zero trades because a declared, pre-registered cost or edge gate excluded every candidate"
        in verification
    )
    assert "eligible for a normal DISCARD" in verification
    assert "empty panel, broken feature build, or exception remain BUG_SIGNAL" in verification
    assert "4096-character cap" in verification
    assert "serialized length <= 3800 locally" in verification
    assert "top-K by |magnitude| with K named" in verification
    assert "~6 significant figures" in verification
    assert "prefixes stripped" in verification
    assert "comparators as scalars" in verification
    assert "bulk detail in the run artifact" in verification

    review = autoresearch_engine._phase_instruction(
        _state_to_review(policy),
        Phase.REVIEW,
        ArtifactType.REVIEW_RESULT,
        ("reviewer",),
        state_path=Path("/tmp/state.json"),
    )
    assert "exact gate parameter names and values match the approved consensus" in review
    assert "excluded_candidate_count is present" in review
    assert "otherwise do not accept the DISCARD" in review
    assert "data_requirements as the arbiter's declaration trust point" in review
    assert "not mechanical semantic parsing" in review
    assert "MUST verify the implementation actually consumed only declared transports" in review
    assert "flag any undeclared data dependency as a CRITICAL issue" in review
    assert "dishonest declaration" in review
    assert "mid-implementation INFRA_BLOCKED route bounds the cost" in review
    assert "named contract is recorded in the hypothesis registry" in review


def test_decision_and_review_instructions_require_oos_metric_and_activity_floor(
    policy: AutoresearchPolicy,
) -> None:
    review = autoresearch_engine._phase_instruction(
        _state_to_review(policy),
        Phase.REVIEW,
        ArtifactType.REVIEW_RESULT,
        ("reviewer",),
        state_path=Path("/tmp/state.json"),
    )
    assert "recommended_metric_name MUST name an out-of-sample, cost-net metric" in review
    assert "An in-sample metric is not a valid recommendation." in review

    decision = autoresearch_engine._phase_instruction(
        _state_to_decision(policy),
        Phase.DECISION_LOG,
        ArtifactType.FINAL_DECISION,
        ("pm",),
        state_path=Path("/tmp/state.json"),
    )
    assert (
        "if the reviewer recommended an in-sample metric, substitute the out-of-sample "
        "net Sharpe and state the substitution in the rationale" in decision
    )
    assert "the decision Sharpe is the out-of-sample cost-net Sharpe" in decision
    assert (
        "Average activity below 1.0 trades/day over the OOS window is DISCARD regardless of Sharpe."
        in decision
    )
