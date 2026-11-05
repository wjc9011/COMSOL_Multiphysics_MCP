"""Heavy-load durable state reader/writer stress without COMSOL."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import src.jobs.store as store_module
from src.jobs.store import JOB_SCHEMA_VERSION, JobStore


def _stress_root(ascii_tmp_path: Path) -> Path:
    root = ascii_tmp_path / "durable_state"
    root.mkdir(parents=True)
    return root


def _archive_failure(root: Path) -> Path:
    archive_root = root.parent / "failure_archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    archive = archive_root / root.name
    if archive.exists():
        shutil.rmtree(archive)
    shutil.copytree(root, archive)
    return archive


def _raise_stress_failure(root: Path, message: str, primary: BaseException) -> None:
    try:
        archive = _archive_failure(root)
    except Exception as archive_error:
        failure = AssertionError(
            f"{message}; evidence archive failed ({type(archive_error).__name__})"
        )
        raise failure from primary
    raise AssertionError(f"{message}; evidence archived at {archive}") from primary


def test_archive_failure_preserves_the_primary_stress_exception(tmp_path, monkeypatch):
    primary = RuntimeError("primary stress failure")
    monkeypatch.setattr(
        f"{__name__}._archive_failure",
        lambda _root: (_ for _ in ()).throw(OSError("archive failure")),
    )

    with pytest.raises(AssertionError, match="archive failed") as caught:
        _raise_stress_failure(tmp_path, "stress failed", primary)

    assert caught.value.__cause__ is primary


def test_concurrent_state_readers_writers_survive_sharing_violations(monkeypatch, ascii_tmp_path):
    root = _stress_root(ascii_tmp_path)
    try:
        store = JobStore(root)
        job_id = store.create(
            {"schema_version": JOB_SCHEMA_VERSION, "job_type": "stress"},
            {
                "schema_version": JOB_SCHEMA_VERSION,
                "status": "running",
                "attempt": 1,
                "terminal_marker": False,
            },
        )
        state_path = store.job_dir(job_id) / "state.json"
        original_replace = store_module.os.replace
        original_read_bytes = store_module.read_file_bytes_bounded
        counter_lock = threading.Lock()
        replace_calls = 0
        read_calls = 0
        injected_replace_failures = 0
        injected_read_failures = 0

        def flaky_replace(source, destination):
            nonlocal replace_calls, injected_replace_failures
            if Path(destination) == state_path:
                with counter_lock:
                    replace_calls += 1
                    call = replace_calls
                if call % 9 == 0:
                    with counter_lock:
                        injected_replace_failures += 1
                    raise PermissionError("injected state replace sharing violation")
            return original_replace(source, destination)

        def flaky_read_bytes(path, *args, **kwargs):
            nonlocal read_calls, injected_read_failures
            if path == state_path:
                with counter_lock:
                    read_calls += 1
                    call = read_calls
                if call % 13 == 0:
                    with counter_lock:
                        injected_read_failures += 1
                    raise PermissionError("injected state read sharing violation")
            return original_read_bytes(path, *args, **kwargs)

        monkeypatch.setattr(store_module.os, "replace", flaky_replace)
        monkeypatch.setattr(store_module, "read_file_bytes_bounded", flaky_read_bytes)
        stop = threading.Event()
        observations: list[dict] = []
        observation_lock = threading.Lock()

        def writer(writer_id: int) -> None:
            for sequence in range(18):
                state = store.update_state(
                    job_id,
                    patch={f"writer_{writer_id}": sequence},
                )
                assert state["status"] == "running"

        def reader() -> None:
            trailing = 0
            while not stop.is_set() or trailing < 2:
                state = store.read_state(job_id)
                json.dumps(state, sort_keys=True)
                with observation_lock:
                    observations.append(state)
                if stop.is_set():
                    trailing += 1

        started = time.monotonic()
        executor = ThreadPoolExecutor(max_workers=8)
        try:
            readers = [executor.submit(reader) for _ in range(5)]
            writers = [executor.submit(writer, writer_id) for writer_id in range(3)]
            for future in writers:
                future.result(timeout=20)
            final = store.update_state(
                job_id,
                "completed",
                patch={"terminal_marker": True},
            )
            stop.set()
            for future in readers:
                future.result(timeout=20)
        finally:
            stop.set()
            executor.shutdown(wait=True, cancel_futures=True)
        elapsed = time.monotonic() - started

        with pytest.raises(ValueError, match="Completed job state is immutable"):
            store.update_state(job_id, patch={"late_write": True})
        # An empty patch must be rejected too: it would otherwise rewrite the
        # completed artifact with a fresh updated_at_epoch.
        frozen = store.read_state(job_id)
        with pytest.raises(ValueError, match="Completed job state is immutable"):
            store.update_state(job_id, patch={})
        assert store.read_state(job_id) == frozen
        assert final["status"] == "completed"
        assert final["terminal_marker"] is True
        assert {key: final[key] for key in ("writer_0", "writer_1", "writer_2")} == {
            "writer_0": 17,
            "writer_1": 17,
            "writer_2": 17,
        }
        assert observations
        assert all(item["status"] in {"running", "completed"} for item in observations)
        assert all(
            item.get("terminal_marker") is True
            for item in observations
            if item["status"] == "completed"
        )
        assert injected_replace_failures >= 5
        assert injected_read_failures >= 5
        assert elapsed < 20
        assert not list(store.job_dir(job_id).glob(".*.tmp"))
        assert not (store.job_dir(job_id) / ".state.lock").exists()
        assert store.read_state(job_id) == final
    except BaseException as exc:
        _raise_stress_failure(root, "durable-state stress failed", exc)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows sharing semantics")
def test_real_windows_exclusive_reader_does_not_corrupt_durable_state(ascii_tmp_path):
    root = _stress_root(ascii_tmp_path)
    try:
        store = JobStore(root)
        job_id = store.create(
            {"schema_version": JOB_SCHEMA_VERSION, "job_type": "windows-sharing-stress"},
            {
                "schema_version": JOB_SCHEMA_VERSION,
                "status": "running",
                "attempt": 1,
                "terminal_marker": False,
                "sequence": 0,
            },
        )
        state_path = store.job_dir(job_id) / "state.json"
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        invalid_handle = ctypes.c_void_p(-1).value
        first_lock = threading.Event()
        stop = threading.Event()
        lock_count = 0
        observations: list[dict] = []
        observation_lock = threading.Lock()

        def exclusive_locker() -> None:
            nonlocal lock_count
            for _ in range(80):
                handle = create_file(
                    str(state_path),
                    0x80000000,
                    0,
                    None,
                    3,
                    0x80,
                    None,
                )
                if handle == invalid_handle:
                    time.sleep(0.002)
                    continue
                lock_count += 1
                first_lock.set()
                try:
                    time.sleep(0.008)
                finally:
                    if not close_handle(handle):
                        raise ctypes.WinError(ctypes.get_last_error())
                time.sleep(0.002)

        def writer() -> None:
            try:
                assert first_lock.wait(timeout=2)
                for sequence in range(1, 61):
                    store.update_state(job_id, patch={"sequence": sequence})
                store.update_state(
                    job_id,
                    "completed",
                    patch={"terminal_marker": True},
                )
            finally:
                stop.set()

        def reader() -> None:
            trailing = 0
            while not stop.is_set() or trailing < 20:
                state = store.read_state(job_id)
                with observation_lock:
                    observations.append(state)
                if stop.is_set():
                    trailing += 1

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(exclusive_locker), executor.submit(writer)]
            futures.extend(executor.submit(reader) for _ in range(3))
            for future in futures:
                future.result(timeout=30)
        elapsed = time.monotonic() - started
        final = store.read_state(job_id)

        assert lock_count >= 10
        assert elapsed < 30
        assert final["status"] == "completed"
        assert final["sequence"] == 60
        assert final["terminal_marker"] is True
        assert observations
        assert all(item["status"] in {"running", "completed"} for item in observations)
        assert all(
            item.get("terminal_marker") is True
            for item in observations
            if item["status"] == "completed"
        )
        assert not list(store.job_dir(job_id).glob(".*.tmp"))
        assert not (store.job_dir(job_id) / ".state.lock").exists()
    except BaseException as exc:
        _raise_stress_failure(root, "durable-state Windows sharing stress failed", exc)
    finally:
        shutil.rmtree(root, ignore_errors=True)
