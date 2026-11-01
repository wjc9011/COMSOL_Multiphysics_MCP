"""Solver-free surrogate manifest and group-disjoint split tests."""

from __future__ import annotations

import pytest

from comsol_mcp.surrogate.manifests import (
    build_dataset_manifest,
    build_schema_manifest,
    build_training_transforms,
    canonical_manifest_sha256,
    validate_dataset_manifest,
    validate_group_disjoint_split,
    validate_schema_manifest,
    validate_training_transforms,
)

SOURCE_SHA = "a" * 64


def test_schema_manifest_roundtrip_and_hash_stability() -> None:
    manifest = build_schema_manifest(
        schema_id="waveopt-scalar-v1",
        feature_names=["w", "h", "gap"],
        target_names=["qoi"],
    )
    assert manifest["input_dim"] == 3
    assert manifest["target_dim"] == 1
    assert manifest["continuous_only"] is True
    assert validate_schema_manifest(manifest)["schema_id"] == "waveopt-scalar-v1"
    again = build_schema_manifest(
        schema_id="waveopt-scalar-v1",
        feature_names=["w", "h", "gap"],
        target_names=["qoi"],
    )
    assert again["manifest_sha256"] == manifest["manifest_sha256"]
    assert (
        canonical_manifest_sha256({k: v for k, v in manifest.items() if k != "manifest_sha256"})
        == manifest["manifest_sha256"]
    )


def test_schema_manifest_rejects_out_of_bound_inputs() -> None:
    with pytest.raises(ValueError, match="2-12"):
        build_schema_manifest(
            schema_id="bad",
            feature_names=["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m"],
            target_names=["qoi"],
        )
    with pytest.raises(ValueError, match="2-32"):
        build_schema_manifest(schema_id="bad", feature_names=["only"], target_names=["q"])


def test_training_transforms_fit_only_on_train() -> None:
    transforms = build_training_transforms(
        transform_id="std-v1",
        transforms=[{"kind": "standardize", "columns": ["x0", "x1"]}],
    )
    assert transforms["fit_split"] == "train"
    validate_training_transforms(transforms)
    broken = dict(transforms)
    broken.pop("manifest_sha256")
    broken["fit_split"] = "validation"
    sealed = {
        **broken,
        "manifest_sha256": canonical_manifest_sha256(broken),
    }
    with pytest.raises(ValueError, match="fit only on train"):
        validate_training_transforms(sealed)


def _dataset_kwargs() -> dict:
    return {
        "dataset_id": "ds-001",
        "source_kind": "campaign",
        "source_identity_sha256": SOURCE_SHA,
        "candidate_ids": ["c1", "c2", "c3", "c4"],
        "row_ids": ["r1", "r2", "r3", "r4", "r5"],
        "leakage_groups": [
            {"group_id": "g1", "row_ids": ["r1", "r2"]},
            {"group_id": "g2", "row_ids": ["r3"]},
            {"group_id": "g3", "row_ids": ["r4", "r5"]},
        ],
        "split_assignments": [
            {"row_id": "r1", "split": "train"},
            {"row_id": "r2", "split": "train"},
            {"row_id": "r3", "split": "validation"},
            {"row_id": "r4", "split": "test"},
        ],
        "ineligible_rows": [
            {"row_id": "r5", "reason_code": "label_missing", "detail": "QoI absent"}
        ],
    }


def test_dataset_manifest_retains_ineligible_rows_and_is_sealed() -> None:
    manifest = build_dataset_manifest(**_dataset_kwargs())
    assert len(manifest["ineligible_rows"]) == 1
    assert manifest["ineligible_rows"][0]["reason_code"] == "label_missing"
    validate_dataset_manifest(manifest)
    tampered = dict(manifest)
    tampered["row_ids"] = ["r1"]
    with pytest.raises(ValueError):
        validate_dataset_manifest(tampered)


def test_group_disjoint_split_rejects_cross_split_leakage() -> None:
    kwargs = _dataset_kwargs()
    kwargs["split_assignments"] = [
        {"row_id": "r1", "split": "train"},
        {"row_id": "r2", "split": "test"},
        {"row_id": "r3", "split": "validation"},
        {"row_id": "r4", "split": "test"},
        {"row_id": "r5", "split": "scientific_holdout"},
    ]
    kwargs["ineligible_rows"] = []
    manifest = build_dataset_manifest(**kwargs)
    with pytest.raises(ValueError, match="multiple splits"):
        validate_group_disjoint_split(manifest)


def test_group_disjoint_split_allows_unassigned_ineligible_rows() -> None:
    kwargs = _dataset_kwargs()
    kwargs["split_assignments"] = [
        {"row_id": "r1", "split": "train"},
        {"row_id": "r2", "split": "train"},
        {"row_id": "r3", "split": "validation"},
    ]
    kwargs["ineligible_rows"] = [
        {"row_id": "r4", "reason_code": "fem_failed", "detail": "solver error"},
        {"row_id": "r5", "reason_code": "label_missing", "detail": "QoI absent"},
    ]
    summary = validate_group_disjoint_split(build_dataset_manifest(**kwargs))
    assert summary["group_disjoint"] is True
    assert summary["ineligible_count"] == 2
    assert summary["split_counts"]["train"] == 2
    assert summary["split_counts"]["scientific_holdout"] == 0


def test_group_disjoint_split_rejects_an_eligible_row_in_no_group() -> None:
    """Regression: a row outside every leakage group must not certify as disjoint.

    The group loop iterates over *groups*, so a row belonging to none of them was
    never examined and the verdict still claimed `group_disjoint: True`. Such a
    row has unknown leakage structure, so the claim was vacuous for it.
    """
    kwargs = _dataset_kwargs()
    # r3 is a declared row, eligible, and assigned to a split, but no leakage
    # group covers it: g2 (which held it) is removed while r3 stays in row_ids.
    kwargs["leakage_groups"] = [
        {"group_id": "g1", "row_ids": ["r1", "r2"]},
        {"group_id": "g3", "row_ids": ["r4"]},
    ]
    kwargs["row_ids"] = ["r1", "r2", "r3", "r4"]
    kwargs["split_assignments"] = [
        {"row_id": "r1", "split": "train"},
        {"row_id": "r2", "split": "train"},
        {"row_id": "r3", "split": "validation"},
        {"row_id": "r4", "split": "test"},
    ]
    kwargs["ineligible_rows"] = []
    manifest = build_dataset_manifest(**kwargs)
    with pytest.raises(ValueError, match="must belong to a leakage group"):
        validate_group_disjoint_split(manifest)


def test_group_disjoint_split_still_allows_an_ungrouped_ineligible_row() -> None:
    """The new rule must not block a row that is legitimately out of scope.

    An ineligible row is never fitted, so it needs no leakage group; only
    eligible assigned rows must be grouped.
    """
    kwargs = _dataset_kwargs()
    kwargs["leakage_groups"] = [{"group_id": "g1", "row_ids": ["r1", "r2"]}]
    kwargs["split_assignments"] = [
        {"row_id": "r1", "split": "train"},
        {"row_id": "r2", "split": "train"},
    ]
    kwargs["ineligible_rows"] = [
        {"row_id": "r3", "reason_code": "fem_failed", "detail": "solver error"},
        {"row_id": "r4", "reason_code": "label_missing", "detail": "QoI absent"},
        {"row_id": "r5", "reason_code": "label_missing", "detail": "QoI absent"},
    ]
    summary = validate_group_disjoint_split(build_dataset_manifest(**kwargs))
    assert summary["group_disjoint"] is True
    assert summary["ineligible_count"] == 3


def test_module_is_solver_free() -> None:
    import comsol_mcp.surrogate.manifests as mod

    with open(mod.__file__, encoding="utf-8") as handle:
        source = handle.read()
    for banned in (
        "import mph",
        "from mph",
        "import jpype",
        "from jpype",
        "import java",
        "from java",
        "import comsol",
        "from comsol.",
    ):
        assert banned not in source


def test_ineligible_rows_must_not_carry_split_assignments() -> None:
    """L-05: an ineligible row with a split assignment is contradictory.

    Before this contract the builder accepted it, the group-disjoint check
    let the ineligible assignment reject (or holdout-pollute) real splits,
    and split_counts counted unusable rows. Both the builder and the
    group-disjoint validator now refuse fail-closed.
    """
    kwargs = _dataset_kwargs()
    kwargs["split_assignments"] = [
        {"row_id": "r1", "split": "train"},
        {"row_id": "r2", "split": "train"},
        {"row_id": "r3", "split": "validation"},
        {"row_id": "r4", "split": "test"},
        {"row_id": "r5", "split": "scientific_holdout"},
    ]
    kwargs["ineligible_rows"] = [
        {"row_id": "r5", "reason_code": "label_missing", "detail": "QoI absent"}
    ]
    with pytest.raises(ValueError, match="must not carry a split assignment"):
        build_dataset_manifest(**kwargs)

    # The same contract holds for a pre-built manifest reaching the
    # group-disjoint validator.
    manifest = build_dataset_manifest(
        **{
            **kwargs,
            "split_assignments": [
                {"row_id": "r1", "split": "train"},
                {"row_id": "r2", "split": "train"},
                {"row_id": "r3", "split": "validation"},
                {"row_id": "r4", "split": "test"},
            ],
        }
    )
    tampered = dict(manifest)
    tampered["split_assignments"] = [
        *manifest["split_assignments"],
        {"row_id": "r5", "split": "scientific_holdout"},
    ]
    from comsol_mcp.surrogate.manifests import _finalize

    body = {key: value for key, value in tampered.items() if key != "manifest_sha256"}
    with pytest.raises(ValueError, match="must not carry a split assignment"):
        validate_group_disjoint_split(_finalize(body))
