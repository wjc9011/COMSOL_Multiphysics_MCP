"""Regression test for the 1D ``Edge`` operation auto-mapping fix.

Background (Cowork Pilot 07 v2 caveat E1, measured 2026-05-10):
``mesh_add_sequence`` on a 1D Interval geometry with
``default_operation_type=None`` silently fell through to the
``FreeTri`` default (sdim=2 dict lookup), which COMSOL then rejected
with ``Operation cannot be created in this context: FreeTri`` —
surfaced through the silent_exception channel rather than the response
``error`` so callers had to opt-in to ``default_operation_type="Edge"``
to get a working 1D mesh.

Root cause: ``_geom_sdim`` only tried lowercase ``sdim`` /
``geomDim`` / ``space_dimension`` accessors. The real COMSOL Java API
exposes ``GeomSequence.sDim()`` (camelCase D), and JPype is
case-sensitive — so the accessor was missed and ``_geom_sdim``
returned ``None``, falling back to sdim=2 (FreeTri).

Fix: ``_geom_sdim`` now tries ``sDim`` first (canonical Java) and
``dimension`` (mph wrapper alias) before the legacy aliases. This
test pins the 1D → Edge auto-mapping by using a ``spec=`` MagicMock
that only exposes ``sDim`` and ``dimension`` — the legacy lowercase
``sdim`` attribute is absent, mimicking the real JPype-bridged
GeomSequence.
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


def _build_fake_model_1d(expose_sdim_lowercase: bool = False):
    """1D-geometry fake. By default, only the canonical Java accessor
    ``sDim()`` (camelCase D) and the mph alias ``dimension()`` are
    exposed — the legacy lowercase ``sdim`` is absent, mimicking real
    JPype behavior."""
    if expose_sdim_lowercase:
        spec = ["sDim", "sdim", "dimension", "tag", "label"]
    else:
        spec = ["sDim", "dimension", "tag", "label"]

    geom_obj = MagicMock(spec=spec)
    geom_obj.sDim.return_value = 1
    geom_obj.dimension.return_value = 1
    if expose_sdim_lowercase:
        geom_obj.sdim.return_value = 1
    geom_obj.tag.return_value = "geom1"
    geom_obj.label.return_value = "Geometry 1"

    mesh_seq = MagicMock()
    op_create_calls: list = []
    op_obj = MagicMock()
    op_create_subcalls: list = []

    def mesh_seq_create(ftag, ftype):
        op_create_calls.append((str(ftag), str(ftype)))
        return op_obj

    mesh_seq.create.side_effect = mesh_seq_create
    mesh_seq.label.return_value = "Mesh 1"

    op_obj.create.side_effect = lambda ftag, ftype: (
        op_create_subcalls.append((str(ftag), str(ftype)))
        or MagicMock()
    )
    op_obj.selection.return_value = MagicMock()
    op_obj._subcreates = op_create_subcalls

    mesh_factory = MagicMock()
    mesh_factory.create.return_value = mesh_seq
    mesh_factory.__iter__ = lambda self: iter([])

    comp = MagicMock()
    comp.tag.return_value = "comp1"
    comp.geom.side_effect = (
        lambda *args: geom_obj if args else MagicMock(
            __iter__=lambda self: iter([geom_obj])
        )
    )
    comp.mesh.side_effect = lambda *args: mesh_factory if not args else None

    jm = MagicMock()

    def jm_component(*args):
        if args:
            return comp
        m = MagicMock()
        m.__iter__ = lambda self: iter([comp])
        return m

    jm.component.side_effect = jm_component

    fake_model = MagicMock()
    fake_model.java = jm
    return fake_model, mesh_seq, op_create_calls, op_obj


def test_mesh_add_sequence_auto_edge_for_1d(monkeypatch):
    """Spec — 1D Interval geometry + ``default_operation_type=None``
    must auto-map to ``Edge``. Pre-fix this silently fell through to
    ``FreeTri`` (sdim=2 dict fallback) and COMSOL raised "Operation
    cannot be created in this context: FreeTri".
    """
    from src.tools.mesh import register_mesh_tools

    captured = _capture_register(register_mesh_tools)
    mesh_add_sequence = captured["mesh_add_sequence"]

    fake_model, _mesh_seq, op_create_calls, _op_obj = (
        _build_fake_model_1d(expose_sdim_lowercase=False)
    )
    _wire_fake_model(monkeypatch, fake_model)

    result = mesh_add_sequence(auto_default_features=True)

    assert result["success"] is True, result
    default_op = result["mesh"]["default_operation"]
    assert default_op is not None, (
        "default_operation must be populated when auto_default_features=True"
    )
    assert default_op["type"] == "Edge", (
        f"sdim=1 must auto-map to Edge, got {default_op!r}. "
        "If this is 'FreeTri', _geom_sdim() failed to detect sdim=1 "
        "and fell through to the sdim=2 default — exactly the Pilot "
        "07 v2 caveat E1 regression."
    )
    assert default_op["tag"].startswith("edg"), (
        f"Edge operation tag should follow GUI convention 'edg*', "
        f"got {default_op['tag']!r}"
    )

    # The very first feature created on the mesh sequence must be the
    # Edge operation. Without it, COMSOL has no operation to drive
    # cell creation on the 1D interval.
    assert op_create_calls, "no create() calls on mesh sequence"
    first_ftag, first_ftype = op_create_calls[0]
    assert first_ftype == "Edge", (
        f"first feature on 1D mesh sequence should be Edge, "
        f"got {first_ftype!r}"
    )
    assert first_ftag.startswith("edg")

    # silent_exception must be None — the pre-fix code would have
    # tried to create FreeTri here and recorded the COMSOL rejection.
    assert result["mesh"].get("silent_exception") is None, (
        f"silent_exception must be None for a successful 1D Edge "
        f"create; got {result['mesh'].get('silent_exception')!r}. "
        "Non-None here means _geom_sdim picked the wrong op_type."
    )


def test_mesh_add_sequence_1d_still_works_with_legacy_sdim_accessor(
    monkeypatch,
):
    """Belt-and-suspenders: even if a future / older bridge exposes
    BOTH ``sDim`` and the legacy lowercase ``sdim``, the 1D detection
    must still resolve to Edge (not silently prefer one over the
    other in a way that breaks)."""
    from src.tools.mesh import register_mesh_tools

    captured = _capture_register(register_mesh_tools)
    mesh_add_sequence = captured["mesh_add_sequence"]

    fake_model, _mesh_seq, op_create_calls, _op_obj = (
        _build_fake_model_1d(expose_sdim_lowercase=True)
    )
    _wire_fake_model(monkeypatch, fake_model)

    result = mesh_add_sequence(auto_default_features=True)

    assert result["success"] is True, result
    assert result["mesh"]["default_operation"]["type"] == "Edge"
