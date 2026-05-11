"""Tests for the physics_set_material deprecation contract.

Spec: plans/mcp_set_material_deprecation_spec.md §3.1, §5.1.

The tool is advisory-only — it must not mutate the model — and its
response must announce that fact via ``deprecated: True`` plus a
``redirect_tool`` field pointing at ``material_assign_to_domain``.
The ``domain_selection`` argument is accepted but ignored, and that
fact must surface in ``validated.domain_selection_arg_ignored``.

Mock-based; mirrors the pattern in test_physics_set_property.py so no
live JVM is required.
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


def _build_fake_model_with(physics_names: tuple, material_names: tuple):
    """Build a fake model whose ``model.physics()`` and ``model.materials()``
    return dict-like objects keyed by the given names (the production code
    only uses ``in``-membership tests against the result).
    """
    fake_model = MagicMock()
    fake_model.physics.return_value = {n: object() for n in physics_names}
    fake_model.materials.return_value = {n: object() for n in material_names}
    return fake_model


def _get_tool():
    from src.tools.physics import register_physics_tools

    captured = _capture_register(register_physics_tools)
    return captured["physics_set_material"]


def test_set_material_returns_deprecated_flag(monkeypatch):
    """1/4 — Normal call returns deprecated=True, redirect_tool, and the
    advisory mentions 'no Java API mutation'."""
    fake_model = _build_fake_model_with(
        physics_names=("Heat Transfer in Solids",),
        material_names=("M1",),
    )
    _wire_fake_model(monkeypatch, fake_model)

    tool = _get_tool()
    r = tool(
        physics_name="Heat Transfer in Solids",
        material_name="M1",
    )
    assert r["success"] is True, r
    assert r["deprecated"] is True
    assert r["redirect_tool"] == "material_assign_to_domain"
    assert "no Java API mutation" in r["advisory"]
    assert r["physics"] == "Heat Transfer in Solids"
    assert r["material"] == "M1"
    assert r["validated"]["physics_exists"] is True
    assert r["validated"]["material_exists"] is True
    assert r["validated"]["domain_selection_arg_ignored"] is False


def test_set_material_ignores_domain_selection_arg(monkeypatch):
    """2/4 — When ``domain_selection`` is passed, the response must
    surface that the argument was ignored. Crucially, the tool must not
    call any selection-mutating Java method on the material — verified
    by the fact that no MagicMock side effect is recorded."""
    fake_model = _build_fake_model_with(
        physics_names=("Heat Transfer in Solids",),
        material_names=("M1",),
    )
    _wire_fake_model(monkeypatch, fake_model)

    tool = _get_tool()
    r = tool(
        physics_name="Heat Transfer in Solids",
        material_name="M1",
        domain_selection=[1],
    )
    assert r["success"] is True, r
    assert r["validated"]["domain_selection_arg_ignored"] is True
    # No mutation path should have touched the model's java bridge.
    assert not fake_model.java.method_calls, (
        f"physics_set_material must not mutate the model; "
        f"got java calls: {fake_model.java.method_calls!r}"
    )


def test_set_material_unknown_physics(monkeypatch):
    """3/4 — Missing physics keeps success=False (validation pre-flight
    is the only useful behaviour the tool retains)."""
    fake_model = _build_fake_model_with(
        physics_names=(),  # no physics
        material_names=("M1",),
    )
    _wire_fake_model(monkeypatch, fake_model)

    tool = _get_tool()
    r = tool(
        physics_name="NoSuchPhysics",
        material_name="M1",
    )
    assert r["success"] is False, r
    assert "not found" in r["error"].lower()


def test_set_material_unknown_material(monkeypatch):
    """4/4 — Missing material keeps success=False."""
    fake_model = _build_fake_model_with(
        physics_names=("Heat Transfer in Solids",),
        material_names=(),  # no materials
    )
    _wire_fake_model(monkeypatch, fake_model)

    tool = _get_tool()
    r = tool(
        physics_name="Heat Transfer in Solids",
        material_name="NoSuchMat",
    )
    assert r["success"] is False, r
    assert "not found" in r["error"].lower()


def test_docstring_starts_with_deprecated_marker():
    """Docstring first non-blank line begins with the deprecation marker
    so deferred-tool schemas surface the warning to caller agents at
    selection time (spec §3.1 note)."""
    tool = _get_tool()
    doc = (tool.__doc__ or "").strip().splitlines()
    first = next((ln.strip() for ln in doc if ln.strip()), "")
    assert first.startswith("[DEPRECATED"), (
        f"Expected deprecation marker on first docstring line; got {first!r}"
    )
