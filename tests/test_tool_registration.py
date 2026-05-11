"""Tool surface registration tests.

Verifies that each spec adds the expected new tool names to the
register functions, without booting the live MCP transport.
"""

import pytest

from src.tools import (
    register_session_tools,
    register_model_tools,
    register_parameter_tools,
    register_geometry_tools,
    register_physics_tools,
    register_material_tools,
    register_mesh_tools,
    register_study_tools,
    register_results_tools,
)
from src.knowledge.embedded import register_knowledge_tools
from src.knowledge.material_kb_tools import register_material_kb_tools


class StubMCP:
    """Captures @mcp.tool() registrations as a name -> callable dict."""
    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _gather() -> dict[str, dict]:
    """Run every register_* once and return the per-namespace tool maps."""
    namespaces = {
        "session":   register_session_tools,
        "model":     register_model_tools,
        "parameter": register_parameter_tools,
        "geometry":  register_geometry_tools,
        "physics":   register_physics_tools,
        "material":  register_material_tools,
        "mesh":      register_mesh_tools,
        "study":     register_study_tools,
        "results":   register_results_tools,
        "knowledge": register_knowledge_tools,
        "material_kb": register_material_kb_tools,
    }
    out = {}
    for ns, fn in namespaces.items():
        stub = StubMCP()
        fn(stub)
        out[ns] = stub.tools
    return out


def test_material_namespace_has_5_tools():
    """Spec mcp_material_tools_spec.md §3.2 — 5 tools in tools/material.py."""
    by_ns = _gather()
    expected = {
        "material_create_user_defined",
        "material_create_from_kb",
        "material_assign_to_domain",
        "material_get_property",
        "material_list",
    }
    assert expected <= set(by_ns["material"].keys())
    assert len(by_ns["material"]) == 5


def test_material_kb_namespace_has_2_tools():
    """Spec mcp_material_tools_spec.md §4.2 — 2 tools."""
    by_ns = _gather()
    expected = {"kb_material_list", "kb_material_get"}
    assert expected <= set(by_ns["material_kb"].keys())
    assert len(by_ns["material_kb"]) == 2


def test_mesh_has_new_tools():
    """Spec mcp_mesh_study_tools_spec.md §3.2 — mesh_add_sequence is core."""
    by_ns = _gather()
    expected = {"mesh_add_sequence", "mesh_set_global_size", "mesh_remove"}
    assert expected <= set(by_ns["mesh"].keys())


def test_study_has_new_tools():
    """Spec mcp_mesh_study_tools_spec.md §4 — study_create is core."""
    by_ns = _gather()
    expected = {"study_create", "study_add_step", "study_remove"}
    assert expected <= set(by_ns["study"].keys())


def test_total_tool_count_matches_spec_after_all_specs():
    """After this PR, comsol61-ops exposes the documented increments:
        +5 material + 2 material_kb + 3 mesh + 3 study = +13, plus
        +1 physics_set_property added by the Pilot 08 Solid Mechanics
        fix (mcp_solid_mechanics_fix_spec.md §3.3).
    Spec rationale lives in plans/code_session_routing_guide.md §1; the
    baseline number quoted there (82) was an estimate from Pilot 04 era
    that pre-dated subsequent counting; the real pre-PR count from this
    repo is 75 (session 4 + model 11 + parameter 5 + geometry 14 +
    physics 17 + mesh 3 + study 8 + results 8 + knowledge 5).  This
    test asserts the deltas, which is what the spec actually requires.
    """
    by_ns = _gather()
    counts = {k: len(v) for k, v in by_ns.items()}

    # Per-namespace expected deltas vs. pre-spec baseline.
    assert counts["material"] == 5,    f"material: {counts}"
    assert counts["material_kb"] == 2, f"material_kb: {counts}"
    assert counts["mesh"] == 6,        f"mesh: 3 baseline + 3 new = 6, got {counts['mesh']}"
    assert counts["study"] == 11,      f"study: 8 baseline + 3 new = 11, got {counts['study']}"
    assert counts["physics"] == 18,    (
        f"physics: 17 baseline + 1 new (physics_set_property) = 18, "
        f"got {counts['physics']}"
    )

    # Total grew by 14 from the baseline (5+2+3+3+1).
    assert sum(counts.values()) == 89, (
        f"Expected total 89 (75 baseline + 14 new). Got {sum(counts.values())}: {counts}"
    )


def test_geometry_create_has_axisymmetric_kwarg_no_strict_dim_check():
    """Spec mcp_pr_c_fix_spec.md §3.2 — `strict_dim_check` is removed
    (component-level dim check was unworkable since COMSOL components
    have no SDimSpec property), replaced by an `axisymmetric: bool`
    kwarg that maps to the geometry-level Java boolean property."""
    import inspect
    by_ns = _gather()
    sig = inspect.signature(by_ns["geometry"]["geometry_create"])
    assert "strict_dim_check" not in sig.parameters
    assert "axisymmetric" in sig.parameters
    assert sig.parameters["axisymmetric"].default is False
    assert "space_dimension" in sig.parameters
    assert sig.parameters["space_dimension"].default == 3


def test_model_create_component_no_space_dim_kind_kwarg():
    """Spec mcp_pr_c_fix_spec.md §3.1 — `space_dim_kind` is removed
    (component has no SDimSpec property in the Java API; spatial
    dimension is set via geometry_create instead). Only the legacy
    `set_active` kwarg remains alongside `component_name` / `model_name`.
    """
    import inspect
    by_ns = _gather()
    sig = inspect.signature(by_ns["model"]["model_create_component"])
    assert "space_dim_kind" not in sig.parameters
    assert "set_active" in sig.parameters
    assert sig.parameters["set_active"].default is True
    assert "component_name" in sig.parameters
