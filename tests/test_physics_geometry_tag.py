"""Every standard physics interface must be created with a geometry tag.

COMSOL 6.x creates an interface at space dimension 0D when create() is called
without one, and initialisation then fails. The specialized tools added later
already passed the tag; these tests pin the five older ones down.
"""

import pytest

from src.tools import physics


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(function):
            self.tools[function.__name__] = function
            return function

        return decorator


class FakeGeometry:
    def __init__(self, tag="geom1"):
        self._tag = tag

    def tag(self):
        return self._tag


class FakeGeometryList:
    def __init__(self, items):
        self.items = items

    def size(self):
        return len(self.items)

    def tags(self):
        return [item.tag() for item in self.items]

    def __iter__(self):
        return iter(self.items)


class FakeSelection:
    def __init__(self):
        self.values = None

    def set(self, values):
        self.values = values


class FakePhysics:
    def __init__(self, tag, physics_type, geom_tag):
        self._tag = tag
        self.physics_type = physics_type
        self.geom_tag = geom_tag
        self._selection = FakeSelection()

    def tag(self):
        return self._tag

    def label(self):
        return self.physics_type

    def selection(self):
        return self._selection


class FakePhysicsList:
    def __init__(self):
        self.created = []

    def create(self, tag, physics_type, geom_tag=None):
        item = FakePhysics(tag, physics_type, geom_tag)
        self.created.append(item)
        return item

    def size(self):
        return len(self.created)

    def tags(self):
        return [item.tag() for item in self.created]

    def __call__(self, tag):
        return next((item for item in self.created if item.tag() == tag), None)


class FakeComponent:
    def __init__(self, tag="comp1", geometries=None):
        self._tag = tag
        self._physics = FakePhysicsList()
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
        return next((item for item in self._geometry.items if item.tag() == tag), None)


class FakeComponentList:
    def __init__(self, items):
        self.items = items

    def size(self):
        return len(self.items)

    def tags(self):
        return [item.tag() for item in self.items]

    def __call__(self, tag=None):
        if tag is None:
            return self
        return next((item for item in self.items if item.tag() == tag), None)


class FakeJavaModel:
    def __init__(self, components):
        self._components = FakeComponentList(components)

    def component(self, tag=None):
        return self._components if tag is None else self._components(tag)


class FakeModel:
    def __init__(self, components):
        self.java = FakeJavaModel(components)


def registered(monkeypatch, geometries=None):
    component = FakeComponent(geometries=geometries)
    model = FakeModel([component])
    monkeypatch.setattr(physics.session_manager, "get_model", lambda name=None: model)
    mcp = FakeMCP()
    physics.register_physics_tools(mcp)
    return component, mcp.tools


@pytest.mark.parametrize(
    "tool_name, tool_args, expected_tag, expected_type",
    [
        ("physics_add", {"physics_type": "HeatTransfer"}, "heattransfer", "HeatTransfer"),
        ("physics_add_electrostatics", {}, "es", "Electrostatics"),
        ("physics_add_solid_mechanics", {}, "solid", "SolidMechanics"),
        ("physics_add_heat_transfer", {}, "ht", "HeatTransfer"),
        ("physics_add_laminar_flow", {}, "spf", "LaminarFlow"),
    ],
)
def test_standard_interface_is_created_with_geometry(
    monkeypatch, tool_name, tool_args, expected_tag, expected_type
):
    component, tools = registered(monkeypatch)

    result = tools[tool_name](**tool_args)

    assert result["success"] is True, result
    created = component.physics().created[0]
    assert created.tag() == expected_tag
    assert created.physics_type == expected_type
    assert created.geom_tag == "geom1"


def test_physics_add_reports_missing_geometry(monkeypatch):
    _, tools = registered(monkeypatch, geometries=[])

    result = tools["physics_add"](physics_type="Electrostatics")

    assert result["success"] is False
    assert "No geometry found" in result["error"]


def test_physics_add_keeps_domain_selection(monkeypatch):
    component, tools = registered(monkeypatch)

    result = tools["physics_add_electrostatics"](domain_selection=[2, 3])

    created = component.physics().created[0]
    assert result["success"] is True
    assert created.selection().values == [2, 3]
