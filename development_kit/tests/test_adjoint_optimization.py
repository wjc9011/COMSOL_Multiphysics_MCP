"""Solver-free manifest and fake-worker contracts for adjoint jobs."""

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from comsol_mcp.jobs import native_adjoint_runtime
from comsol_mcp.jobs.adjoint_optimization import (
    expand_adjoint_optimization_manifest,
    normalize_adjoint_optimization_submission,
    normalize_native_execution_policy,
)
from comsol_mcp.jobs.adjoint_optimization_worker import run as run_adjoint_worker
from comsol_mcp.jobs.adjoint_rows import read_adjoint_rows
from comsol_mcp.jobs.manager import JobManager
from comsol_mcp.jobs.store import JobStore, process_identity, read_json
from development_kit.tests.test_derivative_support import _support
from development_kit.tests.test_gradient_contracts import _optimizer


def _execution_policy():
    return {
        "smoothing_type": "hyperelastic",
        "geometry_shape_order": "1",
        "gradient_method": "adjoint",
        "finalist_fractions": [0.25, 0.5, 0.75, 1.0],
        "geometry_jacobian_expression": "reldetjac",
        "minimum_geometry_jacobian": 0.0,
        "material_jacobian_expression": "comp1.material.detF",
        "minimum_material_jacobian": 0.0,
        "minimum_element_quality": 0.1,
        "passivity_absolute_tolerance": 1e-6,
        "closure_absolute_tolerance": 1e-6,
    }


@pytest.mark.parametrize("fractions", [[], [0, 1], [True, 1], [0.5, 0.5, 1], [0.9], [1] * 33])
def test_execution_policy_refuses_incomplete_or_invalid_loading(fractions):
    policy = _execution_policy()
    policy["finalist_fractions"] = fractions
    with pytest.raises(ValueError):
        normalize_native_execution_policy(policy)


def test_execution_policy_preserves_explicit_configuration():
    policy = _execution_policy()
    assert normalize_native_execution_policy(policy) == policy
    policy["automatic_fallback"] = True
    with pytest.raises(ValueError, match="fields mismatch"):
        normalize_native_execution_policy(policy)


def test_versioned_execution_policy_is_hash_bound_and_reserves_solves(ascii_tmp_path):
    envelope, _, manifest = _write_manifest(ascii_tmp_path)
    raw = json.loads(manifest.read_text())
    raw["schema_version"] = "1.1.0"
    raw["execution_policy"] = _execution_policy()
    raw["optimizer"]["budget"]["max_solves"] = 5

    def publish():
        payload = json.dumps(raw).encode()
        manifest.write_bytes(payload)
        envelope["submission_manifest_sha256"] = hashlib.sha256(payload).hexdigest()

    publish()
    with pytest.raises(ValueError, match="reserve"):
        expand_adjoint_optimization_manifest(envelope)
    raw["optimizer"]["budget"]["max_solves"] = 6
    publish()
    spec = expand_adjoint_optimization_manifest(envelope)
    assert spec["execution_policy"] == _execution_policy()
    assert spec["schema_version"] == "1.1.0"


def test_loading_uses_actual_solver_and_preserves_initialization():
    values = {}
    calls = []
    node = SimpleNamespace(set=lambda k, v: values.__setitem__(k, v), getString=lambda k: values[k])
    solution = SimpleNamespace(feature=lambda tag: node, runAll=lambda: calls.append("solve"))
    model = SimpleNamespace(java=SimpleNamespace(sol=lambda tag: solution))
    study = SimpleNamespace(
        createAutoSequences=lambda kind: calls.append(kind),
        getSolverSequences=lambda kind: ["actual_solver"],
    )
    tag, observed = native_adjoint_runtime._run_loading_solution(model, study)
    assert tag == "actual_solver"
    assert observed == {"initmethod": "sol", "initsol": tag, "solnum": "last"}
    assert calls == ["all", "solve"]


def test_loading_initial_vector_converts_declared_units():
    support = {
        "variables": [
            {"variable_id": "patch_length_x", "unit": "nm"},
            {"variable_id": "patch_length_y", "unit": "um"},
        ]
    }
    result = native_adjoint_runtime._initial_values_si(support, [800.0, 0.9])
    assert result == pytest.approx({"patch_length_x": 8e-7, "patch_length_y": 9e-7})


def _resource_policy() -> dict:
    return {"max_mesh_elements": 1000}


@pytest.mark.parametrize("failure", [None, "empty", "mismatch", "exited"])
def test_watchdog_identity_confirmation_is_bounded_and_strict(monkeypatch, failure):
    from comsol_mcp.jobs import manager as module

    command = ["python", "-m", "owned-watchdog"]
    signature = hashlib.sha256("\0".join(command).encode()).hexdigest()
    expected = {"pid": 123, "process_create_time": 1.0, "command_signature": signature}
    clock = [0.0]
    calls = []

    def observe(pid):
        calls.append(pid)
        if failure == "empty" or (failure is None and len(calls) == 1):
            raise OSError("temporarily empty command line")
        return {**expected, "command_signature": "0" * 64} if failure == "mismatch" else expected

    monkeypatch.setattr(module, "process_identity", observe)
    import time as real_time

    fake_time = SimpleNamespace(
        time=real_time.time,
        monotonic=lambda: clock[0],
        sleep=lambda interval: clock.__setitem__(0, clock[0] + interval),
    )
    monkeypatch.setattr(module, "time", fake_time)
    process = SimpleNamespace(pid=123, poll=lambda: 0 if failure == "exited" else None)
    if failure is None:
        assert module._wait_for_launched_identity(process, command) == expected
        assert len(calls) == 2
    else:
        with pytest.raises(RuntimeError, match="identity could not be verified"):
            module._wait_for_launched_identity(process, command)
        assert clock[0] <= 2.01
        if failure == "exited":
            assert calls == []


def _write_manifest(tmp_path):
    source = tmp_path / "source.mph"
    source.write_bytes(b"synthetic source")
    support = _support()
    support["source_identity"] = hashlib.sha256(source.read_bytes()).hexdigest()
    body = {
        "schema_name": "comsol_mcp.adjoint_optimization_manifest",
        "schema_version": "1.0.0",
        "source_model_path": str(source),
        "source_model_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "support": support,
        "optimizer": _optimizer(),
        "initial_values": [856.0],
        "synthetic_mode": True,
    }
    manifest = tmp_path / "manifest.json"
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    manifest.write_bytes(payload)
    envelope = {
        "job_type": "adjoint_optimization",
        "submission_manifest_path": str(manifest),
        "submission_manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "cores": 3,
        "version": "6.4",
        "resource_policy": _resource_policy(),
    }
    return envelope, source, manifest


@pytest.mark.parametrize("release_mode", ["success", "refused", "exception"])
@pytest.mark.parametrize("runtime_mode", ["success", "failure", "cancel"])
def test_native_completion_waits_for_lease_cleanup(
    ascii_tmp_path, monkeypatch, release_mode, runtime_mode
):
    from comsol_mcp.jobs import adjoint_optimization_worker as worker
    from comsol_mcp.tools import ownership as ownership_module

    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = JobManager(
        ascii_tmp_path / "jobs", preflight=lambda **_kwargs: {"success": True, "ready": True}
    )
    monkeypatch.setattr(manager, "_launch_worker", lambda *_args: process_identity(os.getpid()))
    job_id = manager.submit(envelope)["job_id"]
    store = manager.store
    release_observations = []

    class Owner:
        def __init__(self, *_args, **_kwargs):
            pass

        def preflight(self, **_kwargs):
            return {"ready": True}

        def acquire(self, **_kwargs):
            return {"success": True}

        def release(self):
            release_observations.append(store.read_state(job_id)["status"])
            if release_mode == "exception":
                raise OSError("injected lease sharing failure")
            return {"success": release_mode == "success"}

    monkeypatch.setattr(ownership_module, "SolverOwnership", Owner)
    monkeypatch.setattr(worker, "contain_current_process_tree", lambda: True)
    monkeypatch.setattr(worker, "_await_licensed_wall_watchdog", lambda *_args: {})

    def execute(*_args):
        if runtime_mode == "cancel":
            store.update_state(job_id, "cancel_requested", event="test_cancel")
        if runtime_mode != "success":
            raise RuntimeError("injected runtime failure")
        return {
            "success": True,
            "final_variables_si": {"x": 1.0},
            "fresh_forward_objective": 0.5,
            "cleanup": {"source_unchanged": True},
        }

    monkeypatch.setattr(
        native_adjoint_runtime,
        "execute_native_adjoint_optimization",
        execute,
    )
    code = worker._run_native(str(store.root), job_id)
    state = store.read_state(job_id)
    assert state["owned_cleanup"]["success"] is (release_mode == "success")
    assert state["cleanup_pending"] is (release_mode != "success")
    if runtime_mode != "success":
        assert state["status"] != "completed"
        assert release_observations == [state["status"]]
        assert code == (0 if runtime_mode == "cancel" and release_mode == "success" else 1)
        if runtime_mode == "failure":
            assert state["last_error"]["message"] == "injected runtime failure"
        return
    assert release_observations == ["smoke_validated"]
    assert state["status"] == ("completed" if release_mode == "success" else "failed")
    assert code == (0 if release_mode == "success" else 1)
    assert state["progress"] == {"completed": 1, "total": 1}
    assert state["progress_unit"] == "validated_final_candidates"
    assert state["native_iterations_completed"] is None


def test_submission_requires_explicit_ascii_manifest_and_resources(ascii_tmp_path):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    normalized = normalize_adjoint_optimization_submission(envelope)
    assert normalized["cores"] == 3
    assert normalized["resource_policy"]["host_defaults_applied"] is False
    missing = dict(envelope)
    missing.pop("cores")
    with pytest.raises(ValueError, match="fields"):
        normalize_adjoint_optimization_submission(missing)


def test_submission_normalization_is_idempotent_over_its_own_output(ascii_tmp_path):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    once = normalize_adjoint_optimization_submission(envelope)
    twice = normalize_adjoint_optimization_submission(once)
    assert twice == once

    mismatched = dict(once)
    mismatched["schema_version"] = "9.9.9"
    with pytest.raises(ValueError, match="schema version is unsupported"):
        normalize_adjoint_optimization_submission(mismatched)

    foreign_name = dict(once)
    foreign_name["schema_name"] = "other.submission"
    with pytest.raises(ValueError, match="schema name is unsupported"):
        normalize_adjoint_optimization_submission(foreign_name)

    expanded_once = expand_adjoint_optimization_manifest(once)
    expanded_twice = expand_adjoint_optimization_manifest(twice)
    assert expanded_twice == expanded_once


def test_manifest_expansion_hashes_source_and_support_identity(ascii_tmp_path):
    envelope, source, _ = _write_manifest(ascii_tmp_path)
    spec = expand_adjoint_optimization_manifest(envelope)
    assert spec["source_model_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert spec["support"]["derivative_method"] == "adjoint"
    assert spec["synthetic_mode"] is True
    changed = dict(envelope)
    changed["submission_manifest_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="SHA-256"):
        expand_adjoint_optimization_manifest(changed)


def test_manifest_rejects_source_mutation_after_submission(ascii_tmp_path):
    envelope, source, _ = _write_manifest(ascii_tmp_path)
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="source SHA-256"):
        expand_adjoint_optimization_manifest(envelope)


def test_manifest_rejects_match_without_an_explicit_native_target(ascii_tmp_path):
    envelope, _, manifest = _write_manifest(ascii_tmp_path)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["support"]["objective"]["direction"] = "match"
    payload = json.dumps(raw).encode()
    manifest.write_bytes(payload)
    envelope["submission_manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    with pytest.raises(ValueError, match="match.*target"):
        expand_adjoint_optimization_manifest(envelope)


def test_native_runtime_rejects_match_before_client_or_workdir(ascii_tmp_path):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    spec = expand_adjoint_optimization_manifest(envelope)
    spec["support"]["objective"]["direction"] = "match"
    root = ascii_tmp_path / "refused-native-runtime"

    def unexpected_client(**kwargs):
        pytest.fail("unsupported match must not construct a native client")

    with pytest.raises(ValueError, match="match.*target"):
        native_adjoint_runtime.execute_native_adjoint_optimization(
            spec, root, client_factory=unexpected_client
        )
    assert not root.exists()


def test_manifest_rejects_oversized_or_non_numeric_initial_values(ascii_tmp_path):
    envelope, _, manifest = _write_manifest(ascii_tmp_path)

    def rewrite(mutate):
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        mutate(raw)
        payload = json.dumps(
            raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        manifest.write_bytes(payload)
        envelope["submission_manifest_sha256"] = hashlib.sha256(payload).hexdigest()

    def set_values(raw, values):
        raw["initial_values"] = values

    rewrite(lambda raw: set_values(raw, [True]))
    with pytest.raises(ValueError, match="must be a number"):
        expand_adjoint_optimization_manifest(envelope)
    rewrite(lambda raw: set_values(raw, ["856.0"]))
    with pytest.raises(ValueError, match="must be a number"):
        expand_adjoint_optimization_manifest(envelope)
    rewrite(lambda raw: set_values(raw, [None]))
    with pytest.raises(ValueError, match="must be a number"):
        expand_adjoint_optimization_manifest(envelope)


def test_manifest_rejects_oversized_submission_before_reading(ascii_tmp_path):
    envelope, _, manifest = _write_manifest(ascii_tmp_path)
    manifest.write_bytes(b"x" * (513 * 1024))
    with pytest.raises(ValueError, match="byte limit"):
        expand_adjoint_optimization_manifest(envelope)


def test_manifest_rejects_unvalidated_method_or_mismatched_core_budget(ascii_tmp_path):
    envelope, _, manifest = _write_manifest(ascii_tmp_path)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["optimizer"]["method"] = "mma"
    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    manifest.write_bytes(payload)
    envelope["submission_manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    with pytest.raises(ValueError, match="only validated gcmma"):
        expand_adjoint_optimization_manifest(envelope)

    raw["optimizer"]["method"] = "gcmma"
    raw["optimizer"]["budget"]["cores"] = 2
    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    manifest.write_bytes(payload)
    envelope["submission_manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    with pytest.raises(ValueError, match="budget cores"):
        expand_adjoint_optimization_manifest(envelope)


def test_durable_manager_accepts_synthetic_adjoint_discriminator(ascii_tmp_path, monkeypatch):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = JobManager(
        ascii_tmp_path / "jobs",
        preflight=lambda **_kwargs: {"success": True, "ready": True},
    )
    monkeypatch.setattr(
        manager,
        "_launch_worker",
        lambda _job_id, module: (
            process_identity(os.getpid())
            if module == "comsol_mcp.jobs.adjoint_optimization_worker"
            else (_ for _ in ()).throw(AssertionError(module))
        ),
    )
    result = manager.submit(envelope)
    spec = manager.store.read_spec(result["job_id"])
    assert spec["job_type"] == "adjoint_optimization"
    assert spec["synthetic_mode"] is True
    assert not (manager.store.job_dir(result["job_id"]) / "wall-watchdog.json").exists()
    assert spec["optimizer"]["budget"]["max_iterations"] == 10
    assert manager.store.read_state(result["job_id"])["progress"] == {
        "completed": 0,
        "total": 10,
    }
    assert run_adjoint_worker(str(manager.store.root), result["job_id"]) == 0
    terminal = manager.store.read_state(result["job_id"])
    assert terminal["status"] == "completed"
    assert terminal["solver_started"] is False


def test_synthetic_worker_writes_rows_for_its_own_attempt_after_prior_attempt_rows(
    ascii_tmp_path, monkeypatch
):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = JobManager(
        ascii_tmp_path / "jobs",
        preflight=lambda **_kwargs: {"success": True, "ready": True},
    )
    monkeypatch.setattr(
        manager,
        "_launch_worker",
        lambda _job_id, _module: process_identity(os.getpid()),
    )
    result = manager.submit(envelope)
    job_id = result["job_id"]
    store = manager.store

    def broken_identity(self, *_args, **_kwargs):
        raise OSError("injected identity failure")

    monkeypatch.setattr(JobStore, "bind_worker_identity", broken_identity)
    assert run_adjoint_worker(str(store.root), job_id) == 1
    store.update_state(job_id, "starting", event="retry_starting")
    rows_path = store.job_dir(job_id) / "optimization_rows.jsonl"
    spec_fingerprint = store.read_spec(job_id)["spec_fingerprint"]
    from comsol_mcp.jobs.adjoint_rows import append_adjoint_row, read_adjoint_rows

    foreign_attempt = 9
    append_adjoint_row(
        rows_path,
        job_fingerprint=spec_fingerprint,
        attempt=foreign_attempt,
        kind="gradient",
        payload={
            "iteration_id": "it-0",
            "gradient_fingerprint": "a" * 64,
            "check_fingerprint": "b" * 64,
            "evidence_state": "gradient_validated",
        },
    )
    monkeypatch.setattr(JobStore, "bind_worker_identity", lambda self, *a, **k: None)
    assert run_adjoint_worker(str(store.root), job_id) == 0
    all_rows = read_adjoint_rows(rows_path, job_fingerprint=spec_fingerprint)
    assert any(row["attempt"] == foreign_attempt for row in all_rows)
    current_rows = [row for row in all_rows if row["attempt"] != foreign_attempt]
    assert {"gradient", "iteration"} <= {row["kind"] for row in current_rows}


def test_synthetic_worker_failure_reaches_terminal_state_instead_of_propagating(
    ascii_tmp_path, monkeypatch
):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = JobManager(
        ascii_tmp_path / "jobs",
        preflight=lambda **_kwargs: {"success": True, "ready": True},
    )
    monkeypatch.setattr(
        manager,
        "_launch_worker",
        lambda _job_id, _module: process_identity(os.getpid()),
    )
    result = manager.submit(envelope)
    job_id = result["job_id"]
    store = manager.store

    def broken_identity(self, *_args, **_kwargs):
        raise OSError("injected identity failure")

    monkeypatch.setattr(JobStore, "bind_worker_identity", broken_identity)

    assert run_adjoint_worker(str(store.root), job_id) == 1

    terminal = store.read_state(job_id)
    assert terminal["status"] == "failed"
    assert terminal["last_error"]["type"] == "OSError"
    assert "injected identity failure" in terminal["last_error"]["message"]


def test_synthetic_worker_exception_during_cancelling_records_cooperative_cancel(
    ascii_tmp_path, monkeypatch
):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = JobManager(
        ascii_tmp_path / "jobs",
        preflight=lambda **_kwargs: {"success": True, "ready": True},
    )
    monkeypatch.setattr(
        manager,
        "_launch_worker",
        lambda _job_id, _module: process_identity(os.getpid()),
    )
    result = manager.submit(envelope)
    job_id = result["job_id"]
    store = manager.store
    store.request_cancel(job_id, requester_identity=process_identity(os.getpid()))

    def broken_identity(self, *_args, **_kwargs):
        raise OSError("injected cancel-window failure")

    monkeypatch.setattr(JobStore, "bind_worker_identity", broken_identity)

    observed = {}

    def record_observed(self, job_id_arg, *, attempt, message, worker_error):
        observed["attempt"] = attempt
        observed["message"] = message
        observed["worker_error"] = worker_error

    monkeypatch.setattr(JobStore, "record_cooperative_cancel_observed", record_observed)

    assert run_adjoint_worker(str(store.root), job_id) == 0
    assert observed["message"] == "Stopped during synthetic validation"
    assert observed["worker_error"]["type"] == "OSError"
    assert store.read_state(job_id)["status"] != "failed"


def test_real_adjoint_worker_dispatches_validated_runtime_and_persists_rows(
    ascii_tmp_path, monkeypatch
):
    envelope, _, manifest = _write_manifest(ascii_tmp_path)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["synthetic_mode"] = False
    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    manifest.write_bytes(payload)
    envelope["submission_manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    manager = JobManager(
        ascii_tmp_path / "jobs",
        preflight=lambda **_kwargs: {"success": True, "ready": True},
    )
    monkeypatch.setattr(
        manager, "_launch_worker", lambda _job_id, _module: process_identity(os.getpid())
    )
    result = manager.submit(envelope)

    class Ownership:
        def __init__(self, *_args, **_kwargs):
            pass

        def preflight(self, **_kwargs):
            return {"ready": True}

        def acquire(self, **_kwargs):
            return {"success": True}

        def release(self):
            return {"success": True}

    monkeypatch.setitem(
        sys.modules,
        "comsol_mcp.tools.ownership",
        SimpleNamespace(SolverOwnership=Ownership),
    )
    monkeypatch.setattr(
        native_adjoint_runtime,
        "execute_native_adjoint_optimization",
        lambda _spec, _directory: {
            "success": True,
            "final_variables_si": {"patch_length_x": 8.0e-7},
            "fresh_forward_objective": 0.6,
            "cleanup": {"source_unchanged": True, "client_clear": True},
        },
    )

    assert run_adjoint_worker(str(manager.store.root), result["job_id"]) == 0
    terminal = manager.store.read_state(result["job_id"])
    rows = manager.store.job_dir(result["job_id"]) / "optimization_rows.jsonl"
    persisted = read_adjoint_rows(
        rows,
        job_fingerprint=manager.store.read_spec(result["job_id"])["spec_fingerprint"],
    )
    assert terminal["status"] == "completed"
    assert terminal["solver_started"] is True
    assert [row["kind"] for row in persisted] == ["gradient", "iteration"]
    assert persisted[0]["payload"]["evidence_state"] == "native_unchecked"


def _real_adjoint_envelope(ascii_tmp_path):
    envelope, _, manifest = _write_manifest(ascii_tmp_path)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["synthetic_mode"] = False
    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    manifest.write_bytes(payload)
    envelope["submission_manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    return envelope


def test_real_adjoint_submission_arms_the_wall_watchdog(ascii_tmp_path):
    envelope = _real_adjoint_envelope(ascii_tmp_path)
    manager = JobManager(
        ascii_tmp_path / "jobs",
        preflight=lambda **_kwargs: {"success": True, "ready": True},
    )
    result = manager.submit(envelope)

    record = read_json(manager.store.job_dir(result["job_id"]) / "wall-watchdog.json")
    assert record["status"] == "armed"
    assert record["budget_source"] == "optimizer.budget.max_wall_time_seconds"
    assert record["budget_seconds"] == 3600


def test_native_adjoint_worker_fails_closed_without_armed_watchdog(ascii_tmp_path, monkeypatch):
    envelope = _real_adjoint_envelope(ascii_tmp_path)
    manager = JobManager(
        ascii_tmp_path / "jobs",
        preflight=lambda **_kwargs: {"success": True, "ready": True},
    )
    monkeypatch.setattr(
        manager, "_launch_worker", lambda _job_id, _module: process_identity(os.getpid())
    )
    result = manager.submit(envelope)
    job_id = result["job_id"]
    (manager.store.job_dir(job_id) / "wall-watchdog.json").unlink()

    assert run_adjoint_worker(str(manager.store.root), job_id) == 1

    terminal = manager.store.read_state(job_id)
    assert terminal["status"] == "failed"
    assert "was not armed" in terminal["last_error"]["message"]


def _minimal_adjoint_runtime_spec(tmp_path):
    source = tmp_path / "source.mph"
    source.write_bytes(b"adjoint runtime fixture")
    return {
        "source_model_path": str(source),
        "source_model_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "support": {
            "support_fingerprint": "a" * 64,
            "objective": {"direction": "maximize"},
        },
        "optimizer": {
            "method": "gcmma",
            "optimizer_fingerprint": "b" * 64,
            "budget": {"cores": 1, "max_solves": 5, "max_wall_time_seconds": 60},
        },
        "cores": 1,
        "version": "6.4",
    }


@pytest.mark.parametrize("total", [True, False, 0, 1, 2, 3.0, None])
def test_native_runtime_refuses_insufficient_solve_budget_before_client(ascii_tmp_path, total):
    spec = _minimal_adjoint_runtime_spec(ascii_tmp_path)
    spec["optimizer"]["budget"]["max_solves"] = total
    root = ascii_tmp_path / "refused-budget"

    def unexpected_client(**kwargs):
        pytest.fail("insufficient budget must not construct a native client")

    with pytest.raises(ValueError, match="baseline.*optimization.*finalist"):
        native_adjoint_runtime.execute_native_adjoint_optimization(
            spec, root, client_factory=unexpected_client
        )
    assert not root.exists()


def test_native_runtime_reserves_non_optimizer_solves():
    assert native_adjoint_runtime._optimizer_solve_budget(3) == 1
    assert native_adjoint_runtime._optimizer_solve_budget(25) == 23


@pytest.mark.parametrize(
    ("evaluated", "solved", "error"),
    [
        (1.1e-6, 1e-6, "evaluated wavelength changed"),
        (1e-6, 1.1e-6, "solved wavelength changed"),
        (1.1e-6, 1.1e-6, "evaluated wavelength changed"),
        (float("nan"), 1e-6, "nonfinite"),
        (1e-6, float("inf"), "nonfinite"),
    ],
)
def test_finalist_wavelength_checks_bind_both_values_to_request(evaluated, solved, error):
    with pytest.raises(ValueError, match=error):
        native_adjoint_runtime._validate_wavelengths(1e-6, evaluated, solved)


def test_finalist_wavelength_checks_accept_matching_values():
    native_adjoint_runtime._validate_wavelengths(1e-6, 1e-6, 1e-6)


def test_native_runtime_applies_remaining_budget_to_actual_optimizer(ascii_tmp_path, monkeypatch):
    spec = _minimal_adjoint_runtime_spec(ascii_tmp_path)
    spec["initial_values"] = [856.0]
    spec["support"]["objective"]["expression"] = "objective"
    seen = {}
    feature = SimpleNamespace(set=lambda key, value: seen.__setitem__(key, value))
    features = SimpleNamespace(remove=lambda tag: None)
    study = SimpleNamespace(feature=lambda tag=None: features if tag is None else feature)
    model = SimpleNamespace(
        java=SimpleNamespace(
            save=lambda path, copy: Path(path).write_bytes(b"configured"),
            study=lambda tag: study,
        ),
        evaluate=lambda *args, **kwargs: [0.5],
    )
    study.run = lambda: None
    client = SimpleNamespace(load=lambda path: model, remove=lambda model: None, clear=lambda: None)
    monkeypatch.setattr(native_adjoint_runtime, "configure_native_adjoint", lambda *a, **kw: None)
    monkeypatch.setattr(native_adjoint_runtime, "_freeze_wavelength", lambda *a: None)
    monkeypatch.setattr(native_adjoint_runtime, "_forward_sweep_dataset", lambda model: object())

    def stop_before_optimizer(*args):
        assert seen["nsolvemax"] == "3"
        raise RuntimeError("verified optimizer budget")

    monkeypatch.setattr(native_adjoint_runtime, "_configure_solver", stop_before_optimizer)
    with pytest.raises(RuntimeError, match="verified optimizer budget"):
        native_adjoint_runtime.execute_native_adjoint_optimization(
            spec, ascii_tmp_path / "budget-runtime", client_factory=lambda **kwargs: client
        )


def test_native_runtime_passes_manifest_initial_values_to_adapter(ascii_tmp_path, monkeypatch):
    envelope, _source, _manifest = _write_manifest(ascii_tmp_path)
    spec = expand_adjoint_optimization_manifest(envelope)
    spec["initial_values"] = [800.0]
    seen = []

    def configure(_backend, support, optimizer, *, initial_values):
        seen.append((initial_values, support["variables"][0]["baseline"]))
        raise RuntimeError("stop after configuration boundary")

    monkeypatch.setattr(native_adjoint_runtime, "configure_native_adjoint", configure)
    client = SimpleNamespace(load=lambda _path: object(), clear=lambda: None)
    with pytest.raises(RuntimeError, match="configuration boundary"):
        native_adjoint_runtime.execute_native_adjoint_optimization(
            spec, ascii_tmp_path / "initial-runtime", client_factory=lambda **_kwargs: client
        )
    assert seen == [([800.0], 856.0)]


def test_freeze_wavelength_preserves_caller_initial_vector(monkeypatch):
    from comsol_mcp.contracts.derivative_support import normalize_derivative_support

    values = {}
    nodes = {
        tag: SimpleNamespace(set=lambda key, value, tag=tag: values.__setitem__((tag, key), value))
        for tag in ("sweep1", "step1", "sens_a71")
    }
    study = SimpleNamespace(feature=lambda tag: nodes[tag])
    model = SimpleNamespace(
        java=SimpleNamespace(
            study=lambda tag: study,
            param=lambda: SimpleNamespace(
                set=lambda key, value: values.__setitem__(("param", key), value)
            ),
        )
    )
    monkeypatch.setattr(
        native_adjoint_runtime, "_set_vector", lambda node, key, value: node.set(key, value)
    )
    support = normalize_derivative_support(_support())
    native_adjoint_runtime._freeze_wavelength(model, support, [800.0])
    assert values[("sens_a71", "initval")] == ["800[nm]"]
    assert values[("sens_a71", "pname")] == ["patch_length_x"]
    assert support["variables"][0]["baseline"] == 856.0


def test_cleanup_failure_records_errors_and_preserves_original_error(ascii_tmp_path):
    class FailingClearClient:
        def load(self, _path):
            raise RuntimeError("boom")

        def clear(self):
            raise OSError("clear failed")

    spec = _minimal_adjoint_runtime_spec(ascii_tmp_path)
    workdir = ascii_tmp_path / "runtime-work"

    with pytest.raises(RuntimeError, match="boom"):
        native_adjoint_runtime.execute_native_adjoint_optimization(
            spec,
            workdir,
            client_factory=lambda **_kwargs: FailingClearClient(),
        )

    receipt = json.loads((workdir / "native-optimizer-receipt.json").read_text(encoding="utf-8"))
    assert receipt["success"] is False
    assert receipt["cleanup"]["client_clear"] is False
    assert receipt["cleanup"]["source_unchanged"] is True
    assert receipt["cleanup"]["cleanup_errors"] == ["client_clear:OSError:clear failed"]


def test_clean_failure_receipt_omits_empty_cleanup_errors(ascii_tmp_path):
    class CleanClient:
        def load(self, _path):
            raise RuntimeError("boom")

        def clear(self):
            return None

    spec = _minimal_adjoint_runtime_spec(ascii_tmp_path)
    workdir = ascii_tmp_path / "runtime-clean"

    with pytest.raises(RuntimeError, match="boom"):
        native_adjoint_runtime.execute_native_adjoint_optimization(
            spec,
            workdir,
            client_factory=lambda **_kwargs: CleanClient(),
        )

    receipt = json.loads((workdir / "native-optimizer-receipt.json").read_text(encoding="utf-8"))
    assert receipt["success"] is False
    assert receipt["cleanup"] == {"client_clear": True, "source_unchanged": True}


def test_mapping_refusal_retains_failed_extrema():
    evidence = {}
    model = SimpleNamespace(evaluate=lambda *a, **kw: [1.0, -0.25])
    with pytest.raises(ValueError, match="geometry mapping"):
        native_adjoint_runtime._mapping_evidence(
            model, object(), _execution_policy(), evidence=evidence
        )
    assert evidence["geometry"]["minimum"] == -0.25
    assert evidence["geometry"]["maximum"] == 1.0
    assert evidence["geometry"]["sample_count"] == 2


def test_constraint_refusal_retains_raw_residual():
    evidence = []
    model = SimpleNamespace(evaluate=lambda *a, **kw: 1.2)
    support = {
        "constraints": [
            {"constraint_id": "bound", "expression": "x", "unit": "m", "lower": None, "upper": 1.0}
        ]
    }
    with pytest.raises(ValueError, match="constraint violates"):
        native_adjoint_runtime._constraint_evidence(
            model, object(), support, 0.01, evidence=evidence
        )
    assert evidence[0]["value"] == 1.2
    assert evidence[0]["violation"] == pytest.approx(0.2)
    assert evidence[0]["unit"] == "m"
    assert evidence[0]["tolerance"] == 0.01


@pytest.mark.parametrize("absorption", [0.1, -0.004867])
def test_explicit_runtime_loading_checks_physics_and_actual_solve_budget(
    ascii_tmp_path, monkeypatch, absorption
):
    spec = _minimal_adjoint_runtime_spec(ascii_tmp_path)
    spec["execution_policy"] = _execution_policy()
    spec["resource_policy"] = {"rules": {"max_mesh_elements": 1000}}
    spec["initial_values"] = [800.0, 900.0]
    spec["support"].update(
        variables=[
            {"variable_id": "patch_length_x", "unit": "nm"},
            {"variable_id": "patch_length_y", "unit": "nm"},
        ],
        constraints=[],
    )
    spec["support"]["objective"].update(expression="objective", wavelength_um=1.0)
    spec["optimizer"].update(constraint_tolerance=0.0)
    spec["optimizer"]["budget"].update(max_solves=25, max_disk_bytes=100000)
    values = {}
    calls = []
    node = SimpleNamespace(set=lambda k, v: values.__setitem__(k, v), getString=lambda k: values[k])
    study = SimpleNamespace(
        feature=lambda tag=None: SimpleNamespace(remove=lambda tag: None) if tag is None else node,
        run=lambda: calls.append("study"),
    )
    solution = SimpleNamespace(feature=lambda tag: node)
    deformation = SimpleNamespace(prop=lambda tag: node)
    mesh = SimpleNamespace(run=lambda: calls.append("mesh"))
    component = SimpleNamespace(physics=lambda tag: deformation, mesh=lambda tag: mesh)
    java = SimpleNamespace(
        component=lambda tag: component,
        study=lambda tag: study,
        sol=lambda tag: solution,
        param=lambda: node,
        save=lambda path, copy: Path(path).write_bytes(b"configured"),
    )

    def evaluate(expression, **kwargs):
        if isinstance(expression, list):
            if len(expression) == 3:
                return [0.6, 800.0, 900.0]
            return [0.6, 0.3, 0.7 - absorption, absorption, 1e-6, 1e-6]
        return {
            "objective": 0.6,
            "patch_length_x": 8e-7,
            "patch_length_y": 9e-7,
            "reldetjac": 1.0,
            "comp1.material.detF": 0.5,
        }[expression]

    model = SimpleNamespace(java=java, evaluate=evaluate)
    client = SimpleNamespace(load=lambda path: model, remove=lambda model: None, clear=lambda: None)
    dataset = SimpleNamespace(tag=lambda: "bound_dataset")
    monkeypatch.setattr(
        native_adjoint_runtime,
        "configure_native_adjoint",
        lambda *a, **kw: {"controls": {"deformed_geometry": {"patch_domain": 3}}},
    )
    monkeypatch.setattr(native_adjoint_runtime, "_freeze_wavelength", lambda *a: None)
    monkeypatch.setattr(
        native_adjoint_runtime,
        "_configure_solver",
        lambda *a: {"solution_tag": "actual_solver", "feature_tag": "o1"},
    )
    monkeypatch.setattr(native_adjoint_runtime, "_dataset_for_solution", lambda *a: dataset)
    monkeypatch.setattr(native_adjoint_runtime, "_forward_sweep_dataset", lambda *a: dataset)
    monkeypatch.setattr(
        native_adjoint_runtime,
        "_mesh_statistics",
        lambda *a: {"element_count": 100, "minimum_quality": 0.5},
    )
    monkeypatch.setattr(native_adjoint_runtime, "_admit_policy_mesh", lambda *a: None)
    monkeypatch.setattr(
        native_adjoint_runtime,
        "_solved_dimensions",
        lambda model, dataset, domain, controls: dict(controls),
    )

    def load_step(*args):
        calls.append("loading")
        return "actual_solver", {"initmethod": "sol"}

    monkeypatch.setattr(native_adjoint_runtime, "_run_loading_solution", load_step)
    root = ascii_tmp_path / "explicit-runtime"
    if absorption < 0:
        with pytest.raises(ValueError, match="passivity"):
            native_adjoint_runtime.execute_native_adjoint_optimization(
                spec, root, client_factory=lambda **kw: client
            )
    else:
        result = native_adjoint_runtime.execute_native_adjoint_optimization(
            spec, root, client_factory=lambda **kw: client
        )
        assert result["success"]
        assert result["final_variables_si"] == {"patch_length_x": 8e-7, "patch_length_y": 9e-7}
    receipt = json.loads((root / "native-optimizer-receipt.json").read_text())
    assert receipt["success"] == (absorption >= 0)
    assert receipt["physical_values"]["ewfd.Atotal"] == absorption
    assert values["nsolvemax"] == "20"
    assert calls.count("loading") == 4
    assert receipt["cleanup"] == {"client_clear": True, "source_unchanged": True}


def test_synthetic_worker_resume_does_not_duplicate_persisted_row(ascii_tmp_path, monkeypatch):
    """D-01: a crash between the gradient and iteration row appends leaves
    only the gradient row persisted; the resumed worker must add exactly the
    MISSING iteration row instead of duplicating the gradient row."""
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = JobManager(
        ascii_tmp_path / "jobs",
        preflight=lambda **_kwargs: {"success": True, "ready": True},
    )
    monkeypatch.setattr(
        manager,
        "_launch_worker",
        lambda _job_id, _module: process_identity(os.getpid()),
    )
    result = manager.submit(envelope)
    job_id = result["job_id"]
    store = manager.store
    rows_path = store.job_dir(job_id) / "optimization_rows.jsonl"
    spec_fingerprint = store.read_spec(job_id)["spec_fingerprint"]
    from comsol_mcp.jobs.adjoint_rows import append_adjoint_row, domain_sha256_v2, read_adjoint_rows

    # Simulate the crash window: the gradient row was persisted, the
    # iteration row was not, and the job returned to a startable state.
    append_adjoint_row(
        rows_path,
        job_fingerprint=spec_fingerprint,
        attempt=1,
        kind="gradient",
        payload={
            "iteration_id": "it-0",
            "gradient_fingerprint": domain_sha256_v2(
                "comsol_mcp.synthetic_gradient", {"values": [0.0]}
            ),
            "check_fingerprint": domain_sha256_v2(
                "comsol_mcp.synthetic_gradient_check", {"passed": True}
            ),
            "evidence_state": "gradient_validated",
        },
    )
    store.update_state(job_id, "starting", event="resume_after_crash_window")

    assert run_adjoint_worker(str(store.root), job_id) == 0
    rows = [
        row
        for row in read_adjoint_rows(rows_path, job_fingerprint=spec_fingerprint)
        if row.get("attempt") == 1
    ]
    kinds = [row["kind"] for row in rows]
    assert kinds.count("gradient") == 1
    assert kinds.count("iteration") == 1
