"""Pure validation for bounded durable physical-evidence matrices."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from comsol_mcp.contracts.job_submission import MAX_VALIDATION_MATRIX_POINTS
from comsol_mcp.contracts.native_condition import (
    NATIVE_CONDITION_COLLECTOR,
    NATIVE_CONDITION_COLLECTORS,
    NATIVE_FORWARD_COLLECTOR,
    normalize_native_condition_request,
)
from comsol_mcp.contracts.startup_admission import normalize_robust_startup_policy
from comsol_mcp.evidence.field_matrix import (
    MATRIX_FIELD_COLLECTOR,
    normalize_validation_matrix_field_inputs,
)
from comsol_mcp.utils.validation import strict_json_number

from .resource_admission import normalize_resource_policy
from .store import JOB_SCHEMA_VERSION

MAX_COLLECTORS_PER_POINT = 4
MAX_EXPECTED_ARTIFACTS_PER_POINT = 16
MAX_COLLECTOR_INPUT_BYTES = 64 * 1024
MAX_SPEC_BYTES = 512 * 1024
MAX_VALIDATION_MATRIX_CORES = 1024

SUPPORTED_VALIDATION_COLLECTORS = frozenset(
    {
        "wave_optics_point_audit",
        "wave_optics_reference_audit",
        MATRIX_FIELD_COLLECTOR,
        NATIVE_CONDITION_COLLECTOR,
        NATIVE_FORWARD_COLLECTOR,
    }
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")
_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")


def _fingerprint(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} keys must be strings")
    return dict(value)


def _positive_integer(value: object, name: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must not exceed {maximum}")
    return value


def _finite_number(value: object, name: str, *, positive: bool = False) -> float:
    return strict_json_number(value, name, positive=positive)


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a bounded portable identifier")
    return value


def _normalize_wavelength(value: object, point_name: str) -> dict[str, Any]:
    raw = _mapping(value, f"{point_name}.wavelength")
    allowed = {"value", "unit", "parameter"}
    unknown = sorted(set(raw) - allowed)
    if unknown or set(raw) != allowed:
        raise ValueError(f"{point_name}.wavelength requires exactly value, unit, and parameter")
    unit = raw["unit"]
    if not isinstance(unit, str) or not unit.strip() or len(unit) > 32:
        raise ValueError(f"{point_name}.wavelength.unit must be a bounded nonempty string")
    if unit.strip() not in {"m", "um", "nm"}:
        raise ValueError(f"{point_name}.wavelength.unit must be m, um, or nm")
    parameter = raw["parameter"]
    if not isinstance(parameter, str) or not _TAG.fullmatch(parameter):
        raise ValueError(f"{point_name}.wavelength.parameter must be one exact tag")
    return {
        "value": _finite_number(raw["value"], f"{point_name}.wavelength.value", positive=True),
        "unit": unit.strip(),
        "parameter": parameter,
    }


def _normalize_incidence(value: object, point_name: str) -> dict[str, Any]:
    raw = _mapping(value, f"{point_name}.incidence")
    allowed = {"theta_degrees", "phi_degrees", "polarization"}
    unknown = sorted(set(raw) - allowed)
    if unknown or set(raw) != allowed:
        raise ValueError(
            f"{point_name}.incidence requires exactly theta_degrees, phi_degrees, and polarization"
        )
    polarization = raw["polarization"]
    if not isinstance(polarization, str) or not polarization.strip() or len(polarization) > 64:
        raise ValueError(f"{point_name}.incidence.polarization must be a bounded label")
    return {
        "theta_degrees": _finite_number(
            raw["theta_degrees"], f"{point_name}.incidence.theta_degrees"
        ),
        "phi_degrees": _finite_number(raw["phi_degrees"], f"{point_name}.incidence.phi_degrees"),
        "polarization": polarization.strip(),
        "polarization_evidence": "label_only",
    }


def _require_string_keys(value: object, name: str) -> None:
    # json.dumps silently coerces non-string mapping keys to strings, so the
    # strict string-key contract must be checked before the round-trip.
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError(f"{name} keys must be strings")
        for key, item in value.items():
            _require_string_keys(item, f"{name}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _require_string_keys(item, f"{name}[{index}]")


def _normalize_json_object(value: object, name: str) -> dict[str, Any]:
    raw = _mapping(value, name)
    _require_string_keys(raw, name)
    try:
        payload = json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain only finite JSON values") from exc
    if len(payload) > MAX_COLLECTOR_INPUT_BYTES:
        raise ValueError(
            f"{name} exceeds the {MAX_COLLECTOR_INPUT_BYTES}-byte collector-input limit"
        )
    return json.loads(payload.decode("utf-8"))


def _normalize_collectors(value: object, point_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{point_name}.collectors must be a nonempty list")
    if len(value) > MAX_COLLECTORS_PER_POINT:
        raise ValueError(
            f"{point_name}.collectors must not exceed {MAX_COLLECTORS_PER_POINT} entries"
        )
    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, item in enumerate(value):
        name = f"{point_name}.collectors[{index}]"
        raw = _mapping(item, name)
        if set(raw) != {"name", "inputs"}:
            raise ValueError(f"{name} requires exactly name and inputs")
        collector_name = raw["name"]
        if (
            not isinstance(collector_name, str)
            or collector_name not in SUPPORTED_VALIDATION_COLLECTORS
        ):
            raise ValueError(f"{name}.name is not a supported validation collector")
        if collector_name in names:
            raise ValueError(f"{point_name}.collectors must not contain duplicates")
        names.add(collector_name)
        inputs = _normalize_json_object(raw["inputs"], f"{name}.inputs")
        if collector_name == MATRIX_FIELD_COLLECTOR:
            inputs = normalize_validation_matrix_field_inputs(inputs)
        elif collector_name in NATIVE_CONDITION_COLLECTORS:
            inputs = normalize_native_condition_request(
                inputs, gradients=collector_name == NATIVE_CONDITION_COLLECTOR
            )
        normalized.append(
            {
                "name": collector_name,
                "inputs": inputs,
            }
        )
    return normalized


def _normalize_point(value: object, index: int, source_sha256: str) -> dict[str, Any]:
    name = f"points[{index}]"
    raw = _mapping(value, name)
    allowed = {
        "point_id",
        "configuration_sha256",
        "wavelength",
        "incidence",
        "collectors",
        "expected_artifact_ids",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown or not {
        "point_id",
        "configuration_sha256",
        "wavelength",
        "collectors",
        "expected_artifact_ids",
    } <= set(raw):
        raise ValueError(f"{name} has missing or unsupported fields")
    configuration_sha256 = raw["configuration_sha256"]
    if not isinstance(configuration_sha256, str) or not _SHA256.fullmatch(configuration_sha256):
        raise ValueError(f"{name}.configuration_sha256 must be exactly 64 hexadecimal characters")
    artifacts = raw["expected_artifact_ids"]
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError(f"{name}.expected_artifact_ids must be a nonempty list")
    if len(artifacts) > MAX_EXPECTED_ARTIFACTS_PER_POINT:
        raise ValueError(
            f"{name}.expected_artifact_ids must not exceed "
            f"{MAX_EXPECTED_ARTIFACTS_PER_POINT} entries"
        )
    artifact_ids = [
        _identifier(item, f"{name}.expected_artifact_ids[{artifact_index}]")
        for artifact_index, item in enumerate(artifacts)
    ]
    if len(set(artifact_ids)) != len(artifact_ids):
        raise ValueError(f"{name}.expected_artifact_ids must not contain duplicates")
    collectors = _normalize_collectors(raw["collectors"], name)
    if len(artifact_ids) != len(collectors):
        raise ValueError(f"{name} requires exactly one expected artifact ID per collector")
    field_indices = [
        index
        for index, collector in enumerate(collectors)
        if collector["name"] == MATRIX_FIELD_COLLECTOR
    ]
    if field_indices:
        field_index = field_indices[0]
        source_artifact_id = collectors[field_index]["inputs"]["source_artifact_id"]
        if source_artifact_id not in artifact_ids:
            raise ValueError(f"{name} field source_artifact_id is not declared")
        source_index = artifact_ids.index(source_artifact_id)
        if source_index >= field_index:
            raise ValueError(f"{name} field source artifact must precede the field collector")
        if collectors[source_index]["name"] != "wave_optics_point_audit":
            raise ValueError(f"{name} field source artifact must belong to point audit")
    point = {
        "point_id": _identifier(raw["point_id"], f"{name}.point_id"),
        "configuration_sha256": configuration_sha256.lower(),
        "wavelength": _normalize_wavelength(raw["wavelength"], name),
        "incidence": (
            _normalize_incidence(raw["incidence"], name)
            if "incidence" in raw and raw["incidence"] is not None
            else None
        ),
        "collectors": collectors,
        "expected_artifact_ids": artifact_ids,
    }
    for collector in collectors:
        if collector["name"] in NATIVE_CONDITION_COLLECTORS:
            from comsol_mcp.contracts.native_condition import bind_native_condition_point

            bind_native_condition_point(
                collector["inputs"],
                point,
                source_sha256,
                gradients=collector["name"] == NATIVE_CONDITION_COLLECTOR,
            )
    point["point_fingerprint"] = _fingerprint(
        {
            "source_model_sha256": source_sha256,
            "configuration_sha256": point["configuration_sha256"],
            "wavelength": point["wavelength"],
            "incidence": point["incidence"],
            "collectors": point["collectors"],
        }
    )
    return point


def normalize_validation_matrix_spec(raw_spec: object) -> dict[str, Any]:
    """Normalize one immutable matrix specification without importing COMSOL."""
    raw = _mapping(raw_spec, "validation_matrix specification")
    allowed = {
        "job_type",
        "source_model_path",
        "points",
        "point_limit",
        "resource_policy",
        "cores",
        "version",
        "max_retries",
        "continue_on_error",
        "startup_admission",
        "comsol_temporary_directory",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"Unsupported validation_matrix fields: {unknown}")
    if raw.get("job_type") != "validation_matrix":
        raise ValueError("job_type must be 'validation_matrix'")
    source_value = raw.get("source_model_path")
    if not isinstance(source_value, str) or not source_value.strip():
        raise ValueError("source_model_path must be a nonempty string")
    source = Path(source_value).expanduser().resolve()
    if not source.is_file() or source.suffix.casefold() != ".mph":
        raise ValueError("source_model_path must name an existing MPH file")
    source_sha256 = _sha256_file(source)

    point_limit = _positive_integer(
        raw.get("point_limit"),
        "point_limit",
        maximum=MAX_VALIDATION_MATRIX_POINTS,
    )
    points = raw.get("points")
    if not isinstance(points, list) or not points:
        raise ValueError("points must be a nonempty list")
    if len(points) > point_limit:
        raise ValueError("points exceed the caller-declared point_limit")
    normalized_points = [
        _normalize_point(point, index, source_sha256) for index, point in enumerate(points)
    ]
    point_ids = [point["point_id"] for point in normalized_points]
    if len(set(point_ids)) != len(point_ids):
        raise ValueError("points must have unique point_id values")
    fingerprints = [point["point_fingerprint"] for point in normalized_points]
    if len(set(fingerprints)) != len(fingerprints):
        raise ValueError("points must have unique exact configuration identities")
    artifact_ids = [
        artifact for point in normalized_points for artifact in point["expected_artifact_ids"]
    ]
    if len(set(artifact_ids)) != len(artifact_ids):
        raise ValueError("expected_artifact_ids must be unique across the matrix")

    policy = normalize_resource_policy(raw.get("resource_policy"))
    if policy is None:
        raise ValueError("resource_policy is required for validation_matrix jobs")
    rules = policy["rules"]
    wall_fields = {"wall_time_budget_seconds", "minimum_next_point_seconds"}
    if not wall_fields <= set(rules):
        raise ValueError("resource_policy must declare a wall-time budget")
    resource_fields = set(rules) - wall_fields
    if not resource_fields:
        raise ValueError("resource_policy must declare at least one non-wall resource limit")
    minimum_matrix_seconds = len(normalized_points) * rules["minimum_next_point_seconds"]
    if minimum_matrix_seconds > rules["wall_time_budget_seconds"]:
        raise ValueError("points exceed the caller-declared wall-time budget")

    cores = _positive_integer(raw.get("cores"), "cores", maximum=MAX_VALIDATION_MATRIX_CORES)
    max_retries = raw.get("max_retries", 0)
    if (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or not 0 <= max_retries <= 3
    ):
        raise ValueError("max_retries must be an integer between 0 and 3")
    continue_on_error = raw.get("continue_on_error", False)
    if not isinstance(continue_on_error, bool):
        raise ValueError("continue_on_error must be boolean")
    version = raw.get("version")
    if version is not None and (
        not isinstance(version, str) or not version.strip() or len(version) > 32
    ):
        raise ValueError("version must be a bounded nonempty string when provided")

    spec = {
        "job_type": "validation_matrix",
        "schema_version": JOB_SCHEMA_VERSION,
        "source_model_path": str(source),
        "source_model_sha256": source_sha256,
        "points": normalized_points,
        "point_limit": point_limit,
        "resource_policy": policy,
        "cores": cores,
        "version": version.strip() if isinstance(version, str) else None,
        "max_retries": max_retries,
        "continue_on_error": continue_on_error,
    }
    native_requests = [
        collector["inputs"]
        for point in normalized_points
        for collector in point["collectors"]
        if collector["name"] in NATIVE_CONDITION_COLLECTORS
    ]
    if "startup_admission" in raw:
        spec["startup_admission"] = normalize_robust_startup_policy(raw["startup_admission"])
    if "comsol_temporary_directory" in raw:
        if (
            not isinstance(raw["comsol_temporary_directory"], str)
            or not raw["comsol_temporary_directory"].strip()
        ):
            raise ValueError("COMSOL temporary directory must be a nonempty path string")
        temporary = Path(raw["comsol_temporary_directory"])
        if (
            not temporary.is_absolute()
            or temporary.is_symlink()
            or not temporary.is_dir()
            or not str(temporary).isascii()
            or any(character.isspace() for character in str(temporary))
        ):
            raise ValueError(
                "COMSOL temporary directory must be an existing absolute ASCII directory "
                "without whitespace"
            )
        spec["comsol_temporary_directory"] = str(temporary.resolve())
    if native_requests:
        if "startup_admission" not in spec:
            raise ValueError("native condition matrix requires explicit startup admission")
        if any(
            request["support"]["comsol_version"] != spec["version"] for request in native_requests
        ):
            raise ValueError("native condition COMSOL version differs from the matrix version")
        for request in native_requests:
            controls = request["adapter_configuration"]["configuration"]["condition_controls"]
            if (
                controls.get("out_of_core_value", "off").casefold() == "on"
                and "comsol_temporary_directory" not in spec
            ):
                raise ValueError(
                    "out-of-core native collection requires an explicit COMSOL temporary directory"
                )
            if (
                spec["resource_policy"]["rules"].get("max_mesh_elements")
                != request["shape_policy"]["mesh_admission"]["max_elements_per_model"]
            ):
                raise ValueError("native mesh cap differs from the matrix resource policy")
        spec["synthetic_mode"] = False
    spec["spec_fingerprint"] = _fingerprint(spec)
    encoded = json.dumps(
        spec,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_SPEC_BYTES:
        raise ValueError(f"validation_matrix specification exceeds {MAX_SPEC_BYTES} bytes")
    return spec


__all__ = [
    "MAX_VALIDATION_MATRIX_CORES",
    "MAX_VALIDATION_MATRIX_POINTS",
    "SUPPORTED_VALIDATION_COLLECTORS",
    "normalize_validation_matrix_spec",
]
