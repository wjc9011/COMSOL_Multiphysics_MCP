"""Regression test for the radiation BC ``epsilon_mat="userdef"`` fix.

Background: Pilot 07 v2 (live Cowork measurement) hit
``Undefined material property 'epsilon rad'`` at study_solve time after
``physics_setup_heat_boundaries(radiation_boundaries=[...], ...)``. Root
cause: the freshly-created ``SurfaceToAmbientRadiation`` (``sar``)
feature defaults its emissivity source to *From material*
(``epsilon_mat = "fromMaterial"``), which makes COMSOL look up the
internal property ``epsilon rad`` (with a space in the internal name)
on the boundary's material — and most materials don't define it.

Fix: before setting ``epsilon_rad`` to the user-supplied numeric value,
flip ``epsilon_mat`` to ``"userdef"`` so COMSOL uses the literal value
the caller passed instead of probing the material library. The order
must be ``epsilon_mat`` -> ``epsilon_rad`` so the user-defined value
takes effect.

Unit-test pattern mirrors ``test_radiation_bc_fix.py`` — fake Java
bridge records ``bc.set`` calls and we assert the call ordering.
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


def _build_fake_ht_model(set_raises_for: tuple[str, ...] = ()):
    recorder: dict = {
        "feature_creates": [],
        "selection_sets": [],
        "set_calls": [],
        "set_raises_for": set(set_raises_for),
        "labels": [],
    }

    def make_bc(tag: str):
        bc = MagicMock()

        sel = MagicMock()
        sel.set.side_effect = lambda values: recorder["selection_sets"].append(
            (tag, list(values))
        )
        bc.selection.return_value = sel

        def bc_set(key, value):
            recorder["set_calls"].append((tag, str(key), value))
            if key in recorder["set_raises_for"]:
                raise RuntimeError(
                    "com.comsol.util.exceptions.UnknownEntityException: "
                    f"Unknown parameter X#{key}"
                )

        bc.set.side_effect = bc_set
        bc.label.side_effect = lambda text: recorder["labels"].append((tag, text))
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
    fake_model.physics.return_value = {"Heat Transfer in Solids": object()}
    return fake_model, recorder


def _get_setup_tool():
    from src.tools.physics import register_physics_tools

    captured = _capture_register(register_physics_tools)
    return captured["physics_setup_heat_boundaries"]


def test_radiation_sets_epsilon_mat_userdef_before_epsilon_rad(monkeypatch):
    """epsilon_mat='userdef' must be set BEFORE epsilon_rad.

    Without the mode flip, ``epsilon_rad`` is ignored (COMSOL keeps
    sourcing emissivity from the material) and study_solve raises
    ``Undefined material property 'epsilon rad'``.
    """
    fake_model, rec = _build_fake_ht_model()
    _wire_fake_model(monkeypatch, fake_model)

    setup = _get_setup_tool()
    result = setup(
        physics_name="Heat Transfer in Solids",
        radiation_boundaries=[2],
        radiation_emissivity="0.95",
        radiation_ambient_temp="293.15[K]",
    )

    assert result["success"] is True, result

    sar_calls = [(k, v) for (t, k, v) in rec["set_calls"] if t == "sar1"]
    keys = [k for (k, _v) in sar_calls]

    assert ("epsilon_mat", "userdef") in sar_calls, (
        "bc.set('epsilon_mat', 'userdef') must be called so the "
        "feature stops sourcing emissivity from the material. Got "
        f"set_calls on sar1: {sar_calls!r}"
    )
    assert "epsilon_rad" in keys, (
        f"epsilon_rad must still be set; got {sar_calls!r}"
    )
    assert keys.index("epsilon_mat") < keys.index("epsilon_rad"), (
        "epsilon_mat='userdef' must be set BEFORE epsilon_rad — "
        "otherwise the user-defined value is overwritten by the "
        "fromMaterial default. Got call order: "
        f"{keys!r}"
    )

    entry = result["configured_boundaries"]["radiation"][0]
    assert entry["silent_exception"] is None, (
        f"silent_exception should be None on success, got "
        f"{entry['silent_exception']!r}"
    )


def test_radiation_epsilon_mat_failure_recorded_in_silent_exception(
    monkeypatch,
):
    """If a future COMSOL renames ``epsilon_mat``, the per-property
    failure must surface in silent_exception (not abort the tool) —
    same diagnostic guarantee the v2 PR set up for Tamb / epsilon_rad.
    """
    fake_model, rec = _build_fake_ht_model(set_raises_for=("epsilon_mat",))
    _wire_fake_model(monkeypatch, fake_model)

    setup = _get_setup_tool()
    result = setup(
        physics_name="Heat Transfer in Solids",
        radiation_boundaries=[2],
        radiation_emissivity="0.95",
    )

    assert result["success"] is True, result
    silent = result["configured_boundaries"]["radiation"][0]["silent_exception"]
    assert silent is not None and "epsilon_mat" in silent, (
        f"silent_exception must mention epsilon_mat, got {silent!r}"
    )
