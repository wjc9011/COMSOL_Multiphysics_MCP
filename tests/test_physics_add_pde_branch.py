"""Pure-Python tests for the Math/PDE branch added to physics_add.

Spec: plans/mcp_physics_add_pde_branch_spec.md (§3, §5).

The spec adds the Mathematics > PDE Interfaces branch to two tables in
``src/tools/physics.py``:

- ``PHYSICS_INTERFACES`` — new ``"Mathematics"`` category with 10 items
  for discoverability via ``physics_get_available()``.
- ``PHYSICS_TYPE_ALIASES`` — 31 new alias entries (10 PDE types ×
  3 aliases each, plus ConvectionDiffusionEquation's extra alias key
  ``convection_diffusion_equation``) for ``physics_add(physics_type=...)``.

These tests verify the alias resolver and discoverability surface
without spinning up the COMSOL Java bridge. Live integration coverage
lives in the Cowork-side ``runs\\sanity_pde_branch_v1\\`` directory
(see spec §6.1).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.tools.physics import (
    PHYSICS_INTERFACES,
    PHYSICS_TYPE_ALIASES,
    _resolve_physics_type,
    register_physics_tools,
)
from src.tools.session import session_manager


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _capture_register():
    """Capture registered MCP tools without a real FastMCP instance.

    Mirrors the pattern from test_set_material_deprecation.py.
    """
    captured: dict = {}

    class _Stub:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    register_physics_tools(_Stub())
    return captured


# ---------------------------------------------------------------------------
# §5.1 — Unit tests (10 cases)
# ---------------------------------------------------------------------------

class TestPDEBranchAliases:
    """Spec §5.1 unit tests 1–10."""

    # ---- 1. No alias key collisions (spec §3.3) -------------------------
    def test_alias_no_collision(self):
        """1/10 — Adding the Math/PDE entries must not silently shadow
        any of the existing 19 base-physics aliases. dict literals
        collapse duplicate keys, so we re-derive the key list from the
        module source and assert ``len(list) == len(set)``.
        """
        import re
        from pathlib import Path
        src_path = Path(__file__).resolve().parent.parent / "src" / "tools" / "physics.py"
        src = src_path.read_text(encoding="utf-8")
        m = re.search(
            r"PHYSICS_TYPE_ALIASES.*?=\s*\{(.*?)^\}",
            src,
            re.S | re.M,
        )
        assert m is not None, "PHYSICS_TYPE_ALIASES dict literal not found"
        body = m.group(1)
        keys = re.findall(r'^\s*"([^"]+)"\s*:', body, re.M)
        # 19 base entries + 31 new Math/PDE entries = 50 total.
        assert len(keys) == 50, f"expected 50 raw entries, got {len(keys)}"
        assert len(keys) == len(set(keys)), (
            "Alias key collision in PHYSICS_TYPE_ALIASES: "
            f"{sorted({k for k in keys if keys.count(k) > 1})}"
        )
        # Live dict equally has no missing entries.
        assert len(PHYSICS_TYPE_ALIASES) == 50

    # ---- 2–4. CoefficientFormPDE — 3-alias resolution ------------------
    def test_resolve_coefficient_form_pde_pascal(self):
        """2/10 — PascalCase canonical name (matches FEABench ground_truth)."""
        assert _resolve_physics_type("CoefficientFormPDE") == (
            "CoefficientFormPDE", "c",
        )

    def test_resolve_coefficient_form_pde_snake(self):
        """3/10 — snake_case key (matches PHYSICS_INTERFACES output)."""
        assert _resolve_physics_type("coefficient_form_pde") == (
            "CoefficientFormPDE", "c",
        )

    def test_resolve_coefficient_form_pde_short(self):
        """4/10 — short tag (matches KB scripting_completion id="c")."""
        assert _resolve_physics_type("c") == ("CoefficientFormPDE", "c")

    # ---- 5. GeneralFormPDE — 3-alias ----------------------------------
    @pytest.mark.parametrize(
        "alias",
        ["g", "general_form_pde", "GeneralFormPDE"],
    )
    def test_resolve_general_form_pde(self, alias):
        """5/10 — All 3 GeneralFormPDE aliases resolve identically."""
        assert _resolve_physics_type(alias) == ("GeneralFormPDE", "g")

    # ---- 6. WeakFormPDE — 3-alias -------------------------------------
    @pytest.mark.parametrize(
        "alias",
        ["w", "weak_form_pde", "WeakFormPDE"],
    )
    def test_resolve_weak_form_pde(self, alias):
        """6/10 — All 3 WeakFormPDE aliases resolve identically."""
        assert _resolve_physics_type(alias) == ("WeakFormPDE", "w")

    # ---- 7. ConvectionDiffusionEquation — 4 aliases (incl. cdeq) ------
    @pytest.mark.parametrize(
        "alias",
        [
            "cdeq",
            "convection_diffusion",
            "convection_diffusion_equation",
            "ConvectionDiffusionEquation",
        ],
    )
    def test_resolve_convection_diffusion(self, alias):
        """7/10 — All 4 ConvectionDiffusionEquation aliases resolve to the
        same (class, tag) tuple. ``cdeq`` is the short tag from KB
        scripting_completion id; ``convection_diffusion`` is the
        PHYSICS_INTERFACES snake_case key.
        """
        assert _resolve_physics_type(alias) == (
            "ConvectionDiffusionEquation", "cdeq",
        )

    # ---- 8. Boundary / edge / point variants — 6 types ----------------
    @pytest.mark.parametrize(
        "alias, expected_class, expected_tag",
        [
            # CoefficientFormBoundaryPDE
            ("cb", "CoefficientFormBoundaryPDE", "cb"),
            ("coefficient_form_boundary_pde", "CoefficientFormBoundaryPDE", "cb"),
            ("CoefficientFormBoundaryPDE", "CoefficientFormBoundaryPDE", "cb"),
            # CoefficientFormEdgePDE
            ("ce", "CoefficientFormEdgePDE", "ce"),
            ("coefficient_form_edge_pde", "CoefficientFormEdgePDE", "ce"),
            ("CoefficientFormEdgePDE", "CoefficientFormEdgePDE", "ce"),
            # CoefficientFormPointPDE
            ("cp", "CoefficientFormPointPDE", "cp"),
            ("coefficient_form_point_pde", "CoefficientFormPointPDE", "cp"),
            ("CoefficientFormPointPDE", "CoefficientFormPointPDE", "cp"),
            # GeneralFormBoundaryPDE
            ("gb", "GeneralFormBoundaryPDE", "gb"),
            ("general_form_boundary_pde", "GeneralFormBoundaryPDE", "gb"),
            ("GeneralFormBoundaryPDE", "GeneralFormBoundaryPDE", "gb"),
            # GeneralFormEdgePDE
            ("ge", "GeneralFormEdgePDE", "ge"),
            ("general_form_edge_pde", "GeneralFormEdgePDE", "ge"),
            ("GeneralFormEdgePDE", "GeneralFormEdgePDE", "ge"),
            # GeneralFormPointPDE
            ("gp", "GeneralFormPointPDE", "gp"),
            ("general_form_point_pde", "GeneralFormPointPDE", "gp"),
            ("GeneralFormPointPDE", "GeneralFormPointPDE", "gp"),
        ],
    )
    def test_resolve_boundary_variants(self, alias, expected_class, expected_tag):
        """8/10 — All 18 boundary/edge/point alias entries resolve to the
        expected (class, tag) tuple (6 types × 3 aliases each)."""
        assert _resolve_physics_type(alias) == (expected_class, expected_tag)

    # ---- 9. physics_get_available exposes Mathematics ------------------
    def test_physics_get_available_exposes_mathematics(self):
        """9/10 — ``physics_get_available()`` response must include a
        ``"Mathematics"`` category with the 10 Math/PDE items so agents
        can discover the branch without reading the source."""
        tools = _capture_register()
        result = tools["physics_get_available"]()
        assert result["success"] is True
        interfaces = result["interfaces"]
        assert "Mathematics" in interfaces, (
            f"Mathematics category missing; got categories: "
            f"{sorted(interfaces.keys())!r}"
        )
        math = interfaces["Mathematics"]
        assert len(math) == 10, f"expected 10 items, got {len(math)}: {math!r}"
        for key in (
            "coefficient_form_pde",
            "general_form_pde",
            "weak_form_pde",
            "convection_diffusion",
            "coefficient_form_boundary_pde",
            "coefficient_form_edge_pde",
            "coefficient_form_point_pde",
            "general_form_boundary_pde",
            "general_form_edge_pde",
            "general_form_point_pde",
        ):
            assert key in math, f"missing Mathematics.{key}"

    # ---- 10. Unknown PDE type still rejected --------------------------
    def test_unknown_pde_type_still_rejected(self):
        """10/10 — Unknown / fictitious PDE names must still resolve to
        (None, None) so ``_add_physics_interface`` surfaces the
        "Unknown physics type" error. The Known: list must include the
        new Math/PDE aliases so the error is self-documenting.
        """
        assert _resolve_physics_type("NonExistentPDE") == (None, None)
        assert _resolve_physics_type("HelmholtzPDE") == (None, None)
        assert _resolve_physics_type("") == (None, None)

        known = sorted(set(PHYSICS_TYPE_ALIASES))
        # 19 base + 31 new = 50 entries (see test 1 for the count check).
        assert len(known) == 50
        # The new Math/PDE canonical names show up in the Known list.
        for key in (
            "CoefficientFormPDE", "GeneralFormPDE", "WeakFormPDE",
            "ConvectionDiffusionEquation",
            "CoefficientFormBoundaryPDE", "CoefficientFormEdgePDE",
            "CoefficientFormPointPDE",
            "GeneralFormBoundaryPDE", "GeneralFormEdgePDE",
            "GeneralFormPointPDE",
            "c", "g", "w", "cdeq", "cb", "ce", "cp", "gb", "ge", "gp",
        ):
            assert key in known, f"{key!r} missing from PHYSICS_TYPE_ALIASES"


# ---------------------------------------------------------------------------
# §5.2 — Integration test (1 case, mock-based)
# ---------------------------------------------------------------------------

class TestPDEBranchIntegration:
    """Spec §5.2 integration coverage. The spec proposes a live-COMSOL
    test that recreates Pilot 09 / comsol_82 Step 2.8 (1D Interval
    geometry + ``physics_add("CoefficientFormPDE", tag="c")``). The
    rest of this test suite is mock-based (the repo has no live-JVM CI
    fixture), so we wire a fake Java bridge that emulates the
    ``comp.physics().create(tag, type, geom_tag)`` contract from
    FEABench ``comsol_82.json`` ground_truth — verifying the resolver
    feeds the right (class, tag, geom) triple into the create call.
    """

    def test_physics_add_coefficient_form_pde_end_to_end(self, monkeypatch):
        """1/1 — physics_add('CoefficientFormPDE', tag='c') routes through
        _resolve_physics_type, _get_comp_and_geom_tag, and finally
        ``comp.physics().create("c", "CoefficientFormPDE", "geom1")``
        — matching FEABench comsol_82 ground_truth ::

            model.component("comp1").physics().create(
                "c", "CoefficientFormPDE", "geom1");
        """
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

        monkeypatch.setattr(
            session_manager, "get_model",
            lambda name=None: fake_model,
        )

        tools = _capture_register()
        physics_add = tools["physics_add"]

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


# ---------------------------------------------------------------------------
# §5.3 — Regression tests (4 cases) — 6 base physics unaffected
# ---------------------------------------------------------------------------

class TestBasePhysicsRegression:
    """Spec §5.3 — verify the 6 pre-existing base physics aliases all
    still resolve to the same (class, tag) tuples after the Math/PDE
    table extension. Single-PR regression budget covers Pilot 06/07/08
    sequences (heat / solid mechanics / electrostatics / laminar flow).
    """

    @pytest.mark.parametrize(
        "alias",
        ["ht", "heat_transfer", "HeatTransfer"],
    )
    def test_physics_add_heat_transfer_still_works(self, alias):
        """1/4 — HeatTransfer 3-alias resolution preserved (Pilot 06/07)."""
        assert _resolve_physics_type(alias) == ("HeatTransfer", "ht")

    @pytest.mark.parametrize(
        "alias",
        ["solid", "solid_mechanics", "SolidMechanics"],
    )
    def test_physics_add_solid_mechanics_still_works(self, alias):
        """2/4 — SolidMechanics 3-alias resolution preserved (Pilot 08)."""
        assert _resolve_physics_type(alias) == ("SolidMechanics", "solid")

    @pytest.mark.parametrize(
        "alias",
        ["es", "electrostatic", "electrostatics", "Electrostatics"],
    )
    def test_physics_add_electrostatics_still_works(self, alias):
        """3/4 — Electrostatics 4-alias resolution preserved."""
        assert _resolve_physics_type(alias) == ("Electrostatics", "es")

    @pytest.mark.parametrize(
        "alias",
        ["spf", "laminar_flow", "LaminarFlow"],
    )
    def test_physics_add_laminar_flow_still_works(self, alias):
        """4/4 — LaminarFlow 3-alias resolution preserved."""
        assert _resolve_physics_type(alias) == ("LaminarFlow", "spf")
