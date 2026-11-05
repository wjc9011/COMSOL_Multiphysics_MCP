"""Read-only, threshold-free evidence collection for Wave Optics models."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from mcp.server.mcpserver import MCPServer

from .catalog import get_tool_metadata
from .ownership import ownership_manager
from .session import session_manager

MAX_BOUNDARIES = 256
MAX_TAGS = 256
MAX_ERROR_CHARS = 300
POINT_AUDIT_TOOL = "wave_optics_point_audit"


def _point_audit_next_call(
    *, active_profile: str, inspection_status: str, missing_evidence: list[str]
) -> dict[str, Any]:
    metadata = get_tool_metadata(POINT_AUDIT_TOOL)
    profile = active_profile.strip().lower() if isinstance(active_profile, str) else ""
    return {
        "tool": POINT_AUDIT_TOOL,
        "available": (
            profile in metadata.intended_profiles
            and not missing_evidence
            and inspection_status == "complete"
        ),
        "implementation_status": metadata.maturity,
        "missing_evidence": list(missing_evidence),
    }


@dataclass
class EvidenceLedger:
    """Collect stable evidence codes without turning observations into policy."""

    observations: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    unknowns: list[dict[str, Any]] = field(default_factory=list)
    integrity_errors: list[dict[str, Any]] = field(default_factory=list)

    def add(self, level: str, code: str, message: str, **evidence: Any) -> None:
        record = {"code": code, "message": message}
        if evidence:
            record["evidence"] = evidence
        target = {
            "observation": self.observations,
            "warning": self.warnings,
            "unknown": self.unknowns,
            "integrity_error": self.integrity_errors,
        }.get(level)
        if target is None:
            raise ValueError(f"unsupported evidence level: {level}")
        target.append(record)

    @property
    def inspection_status(self) -> str:
        if self.integrity_errors:
            return "integrity_blocked"
        if self.unknowns:
            return "partial"
        return "complete"

    def to_dict(self) -> dict[str, Any]:
        return {
            "observations": self.observations,
            "warnings": self.warnings,
            "unknowns": self.unknowns,
            "integrity_errors": self.integrity_errors,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _error(exc: Exception) -> str:
    return str(exc)[:MAX_ERROR_CHARS]


def _safe_text(callable_value: Callable[[], Any], ledger: EvidenceLedger, code: str) -> str | None:
    try:
        value = callable_value()
        return None if value is None else str(value)
    except Exception as exc:
        ledger.add("unknown", code, "Model metadata could not be read.", error=_error(exc))
        return None


def _tags(container: Any) -> list[str]:
    return _all_tags(container)[:MAX_TAGS]


def _all_tags(container: Any) -> list[str]:
    return [str(value) for value in list(container.tags())]


def _get(container: Any, tag: str) -> Any:
    try:
        return container.get(tag)
    except Exception:
        return container(tag)


def _feature_type(feature: Any) -> str | None:
    for name in ("getType", "type"):
        try:
            return str(getattr(feature, name)())
        except Exception:  # noqa: S112 - try each documented type getter
            continue
    return None


def _label(feature: Any) -> str | None:
    try:
        return str(feature.label())
    except Exception:
        return None


def _property(feature: Any, name: str) -> str | None:
    """Read one existing property without reflection or mutation."""
    for getter in ("getString", "get"):
        try:
            value = getattr(feature, getter)(name)
            if value is None:
                return None
            if getter == "getString" or isinstance(value, (str, int, float, bool)):
                return str(value)
            class_name = type(value).__name__.casefold()
            if "string" in class_name:
                return str(value)
            try:
                return " ".join(str(item) for item in list(value))
            except Exception:
                return str(value)
        except Exception:  # noqa: S112 - try each documented property getter
            continue
    return None


def _properties(feature: Any, names: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in names:
        value = _property(feature, name)
        if value is not None:
            result[name] = value
    return result


def _selection_entities(owner: Any, name: str | None = None) -> tuple[list[int] | None, str | None]:
    try:
        selection = owner.selection(name) if name is not None else owner.selection()
        values = [int(value) for value in list(selection.entities())]
        return values, None
    except Exception as exc:
        return None, _error(exc)


def _resolve_tag(
    *,
    available: list[str],
    expected: str | None,
    preferred: str | None,
    kind: str,
    ledger: EvidenceLedger,
) -> str | None:
    if expected:
        if expected not in available:
            ledger.add(
                "integrity_error",
                f"requested_{kind}_missing",
                f"The exact requested {kind} tag does not exist.",
                expected=expected,
                available=available[:MAX_TAGS],
                available_count=len(available),
                available_truncated=len(available) > MAX_TAGS,
            )
            return None
        return expected
    if preferred and preferred in available:
        return preferred
    if len(available) == 1:
        return available[0]
    ledger.add(
        "unknown",
        f"{kind}_tag_ambiguous",
        f"An exact {kind} tag could not be selected without caller input.",
        available=available[:MAX_TAGS],
        available_count=len(available),
        available_truncated=len(available) > MAX_TAGS,
    )
    return None


def _face_point(values: list[float]) -> Any:
    try:
        import jpype

        point = jpype.JArray(jpype.JArray(jpype.JDouble))(1)
        point[0] = jpype.JArray(jpype.JDouble)(values)
        return point
    except Exception:
        return [values]


def _probe_boundary_read_only(
    geom: Any,
    number: int,
    *,
    sdim: int,
    ups: list[int],
    downs: list[int],
) -> dict[str, Any]:
    item: dict[str, Any] = {"boundary": number}
    if number <= len(ups) and number <= len(downs):
        item.update(
            up_domain=ups[number - 1],
            down_domain=downs[number - 1],
            interior=ups[number - 1] != 0 and downs[number - 1] != 0,
        )
    try:
        if sdim == 3:
            ranges = [float(value) for value in list(geom.faceParamRange(number))]
            point = _face_point([(ranges[0] + ranges[1]) / 2, (ranges[2] + ranges[3]) / 2])
            item["center"] = [float(value) for value in list(geom.faceX(number, point)[0])]
            item["normal"] = [float(value) for value in list(geom.faceNormal(number, point)[0])]
        elif sdim == 2:
            ranges = [float(value) for value in list(geom.edgeParamRange(number))]
            point = _face_point([(ranges[0] + ranges[1]) / 2])[0]
            item["center"] = [float(value) for value in list(geom.edgeX(number, point)[0])]
            item["normal"] = [float(value) for value in list(geom.edgeNormal(number, point)[0])]
    except Exception as exc:
        item["probe_error"] = _error(exc)
    return item


def _probe_boundaries_read_only(
    geom: Any, ledger: EvidenceLedger
) -> tuple[list[dict[str, Any]], int, int, int]:
    n_boundaries = int(geom.getNBoundaries())
    n_domains = int(geom.getNDomains())
    sdim = int(geom.getSDim())
    if n_boundaries > MAX_BOUNDARIES:
        ledger.add(
            "warning",
            "boundary_response_truncated",
            "Boundary details were truncated to keep the MCP response bounded.",
            total=n_boundaries,
            returned=MAX_BOUNDARIES,
        )
    ups: list[int] = []
    downs: list[int] = []
    try:
        up_down = geom.getUpDown()
        ups = [int(item) for item in list(up_down[0])]
        downs = [int(item) for item in list(up_down[1])]
    except Exception as exc:
        ledger.add(
            "unknown",
            "up_down_unreadable",
            "Boundary-domain adjacency could not be read.",
            error=_error(exc),
        )

    boundaries: list[dict[str, Any]] = []
    for number in range(1, min(n_boundaries, MAX_BOUNDARIES) + 1):
        boundaries.append(
            _probe_boundary_read_only(
                geom,
                number,
                sdim=sdim,
                ups=ups,
                downs=downs,
            )
        )
    failed = [item["boundary"] for item in boundaries if item.get("probe_error")]
    if failed:
        ledger.add(
            "unknown",
            "boundary_probes_incomplete",
            "One or more boundary center/normal probes failed.",
            count=len(failed),
            boundaries=failed[:MAX_TAGS],
            boundaries_truncated=len(failed) > MAX_TAGS,
        )
    return boundaries, n_domains, n_boundaries, sdim


def _extend_boundary_map(
    geom: Any,
    boundary_map: dict[int, dict[str, Any]],
    requested: Iterable[int],
    ledger: EvidenceLedger,
) -> None:
    missing: list[int] = []
    seen = set(boundary_map)
    truncated = False
    for value in requested:
        number = int(value)
        if number in seen:
            continue
        seen.add(number)
        if len(missing) >= MAX_BOUNDARIES:
            truncated = True
            continue
        missing.append(number)
    missing.sort()
    if truncated:
        ledger.add(
            "unknown",
            "selected_boundary_probe_budget_exceeded",
            "Selected boundary evidence exceeded the bounded topology probe budget.",
            probe_budget=MAX_BOUNDARIES,
        )
    if not missing:
        return
    try:
        n_boundaries = int(geom.getNBoundaries())
        sdim = int(geom.getSDim())
    except Exception as exc:
        ledger.add(
            "unknown",
            "selected_boundary_topology_unreadable",
            "Selected-boundary topology could not be read.",
            error=_error(exc),
        )
        return
    try:
        up_down = geom.getUpDown()
        ups = [int(item) for item in list(up_down[0])]
        downs = [int(item) for item in list(up_down[1])]
    except Exception:
        ups = []
        downs = []
    invalid = [number for number in missing if number < 1 or number > n_boundaries]
    failures: list[int] = []
    for number in missing:
        if number in invalid:
            continue
        item = _probe_boundary_read_only(
            geom,
            number,
            sdim=sdim,
            ups=ups,
            downs=downs,
        )
        boundary_map[number] = item
        if item.get("probe_error"):
            failures.append(number)
    if invalid:
        ledger.add(
            "unknown",
            "selected_boundaries_invalid",
            "One or more selected boundaries fall outside the built topology.",
            boundaries=invalid[:MAX_TAGS],
            boundaries_truncated=len(invalid) > MAX_TAGS,
        )
    if failures:
        ledger.add(
            "unknown",
            "selected_boundary_probes_incomplete",
            "Selected boundary center/normal evidence could not be read.",
            boundaries=failures[:MAX_TAGS],
            boundaries_truncated=len(failures) > MAX_TAGS,
        )


def _pair_metadata(component: Any, ledger: EvidenceLedger) -> list[dict[str, Any]]:
    try:
        pair_container = component.pair()
        pair_tags = _tags(pair_container)
    except Exception as exc:
        ledger.add(
            "unknown",
            "pairs_unreadable",
            "Identity/assembly pairs could not be inspected.",
            error=_error(exc),
        )
        return []
    pairs: list[dict[str, Any]] = []
    for tag in pair_tags:
        pair = _get(pair_container, tag)
        item: dict[str, Any] = {"tag": tag, "label": _label(pair), "type": _feature_type(pair)}
        for selection_name in ("source", "destination"):
            entities, error = _selection_entities(pair, selection_name)
            item[selection_name] = entities
            if error:
                item[f"{selection_name}_error"] = error
        pairs.append(item)
    return pairs


def _collect_topology(
    jm: Any,
    ledger: EvidenceLedger,
    *,
    expected_component_tag: str | None,
) -> tuple[dict[str, Any], Any | None, Any | None, dict[int, dict[str, Any]]]:
    try:
        component_container = jm.component()
        all_component_tags = _all_tags(component_container)
        component_tags = all_component_tags[:MAX_TAGS]
    except Exception as exc:
        ledger.add(
            "unknown",
            "components_unreadable",
            "Component tags could not be read.",
            error=_error(exc),
        )
        return {}, None, None, {}
    component_tag = _resolve_tag(
        available=all_component_tags,
        expected=expected_component_tag,
        preferred="comp1",
        kind="component",
        ledger=ledger,
    )
    if component_tag is None:
        return (
            {
                "component_tags": component_tags,
                "component_tag_count": len(all_component_tags),
                "component_tags_truncated": len(all_component_tags) > len(component_tags),
            },
            None,
            None,
            {},
        )
    component = _get(component_container, component_tag)
    try:
        geometry_container = component.geom()
        all_geometry_tags = _all_tags(geometry_container)
        geometry_tags = all_geometry_tags[:MAX_TAGS]
    except Exception as exc:
        ledger.add(
            "unknown",
            "geometries_unreadable",
            "Geometry tags could not be read.",
            error=_error(exc),
        )
        return {"component_tag": component_tag}, component, None, {}
    geometry_tag = _resolve_tag(
        available=all_geometry_tags,
        expected=None,
        preferred="geom1",
        kind="geometry",
        ledger=ledger,
    )
    if geometry_tag is None:
        return (
            {
                "component_tag": component_tag,
                "geometry_tags": geometry_tags,
                "geometry_tag_count": len(all_geometry_tags),
                "geometry_tags_truncated": len(all_geometry_tags) > len(geometry_tags),
            },
            component,
            None,
            {},
        )
    geom = _get(geometry_container, geometry_tag)
    try:
        boundaries, n_domains, n_boundaries, sdim = _probe_boundaries_read_only(geom, ledger)
        bbox = [float(value) for value in list(geom.getBoundingBox())]
    except Exception as exc:
        ledger.add(
            "unknown",
            "topology_unreadable",
            "Built geometry topology could not be read without rebuilding.",
            error=_error(exc),
        )
        return {"component_tag": component_tag, "geometry_tag": geometry_tag}, component, geom, {}
    fin: dict[str, Any] = {}
    try:
        feature_container = geom.feature()
        if "fin" in _tags(feature_container):
            feature = _get(feature_container, "fin")
            fin = {
                "tag": "fin",
                "type": _feature_type(feature),
                "label": _label(feature),
                "properties": _properties(feature, ("action", "createpairs", "imprint", "keep")),
            }
    except Exception as exc:
        fin = {"error": _error(exc)}
    ledger.add(
        "observation",
        "topology_inspected",
        "Built geometry topology was inspected without running the geometry.",
    )
    return (
        {
            "component_tag": component_tag,
            "component_tags": component_tags,
            "component_tag_count": len(all_component_tags),
            "component_tags_truncated": len(all_component_tags) > len(component_tags),
            "geometry_tag": geometry_tag,
            "geometry_tags": geometry_tags,
            "geometry_tag_count": len(all_geometry_tags),
            "geometry_tags_truncated": len(all_geometry_tags) > len(geometry_tags),
            "space_dimension": sdim,
            "bounding_box": bbox,
            "domain_count": n_domains,
            "boundary_count": n_boundaries,
            "boundaries": boundaries,
            "form_finalization": fin,
            "pairs": _pair_metadata(component, ledger),
        },
        component,
        geom,
        {item["boundary"]: item for item in boundaries},
    )


def _find_physics(
    component: Any, expected: str | None, ledger: EvidenceLedger
) -> tuple[str | None, Any | None, list[str]]:
    try:
        container = component.physics()
        tags = _all_tags(container)
    except Exception as exc:
        ledger.add(
            "unknown",
            "physics_tags_unreadable",
            "Physics tags could not be read.",
            error=_error(exc),
        )
        return None, None, []
    tag = _resolve_tag(
        available=tags, expected=expected, preferred="ewfd", kind="physics", ledger=ledger
    )
    return tag, _get(container, tag) if tag else None, tags


def _feature_inventory(container: Any) -> list[tuple[str, Any, str | None]]:
    result: list[tuple[str, Any, str | None]] = []
    for tag in _all_tags(container):
        feature = _get(container, tag)
        result.append((tag, feature, _feature_type(feature)))
    return result


def _is_kind(
    tag: str, feature_type: str | None, label: str | None, needles: tuple[str, ...]
) -> bool:
    text = " ".join(value for value in (tag, feature_type, label) if value).lower()
    return any(needle.lower() in text for needle in needles)


def _collect_periodic_ports_incidence(
    component: Any,
    geom: Any,
    physics: Any,
    physics_tag: str,
    boundary_map: dict[int, dict[str, Any]],
    ledger: EvidenceLedger,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    try:
        features = _feature_inventory(physics.feature())
    except Exception as exc:
        ledger.add(
            "unknown",
            "physics_features_unreadable",
            "Physics feature tags could not be read.",
            error=_error(exc),
        )
        return {}, {}, {"physical_polarization_evidence": "label_only"}
    periodic_structures = [
        (tag, feature)
        for tag, feature, kind in features
        if _is_kind(tag, kind, _label(feature), ("periodicstructure", "periodic structure", "ps1"))
    ]
    if not periodic_structures:
        ledger.add(
            "unknown",
            "periodic_structure_missing",
            "No PeriodicStructure feature was identified.",
            physics_tag=physics_tag,
        )
        feature_tags = [tag for tag, _, _ in features]
        return (
            {
                "physics_tag": physics_tag,
                "physics_feature_tags": feature_tags[:MAX_TAGS],
                "physics_feature_tag_count": len(feature_tags),
                "physics_feature_tags_truncated": len(feature_tags) > MAX_TAGS,
            },
            {},
            {"physical_polarization_evidence": "label_only"},
        )
    if len(periodic_structures) > 1:
        candidate_tags = [tag for tag, _ in periodic_structures]
        ledger.add(
            "unknown",
            "periodic_structure_ambiguous",
            "Multiple PeriodicStructure candidates exist; no candidate was audited.",
            tags=candidate_tags[:MAX_TAGS],
            tag_count=len(candidate_tags),
            tags_truncated=len(candidate_tags) > MAX_TAGS,
        )
        return (
            {
                "physics_tag": physics_tag,
                "periodic_structure_tag": None,
                "candidate_tags": candidate_tags[:MAX_TAGS],
                "candidate_count": len(candidate_tags),
                "candidate_tags_truncated": len(candidate_tags) > MAX_TAGS,
                "selection_status": "ambiguous",
                "floquet_features": [],
            },
            {
                "periodic_port_features": [],
                "reference_direction_features": [],
                "material_assignments": [],
                "homogeneity_isotropy_evidence": "not_inspected_due_to_ambiguity",
            },
            {
                "physical_polarization_evidence": "label_only",
                "selection_status": "ambiguous_periodic_structure",
            },
        )
    ps_tag, ps = periodic_structures[0]
    ps_props = _properties(ps, ("Polarization", "LinearPol", "alpha1_inc", "alpha2_inc"))
    if not ps_props:
        ledger.add(
            "unknown",
            "incidence_properties_unreadable",
            "PeriodicStructure incidence properties could not be read on this clientapi build.",
        )
    all_boundaries, all_error = _selection_entities(ps, "allBoundaries")
    excited, excited_error = _selection_entities(ps, "excitedPortSelection")
    if all_error:
        ledger.add(
            "unknown",
            "periodic_all_boundaries_unreadable",
            "The PeriodicStructure all-boundaries selection could not be read.",
            error=all_error,
        )
    try:
        children = _feature_inventory(ps.feature())
    except Exception as exc:
        ledger.add(
            "unknown",
            "periodic_children_unreadable",
            "PeriodicStructure subfeatures could not be read.",
            error=_error(exc),
        )
        children = []

    child_records: list[tuple[str, Any, str | None, str | None, list[int] | None, str | None]] = []
    for tag, feature, kind in children:
        label = _label(feature)
        entities, selection_error = _selection_entities(feature)
        child_records.append((tag, feature, kind, label, entities, selection_error))

    selected_boundaries = {
        int(number) for values in (all_boundaries, excited) for number in values or []
    }
    selected_boundaries.update(
        int(number)
        for _tag, _feature, kind, label, entities, _error_value in child_records
        if _is_kind(
            _tag,
            kind,
            label,
            ("floquet", "periodiccondition", "fpc", "periodicport", "periodic port", "pport"),
        )
        for number in entities or []
    )
    _extend_boundary_map(geom, boundary_map, selected_boundaries, ledger)

    selection_failures = [
        {"tag": tag, "type": kind}
        for tag, _feature, kind, label, _entities, selection_error in child_records
        if selection_error
        and _is_kind(
            tag,
            kind,
            label,
            (
                "floquet",
                "periodiccondition",
                "fpc",
                "periodicport",
                "periodic port",
                "pport",
                "referencedirection",
                "reference direction",
                "rdir",
            ),
        )
    ]
    if selection_failures:
        ledger.add(
            "unknown",
            "periodic_subfeature_selections_unreadable",
            "One or more PeriodicStructure subfeature selections could not be read.",
            features=selection_failures[:MAX_TAGS],
            features_truncated=len(selection_failures) > MAX_TAGS,
        )

    floquet: list[dict[str, Any]] = []
    ports: list[dict[str, Any]] = []
    reference: list[dict[str, Any]] = []
    for tag, feature, kind, label, entities, selection_error in child_records:
        bounded_entities = None if entities is None else entities[:MAX_BOUNDARIES]
        base = {
            "tag": tag,
            "type": kind,
            "label": label,
            "selection": bounded_entities,
            "selection_count": None if entities is None else len(entities),
            "selection_truncated": bool(entities is not None and len(entities) > MAX_BOUNDARIES),
        }
        if selection_error:
            base["selection_error"] = selection_error
        if _is_kind(tag, kind, label, ("floquet", "periodiccondition", "fpc")):
            base["properties"] = _properties(
                feature, ("PeriodicType", "Floquet_source", "kFloquet")
            )
            selected = [boundary_map[number] for number in entities or [] if number in boundary_map]
            normals = [item.get("normal") for item in selected if item.get("normal")]
            opposite_groups = None
            if len(normals) >= 2:
                first = normals[0]
                same_items = [
                    item
                    for item in selected
                    if item.get("normal") and sum(a * b for a, b in zip(first, item["normal"])) > 0
                ]
                opposite_items = [
                    item
                    for item in selected
                    if item.get("normal") and sum(a * b for a, b in zip(first, item["normal"])) < 0
                ]
                positive = len(same_items)
                negative = len(opposite_items)
                translation = None
                if (
                    positive
                    and negative
                    and all(item.get("center") for item in same_items + opposite_items)
                ):
                    same_mean = [
                        sum(item["center"][axis] for item in same_items) / positive
                        for axis in range(len(same_items[0]["center"]))
                    ]
                    opposite_mean = [
                        sum(item["center"][axis] for item in opposite_items) / negative
                        for axis in range(len(opposite_items[0]["center"]))
                    ]
                    translation = [
                        opposite_mean[axis] - same_mean[axis] for axis in range(len(same_mean))
                    ]
                signatures = {
                    "same": sorted(
                        sorted(
                            domain
                            for domain in (item.get("up_domain", 0), item.get("down_domain", 0))
                            if domain
                        )
                        for item in same_items
                    ),
                    "opposite": sorted(
                        sorted(
                            domain
                            for domain in (item.get("up_domain", 0), item.get("down_domain", 0))
                            if domain
                        )
                        for item in opposite_items
                    ),
                }
                opposite_groups = {
                    "same_normal_count": positive,
                    "opposite_normal_count": negative,
                    "unclassified_count": len(selected) - positive - negative,
                    "count_balanced": positive == negative and positive + negative == len(selected),
                    "adjacent_domain_signatures": signatures,
                    "adjacent_domain_signatures_match": signatures["same"]
                    == signatures["opposite"],
                    "inferred_translation": translation,
                    "limitation": (
                        "This is a topology/selection check, not proof of "
                        "source-destination mesh compatibility."
                    ),
                }
                if not opposite_groups["count_balanced"]:
                    ledger.add(
                        "warning",
                        "floquet_face_count_mismatch",
                        "A Floquet selection has unequal opposing-normal face counts.",
                        tag=tag,
                        groups=opposite_groups,
                    )
            base["opposing_face_groups"] = opposite_groups
            floquet.append(base)
        elif _is_kind(tag, kind, label, ("periodicport", "periodic port", "pport")):
            adjacent = sorted(
                {
                    domain
                    for number in entities or []
                    for domain in (
                        boundary_map.get(number, {}).get("up_domain", 0),
                        boundary_map.get(number, {}).get("down_domain", 0),
                    )
                    if domain
                }
            )
            base["adjacent_domains"] = adjacent
            base["properties"] = _properties(
                feature,
                ("alpha1_inc", "alpha2_inc", "PortType", "DiffractionOrder", "EnableActiveMode"),
            )
            ports.append(base)
        elif _is_kind(tag, kind, label, ("referencedirection", "reference direction", "rdir")):
            reference.append(base)

    if not floquet:
        ledger.add(
            "unknown",
            "floquet_features_missing",
            "No Floquet periodic subfeatures were identified.",
        )
    if not ports:
        ledger.add(
            "unknown", "periodic_ports_missing", "No PeriodicPort subfeatures were identified."
        )
    if not reference or not any(item.get("selection") for item in reference):
        ledger.add(
            "unknown",
            "reference_direction_missing",
            "No non-empty rdir1/reference-direction selection was identified.",
        )
    if excited is None:
        ledger.add(
            "unknown",
            "excited_port_selection_unreadable",
            "The excited-port selection could not be read.",
            error=excited_error,
        )
    elif not excited:
        ledger.add(
            "unknown", "excited_port_selection_empty", "The excited-port selection is empty."
        )

    ledger.add(
        "observation",
        "periodic_structure_inspected",
        "PeriodicStructure configuration was inspected without mutation.",
        tag=ps_tag,
    )
    material_assignments: list[dict[str, Any]] = []
    try:
        material_container = component.material()
        for material_tag in _tags(material_container):
            material = _get(material_container, material_tag)
            domains, selection_error = _selection_entities(material)
            permittivity = None
            try:
                permittivity = _property(material.propertyGroup("def"), "relpermittivity")
            except Exception:  # noqa: S110 - absent relpermittivity is recorded as None
                pass
            material_assignments.append(
                {
                    "tag": material_tag,
                    "label": _label(material),
                    "domains": domains,
                    "selection_error": selection_error,
                    "relative_permittivity": permittivity,
                    "isotropy_evidence": "scalar_expression"
                    if permittivity and len(permittivity.replace(",", " ").split()) == 1
                    else "unresolved",
                }
            )
    except Exception as exc:
        ledger.add(
            "unknown",
            "materials_unreadable",
            "Port-adjacent material assignments could not be inspected.",
            error=_error(exc),
        )
    for port in ports:
        adjacent = set(port.get("adjacent_domains", []))
        matches = [
            item
            for item in material_assignments
            if adjacent and adjacent <= set(item.get("domains") or [])
        ]
        port["material_assignment_evidence"] = matches
        if len(matches) != 1 or matches[0].get("isotropy_evidence") != "scalar_expression":
            ledger.add(
                "unknown",
                "port_medium_unresolved",
                "A periodic port's adjacent medium could not be established as one "
                "scalar-permittivity material from assignments alone.",
                port_tag=port["tag"],
                adjacent_domains=sorted(adjacent),
            )
    if any(len(items) > MAX_TAGS for items in (floquet, ports, reference)):
        ledger.add(
            "warning",
            "periodic_feature_response_truncated",
            "Periodic subfeature details were truncated only after complete discovery.",
            floquet_count=len(floquet),
            port_count=len(ports),
            reference_direction_count=len(reference),
            returned_per_kind=MAX_TAGS,
        )
    periodicity = {
        "physics_tag": physics_tag,
        "periodic_structure_tag": ps_tag,
        "all_boundaries_selection": (
            None if all_boundaries is None else all_boundaries[:MAX_BOUNDARIES]
        ),
        "all_boundaries_count": None if all_boundaries is None else len(all_boundaries),
        "all_boundaries_truncated": bool(
            all_boundaries is not None and len(all_boundaries) > MAX_BOUNDARIES
        ),
        "all_boundaries_error": all_error,
        "floquet_features": floquet[:MAX_TAGS],
        "floquet_feature_count": len(floquet),
        "floquet_features_truncated": len(floquet) > MAX_TAGS,
    }
    port_section = {
        "periodic_port_features": ports[:MAX_TAGS],
        "periodic_port_feature_count": len(ports),
        "periodic_port_features_truncated": len(ports) > MAX_TAGS,
        "excited_port_selection": None if excited is None else excited[:MAX_BOUNDARIES],
        "excited_port_selection_count": None if excited is None else len(excited),
        "excited_port_selection_truncated": bool(
            excited is not None and len(excited) > MAX_BOUNDARIES
        ),
        "excited_port_selection_error": excited_error,
        "reference_direction_features": reference[:MAX_TAGS],
        "reference_direction_feature_count": len(reference),
        "reference_direction_features_truncated": len(reference) > MAX_TAGS,
        "material_assignments": material_assignments,
        "homogeneity_isotropy_evidence": "per_port_material_assignment_only",
    }
    incidence = {
        "raw_properties": ps_props,
        "reference_direction_features": reference[:MAX_TAGS],
        "physical_polarization_evidence": "label_only",
        "limitation": (
            "S/P or CircularPol labels and rdir1 do not prove the physical incident field vector."
        ),
    }
    return periodicity, port_section, incidence


def _parameter_expressions(model: Any) -> tuple[dict[str, Any], str | None]:
    try:
        raw = model.parameters(evaluate=False)
        return {str(key): str(value) for key, value in dict(raw).items()}, None
    except Exception as exc:
        return {}, _error(exc)


def _collect_wavelength(
    model: Any,
    jm: Any,
    ledger: EvidenceLedger,
    *,
    expected_study_tag: str | None,
    target_parameter_name: str | None,
    physics_tag: str | None,
) -> tuple[dict[str, Any], Any | None, str | None]:
    parameter_name = target_parameter_name or "wl"
    expressions, parameter_error = _parameter_expressions(model)
    if parameter_error:
        ledger.add(
            "unknown",
            "parameters_unreadable",
            "Global parameter expressions could not be read.",
            error=parameter_error,
        )
    parameter_expression = expressions.get(parameter_name)
    if parameter_expression is None:
        ledger.add(
            "unknown",
            "wavelength_parameter_missing",
            "The requested wavelength parameter was not found.",
            parameter=parameter_name,
        )
    try:
        study_container = jm.study()
        all_study_tags = _all_tags(study_container)
        study_tags = all_study_tags[:MAX_TAGS]
    except Exception as exc:
        ledger.add(
            "unknown", "studies_unreadable", "Study tags could not be read.", error=_error(exc)
        )
        return (
            {"parameter_name": parameter_name, "parameter_expression": parameter_expression},
            None,
            None,
        )
    study_tag = _resolve_tag(
        available=all_study_tags,
        expected=expected_study_tag,
        preferred="std1",
        kind="study",
        ledger=ledger,
    )
    if study_tag is None:
        return (
            {
                "parameter_name": parameter_name,
                "parameter_expression": parameter_expression,
                "study_tags": study_tags,
                "study_tag_count": len(all_study_tags),
                "study_tags_truncated": len(all_study_tags) > len(study_tags),
            },
            None,
            None,
        )
    study = _get(study_container, study_tag)
    steps: list[dict[str, Any]] = []
    linked_locations: list[dict[str, str]] = []
    try:
        for tag, feature, kind in _feature_inventory(study.feature()):
            props = _properties(
                feature, ("plist", "punit", "pname", "plistarr", "sweeptype", "activate")
            )
            for prop, value in props.items():
                if re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(parameter_name)}(?![A-Za-z0-9_])", value
                ):
                    linked_locations.append({"step_tag": tag, "property": prop, "value": value})
            steps.append({"tag": tag, "type": kind, "label": _label(feature), "properties": props})
    except Exception as exc:
        ledger.add(
            "unknown",
            "study_steps_unreadable",
            "Study-step properties could not be read.",
            error=_error(exc),
        )
    structurally_linked: bool | None = bool(linked_locations) if steps else None
    if not steps:
        ledger.add(
            "unknown",
            "study_steps_missing",
            "The selected study contains no readable study steps for wavelength-link inspection.",
            study_tag=study_tag,
        )
    if structurally_linked is False:
        ledger.add(
            "unknown",
            "wavelength_link_missing",
            "No structural link from the selected study to the wavelength parameter was found.",
            parameter=parameter_name,
            study_tag=study_tag,
        )
    ledger.add(
        "observation",
        "wavelength_controls_inspected",
        "Wavelength parameters and study controls were inspected structurally; "
        "no numeric synchronization verdict was made.",
    )
    return (
        {
            "parameter_name": parameter_name,
            "parameter_expression": parameter_expression,
            "study_tag": study_tag,
            "study_tags": study_tags,
            "study_tag_count": len(all_study_tags),
            "study_tags_truncated": len(all_study_tags) > len(study_tags),
            "steps": steps[:MAX_TAGS],
            "step_count": len(steps),
            "steps_truncated": len(steps) > MAX_TAGS,
            "structurally_linked": structurally_linked,
            "link_evidence": linked_locations[:MAX_TAGS],
            "link_evidence_count": len(linked_locations),
            "link_evidence_truncated": len(linked_locations) > MAX_TAGS,
            "solved_frequency_expression": (
                f"c_const/{physics_tag}.freq" if physics_tag is not None else None
            ),
            "numeric_synchronization": "not_evaluated_in_read_only_preflight",
        },
        study,
        study_tag,
    )


def _collect_mesh_study_results(
    model: Any,
    component: Any,
    physics_tag: str | None,
    study_tag: str | None,
    ledger: EvidenceLedger,
) -> dict[str, Any]:
    meshes: list[dict[str, Any]] = []
    try:
        mesh_container = component.mesh()
        for tag in _tags(mesh_container):
            mesh = _get(mesh_container, tag)
            item: dict[str, Any] = {"tag": tag, "label": _label(mesh)}
            try:
                item["element_count"] = int(mesh.getNumElem())
                item["vertex_count"] = int(mesh.getNumVertex())
                if item["element_count"] == 0:
                    ledger.add(
                        "warning",
                        "mesh_empty",
                        "A mesh sequence contains zero elements.",
                        mesh_tag=tag,
                    )
            except Exception as exc:
                item["count_error"] = _error(exc)
                ledger.add(
                    "unknown",
                    "mesh_counts_unreadable",
                    "Mesh counts could not be read.",
                    mesh_tag=tag,
                    error=_error(exc),
                )
            meshes.append(item)
    except Exception as exc:
        ledger.add(
            "unknown",
            "meshes_unreadable",
            "Mesh sequences could not be inspected.",
            error=_error(exc),
        )
    try:
        solutions = [str(value) for value in list(model.solutions())[:MAX_TAGS]]
    except Exception as exc:
        solutions = []
        ledger.add(
            "unknown", "solutions_unreadable", "Solution tags could not be read.", error=_error(exc)
        )
    try:
        datasets = [str(value) for value in list(model.datasets())[:MAX_TAGS]]
    except Exception as exc:
        datasets = []
        ledger.add(
            "unknown", "datasets_unreadable", "Dataset tags could not be read.", error=_error(exc)
        )
    expression_prefix = physics_tag or "ewfd"
    ledger.add(
        "observation",
        "mesh_study_results_inspected",
        "Mesh, study, solution, and dataset metadata were inspected without solving.",
    )
    return {
        "meshes": meshes,
        "study_tag": study_tag,
        "solutions": solutions,
        "datasets": datasets,
        "power_expression_candidates": [
            f"{expression_prefix}.Rtotal",
            f"{expression_prefix}.Ttotal",
            f"{expression_prefix}.Atotal",
        ],
        "power_expression_availability": "not_evaluated_in_read_only_preflight",
        "loss_expression_candidates": [f"{expression_prefix}.Qh"],
        "loss_operator_availability": "not_evaluated_in_read_only_preflight",
    }


def collect_preflight_foundation(
    model: Any,
    *,
    model_name: str,
    session_state: dict[str, Any],
    active_profile: str,
    expected_source_path: str | None = None,
    expected_source_sha256: str | None = None,
    loaded_source_identity: dict[str, Any] | None = None,
    mark_uninspected: bool = True,
) -> dict[str, Any]:
    """Collect provenance and ownership without running or mutating clientapi."""
    if not isinstance(model_name, str) or not model_name.strip():
        raise ValueError("model_name must be exact and non-empty")

    ledger = EvidenceLedger()
    loaded_path_text = _safe_text(model.file, ledger, "model_file_unreadable")
    model_label = _safe_text(model.name, ledger, "model_label_unreadable")
    comsol_version = _safe_text(model.version, ledger, "comsol_version_unreadable")
    loaded_path = Path(loaded_path_text).resolve() if loaded_path_text else None
    current_file_sha256 = None

    if loaded_path is None or not loaded_path.is_file():
        ledger.add(
            "unknown",
            "source_file_unavailable",
            "The loaded model has no readable source file for hashing.",
            loaded_path=loaded_path_text,
        )
    else:
        try:
            current_file_sha256 = _sha256(loaded_path)
            ledger.add(
                "observation",
                "current_file_hash_measured",
                "The file currently named by the loaded model was hashed without modification.",
                sha256=current_file_sha256,
            )
        except OSError as exc:
            ledger.add(
                "unknown",
                "source_hash_unavailable",
                "The loaded source file could not be hashed.",
                error=_error(exc),
            )

    source_path = None
    source_sha256 = None
    source_capture = None
    if isinstance(loaded_source_identity, dict):
        candidate_path = loaded_source_identity.get("source_path")
        candidate_hash = str(loaded_source_identity.get("source_sha256") or "").lower()
        if candidate_path and re.fullmatch(r"[0-9a-f]{64}", candidate_hash):
            source_path = Path(str(candidate_path)).resolve()
            source_sha256 = candidate_hash
            source_capture = str(loaded_source_identity.get("capture") or "session_registration")
            ledger.add(
                "observation",
                "loaded_source_identity_available",
                "The session supplied the source identity captured when this model was loaded.",
                sha256=source_sha256,
                capture=source_capture,
            )
        else:
            ledger.add(
                "unknown",
                "loaded_source_identity_invalid",
                "The session's captured loaded-source identity is malformed.",
            )
    else:
        ledger.add(
            "unknown",
            "loaded_source_identity_unavailable",
            "Only the current on-disk file can be measured; the bytes loaded "
            "into the model are not attested.",
        )

    if (
        source_path is not None
        and loaded_path is not None
        and source_path == loaded_path
        and source_sha256 is not None
        and current_file_sha256 is not None
        and source_sha256 != current_file_sha256
    ):
        ledger.add(
            "warning",
            "source_file_changed_since_load",
            "The current source path no longer contains the bytes captured "
            "when the model was loaded.",
            loaded_source_sha256=source_sha256,
            current_file_sha256=current_file_sha256,
        )

    if expected_source_path is not None:
        expected_path = Path(expected_source_path).resolve()
        if source_path is None or source_path != expected_path:
            ledger.add(
                "integrity_error",
                "source_path_mismatch",
                "Captured loaded-source path does not match the caller-declared path.",
                expected=str(expected_path),
                actual=str(source_path) if source_path else None,
            )
    if expected_source_sha256 is not None:
        normalized_expected = expected_source_sha256.strip().lower()
        if source_sha256 is None or source_sha256.lower() != normalized_expected:
            ledger.add(
                "integrity_error",
                "source_hash_mismatch",
                "Captured loaded-source hash does not match the caller-declared hash.",
                expected=normalized_expected,
                actual=source_sha256,
            )

    ownership = ownership_manager.status(session_state=session_state)
    collision = bool(ownership.get("collision"))
    if collision:
        ledger.add(
            "integrity_error", "solver_collision", "Solver ownership evidence reports a collision."
        )
    else:
        ledger.add(
            "observation",
            "solver_ownership_inspected",
            "Solver ownership was inspected without starting COMSOL.",
        )

    if mark_uninspected:
        for section, code in (
            ("topology", "topology_not_inspected"),
            ("periodicity", "periodicity_not_inspected"),
            ("ports", "ports_not_inspected"),
            ("incidence", "incidence_not_inspected"),
            ("wavelength", "wavelength_not_inspected"),
            ("mesh_study_results", "mesh_study_results_not_inspected"),
        ):
            ledger.add(
                "unknown",
                code,
                f"The {section} evidence collector has not populated this section yet.",
            )

    return {
        "inspection_status": ledger.inspection_status,
        "assessment": {
            "mode": "evidence_only",
            "project_verdict": None,
            "long_sweep_recommendation": None,
        },
        "evidence": ledger.to_dict(),
        "provenance": {
            "requested_model_name": model_name,
            "model_label": model_label,
            "loaded_path": str(loaded_path) if loaded_path else loaded_path_text,
            "source_path": str(source_path) if source_path else None,
            "source_sha256": source_sha256,
            "source_identity_capture": source_capture,
            "current_file_sha256": current_file_sha256,
            "comsol_version": comsol_version,
            "active_profile": active_profile,
        },
        "ownership": {
            "session": ownership.get("session"),
            "lease": ownership.get("lease"),
            "external_solver_processes": ownership.get("external_solver_processes", []),
            "collision": collision,
            "solve_permitted": not collision,
        },
        "topology": {},
        "periodicity": {},
        "ports": {},
        "incidence": {},
        "wavelength": {},
        "mesh_study_results": {},
        "next_call": _point_audit_next_call(
            active_profile=active_profile,
            inspection_status=ledger.inspection_status,
            missing_evidence=[
                "topology",
                "periodicity",
                "ports",
                "incidence",
                "wavelength",
                "mesh_study_results",
            ],
        ),
    }


def collect_wave_optics_preflight(
    model: Any,
    *,
    model_name: str,
    session_state: dict[str, Any],
    active_profile: str,
    expected_component_tag: str | None = None,
    expected_physics_tag: str | None = None,
    expected_study_tag: str | None = None,
    expected_source_path: str | None = None,
    expected_source_sha256: str | None = None,
    loaded_source_identity: dict[str, Any] | None = None,
    target_wavelength_parameter: str | None = None,
    expected_lattice_axes: list[str] | None = None,
    target_physical_polarization: list[float] | None = None,
) -> dict[str, Any]:
    """Inspect one already-loaded model without running or mutating it."""
    if expected_lattice_axes is not None and (
        not isinstance(expected_lattice_axes, list)
        or len(expected_lattice_axes) > 3
        or any(
            not isinstance(axis, str) or axis.lower() not in {"x", "y", "z"}
            for axis in expected_lattice_axes
        )
    ):
        raise ValueError("expected_lattice_axes must contain at most three x, y, or z labels")
    result = collect_preflight_foundation(
        model,
        model_name=model_name,
        session_state=session_state,
        active_profile=active_profile,
        expected_source_path=expected_source_path,
        expected_source_sha256=expected_source_sha256,
        loaded_source_identity=loaded_source_identity,
        mark_uninspected=False,
    )
    ledger = EvidenceLedger(**result["evidence"])
    if result["inspection_status"] == "integrity_blocked":
        result["next_call"]["missing_evidence"] = ["integrity_clearance"]
        return result
    try:
        jm = model.java
    except Exception as exc:
        ledger.add(
            "unknown",
            "clientapi_unavailable",
            "The read-only clientapi model handle is unavailable.",
            error=_error(exc),
        )
        result["evidence"] = ledger.to_dict()
        result["inspection_status"] = ledger.inspection_status
        return result

    result["provenance"]["model_tag"] = _safe_text(lambda: jm.tag(), ledger, "model_tag_unreadable")
    try:
        import mph

        result["provenance"]["mph_version"] = getattr(mph, "__version__", None)
    except Exception as exc:
        result["provenance"]["mph_version"] = None
        ledger.add(
            "unknown",
            "mph_version_unreadable",
            "The MPh package version could not be read.",
            error=_error(exc),
        )

    topology, component, geom, boundary_map = _collect_topology(
        jm, ledger, expected_component_tag=expected_component_tag
    )
    result["topology"] = topology
    physics_tag = None
    if component is not None:
        physics_tag, physics, physics_tags = _find_physics(component, expected_physics_tag, ledger)
        topology["physics_tags"] = physics_tags[:MAX_TAGS]
        topology["physics_tag_count"] = len(physics_tags)
        topology["physics_tags_truncated"] = len(physics_tags) > MAX_TAGS
        if physics is not None and physics_tag is not None and geom is not None:
            periodicity, ports, incidence = _collect_periodic_ports_incidence(
                component,
                geom,
                physics,
                physics_tag,
                boundary_map,
                ledger,
            )
            result["periodicity"] = periodicity
            result["ports"] = ports
            result["incidence"] = incidence
        elif physics is not None and physics_tag is not None:
            ledger.add(
                "unknown",
                "periodic_topology_unavailable",
                "PeriodicStructure evidence requires readable built geometry topology.",
            )
    wavelength, _study, study_tag = _collect_wavelength(
        model,
        jm,
        ledger,
        expected_study_tag=expected_study_tag,
        target_parameter_name=target_wavelength_parameter,
        physics_tag=physics_tag,
    )
    result["wavelength"] = wavelength
    if component is not None:
        result["mesh_study_results"] = _collect_mesh_study_results(
            model, component, physics_tag, study_tag, ledger
        )
    else:
        ledger.add(
            "unknown",
            "mesh_study_results_unavailable",
            "Mesh/study/result evidence requires an unambiguous component.",
        )

    if expected_lattice_axes is not None:
        expected_axes = [value.lower() for value in expected_lattice_axes]
        inferred_axes = []
        axis_names = ("x", "y", "z")
        for feature in result["periodicity"].get("floquet_features", []):
            translation = (feature.get("opposing_face_groups") or {}).get("inferred_translation")
            if translation and any(abs(value) > 0 for value in translation):
                inferred_axes.append(
                    axis_names[
                        max(range(len(translation)), key=lambda index: abs(translation[index]))
                    ]
                )
            else:
                inferred_axes.append(None)
        result["periodicity"]["caller_expected_lattice_axes"] = expected_axes
        result["periodicity"]["inferred_dominant_translation_axes"] = inferred_axes
        result["periodicity"]["expected_axis_comparison"] = [
            {
                "expected": expected,
                "inferred": inferred_axes[index] if index < len(inferred_axes) else None,
            }
            for index, expected in enumerate(expected_axes)
        ]
    if target_physical_polarization is not None:
        values = [float(value) for value in target_physical_polarization]
        if len(values) not in (2, 3) or any(not math.isfinite(value) for value in values):
            raise ValueError("target_physical_polarization must contain 2 or 3 finite numbers")
        result["incidence"]["caller_target_physical_polarization"] = values
        result["incidence"]["target_comparison"] = "not_available_from_label_only_evidence"

    result["evidence"] = ledger.to_dict()
    result["inspection_status"] = ledger.inspection_status
    missing = [
        section
        for section in (
            "topology",
            "periodicity",
            "ports",
            "incidence",
            "wavelength",
            "mesh_study_results",
        )
        if not result.get(section)
    ]
    result["next_call"] = _point_audit_next_call(
        active_profile=active_profile,
        inspection_status=result["inspection_status"],
        missing_evidence=missing,
    )
    result["next_call"]["minimal_inputs"] = {
        "model_name": model_name,
        "component_tag": topology.get("component_tag"),
        "physics_tag": physics_tag,
        "study_tag": study_tag,
        "wavelength_parameter": wavelength.get("parameter_name"),
        "source_sha256": result["provenance"].get("source_sha256"),
    }
    return result


def register_wave_optics_preflight_tools(mcp: MCPServer) -> None:
    """Register the public read-only Wave Optics preflight preflight tool."""

    @mcp.tool()
    def wave_optics_preflight(
        model_name: str,
        expected_component_tag: Optional[str] = None,
        expected_physics_tag: Optional[str] = None,
        expected_study_tag: Optional[str] = None,
        expected_source_path: Optional[str] = None,
        expected_source_sha256: Optional[str] = None,
        target_wavelength_parameter: Optional[str] = None,
        expected_lattice_axes: Optional[list[str]] = None,
        target_physical_polarization: Optional[list[float]] = None,
    ) -> dict[str, Any]:
        """Inspect one exact loaded Wave Optics model without solving or mutation."""
        if not isinstance(model_name, str) or not model_name.strip():
            return {"success": False, "error": "model_name must be exact and non-empty"}
        model = session_manager.get_model(model_name)
        if model is None:
            return {"success": False, "error": f"Model not found: {model_name}"}
        profile_selection = getattr(mcp, "profile_selection", None)
        active_profile = getattr(profile_selection, "name", "unknown")
        try:
            result = collect_wave_optics_preflight(
                model,
                model_name=model_name,
                session_state=session_manager.get_status(),
                active_profile=active_profile,
                expected_component_tag=expected_component_tag,
                expected_physics_tag=expected_physics_tag,
                expected_study_tag=expected_study_tag,
                expected_source_path=expected_source_path,
                expected_source_sha256=expected_source_sha256,
                loaded_source_identity=session_manager.get_model_source_identity(model_name),
                target_wavelength_parameter=target_wavelength_parameter,
                expected_lattice_axes=expected_lattice_axes,
                target_physical_polarization=target_physical_polarization,
            )
            return {"success": True, **result}
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            return {
                "success": False,
                "error": f"Wave Optics preflight failed safely: {_error(exc)}",
            }


__all__ = [
    "EvidenceLedger",
    "collect_preflight_foundation",
    "collect_wave_optics_preflight",
    "register_wave_optics_preflight_tools",
]
