"""Regression tests for ``physics_boundary_selection`` selection_dim
plumbing — Pilot 08 Solid Mechanics fix.

Background: Pilot 08 (comsol_12681_force, PARTIAL_BLOCKED) measured a
PointLoad add against a 3D Solid Mechanics interface failing because
``physics.create(tag, "PointLoad")`` (2-arg form) defaults the
selection dim to the geometry boundary dim (sdim - 1 = 2 for a 3D
geom), and COMSOL then rejects point indices on a face-dim selection.

The ground-truth Java exports for these models always use the 3-arg
``physics.create(tag, type, dim)`` form (KB ProgrammingReferenceManual
chunk 77014) with an explicit dim — so the fix adds a
``selection_dim`` argument plus auto-inference of ``sdim - 1`` (the
boundary dim, the natural default for boundary BCs like Fixed,
BoundaryLoad, HeatFluxBoundary).

These tests pin both branches without booting a real COMSOL/JVM by
mocking the Java handle and recording the create + selection() call
sequence — same pattern used in ``test_radiation_bc_fix.py`` and
``test_mesh_1d_edge_auto.py``.
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
    """Build a fake Solid Mechanics model with one component, one
    geometry of the given sdim, and one ``solid`` physics interface.

    Returns ``(fake_model, recorder)`` where ``recorder`` captures
    ``feature_creates`` (the args passed to ``physics.create`` —
    either 2-arg or 3-arg) and ``selection_sets`` (the int[] passed
    to ``bc.selection().set``).
    """
    recorder: dict = {
        "feature_creates": [],   # [(tag, type, dim_or_None), ...]
        "selection_sets": [],    # [(tag, [ints]), ...]
        "set_calls": [],         # [(tag, key, value), ...]
        "labels": [],            # [(tag, label), ...]
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

        def bc_label(text):
            recorder["labels"].append((tag, text))

        bc.label.side_effect = bc_label
        return bc

    physics = MagicMock()
    physics.label.return_value = "Solid Mechanics"
    physics.tag.return_value = "solid"

    def physics_create(*args):
        # Both 2-arg create(tag, type) and 3-arg create(tag, type, dim)
        # land here. Record dim=None for the 2-arg path.
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
        # comp.geom() returns iterable of geom; comp.geom(tag) returns
        # the geom with that tag.
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
    return captured["physics_boundary_selection"]


def test_point_selection_explicit_dim0_uses_3arg_create(monkeypatch):
    """Spec test 1 — caller passes ``selection_dim=0`` for a PointLoad
    on a 3D geom. The tool must use the 3-arg
    ``physics.create(tag, "PointLoad", 0)`` form (KB
    ProgrammingReferenceManual chunk 77014) so the selection lives at
    the point dim, then ``bc.selection().set([1, 2])``.

    Pre-fix the 2-arg create defaulted dim to the geom boundary dim
    (sdim-1 = 2 for 3D), and COMSOL rejected point indices."""
    fake_model, rec = _build_fake_solid_model(sdim=3)
    _wire_fake_model(monkeypatch, fake_model)

    tool = _get_tool()
    result = tool(
        physics_name="Solid Mechanics",
        boundary_condition_type="PointLoad",
        boundary_numbers=[1, 2],
        selection_dim=0,
        properties={"Fp": "100[N]"},
    )

    assert result["success"] is True, result

    # 3-arg create with explicit dim=0.
    assert len(rec["feature_creates"]) == 1, rec["feature_creates"]
    tag, ftype, dim = rec["feature_creates"][0]
    assert ftype == "PointLoad"
    assert dim == 0, (
        f"selection_dim=0 must trigger 3-arg physics.create(tag, type, 0), "
        f"got dim={dim!r}. The 2-arg create would default to the geom "
        f"boundary dim and COMSOL would reject point indices."
    )

    assert (tag, [1, 2]) in rec["selection_sets"], rec["selection_sets"]
    assert (tag, "Fp", "100[N]") in rec["set_calls"], rec["set_calls"]
    assert result["boundary_condition"]["selection_dim"] == 0


def test_boundary_default_autoinfers_sdim_minus_1_3d(monkeypatch):
    """Spec test 2 — caller does NOT pass selection_dim. On a 3D
    geometry the tool must auto-infer ``sdim - 1 = 2`` and call the
    3-arg create with dim=2. This matches the natural dim of
    boundary BCs like Fixed / BoundaryLoad / HeatFluxBoundary."""
    fake_model, rec = _build_fake_solid_model(sdim=3)
    _wire_fake_model(monkeypatch, fake_model)

    tool = _get_tool()
    result = tool(
        physics_name="Solid Mechanics",
        boundary_condition_type="Fixed",
        boundary_numbers=[5, 6],
    )

    assert result["success"] is True, result
    tag, ftype, dim = rec["feature_creates"][0]
    assert ftype == "Fixed"
    assert dim == 2, (
        f"3D geom + selection_dim=None must auto-infer dim=2 (boundaries), "
        f"got dim={dim!r}"
    )
    assert (tag, [5, 6]) in rec["selection_sets"]
    assert result["boundary_condition"]["selection_dim"] == 2


def test_boundary_default_autoinfers_sdim_minus_1_2d(monkeypatch):
    """Spec test 3 — same auto-inference for a 2D geom: ``sdim - 1 = 1``
    (edges are the boundary dim in 2D)."""
    fake_model, rec = _build_fake_solid_model(sdim=2)
    _wire_fake_model(monkeypatch, fake_model)

    tool = _get_tool()
    result = tool(
        physics_name="Solid Mechanics",
        boundary_condition_type="BoundaryLoad",
        boundary_numbers=[3],
        properties={"FAx": "1e3[N/m^2]"},
    )

    assert result["success"] is True, result
    tag, ftype, dim = rec["feature_creates"][0]
    assert ftype == "BoundaryLoad"
    assert dim == 1, (
        f"2D geom + selection_dim=None must auto-infer dim=1 (edges = "
        f"boundary dim), got dim={dim!r}"
    )
    assert result["boundary_condition"]["selection_dim"] == 1


def test_explicit_dim_overrides_autoinference(monkeypatch):
    """Spec test 4 — caller's explicit ``selection_dim=1`` (edges)
    must override the geom-based auto-inference (which would be 2 on
    a 3D geom). This is the EdgeLoad path on a 3D Solid Mechanics
    model."""
    fake_model, rec = _build_fake_solid_model(sdim=3)
    _wire_fake_model(monkeypatch, fake_model)

    tool = _get_tool()
    result = tool(
        physics_name="Solid Mechanics",
        boundary_condition_type="EdgeLoad",
        boundary_numbers=[7, 8, 9],
        selection_dim=1,
    )

    assert result["success"] is True, result
    tag, ftype, dim = rec["feature_creates"][0]
    assert ftype == "EdgeLoad"
    assert dim == 1, (
        f"explicit selection_dim=1 must override the sdim-1=2 default; "
        f"got dim={dim!r}. If this is 2, the auto-inference path is "
        f"shadowing the explicit caller value."
    )
    assert (tag, [7, 8, 9]) in rec["selection_sets"]
    assert result["boundary_condition"]["selection_dim"] == 1
