"""Solver-free bounded surrogate campaign and FEM-escalation tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from comsol_mcp.durable.canonical import canonical_sha256_v1
from comsol_mcp.surrogate.campaign import (
    MAX_FEM_ESCALATIONS,
    assert_no_prediction_promotion,
    build_campaign_spec,
    build_screening_record,
    rank_candidates,
    record_fem_result,
    summarize_campaign,
    validate_campaign_spec,
    validate_screening_record,
)
from development_kit.scripts import surrogate_campaign_licensed_gate as licensed_gate

SOLVER_FREE_BANNED = (
    "import mph",
    "from mph",
    "import jpype",
    "from jpype",
    "import comsol",
    "from comsol.",
    "import numpy",
    "import torch",
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def test_licensed_gate_hashes_the_saved_fem_model_file(tmp_path: Path) -> None:
    class FakeModel:
        def save(self, path: str) -> None:
            Path(path).write_bytes(b"reviewable COMSOL model artifact")

    target = tmp_path / "fem-results" / "candidate.mph"
    digest = licensed_gate._save_fem_artifact(FakeModel(), target)
    assert target.read_bytes() == b"reviewable COMSOL model artifact"
    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()


def test_licensed_gate_rejects_missing_fem_model_artifact(tmp_path: Path) -> None:
    class EmptyModel:
        def save(self, path: str) -> None:
            Path(path).touch()

    with pytest.raises(RuntimeError, match="durable FEM model artifact"):
        licensed_gate._save_fem_artifact(EmptyModel(), tmp_path / "empty.mph")


def _spec(**overrides) -> dict:
    kwargs = {
        "campaign_id": "campaign-1",
        "candidate_count": 10,
        "top_k": 3,
        "maximum_fem_escalations": 5,
        "wall_time_budget_seconds": 3600,
        "objective": "maximize_R",
        "maximize": True,
        "surrogate_registry_entry_sha256": SHA_A,
        "dataset_manifest_sha256": SHA_B,
    }
    kwargs.update(overrides)
    return build_campaign_spec(**kwargs)


def _record(candidate_id: str, value: float, ood: str = "in_domain") -> dict:
    return build_screening_record(
        candidate_id=candidate_id,
        features={"w": 1.0, "h": 2.0},
        predicted_objective=value,
        ood_state=ood,
        surrogate_registry_entry_sha256=SHA_A,
    )


# --------------------------------------------------------------------------
# Campaign spec
# --------------------------------------------------------------------------


def test_spec_is_bounded_and_forbids_surrogate_evidence() -> None:
    spec = _spec()
    assert spec["require_fem_for_selected"] is True
    assert spec["surrogate_prediction_is_evidence"] is False
    validate_campaign_spec(spec)


def test_spec_refuses_to_accept_surrogate_only_results() -> None:
    with pytest.raises(ValueError, match="require_fem_for_selected must be true"):
        _spec(require_fem_for_selected=False)


def test_spec_bounds_are_enforced() -> None:
    with pytest.raises(ValueError, match="candidate_count"):
        _spec(candidate_count=0)
    with pytest.raises(ValueError, match="top_k must not exceed"):
        _spec(top_k=11)
    with pytest.raises(ValueError, match="maximum_fem_escalations"):
        _spec(maximum_fem_escalations=MAX_FEM_ESCALATIONS + 1)
    with pytest.raises(ValueError, match="wall_time_budget_seconds"):
        _spec(wall_time_budget_seconds=0)


def test_escalation_budget_must_cover_the_selection() -> None:
    with pytest.raises(ValueError, match="must cover every selected candidate"):
        _spec(top_k=5, maximum_fem_escalations=2)


def test_spec_is_sealed_and_reproducible() -> None:
    spec = _spec()
    assert len(spec["spec_sha256"]) == 64
    tampered = dict(spec)
    tampered["top_k"] = 9
    with pytest.raises(ValueError):
        validate_campaign_spec(tampered)


def test_spec_cannot_be_resealed_to_allow_surrogate_evidence() -> None:
    spec = _spec()
    body = {k: v for k, v in spec.items() if k != "spec_sha256"}
    body["require_fem_for_selected"] = False
    resealed = {**body, "spec_sha256": canonical_sha256_v1(body)}
    with pytest.raises(ValueError, match="must require FEM"):
        validate_campaign_spec(resealed)


# --------------------------------------------------------------------------
# Screening records
# --------------------------------------------------------------------------


def test_screening_record_is_always_a_prediction() -> None:
    record = _record("c1", 0.5)
    assert record["state"] == "predicted"
    assert record["is_fem_evidence"] is False
    assert record["upgrades_fem_evidence"] is False
    assert record["requires_fresh_fem"] is True
    validate_screening_record(record)


def test_screening_record_cannot_be_resealed_as_verified() -> None:
    record = _record("c1", 0.5)
    body = {k: v for k, v in record.items() if k != "record_sha256"}
    body["state"] = "verified"
    body["is_fem_evidence"] = True
    resealed = {**body, "record_sha256": canonical_sha256_v1(body)}
    with pytest.raises(ValueError, match="always in the predicted state"):
        validate_screening_record(resealed)


def test_screening_record_cannot_claim_fem_evidence_flag_alone() -> None:
    record = _record("c1", 0.5)
    body = {k: v for k, v in record.items() if k != "record_sha256"}
    body["is_fem_evidence"] = True
    resealed = {**body, "record_sha256": canonical_sha256_v1(body)}
    with pytest.raises(ValueError, match="never FEM evidence"):
        validate_screening_record(resealed)


def test_screening_record_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="unknown ood_state"):
        _record("c1", 0.5, ood="probably_fine")
    with pytest.raises(ValueError, match="non-empty mapping"):
        build_screening_record(
            candidate_id="c1",
            features={},
            predicted_objective=0.5,
            ood_state="in_domain",
            surrogate_registry_entry_sha256=SHA_A,
        )
    with pytest.raises(ValueError, match="finite"):
        _record("c1", float("inf"))


# --------------------------------------------------------------------------
# Ranking and escalation
# --------------------------------------------------------------------------


def test_ranking_selects_top_k_when_maximizing() -> None:
    records = [_record(f"c{i}", float(i)) for i in range(5)]
    report = rank_candidates(records, maximize=True, top_k=2)
    assert report["selected_candidate_ids"] == ["c4", "c3"]
    assert report["ranking_is_evidence"] is False
    assert report["selection_is_evidence"] is False


def test_ranking_reverses_when_minimizing() -> None:
    records = [_record(f"c{i}", float(i)) for i in range(5)]
    report = rank_candidates(records, maximize=False, top_k=2)
    assert report["selected_candidate_ids"] == ["c0", "c1"]


def test_out_of_domain_candidates_are_always_escalated() -> None:
    records = [
        _record("best", 10.0),
        _record("mid", 5.0),
        _record("ood_low", 0.1, ood="out_of_domain"),
        _record("uncal", 0.2, ood="uncalibrated"),
    ]
    report = rank_candidates(records, maximize=True, top_k=2)
    assert report["selected_candidate_ids"] == ["best", "mid"]
    # The untrustworthy candidates are escalated even though they did not rank.
    assert report["forced_candidate_ids"] == ["ood_low", "uncal"]
    reasons = {item["candidate_id"]: item["reason"] for item in report["escalation_plan"]}
    assert reasons["ood_low"] == "out_of_domain"
    assert reasons["uncal"] == "uncalibrated"
    assert report["escalation_count"] == 4


def test_edge_candidates_are_escalated_with_the_edge_reason() -> None:
    report = rank_candidates([_record("c1", 1.0, ood="edge")], maximize=True, top_k=1)
    assert report["escalation_plan"][0]["reason"] == "edge_of_domain"


def test_ranking_rejects_duplicates_and_empty_input() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        rank_candidates([], maximize=True, top_k=1)
    with pytest.raises(ValueError, match="unique"):
        rank_candidates([_record("c1", 1.0), _record("c1", 2.0)], maximize=True, top_k=1)


# --------------------------------------------------------------------------
# FEM results
# --------------------------------------------------------------------------


def test_verified_fem_result_promotes_the_candidate() -> None:
    result = record_fem_result(
        candidate_id="c1",
        predicted_objective=1.0,
        measured_objective=1.0 + 1e-9,
        fem_evidence_state="verified",
        fem_artifact_sha256=SHA_C,
        absolute_tolerance=1e-6,
    )
    assert result["state"] == "verified"
    assert result["is_fem_evidence"] is True
    assert result["disagreement"]["agrees_with_prediction"] is True
    # The prediction is not what promotes it; the FEM run is.
    assert result["promotes_prediction"] is False


def test_failed_fem_run_leaves_the_candidate_unverified() -> None:
    result = record_fem_result(
        candidate_id="c1",
        predicted_objective=1.0,
        measured_objective=None,
        fem_evidence_state="failed",
        fem_artifact_sha256=None,
        absolute_tolerance=1e-6,
    )
    assert result["state"] == "failed"
    assert result["is_fem_evidence"] is False
    assert result["reason_code"] == "fem_failed"


def test_unknown_fem_evidence_leaves_the_candidate_unverified() -> None:
    result = record_fem_result(
        candidate_id="c1",
        predicted_objective=1.0,
        measured_objective=None,
        fem_evidence_state="unknown",
        fem_artifact_sha256=None,
        absolute_tolerance=1e-6,
    )
    assert result["state"] == "unverified"
    assert result["is_fem_evidence"] is False


@pytest.mark.parametrize("evidence_state", ["failed", "unknown", "not_requested"])
def test_unverified_fem_result_preserves_record_integrity(evidence_state: str) -> None:
    result = record_fem_result(
        candidate_id="c1",
        predicted_objective=1.0,
        measured_objective=None,
        fem_evidence_state=evidence_state,
        fem_artifact_sha256=None,
        absolute_tolerance=1e-6,
    )
    digest = result.pop("result_sha256")
    assert digest == canonical_sha256_v1(result)
    assert result["is_fem_evidence"] is False
    result["candidate_id"] = "c2"
    assert digest != canonical_sha256_v1(result)


def test_disagreement_is_recorded_not_hidden() -> None:
    result = record_fem_result(
        candidate_id="c1",
        predicted_objective=1.0,
        measured_objective=5.0,
        fem_evidence_state="verified",
        fem_artifact_sha256=SHA_C,
        absolute_tolerance=1e-6,
    )
    assert result["state"] == "verified"  # the FEM result is valid evidence
    assert result["disagreement"]["agrees_with_prediction"] is False
    assert result["disagreement"]["absolute_difference"] == pytest.approx(4.0)


def test_verified_fem_result_requires_measured_value_and_artifact() -> None:
    with pytest.raises(ValueError, match="measured objective"):
        record_fem_result(
            candidate_id="c1",
            predicted_objective=1.0,
            measured_objective=None,
            fem_evidence_state="verified",
            fem_artifact_sha256=SHA_C,
            absolute_tolerance=1e-6,
        )
    with pytest.raises(ValueError, match="artifact hash"):
        record_fem_result(
            candidate_id="c1",
            predicted_objective=1.0,
            measured_objective=1.0,
            fem_evidence_state="verified",
            fem_artifact_sha256=None,
            absolute_tolerance=1e-6,
        )


# --------------------------------------------------------------------------
# Summary and the core invariant
# --------------------------------------------------------------------------


def test_summary_separates_predictions_from_evidence() -> None:
    spec = _spec()
    records = [_record(f"c{i}", float(i)) for i in range(4)]
    fem_results = [
        record_fem_result(
            candidate_id="c3",
            predicted_objective=3.0,
            measured_objective=3.1,
            fem_evidence_state="verified",
            fem_artifact_sha256=SHA_C,
            absolute_tolerance=1e-6,
        ),
        record_fem_result(
            candidate_id="c2",
            predicted_objective=2.0,
            measured_objective=None,
            fem_evidence_state="failed",
            fem_artifact_sha256=None,
            absolute_tolerance=1e-6,
        ),
    ]
    summary = summarize_campaign(
        spec=spec,
        screening_records=records,
        fem_results=fem_results,
        fem_escalations_used=2,
        wall_time_seconds=100.0,
    )
    assert summary["screened_count"] == 4
    assert summary["verified_count"] == 1
    assert summary["predicted_only_count"] == 3
    assert summary["worst_absolute_error"] == pytest.approx(0.1)
    assert summary["error_metrics_source"] == "fresh_fem_comparison"
    assert summary["predictions_are_not_evidence"] is True
    assert summary["verified_requires_fresh_fem"] is True
    assert summary["upgrades_fem_evidence"] is False


def test_summary_counts_disagreements() -> None:
    summary = summarize_campaign(
        spec=_spec(),
        screening_records=[_record("c1", 1.0)],
        fem_results=[
            record_fem_result(
                candidate_id="c1",
                predicted_objective=1.0,
                measured_objective=9.0,
                fem_evidence_state="verified",
                fem_artifact_sha256=SHA_C,
                absolute_tolerance=1e-6,
            )
        ],
        fem_escalations_used=1,
        wall_time_seconds=10.0,
    )
    assert summary["disagreement_count"] == 1


def test_summary_reports_no_metrics_without_verified_fem() -> None:
    summary = summarize_campaign(
        spec=_spec(),
        screening_records=[_record("c1", 1.0)],
        fem_results=[],
        fem_escalations_used=0,
        wall_time_seconds=10.0,
    )
    assert summary["verified_count"] == 0
    assert summary["worst_absolute_error"] is None
    assert summary["error_metrics_source"] == "no_verified_fem_results"


def test_summary_rejects_duplicate_or_unscreened_fem_results() -> None:
    result = record_fem_result(
        candidate_id="c1",
        predicted_objective=1.0,
        measured_objective=None,
        fem_evidence_state="failed",
        fem_artifact_sha256=None,
        absolute_tolerance=1e-6,
    )
    with pytest.raises(ValueError, match="duplicate FEM result"):
        summarize_campaign(
            spec=_spec(),
            screening_records=[_record("c1", 1.0)],
            fem_results=[result, result],
            fem_escalations_used=2,
            wall_time_seconds=1.0,
        )
    unknown = {**result, "candidate_id": "missing"}
    with pytest.raises(ValueError, match="unscreened candidate"):
        summarize_campaign(
            spec=_spec(),
            screening_records=[_record("c1", 1.0)],
            fem_results=[unknown],
            fem_escalations_used=1,
            wall_time_seconds=1.0,
        )


def test_summary_enforces_the_escalation_and_time_budgets() -> None:
    with pytest.raises(ValueError, match="exceeds the declared budget"):
        summarize_campaign(
            spec=_spec(maximum_fem_escalations=1, top_k=1),
            screening_records=[_record("c1", 1.0)],
            fem_results=[],
            fem_escalations_used=2,
            wall_time_seconds=1.0,
        )
    summary = summarize_campaign(
        spec=_spec(),
        screening_records=[_record("c1", 1.0)],
        fem_results=[],
        fem_escalations_used=0,
        wall_time_seconds=99999.0,
    )
    assert summary["within_wall_time_budget"] is False


def test_summary_rejects_a_fem_result_claiming_predicted_state() -> None:
    with pytest.raises(ValueError, match="never be in the predicted state"):
        summarize_campaign(
            spec=_spec(),
            screening_records=[_record("c1", 1.0)],
            fem_results=[{"candidate_id": "c1", "state": "predicted"}],
            fem_escalations_used=1,
            wall_time_seconds=1.0,
        )


def test_no_prediction_promotion_is_provable() -> None:
    records = [_record("c1", 1.0), _record("c2", 2.0)]
    fem_results = [
        record_fem_result(
            candidate_id="c1",
            predicted_objective=1.0,
            measured_objective=1.0,
            fem_evidence_state="verified",
            fem_artifact_sha256=SHA_C,
            absolute_tolerance=1e-6,
        )
    ]
    report = assert_no_prediction_promotion(records, fem_results)
    assert report["no_unverified_promotion"] is True
    assert report["verified_candidate_ids"] == ["c1"]
    assert report["predictions_are_not_evidence"] is True


def test_promotion_without_verified_fem_is_detected() -> None:
    records = [_record("c1", 1.0)]
    # A forged result that claims verified state without FEM evidence.
    forged = {
        "candidate_id": "c1",
        "state": "verified",
        "fem_evidence_state": "verified",
        "fem_artifact_sha256": None,
        "is_fem_evidence": True,
    }
    with pytest.raises(ValueError, match="without a FEM artifact hash"):
        assert_no_prediction_promotion(records, [forged])
    forged_no_flag = {**forged, "fem_artifact_sha256": SHA_C, "is_fem_evidence": False}
    with pytest.raises(ValueError, match="without FEM evidence flag"):
        assert_no_prediction_promotion(records, [forged_no_flag])
    wrong_state = {**forged, "fem_artifact_sha256": SHA_C, "fem_evidence_state": "unknown"}
    with pytest.raises(ValueError, match="without verified FEM evidence"):
        assert_no_prediction_promotion(records, [wrong_state])


def test_fem_result_for_unscreened_candidate_is_refused() -> None:
    with pytest.raises(ValueError, match="unscreened candidate"):
        assert_no_prediction_promotion(
            [_record("c1", 1.0)],
            [
                {
                    "candidate_id": "ghost",
                    "state": "verified",
                    "fem_evidence_state": "verified",
                    "fem_artifact_sha256": SHA_C,
                    "is_fem_evidence": True,
                }
            ],
        )


# --------------------------------------------------------------------------
# Solver-free guard
# --------------------------------------------------------------------------


def test_campaign_module_is_solver_free() -> None:
    import comsol_mcp.surrogate.campaign as module

    source = open(module.__file__, encoding="utf-8").read().lower()
    for banned in SOLVER_FREE_BANNED:
        assert banned not in source, f"campaign references {banned}"


def test_forced_escalation_is_by_design_and_plan_budget_bounded() -> None:
    """L-02 (c081k): forced escalation of uncalibrated candidates is
    ordinary-input reachable BY DESIGN (never trust an uncalibrated
    prediction), the declared budget stays authoritative, and a supplied
    plan budget refuses fail-closed at plan time instead of at summary
    time."""
    records = [
        _record("trusted", 0.9, ood="in_domain"),
        _record("untrusted", 0.1, ood="uncalibrated"),
    ]
    report = rank_candidates(records, maximize=True, top_k=1)
    assert report["escalation_count"] == 2
    assert [item["reason"] for item in report["escalation_plan"]] == [
        "selected_by_surrogate",
        "uncalibrated",
    ]
    # Within the declared budget the plan stands.
    assert (
        rank_candidates(records, maximize=True, top_k=1, maximum_fem_escalations=2)[
            "escalation_count"
        ]
        == 2
    )
    # Over the declared budget the plan itself refuses fail-closed.
    with pytest.raises(ValueError, match="exceeds the declared budget"):
        rank_candidates(records, maximize=True, top_k=1, maximum_fem_escalations=1)
