"""Regression tests for the PR-C-fix v2 study tag/label resolver.

Spec: ``plans/mcp_pr_c_fix_v2_spec.md`` §3.3, §5.1.

Pilot 05 hit ``Study "std1" does not exist`` because mph's
``model.solve(name)`` keys studies by node *name* (display label)
while ``study_create`` returns the Java *tag*. The fix mirrors the
PR-C-fix v1 mesh resolver: ``_resolve_study`` accepts either, and the
solve path translates tag → label before calling mph.

These tests do not boot a real COMSOL/JVM. They use a fake Java bridge
plus a fake mph wrapper to verify the resolver/translation behavior.
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


def _build_fake_model_with_studies(studies_spec):
    """Build a fake model whose ``model.java.study()`` exposes the
    given studies.

    ``studies_spec`` is a list of ``(tag, label, step_type)`` tuples.
    Returns ``(fake_model, solve_calls, removed_tags)`` so tests can
    assert what landed at the mph layer.
    """
    study_objs = []
    for tag, label, step_type in studies_spec:
        s = MagicMock()
        s.tag.return_value = tag
        s.label.return_value = label
        # study.feature() iterable of one step with .getType() = step_type.
        step = MagicMock()
        step.tag.return_value = "stat"
        step.getType.return_value = step_type
        feat_iter = MagicMock()
        # Bind ``step`` per-iteration via default arg — without this, every
        # closure captures the loop's last value.
        feat_iter.__iter__ = lambda self, _s=step: iter([_s])
        s.feature.return_value = feat_iter
        study_objs.append((tag, label, s))

    removed_tags: list = []

    def study_factory(*args):
        if args:
            tag = str(args[0])
            for t, _l, obj in study_objs:
                if t == tag:
                    return obj
            return None
        # No-arg → iterable + .remove()
        m = MagicMock()
        m.__iter__ = lambda self: iter([obj for _t, _l, obj in study_objs])
        m.remove.side_effect = lambda t: removed_tags.append(str(t))
        return m

    jm = MagicMock()
    jm.study.side_effect = study_factory

    solve_calls: list = []

    fake_model = MagicMock()
    fake_model.java = jm
    fake_model.name.return_value = "test_model"
    fake_model.solve.side_effect = lambda name=None: solve_calls.append(name)
    return fake_model, solve_calls, removed_tags


def _stub_async_solver(monkeypatch):
    """Replace the global async_solver so study_solve(wait=True) is the
    only code path exercised."""
    import src.tools.study as study_mod

    fake = MagicMock()
    fake.is_running = False
    monkeypatch.setattr(study_mod, "async_solver", fake)
    return fake


def test_study_solve_with_tag(monkeypatch):
    """study_solve('std1') (the tag form) must succeed and feed the
    LABEL down to mph.solve, because mph keys studies by display
    name."""
    from src.tools.study import register_study_tools

    captured = _capture_register(register_study_tools)
    study_solve = captured["study_solve"]

    fake_model, solve_calls, _ = _build_fake_model_with_studies(
        [("std1", "Stationary Study", "Stationary")]
    )
    _wire_fake_model(monkeypatch, fake_model)
    _stub_async_solver(monkeypatch)

    result = study_solve(study_name="std1")

    assert result["success"] is True, result
    # The mph layer received the LABEL, not the tag.
    assert solve_calls == ["Stationary Study"], (
        f"study_solve(tag='std1') should call mph.solve with the LABEL "
        f"'Stationary Study'; got {solve_calls!r}"
    )
    # Response carries both forms.
    assert result["study"]["tag"] == "std1"
    assert result["study"]["label"] == "Stationary Study"
    assert result["study"]["type"] == "stationary"


def test_study_solve_with_label(monkeypatch):
    """study_solve('Stationary Study') (label form) must also resolve
    and feed the same label to mph."""
    from src.tools.study import register_study_tools

    captured = _capture_register(register_study_tools)
    study_solve = captured["study_solve"]

    fake_model, solve_calls, _ = _build_fake_model_with_studies(
        [("std1", "Stationary Study", "Stationary")]
    )
    _wire_fake_model(monkeypatch, fake_model)
    _stub_async_solver(monkeypatch)

    result = study_solve(study_name="Stationary Study")

    assert result["success"] is True, result
    assert solve_calls == ["Stationary Study"]
    assert result["study"]["tag"] == "std1"
    assert result["study"]["label"] == "Stationary Study"


def test_study_remove_with_tag(monkeypatch):
    """study_remove('std1') must remove by tag and report tag+label."""
    from src.tools.study import register_study_tools

    captured = _capture_register(register_study_tools)
    study_remove = captured["study_remove"]

    fake_model, _solve_calls, removed_tags = _build_fake_model_with_studies(
        [("std1", "Stationary Study", "Stationary")]
    )
    _wire_fake_model(monkeypatch, fake_model)

    result = study_remove(study_name="std1")

    assert result["success"] is True, result
    assert removed_tags == ["std1"], (
        f"jm.study().remove() should be called with the tag; got "
        f"{removed_tags!r}"
    )
    assert result["removed"] == {"tag": "std1", "label": "Stationary Study"}


def test_study_list_dict_format(monkeypatch):
    """study_list() must return rich dicts with tag/label/type/steps —
    not the legacy list-of-strings shape (spec §3.3)."""
    from src.tools.study import register_study_tools

    captured = _capture_register(register_study_tools)
    study_list = captured["study_list"]

    fake_model, _solve_calls, _ = _build_fake_model_with_studies(
        [
            ("std1", "Stationary Study", "Stationary"),
            ("std2", "Time Dependent Study", "Transient"),
        ]
    )
    _wire_fake_model(monkeypatch, fake_model)

    result = study_list()

    assert result["success"] is True, result
    studies = result["studies"]
    assert len(studies) == 2
    for s in studies:
        assert "tag" in s and "label" in s, (
            f"each study row must include tag and label; got {s!r}"
        )
        assert "type" in s and "steps" in s
    tags = [s["tag"] for s in studies]
    assert tags == ["std1", "std2"]
    types = [s["type"] for s in studies]
    assert types == ["stationary", "transient"]
