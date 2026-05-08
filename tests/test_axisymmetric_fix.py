"""Regression tests for the PR-C axisymmetric fix.

Spec: plans/mcp_pr_c_fix_spec.md §3.1, §3.2, §6.1.

These tests do NOT boot a real COMSOL/JVM. They use a fake Java bridge
to verify the call sequence on ``geometry_create`` and that
``model_create_component`` no longer leaks the dropped ``space_dim_kind``
fields into the response. The 3D + axisymmetric=True rejection is pure
Python logic and runs without any mock at all.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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


def test_geometry_create_2d_axisymmetric(monkeypatch):
    from src.tools.geometry import register_geometry_tools

    captured = _capture_register(register_geometry_tools)
    geometry_create = captured["geometry_create"]

    geom_obj = MagicMock()
    geom_obj.tag.return_value = "geom1"

    geom_factory = MagicMock()
    geom_factory.create.return_value = geom_obj

    geom_named = MagicMock()
    axisym_setter = MagicMock()
    geom_named.axisymmetric = axisym_setter

    comp = MagicMock()
    comp.geom.side_effect = lambda *args: geom_factory if not args else geom_named
    comp.tag.return_value = "comp1"

    jm = MagicMock()
    jm.component.return_value = comp

    fake_model = MagicMock()
    fake_model.java = jm
    _wire_fake_model(monkeypatch, fake_model)

    result = geometry_create(
        geometry_name="geom1",
        space_dimension=2,
        axisymmetric=True,
        component_name="comp1",
    )

    assert result["success"] is True, result
    assert result["axisymmetric"] is True
    assert result["space_dimension"] == 2
    assert result["tag"] == "geom1"
    geom_factory.create.assert_called_once_with("geom1", 2)
    axisym_setter.assert_called_once_with(True)


def test_geometry_create_3d_axisymmetric_rejected(monkeypatch):
    """axisymmetric=True with 3D must return an error WITHOUT touching
    the Java bridge (spec §3.2: 1D/2D only)."""
    from src.tools.geometry import register_geometry_tools

    captured = _capture_register(register_geometry_tools)
    geometry_create = captured["geometry_create"]

    fake_model = MagicMock()
    java_calls: list = []
    fake_model.java.component.side_effect = lambda *a: java_calls.append(a) or MagicMock()
    _wire_fake_model(monkeypatch, fake_model)

    result = geometry_create(
        space_dimension=3,
        axisymmetric=True,
    )

    assert result["success"] is False
    err = result["error"]
    assert "1, 2" in err or "1D" in err or "1/2" in err.lower() or "1d/2d" in err.lower()
    # Must reject before any Java call (validation guard runs first).
    assert java_calls == [], (
        "geometry_create should reject axisymmetric+3D before "
        "touching the Java API"
    )


def test_geometry_create_no_strict_dim_check_kwarg(monkeypatch):
    """The dropped ``strict_dim_check`` kwarg must no longer be accepted
    (spec §3.2: arg removed entirely)."""
    from src.tools.geometry import register_geometry_tools

    captured = _capture_register(register_geometry_tools)
    geometry_create = captured["geometry_create"]

    fake_model = MagicMock()
    _wire_fake_model(monkeypatch, fake_model)

    with pytest.raises(TypeError):
        geometry_create(strict_dim_check=False)


def test_model_create_component_no_space_dim_kind(monkeypatch):
    """model_create_component response must not contain the removed
    space_dim_kind / java_kind_string / space_dimension_int fields
    (spec §3.1)."""
    from src.tools.model import register_model_tools

    captured = _capture_register(register_model_tools)
    model_create_component = captured["model_create_component"]

    fake_model = MagicMock()
    fake_model.name.return_value = "test_model"
    fake_model.java.component.return_value.create.return_value = MagicMock()
    _wire_fake_model(monkeypatch, fake_model)

    result = model_create_component(component_name="c1")

    assert result["success"] is True, result
    component = result["component"]
    assert component["tag"] == "c1"
    assert component["set_active"] is True
    # Removed in spec §3.1.
    assert "space_dim_kind" not in component
    assert "java_kind_string" not in component
    assert "space_dimension_int" not in component


def test_model_create_component_rejects_space_dim_kind_kwarg(monkeypatch):
    """The dropped ``space_dim_kind`` kwarg must no longer be accepted
    (spec §3.1: arg removed entirely)."""
    from src.tools.model import register_model_tools

    captured = _capture_register(register_model_tools)
    model_create_component = captured["model_create_component"]

    fake_model = MagicMock()
    _wire_fake_model(monkeypatch, fake_model)

    with pytest.raises(TypeError):
        model_create_component(component_name="c1", space_dim_kind="2D-Axisymmetric")
