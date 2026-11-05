"""Unit tests for MIM patch helpers without a COMSOL client."""

import json

import pytest
from mcp.server.mcpserver import MCPServer
from src.tools import mim_patch as mim_patch_module
from src.tools.mim_patch import (
    _build_periodic_mesh,
    _find_air_block_tag,
    _identify_patch_topology,
    _identify_side_pairs,
    _list_pair_metadata,
    _normalize_spectral_rows,
    _require_mim_selections,
    _set_copy_face_selections,
    register_mim_patch_tools,
)


def test_boundary_probe_refuses_oversized_geometry_before_adjacency():
    class Geometry:
        def getNBoundaries(self):
            return 4097

        def getNDomains(self):
            raise AssertionError("oversized geometry must be refused before further reads")

    with pytest.raises(ValueError, match="boundary probe limit"):
        mim_patch_module._probe_boundaries(Geometry())


class JavaStringLike:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value


class PairNode:
    def label(self):
        return JavaStringLike("Identity Pair 1")


class PairCollection:
    def tags(self):
        return [JavaStringLike("pair1")]

    def get(self, tag):
        assert str(tag) == "pair1"
        return PairNode()


class Component:
    def pair(self):
        return PairCollection()


class GeometryFeature:
    def __init__(self, size):
        self.size = size

    def getString(self, name):
        assert name == "size"
        return JavaStringLike(self.size)


class GeometryFeatures:
    def __init__(self):
        self.nodes = {
            "thin": GeometryFeature("1e-6, 1e-6, 4e-8"),
            "air": GeometryFeature("1e-6, 1e-6, 2e-6"),
            "fin": GeometryFeature(""),
        }

    def tags(self):
        return [JavaStringLike(tag) for tag in self.nodes]

    def get(self, tag):
        return self.nodes[str(tag)]


class Geometry:
    def feature(self):
        return GeometryFeatures()


def test_pair_metadata_normalizes_clientapi_strings_for_json():
    pairs = _list_pair_metadata(Component())

    assert pairs == [{"tag": "pair1", "label": "Identity Pair 1"}]
    assert json.loads(json.dumps(pairs)) == pairs


def test_air_block_detection_returns_python_string_tag():
    tag = _find_air_block_tag(Geometry())

    assert tag == "air"
    assert type(tag) is str


def test_air_block_detection_selects_the_largest_candidate_not_the_first_tall_one():
    features = GeometryFeatures()
    features.nodes = {
        "small_tall": GeometryFeature("1e-6, 1e-6, 5e-7"),
        "largest": GeometryFeature("2e-6, 2e-6, 2e-6"),
        "fin": GeometryFeature(""),
    }
    geometry = type("LargestGeometry", (), {"feature": lambda self: features})()

    assert _find_air_block_tag(geometry) == "largest"


def test_equal_largest_air_candidates_require_an_explicit_tag():
    features = GeometryFeatures()
    features.nodes = {
        "first": GeometryFeature("2e-6, 2e-6, 2e-6"),
        "second": GeometryFeature("2e-6, 2e-6, 2e-6"),
    }
    geometry = type("AmbiguousGeometry", (), {"feature": lambda self: features})()

    assert _find_air_block_tag(geometry) is None


def test_air_block_detection_prefers_height_over_volume():
    features = GeometryFeatures()
    features.nodes = {
        "wide_substrate": GeometryFeature("20e-6, 20e-6, 0.5e-6"),
        "air": GeometryFeature("2e-6, 2e-6, 2e-6"),
    }
    geometry = type("HeightGeometry", (), {"feature": lambda self: features})()

    assert _find_air_block_tag(geometry) == "air"


def test_side_pair_classification_requires_cell_coordinates():
    with pytest.raises(ValueError, match="bbox or period"):
        _identify_side_pairs(
            [{"boundary_number": 1, "normal": [1.0, 0.0, 0.0], "center": [0.5, 0, 0]}]
        )


def test_side_pair_classification_requires_exterior_boundaries():
    bbox = (0.0, 1.0, 0.0, 1.0, 0.0, 1.0)
    interior_edge_face = {
        "boundary_number": 41,
        "normal": [0.0, 0.0, -1.0],
        "center": [0.5, 0.5, 0.0],
        "interior": True,
    }
    exterior_bottom = {
        "boundary_number": 42,
        "normal": [0.0, 0.0, -1.0],
        "center": [0.5, 0.5, 0.0],
    }

    result = _identify_side_pairs([interior_edge_face, exterior_bottom], bbox=bbox)

    assert result["bottom"] == [42]
    assert result["top"] == []
    assert result["x_src"] == result["x_dst"] == result["y_src"] == result["y_dst"] == []


def test_square_spectral_matrix_layout_is_rejected_as_ambiguous():
    with pytest.raises(ValueError, match="square and its layout is ambiguous"):
        _normalize_spectral_rows([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]], 3)


def _side_pairs():
    return {"x_src": [1], "x_dst": [2], "y_src": [3], "y_dst": [4]}


@pytest.mark.parametrize(
    "missing",
    ["patch_footprint", "bottom", "top", "x_src", "x_dst", "y_src", "y_dst"],
)
def test_required_mim_selections_reject_every_missing_build_input(missing):
    values = {
        "patch_footprint": [5],
        "bottom": [6],
        "top": [7],
        **_side_pairs(),
    }
    values[missing] = []

    with pytest.raises(ValueError, match=missing):
        _require_mim_selections(
            values["patch_footprint"],
            values["bottom"],
            values["top"],
            values,
        )


def test_patch_topology_is_bound_by_geometry_and_adjacency_not_domain_order():
    boundaries = [
        {
            "boundary_number": 31,
            "up_domain": 17,
            "down_domain": 4,
            "interior": True,
            "center": [0.3e-6, 0.3e-6, 30e-9],
            "normal": [0.0, 0.0, -1.0],
        },
        {
            "boundary_number": 32,
            "up_domain": 9,
            "down_domain": 17,
            "interior": True,
            "center": [0.3e-6, 0.3e-6, 60e-9],
            "normal": [0.0, 0.0, 1.0],
        },
        {
            "boundary_number": 33,
            "up_domain": 9,
            "down_domain": 17,
            "interior": True,
            "center": [0.15e-6, 0.3e-6, 45e-9],
            "normal": [-1.0, 0.0, 0.0],
        },
    ]

    domain, footprint = _identify_patch_topology(
        boundaries,
        [0.3e-6, 0.3e-6, 30e-9],
        [0.15e-6, 0.15e-6, 30e-9],
    )

    assert domain == 17
    assert footprint == [31]


def test_patch_topology_rejects_an_ambiguous_domain_identity():
    boundaries = [
        {
            "boundary_number": 1,
            "up_domain": 3,
            "down_domain": 4,
            "interior": True,
            "center": [0.5, 0.5, 0.0],
            "normal": [0.0, 0.0, -1.0],
        },
        {
            "boundary_number": 2,
            "up_domain": 3,
            "down_domain": 4,
            "interior": True,
            "center": [0.5, 0.5, 1.0],
            "normal": [0.0, 0.0, 1.0],
        },
    ]

    with pytest.raises(ValueError, match="ambiguous domain"):
        _identify_patch_topology(boundaries, [1.0, 1.0, 1.0], [0.0, 0.0, 0.0])


def test_patch_topology_uses_bottom_top_intersection_when_face_counts_tie():
    boundaries = [
        {
            "boundary_number": 1,
            "up_domain": 3,
            "down_domain": 7,
            "interior": True,
            "center": [0.5, 0.5, 0.0],
            "normal": [0.0, 0.0, -1.0],
        },
        {
            "boundary_number": 2,
            "up_domain": 3,
            "down_domain": 9,
            "interior": True,
            "center": [0.5, 0.5, 1.0],
            "normal": [0.0, 0.0, 1.0],
        },
        {
            "boundary_number": 3,
            "up_domain": 7,
            "down_domain": 9,
            "interior": True,
            "center": [0.0, 0.5, 0.5],
            "normal": [-1.0, 0.0, 0.0],
        },
    ]

    domain, footprint = _identify_patch_topology(boundaries, [1.0, 1.0, 1.0], [0.0, 0.0, 0.0])

    assert domain == 3
    assert footprint == [1]


def test_trusted_footprint_disambiguates_coincident_bottom_interfaces():
    boundaries = [
        {
            "boundary_number": 6,
            "up_domain": 2,
            "down_domain": 1,
            "interior": True,
            "center": [0.5, 0.5, 0.0],
            "normal": [0.0, 0.0, 1.0],
        },
        {
            "boundary_number": 12,
            "up_domain": 3,
            "down_domain": 1,
            "interior": True,
            "center": [0.5, 0.5, 0.0],
            "normal": [0.0, 0.0, 1.0],
        },
        {
            "boundary_number": 13,
            "up_domain": 2,
            "down_domain": 3,
            "interior": True,
            "center": [0.5, 0.5, 1.0],
            "normal": [0.0, 0.0, 1.0],
        },
    ]

    domain, footprint = _identify_patch_topology(
        boundaries,
        [1.0, 1.0, 1.0],
        [0.0, 0.0, 0.0],
        preferred_footprint=[12],
    )

    assert domain == 3
    assert footprint == [12]


class MeshSelection:
    def __init__(self):
        self.entities = None

    def set(self, entities):
        self.entities = list(entities)


class MeshFeature:
    def __init__(self):
        self.selections = {}

    def selection(self, name="default"):
        return self.selections.setdefault(name, MeshSelection())


class UnsupportedCopyFace:
    def selection(self, _name="default"):
        raise RuntimeError("named directed selections unavailable")


def test_copy_face_requires_explicit_source_and_destination_contract():
    with pytest.raises(RuntimeError, match="directed source/destination"):
        _set_copy_face_selections(UnsupportedCopyFace(), [1], [2])


def test_expression_major_spectral_values_are_transposed_to_wavelength_rows():
    rows = _normalize_spectral_rows(
        [
            [0.1, 0.2, 0.3],
            [0.4, 0.5, 0.6],
            [0.5, 0.3, 0.1],
            [4.0e-6, 4.1e-6, 4.2e-6],
        ],
        4,
    )

    assert rows == [
        [0.1, 0.4, 0.5, 4.0e-6],
        [0.2, 0.5, 0.3, 4.1e-6],
        [0.3, 0.6, 0.1, 4.2e-6],
    ]


def test_point_major_spectral_values_are_preserved_as_wavelength_rows():
    rows = _normalize_spectral_rows(
        [
            [0.1, 0.4, 0.5, 4.0e-6],
            [0.2, 0.5, 0.3, 4.1e-6],
            [0.3, 0.6, 0.1, 4.2e-6],
        ],
        4,
    )

    assert rows == [
        [0.1, 0.4, 0.5, 4.0e-6],
        [0.2, 0.5, 0.3, 4.1e-6],
        [0.3, 0.6, 0.1, 4.2e-6],
    ]


def test_singleton_wrapped_expression_major_values_are_normalized():
    rows = _normalize_spectral_rows(
        [
            [[0.1, 0.2, 0.3]],
            [[0.4, 0.5, 0.6]],
            [[0.5, 0.3, 0.1]],
            [[4.0e-6, 4.1e-6, 4.2e-6]],
        ],
        4,
    )

    assert rows == [
        [0.1, 0.4, 0.5, 4.0e-6],
        [0.2, 0.5, 0.3, 4.1e-6],
        [0.3, 0.6, 0.1, 4.2e-6],
    ]


def test_spectral_emissivity_preserves_transmission_and_prefers_absorptivity(monkeypatch):
    class Model:
        def evaluate(self, expressions):
            values = {
                "ewfd.Rtotal": [0.2],
                "ewfd.Ttotal": [0.3],
                "ewfd.Atotal": [0.5],
                "wl": [4.0e-6],
            }
            return [values[expression] for expression in expressions]

    monkeypatch.setattr(
        mim_patch_module.session_manager,
        "get_model",
        lambda _name: Model(),
    )
    server = MCPServer("mim-emissivity-closure-test")
    register_mim_patch_tools(server)
    tool = server._tool_manager._tools["mim_evaluate_spectral"]

    evaluated = tool.fn(model_name="model")
    derived = tool.fn(
        model_name="model",
        expressions=["ewfd.Rtotal", "ewfd.Ttotal", "wl"],
    )

    assert evaluated["spectral_data"][0]["emissivity"] == 0.5
    assert evaluated["spectral_data"][0]["emissivity_basis"] == "evaluated_absorptivity"
    assert derived["spectral_data"][0]["emissivity"] == pytest.approx(0.5)
    assert derived["spectral_data"][0]["emissivity_basis"] == "one_minus_reflectance_transmittance"


class MeshFeatures:
    def __init__(self):
        self.created = []

    def create(self, tag, feature_type):
        feature = MeshFeature()
        self.created.append((tag, feature_type, feature))
        return feature


class MeshNode:
    def __init__(self, *, fail_run=False):
        self.features = MeshFeatures()
        self.fail_run = fail_run
        self.ran = False

    def feature(self):
        return self.features

    def run(self):
        if self.fail_run:
            raise RuntimeError("mesh build failure")
        self.ran = True


class MeshList:
    def __init__(self, *, fail_run=False):
        self.nodes = {"mesh1": object()}
        self.fail_run = fail_run
        self.removed = []

    def tags(self):
        return list(self.nodes)

    def create(self, tag):
        node = MeshNode(fail_run=self.fail_run)
        self.nodes[tag] = node
        return node

    def remove(self, tag):
        self.removed.append(tag)
        del self.nodes[tag]


class MeshComponent:
    def __init__(self, *, fail_run=False):
        self.meshes = MeshList(fail_run=fail_run)

    def mesh(self):
        return self.meshes


def test_periodic_mesh_build_preserves_existing_sequences():
    component = MeshComponent()

    mesh, tag, preserved = _build_periodic_mesh(component, _side_pairs())

    assert tag == "mesh2"
    assert preserved == ["mesh1"]
    assert component.meshes.removed == []
    assert set(component.meshes.nodes) == {"mesh1", "mesh2"}
    assert mesh.ran is True
    assert [item[1] for item in mesh.features.created] == [
        "FreeTri",
        "FreeTri",
        "CopyFace",
        "CopyFace",
        "FreeTet",
    ]


def test_periodic_mesh_failure_removes_only_new_sequence():
    component = MeshComponent(fail_run=True)

    with pytest.raises(RuntimeError, match="mesh build failure"):
        _build_periodic_mesh(component, _side_pairs())

    assert component.meshes.removed == ["mesh2"]
    assert set(component.meshes.nodes) == {"mesh1"}


class BuildBlock:
    def __init__(self, pos, size):
        self.pos = pos
        self.size = size
        self.props = {}

    def getString(self, name):
        if name == "pos":
            return self.pos
        if name == "size":
            return self.size
        if name == "action":
            return "union"
        raise RuntimeError(f"unsupported property {name}")

    def set(self, name, value):
        self.props[name] = value

    def selection(self, _name):
        return type("Selection", (), {"set": lambda self, _v: None})()


class BuildFeatures:
    def __init__(self, nodes):
        self.nodes = dict(nodes)
        self.created = []
        self.removed = []

    def tags(self):
        return [JavaStringLike(tag) for tag in self.nodes]

    def get(self, tag):
        return self.nodes[str(tag)]

    def create(self, tag, kind):
        tag = str(tag)
        if tag in self.nodes:
            raise RuntimeError(f"duplicate feature {tag}")
        feature = BuildBlock(None, None)
        self.nodes[tag] = feature
        self.created.append(tag)
        return feature

    def remove(self, tag):
        self.removed.append(str(tag))
        self.nodes.pop(str(tag), None)


class BuildGeom:
    def __init__(self, features):
        self._features = features
        self.runs = 0

    def feature(self):
        return self._features

    def getNBoundaries(self):
        return 0

    def getNDomains(self):
        return 2

    def getSDim(self):
        return 3

    def getUpDown(self):
        return ([], [])

    def run(self):
        self.runs += 1

    def getBoundingBox(self):
        return [0, 1, 0, 1, 0, 1]


def test_mim_build_failure_restores_existing_physics_material_and_fin_state(monkeypatch):
    class Selection:
        def __init__(self, entities):
            self.value = list(entities)

        def entities(self):
            return list(self.value)

        def set(self, entities):
            self.value = list(entities)

    class PhysicsFeature:
        def __init__(self, named=None, entities=()):
            self.selections = {"default": Selection(entities)}
            if named:
                self.selections[named] = Selection(entities)

        def selection(self, name="default"):
            return self.selections[name]

    class PhysicsFeatures:
        def __init__(self):
            self.nodes = {
                "ltr1": PhysicsFeature(entities=[11]),
                "lib1": PhysicsFeature(entities=[12]),
                "ps1": PhysicsFeature(named="excitedPortSelection", entities=[13]),
            }

        def get(self, tag):
            return self.nodes.get(tag)

    physics_features = PhysicsFeatures()

    class Physics:
        def feature(self):
            return physics_features

    class PhysicsList:
        def get(self, tag):
            return Physics() if tag == "ewfd" else None

    class Material:
        def __init__(self):
            self.selection_node = Selection([21])

        def selection(self):
            return self.selection_node

    material = Material()

    class MaterialList:
        def get(self, tag):
            return material if tag == "mat_air" else None

    class Component:
        def physics(self):
            return PhysicsList()

        def material(self):
            return MaterialList()

    component = Component()
    fin = BuildBlock("0, 0, 0", "1, 1, 1")
    fin.getString = lambda _name: fin.props.get("action", "assembly")
    features = BuildFeatures({"air": BuildBlock("0, 0, 0", "1, 1, 1"), "fin": fin})
    geom = BuildGeom(features)
    built_actions = []

    def run_geometry():
        built_actions.append(fin.getString("action"))
        geom.runs += 1
        if geom.runs == 1:
            physics_features.nodes["ltr1"].selection().set([101])
            physics_features.nodes["lib1"].selection().set([102])
            physics_features.nodes["ps1"].selection("excitedPortSelection").set([103])
            material.selection().set([121])

    geom.run = run_geometry

    class Java:
        def component(self, _tag):
            return component

    class Model:
        java = Java()

    monkeypatch.setattr(mim_patch_module.session_manager, "get_model", lambda _name=None: Model())
    monkeypatch.setattr(mim_patch_module, "_get_geom_node", lambda *_args: (geom, None))
    monkeypatch.setattr(mim_patch_module, "_probe_boundaries", lambda _geom: ([], 3, 8, 3))
    monkeypatch.setattr(mim_patch_module, "_identify_patch_topology", lambda *_args: (5, [10]))
    monkeypatch.setattr(
        mim_patch_module,
        "_identify_side_pairs",
        lambda **_kwargs: {
            "bottom": [14],
            "top": [15],
            "x_src": [1],
            "x_dst": [2],
            "y_src": [3],
            "y_dst": [4],
        },
    )

    def fail_mesh(*_args):
        raise RuntimeError("mesh fail")

    monkeypatch.setattr(mim_patch_module, "_build_periodic_mesh", fail_mesh)
    server = MCPServer("mim-build-full-rollback-test")
    register_mim_patch_tools(server)

    result = server._tool_manager._tools["mim_patch_build"].fn(
        [0.2, 0.2, 0.1], [0.1, 0.1, 0.1], air_block_tag="air"
    )

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert physics_features.nodes["ltr1"].selection().entities() == [11]
    assert physics_features.nodes["lib1"].selection().entities() == [12]
    assert physics_features.nodes["ps1"].selection("excitedPortSelection").entities() == [13]
    assert material.selection().entities() == [21]
    assert fin.props["action"] == "assembly"
    assert built_actions == ["union", "assembly"]
    assert set(features.nodes) == {"air", "fin"}


def test_air_block_auto_detection_must_contain_the_patch_footprint():
    geom_features = BuildFeatures(
        {
            # Taller substrate is the largest block but is not the air region.
            "sub": BuildBlock("0, 0, 0", "1e-6, 1e-6, 5e-6"),
            "air": BuildBlock("0, 0, 5e-6", "1e-6, 1e-6, 2e-6"),
        }
    )
    geom = BuildGeom(geom_features)

    assert _find_air_block_tag(geom) == "sub"
    with pytest.raises(ValueError, match="does not contain the patch"):
        mim_patch_module._require_air_block_contains_patch(
            geom,
            "sub",
            [1e-7, 1e-7, 5.5e-6],
            [2e-7, 2e-7, 1e-7],
        )

    # The genuine air block accepts the same footprint.
    mim_patch_module._require_air_block_contains_patch(
        geom,
        "air",
        [1e-7, 1e-7, 5.5e-6],
        [2e-7, 2e-7, 1e-7],
    )


def test_failed_build_rolls_back_created_geometry_features(monkeypatch):
    from types import SimpleNamespace

    features = BuildFeatures({"air": BuildBlock("0, 0, 0", "1e-6, 1e-6, 4e-6")})
    geom = BuildGeom(features)
    selection = SimpleNamespace(entities=lambda: [], set=lambda _entities: None)
    feature = SimpleNamespace(selection=lambda *_args: selection)
    physics = SimpleNamespace(feature=lambda: SimpleNamespace(get=lambda _tag: feature))

    class Component:
        def geom(self, _name):
            return geom

        def physics(self):
            return SimpleNamespace(get=lambda _tag: physics)

        def material(self):
            return SimpleNamespace(get=lambda _tag: feature)

    class Java:
        def component(self, _name):
            return Component()

    class Model:
        java = Java()

    monkeypatch.setattr(mim_patch_module.session_manager, "get_model", lambda _name=None: Model())
    # The boundary probe needs a live JVM; the rollback contract under test
    # begins once probing reports no readable patch topology.
    monkeypatch.setattr(mim_patch_module, "_probe_boundaries", lambda _geom: ([], 2, 0, 3))
    server = MCPServer("mim-build-rollback-test")
    register_mim_patch_tools(server)
    build = server._tool_manager._tools["mim_patch_build"].fn

    first = build([2e-7, 2e-7, 1e-7], [1e-7, 1e-7, 1e-7], geometry_name="geom1")

    assert first["success"] is False
    assert "patch topology" in first["error"]
    assert first["rolled_back"] is True
    assert features.removed == ["dif1", "b_pat"]
    assert set(features.nodes) == {"air"}
    assert geom.runs == 2

    second = build([2e-7, 2e-7, 1e-7], [1e-7, 1e-7, 1e-7], geometry_name="geom1")

    assert "duplicate feature" not in second["error"]
    assert "patch topology" in second["error"]


def test_build_failure_before_feature_creation_reports_no_rollback_claims(monkeypatch):
    features = BuildFeatures({})  # no air block candidate at all

    class Component:
        def geom(self, _name):
            return BuildGeom(features)

    class Java:
        def component(self, _name):
            return Component()

    class Model:
        java = Java()

    monkeypatch.setattr(mim_patch_module.session_manager, "get_model", lambda _name=None: Model())
    server = MCPServer("mim-build-nofeature-test")
    register_mim_patch_tools(server)
    build = server._tool_manager._tools["mim_patch_build"].fn

    result = build([2e-7, 2e-7, 1e-7], [1e-7, 1e-7, 1e-7], geometry_name="geom1")

    assert result == {
        "success": False,
        "error": "Could not auto-detect air block tag. Please specify air_block_tag.",
    }
