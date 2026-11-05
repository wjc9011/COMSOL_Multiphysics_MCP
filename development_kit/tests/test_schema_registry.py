"""Named artifact schema registry and support-resolution tests."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from src.schema_registry import (
    check_schema_support,
    get_schema_registry,
    select_schema_entry,
)
from src.tools.capabilities import get_capabilities
from src.tools.profiles import ProfileSelection

from src import __version__

ROOT = Path(__file__).parents[2]

# Every dict key an artifact may use to declare its own schema identity.  Both
# spellings occur in this codebase: most modules use ``schema_name``, while the
# surrogate modules use ``schema``.  Scanning only one spelling silently hid 20
# emitted surrogate schemas from the completeness assertion below, so the scanner
# accepts either and the assertion compares the union.
_SCHEMA_IDENTITY_KEYS = frozenset({"schema", "schema_name"})


def _resolve_string(node: ast.AST, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _resolve_string(node.left, constants)
        right = _resolve_string(node.right, constants)
        return left + right if left is not None and right is not None else None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "replace"
        and len(node.args) == 2
    ):
        value = _resolve_string(node.func.value, constants)
        old = _resolve_string(node.args[0], constants)
        new = _resolve_string(node.args[1], constants)
        if value is not None and old is not None and new is not None:
            return value.replace(old, new)
    return None


def _module_string_constants(
    tree: ast.Module, seed: dict[str, str] | None = None
) -> dict[str, str]:
    constants = dict(seed or {})
    pending = list(tree.body)
    for _pass in range(len(pending) + 1):
        changed = False
        for node in pending:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    value = _resolve_string(node.value, constants)
                    if value is not None and constants.get(target.id) != value:
                        constants[target.id] = value
                        changed = True
                elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(
                    node.value, (ast.Tuple, ast.List)
                ):
                    for name, value_node in zip(target.elts, node.value.elts, strict=True):
                        value = _resolve_string(value_node, constants)
                        if isinstance(name, ast.Name) and value is not None:
                            if constants.get(name.id) != value:
                                constants[name.id] = value
                                changed = True
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                value = _resolve_string(node.value, constants) if node.value is not None else None
                if value is not None and constants.get(node.target.id) != value:
                    constants[node.target.id] = value
                    changed = True
        if not changed:
            break
    return constants


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def _imported_string_constants(
    module_name: str,
    tree: ast.Module,
    constants_by_module: dict[str, dict[str, str]],
) -> dict[str, str]:
    imported: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        if node.level:
            package = module_name.split(".")[: -node.level]
            target_module = ".".join([*package, node.module])
        else:
            target_module = node.module
        target_constants = constants_by_module.get(target_module, {})
        for alias in node.names:
            value = target_constants.get(alias.name)
            if value is not None:
                imported[alias.asname or alias.name] = value
    return imported


def _emitted_schemas_in_source() -> set[str]:
    trees = {
        _module_name(path): ast.parse(path.read_text(encoding="utf-8"))
        for path in (ROOT / "comsol_mcp").rglob("*.py")
    }
    constants_by_module = {
        module_name: _module_string_constants(tree) for module_name, tree in trees.items()
    }
    for _pass in range(len(trees) + 1):
        changed = False
        for module_name, tree in trees.items():
            imported = _imported_string_constants(module_name, tree, constants_by_module)
            resolved = _module_string_constants(tree, imported)
            if resolved != constants_by_module[module_name]:
                constants_by_module[module_name] = resolved
                changed = True
        if not changed:
            break

    names: set[str] = set()
    for module_name, tree in trees.items():
        constants = constants_by_module[module_name]
        for node in ast.walk(tree):
            candidates: list[ast.AST] = []
            if isinstance(node, ast.Dict):
                candidates.extend(
                    value
                    for key, value in zip(node.keys, node.values, strict=True)
                    if isinstance(key, ast.Constant) and key.value in _SCHEMA_IDENTITY_KEYS
                )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "dict"
            ):
                candidates.extend(
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg in _SCHEMA_IDENTITY_KEYS
                )
            for candidate in candidates:
                choices = (
                    [candidate.body, candidate.orelse]
                    if isinstance(candidate, ast.IfExp)
                    else [candidate]
                )
                for choice in choices:
                    value = _resolve_string(choice, constants)
                    if value is not None and value.startswith("comsol_mcp."):
                        names.add(value)
    return names


def _selection() -> ProfileSelection:
    return ProfileSelection(
        name="core",
        source="schema-registry-test",
        environment_variable="COMSOL_MCP_PROFILE",
        default_used=False,
    )


def test_registry_is_complete_sorted_and_snapshot_stable():
    registry = get_schema_registry()
    entries = registry["entries"]
    names = [item["schema_name"] for item in entries]

    assert registry["schema_name"] == "comsol_mcp.schema_registry"
    assert registry["schema_version"] == "1.0.0"
    assert registry["producer"] == {"package": "comsol-mcp", "version": __version__}
    # These are deliberate public release snapshots. A registry change updates
    # both literals and development_kit/release/release_facts.json together.
    assert registry["entry_count"] == len(entries) == 141
    assert names == sorted(names)
    assert len(names) == len(set(names))
    emitted = _emitted_schemas_in_source()
    assert emitted
    registry_only = {
        "comsol_mcp.cleanup_outcome",
        "comsol_mcp.execution_evidence_outcome",
        "comsol_mcp.h1_licensed_gate",
        "comsol_mcp.portfolio_evidence_request",
        "comsol_mcp.runtime_compatibility",
        "comsol_mcp.robust_pedot_fixture_manifest",
        "comsol_mcp.robust_forward_shape_stage",
        "comsol_mcp.lin2025_pedot_cylinder_fixture",
        "comsol_mcp.lin2025_pedot_cylinder_fixture.binding",
        "comsol_mcp.simulation_configuration",
        "comsol_mcp.standalone_driver_event",
        "comsol_mcp.standalone_licensed_acceptance",
        "comsol_mcp.standalone_owner",
        "comsol_mcp.standalone_pause_ack",
        "comsol_mcp.standalone_pause_request",
        "comsol_mcp.standalone_status",
        "comsol_mcp.standalone_terminal",
        "comsol_mcp.thermo_optomechanical_replay_manifest",
        "comsol_mcp.wave_optics_point_audit",
    }
    assert set(names) == emitted | registry_only
    assert re.fullmatch(r"[0-9a-f]{64}", registry["registry_sha256"])
    assert registry["registry_sha256"] == (
        "a459a94664382d02d579609afe86797844149e0d5b01a0fa36827a63ee98e265"
    )
    assert registry["registry_sha256"] == get_schema_registry()["registry_sha256"]
    assert check_schema_support("comsol_mcp.session_startup_state", "1.0.0")["supported"] is True


def test_schema_identity_scanner_detects_both_spelling_variants() -> None:
    """The completeness scan must not depend on one identity-key spelling.

    Surrogate modules declare their identity with ``"schema"`` while most of the
    codebase uses ``"schema_name"``.  Scanning only one spelling silently hid 20
    emitted surrogate schemas from ``test_registry_is_complete_sorted_and_snapshot_stable``,
    so this guards the scanner itself rather than the registry contents.
    """
    assert _SCHEMA_IDENTITY_KEYS == frozenset({"schema", "schema_name"})
    source = "\n".join(
        [
            "def build():",
            "    return {'schema': 'comsol_mcp.probe_a', 'schema_version': '1.0.0'}",
            "",
            "def other():",
            "    return {'schema_name': 'comsol_mcp.probe_b'}",
            "",
            "def via_call():",
            "    return dict(schema='comsol_mcp.probe_c')",
            "",
            "def via_call_long():",
            "    return dict(schema_name='comsol_mcp.probe_d')",
        ]
    )
    tree = ast.parse(source)
    constants = _module_string_constants(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        candidates: list[ast.AST] = []
        if isinstance(node, ast.Dict):
            candidates.extend(
                value
                for key, value in zip(node.keys, node.values, strict=True)
                if isinstance(key, ast.Constant) and key.value in _SCHEMA_IDENTITY_KEYS
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "dict"
        ):
            candidates.extend(
                keyword.value for keyword in node.keywords if keyword.arg in _SCHEMA_IDENTITY_KEYS
            )
        for candidate in candidates:
            value = _resolve_string(candidate, constants)
            if value is not None:
                found.add(value)
    assert found == {
        "comsol_mcp.probe_a",
        "comsol_mcp.probe_b",
        "comsol_mcp.probe_c",
        "comsol_mcp.probe_d",
    }


def test_every_emitted_surrogate_schema_is_registered_and_resolvable() -> None:
    """Every emitted surrogate schema must be registered at the version it emits.

    This is the specific guard for the S10/S11 gap: 20 surrogate schemas were
    emitted with ``"schema"`` identity keys and were neither registered nor
    resolvable, while the registry reported itself complete.
    """
    emitted = _emitted_schemas_in_source()
    registered = {entry["schema_name"] for entry in get_schema_registry()["entries"]}
    surrogate = sorted(name for name in emitted if "surrogate" in name)
    assert len(surrogate) >= 22
    assert not [name for name in surrogate if name not in registered]
    for name in surrogate:
        assert check_schema_support(name, "1.0.0")["supported"] is True, name


def test_every_entry_declares_read_write_and_non_mutating_migration_policy():
    for entry in get_schema_registry()["entries"]:
        assert entry["producer_version"] == __version__
        assert entry["readable_versions"]
        assert len(entry["readable_versions"]) == len(set(entry["readable_versions"]))
        assert (
            entry["writable_version"] is None
            or entry["writable_version"] in entry["readable_versions"]
        )
        assert entry["migration"]["rewrites_source_in_place"] is False
        assert entry["migration"]["available"] == bool(entry["migration"]["source_schema_names"])
    physical = next(
        item
        for item in get_schema_registry()["entries"]
        if item["schema_name"] == "comsol_mcp.physical_evidence"
    )
    assert physical["readable_versions"] == ["1.0.0", "1.1.0"]
    assert physical["writable_version"] == "1.1.0"
    assert not check_schema_support("comsol_mcp.robust_shape_optimization_manifest", "1.1.0")[
        "supported"
    ]
    robust_controls = next(
        item
        for item in get_schema_registry()["entries"]
        if item["schema_name"] == "comsol_mcp.robust_condition_controls"
    )
    assert robust_controls["readable_versions"] == [
        "1.0.0",
        "1.1.0",
        "1.2.0",
        "1.3.0",
        "1.4.0",
        "1.5.0",
    ]
    assert robust_controls["writable_version"] == "1.5.0"
    deployment = next(
        item
        for item in get_schema_registry()["entries"]
        if item["schema_name"] == "comsol_mcp.deployment_identity"
    )
    assert deployment["readable_versions"] == ["1.0.0", "1.1.0", "1.2.0"]
    assert deployment["writable_version"] == "1.2.0"
    path_policy = next(
        item
        for item in get_schema_registry()["entries"]
        if item["schema_name"] == "comsol_mcp.path_policy"
    )
    assert path_policy["readable_versions"] == ["1.0.0", "1.1.0"]
    assert path_policy["writable_version"] == "1.1.0"


def test_support_resolution_accepts_current_and_rejects_future_without_mutation():
    registry_before = get_schema_registry()

    accepted = check_schema_support("comsol_mcp.physical_evidence", "1.0.0")
    future = check_schema_support("comsol_mcp.physical_evidence", "99.0.0")
    unknown = check_schema_support("comsol_mcp.unknown_artifact", "1.0.0")

    assert accepted["supported"] is True
    assert accepted["reason_code"] == "supported"
    assert future == {
        "supported": False,
        "reason_code": "unsupported_schema_version",
        "schema_name": "comsol_mcp.physical_evidence",
        "schema_version": "99.0.0",
        "supported_versions": ["1.0.0", "1.1.0"],
        "migration_available": False,
    }
    assert unknown == {
        "supported": False,
        "reason_code": "unknown_schema_name",
        "schema_name": "comsol_mcp.unknown_artifact",
        "schema_version": "1.0.0",
    }
    assert check_schema_support("comsol_mcp.wave_optics_point_audit", "1", for_write=True) == {
        "supported": False,
        "reason_code": "unsupported_schema_version",
        "schema_name": "comsol_mcp.wave_optics_point_audit",
        "schema_version": "1",
        "supported_versions": [],
        "migration_available": True,
    }
    assert (
        check_schema_support("comsol_mcp.wave_optics_point_audit", "99")["migration_available"]
        is False
    )
    registry_after = get_schema_registry()
    assert registry_after == registry_before
    assert registry_after is not registry_before


def test_select_schema_entry_distinguishes_blank_from_unknown_names():
    """Both refusals are explicit and neither ever substitutes another schema.

    ``catalog`` rejects a blank selector before reaching this function, so the
    two refusal paths are asserted here directly: a caller that asked for a
    malformed or unknown name must never receive a plausible-looking entry.
    """
    for blank in ("", "   ", "\t\n"):
        refused = select_schema_entry(blank)
        assert refused["found"] is False
        assert refused["reason_code"] == "invalid_schema_name"
        assert "entry" not in refused
        assert "registry_sha256" not in refused

    for non_string in (None, 7, ["comsol_mcp.physical_evidence"], {"a": 1}):
        refused = select_schema_entry(non_string)
        assert refused["found"] is False
        assert refused["reason_code"] == "invalid_schema_name"
        assert refused["schema_name"] is None

    unknown = select_schema_entry("comsol_mcp.not_a_schema")
    assert unknown["found"] is False
    assert unknown["reason_code"] == "unknown_schema_name"
    assert unknown["schema_name"] == "comsol_mcp.not_a_schema"
    assert "entry" not in unknown

    # Surrounding whitespace is normalized, not treated as a different name.
    found = select_schema_entry("  comsol_mcp.physical_evidence  ")
    assert found["found"] is True
    assert found["reason_code"] == "found"
    assert found["entry"]["schema_name"] == "comsol_mcp.physical_evidence"
    assert found["registry_sha256"] == get_schema_registry()["registry_sha256"]


def test_select_schema_entry_never_hands_out_mutable_registry_state():
    """An on-demand entry is a copy; editing it must not corrupt the registry."""
    selected = select_schema_entry("comsol_mcp.physical_evidence")
    selected["entry"]["schema_name"] = "tampered"
    selected["entry"]["readable_versions"].append("9.9.9")

    again = select_schema_entry("comsol_mcp.physical_evidence")
    assert again["entry"]["schema_name"] == "comsol_mcp.physical_evidence"
    assert "9.9.9" not in again["entry"]["readable_versions"]
    assert again["entry"] == next(
        entry
        for entry in get_schema_registry()["entries"]
        if entry["schema_name"] == "comsol_mcp.physical_evidence"
    )


def test_capabilities_carry_the_compact_registry_view_not_the_full_registry():
    """Cold discovery must not embed the full registry.

    Embedding it made bootstrap scale with the artifact surface: at 0.7.5 the
    193-entry registry was 60,293 B of an 81,845 B capabilities response. The
    bootstrap now carries the same identity as a compact summary, and the full
    entries are fetched on demand from ``catalog``. This assertion is the
    regression guard for that boundary.
    """
    capabilities = get_capabilities(_selection())
    summary = capabilities["schema_registry"]
    full = get_schema_registry()

    assert "entries" not in summary
    assert summary["view"] == "summary"
    # Identity is preserved exactly: the fingerprint is computed over the same
    # body the full registry hashes, so the two views cannot disagree.
    assert summary["registry_sha256"] == full["registry_sha256"]
    assert summary["entry_count"] == full["entry_count"]
    assert summary["producer"] == full["producer"]
    assert summary["full_registry_available"] is True
    assert summary["on_demand_operation"] == "catalog"
    # Bounded: the summary must stay small even as the registry grows.
    assert len(json.dumps(summary, sort_keys=True).encode("utf-8")) < 2048
    assert json.dumps(capabilities, sort_keys=True).encode("utf-8").__len__() <= 32 * 1024


def test_on_demand_schema_fetch_returns_exactly_one_entry():
    """``catalog`` is the on-demand half and never guesses at a name."""
    from src.tools.discovery import get_tool_catalog

    found = get_tool_catalog(_selection(), schema="comsol_mcp.physical_evidence")
    assert found["success"] is True
    assert found["entry"]["schema_name"] == "comsol_mcp.physical_evidence"
    assert found["entry"]["writable_version"] == "1.1.0"
    assert found["registry_sha256"] == get_schema_registry()["registry_sha256"]

    missing = get_tool_catalog(_selection(), schema="comsol_mcp.not_a_schema")
    assert missing["success"] is False
    assert missing["reason_code"] == "unknown_schema_name"
    assert "entry" not in missing


def test_summary_and_full_registry_agree_on_every_count():
    """The compact view must describe the whole registry, never a truncation."""
    from collections import Counter

    from src.schema_registry import summarize_schema_registry

    full = get_schema_registry()
    summary = summarize_schema_registry()
    entries = full["entries"]

    assert summary["entry_count"] == len(entries)
    assert summary["artifact_kind_counts"] == dict(
        sorted(Counter(entry["artifact_kind"] for entry in entries).items())
    )
    assert summary["writable_entry_count"] == sum(
        1 for entry in entries if entry["writable_version"] is not None
    )
    assert summary["migration_capable_entry_count"] == sum(
        1 for entry in entries if entry["migration"]["available"]
    )
    versions = Counter(str(len(entry["readable_versions"])) for entry in entries)
    assert summary["readable_version_count_histogram"] == dict(sorted(versions.items()))


def test_every_advertised_capability_schema_pair_is_readable():
    capabilities = get_capabilities(_selection())
    pairs: list[tuple[str, str, str]] = []

    def collect(value: object, path: str = "capabilities") -> None:
        if isinstance(value, dict):
            schema_name = value.get("schema_name")
            schema_version = value.get("schema_version")
            if isinstance(schema_name, str) and isinstance(schema_version, str):
                pairs.append((path, schema_name, schema_version))
            for key, nested in value.items():
                collect(nested, f"{path}.{key}")
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                collect(nested, f"{path}[{index}]")

    collect(capabilities)
    assert pairs
    unsupported = [
        (path, schema_name, schema_version)
        for path, schema_name, schema_version in pairs
        if not check_schema_support(schema_name, schema_version)["supported"]
    ]
    assert unsupported == []


def test_explicitly_empty_readable_versions_is_not_readable():
    from comsol_mcp.schema_registry import _entry

    not_readable = _entry(
        "demo.artifact.schema",
        "2.0.0",
        "demo-producer",
        readable_versions=(),
    )
    assert not_readable["readable_versions"] == []
    assert not_readable["writable_version"] == "2.0.0"

    fallback = _entry("demo.artifact.schema", "2.0.0", "demo-producer")
    assert fallback["readable_versions"] == ["2.0.0"]
