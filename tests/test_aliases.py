"""Pure-Python tests for the alias normalizers introduced by:

- plans/mcp_mesh_study_tools_spec.md §4.1
  (study._normalize_study_type / _STUDY_TYPE_TO_STEP)
- plans/mcp_mesh_study_tools_spec.md §3.2
  (mesh._HAUTO_BY_SIZE_KIND)

These tests do not exercise the COMSOL Java bridge.

Note: The `space_dim_kind` normalizer block was removed by
plans/mcp_pr_c_fix_spec.md §3.1 — the underlying Java API has no such
property on components (it's a property of geometries). See
tests/test_axisymmetric_fix.py for the replacement coverage.
"""

import pytest

from src.tools.study import (
    _normalize_study_type,
    _STUDY_TYPE_TO_STEP,
)
from src.tools.mesh import _HAUTO_BY_SIZE_KIND


# ---------------------------------------------------------------------------
# study_type aliases
# ---------------------------------------------------------------------------

class TestStudyTypeNormalize:
    @pytest.mark.parametrize(
        "raw, canonical",
        [
            ("stationary", "stationary"),
            ("Stationary", "stationary"),
            ("STATIONARY", "stationary"),
            ("transient", "transient"),
            ("time_dependent", "time_dependent"),
            ("Time Dependent", "time_dependent"),
            ("Time-Dependent", "time_dependent"),
            ("eigenfrequency", "eigenfrequency"),
            ("Frequency Domain", "frequency_domain"),
            ("frequency_domain", "frequency_domain"),
            ("eigenvalue", "eigenvalue"),
        ],
    )
    def test_valid_aliases(self, raw, canonical):
        assert _normalize_study_type(raw) == canonical

    @pytest.mark.parametrize("raw", ["", None, "xxx", "stationarry", "  "])
    def test_invalid_aliases(self, raw):
        assert _normalize_study_type(raw) is None

    def test_step_tag_table(self):
        assert _STUDY_TYPE_TO_STEP["stationary"] == ("stat", "Stationary")
        assert _STUDY_TYPE_TO_STEP["transient"] == ("time", "Transient")
        assert _STUDY_TYPE_TO_STEP["time_dependent"] == ("time", "Transient")
        assert _STUDY_TYPE_TO_STEP["eigenfrequency"][1] == "Eigenfrequency"
        assert _STUDY_TYPE_TO_STEP["frequency_domain"][1] == "Frequency"
        assert _STUDY_TYPE_TO_STEP["eigenvalue"][1] == "Eigenvalue"


# ---------------------------------------------------------------------------
# mesh size kind table
# ---------------------------------------------------------------------------

class TestMeshSizeKind:
    def test_normal_is_5(self):
        assert _HAUTO_BY_SIZE_KIND["Normal"] == 5

    def test_finer_is_smaller_than_normal(self):
        # COMSOL convention: smaller hauto = finer mesh.
        assert _HAUTO_BY_SIZE_KIND["Finer"] < _HAUTO_BY_SIZE_KIND["Normal"]
        assert _HAUTO_BY_SIZE_KIND["Coarser"] > _HAUTO_BY_SIZE_KIND["Normal"]

    def test_full_table(self):
        for label in (
            "Extremely fine", "Extra fine", "Finer", "Fine", "Normal",
            "Coarse", "Coarser", "Extra coarse", "Extremely coarse",
        ):
            assert label in _HAUTO_BY_SIZE_KIND
        assert min(_HAUTO_BY_SIZE_KIND.values()) == 1
        assert max(_HAUTO_BY_SIZE_KIND.values()) == 9
