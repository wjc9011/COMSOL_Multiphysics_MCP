"""Pure-Python tests for the alias normalizers introduced by:

- plans/geometry_axisymmetric_support_spec.md §3.1 / §3.2 / §5.1
  (model._normalize_space_dim_kind)
- plans/mcp_mesh_study_tools_spec.md §4.1
  (study._normalize_study_type / _STUDY_TYPE_TO_STEP)
- plans/mcp_mesh_study_tools_spec.md §3.2
  (mesh._HAUTO_BY_SIZE_KIND)

These tests do not exercise the COMSOL Java bridge.
"""

import pytest

from src.tools.model import (
    _normalize_space_dim_kind,
    _SPACE_DIM_KIND_TO_JAVA,
    _SPACE_DIM_KIND_TO_INT,
    _supported_space_dim_kinds,
)
from src.tools.study import (
    _normalize_study_type,
    _STUDY_TYPE_TO_STEP,
)
from src.tools.mesh import _HAUTO_BY_SIZE_KIND


# ---------------------------------------------------------------------------
# space_dim_kind
# ---------------------------------------------------------------------------

class TestSpaceDimKindNormalize:
    @pytest.mark.parametrize(
        "raw, canonical",
        [
            ("3D", "3D"),
            ("3d", "3D"),
            ("2D", "2D"),
            ("2D-Cartesian", "2D"),
            ("2D Cartesian", "2D"),
            ("2D_Cartesian", "2D"),
            ("2D-Axisymmetric", "2D-Axisymmetric"),
            ("2D Axisymmetric", "2D-Axisymmetric"),
            ("2D_Axisymmetric", "2D-Axisymmetric"),
            ("2d-axisymmetric", "2D-Axisymmetric"),
            ("2D-Axisym", "2D-Axisymmetric"),
            ("2D-axi", "2D-Axisymmetric"),
            ("2DAxi", "2DAxi"),
            ("1D", "1D"),
            ("1D-Axisymmetric", "1D-Axisymmetric"),
            ("1DAxi", "1DAxi"),
        ],
    )
    def test_valid_aliases(self, raw, canonical):
        assert _normalize_space_dim_kind(raw) == canonical

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "2.5D", "axi", "axisymmetric", "4D", None, "garbage"],
    )
    def test_invalid_aliases(self, raw):
        # Non-string inputs return None, not raise.
        assert _normalize_space_dim_kind(raw) is None

    def test_all_canonical_have_java_mapping(self):
        for kind in _supported_space_dim_kinds():
            assert kind in _SPACE_DIM_KIND_TO_JAVA
            assert kind in _SPACE_DIM_KIND_TO_INT

    def test_axi_kinds_map_to_axisymmetric_java(self):
        assert _SPACE_DIM_KIND_TO_JAVA["2D-Axisymmetric"] == "AxisymmetricSpaceDim2DAxi"
        assert _SPACE_DIM_KIND_TO_JAVA["1D-Axisymmetric"] == "AxisymmetricSpaceDim1DAxi"

    def test_dim_int_lookup(self):
        assert _SPACE_DIM_KIND_TO_INT["1D"] == 1
        assert _SPACE_DIM_KIND_TO_INT["1D-Axisymmetric"] == 1
        assert _SPACE_DIM_KIND_TO_INT["2D"] == 2
        assert _SPACE_DIM_KIND_TO_INT["2D-Axisymmetric"] == 2
        assert _SPACE_DIM_KIND_TO_INT["3D"] == 3


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
