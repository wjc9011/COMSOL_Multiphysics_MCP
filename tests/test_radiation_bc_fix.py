"""Regression tests for the surface-to-ambient radiation BC fix.

Background: Pilot 07 (comsol_266) was BLOCKED because
``physics_configure_boundary("SurfaceToAmbientRadiation", [2])`` raised
"Unknown parameter X#selection". The mph wrapper's
``bc_node.property("selection", list)`` path is wrong for this feature —
the canonical Java API is ``bc.selection().set(int[])``. The KB
``scripting_completion_text/physics.md`` confirms the feature class id
is ``SurfaceToAmbientRadiation`` (short id ``sar``) under the ``ht``
interface, and a live probe (probe_sar.java export) confirms the
canonical create line:

    model.component("comp1").physics("ht").feature()
        .create("sar1", "SurfaceToAmbientRadiation");

This test exercises the new ``radiation_boundaries`` arg of
``physics_setup_heat_boundaries`` without booting a real COMSOL/JVM.
It uses MagicMock to record the Java call sequence, mirroring the
pattern in ``test_mesh_operation_fix.py``.

Per spec, on a successful path:
  1. ``physics.create('sar1', 'SurfaceToAmbientRadiation')`` is called
  2. ``bc.selection().set([2])`` is called  (NOT bc.set('selection',…))
  3. ``bc.set('Tamb',  '300[K]')`` is called
  4. ``bc.set('epsilon_rad', '0.98')`` is called
  5. ``configured_boundaries.radiation`` includes the per-boundary entry
  6. each entry has ``silent_exception`` (None on success) — PR-C-fix v2
     pattern, commit 00330d0 — so future property-name drift surfaces
     the exact error rather than aborting the whole tool.
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
    monkeypatch.setattr(
        session_manager, "get_model",
        lambda name=None: fake_model,
    )


def _build_fake_ht_model(set_raises_for: tuple[str, ...] = ()):
    """Build a fake HT model with one component and one ``ht`` physics.

    ``set_raises_for`` is a tuple of property names that should raise
    UnknownEntityException-like errors when ``bc.set(name, value)`` is
    called — used to verify the silent_exception capture.

    Returns ``(fake_model, recorder)`` where ``recorder`` is a dict with
    ``feature_creates``, ``selection_sets``, ``set_calls`` lists for
    assertions.
    """
    recorder: dict = {
        "feature_creates": [],   # [(tag, type), ...]
        "selection_sets": [],    # [int[], ...]
        "set_calls": [],         # [(tag, key, value), ...]
        "set_raises_for": set(set_raises_for),
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
            if key in recorder["set_raises_for"]:
                raise RuntimeError(
                    f"com.comsol.util.exceptions.UnknownEntityException: "
                    f"Unknown parameter X#{key}"
                )

        bc.set.side_effect = bc_set

        def bc_label(text):
            recorder["labels"].append((tag, text))

        bc.label.side_effect = bc_label
        return bc

    physics = MagicMock()
    physics.label.return_value = "Heat Transfer in Solids"
    physics.tag.return_value = "ht"

    def physics_create(tag, ftype):
        recorder["feature_creates"].append((str(tag), str(ftype)))
        return make_bc(str(tag))

    physics.create.side_effect = physics_create

    comp = MagicMock()
    comp.tag.return_value = "comp1"
    comp.physics.return_value = [physics]

    jm = MagicMock()
    jm.component.return_value = [comp]

    fake_model = MagicMock()
    fake_model.java = jm
    # model.physics() returns a dict-like containing the physics name
    # the tool checks against (matches the existing implementation).
    fake_model.physics.return_value = {"Heat Transfer in Solids": object()}

    return fake_model, recorder


def _get_setup_tool():
    from src.tools.physics import register_physics_tools

    captured = _capture_register(register_physics_tools)
    return captured["physics_setup_heat_boundaries"]


def test_radiation_boundary_create_call_sequence(monkeypatch):
    """Spec test 1 — single radiation BC: create + selection + Tamb +
    epsilon_rad in that order, with silent_exception=None on success."""
    fake_model, rec = _build_fake_ht_model()
    _wire_fake_model(monkeypatch, fake_model)

    setup = _get_setup_tool()
    result = setup(
        physics_name="Heat Transfer in Solids",
        radiation_boundaries=[2],
        radiation_emissivity="0.98",
        radiation_ambient_temp="300[K]",
    )

    assert result["success"] is True, result

    # Feature class id must be the canonical SurfaceToAmbientRadiation
    # (KB scripting_completion_text/physics.md, ht interface, id="sar").
    assert ("sar1", "SurfaceToAmbientRadiation") in rec["feature_creates"], (
        f"expected create('sar1', 'SurfaceToAmbientRadiation'), "
        f"got {rec['feature_creates']!r}"
    )

    # Selection must go through bc.selection().set(int[]) — NOT
    # bc.set('selection', ...). The latter is what the original
    # physics_configure_boundary did, raising "Unknown parameter
    # X#selection" — the symptom this fix is for.
    assert ("sar1", [2]) in rec["selection_sets"], (
        f"expected selection().set([2]) on sar1, "
        f"got {rec['selection_sets']!r}"
    )
    sar_set_keys = [k for (t, k, _v) in rec["set_calls"] if t == "sar1"]
    assert "selection" not in sar_set_keys, (
        "bc.set('selection', ...) must NOT be used — that path raises "
        "UnknownEntityException for SurfaceToAmbientRadiation. Use "
        "bc.selection().set(int[]) instead."
    )

    # Property setters: Tamb and epsilon_rad with the user's values.
    assert ("sar1", "Tamb", "300[K]") in rec["set_calls"], rec["set_calls"]
    assert ("sar1", "epsilon_rad", "0.98") in rec["set_calls"], rec["set_calls"]

    # Response shape — radiation list populated, summary updated.
    radiation = result["configured_boundaries"]["radiation"]
    assert len(radiation) == 1
    entry = radiation[0]
    assert entry["tag"] == "sar1"
    assert entry["boundary"] == 2
    assert entry["emissivity"] == "0.98"
    assert entry["ambient_temp"] == "300[K]"
    assert entry["silent_exception"] is None, (
        f"silent_exception should be None on success, got "
        f"{entry['silent_exception']!r}"
    )
    assert result["summary"]["radiation_boundaries"] == 1


def test_radiation_multiple_boundaries(monkeypatch):
    """Spec test 2 — multiple radiation BCs get sequential tags
    sar1, sar2, ... and each gets its own selection + properties."""
    fake_model, rec = _build_fake_ht_model()
    _wire_fake_model(monkeypatch, fake_model)

    setup = _get_setup_tool()
    result = setup(
        physics_name="Heat Transfer in Solids",
        radiation_boundaries=[2, 4, 6],
    )

    assert result["success"] is True, result
    radiation = result["configured_boundaries"]["radiation"]
    assert [r["tag"] for r in radiation] == ["sar1", "sar2", "sar3"]
    assert [r["boundary"] for r in radiation] == [2, 4, 6]

    # Selection set once per boundary.
    sar_sels = [s for s in rec["selection_sets"] if s[0].startswith("sar")]
    assert sar_sels == [("sar1", [2]), ("sar2", [4]), ("sar3", [6])]


def test_radiation_silent_exception_captures_unknown_property(monkeypatch):
    """Spec test 3 — if a property name is unknown to this COMSOL
    version, the per-property failure is captured in silent_exception
    and the tool still returns success=True (PR-C-fix v2 pattern,
    commit 00330d0). This is the diagnostic guarantee — a future
    COMSOL rename of Tamb/epsilon_rad will surface the exact error
    rather than aborting the whole tool."""
    fake_model, rec = _build_fake_ht_model(
        set_raises_for=("epsilon_rad",)
    )
    _wire_fake_model(monkeypatch, fake_model)

    setup = _get_setup_tool()
    result = setup(
        physics_name="Heat Transfer in Solids",
        radiation_boundaries=[2],
        radiation_emissivity="0.98",
    )

    assert result["success"] is True, result
    entry = result["configured_boundaries"]["radiation"][0]
    silent = entry["silent_exception"]
    assert silent is not None, (
        "silent_exception must capture the failed setter, not be None"
    )
    assert "epsilon_rad" in silent, silent
    assert "Unknown parameter" in silent or "UnknownEntity" in silent, silent


def test_radiation_combined_with_other_bcs(monkeypatch):
    """Spec test 4 — radiation BC coexists with heat_flux + temperature
    + convection in one call. configured_boundaries has all four lists
    populated and summary counts match."""
    fake_model, rec = _build_fake_ht_model()
    _wire_fake_model(monkeypatch, fake_model)

    setup = _get_setup_tool()
    result = setup(
        physics_name="Heat Transfer in Solids",
        heat_flux_boundaries=[1],
        temperature_boundaries=[3],
        convection_boundaries=[5],
        radiation_boundaries=[2],
    )

    assert result["success"] is True, result
    summary = result["summary"]
    assert summary["heat_flux_boundaries"] == 1
    assert summary["temperature_boundaries"] == 1
    assert summary["convection_boundaries"] == 1
    assert summary["radiation_boundaries"] == 1
    cb = result["configured_boundaries"]
    assert len(cb["heat_flux"]) == 1
    assert len(cb["temperature"]) == 1
    assert len(cb["convection"]) == 1
    assert len(cb["radiation"]) == 1


def test_radiation_arg_is_optional(monkeypatch):
    """Default radiation_boundaries=[] must remain backwards-compatible —
    existing Pilot 03/04/05 callers that only pass heat_flux + temp
    + convection must continue to work unchanged."""
    fake_model, rec = _build_fake_ht_model()
    _wire_fake_model(monkeypatch, fake_model)

    setup = _get_setup_tool()
    result = setup(
        physics_name="Heat Transfer in Solids",
        heat_flux_boundaries=[1],
        temperature_boundaries=[3],
    )

    assert result["success"] is True, result
    assert result["configured_boundaries"]["radiation"] == []
    assert result["summary"]["radiation_boundaries"] == 0
    # No SurfaceToAmbientRadiation feature should be created.
    sar_creates = [
        c for c in rec["feature_creates"]
        if c[1] == "SurfaceToAmbientRadiation"
    ]
    assert sar_creates == []
