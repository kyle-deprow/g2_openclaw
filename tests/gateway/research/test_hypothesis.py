"""Strict synthetic tests for the pure scientific hypothesis document."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from typing import cast

import pytest
from gateway.research.hypothesis import (
    ComputeLimits,
    DateRange,
    FeatureDeclaration,
    HypothesisDocument,
    MinimumEvidence,
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
        (("compute", "max_rss_mb"), 8193),
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
        (7_201, "compute.max_wall_seconds is outside the supported bounds"),
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
    changed["compute"]["max_wall_seconds"] = 7_200  # type: ignore[index]

    document = HypothesisDocument.from_json(_json(changed))

    assert document.compute.max_wall_seconds == 7_200.0


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
