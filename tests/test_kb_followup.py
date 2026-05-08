"""Unit tests for embedded.py kb_followup field.

Spec: plans/comsol_kb_integration_spec.md §6.1.
"""

import pytest

from src.knowledge.embedded import (
    get_docs,
    list_docs,
    get_physics_guide,
    get_troubleshoot,
    get_best_practices,
)


def test_docs_get_has_kb_followup_for_known_topic():
    r = get_docs("physics_guide")
    assert r["success"] is True
    assert "kb_followup" in r
    assert isinstance(r["kb_followup"], list)
    for item in r["kb_followup"]:
        assert {"purpose", "tool", "args_template", "expected"} <= set(item)


def test_physics_get_guide_followup_includes_module():
    r = get_physics_guide("heat_transfer")
    assert r["success"] is True
    assert "kb_followup" in r
    assert any(
        it["args_template"].get("module") == "Heat_Transfer_Module"
        for it in r["kb_followup"]
    )


def test_troubleshoot_followup_uses_resources_text():
    r = get_troubleshoot("solver_no_convergence", context="2D heat")
    assert r["success"] is True
    assert any(
        it["args_template"].get("source") == "resources_text"
        for it in r["kb_followup"]
    )


def test_modeling_best_practices_followup_returns_module_overview_tool():
    r = get_best_practices("solver")
    assert r["success"] is True
    assert any(
        it["tool"] == "kb_get_module_overview" for it in r["kb_followup"]
    )


def test_docs_list_global_followup_uses_semantic_search():
    r = list_docs()
    assert r["success"] is True
    assert any(
        it["tool"] == "kb_semantic_search" for it in r["kb_followup"]
    )


def test_failure_branch_unchanged():
    r = get_docs("not_a_real_topic")
    assert r["success"] is False
    assert "kb_followup" not in r


def test_physics_get_guide_unmapped_type_no_kb_module():
    """Defensive: a TOPIC_GUIDES entry without _PHYSICS_TO_KB_MODULE
    coverage still returns a list (possibly empty) so the schema stays
    consistent."""
    # All 4 TOPIC_GUIDES are mapped today, so this is a sanity check
    # against accidental KeyError if the table goes out of sync.
    r = get_physics_guide("electrostatics")
    assert r["success"] is True
    assert isinstance(r.get("kb_followup"), list)


def test_troubleshoot_no_context_still_has_resources_followup():
    r = get_troubleshoot("memory_error")
    assert r["success"] is True
    sources = [it["args_template"].get("source") for it in r["kb_followup"]]
    assert "resources_text" in sources
