"""Detached, solver-free durable cancellation cancellation coordinator."""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

import psutil

from .process_control import (
    capture_owned_descendants,
    inspect_identity,
    terminate_exact,
    verify_absent,
)
from .store import JobStore, atomic_write_json, cancel_request_targets_attempt, process_identity

_RESUMABLE_PHASES = {"terminate", "force_kill", "verifying"}
_PROCESS_IDENTITY_FIELDS = ("pid", "process_create_time", "command_signature")


def _capture_with_inspection_budget(worker: dict[str, Any], budget: float) -> dict[str, Any]:
    """Recheck transient inspection failure; never treat uncertainty as exit."""
    deadline = time.monotonic() + budget
    while True:
        captured = capture_owned_descendants(worker)
        if captured["worker"]["state"] != "uncertain" or time.monotonic() >= deadline:
            return captured
        time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))


def _same_process_identity(left: Any, right: Any) -> bool:
    return (
        isinstance(left, dict)
        and isinstance(right, dict)
        and all(left.get(field) == right.get(field) for field in _PROCESS_IDENTITY_FIELDS)
    )


def _owns_cancel_write(
    state: dict[str, Any],
    control: dict[str, Any],
    request_id: str,
    identity: dict[str, Any],
) -> bool:
    cancel = state.get("cancel")
    if not isinstance(cancel, dict):
        return False
    raw_attempt = state.get("attempt", 1)
    if isinstance(raw_attempt, bool):
        return False
    try:
        attempt = int(raw_attempt)
    except TypeError, ValueError, OverflowError:
        return False
    if attempt <= 0:
        return False
    return bool(
        state.get("status") == "cancelling"
        and control.get("request") == "cancel_requested"
        and control.get("request_id") == request_id
        and cancel_request_targets_attempt(control, attempt)
        and cancel.get("target_attempt") in (None, attempt)
        and _same_process_identity(cancel.get("coordinator"), identity)
    )


def _remaining_phase_budget(
    cancel: dict[str, Any],
    phase: str,
    budget_seconds: float,
) -> float:
    budget = max(0.0, float(budget_seconds))
    started = (cancel.get("phase_timestamps") or {}).get(phase)
    if started is None:
        return budget
    if isinstance(started, bool) or not isinstance(started, (int, float)):
        return 0.0
    started_value = float(started)
    if not math.isfinite(started_value):
        return 0.0
    elapsed = max(0.0, time.time() - started_value)
    return max(0.0, budget - elapsed)


def _persisted_budget(
    policy: dict[str, Any],
    key: str,
    fallback: float,
) -> float:
    value = policy.get(key)
    if value is None:
        return max(0.0, float(fallback))
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        return 0.0
    return float(value)


def _process_identity_key(identity: Any) -> tuple[int, float, str]:
    if not isinstance(identity, dict):
        raise ValueError("captured process identity must be an object")
    pid = identity.get("pid")
    created = identity.get("process_create_time")
    signature = identity.get("command_signature")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("captured process identity has an invalid PID")
    if (
        isinstance(created, bool)
        or not isinstance(created, (int, float))
        or not math.isfinite(float(created))
    ):
        raise ValueError("captured process identity has an invalid creation time")
    if not isinstance(signature, str) or not signature:
        raise ValueError("captured process identity has an invalid command signature")
    return pid, float(created), signature


def _merge_process_identities(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[int, float, str]] = set()
    for group in groups:
        if not isinstance(group, list):
            raise ValueError("captured descendants must be a list")
        for identity in group:
            key = _process_identity_key(identity)
            if key not in seen:
                seen.add(key)
                merged.append(dict(identity))
    return merged


def _enter_phase(cancel: dict[str, Any], phase: str, now: float) -> dict[str, Any]:
    updated = dict(cancel)
    timestamps = dict(updated.get("phase_timestamps") or {})
    timestamps.setdefault(phase, float(now))
    updated["phase"] = phase
    updated["phase_timestamps"] = timestamps
    return updated


def _claim(
    store: JobStore,
    job_id: str,
    request_id: str,
    identity: dict[str, Any],
    *,
    grace_seconds: float,
    terminate_seconds: float,
) -> dict[str, Any] | None:
    with store.lock(job_id):
        state = store.read_state(job_id)
        control = store.read_control(job_id)
        cancel = state.get("cancel") if isinstance(state.get("cancel"), dict) else {}
        raw_attempt = state.get("attempt", 1)
        if isinstance(raw_attempt, bool):
            return None
        try:
            attempt = int(raw_attempt)
        except TypeError, ValueError, OverflowError:
            return None
        if attempt <= 0:
            return None
        if (
            control.get("request") != "cancel_requested"
            or control.get("request_id") != request_id
            or not cancel_request_targets_attempt(control, attempt)
            or cancel.get("target_attempt") not in (None, attempt)
            or state.get("status") not in {"cancel_requested", "cancelling"}
        ):
            return None
        coordinator = cancel.get("coordinator")
        if isinstance(coordinator, dict) and not _same_process_identity(coordinator, identity):
            if inspect_identity(coordinator)["state"] != "stale":
                return None
        previous_phase = cancel.get("phase")
        now = time.time()
        cancel["coordinator"] = identity
        cancel = _enter_phase(
            cancel,
            previous_phase if previous_phase in _RESUMABLE_PHASES else "native_grace",
            now,
        )
        timing_policy = dict(cancel.get("timing_policy") or {})
        cancel["timing_policy"] = {
            **timing_policy,
            "native_grace_budget_s": _persisted_budget(
                timing_policy,
                "native_grace_budget_s",
                grace_seconds,
            ),
            "terminate_budget_s": _persisted_budget(
                timing_policy,
                "terminate_budget_s",
                terminate_seconds,
            ),
        }
        cancel.pop("blocker", None)
        state["cancel"] = cancel
        state["status"] = "cancelling"
        state["updated_at_epoch"] = now
        atomic_write_json(store.job_dir(job_id) / "state.json", state)
        store._append_event_unlocked(
            job_id,
            "cancel_coordinator_claimed",
            {"request_id": request_id, "coordinator_pid": identity["pid"]},
            "cancelling",
        )
        return {"state": state, "control": control, "resume_phase": cancel["phase"]}


def _checkpoint(
    store: JobStore,
    job_id: str,
    request_id: str,
    identity: dict[str, Any],
    phase: str,
    *,
    patch: dict[str, Any] | None = None,
) -> bool:
    """Persist one restartable phase before its associated side effect."""
    with store.lock(job_id):
        state = store.read_state(job_id)
        control = store.read_control(job_id)
        cancel = dict(state.get("cancel") or {})
        if not _owns_cancel_write(state, control, request_id, identity):
            return False
        now = time.time()
        cancel = _enter_phase(cancel, phase, now)
        if patch:
            cancel.update(patch)
        state["cancel"] = cancel
        state["updated_at_epoch"] = now
        atomic_write_json(store.job_dir(job_id) / "state.json", state)
        store._append_event_unlocked(
            job_id,
            "cancel_phase_checkpoint",
            {"request_id": request_id, "phase": phase},
            "cancelling",
        )
        return True


def _record_blocker(
    store: JobStore,
    job_id: str,
    request_id: str,
    identity: dict[str, Any],
    message: str,
) -> bool:
    with store.lock(job_id):
        state = store.read_state(job_id)
        control = store.read_control(job_id)
        if not _owns_cancel_write(state, control, request_id, identity):
            return False
        cancel = dict(state.get("cancel") or {})
        now = time.time()
        cancel = _enter_phase(cancel, "blocked", now)
        cancel["blocker"] = message
        state["cancel"] = cancel
        state["updated_at_epoch"] = now
        atomic_write_json(store.job_dir(job_id) / "state.json", state)
        store._append_event_unlocked(
            job_id, "cancel_blocked", {"request_id": request_id, "message": message}, "cancelling"
        )
        return True


def _commit_cancelled(
    store: JobStore,
    job_id: str,
    request_id: str,
    identity: dict[str, Any],
    verification: dict[str, Any],
    actions: list[dict[str, Any]],
) -> bool:
    with store.lock(job_id):
        state = store.read_state(job_id)
        control = store.read_control(job_id)
        if not _owns_cancel_write(state, control, request_id, identity):
            return False
        cancel = dict(state.get("cancel") or {})
        completed_at = time.time()
        cancel = _enter_phase(cancel, "verified", completed_at)
        cancel = _enter_phase(cancel, "terminal_commit", completed_at)
        timestamps = dict(cancel.get("phase_timestamps") or {})

        def finite_timestamp(value: object) -> float | None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            try:
                parsed = float(value)
            except OverflowError:
                return None
            return parsed if math.isfinite(parsed) else None

        requested_at = finite_timestamp(
            timestamps.get("requested", cancel.get("requested_at_epoch"))
        )
        coordinator_at = min(
            (
                parsed
                for phase, value in timestamps.items()
                if phase not in {"requested", "terminal_commit"}
                if (parsed := finite_timestamp(value)) is not None
            ),
            default=None,
        )
        verifying_at = finite_timestamp(timestamps.get("verifying"))
        cancel.update(
            {
                "phase": "verified",
                "worker_actions": actions,
                "verification": verification,
                "completed_at_epoch": completed_at,
                "teardown_latency": {
                    "requested_to_terminal_s": (
                        max(0.0, completed_at - requested_at) if requested_at is not None else None
                    ),
                    "coordinator_to_terminal_s": (
                        max(0.0, completed_at - coordinator_at)
                        if coordinator_at is not None
                        else None
                    ),
                    "verification_to_terminal_s": (
                        max(0.0, completed_at - verifying_at) if verifying_at is not None else None
                    ),
                },
            }
        )
        state["status"] = "cancelled"
        state["cancel"] = cancel
        state["updated_at_epoch"] = completed_at
        atomic_write_json(store.job_dir(job_id) / "state.json", state)
        store._append_event_unlocked(job_id, "cancelled", {"request_id": request_id}, "cancelled")
        return True


def _verify_solver_cleanup(store: JobStore, job_id: str) -> dict[str, Any]:
    """Verify the target job's recorded lease, server identities, and port.

    Bare historical server PIDs are intentionally insufficient: an durable cancellation terminal
    cancellation must remain blocked rather than risk acting after PID reuse.
    """

    def with_attached_preservation(result: dict[str, Any]) -> dict[str, Any]:
        spec = store.read_spec(job_id)
        backend = spec.get("execution_backend")
        if backend is None:
            return result
        from comsol_mcp.jobs.attached_runtime import (
            normalize_attached_execution_target,
            verify_attached_process_preservation,
        )
        from comsol_mcp.shared_session.process_probe import (
            collect_shared_preflight_snapshot,
        )

        target = normalize_attached_execution_target(backend)
        preservation = verify_attached_process_preservation(
            target,
            first_probe=collect_shared_preflight_snapshot(),
            second_probe=collect_shared_preflight_snapshot(),
        )
        state = store.read_state(job_id)
        cleanup = state.get("attached_cleanup")
        model_identity_preserved = bool(
            isinstance(cleanup, dict)
            and cleanup.get("success") is True
            and cleanup.get("model_identity_preserved") is True
        )
        model_status = (
            "verified_before_cooperative_disconnect"
            if model_identity_preserved
            else "not_automatically_rechecked_after_worker_exit"
        )
        attached = {
            **preservation,
            "model_preservation_status": model_status,
            "model_clear_attempted": False,
            "external_server_termination_attempted": False,
        }
        return {
            **result,
            "ok": bool(
                result.get("ok") and preservation.get("success") and model_identity_preserved
            ),
            "attached_external_resources": attached,
        }

    # This common path is intentionally dependency-free: most workers release
    # their lease before exiting, and a coordinator must not import the complete
    # MCP tool package merely to confirm its absence.
    if not (store.root.parent / "solver_owner.json").is_file():
        return with_attached_preservation(
            {
                "ok": True,
                "lease_state": "absent",
                "lease_recovered": False,
                "recorded_port_closed": True,
            }
        )

    from comsol_mcp.tools.ownership import SolverOwnership

    ownership = SolverOwnership(store.root.parent)
    status = ownership.status()
    lease_status = status["lease"]
    if lease_status["state"] == "absent":
        return with_attached_preservation(
            {
                "ok": True,
                "lease_state": "absent",
                "lease_recovered": False,
                "recorded_port_closed": True,
            }
        )
    lease = lease_status.get("lease")
    if not isinstance(lease, dict) or lease.get("owner") != f"job:{job_id}":
        return {
            "ok": False,
            "reason": "target job does not exclusively own the recorded solver lease",
            "lease_state": lease_status["state"],
        }
    from comsol_mcp.jobs.process_control import owned_solver_identities_from_lease

    try:
        servers = owned_solver_identities_from_lease(lease)
    except ValueError as exc:
        return {"ok": False, "reason": str(exc), "lease_state": lease_status["state"]}
    if lease.get("comsol_server_pids") and not servers:
        return {
            "ok": False,
            "reason": "lease contains only legacy server PIDs",
            "lease_state": lease_status["state"],
        }
    server_verification = verify_absent(servers)
    if not server_verification["absent"]:
        return {
            "ok": False,
            "reason": "recorded server identity remains active or uncertain",
            "servers": server_verification,
            "lease_state": lease_status["state"],
        }
    port = lease.get("comsol_server_port")
    try:
        port_open = bool(port) and any(
            connection.laddr and int(connection.laddr.port) == int(port)
            for connection in psutil.net_connections(kind="inet")
        )
    except (psutil.AccessDenied, OSError) as exc:
        return {
            "ok": False,
            "reason": f"cannot verify recorded server port: {exc}",
            "lease_state": lease_status["state"],
        }
    if port_open:
        return {
            "ok": False,
            "reason": "recorded COMSOL server port remains open",
            "lease_state": lease_status["state"],
        }
    if lease_status["state"] != "stale":
        return {
            "ok": False,
            "reason": "target job lease is not proven stale",
            "lease_state": lease_status["state"],
        }
    recovered = ownership.recover_stale()
    if not recovered.get("success"):
        return {
            "ok": False,
            "reason": f"stale lease recovery refused: {recovered}",
            "lease_state": lease_status["state"],
        }
    return with_attached_preservation(
        {
            "ok": True,
            "lease_state": "recovered",
            "lease_recovered": bool(recovered.get("recovered")),
            "recorded_port_closed": True,
            "servers": server_verification,
        }
    )


def _verified_cancel(
    store: JobStore, job_id: str, worker_verification: dict[str, Any]
) -> dict[str, Any]:
    solver = _verify_solver_cleanup(store, job_id)
    return {
        **worker_verification,
        "solver": solver,
        "absent": bool(worker_verification["absent"] and solver["ok"]),
    }


def _wait_for_process_absence(
    identities: list[dict[str, Any]],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Recheck exit within the budget; unresolved uncertainty never proves absence."""
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        verification = verify_absent(identities)
        if verification["absent"]:
            return verification
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return verification
        time.sleep(min(0.025, remaining))


def run(
    root: str,
    job_id: str,
    request_id: str,
    grace_seconds: float = 10.0,
    terminate_seconds: float = 5.0,
    *,
    phase_hook: Callable[[str], None] | None = None,
) -> int:
    store = JobStore(Path(root))
    identity = process_identity(os.getpid())
    claimed = _claim(
        store,
        job_id,
        request_id,
        identity,
        grace_seconds=grace_seconds,
        terminate_seconds=terminate_seconds,
    )
    if claimed is None:
        return 0
    control = claimed["control"]
    resume_phase = claimed["resume_phase"]
    worker = control.get("target_worker")
    if not isinstance(worker, dict) or worker.get("pid") is None:
        _record_blocker(
            store,
            job_id,
            request_id,
            identity,
            "target worker identity is missing",
        )
        return 2

    cancel_evidence = claimed["state"].get("cancel") or {}
    timing_policy = dict(cancel_evidence.get("timing_policy") or {})
    grace_budget = _persisted_budget(
        timing_policy,
        "native_grace_budget_s",
        grace_seconds,
    )
    terminate_budget = _persisted_budget(
        timing_policy,
        "terminate_budget_s",
        terminate_seconds,
    )
    cleanup_verify_seconds = _persisted_budget(
        timing_policy,
        "cleanup_verification_budget_s",
        max(0.5, terminate_budget),
    )
    cancel_evidence["timing_policy"] = {
        **timing_policy,
        "native_grace_budget_s": grace_budget,
        "terminate_budget_s": terminate_budget,
        "cleanup_verification_budget_s": cleanup_verify_seconds,
    }
    if not _checkpoint(
        store,
        job_id,
        request_id,
        identity,
        resume_phase,
        patch={"timing_policy": cancel_evidence["timing_policy"]},
    ):
        return 0
    process_tree_contained = bool(store.read_state(job_id).get("process_tree_contained"))
    persisted_descendants = cancel_evidence.get("descendants", [])
    try:
        descendants = _merge_process_identities(persisted_descendants)
    except ValueError as exc:
        _record_blocker(store, job_id, request_id, identity, str(exc))
        return 0

    def refresh_descendants(
        current: list[dict[str, Any]],
        phase: str,
    ) -> list[dict[str, Any]] | None:
        captured = _capture_with_inspection_budget(worker, cleanup_verify_seconds)
        from .process_control import controlled_leaf_worker_proved

        controlled_leaf = controlled_leaf_worker_proved(
            store.read_spec(job_id), store.read_state(job_id), worker
        ) and not (current or captured.get("descendants"))
        if captured["worker"]["state"] == "uncertain":
            _record_blocker(
                store,
                job_id,
                request_id,
                identity,
                str(captured["worker"]["reason"]),
            )
            return None
        capture_complete = bool(
            captured.get(
                "capture_complete",
                captured["worker"].get("state") == "active",
            )
        )
        if controlled_leaf and not captured.get("descendants"):
            capture_complete = True
            captured["capture_complete"] = True
        previous_capture = cancel_evidence.get("descendant_capture")
        previous_complete_capture = bool(
            isinstance(previous_capture, dict)
            and isinstance(previous_capture.get("worker"), dict)
            and previous_capture["worker"].get("state") == "active"
            and previous_capture.get("capture_method", "live_enumeration") == "live_enumeration"
        )
        if not capture_complete and not process_tree_contained and not previous_complete_capture:
            _record_blocker(
                store,
                job_id,
                request_id,
                identity,
                str(
                    captured.get("reason")
                    or (
                        "worker exited before descendants were captured and no "
                        "process-tree containment is proved"
                    )
                ),
            )
            return None
        try:
            merged = _merge_process_identities(current, captured.get("descendants", []))
        except ValueError as exc:
            _record_blocker(store, job_id, request_id, identity, str(exc))
            return None
        history = list(cancel_evidence.get("descendant_captures") or [])
        capture_record = {
            **captured,
            "captured_at_epoch": time.time(),
            "capture_method": (
                "controlled_leaf_worker"
                if controlled_leaf
                else "live_enumeration"
                if capture_complete
                else "contained_worker_exit"
                if process_tree_contained
                else "worker_exit_after_live_capture"
            ),
            "process_tree_contained": process_tree_contained,
        }
        history.append(capture_record)
        # Retain the last proved live enumeration when an uncontained worker
        # exits. An incomplete exit observation is history, not a replacement
        # for the enumeration required by coordinator restart reconciliation.
        authoritative_capture = (
            previous_capture
            if not capture_complete and not process_tree_contained and previous_complete_capture
            else capture_record
        )
        if not _checkpoint(
            store,
            job_id,
            request_id,
            identity,
            phase,
            patch={
                "descendants": merged,
                "descendant_capture": authoritative_capture,
                "descendant_captures": history,
            },
        ):
            return None
        cancel_evidence["descendant_captures"] = history
        cancel_evidence["descendant_capture"] = authoritative_capture
        cancel_evidence["descendants"] = merged
        return merged

    refreshed = refresh_descendants(descendants, resume_phase)
    if refreshed is None:
        return 0
    descendants = refreshed

    if phase_hook is not None:
        phase_hook(resume_phase)

    if resume_phase not in _RESUMABLE_PHASES:
        grace_remaining = _remaining_phase_budget(
            cancel_evidence,
            "native_grace",
            grace_budget,
        )
        deadline = time.monotonic() + grace_remaining
        while time.monotonic() < deadline:
            if inspect_identity(worker)["state"] != "active" and not any(
                inspect_identity(descendant)["state"] == "active" for descendant in descendants
            ):
                if not _checkpoint(
                    store,
                    job_id,
                    request_id,
                    identity,
                    "verifying",
                    patch={"descendants": descendants},
                ):
                    return 0
                if phase_hook is not None:
                    phase_hook("verifying")
                verification = _verified_cancel(
                    store,
                    job_id,
                    _wait_for_process_absence(
                        [worker, *descendants],
                        cleanup_verify_seconds,
                    ),
                )
                if verification["absent"]:
                    return (
                        0
                        if _commit_cancelled(
                            store,
                            job_id,
                            request_id,
                            identity,
                            verification,
                            [],
                        )
                        else 1
                    )
                _record_blocker(
                    store,
                    job_id,
                    request_id,
                    identity,
                    str(
                        verification.get("solver", {}).get("reason")
                        or "worker exited during grace with uncertain descendants"
                    ),
                )
                return 0
            time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))

    captured = _capture_with_inspection_budget(worker, cleanup_verify_seconds)
    if captured["worker"]["state"] == "uncertain":
        _record_blocker(
            store,
            job_id,
            request_id,
            identity,
            str(captured["worker"]["reason"]),
        )
        return 0
    try:
        descendants = _merge_process_identities(
            descendants,
            captured.get("descendants", []),
        )
    except ValueError as exc:
        _record_blocker(store, job_id, request_id, identity, str(exc))
        return 0
    if (
        captured["worker"]["state"] != "active"
        and resume_phase not in {"terminate", "force_kill"}
        and not any(inspect_identity(descendant)["state"] == "active" for descendant in descendants)
    ):
        if not _checkpoint(
            store,
            job_id,
            request_id,
            identity,
            "verifying",
            patch={"descendants": descendants},
        ):
            return 0
        if phase_hook is not None:
            phase_hook("verifying")
        verification = _verified_cancel(
            store,
            job_id,
            _wait_for_process_absence(
                [worker, *descendants],
                cleanup_verify_seconds,
            ),
        )
        if verification["absent"]:
            return (
                0
                if _commit_cancelled(
                    store,
                    job_id,
                    request_id,
                    identity,
                    verification,
                    [],
                )
                else 1
            )
        _record_blocker(
            store,
            job_id,
            request_id,
            identity,
            str(verification.get("solver", {}).get("reason") or captured["worker"]["reason"]),
        )
        return 0
    actions = list(cancel_evidence.get("worker_actions") or [])
    if resume_phase != "verifying":
        if resume_phase != "force_kill":
            if not _checkpoint(
                store,
                job_id,
                request_id,
                identity,
                "terminate",
                patch={"descendants": descendants, "worker_actions": actions},
            ):
                return 0
            if phase_hook is not None:
                phase_hook("terminate")
            actions.append(terminate_exact(worker, force=False))
            if not _checkpoint(
                store, job_id, request_id, identity, "terminate", patch={"worker_actions": actions}
            ):
                return 0
            terminate_remaining = _remaining_phase_budget(
                store.read_state(job_id).get("cancel") or {},
                "terminate",
                terminate_budget,
            )
            wait_deadline = time.monotonic() + terminate_remaining
            while (
                time.monotonic() < wait_deadline and inspect_identity(worker)["state"] == "active"
            ):
                time.sleep(min(0.025, max(0.0, wait_deadline - time.monotonic())))
        if inspect_identity(worker)["state"] == "active":
            if not _checkpoint(
                store, job_id, request_id, identity, "force_kill", patch={"worker_actions": actions}
            ):
                return 0
            if phase_hook is not None:
                phase_hook("force_kill")
            actions.append(terminate_exact(worker, force=True))
            if not _checkpoint(
                store, job_id, request_id, identity, "force_kill", patch={"worker_actions": actions}
            ):
                return 0
    current_phase = str((store.read_state(job_id).get("cancel") or {}).get("phase") or resume_phase)
    refreshed = refresh_descendants(descendants, current_phase)
    if refreshed is None:
        return 0
    descendants = refreshed
    for descendant in descendants:
        if inspect_identity(descendant)["state"] == "active":
            actions.append(terminate_exact(descendant, force=True))
    if not _checkpoint(
        store,
        job_id,
        request_id,
        identity,
        "verifying",
        patch={"worker_actions": actions, "descendants": descendants},
    ):
        return 0
    if phase_hook is not None:
        phase_hook("verifying")
    verification = _verified_cancel(
        store,
        job_id,
        _wait_for_process_absence(
            [worker, *descendants],
            cleanup_verify_seconds,
        ),
    )
    if not verification["absent"]:
        _record_blocker(
            store,
            job_id,
            request_id,
            identity,
            str(
                verification.get("solver", {}).get("reason")
                or "worker or captured descendant identity remains active/uncertain"
            ),
        )
        return 0
    return (
        0
        if _commit_cancelled(
            store,
            job_id,
            request_id,
            identity,
            verification,
            actions,
        )
        else 1
    )


if __name__ == "__main__":
    code = run(sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]), float(sys.argv[5]))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
