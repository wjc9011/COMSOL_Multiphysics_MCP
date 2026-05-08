"""Regression tests for PR-B M2 fix — material_create_from_kb must
explicitly create non-default propertyGroups (e.g. 'Enu') via the
two-arg ``mat.propertyGroup().create(<tag>, <descr>)`` Java API
(KB COMSOL_ProgrammingReferenceManual chunk 76973).

Spec: plans/mcp_pr_c_fix_spec.md §4.2.1, §6.1.

These tests do NOT boot a real COMSOL/JVM. A minimal Java-side stub
emulates the propertyGroup() factory, propertyGroup(<tag>) accessor,
and per-group set() recorder.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Java-side stubs
# ---------------------------------------------------------------------------

class _PropertyGroup:
    """Stand-in for COMSOL Java PropertyGroup. Records set() calls and
    rejects unknown property names if marked strict."""
    def __init__(self, tag: str, descr: str = ""):
        self.tag = tag
        self.descr = descr
        self.props: dict = {}

    def set(self, k, v):
        self.props[str(k)] = str(v)

    def get(self, k):
        return self.props.get(str(k))

    def getString(self, k):
        return self.props.get(str(k))


class _PropertyGroupCollection:
    """Behaves like the Java propertyGroup() factory.

    - Calling with no args returns this collection (which has create()).
    - Calling with a tag returns the named group, or raises if missing.
    - create(tag, descr) registers a new group.
    """
    def __init__(self, parent_material: "_Material"):
        self.parent = parent_material

    def create(self, tag, descr=None):
        # Spec/KB: two-arg form (tag, descr) is the canonical one.
        # We accept the single-arg fallback for parity with the helper.
        if tag in self.parent.groups:
            raise RuntimeError(f"propertyGroup '{tag}' already exists")
        self.parent.groups[tag] = _PropertyGroup(tag, descr or "")
        # Record the create-call shape so tests can assert two-arg usage.
        self.parent.create_calls.append(
            {"tag": tag, "descr": descr, "argc": 1 if descr is None else 2}
        )
        return self.parent.groups[tag]


class _Material:
    def __init__(self, tag: str):
        self._tag = tag
        self._label = ""
        self.groups: dict = {"def": _PropertyGroup("def", "Basic")}
        self.create_calls: list = []

    def tag(self):
        return self._tag

    def label(self, *args):
        if args:
            self._label = args[0]
        return self._label

    def propertyGroup(self, name=None):
        if name is None:
            return _PropertyGroupCollection(self)
        if name in self.groups:
            return self.groups[name]
        raise RuntimeError(f"propertyGroup '{name}' not found")

    def selection(self):
        sel = type("_Sel", (), {"set": lambda self, *a: None})()
        return sel


class _MaterialFactory:
    """Mirrors comp.material() factory: create(tag, type) and indexer."""
    def __init__(self):
        self._materials: dict = {}

    def __call__(self, name=None):
        # Behave as both a factory (no-arg returns self) and an indexer
        # (str arg returns the named material).
        if name is None:
            return self
        return self._materials[name]

    def __iter__(self):
        return iter(self._materials.values())

    def create(self, tag, mtype):
        m = _Material(tag)
        self._materials[tag] = m
        return m


class _Component:
    def __init__(self, tag: str = "comp1"):
        self._tag = tag
        self._material_factory = _MaterialFactory()

    def tag(self):
        return self._tag

    def material(self, name=None):
        if name is None:
            return self._material_factory
        return self._material_factory._materials[name]


class _ComponentFactory:
    def __init__(self):
        self._comp = _Component()

    def __call__(self, name=None):
        # Java API: jm.component() (no-arg) returns the ComponentList,
        # which is iterable; jm.component(<tag>) returns the named comp.
        if name is None:
            return self
        return self._comp

    def __iter__(self):
        return iter([self._comp])


class _Java:
    def __init__(self):
        self.component = _ComponentFactory()


class _Model:
    def __init__(self):
        self.java = _Java()
        self._name = "test_model"

    def name(self):
        return self._name


# ---------------------------------------------------------------------------
# KB fixture (mirrors test_material_kb.py shape)
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_material_kb(tmp_path, monkeypatch):
    """Build a minimal {KB}/materials/basic/Cast_iron.json with both 'def'
    and 'Enu' property groups, then point the tool at it."""
    json_dir = tmp_path / "materials" / "basic"
    json_dir.mkdir(parents=True)

    cast_iron = {
        "name": "Cast iron",
        "source_lib": "basic",
        "source_path": r"C:\fake\comsol_basic_material_lib.mph",
        "comsol_version": "6.1",
        "extracted_at": "2026-04-27T12:00:00",
        "propertyGroups": {
            "def": {
                "thermalconductivity": "52[W/(m*K)]",
                "density": "7150[kg/m^3]",
                "heatcapacity": "490[J/(kg*K)]",
            },
            "Enu": {
                "youngsmodulus": "1.0e11",
                "poissonsratio": "0.27",
            },
        },
    }
    (json_dir / "Cast_iron.json").write_text(
        json.dumps(cast_iron, indent=2), encoding="utf-8"
    )

    from src.tools import material as material_mod
    monkeypatch.setattr(
        material_mod, "MATERIALS_CATALOG", tmp_path / "catalogs" / "materials_catalog.csv"
    )
    monkeypatch.setattr(material_mod, "MATERIALS_JSON_DIR", tmp_path / "materials")
    return tmp_path


def _capture_material_tools():
    from src.tools.material import register_material_tools
    captured: dict = {}

    class _Stub:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    register_material_tools(_Stub())
    return captured


def test_material_create_from_kb_creates_enu_group(fake_material_kb, monkeypatch):
    """M2 regression: extra propertyGroups (e.g. 'Enu') from the KB record
    must end up actually populated on the material — no warnings, and the
    youngsmodulus / poissonsratio values must round-trip through the
    Java-side stub."""
    from src.tools.session import session_manager

    fake_model = _Model()
    monkeypatch.setattr(session_manager, "get_model", lambda name=None: fake_model)

    captured = _capture_material_tools()
    create_from_kb = captured["material_create_from_kb"]

    result = create_from_kb(kb_name="Cast iron")

    assert result["success"] is True, result
    # No 'extra_groups' warnings — every group from the KB record landed.
    warnings = result.get("warnings", {})
    assert "extra_groups" not in warnings, (
        f"unexpected extra_groups warnings: {warnings!r}"
    )

    # Read back via the Java stubs to confirm Enu was created and populated.
    comp = next(iter(fake_model.java.component()))
    mat = next(iter(comp.material()))
    assert "Enu" in mat.groups, (
        f"Enu propertyGroup was not created on material; "
        f"have: {list(mat.groups.keys())}"
    )
    enu = mat.groups["Enu"]
    assert enu.props.get("youngsmodulus") == "1.0e11"
    assert enu.props.get("poissonsratio") == "0.27"

    # Verify the create() call carried (tag, descr) per KB chunk 76973.
    enu_create = [c for c in mat.create_calls if c["tag"] == "Enu"]
    assert enu_create, "propertyGroup().create('Enu', ...) was never called"
    # The implementation prefers the (tag, descr) two-arg form. Allow the
    # single-arg fallback only if it actually succeeded; in our stub the
    # two-arg form succeeds so it should be used.
    assert enu_create[0]["argc"] == 2, (
        f"propertyGroup().create should be called with (tag, descr); "
        f"got argc={enu_create[0]['argc']}"
    )


def test_material_create_from_kb_def_group_populated(fake_material_kb, monkeypatch):
    """Sanity: the primary 'def' group is also populated (regression
    guard against the Enu fix accidentally breaking the default path)."""
    from src.tools.session import session_manager

    fake_model = _Model()
    monkeypatch.setattr(session_manager, "get_model", lambda name=None: fake_model)

    captured = _capture_material_tools()
    create_from_kb = captured["material_create_from_kb"]

    result = create_from_kb(kb_name="Cast iron")
    assert result["success"] is True, result

    comp = next(iter(fake_model.java.component()))
    mat = next(iter(comp.material()))
    def_group = mat.groups["def"]
    assert def_group.props.get("thermalconductivity") == "52[W/(m*K)]"
    assert def_group.props.get("density") == "7150[kg/m^3]"
    assert def_group.props.get("heatcapacity") == "490[J/(kg*K)]"
