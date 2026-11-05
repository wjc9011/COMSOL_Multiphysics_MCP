"""Attached client lifecycle that never starts, clears, or owns COMSOL Server."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from comsol_mcp.durable import (
    atomic_write_json,
    canonical_json_v1,
    canonical_sha256_v1,
)

from .attach_request import normalize_shared_server_attach_request
from .cleanup import evaluate_attached_detach
from .contracts import (
    summarize_shared_listener_bindings,
)
from .identity import (
    normalize_attached_server_identity,
    normalize_shared_model_selector,
)
from .locking import (
    build_shared_model_lock,
    build_shared_model_revision,
    normalize_shared_model_identity,
)
from .preflight import (
    ACCEPTED_RELEASE_LINE,
    classify_shared_server_preflight,
    normalize_comsol_version_readback,
    normalize_shared_preflight_snapshot,
)
from .process_probe import collect_shared_preflight_snapshot

MAX_SERVER_MODELS = 32
MAX_UNLOCK_REASON_CHARACTERS = 512
SOURCE_HASH_CHUNK_BYTES = 1024 * 1024
MAX_REVISION_TREE_NODES = 4096
MAX_REVISION_TREE_BYTES = 4 * 1024 * 1024
MAX_REVISION_PROPERTY_ITEMS = 4096
MAX_REVISION_PROPERTY_TEXT_CHARACTERS = 65536
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024 * 1024
SHARED_MODEL_SNAPSHOT_SCHEMA = "comsol_mcp.shared_model_snapshot"
SHARED_MODEL_SNAPSHOT_VERSION = "1.0.0"
ATTACHED_HANDOFF_RECEIPT_DIRECTORY = "attached-handoff-receipts"
MAX_ATTACHED_HANDOFF_RECEIPTS = 64
REVISION_TREE_GROUPS = (
    "functions",
    "components",
    "geometries",
    "selections",
    "variables",
    "couplings",
    "physics",
    "multiphysics",
    "materials",
    "meshes",
    "studies",
    "solutions",
    "batches",
    "datasets",
)


def _default_ownership_factory():
    from comsol_mcp.tools.ownership import SolverOwnership

    return SolverOwnership()


def _default_attached_receipt_root() -> Path:
    """Durable home for completed attached-handoff receipts (policy-scoped).

    Resolved lazily on first use, never at construction: the production
    singleton builds at import time, before the environment may be fully
    configured. Receipts are keyed by the handed-off model lock SHA-256,
    so the store is identity-bound rather than positional.
    """
    from comsol_mcp.path_policy import PathPolicy

    policy = PathPolicy.from_environment()
    return policy.artifact_write_root / ATTACHED_HANDOFF_RECEIPT_DIRECTORY


def _default_client_factory(host: str, port: int):
    import mph

    return mph.Client(host=host, port=port)


def _default_client_version_reader(client: Any) -> str:
    return str(client.java.getComsolVersion())


def _default_model_inventory_reader(client: Any) -> list[dict[str, Any]]:
    models = list(client.models())
    if len(models) > MAX_SERVER_MODELS:
        raise ValueError(f"server model inventory exceeds {MAX_SERVER_MODELS}")
    inventory = []
    for model in models:
        java = model.java
        tag = str(java.tag())
        label = str(java.label())
        raw_path = java.getFilePath()
        path = None if raw_path is None else str(raw_path) or None
        inventory.append(
            {
                "tag": tag,
                "label": label,
                "file_path": path,
                "unsaved": path is None,
            }
        )
    return inventory


def _revision_json_value(value: Any, remaining_items: list[int] | None = None) -> Any:
    if remaining_items is None:
        remaining_items = [MAX_REVISION_PROPERTY_ITEMS]
    remaining_items[0] -= 1
    if remaining_items[0] < 0:
        raise ValueError("model-tree property exceeds the aggregate item limit")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        if len(value) > MAX_REVISION_PROPERTY_TEXT_CHARACTERS:
            raise ValueError("model-tree property contains oversized text")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("model-tree property contains a non-finite number")
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        if len(value) > MAX_REVISION_PROPERTY_ITEMS:
            raise ValueError("model-tree property contains an oversized object")
        return {
            str(key): _revision_json_value(item, remaining_items)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_REVISION_PROPERTY_ITEMS:
            raise ValueError("model-tree property contains an oversized collection")
        return [_revision_json_value(item, remaining_items) for item in value]
    if value.__class__.__module__.startswith("numpy") and hasattr(value, "tolist"):
        if getattr(value, "size", MAX_REVISION_PROPERTY_ITEMS + 1) > (MAX_REVISION_PROPERTY_ITEMS):
            raise ValueError("model-tree property contains an oversized array")
        return _revision_json_value(value.tolist(), remaining_items)
    if value.__class__.__module__.startswith("mph.") and hasattr(value, "tag"):
        return {
            "node_path": str(value),
            "node_tag": value.tag(),
        }
    raise ValueError(f"model-tree property type is unsupported: {type(value).__name__}")


def _hash_revision_tree_group(
    root: Any,
    *,
    remaining_nodes: list[int],
    remaining_bytes: list[int],
) -> tuple[dict[str, Any], dict[str, Any]]:
    structural_digest = hashlib.sha256()
    state_digest = hashlib.sha256()
    root_children = root.children()
    if len(root_children) > remaining_nodes[0]:
        raise ValueError(f"model revision tree exceeds {MAX_REVISION_TREE_NODES} nodes")
    nodes = [
        ((str(node), node.tag() or ""), index, node) for index, node in enumerate(root_children)
    ]
    heapq.heapify(nodes)
    sequence = len(nodes)
    count = 0
    while nodes:
        _key, _sequence, node = heapq.heappop(nodes)
        remaining_nodes[0] -= 1
        if remaining_nodes[0] < 0:
            raise ValueError(f"model revision tree exceeds {MAX_REVISION_TREE_NODES} nodes")
        path = str(node)
        structural = {
            "path": path,
            "tag": node.tag(),
            "type": node.type(),
        }
        state = {
            "path": path,
            "properties": _revision_json_value(node.properties()),
        }
        for digest, record in (
            (structural_digest, structural),
            (state_digest, state),
        ):
            encoded = canonical_json_v1(record)
            remaining_bytes[0] -= len(encoded)
            if remaining_bytes[0] < 0:
                raise ValueError("model revision tree exceeds the bounded canonical byte budget")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        count += 1
        children = sorted(node.children(), key=lambda child: (str(child), child.tag() or ""))
        if len(children) > remaining_nodes[0]:
            raise ValueError(f"model revision tree exceeds {MAX_REVISION_TREE_NODES} nodes")
        for child in children:
            heapq.heappush(nodes, ((str(child), child.tag() or ""), sequence, child))
            sequence += 1
    return (
        {"node_count": count, "tree_sha256": structural_digest.hexdigest()},
        {"node_count": count, "tree_sha256": state_digest.hexdigest()},
    )


def _default_model_revision_reader(
    client: Any, model_tag: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    matches = [model for model in list(client.models()) if str(model.java.tag()) == model_tag]
    if len(matches) != 1:
        raise ValueError("adopted server model is no longer uniquely available")
    model = matches[0]
    if model.__class__.__module__.startswith("mph."):
        structural_groups = {}
        state_groups = {}
        remaining_nodes = [MAX_REVISION_TREE_NODES]
        remaining_bytes = [MAX_REVISION_TREE_BYTES]
        for group in REVISION_TREE_GROUPS:
            structural_groups[group], state_groups[group] = _hash_revision_tree_group(
                model / group,
                remaining_nodes=remaining_nodes,
                remaining_bytes=remaining_bytes,
            )
        parameters = {
            str(name): str(value)
            for name, value in sorted(model.parameters(evaluate=False).items())
        }
        descriptions = {
            str(name): str(value) for name, value in sorted(model.descriptions().items())
        }
        return (
            {"model_tree": structural_groups},
            {
                "parameters": parameters,
                "parameter_descriptions": descriptions,
                "model_tree": state_groups,
            },
        )
    structural = {
        "components": sorted(str(value) for value in model.components()),
        "studies": sorted(str(value) for value in model.studies()),
        "datasets": sorted(str(value) for value in model.datasets()),
    }
    state = {
        "parameters": {str(name): str(value) for name, value in sorted(model.parameters().items())}
    }
    return structural, state


def _default_mcp_process_identity() -> dict[str, Any]:
    import psutil

    process = psutil.Process(os.getpid())
    try:
        command = list(process.cmdline())
    except psutil.AccessDenied, psutil.ZombieProcess:
        command = []
    signature = hashlib.sha256(
        "\0".join(str(part) for part in command).encode("utf-8", errors="replace")
    ).hexdigest()
    return {
        "pid": process.pid,
        "process_create_time": process.create_time(),
        "command_signature": signature,
    }


def _verify_immutable_source(
    immutable_source: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    if immutable_source is None:
        return None
    if not isinstance(immutable_source, Mapping):
        raise ValueError("immutable source must be an object")
    path_value = immutable_source.get("path")
    expected_sha256 = immutable_source.get("sha256")
    if not isinstance(path_value, str) or not isinstance(expected_sha256, str):
        raise ValueError("immutable source path and SHA-256 are required together")
    path = Path(path_value)
    if not path.is_file():
        raise ValueError("immutable source is not an existing file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(SOURCE_HASH_CHUNK_BYTES):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256.casefold():
        raise ValueError("immutable source SHA-256 does not match the source bytes")
    return dict(immutable_source)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(SOURCE_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _default_snapshot_target_factory(model_tag: str) -> Path:
    from comsol_mcp.path_policy import PathPolicy

    policy = PathPolicy.from_environment()
    candidate = policy.shared_snapshot_root / f"{model_tag}-{uuid.uuid4().hex}.mph"
    return policy.validate_shared_snapshot_write(str(candidate)).normalized_path


def _default_save_copy_writer(client: Any, model_tag: str, target: Path) -> None:
    matches = [model for model in list(client.models()) if str(model.java.tag()) == model_tag]
    if len(matches) != 1:
        raise ValueError("adopted server model is no longer uniquely available")
    matches[0].java.save(str(target), True)


def _default_manifest_writer(path: Path, value: dict[str, Any]) -> None:
    atomic_write_json(path, value)


class SharedSessionManager:
    """One process-local facade for an exact non-owned server connection."""

    def __init__(
        self,
        *,
        snapshot_provider: Callable[[], Mapping[str, Any]] = collect_shared_preflight_snapshot,
        ownership_factory: Callable[[], Any] = _default_ownership_factory,
        client_factory: Callable[[str, int], Any] = _default_client_factory,
        client_version_reader: Callable[[Any], str] = _default_client_version_reader,
        model_inventory_reader: Callable[
            [Any], list[dict[str, Any]]
        ] = _default_model_inventory_reader,
        model_revision_reader: Callable[
            [Any, str], tuple[dict[str, Any], dict[str, Any]]
        ] = _default_model_revision_reader,
        mcp_process_identity_provider: Callable[
            [], Mapping[str, Any]
        ] = _default_mcp_process_identity,
        snapshot_target_factory: Callable[[str], Path] = _default_snapshot_target_factory,
        save_copy_writer: Callable[[Any, str, Path], None] = _default_save_copy_writer,
        save_copy_writer_is_bounded: bool = False,
        manifest_writer: Callable[[Path, dict[str, Any]], None] = _default_manifest_writer,
        clock: Callable[[], float] = time.time,
        attached_receipt_root: Path | None = None,
    ):
        self._snapshot_provider = snapshot_provider
        self._ownership_factory = ownership_factory
        self._client_factory = client_factory
        self._client_version_reader = client_version_reader
        self._model_inventory_reader = model_inventory_reader
        self._model_revision_reader = model_revision_reader
        self._mcp_process_identity_provider = mcp_process_identity_provider
        self._snapshot_target_factory = snapshot_target_factory
        self._save_copy_writer = save_copy_writer
        self._save_copy_writer_is_bounded = bool(save_copy_writer_is_bounded)
        self._manifest_writer = manifest_writer
        self._clock = clock
        self._lock = threading.RLock()
        self._client = None
        self._ownership = None
        self._server_identity = None
        self._selector = None
        self._selected_model = None
        self._inventory_sha256 = None
        self._session_acquisition_id = None
        self._model_lock = None
        self._client_disconnected = False
        self._attach_cleanup_pending = False
        self._detach_inventory_after = None
        self._detach_inventory_error = None
        self._unlock_audit: list[dict[str, Any]] = []
        # Survives detach and session-state resets on purpose: it is the
        # reconciliation identity for an admission interrupted after the
        # lifecycle handoff completed but before its caller recorded it.
        self._last_attached_handoff: dict[str, Any] | None = None
        # Explicit roots win; None defers to the policy-scoped default on
        # first use so construction never evaluates the environment early.
        self._attached_receipt_root_override = (
            None if attached_receipt_root is None else Path(attached_receipt_root)
        )
        self._attached_receipt_root_resolved: Path | None = None

    def _reset_session_state(self) -> None:
        self._client = None
        self._ownership = None
        self._server_identity = None
        self._selector = None
        self._selected_model = None
        self._inventory_sha256 = None
        self._session_acquisition_id = None
        self._model_lock = None
        self._client_disconnected = False
        self._attach_cleanup_pending = False
        self._detach_inventory_after = None
        self._detach_inventory_error = None

    def _unlock_record(self, *, reason: str) -> dict[str, Any]:
        if self._model_lock is None:
            raise RuntimeError("shared model is not locked")
        body = {
            "lock_id": self._model_lock.lock_id,
            "lock_sha256": self._model_lock.lock_sha256,
            "reason": reason,
            "unlocked_at_epoch": self._clock(),
        }
        return {**body, "audit_sha256": canonical_sha256_v1(body)}

    @staticmethod
    def _inventory(reader: Callable[[Any], list[dict[str, Any]]], client: Any):
        raw = reader(client)
        if not isinstance(raw, list) or len(raw) > MAX_SERVER_MODELS:
            raise ValueError("server model inventory is not a bounded list")
        normalized = sorted(
            (normalize_shared_model_identity(item) for item in raw),
            key=lambda item: item.tag,
        )
        tags = [item.tag for item in normalized]
        if len(tags) != len(set(tags)):
            raise ValueError("server model inventory contains duplicate tags")
        public = [item.to_dict() for item in normalized]
        return normalized, canonical_sha256_v1(public)

    @staticmethod
    def _matches(selector, model) -> bool:
        return (
            model.tag == selector.tag
            and (selector.expected_label is None or model.label == selector.expected_label)
            and (
                selector.expected_file_path is None
                or model.file_path == selector.expected_file_path
            )
            and (selector.expected_unsaved is None or model.unsaved is selector.expected_unsaved)
        )

    @staticmethod
    def _server_identity_from_snapshot(endpoint, snapshot):
        normalized = normalize_shared_preflight_snapshot(snapshot)
        listener = summarize_shared_listener_bindings(normalized["listeners"], endpoint=endpoint)
        if not listener["stable"]:
            raise ValueError("declared listener is no longer unique")
        pid = listener["owner_pid"]
        server = next(
            (
                item
                for item in normalized["processes"]
                if item["pid"] == pid and item["kind"] == "comsol_server"
            ),
            None,
        )
        if server is None:
            raise ValueError("declared listener owner is not the exact COMSOL Server")
        return normalize_attached_server_identity(
            {
                "endpoint": {"host": endpoint.host, "port": endpoint.port},
                "server_pid": pid,
                "server_process_create_time": server["create_time"],
                "server_command_signature": server["command_signature"],
                "listener_bind_scope": listener["bind_scope"],
                "listener_observed_at_epoch": normalized["observed_at_epoch"],
            }
        )

    def attach(
        self,
        request: Mapping[str, Any],
        *,
        profile: str,
        environ: Mapping[str, str] | None = None,
        feature_enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Attach to one exact existing server without selecting a model."""
        normalized_request = normalize_shared_server_attach_request(
            request,
            profile=profile,
            environ=environ,
            feature_enabled=feature_enabled,
        )
        with self._lock:
            if self._client is not None or self._ownership is not None:
                return {
                    "success": False,
                    "state": (
                        "cleanup_pending"
                        if self._client_disconnected or self._attach_cleanup_pending
                        else "already_attached"
                    ),
                    "error": (
                        "Complete retained shared-session cleanup before attaching."
                        if self._client_disconnected or self._attach_cleanup_pending
                        else "A shared server is already attached."
                    ),
                }
            first = self._snapshot_provider()
            second = self._snapshot_provider()
            preflight = classify_shared_server_preflight(
                endpoint={
                    "host": normalized_request.endpoint.host,
                    "port": normalized_request.endpoint.port,
                },
                first_probe=first,
                second_probe=second,
            )
            if not preflight["success"]:
                return {**preflight, "success": False, "lease_acquired": False}
            server_identity = self._server_identity_from_snapshot(
                normalized_request.endpoint, second
            )
            ownership = self._ownership_factory()
            lease = ownership.acquire_attached(server_identity)
            if not lease.get("success"):
                return {
                    "success": False,
                    "state": "attached_lease_rejected",
                    "error": lease.get("error", "Attached lease was rejected."),
                    "preflight": preflight,
                    "lease_acquired": False,
                }
            client = None
            post_connect = None
            try:
                client = self._client_factory(
                    normalized_request.endpoint.host,
                    normalized_request.endpoint.port,
                )
                clientapi_raw_version = self._client_version_reader(client)
                server_versions = {
                    item["file_version"]
                    for item in preflight["processes"]
                    if item["kind"] == "comsol_server"
                }
                if len(server_versions) != 1:
                    raise RuntimeError("post-connect Server file version is not unique")
                clientapi_version, version_parts = normalize_comsol_version_readback(
                    clientapi_raw_version,
                    expected_file_version=next(iter(server_versions)),
                )
                if version_parts is None or version_parts[:3] != ACCEPTED_RELEASE_LINE:
                    raise RuntimeError(
                        "post-connect COMSOL version is outside the accepted 6.4.0.* line"
                    )
                server_after = self._server_identity_from_snapshot(
                    normalized_request.endpoint, self._snapshot_provider()
                )
                if server_after.identity_sha256 != server_identity.identity_sha256:
                    raise RuntimeError("attached server identity changed after client connection")
                version_warnings = []
                if server_versions != {clientapi_version}:
                    version_warnings.append("same_accepted_release_line_build_difference")
                post_connect = {
                    "clientapi_raw_version": clientapi_raw_version,
                    "clientapi_comsol_version": clientapi_version,
                    "accepted_release_line": "6.4.0.*",
                    "server_identity_verified": True,
                    "warnings": version_warnings,
                }
                models, inventory_sha256 = self._inventory(self._model_inventory_reader, client)
            except Exception as exc:
                disconnected = client is None
                if client is not None:
                    try:
                        client.disconnect()
                        disconnected = True
                    except Exception:
                        disconnected = False
                if disconnected:
                    try:
                        release = ownership.release()
                    except Exception as cleanup_exc:
                        release = {
                            "success": False,
                            "released": False,
                            "error": (f"{type(cleanup_exc).__name__}: {cleanup_exc}"),
                        }
                else:
                    release = {
                        "success": False,
                        "released": False,
                        "error": "Client disconnect could not be verified.",
                    }
                lease_released = bool(release.get("success") and not ownership.lease_path.exists())
                if not disconnected or not lease_released:
                    self._client = client
                    self._ownership = ownership
                    self._server_identity = server_identity
                    self._inventory_sha256 = None
                    self._session_acquisition_id = lease["lease"]["acquisition_id"]
                    self._client_disconnected = disconnected
                    self._attach_cleanup_pending = True
                return {
                    "success": False,
                    "state": (
                        "attach_cleanup_pending"
                        if not disconnected or not lease_released
                        else "attach_failed"
                    ),
                    "error": f"{type(exc).__name__}: {exc}",
                    "client_disconnected": disconnected,
                    "lease_release": release,
                }

            self._client = client
            self._ownership = ownership
            self._server_identity = server_identity
            self._selector = None
            self._selected_model = None
            self._inventory_sha256 = inventory_sha256
            self._session_acquisition_id = lease["lease"]["acquisition_id"]
            self._client_disconnected = False
            self._attach_cleanup_pending = False
            self._detach_inventory_after = None
            self._detach_inventory_error = None
            return {
                "success": True,
                "state": "attached_model_pending_adoption",
                "server_identity_sha256": server_identity.identity_sha256,
                "session_acquisition_id": self._session_acquisition_id,
                "model_count": len(models),
                "model_inventory_sha256": inventory_sha256,
                "ownership": "external_user_owned_server",
                "can_start_comsol": False,
                "preflight": preflight,
                "post_connect": post_connect,
            }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "success": True,
                "attached": self._client is not None,
                "state": (
                    "attach_cleanup_pending"
                    if self._attach_cleanup_pending
                    else (
                        "detach_cleanup_pending"
                        if self._client_disconnected and self._ownership is not None
                        else (
                            "attached_model_locked"
                            if self._model_lock is not None
                            else (
                                "attached_model_pending_lock"
                                if self._selected_model is not None
                                else (
                                    "attached_model_pending_adoption"
                                    if self._client is not None
                                    else "detached"
                                )
                            )
                        )
                    )
                ),
                "server_identity_sha256": (
                    None if self._server_identity is None else self._server_identity.identity_sha256
                ),
                "session_acquisition_id": self._session_acquisition_id,
                "model_inventory_sha256": self._inventory_sha256,
                "ownership": (
                    "external_user_owned_server" if self._ownership is not None else None
                ),
                "can_start_comsol": False,
                "model_lock": (
                    None
                    if self._model_lock is None
                    else {
                        "lock_id": self._model_lock.lock_id,
                        "lock_sha256": self._model_lock.lock_sha256,
                        "revision_sha256": self._model_lock.revision["revision_sha256"],
                        "collaboration_mode": self._model_lock.collaboration_mode,
                    }
                ),
                "last_unlock_audit": (
                    None if not self._unlock_audit else dict(self._unlock_audit[-1])
                ),
            }

    def models(self) -> dict[str, Any]:
        """Return one bounded fresh inventory without changing the baseline."""
        with self._lock:
            if self._attach_cleanup_pending or self._client_disconnected:
                return {
                    "success": False,
                    "state": self.status()["state"],
                    "models": [],
                    "model_count": 0,
                }
            if self._client is None:
                return {
                    "success": False,
                    "state": "detached",
                    "models": [],
                    "model_count": 0,
                }
            try:
                models, inventory_sha256 = self._inventory(
                    self._model_inventory_reader, self._client
                )
            except Exception as exc:
                return {
                    "success": False,
                    "state": "model_inventory_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            return {
                "success": True,
                "state": (
                    "attached_model_locked"
                    if self._model_lock is not None
                    else (
                        "attached_model_pending_lock"
                        if self._selected_model is not None
                        else "attached_model_pending_adoption"
                    )
                ),
                "models": [model.to_dict() for model in models],
                "model_count": len(models),
                "model_inventory_sha256": inventory_sha256,
                "attached_inventory_sha256": self._inventory_sha256,
            }

    def adopt_model(self, selector: Mapping[str, Any]) -> dict[str, Any]:
        """Adopt one fresh exact tag after the caller has seen inventory."""
        normalized_selector = normalize_shared_model_selector(selector)
        with self._lock:
            if self._client is None:
                return {"success": False, "state": "detached"}
            if self._model_lock is not None:
                return {"success": False, "state": "model_lock_active"}
            try:
                models, inventory_sha256 = self._inventory(
                    self._model_inventory_reader, self._client
                )
                matches = [model for model in models if self._matches(normalized_selector, model)]
            except Exception as exc:
                return {
                    "success": False,
                    "state": "model_adoption_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            if len(matches) != 1:
                return {
                    "success": False,
                    "state": ("no_server_models" if not models else "model_selector_not_unique"),
                    "model_count": len(models),
                    "match_count": len(matches),
                    "model_inventory_sha256": inventory_sha256,
                }
            self._selector = normalized_selector
            self._selected_model = matches[0]
            return {
                "success": True,
                "state": "attached_model_pending_lock",
                "selected_model": matches[0].to_dict(),
                "model_count": len(models),
                "model_inventory_sha256": inventory_sha256,
            }

    def lock_model(
        self,
        *,
        collaboration_mode: str,
        immutable_source: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Lock the exact adopted model against bounded optimistic readback."""
        with self._lock:
            if self._client is None or self._selected_model is None:
                return {"success": False, "state": "detached"}
            if self._model_lock is not None:
                return {
                    "success": False,
                    "state": "model_already_locked",
                    "lock_sha256": self._model_lock.lock_sha256,
                }
            try:
                server_now = self._server_identity_from_snapshot(
                    self._server_identity.endpoint, self._snapshot_provider()
                )
                if server_now.identity_sha256 != self._server_identity.identity_sha256:
                    raise RuntimeError("attached server identity changed before model lock")
                models, _inventory_sha256 = self._inventory(
                    self._model_inventory_reader, self._client
                )
                matches = [model for model in models if model.tag == self._selected_model.tag]
                if len(matches) != 1:
                    raise RuntimeError("adopted model tag is no longer unique")
                current_model = matches[0]
                if current_model.identity_sha256 != self._selected_model.identity_sha256:
                    raise RuntimeError("adopted model identity changed before model lock")
                structural, state = self._model_revision_reader(self._client, current_model.tag)
                revision = build_shared_model_revision(
                    current_model,
                    sequence=0,
                    structural_readback=structural,
                    state_readback=state,
                )
                model_lock = build_shared_model_lock(
                    attached_server=self._server_identity,
                    session_acquisition_id=self._session_acquisition_id,
                    model=current_model,
                    revision=revision,
                    collaboration_mode=collaboration_mode,
                    immutable_source=_verify_immutable_source(immutable_source),
                    lock_created_at_epoch=self._clock(),
                    mcp_process=self._mcp_process_identity_provider(),
                )
            except Exception as exc:
                return {
                    "success": False,
                    "state": "model_lock_rejected",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            self._model_lock = model_lock
            return {
                "success": True,
                "state": "attached_model_locked",
                "model_lock": model_lock.to_dict(),
            }

    def verify_model_lock(
        self, *, expected_lock_sha256: str, expected_revision_sha256: str
    ) -> dict[str, Any]:
        """Re-read exact server/model/revision identities and fail closed."""
        with self._lock:
            if self._model_lock is None or self._client is None:
                return {"success": False, "state": "model_not_locked"}
            changed_fields = []
            if expected_lock_sha256 != self._model_lock.lock_sha256:
                changed_fields.append("expected_lock_sha256")
            locked_revision_sha256 = self._model_lock.revision["revision_sha256"]
            if expected_revision_sha256 != locked_revision_sha256:
                changed_fields.append("expected_revision_sha256")
            try:
                server_now = self._server_identity_from_snapshot(
                    self._server_identity.endpoint, self._snapshot_provider()
                )
                if server_now.identity_sha256 != self._server_identity.identity_sha256:
                    changed_fields.append("attached_server")
                models, _inventory_sha256 = self._inventory(
                    self._model_inventory_reader, self._client
                )
                matches = [model for model in models if model.tag == self._selected_model.tag]
                if len(matches) != 1:
                    changed_fields.append("model_tag")
                else:
                    current_model = matches[0]
                    if current_model.identity_sha256 != self._selected_model.identity_sha256:
                        changed_fields.append("model_identity")
                    else:
                        structural, state = self._model_revision_reader(
                            self._client, current_model.tag
                        )
                        current_revision = build_shared_model_revision(
                            current_model,
                            sequence=self._model_lock.revision["sequence"],
                            structural_readback=structural,
                            state_readback=state,
                        )
                        if (
                            current_revision.structural_sha256
                            != self._model_lock.revision["structural_sha256"]
                        ):
                            changed_fields.append("structural_readback")
                        if (
                            current_revision.readback_sha256
                            != self._model_lock.revision["readback_sha256"]
                        ):
                            changed_fields.append("state_readback")
            except Exception as exc:
                return {
                    "success": False,
                    "state": "model_lock_verification_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "changed_fields": sorted(set(changed_fields)),
                }
            changed_fields = sorted(set(changed_fields))
            return {
                "success": not changed_fields,
                "state": "model_lock_verified" if not changed_fields else "model_guard_mismatch",
                "changed_fields": changed_fields,
                "lock_sha256": self._model_lock.lock_sha256,
                "revision_sha256": locked_revision_sha256,
            }

    def unlock_model(self, *, expected_lock_sha256: str, reason: str) -> dict[str, Any]:
        """Release only the MCP guard and retain one bounded audit record."""
        with self._lock:
            if self._model_lock is None:
                return {"success": False, "state": "model_not_locked"}
            if expected_lock_sha256 != self._model_lock.lock_sha256:
                return {"success": False, "state": "model_lock_identity_mismatch"}
            if not isinstance(reason, str) or not reason.strip():
                return {"success": False, "state": "unlock_reason_required"}
            normalized_reason = reason.strip()
            if len(normalized_reason) > MAX_UNLOCK_REASON_CHARACTERS:
                return {"success": False, "state": "unlock_reason_too_long"}
            audit = self._unlock_record(reason=normalized_reason)
            self._unlock_audit.append(audit)
            self._unlock_audit = self._unlock_audit[-32:]
            self._model_lock = None
            return {
                "success": True,
                "state": "attached_model_pending_lock",
                "unlock_audit": audit,
            }

    def snapshot_model(
        self,
        *,
        expected_lock_sha256: str,
        expected_revision_sha256: str,
        max_snapshot_bytes: int,
    ) -> dict[str, Any]:
        """Create one contained Save Copy and commit a complete manifest last."""
        with self._lock:
            if (
                isinstance(max_snapshot_bytes, bool)
                or not isinstance(max_snapshot_bytes, int)
                or max_snapshot_bytes <= 0
                or max_snapshot_bytes > MAX_SNAPSHOT_BYTES
            ):
                return {"success": False, "state": "snapshot_size_limit_invalid"}
            if not self._save_copy_writer_is_bounded:
                return {
                    "success": False,
                    "state": "snapshot_write_bound_unavailable",
                    "write_attempted": False,
                    "required_capability": "incremental_native_write_byte_limit",
                }
            verified_before = self.verify_model_lock(
                expected_lock_sha256=expected_lock_sha256,
                expected_revision_sha256=expected_revision_sha256,
            )
            if not verified_before.get("success"):
                return {
                    "success": False,
                    "state": "snapshot_precondition_failed",
                    "model_lock_verification": verified_before,
                }
            target = None
            manifest_path = None
            target_owned = False
            manifest_owned = False
            try:
                target = self._snapshot_target_factory(self._selected_model.tag)
                if target.exists():
                    raise FileExistsError("shared snapshot target already exists")
                manifest_path = target.with_suffix(".manifest.json")
                if manifest_path.exists():
                    raise FileExistsError("shared snapshot manifest already exists")
                target_owned = True
                self._save_copy_writer(self._client, self._selected_model.tag, target)
                if not target.is_file():
                    raise RuntimeError("Save Copy did not create a snapshot file")
                snapshot_size = target.stat().st_size
                if snapshot_size <= 0:
                    raise RuntimeError("Save Copy created an empty snapshot file")
                if snapshot_size > max_snapshot_bytes:
                    raise ValueError("Save Copy exceeds the caller-declared byte limit")
                verified_after = self.verify_model_lock(
                    expected_lock_sha256=expected_lock_sha256,
                    expected_revision_sha256=expected_revision_sha256,
                )
                if not verified_after.get("success"):
                    raise RuntimeError("model identity or revision changed during Save Copy")
                source = _verify_immutable_source(self._model_lock.immutable_source)
                snapshot_sha256 = _hash_file(target)
                body = {
                    "schema_name": SHARED_MODEL_SNAPSHOT_SCHEMA,
                    "schema_version": SHARED_MODEL_SNAPSHOT_VERSION,
                    "snapshot_id": target.stem,
                    "created_at_epoch": self._clock(),
                    "save_copy_api": "Model.java.save(path, True)",
                    "model": dict(self._model_lock.model),
                    "lock_sha256": self._model_lock.lock_sha256,
                    "revision_sha256": self._model_lock.revision["revision_sha256"],
                    "immutable_source": source,
                    "snapshot": {
                        "file_name": target.name,
                        "sha256": snapshot_sha256,
                        "size_bytes": snapshot_size,
                    },
                    "identity_preserved": True,
                    "complete": True,
                }
                manifest_owned = True
                self._manifest_writer(manifest_path, body)
                manifest_sha256 = _hash_file(manifest_path)
                return {
                    "success": True,
                    "state": "snapshot_complete",
                    "snapshot_path": str(target),
                    "snapshot_sha256": snapshot_sha256,
                    "snapshot_size_bytes": snapshot_size,
                    "manifest_path": str(manifest_path),
                    "manifest_sha256": manifest_sha256,
                    "model_lock_verification": verified_after,
                    "identity_preserved": True,
                }
            except Exception as exc:
                cleanup_errors = []
                for artifact, owned in (
                    (manifest_path, manifest_owned),
                    (target, target_owned),
                ):
                    if artifact is None or not owned:
                        continue
                    try:
                        artifact.unlink(missing_ok=True)
                    except Exception as cleanup_exc:
                        cleanup_errors.append(
                            f"{artifact.name}: {type(cleanup_exc).__name__}: {cleanup_exc}"
                        )
                return {
                    "success": False,
                    "state": "snapshot_incomplete",
                    "error": f"{type(exc).__name__}: {exc}",
                    "snapshot_path": None if target is None else str(target),
                    "partial_snapshot_exists": bool(
                        target_owned and target is not None and target.is_file()
                    ),
                    "complete_manifest_exists": bool(
                        manifest_owned and manifest_path is not None and manifest_path.is_file()
                    ),
                    "artifacts_removed": (
                        not cleanup_errors
                        and not bool(target_owned and target is not None and target.exists())
                        and not bool(
                            manifest_owned and manifest_path is not None and manifest_path.exists()
                        )
                    ),
                    "cleanup_errors": cleanup_errors,
                }

    def prepare_attached_job_handoff(
        self,
        *,
        expected_lock_sha256: str,
        expected_revision_sha256: str,
        source_model_path: str,
        user_confirmed_automation_exclusive: bool,
    ) -> dict[str, Any]:
        """Create an immutable worker target and release this client's session."""
        with self._lock:
            if self._client is None or self._model_lock is None:
                return {"success": False, "state": "model_not_locked"}
            if user_confirmed_automation_exclusive is not True:
                return {
                    "success": False,
                    "state": "automation_confirmation_required",
                }
            if self._model_lock.collaboration_mode != "automation_exclusive":
                return {
                    "success": False,
                    "state": "automation_exclusive_lock_required",
                }
            verified = self.verify_model_lock(
                expected_lock_sha256=expected_lock_sha256,
                expected_revision_sha256=expected_revision_sha256,
            )
            if not verified.get("success"):
                return {
                    "success": False,
                    "state": "handoff_precondition_failed",
                    "model_lock_verification": verified,
                }
            try:
                from comsol_mcp.jobs.attached_backend import (
                    normalize_attached_execution_backend,
                )

                source = _verify_immutable_source(self._model_lock.immutable_source)
                if source is None:
                    raise ValueError("attached durable work requires an immutable source")
                requested_path = Path(source_model_path).expanduser().resolve()
                declared_path = Path(source["path"]).expanduser().resolve()
                if os.path.normcase(str(requested_path)) != os.path.normcase(str(declared_path)):
                    raise ValueError(
                        "job source_model_path does not match the locked immutable source"
                    )
                backend = normalize_attached_execution_backend(
                    {
                        "kind": "attached_shared_server",
                        "user_confirmed_automation_exclusive": True,
                        "source_model_lock_sha256": self._model_lock.lock_sha256,
                        "attached_server": self._model_lock.attached_server,
                        "model": self._model_lock.model,
                        "expected_revision": self._model_lock.revision,
                    }
                )
            except Exception as exc:
                return {
                    "success": False,
                    "state": "handoff_target_rejected",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            unlocked = self._unlock_record(reason="durable attached job handoff")
            handed_off_lock_sha256 = self._model_lock.lock_sha256
            handed_off_revision_sha256 = self._model_lock.revision["revision_sha256"]
            detached = self._detach(allow_model_lock=True)
            if not detached.get("success"):
                return {
                    "success": False,
                    "state": "handoff_detach_failed",
                    "execution_backend": backend,
                    "detach": detached,
                    "model_guard_preserved": self._model_lock is not None,
                }
            self._unlock_audit.append(unlocked)
            self._unlock_audit = self._unlock_audit[-32:]
            receipt = {
                "success": True,
                "state": "attached_job_handoff_ready",
                "execution_backend": backend,
                "unlock": unlocked,
                "detach": detached,
                "source_model_lock_sha256": handed_off_lock_sha256,
                "source_revision_sha256": handed_off_revision_sha256,
                "source_model_path": str(declared_path),
            }
            # Record the completed handoff BEFORE the caller can observe it,
            # durably (identity-bound, survives process replacement) and in
            # memory (survives the return boundary): a caller that dies
            # between this detach and its own durable recording leaves an
            # admission that can never repeat the handoff (the model is no
            # longer locked), so this receipt is the identity its recovery
            # reconciles against.
            self._persist_attached_handoff_receipt(receipt)
            self._last_attached_handoff = receipt
            return dict(receipt)

    def _attached_receipt_root(self) -> Path:
        if self._attached_receipt_root_resolved is None:
            override = self._attached_receipt_root_override
            self._attached_receipt_root_resolved = (
                _default_attached_receipt_root() if override is None else override
            )
        return self._attached_receipt_root_resolved

    @staticmethod
    def _attached_receipt_path(root: Path, lock_sha256: str) -> Path:
        return root / f"{lock_sha256}.json"

    def _persist_attached_handoff_receipt(self, receipt: dict[str, Any]) -> None:
        """Write one identity-bound completed-handoff receipt, best effort.

        The write must never undo a completed detach: on any storage or
        serialization failure the in-memory slot still serves same-process
        recovery, and the next handoff retry reports its own truthful
        failure. Receipts are capped so the runtime directory stays
        bounded; the request-binding receipt (written by the attached flow
        once it observes success) remains the primary durable evidence.
        """
        try:
            root = self._attached_receipt_root()
            root.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                self._attached_receipt_path(root, receipt["source_model_lock_sha256"]),
                receipt,
            )
            self._prune_attached_receipt_files(root)
        except OSError, TypeError, ValueError:
            return

    @staticmethod
    def _prune_attached_receipt_files(root: Path) -> None:
        try:
            receipts = [path for path in root.iterdir() if path.is_file()]
        except OSError:
            return
        excess = len(receipts) - MAX_ATTACHED_HANDOFF_RECEIPTS
        if excess <= 0:
            return
        try:
            receipts.sort(key=lambda path: path.stat().st_mtime)
        except OSError:
            return
        for stale in receipts[:excess]:
            try:
                stale.unlink()
            except OSError:
                return

    @staticmethod
    def _matching_attached_receipt(
        receipt: object,
        *,
        expected_lock_sha256: str,
        expected_revision_sha256: str,
        source_model_path: str,
    ) -> dict[str, Any] | None:
        """Return the receipt when its identity matches exactly, else None."""
        if not isinstance(receipt, dict):
            return None
        if receipt.get("source_model_lock_sha256") != expected_lock_sha256:
            return None
        if receipt.get("source_revision_sha256") != expected_revision_sha256:
            return None
        try:
            requested = Path(str(source_model_path)).expanduser().resolve()
            recorded = Path(str(receipt.get("source_model_path", ""))).expanduser().resolve()
        except OSError, RuntimeError:
            return None
        if os.path.normcase(str(requested)) != os.path.normcase(str(recorded)):
            return None
        return receipt

    def _read_durable_attached_receipt(self, lock_sha256: str) -> dict[str, Any] | None:
        if not isinstance(lock_sha256, str) or len(lock_sha256) != 64:
            return None
        if any(character not in "0123456789abcdef" for character in lock_sha256):
            return None
        try:
            with self._attached_receipt_path(self._attached_receipt_root(), lock_sha256).open(
                "r", encoding="utf-8"
            ) as handle:
                payload = json.load(handle)
        except OSError, ValueError, RuntimeError:
            return None
        return payload if isinstance(payload, dict) else None

    def completed_attached_handoff(
        self,
        *,
        expected_lock_sha256: str,
        expected_revision_sha256: str,
        source_model_path: str,
    ) -> dict[str, Any] | None:
        """Return this session's completed handoff receipt when identity matches.

        A caller recovering an interrupted attached admission lost the
        handoff result between the lifecycle detach and its own durable
        recording; repeating the handoff is impossible because the model is
        no longer locked. The in-memory slot reconciles same-process loss
        at the return boundary; the identity-bound durable store reconciles
        loss across process replacement (a fresh manager and session
        reconstructed over retained storage). When the expected lock,
        revision and immutable source match a completed handoff EXACTLY,
        its receipt is the reconciled identity for completing that
        admission. Any disagreement returns None so the caller falls
        through to its normal handoff path.
        """
        with self._lock:
            memory = self._matching_attached_receipt(
                self._last_attached_handoff,
                expected_lock_sha256=expected_lock_sha256,
                expected_revision_sha256=expected_revision_sha256,
                source_model_path=source_model_path,
            )
            if memory is not None:
                return dict(memory)
            durable = self._matching_attached_receipt(
                self._read_durable_attached_receipt(expected_lock_sha256),
                expected_lock_sha256=expected_lock_sha256,
                expected_revision_sha256=expected_revision_sha256,
                source_model_path=source_model_path,
            )
            if durable is not None:
                return dict(durable)
            return None

    def recover_attached_job_handoff(
        self,
        execution_backend: Mapping[str, Any],
        *,
        profile: str,
        feature_enabled: bool,
    ) -> dict[str, Any]:
        """Reattach and re-adopt an exact target after a pre-launch submit failure."""
        from comsol_mcp.jobs.attached_backend import (
            normalize_attached_execution_backend,
        )

        with self._lock:
            try:
                backend = normalize_attached_execution_backend(execution_backend)
            except (TypeError, ValueError) as exc:
                return {
                    "success": False,
                    "state": "attached_handoff_backend_invalid",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            server = backend["attached_server"]
            model = backend["model"]
            attached = self.attach(
                {
                    "endpoint": {
                        "host": server["endpoint"]["host"],
                        "port": server["endpoint"]["port"],
                    },
                    "user_confirmed": True,
                },
                profile=profile,
                feature_enabled=feature_enabled,
            )
            if not attached.get("success"):
                return {
                    "success": False,
                    "state": "attached_handoff_reattach_failed",
                    "attach": attached,
                }
            if attached.get("server_identity_sha256") != server["identity_sha256"]:
                cleanup = self.detach()
                return {
                    "success": False,
                    "state": "attached_handoff_server_identity_changed",
                    "attach": attached,
                    "cleanup": cleanup,
                }
            selector = {
                "tag": model["tag"],
                "expected_label": model["label"],
                "expected_file_path": (None if model["unsaved"] else model["file_path"]),
                "expected_unsaved": True if model["unsaved"] else None,
            }
            adoption = self.adopt_model(selector)
            selected = adoption.get("selected_model") or {}
            if (
                not adoption.get("success")
                or selected.get("identity_sha256") != model["identity_sha256"]
            ):
                cleanup = self.detach()
                return {
                    "success": False,
                    "state": "attached_handoff_model_recovery_failed",
                    "attach": attached,
                    "adoption": adoption,
                    "cleanup": cleanup,
                }
            return {
                "success": True,
                "state": "attached_handoff_reclaimed_pending_lock",
                "server_identity_sha256": server["identity_sha256"],
                "model_identity_sha256": model["identity_sha256"],
                "model_lock_restored": False,
                "next_action": ("lock the adopted model before further mutation or execution"),
            }

    def detach(self) -> dict[str, Any]:
        """Disconnect only the MCP client and prove external preservation."""
        return self._detach(allow_model_lock=False)

    def _detach(self, *, allow_model_lock: bool) -> dict[str, Any]:
        with self._lock:
            if self._client is None and self._ownership is None:
                return {"success": True, "state": "detached", "detached": False}
            if self._model_lock is not None and not allow_model_lock:
                return {
                    "success": False,
                    "state": "model_lock_active",
                    "error": "Unlock the shared model before detaching.",
                }
            if (
                not self._attach_cleanup_pending
                and self._detach_inventory_after is None
                and self._detach_inventory_error is None
            ):
                try:
                    _models, self._detach_inventory_after = self._inventory(
                        self._model_inventory_reader, self._client
                    )
                except Exception as exc:
                    self._detach_inventory_error = f"{type(exc).__name__}: {exc}"
            if not self._client_disconnected:
                try:
                    self._client.disconnect()
                    self._client_disconnected = True
                except Exception as exc:
                    return {
                        "success": False,
                        "state": "detach_uncertain",
                        "error": f"{type(exc).__name__}: {exc}",
                        "client_disconnected": False,
                        "lease_released": False,
                        "model_guard_preserved": self._model_lock is not None,
                    }
            try:
                release = self._ownership.release()
            except Exception as exc:
                release = {
                    "success": False,
                    "released": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            lease_released = bool(
                release.get("success") and not self._ownership.lease_path.exists()
            )
            if not lease_released:
                return {
                    "success": False,
                    "state": "detach_cleanup_pending",
                    "error": release.get("error", "Attached lease release was not verified."),
                    "client_disconnected": True,
                    "lease_released": False,
                    "detach_release": release,
                    "model_guard_preserved": self._model_lock is not None,
                }
            if self._attach_cleanup_pending:
                self._reset_session_state()
                return {
                    "success": True,
                    "state": "detached",
                    "detached": True,
                    "client_disconnected": True,
                    "lease_released": True,
                    "attach_cleanup_completed": True,
                    "detach_release": release,
                }
            try:
                snapshot = self._snapshot_provider()
            except Exception:
                snapshot = {"processes": [], "listeners": []}
            try:
                server_after = self._server_identity_from_snapshot(
                    self._server_identity.endpoint, snapshot
                )
                listener_active = True
            except KeyError, TypeError, ValueError:
                server_after = None
                listener_active = False
            outcome = evaluate_attached_detach(
                server_before=self._server_identity,
                server_after=server_after,
                model_inventory_before_sha256=self._inventory_sha256,
                model_inventory_after_sha256=self._detach_inventory_after,
                client_disconnected=True,
                lease_released=lease_released,
                listener_active_after=listener_active,
                model_clear_attempted=False,
                external_server_shutdown_attempted=False,
                external_server_termination_attempted=False,
            )
            result = {
                **outcome.to_dict(),
                "state": "detached" if outcome.success else "detached_preservation_failed",
                "detach_release": release,
            }
            if self._detach_inventory_error is not None:
                result["inventory_error"] = self._detach_inventory_error
            self._reset_session_state()
            return result


__all__ = ["SharedSessionManager"]
