"""Tests for the new ``study_step_set_property`` MCP tool — Pilot 09 v2
backward-time-tlist unblocker.

Spec: ``plans/mcp_study_step_set_property_spec.md`` §3 (tool surface)
and §5 (unit / integration / regression coverage).

Background: Pilot 09 v2 (Black-Scholes, comsol_82) reached Step 2.11
with ``physics_add("CoefficientFormPDE")`` unblocked by spec #13
(commit 42e75c5) but BLOCKED at Step 2.12 because no MCP tool exposed
the canonical Java path::

    model.study("std1").feature("time").set("tlist", "range(12,-0.5,0)")

The new tool wraps that path. These tests use the MagicMock pattern
from ``test_study_naming_fix.py`` and ``test_physics_set_property.py``
— no live JVM is required.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.tools.session import session_manager
from src.tools.study import (
    _STEP_SET_PROPERTY_HINT,
    _coerce_step_property_value,
    register_study_tools,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

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


def _stub_async_solver(monkeypatch):
    """Stub the global async_solver so regression tests that hit
    study_solve don't try to spin up a real solver thread."""
    import src.tools.study as study_mod

    fake = MagicMock()
    fake.is_running = False
    monkeypatch.setattr(study_mod, "async_solver", fake)
    return fake


def _build_fake_model_with_steps(study_specs, set_raises_for=()):
    """Build a fake model whose ``model.java.study()`` exposes studies
    with the given step tags. ``set_raises_for`` is a list of
    ``(study_tag, step_tag, property_name)`` triples that should raise a
    Java-style exception when ``.set()`` is called — used to verify
    diagnostic error reporting.

    Each study object exposes:
      - ``.tag()`` / ``.label()``
      - ``.feature()`` (no-args) returning the step-list iterable
      - ``.feature(step_tag)`` returning a step feature with ``.set()``
        (or raising for unknown step_tags)
      - ``.create(...)`` / ``.label(...)`` (regression coverage for
        study_add_step / study_create)

    Returns ``(fake_model, recorder)`` where ``recorder`` collects
    every ``.set()`` call as ``[(study_tag, step_tag, prop, value),
    ...]``.
    """
    recorder: dict = {
        "set_calls": [],
        "create_calls": [],
    }
    raises_keys = set(set_raises_for)

    study_objs: list = []
    for (study_tag, label, steps) in study_specs:
        # steps is a list of (step_tag, step_type). Build per-step
        # feature objects that record .set() calls.
        step_features: dict = {}
        step_iter_objs: list = []
        for step_tag, step_type in steps:
            step_feat = MagicMock()
            step_feat.tag.return_value = step_tag
            step_feat.getType.return_value = step_type

            def _set(name, value, _stag=study_tag, _ftag=step_tag):
                recorder["set_calls"].append(
                    (_stag, _ftag, str(name), value)
                )
                if (_stag, _ftag, str(name)) in raises_keys:
                    raise RuntimeError(
                        "com.comsol.util.exceptions.UnknownEntityException: "
                        f"Unknown parameter X#{name}"
                    )

            step_feat.set.side_effect = _set
            step_features[step_tag] = step_feat
            step_iter_objs.append(step_feat)

        study_obj = MagicMock()
        study_obj.tag.return_value = study_tag
        study_obj.label.return_value = label

        def _feature(*args, _features=step_features, _iter=step_iter_objs):
            if not args:
                # No-arg call: return the iterable of step features
                # (used by _study_descriptors).
                m = MagicMock()
                m.__iter__ = lambda self: iter(_iter)
                return m
            requested = str(args[0])
            if requested in _features:
                return _features[requested]
            # Java behaviour for unknown step tags is to raise.
            raise RuntimeError(
                "com.comsol.util.exceptions.UnknownEntityException: "
                f"Unknown feature tag {requested!r}"
            )

        study_obj.feature.side_effect = _feature

        def _create(new_tag, new_type, _stag=study_tag):
            recorder["create_calls"].append((_stag, str(new_tag), str(new_type)))

        study_obj.create.side_effect = _create
        study_objs.append(study_obj)

    def study_factory(*args):
        if args:
            tag = str(args[0])
            for s in study_objs:
                if s.tag() == tag:
                    return s
            return None
        m = MagicMock()
        m.__iter__ = lambda self: iter(study_objs)
        m.remove.side_effect = lambda t: None
        # study_create path: jm.study().create(tag) → new study.
        m.create.side_effect = lambda new_tag: _new_study_for(new_tag)

        def _new_study_for(new_tag):
            extra = MagicMock()
            extra.tag.return_value = str(new_tag)
            extra.create.side_effect = lambda st, ty: recorder["create_calls"].append(
                (str(new_tag), str(st), str(ty)),
            )
            extra.label.side_effect = lambda lbl: None
            extra.feature.side_effect = lambda *a: MagicMock(
                __iter__=lambda self: iter([]),
            )
            study_objs.append(extra)
            return extra

        return m

    jm = MagicMock()
    jm.study.side_effect = study_factory

    fake_model = MagicMock()
    fake_model.java = jm
    fake_model.name.return_value = "test_model"
    return fake_model, recorder


def _get_tool():
    return _capture_register(register_study_tools)["study_step_set_property"]


# ---------------------------------------------------------------------------
# §5.1 — Unit tests (9 cases)
# ---------------------------------------------------------------------------

class TestStudyStepSetProperty:
    """Spec §5.1 unit tests 1–9."""

    # ---- 1. tlist with string expression -------------------------------
    def test_set_tlist_string_expression(self, monkeypatch):
        """1/9 — Canonical use case: ``range(0,0.1,1)`` passed through
        verbatim. The mocked Java side records exactly one
        ``study('std1').feature('time').set('tlist', 'range(0,0.1,1)')``
        call."""
        fake_model, rec = _build_fake_model_with_steps(
            [("std1", "Transient Study", [("time", "Transient")])]
        )
        _wire_fake_model(monkeypatch, fake_model)

        result = _get_tool()(
            study_name="std1",
            step_tag="time",
            property_name="tlist",
            value="range(0,0.1,1)",
        )

        assert result["success"] is True, result
        assert rec["set_calls"] == [
            ("std1", "time", "tlist", "range(0,0.1,1)"),
        ], rec
        assert result["property"]["name"] == "tlist"
        assert result["property"]["value"] == "range(0,0.1,1)"
        assert result["study"]["tag"] == "std1"
        assert result["study"]["label"] == "Transient Study"
        assert result["step"]["tag"] == "time"
        assert result["java_path"] == (
            "model.study('std1').feature('time')"
            ".set('tlist', <value>)"
        )

    # ---- 2. Pilot 09 v2 backward-time use case -------------------------
    def test_set_tlist_backward_time(self, monkeypatch):
        """2/9 — Pilot 09 v2 / comsol_82 Black-Scholes use case:
        ``range(12,-0.5,0)`` must reach Java verbatim. Mirrors
        FEABench ground_truth ``model.study("std1").feature("time")
        .set("tlist", "range(12,-0.5,0)");``."""
        fake_model, rec = _build_fake_model_with_steps(
            [("std1", "Transient Study", [("time", "Transient")])]
        )
        _wire_fake_model(monkeypatch, fake_model)

        result = _get_tool()(
            study_name="std1",
            step_tag="time",
            property_name="tlist",
            value="range(12,-0.5,0)",
        )

        assert result["success"] is True, result
        assert rec["set_calls"] == [
            ("std1", "time", "tlist", "range(12,-0.5,0)"),
        ], (
            f"FEABench comsol_82 backward-time tlist must be forwarded "
            f"to Java verbatim; got {rec['set_calls']!r}"
        )

    # ---- 3. Numeric tolerance as string --------------------------------
    def test_set_atolglobal_string_numeric(self, monkeypatch):
        """3/9 — ``atolglobal`` accepts string numerics
        (e.g. ``"1e-6"``). The tool must NOT coerce strings to floats —
        Java's `.set()` for tolerances expects expression strings."""
        fake_model, rec = _build_fake_model_with_steps(
            [("std1", "Transient Study", [("time", "Transient")])]
        )
        _wire_fake_model(monkeypatch, fake_model)

        result = _get_tool()(
            study_name="std1",
            step_tag="time",
            property_name="atolglobal",
            value="1e-6",
        )

        assert result["success"] is True, result
        assert rec["set_calls"] == [
            ("std1", "time", "atolglobal", "1e-6"),
        ]
        # And verify the coercion helper is identity for strings.
        assert _coerce_step_property_value("1e-6") == "1e-6"

    # ---- 4. Boolean property -------------------------------------------
    def test_set_useparam_boolean(self, monkeypatch):
        """4/9 — Python ``True`` is forwarded as-is so JPype's auto-
        boxing produces ``Boolean.TRUE`` at the Java boundary. The
        tool itself does NOT coerce ``True`` to a string like ``"on"``
        (that's COMSOL's geometry-property convention, not the study-
        step one)."""
        fake_model, rec = _build_fake_model_with_steps(
            [("std1", "Stationary Study", [("stat", "Stationary")])]
        )
        _wire_fake_model(monkeypatch, fake_model)

        result = _get_tool()(
            study_name="std1",
            step_tag="stat",
            property_name="useparam",
            value=True,
        )

        assert result["success"] is True, result
        assert rec["set_calls"] == [("std1", "stat", "useparam", True)]
        # Coercion helper preserves bools so JPype handles them.
        assert _coerce_step_property_value(True) is True
        assert _coerce_step_property_value(False) is False

    # ---- 5. List of floats ---------------------------------------------
    def test_set_tlist_list_of_floats(self, monkeypatch):
        """5/9 — Explicit list of floats ``[0, 0.5, 1.0, 2.0]`` is
        forwarded (tuples are normalized to lists for predictable
        Java array conversion)."""
        fake_model, rec = _build_fake_model_with_steps(
            [("std1", "Transient Study", [("time", "Transient")])]
        )
        _wire_fake_model(monkeypatch, fake_model)

        explicit_list = [0, 0.5, 1.0, 2.0]
        result = _get_tool()(
            study_name="std1",
            step_tag="time",
            property_name="tlist",
            value=explicit_list,
        )

        assert result["success"] is True, result
        assert rec["set_calls"] == [
            ("std1", "time", "tlist", [0, 0.5, 1.0, 2.0]),
        ]
        # Tuple input is normalized.
        assert _coerce_step_property_value((1, 2, 3)) == [1, 2, 3]
        # Lists are passed through.
        assert _coerce_step_property_value([1, 2, 3]) == [1, 2, 3]

    # ---- 6. Unknown step tag → diagnostic ------------------------------
    def test_unknown_step_tag_returns_diagnostic(self, monkeypatch):
        """6/9 — Calling with a step_tag that the study doesn't have
        surfaces ``success=False`` plus an ``attempted_java_path`` field
        so the agent can diagnose the typo without re-querying."""
        fake_model, rec = _build_fake_model_with_steps(
            [("std1", "Transient Study", [("time", "Transient")])]
        )
        _wire_fake_model(monkeypatch, fake_model)

        result = _get_tool()(
            study_name="std1",
            step_tag="no_such_step",
            property_name="tlist",
            value="range(0,1,10)",
        )

        assert result["success"] is False, result
        assert "no_such_step" in result["error"]
        # No .set() call should have happened.
        assert rec["set_calls"] == [], rec
        assert "attempted_java_path" in result
        assert (
            result["attempted_java_path"]
            == "model.study('std1').feature('no_such_step')"
        )

    # ---- 7. Unknown property → diagnostic with full path ---------------
    def test_unknown_property_returns_diagnostic(self, monkeypatch):
        """7/9 — When Java ``.set()`` itself rejects the (step, prop)
        combination, the error must echo the full attempted Java path
        with both the step_tag and property_name interpolated."""
        fake_model, rec = _build_fake_model_with_steps(
            [("std1", "Transient Study", [("time", "Transient")])],
            set_raises_for=[("std1", "time", "NonExistent")],
        )
        _wire_fake_model(monkeypatch, fake_model)

        result = _get_tool()(
            study_name="std1",
            step_tag="time",
            property_name="NonExistent",
            value="x",
        )

        assert result["success"] is False, result
        # .set() was invoked (so the error came from Java, not the
        # step-resolution path).
        assert rec["set_calls"] == [("std1", "time", "NonExistent", "x")]
        err = result["error"]
        assert "NonExistent" in err, err
        assert "Unknown parameter" in err or "UnknownEntity" in err, err
        # attempted_java_path field carries the canonical signature.
        assert result["attempted_java_path"] == (
            "model.study('std1').feature('time')"
            ".set('NonExistent', <value>)"
        )

    # ---- 8. Unknown study --------------------------------------------
    def test_unknown_study_returns_error(self, monkeypatch):
        """8/9 — Calling with a non-existent study tag/label is caught
        by ``_resolve_study`` and surfaced via the unified error
        message; no Java step lookup is attempted."""
        fake_model, rec = _build_fake_model_with_steps(
            [("std1", "Transient Study", [("time", "Transient")])]
        )
        _wire_fake_model(monkeypatch, fake_model)

        result = _get_tool()(
            study_name="ghost_study",
            step_tag="time",
            property_name="tlist",
            value="range(0,1,10)",
        )

        assert result["success"] is False, result
        assert "ghost_study" in result["error"]
        assert "not found" in result["error"].lower()
        assert rec["set_calls"] == []

    # ---- 9. Label resolution -----------------------------------------
    def test_label_resolution(self, monkeypatch):
        """9/9 — Calling with the display label (``"Transient Study"``)
        instead of the tag must resolve via _resolve_study's label
        fallback. The response.study.tag is the resolved tag, and the
        java_path string uses the tag (not the label) for the Java
        call signature."""
        fake_model, rec = _build_fake_model_with_steps(
            [("std1", "Transient Study", [("time", "Transient")])]
        )
        _wire_fake_model(monkeypatch, fake_model)

        result = _get_tool()(
            study_name="Transient Study",
            step_tag="time",
            property_name="tlist",
            value="range(0,0.1,1)",
        )

        assert result["success"] is True, result
        assert rec["set_calls"] == [
            ("std1", "time", "tlist", "range(0,0.1,1)"),
        ]
        # Resolved back to the tag in the response.
        assert result["study"]["tag"] == "std1"
        assert result["study"]["label"] == "Transient Study"
        assert result["java_path"].startswith(
            "model.study('std1').feature('time')"
        )


# ---------------------------------------------------------------------------
# §5.2 — Integration test (1 case, mock-based)
# ---------------------------------------------------------------------------

class TestStudyStepSetPropertyIntegration:
    """Spec §5.2 — integration coverage. The spec proposes a live
    COMSOL test recreating Pilot 09 v2 Step 2.12 (1D Interval geom +
    transient study + ``tlist=range(12,-0.5,0)`` setter). The rest of
    the suite is mock-based, so we wire a fake Java bridge that emulates
    the full create-study → set-tlist → read-back flow and verifies the
    final stored value matches FEABench ``comsol_82.json`` ground_truth.
    """

    def test_study_step_set_tlist_integration(self, monkeypatch):
        """1/1 — End-to-end: study_create → study_step_set_property →
        the Java step feature holds the expected ``tlist`` string,
        matching FEABench comsol_82 ground_truth."""
        # Step feature with .set() that stashes into _stored.
        _stored: dict = {}

        time_feat = MagicMock()
        time_feat.tag.return_value = "time"
        time_feat.getType.return_value = "Transient"

        def _step_set(name, value):
            _stored[str(name)] = value

        time_feat.set.side_effect = _step_set
        time_feat.getString.side_effect = lambda k: _stored.get(k)

        # Study with feature("time")  → time_feat ; feature()  → [time_feat]
        study_obj = MagicMock()
        study_obj.tag.return_value = "std1"
        study_obj.label.return_value = "Transient Study"

        def _study_feature(*args):
            if not args:
                m = MagicMock()
                m.__iter__ = lambda self: iter([time_feat])
                return m
            assert str(args[0]) == "time"
            return time_feat

        study_obj.feature.side_effect = _study_feature

        # jm.study() returns the iterable; jm.study(tag) returns the obj.
        def study_factory(*args):
            if args:
                return study_obj if str(args[0]) == "std1" else None
            m = MagicMock()
            m.__iter__ = lambda self: iter([study_obj])
            return m

        jm = MagicMock()
        jm.study.side_effect = study_factory

        fake_model = MagicMock()
        fake_model.java = jm

        _wire_fake_model(monkeypatch, fake_model)

        # End-to-end call.
        result = _get_tool()(
            study_name="std1",
            step_tag="time",
            property_name="tlist",
            value="range(12,-0.5,0)",
        )

        assert result["success"] is True, result
        # FEABench comsol_82 ground_truth signature: the stored value
        # at the Java feature is exactly the backward-time expression.
        assert _stored == {"tlist": "range(12,-0.5,0)"}
        # And the Java-style getString readback matches.
        assert time_feat.getString("tlist") == "range(12,-0.5,0)"
        # And the response java_path mirrors ground_truth.
        assert result["java_path"] == (
            "model.study('std1').feature('time')"
            ".set('tlist', <value>)"
        )


# ---------------------------------------------------------------------------
# §5.3 — Regression tests (4 cases) — existing study tools unaffected
# ---------------------------------------------------------------------------

class TestStudyToolRegression:
    """Spec §5.3 — existing study_* tools must keep their schemas. The
    only schema delta (§3.3) is the additive ``set_property_hint`` field
    on ``response.study.step``."""

    def test_study_create_response_schema_compat(self, monkeypatch):
        """1/4 — study_create response keeps tag/label/type/step.tag/
        step.type; the new ``step.set_property_hint`` is the spec's only
        additive change."""
        fake_model, _rec = _build_fake_model_with_steps(study_specs=[])
        _wire_fake_model(monkeypatch, fake_model)

        tools = _capture_register(register_study_tools)
        result = tools["study_create"](study_type="time_dependent", name="std1")

        assert result["success"] is True, result
        study = result["study"]
        # Existing fields preserved.
        assert study["tag"] == "std1"
        assert study["type"] == "time_dependent"
        assert study["label"] == "Transient Study"
        assert study["step"]["tag"] == "time"
        assert study["step"]["type"] == "Transient"
        # New advisory field present.
        assert study["step"]["set_property_hint"] == _STEP_SET_PROPERTY_HINT
        assert "study_step_set_property" in study["step"]["set_property_hint"]

    def test_study_solve_unchanged(self, monkeypatch):
        """2/4 — study_solve behaviour and response shape unchanged."""
        fake_model, _rec = _build_fake_model_with_steps(
            [("std1", "Stationary Study", [("stat", "Stationary")])]
        )
        _wire_fake_model(monkeypatch, fake_model)
        _stub_async_solver(monkeypatch)
        # study_solve calls model.solve(label).
        fake_model.solve = MagicMock()

        tools = _capture_register(register_study_tools)
        result = tools["study_solve"](study_name="std1")

        assert result["success"] is True, result
        assert result["study"] == {
            "tag": "std1",
            "label": "Stationary Study",
            "type": "stationary",
        }
        assert result["message"] == "Solving completed."
        fake_model.solve.assert_called_once_with("Stationary Study")

    def test_study_list_unchanged(self, monkeypatch):
        """3/4 — study_list response shape unchanged
        (tag/label/type/steps) — no set_property_hint pollution."""
        fake_model, _rec = _build_fake_model_with_steps(
            [
                ("std1", "Stationary Study", [("stat", "Stationary")]),
                ("std2", "Transient Study", [("time", "Transient")]),
            ]
        )
        _wire_fake_model(monkeypatch, fake_model)

        tools = _capture_register(register_study_tools)
        result = tools["study_list"]()

        assert result["success"] is True, result
        assert result["count"] == 2
        assert result["studies"] == [
            {"tag": "std1", "label": "Stationary Study",
             "type": "stationary", "steps": ["stat"]},
            {"tag": "std2", "label": "Transient Study",
             "type": "transient", "steps": ["time"]},
        ]
        # study_list result must NOT carry set_property_hint anywhere.
        for s in result["studies"]:
            assert "set_property_hint" not in s

    def test_study_add_step_unchanged(self, monkeypatch):
        """4/4 — study_add_step adds the same study.create(tag, type)
        call as before; the response's existing fields are preserved
        and only the additive set_property_hint joins step."""
        fake_model, rec = _build_fake_model_with_steps(
            [("std1", "Stationary Study", [("stat", "Stationary")])]
        )
        _wire_fake_model(monkeypatch, fake_model)

        tools = _capture_register(register_study_tools)
        result = tools["study_add_step"](
            study_name="std1",
            step_type="transient",
        )

        assert result["success"] is True, result
        assert result["study"] == {"tag": "std1", "label": "Stationary Study"}
        assert result["step"]["type"] == "Transient"
        assert result["step"]["tag"] == "time"
        assert result["step"]["set_property_hint"] == _STEP_SET_PROPERTY_HINT
        # study.create("time", "Transient") was invoked.
        assert ("std1", "time", "Transient") in rec["create_calls"], (
            f"expected study('std1').create('time', 'Transient'); "
            f"got {rec['create_calls']!r}"
        )


# ---------------------------------------------------------------------------
# Spec #13 (commit 42e75c5) PDE regression — 1 case
# ---------------------------------------------------------------------------
#
# User instruction: include 1 PDE regression from spec #13
# (mcp_physics_add_pde_branch_spec.md). Spec #14 only touches
# src/tools/study.py and tests, but the Pilot 09 v2 unblock path that
# motivated spec #14 starts with spec #13's CoefficientFormPDE alias
# resolution. This regression pins the cross-spec invariant: the
# Mathematics > PDE Interfaces branch resolver added in 42e75c5
# (PHYSICS_TYPE_ALIASES → "CoefficientFormPDE" → ("CoefficientFormPDE",
# "c")) continues to feed the canonical Java tuple into
# comp.physics().create(tag, class, geom_tag) after spec #14's
# study.py changes land — i.e. the FEABench comsol_82.json
# ground_truth physics_add call still works alongside the new
# study_step_set_property tool.

class TestSpec13PDERegression:
    """1 PDE regression from spec #13 (commit 42e75c5). Pinned here per
    user instruction so spec #14's merge cannot silently break the
    Pilot 09 v2 prereq path."""

    def test_physics_add_coefficient_form_pde_still_resolves(
        self, monkeypatch,
    ):
        """1/1 — physics_add('CoefficientFormPDE', tag='c') still routes
        through _resolve_physics_type and invokes
        ``comp.physics().create("c", "CoefficientFormPDE", "geom1")``
        — matching FEABench comsol_82 ground_truth and unchanged by
        spec #14 (which touches only study.py).
        """
        from src.tools.physics import (
            _resolve_physics_type,
            register_physics_tools,
        )

        # Resolver-level invariant (mirrors spec #13 §5.1 unit test 2).
        assert _resolve_physics_type("CoefficientFormPDE") == (
            "CoefficientFormPDE", "c",
        )

        # End-to-end physics_add invariant (mirrors spec #13 §5.2
        # integration), inlined here so this file is self-contained.
        fake_model = MagicMock()
        fake_phys = MagicMock()
        fake_phys.label.return_value = "Coefficient Form PDE"

        fake_comp = MagicMock()
        fake_comp.tag.return_value = "comp1"

        fake_geom = MagicMock()
        fake_geom.tag.return_value = "geom1"
        fake_comp.geom.return_value = [fake_geom]

        fake_physics_factory = MagicMock()
        fake_physics_factory.create.return_value = fake_phys
        fake_comp.physics.return_value = fake_physics_factory

        fake_model.java.component.return_value = [fake_comp]

        _wire_fake_model(monkeypatch, fake_model)

        captured: dict = {}

        class _Stub:
            def tool(self):
                def deco(fn):
                    captured[fn.__name__] = fn
                    return fn
                return deco

        register_physics_tools(_Stub())
        physics_add = captured["physics_add"]

        result = physics_add(physics_type="CoefficientFormPDE", tag="c")

        assert result["success"] is True, result
        assert result["physics"]["type"] == "CoefficientFormPDE"
        assert result["physics"]["tag"] == "c"
        assert result["physics"]["component"] == "comp1"
        assert result["physics"]["geometry"] == "geom1"

        # FEABench ground_truth signature — assert the literal call args.
        fake_physics_factory.create.assert_called_once_with(
            "c", "CoefficientFormPDE", "geom1",
        )
