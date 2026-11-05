"""Crash-durable job artifacts, state transitions, and process-safe locking."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from weakref import WeakValueDictionary

import psutil

from comsol_mcp.durable import (
    atomic_write_bytes as _durable_atomic_write_bytes,
)
from comsol_mcp.durable import (
    atomic_write_json as _durable_atomic_write_json,
)
from comsol_mcp.durable import (
    fsync_directory as _fsync_directory,
)
from comsol_mcp.durable import (
    json_document_bytes as _json_bytes,
)
from comsol_mcp.durable.io import DEFAULT_MAX_JSONL_BYTES, read_file_bytes_bounded
from comsol_mcp.utils.runtime_paths import default_jobs_root as _shared_default_jobs_root

JOB_SCHEMA_VERSION = "2"
CREATE_TIME_TOLERANCE_SECONDS = 0.05
LOCK_RECOVERY_GRACE_SECONDS = 2.0
ACTIVE_STATES = {
    "submitted",
    "starting",
    "smoke_running",
    "smoke_validated",
    "running",
    "cancel_requested",
    "cancelling",
}
TERMINAL_STATES = {"completed", "failed", "interrupted", "cancelled"}
TRANSITIONS = {
    "submitted": {"starting", "failed", "interrupted", "cancel_requested"},
    "starting": {"smoke_running", "failed", "interrupted", "cancel_requested"},
    "smoke_running": {"smoke_validated", "failed", "interrupted", "cancel_requested"},
    "smoke_validated": {"running", "completed", "failed", "interrupted", "cancel_requested"},
    "running": {"completed", "failed", "interrupted", "cancel_requested"},
    "cancel_requested": {"cancelling", "interrupted", "failed"},
    "cancelling": {"cancelled", "interrupted", "failed"},
    "failed": {"starting"},
    "interrupted": {"starting"},
    "cancelled": {"starting"},
    "completed": set(),
}

_PROCESS_GUARD_REGISTRY_LOCK = threading.Lock()
_PROCESS_GUARDS: WeakValueDictionary[str, Any] = WeakValueDictionary()
_RESOURCE_JOURNAL_NAME = re.compile(r"resource(?:-level-\d+|-state-\d+|-segment-\d+)?\.jsonl\Z")
_RESOURCE_JOURNAL_SEGMENT_NAME = re.compile(r"resource-segment-(\d+)\.jsonl\Z")
#: The default journal base name: reads of the base resolve the base file
#: plus every contiguous ``resource-segment-N.jsonl`` in order, so callers
#: observe one logical journal while each physical file stays bounded.
_RESOURCE_JOURNAL_BASE_NAME = "resource.jsonl"


def _validate_resource_journal_name(name: str) -> str:
    if not isinstance(name, str) or not _RESOURCE_JOURNAL_NAME.fullmatch(name):
        raise ValueError("resource journal name must be a supported JSONL filename")
    return name


def _process_guard(path: Path) -> Any:
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _PROCESS_GUARD_REGISTRY_LOCK:
        guard = _PROCESS_GUARDS.get(key)
        if guard is None:
            guard = threading.Lock()
            _PROCESS_GUARDS[key] = guard
        return guard


def default_jobs_root() -> Path:
    return _shared_default_jobs_root()


def _require_ascii_path(path: Path) -> None:
    try:
        str(path.resolve()).encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("Durable job runtime paths must contain ASCII characters only") from exc


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    _durable_atomic_write_json(path, value, replace_fn=os.replace)


def read_json(path: Path) -> dict[str, Any]:
    deadline = time.monotonic() + 1.0
    while True:
        try:
            value = json.loads(
                read_file_bytes_bounded(path, max_bytes=DEFAULT_MAX_JSONL_BYTES).decode("utf-8")
            )
            break
        except PermissionError as exc:
            # Windows scanners and concurrent atomic replacement can briefly
            # deny a reader even though the previous or replacement file is
            # complete. Retry the read; never weaken atomic writer semantics.
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Cannot read durable job artifact {path}: {exc}") from exc
            time.sleep(0.02)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError(f"Cannot read durable job artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Durable job artifact must contain a JSON object: {path}")
    return value


def process_identity(pid: int) -> dict[str, Any]:
    process = psutil.Process(int(pid))
    with process.oneshot():
        command = list(process.cmdline())
        if not command:
            if os.name != "nt" and process.status() == psutil.STATUS_ZOMBIE:
                raise psutil.NoSuchProcess(process.pid, msg="worker has exited and awaits reaping")
            raise OSError("process command line is temporarily unavailable")
        return {
            "pid": process.pid,
            "process_create_time": process.create_time(),
            "command_signature": hashlib.sha256(
                "\0".join(command).encode("utf-8", errors="replace")
            ).hexdigest(),
        }


def process_identity_state(identity: dict[str, Any]) -> tuple[str, str]:
    try:
        actual = process_identity(int(identity["pid"]))
    except KeyError, TypeError, ValueError:
        return "uncertain", "process identity fields are missing or invalid"
    except psutil.NoSuchProcess:
        return "stale", "worker PID no longer exists"
    except (psutil.AccessDenied, psutil.ZombieProcess, OSError) as exc:
        return "uncertain", f"worker identity cannot be inspected: {exc}"
    expected_time = identity.get("process_create_time")
    if (
        isinstance(expected_time, bool)
        or not isinstance(expected_time, (int, float))
        or not math.isfinite(float(expected_time))
    ):
        return "uncertain", "worker creation time is missing or invalid"
    if (
        abs(float(actual["process_create_time"]) - float(expected_time))
        > CREATE_TIME_TOLERANCE_SECONDS
    ):
        return "stale", "worker PID was reused"
    expected_signature = identity.get("command_signature")
    if expected_signature and actual["command_signature"] != expected_signature:
        return "stale", "worker command line no longer matches"
    return "active", "worker PID, creation time, and command line match"


def cancel_request_targets_attempt(control: dict[str, Any], attempt: int) -> bool:
    """Return whether a durable cancel request belongs to this worker attempt.

    Schema-v1 controls have no target attempt, so they retain their reference-power meaning
    for the attempt that reads them. durable cancellation controls are strict: an old request
    must never stop a resumed worker.
    """
    if control.get("request") != "cancel_requested":
        return False
    target = control.get("target_attempt")
    return target is None or int(target) == int(attempt)


class JobLock:
    """Exclusive file lock that removes only locks proven stale by process identity."""

    def __init__(self, path: Path, timeout: float = 5.0, poll_interval: float = 0.025):
        self.path = path
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self.identity = process_identity(os.getpid())
        self.identity["created_at_epoch"] = time.time()
        self._owned_bytes: bytes | None = None
        self._guard_path = self.path.with_name(f".{self.path.name}.guard")
        self._guard_handle = None
        self._posix_guard: Any = None
        self._process_guard = _process_guard(self.path)
        self._process_guard_owned = False

    def _acquire_guard(self, *, deadline: float) -> None:
        if os.name != "nt":
            from comsol_mcp.posix_lock import LockBusy, PosixFileLock

            remaining = max(0.0, deadline - time.monotonic())
            if not self._process_guard.acquire(timeout=remaining):
                raise TimeoutError("in-process durable job lock guard is busy")
            self._process_guard_owned = True
            self._guard_path.parent.mkdir(parents=True, exist_ok=True)
            self._posix_guard = PosixFileLock(self._guard_path)
            try:
                while True:
                    try:
                        self._posix_guard.acquire()
                        return
                    except LockBusy:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("durable job guard is busy") from None
                        time.sleep(self.poll_interval)
            except BaseException:
                self._release_guard()
                raise
        import msvcrt

        remaining = max(0.0, deadline - time.monotonic())
        if not self._process_guard.acquire(timeout=remaining):
            raise TimeoutError(
                f"Timed out waiting for in-process durable job lock guard: {self.path}"
            )
        self._process_guard_owned = True
        self._guard_path.parent.mkdir(parents=True, exist_ok=True)
        handle = None
        try:
            handle = self._guard_path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    self._guard_handle = handle
                    return
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Timed out waiting for durable job lock guard: {self.path}"
                        ) from exc
                    time.sleep(self.poll_interval)
        except Exception:
            if handle is not None:
                handle.close()
            self._process_guard_owned = False
            self._process_guard.release()
            raise

    def _release_guard(self) -> None:
        if os.name != "nt":
            try:
                if self._posix_guard is not None:
                    self._posix_guard.close()
                    self._posix_guard = None
            finally:
                if self._process_guard_owned:
                    self._process_guard_owned = False
                    self._process_guard.release()
            return
        import msvcrt

        handle = self._guard_handle
        self._guard_handle = None
        try:
            if handle is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            if handle is not None:
                handle.close()
            if self._process_guard_owned:
                self._process_guard_owned = False
                self._process_guard.release()

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout
        payload = _json_bytes(self.identity)
        self._acquire_guard(deadline=deadline)
        created = False
        try:
            while True:
                try:
                    descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(self.poll_interval)
                    continue
                except FileExistsError:
                    if not self.path.is_file() or self.path.is_symlink():
                        raise TimeoutError("durable job lock path is not a regular file")
                    observed = None
                    try:
                        observed = self.path.read_bytes()
                        existing = json.loads(observed.decode("utf-8"))
                        state, _ = process_identity_state(existing)
                        if state == "stale" and self.path.read_bytes() == observed:
                            self._unlink_with_retry(expected=observed)
                            continue
                    except OSError, UnicodeDecodeError, json.JSONDecodeError:
                        # A crash between exclusive creation and the fsynced
                        # identity write leaves an empty or partial lock that
                        # no owner will ever release.  Once such a file is
                        # older than the recovery grace period -- far longer
                        # than any live creator's write window -- reclaim it
                        # only while its bytes remain exactly the observed
                        # unparseable snapshot.
                        try:
                            stat = self.path.stat()
                            observed_again = self.path.read_bytes()
                        except OSError:
                            pass
                        else:
                            age_exceeded = (
                                time.time() - stat.st_mtime >= LOCK_RECOVERY_GRACE_SECONDS
                            )
                            if observed is not None and age_exceeded and observed_again == observed:
                                self._unlink_with_retry(expected=observed)
                                continue
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for durable job lock: {self.path}")
                    time.sleep(self.poll_interval)
                    continue
                created = True
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._owned_bytes = payload
                return
        except Exception:
            try:
                if created:
                    try:
                        partial = self.path.read_bytes()
                    except FileNotFoundError:
                        partial = None
                    if partial is not None:
                        self._unlink_with_retry(expected=partial)
            finally:
                self._release_guard()
            raise

    def _unlink_with_retry(self, *, expected: bytes) -> bool:
        # NOTE: the verify-then-unlink window cannot be closed atomically on
        # this platform: Windows has no compare-and-delete primitive, and the
        # always-open msvcrt guard handle (no FILE_SHARE_DELETE) blocks any
        # rename-based swap while the lock is held. Byte revalidation on every
        # retry plus exclusive-create acquisition remain the binding guards.
        deadline = time.monotonic() + 2.0
        while True:
            try:
                if self.path.read_bytes() != expected:
                    return False
                self.path.unlink()
                return True
            except FileNotFoundError:
                return True
            except PermissionError:
                # A polling process or file scanner can momentarily hold the
                # lock file without owning it on Windows.  Revalidate its exact
                # bytes on every retry so a replaced/foreign lock is never removed.
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    def release(self) -> None:
        if self._owned_bytes is None:
            return
        try:
            self._unlink_with_retry(expected=self._owned_bytes)
        finally:
            self._owned_bytes = None
            self._release_guard()

    def __enter__(self) -> "JobLock":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class JobStore:
    """Own the stable on-disk contract for all durable jobs."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else default_jobs_root()
        _require_ascii_path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        if not job_id or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in job_id
        ):
            raise ValueError("Invalid job ID")
        path = self.root / job_id
        if path.parent.resolve() != self.root.resolve():
            raise ValueError("Job path escapes the runtime root")
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("Job directory must not be a symlink or junction")
        if path.exists() and path.resolve(strict=True).parent != self.root.resolve(strict=True):
            raise ValueError("Job path escapes the runtime root")
        return path

    def create(self, spec: dict[str, Any], state: dict[str, Any], job_id: str | None = None) -> str:
        job_id = job_id or f"job-{uuid.uuid4().hex}"
        directory = self.job_dir(job_id)
        if directory.exists():
            raise FileExistsError(f"Job directory already exists: {directory}")
        staging = self.root / f".{job_id}.{uuid.uuid4().hex}.tmp"
        staging.mkdir(parents=False, exist_ok=False)
        published = False
        try:
            atomic_write_json(staging / "spec.json", spec)
            atomic_write_json(staging / "state.json", state)
            atomic_write_json(
                staging / "control.json",
                {
                    "schema_version": JOB_SCHEMA_VERSION,
                    "request": None,
                    "updated_at_epoch": time.time(),
                },
            )
            (staging / "events.jsonl").touch(exist_ok=False)
            (staging / "resource.jsonl").touch(exist_ok=False)
            (staging / "worker.log").touch(exist_ok=False)
            _fsync_directory(staging)
            os.rename(staging, directory)
            published = True
            _fsync_directory(self.root)
        finally:
            if not published:
                shutil.rmtree(staging, ignore_errors=True)
        return job_id

    def bind_worker_identity(self, job_id: str, identity: dict[str, Any]) -> dict[str, Any]:
        """Let a launched worker durably bind itself before any side effect."""
        required = {"pid", "process_create_time", "command_signature"}
        if not isinstance(identity, dict) or not required <= set(identity):
            raise ValueError("worker identity is incomplete")
        with self.lock(job_id):
            state = self.read_state(job_id)
            observed = {
                "pid": state.get("worker_pid"),
                "process_create_time": state.get("worker_process_create_time"),
                "command_signature": state.get("worker_command_signature"),
            }
            expected = {key: identity[key] for key in required}
            if observed["pid"] is not None:
                if observed != expected:
                    raise RuntimeError("durable job is already bound to another worker")
                return {"bound": True, "idempotent": True, "state": state}
            state.update(
                {
                    "worker_pid": expected["pid"],
                    "worker_process_create_time": expected["process_create_time"],
                    "worker_command_signature": expected["command_signature"],
                    "updated_at_epoch": time.time(),
                }
            )
            atomic_write_json(self.job_dir(job_id) / "state.json", state)
            self._append_event_unlocked(
                job_id,
                "worker_identity_bound",
                {"pid": expected["pid"]},
                str(state["status"]),
            )
            return {"bound": True, "idempotent": False, "state": state}

    def read_spec(self, job_id: str) -> dict[str, Any]:
        return read_json(self.job_dir(job_id) / "spec.json")

    def read_state(self, job_id: str) -> dict[str, Any]:
        return read_json(self.job_dir(job_id) / "state.json")

    def read_control(self, job_id: str) -> dict[str, Any]:
        return read_json(self.job_dir(job_id) / "control.json")

    def write_control(
        self,
        job_id: str,
        request: str | None = None,
        *,
        fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        control = {
            "schema_version": JOB_SCHEMA_VERSION,
            "request": request,
            "updated_at_epoch": time.time(),
        }
        control.update(fields or {})
        atomic_write_json(self.job_dir(job_id) / "control.json", control)
        return control

    def request_cancel(
        self,
        job_id: str,
        *,
        requester_identity: dict[str, Any],
        expected_attempt: int | None = None,
    ) -> dict[str, Any]:
        """Durably linearize one attempt-bound cancellation request.

        The control artifact is the durable authorization and is written before
        the state transition/event. Repeated callers observe the same request
        ID. A completed job wins if it was already terminal under this lock.
        """
        with self.lock(job_id):
            state = self.read_state(job_id)
            status = str(state.get("status"))
            control = self.read_control(job_id)
            attempt = int(state.get("attempt", 1))
            if expected_attempt is not None and attempt != int(expected_attempt):
                return {
                    "accepted": False,
                    "reason": "attempt_mismatch",
                    "state": state,
                    "control": control,
                }
            if status in TERMINAL_STATES:
                return {
                    "accepted": False,
                    "reason": "terminal",
                    "state": state,
                    "control": control,
                }
            existing_request = control.get("request")
            if existing_request == "cancel_requested":
                existing_attempt = control.get("target_attempt")
                if existing_attempt not in (None, attempt):
                    return {
                        "accepted": False,
                        "reason": "stale_control_attempt",
                        "state": state,
                        "control": control,
                    }
                request_id = control.get("request_id") or f"cancel-{uuid.uuid4().hex}"
                requested_at = control.get("requested_at_epoch") or time.time()
                if not control.get("request_id"):
                    control = {
                        **control,
                        "schema_version": JOB_SCHEMA_VERSION,
                        "request_id": request_id,
                        "target_attempt": attempt,
                        "requested_at_epoch": requested_at,
                        "updated_at_epoch": time.time(),
                    }
                    atomic_write_json(self.job_dir(job_id) / "control.json", control)
                if status != "cancel_requested":
                    if "cancel_requested" not in TRANSITIONS.get(status, set()):
                        return {
                            "accepted": False,
                            "reason": "state_cannot_accept_cancel",
                            "state": state,
                            "control": control,
                        }
                    state["status"] = "cancel_requested"
                    state["cancel"] = {
                        "request_id": request_id,
                        "target_attempt": attempt,
                        "phase": "requested",
                        "requested_at_epoch": requested_at,
                        "phase_timestamps": {"requested": requested_at},
                    }
                    state["updated_at_epoch"] = time.time()
                    atomic_write_json(self.job_dir(job_id) / "state.json", state)
                    self._append_event_unlocked(
                        job_id,
                        "cancel_request_reconciled",
                        {"request_id": request_id, "target_attempt": attempt},
                        "cancel_requested",
                    )
                return {"accepted": True, "idempotent": True, "state": state, "control": control}
            if existing_request not in (None, ""):
                return {
                    "accepted": False,
                    "reason": "unknown_control_request",
                    "state": state,
                    "control": control,
                }
            if "cancel_requested" not in TRANSITIONS.get(status, set()):
                return {
                    "accepted": False,
                    "reason": "state_cannot_accept_cancel",
                    "state": state,
                    "control": control,
                }
            request_id = f"cancel-{uuid.uuid4().hex}"
            target_worker = {
                "pid": state.get("worker_pid"),
                "process_create_time": state.get("worker_process_create_time"),
                "command_signature": state.get("worker_command_signature"),
            }
            now = time.time()
            control = {
                "schema_version": JOB_SCHEMA_VERSION,
                "request": "cancel_requested",
                "request_id": request_id,
                "request_type": "cancel",
                "target_attempt": attempt,
                "target_worker": target_worker,
                "requested_at_epoch": now,
                "requester_identity": requester_identity,
                "updated_at_epoch": now,
            }
            # Intent is durable before the state transition or any future
            # coordinator side effect.
            atomic_write_json(self.job_dir(job_id) / "control.json", control)
            state["status"] = "cancel_requested"
            state["cancel"] = {
                "request_id": request_id,
                "target_attempt": attempt,
                "requested_at_epoch": now,
                "requester_identity": requester_identity,
                "phase": "requested",
                "phase_timestamps": {"requested": now},
                "native": {"candidate": None, "supported": None, "attempted": False},
                "worker": {"exact_identity": target_worker},
            }
            state["updated_at_epoch"] = time.time()
            atomic_write_json(self.job_dir(job_id) / "state.json", state)
            self._append_event_unlocked(
                job_id,
                "cancel_requested",
                {"request_id": request_id, "target_attempt": attempt},
                "cancel_requested",
            )
            return {"accepted": True, "idempotent": False, "state": state, "control": control}

    def record_cooperative_cancel_observed(
        self,
        job_id: str,
        *,
        attempt: int,
        message: str,
        worker_error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record that the target worker observed its matching cancel request.

        This is nonterminal evidence.  Only the detached coordinator may commit
        ``cancelled`` after worker, descendant, server-port, and lease cleanup is
        proved.  A stale-attempt request is ignored rather than being allowed to
        affect a resumed worker.
        """
        with self.lock(job_id):
            state = self.read_state(job_id)
            control = self.read_control(job_id)
            if int(state.get("attempt", -1)) != int(attempt):
                return {"recorded": False, "reason": "state_attempt_mismatch", "state": state}
            if not cancel_request_targets_attempt(control, attempt):
                return {"recorded": False, "reason": "no_matching_cancel_request", "state": state}
            if state.get("status") not in {"cancel_requested", "cancelling"}:
                status = str(state.get("status"))
                if "cancel_requested" not in TRANSITIONS.get(status, set()):
                    return {
                        "recorded": False,
                        "reason": "state_not_cancelling",
                        "state": state,
                    }
                requested_at = float(control.get("requested_at_epoch", time.time()))
                state["status"] = "cancel_requested"
                state["cancel"] = {
                    "request_id": control.get("request_id"),
                    "target_attempt": int(attempt),
                    "requested_at_epoch": requested_at,
                    "requester_identity": control.get("requester_identity"),
                    "phase": "requested",
                    "phase_timestamps": {"requested": requested_at},
                    "native": {"candidate": None, "supported": None, "attempted": False},
                    "worker": {"exact_identity": control.get("target_worker")},
                }
                state["updated_at_epoch"] = time.time()
                atomic_write_json(self.job_dir(job_id) / "state.json", state)
                self._append_event_unlocked(
                    job_id,
                    "cancel_request_reconciled",
                    {
                        "request_id": control.get("request_id"),
                        "target_attempt": int(attempt),
                    },
                    "cancel_requested",
                )

            cancel = dict(state.get("cancel") or {})
            request_id = control.get("request_id")
            if cancel.get("request_id") not in (None, request_id):
                return {"recorded": False, "reason": "state_request_mismatch", "state": state}
            normalized_error = None
            if worker_error is not None:
                if not isinstance(worker_error, dict):
                    raise ValueError("worker cancellation error must be an object")
                error_type = worker_error.get("type")
                error_message = worker_error.get("message")
                cleanup_errors = worker_error.get("cleanup_errors", [])
                if not isinstance(error_type, str) or not error_type:
                    raise ValueError("worker cancellation error type is required")
                if not isinstance(error_message, str) or not error_message:
                    raise ValueError("worker cancellation error message is required")
                if not isinstance(cleanup_errors, list) or not all(
                    isinstance(item, str) for item in cleanup_errors
                ):
                    raise ValueError("worker cancellation cleanup errors must be a string list")
                normalized_error = {
                    "type": error_type[:128],
                    "message": error_message[:2000],
                    "cleanup_errors": [item[:500] for item in cleanup_errors[:20]],
                }
            error_changed = bool(
                normalized_error is not None and cancel.get("worker_error") != normalized_error
            )
            if normalized_error is not None:
                cancel["worker_error"] = normalized_error
            existing = cancel.get("cooperative_observation")
            if isinstance(existing, dict) and existing.get("request_id") == request_id:
                if error_changed:
                    state["cancel"] = cancel
                    state["updated_at_epoch"] = time.time()
                    atomic_write_json(self.job_dir(job_id) / "state.json", state)
                    self._append_event_unlocked(
                        job_id,
                        "cancel_worker_error_recorded",
                        {
                            "request_id": request_id,
                            "target_attempt": int(attempt),
                            "error_type": normalized_error["type"],
                        },
                        str(state["status"]),
                    )
                return {"recorded": True, "idempotent": True, "state": state}

            observed_at = time.time()
            cancel["cooperative_observation"] = {
                "request_id": request_id,
                "target_attempt": int(attempt),
                "observed_at_epoch": observed_at,
                "message": str(message),
                "worker": {
                    "pid": state.get("worker_pid"),
                    "process_create_time": state.get("worker_process_create_time"),
                    "command_signature": state.get("worker_command_signature"),
                },
            }
            state["cancel"] = cancel
            state["updated_at_epoch"] = observed_at
            atomic_write_json(self.job_dir(job_id) / "state.json", state)
            self._append_event_unlocked(
                job_id,
                "cooperative_cancel_observed",
                {
                    "request_id": request_id,
                    "target_attempt": int(attempt),
                    "worker_error_type": (
                        normalized_error["type"] if normalized_error is not None else None
                    ),
                },
                str(state["status"]),
            )
            return {"recorded": True, "idempotent": False, "state": state}

    @contextmanager
    def lock(self, job_id: str, timeout: float = 5.0) -> Iterator[None]:
        with JobLock(self.job_dir(job_id) / ".state.lock", timeout=timeout):
            yield

    def update_state(
        self,
        job_id: str,
        new_status: str | None = None,
        *,
        patch: dict[str, Any] | None = None,
        event: str | None = None,
        event_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if patch and "status" in patch:
            raise ValueError("status must be changed through new_status")
        with self.lock(job_id):
            state = self.read_state(job_id)
            current = str(state.get("status"))
            if new_status is not None and new_status != current:
                if new_status not in TRANSITIONS.get(current, set()):
                    raise ValueError(f"Invalid job state transition: {current} -> {new_status}")
                state["status"] = new_status
            if current == "completed" and patch is not None:
                raise ValueError("Completed job state is immutable")
            state.update(patch or {})
            state["updated_at_epoch"] = time.time()
            atomic_write_json(self.job_dir(job_id) / "state.json", state)
            if event:
                self._append_event_unlocked(job_id, event, event_data or {}, state["status"])
            return state

    def append_event(self, job_id: str, event: str, data: dict[str, Any] | None = None) -> None:
        with self.lock(job_id):
            state = self.read_state(job_id)
            self._append_event_unlocked(job_id, event, data or {}, str(state["status"]))

    def record_startup_failure(
        self, job_id: str, *, attempt: int, error: dict[str, str], event: str
    ) -> dict[str, Any]:
        """Publish startup failure only while this attempt still owns startup.

        Cancellation remains owned by its coordinator. A resumed or terminal
        attempt must not be overwritten by an older worker's validation error.
        """
        with self.lock(job_id):
            state = self.read_state(job_id)
            if state.get("attempt", 1) != attempt:
                return {"recorded": False, "reason": "attempt_mismatch"}
            control = self.read_control(job_id)
            if state["status"] in {"cancel_requested", "cancelling"} or (
                cancel_request_targets_attempt(control, attempt)
            ):
                return {"recorded": False, "reason": "cancellation_owns_state"}
            if state["status"] not in {"submitted", "starting"}:
                return {"recorded": False, "reason": "startup_no_longer_owned"}
            state.update({"status": "failed", "last_error": error, "updated_at_epoch": time.time()})
            atomic_write_json(self.job_dir(job_id) / "state.json", state)
            self._append_event_unlocked(job_id, event, {}, "failed")
            return {"recorded": True}

    def _read_resource_journal_unlocked_path(self, path: Path) -> list[dict[str, Any]]:
        from .resource_admission import RESOURCE_JOURNAL_MAX_ENTRIES

        entries: list[dict[str, Any]] = []
        if not path.exists():
            return entries
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    raise ValueError("resource journal contains a blank record")
                entries.append(json.loads(line))
                if len(entries) > RESOURCE_JOURNAL_MAX_ENTRIES:
                    raise ValueError("resource journal exceeds the entry limit")
        return entries

    def _resource_journal_files(self, job_dir: Path, journal_name: str) -> list[Path]:
        """Resolve the physical journal files for one logical journal name.

        The default base name resolves the base file plus every contiguous
        ``resource-segment-N.jsonl`` in ascending order (segments number
        from 2, so the base is unambiguously segment 1). A gap between
        existing segments is damaged evidence, not absence: silently
        skipping it would hide retained history, so it refuses. An explicit
        non-base name always addresses exactly one physical file.
        """
        if journal_name != _RESOURCE_JOURNAL_BASE_NAME:
            return [job_dir / journal_name]
        files = [job_dir / journal_name]
        if not job_dir.is_dir():
            # A missing job directory has no journal and no segments: the
            # per-file reader treats a missing file as empty.
            return files
        segment_numbers = sorted(
            int(match.group(1))
            for path in job_dir.iterdir()
            if path.is_file()
            for match in [_RESOURCE_JOURNAL_SEGMENT_NAME.fullmatch(path.name)]
            if match
        )
        expected = 2
        for number in segment_numbers:
            if number != expected:
                raise ValueError(
                    "resource journal segments are not contiguous: expected "
                    f"segment {expected}, found {number}; repair or export the "
                    "retained evidence before continuing this job"
                )
            files.append(job_dir / f"resource-segment-{number}.jsonl")
            expected += 1
        return files

    def read_resource_journal(
        self, job_id: str, *, journal_name: str = "resource.jsonl"
    ) -> list[dict[str, Any]]:
        """Read and validate the bounded append-only resource journal.

        Reading the default base journal returns the LOGICAL whole journal:
        the base file plus every segment, each individually bounded by the
        hard per-file entry limit, concatenated in order and replayed as
        one sequence.
        """
        from .resource_admission import replay_resource_journal

        with self.lock(job_id):
            journal_name = _validate_resource_journal_name(journal_name)
            job_dir = self.job_dir(job_id).resolve(strict=False)
            files = self._resource_journal_files(job_dir, journal_name)
            resolved = []
            for path in files:
                path = path.resolve(strict=False)
                if path.parent != job_dir:
                    raise ValueError("resource journal path escapes job directory")
                resolved.append(path)
            entries: list[dict[str, Any]] = []
            for path in resolved:
                entries.extend(self._read_resource_journal_unlocked_path(path))
            if entries:
                admission = next(
                    (item for item in entries if item.get("entry_type") == "admission"),
                    None,
                )
                expected_policy = admission.get("policy") if admission is not None else None
                replay_resource_journal(
                    entries,
                    attempt=max(int(item.get("attempt", 0)) for item in entries),
                    expected_policy=expected_policy,
                )
            return entries

    def append_resource_journal(
        self,
        job_id: str,
        entries: list[dict[str, Any]],
        *,
        expected_policy: object,
        journal_name: str = "resource.jsonl",
    ) -> dict[str, Any]:
        """Durably append validated resource transitions for the active attempt.

        Appends to the default base journal roll into the next bounded
        ``resource-segment-N.jsonl`` when the active segment cannot hold the
        batch whole; the per-file entry limit is never exceeded, history is
        never truncated, and exhausting the bounded segment count refuses
        with export guidance. Validation replays the LOGICAL whole journal.
        """
        from .resource_admission import (
            RESOURCE_JOURNAL_MAX_ENTRIES,
            RESOURCE_JOURNAL_MAX_SEGMENTS,
            replay_resource_journal,
        )

        if not isinstance(entries, list) or not entries:
            raise ValueError("resource journal append requires a non-empty entry list")
        if len(entries) > RESOURCE_JOURNAL_MAX_ENTRIES:
            raise ValueError(
                "resource journal append exceeds the per-segment entry limit "
                f"({len(entries)} entries against {RESOURCE_JOURNAL_MAX_ENTRIES})"
            )
        with self.lock(job_id):
            journal_name = _validate_resource_journal_name(journal_name)
            job_dir = self.job_dir(job_id).resolve(strict=False)
            files = self._resource_journal_files(job_dir, journal_name)
            per_file: list[tuple[Path, list[dict[str, Any]]]] = []
            logical: list[dict[str, Any]] = []
            for path in files:
                path = path.resolve(strict=False)
                if path.parent != job_dir:
                    raise ValueError("resource journal path escapes job directory")
                file_entries = self._read_resource_journal_unlocked_path(path)
                per_file.append((path, file_entries))
                logical.extend(file_entries)
            state = self.read_state(job_id)
            attempt = int(state.get("attempt", 1))
            replay = replay_resource_journal(
                logical + entries,
                attempt=attempt,
                expected_policy=expected_policy,
            )
            target_path, target_entries = per_file[-1]
            if len(target_entries) + len(entries) > RESOURCE_JOURNAL_MAX_ENTRIES:
                next_segment = len(per_file) + 1
                if next_segment > RESOURCE_JOURNAL_MAX_SEGMENTS:
                    raise ValueError(
                        "resource journal segments are exhausted: "
                        f"{RESOURCE_JOURNAL_MAX_SEGMENTS} bounded segments hold "
                        f"{RESOURCE_JOURNAL_MAX_ENTRIES * RESOURCE_JOURNAL_MAX_SEGMENTS} "
                        "entries; export the retained evidence and submit a fresh "
                        "job instead of growing the journal without bound"
                    )
                target_path = job_dir / f"resource-segment-{next_segment}.jsonl"
                target_entries = []
            payload = b"".join(
                json.dumps(
                    entry,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
                for entry in target_entries + entries
            )
            _durable_atomic_write_bytes(target_path, payload, replace_fn=os.replace)
            return replay

    def _append_event_unlocked(
        self, job_id: str, event: str, data: dict[str, Any], status: str
    ) -> None:
        record = {
            "schema_version": JOB_SCHEMA_VERSION,
            "timestamp_epoch": time.time(),
            "event": event,
            "status": status,
            "data": data,
        }
        with (self.job_dir(job_id) / "events.jsonl").open("ab") as handle:
            handle.write(
                json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
            )
            handle.flush()
            os.fsync(handle.fileno())

    def tail(self, job_id: str, n: int = 20) -> dict[str, Any]:
        count = max(1, min(int(n), 200))
        directory = self.job_dir(job_id)
        max_line_chars = 65_536

        def read_tail(path: Path) -> deque[str]:
            lines: deque[str] = deque(maxlen=count)
            read_size = max_line_chars + 2
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                while line := handle.readline(read_size):
                    prefix = line.rstrip("\r\n")[:max_line_chars]
                    # Discard the rest in bounded chunks, retaining one logical
                    # line even when a native writer emits a very long record.
                    while len(line) == read_size and not line.endswith("\n"):
                        line = handle.readline(read_size)
                    lines.append(prefix)
            return lines

        events = read_tail(directory / "events.jsonl")
        logs = read_tail(directory / "worker.log")
        return {"job_id": job_id, "limit": count, "events": list(events), "worker_log": list(logs)}
