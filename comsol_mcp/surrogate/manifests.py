"""Closed solver-free surrogate dataset and split manifests.

This module never imports COMSOL, Java, MPh, or network clients. It only
normalizes versioned JSON-compatible manifests and deterministic hashes used
by the current surrogate lifecycle.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal, TypedDict

from comsol_mcp.durable.canonical import canonical_sha256_v1

SCHEMA_VERSION = "1.0.0"
MAX_ROWS = 4096
MAX_FEATURES = 32
MAX_TRANSFORMS = 32
SPLIT_NAMES = ("train", "validation", "test", "scientific_holdout")
ROW_STATUS = ("eligible", "ineligible")


class IneligibleRow(TypedDict):
    row_id: str
    reason_code: str
    detail: str


class LeakageGroup(TypedDict):
    group_id: str
    row_ids: list[str]


class SplitAssignment(TypedDict):
    row_id: str
    split: str


class SurrogateDatasetManifest(TypedDict):
    schema: str
    schema_version: str
    dataset_id: str
    source_kind: str
    source_identity_sha256: str
    candidate_ids: list[str]
    row_ids: list[str]
    leakage_groups: list[LeakageGroup]
    split_assignments: list[SplitAssignment]
    ineligible_rows: list[IneligibleRow]
    manifest_sha256: str


class SurrogateSchemaManifest(TypedDict):
    schema: str
    schema_version: str
    schema_id: str
    feature_names: list[str]
    target_names: list[str]
    input_dim: int
    target_dim: int
    continuous_only: bool
    manifest_sha256: str


class SurrogateTrainingTransforms(TypedDict):
    schema: str
    schema_version: str
    transform_id: str
    fit_split: Literal["train"]
    transforms: list[dict[str, Any]]
    manifest_sha256: str


def _require_hex64(name: str, value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise ValueError(f"{name} must be a 64-character hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be a 64-character hex digest") from exc
    return value.lower()


def _require_str(name: str, value: Any, *, max_len: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ValueError(f"{name} must be a non-empty string up to {max_len}")
    return value


def _require_unique(name: str, values: Sequence[str]) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must be unique")
    return list(values)


def canonical_manifest_sha256(body: Mapping[str, Any]) -> str:
    """Hash one manifest body with the durable v1 canonical encoding."""
    digest = canonical_sha256_v1(dict(body))
    return str(digest)


def _finalize(body: dict[str, Any]) -> dict[str, Any]:
    manifest = {**body, "manifest_sha256": canonical_manifest_sha256(body)}
    return manifest


def _validate_sealed(manifest: Mapping[str, Any], required: Iterable[str]) -> dict[str, Any]:
    keys = set(manifest)
    required_keys = set(required) | {"manifest_sha256"}
    if keys != required_keys:
        raise ValueError("manifest keys are closed")
    body = {key: manifest[key] for key in required if key in manifest}
    expected = canonical_manifest_sha256(body)
    if manifest["manifest_sha256"] != expected:
        raise ValueError("manifest_sha256 mismatch")
    return dict(manifest)


def build_schema_manifest(
    *,
    schema_id: str,
    feature_names: Sequence[str],
    target_names: Sequence[str],
) -> SurrogateSchemaManifest:
    features = _require_unique("feature_names", [str(item) for item in feature_names])
    targets = _require_unique("target_names", [str(item) for item in target_names])
    if not 2 <= len(features) <= MAX_FEATURES:
        raise ValueError("feature_names must contain 2-32 entries")
    if not 1 <= len(targets) <= 8:
        raise ValueError("target_names must contain 1-8 entries")
    if not 2 <= len(features) <= 12:
        raise ValueError("input_dim must stay inside the 2-12 continuous first target")
    body = {
        "schema": "comsol_mcp.surrogate_schema_manifest",
        "schema_version": SCHEMA_VERSION,
        "schema_id": _require_str("schema_id", schema_id),
        "feature_names": features,
        "target_names": targets,
        "input_dim": len(features),
        "target_dim": len(targets),
        "continuous_only": True,
    }
    return _finalize(body)  # type: ignore[return-value]


def validate_schema_manifest(value: Any) -> SurrogateSchemaManifest:
    if not isinstance(value, Mapping):
        raise ValueError("schema manifest must be a mapping")
    manifest = _validate_sealed(
        value,
        (
            "schema",
            "schema_version",
            "schema_id",
            "feature_names",
            "target_names",
            "input_dim",
            "target_dim",
            "continuous_only",
        ),
    )
    if manifest["schema"] != "comsol_mcp.surrogate_schema_manifest":
        raise ValueError("unexpected schema name")
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    if manifest["continuous_only"] is not True:
        raise ValueError("first target lane requires continuous_only")
    if manifest["input_dim"] != len(manifest["feature_names"]):
        raise ValueError("input_dim mismatch")
    if manifest["target_dim"] != len(manifest["target_names"]):
        raise ValueError("target_dim mismatch")
    if not 2 <= manifest["input_dim"] <= 12:
        raise ValueError("input_dim outside 2-12")
    if not 1 <= manifest["target_dim"] <= 8:
        raise ValueError("target_dim outside 1-8")
    rebuilt = build_schema_manifest(
        schema_id=manifest["schema_id"],
        feature_names=manifest["feature_names"],
        target_names=manifest["target_names"],
    )
    if rebuilt != manifest:
        raise ValueError("schema manifest content is not canonical")
    return rebuilt


def build_training_transforms(
    *,
    transform_id: str,
    transforms: Sequence[Mapping[str, Any]],
) -> SurrogateTrainingTransforms:
    if not transforms or len(transforms) > MAX_TRANSFORMS:
        raise ValueError("transforms must contain 1-32 entries")
    normalized: list[dict[str, Any]] = []
    for item in transforms:
        if not isinstance(item, Mapping):
            raise ValueError("each transform must be a mapping")
        kind = _require_str("transform.kind", item.get("kind"))
        if kind not in {"standardize", "minmax", "identity"}:
            raise ValueError("unsupported transform kind")
        # Annotated explicitly: the first key holds a string while a later key
        # holds a list, so inference from the literal would narrow the value type.
        entry: dict[str, Any] = {"kind": kind}
        if kind in {"standardize", "minmax"}:
            entry["columns"] = _require_unique(
                "transform.columns", [str(col) for col in item.get("columns", [])]
            )
            if not entry["columns"]:
                raise ValueError("transform.columns must be non-empty")
        normalized.append(entry)
    body = {
        "schema": "comsol_mcp.surrogate_training_transforms",
        "schema_version": SCHEMA_VERSION,
        "transform_id": _require_str("transform_id", transform_id),
        "fit_split": "train",
        "transforms": normalized,
    }
    return _finalize(body)  # type: ignore[return-value]


def validate_training_transforms(value: Any) -> SurrogateTrainingTransforms:
    if not isinstance(value, Mapping):
        raise ValueError("training transforms must be a mapping")
    manifest = _validate_sealed(
        value,
        (
            "schema",
            "schema_version",
            "transform_id",
            "fit_split",
            "transforms",
        ),
    )
    if manifest["schema"] != "comsol_mcp.surrogate_training_transforms":
        raise ValueError("unexpected schema name")
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    if manifest["fit_split"] != "train":
        raise ValueError("transforms may fit only on train")
    for item in manifest["transforms"]:
        if item.get("kind") not in {"standardize", "minmax", "identity"}:
            raise ValueError("unsupported transform kind")
    rebuilt = build_training_transforms(
        transform_id=manifest["transform_id"], transforms=manifest["transforms"]
    )
    if rebuilt != manifest:
        raise ValueError("training transforms content is not canonical")
    return rebuilt


def build_dataset_manifest(
    *,
    dataset_id: str,
    source_kind: str,
    source_identity_sha256: str,
    candidate_ids: Sequence[str],
    row_ids: Sequence[str],
    leakage_groups: Sequence[Mapping[str, Any]],
    split_assignments: Sequence[Mapping[str, Any]],
    ineligible_rows: Sequence[Mapping[str, Any]] = (),
) -> SurrogateDatasetManifest:
    source_identity_sha256 = _require_hex64("source_identity_sha256", source_identity_sha256)
    # NOTE: this ``source_kind`` is dataset *provenance* (for example
    # ``campaign``), which is a different vocabulary from the source-*reference*
    # kinds in ``comsol_mcp.contracts.surrogate.SOURCE_KINDS``
    # (``file``/``directory``/``dbmodel``).  Enforcing the reference vocabulary
    # here would reject legitimate provenance values, so provenance stays a
    # required non-empty string and the reference vocabulary is enforced where a
    # caller actually selects a source representation.
    source_kind = _require_str("source_kind", source_kind)
    candidates = _require_unique("candidate_ids", [str(item) for item in candidate_ids])
    rows = _require_unique("row_ids", [str(item) for item in row_ids])
    if not rows or len(rows) > MAX_ROWS:
        raise ValueError("row_ids must contain 1-4096 entries")

    groups: list[LeakageGroup] = []
    seen_group_ids: set[str] = set()
    grouped_rows: set[str] = set()
    for group in leakage_groups:
        if not isinstance(group, Mapping):
            raise ValueError("each leakage group must be a mapping")
        group_id = _require_str("leakage_group.group_id", group.get("group_id"))
        if group_id in seen_group_ids:
            raise ValueError("leakage group_id must be unique")
        seen_group_ids.add(group_id)
        member_rows = _require_unique(
            "leakage_group.row_ids",
            [str(item) for item in group.get("row_ids", [])],
        )
        if not member_rows:
            raise ValueError("leakage group must contain at least one row")
        for row_id in member_rows:
            if row_id not in rows:
                raise ValueError("leakage group references unknown row_id")
            if row_id in grouped_rows:
                raise ValueError("row_id may belong to only one leakage group")
            grouped_rows.add(row_id)
        groups.append({"group_id": group_id, "row_ids": member_rows})

    splits: list[SplitAssignment] = []
    assigned: set[str] = set()
    for item in split_assignments:
        if not isinstance(item, Mapping):
            raise ValueError("each split assignment must be a mapping")
        row_id = _require_str("split.row_id", item.get("row_id"))
        split = _require_str("split.split", item.get("split"))
        if split not in SPLIT_NAMES:
            raise ValueError("split must be train, validation, test, or scientific_holdout")
        if row_id not in rows:
            raise ValueError("split assignment references unknown row_id")
        if row_id in assigned:
            raise ValueError("row_id may appear in only one split")
        assigned.add(row_id)
        splits.append({"row_id": row_id, "split": split})

    ineligible: list[IneligibleRow] = []
    ineligible_ids: set[str] = set()
    for item in ineligible_rows:
        if not isinstance(item, Mapping):
            raise ValueError("each ineligible row must be a mapping")
        row_id = _require_str("ineligible.row_id", item.get("row_id"))
        reason_code = _require_str("ineligible.reason_code", item.get("reason_code"))
        detail = _require_str("ineligible.detail", item.get("detail"), max_len=1024)
        if row_id not in rows:
            raise ValueError("ineligible row references unknown row_id")
        if row_id in ineligible_ids:
            raise ValueError("ineligible row_id must be unique")
        ineligible_ids.add(row_id)
        ineligible.append({"row_id": row_id, "reason_code": reason_code, "detail": detail})
    # L-05: split assignments exist so fitted splits cover ELIGIBLE rows.
    # An ineligible row with an assignment is a contradictory state: it
    # polluted split counts and group-disjoint/holdout checks while never
    # being usable for fitting, so it is refused fail-closed here.
    conflicted = sorted(ineligible_ids & assigned)
    if conflicted:
        preview = ", ".join(conflicted[:5])
        suffix = "" if len(conflicted) <= 5 else f" (+{len(conflicted) - 5} more)"
        raise ValueError(f"ineligible rows must not carry a split assignment: {preview}{suffix}")

    body = {
        "schema": "comsol_mcp.surrogate_dataset_manifest",
        "schema_version": SCHEMA_VERSION,
        "dataset_id": _require_str("dataset_id", dataset_id),
        "source_kind": source_kind,
        "source_identity_sha256": source_identity_sha256,
        "candidate_ids": candidates,
        "row_ids": rows,
        "leakage_groups": groups,
        "split_assignments": splits,
        "ineligible_rows": ineligible,
    }
    return _finalize(body)  # type: ignore[return-value]


def validate_dataset_manifest(value: Any) -> SurrogateDatasetManifest:
    if not isinstance(value, Mapping):
        raise ValueError("dataset manifest must be a mapping")
    manifest = _validate_sealed(
        value,
        (
            "schema",
            "schema_version",
            "dataset_id",
            "source_kind",
            "source_identity_sha256",
            "candidate_ids",
            "row_ids",
            "leakage_groups",
            "split_assignments",
            "ineligible_rows",
        ),
    )
    if manifest["schema"] != "comsol_mcp.surrogate_dataset_manifest":
        raise ValueError("unexpected schema name")
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    rebuilt = build_dataset_manifest(
        dataset_id=manifest["dataset_id"],
        source_kind=manifest["source_kind"],
        source_identity_sha256=manifest["source_identity_sha256"],
        candidate_ids=manifest["candidate_ids"],
        row_ids=manifest["row_ids"],
        leakage_groups=manifest["leakage_groups"],
        split_assignments=manifest["split_assignments"],
        ineligible_rows=manifest["ineligible_rows"],
    )
    if rebuilt != manifest:
        raise ValueError("dataset manifest content is not canonical")
    return manifest


def validate_group_disjoint_split(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Prove leakage groups never span more than one split.

    Ineligible rows may remain unassigned. Assigned eligible rows must be
    covered exactly once. A scientific holdout is optional but, when present,
    must not share a leakage group with any fitted split.

    Every eligible assigned row must also belong to a leakage group. Without
    that requirement the group-disjointness claim is vacuous for such a row: the
    check below iterates over *groups*, so a row in no group is never examined
    and the verdict `group_disjoint: True` would be reported for data whose
    leakage structure is unknown. A row that cannot be grouped is not proven
    disjoint, so it is refused rather than silently certified.
    """
    validated = validate_dataset_manifest(manifest)
    row_to_split = {item["row_id"]: item["split"] for item in validated["split_assignments"]}
    ineligible = {item["row_id"] for item in validated["ineligible_rows"]}
    eligible = [row for row in validated["row_ids"] if row not in ineligible]
    # L-05: the same fail-closed contract as the builder, applied to any
    # pre-built manifest: an ineligible row must not carry an assignment,
    # otherwise split counts and group/holdout verdicts count unusable rows.
    conflicted = sorted(ineligible & set(row_to_split))
    if conflicted:
        preview = ", ".join(conflicted[:5])
        suffix = "" if len(conflicted) <= 5 else f" (+{len(conflicted) - 5} more)"
        raise ValueError(f"ineligible rows must not carry a split assignment: {preview}{suffix}")
    missing = [row for row in eligible if row not in row_to_split]
    if missing:
        raise ValueError("eligible rows must be assigned to a split")

    grouped = {row_id for group in validated["leakage_groups"] for row_id in group["row_ids"]}
    ungrouped = sorted(row for row in eligible if row in row_to_split and row not in grouped)
    if ungrouped:
        preview = ", ".join(ungrouped[:5])
        suffix = "" if len(ungrouped) <= 5 else f" (+{len(ungrouped) - 5} more)"
        raise ValueError(
            f"eligible assigned rows must belong to a leakage group: {preview}{suffix}"
        )

    for group in validated["leakage_groups"]:
        splits_in_group = {row_to_split[row] for row in group["row_ids"] if row in row_to_split}
        if len(splits_in_group) > 1:
            raise ValueError("leakage group spans multiple splits")

    fitted = {"train", "validation", "test"}
    holdout_groups = set()
    for group in validated["leakage_groups"]:
        group_splits = {row_to_split[row] for row in group["row_ids"] if row in row_to_split}
        if "scientific_holdout" in group_splits and group_splits & fitted:
            holdout_groups.add(group["group_id"])
    if holdout_groups:
        raise ValueError("scientific holdout leaks into fitted splits")

    counts = {name: 0 for name in SPLIT_NAMES}
    for split in row_to_split.values():
        counts[split] += 1
    return {
        "dataset_id": validated["dataset_id"],
        "manifest_sha256": validated["manifest_sha256"],
        "row_count": len(validated["row_ids"]),
        "eligible_count": len(eligible),
        "ineligible_count": len(ineligible),
        "split_counts": counts,
        "group_count": len(validated["leakage_groups"]),
        "group_disjoint": True,
    }
