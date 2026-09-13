"""Tests for the geometry features that go through the Java API.

These cover the four tools that used to reach COMSOL through the MPh node
proxy: geometry_add_feature, geometry_add_circle, geometry_import and
geometry_list_features.
"""

from src.tools import geometry


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(function):
            self.tools[function.__name__] = function
            return function

        return decorator


class FakeFeature:
    def __init__(self, tag, feature_type, rejected_properties=()):
        self._tag = tag
        self.feature_type = feature_type
        self.properties = {}
        self.rejected = set(rejected_properties)

    def tag(self):
        return self._tag

    def label(self):
        return self.feature_type

    def set(self, name, value):
        if name in self.rejected:
            raise ValueError(f"Unsupported property: {name}")
        self.properties[name] = value


class FakeFeatureList:
    def __init__(self, items=None):
        self.items = list(items or [])

    def size(self):
        return len(self.items)

    def tags(self):
        return [item.tag() for item in self.items]

    def create(self, tag, feature_type):
        feature = FakeFeature(tag, feature_type, rejected_properties=("rejected",))
        self.items.append(feature)
        return feature

    def __call__(self, tag):
        return next(item for item in self.items if item.tag() == tag)


class FakeGeometry:
    def __init__(self, tag="geom1", features=None):
        self._tag = tag
        self._features = FakeFeatureList(features)

    def tag(self):
        return self._tag

    def feature(self, tag=None):
        return self._features if tag is None else self._features(tag)


class FakeGeometryList:
    """Mirrors ComponentGeomListClient: iterable, size() and tags()."""

    def __init__(self, items=None):
        self.items = list(items or [])

    def size(self):
        return len(self.items)

    def tags(self):
        return [item.tag() for item in self.items]

    def __iter__(self):
        return iter(self.items)


class FakeComponent:
    def __init__(self, tag="comp1", geometries=None):
        self._tag = tag
        self._geometry = FakeGeometryList(
            list(geometries) if geometries is not None else [FakeGeometry()]
        )

    def tag(self):
        return self._tag

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

    def __call__(self, tag):
        return next((item for item in self.items if item.tag() == tag), None)


class FakeJavaModel:
    def __init__(self, components):
        self._components = FakeComponentList(components)

    def component(self, tag=None):
        return self._components if tag is None else self._components(tag)


class FakeModel:
    def __init__(self, components):
        self.java = FakeJavaModel(components)


def registered_tools(monkeypatch, geometries=None):
    component = FakeComponent(geometries=geometries)
    model = FakeModel([component])
    monkeypatch.setattr(geometry.session_manager, "get_model", lambda name=None: model)
    mcp = FakeMCP()
    geometry.register_geometry_tools(mcp)
    return component, mcp.tools


def test_add_feature_creates_java_feature(monkeypatch):
    component, tools = registered_tools(monkeypatch)

    result = tools["geometry_add_feature"](
        feature_type="Polygon",
        feature_name="poly1",
        points=[[0, 0], [1, 0], [1, 1]],
    )

    created = component.geom().items[0].feature("poly1")
    assert result["success"] is True
    assert created.feature_type == "Polygon"
    assert created.properties == {"points": [[0, 0], [1, 0], [1, 1]]}
    assert result["feature"] == {"name": "poly1", "type": "Polygon"}


def test_add_feature_autogenerates_tag(monkeypatch):
    component, tools = registered_tools(monkeypatch)

    result = tools["geometry_add_feature"](feature_type="Block")

    assert result["success"] is True
    assert result["feature"]["name"] == "block1"
    assert component.geom().items[0].feature("block1").feature_type == "Block"


def test_add_feature_reports_rejected_properties(monkeypatch):
    _, tools = registered_tools(monkeypatch)

    result = tools["geometry_add_feature"](
        feature_type="Block",
        feature_name="blk1",
        rejected="value",
        size=[1, 1, 1],
    )

    assert result["success"] is True
    assert list(result["failed_properties"]) == ["rejected"]


def test_add_feature_unknown_geometry_reports_error(monkeypatch):
    _, tools = registered_tools(monkeypatch)

    result = tools["geometry_add_feature"](
        feature_type="Block",
        geometry_name="missing",
    )

    assert result["success"] is False
    assert "not found" in result["error"]


def test_add_circle_creates_java_feature(monkeypatch):
    component, tools = registered_tools(monkeypatch)

    result = tools["geometry_add_circle"](
        position=[1, 2],
        radius=3.0,
        feature_name="c1",
    )

    created = component.geom().items[0].feature("c1")
    assert result["success"] is True
    assert created.feature_type == "Circle"
    assert created.properties == {"pos": ["1", "2"], "r": "3.0"}
    assert result["feature"]["radius"] == 3.0


def test_import_sets_filename_on_the_java_feature(monkeypatch):
    component, tools = registered_tools(monkeypatch)

    result = tools["geometry_import"](file_path="C:/tmp/cube.stl")

    created = component.geom().items[0].feature("imp1")
    assert result["success"] is True
    assert created.feature_type == "Import"
    assert created.properties == {"filename": "C:/tmp/cube.stl"}
    assert result["feature"]["file"] == "C:/tmp/cube.stl"


def test_list_features_reads_java_features(monkeypatch):
    features = [FakeFeature("blk1", "Block"), FakeFeature("c1", "Circle")]
    _, tools = registered_tools(monkeypatch, geometries=[FakeGeometry(features=features)])

    result = tools["geometry_list_features"]()

    assert result["success"] is True
    assert result["count"] == 2
    assert [item["name"] for item in result["features"]] == ["blk1", "c1"]
    assert result["features"][0]["label"] == "Block"


def test_tools_report_missing_geometry(monkeypatch):
    _, tools = registered_tools(monkeypatch, geometries=[])

    for tool_name, call in (
        ("geometry_add_feature", lambda: tools["geometry_add_feature"](feature_type="Block")),
        ("geometry_add_circle", lambda: tools["geometry_add_circle"]()),
        ("geometry_import", lambda: tools["geometry_import"](file_path="C:/tmp/cube.stl")),
        ("geometry_list_features", lambda: tools["geometry_list_features"]()),
    ):
        result = call()
        assert result["success"] is False, tool_name
        assert "No geometry sequences found" in result["error"], tool_name
