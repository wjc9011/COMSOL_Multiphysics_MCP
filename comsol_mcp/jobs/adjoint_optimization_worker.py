"""Solver-free fake worker used to exercise adjoint durable lifecycle contracts."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .adjoint_rows import append_adjoint_row, read_adjoint_rows
from .process_control import contain_current_process_tree
from .store import JobStore, cancel_request_targets_attempt, process_identity


def _await_licensed_wall_watchdog(
    store: JobStore,
    job_id: str,
    spec: dict[str, Any],
    attempt: int,
    *,
    timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    """Fail closed before licensed startup unless this exact attempt is guarded."""
    import time

    from .store import read_json

    path = store.job_dir(job_id) / "wall-watchdog.json"
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            record = read_json(path)
        except FileNotFoundError, RuntimeError:
            record = None
        if isinstance(record, dict) and record.get("status") == "armed":
            state = store.read_state(job_id)
            target_worker = {
                "pid": state.get("worker_pid"),
                "process_create_time": state.get("worker_process_create_time"),
                "command_signature": state.get("worker_command_signature"),
            }
            budget = int(spec["optimizer"]["budget"]["max_wall_time_seconds"])
            if int(record.get("attempt", -1)) != int(attempt):
                raise RuntimeError("licensed adjoint wall watchdog attempt is not bound")
            if record.get("target_worker") != target_worker:
                raise RuntimeError("licensed adjoint wall watchdog worker identity is not bound")
            if int(record.get("budget_seconds", -1)) != budget:
                raise RuntimeError("licensed adjoint wall watchdog budget is not bound")
            expected_deadline = float(target_worker["process_create_time"]) + budget
            if float(record.get("deadline_epoch", 0.0)) != expected_deadline:
                raise RuntimeError("licensed adjoint wall watchdog deadline is not bound")
            if time.time() >= expected_deadline:
                raise RuntimeError("licensed adjoint wall watchdog deadline already expired")
            return record
        if isinstance(record, dict) and record.get("status") == "launch_failed":
            raise RuntimeError("licensed adjoint wall watchdog launch failed")
        if time.monotonic() >= deadline:
            raise RuntimeError("licensed adjoint wall watchdog was not armed before startup")
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))


def _run_native(root: str, job_id: str) -> int:
    store = JobStore(Path(root))
    directory = store.job_dir(job_id)
    spec = store.read_spec(job_id)
    attempt = int(store.read_state(job_id).get("attempt", 1))
    ownership = None
    lease_acquired = False
    completion_patch = None
    result_code = 1
    try:
        store.bind_worker_identity(job_id, process_identity(os.getpid()))
        store.update_state(
            job_id,
            patch={"process_tree_contained": bool(contain_current_process_tree())},
            event="worker_containment_recorded",
        )
        state = store.read_state(job_id)
        if state["status"] == "cancel_requested" or cancel_request_targets_attempt(
            store.read_control(job_id), attempt
        ):
            store.record_cooperative_cancel_observed(
                job_id, attempt=attempt, message="Stopped before native startup"
            )
            return 0
        if state["status"] == "submitted":
            store.update_state(job_id, "starting", event="worker_started")
        elif state["status"] != "starting":
            raise ValueError(f"adjoint native worker cannot start from {state['status']}")
        _await_licensed_wall_watchdog(store, job_id, spec, attempt)
        from comsol_mcp.tools.ownership import SolverOwnership

        ownership = SolverOwnership(store.root.parent, owner=f"job:{job_id}")
        source = Path(spec["source_model_path"])
        preflight = ownership.preflight(
            model_path=str(source),
            output_path=str(directory / "native-optimizer-receipt.json"),
            requested_version=spec["version"],
        )
        if not preflight.get("ready"):
            raise RuntimeError(f"Worker preflight failed: {preflight.get('blockers')}")
        claim = ownership.acquire(mode="durable-job", model_path=str(source))
        if not claim.get("success"):
            raise RuntimeError(claim.get("error", "solver ownership claim failed"))
        lease_acquired = True
        store.update_state(job_id, "smoke_running", event="adjoint_native_started")
        from .native_adjoint_runtime import execute_native_adjoint_optimization

        receipt = execute_native_adjoint_optimization(spec, directory)
        if not receipt.get("success"):
            raise RuntimeError("native adjoint runtime returned an unsuccessful receipt")
        receipt_fingerprint = domain_sha256_v2(
            "comsol_mcp.native_optimizer_runtime_receipt", receipt
        )
        gradient_fingerprint = domain_sha256_v2(
            "comsol_mcp.native_gradient_support", spec["support"]
        )
        candidate_fingerprint = domain_sha256_v2(
            "comsol_mcp.native_optimizer_candidate", receipt["final_variables_si"]
        )
        rows_path = directory / "optimization_rows.jsonl"
        append_adjoint_row(
            rows_path,
            job_fingerprint=spec["spec_fingerprint"],
            attempt=attempt,
            kind="gradient",
            payload={
                "iteration_id": "native-final",
                "gradient_fingerprint": gradient_fingerprint,
                "check_fingerprint": receipt_fingerprint,
                # Fresh forward validation does not check the native gradient.
                "evidence_state": "native_unchecked",
            },
        )
        iteration = append_adjoint_row(
            rows_path,
            job_fingerprint=spec["spec_fingerprint"],
            attempt=attempt,
            kind="iteration",
            payload={
                "iteration_id": "native-final",
                # This is the single retained final candidate, not a measured
                # count of native optimizer iterations.
                "iteration_index": 0,
                "candidate_fingerprint": candidate_fingerprint,
                "objective_value": receipt["fresh_forward_objective"],
                "status": "accepted",
                "gradient_fingerprint": gradient_fingerprint,
                "forward_fingerprint": receipt_fingerprint,
                "reason_code": "fresh_forward_validated",
            },
        )
        store.update_state(job_id, "smoke_validated", event="adjoint_native_validated")
        completion_patch = {
            "progress": {"completed": 1, "total": 1},
            "progress_unit": "validated_final_candidates",
            "native_iterations_completed": None,
            "solver_started": True,
            "native_optimizer_receipt_fingerprint": receipt_fingerprint,
            "last_iteration_row_sha256": iteration["row_sha256"],
            "source_unchanged": receipt["cleanup"]["source_unchanged"],
        }
        result_code = 0
    except Exception as exc:
        result_code = _observe_worker_failure(
            store,
            job_id,
            attempt,
            exc,
            stopping_message="Stopped during native optimization",
        )
    finally:
        cleanup_errors = []
        if ownership is not None and lease_acquired:
            try:
                release = ownership.release()
                if not isinstance(release, dict) or release.get("success") is not True:
                    cleanup_errors.append("lease_release_refused")
            except Exception as exc:
                cleanup_errors.append(f"lease_release:{type(exc).__name__}")
                print(f"lease release failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        if lease_acquired:
            patch = {
                "owned_cleanup": {"success": not cleanup_errors, "errors": cleanup_errors},
                "cleanup_pending": bool(cleanup_errors),
            }
            current = store.read_state(job_id)["status"]
            if completion_patch is not None and current == "smoke_validated":
                patch.update(completion_patch)
                if cleanup_errors:
                    patch["last_error"] = {
                        "type": "OwnedCleanupUnverified",
                        "message": "Native optimizer lease cleanup could not be verified.",
                    }
                store.update_state(
                    job_id,
                    "failed" if cleanup_errors else "completed",
                    patch=patch,
                    event="owned_cleanup_unverified" if cleanup_errors else "completed",
                )
            else:
                store.update_state(job_id, patch=patch, event="owned_cleanup_recorded")
            if cleanup_errors:
                result_code = 1
    return result_code


def _observe_worker_failure(
    store: JobStore, job_id: str, attempt: int, exc: Exception, *, stopping_message: str
) -> int:
    current = store.read_state(job_id)["status"]
    if current in {"cancel_requested", "cancelling"}:
        store.record_cooperative_cancel_observed(
            job_id,
            attempt=attempt,
            message=stopping_message,
            worker_error={"type": type(exc).__name__, "message": str(exc)[:2000]},
        )
        return 0
    if current not in {"completed", "failed", "interrupted"}:
        store.update_state(
            job_id,
            "failed",
            patch={"last_error": {"type": type(exc).__name__, "message": str(exc)[:2000]}},
            event="worker_failed",
        )
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    return 1


def run(root: str, job_id: str) -> int:
    store = JobStore(Path(root))
    directory = store.job_dir(job_id)
    spec = store.read_spec(job_id)
    if spec.get("job_type") != "adjoint_optimization":
        raise ValueError("adjoint worker accepts only adjoint_optimization jobs")
    if not spec.get("synthetic_mode"):
        return _run_native(root, job_id)
    attempt = int(store.read_state(job_id).get("attempt", 1))
    try:
        store.bind_worker_identity(job_id, process_identity(os.getpid()))
        store.update_state(
            job_id,
            patch={"process_tree_contained": bool(contain_current_process_tree())},
            event="worker_containment_recorded",
        )
        state = store.read_state(job_id)
        if state["status"] == "cancel_requested" or cancel_request_targets_attempt(
            store.read_control(job_id), attempt
        ):
            store.record_cooperative_cancel_observed(
                job_id, attempt=attempt, message="Stopped before startup"
            )
            return 0
        if state["status"] == "submitted":
            store.update_state(job_id, "starting", event="worker_started")
        elif state["status"] != "starting":
            raise ValueError(f"adjoint fake worker cannot start from {state['status']}")
        store.update_state(job_id, "smoke_running", event="adjoint_synthetic_started")
        rows_path = directory / "optimization_rows.jsonl"
        existing = [
            row
            for row in read_adjoint_rows(rows_path, job_fingerprint=spec["spec_fingerprint"])
            if row.get("attempt") == attempt
        ]
        # D-01: a crash between the two row appends must not duplicate the
        # already-persisted row on resume - only the MISSING kind is added.
        missing_kinds = {"gradient", "iteration"} - {row.get("kind") for row in existing}
        if missing_kinds:
            gradient_fp = domain_sha256_v2("comsol_mcp.synthetic_gradient", {"values": [0.0]})
            check_fp = domain_sha256_v2("comsol_mcp.synthetic_gradient_check", {"passed": True})
            if "gradient" in missing_kinds:
                append_adjoint_row(
                    rows_path,
                    job_fingerprint=spec["spec_fingerprint"],
                    attempt=attempt,
                    kind="gradient",
                    payload={
                        "iteration_id": "it-0",
                        "gradient_fingerprint": gradient_fp,
                        "check_fingerprint": check_fp,
                        "evidence_state": "gradient_validated",
                    },
                )
            if "iteration" in missing_kinds:
                append_adjoint_row(
                    rows_path,
                    job_fingerprint=spec["spec_fingerprint"],
                    attempt=attempt,
                    kind="iteration",
                    payload={
                        "iteration_id": "it-0",
                        "iteration_index": 0,
                        "candidate_fingerprint": domain_sha256_v2(
                            "comsol_mcp.synthetic_candidate", {"values": spec["initial_values"]}
                        ),
                        "objective_value": 0.0,
                        "status": "accepted",
                        "gradient_fingerprint": gradient_fp,
                        "forward_fingerprint": check_fp,
                        "reason_code": "synthetic_contract_only",
                    },
                )
        store.update_state(job_id, "smoke_validated", event="adjoint_synthetic_validated")
        store.update_state(
            job_id,
            "completed",
            patch={"progress": {"completed": 1, "total": 1}, "solver_started": False},
            event="completed",
        )
        return 0
    except Exception as exc:
        return _observe_worker_failure(
            store,
            job_id,
            attempt,
            exc,
            stopping_message="Stopped during synthetic validation",
        )


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1], sys.argv[2]))
