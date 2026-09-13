"""Regression tests for COMSOL 6.x Java API calls in the core physics tools.

The fakes below deliberately mirror the shapes the COMSOL client API exposes:
a component list and a geometry list offer ``size()`` and ``tags()`` but no
indexing, which is what broke ``geometry_get_boundaries``.
"""

from src.tools import physics


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(function):
            self.tools[function.__name__] = function
            return function

        return decorator


class FakePhysics:
    def __init__(self, tag, label):
        self._tag = tag
        self._label = label

    def tag(self):
        return self._tag

    def label(self):
        return self._label


class FakePhysicsList:
    def __init__(self, items):
        self.items = items

    def size(self):
        return len(self.items)

    def tags(self):
        return [item.tag() for item in self.items]

    def __call__(self, tag):
        return next(item for item in self.items if item.tag() == tag)


class FakeGeometry:
    def __init__(self, tag="geom1", boundaries=6, domains=1):
        self._tag = tag
        self._boundaries = boundaries
        self._domains = domains
        self.ran = False

    def tag(self):
        return self._tag

    def run(self):
        self.ran = True

    def getNBoundaries(self):
        return self._boundaries

    def getNDomains(self):
        return self._domains


class FakeGeometryList:
    """Mirrors ComponentGeomListClient: size() and tags(), no indexing."""

    def __init__(self, items):
        self.items = items

    def size(self):
        return len(self.items)

    def tags(self):
        return [item.tag() for item in self.items]


class FakeComponent:
    def __init__(self, tag="comp1", physics_items=(), geometries=None):
        self._tag = tag
        self._physics = FakePhysicsList(list(physics_items))
        self._geometry = FakeGeometryList(
            list(geometries) if geometries is not None else [FakeGeometry()]
        )

    def tag(self):
        return self._tag

    def physics(self, tag=None):
        return self._physics if tag is None else self._physics(tag)

    def geom(self, tag=None):
        if tag is None:
            return self._geometry
        return next(item for item in self._geometry.items if item.tag() == tag)


class FakeComponentList:
    def __init__(self, items):
        self.items = items

    def size(self):
        return len(self.items)

    def tags(self):
        return [item.tag() for item in self.items]

    def __call__(self, tag):
        return next(item for item in self.items if item.tag() == tag)


class FakeCoupling:
    def __init__(self, tag, coupling_type, geom_tag):
        self._tag = tag
        self.coupling_type = coupling_type
        self.geom_tag = geom_tag

    def tag(self):
        return self._tag

    def label(self):
        return self.coupling_type


class FakeMultiphysicsList:
    def __init__(self):
        self.created = []

    def create(self, tag, coupling_type, geom_tag):
        coupling = FakeCoupling(tag, coupling_type, geom_tag)
        self.created.append(coupling)
        return coupling


class FakeJavaModel:
    def __init__(self, components, multiphysics):
        self._components = FakeComponentList(components)
        self._multiphysics = multiphysics

    def component(self, tag=None):
        return self._components if tag is None else self._components(tag)

    def multiphysics(self):
        return self._multiphysics


class FakeModel:
    def __init__(self, components, multiphysics):
        self.java = FakeJavaModel(components, multiphysics)


def registered_tools(monkeypatch, physics_items=(), geometries=None):
    multiphysics = FakeMultiphysicsList()
    component = FakeComponent(physics_items=physics_items, geometries=geometries)
    model = FakeModel([component], multiphysics)
    monkeypatch.setattr(physics.session_manager, "get_model", lambda name=None: model)
    mcp = FakeMCP()
    physics.register_physics_tools(mcp)
    return model, multiphysics, mcp.tools


def test_geometry_get_boundaries_resolves_tag_from_list(monkeypatch):
    _, _, tools = registered_tools(monkeypatch)

    result = tools["geometry_get_boundaries"](component_name="comp1")

    assert result["success"] is True
    assert result["geometry"] == "geom1"
    assert result["total_boundaries"] == 6
    assert result["total_domains"] == 1
    assert len(result["boundaries"]) == 6


def test_geometry_get_boundaries_honours_explicit_name(monkeypatch):
    geometries = [FakeGeometry("geom1", 4), FakeGeometry("geom2", 9, 3)]
    _, _, tools = registered_tools(monkeypatch, geometries=geometries)

    result = tools["geometry_get_boundaries"](geometry_name="geom2")

    assert result["success"] is True
    assert result["geometry"] == "geom2"
    assert result["total_boundaries"] == 9
    assert result["total_domains"] == 3


def test_geometry_get_boundaries_without_geometry(monkeypatch):
    _, _, tools = registered_tools(monkeypatch, geometries=[])

    result = tools["geometry_get_boundaries"](component_name="comp1")

    assert result["success"] is False
    assert "No geometries" in result["error"]


def test_multiphysics_add_uses_three_argument_create(monkeypatch):
    _, multiphysics, tools = registered_tools(
        monkeypatch, physics_items=[FakePhysics("ht", "Heat Transfer")]
    )

    result = tools["multiphysics_add"](
        coupling_type="JouleHeating",
        physics_list=["Heat Transfer"],
    )

    assert result["success"] is True
    assert len(multiphysics.created) == 1
    created = multiphysics.created[0]
    assert created.coupling_type == "JouleHeating"
    assert created.geom_tag == "geom1"
    assert result["coupling"]["type"] == "JouleHeating"
    assert result["coupling"]["physics"] == ["Heat Transfer"]


def test_multiphysics_add_falls_back_to_first_component(monkeypatch):
    _, multiphysics, tools = registered_tools(monkeypatch)

    result = tools["multiphysics_add"](
        coupling_type="ThermalStress",
        physics_list=["not a real interface"],
    )

    assert result["success"] is True
    assert multiphysics.created[0].geom_tag == "geom1"
