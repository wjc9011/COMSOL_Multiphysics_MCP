"""Mock gates for threshold-free, read-only Wave Optics preflight evidence."""

from __future__ import annotations

import hashlib

import pytest
from src.tools.wave_optics_preflight import (
    MAX_BOUNDARIES,
    EvidenceLedger,
    _extend_boundary_map,
    _point_audit_next_call,
    collect_preflight_foundation,
    collect_wave_optics_preflight,
)


def test_partial_preflight_never_authorizes_point_audit_from_truthy_skeletons():
    result = _point_audit_next_call(
        active_profile="wave_optics",
        inspection_status="partial",
        missing_evidence=[],
    )

    assert result["available"] is False


class MetadataOnlyModel:
    def __init__(self, path):
        self._path = path

    def file(self):
        return str(self._path)

    def name(self):
        return "LoadedModel"

    def version(self):
        return "6.4.0.293"

    @property
    def java(self):
        raise AssertionError("foundation collector must not touch clientapi")


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_evidence_ledger_has_stable_status_precedence():
    ledger = EvidenceLedger()
    assert ledger.inspection_status == "complete"
    ledger.add("integrity_error", "early_integrity_code", "blocked")
    assert ledger.inspection_status == "integrity_blocked"
    ledger.add("warning", "warning_code", "warning")
    assert ledger.inspection_status == "integrity_blocked"
    ledger.add("unknown", "unknown_code", "unknown")
    assert ledger.inspection_status == "integrity_blocked"
    lower_first = EvidenceLedger()
    lower_first.add("unknown", "unknown_code", "unknown")
    lower_first.add("warning", "warning_code", "warning")
    lower_first.add("integrity_error", "integrity_code", "blocked")
    assert lower_first.inspection_status == "integrity_blocked"


def test_foundation_reports_evidence_only_and_preserves_source(tmp_path, monkeypatch):
    source = tmp_path / "source.mph"
    source.write_bytes(b"immutable model bytes")
    source_hash = _hash(source)
    monkeypatch.setattr(
        "src.tools.wave_optics_preflight.ownership_manager.status",
        lambda **_kwargs: {
            "session": {"connected": True},
            "lease": {"state": "absent"},
            "external_solver_processes": [],
            "collision": False,
        },
    )

    result = collect_preflight_foundation(
        MetadataOnlyModel(source),
        model_name="ExactModel",
        session_state={"connected": True},
        active_profile="wave_optics",
        expected_source_path=str(source),
        expected_source_sha256=source_hash,
        loaded_source_identity={
            "source_path": str(source),
            "source_sha256": source_hash,
            "capture": "test_load",
        },
    )

    assert result["inspection_status"] == "partial"
    assert result["assessment"] == {
        "mode": "evidence_only",
        "project_verdict": None,
        "long_sweep_recommendation": None,
    }
    assert result["provenance"]["source_sha256"] == source_hash
    assert result["ownership"]["solve_permitted"] is True
    assert result["incidence"] == {}
    assert "incidence_not_inspected" in {item["code"] for item in result["evidence"]["unknowns"]}
    assert result["next_call"]["available"] is False
    assert result["next_call"]["missing_evidence"] == [
        "topology",
        "periodicity",
        "ports",
        "incidence",
        "wavelength",
        "mesh_study_results",
    ]
    assert _hash(source) == source_hash


@pytest.mark.parametrize("mismatch", ["path", "hash"])
def test_foundation_blocks_only_declared_integrity_mismatch(tmp_path, monkeypatch, mismatch):
    source = tmp_path / "source.mph"
    source.write_bytes(b"source")
    monkeypatch.setattr(
        "src.tools.wave_optics_preflight.ownership_manager.status",
        lambda **_kwargs: {"collision": False},
    )
    kwargs = {
        "expected_source_path": str(source),
        "expected_source_sha256": _hash(source),
    }
    if mismatch == "path":
        kwargs["expected_source_path"] = str(tmp_path / "other.mph")
    else:
        kwargs["expected_source_sha256"] = "0" * 64

    result = collect_preflight_foundation(
        MetadataOnlyModel(source),
        model_name="ExactModel",
        session_state={},
        active_profile="full",
        loaded_source_identity={
            "source_path": str(source),
            "source_sha256": _hash(source),
            "capture": "test_load",
        },
        **kwargs,
    )

    assert result["inspection_status"] == "integrity_blocked"
    assert result["next_call"]["available"] is False
    codes = {item["code"] for item in result["evidence"]["integrity_errors"]}
    assert f"source_{mismatch}_mismatch" in codes


def test_foundation_treats_solver_collision_as_integrity_blocker(tmp_path, monkeypatch):
    source = tmp_path / "source.mph"
    source.write_bytes(b"source")
    monkeypatch.setattr(
        "src.tools.wave_optics_preflight.ownership_manager.status",
        lambda **_kwargs: {"collision": True, "external_solver_processes": [{"pid": 1}]},
    )

    result = collect_preflight_foundation(
        MetadataOnlyModel(source),
        model_name="ExactModel",
        session_state={},
        active_profile="core",
    )

    assert result["inspection_status"] == "integrity_blocked"
    assert result["ownership"]["solve_permitted"] is False


def test_foundation_requires_exact_nonempty_model_name(tmp_path):
    with pytest.raises(ValueError, match="exact and non-empty"):
        collect_preflight_foundation(
            MetadataOnlyModel(tmp_path / "missing.mph"),
            model_name="",
            session_state={},
            active_profile="full",
        )


class FakeSelection:
    def __init__(self, entities=None, error=None):
        self._entities = entities or []
        self._error = error

    def entities(self):
        if self._error:
            raise RuntimeError(self._error)
        return self._entities


class FakeContainer:
    def __init__(self, items=None):
        self.items = items or {}

    def tags(self):
        return list(self.items)

    def get(self, tag):
        return self.items[str(tag)]


class FakeFeature:
    def __init__(self, tag, kind, *, props=None, selections=None, children=None, label=None):
        self.tag = tag
        self.kind = kind
        self.props = props or {}
        self.selections = selections or {}
        self.children = FakeContainer(children)
        self._label = label or tag

    def getType(self):
        return self.kind

    def label(self):
        return self._label

    def getString(self, name):
        if name not in self.props:
            raise RuntimeError("property unavailable")
        return self.props[name]

    def selection(self, name=None):
        key = name or "default"
        value = self.selections.get(key)
        if isinstance(value, Exception):
            raise value
        return FakeSelection(value)

    def feature(self):
        return self.children


class FakeMaterial(FakeFeature):
    def propertyGroup(self, name):
        assert name == "def"
        return FakeFeature("def", "Basic", props={"relpermittivity": "1"})


class FakeGeometry(FakeFeature):
    centers = {
        1: [0, 0.5, 0.5],
        2: [1, 0.5, 0.5],
        3: [0.5, 0, 0.5],
        4: [0.5, 1, 0.5],
        5: [0.5, 0.5, 0],
        6: [0.5, 0.5, 1],
    }
    normals = {
        1: [-1, 0, 0],
        2: [1, 0, 0],
        3: [0, -1, 0],
        4: [0, 1, 0],
        5: [0, 0, -1],
        6: [0, 0, 1],
    }

    def getNBoundaries(self):
        return 6

    def getNDomains(self):
        return 1

    def getSDim(self):
        return 3

    def getUpDown(self):
        return [[1, 0, 1, 0, 1, 0], [0, 1, 0, 1, 0, 1]]

    def getBoundingBox(self):
        return [0, 1, 0, 1, 0, 1]

    def faceParamRange(self, _number):
        return [0, 1, 0, 1]

    def faceX(self, number, _point):
        return [self.centers[number]]

    def faceNormal(self, number, _point):
        return [self.normals[number]]


class FakeMesh(FakeFeature):
    def __init__(self, elements, features=None):
        super().__init__("mesh1", "MeshSequence", children=features)
        self.elements = elements

    def getNumElem(self):
        return self.elements

    def getNumVertex(self):
        return self.elements // 2


class FakeComponent:
    def __init__(
        self,
        *,
        missing_rdir=False,
        mismatched_floquet=False,
        absent_excited=False,
        empty_mesh=False,
        inaccessible_incidence=False,
        mesh_case="valid",
    ):
        fpc_x = [1, 3, 2] if mismatched_floquet else [1, 2]
        children = {
            "fpc1": FakeFeature(
                "fpc1",
                "PeriodicCondition",
                selections={"default": fpc_x},
                props={"PeriodicType": "Floquet"},
            ),
            "fpc2": FakeFeature(
                "fpc2",
                "PeriodicCondition",
                selections={"default": [3, 4]},
                props={"PeriodicType": "Floquet"},
            ),
            "pport1": FakeFeature("pport1", "PeriodicPort", selections={"default": [6]}),
            "pport2": FakeFeature("pport2", "PeriodicPort", selections={"default": [5]}),
        }
        if not missing_rdir:
            children["rdir1"] = FakeFeature(
                "rdir1", "ReferenceDirection", selections={"default": [10]}
            )
        props = (
            {}
            if inaccessible_incidence
            else {
                "Polarization": "LinearPol",
                "LinearPol": "S",
                "alpha1_inc": "theta",
                "alpha2_inc": "phi",
            }
        )
        ps = FakeFeature(
            "ps1",
            "PeriodicStructure",
            props=props,
            children=children,
            selections={
                "allBoundaries": [1, 2, 3, 4, 5, 6],
                "excitedPortSelection": [] if absent_excited else [6],
            },
        )
        ewfd = FakeFeature("ewfd", "ElectromagneticWavesFrequencyDomain", children={"ps1": ps})
        fin = FakeFeature("fin", "FormUnion", props={"action": "union", "createpairs": "off"})
        geom = FakeGeometry("geom1", "Geometry", children={"fin": fin})
        mesh_features = {
            "ft_x": FakeFeature("ft_x", "FreeTri", selections={"default": [1]}),
            "cp_x": FakeFeature("cp_x", "CopyFace", selections={"source": [1], "destination": [2]}),
            "ft_y": FakeFeature("ft_y", "FreeTri", selections={"default": [3]}),
            "cp_y": FakeFeature("cp_y", "CopyFace", selections={"source": [3], "destination": [4]}),
            "ftet1": FakeFeature("ftet1", "FreeTet", selections={"default": [1]}),
        }
        if mesh_case == "missing_copy_y":
            del mesh_features["cp_y"]
        elif mesh_case == "wrong_order":
            mesh_features = {
                "cp_x": mesh_features["cp_x"],
                "ft_x": mesh_features["ft_x"],
                "ft_y": mesh_features["ft_y"],
                "cp_y": mesh_features["cp_y"],
                "ftet1": mesh_features["ftet1"],
            }
        self._physics = FakeContainer({"ewfd": ewfd})
        self._geom = FakeContainer({"geom1": geom})
        self._mesh = FakeContainer({"mesh1": FakeMesh(0 if empty_mesh else 1200, mesh_features)})
        self._materials = FakeContainer(
            {"mat1": FakeMaterial("mat1", "Common", selections={"default": [1]})}
        )

    def physics(self):
        return self._physics

    def geom(self):
        return self._geom

    def mesh(self):
        return self._mesh

    def material(self):
        return self._materials

    def pair(self):
        return FakeContainer()


class FakeStudy:
    def __init__(self, linked=True, empty=False):
        props = {"plist": "wl" if linked else "5e-6", "punit": "m"}
        self._features = FakeContainer(
            {} if empty else {"wl_step": FakeFeature("wl_step", "Wavelength", props=props)}
        )

    def feature(self):
        return self._features


class FakeJavaModel:
    def __init__(self, component, linked=True, empty_steps=False):
        self._components = FakeContainer({"comp1": component})
        self._studies = FakeContainer({"std1": FakeStudy(linked, empty=empty_steps)})

    def component(self):
        return self._components

    def study(self):
        return self._studies

    def tag(self):
        return "ExactModel"


class FullFakeModel(MetadataOnlyModel):
    def __init__(self, path, **fixture):
        super().__init__(path)
        linked = fixture.pop("linked", True)
        empty_steps = fixture.pop("empty_steps", False)
        self._java = FakeJavaModel(FakeComponent(**fixture), linked=linked, empty_steps=empty_steps)
        self._linked = linked

    @property
    def java(self):
        return self._java

    def parameters(self, evaluate=False):
        assert evaluate is False
        return {"wl": "5[um]", "theta": "0[deg]"}

    def solutions(self):
        return ["Solution 1"]

    def datasets(self):
        return ["Study 1//Solution 1"]


def _full_result(tmp_path, monkeypatch, *, active_profile="wave_optics", **fixture):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"immutable fixture")
    monkeypatch.setattr(
        "src.tools.wave_optics_preflight.ownership_manager.status",
        lambda **_kwargs: {"collision": False, "session": {"connected": True}},
    )
    result = collect_wave_optics_preflight(
        FullFakeModel(source, **fixture),
        model_name="ExactModel",
        session_state={"connected": True},
        active_profile=active_profile,
        expected_component_tag="comp1",
        expected_physics_tag="ewfd",
        expected_study_tag="std1",
        expected_source_path=str(source),
        expected_source_sha256=_hash(source),
        loaded_source_identity={
            "source_path": str(source),
            "source_sha256": _hash(source),
            "capture": "test_load",
        },
        target_wavelength_parameter="wl",
    )
    assert _hash(source) == result["provenance"]["source_sha256"]
    return result


@pytest.mark.parametrize("axes", [["x", "y", "z", "x"], ["invalid"], [1]])
def test_preflight_refuses_invalid_axes_before_model_access(axes):
    with pytest.raises(ValueError, match="expected_lattice_axes"):
        collect_wave_optics_preflight(
            object(),
            model_name="unused",
            session_state={},
            active_profile="wave_optics",
            expected_lattice_axes=axes,
        )


def test_wavelength_expression_uses_selected_physics_tag(tmp_path, monkeypatch):
    original = FakeComponent.__init__

    def renamed(self, **kwargs):
        original(self, **kwargs)
        self._physics.items["optics2"] = self._physics.items.pop("ewfd")

    monkeypatch.setattr(FakeComponent, "__init__", renamed)
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(
        "src.tools.wave_optics_preflight.ownership_manager.status",
        lambda **_kwargs: {"collision": False, "session": {"connected": True}},
    )
    result = collect_wave_optics_preflight(
        FullFakeModel(source),
        model_name="ExactModel",
        session_state={"connected": True},
        active_profile="wave_optics",
        expected_physics_tag="optics2",
    )
    assert result["wavelength"]["solved_frequency_expression"] == "c_const/optics2.freq"


def test_full_preflight_collects_read_only_wave_optics_evidence(tmp_path, monkeypatch):
    result = _full_result(tmp_path, monkeypatch)

    assert result["inspection_status"] == "complete"
    assert result["topology"]["domain_count"] == 1
    assert result["topology"]["form_finalization"]["properties"]["action"] == "union"
    assert len(result["periodicity"]["floquet_features"]) == 2
    assert result["ports"]["excited_port_selection"] == [6]
    assert result["incidence"]["raw_properties"]["LinearPol"] == "S"
    assert result["incidence"]["physical_polarization_evidence"] == "label_only"
    assert result["wavelength"]["structurally_linked"] is True
    assert result["mesh_study_results"]["meshes"][0]["element_count"] == 1200
    assert result["assessment"]["project_verdict"] is None
    assert result["next_call"]["available"] is True
    assert result["next_call"]["implementation_status"] == "experimental"
    assert result["next_call"]["missing_evidence"] == []


def test_empty_study_steps_are_explicit_unknown_evidence(tmp_path, monkeypatch):
    result = _full_result(tmp_path, monkeypatch, empty_steps=True)

    assert result["wavelength"]["structurally_linked"] is None
    assert "study_steps_missing" in {item["code"] for item in result["evidence"]["unknowns"]}


def test_selected_boundary_probes_obey_global_budget(monkeypatch):
    class LargeGeometry:
        @staticmethod
        def getNBoundaries():
            return MAX_BOUNDARIES * 3

        @staticmethod
        def getSDim():
            return 3

        @staticmethod
        def getUpDown():
            count = MAX_BOUNDARIES * 3
            return [[1] * count, [0] * count]

    probed = []

    def probe(_geom, number, **_kwargs):
        probed.append(number)
        return {"number": number}

    monkeypatch.setattr("src.tools.wave_optics_preflight._probe_boundary_read_only", probe)
    ledger = EvidenceLedger()
    boundary_map = {}

    _extend_boundary_map(LargeGeometry(), boundary_map, range(1, MAX_BOUNDARIES * 2 + 1), ledger)

    assert len(probed) == MAX_BOUNDARIES
    assert len(boundary_map) == MAX_BOUNDARIES
    assert "selected_boundary_probe_budget_exceeded" in {item["code"] for item in ledger.unknowns}


def test_selected_boundary_topology_failure_remains_partial_evidence(monkeypatch):
    class UnreadableGeometry:
        @staticmethod
        def getNBoundaries():
            raise RuntimeError("topology unavailable")

    monkeypatch.setattr(
        "src.tools.wave_optics_preflight._probe_boundary_read_only",
        lambda *_args, **_kwargs: pytest.fail("unreadable topology must not be probed"),
    )
    ledger = EvidenceLedger()

    _extend_boundary_map(UnreadableGeometry(), {}, [1], ledger)

    assert [item["code"] for item in ledger.unknowns] == ["selected_boundary_topology_unreadable"]


def test_complete_preflight_does_not_recommend_tool_outside_profile(tmp_path, monkeypatch):
    result = _full_result(tmp_path, monkeypatch, active_profile="core")

    assert result["inspection_status"] != "integrity_blocked"
    assert result["next_call"]["available"] is False
    assert result["next_call"]["implementation_status"] == "experimental"
    assert result["next_call"]["missing_evidence"] == []


@pytest.mark.parametrize(
    ("fixture", "code", "level"),
    [
        ({"missing_rdir": True}, "reference_direction_missing", "unknowns"),
        ({"mismatched_floquet": True}, "floquet_face_count_mismatch", "warnings"),
        ({"absent_excited": True}, "excited_port_selection_empty", "unknowns"),
        ({"linked": False}, "wavelength_link_missing", "unknowns"),
        ({"empty_mesh": True}, "mesh_empty", "warnings"),
        ({"inaccessible_incidence": True}, "incidence_properties_unreadable", "unknowns"),
    ],
)
def test_preflight_fixtures_preserve_failures_as_evidence(
    tmp_path, monkeypatch, fixture, code, level
):
    result = _full_result(tmp_path, monkeypatch, **fixture)
    codes = {item["code"] for item in result["evidence"][level]}
    assert code in codes


def test_exact_tags_are_resolved_from_complete_discovery_lists(tmp_path, monkeypatch):
    source = tmp_path / "many-tags.mph"
    source.write_bytes(b"many tags")
    model = FullFakeModel(source)
    component = model._java._components.items["comp1"]
    physics = component._physics.items["ewfd"]
    study = model._java._studies.items["std1"]
    model._java._components.items = {
        **{f"dummy_comp_{index}": object() for index in range(256)},
        "comp1": component,
    }
    component._physics.items = {
        **{f"dummy_physics_{index}": object() for index in range(256)},
        "ewfd": physics,
    }
    model._java._studies.items = {
        **{f"dummy_study_{index}": object() for index in range(256)},
        "std1": study,
    }
    monkeypatch.setattr(
        "src.tools.wave_optics_preflight.ownership_manager.status",
        lambda **_kwargs: {"collision": False},
    )

    result = collect_wave_optics_preflight(
        model,
        model_name="ExactModel",
        session_state={},
        active_profile="wave_optics",
        expected_component_tag="comp1",
        expected_physics_tag="ewfd",
        expected_study_tag="std1",
        expected_source_path=str(source),
        expected_source_sha256=_hash(source),
        loaded_source_identity={
            "source_path": str(source),
            "source_sha256": _hash(source),
            "capture": "test_load",
        },
        target_wavelength_parameter="wl",
    )

    assert result["inspection_status"] == "complete"
    assert result["topology"]["component_tag"] == "comp1"
    assert result["topology"]["component_tags_truncated"] is True
    assert result["topology"]["physics_tags_truncated"] is True
    assert result["wavelength"]["study_tag"] == "std1"
    assert result["wavelength"]["study_tags_truncated"] is True


def test_ambiguous_periodic_structure_does_not_audit_arbitrary_candidate(tmp_path, monkeypatch):
    source = tmp_path / "ambiguous.mph"
    source.write_bytes(b"ambiguous")
    model = FullFakeModel(source)
    physics = model._java._components.items["comp1"]._physics.items["ewfd"]
    first = physics.children.items["ps1"]
    physics.children.items["ps2"] = FakeFeature(
        "ps2",
        "PeriodicStructure",
        props={"LinearPol": "P"},
    )
    monkeypatch.setattr(
        "src.tools.wave_optics_preflight.ownership_manager.status",
        lambda **_kwargs: {"collision": False},
    )

    result = collect_wave_optics_preflight(
        model,
        model_name="ExactModel",
        session_state={},
        active_profile="wave_optics",
        expected_component_tag="comp1",
        expected_physics_tag="ewfd",
        expected_study_tag="std1",
        expected_source_sha256=_hash(source),
        loaded_source_identity={
            "source_path": str(source),
            "source_sha256": _hash(source),
            "capture": "test_load",
        },
        target_wavelength_parameter="wl",
    )

    assert first.props["LinearPol"] == "S"
    assert result["periodicity"]["periodic_structure_tag"] is None
    assert result["periodicity"]["candidate_count"] == 2
    assert result["ports"]["periodic_port_features"] == []
    assert result["incidence"]["selection_status"] == "ambiguous_periodic_structure"


def test_selected_boundary_after_response_cap_is_used_for_physical_inference(tmp_path, monkeypatch):
    class LargeGeometry(FakeGeometry):
        def getNBoundaries(self):
            return 300

        def getUpDown(self):
            return [[1] * 300, [0] * 300]

        def faceX(self, number, _point):
            return [[1.0 if number == 300 else 0.0, float(number), 0.0]]

        def faceNormal(self, number, _point):
            return [[1.0, 0.0, 0.0] if number == 300 else [-1.0, 0.0, 0.0]]

    source = tmp_path / "large-topology.mph"
    source.write_bytes(b"large topology")
    model = FullFakeModel(source)
    component = model._java._components.items["comp1"]
    component._geom.items["geom1"] = LargeGeometry(
        "geom1", "Geometry", children={"fin": FakeFeature("fin", "FormUnion")}
    )
    ps = component._physics.items["ewfd"].children.items["ps1"]
    ps.selections["allBoundaries"] = [1, 300]
    ps.children.items["fpc1"].selections["default"] = [1, 300]
    ps.children.items["pport1"].selections["default"] = [300]
    monkeypatch.setattr(
        "src.tools.wave_optics_preflight.ownership_manager.status",
        lambda **_kwargs: {"collision": False},
    )

    result = collect_wave_optics_preflight(
        model,
        model_name="ExactModel",
        session_state={},
        active_profile="wave_optics",
        expected_component_tag="comp1",
        expected_physics_tag="ewfd",
        expected_study_tag="std1",
        expected_source_sha256=_hash(source),
        loaded_source_identity={
            "source_path": str(source),
            "source_sha256": _hash(source),
            "capture": "test_load",
        },
        target_wavelength_parameter="wl",
    )

    assert len(result["topology"]["boundaries"]) == 256
    group = result["periodicity"]["floquet_features"][0]["opposing_face_groups"]
    assert group["count_balanced"] is True
    assert result["ports"]["periodic_port_features"][0]["adjacent_domains"] == [1]


@pytest.mark.parametrize(
    ("target", "code"),
    [
        ("all_boundaries", "periodic_all_boundaries_unreadable"),
        ("subfeature", "periodic_subfeature_selections_unreadable"),
        ("boundary_probe", "boundary_probes_incomplete"),
    ],
)
def test_failed_periodic_probes_and_selections_enter_unknown_ledger(
    tmp_path, monkeypatch, target, code
):
    source = tmp_path / f"{target}.mph"
    source.write_bytes(target.encode())
    model = FullFakeModel(source)
    component = model._java._components.items["comp1"]
    ps = component._physics.items["ewfd"].children.items["ps1"]
    if target == "all_boundaries":
        ps.selections["allBoundaries"] = RuntimeError("unreadable")
    elif target == "subfeature":
        ps.children.items["fpc1"].selections["default"] = RuntimeError("unreadable")
    else:
        geometry = component._geom.items["geom1"]
        geometry.faceNormal = lambda number, point: (
            (_ for _ in ()).throw(RuntimeError("probe failed"))
            if number == 1
            else [geometry.normals[number]]
        )
    monkeypatch.setattr(
        "src.tools.wave_optics_preflight.ownership_manager.status",
        lambda **_kwargs: {"collision": False},
    )

    result = collect_wave_optics_preflight(
        model,
        model_name="ExactModel",
        session_state={},
        active_profile="wave_optics",
        expected_component_tag="comp1",
        expected_physics_tag="ewfd",
        expected_study_tag="std1",
        expected_source_sha256=_hash(source),
        loaded_source_identity={
            "source_path": str(source),
            "source_sha256": _hash(source),
            "capture": "test_load",
        },
        target_wavelength_parameter="wl",
    )

    assert result["inspection_status"] == "partial"
    assert code in {item["code"] for item in result["evidence"]["unknowns"]}


def test_preflight_uses_loaded_identity_when_source_path_changes(tmp_path, monkeypatch):
    source = tmp_path / "replaced.mph"
    source.write_bytes(b"loaded bytes")
    loaded_hash = _hash(source)
    model = FullFakeModel(source)
    source.write_bytes(b"replacement bytes")
    monkeypatch.setattr(
        "src.tools.wave_optics_preflight.ownership_manager.status",
        lambda **_kwargs: {"collision": False},
    )

    result = collect_wave_optics_preflight(
        model,
        model_name="ExactModel",
        session_state={},
        active_profile="wave_optics",
        expected_component_tag="comp1",
        expected_physics_tag="ewfd",
        expected_study_tag="std1",
        expected_source_sha256=loaded_hash,
        loaded_source_identity={
            "source_path": str(source),
            "source_sha256": loaded_hash,
            "capture": "load_bracketed",
        },
        target_wavelength_parameter="wl",
    )

    assert result["inspection_status"] == "complete"
    assert result["provenance"]["source_sha256"] == loaded_hash
    assert result["provenance"]["current_file_sha256"] == _hash(source)
    assert "source_file_changed_since_load" in {
        item["code"] for item in result["evidence"]["warnings"]
    }
