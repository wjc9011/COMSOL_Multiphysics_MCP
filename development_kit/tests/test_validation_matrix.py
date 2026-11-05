from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import src.jobs.validation_matrix as validation_matrix_module
from src.jobs.validation_matrix import (
    MAX_VALIDATION_MATRIX_POINTS,
    normalize_validation_matrix_spec,
)


def _point(point_id: str = "off-resonance", wavelength: float = 5.1) -> dict:
    return {
        "point_id": point_id,
        "configuration_sha256": "a" * 64,
        "wavelength": {"value": wavelength, "unit": "um", "parameter": "wl"},
        "incidence": {
            "theta_degrees": 0.0,
            "phi_degrees": 0.0,
            "polarization": "S",
        },
        "collectors": [
            {
                "name": "wave_optics_point_audit",
                "inputs": {"component_tag": "comp1", "physics_tag": "ewfd"},
            }
        ],
        "expected_artifact_ids": [f"audit-{point_id}"],
    }


def _spec(source, points=None, **overrides) -> dict:
    value = {
        "job_type": "validation_matrix",
        "source_model_path": str(source),
        "points": [_point()] if points is None else points,
        "point_limit": 2,
        "cores": 1,
        "resource_policy": {
            "wall_time_budget_seconds": 120,
            "minimum_next_point_seconds": 30,
            "max_mesh_elements": 100_000,
        },
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize("unit", ["mm", "cm", "angstrom", "UM"])
def test_normalization_refuses_units_unavailable_to_collectors(tmp_path, unit):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"controlled fixture")
    point = _point()
    point["wavelength"]["unit"] = unit
    with pytest.raises(ValueError, match="wavelength.unit"):
        normalize_validation_matrix_spec(_spec(source, points=[point]))


@pytest.mark.parametrize("unit", ["m", "um", "nm", " um "])
def test_normalization_retains_supported_wavelength_units(tmp_path, unit):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"controlled fixture")
    point = _point()
    point["wavelength"]["unit"] = unit
    result = normalize_validation_matrix_spec(_spec(source, points=[point]))
    assert result["points"][0]["wavelength"]["unit"] == unit.strip()


def test_normalization_is_solver_free_and_binds_exact_point_identity(tmp_path):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"controlled fixture")
    raw_spec = _spec(source)
    child = (
        "import builtins,json,os;"
        "real_import=builtins.__import__;"
        "builtins.__import__=lambda name,*args,**kwargs: "
        "(_ for _ in ()).throw(AssertionError('normalization imported mph')) "
        "if name == 'mph' or name.startswith('mph.') else real_import(name,*args,**kwargs);"
        "from comsol_mcp.jobs.validation_matrix import normalize_validation_matrix_spec;"
        "print(json.dumps(normalize_validation_matrix_spec(json.loads(os.environ['RAW_SPEC']))))"
    )
    env = {**os.environ, "RAW_SPEC": json.dumps(raw_spec)}
    completed = subprocess.run(
        [sys.executable, "-c", child],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    first = json.loads(completed.stdout)
    second = normalize_validation_matrix_spec(_spec(source))

    assert first == second
    assert first["source_model_sha256"] != first["points"][0]["point_fingerprint"]
    assert len(first["source_model_sha256"]) == 64
    assert len(first["points"][0]["point_fingerprint"]) == 64
    assert len(first["spec_fingerprint"]) == 64
    assert first["points"][0]["incidence"]["polarization_evidence"] == "label_only"
    assert first["resource_policy"]["host_defaults_applied"] is False


def test_matrix_fixture_preserves_explicit_empty_points(tmp_path):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"controlled fixture")

    with pytest.raises(ValueError, match="points must be a nonempty list"):
        normalize_validation_matrix_spec(_spec(source, points=[]))


def test_source_or_point_changes_change_immutable_fingerprints(tmp_path):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"v1")
    first = normalize_validation_matrix_spec(_spec(source))
    changed_point = normalize_validation_matrix_spec(_spec(source, points=[_point(wavelength=5.2)]))
    source.write_bytes(b"v2")
    changed_source = normalize_validation_matrix_spec(_spec(source))

    assert (
        first["points"][0]["point_fingerprint"] != changed_point["points"][0]["point_fingerprint"]
    )
    assert first["source_model_sha256"] != changed_source["source_model_sha256"]
    assert first["spec_fingerprint"] != changed_source["spec_fingerprint"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda point: point.__setitem__("configuration_sha256", "b" * 64),
        lambda point: point["incidence"].__setitem__("theta_degrees", 5.0),
        lambda point: point["collectors"][0]["inputs"].__setitem__("physics_tag", "ewfd2"),
    ],
)
def test_point_identity_binds_configuration_incidence_and_collector(tmp_path, mutation):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    baseline = normalize_validation_matrix_spec(_spec(source))
    point = _point()
    mutation(point)
    changed = normalize_validation_matrix_spec(_spec(source, points=[point]))

    assert changed["points"][0]["point_fingerprint"] != baseline["points"][0]["point_fingerprint"]
    assert changed["spec_fingerprint"] != baseline["spec_fingerprint"]


def test_expected_artifact_ids_participate_in_matrix_identity(tmp_path):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    baseline = normalize_validation_matrix_spec(_spec(source))
    point = _point()
    point["expected_artifact_ids"] = ["audit-renamed"]
    changed = normalize_validation_matrix_spec(_spec(source, points=[point]))

    assert changed["points"][0]["point_fingerprint"] == baseline["points"][0]["point_fingerprint"]
    assert changed["spec_fingerprint"] != baseline["spec_fingerprint"]


def test_collector_name_and_json_recursion_fail_with_validation_errors(tmp_path, monkeypatch):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    unhashable = _point()
    unhashable["collectors"][0]["name"] = []
    with pytest.raises(ValueError, match="supported validation collector"):
        normalize_validation_matrix_spec(_spec(source, points=[unhashable]))

    def raise_recursion(*_args, **_kwargs):
        raise RecursionError("injected encoder recursion")

    monkeypatch.setattr(validation_matrix_module.json, "dumps", raise_recursion)
    with pytest.raises(ValueError, match="finite JSON values"):
        normalize_validation_matrix_spec(_spec(source))


def test_spec_byte_limit_covers_the_returned_fingerprint(tmp_path, monkeypatch):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    raw = _spec(source)
    normalized = normalize_validation_matrix_spec(raw)
    final_size = len(
        json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    monkeypatch.setattr(validation_matrix_module, "MAX_SPEC_BYTES", final_size - 1)

    with pytest.raises(ValueError, match="specification exceeds"):
        normalize_validation_matrix_spec(raw)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda spec: spec.update(point_limit=0), "point_limit must be a positive integer"),
        (
            lambda spec: spec.update(point_limit=MAX_VALIDATION_MATRIX_POINTS + 1),
            "point_limit must not exceed",
        ),
        (
            lambda spec: spec.update(points=[_point("one"), _point("two")], point_limit=1),
            "points exceed the caller-declared point_limit",
        ),
        (
            lambda spec: spec.update(
                resource_policy={
                    "wall_time_budget_seconds": 20,
                    "minimum_next_point_seconds": 30,
                    "max_mesh_elements": 100_000,
                }
            ),
            "minimum_next_point_seconds must not exceed wall_time_budget_seconds",
        ),
        (
            lambda spec: spec.update(
                points=[_point("one"), _point("two", 5.2)],
                resource_policy={
                    "wall_time_budget_seconds": 50,
                    "minimum_next_point_seconds": 30,
                    "max_mesh_elements": 100_000,
                },
            ),
            "points exceed the caller-declared wall-time budget",
        ),
        (
            lambda spec: spec.update(
                resource_policy={
                    "wall_time_budget_seconds": 120,
                    "minimum_next_point_seconds": 30,
                }
            ),
            "at least one non-wall resource limit",
        ),
    ],
)
def test_declared_point_time_and_resource_bounds_fail_closed_before_runtime(
    tmp_path, mutation, message
):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    spec = _spec(source)
    mutation(spec)

    with pytest.raises(ValueError, match=message):
        normalize_validation_matrix_spec(spec)


def test_matrix_accepts_the_exact_maximum_point_count(tmp_path):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    points = [
        _point(f"point-{index}", wavelength=5.0 + index / 100.0)
        for index in range(MAX_VALIDATION_MATRIX_POINTS)
    ]
    spec = _spec(
        source,
        points=points,
        point_limit=MAX_VALIDATION_MATRIX_POINTS,
        resource_policy={
            "wall_time_budget_seconds": 2_000,
            "minimum_next_point_seconds": 30,
            "max_mesh_elements": 100_000,
        },
    )

    normalized = normalize_validation_matrix_spec(spec)

    assert len(normalized["points"]) == MAX_VALIDATION_MATRIX_POINTS
    assert normalized["point_limit"] == MAX_VALIDATION_MATRIX_POINTS


def test_matrix_accepts_wall_budget_equal_to_declared_minimum(tmp_path):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    spec = _spec(
        source,
        points=[_point("one"), _point("two", 5.2)],
        point_limit=2,
        resource_policy={
            "wall_time_budget_seconds": 60,
            "minimum_next_point_seconds": 30,
            "max_mesh_elements": 100_000,
        },
    )

    normalized = normalize_validation_matrix_spec(spec)

    assert normalized["resource_policy"]["rules"]["wall_time_budget_seconds"] == 60


@pytest.mark.parametrize("field", ["wavelength", "theta_degrees", "phi_degrees"])
def test_matrix_numeric_fields_reject_integer_overflow_as_validation_error(tmp_path, field):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    point = _point()
    if field == "wavelength":
        point["wavelength"]["value"] = 10**400
    else:
        point["incidence"][field] = 10**400

    with pytest.raises(ValueError, match="must be finite"):
        normalize_validation_matrix_spec(_spec(source, points=[point]))


def test_duplicate_exact_points_and_artifact_ids_are_rejected(tmp_path):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    duplicate_identity = _point("second")
    duplicate_artifact = _point("second", 5.2)
    duplicate_artifact["expected_artifact_ids"] = ["audit-off-resonance"]

    with pytest.raises(ValueError, match="unique exact configuration identities"):
        normalize_validation_matrix_spec(_spec(source, points=[_point(), duplicate_identity]))
    with pytest.raises(ValueError, match="unique across the matrix"):
        normalize_validation_matrix_spec(_spec(source, points=[_point(), duplicate_artifact]))


def test_collector_inputs_are_finite_bounded_json(tmp_path):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    nonfinite = _point()
    nonfinite["collectors"][0]["inputs"]["bad"] = float("nan")
    oversized = _point()
    oversized["collectors"][0]["inputs"]["payload"] = "x" * (64 * 1024)

    with pytest.raises(ValueError, match="finite JSON"):
        normalize_validation_matrix_spec(_spec(source, points=[nonfinite]))
    with pytest.raises(ValueError, match="collector-input limit"):
        normalize_validation_matrix_spec(_spec(source, points=[oversized]))


def test_only_declared_collectors_and_portable_artifact_ids_are_accepted(tmp_path):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    unsupported = _point()
    unsupported["collectors"][0]["name"] = "arbitrary_python"
    unsafe_artifact = _point()
    unsafe_artifact["expected_artifact_ids"] = ["C:\\private\\result.npz"]

    with pytest.raises(ValueError, match="not a supported validation collector"):
        normalize_validation_matrix_spec(_spec(source, points=[unsupported]))
    with pytest.raises(ValueError, match="bounded portable identifier"):
        normalize_validation_matrix_spec(_spec(source, points=[unsafe_artifact]))


def test_each_collector_requires_one_unique_expected_artifact_id(tmp_path):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    point = _point()
    point["expected_artifact_ids"] = ["first", "extra"]

    with pytest.raises(ValueError, match="exactly one expected artifact ID per collector"):
        normalize_validation_matrix_spec(_spec(source, points=[point]))


def test_cores_share_the_explicit_product_bound(tmp_path):
    source = tmp_path / "cores-fixture.mph"
    source.write_bytes(b"model")

    with pytest.raises(ValueError, match="cores must not exceed 1024"):
        normalize_validation_matrix_spec(_spec(source, cores=2000))


def test_nested_collector_inputs_require_string_keys(tmp_path):
    source = tmp_path / "keys-fixture.mph"
    source.write_bytes(b"model")
    point = _point()
    point["collectors"][0]["inputs"] = {
        "component_tag": "comp1",
        "nested": {1: "json-roundtrip-would-coerce-this"},
    }

    with pytest.raises(ValueError, match="keys must be strings"):
        normalize_validation_matrix_spec(_spec(source, points=[point]))


def test_point_limit_schema_and_runtime_share_one_supported_maximum():
    import json
    from pathlib import Path

    from pydantic import TypeAdapter

    from comsol_mcp.contracts.job_submission import (
        MAX_JOB_COLLECTION,
        MAX_VALIDATION_MATRIX_POINTS,
        ValidationMatrixInput,
    )

    schema = TypeAdapter(ValidationMatrixInput).json_schema()
    declared = schema["properties"]["point_limit"]["maximum"]
    assert declared == MAX_VALIDATION_MATRIX_POINTS == 32
    assert declared < MAX_JOB_COLLECTION

    snapshot = json.loads(
        (Path(__file__).parent / "snapshots" / "full_tool_schemas.json").read_text(encoding="utf-8")
    )
    text = json.dumps(snapshot)
    assert '"maximum": 32' in text or "'maximum': 32" in text
