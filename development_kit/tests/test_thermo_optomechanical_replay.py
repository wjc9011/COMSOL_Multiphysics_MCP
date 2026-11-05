"""Solver-free thermo-optomechanical contract, durability, and cleanup tests."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import src.jobs.thermo_optomechanical_replay as replay_module
import src.jobs.thermo_optomechanical_replay_execution as replay_execution_module
import src.jobs.thermo_optomechanical_replay_rows as thermo_rows_module
from pydantic import ValidationError
from src.jobs.manager import JobManager, _worker_module
from src.jobs.store import JobStore, process_identity
from src.jobs.thermo_optomechanical_replay import (
    THERMO_OPTOMECHANICAL_CONTROLS,
    THERMO_OPTOMECHANICAL_STAGES,
    normalize_thermo_optomechanical_replay_spec,
)
from src.jobs.thermo_optomechanical_replay_execution import (
    ThermoOptomechanicalComsolExecutor,
)
from src.jobs.thermo_optomechanical_replay_rows import (
    _validate_stage_payload,
    append_thermo_optomechanical_stage_row,
    build_stage_evidence,
    read_thermo_optomechanical_stage_rows,
)
from src.jobs.thermo_optomechanical_replay_runner import (
    run_thermo_optomechanical_replay,
)
from src.jobs.thermo_optomechanical_replay_worker import _run as run_worker
from src.tools.jobs import _preview_job_spec

from comsol_mcp.contracts.thermo_optomechanical import (
    ThermoOpticalMaterialValidity,
    ThermoOptomechanicalReplayManifest,
)
from development_kit.scripts import thermo_optomechanical_licensed_gate as licensed_gate


def _material_state(source: Path) -> dict:
    return {
        "schema_name": "comsol_mcp.thermal_material_state_reference",
        "schema_version": "1.0.0",
        "ledger_sha256": "a" * 64,
        "material_identity_sha256": "a" * 64,
        "sample_identity_sha256": "b" * 64,
        "state_id": "state_a",
        "classification": "fitted",
        "validity": {
            "wavelength_min_m": 1.0e-6,
            "wavelength_max_m": 3.0e-6,
            "temperature_min_K": 250.0,
            "temperature_max_K": 500.0,
        },
        "target": {
            "component_tag": "comp1",
            "material_tag": "mat1",
            "property_group_tag": "def",
            "property_key": "relpermittivity",
        },
        "source_model_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "expected_property_values": ["2.25"],
        "expected_function_tags": [],
        "application_receipt_sha256": "d" * 64,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"wavelength_min_m": 3.0e-6, "wavelength_max_m": 1.0e-6},
        {"temperature_min_K": 500.0, "temperature_max_K": 250.0},
    ],
)
def test_material_validity_rejects_inverted_ranges(overrides):
    value = {
        "wavelength_min_m": 1.0e-6,
        "wavelength_max_m": 3.0e-6,
        "temperature_min_K": 250.0,
        "temperature_max_K": 500.0,
        **overrides,
    }

    with pytest.raises(ValidationError):
        ThermoOpticalMaterialValidity.model_validate(value)


def test_licensed_fixture_manifest_is_valid_before_comsol_start(ascii_tmp_path):
    source = ascii_tmp_path / "source.mph"
    source.write_bytes(b"synthetic model stand-in")
    manifest = ascii_tmp_path / "submission.json"
    raw = licensed_gate._raw_spec(source, manifest, cores=1)
    spec = normalize_thermo_optomechanical_replay_spec(raw)
    ledger = licensed_gate._frozen_material_ledger()
    state = ledger["states"][0]
    assert state["classification"] == "assumed"
    assert state["uncertainty"]["relative_fraction"] == 0.01
    assert "not measured" in state["source"]["source_state_description"]
    assert spec["cores"] == 1
    assert spec["source_model_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert spec["model_contract"]["expressions"]["transfer_displacement_max"] == "dmeshH1"
    assert spec["model_contract"]["expressions"]["displacement_max"] == "umaxH1"


def test_omitted_unit_defaults_are_materialized_into_the_spec(ascii_tmp_path):
    submission = _raw_spec(ascii_tmp_path / "unit-defaults")
    manifest_path = Path(submission["specification_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for unit_key in ("temperature_unit", "heat_source_unit", "convection_coefficient_unit"):
        del manifest["thermal_load"][unit_key]
    payload = json.dumps(manifest).encode("utf-8")
    manifest_path.write_bytes(payload)
    submission["specification_sha256"] = hashlib.sha256(payload).hexdigest()

    spec = normalize_thermo_optomechanical_replay_spec(submission)

    # The executor indexes these unit literals; a caller legitimately omitting
    # them must receive the contract defaults instead of a KeyError mid-replay.
    assert spec["thermal_load"]["temperature_unit"] == "K"
    assert spec["thermal_load"]["heat_source_unit"] == "W/m^3"
    assert spec["thermal_load"]["convection_coefficient_unit"] == "W/(m^2*K)"


def test_licensed_gate_cleanup_failure_preserves_verdict_and_writes_fallback(tmp_path, monkeypatch):
    class BrokenOwner:
        def status(self, *, require_fresh_inventory):
            assert require_fresh_inventory is True
            raise RuntimeError("injected inventory failure")

    original_atomic_json = licensed_gate._atomic_json

    def fail_primary_receipt(path, value):
        if path.name == "licensed_acceptance.json":
            raise OSError("injected primary receipt failure")
        original_atomic_json(path, value)

    monkeypatch.setattr(licensed_gate, "_atomic_json", fail_primary_receipt)
    result = {
        "schema_name": licensed_gate.SCHEMA_NAME,
        "schema_version": licensed_gate.SCHEMA_VERSION,
        "success": False,
        "error": {"type": "RuntimeError", "message": "original gate failure"},
    }

    finalized = licensed_gate._finalize_gate_result(tmp_path, result, BrokenOwner())

    assert finalized["error"]["message"] == "original gate failure"
    assert finalized["cleanup"]["inventory_complete"] is False
    assert finalized["cleanup"]["error"]["message"] == "injected inventory failure"
    assert finalized["receipt_write_error"]["type"] == "OSError"
    fallback = json.loads((tmp_path / "licensed_acceptance_failure.json").read_text("utf-8"))
    assert fallback["success"] is False
    assert fallback["error"]["message"] == "original gate failure"


def _raw_spec(root: Path) -> dict:
    source = root / "source.mph"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"synthetic-thermo-optomechanical-source")
    manifest = {
        "job_type": "thermo_optomechanical_replay",
        "source_model_path": str(source),
        "source_model_relative_identity": "fixtures/thermo_optomechanical/source.mph",
        "optical_configuration_sha256": "c" * 64,
        "material_state": _material_state(source),
        "model_contract": {
            "component_tag": "comp1",
            "heat_transfer_tag": "ht",
            "solid_mechanics_tag": "solid",
            "moving_mesh_tag": "ale",
            "wave_optics_tag": "ewfd",
            "thermal_structure_study_tag": "std_ts",
            "transfer_study_tag": "std_ale",
            "optical_study_tag": "std_opt",
            "initial_temperature_parameter": "Tinit",
            "ambient_temperature_parameter": "Tamb",
            "applied_temperature_parameter": "Tapp",
            "cte_parameter": "alpha",
            "reference_temperature_parameter": "Tref",
            "wavelength_parameter": "lambda0",
            "polarization_parameter": "pol",
            "deformation_scale_parameter": "dscale",
            "heated_domain_selection": "sel_heat",
            "structural_domain_selection": "sel_solid",
            "fixed_boundary_selection": "sel_fixed",
            "thermal_boundary_selection": "sel_temp",
            "optical_domain_selection": "sel_opt",
            "mesh_tag": "mesh1",
            "expressions": {
                "temperature_min": "Tmin",
                "temperature_max": "Tmax",
                "displacement_max": "umax",
                "stress_max": "smax",
                "heat_source_integral": "Qin",
                "boundary_loss_integral": "Qout",
                "delta_length": "dL",
                "minimum_mesh_quality": "qmin",
                "mesh_element_count": "nelem",
                "mesh_vertex_count": "nvert",
                "reflectance": "R",
                "transmittance": "Tr",
                "absorptance": "A",
            },
        },
        "thermal_load": {
            "temperature_unit": "K",
            "heat_source_unit": "W/m^3",
            "convection_coefficient_unit": "W/(m^2*K)",
            "initial_temperature_K": 300.0,
            "ambient_temperature_K": 300.0,
            "applied_temperature_K": 400.0,
            "volumetric_heat_source_W_per_m3": 0.0,
            "convection_coefficient_W_per_m2_K": 10.0,
        },
        "thermal_expansion": {
            "coefficient_input_type": "secant_coefficient",
            "coefficient_per_K": 1.0e-5,
            "reference_temperature_K": 300.0,
            "reference_length_m": 0.01,
            "measurement_axis": "x",
        },
        "deformation_transfer": {
            "method": "moving_mesh_spatial_frame",
            "displacement_frame": "spatial",
            "topology_change_allowed": False,
            "deformation_scale": 1.0,
        },
        "optical_replay": {
            "wavelengths_m": [1.5e-6, 2.0e-6],
            "branches": ["TE"],
            "wavelength_coordinate": "vacuum_wavelength_m",
        },
        "validation_controls": list(THERMO_OPTOMECHANICAL_CONTROLS),
        "acceptance_policy": {
            "expansion_relative_tolerance": 0.02,
            "zero_control_absolute_tolerance_m": 1.0e-10,
            "energy_relative_tolerance": 1.0e-6,
            "rta_closure_absolute_tolerance": 1.0e-8,
            "minimum_mesh_quality": 0.1,
            "maximum_displacement_to_length": 0.01,
        },
        "resource_policy": {
            "wall_time_budget_seconds": 300.0,
            "minimum_next_point_seconds": 1.0,
        },
        "cores": 1,
        "wall_time_budget_seconds": 300,
        "version": "6.4",
        "max_retries": 1,
        "continue_on_error": False,
    }
    specification = root / "thermo_optomechanical_specification.json"
    specification.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "job_type": "thermo_optomechanical_replay",
        "specification_path": str(specification),
        "specification_sha256": hashlib.sha256(specification.read_bytes()).hexdigest(),
    }


_SYNTHETIC_CONTROL_LABELS = {
    "wrong_selection": ("structural", "structural_assertion"),
    "missing_material_state": ("structural", "structural_assertion"),
    "convection": ("declared", "declared_value"),
    "temperature_unit": ("declared", "declared_value"),
}


def _synthetic_control_provenance(control_id):
    provenance, disposition = _SYNTHETIC_CONTROL_LABELS.get(
        control_id, ("measured", "executed_control")
    )
    return {"provenance": provenance, "disposition": disposition}


def _payload(stage_id: str, spec: dict) -> dict:
    contract = spec["model_contract"]
    if stage_id == "preflight":
        return {
            "required_products": ["COMSOL", "Heat", "Structural", "Wave Optics"],
            "available_products": ["COMSOL", "Heat", "Structural", "Wave Optics"],
            "interface_tags": {
                "heat_transfer": contract["heat_transfer_tag"],
                "solid_mechanics": contract["solid_mechanics_tag"],
                "moving_mesh": contract["moving_mesh_tag"],
                "wave_optics": contract["wave_optics_tag"],
            },
            "selection_readback": {
                "heated_domain": {"tag": "sel_heat", "entity_count": 1},
                "structural_domain": {"tag": "sel_solid", "entity_count": 1},
                "fixed_boundary": {"tag": "sel_fixed", "entity_count": 1},
                "thermal_boundary": {"tag": "sel_temp", "entity_count": 1},
                "optical_domain": {"tag": "sel_opt", "entity_count": 1},
            },
            "temperature_unit_readback": "K",
            "material_state_id": "state_a",
            "material_state_readback": {
                "ledger_sha256": spec["material_state"]["ledger_sha256"],
                "state_id": "state_a",
                "classification": "fitted",
                "target": spec["material_state"]["target"],
                "property_value_type": "String",
                "property_values": ["2.25"],
                "function_tags": [],
                "application_receipt_sha256": spec["material_state"]["application_receipt_sha256"],
            },
            "source_unchanged": True,
            "rollback_available": False,
        }
    if stage_id == "thermal_structural_solve":
        return {
            "temperature": {"minimum_K": 400.0, "maximum_K": 400.0},
            "displacement": {"maximum_m": 1.0e-5, "delta_length_m": 1.0e-5, "frame": "spatial"},
            "stress": {"maximum_abs_Pa": 0.0},
            "energy_balance": {
                "source_W": 0.0,
                "loss_W": 0.0,
                "residual_W": 0.0,
                "relative_residual": 0.0,
            },
            "expansion": {
                "coefficient_input_type": "secant_coefficient",
                "coefficient_per_K": 1.0e-5,
                "reference_temperature_K": 300.0,
                "expected_delta_length_m": 1.0e-5,
                "observed_delta_length_m": 1.0e-5,
                "relative_error": 0.0,
            },
        }
    if stage_id == "state_evidence":
        return {
            "mesh": {
                "identity_sha256": "d" * 64,
                "element_count": 100,
                "vertex_count": 80,
                "minimum_quality": 0.8,
                "inverted_element_count": 0,
                "inverted_element_count_disposition": "inferred_from_minimum_quality",
            },
            "frame": {
                "identity_sha256": "e" * 64,
                "displacement_frame": "spatial",
                "topology_change_allowed": False,
            },
            "deformation_scale": 1.0,
            "displacement_to_length": 0.001,
        }
    if stage_id == "deformation_transfer":
        return {
            "method": "moving_mesh_spatial_frame",
            "material_frame_semantics": "spatial_deformation_preserves_material",
            "source_geometry_sha256": "f" * 64,
            "deformed_geometry_sha256": "1" * 64,
            "readback_exact": True,
            "checkpoint_integrity_verified": True,
            "rollback_exercised": False,
            "rollback_evidence": "checkpoint_integrity_only_no_rollback_in_transfer_stage",
        }
    rows = []
    for wavelength in spec["optical_replay"]["wavelengths_m"]:
        for branch in spec["optical_replay"]["branches"]:
            rta = {"R": 0.2, "T": 0.7, "A": 0.1, "closure_residual": 0.0, "passive": True}
            rows.append(
                {
                    "requested_wavelength_m": wavelength,
                    "solved_wavelength_m": wavelength,
                    "branch": branch,
                    "baseline_rta": dict(rta),
                    "deformed_rta": dict(rta),
                }
            )
    return {
        "rows": rows,
        "control_results": [
            {
                "control_id": item,
                "passed": True,
                "reason_code": "synthetic_control_passed",
                **_synthetic_control_provenance(item),
            }
            for item in spec["validation_controls"]
        ],
        "source_unchanged": True,
        "derived_model_sha256": "2" * 64,
    }


def test_thermo_optomechanical_spec_is_closed_bounded_hash_bound_and_previewable(
    ascii_tmp_path,
):
    raw = _raw_spec(ascii_tmp_path / "spec")
    spec = normalize_thermo_optomechanical_replay_spec(raw)
    assert spec["declared_stages"] == list(THERMO_OPTOMECHANICAL_STAGES)
    assert spec["declared_optical_point_count"] == 2
    assert len(spec["source_model_sha256"]) == 64
    assert len(spec["material_ledger_sha256"]) == 64
    assert _worker_module(spec["job_type"]).endswith("thermo_optomechanical_replay_worker")
    preview = _preview_job_spec(raw)
    assert preview["inventory"]["declared_optical_points"] == 2
    assert preview["inventory"]["control_count"] == len(THERMO_OPTOMECHANICAL_CONTROLS)
    assert preview["submission_manifest"]["hash_verified"] is True

    manager = JobManager(
        ascii_tmp_path / "manager" / "jobs",
        preflight=lambda **_kwargs: {"ready": True},
        reconcile_on_start=False,
    )
    manager._launch_worker = lambda _job_id, module: {
        "pid": 123,
        "process_create_time": 456.0,
        "command_signature": module,
    }
    submitted = manager.submit(raw)
    stored = manager.store.read_spec(submitted["job_id"])
    assert stored["job_type"] == "thermo_optomechanical_replay"
    assert manager.store.read_state(submitted["job_id"])["progress"]["total"] == 5


def test_submission_manifest_hash_must_match_before_normalization(ascii_tmp_path):
    raw = _raw_spec(ascii_tmp_path / "manifest-hash")
    raw["specification_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="specification_sha256"):
        normalize_thermo_optomechanical_replay_spec(raw)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_manifest_rejects_nonfinite_physical_values_at_the_typed_boundary(ascii_tmp_path, value):
    raw = _raw_spec(ascii_tmp_path / "manifest-nonfinite")
    specification = Path(raw["specification_path"])
    manifest = json.loads(specification.read_text(encoding="utf-8"))
    manifest["thermal_load"]["initial_temperature_K"] = value
    specification.write_text(json.dumps(manifest), encoding="utf-8")
    raw["specification_sha256"] = hashlib.sha256(specification.read_bytes()).hexdigest()

    with pytest.raises(ValidationError, match="finite number"):
        normalize_thermo_optomechanical_replay_spec(raw)


def test_manifest_contract_requires_each_validation_control_exactly_once(ascii_tmp_path):
    raw = _raw_spec(ascii_tmp_path / "typed-controls")
    manifest = json.loads(Path(raw["specification_path"]).read_text(encoding="utf-8"))
    manifest["validation_controls"][0] = manifest["validation_controls"][1]

    with pytest.raises(ValidationError, match="exactly once"):
        ThermoOptomechanicalReplayManifest.model_validate(manifest)


def test_submission_manifest_hash_and_parse_share_one_snapshot(ascii_tmp_path, monkeypatch):
    raw = _raw_spec(ascii_tmp_path / "manifest-snapshot")
    manifest_path = Path(raw["specification_path"])
    original_read = replay_module.read_file_bytes_bounded
    reads = 0

    def read_once(path, *, max_bytes):
        nonlocal reads
        payload = original_read(path, max_bytes=max_bytes)
        if Path(path).samefile(manifest_path):
            reads += 1
            manifest_path.write_text("{}\n", encoding="utf-8")
        return payload

    monkeypatch.setattr(replay_module, "read_file_bytes_bounded", read_once)
    spec = normalize_thermo_optomechanical_replay_spec(raw)

    assert reads == 1
    assert spec["submission_manifest_sha256"] == raw["specification_sha256"]
    assert spec["declared_optical_point_count"] == 2


def test_source_hash_race_surfaces_as_the_validation_error_boundary(ascii_tmp_path, monkeypatch):
    raw = _raw_spec(ascii_tmp_path / "source-race")

    def racing_hash(_path):
        raise FileNotFoundError("removed between is_file and hash")

    monkeypatch.setattr(replay_module, "_sha256_file", racing_hash)
    with pytest.raises(ValueError, match="existing MPH file"):
        normalize_thermo_optomechanical_replay_spec(raw)


@pytest.mark.parametrize(
    "mutation,match",
    [
        (
            lambda value: value["thermal_load"].__setitem__("temperature_unit", "degC"),
            "literal_error",
        ),
        (lambda value: value.pop("material_state"), "Field required"),
        (lambda value: value["optical_replay"].__setitem__("wavelengths_m", [4.0e-6]), "outside"),
        (
            lambda value: value.__setitem__("source_model_relative_identity", "../source.mph"),
            "contained",
        ),
        (lambda value: value["validation_controls"].__setitem__(0, "zero_cte"), "exactly once"),
    ],
)
def test_thermo_optomechanical_units_state_domain_path_and_controls_fail_closed(
    ascii_tmp_path, mutation, match
):
    raw = _raw_spec(ascii_tmp_path / "negative")
    specification = Path(raw["specification_path"])
    manifest = json.loads(specification.read_text(encoding="utf-8"))
    mutation(manifest)
    specification.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    raw["specification_sha256"] = hashlib.sha256(specification.read_bytes()).hexdigest()
    with pytest.raises((ValueError, ValidationError), match=match):
        normalize_thermo_optomechanical_replay_spec(raw)


def test_stage_evidence_resume_never_executes_a_completed_or_orphaned_stage_twice(
    ascii_tmp_path,
):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "resume"))
    root = ascii_tmp_path / "resume-artifacts"
    calls = []

    def executor(stage_id, _stage_dir, current_spec):
        calls.append(stage_id)
        return _payload(stage_id, current_spec)

    def fail_after_thermal_evidence(point, evidence):
        if point == "after_stage_evidence" and evidence["stage_id"] == "thermal_structural_solve":
            raise RuntimeError("injected crash after durable evidence")

    with pytest.raises(RuntimeError, match="injected crash"):
        run_thermo_optomechanical_replay(
            spec,
            root,
            attempt=1,
            stage_executor=executor,
            fault_hook=fail_after_thermal_evidence,
        )
    result = run_thermo_optomechanical_replay(spec, root, attempt=2, stage_executor=executor)
    assert result["completed"] is True
    assert result["skipped_complete"] == 1
    assert result["recovered_from_evidence"] == 1
    assert calls.count("preflight") == 1
    assert calls.count("thermal_structural_solve") == 1
    assert calls == list(THERMO_OPTOMECHANICAL_STAGES)
    rows = read_thermo_optomechanical_stage_rows(
        root / "thermo_optomechanical_stages.jsonl", spec, artifact_root=root
    )
    assert [row["attempt"] for row in rows] == [1, 2, 2, 2, 2]
    assert result["summary"]["scientific_disposition"] == "accepted"


def test_stage_artifact_tampering_is_rejected_before_resume(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "tamper"))
    root = ascii_tmp_path / "tamper-artifacts"
    run_thermo_optomechanical_replay(
        spec,
        root,
        attempt=1,
        stage_executor=lambda stage, _directory, current: _payload(stage, current),
    )
    evidence = root / "state_evidence" / "evidence.json"
    original = evidence.read_bytes()
    changed = original.replace(b"0.8", b"0.7", 1)
    assert changed != original
    evidence.write_bytes(changed)
    with pytest.raises(ValueError, match="artifact|hash|evidence"):
        read_thermo_optomechanical_stage_rows(
            root / "thermo_optomechanical_stages.jsonl", spec, artifact_root=root
        )


def test_invalid_stage_attempt_is_rejected_before_journal_append(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "row"))
    root = ascii_tmp_path / "row-artifacts"
    evidence = build_stage_evidence(spec, "preflight", _payload("preflight", spec))
    evidence_path = root / "preflight" / "evidence.json"
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    journal = root / "thermo_optomechanical_stages.jsonl"

    with pytest.raises(ValueError, match="attempt"):
        append_thermo_optomechanical_stage_row(
            journal,
            spec,
            attempt=0,
            artifact_root=root,
        )

    assert not journal.exists() or journal.read_bytes() == b""


def test_stage_evidence_symlink_escape_is_refused_before_parsing(ascii_tmp_path):
    outside = ascii_tmp_path / "outside"
    outside.mkdir()
    (outside / "evidence.json").write_text("x" * 32, encoding="utf-8")
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "link-root"))
    root = ascii_tmp_path / "link-root" / "stages"
    stage_dir = root / "preflight"
    stage_dir.mkdir(parents=True)
    link = stage_dir / "evidence.json"
    try:
        link.symlink_to(outside / "evidence.json")
    except OSError:
        pytest.skip("this host cannot create file symlinks without privilege")

    with pytest.raises(ValueError, match="escapes the job directory"):
        thermo_rows_module._load_stage_evidence(root, "preflight", spec)


def test_optical_evidence_preserves_tiny_negative_absorption_but_rejects_active_values(
    ascii_tmp_path,
):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "rta"))
    payload = _payload("optical_replay", spec)
    payload["rows"][0]["deformed_rta"]["A"] = -1.0e-18
    payload["rows"][0]["deformed_rta"]["closure_residual"] = (
        sum(payload["rows"][0]["deformed_rta"][key] for key in ("R", "T", "A")) - 1.0
    )
    evidence = build_stage_evidence(spec, "optical_replay", payload)
    assert evidence["payload"]["rows"][0]["deformed_rta"]["A"] == -1.0e-18

    # A finite but unphysical measurement is real evidence: it stays
    # durable with an explicit measured rejection instead of being erased.
    payload["rows"][0]["deformed_rta"]["A"] = -1.0e-8
    rejected = build_stage_evidence(spec, "optical_replay", payload)
    assert rejected["scientific_disposition"] == "measured_gate_failed"
    assert rejected["rejection_reason_codes"] == ["optical_rta_nonpassive_deformed_rta_row_0"]
    assert rejected["payload"]["rows"][0]["deformed_rta"]["A"] == -1.0e-8
    # A verdict that drifts from the measured rows is refused even when the
    # tamperer re-hashes the evidence: the validator re-derives the verdict.
    from src.jobs.thermo_optomechanical_replay_rows import (
        _fingerprint,
        _validate_common_evidence,
    )

    tampered = dict(rejected)
    tampered["scientific_disposition"] = "measured_gate_passed"
    tampered.pop("evidence_sha256")
    tampered["evidence_sha256"] = _fingerprint(tampered)
    with pytest.raises(ValueError, match="disposition must match"):
        _validate_common_evidence(tampered, spec, "optical_replay")


@pytest.mark.parametrize(
    "stage_id,mutation,match",
    [
        (
            "thermal_structural_solve",
            lambda payload: payload["displacement"].__setitem__("delta_length_m", "bad"),
            "displacement delta length",
        ),
        (
            "thermal_structural_solve",
            lambda payload: payload["expansion"].__setitem__("expected_delta_length_m", 2.0e-5),
            "thermal expansion readback",
        ),
        (
            "state_evidence",
            lambda payload: payload["mesh"].__setitem__("inverted_element_count", False),
            "mesh evidence",
        ),
        (
            "optical_replay",
            lambda payload: payload.__setitem__("derived_model_sha256", None),
            "derived_model_sha256",
        ),
        (
            "state_evidence",
            lambda payload: payload["mesh"].__setitem__("identity_sha256", "not-hex"),
            "mesh identity_sha256",
        ),
        (
            "state_evidence",
            lambda payload: payload["frame"].__setitem__("identity_sha256", None),
            "frame identity_sha256",
        ),
        (
            "deformation_transfer",
            lambda payload: payload.__setitem__("source_geometry_sha256", "abc"),
            "source_geometry_sha256 must contain exactly 64 hexadecimal",
        ),
    ],
)
def test_stage_evidence_rejects_unbound_or_malformed_scientific_values(
    ascii_tmp_path, stage_id, mutation, match
):
    spec = normalize_thermo_optomechanical_replay_spec(
        _raw_spec(ascii_tmp_path / f"invalid-{stage_id}")
    )
    payload = _payload(stage_id, spec)
    mutation(payload)

    with pytest.raises(ValueError, match=match):
        build_stage_evidence(spec, stage_id, payload)


class _Dataset:
    def __init__(self, tag="dset1", solution="sol1"):
        self._tag = tag
        self._solution = solution

    def properties(self):
        return ["solution"]

    def property(self, name):
        assert name == "solution"
        return self._solution

    def tag(self):
        return self._tag


class _Study:
    def getSolverSequences(self, kind):
        assert kind == "All"
        return ["sol1"]


class _Solution:
    def isEmpty(self):
        return False


class _JavaModel:
    def __init__(self, current_path=None):
        self.current_path = "" if current_path is None else str(current_path)
        self.saved = []

    def study(self, tag):
        assert tag == "std_ts"
        return _Study()

    def sol(self, tag):
        assert tag == "sol1"
        return _Solution()

    def getFilePath(self):
        return self.current_path

    def save(self, path, save_copy):
        self.saved.append((path, save_copy))
        if save_copy:
            Path(path).write_bytes(b"checkpoint")


class _ExecutorModel:
    def __init__(self, java, datasets=None):
        self.java = java
        self.datasets = [_Dataset()] if datasets is None else datasets
        self.evaluations = []

    def __truediv__(self, group):
        assert group == "datasets"
        return self.datasets

    def evaluate(self, expression, *, dataset):
        self.evaluations.append((expression, dataset))
        return 2.5


def test_executor_binds_the_study_solution_dataset_before_evaluation(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "dataset"))
    executor = ThermoOptomechanicalComsolExecutor(None, spec, ascii_tmp_path / "job")
    model = _ExecutorModel(_JavaModel())
    executor.model = model
    executor._bind_study_dataset("std_ts")
    assert executor._evaluate("temperature_min") == 2.5
    assert model.evaluations == [("Tmin", model.datasets[0])]


@pytest.mark.parametrize("drift", ["none", "parameter", "frequency"])
def test_optical_wavelength_binds_parameter_and_solution(ascii_tmp_path, drift):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "wave"))
    executor = ThermoOptomechanicalComsolExecutor(None, spec, ascii_tmp_path / "job")
    dataset = object()
    executor._active_dataset = dataset
    calls = []

    class Model:
        def evaluate(self, expression, **kwargs):
            assert kwargs == {"dataset": dataset}
            calls.append(expression)
            shifted = expression == ("lambda0" if drift == "parameter" else "c_const/freq")
            return 2e-6 if drift != "none" and shifted else 1.5e-6

    executor.model = Model()
    if drift == "none":
        assert executor._solved_wavelength(1.5e-6) == 1.5e-6
    else:
        with pytest.raises(RuntimeError, match="solved wavelength differs"):
            executor._solved_wavelength(1.5e-6)
    assert calls == ["lambda0", "c_const/freq"]


def test_thermal_structure_readbacks_bind_before_mesh_and_expansion(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "readbacks"))
    executor = ThermoOptomechanicalComsolExecutor(None, spec, ascii_tmp_path / "job")
    calls = []
    executor._bind_study_dataset = lambda tag: calls.append(("bind", tag))
    executor._evaluate = lambda key: calls.append(("evaluate", key)) or 0.5

    assert executor._thermal_structure_readbacks() == (0.5, 0.5)
    assert calls == [
        ("bind", "std_ts"),
        ("evaluate", "minimum_mesh_quality"),
        ("evaluate", "delta_length"),
    ]


def test_zero_temperature_control_resets_every_temperature_driver(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(
        _raw_spec(ascii_tmp_path / "zero-temperature")
    )
    executor = ThermoOptomechanicalComsolExecutor(None, spec, ascii_tmp_path / "job")
    calls = []
    executor._load_derived = lambda: None
    executor._apply_positive_parameters = lambda: None
    executor._parameter = lambda key, value, unit=None: calls.append((key, value, unit))
    executor._set_moving_mesh_active = lambda _enabled: None
    executor._study = lambda _key: None
    executor._bind_study_dataset = lambda _tag: None
    executor._evaluate = lambda _key: 0.0

    assert executor._zero_control(zero_cte=False) == 0.0
    reference = spec["thermal_expansion"]["reference_temperature_K"]
    assert calls == [
        ("initial_temperature_parameter", reference, "K"),
        ("ambient_temperature_parameter", reference, "K"),
        ("applied_temperature_parameter", reference, "K"),
    ]


def test_wavelength_readback_accepts_last_ulp_but_rejects_material_drift():
    requested = 1.5e-6

    assert replay_execution_module._wavelength_readback_matches(
        math.nextafter(requested, float("inf")), requested
    )
    assert not replay_execution_module._wavelength_readback_matches(1.6e-6, requested)


def test_thermo_evidence_rejects_inconsistent_energy_and_negative_displacement_ratio(
    ascii_tmp_path,
):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path))
    payload = _payload("thermal_structural_solve", spec)
    payload["energy_balance"]["residual_W"] = 1.0
    with pytest.raises(ValueError, match="energy balance residual"):
        build_stage_evidence(spec, "thermal_structural_solve", payload)
    payload["energy_balance"] = {
        "source_W": 10.0,
        "loss_W": 1.0,
        "residual_W": 9.0,
        "relative_residual": 0.0,
    }
    with pytest.raises(ValueError, match="relative energy residual"):
        build_stage_evidence(spec, "thermal_structural_solve", payload)
    payload["energy_balance"]["relative_residual"] = 0.9
    assert build_stage_evidence(spec, "thermal_structural_solve", payload)
    state = _payload("state_evidence", spec)
    state["displacement_to_length"] = -1.0
    with pytest.raises(ValueError, match="displacement ratio"):
        build_stage_evidence(spec, "state_evidence", state)


def test_thermo_optical_evidence_uses_bounded_wavelength_tolerance(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path))
    payload = _payload("optical_replay", spec)
    payload["rows"][0]["solved_wavelength_m"] = math.nextafter(
        payload["rows"][0]["requested_wavelength_m"], float("inf")
    )
    assert build_stage_evidence(spec, "optical_replay", payload)
    payload["rows"][0]["solved_wavelength_m"] = 1.6e-6
    with pytest.raises(ValueError, match="wavelength readback"):
        build_stage_evidence(spec, "optical_replay", payload)


def test_rollback_availability_requires_a_persisted_checkpoint(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "rollback"))
    executor = ThermoOptomechanicalComsolExecutor(None, spec, ascii_tmp_path / "job")

    assert executor._rollback_available() is False
    executor.checkpoint_path.parent.mkdir(parents=True)
    executor.checkpoint_path.write_bytes(b"checkpoint")
    assert executor._rollback_available() is True


def test_deformation_transfer_readback_is_observed_twice_not_tautological(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(
        _raw_spec(ascii_tmp_path / "deformation-readback")
    )
    executor = ThermoOptomechanicalComsolExecutor(None, spec, ascii_tmp_path / "job")
    values = iter((1.0e-6, 2.0e-6))
    executor._load_derived = lambda: None
    executor._set_moving_mesh_active = lambda _enabled: None
    executor._study = lambda _key: None
    executor._bind_study_dataset = lambda _tag: None
    executor._evaluate = lambda key: next(values) if key == "displacement_max" else 0.0

    def save(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"model")

    executor._save = save

    assert executor._deformation_transfer()["readback_exact"] is False


@pytest.mark.parametrize("evaluation_fails", [False, True])
def test_transfer_uses_declared_mesh_displacement_without_structural_fallback(
    ascii_tmp_path, evaluation_fails
):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "mesh-readback"))
    spec["model_contract"]["expressions"]["transfer_displacement_max"] = "mesh_displacement"
    executor = ThermoOptomechanicalComsolExecutor(None, spec, ascii_tmp_path / "job")
    executor._load_derived = lambda: None
    executor._set_moving_mesh_active = lambda _enabled: None
    executor._study = lambda _key: None
    executor._bind_study_dataset = lambda _tag: None
    calls = []

    def evaluate(key):
        calls.append(key)
        if evaluation_fails:
            raise RuntimeError("mesh readback unavailable")
        return 1.7e-9

    def save(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"model")

    executor._evaluate = evaluate
    executor._save = save
    if evaluation_fails:
        with pytest.raises(RuntimeError, match="mesh readback unavailable"):
            executor._deformation_transfer()
        assert calls == ["transfer_displacement_max"]
    else:
        assert executor._deformation_transfer()["readback_exact"] is True
        assert calls == ["transfer_displacement_max", "transfer_displacement_max"]


def test_interrupted_transfer_restarts_from_the_pretransfer_checkpoint(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(
        _raw_spec(ascii_tmp_path / "transfer-resume")
    )
    executor = ThermoOptomechanicalComsolExecutor(None, spec, ascii_tmp_path / "job")
    loaded = []
    executor._load_derived = lambda: loaded.append("derived")
    executor._load_checkpoint = lambda: loaded.append("checkpoint")
    executor._set_moving_mesh_active = lambda _enabled: None
    executor._study = lambda _key: None
    executor._bind_study_dataset = lambda _tag: None
    executor._evaluate = lambda _key: 1.0e-6

    def save(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"model")

    executor._save = save

    # First pass: no checkpoint exists, so the pre-transfer derived model is
    # loaded, checkpointed, and reloaded after the transfer rebind.
    executor._deformation_transfer()
    assert loaded == ["derived", "derived"]
    assert executor.checkpoint_path.is_file()

    # Interrupted resume: the checkpoint now exists and derived.mph holds the
    # already-deformed model; the transfer must restart from the checkpoint
    # instead of stacking a second deformation. The trailing derived reload is
    # the post-transfer rebind.
    executor._deformation_transfer()
    assert loaded == ["derived", "derived", "checkpoint", "derived"]


def test_thermal_rerun_deletes_a_stale_transfer_checkpoint(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(
        _raw_spec(ascii_tmp_path / "thermal-invalidate")
    )
    executor = ThermoOptomechanicalComsolExecutor(None, spec, ascii_tmp_path / "job")
    executor.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    executor.checkpoint_path.write_bytes(b"stale checkpoint")
    executor.derived_path.write_bytes(b"derived")
    executor._verify_source = lambda: None
    executor._load_source = lambda: None
    executor._load_derived = lambda: None
    executor._apply_positive_parameters = lambda: None
    executor._set_moving_mesh_active = lambda _enabled: None
    executor._study = lambda _key: None
    executor._bind_study_dataset = lambda _tag: None
    executor._evaluate = lambda _key: 0.0
    executor._save = lambda path: (
        path.parent.mkdir(parents=True, exist_ok=True),
        path.write_bytes(b"model"),
    )

    executor._thermal_structural()

    # Rebuilding the pre-transfer state invalidated the stale checkpoint, so
    # the next transfer cannot restart from a state that predates this solve.
    assert not executor.checkpoint_path.exists()


def test_displacement_readback_tolerates_last_ulp_but_not_material_drift():
    value = 1.2345678901234e-6
    assert replay_execution_module._displacement_readback_matches(
        math.nextafter(value, float("inf")), value
    )
    assert (
        replay_execution_module._displacement_readback_matches(value * (1.0 + 1.0e-6), value)
        is False
    )


def test_executor_uses_normal_save_for_current_model_and_save_copy_for_checkpoint(
    ascii_tmp_path,
):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "save"))
    current = ascii_tmp_path / "current.mph"
    current.write_bytes(b"current")
    java = _JavaModel(current)
    executor = ThermoOptomechanicalComsolExecutor(None, spec, ascii_tmp_path / "job")
    executor.model = _ExecutorModel(java)
    executor._save(current)
    assert java.saved == [(str(current), False)]

    checkpoint = ascii_tmp_path / "checkpoint.mph"
    executor._save(checkpoint)
    assert java.saved[-1][1] is True
    assert checkpoint.read_bytes() == b"checkpoint"


class _Ownership:
    def __init__(self):
        self.acquired = False
        self.released = False

    def preflight(self, **_kwargs):
        return {"ready": True}

    def acquire(self, **_kwargs):
        self.acquired = True
        return {"success": True}

    def release(self):
        self.released = True
        return {"success": True}


class _Client:
    port = None

    def __init__(self):
        self.clear_count = 0

    def clear(self):
        self.clear_count += 1


def test_worker_publishes_completion_only_after_client_and_lease_cleanup(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "worker"))
    store = JobStore(ascii_tmp_path / "worker-runtime" / "jobs")
    job_id = store.create(
        spec,
        {
            "schema_version": "2",
            "status": "submitted",
            "attempt": 1,
            "worker_pid": None,
            "worker_process_create_time": None,
            "worker_command_signature": None,
            "progress": {"completed": 0, "total": 5},
            "last_error": None,
        },
    )
    owner = _Ownership()
    client = _Client()

    def factory(_client, current_spec, _root):
        return lambda stage, _directory, _spec: _payload(stage, current_spec)

    code = run_worker(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: owner,
        client_factory=lambda _spec: client,
        stage_executor_factory=factory,
        native_cancel_enabled=False,
    )
    state = store.read_state(job_id)
    assert code == 0
    assert state["status"] == "completed"
    assert state["thermo_optomechanical_summary"]["scientific_disposition"] == "accepted"
    assert state["cleanup"] == {
        "client_cleared": True,
        "lease_released": True,
        "errors": [],
    }
    assert client.clear_count == 1
    assert owner.acquired is True
    assert owner.released is True
    resource_entries = store.read_resource_journal(job_id)
    assert resource_entries
    assert {entry["stage"] for entry in resource_entries} >= {"pre_mesh", "pre_solve", "post_solve"}


@pytest.mark.parametrize("mesh_state", ["available", "failed", "absent"])
def test_worker_default_mesh_telemetry_refuses_without_stage_execution(
    ascii_tmp_path, monkeypatch, mesh_state
):
    from comsol_mcp.jobs.resource_admission import normalize_resource_policy
    from comsol_mcp.tools import mesh as mesh_module

    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "mesh-budget"))
    spec["resource_policy"] = normalize_resource_policy({"max_mesh_elements": 100})
    store = JobStore(ascii_tmp_path / "mesh-runtime" / "jobs")
    job_id = store.create(
        spec,
        {
            "schema_version": "2",
            "status": "submitted",
            "attempt": 1,
            "progress": {"completed": 0, "total": 5},
        },
    )
    model = object()
    calls = []

    class Executor:
        def __init__(self):
            self.model = None if mesh_state == "absent" else model

        def __call__(self, *_args):
            pytest.fail("resource refusal must precede stage execution")

    def mesh_info(current, *, component_name, mesh_name):
        assert current is model
        assert component_name == spec["model_contract"]["component_tag"]
        assert mesh_name == spec["model_contract"]["mesh_tag"]
        calls.append(current)
        if mesh_state == "failed":
            raise RuntimeError("mesh telemetry unavailable")
        return {"success": True, "mesh": {"num_elements": 101}}

    monkeypatch.setattr(mesh_module, "get_mesh_info", mesh_info)
    owner, client = _Ownership(), _Client()
    assert (
        run_worker(
            str(store.root),
            job_id,
            ownership_factory=lambda *_args: owner,
            client_factory=lambda _spec: client,
            stage_executor_factory=lambda *_args: Executor(),
            native_cancel_enabled=False,
        )
        == 0
    )
    state = store.read_state(job_id)
    assert state["status"] == "interrupted"
    assert state["resource_gate"]["decision"] == "refuse"
    assert calls == ([] if mesh_state == "absent" else [model])
    entries = store.read_resource_journal(job_id)
    assert entries[-1]["start_authorized"] is False
    assert not (store.job_dir(job_id) / "thermo_optomechanical_stages.jsonl").exists()
    assert owner.released is True
    assert client.clear_count == 1


@pytest.mark.parametrize("elements", [100, 101])
def test_worker_prepares_resource_model_before_mesh_admission(
    ascii_tmp_path, monkeypatch, elements
):
    from comsol_mcp.jobs.resource_admission import normalize_resource_policy
    from comsol_mcp.tools import mesh as mesh_module

    raw = _raw_spec(ascii_tmp_path / "prepare")
    path = Path(raw["specification_path"])
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["model_contract"]["component_tag"] = "thermal_component"
    manifest["material_state"]["target"]["component_tag"] = "thermal_component"
    manifest["model_contract"]["mesh_tag"] = "thermal_mesh"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    raw["specification_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    spec = normalize_thermo_optomechanical_replay_spec(raw)
    spec["resource_policy"] = normalize_resource_policy({"max_mesh_elements": 100})
    store = JobStore(ascii_tmp_path / "prepare-runtime" / "jobs")
    job_id = store.create(
        spec,
        {
            "schema_version": "2",
            "status": "submitted",
            "attempt": 1,
            "progress": {"completed": 0, "total": 5},
        },
    )
    calls = []
    model = object()

    class Executor:
        model = None

        def prepare_resource_model(self):
            calls.append("prepare")
            self.model = model

        def __call__(self, stage, *_args):
            calls.append(stage)
            return _payload(stage, spec)

    def mesh_info(current, *, component_name, mesh_name):
        assert current is model
        assert component_name == spec["model_contract"]["component_tag"]
        assert mesh_name == spec["model_contract"]["mesh_tag"]
        calls.append("sample")
        return {"success": True, "mesh": {"num_elements": elements}}

    monkeypatch.setattr(mesh_module, "get_mesh_info", mesh_info)
    assert (
        run_worker(
            str(store.root),
            job_id,
            ownership_factory=lambda *_args: _Ownership(),
            client_factory=lambda _spec: _Client(),
            stage_executor_factory=lambda *_args: Executor(),
            native_cancel_enabled=False,
        )
        == 0
    )
    assert calls[:2] == ["prepare", "sample"]
    assert calls.count("prepare") == 1
    state = store.read_state(job_id)
    assert state["status"] == ("completed" if elements == 100 else "interrupted")
    executed = [item for item in calls if item in THERMO_OPTOMECHANICAL_STAGES]
    assert executed == (list(THERMO_OPTOMECHANICAL_STAGES) if elements == 100 else [])


@pytest.mark.parametrize("derived", [False, True])
@pytest.mark.parametrize("mutate_source", [False, True])
def test_resource_preparation_loads_actual_model_without_solving(
    ascii_tmp_path, derived, mutate_source
):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "prepare-load"))
    calls = []
    model = object()

    def load(path):
        calls.append(("load", path))
        if mutate_source:
            Path(spec["source_model_path"]).write_bytes(b"changed during load")
        return model

    client = SimpleNamespace(clear=lambda: calls.append(("clear", None)), load=load)
    executor = ThermoOptomechanicalComsolExecutor(client, spec, ascii_tmp_path / "job")
    if derived:
        executor.derived_path.parent.mkdir(parents=True)
        executor.derived_path.write_bytes(b"derived")
    expected = executor.derived_path if derived else executor.source
    if mutate_source:
        with pytest.raises(RuntimeError, match="source changed"):
            executor.prepare_resource_model()
    else:
        executor.prepare_resource_model()
        executor.prepare_resource_model()
        assert executor.model is model
    assert calls == [("clear", None), ("load", str(expected))]


def test_resource_model_preparation_rejects_changed_source_before_load(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "changed"))
    calls = []
    client = SimpleNamespace(
        clear=lambda: calls.append("clear"), load=lambda _: calls.append("load")
    )
    executor = ThermoOptomechanicalComsolExecutor(client, spec, ascii_tmp_path / "job")
    Path(spec["source_model_path"]).write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="source changed"):
        executor.prepare_resource_model()
    assert calls == []


def test_worker_refuses_thermo_stage_when_resource_budget_is_exceeded(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(
        _raw_spec(ascii_tmp_path / "resource-refusal")
    )
    store = JobStore(ascii_tmp_path / "resource-refusal-runtime" / "jobs")
    job_id = store.create(
        spec,
        {
            "schema_version": "2",
            "status": "submitted",
            "attempt": 1,
            "worker_pid": None,
            "worker_process_create_time": None,
            "worker_command_signature": None,
            "progress": {"completed": 0, "total": 5},
            "last_error": None,
        },
    )

    def over_budget(stage, _point_id, _client, _directory, _elapsed):
        return {"stage": stage, "elapsed_wall_seconds": 301.0}

    code = run_worker(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: _Ownership(),
        client_factory=lambda _spec: _Client(),
        stage_executor_factory=lambda *_args: (
            lambda *_stage_args: pytest.fail("stage executor must not start")
        ),
        native_cancel_enabled=False,
        telemetry_provider=over_budget,
    )
    state = store.read_state(job_id)
    assert code == 0
    assert state["status"] == "interrupted"
    assert "before_stage_stop" in state["last_error"]["message"]
    entries = store.read_resource_journal(job_id)
    assert entries
    assert entries[-1]["entry_type"] == "admission"
    assert entries[-1]["decision"] == "refuse"
    assert entries[-1]["start_authorized"] is False
    assert state["resource_gate"]["latest_entry_sha256"] == entries[-1]["entry_sha256"]
    assert not (store.job_dir(job_id) / "thermo_optomechanical_stages.jsonl").exists()


@pytest.mark.parametrize("refusal_stage", ["pre_mesh", "post_solve", "pre_solve"])
def test_resource_refusal_resume_preserves_completed_stages(ascii_tmp_path, refusal_stage):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "resume-budget"))
    store = JobStore(ascii_tmp_path / "resume-budget-runtime" / "jobs")
    job_id = store.create(
        spec,
        {
            "schema_version": "2",
            "status": "submitted",
            "attempt": 1,
            "worker_pid": None,
            "worker_process_create_time": None,
            "worker_command_signature": None,
            "progress": {"completed": 0, "total": 5},
            "last_error": None,
        },
    )
    first_calls = []

    def factory(_client, current_spec, _root):
        def execute(stage, _directory, _spec):
            first_calls.append(stage)
            return _payload(stage, current_spec)

        return execute

    def telemetry(stage, _point_id, _client, _directory, _elapsed):
        return {"stage": stage, "elapsed_wall_seconds": 301.0 if stage == refusal_stage else 0.0}

    owner = _Ownership()
    client = _Client()
    assert (
        run_worker(
            str(store.root),
            job_id,
            ownership_factory=lambda *_args: owner,
            client_factory=lambda _spec: client,
            stage_executor_factory=factory,
            telemetry_provider=telemetry,
            native_cancel_enabled=False,
        )
        == 0
    )
    assert store.read_state(job_id)["status"] == "interrupted"
    expected_count = 0 if refusal_stage == "pre_mesh" else 1
    assert len(first_calls) == expected_count
    assert owner.released is True
    assert client.clear_count == 1
    journal = store.job_dir(job_id) / "thermo_optomechanical_stages.jsonl"
    original_rows = journal.read_bytes() if journal.exists() else b""
    assert not (store.job_dir(job_id) / "analysis" / "summary.json").exists()
    store.update_state(job_id, "starting", patch={"attempt": 2})
    second_calls = []

    def second_factory(_client, current_spec, _root):
        def execute(stage, _directory, _spec):
            second_calls.append(stage)
            return _payload(stage, current_spec)

        return execute

    fresh_owner, fresh_client = _Ownership(), _Client()
    assert (
        run_worker(
            str(store.root),
            job_id,
            ownership_factory=lambda *_args: fresh_owner,
            client_factory=lambda _spec: fresh_client,
            stage_executor_factory=second_factory,
            telemetry_provider=lambda stage, *_args: {"stage": stage, "elapsed_wall_seconds": 0.0},
            native_cancel_enabled=False,
        )
        == 0
    )
    assert store.read_state(job_id)["status"] == "completed"
    assert first_calls + second_calls == list(THERMO_OPTOMECHANICAL_STAGES)
    assert journal.read_bytes().startswith(original_rows)
    rows = read_thermo_optomechanical_stage_rows(journal, spec, artifact_root=store.job_dir(job_id))
    assert [row["attempt"] for row in rows] == [1] * expected_count + [2] * (5 - expected_count)
    assert {entry["attempt"] for entry in store.read_resource_journal(job_id)} == {1, 2}


class _ThermalFeature:
    def __init__(self, feature_type, selections):
        self.feature_type = feature_type
        self.values = {}
        self.selections = selections
        self.selection_tag = None
        self.ignore_set = False
        self.ignore_selection = False

    def getType(self):
        return self.feature_type

    def set(self, key, value):
        if not self.ignore_set:
            self.values[key] = value

    def getString(self, key):
        return self.values.get(key, "stale")

    def selection(self):
        return self

    def named(self, tag):
        if not self.ignore_selection:
            self.selection_tag = tag

    def entities(self):
        return self.selections.get(self.selection_tag, [99])


def _thermal_executor_fixture(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "load-readback"))
    selections = {"sel_heat": [3, 7], "sel_temp": [2, 4, 6]}
    source = _ThermalFeature("HeatSource", selections)
    convection = _ThermalFeature("HeatFluxBoundary", selections)
    convection.values["HeatFluxType"] = "ConvectiveHeatFlux"
    features = {"custom_source": source, "custom_flux": convection}
    physics = SimpleNamespace(
        feature=lambda tag=None: (
            features[tag] if tag else SimpleNamespace(tags=lambda: list(features))
        )
    )
    component = SimpleNamespace(
        physics=lambda tag: physics,
        selection=lambda tag: SimpleNamespace(entities=lambda: selections[tag]),
    )
    executor = ThermoOptomechanicalComsolExecutor(None, spec, ascii_tmp_path / "job")
    executor.model = SimpleNamespace(java=SimpleNamespace(component=lambda tag: component))
    return executor, source, convection, selections, features


def test_thermal_load_applies_values_units_and_named_selections(ascii_tmp_path):
    executor, source, convection, _selections, _features = _thermal_executor_fixture(ascii_tmp_path)
    executor.spec["thermal_load"]["volumetric_heat_source_W_per_m3"] = 25.0
    executor.spec["thermal_load"]["convection_coefficient_W_per_m2_K"] = 12.5
    executor._apply_thermal_load_features()
    assert source.values == {"Q0": "25[W/m^3]"}
    assert convection.values == {
        "HeatFluxType": "ConvectiveHeatFlux",
        "h": "12.5[W/(m^2*K)]",
        "Text": "300[K]",
    }
    assert source.selection_tag == "sel_heat"
    assert convection.selection_tag == "sel_temp"
    assert source.entities() == [3, 7]
    assert convection.entities() == [2, 4, 6]


@pytest.mark.parametrize("feature", ["source", "convection"])
def test_thermal_load_rejects_silent_parameter_set(ascii_tmp_path, feature):
    executor, source, convection, _selections, _features = _thermal_executor_fixture(ascii_tmp_path)
    (source if feature == "source" else convection).ignore_set = True
    with pytest.raises(RuntimeError, match="did not read back exactly"):
        executor._apply_thermal_load_features()


def test_thermal_load_does_not_treat_general_flux_as_convection(ascii_tmp_path):
    executor, _source, convection, _selections, _features = _thermal_executor_fixture(
        ascii_tmp_path
    )
    convection.values["HeatFluxType"] = "GeneralInwardHeatFlux"
    with pytest.raises(RuntimeError, match="exactly one ConvectiveHeatFlux"):
        executor._apply_thermal_load_features()
    assert convection.values == {"HeatFluxType": "GeneralInwardHeatFlux"}


def test_thermal_load_ignores_other_heat_flux_subtypes(ascii_tmp_path):
    executor, _source, convection, selections, features = _thermal_executor_fixture(ascii_tmp_path)
    other = _ThermalFeature("HeatFluxBoundary", selections)
    other.values["HeatFluxType"] = "GeneralInwardHeatFlux"
    features["other_flux"] = other
    executor._apply_thermal_load_features()
    assert convection.values["h"] == "10[W/(m^2*K)]"
    assert other.values == {"HeatFluxType": "GeneralInwardHeatFlux"}


@pytest.mark.parametrize("feature", ["source", "convection"])
def test_thermal_load_rejects_silent_selection_bind(ascii_tmp_path, feature):
    executor, source, convection, _selections, _features = _thermal_executor_fixture(ascii_tmp_path)
    (source if feature == "source" else convection).ignore_selection = True
    with pytest.raises(RuntimeError, match="selection .* did not read back exactly"):
        executor._apply_thermal_load_features()


@pytest.mark.parametrize("tag", ["sel_heat", "sel_temp"])
def test_thermal_load_rejects_empty_selection(ascii_tmp_path, tag):
    executor, _source, _convection, selections, _features = _thermal_executor_fixture(
        ascii_tmp_path
    )
    selections[tag] = []
    with pytest.raises(RuntimeError, match="selection .* is empty"):
        executor._apply_thermal_load_features()


@pytest.mark.parametrize("mutation", ["missing", "duplicate"])
def test_thermal_load_rejects_ambiguous_convection_feature(ascii_tmp_path, mutation):
    executor, _source, convection, _selections, features = _thermal_executor_fixture(ascii_tmp_path)
    if mutation == "missing":
        del features["custom_flux"]
    else:
        features["other_flux"] = convection
    with pytest.raises(RuntimeError, match="exactly one ConvectiveHeatFlux"):
        executor._apply_thermal_load_features()


def test_native_cancel_monitor_failure_becomes_durable_worker_error(ascii_tmp_path, monkeypatch):
    spec = normalize_thermo_optomechanical_replay_spec(
        _raw_spec(ascii_tmp_path / "native-monitor-error")
    )
    store = JobStore(ascii_tmp_path / "native-monitor-runtime" / "jobs")
    job_id = store.create(
        spec,
        {
            "schema_version": "2",
            "status": "submitted",
            "attempt": 1,
            "worker_pid": None,
            "worker_process_create_time": None,
            "worker_command_signature": None,
            "progress": {"completed": 0, "total": 5},
            "last_error": None,
        },
    )
    monkeypatch.setattr(
        "src.jobs.native_cancel_probe.request_native_cancel_once",
        lambda: (_ for _ in ()).throw(RuntimeError("injected native monitor failure")),
    )
    requested = False

    def factory(_client, current_spec, _root):
        def execute(stage, _directory, _spec):
            nonlocal requested
            if not requested:
                requested = True
                store.request_cancel(job_id, requester_identity=process_identity(os.getpid()))
                time.sleep(0.1)
            return _payload(stage, current_spec)

        return execute

    code = run_worker(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: _Ownership(),
        client_factory=lambda _spec: _Client(),
        stage_executor_factory=factory,
        native_cancel_enabled=True,
    )
    state = store.read_state(job_id)

    assert code == 1
    assert state["status"] == "cancel_requested"
    assert "native cancel monitor failed" in state["cancel"]["worker_error"]["message"]


def test_stuck_native_monitor_retains_the_ownership_lease(ascii_tmp_path, monkeypatch):
    spec = normalize_thermo_optomechanical_replay_spec(
        _raw_spec(ascii_tmp_path / "native-monitor-stuck")
    )
    store = JobStore(ascii_tmp_path / "native-monitor-runtime" / "jobs")
    job_id = store.create(
        spec,
        {
            "schema_version": "2",
            "status": "submitted",
            "attempt": 1,
            "worker_pid": None,
            "worker_process_create_time": None,
            "worker_command_signature": None,
            "progress": {"completed": 0, "total": 5},
            "last_error": None,
        },
    )

    entered = threading.Event()
    release = threading.Event()

    def blocked_probe():
        entered.set()
        assert release.wait(timeout=10)
        return {"requested": False}

    monkeypatch.setattr("src.jobs.native_cancel_probe.request_native_cancel_once", blocked_probe)
    requested = False

    def factory(_client, current_spec, _root):
        def execute(stage, _directory, _spec):
            nonlocal requested
            if not requested:
                requested = True
                store.request_cancel(job_id, requester_identity=process_identity(os.getpid()))
                assert entered.wait(timeout=2)
            return _payload(stage, current_spec)

        return execute

    ownership = _Ownership()
    try:
        code = run_worker(
            str(store.root),
            job_id,
            ownership_factory=lambda *_args: ownership,
            client_factory=lambda _spec: _Client(),
            stage_executor_factory=factory,
            native_cancel_enabled=True,
        )
        state = store.read_state(job_id)

        assert code == 1
        assert ownership.released is False
        assert state["status"] in {"cancel_requested", "cancelling"}
        recorded = state["cancel"]["worker_error"]["cleanup_errors"]
        assert any(
            "native_cancel_thread:still_active_after_join_timeout" in item for item in recorded
        )
    finally:
        monitors = [
            thread for thread in threading.enumerate() if thread.name == "comsol-native-cancel"
        ]
        release.set()
        for monitor in monitors:
            monitor.join(timeout=2)
            assert not monitor.is_alive()


def test_worker_rejects_changed_submission_manifest_before_client_start(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(
        _raw_spec(ascii_tmp_path / "changed-manifest")
    )
    store = JobStore(ascii_tmp_path / "changed-manifest-runtime" / "jobs")
    job_id = store.create(
        spec,
        {
            "schema_version": "2",
            "status": "submitted",
            "attempt": 1,
            "worker_pid": None,
            "worker_process_create_time": None,
            "worker_command_signature": None,
            "progress": {"completed": 0, "total": 5},
            "last_error": None,
        },
    )
    Path(spec["submission_manifest_path"]).write_text("{}\n", encoding="utf-8")
    owner = _Ownership()

    code = run_worker(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: owner,
        client_factory=lambda _spec: pytest.fail("client must not start"),
        native_cancel_enabled=False,
    )
    state = store.read_state(job_id)
    assert code == 1
    assert state["status"] == "failed"
    assert "manifest changed before client startup" in state["last_error"]["message"]
    assert owner.acquired is False


def test_worker_reconciles_cancellation_racing_the_starting_transition(ascii_tmp_path, monkeypatch):
    spec = normalize_thermo_optomechanical_replay_spec(
        _raw_spec(ascii_tmp_path / "startup-cancel-race")
    )
    store = JobStore(ascii_tmp_path / "startup-cancel-runtime" / "jobs")
    job_id = store.create(
        spec,
        {
            "schema_version": "2",
            "status": "submitted",
            "attempt": 1,
            "worker_pid": None,
            "worker_process_create_time": None,
            "worker_command_signature": None,
            "progress": {"completed": 0, "total": 5},
            "last_error": None,
        },
    )
    original_update = JobStore.update_state
    injected = False

    def racing_update(self, current_job_id, new_status=None, **kwargs):
        nonlocal injected
        if current_job_id == job_id and new_status == "starting" and not injected:
            injected = True
            self.request_cancel(current_job_id, requester_identity=process_identity(os.getpid()))
        return original_update(self, current_job_id, new_status, **kwargs)

    monkeypatch.setattr(JobStore, "update_state", racing_update)
    code = run_worker(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: pytest.fail("ownership must not start"),
        client_factory=lambda _spec: pytest.fail("client must not start"),
        native_cancel_enabled=False,
    )
    state = store.read_state(job_id)

    assert code == 0
    assert state["status"] == "cancel_requested"
    assert state["cancel"]["cooperative_observation"]["target_attempt"] == 1


def test_optical_branch_is_validated_against_grid_before_hashing(ascii_tmp_path):
    from comsol_mcp.jobs.thermo_optomechanical_replay import (
        normalize_thermo_optomechanical_replay_spec,
    )

    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "branch"))
    payload = _payload("optical_replay", spec)
    payload["rows"][0]["branch"] = ["x"]

    with pytest.raises(ValueError, match="coordinates differ"):
        build_stage_evidence(spec, "optical_replay", payload)


def test_control_id_type_drift_fails_closed(ascii_tmp_path):
    from comsol_mcp.jobs.thermo_optomechanical_replay import (
        normalize_thermo_optomechanical_replay_spec,
    )

    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "control"))
    payload = _payload("optical_replay", spec)
    payload["control_results"][0]["control_id"] = ["thermal"]

    with pytest.raises(ValueError, match="control matrix is incomplete"):
        build_stage_evidence(spec, "optical_replay", payload)


def test_spec_size_bound_includes_the_persisted_fingerprint(ascii_tmp_path, monkeypatch):
    import comsol_mcp.jobs.thermo_optomechanical_replay as replay_spec_module

    raw = _raw_spec(ascii_tmp_path / "cap")
    spec = replay_spec_module.normalize_thermo_optomechanical_replay_spec(raw)
    bound_with_fingerprint = len(replay_spec_module._canonical_bytes(spec))
    monkeypatch.setattr(
        replay_spec_module,
        "MAX_THERMO_OPTOMECHANICAL_SPEC_BYTES",
        bound_with_fingerprint - 1,
    )

    with pytest.raises(ValueError, match="exceeds its bound"):
        replay_spec_module.normalize_thermo_optomechanical_replay_spec(raw)


def test_summary_separates_executed_structural_and_declared_control_counts(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "counts"))
    root = ascii_tmp_path / "counts-artifacts"
    result = run_thermo_optomechanical_replay(
        spec, root, attempt=1, stage_executor=lambda stage, d, s: _payload(stage, s)
    )
    summary = result["summary"]

    structural = {"wrong_selection", "missing_material_state"}
    declared = {"convection", "temperature_unit"}
    executed = set(spec["validation_controls"]) - structural - declared
    assert summary["control_count"] == len(spec["validation_controls"])
    assert summary["executed_control_count"] == len(executed)
    assert summary["structural_assertion_count"] == len(
        structural & set(spec["validation_controls"])
    )
    assert summary["declared_value_count"] == len(declared & set(spec["validation_controls"]))
    assert (
        summary["executed_control_count"]
        + summary["structural_assertion_count"]
        + summary["declared_value_count"]
        == summary["control_count"]
    )


def test_a_failing_control_is_structurally_valid_rejection_evidence(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "mut"))
    for broken in ("convection", "fixed_boundary", "wrong_selection"):
        payload = _payload("optical_replay", spec)
        for row in payload["control_results"]:
            if row["control_id"] == broken:
                row["passed"] = False
        # A well-formed failed control persists as scientific rejection
        # evidence; the summary, not the validator, rejects the run.
        _validate_stage_payload("optical_replay", payload, spec)
    nonboolean = _payload("optical_replay", spec)
    nonboolean["control_results"][0]["passed"] = "false"
    with pytest.raises(ValueError, match="control matrix is incomplete"):
        _validate_stage_payload("optical_replay", nonboolean, spec)


def test_control_rows_without_provenance_are_rejected(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "oldshape"))
    payload = _payload("optical_replay", spec)
    legacy = [
        {
            "control_id": item["control_id"],
            "passed": item["passed"],
            "reason_code": item["reason_code"],
        }
        for item in payload["control_results"]
    ]
    with pytest.raises(ValueError, match="control matrix is incomplete"):
        _validate_stage_payload("optical_replay", {**payload, "control_results": legacy}, spec)

    invalid = [dict(item) for item in payload["control_results"]]
    invalid[0]["provenance"] = "guessed"
    with pytest.raises(ValueError, match="control matrix is incomplete"):
        _validate_stage_payload("optical_replay", {**payload, "control_results": invalid}, spec)


def test_transfer_evidence_distinguishes_checkpoint_integrity_from_rollback(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "rb"))
    payload = _payload("deformation_transfer", spec)

    assert payload["checkpoint_integrity_verified"] is True
    assert payload["rollback_exercised"] is False
    assert payload["rollback_evidence"] == "checkpoint_integrity_only_no_rollback_in_transfer_stage"
    # An unsupported exercised-rollback claim is refused by the validator.
    forged = dict(payload)
    forged["rollback_exercised"] = True
    with pytest.raises(ValueError, match="deformation transfer evidence is invalid"):
        _validate_stage_payload("deformation_transfer", forged, spec)


def test_inversion_statistic_is_reported_as_inferred_not_measured(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "inv"))
    payload = _payload("state_evidence", spec)

    assert payload["mesh"]["inverted_element_count"] in (0, None)
    assert payload["mesh"]["inverted_element_count_disposition"] == "inferred_from_minimum_quality"
    # An evidence row claiming a measured inversion count without the
    # inference disposition is refused.
    forged = json.loads(json.dumps(payload))
    del forged["mesh"]["inverted_element_count_disposition"]
    with pytest.raises(ValueError, match="mesh evidence"):
        _validate_stage_payload("state_evidence", forged, spec)


def test_a_well_formed_failed_control_persists_and_rejects_without_rerun(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "rej"))
    root = ascii_tmp_path / "rej-artifacts"
    calls = []

    def executor(stage_id, stage_dir, current_spec):
        calls.append(stage_id)
        payload = _payload(stage_id, current_spec)
        if stage_id == "optical_replay":
            for row in payload["control_results"]:
                if row["control_id"] == "convection":
                    row["passed"] = False
        return payload

    result = run_thermo_optomechanical_replay(spec, root, attempt=1, stage_executor=executor)

    assert result["summary"]["scientific_disposition"] == "rejected"
    assert "control_matrix_failed" in result["summary"]["reason_codes"]
    rows = read_thermo_optomechanical_stage_rows(
        root / "thermo_optomechanical_stages.jsonl", spec, artifact_root=root
    )
    optical = next(row for row in rows if row["stage_id"] == "optical_replay")
    failed = [
        item
        for item in _load_stage_evidence_public(root, "optical_replay")["control_results"]
        if item["control_id"] == "convection"
    ]
    assert failed and failed[0]["passed"] is False
    assert optical["execution_state"] == "completed"

    resumed = run_thermo_optomechanical_replay(spec, root, attempt=2, stage_executor=executor)
    assert resumed["summary"]["scientific_disposition"] == "rejected"
    assert calls.count("optical_replay") == 1


def _load_stage_evidence_public(root, stage_id):
    import json as _json

    return _json.loads((root / stage_id / "evidence.json").read_text(encoding="utf-8"))["payload"]


def test_summary_rejects_on_retained_nonpassive_measurements(ascii_tmp_path):
    """C081-08: finite unphysical rows stay durable and reject the summary."""
    spec = normalize_thermo_optomechanical_replay_spec(
        _raw_spec(ascii_tmp_path / "nonpassive-summary")
    )

    def executor(stage_id, _stage_dir, stage_spec):
        payload = _payload(stage_id, stage_spec)
        if stage_id == "optical_replay":
            payload["rows"][0]["deformed_rta"]["R"] = 1.2
            payload["rows"][0]["deformed_rta"]["T"] = 0.1
            payload["rows"][0]["deformed_rta"]["A"] = -0.3
            payload["rows"][0]["deformed_rta"]["closure_residual"] = 0.0
        return payload

    root = ascii_tmp_path / "nonpassive-summary" / "job"
    result = run_thermo_optomechanical_replay(spec, root, attempt=1, stage_executor=executor)

    summary = result["summary"]
    assert summary["scientific_disposition"] == "rejected"
    assert "optical_rta_nonpassive" in summary["reason_codes"]
    # The measured rows themselves survive as durable stage evidence.
    optical = (root / "optical_replay" / "evidence.json").read_text(encoding="utf-8")
    assert '"measured_gate_failed"' in optical
    assert '"R": 1.2' in optical
