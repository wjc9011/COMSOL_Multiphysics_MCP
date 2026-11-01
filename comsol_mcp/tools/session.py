"""Session management tools for COMSOL MCP Server."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Optional

from mcp.server.mcpserver import MCPServer

from comsol_mcp.durable import atomic_write_json

from .ownership import ownership_manager
from .session_status import set_session_status

STARTUP_STATE_SCHEMA = "comsol_mcp.session_startup_state"
STARTUP_STATE_VERSION = "1.0.0"
# Give the MCP transport enough time to serialize and flush the start response
# before JPype can enter blocking JVM initialization in the worker thread.
STARTUP_RESPONSE_GRACE_SECONDS = 2.0
STARTUP_TIMEOUT_SECONDS = 180.0
MAX_STARTUP_PHASES = 32


class _LazyMphModule:
    """Compatibility proxy that imports MPh only when a client is needed.

    Attribute READS fall back to the real module, imported on first use.
    Attribute WRITES and DELETES stay on the proxy's own __dict__:
    patching tools (pytest's monkeypatch, unittest.mock) record and undo
    instance-dict state, and a proxy that forwarded writes into the real
    module would leave their undo deleting real module attributes (the
    pytest 9.2 __dict__-based undo did exactly that to mph.Client).
    Production code mutates the real modules only through references it
    obtained from them directly, never through this proxy.
    """

    def __init__(self) -> None:
        self._module = None

    def _get(self):
        module = self._module
        if module is None:
            import mph as module

            self._module = module
        return module

    def __getattr__(self, name: str):
        if name == "_module":
            raise AttributeError(name)
        return getattr(self._get(), name)


class _LazyMphSessionModule(_LazyMphModule):
    """Compatibility proxy for MPh's process-global session module."""

    def _get(self):
        module = self._module
        if module is None:
            import mph.session as module

            self._module = module
        return module


mph = _LazyMphModule()
mph_session = _LazyMphSessionModule()


def _load_mph():
    """Return the lazy MPh proxies used by every session read path.

    Returning the proxies keeps test patches on session.mph /
    session.mph_session effective for code that binds these names
    locally, while the proxies keep real-module reads lazy.
    """

    return mph, mph_session


class SessionManager:
    """Singleton manager for COMSOL client session."""

    _instance: Optional["SessionManager"] = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is not None:
            return cls._instance

        with cls._instance_lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._client = None
                instance._client_status = None
                instance._client_status_client = None
                instance._models = {}
                instance._model_paths = {}
                instance._model_source_identities = {}
                instance._model_java_tags = {}
                instance._model_revisions = {}
                instance._model_cleanup_paths = {}
                instance._model_removal_listeners = []
                instance._current_model = None
                # comsol_start runs mph.Client() in this background thread.
                instance._starting = False
                instance._start_thread = None
                instance._start_error = None
                instance._start_message = ""
                instance._start_cancel_requested = False
                instance._start_cleanup_pending = False
                instance._start_timed_out = False
                instance._host_restart_required = False
                instance._start_attempt_id = None
                instance._startup_record = None
                instance._start_watchdog = None
                instance._start_lock = threading.RLock()
                instance._ownership = ownership_manager
                instance._owns_solver_lease = False
                # A stand-alone MPh client owns a process-global JVM and cannot
                # be reconstructed after disconnect. Keep the exact wrapper for
                # reuse while presenting an inactive MCP session to callers.
                instance._reusable_client = None
                instance._reusable_client_kind = None
                instance._startup_receipt_path = (
                    ownership_manager.runtime_dir / "session" / "startup-state.json"
                )
                cls._instance = instance
        return cls._instance

    @property
    def client(self) -> Optional[mph.Client]:
        with self._start_lock:
            return self._client

    @property
    def is_connected(self) -> bool:
        with self._start_lock:
            return self._client is not None

    @property
    def current_model(self) -> Optional[str]:
        with self._start_lock:
            return self._current_model

    @property
    def models(self) -> dict[str, mph.Model]:
        with self._start_lock:
            return self._models.copy()

    @staticmethod
    def _client_status_snapshot(client) -> dict:
        def read(name: str, default):
            try:
                return getattr(client, name)
            except Exception:
                return default

        return {
            "version": read("version", None),
            "cores": read("cores", None),
            "standalone": bool(read("standalone", False)),
        }

    def _publish_control_plane_status_locked(self) -> None:
        set_session_status(
            connected=self._client is not None,
            starting=self._starting,
        )

    def _startup_path(self) -> Path:
        runtime_dir = getattr(self._ownership, "runtime_dir", None)
        if runtime_dir is not None:
            return Path(runtime_dir) / "session" / "startup-state.json"
        return self._startup_receipt_path

    def _write_startup_record_locked(self) -> None:
        if self._startup_record is None:
            return
        payload = {
            key: value for key, value in self._startup_record.items() if key != "started_monotonic"
        }
        atomic_write_json(self._startup_path(), payload)

    def _record_startup_phase_locked(
        self,
        phase: str,
        *,
        state: Optional[str] = None,
        terminal: Optional[bool] = None,
        details: Optional[dict] = None,
    ) -> None:
        record = self._startup_record
        if record is None:
            return
        now_epoch = time.time()
        now_monotonic = time.monotonic()
        phase_record = {
            "phase": phase,
            "timestamp_epoch": now_epoch,
            "elapsed_seconds": round(now_monotonic - float(record["started_monotonic"]), 6),
        }
        if details:
            phase_record["details"] = details
        phases = record["phases"]
        phases.append(phase_record)
        if len(phases) > MAX_STARTUP_PHASES:
            del phases[: len(phases) - MAX_STARTUP_PHASES]
        record["updated_at_epoch"] = now_epoch
        if state is not None:
            record["state"] = state
        if terminal is not None:
            record["terminal"] = terminal
        record["starting"] = self._starting
        record["connected"] = self._client is not None
        record["cleanup_pending"] = self._start_cleanup_pending
        record["owns_solver_lease"] = self._owns_solver_lease
        self._publish_control_plane_status_locked()
        self._write_startup_record_locked()

    def _startup_summary_locked(self) -> Optional[dict]:
        if self._startup_record is None:
            return None
        record = self._startup_record
        return deepcopy({key: value for key, value in record.items() if key != "started_monotonic"})

    def _begin_startup_record_locked(
        self,
        *,
        attempt_id: str,
        kwargs: dict,
    ) -> None:
        now_epoch = time.time()
        self._startup_record = {
            "schema_name": STARTUP_STATE_SCHEMA,
            "schema_version": STARTUP_STATE_VERSION,
            "attempt_id": attempt_id,
            "started_at_epoch": now_epoch,
            "started_monotonic": time.monotonic(),
            "updated_at_epoch": now_epoch,
            "state": "request_accepted",
            "terminal": False,
            "starting": True,
            "connected": False,
            "cleanup_pending": False,
            "owns_solver_lease": False,
            "lease_acquisition_id": None,
            "requested": {
                "cores": kwargs.get("cores"),
                "version": kwargs.get("version"),
            },
            "response_grace_seconds": STARTUP_RESPONSE_GRACE_SECONDS,
            "startup_timeout_seconds": STARTUP_TIMEOUT_SECONDS,
            "phases": [],
        }
        self._record_startup_phase_locked(
            "request_accepted",
            state="request_accepted",
        )

    def _mark_start_timeout(self, attempt_id: str) -> None:
        with self._start_lock:
            if attempt_id != self._start_attempt_id or not self._starting:
                return
            self._start_timed_out = True
            self._start_cancel_requested = True
            self._start_cleanup_pending = True
            self._starting = False
            self._start_error = (
                f"COMSOL startup exceeded {STARTUP_TIMEOUT_SECONDS:.0f} seconds. "
                "The result is terminal, but the owned lease remains held until "
                "the blocking MPh call returns and cleanup is verified."
            )
            self._start_message = self._start_error
            self._record_startup_phase_locked(
                "startup_timeout",
                state="timed_out_cleanup_pending",
                terminal=True,
            )

    @staticmethod
    def _clear_client_models(client) -> list[str]:
        errors = []
        try:
            client.clear()
        except Exception as exc:
            errors.append(f"client_clear:{type(exc).__name__}:{exc}")
        return errors

    def _retire_client(self, client, *, clear_models: bool = True) -> tuple[bool, list[str]]:
        """Deactivate one exact client without constructing a second JVM client."""
        with self._start_lock:
            self._reusable_client = None
            self._reusable_client_kind = None
        errors = self._clear_client_models(client) if clear_models else []
        reusable = bool(getattr(client, "standalone", False))
        if reusable:
            with self._start_lock:
                self._reusable_client = client
                self._reusable_client_kind = "standalone"
        else:
            try:
                client.disconnect()
            except Exception as exc:
                errors.append(f"client_disconnect:{type(exc).__name__}:{exc}")
            if not errors:
                with self._start_lock:
                    self._reusable_client = client
                    self._reusable_client_kind = "remote"
        return reusable or not errors, errors

    def _rollback_remote_activation(self, client, *, error: str) -> dict:
        reusable, cleanup_errors = self._retire_client(client, clear_models=False)
        if cleanup_errors:
            with self._start_lock:
                self._client = client
                self._client_status = self._client_status_snapshot(client)
                self._client_status_client = client
                self._publish_control_plane_status_locked()
            return {
                "success": False,
                "cleanup_pending": True,
                "connected_uncertain": True,
                "error": error,
                "cleanup_errors": cleanup_errors,
            }

        with self._start_lock:
            self._client = None
            self._client_status = None
            self._client_status_client = None
            self._publish_control_plane_status_locked()
        release = self._release_owned_lease()
        result = {
            "success": False,
            "client_reusable": reusable,
            "error": error,
        }
        if release is not None:
            result["lease_release"] = release
            if not release.get("success"):
                result["cleanup_pending"] = True
        return result

    def _publish_remote_client(
        self,
        client,
        *,
        port: int,
        host: str,
        message: Optional[str] = None,
    ) -> dict:
        try:
            if self._ownership.heartbeat(refresh_server_processes=True) is not True:
                raise RuntimeError("solver ownership heartbeat could not be verified")
            version = client.version
        except Exception as exc:
            return self._rollback_remote_activation(client, error=str(exc))

        with self._start_lock:
            self._client = client
            self._client_status = self._client_status_snapshot(client)
            self._client_status_client = client
            if self._reusable_client is client:
                self._reusable_client = None
                self._reusable_client_kind = None
            self._publish_control_plane_status_locked()
        result = {
            "success": True,
            "version": version,
            "port": port,
            "host": host,
        }
        if message is not None:
            result["message"] = message
        return result

    def _release_owned_lease(self) -> Optional[dict]:
        with self._start_lock:
            if not self._owns_solver_lease:
                return None
            result = self._ownership.release()
            if result.get("success"):
                self._owns_solver_lease = False
            return result

    def start(
        self,
        cores: Optional[int] = None,
        version: Optional[str] = None,
        products: Optional[list[str]] = None,
    ) -> dict:
        """Start a COMSOL client session (non-blocking)."""
        with self._start_lock:
            if self._client is not None:
                return {
                    "success": True,
                    "connected": True,
                    "version": self._client.version,
                    "cores": self._client.cores,
                    "standalone": self._client.standalone,
                    "message": "COMSOL session is already connected; no action taken.",
                }
            # A background start is in flight — tell caller to poll status.
            if self._starting:
                return {
                    "success": True,
                    "starting": True,
                    "message": self._start_message
                    or "COMSOL is still starting. Poll comsol_status.",
                }
            if self._start_cleanup_pending:
                return {
                    "success": False,
                    "starting": False,
                    "cleanup_pending": True,
                    "reset_required": True,
                    "error": self._start_error,
                    "message": (
                        "The previous COMSOL start reached a terminal timeout, "
                        "but its blocking MPh call has not returned. A second "
                        "client is forbidden."
                    ),
                }
            if self._start_error:
                return {
                    "success": False,
                    "starting": False,
                    "reset_required": True,
                    "error": self._start_error,
                    "message": (
                        "The previous COMSOL start failed. Call session_reset before retrying."
                    ),
                }
            if self._host_restart_required:
                return {
                    "success": False,
                    "starting": False,
                    "host_restart_required": True,
                    "error": (
                        "The process JVM started without a reusable MPh client. "
                        "Restart the MCP host before another COMSOL start."
                    ),
                }
            if self._reusable_client is not None and self._reusable_client_kind != "standalone":
                return {
                    "success": False,
                    "starting": False,
                    "host_restart_required": True,
                    "error": (
                        "The process-global MPh client was created for a remote "
                        "topology and cannot become a local stand-alone client."
                    ),
                }
            if isinstance(cores, bool) or not isinstance(cores, int) or cores < 1:
                return {
                    "success": False,
                    "starting": False,
                    "error": {
                        "code": "cores_required",
                        "message": (
                            "cores must be an explicit positive integer declared by the caller"
                        ),
                    },
                }
            available_cores = os.cpu_count()
            if not isinstance(available_cores, int) or available_cores < 1:
                return {
                    "success": False,
                    "starting": False,
                    "error": {
                        "code": "cpu_capacity_unavailable",
                        "message": (
                            "the host CPU capacity could not be measured; refusing solver start"
                        ),
                    },
                }
            if cores > available_cores:
                return {
                    "success": False,
                    "starting": False,
                    "error": {
                        "code": "cores_exceed_host_capacity",
                        "message": (
                            "caller-declared cores exceed the currently measured host capacity"
                        ),
                        "requested": cores,
                        "available": available_cores,
                    },
                }
            if not isinstance(version, str) or not version.strip():
                return {
                    "success": False,
                    "starting": False,
                    "error": {
                        "code": "version_required",
                        "message": "version must be explicitly declared by the caller",
                    },
                }
            # Claim the starting flag for this call.
            self._starting = True
            self._start_error = None
            self._start_message = "Starting COMSOL client in background..."
            self._start_cancel_requested = False
            self._start_cleanup_pending = False
            self._start_timed_out = False
            self._host_restart_required = False

            attempt_id = uuid.uuid4().hex
            self._start_attempt_id = attempt_id

            # MPh 1.3.1 Client accepts cores/version/port/host only. COMSOL
            # checks out licensed products when a physics interface is used.
            kwargs = {"cores": cores, "version": version}
            kwargs = {key: value for key, value in kwargs.items() if value is not None}
            try:
                self._begin_startup_record_locked(
                    attempt_id=attempt_id,
                    kwargs=kwargs,
                )
                self._record_startup_phase_locked(
                    "response_ready",
                    state="response_grace",
                )
            except Exception as exc:
                release = self._release_owned_lease()
                self._starting = False
                self._start_error = f"Cannot persist COMSOL startup state: {exc}"
                self._start_message = self._start_error
                self._publish_control_plane_status_locked()
                return {
                    "success": False,
                    "starting": False,
                    "error": self._start_error,
                    "lease_release": release,
                }

        # A Timer is deliberately used instead of launching the worker
        # immediately. In a real cold start JPype can monopolize the response
        # path before the MCP transport has serialized the accepted response.
        self._start_thread = threading.Timer(
            STARTUP_RESPONSE_GRACE_SECONDS,
            function=self._start_worker,
            args=(attempt_id, kwargs),
        )
        self._start_thread.name = "comsol-start"
        self._start_thread.daemon = True
        self._start_thread.start()

        self._start_watchdog = threading.Timer(
            max(0.0, STARTUP_TIMEOUT_SECONDS - STARTUP_RESPONSE_GRACE_SECONDS),
            self._mark_start_timeout,
            args=(attempt_id,),
        )
        self._start_watchdog.name = "comsol-start-watchdog"
        self._start_watchdog.daemon = True
        self._start_watchdog.start()

        result = {
            "success": True,
            "starting": True,
            "message": (
                "COMSOL is starting in the background (JVM + back-end). "
                "This takes 30-90s. Poll comsol_status until 'connected' is true; "
                "do NOT retry comsol_start."
            ),
        }
        if products:
            result["warning"] = (
                "MPh 1.3.1 does not accept a products argument; COMSOL will "
                "load and license requested physics products on demand."
            )
        return result

    def _start_worker(self, attempt_id: str, kwargs: dict) -> None:
        """Runs mph.Client() in a daemon thread. Sets _client on success."""
        mph_module = None
        mph_session_module = None
        client = None
        release_result = None
        client_reusable = False
        cleanup_errors: list[str] = []
        client_retirement_attempted = False
        try:
            with self._start_lock:
                if attempt_id != self._start_attempt_id:
                    return
                self._record_startup_phase_locked(
                    "worker_entered",
                    state="preflight_running",
                )
                client = self._reusable_client

            preflight = self._ownership.preflight(
                session_state=self.get_status(),
                requested_version=kwargs.get("version"),
            )
            if not preflight.get("ready"):
                blockers = preflight.get("blockers") or ["unknown preflight refusal"]
                raise RuntimeError("COMSOL start preflight failed: " + "; ".join(blockers[:8]))
            with self._start_lock:
                if self._start_cancel_requested:
                    self._start_cleanup_pending = True
                else:
                    self._record_startup_phase_locked(
                        "preflight_completed",
                        state="acquiring_lease",
                        details={
                            "inventory_state": preflight.get("ownership", {})
                            .get("process_inventory", {})
                            .get("state"),
                            "warning_count": len(preflight.get("warnings") or []),
                        },
                    )
            if self._start_cancel_requested:
                return

            claim = self._ownership.acquire(mode="local-client")
            if not claim.get("success"):
                raise RuntimeError(
                    str(claim.get("error") or "COMSOL solver lease acquisition failed")
                )
            with self._start_lock:
                self._owns_solver_lease = True
                if self._startup_record is not None:
                    self._startup_record["lease_acquisition_id"] = (claim.get("lease") or {}).get(
                        "acquisition_id"
                    )
                self._record_startup_phase_locked(
                    "lease_acquired",
                    state="loading_mph",
                )
                if self._start_cancel_requested:
                    self._start_cleanup_pending = True
            if self._start_cancel_requested:
                return

            mph_module, mph_session_module = _load_mph()
            with self._start_lock:
                cancelled = self._start_cancel_requested
                if cancelled:
                    self._start_cleanup_pending = True
                else:
                    self._record_startup_phase_locked(
                        "mph_loaded",
                        state="initializing_client",
                        details={"reusing_process_client": client is not None},
                    )
            if cancelled:
                return

            if client is None:
                try:
                    client = mph_session_module.client
                except Exception:
                    client = None
            if client is None:
                with self._start_lock:
                    cancelled = self._start_cancel_requested
                    if cancelled:
                        self._start_cleanup_pending = True
                    else:
                        self._record_startup_phase_locked(
                            "client_initialization_started",
                            state="initializing_client",
                        )
                if cancelled:
                    return
                client = mph_module.Client(**kwargs)

            with self._start_lock:
                if self._start_cancel_requested:
                    self._start_message = "Start cancelled; releasing client."
                else:
                    self._client = client
                    self._client_status = self._client_status_snapshot(client)
                    self._client_status_client = client
                    self._reusable_client = None
                    self._reusable_client_kind = None
                    self._start_message = "Client ready."
                    if self._ownership.heartbeat(refresh_server_processes=True) is not True:
                        raise RuntimeError("solver ownership heartbeat could not be verified")
                    self._starting = False
                    self._record_startup_phase_locked(
                        "connected",
                        state="connected",
                        terminal=True,
                    )
        except Exception as e:
            if client is not None:
                client_retirement_attempted = True
                client_reusable, cleanup_errors = self._retire_client(client)
            jvm_started_without_client = False
            if client is None and mph_module is not None:
                try:
                    import jpype

                    jvm_started_without_client = bool(jpype.isJVMStarted())
                except Exception:
                    jvm_started_without_client = True
            with self._start_lock:
                if cleanup_errors:
                    # Retirement failed, so the client may still be alive;
                    # surface the uncertainty instead of claiming a clean state.
                    self._client = client
                    self._client_status = (
                        self._client_status_snapshot(client) if client is not None else None
                    )
                    self._client_status_client = client
                    self._start_cleanup_pending = True
                else:
                    self._client = None
                    self._client_status = None
                    self._client_status_client = None
                    self._start_cleanup_pending = False
                self._host_restart_required = jvm_started_without_client
                self._start_error = str(e)
                self._start_message = f"Start failed: {e}"
                if not cleanup_errors:
                    self._start_cleanup_pending = False
                self._starting = False
                if mph_session_module is not None and not cleanup_errors:
                    try:
                        # Clearing MPh's process-global singleton must reach
                        # the real mph.session module: a proxy-scoped write
                        # would leave the real global holding a dead client.
                        session_global = (
                            mph_session_module._get()
                            if isinstance(mph_session_module, _LazyMphModule)
                            else mph_session_module
                        )
                        if session_global.client is client:
                            session_global.client = None
                    except Exception as singleton_exc:
                        self._start_message = (
                            f"{self._start_message}; singleton cleanup warning: "
                            f"{type(singleton_exc).__name__}"
                        )
                if not cleanup_errors:
                    release_result = self._release_owned_lease()
                self._record_startup_phase_locked(
                    "start_failed",
                    state="failed",
                    terminal=True,
                    details={
                        "error_type": type(e).__name__,
                        "error": str(e)[:512],
                        "client_reusable": client_reusable,
                        "cleanup_errors": cleanup_errors,
                        "host_restart_required": self._host_restart_required,
                        "lease_release_success": bool(
                            release_result and release_result.get("success")
                        ),
                    },
                )
        finally:
            with self._start_lock:
                cancelled = self._start_cancel_requested
                timed_out = self._start_timed_out
                if not self._start_cleanup_pending:
                    self._starting = False
                self._start_cancel_requested = False
                watchdog = self._start_watchdog
                self._start_watchdog = None
            if watchdog is not None:
                watchdog.cancel()

            if cancelled and client is not None and mph_session_module is not None:
                if not client_retirement_attempted:
                    client_retirement_attempted = True
                    client_reusable, cleanup_errors = self._retire_client(client)
                with self._start_lock:
                    if cleanup_errors:
                        self._client = client
                        self._client_status = self._client_status_snapshot(client)
                        self._client_status_client = client
                        self._start_cleanup_pending = True
                    else:
                        self._client = None
                        self._client_status = None
                        self._client_status_client = None
                        if not client_reusable:
                            self._reusable_client = None
                    if not cleanup_errors and release_result is None:
                        release_result = self._release_owned_lease()
                    self._start_cleanup_pending = bool(cleanup_errors) or self._owns_solver_lease
                    self._starting = False
                    state = (
                        "cleanup_pending"
                        if self._start_cleanup_pending
                        else ("timed_out" if timed_out else "cancelled")
                    )
                    self._record_startup_phase_locked(
                        "cleanup_pending" if self._start_cleanup_pending else "cleanup_completed",
                        state=state,
                        terminal=True,
                        details={
                            "client_reusable": client_reusable,
                            "cleanup_errors": cleanup_errors,
                            "lease_release_success": bool(
                                release_result and release_result.get("success")
                            ),
                        },
                    )
            elif cancelled and release_result is None:
                with self._start_lock:
                    release_result = self._release_owned_lease()
                    self._start_cleanup_pending = self._owns_solver_lease
                    self._starting = False
                    self._record_startup_phase_locked(
                        "cleanup_pending" if self._start_cleanup_pending else "cleanup_completed",
                        state=(
                            "cleanup_pending"
                            if self._start_cleanup_pending
                            else ("timed_out" if timed_out else "cancelled")
                        ),
                        terminal=True,
                        details={
                            "client_reusable": False,
                            "cleanup_errors": [],
                            "lease_release_success": bool(
                                release_result and release_result.get("success")
                            ),
                        },
                    )

    def connect(self, port: int, host: str = "localhost") -> dict:
        """Serialize remote activation against local start and disconnect."""
        with self._start_lock:
            return self._connect_locked(port, host)

    def _connect_locked(self, port: int, host: str) -> dict:
        """Connect to a remote COMSOL server."""
        with self._start_lock:
            if self._starting:
                return {
                    "success": False,
                    "error": (
                        "A local COMSOL client is still starting. Poll "
                        "comsol_status before connecting to another server."
                    ),
                }
            if self._start_cleanup_pending:
                return {
                    "success": False,
                    "cleanup_pending": True,
                    "error": (
                        "A timed-out local COMSOL start still owns cleanup. Wait "
                        "for cleanup to finish before connecting to another server."
                    ),
                }
        mph, mph_session = _load_mph()
        if self._client is not None:
            return {"success": False, "error": "COMSOL session already running. Disconnect first."}
        if self._reusable_client is not None and self._reusable_client_kind == "standalone":
            return {
                "success": False,
                "host_restart_required": True,
                "error": (
                    "The process-global MPh client is stand-alone and cannot "
                    "be converted into a remote client."
                ),
            }
        preflight = self._ownership.preflight(session_state=self.get_status())
        if not preflight["ready"]:
            return {
                "success": False,
                "error": "COMSOL connection preflight failed.",
                "preflight": preflight,
            }
        claim = self._ownership.acquire(mode="remote-connect")
        if not claim["success"]:
            return {"success": False, "error": claim["error"], "ownership": claim.get("status")}
        self._owns_solver_lease = True
        if self._reusable_client is not None and self._reusable_client_kind == "remote":
            try:
                candidate = self._reusable_client
                candidate.connect(port, host)
            except Exception as exc:
                return self._rollback_remote_activation(candidate, error=str(exc))
            return self._publish_remote_client(
                candidate,
                port=port,
                host=host,
                message="Reused the process-global MPh client.",
            )
        if mph_session.client is not None:
            candidate = mph_session.client
            if getattr(candidate, "standalone", False):
                release = self._release_owned_lease()
                return {
                    "success": False,
                    "host_restart_required": True,
                    "error": (
                        "Existing process-global MPh client is stand-alone; "
                        "restart the MCP host before remote connection."
                    ),
                    "lease_release": release,
                }
            try:
                candidate_port = getattr(candidate, "port", None)
                candidate_host = getattr(candidate, "host", None)
                if candidate_port is None:
                    candidate.connect(port, host)
                elif (
                    int(candidate_port) != int(port)
                    or str(candidate_host).casefold() != str(host).casefold()
                ):
                    return self._rollback_remote_activation(
                        candidate,
                        error="Existing process-global MPh client endpoint does not match.",
                    )
            except Exception as exc:
                return self._rollback_remote_activation(candidate, error=str(exc))
            return self._publish_remote_client(
                candidate,
                port=port,
                host=host,
                message="Reused existing client from MPh session.",
            )
        candidate = None
        try:
            candidate = mph.Client(port=port, host=host)
        except Exception as e:
            self._release_owned_lease()
            return {"success": False, "error": str(e)}
        return self._publish_remote_client(candidate, port=port, host=host)

    def disconnect(self) -> dict:
        """Disconnect and clear the session."""
        # A blocking mph.Client() construction cannot be interrupted safely.
        # Mark it for disposal as soon as the worker receives the client.
        with self._start_lock:
            if self._client is None and self._starting:
                self._start_cancel_requested = True
                self._start_message = (
                    "Cancellation requested; waiting for COMSOL startup to return."
                )
                return {
                    "success": True,
                    "starting": True,
                    "message": self._start_message,
                }
            if self._client is None and self._start_cleanup_pending:
                self._start_cancel_requested = True
                return {
                    "success": False,
                    "starting": False,
                    "cleanup_pending": True,
                    "error": self._start_error,
                    "message": (
                        "Startup already reached a terminal timeout. The owned "
                        "lease remains held until the blocking MPh call returns."
                    ),
                }
            self._start_error = None
            self._start_message = ""
            if self._client is None:
                release = self._release_owned_lease()
                self._publish_control_plane_status_locked()
                result = {"success": True, "message": "No active session."}
                if release is not None:
                    result["lease_release"] = release
                return result
            client = self._client

        reusable, cleanup_errors = self._retire_client(client)
        if cleanup_errors:
            return {
                "success": False,
                "cleanup_pending": True,
                "connected_uncertain": True,
                "cleanup_errors": cleanup_errors,
                "message": (
                    "Client retirement was not verified; the exact client and "
                    "solver lease remain retained for a cleanup retry."
                ),
            }
        with self._start_lock:
            self._client = None
            self._client_status = None
            self._client_status_client = None
            for name in list(self._model_cleanup_paths):
                self._cleanup_model_artifact(name)
            for name in list(self._models):
                self._notify_model_removed_locked(name)
            self._models.clear()
            self._model_paths.clear()
            self._model_source_identities.clear()
            self._model_revisions.clear()
            self._model_java_tags.clear()
            self._current_model = None
            self._publish_control_plane_status_locked()
        release = self._release_owned_lease()
        lease_release_success = bool(release is None or release.get("success"))
        with self._start_lock:
            if self._startup_record is not None:
                self._record_startup_phase_locked(
                    "session_deactivated",
                    state="deactivated",
                    terminal=True,
                    details={
                        "client_reusable": reusable,
                        "lease_release_success": lease_release_success,
                    },
                )
        result = {
            # A failed owned-lease release must not report a deactivated
            # session: the solver lease is still held and would poison the
            # next start attempt.
            "success": not cleanup_errors and lease_release_success,
            "client_reusable": reusable,
            "message": (
                "Session deactivated and models cleared. The process-global "
                "MPh client was retained for safe same-host reuse."
                if reusable
                else "Session disconnected and models cleared."
            ),
        }
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors
        if not lease_release_success:
            result["lease_release_pending"] = True
        if release is not None:
            result["lease_release"] = release
        return result

    def get_status(self) -> dict:
        """Get current session status."""
        with self._start_lock:
            connected = self._client is not None
            starting = self._starting
            startup = self._startup_summary_locked()
            cleanup_pending = self._start_cleanup_pending
            owns_solver_lease = self._owns_solver_lease
            host_restart_required = self._host_restart_required
            start_error = self._start_error
            start_message = self._start_message
            current_model = self._current_model
            client_status = None
            model_list = []
            if connected:
                if self._client_status_client is not self._client:
                    self._client_status = self._client_status_snapshot(self._client)
                    self._client_status_client = self._client
                client_status = dict(self._client_status)
                for name in self._models:
                    # B10: label, tracking key, Java tag, and file identity
                    # are distinct fields; equal labels never prove equal tags.
                    model_info = {
                        "name": name,
                        "label": name,
                        "tracking_key": name,
                        "java_tag": self._model_java_tags.get(name),
                        "identity_relation": (
                            "session_model" if name in self._model_java_tags else "label_only"
                        ),
                    }
                    model_path = self._model_paths.get(name)
                    if model_path is not None:
                        model_info["file"] = model_path
                    source_identity = self._model_source_identities.get(name)
                    if source_identity is not None:
                        model_info["loaded_source_identity"] = dict(source_identity)
                    revision = self._model_revisions.get(name)
                    if revision is None:
                        revision = self._initialize_model_revision(
                            name,
                            self._model_paths.get(name),
                        )
                    model_info["revision_sha256"] = revision["revision_sha256"]
                    model_info["revision_sequence"] = revision["sequence"]
                    model_list.append(model_info)
            self._publish_control_plane_status_locked()
        # Background start in flight and not yet ready.
        if not connected and starting:
            result = {
                "connected": False,
                "starting": True,
                "cleanup_pending": cleanup_pending,
                "owns_solver_lease": owns_solver_lease,
                "host_restart_required": host_restart_required,
                "message": start_message or "COMSOL is starting in background. Poll again shortly.",
            }
            if startup is not None:
                result["startup"] = startup
            return result
        # Previous background start failed.
        if not connected and start_error:
            result = {
                "connected": False,
                "starting": False,
                "cleanup_pending": cleanup_pending,
                "owns_solver_lease": owns_solver_lease,
                "host_restart_required": host_restart_required,
                "error": start_error,
                "message": start_message,
            }
            if startup is not None:
                result["startup"] = startup
            return result
        if not connected:
            result = {
                "connected": False,
                "starting": False,
                "cleanup_pending": False,
                "owns_solver_lease": owns_solver_lease,
                "host_restart_required": host_restart_required,
                "message": "No active COMSOL session.",
            }
            if startup is not None:
                result["startup"] = startup
            return result

        # The connected response uses only the lock-bound local snapshot above;
        # it never dereferences an MPh client or model after releasing the lock.
        if client_status is None:
            raise RuntimeError("connected session metadata snapshot is unavailable")
        result = {
            "connected": True,
            "starting": False,
            # A failed start retirement can retain a live client; surface the
            # pending cleanup instead of claiming an unconditionally clean
            # connected state.
            "cleanup_pending": cleanup_pending,
            "owns_solver_lease": owns_solver_lease,
            "host_restart_required": host_restart_required,
            **client_status,
            "models": model_list,
            "current_model": current_model,
        }
        if startup is not None:
            result["startup"] = startup
        return result

    def clear_models(self) -> dict:
        """Remove every tracked model while preserving the connected client."""
        with self._start_lock:
            if self._starting:
                return {
                    "success": False,
                    "error": "Cannot clear models while COMSOL is starting.",
                }

        with self._start_lock:
            names = list(self._models)
        failed = []
        for name in names:
            if not self.remove_model(name):
                failed.append(name)

        if failed:
            return {
                "success": False,
                "removed": len(names) - len(failed),
                "failed_models": failed,
                "message": "Some tracked models could not be removed.",
            }
        return {
            "success": True,
            "removed": len(names),
            "connected": self.is_connected,
            "message": "All tracked models were removed; the client was preserved.",
        }

    def reset(self) -> dict:
        """Explicitly destroy or cancel the current client lifecycle."""
        result = self.disconnect()
        return {
            **result,
            "reset": True,
            "message": (
                "Session reset requested. All tracked models are cleared and the "
                "owned client is disconnected or discarded after startup returns."
            ),
        }

    @staticmethod
    def _hash_model_source(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def add_model(
        self,
        model: mph.Model,
        cleanup_path: Optional[str] = None,
        *,
        source_identity: Optional[dict] = None,
    ) -> str:
        """Add a model to tracking.

        B10: label (``model.name()``), tracking key, Java tag, and file
        identity are cached as separate fields. Equal labels never prove
        equal native tags or equal files.
        """
        name = model.name()
        java_tag = None
        try:
            # Prefer the native tag when MPh/ClientAPI exposes it.
            if hasattr(model, "tag"):
                tag_value = model.tag()
                if isinstance(tag_value, str) and tag_value.strip():
                    java_tag = tag_value.strip()
        except Exception:
            java_tag = None
        try:
            model_path = model.file() if hasattr(model, "file") else None
        except Exception:
            model_path = None
        captured_identity = None
        if source_identity is not None:
            source_path = Path(str(source_identity.get("source_path", ""))).resolve()
            source_sha256 = str(source_identity.get("source_sha256", "")).lower()
            if model_path is None or source_path != Path(str(model_path)).resolve():
                raise ValueError("source identity path does not match the registered model path")
            if len(source_sha256) != 64 or any(
                value not in "0123456789abcdef" for value in source_sha256
            ):
                raise ValueError("source identity SHA-256 is invalid")
            captured_identity = {
                "source_path": str(source_path),
                "source_sha256": source_sha256,
                "capture": str(source_identity.get("capture") or "caller_provided"),
            }
        elif model_path is not None:
            source_path = Path(str(model_path)).resolve()
            try:
                if source_path.is_file():
                    captured_identity = {
                        "source_path": str(source_path),
                        "source_sha256": self._hash_model_source(source_path),
                        "capture": "model_registration",
                    }
            except OSError:
                captured_identity = None
        with self._start_lock:
            if name in self._models:
                raise ValueError(f"A model named {name!r} is already registered")
            self._models[name] = model
            if java_tag is not None:
                self._model_java_tags[name] = java_tag
            if cleanup_path:
                self._model_cleanup_paths[name] = str(cleanup_path)
            if self._current_model is None:
                self._current_model = name
            if model_path is not None:
                self._model_paths[name] = str(model_path)
            if captured_identity is not None:
                self._model_source_identities[name] = captured_identity
            self._initialize_model_revision(name, self._model_paths.get(name))
        try:
            self._ownership.heartbeat(model_path=str(model_path) if model_path else None)
        except Exception:  # noqa: S110 - a failed heartbeat must not break activation
            pass
        return name

    def add_model_removal_listener(self, listener) -> None:
        """Register an idempotent in-process callback for model retirement."""
        with self._start_lock:
            if listener not in self._model_removal_listeners:
                self._model_removal_listeners.append(listener)

    def _notify_model_removed_locked(self, name: str) -> None:
        for listener in tuple(self._model_removal_listeners):
            listener(name)

    @staticmethod
    def _revision_hash(body: dict) -> str:
        canonical = json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _initialize_model_revision(self, name: str, source_path: Optional[str]) -> dict:
        with self._start_lock:
            existing = self._model_revisions.get(name)
            if existing is not None:
                return dict(existing)
            body = {
                "model_name": name,
                "sequence": 0,
                "previous_revision_sha256": None,
                "operation": "model_registered",
                "source_path_present": source_path is not None,
            }
            revision = {**body, "revision_sha256": self._revision_hash(body)}
            self._model_revisions[name] = revision
            return dict(revision)

    def get_model_revision(self, name: Optional[str] = None) -> Optional[dict]:
        """Return the local optimistic-concurrency token without clientapi calls."""
        with self._start_lock:
            model_name = name or self._current_model
            if model_name is None or model_name not in self._models:
                return None
            return dict(
                self._model_revisions.get(model_name)
                or self._initialize_model_revision(
                    model_name,
                    self._model_paths.get(model_name),
                )
            )

    def get_model_source_identity(self, name: Optional[str] = None) -> Optional[dict]:
        """Return the source identity captured when the model entered the session."""
        with self._start_lock:
            model_name = name or self._current_model
            if model_name is None or model_name not in self._models:
                return None
            identity = self._model_source_identities.get(model_name)
            return dict(identity) if identity is not None else None

    def advance_model_revision(self, name: str, operation: str) -> dict:
        """Advance one model token after a serialized successful mutation."""
        with self._start_lock:
            current = self.get_model_revision(name)
            if current is None:
                raise ValueError(f"Model revision unavailable: {name}")
            body = {
                "model_name": name,
                "sequence": int(current["sequence"]) + 1,
                "previous_revision_sha256": current["revision_sha256"],
                "operation": operation,
                "source_path_present": self._model_paths.get(name) is not None,
            }
            revision = {**body, "revision_sha256": self._revision_hash(body)}
            self._model_revisions[name] = revision
            return dict(revision)

    def preflight_long_operation(
        self, *, model_path: Optional[str] = None, output_path: Optional[str] = None
    ) -> dict:
        """Require the connected session to own the solver before long work."""
        result = self._ownership.preflight(
            session_state=self.get_status(),
            model_path=model_path,
            output_path=output_path,
        )
        lease = result["ownership"]["lease"]
        if self._client is None:
            result["blockers"].append("no connected COMSOL session")
        if lease.get("state") != "active" or not lease.get("owned_by_current_process", False):
            result["blockers"].append("current MCP process does not own the solver lease")
        result["blockers"] = list(dict.fromkeys(result["blockers"]))
        result["ready"] = not result["blockers"]
        result["success"] = result["ready"]
        return result

    def _cleanup_model_artifact(self, name: str) -> None:
        """Remove a tracked clone backing file after COMSOL releases it."""
        with self._start_lock:
            cleanup_path = self._model_cleanup_paths.pop(name, None)
        if not cleanup_path:
            return
        path = Path(cleanup_path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return
        try:
            if path.parent.name.startswith("comsol_mcp_clone_"):
                path.parent.rmdir()
        except OSError:
            pass

    def get_model(self, name: Optional[str] = None) -> Optional[mph.Model]:
        """Get a model by name or current model."""
        with self._start_lock:
            if name is None:
                name = self._current_model
            return self._models.get(name)

    def set_current_model(self, name: str) -> bool:
        """Set the current active model."""
        with self._start_lock:
            if name in self._models:
                self._current_model = name
                return True
            return False

    def remove_model(self, name: str) -> bool:
        """Remove a model from tracking and client."""
        with self._start_lock:
            client = self._client
            model = self._models.get(name)
        if model is not None and client is not None:
            try:
                client.remove(model)
                with self._start_lock:
                    if self._client is not client or self._models.get(name) is not model:
                        return False
                    del self._models[name]
                    self._notify_model_removed_locked(name)
                    self._model_paths.pop(name, None)
                    self._model_source_identities.pop(name, None)
                    self._model_revisions.pop(name, None)
                    self._model_java_tags.pop(name, None)
                    self._cleanup_model_artifact(name)
                    if self._current_model == name:
                        self._current_model = next(iter(self._models.keys()), None)
                return True
            except Exception:  # noqa: S110 - best-effort cleanup keeps close() truthful
                pass
        return False


session_manager = SessionManager()


def register_session_tools(mcp: MCPServer) -> None:
    """Register session management tools with the MCP server."""

    @mcp.tool()
    def comsol_start(
        cores: Optional[int] = None,
        version: Optional[str] = None,
        products: Optional[list[str]] = None,
    ) -> dict:
        """
        Start a local COMSOL client session.

        Non-blocking: returns ``{"starting": True}`` before a daemon thread
        performs solver preflight, imports MPh, and runs ``mph.Client()``.
        Poll ``comsol_status`` until ``connected`` is true before calling any
        other COMSOL tool. Status includes durable startup phases and a
        terminal timeout. Do NOT retry ``comsol_start`` while a start is in
        flight; the second call reports the existing attempt.

        Args:
            cores: Number of processor cores to use; the caller must declare it
                   explicitly and it is checked against live host capacity.
            version: COMSOL version to use; the caller must declare it explicitly.
            products: Compatibility hint only. MPh 1.3.1 cannot preload a
                     product list; COMSOL checks out licensed products on demand.

        Returns:
            Session info including version and core count, or error message
        """
        return session_manager.start(cores=cores, version=version, products=products)

    @mcp.tool()
    def comsol_connect(port: int, host: str = "localhost") -> dict:
        """
        Connect to a remote COMSOL server.

        Args:
            port: Port number the COMSOL server is listening on
            host: Server hostname or IP address (default: 'localhost')

        Returns:
            Connection info or error message
        """
        return session_manager.connect(port=port, host=host)

    @mcp.tool()
    def comsol_disconnect() -> dict:
        """
        Deactivate COMSOL and clear all models from memory.

        A stand-alone MPh client owns the process-global JVM and is retained
        for exact same-host reuse after its solver lease is released.

        Returns:
            Success status and message
        """
        return session_manager.disconnect()

    @mcp.tool()
    def comsol_status() -> dict:
        """
        Get the current COMSOL session status.

        Returns:
            Session information including connection status, version, and loaded models
        """
        return session_manager.get_status()

    @mcp.tool()
    def session_clear_models() -> dict:
        """
        Destructively remove all models tracked by this MCP session.

        The COMSOL client remains connected. Use this only when loss of all
        unsaved tracked models is intended.
        """
        return session_manager.clear_models()

    @mcp.tool()
    def session_reset() -> dict:
        """
        Destructively reset the MCP-owned COMSOL session.

        This clears all tracked models and deactivates the owned client. The
        exact stand-alone wrapper remains reusable because MPh forbids a second
        client in the same Python process. If a local client is still starting,
        it is marked for disposal when startup returns.
        """
        return session_manager.reset()
