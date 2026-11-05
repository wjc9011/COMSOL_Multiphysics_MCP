"""Dependency-light runtime configuration for the opt-in semantic feature."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Mapping

from comsol_mcp.settings import settings_environment

from .semantic_contracts import PUBLIC_LIMITS, validate_semantic_build_id
from .semantic_process import SemanticWorkerManager

SEMANTIC_ROOT_ENV = "COMSOL_SEMANTIC_ROOT"
SEMANTIC_LEXICAL_ENV = "COMSOL_SEMANTIC_LEXICAL_INDEX"
SEMANTIC_MODEL_ENV = "COMSOL_SEMANTIC_MODEL_PATH"
DEFAULT_SEMANTIC_ROOT: Path | None = None
DEFAULT_LEXICAL_INDEX: Path | None = None


def _ascii_absolute(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    path = path.resolve()
    try:
        str(path).encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must contain ASCII characters only") from exc
    return path


def semantic_configuration(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    environment = settings_environment(environ)
    raw_root = environment.get(SEMANTIC_ROOT_ENV)
    raw_lexical = environment.get(SEMANTIC_LEXICAL_ENV)
    raw_model = environment.get(SEMANTIC_MODEL_ENV)
    root = _ascii_absolute(raw_root, SEMANTIC_ROOT_ENV) if raw_root else None
    lexical = _ascii_absolute(raw_lexical, SEMANTIC_LEXICAL_ENV) if raw_lexical else None
    model = _ascii_absolute(raw_model, SEMANTIC_MODEL_ENV) if raw_model else None
    missing = []
    if root is None:
        missing.append("root_configuration")
    elif not (root / "current.json").is_file():
        missing.append("current_pointer")
    if lexical is None:
        missing.append("lexical_index_configuration")
    elif not lexical.is_file():
        missing.append("lexical_index")
    if model is None:
        missing.append("model_path_configuration")
    elif not (model / "model_manifest.json").is_file():
        missing.append("model_manifest")
    return {
        "root": str(root) if root is not None else None,
        "lexical_index": str(lexical) if lexical is not None else None,
        "model_path": str(model) if model is not None else None,
        "configured": not missing,
        "missing": missing,
        "environment": {
            "root": SEMANTIC_ROOT_ENV,
            "lexical_index": SEMANTIC_LEXICAL_ENV,
            "model_path": SEMANTIC_MODEL_ENV,
        },
    }


def _lightweight_deployment_identity(configuration: Mapping[str, Any]) -> dict[str, Any] | None:
    if not configuration.get("configured"):
        return None
    try:
        root = Path(str(configuration["root"]))
        pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
        if not isinstance(pointer, dict):
            raise TypeError("current pointer must be a JSON object")
        index = _ascii_absolute(pointer["index_path"], "current pointer index path")
        try:
            index.relative_to((root / "indexes").resolve())
        except ValueError as exc:
            raise ValueError(
                "current pointer index path is outside the deployment index root"
            ) from exc
        manifest_bytes = (index / "manifest.json").read_bytes()
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if not isinstance(manifest, dict):
            raise TypeError("index manifest must be a JSON object")
        model = json.loads(
            (Path(str(configuration["model_path"])) / "model_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        if not isinstance(model, dict):
            raise TypeError("model manifest must be a JSON object")
        required_identity = (
            (pointer, "manifest_sha256"),
            (pointer, "build_id"),
            (pointer, "model_fingerprint"),
            (manifest, "build_id"),
            (manifest, "model_fingerprint"),
            (model, "model_sha256"),
        )
        if any(
            not isinstance(document.get(field), str) or not document[field]
            for document, field in required_identity
        ):
            raise ValueError("semantic deployment identity is incomplete")
        validate_semantic_build_id(manifest["build_id"])
        for field in ("model_id", "model_revision", "model_fingerprint", "corpus_fingerprint"):
            if not isinstance(manifest.get(field), str) or len(manifest[field]) > 256:
                raise ValueError("semantic deployment identity is too large")
    except (OSError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return {"readable": False, "error": "semantic deployment identity is invalid"}
    matches = (
        pointer.get("manifest_sha256") == manifest_sha256
        and pointer.get("build_id") == manifest.get("build_id")
        and pointer.get("model_fingerprint") == manifest.get("model_fingerprint")
        and model.get("model_sha256") == manifest.get("model_fingerprint")
    )
    return {
        "readable": True,
        "lightweight_identity_match": bool(matches),
        "build_id": manifest.get("build_id"),
        "corpus_fingerprint": manifest.get("corpus_fingerprint"),
        "model_id": manifest.get("model_id"),
        "model_revision": manifest.get("model_revision"),
        "model_fingerprint": manifest.get("model_fingerprint"),
        "ranker_validation": "worker_required",
    }


class SemanticService:
    """Own the optional worker without importing its ML stack in the MCP host."""

    def __init__(self, environ: Mapping[str, str] | None = None):
        self.configuration = semantic_configuration(environ)
        self._manager: SemanticWorkerManager | None = None
        self._lock = threading.RLock()
        self._health_gate_passed = False
        self._last_error: dict[str, Any] | None = None

    def _get_manager(self) -> SemanticWorkerManager:
        if not self.configuration["configured"]:
            raise RuntimeError(
                f"semantic retrieval is not configured: {self.configuration['missing']}"
            )
        if self._manager is None:
            self._manager = SemanticWorkerManager(
                backend="hybrid",
                deployment_root=self.configuration["root"],
                lexical_index=self.configuration["lexical_index"],
                model_path=self.configuration["model_path"],
                startup_deadline=20.0,
                query_deadline=PUBLIC_LIMITS["query_deadline_seconds"],
                idle_ttl=300.0,
            )
        return self._manager

    def status(self, *, warm: bool = False) -> dict[str, Any]:
        with self._lock:
            deployment = _lightweight_deployment_identity(self.configuration)
            if warm:
                try:
                    manager = self._get_manager()
                except RuntimeError as exc:
                    health = {
                        "success": False,
                        "error": {"code": "semantic_unavailable", "message": str(exc)},
                    }
                    worker = {"state": "stopped", "health": health}
                else:
                    health = manager.health()
                    worker = {"state": manager.status(probe=False)["state"], "health": health}
                self._health_gate_passed = bool(health.get("success"))
                if health.get("success"):
                    self._last_error = None
                else:
                    self._last_error = health.get("error") or {"message": str(health)}
            elif self._manager is None:
                worker = {"state": "stopped", "health": None}
            else:
                manager_status = self._manager.status(probe=False)
                worker = {"state": manager_status["state"], "health": None}
            return {
                "success": True,
                "configured": self.configuration["configured"],
                "configuration": self.configuration,
                "deployment": deployment,
                "worker": worker,
                "health_gate_passed": self._health_gate_passed,
                "available": bool(
                    self.configuration["configured"]
                    and deployment
                    and deployment.get("lightweight_identity_match")
                    and self._health_gate_passed
                    and worker["state"] == "active"
                ),
                "last_error": self._last_error,
                "solver_free": True,
                "device": "cpu",
                "maturity": "experimental",
                "promotion_status": "rejected_by_semantic_minilm_benchmark",
                "known_limitations": [
                    "paraphrase_multi_recall_regressed",
                    "direct_chinese_recall_at_5_is_zero",
                    "negative_query_abstention_failed",
                    "500_query_worker_rss_growth_requires_a_better_backend_or_model",
                ],
            }

    def search(
        self,
        query: str,
        *,
        module: str | None = None,
        limit: int = 5,
        source: str | None = None,
        page_start: int | None = None,
        page_end: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            try:
                manager = self._get_manager()
            except RuntimeError as exc:
                return {
                    "success": False,
                    "error": {"code": "semantic_unavailable", "message": str(exc)},
                    "fallback_tool": "manual_search",
                    "configuration": self.configuration,
                }
            deployment = _lightweight_deployment_identity(self.configuration)
            if not deployment or not deployment.get("lightweight_identity_match"):
                self._health_gate_passed = False
                return {
                    "success": False,
                    "error": {
                        "code": "semantic_unavailable",
                        "message": "semantic deployment identity is unreadable or mismatched",
                    },
                    "deployment": deployment,
                    "fallback_tool": "manual_search",
                    "configuration": self.configuration,
                }
            filters = {
                key: value
                for key, value in {
                    "module": module,
                    "source": source,
                    "page_start": page_start,
                    "page_end": page_end,
                }.items()
                if value is not None
            }
            result = manager.query(query, limit=limit, filters=filters, retrieval_mode="hybrid")
            error = result.get("error")
            input_error = isinstance(error, dict) and error.get("code") in {
                "invalid_query",
                "invalid_limit",
                "invalid_filters",
                "invalid_retrieval_mode",
            }
            if not input_error:
                self._health_gate_passed = bool(result.get("success"))
            if not result.get("success"):
                self._last_error = result.get("error") or {"message": str(result)}
                return {**result, "fallback_tool": "manual_search"}
            self._last_error = None
            return result

    def reset(self) -> dict[str, Any]:
        with self._lock:
            if self._manager is None:
                self._health_gate_passed = False
                self._last_error = None
                return {
                    "success": True,
                    "reset": False,
                    "message": "semantic worker is already stopped",
                }
            result = self._manager.reset()
            self._health_gate_passed = False
            if result.get("success"):
                self._last_error = None
            else:
                # Mirror search(): a failed reset replaces any stale error so
                # status() reports the actual reset failure, never an older one.
                self._last_error = result.get("error") or {"message": str(result)}
            return result


_SERVICE: SemanticService | None = None
_SERVICE_LOCK = threading.Lock()


def get_semantic_service() -> SemanticService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = SemanticService()
        return _SERVICE


def semantic_capability_status(*, feature_enabled: bool) -> dict[str, Any]:
    service = get_semantic_service()
    status = service.status(warm=False)
    return {
        "feature_enabled": feature_enabled,
        "configured": status["configured"],
        "health_gate_passed": status["health_gate_passed"],
        "available": bool(feature_enabled and status["available"]),
        "worker_state": status["worker"]["state"],
        "device": status["device"],
        "maturity": status["maturity"],
        "promotion_status": status["promotion_status"],
        "known_limitations": status["known_limitations"],
        "starts_comsol": False,
        "acquires_solver_lease": False,
        "fallback_tool": "manual_search",
    }


__all__ = [
    "DEFAULT_LEXICAL_INDEX",
    "DEFAULT_SEMANTIC_ROOT",
    "SEMANTIC_LEXICAL_ENV",
    "SEMANTIC_MODEL_ENV",
    "SEMANTIC_ROOT_ENV",
    "SemanticService",
    "get_semantic_service",
    "semantic_capability_status",
    "semantic_configuration",
]
