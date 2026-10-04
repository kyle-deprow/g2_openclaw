"""Strict synthetic tests for the pure scientific hypothesis document."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import cast

import pytest
from gateway.research.hypothesis import (
    ComputeLimits,
    DateRange,
    FeatureDeclaration,
    HypothesisDocument,
    MinimumEvidence,
    PowerDesign,
    minimum_detectable_effect_bps,
)


@pytest.fixture
def document_payload() -> dict[str, object]:
    return {
        "contract": "research-hypothesis-v1",
        "mechanism": "A short-term reversal after a large overnight move.",
        "prediction": "The next regular close return is positive on average.",
        "target": "next_regular_close_return",
        "baseline": "buy-and-hold over the same evaluation sessions",
        "universe_rule": "ETF panel members with a complete regular-close history.",
        "features": [
            {
                "name": "overnight_return",
                "source": "panel",
                "as_of_rule": "known at the session open",
                "lookback_sessions": 5,
            }
        ],
        "entry_rule": "Enter at the next regular open when overnight_return is below -2%.",
        "exit_rule": "Exit at the following regular close.",
        "position_sizing_rule": "Use the trusted outer evaluation sizing contract.",
        "variants": ["threshold-minus-2", "threshold-minus-3"],
        "search_budget_evaluations": 2,
        "analysis": {"start": "2020-01-01", "end": "2024-12-31"},
        "evaluation": {"start": "2024-01-01", "end": "2024-12-31"},
        "training": {"start": "2020-01-01", "end": "2023-12-31"},
        "purpose": "DEVELOPMENT_VALIDATION",
        "forward_label_sessions": 1,
        "purge_rule": "Purge at least the maximum declared forward label horizon.",
        "primary_metric": "mean net return per evaluation session",
        "minimum_evidence": {"sessions": 20, "trades": 5},
        "null_tests": ["placebo entry dates", "sign-shuffled overnight returns"],
        "reject_criteria": "Reject if the primary metric is not positive.",
        "missing_data_rule": "Skip a signal when any required panel value is absent.",
        "compute": {"max_wall_seconds": 120.5, "max_rss_mb": 1024},
        "deliverables": ["reports/summary.json", "metrics/table.csv"],
    }


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(value, separators=(",", ":"))


def _reversed_mapping(value: object) -> object:
    if isinstance(value, dict):
        return {key: _reversed_mapping(child) for key, child in reversed(list(value.items()))}
    if isinstance(value, list):
        return [_reversed_mapping(child) for child in value]
    return value


def _set_path(root: dict[str, object], path: tuple[str | int, ...], value: object) -> None:
    current: object = root
    assert path
    for key in path[:-1]:
        if isinstance(key, str):
            assert isinstance(current, dict)
            current = current[key]
        else:
            assert isinstance(current, list)
            current = current[key]

    key = path[-1]
    if isinstance(key, str):
        assert isinstance(current, dict)
        current[key] = value
    else:
        assert isinstance(current, list)
        current[key] = value


def test_valid_price_only_document_round_trips_with_stable_digest(
    document_payload: dict[str, object],
) -> None:
    document = HypothesisDocument.from_json(_json(document_payload))
    restored = HypothesisDocument.from_json(document.to_json())

    assert restored == document
    assert document.to_json() == restored.to_json()
    assert document.sha256 == restored.sha256
    assert len(document.sha256) == 64


def test_direct_document_construction_path_is_frozen_and_strict(
    document_payload: dict[str, object],
) -> None:
    document = HypothesisDocument.from_json(_json(document_payload))

    with pytest.raises(ValueError):
        replace(document, mechanism=" ")
    with pytest.raises(FrozenInstanceError):
        document.prediction = "changed"  # type: ignore[misc]


def test_nested_value_objects_are_public_frozen_and_strict(
    document_payload: dict[str, object],
) -> None:
    document = HypothesisDocument.from_json(_json(document_payload))

    assert isinstance(document.features[0], FeatureDeclaration)
    assert isinstance(document.analysis, DateRange)
    assert isinstance(document.minimum_evidence, MinimumEvidence)
    assert isinstance(document.compute, ComputeLimits)
    with pytest.raises(FrozenInstanceError):
        document.analysis.start = "2020-01-01"  # type: ignore[misc]
    with pytest.raises(TypeError):
        FeatureDeclaration("name", "panel", "known at open")  # type: ignore[call-arg]


def test_canonical_json_ignores_input_key_order(document_payload: dict[str, object]) -> None:
    document = HypothesisDocument.from_json(_json(document_payload))
    reordered = HypothesisDocument.from_json(
        _json(cast(Mapping[str, object], _reversed_mapping(document_payload)))
    )

    assert reordered.to_json() == document.to_json()
    assert reordered.sha256 == document.sha256


def test_null_training_is_allowed_and_reddit_is_only_a_declaration(
    document_payload: dict[str, object],
) -> None:
    document_payload["training"] = None
    feature = document_payload["features"][0]  # type: ignore[index]
    feature["source"] = "reddit"

    document = HypothesisDocument.from_json(_json(document_payload))

    assert document.training is None
    assert document.features[0].source == "reddit"
    assert "host_support" not in document.to_json()


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (("analysis", "start"), "2025-01-01", "date_range is reversed"),
        (("evaluation", "start"), "2019-12-31", "evaluation must be inside analysis"),
        (("evaluation", "end"), "2025-01-01", "evaluation must be inside analysis"),
        (("training", "start"), "2019-12-31", "training must be inside analysis"),
        (("training", "end"), "2024-01-01", "training must end before evaluation starts"),
        (("analysis", "end"), "not-a-date", "date_range.end must be an ISO date"),
    ],
)
def test_invalid_or_overlapping_date_ranges_are_rejected(
    document_payload: dict[str, object],
    path: tuple[str, str],
    value: str,
    reason: str,
) -> None:
    changed = copy.deepcopy(document_payload)
    target: dict[str, object] = changed[path[0]]  # type: ignore[assignment]
    target[path[1]] = value

    with pytest.raises(ValueError) as error:
        HypothesisDocument.from_json(_json(changed))
    assert str(error.value) == reason


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_document_keys_are_closed(document_payload: dict[str, object], mutation: str) -> None:
    changed = copy.deepcopy(document_payload)
    if mutation == "missing":
        del changed["mechanism"]
    else:
        changed["unexpected"] = True

    with pytest.raises(ValueError):
        HypothesisDocument.from_json(_json(changed))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("contract",), "wrong-contract"),
        (("mechanism",), "   "),
        (("features",), []),
        (("features", 0, "source"), "sentiment"),
        (("features", 0, "lookback_sessions"), True),
        (("variants",), ["same", "same"]),
        (("purpose",), "FINAL"),
        (("forward_label_sessions",), -1),
        (("minimum_evidence", "sessions"), 0),
        (("minimum_evidence", "trades"), -1),
        (("null_tests",), []),
        (("compute", "max_wall_seconds"), 0),
        (("compute", "max_wall_seconds"), 28_801),
        (("compute", "max_rss_mb"), 16_385),
    ],
)
def test_types_and_bounds_are_rejected(
    document_payload: dict[str, object],
    path: tuple[str, ...] | tuple[str, int, str],
    value: object,
) -> None:
    changed = copy.deepcopy(document_payload)
    _set_path(changed, path, value)

    with pytest.raises(ValueError):
        HypothesisDocument.from_json(_json(changed))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_compute_values_are_rejected(
    document_payload: dict[str, object], value: float
) -> None:
    changed = copy.deepcopy(document_payload)
    changed["compute"]["max_wall_seconds"] = value  # type: ignore[index]

    with pytest.raises(ValueError):
        HypothesisDocument.from_json(json.dumps(changed, allow_nan=True))


def test_integer_wall_seconds_normalize_and_round_trip_hash(
    document_payload: dict[str, object],
) -> None:
    changed = copy.deepcopy(document_payload)
    changed["compute"]["max_wall_seconds"] = 120  # type: ignore[index]

    document = HypothesisDocument.from_json(_json(changed))
    restored = HypothesisDocument.from_json(document.to_json())

    assert document.compute.max_wall_seconds == 120.0
    assert '"max_wall_seconds":120.0' in document.to_json()
    assert restored == document
    assert restored.sha256 == document.sha256


def test_overflow_to_infinity_is_rejected(
    document_payload: dict[str, object],
) -> None:
    changed = copy.deepcopy(document_payload)
    changed["compute"]["max_wall_seconds"] = 10**400  # type: ignore[index]

    with pytest.raises(ValueError) as error:
        HypothesisDocument.from_json(_json(changed))
    assert str(error.value) == "compute.max_wall_seconds must be finite"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (True, "compute.max_wall_seconds must be a number"),
        (28_801, "compute.max_wall_seconds is outside the supported bounds"),
    ],
)
def test_wall_seconds_reject_bool_and_upper_bound(
    document_payload: dict[str, object], value: object, reason: str
) -> None:
    changed = copy.deepcopy(document_payload)
    changed["compute"]["max_wall_seconds"] = value  # type: ignore[index]

    with pytest.raises(ValueError) as error:
        HypothesisDocument.from_json(_json(changed))
    assert str(error.value) == reason


def test_wall_seconds_accepts_inclusive_upper_bound(
    document_payload: dict[str, object],
) -> None:
    changed = copy.deepcopy(document_payload)
    changed["compute"]["max_wall_seconds"] = 28_800  # type: ignore[index]
    changed["compute"]["max_rss_mb"] = 16_384  # type: ignore[index]

    document = HypothesisDocument.from_json(_json(changed))

    assert document.compute.max_wall_seconds == 28_800.0
    assert document.compute.max_rss_mb == 16_384


def test_historical_compute_limits_still_parse(document_payload: dict[str, object]) -> None:
    changed = copy.deepcopy(document_payload)
    changed["compute"] = {"max_wall_seconds": 7_200.0, "max_rss_mb": 8_192}

    document = HypothesisDocument.from_json(_json(changed))

    assert (document.compute.max_wall_seconds, document.compute.max_rss_mb) == (7_200.0, 8_192)


def test_noncanonical_date_is_rejected(document_payload: dict[str, object]) -> None:
    changed = copy.deepcopy(document_payload)
    changed["analysis"]["start"] = "20200101"  # type: ignore[index]

    with pytest.raises(ValueError) as error:
        HypothesisDocument.from_json(_json(changed))
    assert str(error.value) == "date_range.start must use canonical YYYY-MM-DD form"


def test_variant_budget_rejection_reason_is_precise(
    document_payload: dict[str, object],
) -> None:
    changed = copy.deepcopy(document_payload)
    changed["search_budget_evaluations"] = 1

    with pytest.raises(ValueError) as error:
        HypothesisDocument.from_json(_json(changed))
    assert str(error.value) == "search_budget_evaluations must cover all variants"


@pytest.mark.parametrize(
    "path",
    [
        ("/tmp/report.json",),
        ("reports/../report.json",),
        ("reports//report.json",),
        ("reports\\report.json",),
        ("reports/report.json",),
    ],
)
def test_unsafe_or_duplicate_deliverables_are_rejected(
    document_payload: dict[str, object], path: tuple[str]
) -> None:
    changed = copy.deepcopy(document_payload)
    changed["deliverables"] = [path[0], path[0]] if path[0] == "reports/report.json" else [path[0]]

    with pytest.raises(ValueError):
        HypothesisDocument.from_json(_json(changed))


@pytest.mark.parametrize(
    "field",
    ["features", "analysis", "evaluation", "training", "minimum_evidence", "compute"],
)
def test_unknown_nested_keys_are_rejected(document_payload: dict[str, object], field: str) -> None:
    changed = copy.deepcopy(document_payload)
    if field == "features":
        changed["features"][0]["unexpected"] = 1  # type: ignore[index]
    else:
        changed[field]["unexpected"] = 1  # type: ignore[index]

    with pytest.raises(ValueError) as error:
        HypothesisDocument.from_json(_json(changed))
    assert field in str(error.value)


def test_duplicate_json_keys_are_rejected(document_payload: dict[str, object]) -> None:
    duplicate = _json(document_payload).replace(
        '"contract":"research-hypothesis-v1"',
        '"contract":"research-hypothesis-v1","contract":"research-hypothesis-v1"',
    )
    with pytest.raises(ValueError):
        HypothesisDocument.from_json(duplicate)


def test_changed_economic_claim_changes_digest(document_payload: dict[str, object]) -> None:
    original = HypothesisDocument.from_json(_json(document_payload))
    changed = copy.deepcopy(document_payload)
    changed["prediction"] = "The next regular close return is negative on average."

    revised = HypothesisDocument.from_json(_json(changed))

    assert revised.sha256 != original.sha256


# Deliberately pins hypothesis._MAX_TEXT_LENGTH; update both together.
_TEXT_FIELD_BOUND = 16_384


@pytest.mark.parametrize("field", ["entry_rule", "position_sizing_rule", "missing_data_rule"])
def test_text_field_accepts_exactly_the_maximum_length(
    document_payload: dict[str, object], field: str
) -> None:
    changed = copy.deepcopy(document_payload)
    changed[field] = "x" * _TEXT_FIELD_BOUND

    document = HypothesisDocument.from_json(_json(changed))

    assert len(getattr(document, field)) == _TEXT_FIELD_BOUND


@pytest.mark.parametrize("field", ["entry_rule", "position_sizing_rule", "missing_data_rule"])
def test_text_field_rejects_one_over_the_maximum_length(
    document_payload: dict[str, object], field: str
) -> None:
    changed = copy.deepcopy(document_payload)
    changed[field] = "x" * (_TEXT_FIELD_BOUND + 1)

    with pytest.raises(ValueError, match=f"{field} exceeds the maximum length"):
        HypothesisDocument.from_json(_json(changed))


def test_text_array_items_share_the_maximum_length(document_payload: dict[str, object]) -> None:
    accepted = copy.deepcopy(document_payload)
    accepted["null_tests"] = ["x" * _TEXT_FIELD_BOUND]
    assert HypothesisDocument.from_json(_json(accepted)).null_tests == ("x" * _TEXT_FIELD_BOUND,)

    rejected = copy.deepcopy(document_payload)
    rejected["null_tests"] = ["x" * (_TEXT_FIELD_BOUND + 1)]
    with pytest.raises(ValueError, match="null_tests"):
        HypothesisDocument.from_json(_json(rejected))


def test_text_field_above_the_historical_bound_round_trips(
    document_payload: dict[str, object],
) -> None:
    changed = copy.deepcopy(document_payload)
    changed["position_sizing_rule"] = "x" * 8_883

    document = HypothesisDocument.from_json(_json(changed))

    assert len(document.position_sizing_rule) == 8_883
    assert HypothesisDocument.from_json(document.to_json()).to_json() == document.to_json()


# --- contract v2: pre-registered power block -------------------------------------------------

H0008_SPEC = Path(__file__).parent / "fixtures" / "H0008-spec.json"
# pragma: allowlist nextline secret
H0008_STORED_SHA256 = "bb9bede618d6b9e5deb185cf10926ced5703c912424942f9d102b3a900304452"


def _power() -> dict[str, object]:
    return {
        "expected_events": 100,
        "per_event_sd_bps": 100.0,
        "sd_basis": "SD of paired events in the 2020-2023 development period, not the test period",
        "alpha": 0.05,
        "power": 0.8,
        "sided": "one",
        "minimum_detectable_effect_bps": 24.86,
        "plausible_effect_bps": 30.0,
        "plausibility_basis": "reported gross reversal edge for liquid ETFs",
    }


@pytest.fixture
def v2_payload(document_payload: dict[str, object]) -> dict[str, object]:
    document_payload["contract"] = "research-hypothesis-v2"
    document_payload["power"] = _power()
    return document_payload


def _v2_power(payload: dict[str, object]) -> dict[str, object]:
    power = payload["power"]
    assert isinstance(power, dict)
    return power


def test_v2_document_round_trips_and_carries_the_power_block(
    v2_payload: dict[str, object],
) -> None:
    document = HypothesisDocument.from_json(_json(v2_payload))

    assert isinstance(document.power, PowerDesign)
    assert document.power.minimum_detectable_effect_bps == 24.86
    assert json.loads(document.to_json())["power"]["sided"] == "one"
    restored = HypothesisDocument.from_json(document.to_json())
    assert restored == document
    assert restored.sha256 == document.sha256


def test_v1_document_does_not_emit_a_power_key(document_payload: dict[str, object]) -> None:
    document = HypothesisDocument.from_json(_json(document_payload))

    assert document.power is None
    assert "power" not in json.loads(document.to_json())


def test_v1_document_rejects_a_power_block_and_v2_requires_one(
    v2_payload: dict[str, object],
) -> None:
    with_power = {**v2_payload, "contract": "research-hypothesis-v1"}
    with pytest.raises(ValueError, match="keys must be exactly"):
        HypothesisDocument.from_json(_json(with_power))
    del v2_payload["power"]
    with pytest.raises(ValueError, match="keys must be exactly"):
        HypothesisDocument.from_json(_json(v2_payload))


def test_real_h0008_v1_spec_round_trips_byte_identically_with_its_stored_digest() -> None:
    text = H0008_SPEC.read_text(encoding="utf-8")

    document = HypothesisDocument.from_json(text)

    assert document.contract == "research-hypothesis-v1"
    assert document.power is None
    assert document.to_json() == text
    assert document.sha256 == H0008_STORED_SHA256
    assert hashlib.sha256(H0008_SPEC.read_bytes()).hexdigest() == H0008_STORED_SHA256


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("expected_events", 0),
        ("expected_events", 1.5),
        ("expected_events", True),
        ("expected_events", "100"),
        ("per_event_sd_bps", 0),
        ("per_event_sd_bps", -1.0),
        ("per_event_sd_bps", True),
        ("per_event_sd_bps", "100"),
        ("sd_basis", ""),
        ("sd_basis", "   "),
        ("alpha", 0),
        ("alpha", 0.51),
        ("alpha", -0.05),
        ("power", 0.49),
        ("power", 0.995),
        ("sided", "both"),
        ("sided", ""),
        ("minimum_detectable_effect_bps", 0),
        ("minimum_detectable_effect_bps", -24.86),
        ("plausible_effect_bps", 0),
        ("plausible_effect_bps", -5),
        ("plausibility_basis", ""),
        ("plausibility_basis", " "),
    ],
)
def test_v2_power_fields_are_strictly_validated(
    v2_payload: dict[str, object], key: str, value: object
) -> None:
    _v2_power(v2_payload)[key] = value

    with pytest.raises(ValueError):
        HypothesisDocument.from_json(_json(v2_payload))


@pytest.mark.parametrize("key", sorted(_power()))
def test_v2_power_block_keys_are_exact(v2_payload: dict[str, object], key: str) -> None:
    power = _v2_power(v2_payload)
    del power[key]
    with pytest.raises(ValueError, match="power keys must be exactly"):
        HypothesisDocument.from_json(_json(v2_payload))
    power[key] = _power()[key]
    power["extra"] = 1
    with pytest.raises(ValueError, match="power keys must be exactly"):
        HypothesisDocument.from_json(_json(v2_payload))


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_v2_power_rejects_non_finite_json_numbers(v2_payload: dict[str, object], bad: str) -> None:
    text = _json(v2_payload).replace('"per_event_sd_bps":100.0', f'"per_event_sd_bps":{bad}')

    with pytest.raises(ValueError, match="invalid hypothesis document JSON"):
        HypothesisDocument.from_json(text)


def test_v2_mde_must_match_the_recomputation_within_half_a_basis_point(
    v2_payload: dict[str, object],
) -> None:
    exact = minimum_detectable_effect_bps(100, 100.0, 0.05, 0.8, "one")
    assert exact == pytest.approx(24.8648, abs=1e-3)
    power = _v2_power(v2_payload)
    for declared in (exact - 0.49, exact + 0.49, exact):
        power["minimum_detectable_effect_bps"] = declared
        HypothesisDocument.from_json(_json(v2_payload))
    for declared in (exact - 0.51, exact + 0.51, 2 * exact):
        power["minimum_detectable_effect_bps"] = declared
        with pytest.raises(ValueError, match="does not match the recomputed"):
            HypothesisDocument.from_json(_json(v2_payload))


def test_v2_one_and_two_sided_mdes_differ_and_each_is_enforced(
    v2_payload: dict[str, object],
) -> None:
    one = minimum_detectable_effect_bps(100, 100.0, 0.05, 0.8, "one")
    two = minimum_detectable_effect_bps(100, 100.0, 0.05, 0.8, "two")
    assert two > one + 3
    power = _v2_power(v2_payload)
    power["sided"] = "two"
    with pytest.raises(ValueError, match="does not match the recomputed"):
        HypothesisDocument.from_json(_json(v2_payload))
    power["minimum_detectable_effect_bps"] = round(two, 2)
    document = HypothesisDocument.from_json(_json(v2_payload))
    assert document.power is not None and document.power.sided == "two"


def test_minimum_detectable_effect_reproduces_the_h0008_numbers() -> None:
    assert minimum_detectable_effect_bps(107, 194.0, 0.05, 0.8, "one") == pytest.approx(
        46.6, abs=0.1
    )


@pytest.mark.parametrize(
    ("events", "sd", "alpha", "power", "sided"),
    [
        (0, 100.0, 0.05, 0.8, "one"),
        (100, 0.0, 0.05, 0.8, "one"),
        (100, float("inf"), 0.05, 0.8, "one"),
        (100, 100.0, 0.0, 0.8, "one"),
        (100, 100.0, 0.6, 0.8, "one"),
        (100, 100.0, 0.05, 0.4, "one"),
        (100, 100.0, 0.05, 0.8, "three"),
    ],
)
def test_minimum_detectable_effect_refuses_invalid_inputs(
    events: int, sd: float, alpha: float, power: float, sided: str
) -> None:
    with pytest.raises(ValueError):
        minimum_detectable_effect_bps(events, sd, alpha, power, sided)


def test_v2_expected_events_must_cover_minimum_evidence_trades(
    v2_payload: dict[str, object],
) -> None:
    v2_payload["minimum_evidence"] = {"sessions": 20, "trades": 100}
    HypothesisDocument.from_json(_json(v2_payload))
    v2_payload["minimum_evidence"] = {"sessions": 20, "trades": 101}

    with pytest.raises(ValueError, match="expected_events must be at least"):
        HypothesisDocument.from_json(_json(v2_payload))


def test_v2_direct_construction_requires_a_power_design(v2_payload: dict[str, object]) -> None:
    document = HypothesisDocument.from_json(_json(v2_payload))

    with pytest.raises(ValueError, match="requires a power block"):
        replace(document, power=None)
    with pytest.raises(ValueError, match="must not carry a power block"):
        replace(document, contract="research-hypothesis-v1")


def test_v2_integer_and_float_bps_values_keep_their_written_types(
    v2_payload: dict[str, object],
) -> None:
    power = _v2_power(v2_payload)
    power.update(
        {"per_event_sd_bps": 100, "minimum_detectable_effect_bps": 25, "plausible_effect_bps": 30}
    )
    canonical = json.dumps(v2_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    document = HypothesisDocument.from_json(canonical)

    assert document.to_json() == canonical
    assert '"per_event_sd_bps":100,' in canonical
    assert document.sha256 == hashlib.sha256(canonical.encode()).hexdigest()
    power["per_event_sd_bps"] = 100.0
    as_float = json.dumps(v2_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert HypothesisDocument.from_json(as_float).to_json() == as_float != canonical


def test_minimum_detectable_effect_refuses_a_zero_effect() -> None:
    with pytest.raises(ValueError, match="zero minimum detectable effect"):
        minimum_detectable_effect_bps(100, 100.0, 0.5, 0.5, "one")
