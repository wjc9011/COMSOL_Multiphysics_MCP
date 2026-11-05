"""Solver-free bounded surrogate campaign with mandatory FEM escalation.

This module never imports COMSOL, Java, MPh, or network clients.  It plans and
adjudicates a bounded screening campaign in which a surrogate ranks candidates
and only a fresh FEM run may promote a candidate to verified evidence.

The central invariant is enforced structurally rather than by convention: a
surrogate prediction is produced in the `predicted` state, and the only path to
`verified` is a fresh FEM result that passes its own gates.  A campaign
therefore cannot report a surrogate prediction as a measurement.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from comsol_mcp.durable.canonical import canonical_sha256_v1

SCHEMA_VERSION = "1.0.0"
STRATEGY_VERSION = "surrogate_screen_then_fem_v1"

# A candidate's evidence state.  Only `verified` is FEM evidence.
CANDIDATE_STATES = ("predicted", "verified", "refuted", "failed", "unverified")

# Reasons a candidate must be escalated to a fresh FEM run.
ESCALATION_REASONS = (
    "selected_by_surrogate",
    "out_of_domain",
    "edge_of_domain",
    "uncalibrated",
    "disagreement_with_prediction",
    "caller_requested",
    "baseline_comparison",
)

# Bounds.  A campaign is always bounded, so an autonomous loop cannot run away.
MAX_CANDIDATES = 4096
MAX_FEM_ESCALATIONS = 64
MAX_TOP_K = 64
MAX_WALL_TIME_SECONDS = 86400


def _require_str(name: str, value: Any, *, max_len: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ValueError(f"{name} must be a non-empty string up to {max_len}")
    return value


def _require_hex64(name: str, value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise ValueError(f"{name} must be a 64-character hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be a 64-character hex digest") from exc
    return value.lower()


def _require_int(name: str, value: Any, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be in {minimum}..{maximum}")
    return value


def _require_finite(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def build_campaign_spec(
    *,
    campaign_id: str,
    candidate_count: int,
    top_k: int,
    maximum_fem_escalations: int,
    wall_time_budget_seconds: int,
    objective: str,
    maximize: bool,
    surrogate_registry_entry_sha256: str,
    dataset_manifest_sha256: str,
    require_fem_for_selected: bool = True,
) -> dict[str, Any]:
    """Declare one bounded screening campaign.

    ``require_fem_for_selected`` must stay true: a campaign that could accept a
    surrogate-only result would be able to report a prediction as evidence.
    """
    candidate_count = _require_int(
        "candidate_count", candidate_count, minimum=1, maximum=MAX_CANDIDATES
    )
    top_k = _require_int("top_k", top_k, minimum=1, maximum=MAX_TOP_K)
    if top_k > candidate_count:
        raise ValueError("top_k must not exceed candidate_count")
    maximum_fem_escalations = _require_int(
        "maximum_fem_escalations",
        maximum_fem_escalations,
        minimum=0,
        maximum=MAX_FEM_ESCALATIONS,
    )
    wall_time_budget_seconds = _require_int(
        "wall_time_budget_seconds",
        wall_time_budget_seconds,
        minimum=1,
        maximum=MAX_WALL_TIME_SECONDS,
    )
    if require_fem_for_selected is not True:
        raise ValueError(
            "require_fem_for_selected must be true: a surrogate prediction is never evidence"
        )
    if maximum_fem_escalations < top_k:
        raise ValueError("maximum_fem_escalations must cover every selected candidate")

    body = {
        "schema": "comsol_mcp.surrogate_campaign_spec",
        "schema_version": SCHEMA_VERSION,
        "strategy": STRATEGY_VERSION,
        "campaign_id": _require_str("campaign_id", campaign_id),
        "candidate_count": candidate_count,
        "top_k": top_k,
        "maximum_fem_escalations": maximum_fem_escalations,
        "wall_time_budget_seconds": wall_time_budget_seconds,
        "objective": _require_str("objective", objective),
        "maximize": bool(maximize),
        "surrogate_registry_entry_sha256": _require_hex64(
            "surrogate_registry_entry_sha256", surrogate_registry_entry_sha256
        ),
        "dataset_manifest_sha256": _require_hex64(
            "dataset_manifest_sha256", dataset_manifest_sha256
        ),
        "require_fem_for_selected": True,
        "surrogate_prediction_is_evidence": False,
    }
    return {**body, "spec_sha256": canonical_sha256_v1(body)}


def validate_campaign_spec(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("campaign spec must be a mapping")
    required = (
        "schema",
        "schema_version",
        "strategy",
        "campaign_id",
        "candidate_count",
        "top_k",
        "maximum_fem_escalations",
        "wall_time_budget_seconds",
        "objective",
        "maximize",
        "surrogate_registry_entry_sha256",
        "dataset_manifest_sha256",
        "require_fem_for_selected",
        "surrogate_prediction_is_evidence",
    )
    if set(value) != set(required) | {"spec_sha256"}:
        raise ValueError("campaign spec keys are closed")
    if value["schema"] != "comsol_mcp.surrogate_campaign_spec":
        raise ValueError("unexpected schema name")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    if value["strategy"] != STRATEGY_VERSION:
        raise ValueError("unsupported campaign strategy")
    body = {key: value[key] for key in required}
    if value["spec_sha256"] != canonical_sha256_v1(body):
        raise ValueError("spec_sha256 mismatch")
    if value["require_fem_for_selected"] is not True:
        raise ValueError("a campaign must require FEM for every selected candidate")
    if value["surrogate_prediction_is_evidence"] is not False:
        raise ValueError("a surrogate prediction is never evidence")
    rebuilt = build_campaign_spec(
        campaign_id=value["campaign_id"],
        candidate_count=value["candidate_count"],
        top_k=value["top_k"],
        maximum_fem_escalations=value["maximum_fem_escalations"],
        wall_time_budget_seconds=value["wall_time_budget_seconds"],
        objective=value["objective"],
        maximize=value["maximize"],
        surrogate_registry_entry_sha256=value["surrogate_registry_entry_sha256"],
        dataset_manifest_sha256=value["dataset_manifest_sha256"],
    )
    if rebuilt != value:
        raise ValueError("campaign spec is not reproducible from its declared inputs")
    return dict(value)


def build_screening_record(
    *,
    candidate_id: str,
    features: Mapping[str, float],
    predicted_objective: float,
    ood_state: str,
    surrogate_registry_entry_sha256: str,
    prediction_uncertainty: float | None = None,
) -> dict[str, Any]:
    """Record one surrogate screening result in the `predicted` state.

    The record explicitly denies FEM evidence, so it can never be mistaken for a
    measurement even after being persisted or reported.
    """
    if ood_state not in ("in_domain", "edge", "out_of_domain", "uncalibrated"):
        raise ValueError("unknown ood_state")
    if not isinstance(features, Mapping) or not features:
        raise ValueError("features must be a non-empty mapping")
    normalized = {
        _require_str("feature name", key, max_len=64): _require_finite(f"features.{key}", value)
        for key, value in features.items()
    }
    uncertainty = None
    if prediction_uncertainty is not None:
        uncertainty = _require_finite("prediction_uncertainty", prediction_uncertainty)
        if uncertainty < 0.0:
            raise ValueError("prediction_uncertainty must be non-negative")

    body = {
        "schema": "comsol_mcp.surrogate_screening_record",
        "schema_version": SCHEMA_VERSION,
        "candidate_id": _require_str("candidate_id", candidate_id),
        "features": dict(sorted(normalized.items())),
        "predicted_objective": _require_finite("predicted_objective", predicted_objective),
        "prediction_uncertainty": uncertainty,
        "ood_state": ood_state,
        "surrogate_registry_entry_sha256": _require_hex64(
            "surrogate_registry_entry_sha256", surrogate_registry_entry_sha256
        ),
        "state": "predicted",
        "is_fem_evidence": False,
        "upgrades_fem_evidence": False,
        "requires_fresh_fem": True,
    }
    return {**body, "record_sha256": canonical_sha256_v1(body)}


def validate_screening_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("screening record must be a mapping")
    required = (
        "schema",
        "schema_version",
        "candidate_id",
        "features",
        "predicted_objective",
        "prediction_uncertainty",
        "ood_state",
        "surrogate_registry_entry_sha256",
        "state",
        "is_fem_evidence",
        "upgrades_fem_evidence",
        "requires_fresh_fem",
    )
    if set(value) != set(required) | {"record_sha256"}:
        raise ValueError("screening record keys are closed")
    if value["schema"] != "comsol_mcp.surrogate_screening_record":
        raise ValueError("unexpected schema name")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    body = {key: value[key] for key in required}
    if value["record_sha256"] != canonical_sha256_v1(body):
        raise ValueError("record_sha256 mismatch")
    # A screening record may only ever be a prediction.
    if value["state"] != "predicted":
        raise ValueError("a screening record is always in the predicted state")
    if value["is_fem_evidence"] is not False:
        raise ValueError("a screening record is never FEM evidence")
    if value["upgrades_fem_evidence"] is not False:
        raise ValueError("a screening record never upgrades FEM evidence")
    return dict(value)


def rank_candidates(
    records: Sequence[Mapping[str, Any]],
    *,
    maximize: bool,
    top_k: int,
    maximum_fem_escalations: int | None = None,
) -> dict[str, Any]:
    """Rank screened candidates and select the bounded escalation set.

    Out-of-domain and uncalibrated candidates are always escalated regardless of
    rank, because their prediction is not trustworthy enough to skip a FEM run.
    That forced escalation is ordinary-input reachable by design; the campaign
    budget stays authoritative. When maximum_fem_escalations is supplied the
    plan itself refuses fail-closed instead of letting a caller discover the
    overspend only at summary time.
    """
    top_k = _require_int("top_k", top_k, minimum=1, maximum=MAX_TOP_K)
    if maximum_fem_escalations is not None:
        maximum_fem_escalations = _require_int(
            "maximum_fem_escalations",
            maximum_fem_escalations,
            minimum=1,
            maximum=MAX_FEM_ESCALATIONS,
        )
    validated = [validate_screening_record(record) for record in records]
    if not validated:
        raise ValueError("records must be non-empty")
    identifiers = [record["candidate_id"] for record in validated]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("candidate_id values must be unique")

    ordered = sorted(
        validated,
        key=lambda record: (
            -record["predicted_objective"] if maximize else record["predicted_objective"],
            record["candidate_id"],
        ),
    )
    selected = ordered[:top_k]

    # A candidate whose prediction cannot be trusted must be verified, even if
    # it did not rank in the top k.
    forced = [
        record
        for record in validated
        if record["ood_state"] in ("out_of_domain", "uncalibrated") and record not in selected
    ]
    escalation: list[dict[str, Any]] = []
    for record in selected:
        reason = (
            "out_of_domain"
            if record["ood_state"] == "out_of_domain"
            else "uncalibrated"
            if record["ood_state"] == "uncalibrated"
            else "edge_of_domain"
            if record["ood_state"] == "edge"
            else "selected_by_surrogate"
        )
        escalation.append({"candidate_id": record["candidate_id"], "reason": reason})
    for record in forced:
        escalation.append(
            {
                "candidate_id": record["candidate_id"],
                "reason": "out_of_domain"
                if record["ood_state"] == "out_of_domain"
                else "uncalibrated",
            }
        )

    if maximum_fem_escalations is not None and len(escalation) > maximum_fem_escalations:
        raise ValueError(
            "escalation plan exceeds the declared budget: "
            f"{len(escalation)} escalations (forced uncalibrated or "
            "out-of-domain candidates included) against "
            f"maximum_fem_escalations={maximum_fem_escalations}; widen the "
            "declared budget or calibrate the surrogate before planning"
        )
    return {
        "ranked_candidate_ids": [record["candidate_id"] for record in ordered],
        "selected_candidate_ids": [record["candidate_id"] for record in selected],
        "forced_candidate_ids": [record["candidate_id"] for record in forced],
        "escalation_plan": escalation,
        "escalation_count": len(escalation),
        "ranking_is_evidence": False,
        "selection_is_evidence": False,
    }


def record_fem_result(
    *,
    candidate_id: str,
    predicted_objective: float,
    measured_objective: float | None,
    fem_evidence_state: str,
    fem_artifact_sha256: str | None,
    absolute_tolerance: float,
    relative_tolerance: float = 0.0,
) -> dict[str, Any]:
    """Record a fresh FEM result and compare it with the prediction.

    Only a FEM run whose own evidence state is `verified` yields a `verified`
    candidate.  A failed or unverified FEM run leaves the candidate unverified,
    and a material disagreement with the prediction is recorded as model-error
    evidence rather than being smoothed away.
    """
    if fem_evidence_state not in ("verified", "failed", "unknown", "not_requested"):
        raise ValueError("unknown fem_evidence_state")
    absolute_tolerance = _require_finite("absolute_tolerance", absolute_tolerance)
    relative_tolerance = _require_finite("relative_tolerance", relative_tolerance)
    if absolute_tolerance < 0.0 or relative_tolerance < 0.0:
        raise ValueError("tolerances must be non-negative")
    predicted_objective = _require_finite("predicted_objective", predicted_objective)

    if fem_evidence_state != "verified":
        unverified_body = {
            "schema": "comsol_mcp.surrogate_fem_result",
            "schema_version": SCHEMA_VERSION,
            "candidate_id": _require_str("candidate_id", candidate_id),
            "predicted_objective": predicted_objective,
            "measured_objective": None,
            "fem_evidence_state": fem_evidence_state,
            "fem_artifact_sha256": None,
            "state": "failed" if fem_evidence_state == "failed" else "unverified",
            "disagreement": None,
            "is_fem_evidence": False,
            "promotes_prediction": False,
            "reason_code": f"fem_{fem_evidence_state}",
        }
        return {**unverified_body, "result_sha256": canonical_sha256_v1(unverified_body)}

    if measured_objective is None:
        raise ValueError("a verified FEM result requires a measured objective")
    if fem_artifact_sha256 is None:
        raise ValueError("a verified FEM result requires an artifact hash")
    measured_objective = _require_finite("measured_objective", measured_objective)
    artifact_sha256 = _require_hex64("fem_artifact_sha256", fem_artifact_sha256)

    difference = abs(measured_objective - predicted_objective)
    within_absolute = difference <= absolute_tolerance
    within_relative = predicted_objective != 0.0 and difference <= relative_tolerance * abs(
        predicted_objective
    )
    agrees = within_absolute or within_relative
    relative_difference = (
        difference / abs(predicted_objective) if predicted_objective != 0.0 else None
    )
    body = {
        "schema": "comsol_mcp.surrogate_fem_result",
        "schema_version": SCHEMA_VERSION,
        "candidate_id": _require_str("candidate_id", candidate_id),
        "predicted_objective": predicted_objective,
        "measured_objective": measured_objective,
        "fem_evidence_state": "verified",
        "fem_artifact_sha256": artifact_sha256,
        # A verified FEM run promotes the candidate to verified evidence.
        "state": "verified",
        "disagreement": {
            "absolute_difference": difference,
            "relative_difference": relative_difference,
            "agrees_with_prediction": agrees,
            "absolute_tolerance": absolute_tolerance,
            "relative_tolerance": relative_tolerance,
        },
        "is_fem_evidence": True,
        "promotes_prediction": False,
        "reason_code": "fem_verified",
    }
    return {**body, "result_sha256": canonical_sha256_v1(body)}


def summarize_campaign(
    *,
    spec: Mapping[str, Any],
    screening_records: Sequence[Mapping[str, Any]],
    fem_results: Sequence[Mapping[str, Any]],
    fem_escalations_used: int,
    wall_time_seconds: float,
) -> dict[str, Any]:
    """Summarize a campaign while keeping prediction and evidence separate.

    The summary reports how many candidates are verified, how many remain
    unverified predictions, and the surrogate's measured error against the FEM
    results.  It never reports a prediction as a measurement.
    """
    validated_spec = validate_campaign_spec(spec)
    records = [validate_screening_record(record) for record in screening_records]
    fem_escalations_used = _require_int(
        "fem_escalations_used",
        fem_escalations_used,
        minimum=0,
        maximum=MAX_FEM_ESCALATIONS,
    )
    if fem_escalations_used > validated_spec["maximum_fem_escalations"]:
        raise ValueError("fem_escalations_used exceeds the declared budget")
    wall_time_seconds = _require_finite("wall_time_seconds", wall_time_seconds)
    if wall_time_seconds < 0.0:
        raise ValueError("wall_time_seconds must be non-negative")

    states: dict[str, int] = {}
    screened_ids = {record["candidate_id"] for record in records}
    fem_candidate_ids: set[str] = set()
    for result in fem_results:
        if not isinstance(result, Mapping):
            raise ValueError("each FEM result must be a mapping")
        candidate_id = result.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("each FEM result must declare a non-empty candidate_id")
        if candidate_id not in screened_ids:
            raise ValueError(f"FEM result for unscreened candidate: {candidate_id!r}")
        if candidate_id in fem_candidate_ids:
            raise ValueError(f"duplicate FEM result for candidate: {candidate_id!r}")
        fem_candidate_ids.add(candidate_id)
        state = result.get("state")
        if state not in CANDIDATE_STATES:
            raise ValueError(f"unknown candidate state: {state!r}")
        if state == "predicted":
            raise ValueError("a FEM result can never be in the predicted state")
        states[state] = states.get(state, 0) + 1

    verified = states.get("verified", 0)
    measured_pairs = [
        (float(result["predicted_objective"]), float(result["measured_objective"]))
        for result in fem_results
        if result.get("state") == "verified" and result.get("measured_objective") is not None
    ]
    if measured_pairs:
        errors = [abs(predicted - measured) for predicted, measured in measured_pairs]
        worst_error = max(errors)
        mean_error = sum(errors) / len(errors)
        disagreements = sum(
            1
            for result in fem_results
            if result.get("state") == "verified"
            and result.get("disagreement")
            and not result["disagreement"]["agrees_with_prediction"]
        )
    else:
        worst_error = None
        mean_error = None
        disagreements = 0

    predicted_only = len(records) - verified
    body = {
        "schema": "comsol_mcp.surrogate_campaign_summary",
        "schema_version": SCHEMA_VERSION,
        "strategy": STRATEGY_VERSION,
        "campaign_id": validated_spec["campaign_id"],
        "spec_sha256": validated_spec["spec_sha256"],
        "screened_count": len(records),
        "fem_result_count": len(fem_results),
        "fem_escalations_used": fem_escalations_used,
        "fem_escalation_budget": validated_spec["maximum_fem_escalations"],
        "wall_time_seconds": wall_time_seconds,
        "wall_time_budget_seconds": validated_spec["wall_time_budget_seconds"],
        "within_wall_time_budget": wall_time_seconds <= validated_spec["wall_time_budget_seconds"],
        "candidate_states": dict(sorted(states.items())),
        "verified_count": verified,
        "predicted_only_count": predicted_only,
        "disagreement_count": disagreements,
        "worst_absolute_error": worst_error,
        "mean_absolute_error": mean_error,
        "error_metrics_source": "fresh_fem_comparison"
        if measured_pairs
        else "no_verified_fem_results",
        # The invariant, restated in the summary itself.
        "predictions_are_not_evidence": True,
        "verified_requires_fresh_fem": True,
        "upgrades_fem_evidence": False,
    }
    return {**body, "summary_sha256": canonical_sha256_v1(body)}


def assert_no_prediction_promotion(
    screening_records: Sequence[Mapping[str, Any]],
    fem_results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Prove no prediction was promoted without a verified FEM result.

    Every candidate reported as verified must have a corresponding FEM result
    whose own evidence state is verified and which carries an artifact hash.
    """
    records = [validate_screening_record(record) for record in screening_records]
    screened = {record["candidate_id"] for record in records}
    verified_ids: set[str] = set()
    for result in fem_results:
        candidate_id = result.get("candidate_id")
        # An absent or non-string id is refused explicitly rather than relying on
        # a None-membership test, so the reported error names the real problem.
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("each FEM result must declare a non-empty candidate_id")
        if candidate_id not in screened:
            raise ValueError(f"FEM result for unscreened candidate: {candidate_id!r}")
        if result.get("state") != "verified":
            continue
        if result.get("fem_evidence_state") != "verified":
            raise ValueError(
                f"candidate {candidate_id!r} is verified without verified FEM evidence"
            )
        if not result.get("fem_artifact_sha256"):
            raise ValueError(f"candidate {candidate_id!r} is verified without a FEM artifact hash")
        if result.get("is_fem_evidence") is not True:
            raise ValueError(f"candidate {candidate_id!r} is verified without FEM evidence flag")
        verified_ids.add(candidate_id)
    return {
        "no_unverified_promotion": True,
        "screened_count": len(screened),
        "verified_count": len(verified_ids),
        "verified_candidate_ids": sorted(verified_ids),
        "predictions_are_not_evidence": True,
    }


__all__ = [
    "CANDIDATE_STATES",
    "ESCALATION_REASONS",
    "MAX_CANDIDATES",
    "MAX_FEM_ESCALATIONS",
    "MAX_TOP_K",
    "MAX_WALL_TIME_SECONDS",
    "SCHEMA_VERSION",
    "STRATEGY_VERSION",
    "assert_no_prediction_promotion",
    "build_campaign_spec",
    "build_screening_record",
    "rank_candidates",
    "record_fem_result",
    "summarize_campaign",
    "validate_campaign_spec",
    "validate_screening_record",
]
