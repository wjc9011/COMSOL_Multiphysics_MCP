"""Regression tests for per-property ``silent_exceptions`` diagnostic
on the generic boundary-feature tools — Pilot 08 v2 BoundaryLoad fix.

Background (Pilot 08 v2, 2026-05-11): after commit e5bc593 the
selection-dim plumbing was correct (E1 PASS), the
``physics_set_property`` thickness setter worked (E2 PASS), but the
BoundaryLoad call with ``properties={"LoadType": "ForceArea",
"FperLength": [...]}`` was silently misconfigured — the solve produced
displacement=0 / stress=0 because the property dict echoed back in the
response but the underlying Java node had never received the values.

Root cause: both ``physics_boundary_selection`` and
``physics_configure_boundary`` iterated the properties dict with an
``except Exception: pass`` that ate every per-key set() failure. Any
type mismatch (e.g. a scalar passed where COMSOL expects a
``StringArray``, or a misnamed property key) vanished — the BC looked
applied, but had no force on the Java side.

Fix: capture each ``bc.set(key, value)`` outcome in a per-key
``silent_exceptions`` dict so the response surfaces ``None`` (set
succeeded) or ``"ExceptionName: message"`` (set raised silently). This
mirrors the per-feature ``silent_exception`` field that
``physics_setup_heat_boundaries`` adds to each sar entry (commit
053f48a pattern), but at finer granularity — one entry per property
key.

These tests use the same MagicMock pattern as the radiation /
point-selection / configure-boundary fixes so no real COMSOL/JVM is
required. They pin both branches (clean success + per-key failure)
and assert the new response schema field name + shape.
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


def _build_fake_solid_model(
    sdim: int = 3,
    set_raises_for: tuple[str, ...] = (),
):
    """Same fake-model pattern as test_point_selection_fix /
    test_configure_boundary_fix, with the addition of a
    ``set_raises_for`` knob so we can simulate per-key bc.set
    failures (analogous to ``_build_fake_ht_model`` in
    test_radiation_userdef_fix).
    """
    recorder: dict = {
        "feature_creates": [],   # [(tag, type, dim_or_None), ...]
        "selection_sets": [],    # [(tag, [ints]), ...]
        "set_calls": [],         # [(tag, key, value), ...]
        "set_raises_for": set(set_raises_for),
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


def _get_tools():
    from src.tools.physics import register_physics_tools

    captured = _capture_register(register_physics_tools)
    return (
        captured["physics_boundary_selection"],
        captured["physics_configure_boundary"],
    )


# ---------------------------------------------------------------------------
# Test 1 — success path: every property setter applies cleanly, so
# silent_exceptions maps every key to None. This pins the schema field
# name (`silent_exceptions`, plural) and the per-key None marker.
# ---------------------------------------------------------------------------


def test_boundary_selection_silent_exceptions_all_none_on_success(monkeypatch):
    fake_model, rec = _build_fake_solid_model(sdim=3)
    _wire_fake_model(monkeypatch, fake_model)

    boundary_selection, _configure = _get_tools()
    result = boundary_selection(
        physics_name="Solid Mechanics",
        boundary_condition_type="BoundaryLoad",
        boundary_numbers=[4],
        properties={
            "LoadType": "ForceArea",
            "FperArea": ["0", "0", "-1e6[N/m^2]"],
        },
    )

    assert result["success"] is True, result
    bc_out = result["boundary_condition"]
    silent = bc_out.get("silent_exceptions")
    assert silent == {"LoadType": None, "FperArea": None}, (
        "On success every property must map to None — schema "
        f"contract for callers. Got: {silent!r}"
    )

    # Sanity: the Java setter actually received both keys.
    sm_set_keys = [k for (_t, k, _v) in rec["set_calls"]]
    assert "LoadType" in sm_set_keys and "FperArea" in sm_set_keys, (
        rec["set_calls"]
    )


# ---------------------------------------------------------------------------
# Test 2 — Pilot 08 v2 failure simulation: BoundaryLoad with a property
# that COMSOL rejects (here we simulate by making bc.set raise for
# FperLength). The tool MUST still succeed (the BC is created, the
# selection landed, the other properties applied), but
# silent_exceptions["FperLength"] must contain the exception trace so
# the caller can self-diagnose without a live mph probe.
# ---------------------------------------------------------------------------


def test_boundary_selection_silent_exceptions_records_per_key_failure(
    monkeypatch,
):
    fake_model, rec = _build_fake_solid_model(
        sdim=3,
        set_raises_for=("FperLength",),
    )
    _wire_fake_model(monkeypatch, fake_model)

    boundary_selection, _configure = _get_tools()
    result = boundary_selection(
        physics_name="Solid Mechanics",
        boundary_condition_type="BoundaryLoad",
        boundary_numbers=[4],
        properties={
            "LoadType": "ForceLength",
            "FperLength": ["0", "0", "-1e3[N/m]"],
        },
    )

    # The tool must still report success — the BC was created, the
    # selection was applied, and the OTHER properties landed. This is
    # the per-key analogue of the sar1 silent_exception contract: don't
    # abort the whole tool just because one setter failed.
    assert result["success"] is True, result
    silent = result["boundary_condition"]["silent_exceptions"]

    assert silent.get("LoadType") is None, (
        f"LoadType setter was not raised; must be None. Got: {silent!r}"
    )
    assert silent.get("FperLength") is not None, (
        "FperLength setter raised — silent_exceptions must record the "
        f"failure (not None). Got: {silent!r}"
    )
    assert "FperLength" in silent["FperLength"], (
        "the recorded exception string must mention the failing key "
        f"so callers can grep for it. Got: {silent['FperLength']!r}"
    )
    assert (
        "RuntimeError" in silent["FperLength"]
        or "UnknownEntityException" in silent["FperLength"]
    ), (
        "the exception type / message text must be present so the "
        f"caller sees the actual COMSOL error. Got: "
        f"{silent['FperLength']!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — same per-key diagnostic on physics_configure_boundary.
# Pilot 08 v2 BoundaryLoad failure could come through either generic
# tool, so both must surface the silent_exceptions field with the
# same schema.
# ---------------------------------------------------------------------------


def test_configure_boundary_silent_exceptions_records_per_key_failure(
    monkeypatch,
):
    fake_model, rec = _build_fake_solid_model(
        sdim=3,
        set_raises_for=("FperLength",),
    )
    _wire_fake_model(monkeypatch, fake_model)

    _boundary_selection, configure = _get_tools()
    result = configure(
        physics_name="Solid Mechanics",
        boundary_condition="BoundaryLoad",
        boundary_selection=[4],
        properties={
            "LoadType": "ForceLength",
            "FperLength": ["0", "0", "-1e3[N/m]"],
        },
    )

    assert result["success"] is True, result
    silent = result["boundary_condition"]["silent_exceptions"]

    assert silent.get("LoadType") is None, silent
    assert silent.get("FperLength") is not None, silent
    assert "FperLength" in silent["FperLength"], silent["FperLength"]

    # The BC create + selection still landed — only the one setter
    # was silently rejected. This is the schema guarantee that lets
    # callers fix one bad key without losing the whole BC.
    assert ("FperLength" in [
        k for (_t, k, _v) in rec["set_calls"]
    ]), rec["set_calls"]
    assert len(rec["selection_sets"]) == 1, rec["selection_sets"]
