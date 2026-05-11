"""Regression tests for the radiation BC ``epsilon_rad_mat="userdef"`` fix.

Background (Pilot 07 v3 evidence, 2026-05-11):
``physics_setup_heat_boundaries(radiation_boundaries=[...], ...)`` — under
commit 256740d — set the wrong mode key ``epsilon_mat`` (which doesn't
exist on the sar feature). COMSOL responded with silent_exception
``Unknown parameter X#epsilon mat``, the mode flip never landed, and
study_solve raised ``Undefined material property 'epsilon rad'`` because
the feature was still sourcing emissivity ``From material``.

Root cause: the ``SurfaceToAmbientRadiation`` (``sar``) feature's
emissivity-source selector is named ``epsilon_rad_mat`` (values
``from_mat | userdef``), NOT ``epsilon_mat``. KB
``scripting_completion_text/physics.md`` (data/completion/physics.xml
lines 18608-18611, token batrsace under the sar prop list) is
authoritative, and a live probe on 2026-05-11 (sar_probe_v3 model)
reproduced the v3 silent_exception: ``Tamb`` and ``epsilon_rad``
applied cleanly, only ``epsilon_mat`` raised UnknownEntityException.

Fix: call ``bc.set('epsilon_rad_mat', 'userdef')`` (the correct key),
keeping the order ``epsilon_rad_mat -> epsilon_rad`` so the user-supplied
emissivity is not overwritten by the fromMaterial default.

These tests use the same MagicMock pattern as ``test_radiation_bc_fix.py``
to exercise the Java call sequence without booting COMSOL/JVM. The
mph-live spec §5.1 tests live in ``runs/sanity_radiation_v3/`` (Cowork
session) and require a real COMSOL session.
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


# ---------------------------------------------------------------------------
# Spec §5.1 tests (mocked variants — live equivalents in
# runs/sanity_radiation_v3/, executed by the Cowork session)
# ---------------------------------------------------------------------------


def test_sar_feature_no_silent_exception(monkeypatch):
    """Spec §5.1 1/4 — radiation BC setup raises no silent_exception.

    Live mph (Pilot 07 v3) confirmed Tamb + epsilon_rad apply cleanly;
    only the wrong mode key produced an exception. After the fix the
    correct mode key (epsilon_rad_mat) must also apply cleanly, so
    silent_exception is None.
    """
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
    rad = result["configured_boundaries"]["radiation"][0]
    assert rad["silent_exception"] is None, (
        "all three sar setters (Tamb, epsilon_rad_mat, epsilon_rad) "
        "must apply cleanly. Got silent_exception="
        f"{rad['silent_exception']!r}"
    )

    sar_set_keys = [k for (t, k, _v) in rec["set_calls"] if t == "sar1"]
    assert "epsilon_mat" not in sar_set_keys, (
        "epsilon_mat is NOT a valid sar parameter — it raised "
        "UnknownEntityException in Pilot 07 v3 and must not be sent "
        "to COMSOL. Got set keys on sar1: "
        f"{sar_set_keys!r}"
    )
    assert "epsilon_rad_mat" in sar_set_keys, (
        "the correct mode key is epsilon_rad_mat (KB physics.xml "
        f"token batrsace under sar). Got: {sar_set_keys!r}"
    )


def test_sar_feature_solves_1d_radiation(monkeypatch):
    """Spec §5.1 2/4 — 1D heat + radiation setup produces the call
    sequence study_solve needs.

    The live spec test boots COMSOL, builds a 1D bar, and runs
    study_solve. Here we mock the Java bridge and assert the Java call
    sequence physics_setup_heat_boundaries emits is exactly the one
    the Pilot 07 v3 model needed: temperature on boundary 1, sar on
    boundary 2 with epsilon_rad_mat=userdef before epsilon_rad. If
    that sequence is correct, study_solve reaches steady-state in the
    Cowork live run.
    """
    fake_model, rec = _build_fake_ht_model()
    _wire_fake_model(monkeypatch, fake_model)

    setup = _get_setup_tool()
    result = setup(
        physics_name="Heat Transfer in Solids",
        temperature_boundaries=[1], temperature_value="1000[K]",
        radiation_boundaries=[2], radiation_emissivity="0.98",
        radiation_ambient_temp="300[K]",
    )
    assert result["success"] is True, result

    # Temperature BC on boundary 1 (heat source side).
    assert ("temp1", "TemperatureBoundary") in rec["feature_creates"], (
        rec["feature_creates"]
    )
    assert ("temp1", [1]) in rec["selection_sets"], rec["selection_sets"]
    assert ("temp1", "T0", "1000[K]") in rec["set_calls"], rec["set_calls"]

    # SAR on boundary 2 with the full userdef sequence.
    assert ("sar1", "SurfaceToAmbientRadiation") in rec["feature_creates"], (
        rec["feature_creates"]
    )
    assert ("sar1", [2]) in rec["selection_sets"], rec["selection_sets"]
    sar_calls = [(k, v) for (t, k, v) in rec["set_calls"] if t == "sar1"]
    assert ("epsilon_rad_mat", "userdef") in sar_calls, sar_calls
    assert ("epsilon_rad", "0.98") in sar_calls, sar_calls
    assert ("Tamb", "300[K]") in sar_calls, sar_calls

    # Ordering — mode flip before value, otherwise epsilon_rad gets
    # silently ignored (still sourced From material).
    keys = [k for (k, _v) in sar_calls]
    assert keys.index("epsilon_rad_mat") < keys.index("epsilon_rad"), (
        "epsilon_rad_mat='userdef' must be set BEFORE epsilon_rad — "
        "otherwise the user-defined value is overwritten by the "
        f"fromMaterial default. Got call order: {keys!r}"
    )


def test_sar_feature_temperature_in_band(monkeypatch):
    """Spec §5.1 3/4 — radiation_ambient_temp value is propagated to
    the Tamb setter unchanged (so the live solve produces a T(0.1) in
    the expected [Tamb, Twall] = [300, 1000] K band).

    The live spec test reads ``results_global_evaluate("at1(0.1, T)")``
    and asserts 300 < value < 1000. That bound depends entirely on the
    correct emissivity + ambient temperature flowing through to
    COMSOL — which is what we assert here at the call-shape level.
    """
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

    rad = result["configured_boundaries"]["radiation"][0]
    assert rad["ambient_temp"] == "300[K]", rad
    assert rad["emissivity"] == "0.98", rad

    sar_calls_dict = {
        k: v for (t, k, v) in rec["set_calls"] if t == "sar1"
    }
    assert sar_calls_dict.get("Tamb") == "300[K]", sar_calls_dict
    assert sar_calls_dict.get("epsilon_rad") == "0.98", sar_calls_dict
    assert sar_calls_dict.get("epsilon_rad_mat") == "userdef", sar_calls_dict


def test_sar_no_material_property_error(monkeypatch):
    """Spec §5.1 4/4 — after the fix, no setter targets the legacy
    `epsilon_mat` key, so COMSOL never falls back to lookup of the
    `epsilon rad` material property.

    The live spec test asserts ``model_inspect.problems`` contains no
    ``Undefined material property 'epsilon_rad'`` entry after solve.
    That outcome depends on the mode flip succeeding — which depends
    on the right key being sent. We verify the right key here.
    """
    fake_model, rec = _build_fake_ht_model()
    _wire_fake_model(monkeypatch, fake_model)

    setup = _get_setup_tool()
    result = setup(
        physics_name="Heat Transfer in Solids",
        radiation_boundaries=[2],
        radiation_emissivity="0.98",
    )
    assert result["success"] is True, result

    sar_set_keys = [k for (t, k, _v) in rec["set_calls"] if t == "sar1"]

    # The buggy key must be gone …
    assert "epsilon_mat" not in sar_set_keys, (
        "epsilon_mat must not be set — it was the Pilot 07 v3 bug. "
        f"Got: {sar_set_keys!r}"
    )
    # … and the correct one must be present, applied successfully.
    assert "epsilon_rad_mat" in sar_set_keys, (
        f"epsilon_rad_mat must be sent. Got: {sar_set_keys!r}"
    )

    rad = result["configured_boundaries"]["radiation"][0]
    assert rad["silent_exception"] is None, (
        "epsilon_rad_mat must apply cleanly — if silent_exception is "
        "non-None then study_solve will hit the legacy "
        "'Undefined material property epsilon rad' error. Got "
        f"silent_exception={rad['silent_exception']!r}"
    )


# ---------------------------------------------------------------------------
# Original PR-radiation-fix v2 ordering test (kept; updated for the
# corrected mode key name)
# ---------------------------------------------------------------------------


def test_radiation_sets_epsilon_rad_mat_userdef_before_epsilon_rad(monkeypatch):
    """epsilon_rad_mat='userdef' must be set BEFORE epsilon_rad.

    Without the mode flip, ``epsilon_rad`` is ignored (COMSOL keeps
    sourcing emissivity from the material) and study_solve raises
    ``Undefined material property 'epsilon rad'``. The mode key name
    was corrected from the buggy ``epsilon_mat`` (commit 256740d) to
    the KB-authoritative ``epsilon_rad_mat`` in this PR.
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

    assert ("epsilon_rad_mat", "userdef") in sar_calls, (
        "bc.set('epsilon_rad_mat', 'userdef') must be called so the "
        "feature stops sourcing emissivity from the material. Got "
        f"set_calls on sar1: {sar_calls!r}"
    )
    assert "epsilon_rad" in keys, (
        f"epsilon_rad must still be set; got {sar_calls!r}"
    )
    assert keys.index("epsilon_rad_mat") < keys.index("epsilon_rad"), (
        "epsilon_rad_mat='userdef' must be set BEFORE epsilon_rad — "
        "otherwise the user-defined value is overwritten by the "
        "fromMaterial default. Got call order: "
        f"{keys!r}"
    )

    entry = result["configured_boundaries"]["radiation"][0]
    assert entry["silent_exception"] is None, (
        f"silent_exception should be None on success, got "
        f"{entry['silent_exception']!r}"
    )


def test_radiation_epsilon_rad_mat_failure_recorded_in_silent_exception(
    monkeypatch,
):
    """If a future COMSOL renames ``epsilon_rad_mat``, the per-property
    failure must surface in silent_exception (not abort the tool) —
    same diagnostic guarantee the v2 PR set up for Tamb / epsilon_rad.
    """
    fake_model, rec = _build_fake_ht_model(
        set_raises_for=("epsilon_rad_mat",),
    )
    _wire_fake_model(monkeypatch, fake_model)

    setup = _get_setup_tool()
    result = setup(
        physics_name="Heat Transfer in Solids",
        radiation_boundaries=[2],
        radiation_emissivity="0.95",
    )

    assert result["success"] is True, result
    silent = result["configured_boundaries"]["radiation"][0]["silent_exception"]
    assert silent is not None and "epsilon_rad_mat" in silent, (
        f"silent_exception must mention epsilon_rad_mat, got {silent!r}"
    )
