"""Regression tests for the new ``physics_set_property`` MCP tool —
Pilot 08 Solid Mechanics fix.

Background: Pilot 08 (comsol_12681_force) needed to set Solid
Mechanics interface-level scalar properties — 2D out-of-plane
thickness ``d``, reference temperature ``Tref``, equation form, etc.
None of the existing physics_* tools could express the canonical
Java path:

    model.component(<ctag>).physics(<tag>)
         .prop(<group>).set(<key>, <value>);

(KB ProgrammingReferenceManual chunk 77014). The new tool wraps
that path and surfaces the underlying Java exception when the
group/key combo is unknown for this COMSOL version.

Tests use the same MagicMock pattern as ``test_radiation_bc_fix.py``
and ``test_point_selection_fix.py`` — no real COMSOL/JVM needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.tools.session import session_manager


def _capture_register(register_fn):
    captured: dict = {}

    class _Stub:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    register_fn(_Stub())
    return captured


def _wire_fake_model(monkeypatch, fake_model):
    monkeypatch.setattr(
        session_manager, "get_model",
        lambda name=None: fake_model,
    )


def _build_fake_solid_model(prop_set_raises_for: tuple = ()):
    """Build a fake Solid Mechanics model wired so we can record every
    ``physics.prop(group).set(key, value)`` call.

    ``prop_set_raises_for`` is a tuple of (group, key) pairs that
    should raise an UnknownEntityException-like error — used to verify
    that the tool surfaces the Java error in its response.
    """
    recorder: dict = {
        "prop_set_calls": [],   # [(group, key, value), ...]
        "prop_groups_accessed": [],   # [group, ...]
    }
    raise_set = set(prop_set_raises_for)

    def make_prop_group(group_name: str):
        prop_group = MagicMock()

        def prop_set(key, value):
            recorder["prop_set_calls"].append(
                (group_name, str(key), value)
            )
            if (group_name, str(key)) in raise_set:
                raise RuntimeError(
                    f"com.comsol.util.exceptions.UnknownEntityException: "
                    f"Unknown parameter X#{key}"
                )

        prop_group.set.side_effect = prop_set
        return prop_group

    physics = MagicMock()
    physics.label.return_value = "Solid Mechanics"
    physics.tag.return_value = "solid"

    def physics_prop(group):
        recorder["prop_groups_accessed"].append(str(group))
        return make_prop_group(str(group))

    physics.prop.side_effect = physics_prop

    comp = MagicMock()
    comp.tag.return_value = "comp1"
    comp.physics.return_value = [physics]

    jm = MagicMock()
    jm.component.return_value = [comp]

    fake_model = MagicMock()
    fake_model.java = jm
    fake_model.physics.return_value = {"Solid Mechanics": object()}

    return fake_model, recorder


def _get_tool():
    from src.tools.physics import register_physics_tools

    captured = _capture_register(register_physics_tools)
    return captured["physics_set_property"]


def test_set_thickness_2d_solid_mechanics(monkeypatch):
    """Spec test 1 — set 2D out-of-plane thickness via the canonical
    ``physics.prop("d").set("d", "1[m]")`` path. Pilot 08 needed
    this to match the ground-truth Java for plane-stress / plane-
    strain models."""
    fake_model, rec = _build_fake_solid_model()
    _wire_fake_model(monkeypatch, fake_model)

    tool = _get_tool()
    result = tool(
        physics_name="Solid Mechanics",
        property_group="d",
        property_name="d",
        value="1[m]",
    )

    assert result["success"] is True, result
    assert ("d", "d", "1[m]") in rec["prop_set_calls"], (
        f"expected physics.prop('d').set('d', '1[m]'); "
        f"got {rec['prop_set_calls']!r}"
    )
    assert result["property_group"] == "d"
    assert result["property_name"] == "d"
    assert result["value"] == "1[m]"


def test_set_reference_temperature(monkeypatch):
    """Spec test 2 — set the reference temperature for thermal
    expansion. Same canonical prop().set() path with a different
    group/key. Verifies the tool is generic across Solid Mechanics
    properties (not hard-coded to thickness)."""
    fake_model, rec = _build_fake_solid_model()
    _wire_fake_model(monkeypatch, fake_model)

    tool = _get_tool()
    result = tool(
        physics_name="Solid Mechanics",
        property_group="Tref",
        property_name="Tref",
        value="293.15[K]",
    )

    assert result["success"] is True, result
    assert ("Tref", "Tref", "293.15[K]") in rec["prop_set_calls"]
    assert "Tref" in rec["prop_groups_accessed"]


def test_unknown_property_surfaces_java_error(monkeypatch):
    """Spec test 3 — if the (group, key) combination is unknown for
    this COMSOL version, the underlying Java exception must surface
    in the response ``error`` field with both the group and key
    interpolated. Without this, callers get a silent no-op and have
    no way to debug a typo (Pilot 08 v1 silent_exception lesson)."""
    fake_model, rec = _build_fake_solid_model(
        prop_set_raises_for=(("ShapeProperty", "frobnicate"),)
    )
    _wire_fake_model(monkeypatch, fake_model)

    tool = _get_tool()
    result = tool(
        physics_name="Solid Mechanics",
        property_group="ShapeProperty",
        property_name="frobnicate",
        value=2,
    )

    assert result["success"] is False, result
    err = result["error"]
    assert "ShapeProperty" in err, err
    assert "frobnicate" in err, err
    assert "Unknown parameter" in err or "UnknownEntity" in err, err
