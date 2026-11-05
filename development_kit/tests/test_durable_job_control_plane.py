"""Process-only acceptance tests for the durable job control plane."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil
import pytest
import src.jobs.store as store_module
from src.jobs import cancel_worker, sequence_worker
from src.jobs import worker as production_worker
from src.jobs.manager import JobManager, validate_staged_sweep_spec
from src.jobs.store import (
    JobLock,
    JobStore,
    atomic_write_json,
    process_identity,
    process_identity_state,
    read_json,
)


def test_job_store_rejects_symlinked_job_directory(ascii_tmp_path):
    from development_kit.tests.platform_fixtures import create_directory_link, remove_directory_link

    outside = ascii_tmp_path / "outside"
    outside.mkdir()
    root = ascii_tmp_path / "jobs"
    store = JobStore(root)
    root.mkdir(parents=True, exist_ok=True)
    link = root / "job-linked"
    create_directory_link(str(outside), str(link))
    try:
        with pytest.raises(ValueError, match="symlink or junction"):
            store.job_dir("job-linked")
    finally:
        remove_directory_link(link)


@pytest.mark.parametrize("artifact", ["state.json", "control.json"])
def test_job_store_refuses_linked_state_and_control(ascii_tmp_path, artifact):
    store = JobStore(ascii_tmp_path / "jobs")
    job_id = store.create({"job_type": "test"}, {"status": "submitted"})
    target = ascii_tmp_path / "outside.json"
    target.write_text('{"status":"completed"}', encoding="utf-8")
    linked = store.job_dir(job_id) / artifact
    linked.unlink()
    try:
        linked.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    reader = store.read_state if artifact == "state.json" else store.read_control
    with pytest.raises(RuntimeError, match="Cannot read durable job artifact"):
        reader(job_id)
    assert target.read_text(encoding="utf-8") == '{"status":"completed"}'


@pytest.fixture()
def jobs_root(ascii_tmp_path):
    root = ascii_tmp_path / "jobs"
    root.mkdir()
    return root


@pytest.mark.parametrize(
    "field,value",
    [
        ("continue_on_error", "false"),
        ("record_wavelength_controls", "false"),
        ("smoke_points", True),
        ("parameter_values", [1.0, 1.0]),
    ],
)
def test_staged_spec_rejects_ambiguous_types_and_duplicate_points(tmp_path, field, value):
    source = tmp_path / "source.mph"
    source.write_bytes(b"immutable source")
    raw = {
        "job_type": "staged_sweep",
        "source_model_path": str(source),
        "parameter_name": "p",
        "parameter_values": [1.0, 2.0],
        "expressions": ["p"],
    }
    raw[field] = value
    with pytest.raises(ValueError, match=field):
        validate_staged_sweep_spec(raw)


def wait_for(manager: JobManager, job_id: str, statuses: set[str], timeout: float = 5.0):
    deadline = monoclock() + timeout
    while monoclock() < deadline:
        state = manager.status(job_id)
        if state["status"] in statuses:
            return state
        time.sleep(0.025)
    final = manager.status(job_id)
    try:
        tail = manager.tail(job_id, 50)
    except Exception as exc:
        tail = {"unavailable": type(exc).__name__}
    raise AssertionError(f"Job did not reach {statuses}: {final}; tail={tail}")


def test_sequence_resume_reconciles_progress_from_all_durable_rows(jobs_root):
    store = JobStore(jobs_root)
    identity = process_identity(os.getpid())
    now = time.time()
    spec = {"job_type": "test_sequence", "delays": [0.01, 0.01]}
    state = {
        "schema_version": "2",
        "status": "starting",
        "attempt": 2,
        "created_at_epoch": now,
        "updated_at_epoch": now,
        "worker_pid": identity["pid"],
        "worker_process_create_time": identity["process_create_time"],
        "worker_command_signature": identity["command_signature"],
        "progress": {"completed": 0, "total": 2},
        "last_error": None,
    }
    job_id = store.create(spec, state)
    (store.job_dir(job_id) / "results.csv").write_text(
        "index,status\n0,ok\n1,ok\n", encoding="utf-8"
    )

    assert sequence_worker._run(str(store.root), job_id) == 0
    final = store.read_state(job_id)
    assert final["status"] == "completed"
    assert final["progress"] == {"completed": 2, "total": 2}


@pytest.mark.parametrize(
    "payload,expected",
    [
        (b"index,status\n0,ok\n1,", {0}),
        (b"index,sta", set()),
    ],
)
def test_sequence_results_repair_only_an_unterminated_crash_tail(tmp_path, payload, expected):
    path = tmp_path / "results.csv"
    path.write_bytes(payload)

    assert sequence_worker._read_completed_results(path, total=2) == expected
    if expected:
        assert path.read_bytes().endswith(b"\n")
    else:
        assert path.read_bytes() == b""


@pytest.mark.parametrize(
    "payload",
    [
        b"index,wrong\n0,ok\n",
        b"index,status\nnot-an-index,ok\n",
        b"index,status\n1,ok\n",
    ],
)
def test_sequence_results_reject_complete_corrupt_records(tmp_path, payload):
    path = tmp_path / "results.csv"
    path.write_bytes(payload)

    with pytest.raises(ValueError, match="sequence result"):
        sequence_worker._read_completed_results(path, total=2)


def test_submit_returns_promptly_and_second_manager_observes_completion(jobs_root):
    first = JobManager(jobs_root, allow_test_jobs=True)
    started = time.monotonic()
    result = first.submit({"job_type": "test_sequence", "delays": [0.1, 0.1]})
    elapsed = time.monotonic() - started

    second = JobManager(jobs_root, allow_test_jobs=True)
    completed = wait_for(second, result["job_id"], {"completed"})

    assert elapsed < 5.0
    assert completed["progress"] == {"completed": 2, "total": 2}
    assert second.tail(result["job_id"], 2)["events"]


def test_detached_worker_survives_submitting_host_exit(jobs_root):
    script = (
        "import json; from src.jobs.manager import JobManager; "
        f"m=JobManager({str(jobs_root)!r}, allow_test_jobs=True); "
        "print(json.dumps(m.submit({'job_type':'test_sequence','delays':[0.2,0.2]})))"
    )
    completed_host = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    job_id = json.loads(completed_host.stdout.strip().splitlines()[-1])["job_id"]

    completed = wait_for(JobManager(jobs_root, allow_test_jobs=True), job_id, {"completed"})

    assert completed["progress"] == {"completed": 2, "total": 2}


def test_killed_worker_is_reconciled_as_interrupted(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True)
    result = manager.submit({"job_type": "test_sequence", "delays": [0.05, 10.0]})
    running = wait_for(manager, result["job_id"], {"running"})
    worker = psutil.Process(running["worker_pid"])
    worker.terminate()
    worker.wait(timeout=5)

    interrupted = wait_for(
        JobManager(jobs_root, allow_test_jobs=True), result["job_id"], {"interrupted"}
    )

    assert interrupted["last_error"]["type"] == "WorkerInterrupted"
    assert interrupted["progress"]["completed"] == 1


def test_killed_worker_resumes_durable_rows_without_duplication(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True)
    result = manager.submit({"job_type": "test_sequence", "delays": [0.05, 1.0, 0.05]})
    running = wait_for(manager, result["job_id"], {"running"})
    worker = psutil.Process(running["worker_pid"])
    worker.terminate()
    worker.wait(timeout=5)
    wait_for(manager, result["job_id"], {"interrupted"})

    resumed = manager.resume(result["job_id"])
    assert resumed["attempt"] == 2
    completed = wait_for(manager, result["job_id"], {"completed"}, timeout=15)
    with (manager.store.job_dir(result["job_id"]) / "results.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        indices = [int(row["index"]) for row in csv.DictReader(handle)]

    assert completed["progress"] == {"completed": 3, "total": 3}
    assert indices == [0, 1, 2]


def test_cooperative_cancel_is_truthful_and_resumable(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True)
    result = manager.submit({"job_type": "test_sequence", "delays": [0.05, 0.3]})
    wait_for(manager, result["job_id"], {"running"})

    requested = manager.cancel(result["job_id"])
    terminal = wait_for(manager, result["job_id"], {"cancelled"})

    assert requested["status"] == "cancel_requested"
    assert terminal["cancel"]["verification"]["absent"] is True
    assert terminal["cancel"]["cooperative_observation"]["target_attempt"] == 1
    timestamps = terminal["cancel"]["phase_timestamps"]
    assert set(timestamps) == {
        "requested",
        "native_grace",
        "verifying",
        "verified",
        "terminal_commit",
    }
    assert (
        timestamps["requested"]
        <= timestamps["native_grace"]
        <= timestamps["verifying"]
        <= timestamps["verified"]
        <= timestamps["terminal_commit"]
    )
    assert terminal["cancel"]["timing_policy"] == {
        "native_grace_budget_s": 10.0,
        "terminate_budget_s": 5.0,
        "cleanup_verification_budget_s": 5.0,
    }
    assert terminal["cancel"]["teardown_latency"]["requested_to_terminal_s"] >= 0
    assert terminal["cancel"]["teardown_latency"]["coordinator_to_terminal_s"] >= 0
    assert terminal["cancel"]["teardown_latency"]["verification_to_terminal_s"] >= 0
    manager.resume(result["job_id"])
    wait_for(manager, result["job_id"], {"completed"})


def test_repeated_and_concurrent_cancel_calls_share_one_attempt_bound_request(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True)
    result = manager.submit({"job_type": "test_sequence", "delays": [0.05, 1.0]})
    wait_for(manager, result["job_id"], {"running"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _value: manager.cancel(result["job_id"]), range(2)))

    assert first["success"] is True
    assert second["success"] is True
    assert first["request_id"] == second["request_id"]
    assert {first["idempotent"], second["idempotent"]} == {False, True}
    control = manager.store.read_control(result["job_id"])
    assert control["target_attempt"] == 1
    assert control["target_worker"]["pid"] is not None
    assert manager.store.read_state(result["job_id"])["cancel"]["request_id"] == first["request_id"]
    wait_for(manager, result["job_id"], {"cancelled"})


def test_completed_before_cancel_acquires_lock_has_no_control_side_effect(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True)
    result = manager.submit({"job_type": "test_sequence", "delays": [0.01]})
    wait_for(manager, result["job_id"], {"completed"})

    cancelled = manager.cancel(result["job_id"])

    assert cancelled["success"] is False
    assert manager.store.read_control(result["job_id"])["request"] is None
    assert manager.store.read_state(result["job_id"])["status"] == "completed"


def test_cancel_requested_state_cannot_be_overwritten_by_completed(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True)
    result = manager.submit({"job_type": "test_sequence", "delays": [0.05, 1.0]})
    wait_for(manager, result["job_id"], {"running"})
    manager.cancel(result["job_id"])

    with pytest.raises(ValueError, match="Invalid job state transition"):
        manager.store.update_state(result["job_id"], "completed")

    wait_for(manager, result["job_id"], {"cancelled"})


def test_stale_attempt_control_is_ignored_after_resume(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True)
    result = manager.submit({"job_type": "test_sequence", "delays": [0.05, 0.4]})
    wait_for(manager, result["job_id"], {"running"})
    first = manager.cancel(result["job_id"])
    wait_for(manager, result["job_id"], {"cancelled"})

    resumed = manager.resume(result["job_id"])
    manager.store.write_control(
        result["job_id"],
        "cancel_requested",
        fields={"request_id": first["request_id"], "target_attempt": 1},
    )
    completed = wait_for(manager, result["job_id"], {"completed"}, timeout=10)

    assert resumed["attempt"] == 2
    assert completed["progress"] == {"completed": 2, "total": 2}


def test_cancel_coordinator_refuses_stale_attempt_request(jobs_root):
    store = JobStore(jobs_root)
    identity = process_identity(os.getpid())
    job_id = store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "cancel_requested",
            "attempt": 2,
            "cancel": {
                "request_id": "cancel-attempt-1",
                "target_attempt": 1,
                "phase": "requested",
            },
        },
    )
    store.write_control(
        job_id,
        "cancel_requested",
        fields={"request_id": "cancel-attempt-1", "target_attempt": 1},
    )

    claimed = cancel_worker._claim(
        store,
        job_id,
        "cancel-attempt-1",
        identity,
        grace_seconds=1.0,
        terminate_seconds=1.0,
    )

    assert claimed is None
    assert store.read_state(job_id)["status"] == "cancel_requested"


def test_legacy_h1_cancel_control_migrates_to_an_idempotent_request(jobs_root):
    store = JobStore(jobs_root)
    identity = process_identity(os.getpid())
    job_id = store.create(
        {"schema_version": "1", "job_type": "test"},
        {
            "schema_version": "1",
            "status": "running",
            "attempt": 1,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
        },
    )
    store.write_control(job_id, "cancel_requested")

    migrated = store.request_cancel(job_id, requester_identity=identity)

    assert migrated["accepted"] is True
    assert migrated["idempotent"] is True
    assert migrated["control"]["target_attempt"] == 1
    assert migrated["control"]["request_id"].startswith("cancel-")


def test_post_create_launch_failure_retains_durable_job_identity(jobs_root, monkeypatch):
    from src.jobs.manager import JobLaunchError

    manager = JobManager(jobs_root, allow_test_jobs=True, reconcile_on_start=False)
    monkeypatch.setattr(
        manager,
        "_launch_worker",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("launch refused")),
    )

    with pytest.raises(JobLaunchError, match="launch refused") as caught:
        manager.submit({"job_type": "test_sequence", "delays": [0.01]})

    assert caught.value.cause_type == "RuntimeError"
    assert caught.value.state_record_error is None
    state = manager.store.read_state(caught.value.job_id)
    assert state["status"] == "failed"
    assert state["last_error"] == {
        "type": "RuntimeError",
        "message": "launch refused",
    }


def test_post_create_failure_keeps_job_identity_when_failure_recording_breaks(
    jobs_root,
    monkeypatch,
):
    from src.jobs.manager import JobLaunchError

    manager = JobManager(jobs_root, allow_test_jobs=True, reconcile_on_start=False)
    monkeypatch.setattr(
        manager,
        "_launch_worker",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("launch refused")),
    )
    monkeypatch.setattr(
        manager.store,
        "update_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("state write refused")),
    )

    with pytest.raises(JobLaunchError, match="launch refused") as caught:
        manager.submit({"job_type": "test_sequence", "delays": [0.01]})

    assert caught.value.state_record_error == "OSError: state write refused"
    assert manager.store.job_dir(caught.value.job_id).is_dir()


def test_unknown_control_request_fails_closed(jobs_root):
    store = JobStore(jobs_root)
    job_id = store.create(
        {"schema_version": "1", "job_type": "test"},
        {"schema_version": "1", "status": "running", "attempt": 1},
    )
    store.write_control(job_id, "unexpected")

    refused = store.request_cancel(job_id, requester_identity=process_identity(os.getpid()))

    assert refused["accepted"] is False
    assert refused["reason"] == "unknown_control_request"
    assert store.read_state(job_id)["status"] == "running"


def test_cooperative_cancel_observation_is_attempt_bound_and_nonterminal(jobs_root):
    store = JobStore(jobs_root)
    identity = process_identity(os.getpid())
    job_id = store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "running",
            "attempt": 1,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
        },
    )
    request = store.request_cancel(job_id, requester_identity=identity)

    observed = store.record_cooperative_cancel_observed(
        job_id,
        attempt=1,
        message="Stopped between points",
    )
    repeated = store.record_cooperative_cancel_observed(
        job_id,
        attempt=1,
        message="A repeated observation must not replace the first",
    )
    state = store.read_state(job_id)

    assert request["accepted"] is True
    assert observed["recorded"] is True
    assert observed["idempotent"] is False
    assert repeated["recorded"] is True
    assert repeated["idempotent"] is True
    assert state["status"] == "cancel_requested"
    assert state["cancel"]["phase"] == "requested"
    assert state["cancel"]["cooperative_observation"] == {
        "request_id": request["control"]["request_id"],
        "target_attempt": 1,
        "observed_at_epoch": state["cancel"]["cooperative_observation"]["observed_at_epoch"],
        "message": "Stopped between points",
        "worker": identity,
    }


def test_stale_attempt_cannot_record_cooperative_cancel_observation(jobs_root):
    store = JobStore(jobs_root)
    identity = process_identity(os.getpid())
    job_id = store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "cancel_requested",
            "attempt": 2,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
            "cancel": {"request_id": "cancel-old", "target_attempt": 1, "phase": "requested"},
        },
    )
    store.write_control(
        job_id,
        "cancel_requested",
        fields={"request_id": "cancel-old", "target_attempt": 1},
    )

    observed = store.record_cooperative_cancel_observed(
        job_id,
        attempt=2,
        message="Must be ignored",
    )

    assert observed == {
        "recorded": False,
        "reason": "no_matching_cancel_request",
        "state": observed["state"],
    }
    assert "cooperative_observation" not in store.read_state(job_id)["cancel"]


def test_status_preserves_matching_cancel_when_worker_identity_is_stale(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True, reconcile_on_start=False)
    identity = process_identity(os.getpid())
    job_id = manager.store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "running",
            "attempt": 1,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"] - 1000,
            "worker_command_signature": identity["command_signature"],
        },
    )
    manager.store.request_cancel(job_id, requester_identity=identity)

    observed = manager.status(job_id)

    assert observed["status"] == "cancel_requested"
    assert observed["worker_process_state"] == "stale"
    assert manager.store.read_state(job_id)["status"] == "cancel_requested"


def test_native_cancel_evidence_merges_without_overwriting_coordinator(jobs_root):
    store = JobStore(jobs_root)
    identity = process_identity(os.getpid())
    job_id = store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "running",
            "attempt": 1,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
        },
    )
    store.request_cancel(job_id, requester_identity=identity)
    coordinator = {"pid": 123, "process_create_time": 456.0, "command_signature": "coordinator"}
    store.update_state(
        job_id,
        "cancelling",
        patch={"cancel": {**store.read_state(job_id)["cancel"], "coordinator": coordinator}},
    )

    recorded = production_worker._record_native_cancel(
        store,
        job_id,
        1,
        {"attempted": True, "supported": True, "outcome": "returned"},
    )
    state = store.read_state(job_id)

    assert recorded is True
    assert state["cancel"]["coordinator"] == coordinator
    assert state["cancel"]["native"] == {
        "attempted": True,
        "supported": True,
        "outcome": "returned",
    }


def test_detached_coordinator_force_stops_exact_test_worker_and_allows_resume(jobs_root):
    manager = JobManager(
        jobs_root,
        allow_test_jobs=True,
        cancel_grace_seconds=0.1,
        cancel_terminate_seconds=0.1,
    )
    result = manager.submit({"job_type": "test_sequence", "delays": [0.05, 0.4]})
    running = wait_for(manager, result["job_id"], {"running"})
    worker_identity = {
        "pid": running["worker_pid"],
        "process_create_time": running["worker_process_create_time"],
        "command_signature": running["worker_command_signature"],
    }

    requested = manager.cancel(result["job_id"])
    cancelled = wait_for(manager, result["job_id"], {"cancelled"}, timeout=10)

    assert requested["request_id"] == cancelled["cancel"]["request_id"]
    assert process_identity_state(worker_identity)[0] == "stale"
    assert cancelled["cancel"]["verification"]["absent"] is True
    resumed = manager.resume(result["job_id"])
    assert resumed["attempt"] == 2
    wait_for(manager, result["job_id"], {"completed"}, timeout=10)


def test_startup_reconciliation_relaunches_only_existing_stale_cancel_request(
    jobs_root, monkeypatch
):
    manager = JobManager(jobs_root, allow_test_jobs=True)
    job_id = manager.store.create(
        {"schema_version": "2", "job_type": "test"},
        {"schema_version": "2", "status": "cancelling", "attempt": 1},
    )
    manager.store.write_control(
        job_id,
        "cancel_requested",
        fields={"request_id": "cancel-existing", "target_attempt": 1},
    )
    calls = []
    monkeypatch.setattr(
        manager, "_launch_cancel_coordinator", lambda jid, rid: calls.append((jid, rid))
    )

    assert manager.reconcile_cancellations() == 1
    assert calls == [(job_id, "cancel-existing")]


def test_orphan_reconciliation_commits_only_from_complete_cleanup_proof(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True, reconcile_on_start=False)
    identity = process_identity(os.getpid())
    stale_worker = {**identity, "process_create_time": identity["process_create_time"] - 1000}
    stale_coordinator = {**identity, "process_create_time": identity["process_create_time"] - 2000}
    job_id = manager.store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "cancelling",
            "attempt": 1,
            "worker_pid": stale_worker["pid"],
            "worker_process_create_time": stale_worker["process_create_time"],
            "worker_command_signature": stale_worker["command_signature"],
            "cancel": {
                "request_id": "cancel-orphan",
                "target_attempt": 1,
                "phase": "verifying",
                "phase_timestamps": {"requested": time.time() - 1, "verifying": time.time()},
                "coordinator": stale_coordinator,
                "descendants": [],
                "descendant_capture": {
                    "worker": {
                        "identity": identity,
                        "state": "active",
                        "reason": "captured while active",
                    },
                    "descendants": [],
                    "captured_at_epoch": time.time() - 0.5,
                },
            },
        },
    )
    manager.store.write_control(
        job_id,
        "cancel_requested",
        fields={
            "request_id": "cancel-orphan",
            "target_attempt": 1,
            "target_worker": stale_worker,
        },
    )

    assert manager.reconcile_cancellations() == 1
    terminal = wait_for(manager, job_id, {"cancelled"}, timeout=10)

    assert terminal["status"] == "cancelled"
    assert terminal["cancel"]["verification"]["absent"] is True
    assert len(terminal["cancel"]["verification"]["verdicts"]) == 1


def test_orphan_reconciliation_fails_closed_without_descendant_capture(jobs_root, monkeypatch):
    manager = JobManager(jobs_root, allow_test_jobs=True, reconcile_on_start=False)
    identity = process_identity(os.getpid())
    stale = {**identity, "process_create_time": identity["process_create_time"] - 1000}
    job_id = manager.store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "cancelling",
            "attempt": 1,
            "cancel": {
                "request_id": "cancel-missing-capture",
                "target_attempt": 1,
                "phase": "verifying",
                "coordinator": stale,
            },
        },
    )
    manager.store.write_control(
        job_id,
        "cancel_requested",
        fields={
            "request_id": "cancel-missing-capture",
            "target_attempt": 1,
            "target_worker": stale,
        },
    )
    launches = []
    monkeypatch.setattr(manager, "_launch_cancel_coordinator", lambda *args: launches.append(args))

    assert manager.reconcile_cancellations() == 0
    blocked = manager.store.read_state(job_id)

    assert blocked["status"] == "cancelling"
    assert blocked["cancel"]["reconciliation"]["outcome"] == "blocked_missing_descendant_capture"
    assert launches == []


def test_orphan_reconciliation_fails_closed_on_uncertain_identity(jobs_root, monkeypatch):
    manager = JobManager(jobs_root, allow_test_jobs=True, reconcile_on_start=False)
    identity = process_identity(os.getpid())
    job_id = manager.store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "cancelling",
            "attempt": 1,
            "cancel": {
                "request_id": "cancel-uncertain",
                "target_attempt": 1,
                "phase": "verifying",
                "coordinator": identity,
                "descendants": [],
                "descendant_capture": {
                    "worker": {
                        "identity": identity,
                        "state": "active",
                        "reason": "captured while active",
                    },
                    "descendants": [],
                },
            },
        },
    )
    manager.store.write_control(
        job_id,
        "cancel_requested",
        fields={
            "request_id": "cancel-uncertain",
            "target_attempt": 1,
            "target_worker": identity,
        },
    )
    monkeypatch.setattr(
        "src.jobs.manager.inspect_identity",
        lambda value: {"identity": value, "state": "uncertain", "reason": "access denied"},
    )
    launches = []
    monkeypatch.setattr(manager, "_launch_cancel_coordinator", lambda *args: launches.append(args))

    assert manager.reconcile_cancellations() == 0
    blocked = manager.store.read_state(job_id)

    assert blocked["status"] == "cancelling"
    assert blocked["cancel"]["reconciliation"]["outcome"] == "blocked_uncertain_identity"
    assert launches == []


@pytest.mark.parametrize("crash_phase", ["native_grace", "terminate", "force_kill", "verifying"])
def test_coordinator_loss_at_each_durable_phase_reconciles_safely(
    jobs_root, monkeypatch, crash_phase
):
    manager = JobManager(
        jobs_root,
        allow_test_jobs=True,
        cancel_grace_seconds=0.02,
        cancel_terminate_seconds=0.05,
    )
    result = manager.submit({"job_type": "test_sequence", "delays": [0.01, 10.0]})
    running = wait_for(manager, result["job_id"], {"running"})
    worker_identity = {
        "pid": running["worker_pid"],
        "process_create_time": running["worker_process_create_time"],
        "command_signature": running["worker_command_signature"],
    }

    normal_launcher = manager._launch_cancel_coordinator
    monkeypatch.setattr(manager, "_launch_cancel_coordinator", lambda *_args: None)
    requested = manager.cancel(result["job_id"])
    monkeypatch.setattr(manager, "_launch_cancel_coordinator", normal_launcher)

    crash_script = (
        "import os; import src.jobs.cancel_worker as c; "
        f"phase={crash_phase!r}; real=c.terminate_exact; "
        "c.terminate_exact=(lambda identity,force=False: real(identity,force=force) if force else "
        "{'acted':False,'reason':'test_noop_terminate'}) if phase=='force_kill' else real; "
        "hook=lambda current: os._exit(91) if current==phase else None; "
        f"c.run({str(jobs_root)!r},{result['job_id']!r},{requested['request_id']!r},0.02,0.05,phase_hook=hook)"
    )
    crashed = subprocess.Popen(
        [sys.executable, "-c", crash_script],
        cwd=Path(__file__).resolve().parents[2],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    crash_code = crashed.wait(timeout=5)
    if crash_code != 91:
        diagnostic_state = manager.store.read_state(result["job_id"])
        diagnostic_events = manager.tail(result["job_id"], 100)["events"]
        pytest.fail(
            f"coordinator did not crash at {crash_phase}: code={crash_code}, "
            f"phase={diagnostic_state.get('cancel', {}).get('phase')}, "
            f"events={diagnostic_events[-10:]}"
        )
    phase_state = manager.store.read_state(result["job_id"])
    assert phase_state["status"] == "cancelling"
    assert phase_state["cancel"]["phase"] == crash_phase
    entered_at = phase_state["cancel"]["phase_timestamps"][crash_phase]
    assert process_identity_state(phase_state["cancel"]["coordinator"])[0] == "stale"

    reconciliation_deadline = time.monotonic() + 5
    reconciled = 0
    while not reconciled and time.monotonic() < reconciliation_deadline:
        reconciled = manager.reconcile_cancellations()
        if not reconciled:
            time.sleep(0.025)
    assert reconciled == 1, json.dumps(manager.store.read_state(result["job_id"]), indent=2)
    cancelled = wait_for(manager, result["job_id"], {"cancelled"}, timeout=10)

    assert process_identity_state(worker_identity)[0] == "stale"
    assert cancelled["cancel"]["verification"]["absent"] is True
    assert cancelled["cancel"]["phase_timestamps"][crash_phase] == entered_at
    events = "\n".join(manager.tail(result["job_id"], 100)["events"])
    assert f'"phase": "{crash_phase}"' in events


def test_read_only_manager_construction_skips_startup_reconciliation(jobs_root, monkeypatch):
    def unexpected_reconciliation(_self, **_kwargs):
        raise AssertionError("read-only manager must not reconcile")

    monkeypatch.setattr(JobManager, "reconcile_cancellations", unexpected_reconciliation)
    JobManager(jobs_root, allow_test_jobs=True, reconcile_on_start=False)


def test_thirty_cancel_status_polling_races_have_no_false_terminal_state(jobs_root):
    iterations_env = "COMSOL_MCP_CANCELLATION_DETERMINISM_SOAK_ITERATIONS"
    artifact_root_env = "COMSOL_MCP_CANCELLATION_DETERMINISM_SOAK_ARTIFACT_ROOT"
    failure_root_env = "COMSOL_MCP_CANCELLATION_DETERMINISM_FAILURE_ROOT"
    iterations = int(os.environ.get(iterations_env, "30"))
    if iterations < 1 or iterations > 100:
        raise ValueError(f"{iterations_env} must be between 1 and 100")
    manager = JobManager(
        jobs_root,
        allow_test_jobs=True,
        cancel_grace_seconds=0.02,
        cancel_terminate_seconds=0.05,
    )
    calibration_count = min(5, iterations)
    deadline_s = 5.0
    latencies: list[float] = []
    records: list[dict[str, object]] = []
    summary_path = jobs_root / "cancellation_soak_summary.json"
    active_job_id = None
    try:
        for index in range(iterations):
            result = manager.submit({"job_type": "test_sequence", "delays": [0.01, 0.25]})
            active_job_id = result["job_id"]
            wait_for(manager, active_job_id, {"running"})
            requested_at = time.monotonic()
            request = manager.cancel(active_job_id)
            final = wait_for(manager, active_job_id, {"cancelled"}, timeout=deadline_s)
            latency = time.monotonic() - requested_at
            latencies.append(latency)
            records.append(
                {
                    "iteration": index + 1,
                    "job_id": active_job_id,
                    "request_id": request["request_id"],
                    "deadline_s": deadline_s,
                    "observed_latency_s": latency,
                    "status": final["status"],
                    "phase": final["cancel"].get("phase"),
                    "cleanup_absent": final["cancel"]["verification"]["absent"],
                    "teardown_latency": final["cancel"].get("teardown_latency"),
                }
            )
            assert final["cancel"]["verification"]["absent"] is True
            if len(latencies) == calibration_count:
                baseline_s = max(latencies)
                deadline_s = min(10.0, max(1.0, baseline_s * 4.0 + 0.5))
        summary = {
            "success": True,
            "iterations": iterations,
            "calibration_iterations": calibration_count,
            "baseline_max_s": max(latencies[:calibration_count]),
            "derived_deadline_s": deadline_s,
            "maximum_observed_latency_s": max(latencies),
            "records": records,
        }
        summary_path.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        artifact_root_value = os.environ.get(artifact_root_env)
        if artifact_root_value:
            artifact_root = Path(artifact_root_value)
            artifact_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(
                summary_path,
                artifact_root / f"soak-{jobs_root.name}.json",
            )
    except Exception as exc:
        summary = {
            "success": False,
            "iterations_requested": iterations,
            "iterations_completed": len(records),
            "active_job_id": active_job_id,
            "deadline_s": deadline_s,
            "error": f"{type(exc).__name__}: {exc}",
            "records": records,
            "active_state": manager.status(active_job_id) if active_job_id else None,
            "active_tail": manager.tail(active_job_id, 100) if active_job_id else None,
        }
        summary_path.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        archive_root = Path(
            os.environ.get(failure_root_env, str(jobs_root.parent / "cancellation-failures"))
        )
        archive_root.mkdir(parents=True, exist_ok=True)
        archive = archive_root / f"{jobs_root.name}-{int(time.time())}"
        archive.mkdir(parents=True, exist_ok=False)
        shutil.copy2(summary_path, archive / summary_path.name)
        try:
            shutil.copytree(jobs_root, archive / "jobs", dirs_exist_ok=True)
        except OSError as archive_exc:
            (archive / "archive_error.txt").write_text(
                f"{type(archive_exc).__name__}: {archive_exc}\n",
                encoding="utf-8",
            )
        message = (
            "cancellation determinism cancellation soak failed; "
            f"durable evidence archived at {archive}"
        )
        raise AssertionError(message) from exc


def test_completed_state_is_immutable(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True)
    result = manager.submit({"job_type": "test_sequence", "delays": [0.01]})
    wait_for(manager, result["job_id"], {"completed"})

    with pytest.raises(ValueError, match="Invalid job state transition"):
        manager.store.update_state(result["job_id"], "failed")
    with pytest.raises(ValueError, match="immutable"):
        manager.store.update_state(result["job_id"], patch={"last_error": {"message": "rewrite"}})


def test_state_patch_cannot_bypass_transition_validation(jobs_root):
    store = JobStore(jobs_root)
    job_id = store.create(
        {"schema_version": "1", "job_type": "test"},
        {"schema_version": "1", "status": "submitted"},
    )

    with pytest.raises(ValueError, match="new_status"):
        store.update_state(job_id, patch={"status": "completed"})

    assert store.read_state(job_id)["status"] == "submitted"


def test_lock_removes_only_proven_stale_identity(jobs_root):
    store = JobStore(jobs_root)
    job_id = store.create(
        {"schema_version": "1", "job_type": "test"},
        {"schema_version": "1", "status": "submitted"},
    )
    lock_path = store.job_dir(job_id) / ".state.lock"
    stale = process_identity(__import__("os").getpid())
    stale["process_create_time"] -= 1000
    atomic_write_json(lock_path, stale)

    with JobLock(lock_path, timeout=0.5):
        assert lock_path.exists()
    assert not lock_path.exists()


def test_atomic_state_replace_retries_transient_windows_file_lock(jobs_root, monkeypatch):
    path = jobs_root / "state.json"
    real_replace = store_module.os.replace
    calls = 0

    def flaky_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("simulated transient sharing violation")
        return real_replace(source, destination)

    monkeypatch.setattr(store_module.os, "replace", flaky_replace)

    atomic_write_json(path, {"status": "completed"})

    assert calls == 2
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "completed"


@pytest.mark.parametrize("error_type", [PermissionError, store_module.FileIdentityChangedError])
def test_durable_json_read_retries_transient_windows_file_lock(jobs_root, monkeypatch, error_type):
    path = jobs_root / "state.json"
    atomic_write_json(path, {"status": "completed"})
    real_read_bytes = store_module.read_file_bytes_bounded
    calls = 0

    def flaky_read_bytes(candidate, *args, **kwargs):
        nonlocal calls
        if candidate == path and calls < 2:
            calls += 1
            raise error_type("simulated transient read conflict")
        return real_read_bytes(candidate, *args, **kwargs)

    monkeypatch.setattr(store_module, "read_file_bytes_bounded", flaky_read_bytes)

    assert read_json(path)["status"] == "completed"
    assert calls == 2


def test_job_lock_release_retries_transient_windows_reader(jobs_root, monkeypatch):
    lock_path = jobs_root / ".state.lock"
    real_unlink = Path.unlink
    calls = 0

    def flaky_unlink(path, *args, **kwargs):
        nonlocal calls
        if path == lock_path:
            calls += 1
            if calls == 1:
                raise PermissionError("simulated polling reader sharing violation")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    with JobLock(lock_path, timeout=0.5):
        assert lock_path.exists()

    assert calls == 2
    assert not lock_path.exists()


def test_job_lock_acquire_retries_transient_windows_sharing_violation(jobs_root, monkeypatch):
    lock_path = jobs_root / ".state.lock"
    real_open = store_module.os.open
    calls = 0

    def flaky_open(path, flags, *args, **kwargs):
        nonlocal calls
        if Path(path) == lock_path:
            calls += 1
            if calls == 1:
                raise PermissionError(13, "simulated transient sharing violation", str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(store_module.os, "open", flaky_open)

    with JobLock(lock_path, timeout=0.5):
        assert lock_path.exists()

    assert calls == 2
    assert not lock_path.exists()


def test_job_lock_acquire_removes_owned_partial_publication(jobs_root, monkeypatch):
    lock_path = jobs_root / ".partial.lock"
    real_fsync = store_module.os.fsync

    def fail_lock_fsync(descriptor):
        if lock_path.exists():
            raise OSError("injected lock fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(store_module.os, "fsync", fail_lock_fsync)
    with pytest.raises(OSError, match="injected lock fsync failure"):
        JobLock(lock_path, timeout=0.5).acquire()
    assert not lock_path.exists()

    monkeypatch.setattr(store_module.os, "fsync", real_fsync)
    with JobLock(lock_path, timeout=0.5):
        assert lock_path.exists()


def test_job_lock_reclaims_aged_unparseable_crash_artifacts(ascii_tmp_path):
    lock_path = ascii_tmp_path / "wedged.lock"
    lock_path.write_bytes(b'{"partial":')
    old = time.time() - 30.0
    os.utime(lock_path, (old, old))

    with JobLock(lock_path, timeout=3.0):
        pass

    assert not lock_path.exists()


def test_job_lock_never_steals_a_fresh_partial_write_window(ascii_tmp_path):
    lock_path = ascii_tmp_path / "fresh.lock"
    lock_path.write_bytes(b'{"partial":')

    with pytest.raises(TimeoutError, match="durable job lock"):
        JobLock(lock_path, timeout=0.5).acquire()

    assert lock_path.read_bytes() == b'{"partial":'


def test_cancel_launch_failure_is_reported_and_next_idempotent_call_retries(jobs_root, monkeypatch):
    manager = JobManager(jobs_root, allow_test_jobs=True, reconcile_on_start=False)
    identity = process_identity(os.getpid())
    job_id = manager.store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "running",
            "attempt": 1,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
        },
    )
    monkeypatch.setattr(
        manager,
        "_launch_cancel_coordinator",
        lambda *_args: (_ for _ in ()).throw(OSError("injected launch failure")),
    )

    failed = manager.cancel(job_id)
    assert failed["success"] is False
    assert failed["error"] == "Cancellation coordinator could not be started"

    launches = []
    monkeypatch.setattr(manager, "_launch_cancel_coordinator", lambda *args: launches.append(args))
    retried = manager.cancel(job_id)
    assert retried["success"] is True
    assert retried["idempotent"] is True
    assert launches == [(job_id, failed["request_id"])]


def test_cancel_claim_rejects_a_malformed_attempt_without_crashing(jobs_root):
    store = JobStore(jobs_root)
    identity = process_identity(os.getpid())
    job_id = store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "cancel_requested",
            "attempt": [],
            "cancel": {"request_id": "cancel-malformed", "target_attempt": None},
        },
    )
    store.write_control(
        job_id,
        "cancel_requested",
        fields={"request_id": "cancel-malformed", "target_attempt": None},
    )

    assert (
        cancel_worker._claim(
            store,
            job_id,
            "cancel-malformed",
            identity,
            grace_seconds=0.1,
            terminate_seconds=0.1,
        )
        is None
    )


def test_job_lock_guard_prevents_replacement_during_release(jobs_root, monkeypatch):
    from threading import Event, Thread

    lock_path = jobs_root / ".state.lock"
    first = JobLock(lock_path, timeout=0.5)
    second = JobLock(lock_path, timeout=0.5)
    first.acquire()
    entered_unlink = Event()
    allow_unlink = Event()
    second_acquired = Event()
    second_attempted = Event()
    release_second = Event()
    original_unlink = Path.unlink

    def paused_unlink(path, *args, **kwargs):
        if path == lock_path and not entered_unlink.is_set():
            entered_unlink.set()
            assert allow_unlink.wait(timeout=1)
        return original_unlink(path, *args, **kwargs)

    def acquire_second():
        second_attempted.set()
        second.acquire()
        second_acquired.set()
        assert release_second.wait(timeout=1)
        second.release()

    monkeypatch.setattr(Path, "unlink", paused_unlink)
    first_release = Thread(target=first.release)
    second_thread = Thread(target=acquire_second)
    first_release.start()
    assert entered_unlink.wait(timeout=1)
    second_thread.start()
    assert second_attempted.wait(timeout=1)
    assert not second_acquired.wait(timeout=0.05)
    allow_unlink.set()
    first_release.join(timeout=2)
    assert second_acquired.wait(timeout=1)
    assert json.loads(lock_path.read_text(encoding="utf-8"))["pid"] == second.identity["pid"]
    release_second.set()
    second_thread.join(timeout=2)

    assert not lock_path.exists()


def test_job_lock_queues_same_process_contenders_before_file_guard(jobs_root):
    lock_path = jobs_root / ".state.lock"
    other_path = jobs_root / ".other.lock"
    first = JobLock(lock_path, timeout=0.5)
    contender = JobLock(lock_path, timeout=0.08, poll_interval=0.005)

    first.acquire()
    try:
        with JobLock(other_path, timeout=0.08):
            assert other_path.exists()
        with pytest.raises(TimeoutError, match="in-process durable job lock guard"):
            contender.acquire()
    finally:
        first.release()

    with contender:
        assert lock_path.exists()
    assert not lock_path.exists()
    assert not other_path.exists()


def test_live_pid_lock_with_malformed_creation_time_stays_bounded(jobs_root):
    lock_path = jobs_root / ".state.lock"
    malformed = process_identity(os.getpid())
    malformed["process_create_time"] = "not-a-time"
    atomic_write_json(lock_path, malformed)
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="Timed out waiting"):
        JobLock(lock_path, timeout=0.08, poll_interval=0.005).acquire()

    assert time.monotonic() - started < 0.5
    assert lock_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO lock boundary")
def test_job_lock_refuses_fifo_without_read_and_releases_guard(tmp_path, monkeypatch):
    lock_path = tmp_path / "job.lock"
    os.mkfifo(lock_path)
    lock = JobLock(lock_path, timeout=0.08)
    original_read = Path.read_bytes

    def guarded_read(path):
        assert path != lock_path, "A FIFO must not be read during lock recovery"
        return original_read(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", guarded_read)
        with pytest.raises(TimeoutError, match="not a regular file"):
            lock.acquire()

    assert lock_path.exists()
    assert lock._posix_guard is None
    assert not lock._process_guard_owned
    lock_path.unlink()
    with JobLock(lock_path, timeout=0.08):
        assert lock_path.is_file()
    assert not lock_path.exists()


def test_job_lock_read_failure_does_not_reclaim_unobserved_lock(jobs_root, monkeypatch):
    lock_path = jobs_root / "job.lock"
    lock_path.write_bytes(b"partial identity")
    os.utime(lock_path, (0, 0))
    lock = JobLock(lock_path, timeout=0.08, poll_interval=0.005)
    original_read = Path.read_bytes

    def interrupted_read(path):
        if path == lock_path:
            raise OSError("lock snapshot unavailable")
        return original_read(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", interrupted_read)
        with pytest.raises(TimeoutError, match="Timed out waiting"):
            lock.acquire()

    assert lock_path.read_bytes() == b"partial identity"
    assert not lock._process_guard_owned
    lock_path.unlink()
    with JobLock(lock_path, timeout=0.08):
        assert lock_path.exists()


def test_tail_does_not_allocate_an_entire_long_log_line(jobs_root):
    import tracemalloc

    store = JobStore(jobs_root)
    job_id = store.create({"job_type": "test"}, {"status": "submitted"})
    log = store.job_dir(job_id) / "worker.log"
    log.write_bytes(b"prefix\n" + b"x" * (16 * 1024 * 1024) + b"\r\nlast")
    tracemalloc.start()
    try:
        tail = store.tail(job_id, 2)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert tail["worker_log"] == ["x" * 65_536, "last"]
    assert peak < 8 * 1024 * 1024


def test_tail_is_bounded(jobs_root):
    store = JobStore(jobs_root)
    job_id = store.create(
        {"schema_version": "1", "job_type": "test"},
        {"schema_version": "1", "status": "submitted"},
    )
    for index in range(10):
        store.append_event(job_id, "line", {"index": index})

    tail = store.tail(job_id, 3)

    assert len(tail["events"]) == 3
    assert json.loads(tail["events"][-1])["data"]["index"] == 9


def test_production_schema_is_solver_free_and_guards_source(jobs_root):
    source = jobs_root / "baseline.mph"
    source.write_bytes(b"model")
    spec = validate_staged_sweep_spec(
        {
            "job_type": "staged_sweep",
            "source_model_path": str(source),
            "parameter_name": "wl",
            "parameter_values": [4.25, 4.251],
            "expressions": ["ewfd.Rtotal", "ewfd.Ttotal", "ewfd.Atotal"],
            "smoke_points": 1,
        }
    )

    assert len(spec["spec_fingerprint"]) == 64
    assert len(spec["source_model_sha256"]) == 64
    with pytest.raises(ValueError, match="Unsupported staged_sweep fields"):
        validate_staged_sweep_spec(
            {
                "job_type": "staged_sweep",
                "source_model_path": str(source),
                "parameter_name": "wl",
                "parameter_values": [4.25],
                "expressions": ["A"],
                "checkpoint_model_path": str(source),
            }
        )


def test_production_submit_uses_parameter_count_not_test_delays(jobs_root, monkeypatch):
    source = jobs_root / "baseline.mph"
    source.write_bytes(b"model")
    manager = JobManager(
        jobs_root,
        preflight=lambda **_kwargs: {"ready": True},
    )
    identity = process_identity(os.getpid())
    monkeypatch.setattr(manager, "_launch_worker", lambda *_args: identity)

    submitted = manager.submit(
        {
            "job_type": "staged_sweep",
            "source_model_path": str(source),
            "parameter_name": "wl",
            "parameter_values": [4.25, 4.251, 4.253],
            "expressions": ["A"],
        }
    )

    assert manager.store.read_state(submitted["job_id"])["progress"] == {
        "completed": 0,
        "total": 3,
    }


def test_resume_preflight_can_read_job_summaries_without_self_lock_delay(jobs_root, monkeypatch):
    source = jobs_root / "baseline.mph"
    source.write_bytes(b"model")
    manager = None

    def preflight(**_kwargs):
        assert manager is not None
        assert manager.summaries()["available"] is True
        return {"ready": True}

    manager = JobManager(jobs_root, preflight=preflight)
    spec = validate_staged_sweep_spec(
        {
            "job_type": "staged_sweep",
            "source_model_path": str(source),
            "parameter_name": "wl",
            "parameter_values": [4.25],
            "expressions": ["A"],
        }
    )
    job_id = manager.store.create(
        spec,
        {
            "schema_version": "1",
            "status": "interrupted",
            "attempt": 1,
            "progress": {"completed": 0, "total": 1},
        },
    )
    identity = process_identity(os.getpid())
    monkeypatch.setattr(manager, "_launch_worker", lambda *_args: identity)

    started = time.monotonic()
    result = manager.resume(job_id)

    assert time.monotonic() - started < 1.0
    assert result["attempt"] == 2


def test_resume_preflight_refusal_preserves_durable_state(jobs_root, monkeypatch):
    source = jobs_root / "baseline.mph"
    source.write_bytes(b"model")
    calls = []

    def preflight(**_kwargs):
        calls.append("preflight")
        return {"ready": False, "blockers": ["owner_busy"]}

    manager = JobManager(jobs_root, preflight=preflight)
    spec = validate_staged_sweep_spec(
        {
            "job_type": "staged_sweep",
            "source_model_path": str(source),
            "parameter_name": "wl",
            "parameter_values": [4.25],
            "expressions": ["A"],
        }
    )
    job_id = manager.store.create(
        spec,
        {
            "schema_version": "1",
            "status": "interrupted",
            "attempt": 1,
            "progress": {"completed": 0, "total": 1},
        },
    )
    root = manager.store.job_dir(job_id)
    before = {
        path.name: path.read_bytes()
        for path in root.iterdir()
        if path.suffix in {".json", ".jsonl"}
    }
    monkeypatch.setattr(manager, "_launch_worker", lambda *_args: calls.append("launch"))
    with pytest.raises(ValueError, match="owner_busy"):
        manager.resume(job_id)
    assert calls == ["preflight"]
    assert {
        path.name: path.read_bytes()
        for path in root.iterdir()
        if path.suffix in {".json", ".jsonl"}
    } == before


@pytest.mark.parametrize("during_preflight", [False, True])
@pytest.mark.parametrize(
    "job_type", ["staged_sweep", "convergence_campaign", "branch_continuation_campaign"]
)
def test_resume_rejects_changed_source_or_removed_job_without_state_change(
    jobs_root, monkeypatch, during_preflight, job_type
):
    source = jobs_root / "baseline.mph"
    source.write_bytes(b"model")
    child = validate_staged_sweep_spec(
        {
            "job_type": "staged_sweep",
            "source_model_path": str(source),
            "parameter_name": "wl",
            "parameter_values": [4.25],
            "expressions": ["A"],
        }
    )
    spec = child
    if job_type != "staged_sweep":
        from comsol_mcp.jobs.manager import _fingerprint

        key = "levels" if job_type == "convergence_campaign" else "states"
        first = jobs_root / "first.mph"
        first.write_bytes(b"model")
        spec = {
            "job_type": job_type,
            key: [
                {"spectral_job": {**child, "source_model_path": str(first)}},
                {"spectral_job": child},
            ],
        }
        spec["spec_fingerprint"] = _fingerprint(spec)
    calls = []

    def preflight(**_kwargs):
        calls.append("preflight")
        if during_preflight:
            source.write_bytes(b"changed")
        return {"ready": True}

    manager = JobManager(jobs_root, preflight=preflight)
    job_id = manager.store.create(
        spec,
        {
            "schema_version": "1",
            "status": "interrupted",
            "attempt": 1,
            "progress": {"completed": 0, "total": 1},
        },
    )
    before = manager.store.read_state(job_id)
    monkeypatch.setattr(manager, "_launch_worker", lambda *_args: calls.append("launch"))
    if not during_preflight:
        source.write_bytes(b"changed")
    rejection = "source SHA-256" if job_type == "staged_sweep" else "removed in 0.8.0"
    with pytest.raises(ValueError, match=rejection):
        manager.resume(job_id)
    assert calls == (["preflight"] if during_preflight and job_type == "staged_sweep" else [])
    assert manager.store.read_state(job_id) == before


@pytest.mark.parametrize("mutation_stage", ["preflight", "client_startup", "model_load"])
def test_owned_worker_rechecks_source_after_mutating_startup(
    jobs_root, monkeypatch, mutation_stage
):
    from contextlib import nullcontext

    import src.tools.ownership as ownership_module

    source = jobs_root / "baseline.mph"
    source.write_bytes(b"model")
    spec = validate_staged_sweep_spec(
        {
            "job_type": "staged_sweep",
            "source_model_path": str(source),
            "parameter_name": "wl",
            "parameter_values": [4.25],
            "expressions": ["A"],
        }
    )
    store = JobStore(jobs_root)
    job_id = store.create(
        spec,
        {
            "schema_version": "1",
            "status": "submitted",
            "attempt": 1,
            "progress": {"completed": 0, "total": 1},
        },
    )
    calls = []

    class Ownership:
        def __init__(self, *_args, **_kwargs):
            pass

        def preflight(self, **_kwargs):
            if mutation_stage == "preflight":
                source.write_bytes(b"changed")
            return {"ready": True}

        def acquire(self, **_kwargs):
            calls.append("acquire")
            return {"success": True}

        def heartbeat(self, **_kwargs):
            return True

        def release(self):
            return {"success": True}

    class Client:
        port = None

        def __init__(self, **_kwargs):
            calls.append("client")
            if mutation_stage == "client_startup":
                source.write_bytes(b"changed")

        def load(self, _path):
            calls.append("load")
            if mutation_stage == "model_load":
                source.write_bytes(b"changed")
                assert Path(_path) != source
                assert Path(_path).read_bytes() == b"model"
                raise RuntimeError("verified snapshot load")
            raise AssertionError("Changed source must never load")

        def clear(self):
            pass

    # Model a platform where read pins detect writes but cannot exclude them.
    monkeypatch.setattr(production_worker, "pin_validated_reads", lambda *_args: nullcontext())
    monkeypatch.setattr(ownership_module, "SolverOwnership", Ownership)
    monkeypatch.setitem(sys.modules, "mph", types.SimpleNamespace(Client=Client))
    assert production_worker.run(str(jobs_root), job_id) == 1
    assert calls == (
        []
        if mutation_stage == "preflight"
        else ["acquire", "client", "load"]
        if mutation_stage == "model_load"
        else ["acquire", "client"]
    )
    state = store.read_state(job_id)
    assert state["status"] == "failed"
    assert (
        "verified snapshot load" if mutation_stage == "model_load" else "source SHA-256 changed"
    ) in state["last_error"]["message"]
    assert list(store.job_dir(job_id).glob("worker-source-*")) == []


def test_test_jobs_require_explicit_injection(jobs_root):
    with pytest.raises(ValueError, match="disabled"):
        JobManager(jobs_root).submit({"job_type": "test_sequence", "delays": [0.01]})


@pytest.mark.parametrize(
    "cleanup_failure", [None, "release_raises", "release_refuses", "clear", "disconnect"]
)
def test_production_worker_bridges_smoke_broad_and_lease_with_mocks(
    jobs_root, monkeypatch, cleanup_failure
):
    import src.tools.ownership as ownership_module
    import src.tools.workflow as workflow_module

    store = JobStore(jobs_root)
    identity = process_identity(os.getpid())
    spec = {
        "schema_version": "1",
        "spec_fingerprint": "mock-config",
        "job_type": "staged_sweep",
        "source_model_path": str(jobs_root / "source.mph"),
        "source_model_sha256": hashlib.sha256(b"mock").hexdigest(),
        "parameter_name": "wl",
        "parameter_values": [1.0, 2.0],
        "expressions": ["A"],
        "smoke_points": 1,
    }
    Path(spec["source_model_path"]).write_bytes(b"mock")
    state = {
        "schema_version": "1",
        "status": "submitted",
        "attempt": 1,
        "worker_pid": identity["pid"],
        "worker_process_create_time": identity["process_create_time"],
        "worker_command_signature": identity["command_signature"],
        "progress": {"completed": 0, "total": 2},
    }
    job_id = store.create(spec, state)
    lease_events = []

    class FakeOwnership:
        def __init__(self, *_args, **_kwargs):
            pass

        def preflight(self, **_kwargs):
            return {"ready": True}

        def acquire(self, **_kwargs):
            lease_events.append("acquired")
            return {"success": True}

        def heartbeat(self, **_kwargs):
            lease_events.append("heartbeat")
            return True

        def release(self):
            lease_events.append("released")
            assert store.read_state(job_id)["status"] != "completed"
            if cleanup_failure == "release_raises":
                raise RuntimeError("injected ownership release failure")
            if cleanup_failure == "release_refuses":
                return {"success": False}
            return {"success": True}

    class FakeClient:
        def __init__(self, **_kwargs):
            self.disconnected = False
            self.cleared = False
            self.port = 2030 if cleanup_failure == "disconnect" else None

        def load(self, _path):
            return object()

        def disconnect(self):
            if cleanup_failure == "disconnect":
                raise RuntimeError("injected disconnect failure")
            self.disconnected = True

        def clear(self):
            if cleanup_failure == "clear":
                raise RuntimeError("injected clear failure")
            self.cleared = True

    def fake_runner(_model, _parameter, values, _expressions, **kwargs):
        output = Path(kwargs["csv_path"])
        existing = []
        if output.exists() and output.stat().st_size:
            with output.open(newline="", encoding="utf-8") as handle:
                existing = list(csv.DictReader(handle))
        completed = {row["parameter_value"] for row in existing if row["status"] == "ok"}
        limit = kwargs.get("max_new_points")
        added = 0
        for value in values:
            token = str(value)
            if token in completed or (limit is not None and added >= limit):
                continue
            row = {
                "config_id": "mock-config",
                "parameter_value": token,
                "status": "ok",
            }
            with output.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(row))
                if handle.tell() == 0:
                    writer.writeheader()
                writer.writerow(row)
                handle.flush()
                os.fsync(handle.fileno())
            kwargs["on_durable_row"](row)
            added += 1
        return {"success": True, "stop_reason": None}

    monkeypatch.setattr(ownership_module, "SolverOwnership", FakeOwnership)
    monkeypatch.setattr(workflow_module, "run_staged_parametric_sweep", fake_runner)
    monkeypatch.setitem(sys.modules, "mph", types.SimpleNamespace(Client=FakeClient))

    code = production_worker.run(str(jobs_root), job_id)

    assert code == (0 if cleanup_failure is None else 1)
    state = store.read_state(job_id)
    assert state["status"] == ("completed" if cleanup_failure is None else "failed")
    assert state["owned_cleanup"]["success"] is (cleanup_failure is None)
    expected_errors = {
        None: [],
        "release_raises": ["lease_release:RuntimeError"],
        "release_refuses": ["lease_release_refused"],
        "clear": ["client_clear:RuntimeError"],
        "disconnect": ["client_disconnect:RuntimeError"],
    }
    assert state["owned_cleanup"]["errors"] == expected_errors[cleanup_failure]
    assert state["cleanup_pending"] is (cleanup_failure is not None)
    if cleanup_failure is not None:
        assert state["last_error"]["type"] == "OwnedCleanupUnverified"
    events = [
        json.loads(line)
        for line in (store.job_dir(job_id) / "events.jsonl").read_text().splitlines()
    ]
    assert any(event["event"] == "completed" for event in events) is (cleanup_failure is None)
    assert store.read_state(job_id)["progress"] == {"completed": 2, "total": 2}
    with (store.job_dir(job_id) / "results.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [(row["parameter_value"], row["status"]) for row in rows] == [
        ("1.0", "ok"),
        ("2.0", "ok"),
    ]
    assert lease_events[0] == "acquired"
    assert lease_events[-1] == "released"


def test_production_worker_rejects_failed_heartbeat_before_model_load(jobs_root, monkeypatch):
    import src.tools.ownership as ownership_module

    store = JobStore(jobs_root)
    identity = process_identity(os.getpid())
    source = jobs_root / "source.mph"
    source.write_bytes(b"mock")
    job_id = store.create(
        {
            "schema_version": "1",
            "spec_fingerprint": "heartbeat-config",
            "job_type": "staged_sweep",
            "source_model_path": str(source),
            "source_model_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "parameter_name": "wl",
            "parameter_values": [1.0],
            "expressions": ["A"],
            "smoke_points": 1,
        },
        {
            "schema_version": "1",
            "status": "submitted",
            "attempt": 1,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
            "progress": {"completed": 0, "total": 1},
        },
    )
    loads = []

    class FailedHeartbeatOwnership:
        def __init__(self, *_args, **_kwargs):
            pass

        def preflight(self, **_kwargs):
            return {"ready": True}

        def acquire(self, **_kwargs):
            return {"success": True}

        def heartbeat(self, **_kwargs):
            return False

        def release(self):
            return {"success": True}

    class FakeClient:
        port = None

        def __init__(self, **_kwargs):
            pass

        def load(self, _path):
            loads.append(_path)
            return object()

        def clear(self):
            pass

    monkeypatch.setattr(ownership_module, "SolverOwnership", FailedHeartbeatOwnership)
    monkeypatch.setitem(sys.modules, "mph", types.SimpleNamespace(Client=FakeClient))

    code = production_worker.run(str(jobs_root), job_id)

    state = store.read_state(job_id)
    assert code == 1
    assert loads == []
    assert state["status"] == "failed"
    assert state["last_error"]["type"] == "RuntimeError"
    assert "heartbeat failed before model load" in state["last_error"]["message"]


def test_production_worker_resource_gate_interrupts_before_second_solve(jobs_root, monkeypatch):
    import src.jobs.resource_admission as resource_module
    import src.tools.ownership as ownership_module
    import src.tools.workflow as workflow_module

    store = JobStore(jobs_root)
    identity = process_identity(os.getpid())
    spec = {
        "schema_version": "1",
        "spec_fingerprint": "mock-resource-config",
        "job_type": "staged_sweep",
        "source_model_path": str(jobs_root / "source.mph"),
        "source_model_sha256": hashlib.sha256(b"mock").hexdigest(),
        "parameter_name": "wl",
        "parameter_values": [1.0, 2.0],
        "expressions": ["A"],
        "smoke_points": 1,
        "resource_policy": {
            "available_memory_refuse_fraction": 0.2,
        },
    }
    Path(spec["source_model_path"]).write_bytes(b"mock")
    job_id = store.create(
        spec,
        {
            "schema_version": "1",
            "status": "submitted",
            "attempt": 1,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
            "progress": {"completed": 0, "total": 2},
        },
    )
    lease_events = []

    class FakeOwnership:
        def __init__(self, *_args, **_kwargs):
            pass

        def preflight(self, **_kwargs):
            return {"ready": True}

        def acquire(self, **_kwargs):
            lease_events.append("acquired")
            return {"success": True}

        def heartbeat(self, **_kwargs):
            return True

        def release(self):
            lease_events.append("released")
            return {"success": True}

    class FakeClient:
        port = None

        def __init__(self, **_kwargs):
            pass

        def load(self, _path):
            return types.SimpleNamespace()

        def clear(self):
            pass

    solved = []

    def fake_runner(_model, parameter, values, _expressions, **kwargs):
        output = Path(kwargs["csv_path"])
        existing = []
        if output.exists() and output.stat().st_size:
            with output.open(newline="", encoding="utf-8") as handle:
                existing = list(csv.DictReader(handle))
        completed = {row["parameter_value"] for row in existing if row["status"] == "ok"}
        processed = 0
        stop_reason = None
        for value in values:
            token = str(value)
            if token in completed:
                continue
            if kwargs.get("max_new_points") is not None and processed >= kwargs["max_new_points"]:
                stop_reason = "max_new_points"
                break
            point_id = workflow_module._sweep_point_id(parameter, token)
            action = kwargs["before_point_hook"](
                {
                    "stage": "pre_solve",
                    "point_id": point_id,
                    "config_id": "mock-resource-config",
                }
            )["action"]
            if action == "checkpoint_no_start":
                stop_reason = "before_point_checkpoint_no_start"
                break
            row = {
                "config_id": "mock-resource-config",
                "parameter_value": token,
                "status": "ok",
            }
            with output.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(row))
                if handle.tell() == 0:
                    writer.writeheader()
                writer.writerow(row)
                handle.flush()
                os.fsync(handle.fileno())
            solved.append(token)
            kwargs["on_durable_row"](row)
            kwargs["after_durable_row_hook"](
                {
                    "stage": "post_solve",
                    "point_id": point_id,
                    "config_id": "mock-resource-config",
                }
            )
            processed += 1
        return {
            "success": True,
            "stop_reason": stop_reason,
            "stopped_early": stop_reason is not None,
        }

    samples = [
        ("pre_solve", 90),
        ("post_solve", 90),
        ("pre_solve", 10),
    ]

    def fake_collect(*, stage, **_kwargs):
        expected_stage, available = samples.pop(0)
        assert stage == expected_stage
        return {
            "stage": stage,
            "observed_at_epoch": 1000.0 + len(solved),
            "available_memory_bytes": available,
            "total_memory_bytes": 100,
        }

    monkeypatch.setattr(ownership_module, "SolverOwnership", FakeOwnership)
    monkeypatch.setattr(workflow_module, "run_staged_parametric_sweep", fake_runner)
    monkeypatch.setattr(resource_module, "collect_resource_telemetry", fake_collect)
    monkeypatch.setattr("src.tools.mesh.get_mesh_info", lambda _model: {"success": False})
    monkeypatch.setitem(sys.modules, "mph", types.SimpleNamespace(Client=FakeClient))

    code = production_worker.run(str(jobs_root), job_id)

    state = store.read_state(job_id)
    assert code == 0
    assert solved == ["1.0"]
    assert state["status"] == "interrupted"
    assert state["last_error"]["type"] == "ResourceAdmissionStop"
    assert state["resource_gate"]["stop_reason"] == "before_point_checkpoint_no_start"
    assert state["progress"] == {"completed": 1, "total": 2}
    assert len(store.read_resource_journal(job_id)) == 6
    assert lease_events == ["acquired", "released"]

    store.update_state(
        job_id,
        "starting",
        patch={
            "attempt": 2,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
            "last_error": None,
        },
        event="resume_requested",
    )
    samples.extend(
        [
            ("pre_solve", 90),
            ("post_solve", 90),
        ]
    )

    resumed_code = production_worker.run(str(jobs_root), job_id)

    resumed_state = store.read_state(job_id)
    assert resumed_code == 0
    assert solved == ["1.0", "2.0"]
    assert resumed_state["status"] == "completed"
    assert resumed_state["progress"] == {"completed": 2, "total": 2}
    journal = store.read_resource_journal(job_id)
    assert len(journal) == 10
    assert [entry["attempt"] for entry in journal] == [1] * 6 + [2] * 4
    assert lease_events == ["acquired", "released", "acquired", "released"]


def test_worker_can_bind_its_own_identity_before_manager_acknowledgement(jobs_root):
    store = JobStore(jobs_root)
    job_id = store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "submitted",
            "attempt": 1,
            "worker_pid": None,
            "worker_process_create_time": None,
            "worker_command_signature": None,
        },
    )
    identity = process_identity(os.getpid())

    first = store.bind_worker_identity(job_id, identity)
    second = store.bind_worker_identity(job_id, identity)

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    state = store.read_state(job_id)
    assert state["worker_pid"] == identity["pid"]
    with pytest.raises(RuntimeError, match="another worker"):
        store.bind_worker_identity(job_id, {**identity, "pid": identity["pid"] + 1})


def test_failed_job_initialization_never_publishes_a_partial_job_directory(jobs_root, monkeypatch):
    store = JobStore(jobs_root)
    real_write = store_module.atomic_write_json
    calls = 0

    def fail_second_write(path, value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected initialization failure")
        real_write(path, value)

    monkeypatch.setattr(store_module, "atomic_write_json", fail_second_write)
    with pytest.raises(OSError, match="initialization failure"):
        store.create({}, {"status": "submitted"}, job_id="job-incomplete")

    assert not store.job_dir("job-incomplete").exists()
    assert not list(jobs_root.glob(".job-incomplete.*.tmp"))


def test_worker_observation_reconciles_control_first_cancel_crash(jobs_root, monkeypatch):
    store = JobStore(jobs_root)
    identity = process_identity(os.getpid())
    job_id = store.create(
        {},
        {
            "schema_version": "2",
            "status": "running",
            "attempt": 1,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
        },
    )
    real_write = store_module.atomic_write_json
    failed = False

    def fail_cancel_state(path, value):
        nonlocal failed
        if path.name == "state.json" and value.get("status") == "cancel_requested" and not failed:
            failed = True
            raise OSError("injected state projection failure")
        real_write(path, value)

    monkeypatch.setattr(store_module, "atomic_write_json", fail_cancel_state)
    with pytest.raises(OSError, match="state projection failure"):
        store.request_cancel(job_id, requester_identity=identity)
    monkeypatch.setattr(store_module, "atomic_write_json", real_write)

    assert store.read_state(job_id)["status"] == "running"
    assert store.read_control(job_id)["request"] == "cancel_requested"
    observed = store.record_cooperative_cancel_observed(
        job_id, attempt=1, message="worker observed recovered request"
    )
    assert observed["recorded"] is True
    assert observed["state"]["status"] == "cancel_requested"


def test_hidden_job_staging_directory_is_not_visible_to_manager_scans(jobs_root, monkeypatch):
    staging = jobs_root / ".job-unpublished.injected.tmp"
    staging.mkdir()
    atomic_write_json(staging / "state.json", {"status": "cancel_requested"})
    atomic_write_json(
        staging / "control.json",
        {"request": "cancel_requested", "request_id": "cancel-unpublished"},
    )
    manager = JobManager(jobs_root, allow_test_jobs=True, reconcile_on_start=False)

    def fail_if_locked(*_args, **_kwargs):
        raise AssertionError("unpublished staging directory was treated as a job")

    monkeypatch.setattr(manager.store, "lock", fail_if_locked)

    assert manager.reconcile_cancellations() == 0
    assert manager.summaries()["count_returned"] == 0


def test_sequence_worker_refuses_mismatched_exact_identity_before_state_change(
    jobs_root, monkeypatch
):
    store = JobStore(jobs_root)
    identity = process_identity(os.getpid())
    now = time.time()
    job_id = store.create(
        {"job_type": "test_sequence", "delays": [0.01]},
        {
            "schema_version": "2",
            "status": "submitted",
            "attempt": 1,
            "created_at_epoch": now,
            "updated_at_epoch": now,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"] - 1000.0,
            "worker_command_signature": identity["command_signature"],
            "progress": {"completed": 0, "total": 1},
            "last_error": None,
        },
    )
    monkeypatch.setattr(sequence_worker, "contain_current_process_tree", lambda: True)

    with pytest.raises(RuntimeError, match="another worker"):
        sequence_worker._run(str(store.root), job_id)

    state = store.read_state(job_id)
    assert state["status"] == "submitted"
    assert not (store.job_dir(job_id) / "results.csv").exists()


def test_sequence_transition_error_remains_bound_to_concurrent_cancel(jobs_root, monkeypatch):
    store = JobStore(jobs_root)
    identity = process_identity(os.getpid())
    now = time.time()
    job_id = store.create(
        {"job_type": "test_sequence", "delays": [0.01]},
        {
            "schema_version": "2",
            "status": "starting",
            "attempt": 1,
            "created_at_epoch": now,
            "updated_at_epoch": now,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
            "progress": {"completed": 0, "total": 1},
            "last_error": None,
        },
    )
    real_update = JobStore.update_state

    def cancel_before_smoke(self, target_job_id, *args, **kwargs):
        if target_job_id == job_id and kwargs.get("event") == "smoke_started":
            self.request_cancel(job_id, requester_identity=identity)
        return real_update(self, target_job_id, *args, **kwargs)

    monkeypatch.setattr(JobStore, "update_state", cancel_before_smoke)
    code = sequence_worker.run(str(store.root), job_id)

    state = store.read_state(job_id)
    assert code == 1
    assert state["status"] == "cancel_requested"
    assert state["cancel"]["worker_error"]["type"] == "ValueError"
    assert "Invalid job state transition" in state["cancel"]["worker_error"]["message"]


def test_default_reconciliation_includes_an_older_accepted_cancellation(jobs_root, monkeypatch):
    manager = JobManager(jobs_root, reconcile_on_start=False)
    identity = process_identity(os.getpid())
    old_job_id = manager.store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "running",
            "attempt": 1,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
        },
    )
    request = manager.store.request_cancel(old_job_id, requester_identity=identity)
    for index in range(21):
        manager.store.create(
            {"schema_version": "2", "job_type": "test", "ordinal": index},
            {"schema_version": "2", "status": "completed", "attempt": 1},
        )
    launches = []
    monkeypatch.setattr(
        manager,
        "_launch_cancel_coordinator",
        lambda job_id, request_id: launches.append((job_id, request_id)),
    )

    assert manager.reconcile_cancellations() == 1
    assert launches == [(old_job_id, request["control"]["request_id"])]


def test_fixture_cleanup_terminates_an_exact_detached_worker(jobs_root):
    child_code = (
        "import pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text('ready', encoding='ascii'); time.sleep(30)"
    )
    worker_ready = jobs_root / "worker.ready"
    coordinator_ready = jobs_root / "coordinator.ready"
    worker = subprocess.Popen(
        [sys.executable, "-c", child_code, str(worker_ready)],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    coordinator = subprocess.Popen(
        [sys.executable, "-c", child_code, str(coordinator_ready)],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        deadline = time.monotonic() + 5.0
        while not (worker_ready.exists() and coordinator_ready.exists()):
            assert worker.poll() is None and coordinator.poll() is None
            assert time.monotonic() < deadline, "fixture children did not become ready"
            time.sleep(0.025)
        identity = process_identity(worker.pid)
        coordinator_identity = process_identity(coordinator.pid)
        JobStore(jobs_root).create(
            {"schema_version": "2", "job_type": "test"},
            {
                "schema_version": "2",
                "status": "running",
                "attempt": 1,
                "worker_pid": identity["pid"],
                "worker_process_create_time": identity["process_create_time"],
                "worker_command_signature": identity["command_signature"],
                "cancel": {"coordinator": coordinator_identity},
            },
        )

        verification = _cleanup_fixture_processes(jobs_root)

        worker.wait(timeout=5)
        coordinator.wait(timeout=5)
        assert verification["absent"] is True
    finally:
        for process in (worker, coordinator):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_wait_for_uses_only_a_monotonic_deadline(monkeypatch):
    class Manager:
        def status(self, _job_id):
            return {"status": "completed"}

        def tail(self, _job_id, _limit):
            raise AssertionError("completed status must return before tail")

    monkeypatch.setattr(
        time,
        "time",
        lambda: (_ for _ in ()).throw(AssertionError("wall clock used for timeout")),
    )

    assert wait_for(Manager(), "job", {"completed"})["status"] == "completed"


def _cleanup_fixture_processes(root: Path) -> dict[str, object]:
    from src.jobs.process_control import (
        capture_owned_descendants,
        terminate_exact,
        verify_absent,
    )

    targets: list[dict[str, object]] = []
    for state_path in root.glob("job-*/state.json"):
        try:
            state = read_json(state_path)
        except RuntimeError:
            continue
        pid = state.get("worker_pid")
        if not isinstance(pid, bool) and isinstance(pid, int) and pid != os.getpid():
            identity = {
                "pid": pid,
                "process_create_time": state.get("worker_process_create_time"),
                "command_signature": state.get("worker_command_signature"),
            }
            if (
                isinstance(identity["process_create_time"], (int, float))
                and not isinstance(identity["process_create_time"], bool)
                and isinstance(identity["command_signature"], str)
                and len(identity["command_signature"]) == 64
            ):
                captured = capture_owned_descendants(identity)
                targets.extend(captured.get("descendants", []))
                targets.append(identity)
        cancel = state.get("cancel")
        coordinator = cancel.get("coordinator") if isinstance(cancel, dict) else None
        if (
            isinstance(coordinator, dict)
            and coordinator.get("pid") != os.getpid()
            and isinstance(coordinator.get("process_create_time"), (int, float))
            and isinstance(coordinator.get("command_signature"), str)
            and len(coordinator["command_signature"]) == 64
        ):
            targets.append(coordinator)

    unique: dict[tuple[object, object, object], dict[str, object]] = {}
    for identity in targets:
        key = (
            identity.get("pid"),
            identity.get("process_create_time"),
            identity.get("command_signature"),
        )
        unique[key] = identity
    identities = list(unique.values())
    for identity in reversed(identities):
        terminate_exact(identity, force=True)

    deadline = time.monotonic() + 5.0
    verification = verify_absent(identities)
    while not verification["absent"] and time.monotonic() < deadline:
        # A killed POSIX child may briefly expose an empty /proc command line.
        # Retry inspection within the existing deadline. Never treat uncertainty as absence.
        time.sleep(0.025)
        verification = verify_absent(identities)
    return verification


def fixture_clean(root: Path, *, ignore_errors: bool) -> None:
    _cleanup_fixture_processes(root)
    shutil.rmtree(root, ignore_errors=ignore_errors)


monoclock = time.monotonic


def test_standalone_submit_surfaces_durable_job_launch_error():
    from src.jobs.manager import JobLaunchError
    from src.tools.jobs import _submit_job

    class RaisingManager:
        def submit(self, spec):
            raise JobLaunchError(
                "job-launch-failure",
                RuntimeError("worker spawn failed"),
                state_record_error=ValueError("stale state"),
            )

    result = _submit_job(
        {
            "job_type": "staged_sweep",
            "source_model_path": "C:/fixtures/sweep.mph",
            "parameter_name": "wl",
            "parameter_values": [1.0],
            "expressions": ["ewfd.Rtotal"],
        },
        profile_name="core",
        shared_enabled=False,
        manager=RaisingManager(),
    )

    assert result["success"] is False
    assert result["state"] == "durable_job_requires_reconciliation"
    assert result["job_id"] == "job-launch-failure"
    assert result["action"] == "inspect_job_status_before_retrying"


def test_job_tail_window_is_clamped_to_the_documented_bound():
    from comsol_mcp.tools.jobs import _clamp_tail_n

    assert _clamp_tail_n(-5) == 1
    assert _clamp_tail_n(0) == 1
    assert _clamp_tail_n(20) == 20
    assert _clamp_tail_n(200) == 200
    assert _clamp_tail_n(10_000) == 200

    for bad in (True, "20", 2.5, None):
        with pytest.raises(ValueError, match="n must be an integer between 1 and 200"):
            _clamp_tail_n(bad)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows venv executable redirector")
@pytest.mark.parametrize("cancel_resume", [False, True])
def test_windows_venv_worker_binds_executing_process(ascii_tmp_path, monkeypatch, cancel_resume):
    import venv

    environment = ascii_tmp_path / "venv space"
    venv.EnvBuilder(with_pip=False, system_site_packages=True).create(environment)
    monkeypatch.setattr(sys, "executable", str(environment / "Scripts" / "python.exe"))
    manager = JobManager(ascii_tmp_path / "jobs", allow_test_jobs=True)
    delays = [0.05, 1.0] if cancel_resume else [0.05]
    submitted = manager.submit({"job_type": "test_sequence", "delays": delays})
    if cancel_resume:
        wait_for(manager, submitted["job_id"], {"running"})
        assert manager.cancel(submitted["job_id"])["success"] is True
        wait_for(manager, submitted["job_id"], {"cancelled"})
        assert manager.resume(submitted["job_id"])["attempt"] == 2
    terminal = wait_for(
        manager, submitted["job_id"], {"completed", "interrupted", "failed"}, timeout=10
    )
    assert terminal["status"] == "completed", manager.tail(submitted["job_id"], 20)
    assert terminal["progress"] == {"completed": len(delays), "total": len(delays)}


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "command",
        "pid",
        "created",
        "older",
        "foreign",
        "multiple",
        "reused_launcher",
        "missing",
    ],
)
def test_venv_redirector_identity_requires_one_exact_child(monkeypatch, failure):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from comsol_mcp.jobs import manager as module

    command = ["python", "-m", "owned-worker"]
    signature = hashlib.sha256("\0".join(command).encode()).hexdigest()
    launcher = {"pid": 41, "process_create_time": 1.0, "command_signature": signature}
    child_identity = {"pid": 42, "process_create_time": 2.0, "command_signature": signature}
    if failure == "command":
        child_identity["command_signature"] = "0" * 64
    elif failure == "pid":
        child_identity["pid"] = 99
    elif failure == "created":
        child_identity["process_create_time"] = 3.0
    elif failure == "older":
        child_identity["process_create_time"] = 0.5
    child = SimpleNamespace(
        pid=42,
        ppid=lambda: 99 if failure == "foreign" else 41,
        create_time=lambda: 0.5 if failure == "older" else 2.0,
        oneshot=nullcontext,
    )
    children = [] if failure == "missing" else [child, child] if failure == "multiple" else [child]
    root = SimpleNamespace(children=lambda: children)
    root_reads = []

    def identify(pid):
        if pid == 41:
            root_reads.append(pid)
            if failure == "reused_launcher" and len(root_reads) > 1:
                return {**launcher, "process_create_time": 9.0}
            return dict(launcher)
        assert pid == 42
        return dict(child_identity)

    monkeypatch.setattr(module, "_uses_windows_venv_redirector", lambda _command: True)
    monkeypatch.setattr(module, "process_identity", identify)
    monkeypatch.setattr(
        module.psutil, "Process", lambda pid: root if pid == 41 else pytest.fail("foreign root")
    )
    process = SimpleNamespace(pid=41)
    if failure is None:
        assert module._launched_process_identity(process, command) == child_identity
    else:
        with pytest.raises(OSError, match="identity"):
            module._launched_process_identity(process, command)
