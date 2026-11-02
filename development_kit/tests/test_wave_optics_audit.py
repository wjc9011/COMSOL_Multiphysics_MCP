"""Unit gates for policy-separated one-point Wave Optics evidence."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
from src.evidence.contracts import example_validation_policies
from src.tools.wave_optics_audit import (
    _load_air_reference,
    _load_policy,
    _replace_clone_materials_with_air,
    _single_real,
    _strict_positive_entity_ids,
    evaluate_validation_policy,
    run_wave_optics_point_audit,
    run_wave_optics_reference_audit,
)


@pytest.mark.parametrize("value", [[True], [1.5], ["2"]])
def test_wave_optics_entity_ids_reject_coercible_non_integer_values(value):
    with pytest.raises(ValueError, match="positive integer"):
        _strict_positive_entity_ids(value, "entity_ids")


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _measurement(*, absorption=1.2, closure=0.5, evidence_level="label_only"):
    return {
        "power": {
            "complete": True,
            "R": 0.2,
            "T": 0.1,
            "A": absorption,
            "closure_abs": closure,
        },
        "wavelength": {
            "complete": True,
            "absolute_difference_m": 2e-12,
            "relative_difference": 4e-7,
        },
        "polarization": {"evidence_level": evidence_level},
        "losses": {"items": []},
        "mesh": {"element_count": 1000, "unchanged_during_audit": True},
        "integrity": {"source_unchanged": True},
    }


def test_same_evidence_can_fail_or_pass_two_declared_policies():
    evidence = _measurement(absorption=0.7, closure=0.05)
    original = deepcopy(evidence)
    strict = evaluate_validation_policy(evidence, {"tolerances": {"closure_abs": 0.01}})
    permissive = evaluate_validation_policy(evidence, {"tolerances": {"closure_abs": 0.1}})

    assert strict["overall"] == "fail"
    assert permissive["overall"] == "pass"
    assert evidence == original


def test_empty_legacy_policy_cannot_produce_a_pass_verdict():
    result = evaluate_validation_policy(_measurement(), {})

    assert result["overall"] == "missing"
    assert result["rules"] == []


@pytest.mark.parametrize(
    "policy",
    [
        {"assumptions": {"passive": 1}},
        {"mesh": {"require_unchanged": "yes"}},
        {"tolerances": {"closure_abs": -1.0}},
        {"tolerances": {"closure_abs": float("inf")}},
        pytest.param(
            {"tolerances": {"closure_abs": 10**10_000}},
            id="huge-integer-tolerance",
        ),
    ],
)
def test_legacy_policy_requires_exact_booleans_and_nonnegative_finite_tolerances(
    policy,
):
    with pytest.raises(ValueError):
        _load_policy(policy, None)


def test_a_above_one_is_classified_only_under_passive_normalized_assumptions():
    evidence = _measurement()
    passive = evaluate_validation_policy(
        evidence,
        {
            "assumptions": {"passive": True, "port_power_normalized": True},
            "tolerances": {"quantity_bounds_margin": 0.0},
        },
    )
    gain_or_unknown = evaluate_validation_policy(
        evidence,
        {
            "assumptions": {"passive": False, "port_power_normalized": True},
            "tolerances": {"quantity_bounds_margin": 0.0},
        },
    )

    assert passive["overall"] == "fail"
    assert gain_or_unknown["overall"] == "pass"
    assert gain_or_unknown["rules"][0]["outcome"] == "not_applicable"


def test_label_only_polarization_is_missing_only_when_policy_requires_incident_evidence():
    result = evaluate_validation_policy(
        _measurement(), {"required_evidence": ["incident_polarization"]}
    )

    assert result["overall"] == "missing"
    assert result["rules"][0]["measured"] is False


def test_incomplete_incident_reference_cannot_elevate_evidence_level():
    evidence = _measurement(
        absorption=0.3,
        closure=0.0,
        evidence_level="incident_reference",
    )
    evidence["polarization"]["incident_reference"] = {
        "component_statistics": {
            "x": {"complex_mean": {"real": 1.0}},
        }
    }

    result = evaluate_validation_policy(
        evidence,
        {"required_evidence": ["incident_polarization"]},
    )

    assert result["overall"] == "missing"
    assert result["rules"][0]["measured"] is False


@pytest.mark.parametrize("value", [1.0 + 0.1j, np.asarray([2.0 - 0.2j])])
def test_materially_complex_scientific_scalars_are_rejected(value):
    with pytest.raises(ValueError, match="materially complex"):
        _single_real(value, "ewfd.Atotal")


def test_numerical_imaginary_noise_is_tolerated_for_real_scientific_scalars():
    assert _single_real(1.0 + 1e-14j, "ewfd.Atotal") == pytest.approx(1.0)


def test_point_audit_does_not_publish_materially_complex_power_as_real(
    tmp_path,
    monkeypatch,
):
    result, _source = _run(
        tmp_path,
        monkeypatch,
        absorption=0.3 + 0.1j,
    )

    assert result["audit_status"] == "measurement_partial"
    assert result["measurement"]["power"]["complete"] is False
    assert "A" not in result["measurement"]["power"]
    assert any(
        item["code"] == "power_expression_unavailable" and item["quantity"] == "A"
        for item in result["measurement"]["measurement_errors"]
    )


def test_wavelength_differences_remain_raw_until_policy_supplies_tolerances():
    evidence = _measurement(absorption=0.3, closure=0.0)
    strict = evaluate_validation_policy(
        evidence,
        {"tolerances": {"wavelength_abs_m": 1e-12, "wavelength_rel": 1e-7}},
    )
    loose = evaluate_validation_policy(
        evidence,
        {"tolerances": {"wavelength_abs_m": 3e-12, "wavelength_rel": 5e-7}},
    )

    assert strict["overall"] == "fail"
    assert loose["overall"] == "pass"
    assert evidence["wavelength"]["absolute_difference_m"] == 2e-12


def test_loss_without_normalization_remains_raw_evidence():
    evidence = _measurement(absorption=0.3, closure=0.0)
    evidence["losses"]["items"] = [{"label": "Au", "value": 0.2, "expression": "intAu(ewfd.Qh)"}]
    result = evaluate_validation_policy(evidence, {"required_evidence": ["volume_loss"]})

    assert result["overall"] == "pass"
    assert "normalized_value" not in evidence["losses"]["items"][0]


def test_air_reference_requires_matching_config_and_field_statistics(tmp_path):
    artifact = tmp_path / "air.json"
    payload = {
        "config_id": "air-v4",
        "component_statistics": {
            axis: {"complex_mean": {"real": value, "imag": 0.0}}
            for axis, value in (("x", 1.0), ("y", 0.0), ("z", 0.0))
        },
        "stokes_xy": {"S0": 1.0, "S3": 0.0},
    }
    artifact.write_text(json.dumps(payload), encoding="utf-8")

    valid, warnings = _load_air_reference(str(artifact), "air-v4")
    invalid, mismatch = _load_air_reference(str(artifact), "other")

    assert valid["config_id"] == "air-v4"
    assert not warnings
    assert invalid is None
    assert mismatch[0]["code"] == "air_reference_config_mismatch"


def test_reference_audit_output_round_trips_as_point_audit_air_reference(tmp_path, monkeypatch):
    result, _source, _cleanup_calls = _run_reference(tmp_path, monkeypatch)

    reference, warnings = _load_air_reference(result["artifacts"]["manifest"], "unit-reference")

    assert reference is not None
    assert reference["component_statistics"]["x"]["complex_mean"]["real"] == 1.0
    assert warnings == []


def test_valid_incident_reference_supports_declared_vector_policy_and_config_gate():
    evidence = _measurement(absorption=0.3, closure=0.0, evidence_level="incident_reference")
    evidence["polarization"]["incident_reference"] = {
        "config_id": "air-v4",
        "component_statistics": {
            "x": {"complex_mean": {"real": 1.0, "imag": 0.0}},
            "y": {"complex_mean": {"real": 0.0, "imag": 0.0}},
            "z": {"complex_mean": {"real": 0.0, "imag": 0.0}},
        },
        "stokes_xy": {"S0": 1.0, "S3": 0.0},
    }
    valid = evaluate_validation_policy(
        evidence,
        {
            "required_evidence": ["incident_polarization"],
            "polarization": {
                "reference_config_id": "air-v4",
                "target_vector": [1, 0, 0],
                "max_cross_power_fraction": 1e-6,
                "max_ellipticity": 1e-6,
            },
        },
    )
    mismatch = evaluate_validation_policy(
        evidence,
        {"polarization": {"reference_config_id": "different"}},
    )

    assert valid["overall"] == "pass"
    assert mismatch["overall"] == "fail"


class Container:
    def __init__(self, items):
        self.items = items

    def tags(self):
        return list(self.items)

    def get(self, tag):
        return self.items[str(tag)]


class Selection:
    def __init__(self, entities):
        self._entities = entities

    def entities(self, *_args):
        return self._entities


class Mesh:
    def getNumElem(self):
        return 1200

    def getNumVertex(self):
        return 600


class Component:
    def __init__(self):
        self._physics = Container({"ewfd": object()})
        self._mesh = Container({"mesh1": Mesh()})

    def physics(self):
        return self._physics

    def mesh(self):
        return self._mesh

    def selection(self, tag):
        assert tag == "topair"
        return Selection([3])


class Step:
    def __init__(self):
        self.values = {"plist": "original_plist"}

    def set(self, name, value):
        self.values[name] = value

    def getString(self, name):
        return self.values[name]


class Study:
    def __init__(self, model):
        self.model = model
        self.step = Step()

    def feature(self):
        return Container({"wl_step": self.step})

    def run(self):
        self.model.solved = True
        if self.model.drift_source:
            self.model.path.write_bytes(self.model.path.read_bytes() + b"drift")


class Parameters:
    def __init__(self):
        self.values = {"wl": "4.36[um]"}

    def set(self, name, value):
        self.values[name] = value


class JavaModel:
    def __init__(self, model):
        self._component = Component()
        self._study = Study(model)
        self._param = Parameters()

    def component(self, tag=None):
        return Container({"comp1": self._component}) if tag is None else self._component

    def study(self, tag=None):
        return Container({"std1": self._study}) if tag is None else self._study

    def param(self):
        return self._param


class AuditModel:
    def __init__(
        self,
        path,
        *,
        name="AuditModel",
        absorption=1.2,
        nonfinite=False,
        drift_source=False,
        evaluated_wavelength_m=4.37e-6,
        solved_wavelength_m=4.37e-6,
    ):
        self.path = path
        self._name = name
        self.absorption = absorption
        self.nonfinite = nonfinite
        self.drift_source = drift_source
        self.evaluated_wavelength_m = evaluated_wavelength_m
        self.solved_wavelength_m = solved_wavelength_m
        self.solved = False
        self.java = JavaModel(self)

    def file(self):
        return str(self.path)

    def name(self):
        return self._name

    def parameters(self, evaluate=False):
        assert evaluate is False
        return dict(self.java._param.values)

    def evaluate(self, expression):
        if isinstance(expression, list):
            if expression == ["wl", "c_const/ewfd.freq"]:
                return [self.evaluated_wavelength_m, self.solved_wavelength_m]
            if expression[-1] == "dom":
                return [
                    np.array([1 + 0j, 1 + 0j, 1 + 0j]),
                    np.array([0 + 0j, 0 + 0j, 0 + 0j]),
                    np.array([0 + 0j, 0 + 0j, 0 + 0j]),
                    np.array([0.1, 0.2, 0.3]),
                    np.array([0.1, 0.2, 0.3]),
                    np.array([0.8, 0.85, 0.9]),
                    np.array([3, 3, 3]),
                ]
        values = {
            "ewfd.Rtotal": 0.2,
            "ewfd.Ttotal": 0.1,
            "ewfd.Atotal": float("nan") if self.nonfinite else self.absorption,
            "4.37[um]": 4.37e-6,
            "intAu(ewfd.Qh)": 0.2,
            "flux_inc": -2.0,
            "flux_refl": 0.4,
            "flux_trans": -1.2,
            "ewfd.sigmaAbs": 2.0e-13,
            "px*py": 1.0e-12,
            "intLoss(ewfd.Qh)": 0.4,
            "incident_flux": -2.0,
        }
        return values[expression]


def _run(tmp_path, monkeypatch, **model_options):
    validation_policy = model_options.pop("validation_policy", None)
    declared_plane_flux = model_options.pop("declared_plane_flux", None)
    internal_absorption = model_options.pop("internal_absorption", None)
    model_observer = model_options.pop("model_observer", None)
    model_setup = model_options.pop("model_setup", None)
    source = tmp_path / "source.mph"
    source.write_bytes(b"immutable")
    model = AuditModel(source, **model_options)
    if model_observer is not None:
        model_observer.append(model)
    if model_setup is not None:
        model_setup(model)
    monkeypatch.setattr(
        "src.tools.wave_optics_audit.collect_wave_optics_preflight",
        lambda *_args, **_kwargs: {
            "inspection_status": "complete",
            "evidence": {
                "observations": [],
                "warnings": [],
                "unknowns": [],
                "integrity_errors": [],
            },
        },
    )
    monkeypatch.setattr(
        "src.tools.wave_optics_audit.ownership_manager.status",
        lambda **_kwargs: {"collision": False, "lease": {"state": "active"}},
    )
    monkeypatch.setattr(
        "src.tools.wave_optics_audit._validate_ascii_dir",
        lambda _value: tmp_path / "artifacts",
    )
    result = run_wave_optics_point_audit(
        model,
        model_name="AuditModel",
        component_tag="comp1",
        physics_tag="ewfd",
        study_tag="std1",
        wavelength_value=4.37,
        wavelength_unit="um",
        wavelength_parameter="wl",
        study_step_tag="wl_step",
        study_step_property="plist",
        expected_source_sha256=_hash(source),
        config_id="unit-audit",
        artifact_dir=str(tmp_path / "ascii_artifacts"),
        top_air_selection="topair",
        top_air_coordinate_range={"x": [0, 1], "y": [0, 1], "z": [0.7, 1]},
        loss_map=[{"label": "Au", "domains": [2], "expression": "intAu(ewfd.Qh)"}],
        declared_plane_flux=declared_plane_flux,
        internal_absorption=internal_absorption,
        validation_policy=validation_policy,
        session_state={"connected": True},
        active_profile="wave_optics",
        ownership_preflight={"ready": True},
    )
    return result, source


def _declared_plane_flux():
    return {
        "incident": {
            "expression": "flux_inc",
            "selection_ids": [10],
            "plane_coordinate_m": 1.0e-6,
            "normal": [0.0, 0.0, -1.0],
            "medium_id": "lossless_air",
            "positive_power_sign": -1,
        },
        "reflected": {
            "expression": "flux_refl",
            "selection_ids": [11],
            "plane_coordinate_m": 1.0e-6,
            "normal": [0.0, 0.0, 1.0],
            "medium_id": "lossless_air",
            "positive_power_sign": 1,
        },
        "transmitted": {
            "expression": "flux_trans",
            "selection_ids": [12],
            "plane_coordinate_m": -1.0e-6,
            "normal": [0.0, 0.0, -1.0],
            "medium_id": "lossless_air",
            "positive_power_sign": -1,
        },
    }


def test_point_audit_wavelength_difference_includes_requested_value(tmp_path, monkeypatch):
    result, _source = _run(
        tmp_path,
        monkeypatch,
        evaluated_wavelength_m=10.0e-6,
        solved_wavelength_m=10.0e-6,
    )

    wavelength = result["measurement"]["wavelength"]
    assert wavelength["evaluated_to_solved_difference_m"] == 0.0
    assert wavelength["requested_to_evaluated_difference_m"] == pytest.approx(-5.63e-6)
    assert wavelength["requested_to_solved_difference_m"] == pytest.approx(-5.63e-6)
    assert wavelength["absolute_difference_m"] == pytest.approx(5.63e-6)
    assert wavelength["relative_difference"] == pytest.approx(5.63 / 4.37)


def _internal_absorption():
    return {
        "cross_section_expression": "ewfd.sigmaAbs",
        "cross_section_unit": "m^2",
        "unit_cell_area_expression": "px*py",
        "source_feature": "ewfd/csc1",
        "volume_loss_expression": "intLoss(ewfd.Qh)",
        "volume_loss_selection_ids": [2],
        "volume_loss_unit": "W",
        "incident_power_expression": "incident_flux",
        "incident_power_sign": -1,
    }


def test_point_audit_persists_declared_plane_flux_and_strict_policy_passes(tmp_path, monkeypatch):
    result, source = _run(
        tmp_path,
        monkeypatch,
        declared_plane_flux=_declared_plane_flux(),
        validation_policy=example_validation_policies()["declared_flux_closure"],
    )

    flux = result["measurement"]["declared_plane_flux"]
    assert result["audit_status"] == "policy_evaluated"
    assert result["assessment"]["project_verdict"] == "pass"
    assert flux["R"] == pytest.approx(0.2)
    assert flux["T"] == pytest.approx(0.6)
    assert flux["A"] == pytest.approx(0.2)
    evidence = result["physical_evidence"]["evidence"]
    assert evidence["flux.incident_raw_power_w"]["state"] == "measured"
    assert evidence["flux.incident_raw_power_w"]["value"] == pytest.approx(-2.0)
    assert evidence["flux.incident_power_w"]["state"] == "derived_from_declared_convention"
    assert evidence["flux.incident_power_w"]["value"] == pytest.approx(2.0)
    assert evidence["flux.physical_flux_closure_eligible"]["value"] is True
    manifest = json.loads(Path(result["artifacts"]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["measurement"]["declared_plane_flux"] == flux
    assert _hash(source) == result["measurement"]["provenance"]["source_sha256_after"]


def test_internal_absorption_agreement_is_persisted_but_not_flux_eligible(tmp_path, monkeypatch):
    result, _source = _run(
        tmp_path,
        monkeypatch,
        internal_absorption=_internal_absorption(),
    )

    comparison = result["measurement"]["internal_absorption_consistency"]
    assert comparison["state"] == "measured"
    assert comparison["relative_residual"] == pytest.approx(0.0)
    assert comparison["physical_flux_closure_eligible"] is False
    evidence = result["physical_evidence"]["evidence"]
    assert evidence["absorption.internal_relative_residual"]["value"] == pytest.approx(0.0)
    assert evidence["absorption.internal_consistency_closure_eligible"]["value"] is False
    assert evidence["flux.physical_flux_closure_eligible"]["state"] == "not_requested"


def test_declared_flux_expression_error_is_durable_partial_evidence(tmp_path, monkeypatch):
    declaration = _declared_plane_flux()
    declaration["reflected"]["expression"] = "missing_flux_expression"
    result, source = _run(
        tmp_path,
        monkeypatch,
        declared_plane_flux=declaration,
    )

    assert result["audit_status"] == "measurement_partial"
    assert result["measurement"]["declared_plane_flux"]["state"] == "unknown"
    assert any(
        item["code"] == "declared_plane_flux_unavailable"
        for item in result["measurement"]["measurement_errors"]
    )
    assert result["physical_evidence"]["evidence"]["flux.R"]["state"] == "unknown"
    manifest = json.loads(Path(result["artifacts"]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["measurement"]["declared_plane_flux"]["declaration"] == declaration
    assert _hash(source) == result["measurement"]["provenance"]["source_sha256_after"]


def test_evidence_only_a_above_one_is_preserved_without_project_verdict(tmp_path, monkeypatch):
    models = []
    result, _source = _run(tmp_path, monkeypatch, model_observer=models)

    assert result["audit_status"] == "measurement_complete"
    assert result["measurement"]["power"]["A"] == 1.2
    assert result["assessment"]["project_verdict"] is None
    assert result["measurement"]["polarization"]["evidence_level"] == "structure_total_field"
    assert result["measurement"]["polarization"]["structure_total_field"]["diagnostic_only"] is True
    manifest = json.loads(Path(result["artifacts"]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["audit_status"] == "measurement_complete"
    assert manifest["config_sha256"] == result["measurement"]["provenance"]["config_sha256"]
    assert manifest["preflight"]["inspection_status"] == "complete"
    assert manifest["physical_evidence"] == result["physical_evidence"]
    assert "migration" not in result["physical_evidence"]
    assert result["physical_evidence"]["producer"]["tool_schema_version"] == "physical-evidence-1"
    assert models[0].java._param.values["wl"] == "4.36[um]"
    assert models[0].java._study.step.values["plist"] == "original_plist"
    assert result["measurement"]["integrity"]["model_state_restored"] is True
    assert (
        result["physical_evidence"]["evidence"]["polarization.physical_incident"]["state"]
        == "label_only"
    )
    assert result["physical_evidence"]["evidence"]["polarization.structure_total_field"][
        "selection_ids"
    ] == ["topair", 3]
    assert result["physical_evidence"]["evidence"]["flux.closure_abs"]["state"] == "not_requested"


def test_strict_policy_uses_physical_evidence_and_cannot_promote_structure_total_field(
    tmp_path, monkeypatch
):
    policy = example_validation_policies()["reference_air_polarization_ratio"]
    result, _source = _run(
        tmp_path,
        monkeypatch,
        validation_policy=policy,
    )

    assert result["audit_status"] == "policy_evaluated"
    assert result["assessment"]["mode"] == "strict_physical_evidence_policy"
    assert result["assessment"]["project_verdict"] == "missing"
    rule = result["assessment"]["policy_evaluation"]["rules"][0]
    assert rule["required_measurement_states"] == {
        "polarization.reference_air_method_valid": "unknown",
        "polarization.target_to_transverse_ratio": "unknown",
    }


def test_nonfinite_power_is_an_integrity_blocker_and_is_journaled(tmp_path, monkeypatch):
    result, _source = _run(tmp_path, monkeypatch, nonfinite=True)

    assert result["audit_status"] == "integrity_blocked"
    assert result["measurement"]["integrity_errors"][0]["code"] == "nonfinite_power"
    assert Path(result["artifacts"]["csv"]).read_text(encoding="utf-8")


def test_source_hash_drift_after_solve_is_an_integrity_blocker(tmp_path, monkeypatch):
    result, source = _run(tmp_path, monkeypatch, drift_source=True)

    assert result["audit_status"] == "integrity_blocked"
    assert result["measurement"]["integrity"]["source_unchanged"] is False
    assert _hash(source) == result["measurement"]["provenance"]["source_sha256_after"]


def test_unhandled_point_audit_failure_terminalizes_running_manifest(tmp_path, monkeypatch):
    models = []
    monkeypatch.setattr(
        "src.tools.wave_optics_audit.build_point_audit_physical_evidence",
        lambda _value: (_ for _ in ()).throw(RuntimeError("injected contract failure")),
    )
    with pytest.raises(RuntimeError, match="contract failure"):
        _run(tmp_path, monkeypatch, model_observer=models)

    manifests = list((tmp_path / "artifacts").rglob("manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["audit_status"] == "runtime_failed"
    assert manifest["runtime_failure"] == {
        "code": "point_audit_runtime_failed",
        "error_type": "RuntimeError",
        "details_included": False,
    }
    assert models[0].java._param.values["wl"] == "4.36[um]"
    assert models[0].java._study.step.values["plist"] == "original_plist"


def test_point_audit_restoration_failure_is_an_integrity_blocker(tmp_path, monkeypatch):
    models = []

    def reject_restoration(model):
        original_set = model.java._study.step.set

        def reject_restore(name, value):
            if value == "original_plist":
                raise RuntimeError("injected restore failure")
            original_set(name, value)

        model.java._study.step.set = reject_restore

    result, _source = _run(
        tmp_path,
        monkeypatch,
        model_observer=models,
        model_setup=reject_restoration,
    )

    assert result["audit_status"] == "integrity_blocked"
    assert result["assessment"]["project_verdict"] is None
    assert result["measurement"]["integrity"]["model_state_restored"] is False
    assert result["measurement"]["integrity_errors"][-1]["code"] == "model_state_restore_failed"


class MaterialSelection:
    def __init__(self):
        self.values = []

    def set(self, values):
        self.values = list(values)

    def entities(self):
        return self.values


class MaterialGroup:
    def __init__(self):
        self.values = {}
        self.reads = []

    def set(self, name, value):
        self.values[name] = value

    def getString(self, name):
        self.reads.append(name)
        return self.values[name]


class Material:
    def __init__(self):
        self.group = MaterialGroup()
        self.selection_value = MaterialSelection()

    def propertyGroup(self, tag):
        assert tag == "def"
        return self.group

    def selection(self):
        return self.selection_value


class Materials:
    def __init__(self, tags):
        self.items = {tag: Material() for tag in tags}

    def tags(self):
        return list(self.items)

    def remove(self, tag):
        del self.items[tag]

    def create(self, tag, kind):
        assert kind == "Common"
        self.items[tag] = Material()
        return self.items[tag]


class MaterialComponent:
    def __init__(self, tags):
        self.materials = Materials(tags)

    def material(self):
        return self.materials


class MaterialJava:
    def __init__(self, tags):
        self.component_value = MaterialComponent(tags)

    def component(self, tag):
        assert tag == "comp1"
        return self.component_value


class MaterialClone:
    def __init__(self, tags):
        self.java = MaterialJava(tags)


def test_all_air_mutation_is_clone_only_and_requires_exact_material_tags():
    clone = MaterialClone(["mat2", "mat1"])

    result = _replace_clone_materials_with_air(
        clone,
        component_tag="comp1",
        expected_material_tags=["mat1", "mat2"],
        all_domain_ids=[1, 2, 3],
    )

    assert result["removed_material_tags"] == ["mat1", "mat2"]
    assert clone.java.component_value.materials.tags() == ["reference_air_material"]
    air = clone.java.component_value.materials.items["reference_air_material"]
    assert air.group.values == {
        "relpermittivity": "1",
        "relpermeability": "1",
        "electricconductivity": "0[S/m]",
    }
    assert air.group.reads == [
        "relpermittivity",
        "relpermeability",
        "electricconductivity",
    ]
    assert result["air_properties"] == air.group.values
    assert air.selection_value.values == [1, 2, 3]

    untouched = MaterialClone(["mat1"])
    with pytest.raises(ValueError, match="exact caller declaration"):
        _replace_clone_materials_with_air(
            untouched,
            component_tag="comp1",
            expected_material_tags=["different"],
            all_domain_ids=[1],
        )
    assert untouched.java.component_value.materials.tags() == ["mat1"]


def _run_reference(
    tmp_path,
    monkeypatch,
    *,
    material_error=False,
    cleanup_result=True,
    register_error=False,
    name_error=False,
    direct_remove_error=False,
    field_samples=(1.0, 0.0, 0.0),
):
    source = tmp_path / "reference_source.mph"
    source.write_bytes(b"immutable-reference")
    source_model = AuditModel(source, absorption=0.0)
    cleanup_calls = []

    def clone_factory(_source, _client, new_name):
        clone_path = tmp_path / f"{new_name}.mph"
        clone_path.write_bytes(b"derived-clone")
        clone = AuditModel(clone_path, name=new_name, absorption=0.0)
        original_evaluate = clone.evaluate

        def evaluate(expression):
            if isinstance(expression, list) and expression and expression[-1] == "dom":
                x, y, z = field_samples
                return [
                    np.asarray([x + 0j] * 3),
                    np.asarray([y + 0j] * 3),
                    np.asarray([z + 0j] * 3),
                    np.array([0.1, 0.2, 0.3]),
                    np.array([0.1, 0.2, 0.3]),
                    np.array([0.8, 0.85, 0.9]),
                    np.array([3, 3, 3]),
                ]
            return original_evaluate(expression)

        clone.evaluate = evaluate
        model_name = clone.name()
        if name_error:
            clone.name = lambda: (_ for _ in ()).throw(RuntimeError("injected clone-name failure"))
        return clone, {
            "derived_model_id": "derived-unit-reference",
            "model_name": model_name,
            "source_path": str(source),
            "source_sha256": _hash(source),
            "backing_path": str(clone_path),
            "backing_sha256": _hash(clone_path),
        }

    def material_mutator(_clone, **_kwargs):
        if material_error:
            raise ValueError("material readback mismatch")
        return {
            "method": "all_air_clone",
            "removed_material_tags": ["mat1"],
            "air_material_tag": "reference_air_material",
            "domain_ids": [1, 2, 3],
            "readback_complete": True,
        }

    def cleanup(name):
        cleanup_calls.append(name)
        return cleanup_result

    class Client:
        @staticmethod
        def remove(_clone):
            cleanup_calls.append("direct-client-remove")
            if direct_remove_error:
                raise RuntimeError("injected direct removal failure")

    def register(clone, _path):
        if register_error:
            raise RuntimeError("injected clone registration failure")
        return f"registered-{clone.name()}"

    monkeypatch.setattr(
        "src.tools.wave_optics_audit._validate_ascii_dir",
        lambda _value: tmp_path / "reference_artifacts",
    )
    result = run_wave_optics_reference_audit(
        source_model,
        Client(),
        model_name="AuditModel",
        component_tag="comp1",
        physics_tag="ewfd",
        study_tag="std1",
        study_step_tag="wl_step",
        study_step_property="plist",
        wavelength_value=4.37,
        wavelength_unit="um",
        wavelength_parameter="wl",
        expected_source_sha256=_hash(source),
        config_id="unit-reference",
        reference_method="all_air_clone",
        expected_material_tags=["mat1"],
        all_domain_ids=[1, 2, 3],
        top_air_domain_ids=[3],
        top_air_coordinate_range={"x": [0, 1], "y": [0, 1], "z": [0.7, 1]},
        target_axis="x",
        aggregation="rms_abs",
        artifact_dir=str(tmp_path / "ascii_reference"),
        validation_policy=example_validation_policies()["reference_air_polarization_ratio"],
        clone_factory=clone_factory,
        clone_register=register,
        clone_cleanup=cleanup,
        material_mutator=material_mutator,
        preflight_collector=lambda *_args, **_kwargs: {
            "inspection_status": "complete",
            "ports": {"features": ["port1", "port2"]},
            "incidence": {"alpha1_evaluated_deg": 0.0, "alpha2_evaluated_deg": 0.0},
        },
    )
    return result, source, cleanup_calls


def test_reference_audit_uses_fresh_clone_and_persists_dominant_component(tmp_path, monkeypatch):
    result, source, cleanup_calls = _run_reference(tmp_path, monkeypatch)

    assert result["audit_status"] == "measurement_complete"
    assert result["cleanup"] == {
        "attempted": True,
        "removed": True,
        "client_model_removed": True,
        "backing_file_removed": True,
    }
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0].startswith("registered-reference_air_clone_")
    assert result["reference"]["method"] == "all_air_clone"
    assert result["reference"]["component_amplitudes"]["x"] == pytest.approx(1.0)
    assert result["reference"]["target_to_transverse_ratio"] > 1e100
    assert result["assessment"]["overall"] == "pass"
    evidence = result["physical_evidence"]["evidence"]
    assert evidence["polarization.reference_air_method_valid"]["value"] is True
    assert evidence["integrity.clone_cleanup_proved"]["value"] is True
    manifest = json.loads(Path(result["artifacts"]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["clone_provenance"]["source_sha256"] == _hash(source)
    assert manifest["source_unchanged"] is True


def test_reference_audit_rejects_overflowed_ratio_and_publishes_terminal_manifest(
    tmp_path, monkeypatch
):
    result, _source, _cleanup_calls = _run_reference(
        tmp_path, monkeypatch, field_samples=(1e308, 1e-308, 0.0)
    )
    assert result["audit_status"] == "integrity_blocked"
    assert result["reference"] is None
    manifest = json.loads(Path(result["artifacts"]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["audit_status"] == "integrity_blocked"
    assert manifest["integrity_errors"][0]["code"] == "nonfinite_reference_evidence"


def test_reference_audit_cleans_clone_after_material_error(tmp_path, monkeypatch):
    result, source, cleanup_calls = _run_reference(tmp_path, monkeypatch, material_error=True)

    assert result["audit_status"] == "measurement_partial"
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0].startswith("registered-reference_air_clone_")
    assert result["cleanup"]["removed"] is True
    assert (
        result["physical_evidence"]["evidence"]["polarization.reference_air_method_valid"]["value"]
        is False
    )
    manifest = json.loads(Path(result["artifacts"]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["measurement_errors"][0]["code"] == "reference_audit_failed"
    assert manifest["source_sha256_before"] == manifest["source_sha256_after"] == _hash(source)


def test_reference_audit_cleans_live_clone_after_registration_failure(tmp_path, monkeypatch):
    result, _source, cleanup_calls = _run_reference(
        tmp_path,
        monkeypatch,
        register_error=True,
    )

    assert len(cleanup_calls) == 1
    assert cleanup_calls[0].startswith("reference_air_clone_")
    assert result["audit_status"] == "measurement_partial"
    assert result["cleanup"]["removed"] is True
    assert result["cleanup"]["backing_file_removed"] is True


def test_reference_audit_falls_back_to_exact_clone_after_name_failure(tmp_path, monkeypatch):
    result, _source, cleanup_calls = _run_reference(
        tmp_path,
        monkeypatch,
        name_error=True,
    )

    assert cleanup_calls == ["direct-client-remove"]
    assert result["audit_status"] == "measurement_partial"
    assert result["cleanup"]["client_model_removed"] is True
    assert result["cleanup"]["backing_file_removed"] is True
    assert result["cleanup"]["removed"] is True


def test_reference_audit_treats_backing_file_cleanup_failure_as_integrity_blocker(
    tmp_path, monkeypatch
):
    real_unlink = Path.unlink

    def fail_clone_unlink(path, *args, **kwargs):
        if path.name.startswith("reference_air_clone_"):
            raise PermissionError("injected backing-file cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_clone_unlink)

    result, _source, cleanup_calls = _run_reference(tmp_path, monkeypatch)

    assert len(cleanup_calls) == 1
    assert cleanup_calls[0].startswith("registered-reference_air_clone_")
    assert result["audit_status"] == "integrity_blocked"
    assert result["cleanup"]["client_model_removed"] is True
    assert result["cleanup"]["backing_file_removed"] is False
    assert result["cleanup"]["removed"] is False
    manifest = json.loads(Path(result["artifacts"]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["integrity_errors"][0]["code"] == "reference_clone_cleanup_unproved"


def test_reference_audit_refuses_terminal_success_when_cleanup_is_unproved(tmp_path, monkeypatch):
    result, _source, cleanup_calls = _run_reference(
        tmp_path,
        monkeypatch,
        cleanup_result=False,
        direct_remove_error=True,
    )

    assert len(cleanup_calls) == 2
    assert cleanup_calls[0].startswith("registered-reference_air_clone_")
    assert cleanup_calls[1] == "direct-client-remove"
    assert result["audit_status"] == "integrity_blocked"
    assert result["cleanup"]["removed"] is False
    manifest = json.loads(Path(result["artifacts"]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["integrity_errors"][0]["code"] == "reference_clone_cleanup_unproved"


def test_required_volume_loss_evidence_needs_a_usable_value(tmp_path, monkeypatch):
    """C081R-01 / J-02: required volume-loss evidence must not pass on
    error-only loss records. An ordinary unavailable integration expression
    records the failed attempt, but it is MISSING evidence - distinct from a
    physical failure - so the rule, the project verdict, and the published
    manifest must not claim success."""
    import json as _json

    def _fail_int_au(model):
        original = model.evaluate

        def evaluate(expression):
            if expression == "intAu(ewfd.Qh)":
                raise RuntimeError("Unknown integration operator intAu")
            return original(expression)

        model.evaluate = evaluate

    # ALL declared loss expressions fail: the evidence is missing.
    result, _ = _run(
        tmp_path,
        monkeypatch,
        validation_policy={"required_evidence": ["volume_loss"]},
        model_setup=_fail_int_au,
    )
    rule = result["assessment"]["policy_evaluation"]["rules"][0]
    assert rule["rule"] == "required_evidence.volume_loss"
    assert rule["outcome"] == "missing"
    assert result["assessment"]["project_verdict"] == "missing"
    assert result["audit_status"] == "policy_evaluated"
    items = result["measurement"]["losses"]["items"]
    assert len(items) == 1 and "value" not in items[0] and items[0]["error"]
    assert any(
        error["code"] == "loss_expression_unavailable"
        for error in result["measurement"]["measurement_errors"]
    )
    manifest = _json.loads(Path(result["artifacts"]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["assessment"]["project_verdict"] == "missing"

    # MIXED success/error: the usable record makes the evidence available,
    # and the failed record stays as recorded evidence of the attempt.
    from src.tools.wave_optics_audit import evaluate_validation_policy

    mixed = evaluate_validation_policy(
        {
            "losses": {
                "items": [
                    {"label": "Au", "expression": "intAu(ewfd.Qh)", "error": "boom"},
                    {"label": "Loss", "expression": "intLoss(ewfd.Qh)", "value": 0.4},
                ]
            }
        },
        {"required_evidence": ["volume_loss"]},
    )
    mixed_rule = mixed["rules"][0]
    assert mixed_rule["rule"] == "required_evidence.volume_loss"
    assert mixed_rule["outcome"] == "pass"
    assert mixed["overall"] == "pass"

    # Valid control: unchanged pass with the raw value (no normalization
    # was requested, so availability must not demand one).
    result_ok, _ = _run(
        tmp_path,
        monkeypatch,
        validation_policy={"required_evidence": ["volume_loss"]},
    )
    assert result_ok["assessment"]["policy_evaluation"]["rules"][0]["outcome"] == "pass"
    assert result_ok["assessment"]["project_verdict"] == "pass"
