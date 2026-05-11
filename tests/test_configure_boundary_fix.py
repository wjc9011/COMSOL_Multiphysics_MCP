"""Regression tests for the ``physics_configure_boundary`` API path
fix — Pilot 07 + Pilot 08 carry-over.

Background: the pre-fix implementation went through

    physics_node = model / "physics" / physics_name
    bc_node = physics_node.create(boundary_condition)
    bc_node.property("selection", list(boundary_selection))

— a mph-wrapper path that COMSOL rejects with
``UnknownEntityException: Unknown parameter X#selection`` for many
feature classes (verified by ``test_radiation_bc_fix.py`` for
SurfaceToAmbientRadiation, and again by Pilot 08 for PointLoad).

The fix moves to the canonical Java path used everywhere else in
this codebase:

    physics.create(tag, type[, dim])     # 3-arg if selection_dim known
    bc.selection().set(int[])            # NOT bc.set('selection', ...)
                                         # NOT bc.property('selection',...)

These tests pin (1) the API path, and (2) the selection_dim plumbing
identical to ``physics_boundary_selection``. Same MagicMock pattern
as the radiation/point-selection tests — no real COMSOL/JVM needed.
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


def _build_fake_solid_model(sdim: int = 3):
    """Same fake-model pattern as test_point_selection_fix —
    Solid Mechanics with one component + one geometry."""
    recorder: dict = {
        "feature_creates": [],   # [(tag, type, dim_or_None), ...]
        "selection_sets": [],    # [(tag, [ints]), ...]
        "set_calls": [],         # [(tag, key, value), ...]
        "property_calls": [],    # [(tag, key, value), ...]  forbidden
    }

    def make_bc(tag: str):
        bc = MagicMock()

        def selection_set(values):
            recorder["selection_sets"].append((tag, list(values)))

        sel = MagicMock()
        sel.set.side_effect = selection_set
        bc.selection.return_value = sel

        def bc_set(key, value):
            recorder["set_calls"].append((tag, str(key), value))

        bc.set.side_effect = bc_set

        def bc_property(*args):
            # Pre-fix path called bc_node.property("selection", ...).
            # We record but DON'T return a usable value — calling this
            # was the bug.
            recorder["property_calls"].append(
                (tag, str(args[0]) if args else None,
                 args[1] if len(args) > 1 else None)
            )
            raise RuntimeError(
                "com.comsol.util.exceptions.UnknownEntityException: "
                "Unknown parameter X#selection"
            )

        bc.property.side_effect = bc_property
        bc.label = MagicMock()
        return bc

    physics = MagicMock()
    physics.label.return_value = "Solid Mechanics"
    physics.tag.return_value = "solid"

    def physics_create(*args):
        if len(args) == 3:
            tag, ftype, dim = args
            recorder["feature_creates"].append(
                (str(tag), str(ftype), int(dim))
            )
        else:
            tag, ftype = args
            recorder["feature_creates"].append(
                (str(tag), str(ftype), None)
            )
        return make_bc(str(tag))

    physics.create.side_effect = physics_create

    geom = MagicMock(spec=["sDim", "dimension", "tag", "label"])
    geom.sDim.return_value = sdim
    geom.dimension.return_value = sdim
    geom.tag.return_value = "geom1"
    geom.label.return_value = "Geometry 1"

    comp = MagicMock()
    comp.tag.return_value = "comp1"
    comp.physics.return_value = [physics]

    def comp_geom(*args):
        if args:
            return geom
        m = MagicMock()
        m.__iter__ = lambda self: iter([geom])
        return m

    comp.geom.side_effect = comp_geom

    jm = MagicMock()
    jm.component.return_value = [comp]

    fake_model = MagicMock()
    fake_model.java = jm
    fake_model.physics.return_value = {"Solid Mechanics": object()}

    return fake_model, recorder


def _get_tool():
    from src.tools.physics import register_physics_tools

    captured = _capture_register(register_physics_tools)
    return captured["physics_configure_boundary"]


def test_configure_boundary_uses_selection_set_not_property(monkeypatch):
    """Spec test 1 — the fixed implementation must route through
    ``bc.selection().set(int[])`` and MUST NOT call
    ``bc.property('selection', ...)``. The latter raises
    UnknownEntityException for many feature classes (the original
    Pilot 07 SurfaceToAmbientRadiation symptom)."""
    fake_model, rec = _build_fake_solid_model(sdim=3)
    _wire_fake_model(monkeypatch, fake_model)

    tool = _get_tool()
    result = tool(
        physics_name="Solid Mechanics",
        boundary_condition="Fixed",
        boundary_selection=[3, 4],
    )

    assert result["success"] is True, result

    # Canonical path was used.
    assert len(rec["selection_sets"]) == 1, rec["selection_sets"]
    tag, sel = rec["selection_sets"][0]
    assert sel == [3, 4]

    # Forbidden path NOT used. The fake's property() side_effect
    # raises if called — if the tool succeeded, it didn't go there.
    assert rec["property_calls"] == [], (
        f"physics_configure_boundary must NOT call bc.property("
        f"'selection', ...) — the canonical path is "
        f"bc.selection().set(int[]). Got property calls: "
        f"{rec['property_calls']!r}"
    )

    # Also no bc.set('selection', ...) — that's a different but
    # equally wrong wrapper-style path.
    sel_set_keys = [k for (_t, k, _v) in rec["set_calls"]]
    assert "selection" not in sel_set_keys, (
        "bc.set('selection', ...) is also wrong; the canonical "
        "path is bc.selection().set(int[])."
    )

    # Auto-inference for 3D geom: dim = sdim - 1 = 2 (boundaries).
    _ftag, ftype, dim = rec["feature_creates"][0]
    assert ftype == "Fixed"
    assert dim == 2
    assert result["boundary_condition"]["selection_dim"] == 2


def test_configure_boundary_explicit_selection_dim(monkeypatch):
    """Spec test 2 — explicit ``selection_dim=0`` for a PointLoad
    must use the 3-arg ``physics.create(tag, "PointLoad", 0)`` form
    so the selection lives at the point dim. This is the Pilot 08
    PointLoad path through ``physics_configure_boundary`` (some
    callers prefer this tool over the newer
    ``physics_boundary_selection`` for symmetry with existing
    Pilot 03/04/05 calls — both must support selection_dim)."""
    fake_model, rec = _build_fake_solid_model(sdim=3)
    _wire_fake_model(monkeypatch, fake_model)

    tool = _get_tool()
    result = tool(
        physics_name="Solid Mechanics",
        boundary_condition="PointLoad",
        boundary_selection=[1],
        selection_dim=0,
        properties={"Fp": "100[N]"},
    )

    assert result["success"] is True, result

    _ftag, ftype, dim = rec["feature_creates"][0]
    assert ftype == "PointLoad"
    assert dim == 0, (
        f"selection_dim=0 must use 3-arg physics.create(tag, type, 0); "
        f"got dim={dim!r}"
    )
    assert result["boundary_condition"]["selection_dim"] == 0

    # Property setter still flows through bc.set(key, value).
    assert any(
        k == "Fp" and v == "100[N]"
        for (_t, k, v) in rec["set_calls"]
    ), rec["set_calls"]
