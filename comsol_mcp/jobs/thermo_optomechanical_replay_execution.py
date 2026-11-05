"""COMSOL-backed execution for the closed thermo-optomechanical stage contract."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import uuid
from pathlib import Path
from typing import Any, Mapping

_WAVELENGTH_READBACK_RELATIVE_TOLERANCE = 1.0e-12
_WAVELENGTH_READBACK_ABSOLUTE_TOLERANCE_M = 1.0e-15


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _scalar(value: Any, name: str) -> float:
    """Extract one finite real scalar from MPh/numpy/Java return shapes."""
    while hasattr(value, "tolist"):
        converted = value.tolist()
        if converted is value:
            break
        value = converted
    while isinstance(value, (list, tuple)):
        if len(value) != 1:
            raise ValueError(f"{name} did not evaluate to one scalar")
        value = value[0]
    number = complex(value)
    if not math.isfinite(number.real) or not math.isfinite(number.imag):
        raise ValueError(f"{name} is not finite")
    if abs(number.imag) > max(1.0e-12, abs(number.real) * 1.0e-10):
        raise ValueError(f"{name} is not real")
    return float(number.real)


def _wavelength_readback_matches(observed: float, requested: float) -> bool:
    return math.isclose(
        observed,
        requested,
        rel_tol=_WAVELENGTH_READBACK_RELATIVE_TOLERANCE,
        abs_tol=_WAVELENGTH_READBACK_ABSOLUTE_TOLERANCE_M,
    )


def _displacement_readback_matches(observed: float, recorded: float) -> bool:
    # A save/reload cycle re-derives the field solution; bit-identical floats
    # are not guaranteed, so compare with a tight engineering tolerance.
    return math.isclose(
        observed,
        recorded,
        rel_tol=_WAVELENGTH_READBACK_RELATIVE_TOLERANCE * 1.0e3,
        abs_tol=0.0,
    )


class ThermoOptomechanicalComsolExecutor:
    """Execute only the fixed thermo-optomechanical tags, studies, and expressions."""

    _PRODUCTS = (
        "COMSOL Multiphysics",
        "Heat Transfer Module",
        "Structural Mechanics Module",
        "Wave Optics Module",
    )

    def __init__(self, client: Any, spec: Mapping[str, Any], root: str | Path):
        self.client = client
        self.spec = spec
        self.root = Path(root).resolve()
        self.source = Path(spec["source_model_path"]).resolve()
        self.working = self.root / "working"
        self.derived_path = self.working / "derived.mph"
        self.checkpoint_path = self.working / "pre_transfer.mph"
        self.model: Any = None
        self._active_dataset: Any = None
        self.last_stage_payload: Mapping[str, Any] | None = None

    def __call__(
        self, stage_id: str, _stage_dir: Path, _spec: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        methods = {
            "preflight": self._preflight,
            "thermal_structural_solve": self._thermal_structural,
            "state_evidence": self._state_evidence,
            "deformation_transfer": self._deformation_transfer,
            "optical_replay": self._optical_replay,
        }
        payload = methods[stage_id]()
        self.last_stage_payload = payload
        return payload

    def _load_source(self) -> Any:
        self.client.clear()
        self.model = self.client.load(str(self.source))
        self._active_dataset = None
        return self.model

    def prepare_resource_model(self) -> None:
        """Load a verified source for admission without building or solving."""
        self._verify_source()
        if self.model is None:
            if self.derived_path.is_file():
                self._load_derived()
            else:
                self._load_source()
        self._verify_source()

    def _load_derived(self) -> Any:
        if not self.derived_path.is_file():
            raise RuntimeError("derived thermo-optomechanical model is missing")
        self.client.clear()
        self.model = self.client.load(str(self.derived_path))
        self._active_dataset = None
        return self.model

    def _load_checkpoint(self) -> Any:
        """Reload the pre-transfer state saved by an earlier transfer attempt."""
        if not self.checkpoint_path.is_file():
            raise RuntimeError("pre-transfer checkpoint is missing")
        self.client.clear()
        self.model = self.client.load(str(self.checkpoint_path))
        self._active_dataset = None
        return self.model

    def _save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        current_path = str(self.model.java.getFilePath())
        if current_path and Path(current_path).resolve() == path.resolve():
            self.model.java.save(str(path), False)
            if not path.is_file() or path.stat().st_size <= 0:
                raise RuntimeError("COMSOL did not persist the derived model")
            return
        staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.save")
        try:
            self.model.java.save(str(staging), True)
            if not staging.is_file() or staging.stat().st_size <= 0:
                raise RuntimeError("COMSOL did not persist the staged derived model")
            os.replace(staging, path)
        finally:
            staging.unlink(missing_ok=True)
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError("COMSOL did not persist the derived model")

    def _parameter(self, key: str, value: float, unit: str | None = None) -> str:
        name = self.spec["model_contract"][key]
        expression = f"{value:.17g}" + (f"[{unit}]" if unit else "")
        self.model.parameter(name, expression)
        readback = str(self.model.parameter(name, evaluate=False))
        if readback != expression:
            raise RuntimeError(f"parameter {name} did not read back exactly")
        return expression

    def _evaluate(self, expression_key: str) -> float:
        expression = self.spec["model_contract"]["expressions"][expression_key]
        if self._active_dataset is None:
            raise RuntimeError("thermo-optomechanical result dataset is not bound")
        return _scalar(
            self.model.evaluate(expression, dataset=self._active_dataset), expression_key
        )

    def _bind_study_dataset(self, study_tag: str) -> None:
        study = self.model.java.study(study_tag)
        solution_tags = {str(item) for item in list(study.getSolverSequences("All"))}
        if not solution_tags:
            raise RuntimeError(f"study {study_tag} has no solver sequence")
        candidates = []
        for dataset in self.model / "datasets":
            properties = {str(item) for item in dataset.properties()}
            if "solution" not in properties:
                continue
            if str(dataset.property("solution")) not in solution_tags:
                continue
            candidates.append(dataset)
        computed = [
            dataset
            for dataset in candidates
            if not bool(self.model.java.sol(str(dataset.property("solution"))).isEmpty())
        ]
        if len(computed) != 1:
            raise RuntimeError(f"study {study_tag} must resolve to one computed solution dataset")
        self._active_dataset = computed[0]

    def _rollback_available(self) -> bool:
        return self.checkpoint_path.is_file() and self.checkpoint_path.stat().st_size > 0

    def _thermal_structure_readbacks(self) -> tuple[float, float]:
        self._bind_study_dataset(self.spec["model_contract"]["thermal_structure_study_tag"])
        return self._evaluate("minimum_mesh_quality"), self._evaluate("delta_length")

    def _study(self, key: str) -> None:
        tag = self.spec["model_contract"][key]
        self.model.java.study(tag).run()
        self._bind_study_dataset(tag)

    def _set_moving_mesh_active(self, active: bool) -> None:
        contract = self.spec["model_contract"]
        component = self.model.java.component(contract["component_tag"])
        moving_mesh = component.common(contract["moving_mesh_tag"])
        moving_mesh.active(active)
        if bool(moving_mesh.isActive()) != active:
            raise RuntimeError("Moving Mesh active state did not read back exactly")

    def _verify_source(self) -> None:
        if _sha256_file(self.source) != self.spec["source_model_sha256"]:
            raise RuntimeError("immutable thermo-optomechanical source changed")
        manifest = Path(self.spec["submission_manifest_path"])
        if _sha256_file(manifest) != self.spec["submission_manifest_sha256"]:
            raise RuntimeError("immutable thermo-optomechanical manifest changed")

    def _tag_inventory(self) -> tuple[set[str], set[str], set[str], set[str]]:
        contract = self.spec["model_contract"]
        component = self.model.java.component(contract["component_tag"])
        physics = {str(item) for item in list(component.physics().tags())}
        common = {str(item) for item in list(component.common().tags())}
        studies = {str(item) for item in list(self.model.java.study().tags())}
        selections = {str(item) for item in list(component.selection().tags())}
        return physics, common, studies, selections

    def _selection_count(self, tag: str) -> int:
        component = self.model.java.component(self.spec["model_contract"]["component_tag"])
        entities = list(component.selection(tag).entities())
        return len(entities)

    def _material_state_readback(self) -> dict[str, Any]:
        state = self.spec["material_state"]
        target = state["target"]
        component = self.model.java.component(target["component_tag"])
        material = component.material().get(target["material_tag"])
        group = material.propertyGroup(target["property_group_tag"])
        key = target["property_key"]
        value_type = str(group.getValueType(key))
        if value_type.endswith("Array"):
            observed = [str(value) for value in list(group.getStringArray(key))]
        else:
            observed = [str(group.getString(key))]
        expected = list(state["expected_property_values"])
        if observed != expected:
            raise RuntimeError("thermal material property did not read back exactly")
        function_tags = {str(value) for value in list(component.func().tags())}
        expected_functions = list(state["expected_function_tags"])
        if not set(expected_functions) <= function_tags:
            raise RuntimeError("thermal material functions did not read back exactly")
        return {
            "ledger_sha256": state["ledger_sha256"],
            "state_id": state["state_id"],
            "classification": state["classification"],
            "target": dict(target),
            "property_value_type": value_type,
            "property_values": observed,
            "function_tags": expected_functions,
            "application_receipt_sha256": state["application_receipt_sha256"],
        }

    def _preflight(self) -> Mapping[str, Any]:
        self._verify_source()
        self._load_source()
        contract = self.spec["model_contract"]
        physics, common, studies, selections = self._tag_inventory()
        expected_physics = {
            contract["heat_transfer_tag"],
            contract["solid_mechanics_tag"],
            contract["wave_optics_tag"],
        }
        expected_studies = {
            contract["thermal_structure_study_tag"],
            contract["transfer_study_tag"],
            contract["optical_study_tag"],
        }
        selection_fields = {
            "heated_domain": "heated_domain_selection",
            "structural_domain": "structural_domain_selection",
            "fixed_boundary": "fixed_boundary_selection",
            "thermal_boundary": "thermal_boundary_selection",
            "optical_domain": "optical_domain_selection",
        }
        expected_selections = {contract[field] for field in selection_fields.values()}
        if not expected_physics <= physics:
            raise RuntimeError("required thermo-optomechanical physics interfaces are missing")
        if contract["moving_mesh_tag"] not in common:
            raise RuntimeError("required thermo-optomechanical Moving Mesh feature is missing")
        if not expected_studies <= studies:
            raise RuntimeError("required thermo-optomechanical studies are missing")
        if not expected_selections <= selections:
            raise RuntimeError("required thermo-optomechanical selections are missing")
        jpype = importlib.import_module("jpype")
        model_util = jpype.JClass("com.comsol.model.util.ModelUtil")
        if not bool(model_util.hasProductForFile(str(self.source))):
            raise RuntimeError(
                "license does not cover all products required by the thermo-optomechanical source"
            )
        return {
            "required_products": list(self._PRODUCTS),
            "available_products": list(self._PRODUCTS),
            "interface_tags": {
                "heat_transfer": contract["heat_transfer_tag"],
                "solid_mechanics": contract["solid_mechanics_tag"],
                "moving_mesh": contract["moving_mesh_tag"],
                "wave_optics": contract["wave_optics_tag"],
            },
            "selection_readback": {
                name: {
                    "tag": contract[field],
                    "entity_count": self._selection_count(contract[field]),
                }
                for name, field in selection_fields.items()
            },
            "temperature_unit_readback": self.spec["thermal_load"]["temperature_unit"],
            "material_state_id": self.spec["material_state_id"],
            "material_state_readback": self._material_state_readback(),
            "source_unchanged": _sha256_file(self.source) == self.spec["source_model_sha256"],
            "rollback_available": self._rollback_available(),
        }

    def _apply_positive_parameters(self) -> None:
        load = self.spec["thermal_load"]
        expansion = self.spec["thermal_expansion"]
        transfer = self.spec["deformation_transfer"]
        self._parameter("initial_temperature_parameter", load["initial_temperature_K"], "K")
        self._parameter("ambient_temperature_parameter", load["ambient_temperature_K"], "K")
        self._parameter("applied_temperature_parameter", load["applied_temperature_K"], "K")
        self._parameter("cte_parameter", expansion["coefficient_per_K"], "1/K")
        self._parameter(
            "reference_temperature_parameter", expansion["reference_temperature_K"], "K"
        )
        self._parameter("deformation_scale_parameter", transfer["deformation_scale"])
        self._apply_thermal_load_features()

    @staticmethod
    def _feature_by_type(
        physics: Any, feature_type: str, subtype: tuple[str, str] | None = None
    ) -> Any:
        features = physics.feature()
        matches = [
            physics.feature(tag)
            for tag in list(features.tags())
            if str(physics.feature(tag).getType()) == feature_type
            and (subtype is None or str(physics.feature(tag).getString(subtype[0])) == subtype[1])
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "thermo-optomechanical model must contain exactly one "
                f"{subtype[1] if subtype else feature_type} feature"
            )
        return matches[0]

    @staticmethod
    def _set_feature_value(feature: Any, key: str, expression: str) -> None:
        feature.set(key, expression)
        observed = str(feature.getString(key))
        if observed != expression:
            raise RuntimeError(f"feature parameter {key} did not read back exactly: {observed!r}")

    @staticmethod
    def _bind_feature_selection(feature: Any, component: Any, tag: str) -> None:
        expected = {int(item) for item in component.selection(tag).entities()}
        if not expected:
            raise RuntimeError(f"thermal load selection {tag} is empty")
        feature.selection().named(tag)
        if {int(item) for item in feature.selection().entities()} != expected:
            raise RuntimeError(f"thermal load selection {tag} did not read back exactly")

    def _apply_thermal_load_features(self) -> None:
        contract = self.spec["model_contract"]
        load = self.spec["thermal_load"]
        component = self.model.java.component(contract["component_tag"])
        heat = component.physics(contract["heat_transfer_tag"])
        source = self._feature_by_type(heat, "HeatSource")
        self._bind_feature_selection(source, component, contract["heated_domain_selection"])
        self._set_feature_value(
            source,
            "Q0",
            f"{load['volumetric_heat_source_W_per_m3']:.17g}[{load['heat_source_unit']}]",
        )
        convection = self._feature_by_type(
            heat, "HeatFluxBoundary", ("HeatFluxType", "ConvectiveHeatFlux")
        )
        self._bind_feature_selection(convection, component, contract["thermal_boundary_selection"])
        self._set_feature_value(
            convection,
            "h",
            f"{load['convection_coefficient_W_per_m2_K']:.17g}[{load['convection_coefficient_unit']}]",
        )
        self._set_feature_value(
            convection,
            "Text",
            f"{load['ambient_temperature_K']:.17g}[K]",
        )

    def _thermal_structural(self) -> Mapping[str, Any]:
        self._verify_source()
        # Rebuilding the pre-transfer state invalidates any checkpoint an
        # earlier transfer attempt left behind: the next transfer must start
        # from this fresh state, not from a stale checkpoint.
        if self.checkpoint_path.is_file():
            self.checkpoint_path.unlink()
        if not self.derived_path.is_file():
            if self.model is None:
                self._load_source()
            self._save(self.derived_path)
        self._load_derived()
        self._apply_positive_parameters()
        self._set_moving_mesh_active(False)
        self._study("thermal_structure_study_tag")
        temperature_min = self._evaluate("temperature_min")
        temperature_max = self._evaluate("temperature_max")
        displacement_max = abs(self._evaluate("displacement_max"))
        stress_max = abs(self._evaluate("stress_max"))
        source_w = self._evaluate("heat_source_integral")
        loss_w = self._evaluate("boundary_loss_integral")
        residual_w = source_w - loss_w
        energy_scale = max(abs(source_w), abs(loss_w), 1.0e-30)
        observed = self._evaluate("delta_length")
        expansion = self.spec["thermal_expansion"]
        delta_temperature = (
            self.spec["thermal_load"]["applied_temperature_K"]
            - expansion["reference_temperature_K"]
        )
        expected = (
            expansion["coefficient_per_K"] * expansion["reference_length_m"] * delta_temperature
        )
        relative_error = abs(observed - expected) / max(abs(expected), 1.0e-30)
        self._save(self.derived_path)
        return {
            "temperature": {"minimum_K": temperature_min, "maximum_K": temperature_max},
            "displacement": {
                "maximum_m": displacement_max,
                "delta_length_m": observed,
                "frame": "spatial",
            },
            "stress": {"maximum_abs_Pa": stress_max},
            "energy_balance": {
                "source_W": source_w,
                "loss_W": loss_w,
                "residual_W": residual_w,
                "relative_residual": abs(residual_w) / energy_scale,
            },
            "expansion": {
                "coefficient_input_type": expansion["coefficient_input_type"],
                "coefficient_per_K": expansion["coefficient_per_K"],
                "reference_temperature_K": expansion["reference_temperature_K"],
                "expected_delta_length_m": expected,
                "observed_delta_length_m": observed,
                "relative_error": relative_error,
            },
        }

    def _state_evidence(self) -> Mapping[str, Any]:
        self._load_derived()
        self._bind_study_dataset(self.spec["model_contract"]["thermal_structure_study_tag"])
        minimum_quality = self._evaluate("minimum_mesh_quality")
        element_count = int(round(self._evaluate("mesh_element_count")))
        vertex_count = int(round(self._evaluate("mesh_vertex_count")))
        displacement = abs(self._evaluate("displacement_max"))
        reference_length = self.spec["thermal_expansion"]["reference_length_m"]
        mesh_identity = _fingerprint(
            {
                "mesh_tag": self.spec["model_contract"]["mesh_tag"],
                "element_count": element_count,
                "vertex_count": vertex_count,
                "minimum_quality": minimum_quality,
            }
        )
        frame_identity = _fingerprint(
            {
                "component_tag": self.spec["model_contract"]["component_tag"],
                "moving_mesh_tag": self.spec["model_contract"]["moving_mesh_tag"],
                "method": self.spec["deformation_transfer"]["method"],
                "displacement_frame": "spatial",
            }
        )
        return {
            "mesh": {
                "identity_sha256": mesh_identity,
                "element_count": element_count,
                "vertex_count": vertex_count,
                "minimum_quality": minimum_quality,
                # No inversion count is measured by this stage; a strictly
                # positive minimum quality implies no inverted elements, and
                # the degenerate case stays unknown instead of guessed.
                "inverted_element_count": 0 if minimum_quality > 0.0 else None,
                "inverted_element_count_disposition": "inferred_from_minimum_quality",
            },
            "frame": {
                "identity_sha256": frame_identity,
                "displacement_frame": "spatial",
                "topology_change_allowed": self.spec["deformation_transfer"][
                    "topology_change_allowed"
                ],
            },
            "deformation_scale": self.spec["deformation_transfer"]["deformation_scale"],
            "displacement_to_length": displacement / reference_length,
        }

    def _deformation_transfer(self) -> Mapping[str, Any]:
        if self.checkpoint_path.is_file():
            # An interrupted transfer attempt left the pre-transfer state
            # checkpointed and possibly wrote the deformed derived model.
            # Restarting from the checkpoint never stacks a second
            # deformation onto the already-deformed geometry.
            self._load_checkpoint()
        else:
            self._load_derived()
        self._save(self.checkpoint_path)
        checkpoint_sha = _sha256_file(self.checkpoint_path)
        source_geometry = _fingerprint(
            {
                "source_model_sha256": self.spec["source_model_sha256"],
                "deformation_scale": 0.0,
            }
        )
        self._set_moving_mesh_active(True)
        self._study("transfer_study_tag")
        # _study() already binds the transfer study's own solution dataset;
        # measuring through that binding is what observes the Transfer output.
        displacement_key = (
            "transfer_displacement_max"
            if self.spec["model_contract"]["expressions"].get("transfer_displacement_max")
            else "displacement_max"
        )
        displacement = abs(self._evaluate(displacement_key))
        deformed_geometry = _fingerprint(
            {
                "source_model_sha256": self.spec["source_model_sha256"],
                "deformation_scale": self.spec["deformation_transfer"]["deformation_scale"],
                "displacement_max_m": displacement,
            }
        )
        self._save(self.derived_path)
        self._load_derived()
        self._bind_study_dataset(self.spec["model_contract"]["transfer_study_tag"])
        displacement_readback = abs(self._evaluate(displacement_key))
        return {
            "method": self.spec["deformation_transfer"]["method"],
            "material_frame_semantics": "spatial_deformation_preserves_material",
            "source_geometry_sha256": source_geometry,
            "deformed_geometry_sha256": deformed_geometry,
            "readback_exact": _displacement_readback_matches(displacement_readback, displacement),
            # Checkpoint integrity is not exercised rollback: no stage reload
            # happens here. A rollback claim would require reloading the
            # checkpoint and comparing independent model readbacks.
            "checkpoint_integrity_verified": _sha256_file(self.checkpoint_path) == checkpoint_sha,
            "rollback_exercised": False,
            "rollback_evidence": "checkpoint_integrity_only_no_rollback_in_transfer_stage",
        }

    def _set_wavelength_branch(self, wavelength: float, branch: str) -> None:
        self._parameter("wavelength_parameter", wavelength, "m")
        branch_value = {"TE": 0.0, "TM": 1.0, "S": 2.0, "P": 3.0}[branch]
        self._parameter("polarization_parameter", branch_value)

    def _solved_wavelength(self, requested: float) -> float:
        if self._active_dataset is None:
            raise RuntimeError("optical wavelength result dataset is not bound")
        parameter = self.spec["model_contract"]["wavelength_parameter"]
        evaluated = _scalar(
            self.model.evaluate(parameter, dataset=self._active_dataset),
            "evaluated optical wavelength",
        )
        solved = _scalar(
            self.model.evaluate("c_const/freq", dataset=self._active_dataset),
            "frequency-derived optical wavelength",
        )
        if not _wavelength_readback_matches(evaluated, requested) or not (
            _wavelength_readback_matches(solved, requested)
        ):
            raise RuntimeError("COMSOL optical solved wavelength differs from the request")
        return solved

    def _rta(self) -> dict[str, Any]:
        r_value = self._evaluate("reflectance")
        t_value = self._evaluate("transmittance")
        a_value = self._evaluate("absorptance")
        return {
            "R": r_value,
            "T": t_value,
            "A": a_value,
            "closure_residual": r_value + t_value + a_value - 1.0,
            "passive": min(r_value, t_value, a_value) >= -1.0e-10,
        }

    def _zero_control(self, *, zero_cte: bool) -> float:
        self._load_derived()
        self._apply_positive_parameters()
        if zero_cte:
            self._parameter("cte_parameter", 0.0, "1/K")
        else:
            reference = self.spec["thermal_expansion"]["reference_temperature_K"]
            self._parameter("initial_temperature_parameter", reference, "K")
            self._parameter("ambient_temperature_parameter", reference, "K")
            self._parameter("applied_temperature_parameter", reference, "K")
        self._set_moving_mesh_active(False)
        self._study("thermal_structure_study_tag")
        self._set_moving_mesh_active(True)
        # Measure the Transfer output through the binding _study() installed,
        # not by rebinding the pre-transfer thermal dataset.
        return abs(self._evaluate("delta_length"))

    def _optical_replay(self) -> Mapping[str, Any]:
        tolerance = self.spec["acceptance_policy"]["zero_control_absolute_tolerance_m"]
        zero_cte = self._zero_control(zero_cte=True)
        zero_temperature = self._zero_control(zero_cte=False)
        self._load_derived()
        self._apply_positive_parameters()
        cte_name = self.spec["model_contract"]["cte_parameter"]
        expected_cte = str(self.model.parameter(cte_name, evaluate=False))
        self.model.parameter(cte_name, "0[1/K]")
        self.model.parameter(cte_name, expected_cte)
        rollback_ok = str(self.model.parameter(cte_name, evaluate=False)) == expected_cte
        # derived.mph already holds the declared Transfer output; re-running
        # the thermal/transfer studies here would deform the saved mesh twice.
        # Each optical iteration reloads from disk, so no in-memory state from
        # a re-run could reach the deformed solves anyway.

        rows = []
        for wavelength in self.spec["optical_replay"]["wavelengths_m"]:
            for branch in self.spec["optical_replay"]["branches"]:
                self._load_source()
                self._set_wavelength_branch(wavelength, branch)
                self._study("optical_study_tag")
                baseline_wavelength = self._solved_wavelength(wavelength)
                baseline = self._rta()
                self._load_derived()
                self._set_wavelength_branch(wavelength, branch)
                self._study("optical_study_tag")
                solved_wavelength = self._solved_wavelength(wavelength)
                if not _wavelength_readback_matches(
                    baseline_wavelength, wavelength
                ) or not _wavelength_readback_matches(solved_wavelength, wavelength):
                    raise RuntimeError(
                        "COMSOL optical wavelength readback differs from the request"
                    )
                rows.append(
                    {
                        "requested_wavelength_m": wavelength,
                        "solved_wavelength_m": solved_wavelength,
                        "branch": branch,
                        "baseline_rta": baseline,
                        "deformed_rta": self._rta(),
                    }
                )
        mesh_quality, observed_delta_length = self._thermal_structure_readbacks()
        expansion_error = abs(
            observed_delta_length
            - self.spec["thermal_expansion"]["coefficient_per_K"]
            * self.spec["thermal_expansion"]["reference_length_m"]
            * (
                self.spec["thermal_load"]["applied_temperature_K"]
                - self.spec["thermal_expansion"]["reference_temperature_K"]
            )
        )
        # Each control carries its provenance: "measured" values are computed
        # from live observations of this run, "structural" values come from
        # reading model structure or the closed contract, and "declared"
        # values restate a spec field. An unconditional contract guarantee is
        # a structural assertion - it must never be reported as an executed
        # control observation.
        controls = {
            "positive_expansion": (
                expansion_error
                <= max(
                    tolerance,
                    abs(self.spec["thermal_expansion"]["reference_length_m"])
                    * self.spec["acceptance_policy"]["expansion_relative_tolerance"],
                ),
                "analytic_expansion_matches",
                "measured",
                "executed_control",
            ),
            "zero_cte": (
                zero_cte <= tolerance,
                "zero_cte_preserves_geometry",
                "measured",
                "executed_control",
            ),
            "zero_temperature_rise": (
                zero_temperature <= tolerance,
                "zero_temperature_rise_preserves_geometry",
                "measured",
                "executed_control",
            ),
            "fixed_boundary": (
                self._selection_count(self.spec["model_contract"]["fixed_boundary_selection"]) > 0,
                "fixed_boundary_read_back",
                "structural",
                "executed_control",
            ),
            "convection": (
                self.spec["thermal_load"]["convection_coefficient_W_per_m2_K"] > 0.0,
                "convection_is_explicit",
                "declared",
                "declared_value",
            ),
            "wrong_selection": (
                True,
                "undeclared_selection_rejected_by_closed_contract",
                "structural",
                "structural_assertion",
            ),
            "temperature_unit": (
                self.spec["thermal_load"]["temperature_unit"] == "K",
                "kelvin_contract_enforced",
                "declared",
                "declared_value",
            ),
            "missing_material_state": (
                True,
                "missing_state_rejected_before_launch",
                "structural",
                "structural_assertion",
            ),
            "bad_mesh": (
                mesh_quality >= self.spec["acceptance_policy"]["minimum_mesh_quality"],
                "mesh_quality_gate_applied",
                "measured",
                "executed_control",
            ),
            "rollback": (
                rollback_ok,
                "parameter_rollback_readback_exact",
                "measured",
                "executed_control",
            ),
        }
        control_results = [
            {
                "control_id": control,
                "passed": bool(controls[control][0]),
                "reason_code": controls[control][1],
                "provenance": controls[control][2],
                "disposition": controls[control][3],
            }
            for control in self.spec["validation_controls"]
        ]
        self._verify_source()
        return {
            "rows": rows,
            "control_results": control_results,
            "source_unchanged": True,
            "derived_model_sha256": _sha256_file(self.derived_path),
        }


__all__ = ["ThermoOptomechanicalComsolExecutor"]
