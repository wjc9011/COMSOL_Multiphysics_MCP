"""Tests for the default-selection echo on material creation.

Spec: plans/mcp_set_material_deprecation_spec.md §3.2, §3.3, §5.1.

After PR, ``material_create_user_defined`` / ``material_create_from_kb``
must surface the COMSOL component-wide default selection (= all
domains) on the response so callers know whether a follow-up
``material_assign_to_domain`` is needed. The new schema fields are:

  - domain_selection (str "all (component-wide default)" or list[int])
  - domain_selection_entities (list[int], possibly empty)
  - selection_note (str advisory or None)

For schema consistency, ``material_assign_to_domain`` also gains the
two new fields (with selection_note=None and entities=domains).

Mock-based; no live JVM required. Fakes ``mat.selection().entities(sdim)``
to control the returned entities list.
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


def _build_fake_model_for_create(
    sdim: int = 2,
    selection_entities=None,
    selection_raises: bool = False,
):
    """Build a fake model whose ``comp.material().create(tag, "Common")``
    returns a Material whose ``selection().entities(sdim)`` returns the
    given list (or raises on demand to test grace-degrade).

    ``selection_entities=None`` ⇒ default-all (echo string).
    ``selection_entities=[1, 2]`` ⇒ explicit entities echoed.
    ``selection_raises=True`` ⇒ entities() raises; helper must not
        propagate the failure to the caller.
    """
    # ----- material object -----
    mat = MagicMock()
    mat.tag.return_value = "mat1"
    mat.label = MagicMock()

    # propertyGroup("def") returns a writable group.
    pg = MagicMock()
    mat.propertyGroup.return_value = pg

    # selection().entities(sdim)
    sel = MagicMock()
    if selection_raises:
        sel.entities.side_effect = RuntimeError("boom")
    else:
        sel.entities.return_value = (
            [] if selection_entities is None else list(selection_entities)
        )
    mat.selection.return_value = sel

    # ----- component object (carries geometry + material registry) -----
    geom = MagicMock()
    geom.sDim.return_value = sdim
    geom.tag.return_value = "geom1"

    comp = MagicMock()
    comp.tag.return_value = "comp1"

    def _comp_geom(*args, **kwargs):
        return [geom]

    comp.geom.side_effect = _comp_geom

    material_registry = MagicMock()
    material_registry.create.return_value = mat
    # iterating list(comp.material()) must yield ZERO existing materials
    # (the helper checks "name in existing_labels" before create).
    material_registry.__iter__ = lambda self: iter([])
    comp.material.return_value = material_registry

    # ----- jm + model -----
    jm = MagicMock()
    jm.component.return_value = [comp]

    fake_model = MagicMock()
    fake_model.java = jm
    return fake_model, mat, comp


def _wire_fake_model(monkeypatch, fake_model):
    monkeypatch.setattr(
        session_manager, "get_model",
        lambda name=None: fake_model,
    )


def _get_create_tool():
    from src.tools.material import register_material_tools

    captured = _capture_register(register_material_tools)
    return captured["material_create_user_defined"]


def _get_assign_tool():
    from src.tools.material import register_material_tools

    captured = _capture_register(register_material_tools)
    return captured["material_assign_to_domain"]


# ---------------------------------------------------------------------------
# §3.2 — material_create_user_defined default echo
# ---------------------------------------------------------------------------


def test_create_user_defined_default_selection_visible(monkeypatch):
    """1/3 — Default-all case: selection().entities(sdim) returns []
    so the echo must be the advisory string + selection_note guidance."""
    fake_model, _mat, _comp = _build_fake_model_for_create(
        sdim=2, selection_entities=[]
    )
    _wire_fake_model(monkeypatch, fake_model)

    tool = _get_create_tool()
    r = tool(
        name="M1",
        properties={"thermalconductivity": "1[W/(m*K)]"},
    )
    assert r["success"] is True, r
    mat = r["material"]
    assert "domain_selection" in mat
    assert "domain_selection_entities" in mat
    assert "selection_note" in mat

    assert mat["domain_selection_entities"] == []
    assert mat["domain_selection"] == "all (component-wide default)"
    assert "material_assign_to_domain" in mat["selection_note"]


def test_create_user_defined_explicit_entities_echo(monkeypatch):
    """2/3 — When COMSOL surfaces explicit entities (rare but possible
    if a future caller pre-restricts), echo them as a list and drop
    the advisory note (selection_note=None)."""
    fake_model, _mat, _comp = _build_fake_model_for_create(
        sdim=2, selection_entities=[1, 2]
    )
    _wire_fake_model(monkeypatch, fake_model)

    tool = _get_create_tool()
    r = tool(
        name="M1",
        properties={"thermalconductivity": "1[W/(m*K)]"},
    )
    assert r["success"] is True, r
    mat = r["material"]
    assert mat["domain_selection_entities"] == [1, 2]
    assert mat["domain_selection"] == [1, 2]
    assert mat["selection_note"] is None


def test_create_user_defined_selection_visualize_grace_degrades(monkeypatch):
    """3/3 — A failure inside ``selection().entities(sdim)`` (e.g.
    JPype boundary error on a degenerate component) must NOT break the
    create. Helper should fall back to default-all echo."""
    fake_model, _mat, _comp = _build_fake_model_for_create(
        sdim=2, selection_raises=True
    )
    _wire_fake_model(monkeypatch, fake_model)

    tool = _get_create_tool()
    r = tool(
        name="M1",
        properties={"thermalconductivity": "1[W/(m*K)]"},
    )
    assert r["success"] is True, r
    mat = r["material"]
    assert mat["domain_selection_entities"] == []
    assert mat["domain_selection"] == "all (component-wide default)"
    assert mat["selection_note"] is not None


# ---------------------------------------------------------------------------
# §3.3 — material_assign_to_domain schema consistency
# ---------------------------------------------------------------------------


def _build_fake_model_with_existing_material(material_name: str = "M1"):
    """Fake model where ``_find_material_in_model`` returns a Material
    whose ``selection().set(domains)`` records the call."""
    mat = MagicMock()
    mat.tag.return_value = "mat1"
    mat.label.return_value = material_name

    sel = MagicMock()
    mat.selection.return_value = sel

    comp = MagicMock()
    comp.tag.return_value = "comp1"
    # iterating list(comp.material()) must yield this one material so
    # _find_material_in_model can locate it by label.
    material_registry = MagicMock()
    material_registry.__iter__ = lambda self: iter([mat])
    comp.material.return_value = material_registry

    jm = MagicMock()
    jm.component.return_value = [comp]

    fake_model = MagicMock()
    fake_model.java = jm
    return fake_model, mat, sel


def test_assign_to_domain_response_includes_new_schema_fields(monkeypatch):
    """material_assign_to_domain response must include the new
    ``domain_selection_entities`` and ``selection_note`` fields, with
    entities==domains and note==None when assignment succeeded."""
    fake_model, mat, sel = _build_fake_model_with_existing_material("M1")
    _wire_fake_model(monkeypatch, fake_model)

    tool = _get_assign_tool()
    r = tool(material_name="M1", domain_selection=[1])
    assert r["success"] is True, r
    assert r["material"] == "M1"
    assert r["domain_selection"] == [1]
    assert r["domain_selection_entities"] == [1]
    assert r["selection_note"] is None
    sel.set.assert_called_once_with([1])
