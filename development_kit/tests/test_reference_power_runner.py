"""Process-only gates for the reference-power licensed coordinator/worker boundary."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from development_kit.tests.integration import reference_power_acceptance as runner_module
from development_kit.tests.integration.reference_power_acceptance import (
    _admit_lightweight_status,
    _communicate_worker,
    _load_worker_payload,
    _redacted_status,
    _stable_lightweight_admission,
    _start_hidden_worker,
    _worker_summary,
)

ROOT = Path(__file__).parents[2]
RUNNER = ROOT / "development_kit" / "tests" / "integration" / "reference_power_acceptance.py"


def _solver_descendant_identities(root_pid: int):
    identities = set()
    try:
        processes = psutil.Process(root_pid).children(recursive=True)
    except psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess:
        return identities
    for process in processes:
        try:
            executable_names = {
                Path(value).name.casefold()
                for value in [process.name(), *process.cmdline()]
                if value
            }
            if any(
                name.startswith("comsol") or name.startswith("mphserver")
                for name in executable_names
            ):
                identities.add((process.pid, process.create_time()))
        except psutil.Error, OSError:
            continue
    return identities


def _run_with_solver_observer(command):
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    observed = set()
    stop = threading.Event()

    def observe():
        while not stop.wait(0.01):
            observed.update(_solver_descendant_identities(process.pid))

    observer = threading.Thread(target=observe, daemon=True)
    observer.start()
    try:
        stdout, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired as initial_timeout:
        process.kill()
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired as drain_timeout:
            stdout = (
                drain_timeout.stdout if drain_timeout.stdout is not None else initial_timeout.stdout
            )
            stderr = (
                drain_timeout.stderr if drain_timeout.stderr is not None else initial_timeout.stderr
            )
        raise subprocess.TimeoutExpired(
            initial_timeout.cmd,
            initial_timeout.timeout,
            output=stdout,
            stderr=stderr,
        ) from initial_timeout
    finally:
        observed.update(_solver_descendant_identities(process.pid))
        stop.set()
        observer.join(timeout=2)
    completed = subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout,
        stderr,
    )
    return completed, observed


def test_solver_observer_timeout_preserves_post_kill_diagnostics(monkeypatch):
    class Process:
        pid = 41000
        returncode = 1

        def __init__(self):
            self.communications = 0
            self.killed = False

        def communicate(self, *, timeout):
            self.communications += 1
            if self.communications == 1:
                raise subprocess.TimeoutExpired(
                    [sys.executable, "worker.py"],
                    timeout,
                    output="before-kill",
                    stderr="before-kill-error",
                )
            return "after-kill", "after-kill-error"

        def kill(self):
            self.killed = True

    process = Process()
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(sys.modules[__name__], "_solver_descendant_identities", lambda _pid: set())

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        _run_with_solver_observer([sys.executable, "worker.py"])

    assert process.killed is True
    assert raised.value.stdout == "after-kill"
    assert raised.value.stderr == "after-kill-error"


def test_stable_admission_rechecks_a_clean_process_snapshot() -> None:
    clean = {
        "complete": True,
        "lease_state": "absent",
        "external_solver_processes": [],
    }
    collision = {
        **clean,
        "external_solver_processes": [{"pid": 41001, "kind": "python-mph-client"}],
    }
    statuses = iter([clean, collision])
    sleeps = []

    status, admitted, blockers = _stable_lightweight_admission(
        status_provider=lambda: next(statuses),
        sleeper=sleeps.append,
    )

    assert status is collision
    assert admitted is False
    assert blockers == ["external COMSOL/MPh solver process detected"]
    assert sleeps == [0.05]


def test_runner_import_and_dry_run_do_not_import_mph_or_start_comsol():
    import_probe, import_solver_starts = _run_with_solver_observer(
        [
            sys.executable,
            "-c",
            (
                "import sys; import development_kit.tests.integration.reference_power_acceptance; "
                "print('true' if 'mph' in sys.modules else 'false')"
            ),
        ],
    )
    dry_run, dry_run_solver_starts = _run_with_solver_observer(
        [sys.executable, str(RUNNER), "--dry-run", "--cores", "1"]
    )

    assert import_probe.returncode == 0, import_probe.stderr
    assert import_probe.stdout.strip() == "false"
    assert import_solver_starts == set()
    assert dry_run.returncode == 0, dry_run.stderr
    assert dry_run_solver_starts == set()
    receipt = json.loads(dry_run.stdout)
    assert receipt["real_comsol_started"] is False
    assert receipt["contract_valid"] is True


def test_solver_observer_attributes_only_transitive_child_launches():
    command = [
        sys.executable,
        "-c",
        (
            "import subprocess,sys,time; "
            # Use the real interpreter so a Windows venv redirector does not
            # add a second marked process to this single-child fixture.
            "subprocess.Popen([sys._base_executable,'-c','import time; time.sleep(0.5)',"
            "'comsol-child-marker']); time.sleep(0.25)"
        ),
    ]

    completed, observed = _run_with_solver_observer(command)

    assert completed.returncode == 0, completed.stderr
    assert len(observed) == 1


def test_lightweight_admission_fails_closed_without_exposing_commands_or_paths():
    status = {
        "complete": True,
        "error": None,
        "lease_path": "D:/private/solver_owner.json",
        "lease_state": "present",
        "lease_sha256": "a" * 64,
        "collision": True,
        "external_solver_processes": [
            {
                "pid": 123,
                "parent_pid": 100,
                "create_time": 1.0,
                "kind": "python-mph-client-script",
                "command_line": ["python", "C:/private/solver.py"],
            }
        ],
    }

    admitted, blockers = _admit_lightweight_status(status)
    redacted = _redacted_status(status)

    assert admitted is False
    assert "solver lease is not absent" in blockers
    assert "external COMSOL/MPh solver process detected" in blockers
    serialized = json.dumps(redacted)
    assert "private" not in serialized
    assert "command_line" not in serialized
    assert redacted["external_solver_processes"][0]["pid"] == 123


def test_lightweight_inventory_marks_access_denied_as_incomplete(tmp_path, monkeypatch):
    class Process:
        pid = 44001

        @property
        def info(self):
            raise psutil.AccessDenied(pid=self.pid)

    monkeypatch.setattr(runner_module, "_ancestor_pids", lambda _pid: set())
    monkeypatch.setattr(runner_module.psutil, "process_iter", lambda _attrs: [Process()])
    monkeypatch.setattr(runner_module, "_runtime_root", lambda: tmp_path)

    status = runner_module._lightweight_solver_status()
    admitted, blockers = runner_module._admit_lightweight_status(status)

    assert status["complete"] is False
    assert status["inspection_error_count"] == 1
    assert status["inspection_errors"] == [{"pid": 44001, "error_type": "AccessDenied"}]
    assert admitted is False
    assert "process inventory incomplete" in blockers


def test_worker_acquires_operation_ownership_before_final_inventory(monkeypatch):
    events = []
    claim = object()

    class Arbiter:
        def try_acquire(self, **_kwargs):
            events.append("acquire")
            return claim, {"state": "acquired"}

    clean = {
        "complete": True,
        "error": None,
        "inspection_error_count": 0,
        "inspection_errors": [],
        "lease_state": "absent",
        "lease_sha256": None,
        "collision": False,
        "external_solver_processes": [],
    }
    monkeypatch.setattr(runner_module, "get_operation_arbiter", lambda: Arbiter())
    monkeypatch.setattr(
        runner_module,
        "_lightweight_solver_status",
        lambda: events.append("inventory") or clean,
    )

    arbiter, observed_claim, evidence = runner_module._prepare_worker_admission()

    assert isinstance(arbiter, Arbiter)
    assert observed_claim is claim
    assert events == ["acquire", "inventory"]
    assert evidence["pre_import_admission"]["admitted"] is True


def test_coordinator_summary_keeps_failure_details_in_worker_artifact_only():
    payload = {
        "success": False,
        "error": "material readback mismatch at C:/private/model.mph",
        "traceback": "C:/private/source.py:1",
        "reference_result": {"source_path": "C:/private/model.mph"},
        "evaluation": {"passed": False},
        "client_clear": True,
        "lease_release": {"success": True},
    }

    summary = _worker_summary(payload)

    assert summary["error"] == "worker execution failed; see worker artifact"
    assert "traceback" not in summary
    assert "reference_result" not in summary
    assert "private" not in json.dumps(summary)


def test_coordinator_rejects_receipt_inside_artifact_root_before_admission(tmp_path, monkeypatch):
    artifact_root = tmp_path / "artifacts"
    output = artifact_root / "worker_result.json"
    monkeypatch.setattr(
        runner_module,
        "_load_inputs",
        lambda *_args, **_kwargs: ({}, {"artifact_dir": str(artifact_root)}),
    )
    monkeypatch.setattr(
        runner_module,
        "_lightweight_solver_status",
        lambda: pytest.fail("admission must not run for an invalid output path"),
    )

    with pytest.raises(ValueError, match="outside the artifact root"):
        runner_module._run_coordinator(
            SimpleNamespace(contract=tmp_path / "contract", spec=tmp_path / "spec", output=output)
        )

    assert not output.exists()


def test_real_mode_requires_explicit_authority_and_resource_limits():
    completed = subprocess.run(
        [sys.executable, str(RUNNER), "--cores", "1"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode != 0
    assert "licensed run requires" in completed.stderr


def test_worker_launch_applies_hidden_process_controls(monkeypatch):
    captured = {}

    class Process:
        pid = 42001

    def popen(command, **kwargs):
        captured.update(command=list(command), kwargs=kwargs)
        return Process()

    monkeypatch.setattr(runner_module.subprocess, "Popen", popen)
    process = _start_hidden_worker([sys.executable, "worker.py"])
    expected_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )

    assert process.pid == 42001
    assert captured["command"] == [sys.executable, "worker.py"]
    assert captured["kwargs"] == {
        "cwd": ROOT,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "creationflags": expected_flags,
    }


def test_coordinator_refuses_collision_before_starting_worker(tmp_path, ascii_tmp_path):
    source = tmp_path / "dummy.mph"
    source.write_bytes(b"not-a-real-model")
    blocker_script = tmp_path / "owned_solver.py"
    blocker_script.write_text(
        "# mph.Client collision marker for the lightweight scanner\n"
        "import sys, time\n"
        "from pathlib import Path\n"
        "marker = Path(sys.argv[1])\n"
        "temporary = marker.with_name(marker.name + '.tmp')\n"
        "temporary.write_text('ready', encoding='utf-8')\n"
        "temporary.replace(marker)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    blocker_ready = tmp_path / "blocker.ready"
    artifact_dir = ascii_tmp_path / "reference_power_collision"
    spec_path = tmp_path / "spec.json"
    output_path = tmp_path / "receipt.json"
    spec = {
        "schema_name": "comsol_mcp.h1_execution_spec",
        "schema_version": "1.0.0",
        "config_id": "collision-refusal",
        "source_model_path": str(source.resolve()),
        "expected_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "artifact_dir": str(artifact_dir),
        "model": {
            "component_tag": "comp1",
            "physics_tag": "ewfd",
            "study_tag": "std1",
            "study_step_tag": "wl_step",
            "study_step_property": "plist",
        },
        "wavelength": {"value": 4.37, "unit": "um", "parameter": "wl"},
        "reference_air": {
            "expected_material_tags": ["mat1"],
            "all_domain_ids": [1, 2],
            "top_air_domain_ids": [2],
            "top_air_coordinate_range": {"x": [0, 1], "y": [0, 1], "z": [0.8, 1]},
            "target_axis": "x",
            "aggregation": "rms_abs",
            "r_expression": "ewfd.Rtotal",
            "t_expression": "ewfd.Ttotal",
        },
        "declared_plane_flux": {
            "incident": {
                "expression": "inc",
                "selection_ids": [10],
                "plane_coordinate_m": 1e-6,
                "normal": [0, 0, -1],
                "medium_id": "air",
                "positive_power_sign": -1,
            },
            "reflected": {
                "expression": "ref",
                "selection_ids": [11],
                "plane_coordinate_m": 1e-6,
                "normal": [0, 0, 1],
                "medium_id": "air",
                "positive_power_sign": 1,
            },
            "transmitted": {
                "expression": "trn",
                "selection_ids": [12],
                "plane_coordinate_m": -1e-6,
                "normal": [0, 0, -1],
                "medium_id": "air",
                "positive_power_sign": -1,
            },
        },
    }
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    blocker = subprocess.Popen(
        [sys.executable, str(blocker_script), str(blocker_ready)],
        cwd=ROOT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        deadline = time.monotonic() + 5.0
        ready = None
        while blocker.poll() is None and time.monotonic() < deadline:
            try:
                ready = blocker_ready.read_text(encoding="utf-8")
            except OSError:
                ready = None
            if ready == "ready":
                break
            time.sleep(0.01)
        assert ready == "ready"
        assert blocker.poll() is None
        scanner_status = runner_module._lightweight_solver_status()
        assert any(
            item["pid"] == blocker.pid and item["kind"] == "python-mph-client-script"
            for item in scanner_status["external_solver_processes"]
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--confirm",
                "RUN_REAL_COMSOL",
                "--spec",
                str(spec_path),
                "--output",
                str(output_path),
                "--cores",
                "1",
                "--timeout-seconds",
                "30",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    finally:
        blocker.terminate()
        blocker.wait(timeout=10)
    receipt = json.loads(output_path.read_text(encoding="utf-8"))

    assert completed.returncode == 2
    assert receipt["worker_started"] is False
    assert receipt["pre_import_admission"]["admitted"] is False
    assert (
        "external COMSOL/MPh solver process detected" in receipt["pre_import_admission"]["blockers"]
    )
    assert not (artifact_dir / "worker_result.json").exists()
    if artifact_dir.exists():
        artifact_dir.rmdir()


def test_timeout_cleanup_contains_taskkill_and_repeat_communication_failures(monkeypatch):
    class Process:
        pid = 41001

        def __init__(self):
            self.returncode = None
            self.communications = 0

        def communicate(self, *, timeout):
            self.communications += 1
            if self.communications == 1:
                raise subprocess.TimeoutExpired("worker", timeout, output=b"partial\xff")
            raise subprocess.TimeoutExpired("worker", timeout)

        def poll(self):
            return self.returncode

        def wait(self, *, timeout):
            self.returncode = 1
            return self.returncode

        def kill(self):
            self.returncode = 1

    class Containment:
        closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        runner_module.subprocess,
        "run",
        lambda *_args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("taskkill", kwargs["timeout"])
        ),
    )
    containment = Containment()

    result = _communicate_worker(Process(), timeout_seconds=0.1, containment=containment)

    assert result["timed_out"] is True
    assert result["errors"] == ["TimeoutExpired"]
    assert result["stdout"] == "partial�"
    assert result["cleanup"]["passed"] is False
    assert result["cleanup"]["errors"] == [{"stage": "taskkill", "type": "TimeoutExpired"}]
    assert containment.closed is True


def test_malformed_worker_result_becomes_structured_failure(tmp_path):
    worker_result = tmp_path / "worker-result.json"
    worker_result.write_bytes(b'{"success":')

    payload, error = _load_worker_payload(worker_result, 1024)

    assert payload == {
        "success": False,
        "error": "worker result artifact is unreadable or invalid",
    }
    assert error == "JSONDecodeError"


def test_coordinator_publishes_failure_receipt_for_malformed_worker_result(tmp_path, monkeypatch):
    artifact_root = tmp_path / "artifacts"
    output = tmp_path / "receipt.json"
    spec = {
        "artifact_dir": str(artifact_root),
        "expected_source_sha256": "a" * 64,
        "config_id": "malformed-worker-result",
    }
    contract = {"limits": {"max_artifact_bytes": 4096}}
    clean_status = {
        "complete": True,
        "error": None,
        "lease_state": "absent",
        "lease_sha256": None,
        "collision": False,
        "external_solver_processes": [],
    }

    class Process:
        pid = 43001
        returncode = 1

        def __init__(self, command, **_kwargs):
            result_path = Path(command[command.index("--worker-result") + 1])
            result_path.write_bytes(b'{"success":')

    monkeypatch.setattr(runner_module, "_load_inputs", lambda *_args, **_kwargs: (contract, spec))
    monkeypatch.setattr(runner_module, "_lightweight_solver_status", lambda: clean_status)
    monkeypatch.setattr(runner_module, "_comsol_pids", lambda: set())
    monkeypatch.setattr(runner_module.subprocess, "Popen", Process)
    monkeypatch.setattr(runner_module.OwnedJobObject, "assign", lambda _pid: object())
    monkeypatch.setattr(
        runner_module,
        "_communicate_worker",
        lambda *_args, **_kwargs: {
            "timed_out": False,
            "errors": [],
            "stdout": "",
            "cleanup": {"passed": True},
        },
    )
    monkeypatch.setattr(runner_module, "_wait_lightweight_clean", lambda: clean_status)
    monkeypatch.setattr(runner_module, "inventory_reference_power_artifacts", lambda *_args: {})

    exit_code = runner_module._run_coordinator(
        SimpleNamespace(
            contract=tmp_path / "contract.json",
            spec=tmp_path / "spec.json",
            output=output,
            cores=1,
            timeout_seconds=30.0,
        )
    )
    receipt = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert receipt["success"] is False
    assert receipt["worker_result_error"] == "JSONDecodeError"
    assert receipt["worker_result"]["error"] == "worker execution failed; see worker artifact"
