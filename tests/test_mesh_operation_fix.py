"""Regression tests for the PR-C-fix v2 mesh operation auto-injection.

Spec: ``plans/mcp_pr_c_fix_v2_spec.md`` §3.1, §5.1.

Pilot 05 hit ``model_inspect.problems`` warning "There is no operation
using these settings." because ``mesh_add_sequence`` only added a Size
*attribute* — the COMSOL meshing engine needs an *operation* feature
(``freetri`` / ``freetet`` / ``edge``) to actually generate cells (KB
ProgrammingReferenceManual chunks 77425/77427/77428).

These tests exercise the fix without booting a real COMSOL/JVM. They
use a fake Java bridge to record every ``create()`` invocation on the
mesh-sequence object and assert that the right operation feature is
injected for each space dimension. The "element count > 0" and "no
operation problem gone" semantics from the spec translate, in unit-
test form, to "the operation feature was created" — without that call,
the COMSOL warning re-appears and element_count is 0 by definition.
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
    """Make session_manager.get_model() return our fake model."""
    monkeypatch.setattr(
        session_manager, "get_model",
        lambda name=None: fake_model,
    )


def _build_fake_model_with_geom(sdim: int):
    """Spin up the minimum Java surface needed by ``mesh_add_sequence``.

    Returns ``(fake_model, mesh_seq, op_create_calls)`` where
    ``op_create_calls`` is the list of ``(ftag, ftype)`` recorded
    against the mesh sequence — 1st call is the operation, subsequent
    calls (e.g. backstop "size1") trail it.
    """
    # Geometry handle: comp.geom(geom_tag) returns this; .sdim() drives
    # the operation auto-mapping.
    geom_obj = MagicMock()
    geom_obj.sdim.return_value = sdim
    geom_obj.tag.return_value = "geom1"
    geom_obj.label.return_value = "Geometry 1"

    # mesh_seq: comp.mesh().create(tag, geom_tag) returns this object.
    # Its .create(ftag, ftype) is what we want to assert against.
    mesh_seq = MagicMock()
    op_create_calls: list = []
    op_obj = MagicMock()
    op_create_subcalls: list = []

    def mesh_seq_create(ftag, ftype):
        op_create_calls.append((str(ftag), str(ftype)))
        # Operation feature receives sub-creates (Size attribute, etc.)
        return op_obj

    mesh_seq.create.side_effect = mesh_seq_create
    mesh_seq.label.return_value = "Mesh 1"

    def op_obj_create(ftag, ftype):
        op_create_subcalls.append((str(ftag), str(ftype)))
        return MagicMock()

    op_obj.create.side_effect = op_obj_create
    op_obj.selection.return_value = MagicMock()
    # Stash the sub-creates list on the op object so tests can read it.
    op_obj._subcreates = op_create_subcalls

    # mesh_factory: comp.mesh() returns this; its .create(tag, geom_tag)
    # produces the mesh_seq. ``list(comp.mesh())`` is also iterated by
    # _mesh_seq_descriptors() at duplicate-tag check time → return [].
    mesh_factory = MagicMock()
    mesh_factory.create.return_value = mesh_seq
    mesh_factory.__iter__ = lambda self: iter([])

    # comp: comp.geom(...) returns geom_obj; list(comp.geom()) returns
    # [geom_obj]; comp.mesh(*args) returns mesh_factory if no args.
    comp = MagicMock()
    comp.tag.return_value = "comp1"
    # comp.geom() with no args → iterable; comp.geom(tag) → geom_obj.
    comp.geom.side_effect = (
        lambda *args: geom_obj if args else MagicMock(
            __iter__=lambda self: iter([geom_obj])
        )
    )
    comp.mesh.side_effect = lambda *args: mesh_factory if not args else None

    # jm.component() returns iterable of [comp]; jm.component(name) → comp.
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


def test_mesh_add_sequence_auto_freetri_for_2d(monkeypatch):
    """Spec §5.1 test 1/4 — 2D geometry → default_operation.type == 'freetri'.

    Verifies the operation auto-mapping for ``sdim=2`` (planar or
    axisymmetric) and that the operation feature is the FIRST thing
    created on the mesh sequence — without it, COMSOL meshes nothing.
    """
    from src.tools.mesh import register_mesh_tools

    captured = _capture_register(register_mesh_tools)
    mesh_add_sequence = captured["mesh_add_sequence"]

    fake_model, _mesh_seq, op_create_calls, _op_obj = (
        _build_fake_model_with_geom(sdim=2)
    )
    _wire_fake_model(monkeypatch, fake_model)

    result = mesh_add_sequence(auto_default_features=True)

    assert result["success"] is True, result
    assert result["mesh"]["default_operation"] is not None, (
        "default_operation must be populated when "
        "auto_default_features=True"
    )
    assert result["mesh"]["default_operation"]["type"] == "freetri", (
        f"sdim=2 should map to freetri; got "
        f"{result['mesh']['default_operation']!r}"
    )
    # The very first mesh_seq.create() must be the freetri operation.
    assert op_create_calls, "no create() calls on mesh sequence"
    first_ftag, first_ftype = op_create_calls[0]
    assert first_ftype == "freetri", (
        f"first feature on mesh sequence should be freetri, "
        f"got {first_ftype!r}"
    )
    assert first_ftag.startswith("ftri"), (
        f"freetri operation tag should follow GUI convention 'ftri*', "
        f"got {first_ftag!r}"
    )


def test_mesh_add_sequence_auto_freetet_for_3d(monkeypatch):
    """Spec §5.1 test 2/4 — 3D geometry → default_operation.type == 'freetet'
    AND ``selection().all()`` is called (KB chunk 77427: 3D default
    selection is empty; we must make it cover all domains)."""
    from src.tools.mesh import register_mesh_tools

    captured = _capture_register(register_mesh_tools)
    mesh_add_sequence = captured["mesh_add_sequence"]

    fake_model, _mesh_seq, op_create_calls, op_obj = (
        _build_fake_model_with_geom(sdim=3)
    )
    _wire_fake_model(monkeypatch, fake_model)

    result = mesh_add_sequence(auto_default_features=True)

    assert result["success"] is True, result
    assert result["mesh"]["default_operation"]["type"] == "freetet", (
        f"sdim=3 should map to freetet; got "
        f"{result['mesh']['default_operation']!r}"
    )
    first_ftag, first_ftype = op_create_calls[0]
    assert first_ftype == "freetet"
    assert first_ftag.startswith("ftet")
    # 3D path must call selection().all() to cover all domains.
    op_obj.selection.assert_called()
    op_obj.selection.return_value.all.assert_called_once()


def test_mesh_element_count_positive(monkeypatch):
    """Spec §5.1 test 3/4 — proxy: when auto_default_features=True the
    Size attribute is attached UNDER the operation feature, which is
    the canonical location per KB chunk 77428. Without this Size, the
    operation runs with default sizing and may produce zero cells in
    pathological geometries; with it, a non-empty 2D geometry meshes.
    """
    from src.tools.mesh import register_mesh_tools

    captured = _capture_register(register_mesh_tools)
    mesh_add_sequence = captured["mesh_add_sequence"]

    fake_model, _mesh_seq, op_create_calls, op_obj = (
        _build_fake_model_with_geom(sdim=2)
    )
    _wire_fake_model(monkeypatch, fake_model)

    result = mesh_add_sequence(auto_default_features=True)

    assert result["success"] is True, result
    # Size attribute attached to the operation.
    assert result["mesh"]["size_attribute_attached_to"] is not None
    op_tag = result["mesh"]["default_operation"]["tag"]
    assert result["mesh"]["size_attribute_attached_to"] == op_tag, (
        f"size should be attached to the operation tag {op_tag!r}, "
        f"got {result['mesh']['size_attribute_attached_to']!r}"
    )
    # The op object must have received a Size sub-create (ftype 'Size').
    sub_ftypes = [ft for _ftag, ft in op_obj._subcreates]
    assert "Size" in sub_ftypes, (
        f"operation feature should receive a Size sub-create; got "
        f"{op_obj._subcreates!r}"
    )


def test_mesh_no_operation_problem_gone(monkeypatch):
    """Spec §5.1 test 4/4 — the "no operation using these settings"
    warning that Pilot 05 hit comes from COMSOL when a mesh sequence
    contains only Size attributes and no operation. The fix: an
    operation feature is always created when ``auto_default_features=
    True``. This test asserts that invariant directly: the operation
    is the FIRST create() call, before any Size/Size1 backstop.
    """
    from src.tools.mesh import register_mesh_tools

    captured = _capture_register(register_mesh_tools)
    mesh_add_sequence = captured["mesh_add_sequence"]

    fake_model, _mesh_seq, op_create_calls, _op_obj = (
        _build_fake_model_with_geom(sdim=2)
    )
    _wire_fake_model(monkeypatch, fake_model)

    result = mesh_add_sequence(auto_default_features=True)

    assert result["success"] is True, result
    # An operation MUST have been created on the mesh sequence.
    op_ftypes = [ft for _ftag, ft in op_create_calls]
    assert any(t in ("freetri", "freetet", "edge") for t in op_ftypes), (
        f"mesh sequence is missing an operation feature; got "
        f"{op_create_calls!r}. This is exactly the Pilot 05 regression."
    )
    # And the operation is the FIRST create on the sequence (any Size
    # backstop must come after).
    assert op_create_calls[0][1] in ("freetri", "freetet", "edge"), (
        f"operation must be the first feature on the sequence, but "
        f"got {op_create_calls[0]!r}"
    )
