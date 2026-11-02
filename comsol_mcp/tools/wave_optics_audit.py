"""Single-point, policy-separated physical evidence audit for Wave Optics."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sys
import time
import uuid
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Literal, Optional

import numpy as np
from mcp.server.mcpserver import MCPServer
from typing_extensions import TypedDict

from comsol_mcp.evidence.contracts import (
    PHYSICAL_EVIDENCE_SCHEMA_NAME,
    PHYSICAL_EVIDENCE_SCHEMA_VERSION,
    VALIDATION_POLICY_SCHEMA_NAME,
    build_physical_evidence,
    build_point_audit_physical_evidence,
    canonical_json_bytes,
    evaluate_physical_evidence_policy,
    validate_validation_policy,
)
from comsol_mcp.evidence.power_audit import (
    normalize_declared_plane_flux,
    normalize_internal_absorption_consistency,
)
from comsol_mcp.utils.runtime_paths import default_runtime_dir

from .derived_geometry import create_derived_geometry_clone
from .ownership import ownership_manager
from .session import session_manager
from .wave_optics_preflight import collect_wave_optics_preflight
from .workflow import _atomic_write_json, _write_rows_csv

AUDIT_SCHEMA_VERSION = "1"
MAX_POLICY_BYTES = 64 * 1024
MAX_REFERENCE_BYTES = 1024 * 1024
MAX_LOSS_ITEMS = 64
MAX_DOMAIN_IDS = 512
_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
logger = logging.getLogger(__name__)
_ACTIVE_POINT_AUDIT_MANIFEST: ContextVar[Path | None] = ContextVar(
    "active_point_audit_manifest", default=None
)
_ACTIVE_POINT_AUDIT_MUTATION: ContextVar[dict[str, Any] | None] = ContextVar(
    "active_point_audit_mutation", default=None
)


def _terminalize_running_point_audit(
    path: Path,
    error: Exception,
    restoration_errors: list[str] | None = None,
) -> None:
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(current, dict) or current.get("audit_status") != "running":
            return
        failed = {
            **current,
            "audit_status": ("integrity_blocked" if restoration_errors else "runtime_failed"),
            "completed_at_epoch": time.time(),
            "runtime_failure": {
                "code": "point_audit_runtime_failed",
                "error_type": type(error).__name__,
                "details_included": False,
            },
        }
        if restoration_errors:
            failed["integrity_errors"] = [
                {
                    "code": "model_state_restore_failed",
                    "errors": restoration_errors,
                }
            ]
        _atomic_write_json(path, failed)
    except Exception:
        logger.exception("Failed to terminalize Wave Optics point-audit manifest")


def _read_feature_property(feature: Any, name: str) -> str:
    for getter in ("getString", "get"):
        try:
            value = getattr(feature, getter)(name)
            return str(value)
        except Exception:  # noqa: S112 - try each supported ClientAPI getter
            continue
    raise ValueError(f"study-step property is unreadable: {name}")


def _capture_point_audit_mutation(
    model: Any,
    jm: Any,
    study_step: Any,
    *,
    wavelength_parameter: str,
    study_step_property: str,
) -> dict[str, Any]:
    parameters = {
        str(name): str(value) for name, value in dict(model.parameters(evaluate=False)).items()
    }
    if wavelength_parameter not in parameters:
        raise ValueError("wavelength parameter is unavailable for transactional readback")
    return {
        "model": model,
        "jm": jm,
        "study_step": study_step,
        "wavelength_parameter": wavelength_parameter,
        "study_step_property": study_step_property,
        "parameter_before": parameters[wavelength_parameter],
        "study_property_before": _read_feature_property(study_step, study_step_property),
    }


def _restore_point_audit_mutation() -> list[str]:
    mutation = _ACTIVE_POINT_AUDIT_MUTATION.get()
    if mutation is None:
        return []
    errors: list[str] = []
    parameter = mutation["wavelength_parameter"]
    property_name = mutation["study_step_property"]
    try:
        mutation["jm"].param().set(parameter, mutation["parameter_before"])
    except Exception as exc:
        errors.append(f"parameter_write:{type(exc).__name__}")
    try:
        mutation["study_step"].set(property_name, mutation["study_property_before"])
    except Exception as exc:
        errors.append(f"study_property_write:{type(exc).__name__}")
    try:
        actual_parameters = {
            str(name): str(value)
            for name, value in dict(mutation["model"].parameters(evaluate=False)).items()
        }
        if actual_parameters.get(parameter) != mutation["parameter_before"]:
            errors.append("parameter_readback_mismatch")
    except Exception as exc:
        errors.append(f"parameter_readback:{type(exc).__name__}")
    try:
        actual_property = _read_feature_property(mutation["study_step"], property_name)
        if actual_property != mutation["study_property_before"]:
            errors.append("study_property_readback_mismatch")
    except Exception as exc:
        errors.append(f"study_property_readback:{type(exc).__name__}")
    return errors


class CoordinateLimits(TypedDict):
    x: list[float]
    y: list[float]
    z: list[float]


class LossSpecification(TypedDict, total=False):
    label: str
    domains: list[int]
    expression: str
    unit: str
    normalization_expression: str


class PowerProvenance(TypedDict, total=False):
    normalization: str
    R_direction: str
    T_direction: str
    A_definition: str


class DeclaredFluxPlane(TypedDict):
    expression: str
    selection_ids: list[int]
    plane_coordinate_m: float
    normal: list[float]
    medium_id: str
    positive_power_sign: int


class DeclaredPlaneFlux(TypedDict):
    incident: DeclaredFluxPlane
    reflected: DeclaredFluxPlane
    transmitted: DeclaredFluxPlane


class InternalAbsorption(TypedDict):
    cross_section_expression: str
    cross_section_unit: str
    unit_cell_area_expression: str
    source_feature: str
    volume_loss_expression: str
    volume_loss_selection_ids: list[int]
    volume_loss_unit: str
    incident_power_expression: str
    incident_power_sign: int


class PolicyAssumptions(TypedDict, total=False):
    passive: bool
    linear: bool
    port_power_normalized: bool
    reciprocal: bool
    target_basis: str


class PolicyTolerances(TypedDict, total=False):
    closure_abs: float
    quantity_bounds_margin: float
    wavelength_abs_m: float
    wavelength_rel: float
    loss_match_abs: float
    loss_match_rel: float


class PolarizationPolicy(TypedDict, total=False):
    basis: str
    target_vector: list[float]
    max_cross_power_fraction: float
    max_ellipticity: float
    reference_config_id: str


class MeshPolicy(TypedDict, total=False):
    minimum_elements: int
    require_unchanged: bool
    convergence_artifact: str


class ValidationPolicy(TypedDict, total=False):
    version: str
    assumptions: PolicyAssumptions
    required_evidence: list[str]
    tolerances: PolicyTolerances
    polarization: PolarizationPolicy
    mesh: MeshPolicy


class StrictPolicyRule(TypedDict):
    rule_id: str
    rule_type: str
    required_measurements: list[str]
    tolerances: dict[str, float]
    assumptions: dict[str, bool]


class StrictValidationPolicy(TypedDict):
    schema_name: str
    schema_version: str
    policy_id: str
    rules: list[StrictPolicyRule]
    policy_sha256: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_number(value: Any) -> Any:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, complex):
        return {"real": float(value.real), "imag": float(value.imag)}
    if isinstance(value, (int, float)):
        return float(value)
    return value


def _finite_complex(value: complex) -> bool:
    return math.isfinite(float(value.real)) and math.isfinite(float(value.imag))


def _single_complex(value: Any, expression: str) -> complex:
    array = np.asarray(value)
    if array.size != 1:
        raise ValueError(
            f"Expression {expression!r} returned {array.size} values; "
            "one solved-point scalar was required"
        )
    result = complex(array.reshape(-1)[0])
    if not _finite_complex(result):
        raise FloatingPointError(f"Expression {expression!r} returned nonfinite data")
    return result


def _single_real(value: Any, expression: str) -> float:
    """Return one finite scientific real, rejecting material imaginary content."""
    result = _single_complex(value, expression)
    imaginary_tolerance = max(1e-15, 1e-12 * abs(float(result.real)))
    if abs(float(result.imag)) > imaginary_tolerance:
        raise ValueError(
            f"Expression {expression!r} returned materially complex data; "
            "a real scientific scalar was required"
        )
    return float(result.real)


def _validate_ascii_dir(path_text: str | None) -> Path:
    path = (
        Path(path_text).expanduser().resolve()
        if path_text
        else (default_runtime_dir() / "audits").resolve()
    )
    try:
        str(path).encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("artifact_dir must contain ASCII characters only") from exc
    return path


def _validate_tag(value: str, name: str) -> str:
    if not isinstance(value, str) or not _TAG.fullmatch(value):
        raise ValueError(f"{name} must be one exact clientapi tag")
    return value


def _validate_coordinate_range(value: dict[str, Any] | None) -> dict[str, list[float]]:
    if not isinstance(value, dict) or not value:
        raise ValueError("top_air_coordinate_range is required")
    result: dict[str, list[float]] = {}
    for axis in ("x", "y", "z"):
        limits = value.get(axis)
        if not isinstance(limits, list) or len(limits) != 2:
            raise ValueError(f"top_air_coordinate_range.{axis} must be [minimum, maximum]")
        low, high = float(limits[0]), float(limits[1])
        if not math.isfinite(low) or not math.isfinite(high) or low > high:
            raise ValueError(f"top_air_coordinate_range.{axis} must contain ordered finite limits")
        result[axis] = [low, high]
    return result


def _load_json_bounded(
    path_text: str, maximum_bytes: int, label: str
) -> tuple[dict[str, Any], Path, str]:
    path = Path(path_text).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"{label} does not exist: {path}")
    if path.stat().st_size > maximum_bytes:
        raise ValueError(f"{label} exceeds {maximum_bytes} bytes")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain one JSON object")
    return payload, path, _sha256_file(path)


def _load_policy(
    validation_policy: dict[str, Any] | None,
    validation_policy_path: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if validation_policy is not None and validation_policy_path is not None:
        raise ValueError("provide validation_policy or validation_policy_path, not both")
    provenance = None
    if validation_policy_path is not None:
        policy, path, source_hash = _load_json_bounded(
            validation_policy_path, MAX_POLICY_BYTES, "validation_policy_path"
        )
        provenance = {"path": str(path), "source_sha256": source_hash}
    else:
        policy = validation_policy
    if policy is None:
        return None, None
    if not isinstance(policy, dict):
        raise ValueError("validation_policy must be a JSON object")
    encoded = canonical_json_bytes(policy)
    if len(encoded) > MAX_POLICY_BYTES:
        raise ValueError(f"validation_policy exceeds {MAX_POLICY_BYTES} bytes")
    if "schema_name" in policy:
        if policy.get("schema_name") != VALIDATION_POLICY_SCHEMA_NAME:
            raise ValueError("validation_policy.schema_name is unsupported")
        normalized = validate_validation_policy(policy)
        provenance = {
            **(provenance or {}),
            "policy_sha256": normalized["policy_sha256"],
            "policy_format": "strict_physical_evidence_v1",
        }
        return normalized, provenance
    allowed = {"version", "assumptions", "required_evidence", "tolerances", "polarization", "mesh"}
    unknown = sorted(set(policy) - allowed)
    if unknown:
        raise ValueError(f"validation_policy contains unknown fields: {unknown}")
    for mapping_name in ("assumptions", "tolerances", "polarization", "mesh"):
        if mapping_name in policy and not isinstance(policy[mapping_name], dict):
            raise ValueError(f"validation_policy.{mapping_name} must be an object")
    nested_allowed = {
        "assumptions": {"passive", "linear", "port_power_normalized", "reciprocal", "target_basis"},
        "tolerances": {
            "closure_abs",
            "quantity_bounds_margin",
            "wavelength_abs_m",
            "wavelength_rel",
            "loss_match_abs",
            "loss_match_rel",
        },
        "polarization": {
            "basis",
            "target_vector",
            "max_cross_power_fraction",
            "max_ellipticity",
            "reference_config_id",
        },
        "mesh": {"minimum_elements", "require_unchanged", "convergence_artifact"},
    }
    for mapping_name, allowed_fields in nested_allowed.items():
        nested = policy.get(mapping_name, {})
        unknown_nested = sorted(set(nested) - allowed_fields)
        if unknown_nested:
            raise ValueError(
                f"validation_policy.{mapping_name} contains unknown fields: {unknown_nested}"
            )
    assumptions = policy.get("assumptions", {})
    for name in ("passive", "linear", "port_power_normalized", "reciprocal"):
        if name in assumptions and type(assumptions[name]) is not bool:
            raise ValueError(f"validation_policy.assumptions.{name} must be boolean")
    mesh = policy.get("mesh", {})
    if "require_unchanged" in mesh and type(mesh["require_unchanged"]) is not bool:
        raise ValueError("validation_policy.mesh.require_unchanged must be boolean")
    for name, value in policy.get("tolerances", {}).items():
        try:
            finite = math.isfinite(float(value))
        except TypeError, ValueError, OverflowError:
            finite = False
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not finite
            or value < 0
        ):
            raise ValueError(f"validation_policy.tolerances.{name} must be finite and nonnegative")
    required = policy.get("required_evidence", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise ValueError("validation_policy.required_evidence must be a string list")
    provenance = {
        **(provenance or {}),
        "policy_sha256": _canonical_hash(policy),
        "policy_format": "legacy_point_audit_v1",
        "migration_semantics": "preserved_without_reinterpretation",
    }
    return policy, provenance


def _policy_rule(
    name: str,
    outcome: str,
    *,
    measured: Any = None,
    threshold: Any = None,
    reason: str | None = None,
) -> dict[str, Any]:
    result = {"rule": name, "outcome": outcome}
    if measured is not None:
        result["measured"] = measured
    if threshold is not None:
        result["threshold"] = threshold
    if reason:
        result["reason"] = reason
    return result


def _usable_volume_loss(measurement: dict[str, Any]) -> bool:
    """Whether at least one declared loss record carries a usable finite value.

    A record whose expression failed to evaluate carries an error and no
    value: it stays recorded as evidence of the attempt, but it is never
    usable loss evidence, so required volume-loss evidence must not pass on
    error-only records (C081R-01). Raw values count as-is - a caller-declared
    normalization is not required for availability, only for normalized
    comparisons.
    """
    for item in measurement.get("losses", {}).get("items", []):
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(value):
            return True
    return False


def _evidence_available(measurement: dict[str, Any], name: str) -> bool:
    mapping = {
        "wavelength_controls": bool(measurement.get("wavelength", {}).get("complete")),
        "flux_RTA": bool(measurement.get("power", {}).get("complete")),
        "incident_polarization": (
            measurement.get("polarization", {}).get("evidence_level")
            in {"incident_reference", "direct_incident_field"}
            and _polarization_vector(measurement.get("polarization", {})) is not None
        ),
        "top_air_region": bool(
            measurement.get("polarization", {}).get("structure_total_field", {}).get("complete")
        ),
        "volume_loss": _usable_volume_loss(measurement),
        "source_integrity": bool(measurement.get("integrity", {}).get("source_unchanged")),
        "mesh_evidence": bool(measurement.get("mesh", {}).get("element_count") is not None),
    }
    return bool(mapping.get(name, False))


def _polarization_vector(polarization: dict[str, Any]) -> list[complex] | None:
    reference = polarization.get("incident_reference")
    if not isinstance(reference, dict):
        return None
    config_id = reference.get("config_id")
    if not isinstance(config_id, str) or not config_id.strip() or len(config_id) > 128:
        return None
    stats = reference.get("component_statistics") or reference.get("field_statistics")
    if not isinstance(stats, dict):
        return None
    vector = []
    for axis in ("x", "y", "z"):
        component = stats.get(axis)
        if not isinstance(component, dict):
            return None
        mean = component.get("complex_mean")
        if not isinstance(mean, dict) or not {"real", "imag"} <= set(mean):
            return None
        real = mean.get("real")
        imag = mean.get("imag")
        if (
            isinstance(real, bool)
            or not isinstance(real, (int, float))
            or isinstance(imag, bool)
            or not isinstance(imag, (int, float))
        ):
            return None
        value = complex(float(real), float(imag))
        if not _finite_complex(value):
            return None
        vector.append(value)
    return vector


def evaluate_validation_policy(
    measurement: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    """Evaluate declared project rules without changing immutable raw evidence."""
    rules: list[dict[str, Any]] = []
    for name in policy.get("required_evidence", []):
        available = _evidence_available(measurement, name)
        rules.append(
            _policy_rule(
                f"required_evidence.{name}",
                "pass" if available else "missing",
                measured=available,
                threshold=True,
            )
        )

    assumptions = policy.get("assumptions", {})
    tolerances = policy.get("tolerances", {})
    power = measurement.get("power", {})
    wavelength = measurement.get("wavelength", {})
    if "closure_abs" in tolerances:
        measured = power.get("closure_abs")
        threshold = float(tolerances["closure_abs"])
        rules.append(
            _policy_rule(
                "tolerances.closure_abs",
                "missing" if measured is None else ("pass" if measured <= threshold else "fail"),
                measured=measured,
                threshold=threshold,
            )
        )
    if "quantity_bounds_margin" in tolerances:
        margin = float(tolerances["quantity_bounds_margin"])
        if not assumptions.get("passive") or not assumptions.get("port_power_normalized"):
            rules.append(
                _policy_rule(
                    "tolerances.quantity_bounds_margin",
                    "not_applicable",
                    threshold=margin,
                    reason="requires passive and port_power_normalized assumptions",
                )
            )
        else:
            values = [power.get(name) for name in ("R", "T", "A")]
            complete = all(isinstance(value, (int, float)) for value in values)
            passed = complete and all(-margin <= float(value) <= 1.0 + margin for value in values)
            rules.append(
                _policy_rule(
                    "tolerances.quantity_bounds_margin",
                    "missing" if not complete else ("pass" if passed else "fail"),
                    measured=values,
                    threshold={"minimum": -margin, "maximum": 1.0 + margin},
                )
            )
    for key, measured_key in (
        ("wavelength_abs_m", "absolute_difference_m"),
        ("wavelength_rel", "relative_difference"),
    ):
        if key in tolerances:
            measured = wavelength.get(measured_key)
            threshold = float(tolerances[key])
            rules.append(
                _policy_rule(
                    f"tolerances.{key}",
                    "missing"
                    if measured is None
                    else ("pass" if measured <= threshold else "fail"),
                    measured=measured,
                    threshold=threshold,
                )
            )
    normalized_losses = [
        item.get("normalized_value")
        for item in measurement.get("losses", {}).get("items", [])
        if isinstance(item.get("normalized_value"), (int, float))
    ]
    loss_target = power.get("one_minus_R_minus_T")
    loss_difference = (
        abs(sum(float(value) for value in normalized_losses) - float(loss_target))
        if normalized_losses and isinstance(loss_target, (int, float))
        else None
    )
    if "loss_match_abs" in tolerances:
        threshold = float(tolerances["loss_match_abs"])
        rules.append(
            _policy_rule(
                "tolerances.loss_match_abs",
                "missing"
                if loss_difference is None
                else ("pass" if loss_difference <= threshold else "fail"),
                measured=loss_difference,
                threshold=threshold,
                reason="sum(caller-normalized losses) versus 1-R-T",
            )
        )
    if "loss_match_rel" in tolerances:
        relative = (
            None
            if loss_difference is None or loss_target in (None, 0)
            else loss_difference / abs(float(loss_target))
        )
        threshold = float(tolerances["loss_match_rel"])
        rules.append(
            _policy_rule(
                "tolerances.loss_match_rel",
                "missing" if relative is None else ("pass" if relative <= threshold else "fail"),
                measured=relative,
                threshold=threshold,
                reason="sum(caller-normalized losses) versus 1-R-T",
            )
        )

    polarization_policy = policy.get("polarization", {})
    if polarization_policy:
        expected_reference = polarization_policy.get("reference_config_id")
        actual_reference = (
            measurement.get("polarization", {}).get("incident_reference") or {}
        ).get("config_id")
        if expected_reference is not None:
            rules.append(
                _policy_rule(
                    "polarization.reference_config_id",
                    "pass" if actual_reference == expected_reference else "fail",
                    measured=actual_reference,
                    threshold=expected_reference,
                )
            )
        target = polarization_policy.get("target_vector")
        maximum_cross = polarization_policy.get("max_cross_power_fraction")
        vector = _polarization_vector(measurement.get("polarization", {}))
        if target is not None and maximum_cross is not None:
            try:
                target_vector = np.asarray(target, dtype=complex).reshape(-1)
                measured_vector = np.asarray(vector, dtype=complex).reshape(-1) if vector else None
                if (
                    measured_vector is None
                    or measured_vector.size != target_vector.size
                    or np.vdot(measured_vector, measured_vector).real == 0
                    or np.vdot(target_vector, target_vector).real == 0
                ):
                    raise ValueError("vector unavailable")
                target_vector = target_vector / math.sqrt(
                    float(np.vdot(target_vector, target_vector).real)
                )
                cross = 1.0 - float(
                    abs(np.vdot(target_vector, measured_vector)) ** 2
                    / np.vdot(measured_vector, measured_vector).real
                )
                cross = max(0.0, min(1.0, cross))
                threshold = float(maximum_cross)
                rules.append(
                    _policy_rule(
                        "polarization.max_cross_power_fraction",
                        "pass" if cross <= threshold else "fail",
                        measured=cross,
                        threshold=threshold,
                    )
                )
            except Exception:
                rules.append(
                    _policy_rule(
                        "polarization.max_cross_power_fraction",
                        "missing",
                        threshold=float(maximum_cross),
                        reason="complex incident-reference vector unavailable",
                    )
                )
        if "max_ellipticity" in polarization_policy:
            stokes = (
                measurement.get("polarization", {})
                .get("incident_reference", {})
                .get("stokes_xy", {})
            )
            s0, s3 = stokes.get("S0"), stokes.get("S3")
            measured = (
                abs(float(s3) / float(s0)) if s0 not in (None, 0) and s3 is not None else None
            )
            threshold = float(polarization_policy["max_ellipticity"])
            rules.append(
                _policy_rule(
                    "polarization.max_ellipticity",
                    "missing"
                    if measured is None
                    else ("pass" if measured <= threshold else "fail"),
                    measured=measured,
                    threshold=threshold,
                )
            )

    mesh_policy = policy.get("mesh", {})
    if "minimum_elements" in mesh_policy:
        measured = measurement.get("mesh", {}).get("element_count")
        threshold = int(mesh_policy["minimum_elements"])
        rules.append(
            _policy_rule(
                "mesh.minimum_elements",
                "missing" if measured is None else ("pass" if measured >= threshold else "fail"),
                measured=measured,
                threshold=threshold,
            )
        )
    if mesh_policy.get("require_unchanged"):
        measured = measurement.get("mesh", {}).get("unchanged_during_audit")
        rules.append(
            _policy_rule(
                "mesh.require_unchanged",
                "pass" if measured is True else ("fail" if measured is False else "missing"),
                measured=measured,
                threshold=True,
            )
        )

    outcomes = [rule["outcome"] for rule in rules]
    overall = (
        "missing"
        if not outcomes
        else "fail"
        if "fail" in outcomes
        else "missing"
        if "missing" in outcomes
        else "pass"
    )
    return {
        "mode": "explicit_policy",
        "overall": overall,
        "rules": rules,
        "policy_sha256": _canonical_hash(policy),
    }


def _component_statistics(values: np.ndarray) -> dict[str, Any]:
    values = np.asarray(values, dtype=complex).reshape(-1)
    if not np.all(np.isfinite(values.real)) or not np.all(np.isfinite(values.imag)):
        raise FloatingPointError("field component contains nonfinite values")
    magnitudes = np.abs(values)
    mean = complex(np.mean(values))
    return {
        "count": int(values.size),
        "rms_abs": float(math.sqrt(float(np.mean(magnitudes**2)))),
        "median_abs": float(np.median(magnitudes)),
        "mean_abs": float(np.mean(magnitudes)),
        "complex_mean": _json_number(mean),
        "phase_of_complex_mean_rad": float(np.angle(mean)),
    }


def _stokes_xy(ex: complex, ey: complex) -> dict[str, float]:
    s0 = abs(ex) ** 2 + abs(ey) ** 2
    return {
        "S0": float(s0),
        "S1": float(abs(ex) ** 2 - abs(ey) ** 2),
        "S2": float(2 * np.real(ex * np.conj(ey))),
        "S3": float(-2 * np.imag(ex * np.conj(ey))),
        "convention": "S3=-2*Im(Ex*conj(Ey))",
    }


def _field_statistics(ex: np.ndarray, ey: np.ndarray, ez: np.ndarray) -> dict[str, Any]:
    stats = {
        "x": _component_statistics(ex),
        "y": _component_statistics(ey),
        "z": _component_statistics(ez),
    }
    means = [
        complex(stats[axis]["complex_mean"]["real"], stats[axis]["complex_mean"]["imag"])
        for axis in ("x", "y", "z")
    ]
    rms = {axis: stats[axis]["rms_abs"] for axis in ("x", "y", "z")}
    ratios = {}
    for numerator in ("x", "y", "z"):
        for denominator in ("x", "y", "z"):
            if numerator != denominator:
                ratios[f"{numerator}_over_{denominator}"] = (
                    None if rms[denominator] == 0 else rms[numerator] / rms[denominator]
                )
    return {
        "component_statistics": stats,
        "rms_amplitude_ratios": ratios,
        "relative_phase_rad": {
            "x_minus_y": float(np.angle(means[0]) - np.angle(means[1])),
            "x_minus_z": float(np.angle(means[0]) - np.angle(means[2])),
            "y_minus_z": float(np.angle(means[1]) - np.angle(means[2])),
        },
        "stokes_xy": _stokes_xy(means[0], means[1]),
    }


def _resolve_named_domains(component: Any, selection_tag: str) -> list[int]:
    selection = component.selection(selection_tag)
    for args in ((3,), tuple()):
        try:
            return sorted({int(value) for value in list(selection.entities(*args))})
        except Exception:  # noqa: S112 - try each supported ClientAPI overload
            continue
    raise ValueError(f"named selection {selection_tag!r} has no readable domain entities")


def _sample_structure_field(
    model: Any,
    *,
    component: Any,
    physics_tag: str,
    coordinate_range: dict[str, list[float]],
    domain_ids: list[int],
    named_selection: str | None,
) -> dict[str, Any]:
    expressions = [
        f"{physics_tag}.Ex",
        f"{physics_tag}.Ey",
        f"{physics_tag}.Ez",
        "x",
        "y",
        "z",
        "dom",
    ]
    values = model.evaluate(expressions)
    arrays = [np.asarray(value).reshape(-1) for value in values]
    size = arrays[0].size
    if not size or any(array.size != size for array in arrays):
        raise ValueError("field sampling expressions returned incompatible arrays")
    mask = np.ones(size, dtype=bool)
    for array, axis in zip(arrays[3:6], ("x", "y", "z")):
        if not np.all(np.isfinite(array.real)) or not np.all(np.isfinite(array.imag)):
            raise FloatingPointError(f"coordinate {axis} contains nonfinite values")
        if np.any(array.imag != 0):
            raise ValueError(f"coordinate {axis} unexpectedly contains imaginary values")
        coordinate = np.asarray(array.real, dtype=float)
        low, high = coordinate_range[axis]
        mask &= coordinate >= low
        mask &= coordinate <= high
    if domain_ids:
        domain_array = arrays[6]
        if not np.all(np.isfinite(domain_array.real)) or np.any(domain_array.imag != 0):
            raise ValueError("domain-number expression returned invalid values")
        mask &= np.isin(np.rint(domain_array.real).astype(int), domain_ids)
    count = int(np.count_nonzero(mask))
    if count == 0:
        raise ValueError("declared top-air domain/coordinate filter selected zero field samples")
    stats = _field_statistics(arrays[0][mask], arrays[1][mask], arrays[2][mask])
    return {
        "complete": True,
        "evidence_level": "structure_total_field",
        "diagnostic_only": True,
        "limitation": (
            "The structure total field contains reflection and cannot verify the incident "
            "vector by itself."
        ),
        "selection": {
            "named_selection": named_selection,
            "domain_ids": domain_ids,
            "coordinate_range": coordinate_range,
            "sample_count": count,
            "total_unfiltered_count": int(size),
        },
        **stats,
    }


def _load_air_reference(
    path_text: str | None, expected_config_id: str | None
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if path_text is None:
        return None, []
    warnings: list[dict[str, Any]] = []
    payload, path, source_hash = _load_json_bounded(
        path_text, MAX_REFERENCE_BYTES, "air_reference_artifact_path"
    )
    reference_payload = payload.get("reference")
    record = reference_payload if isinstance(reference_payload, dict) else payload
    config_id = record.get("config_id", payload.get("config_id"))
    if expected_config_id is not None and config_id != expected_config_id:
        warnings.append(
            {
                "code": "air_reference_config_mismatch",
                "expected": expected_config_id,
                "actual": config_id,
            }
        )
        return None, warnings
    field = record.get("field")
    stats = record.get("component_statistics") or record.get("field_statistics")
    if not isinstance(stats, dict) and isinstance(field, dict):
        stats = field.get("component_statistics") or field.get("field_statistics")
    if not isinstance(stats, dict):
        warnings.append({"code": "air_reference_field_statistics_missing", "path": str(path)})
        return None, warnings
    reference = {
        "config_id": config_id,
        "source_path": str(path),
        "source_sha256": source_hash,
        "component_statistics": stats,
        "stokes_xy": payload.get("stokes_xy", {}),
        "provenance": payload.get("provenance"),
    }
    if (
        _polarization_vector(
            {"evidence_level": "incident_reference", "incident_reference": reference}
        )
        is None
    ):
        warnings.append({"code": "air_reference_identity_or_components_invalid"})
        return None, warnings
    return reference, warnings


def _mesh_state(component: Any) -> dict[str, Any]:
    mesh_tags = [str(value) for value in list(component.mesh().tags())]
    if not mesh_tags:
        return {"mesh_tag": None, "element_count": None, "vertex_count": None}
    mesh_tag = "mesh1" if "mesh1" in mesh_tags else mesh_tags[0]
    mesh = component.mesh().get(mesh_tag)
    return {
        "mesh_tag": mesh_tag,
        "element_count": int(mesh.getNumElem()),
        "vertex_count": int(mesh.getNumVertex()),
    }


def _strict_positive_entity_ids(value: Any, label: str) -> list[int]:
    if not isinstance(value, list) or len(value) > MAX_DOMAIN_IDS:
        raise ValueError(f"{label} must be a bounded integer list")
    normalized: list[int] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or type(item) is not int or item <= 0:
            raise ValueError(f"{label}[{index}] must be a positive integer")
        normalized.append(item)
    return sorted(set(normalized))


def _validate_loss_map(loss_map: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if loss_map is None:
        return []
    if not isinstance(loss_map, list) or len(loss_map) > MAX_LOSS_ITEMS:
        raise ValueError(f"loss_map must contain at most {MAX_LOSS_ITEMS} objects")
    result = []
    for index, item in enumerate(loss_map):
        if not isinstance(item, dict):
            raise ValueError(f"loss_map[{index}] must be an object")
        label = item.get("label")
        expression = item.get("expression")
        domains = item.get("domains")
        if (
            not isinstance(label, str)
            or not label.strip()
            or not isinstance(expression, str)
            or not expression.strip()
        ):
            raise ValueError(f"loss_map[{index}] requires non-empty label and expression")
        if len(label) > 128 or len(expression) > 1024:
            raise ValueError(f"loss_map[{index}] label/expression exceeds the bounded length")
        normalized_domains = _strict_positive_entity_ids(domains, f"loss_map[{index}].domains")
        normalization_expression = item.get("normalization_expression")
        if normalization_expression is not None and (
            not isinstance(normalization_expression, str)
            or not normalization_expression.strip()
            or len(normalization_expression) > 1024
        ):
            raise ValueError(f"loss_map[{index}].normalization_expression is invalid")
        result.append(
            {
                "label": label,
                "expression": expression,
                "domains": normalized_domains,
                "unit": item.get("unit"),
                "normalization_expression": normalization_expression,
            }
        )
    return result


def _validate_declared_plane_flux(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("declared_plane_flux must be an object")
    candidate: dict[str, Any] = {}
    for name, item in value.items():
        if not isinstance(item, dict):
            raise ValueError(f"declared_plane_flux.{name} must be an object")
        sign = item.get("positive_power_sign")
        candidate[name] = {**item, "raw_power_w": float(sign) if sign in {-1, 1} else 0.0}
    normalized = normalize_declared_plane_flux(candidate)
    return {
        name: {
            key: item[key]
            for key in (
                "expression",
                "selection_ids",
                "plane_coordinate_m",
                "normal",
                "medium_id",
                "positive_power_sign",
            )
        }
        for name, item in normalized["planes"].items()
    }


def _bounded_expression(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ValueError(f"{label} must be a non-empty expression of at most 1024 characters")
    return value


def _validate_internal_absorption(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("internal_absorption must be an object")
    allowed = {
        "cross_section_expression",
        "cross_section_unit",
        "unit_cell_area_expression",
        "source_feature",
        "volume_loss_expression",
        "volume_loss_selection_ids",
        "volume_loss_unit",
        "incident_power_expression",
        "incident_power_sign",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"internal_absorption contains unknown fields: {unknown}")
    sign = value.get("incident_power_sign")
    if isinstance(sign, bool) or sign not in {-1, 1}:
        raise ValueError("internal_absorption.incident_power_sign must be exactly -1 or 1")
    normalized = {
        "cross_section_expression": _bounded_expression(
            value.get("cross_section_expression"), "internal_absorption.cross_section_expression"
        ),
        "cross_section_unit": str(value.get("cross_section_unit")),
        "unit_cell_area_expression": _bounded_expression(
            value.get("unit_cell_area_expression"), "internal_absorption.unit_cell_area_expression"
        ),
        "source_feature": _bounded_expression(
            value.get("source_feature"), "internal_absorption.source_feature"
        ),
        "volume_loss_expression": _bounded_expression(
            value.get("volume_loss_expression"), "internal_absorption.volume_loss_expression"
        ),
        "volume_loss_selection_ids": value.get("volume_loss_selection_ids"),
        "volume_loss_unit": str(value.get("volume_loss_unit")),
        "incident_power_expression": _bounded_expression(
            value.get("incident_power_expression"), "internal_absorption.incident_power_expression"
        ),
        "incident_power_sign": int(sign),
    }
    normalize_internal_absorption_consistency(
        {
            "expression": normalized["cross_section_expression"],
            "value_m2": 1.0,
            "unit": normalized["cross_section_unit"],
            "unit_cell_area_expression": normalized["unit_cell_area_expression"],
            "unit_cell_area_m2": 1.0,
            "source_feature": normalized["source_feature"],
        },
        {
            "expression": normalized["volume_loss_expression"],
            "selection_ids": normalized["volume_loss_selection_ids"],
            "value_w": 1.0,
            "incident_power_w": 1.0,
            "unit": normalized["volume_loss_unit"],
        },
    )
    return normalized


def _evaluate_declared_plane_flux(model: Any, declaration: dict[str, Any]) -> dict[str, Any]:
    evaluated: dict[str, Any] = {}
    for name, plane in declaration.items():
        value = _single_real(model.evaluate(plane["expression"]), plane["expression"])
        evaluated[name] = {**plane, "raw_power_w": value}
    return normalize_declared_plane_flux(evaluated)


def _evaluate_internal_absorption(model: Any, declaration: dict[str, Any]) -> dict[str, Any]:
    cross_value = _single_real(
        model.evaluate(declaration["cross_section_expression"]),
        declaration["cross_section_expression"],
    )
    area_value = _single_real(
        model.evaluate(declaration["unit_cell_area_expression"]),
        declaration["unit_cell_area_expression"],
    )
    volume_value = _single_real(
        model.evaluate(declaration["volume_loss_expression"]),
        declaration["volume_loss_expression"],
    )
    incident_value = _single_real(
        model.evaluate(declaration["incident_power_expression"]),
        declaration["incident_power_expression"],
    )
    incident_power = incident_value * declaration["incident_power_sign"]
    return normalize_internal_absorption_consistency(
        {
            "expression": declaration["cross_section_expression"],
            "value_m2": cross_value,
            "unit": declaration["cross_section_unit"],
            "unit_cell_area_expression": declaration["unit_cell_area_expression"],
            "unit_cell_area_m2": area_value,
            "source_feature": declaration["source_feature"],
        },
        {
            "expression": declaration["volume_loss_expression"],
            "selection_ids": declaration["volume_loss_selection_ids"],
            "value_w": volume_value,
            "incident_power_w": incident_power,
            "unit": declaration["volume_loss_unit"],
        },
    )


def _run_wave_optics_point_audit_impl(
    model: Any,
    *,
    model_name: str,
    component_tag: str,
    physics_tag: str,
    study_tag: str,
    wavelength_value: float,
    wavelength_unit: str,
    wavelength_parameter: str,
    study_step_tag: str,
    study_step_property: str,
    expected_source_sha256: str,
    config_id: str,
    artifact_dir: str | None,
    r_expression: str | None = None,
    t_expression: str | None = None,
    a_expression: str | None = None,
    top_air_selection: str | None = None,
    top_air_domain_ids: list[int] | None = None,
    top_air_coordinate_range: dict[str, Any] | None = None,
    loss_map: list[dict[str, Any]] | None = None,
    power_provenance: dict[str, Any] | None = None,
    declared_plane_flux: dict[str, Any] | None = None,
    internal_absorption: dict[str, Any] | None = None,
    air_reference_artifact_path: str | None = None,
    air_reference_config_id: str | None = None,
    validation_policy: dict[str, Any] | None = None,
    validation_policy_path: str | None = None,
    session_state: dict[str, Any] | None = None,
    loaded_source_identity: dict[str, Any] | None = None,
    active_profile: str = "unknown",
    ownership_preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Solve exactly one declared wavelength and persist raw evidence first."""
    component_tag = _validate_tag(component_tag, "component_tag")
    physics_tag = _validate_tag(physics_tag, "physics_tag")
    study_tag = _validate_tag(study_tag, "study_tag")
    wavelength_parameter = _validate_tag(wavelength_parameter, "wavelength_parameter")
    study_step_tag = _validate_tag(study_step_tag, "study_step_tag")
    if not isinstance(study_step_property, str) or not _TAG.fullmatch(study_step_property):
        raise ValueError("study_step_property must be one exact property name")
    if not isinstance(config_id, str) or not config_id.strip() or len(config_id) > 128:
        raise ValueError("config_id must be non-empty and at most 128 characters")
    if not isinstance(expected_source_sha256, str) or not re.fullmatch(
        r"[0-9A-Fa-f]{64}", expected_source_sha256.strip()
    ):
        raise ValueError("expected_source_sha256 must be exactly 64 hexadecimal characters")
    wavelength_value = float(wavelength_value)
    if not math.isfinite(wavelength_value) or wavelength_value <= 0:
        raise ValueError("wavelength_value must be finite and positive")
    if (
        not isinstance(wavelength_unit, str)
        or not wavelength_unit.strip()
        or len(wavelength_unit) > 32
    ):
        raise ValueError("wavelength_unit must be non-empty")
    coordinate_range = _validate_coordinate_range(top_air_coordinate_range)
    explicit_domains = _strict_positive_entity_ids(
        [] if top_air_domain_ids is None else top_air_domain_ids,
        "top_air_domain_ids",
    )
    if len(explicit_domains) > MAX_DOMAIN_IDS:
        raise ValueError(f"top_air_domain_ids exceeds {MAX_DOMAIN_IDS} entries")
    if any(value <= 0 for value in explicit_domains):
        raise ValueError("top_air_domain_ids must contain positive entity IDs")
    if not top_air_selection and not explicit_domains:
        raise ValueError("provide top_air_selection or top_air_domain_ids")
    if top_air_selection is not None:
        _validate_tag(top_air_selection, "top_air_selection")
    for name, expression in (
        ("r_expression", r_expression),
        ("t_expression", t_expression),
        ("a_expression", a_expression),
    ):
        if expression is not None and (
            not isinstance(expression, str) or not expression.strip() or len(expression) > 1024
        ):
            raise ValueError(f"{name} must be a non-empty expression of at most 1024 characters")
    losses = _validate_loss_map(loss_map)
    flux_declaration = _validate_declared_plane_flux(declared_plane_flux)
    internal_absorption_declaration = _validate_internal_absorption(internal_absorption)
    if power_provenance is not None:
        if not isinstance(power_provenance, dict):
            raise ValueError("power_provenance must be an object")
        allowed_power_provenance = {"normalization", "R_direction", "T_direction", "A_definition"}
        unknown_power_provenance = sorted(set(power_provenance) - allowed_power_provenance)
        if unknown_power_provenance:
            raise ValueError(
                f"power_provenance contains unknown fields: {unknown_power_provenance}"
            )
        power_provenance = {key: str(value)[:500] for key, value in power_provenance.items()}
    else:
        power_provenance = {}
    policy, policy_provenance = _load_policy(validation_policy, validation_policy_path)
    root = _validate_ascii_dir(artifact_dir)
    root.mkdir(parents=True, exist_ok=True)
    air_reference, reference_warnings = _load_air_reference(
        air_reference_artifact_path, air_reference_config_id
    )

    source_path = Path(str(model.file())).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"loaded source file is unavailable: {source_path}")
    source_hash_before = _sha256_file(source_path)
    expected_hash = expected_source_sha256.strip().lower()
    if source_hash_before.lower() != expected_hash:
        return {
            "success": True,
            "audit_status": "integrity_blocked",
            "assessment": {"mode": "evidence_only", "project_verdict": None},
            "integrity_errors": [
                {
                    "code": "source_hash_mismatch",
                    "expected": expected_hash,
                    "actual": source_hash_before,
                }
            ],
        }
    if ownership_preflight is not None and not ownership_preflight.get("ready"):
        return {
            "success": True,
            "audit_status": "integrity_blocked",
            "assessment": {"mode": "evidence_only", "project_verdict": None},
            "integrity_errors": [
                {
                    "code": "solver_ownership_blocked",
                    "blockers": ownership_preflight.get("blockers", []),
                }
            ],
        }

    preflight = collect_wave_optics_preflight(
        model,
        model_name=model_name,
        session_state=session_state or {},
        active_profile=active_profile,
        expected_component_tag=component_tag,
        expected_physics_tag=physics_tag,
        expected_study_tag=study_tag,
        expected_source_path=str(source_path),
        expected_source_sha256=source_hash_before,
        loaded_source_identity=loaded_source_identity,
        target_wavelength_parameter=wavelength_parameter,
    )
    if preflight["inspection_status"] == "integrity_blocked":
        return {
            "success": True,
            "audit_status": "integrity_blocked",
            "assessment": {"mode": "evidence_only", "project_verdict": None},
            "integrity_errors": preflight["evidence"]["integrity_errors"],
        }

    jm = model.java
    component = jm.component(component_tag)
    if component is None or physics_tag not in [
        str(value) for value in list(component.physics().tags())
    ]:
        raise ValueError("requested component/physics does not exist")
    if study_tag not in [str(value) for value in list(jm.study().tags())]:
        raise ValueError("requested study does not exist")
    study = jm.study(study_tag)
    if study_step_tag not in [str(value) for value in list(study.feature().tags())]:
        raise ValueError("requested study step does not exist")
    study_step = study.feature().get(study_step_tag)
    domains = explicit_domains
    if top_air_selection:
        named_domains = _resolve_named_domains(component, top_air_selection)
        if domains and domains != named_domains:
            raise ValueError("top_air_selection and top_air_domain_ids disagree")
        domains = named_domains

    power_expressions = {
        "R": r_expression or f"{physics_tag}.Rtotal",
        "T": t_expression or f"{physics_tag}.Ttotal",
        "A": a_expression or f"{physics_tag}.Atotal",
    }
    config_spec = {
        "model_name": model_name,
        "source_sha256": source_hash_before,
        "component_tag": component_tag,
        "physics_tag": physics_tag,
        "study_tag": study_tag,
        "study_step_tag": study_step_tag,
        "study_step_property": study_step_property,
        "wavelength": {
            "value": wavelength_value,
            "unit": wavelength_unit,
            "parameter": wavelength_parameter,
        },
        "power_expressions": power_expressions,
        "power_provenance": power_provenance,
        "top_air": {
            "selection": top_air_selection,
            "domains": domains,
            "coordinate_range": coordinate_range,
        },
        "loss_map": losses,
        "declared_plane_flux": flux_declaration,
        "internal_absorption": internal_absorption_declaration,
        "air_reference_config_id": air_reference_config_id,
    }
    config_sha256 = _canonical_hash(config_spec)

    mesh_before = _mesh_state(component)
    audit_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    directory = root / re.sub(r"[^A-Za-z0-9_.-]", "_", config_id) / audit_id
    directory.mkdir(parents=True, exist_ok=False)
    csv_path = directory / "point.csv"
    manifest_path = directory / "manifest.json"
    _atomic_write_json(
        manifest_path,
        {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "audit_id": audit_id,
            "config_id": config_id,
            "audit_status": "running",
            "created_at_epoch": time.time(),
            "source_path": str(source_path),
            "source_sha256": source_hash_before,
            "config_spec": config_spec,
            "config_sha256": config_sha256,
            "validation_policy": policy,
            "validation_policy_provenance": policy_provenance,
            "preflight": preflight,
            "mesh_before": mesh_before,
            "artifacts": {"csv": str(csv_path), "manifest": str(manifest_path)},
        },
    )
    _ACTIVE_POINT_AUDIT_MANIFEST.set(manifest_path)
    parameter_value = f"{wavelength_value}[{wavelength_unit}]"
    mutation = _capture_point_audit_mutation(
        model,
        jm,
        study_step,
        wavelength_parameter=wavelength_parameter,
        study_step_property=study_step_property,
    )
    _ACTIVE_POINT_AUDIT_MUTATION.set(mutation)
    jm.param().set(wavelength_parameter, parameter_value)
    study_step.set(study_step_property, wavelength_parameter)
    started = time.perf_counter()
    solve_error = None
    try:
        study.run()
    except Exception as exc:
        solve_error = str(exc)[:1000]
    solve_seconds = time.perf_counter() - started

    measurement_errors: list[dict[str, Any]] = []
    integrity_errors: list[dict[str, Any]] = []
    power: dict[str, Any] = {
        "expressions": power_expressions,
        "provenance": {
            "normalization": power_provenance.get("normalization", "not_declared"),
            "flux_directions": {
                "R": power_provenance.get("R_direction", "not_declared"),
                "T": power_provenance.get("T_direction", "not_declared"),
            },
            "A_definition": power_provenance.get("A_definition", "not_declared"),
            "limitation": (
                "The audit preserves caller-declared sign/normalization provenance and "
                "does not infer it from variable names."
            ),
        },
    }
    wavelength: dict[str, Any] = {
        "requested_value": wavelength_value,
        "requested_unit": wavelength_unit,
        "parameter_expression": parameter_value,
    }
    field = None
    loss_result: list[dict[str, Any]] = []
    declared_flux_result: dict[str, Any] = {"state": "not_requested"}
    internal_absorption_result: dict[str, Any] = {"state": "not_requested"}
    if solve_error:
        measurement_errors.append({"code": "solve_failed", "error": solve_error})
    else:
        for label, expression in power_expressions.items():
            try:
                value = _single_complex(model.evaluate(expression), expression)
                power[f"{label}_raw"] = _json_number(value)
                power[label] = _single_real(value, expression)
            except FloatingPointError as exc:
                integrity_errors.append(
                    {"code": "nonfinite_power", "quantity": label, "error": str(exc)}
                )
            except Exception as exc:
                measurement_errors.append(
                    {
                        "code": "power_expression_unavailable",
                        "quantity": label,
                        "expression": expression,
                        "error": str(exc)[:500],
                    }
                )
        if all(name in power for name in ("R", "T", "A")):
            power["complete"] = True
            power["R_plus_T_plus_A"] = power["R"] + power["T"] + power["A"]
            power["closure_residual"] = power["R_plus_T_plus_A"] - 1.0
            power["closure_abs"] = abs(power["closure_residual"])
            power["one_minus_R_minus_T"] = 1.0 - power["R"] - power["T"]
        else:
            power["complete"] = False
        try:
            requested_m = _single_real(model.evaluate(parameter_value), parameter_value)
            controls = model.evaluate([wavelength_parameter, f"c_const/{physics_tag}.freq"])
            evaluated_raw = _single_complex(controls[0], wavelength_parameter)
            solved_raw = _single_complex(controls[1], f"c_const/{physics_tag}.freq")
            evaluated_parameter = _single_real(evaluated_raw, wavelength_parameter)
            solved_frequency_wavelength = _single_real(solved_raw, f"c_const/{physics_tag}.freq")
            evaluated_to_solved = evaluated_parameter - solved_frequency_wavelength
            requested_to_evaluated = requested_m - evaluated_parameter
            requested_to_solved = requested_m - solved_frequency_wavelength
            absolute_difference = max(
                abs(evaluated_to_solved),
                abs(requested_to_evaluated),
                abs(requested_to_solved),
            )
            wavelength.update(
                {
                    "complete": True,
                    "requested_m": requested_m,
                    "evaluated_parameter_m": evaluated_parameter,
                    "solved_frequency_wavelength_m": solved_frequency_wavelength,
                    "signed_difference_m": evaluated_to_solved,
                    "requested_to_evaluated_difference_m": requested_to_evaluated,
                    "requested_to_solved_difference_m": requested_to_solved,
                    "evaluated_to_solved_difference_m": evaluated_to_solved,
                    "absolute_difference_m": absolute_difference,
                    "relative_difference": (
                        None if requested_m == 0 else absolute_difference / abs(requested_m)
                    ),
                    "raw": {
                        "evaluated_parameter": _json_number(evaluated_raw),
                        "solved_frequency_wavelength": _json_number(solved_raw),
                    },
                }
            )
        except FloatingPointError as exc:
            integrity_errors.append({"code": "nonfinite_wavelength_control", "error": str(exc)})
        except Exception as exc:
            wavelength["complete"] = False
            measurement_errors.append(
                {"code": "wavelength_controls_unavailable", "error": str(exc)[:500]}
            )
        for item in losses:
            record = dict(item)
            try:
                value = _single_complex(model.evaluate(item["expression"]), item["expression"])
                record["raw"] = _json_number(value)
                record["value"] = _single_real(value, item["expression"])
                normalization = item.get("normalization_expression")
                if normalization:
                    normalizer = _single_complex(model.evaluate(normalization), normalization)
                    record["normalization_raw"] = _json_number(normalizer)
                    normalizer_real = _single_real(normalizer, normalization)
                    record["normalized_value"] = (
                        None if normalizer_real == 0 else record["value"] / normalizer_real
                    )
            except FloatingPointError as exc:
                integrity_errors.append(
                    {"code": "nonfinite_loss", "label": item["label"], "error": str(exc)}
                )
            except Exception as exc:
                record["error"] = str(exc)[:500]
                measurement_errors.append(
                    {
                        "code": "loss_expression_unavailable",
                        "label": item["label"],
                        "error": str(exc)[:500],
                    }
                )
            loss_result.append(record)
        if flux_declaration is not None:
            try:
                declared_flux_result = _evaluate_declared_plane_flux(model, flux_declaration)
            except FloatingPointError as exc:
                declared_flux_result = {
                    "state": "unknown",
                    "declaration": flux_declaration,
                    "error": str(exc),
                }
                integrity_errors.append(
                    {"code": "nonfinite_declared_plane_flux", "error": str(exc)}
                )
            except Exception as exc:
                declared_flux_result = {
                    "state": "unknown",
                    "declaration": flux_declaration,
                    "error": str(exc)[:500],
                }
                measurement_errors.append(
                    {"code": "declared_plane_flux_unavailable", "error": str(exc)[:500]}
                )
        if internal_absorption_declaration is not None:
            try:
                internal_absorption_result = _evaluate_internal_absorption(
                    model, internal_absorption_declaration
                )
            except FloatingPointError as exc:
                internal_absorption_result = {
                    "state": "unknown",
                    "declaration": internal_absorption_declaration,
                    "error": str(exc),
                    "physical_flux_closure_eligible": False,
                }
                integrity_errors.append(
                    {"code": "nonfinite_internal_absorption", "error": str(exc)}
                )
            except Exception as exc:
                internal_absorption_result = {
                    "state": "unknown",
                    "declaration": internal_absorption_declaration,
                    "error": str(exc)[:500],
                    "physical_flux_closure_eligible": False,
                }
                measurement_errors.append(
                    {"code": "internal_absorption_unavailable", "error": str(exc)[:500]}
                )
        try:
            field = _sample_structure_field(
                model,
                component=component,
                physics_tag=physics_tag,
                coordinate_range=coordinate_range,
                domain_ids=domains,
                named_selection=top_air_selection,
            )
        except FloatingPointError as exc:
            integrity_errors.append({"code": "nonfinite_field", "error": str(exc)})
        except Exception as exc:
            measurement_errors.append(
                {"code": "top_air_field_unavailable", "error": str(exc)[:500]}
            )

    evidence_level = (
        "incident_reference"
        if air_reference is not None
        else ("structure_total_field" if field else "label_only")
    )
    mesh_after = _mesh_state(component)
    mesh = {
        **mesh_after,
        "before": mesh_before,
        "unchanged_during_audit": mesh_before == mesh_after,
    }
    source_hash_after = _sha256_file(source_path)
    source_unchanged = source_hash_after == source_hash_before
    if not source_unchanged:
        integrity_errors.append(
            {"code": "source_hash_drift", "before": source_hash_before, "after": source_hash_after}
        )
    ownership_after = ownership_manager.status(session_state=session_state or {})
    if ownership_after.get("collision"):
        integrity_errors.append({"code": "cleanup_ownership_collision"})

    measurement = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "config_id": config_id,
        "provenance": {
            "model_name": model_name,
            "source_path": str(source_path),
            "source_sha256_before": source_hash_before,
            "source_sha256_after": source_hash_after,
            "config_sha256": config_sha256,
            "component_tag": component_tag,
            "physics_tag": physics_tag,
            "study_tag": study_tag,
            "study_step_tag": study_step_tag,
            "active_profile": active_profile,
        },
        "solve": {"ran": solve_error is None, "seconds": solve_seconds, "error": solve_error},
        "model_configuration": {
            "topology": {
                "space_dimension": preflight.get("topology", {}).get("space_dimension"),
                "domain_count": preflight.get("topology", {}).get("domain_count"),
                "boundary_count": preflight.get("topology", {}).get("boundary_count"),
                "form_finalization": preflight.get("topology", {}).get("form_finalization"),
            },
            "periodicity": preflight.get("periodicity"),
            "ports": preflight.get("ports"),
            "incidence": preflight.get("incidence"),
            "study_controls": preflight.get("wavelength"),
        },
        "wavelength": wavelength,
        "power": power,
        "declared_plane_flux": declared_flux_result,
        "internal_absorption_consistency": internal_absorption_result,
        "losses": {
            "items": loss_result,
            "normalization_limitation": (
                "Only caller-declared normalization expressions are used; no whole-model "
                "loss integration is inferred."
            ),
        },
        "polarization": {
            "evidence_level": evidence_level,
            "structure_total_field": field,
            "incident_reference": air_reference,
            "warnings": reference_warnings,
        },
        "mesh": mesh,
        "integrity": {
            "source_unchanged": source_unchanged,
            "ownership_after": {
                "collision": bool(ownership_after.get("collision")),
                "lease": ownership_after.get("lease"),
            },
            "cleanup": (
                "MCP session retained; ownership remains explicitly tracked by derived geometry."
            ),
        },
        "measurement_errors": measurement_errors,
        "integrity_errors": integrity_errors,
    }
    physical_evidence = build_point_audit_physical_evidence(
        {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "config_id": config_id,
            "config_sha256": config_sha256,
            "source_sha256": source_hash_before,
            "measurement": measurement,
        }
    )
    if integrity_errors:
        audit_status = "integrity_blocked"
    elif measurement_errors:
        audit_status = "measurement_partial"
    else:
        audit_status = "measurement_complete"
    if policy is None:
        assessment = {
            "mode": "evidence_only",
            "project_verdict": None,
            "long_sweep_recommendation": None,
        }
    elif policy.get("schema_name") == VALIDATION_POLICY_SCHEMA_NAME:
        policy_evaluation = evaluate_physical_evidence_policy(physical_evidence, policy)
        assessment = {
            "mode": "strict_physical_evidence_policy",
            "project_verdict": policy_evaluation["overall"],
            "policy_evaluation": policy_evaluation,
        }
        audit_status = "policy_evaluated" if not integrity_errors else "integrity_blocked"
    else:
        policy_evaluation = evaluate_validation_policy(measurement, policy)
        assessment = {
            "mode": "explicit_policy",
            "project_verdict": policy_evaluation["overall"],
            "policy_evaluation": policy_evaluation,
            "policy_format": "legacy_point_audit_v1",
            "migration_semantics": "preserved_without_reinterpretation",
        }
        audit_status = "policy_evaluated" if not integrity_errors else "integrity_blocked"

    row = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "config_id": config_id,
        "audit_status": audit_status,
        "source_sha256": source_hash_before,
        "config_sha256": config_sha256,
        "requested_wavelength_m": wavelength.get("requested_m"),
        "evaluated_wavelength_m": wavelength.get("evaluated_parameter_m"),
        "solved_frequency_wavelength_m": wavelength.get("solved_frequency_wavelength_m"),
        "R": power.get("R"),
        "T": power.get("T"),
        "A": power.get("A"),
        "R_plus_T_plus_A": power.get("R_plus_T_plus_A"),
        "one_minus_R_minus_T": power.get("one_minus_R_minus_T"),
        "polarization_evidence_level": evidence_level,
        "mesh_elements": mesh.get("element_count"),
        "solve_seconds": solve_seconds,
        "error_count": len(measurement_errors),
        "integrity_error_count": len(integrity_errors),
        "physical_evidence_sha256": physical_evidence["contract_sha256"],
    }
    manifest = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "audit_id": audit_id,
        "config_id": config_id,
        "audit_status": audit_status,
        "created_at_epoch": time.time(),
        "measurement": measurement,
        "physical_evidence": physical_evidence,
        "assessment": assessment,
        "validation_policy": policy,
        "validation_policy_provenance": policy_provenance,
        "config_spec": config_spec,
        "config_sha256": config_sha256,
        "preflight": preflight,
        "preflight_summary": {
            "inspection_status": preflight["inspection_status"],
            "evidence_codes": {
                level: [item["code"] for item in preflight["evidence"][level]]
                for level in ("observations", "warnings", "unknowns", "integrity_errors")
            },
        },
        "artifacts": {"csv": str(csv_path), "manifest": str(manifest_path)},
    }
    return {
        "success": True,
        "audit_status": audit_status,
        "assessment": assessment,
        "measurement": measurement,
        "physical_evidence": physical_evidence,
        "artifacts": {
            "directory": str(directory),
            "csv": str(csv_path),
            "manifest": str(manifest_path),
        },
        "_finalization": {
            "row": row,
            "manifest": manifest,
            "csv_path": csv_path,
            "manifest_path": manifest_path,
        },
    }


def _finalize_point_audit_result(
    result: dict[str, Any], restoration_errors: list[str]
) -> dict[str, Any]:
    finalization = result.pop("_finalization", None)
    if finalization is None:
        return result
    measurement = result["measurement"]
    measurement.setdefault("integrity", {})["model_state_restored"] = not restoration_errors
    if restoration_errors:
        measurement["integrity_errors"].append(
            {
                "code": "model_state_restore_failed",
                "errors": restoration_errors,
            }
        )
        result["audit_status"] = "integrity_blocked"
        assessment = dict(result["assessment"])
        assessment.pop("policy_evaluation", None)
        assessment.update(project_verdict=None, integrity_blocked=True)
        result["assessment"] = assessment

    manifest = finalization["manifest"]
    physical_evidence = build_point_audit_physical_evidence(
        {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "config_id": manifest["config_id"],
            "config_sha256": manifest["config_sha256"],
            "source_sha256": measurement["provenance"]["source_sha256_before"],
            "measurement": measurement,
        }
    )
    result["physical_evidence"] = physical_evidence
    row = finalization["row"]
    row.update(
        audit_status=result["audit_status"],
        integrity_error_count=len(measurement["integrity_errors"]),
        physical_evidence_sha256=physical_evidence["contract_sha256"],
    )
    manifest.update(
        audit_status=result["audit_status"],
        measurement=measurement,
        physical_evidence=physical_evidence,
        assessment=result["assessment"],
    )
    _write_rows_csv(str(finalization["csv_path"]), list(row), [row], append=False)
    _atomic_write_json(finalization["manifest_path"], manifest)
    return result


@wraps(_run_wave_optics_point_audit_impl)
def run_wave_optics_point_audit(*args: Any, **kwargs: Any) -> dict[str, Any]:
    manifest_token = _ACTIVE_POINT_AUDIT_MANIFEST.set(None)
    mutation_token = _ACTIVE_POINT_AUDIT_MUTATION.set(None)
    restored = False
    restoration_errors: list[str] = []
    try:
        result = _run_wave_optics_point_audit_impl(*args, **kwargs)
        restoration_errors = _restore_point_audit_mutation()
        restored = True
        return _finalize_point_audit_result(result, restoration_errors)
    except Exception as exc:
        if not restored:
            restoration_errors = _restore_point_audit_mutation()
        manifest_path = _ACTIVE_POINT_AUDIT_MANIFEST.get()
        if manifest_path is not None:
            _terminalize_running_point_audit(
                manifest_path,
                exc,
                restoration_errors=restoration_errors,
            )
        if restoration_errors:
            raise RuntimeError(
                "Wave Optics point audit could not restore the caller model state"
            ) from exc
        raise
    finally:
        _ACTIVE_POINT_AUDIT_MUTATION.reset(mutation_token)
        _ACTIVE_POINT_AUDIT_MANIFEST.reset(manifest_token)


def _replace_clone_materials_with_air(
    clone: Any,
    *,
    component_tag: str,
    expected_material_tags: list[str],
    all_domain_ids: list[int],
) -> dict[str, Any]:
    component = clone.java.component(component_tag)
    materials = component.material()
    before = sorted(str(value) for value in list(materials.tags()))
    expected = sorted(
        _validate_tag(value, "expected_material_tags item") for value in expected_material_tags
    )
    if before != expected:
        raise ValueError(
            f"clone material tags differ from the exact caller declaration: {before} != {expected}"
        )
    for tag in before:
        materials.remove(tag)
    if list(materials.tags()):
        raise ValueError("clone material removal readback is not empty")
    air = materials.create("reference_air_material", "Common")
    group = air.propertyGroup("def")
    group.set("relpermittivity", "1")
    group.set("relpermeability", "1")
    group.set("electricconductivity", "0[S/m]")
    air.selection().set(all_domain_ids)
    expected_properties = {
        "relpermittivity": "1",
        "relpermeability": "1",
        "electricconductivity": "0[S/m]",
    }
    property_readback = {name: str(group.getString(name)) for name in expected_properties}
    if property_readback != expected_properties:
        raise ValueError(f"all-air material property readback differs: {property_readback}")
    after = sorted(str(value) for value in list(materials.tags()))
    if after != ["reference_air_material"]:
        raise ValueError(f"all-air clone material readback is unexpected: {after}")
    selected = sorted(int(value) for value in list(air.selection().entities()))
    if selected != all_domain_ids:
        raise ValueError(
            f"all-air material selection readback differs: {selected} != {all_domain_ids}"
        )
    return {
        "method": "all_air_clone",
        "removed_material_tags": before,
        "air_material_tag": "reference_air_material",
        "air_properties": property_readback,
        "domain_ids": all_domain_ids,
        "readback_complete": True,
    }


def _clone_record_dict(record: Any) -> dict[str, Any]:
    fields = (
        "derived_model_id",
        "model_name",
        "source_path",
        "source_sha256",
        "backing_path",
        "backing_sha256",
    )
    if isinstance(record, dict):
        return {field: record.get(field) for field in fields}
    return {field: getattr(record, field, None) for field in fields}


def run_wave_optics_reference_audit(
    source_model: Any,
    client: Any,
    *,
    model_name: str,
    component_tag: str,
    physics_tag: str,
    study_tag: str,
    study_step_tag: str,
    study_step_property: str,
    wavelength_value: float,
    wavelength_unit: str,
    wavelength_parameter: str,
    expected_source_sha256: str,
    config_id: str,
    reference_method: Literal["all_air_clone"],
    expected_material_tags: list[str],
    all_domain_ids: list[int],
    top_air_domain_ids: list[int],
    top_air_coordinate_range: dict[str, Any],
    target_axis: Literal["x", "y", "z"],
    aggregation: Literal["rms_abs", "median_abs"],
    artifact_dir: str | None,
    r_expression: str | None = None,
    t_expression: str | None = None,
    validation_policy: dict[str, Any] | None = None,
    clone_factory: Callable[..., tuple[Any, Any]] | None = None,
    clone_register: Callable[[Any, str | None], str] | None = None,
    clone_cleanup: Callable[[str], bool] | None = None,
    material_mutator: Callable[..., dict[str, Any]] = _replace_clone_materials_with_air,
    preflight_collector: Callable[..., dict[str, Any]] = collect_wave_optics_preflight,
) -> dict[str, Any]:
    """Solve a bounded all-air reference on a fresh clone and prove cleanup."""
    if reference_method != "all_air_clone":
        raise ValueError("reference_method must be exactly all_air_clone")
    component_tag = _validate_tag(component_tag, "component_tag")
    physics_tag = _validate_tag(physics_tag, "physics_tag")
    study_tag = _validate_tag(study_tag, "study_tag")
    study_step_tag = _validate_tag(study_step_tag, "study_step_tag")
    wavelength_parameter = _validate_tag(wavelength_parameter, "wavelength_parameter")
    if not isinstance(study_step_property, str) or not _TAG.fullmatch(study_step_property):
        raise ValueError("study_step_property must be one exact property name")
    if target_axis not in {"x", "y", "z"} or aggregation not in {"rms_abs", "median_abs"}:
        raise ValueError("target_axis/aggregation is unsupported")
    coordinate_range = _validate_coordinate_range(top_air_coordinate_range)
    domains = _strict_positive_entity_ids(all_domain_ids, "all_domain_ids")
    top_domains = _strict_positive_entity_ids(top_air_domain_ids, "top_air_domain_ids")
    if not domains or not top_domains or any(value <= 0 for value in domains + top_domains):
        raise ValueError(
            "all_domain_ids and top_air_domain_ids must be non-empty positive integer lists"
        )
    if not set(top_domains).issubset(domains):
        raise ValueError("top_air_domain_ids must be a subset of all_domain_ids")
    wavelength_value = float(wavelength_value)
    if not math.isfinite(wavelength_value) or wavelength_value <= 0.0:
        raise ValueError("wavelength_value must be finite and positive")
    if (
        not isinstance(wavelength_unit, str)
        or not wavelength_unit.strip()
        or len(wavelength_unit) > 32
    ):
        raise ValueError("wavelength_unit must be non-empty")
    if not isinstance(config_id, str) or not config_id.strip() or len(config_id) > 128:
        raise ValueError("config_id must be non-empty and at most 128 characters")
    expected_hash = expected_source_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ValueError("expected_source_sha256 must be exactly 64 hexadecimal characters")
    policy = (
        validate_validation_policy(validation_policy) if validation_policy is not None else None
    )
    root = _validate_ascii_dir(artifact_dir)
    root.mkdir(parents=True, exist_ok=True)
    source_path = Path(str(source_model.file())).resolve()
    source_hash_before = _sha256_file(source_path)
    if source_hash_before != expected_hash:
        return {
            "success": True,
            "audit_status": "integrity_blocked",
            "integrity_errors": [
                {
                    "code": "source_hash_mismatch",
                    "expected": expected_hash,
                    "actual": source_hash_before,
                }
            ],
        }
    r_expression = r_expression or f"{physics_tag}.Rtotal"
    t_expression = t_expression or f"{physics_tag}.Ttotal"
    _bounded_expression(r_expression, "r_expression")
    _bounded_expression(t_expression, "t_expression")
    config_spec = {
        "model_name": model_name,
        "component_tag": component_tag,
        "physics_tag": physics_tag,
        "study_tag": study_tag,
        "study_step_tag": study_step_tag,
        "study_step_property": study_step_property,
        "wavelength": {
            "value": wavelength_value,
            "unit": wavelength_unit,
            "parameter": wavelength_parameter,
        },
        "reference_method": reference_method,
        "expected_material_tags": sorted(expected_material_tags),
        "all_domain_ids": domains,
        "top_air_domain_ids": top_domains,
        "top_air_coordinate_range": coordinate_range,
        "target_axis": target_axis,
        "aggregation": aggregation,
        "R_expression": r_expression,
        "T_expression": t_expression,
    }
    config_sha256 = _canonical_hash(config_spec)
    audit_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    directory = root / re.sub(r"[^A-Za-z0-9_.-]", "_", config_id) / audit_id
    directory.mkdir(parents=True, exist_ok=False)
    manifest_path = directory / "reference.json"
    _atomic_write_json(manifest_path, {"audit_status": "running", "config_spec": config_spec})

    factory = clone_factory or (
        lambda source, active_client, new_name: create_derived_geometry_clone(
            source, active_client, new_name=new_name
        )
    )
    clone = None
    clone_name = None
    clone_record: dict[str, Any] = {}
    material_evidence = None
    preflight = None
    reference = None
    measurement_errors: list[dict[str, Any]] = []
    integrity_errors: list[dict[str, Any]] = []
    cleanup = {
        "attempted": False,
        "removed": False,
        "client_model_removed": False,
        "backing_file_removed": False,
    }
    started = time.perf_counter()
    try:
        clone, record = factory(source_model, client, f"reference_air_clone_{uuid.uuid4().hex[:8]}")
        clone_record = _clone_record_dict(record)
        clone_name = str(clone.name())
        if clone_register is not None:
            clone_name = clone_register(clone, clone_record.get("backing_path"))
        material_evidence = material_mutator(
            clone,
            component_tag=component_tag,
            expected_material_tags=expected_material_tags,
            all_domain_ids=domains,
        )
        preflight = preflight_collector(
            clone,
            model_name=clone_name,
            session_state={"connected": True},
            active_profile="wave_optics",
            expected_component_tag=component_tag,
            expected_physics_tag=physics_tag,
            expected_study_tag=study_tag,
            target_wavelength_parameter=wavelength_parameter,
        )
        if preflight.get("inspection_status") == "integrity_blocked":
            raise ValueError("all-air clone preflight is integrity_blocked")
        jm = clone.java
        component = jm.component(component_tag)
        study = jm.study(study_tag)
        parameter_value = f"{wavelength_value}[{wavelength_unit}]"
        jm.param().set(wavelength_parameter, parameter_value)
        study.feature().get(study_step_tag).set(study_step_property, wavelength_parameter)
        study.run()
        requested = _single_real(clone.evaluate(parameter_value), parameter_value)
        controls = clone.evaluate([wavelength_parameter, f"c_const/{physics_tag}.freq"])
        evaluated = _single_real(controls[0], wavelength_parameter)
        solved = _single_real(controls[1], f"c_const/{physics_tag}.freq")
        r_value = _single_real(clone.evaluate(r_expression), r_expression)
        t_value = _single_real(clone.evaluate(t_expression), t_expression)
        field = _sample_structure_field(
            clone,
            component=component,
            physics_tag=physics_tag,
            coordinate_range=coordinate_range,
            domain_ids=top_domains,
            named_selection=None,
        )
        amplitudes = {
            axis: float(field["component_statistics"][axis][aggregation])
            for axis in ("x", "y", "z")
        }
        transverse = max(value for axis, value in amplitudes.items() if axis != target_axis)
        ratio = amplitudes[target_axis] / max(transverse, sys.float_info.min)
        if not math.isfinite(ratio):
            raise FloatingPointError("reference polarization ratio is nonfinite")
        reference = {
            "config_id": config_id,
            "method": reference_method,
            "method_valid": True,
            "requested_wavelength_m": requested,
            "evaluated_wavelength_m": evaluated,
            "solved_frequency_wavelength_m": solved,
            "port_settings": {
                "ports": preflight.get("ports"),
                "incidence": preflight.get("incidence"),
            },
            "R": r_value,
            "T": t_value,
            "R_plus_T_residual_abs": abs(r_value + t_value - 1.0),
            "target_axis": target_axis,
            "aggregation": aggregation,
            "component_amplitudes": amplitudes,
            "target_to_transverse_ratio": ratio,
            "transverse_denominator_zero": transverse == 0.0,
            "field": field,
        }
    except FloatingPointError as exc:
        integrity_errors.append({"code": "nonfinite_reference_evidence", "error": str(exc)[:500]})
    except Exception as exc:
        measurement_errors.append({"code": "reference_audit_failed", "error": str(exc)[:1000]})
    finally:
        if clone is not None:
            cleanup["attempted"] = True
            if clone_cleanup is not None and clone_name is not None:
                try:
                    cleanup["client_model_removed"] = bool(clone_cleanup(clone_name))
                except Exception as exc:
                    cleanup["registered_remove_error"] = str(exc)[:500]
            if not cleanup["client_model_removed"]:
                try:
                    client.remove(clone)
                    cleanup["client_model_removed"] = True
                except Exception as exc:
                    cleanup["direct_remove_error"] = str(exc)[:500]
            backing = clone_record.get("backing_path")
            if backing:
                path = Path(backing)
                try:
                    path.unlink(missing_ok=True)
                    cleanup["backing_file_removed"] = not path.exists()
                except OSError as exc:
                    cleanup["backing_file_error"] = str(exc)[:500]
                try:
                    path.parent.rmdir()
                except OSError:
                    pass
            else:
                cleanup["backing_file_removed"] = True
            cleanup["removed"] = bool(
                cleanup["client_model_removed"] and cleanup["backing_file_removed"]
            )
        if clone is not None and not cleanup["removed"]:
            integrity_errors.append(
                {"code": "reference_clone_cleanup_unproved", "cleanup": cleanup}
            )

    source_hash_after = _sha256_file(source_path)
    source_unchanged = source_hash_after == source_hash_before
    if not source_unchanged:
        integrity_errors.append(
            {"code": "source_hash_drift", "before": source_hash_before, "after": source_hash_after}
        )
    method_valid = bool(
        reference
        and reference.get("method_valid")
        and material_evidence
        and cleanup["removed"]
        and source_unchanged
    )
    evidence = {
        "polarization.reference_air_method_valid": (
            {"state": "measured", "value": method_valid, "source": "wave_optics_reference_audit"}
        ),
        "polarization.target_to_transverse_ratio": (
            {
                "state": "measured",
                "value": reference["target_to_transverse_ratio"],
                "unit": "1",
                "source": "wave_optics_reference_audit",
            }
            if reference is not None
            else {"state": "unknown", "limitations": ["Reference field sampling was incomplete."]}
        ),
        "reference_air.R": (
            {"state": "measured", "value": reference["R"], "unit": "1", "expression": r_expression}
            if reference is not None
            else {"state": "unknown"}
        ),
        "reference_air.T": (
            {"state": "measured", "value": reference["T"], "unit": "1", "expression": t_expression}
            if reference is not None
            else {"state": "unknown"}
        ),
        "integrity.source_unchanged": {"state": "measured", "value": source_unchanged},
        "integrity.clone_cleanup_proved": {"state": "measured", "value": cleanup["removed"]},
    }
    physical_evidence = build_physical_evidence(
        {
            "schema_name": PHYSICAL_EVIDENCE_SCHEMA_NAME,
            "schema_version": PHYSICAL_EVIDENCE_SCHEMA_VERSION,
            "artifact_type": "wave_optics_reference_audit",
            "producer": {"tool": "wave_optics_reference_audit", "tool_schema_version": "1"},
            "identity": {
                "config_id": config_id,
                "config_sha256": config_sha256,
                "source_sha256": source_hash_before,
            },
            "model": {
                "component_tag": component_tag,
                "physics_tag": physics_tag,
                "study_tag": study_tag,
                "study_step_tag": study_step_tag,
            },
            "evidence": evidence,
            "limitations": [
                "Physical classification requires caller policy; the all-air clone does not "
                "validate the target structure."
            ],
        }
    )
    assessment = (
        evaluate_physical_evidence_policy(physical_evidence, policy)
        if policy is not None
        else {"mode": "evidence_only", "overall": None}
    )
    audit_status = (
        "integrity_blocked"
        if integrity_errors
        else ("measurement_partial" if measurement_errors else "measurement_complete")
    )
    manifest = {
        "schema_version": "1",
        "audit_status": audit_status,
        "config_spec": config_spec,
        "config_sha256": config_sha256,
        "clone_provenance": clone_record,
        "material_replacement": material_evidence,
        "preflight": preflight,
        "reference": reference,
        "cleanup": cleanup,
        "source_sha256_before": source_hash_before,
        "source_sha256_after": source_hash_after,
        "source_unchanged": source_unchanged,
        "measurement_errors": measurement_errors,
        "integrity_errors": integrity_errors,
        "physical_evidence": physical_evidence,
        "assessment": assessment,
        "elapsed_seconds": time.perf_counter() - started,
    }
    _atomic_write_json(manifest_path, manifest)
    return {
        "success": True,
        "audit_status": audit_status,
        "reference": reference,
        "cleanup": cleanup,
        "physical_evidence": physical_evidence,
        "assessment": assessment,
        "artifacts": {"directory": str(directory), "manifest": str(manifest_path)},
    }


def register_wave_optics_audit_tools(mcp: MCPServer) -> None:
    """Register the public one-point Wave Optics physical evidence audit."""

    @mcp.tool()
    def wave_optics_point_audit(
        model_name: str,
        component_tag: str,
        physics_tag: str,
        study_tag: str,
        wavelength_value: float,
        wavelength_unit: str,
        wavelength_parameter: str,
        study_step_tag: str,
        expected_source_sha256: str,
        config_id: str,
        top_air_coordinate_range: CoordinateLimits,
        study_step_property: str = "plist",
        artifact_dir: Optional[str] = None,
        r_expression: Optional[str] = None,
        t_expression: Optional[str] = None,
        a_expression: Optional[str] = None,
        top_air_selection: Optional[str] = None,
        top_air_domain_ids: Optional[list[int]] = None,
        loss_map: Optional[list[LossSpecification]] = None,
        power_provenance: Optional[PowerProvenance] = None,
        declared_plane_flux: Optional[DeclaredPlaneFlux] = None,
        internal_absorption: Optional[InternalAbsorption] = None,
        air_reference_artifact_path: Optional[str] = None,
        air_reference_config_id: Optional[str] = None,
        validation_policy: Optional[ValidationPolicy | StrictValidationPolicy] = None,
        validation_policy_path: Optional[str] = None,
    ) -> dict[str, Any]:
        """Solve one wavelength and optionally evaluate a declared policy."""
        if not isinstance(model_name, str) or not model_name.strip():
            return {"success": False, "error": "model_name must be exact and non-empty"}
        model = session_manager.get_model(model_name)
        if model is None:
            return {"success": False, "error": f"Model not found: {model_name}"}
        from .derived_geometry import derived_model_validation_status

        derived_status = derived_model_validation_status(model_name)
        if not derived_status["validation_allowed"]:
            return {
                "success": False,
                "error": "dirty derived model is forbidden from validated point audits",
                "derived_model": derived_status,
            }
        try:
            ownership_preflight = session_manager.preflight_long_operation(
                model_path=str(model.file()) if model.file() else None
            )
            profile_selection = getattr(mcp, "profile_selection", None)
            active_profile = getattr(profile_selection, "name", "unknown")
            return run_wave_optics_point_audit(
                model,
                model_name=model_name,
                component_tag=component_tag,
                physics_tag=physics_tag,
                study_tag=study_tag,
                wavelength_value=wavelength_value,
                wavelength_unit=wavelength_unit,
                wavelength_parameter=wavelength_parameter,
                study_step_tag=study_step_tag,
                study_step_property=study_step_property,
                expected_source_sha256=expected_source_sha256,
                config_id=config_id,
                artifact_dir=artifact_dir,
                r_expression=r_expression,
                t_expression=t_expression,
                a_expression=a_expression,
                top_air_selection=top_air_selection,
                top_air_domain_ids=top_air_domain_ids,
                top_air_coordinate_range=top_air_coordinate_range,
                loss_map=loss_map,
                power_provenance=power_provenance,
                declared_plane_flux=declared_plane_flux,
                internal_absorption=internal_absorption,
                air_reference_artifact_path=air_reference_artifact_path,
                air_reference_config_id=air_reference_config_id,
                validation_policy=validation_policy,
                validation_policy_path=validation_policy_path,
                session_state=session_manager.get_status(),
                loaded_source_identity=session_manager.get_model_source_identity(model_name),
                active_profile=active_profile,
                ownership_preflight=ownership_preflight,
            )
        except (ValueError, TypeError, FileNotFoundError, json.JSONDecodeError) as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            return {
                "success": False,
                "error": f"Wave Optics point audit failed safely: {str(exc)[:1000]}",
            }

    @mcp.tool()
    def wave_optics_reference_audit(
        model_name: str,
        component_tag: str,
        physics_tag: str,
        study_tag: str,
        wavelength_value: float,
        wavelength_unit: str,
        wavelength_parameter: str,
        study_step_tag: str,
        expected_source_sha256: str,
        config_id: str,
        expected_material_tags: list[str],
        all_domain_ids: list[int],
        top_air_domain_ids: list[int],
        top_air_coordinate_range: CoordinateLimits,
        target_axis: Literal["x", "y", "z"],
        aggregation: Literal["rms_abs", "median_abs"],
        reference_method: Literal["all_air_clone"] = "all_air_clone",
        study_step_property: str = "plist",
        artifact_dir: Optional[str] = None,
        r_expression: Optional[str] = None,
        t_expression: Optional[str] = None,
        validation_policy: Optional[StrictValidationPolicy] = None,
    ) -> dict[str, Any]:
        """Solve one all-air reference on a fresh clone and prove clone cleanup."""
        source = session_manager.get_model(model_name)
        client = session_manager.client
        if source is None or client is None:
            return {"success": False, "error": "source model or COMSOL client unavailable"}
        try:
            ownership_preflight = session_manager.preflight_long_operation(
                model_path=str(source.file()) if source.file() else None
            )
            if not ownership_preflight.get("ready"):
                return {
                    "success": True,
                    "audit_status": "integrity_blocked",
                    "integrity_errors": [
                        {
                            "code": "solver_ownership_blocked",
                            "blockers": ownership_preflight.get("blockers", []),
                        }
                    ],
                }
            return run_wave_optics_reference_audit(
                source,
                client,
                model_name=model_name,
                component_tag=component_tag,
                physics_tag=physics_tag,
                study_tag=study_tag,
                study_step_tag=study_step_tag,
                study_step_property=study_step_property,
                wavelength_value=wavelength_value,
                wavelength_unit=wavelength_unit,
                wavelength_parameter=wavelength_parameter,
                expected_source_sha256=expected_source_sha256,
                config_id=config_id,
                reference_method=reference_method,
                expected_material_tags=expected_material_tags,
                all_domain_ids=all_domain_ids,
                top_air_domain_ids=top_air_domain_ids,
                top_air_coordinate_range=top_air_coordinate_range,
                target_axis=target_axis,
                aggregation=aggregation,
                artifact_dir=artifact_dir,
                r_expression=r_expression,
                t_expression=t_expression,
                validation_policy=validation_policy,
                clone_register=lambda clone, path: session_manager.add_model(
                    clone, cleanup_path=path
                ),
                clone_cleanup=session_manager.remove_model,
            )
        except (ValueError, TypeError, FileNotFoundError, json.JSONDecodeError) as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            return {
                "success": False,
                "error": f"Wave Optics reference audit failed safely: {str(exc)[:1000]}",
            }


__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "evaluate_validation_policy",
    "register_wave_optics_audit_tools",
    "run_wave_optics_point_audit",
    "run_wave_optics_reference_audit",
]
