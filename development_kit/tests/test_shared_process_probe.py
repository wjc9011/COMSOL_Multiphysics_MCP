"""Process-only tests for redacted shared Desktop/Server inventory."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from contextlib import nullcontext
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
import src.shared_session.process_probe as process_probe
from src.shared_session.preflight import classify_shared_server_preflight
from src.shared_session.process_probe import (
    _is_primary_desktop_window,
    _listener_records,
    _process_records,
    collect_shared_preflight_snapshot,
)


def _record(pid, parent, name, command, executable=None):
    return {
        "pid": pid,
        "parent_pid": parent,
        "name": name,
        "create_time": float(pid),
        "command_line": command,
        "executable": executable or f"C:/Program Files/COMSOL/{name}",
    }


def test_collector_redacts_paths_and_ignores_declared_process_children():
    records = [
        _record(10, 0, "comsol.exe", ["comsol.exe"]),
        _record(11, 10, "comsolhelper.exe", ["comsolhelper.exe"]),
        _record(20, 0, "comsolmphserver.exe", ["comsolmphserver.exe", "-port", "2036"]),
        _record(21, 20, "java.exe", ["java.exe", "comsol", "worker"]),
    ]
    snapshot = collect_shared_preflight_snapshot(
        process_provider=lambda: records,
        listener_provider=lambda: [{"host": "127.0.0.1", "port": 2036, "pid": 20}],
        window_provider=lambda: {10: {"window_count": 1, "responding": True}},
        version_provider=lambda path: "6.4.0.293",
        clock=lambda: 1000.0,
    )

    assert [item["kind"] for item in snapshot["processes"]] == ["comsol_desktop", "comsol_server"]
    assert all("executable" not in item for item in snapshot["processes"])
    assert all("command_line" not in item for item in snapshot["processes"])
    serialized = str(snapshot)
    assert "Program Files" not in serialized


def test_process_collection_never_attempts_to_import_mph():
    code = r"""
import importlib.abc
import sys

class BlockMph(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "mph" or fullname.startswith("mph."):
            raise AssertionError(f"forbidden mph import attempted: {fullname}")
        return None

sys.meta_path.insert(0, BlockMph())
from comsol_mcp.shared_session.process_probe import collect_shared_preflight_snapshot
snapshot = collect_shared_preflight_snapshot(
    process_provider=list,
    listener_provider=list,
    window_provider=dict,
    version_provider=lambda _path: None,
    clock=lambda: 1000.0,
)
assert snapshot["processes"] == []
assert "mph" not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_collector_exposes_external_mph_as_a_collision_without_version_requirement():
    records = [
        _record(10, 0, "comsol.exe", ["comsol.exe"]),
        _record(20, 0, "comsolmphserver.exe", ["comsolmphserver.exe", "-port", "2036"]),
        _record(30, 0, "python.exe", ["python.exe", "-c", "import mph; mph.Client()"]),
    ]
    snapshot = collect_shared_preflight_snapshot(
        process_provider=lambda: records,
        listener_provider=lambda: [{"host": "127.0.0.1", "port": 2036, "pid": 20}],
        window_provider=lambda: {10: {"window_count": 1, "responding": True}},
        version_provider=lambda path: "6.4.0.310",
        clock=lambda: 1000.0,
    )

    result = classify_shared_server_preflight(
        endpoint={"host": "localhost", "port": 2036},
        first_probe=snapshot,
        second_probe={**snapshot, "observed_at_epoch": 1001.0},
    )

    assert [item["kind"] for item in snapshot["processes"]] == [
        "comsol_desktop",
        "comsol_server",
        "mph_client",
    ]
    assert result["state"] == "unclassified_comsol_or_mph_collision"


def test_unreadable_comsol_file_version_reaches_explicit_classifier_state():
    records = [
        _record(10, 0, "comsol.exe", ["comsol.exe"]),
        _record(20, 0, "comsolmphserver.exe", ["comsolmphserver.exe"]),
    ]
    snapshot = collect_shared_preflight_snapshot(
        process_provider=lambda: records,
        listener_provider=lambda: [{"host": "127.0.0.1", "port": 2036, "pid": 20}],
        window_provider=lambda: {10: {"window_count": 1, "responding": True}},
        version_provider=lambda path: None,
        clock=lambda: 1000.0,
    )

    result = classify_shared_server_preflight(
        endpoint={"host": "127.0.0.1", "port": 2036},
        first_probe=snapshot,
        second_probe={**snapshot, "observed_at_epoch": 1001.0},
    )

    assert result["state"] == "unsupported_or_ambiguous_comsol_version"


def test_collector_excludes_current_mcp_process_identity():
    records = [
        _record(10, 0, "python.exe", ["python.exe", "-m", "src.server", "import mph"]),
    ]
    snapshot = collect_shared_preflight_snapshot(
        process_provider=lambda: records,
        listener_provider=list,
        window_provider=dict,
        version_provider=lambda path: None,
        exclude_pids={10},
        clock=lambda: 1000.0,
    )

    assert snapshot["processes"] == []


def test_collector_keeps_excluded_comsol_roots_for_descendant_filtering():
    records = [
        _record(20, 0, "comsolmphserver.exe", ["comsolmphserver.exe"]),
        _record(21, 20, "comsolhelper.exe", ["comsolhelper.exe"]),
    ]

    snapshot = collect_shared_preflight_snapshot(
        process_provider=lambda: records,
        listener_provider=list,
        window_provider=dict,
        version_provider=lambda path: "6.4.0.293",
        exclude_pids={20},
        clock=lambda: 1000.0,
    )

    assert snapshot["processes"] == []


def test_access_denied_process_metadata_marks_inventory_incomplete(monkeypatch):
    class InaccessibleServer:
        pid = 20

        def oneshot(self):
            return nullcontext()

        def cmdline(self):
            raise psutil.AccessDenied(pid=self.pid)

        def exe(self):
            raise psutil.AccessDenied(pid=self.pid)

        def ppid(self):
            return 0

        def name(self):
            return "comsolmphserver.exe"

        def create_time(self):
            return 20.0

    monkeypatch.setattr(psutil, "process_iter", lambda: [InaccessibleServer()])

    records, complete = _process_records()
    snapshot = collect_shared_preflight_snapshot(
        process_provider=lambda: (records, complete),
        listener_provider=list,
        window_provider=dict,
        version_provider=lambda path: "6.4.0.293",
        clock=lambda: 1000.0,
    )

    assert snapshot["inventory_complete"] is False
    assert [item["kind"] for item in snapshot["processes"]] == ["comsol_server"]


def test_process_inventory_bound_is_inclusive(monkeypatch):
    class Process:
        def __init__(self, pid):
            self.pid = pid

        def oneshot(self):
            return nullcontext()

        def cmdline(self):
            return ["python.exe"]

        def exe(self):
            return "C:/Python314/python.exe"

        def ppid(self):
            return 0

        def name(self):
            return "python.exe"

        def create_time(self):
            return float(self.pid)

    monkeypatch.setattr(process_probe, "MAX_PROCESS_RECORDS", 2)
    monkeypatch.setattr(psutil, "process_iter", lambda: [Process(1), Process(2)])

    records, complete = _process_records()

    assert len(records) == 2
    assert complete is True

    monkeypatch.setattr(
        psutil,
        "process_iter",
        lambda: [Process(1), Process(2), Process(3)],
    )
    with pytest.raises(RuntimeError, match="process inventory exceeds"):
        _process_records()


def test_repository_path_text_does_not_impersonate_comsol_processes():
    records = [
        _record(
            10,
            0,
            "python.exe",
            ["python.exe", "C:/work/COMSOL_Multiphysics_MCP/probe.py"],
            executable="C:/Python314/python.exe",
        ),
        _record(
            20,
            0,
            "Code.exe",
            ["Code.exe", "C:/work/COMSOL_Multiphysics_MCP"],
            executable="C:/Apps/Code.exe",
        ),
        _record(
            30,
            0,
            "powershell.exe",
            ["powershell.exe", "Get-ChildItem", "COMSOL_Multiphysics_MCP"],
            executable="C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        ),
        _record(
            40,
            0,
            "java.exe",
            ["java.exe", "C:/work/COMSOL_Multiphysics_MCP", "server"],
            executable="C:/Java/bin/java.exe",
        ),
    ]

    snapshot = collect_shared_preflight_snapshot(
        process_provider=lambda: records,
        listener_provider=list,
        window_provider=lambda: {20: {"window_count": 1, "responding": True}},
        version_provider=lambda path: "unexpected",
        clock=lambda: 1000.0,
    )

    assert snapshot["processes"] == []


def test_explicit_comsol_executable_identity_does_not_require_command_substrings():
    records = [
        _record(
            10,
            0,
            "comsol.exe",
            ["--neutral-desktop-command"],
        ),
        _record(
            20,
            0,
            "comsolmphserver.exe",
            ["--neutral-server-command"],
        ),
    ]

    snapshot = collect_shared_preflight_snapshot(
        process_provider=lambda: records,
        listener_provider=list,
        window_provider=lambda: {10: {"window_count": 1, "responding": True}},
        version_provider=lambda path: "6.4.0.293",
        clock=lambda: 1000.0,
    )

    assert [item["kind"] for item in snapshot["processes"]] == [
        "comsol_desktop",
        "comsol_server",
    ]


def test_comsol_64_ui_process_is_classified_as_desktop():
    records = [
        _record(
            10,
            0,
            "ComsolUI.exe",
            ["C:/Program Files/COMSOL/COMSOL64/Multiphysics/bin/win64/ComsolUI.exe"],
        )
    ]

    snapshot = collect_shared_preflight_snapshot(
        process_provider=lambda: records,
        listener_provider=list,
        window_provider=lambda: {10: {"window_count": 1, "responding": True}},
        version_provider=lambda path: "6.4.0.293",
        clock=lambda: 1000.0,
    )

    assert snapshot["processes"] == [
        {
            "pid": 10,
            "parent_pid": 0,
            "kind": "comsol_desktop",
            "create_time": 10.0,
            "command_signature": snapshot["processes"][0]["command_signature"],
            "file_version": "6.4.0.293",
            "window_count": 1,
            "responding": True,
        }
    ]


def test_comsol_64_window_filter_rejects_observed_auxiliary_windows():
    assert _is_primary_desktop_window(
        title="Untitled.mph - COMSOL Multiphysics",
        class_name="HwndWrapper[ComsolUI.exe;;]",
    )
    assert not _is_primary_desktop_window(
        title="",
        class_name="ActiproWindowChromeShadow",
    )
    assert not _is_primary_desktop_window(
        title="shadow helper",
        class_name="ActiproWindowChromeShadow",
    )
    assert not _is_primary_desktop_window(
        title="ActiproWindowChromeShadow",
        class_name="HwndWrapper[ComsolUI.exe;;]",
    )
    assert not _is_primary_desktop_window(
        title="",
        class_name="PseudoConsoleWindow",
    )


def test_listener_collector_preserves_wildcard_and_discards_remote_bind(monkeypatch):
    connections = [
        SimpleNamespace(
            status=psutil.CONN_LISTEN,
            pid=20,
            laddr=SimpleNamespace(ip="::", port=2036),
        ),
        SimpleNamespace(
            status=psutil.CONN_LISTEN,
            pid=30,
            laddr=SimpleNamespace(ip="192.168.1.2", port=2036),
        ),
    ]
    monkeypatch.setattr(psutil, "net_connections", lambda kind: connections)

    assert _listener_records() == ([{"host": "::", "port": 2036, "pid": 20}], True)


def test_listener_inventory_is_bounded_at_consumer_limit(monkeypatch):
    def connection(pid):
        return SimpleNamespace(
            status=psutil.CONN_LISTEN,
            pid=pid,
            laddr=SimpleNamespace(ip="127.0.0.1", port=2000 + pid),
        )

    monkeypatch.setattr(process_probe, "MAX_LISTENER_RECORDS", 2)
    monkeypatch.setattr(
        psutil,
        "net_connections",
        lambda kind: [connection(1), connection(2)],
    )
    listeners, complete = _listener_records()
    assert len(listeners) == 2
    assert complete is True

    monkeypatch.setattr(
        psutil,
        "net_connections",
        lambda kind: [connection(1), connection(2), connection(3)],
    )
    with pytest.raises(RuntimeError, match="listener inventory exceeds"):
        _listener_records()

    with pytest.raises(RuntimeError, match="listener inventory exceeds"):
        collect_shared_preflight_snapshot(
            process_provider=list,
            listener_provider=lambda: [
                {"host": "127.0.0.1", "port": 2001 + index, "pid": index + 1} for index in range(3)
            ],
            window_provider=dict,
            version_provider=lambda _path: None,
        )


def test_listener_access_denied_marks_snapshot_incomplete(monkeypatch):
    monkeypatch.setattr(
        psutil,
        "net_connections",
        lambda **_kwargs: (_ for _ in ()).throw(psutil.AccessDenied()),
    )

    snapshot = collect_shared_preflight_snapshot(
        process_provider=list,
        listener_provider=_listener_records,
        window_provider=dict,
        version_provider=lambda _path: None,
    )

    assert snapshot["listeners"] == []
    assert snapshot["inventory_complete"] is False


def test_javaw_server_and_malformed_provider_records_are_classified_safely():
    records = [
        _record(
            20,
            0,
            "javaw.exe",
            ["javaw.exe", "C:/COMSOL/bin/comsolserver.exe"],
        ),
        {"name": "missing-identity.exe", "command_line": []},
    ]

    snapshot = collect_shared_preflight_snapshot(
        process_provider=lambda: records,
        listener_provider=list,
        window_provider=dict,
        version_provider=lambda _path: "6.4.0.293",
    )

    assert [item["kind"] for item in snapshot["processes"]] == ["comsol_server"]
    assert snapshot["inventory_complete"] is False


class _FakeWinFunction:
    def __init__(self, implementation=lambda *_args: True):
        self.implementation = implementation
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.implementation(*args)


@pytest.mark.skipif(os.name != "nt", reason="Windows user32 callback ABI and pointer signatures")
def test_user32_calls_declare_pointer_safe_signatures(monkeypatch):
    api = SimpleNamespace(
        EnumWindows=_FakeWinFunction(),
        IsWindowVisible=_FakeWinFunction(),
        GetWindowTextLengthW=_FakeWinFunction(),
        GetWindowTextW=_FakeWinFunction(),
        GetClassNameW=_FakeWinFunction(),
        GetWindowThreadProcessId=_FakeWinFunction(),
        IsHungAppWindow=_FakeWinFunction(),
    )
    monkeypatch.setattr(process_probe.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        process_probe.ctypes,
        "WinDLL",
        lambda _name, use_last_error: api,
    )

    assert process_probe._window_state_by_pid() == {}
    assert len(api.EnumWindows.argtypes) == 2
    assert api.EnumWindows.restype is wintypes.BOOL
    assert api.IsWindowVisible.argtypes == [wintypes.HWND]
    assert api.GetWindowTextLengthW.restype is ctypes.c_int
    assert api.GetWindowTextW.argtypes == [
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    ]
    assert api.GetClassNameW.argtypes == [
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    ]
    assert api.GetWindowThreadProcessId.argtypes == [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    assert api.IsHungAppWindow.restype is wintypes.BOOL


class _FakeVersionApi:
    def __init__(self, mode):
        self.mode = mode
        self.fixed = process_probe._VS_FIXEDFILEINFO()
        self.fixed.dwSignature = 0 if mode == "bad_signature" else 0xFEEF04BD
        self.fixed.dwFileVersionMS = (6 << 16) | 4
        self.fixed.dwFileVersionLS = 293
        self.GetFileVersionInfoSizeW = _FakeWinFunction(
            lambda _path, _handle: ctypes.sizeof(self.fixed)
        )
        self.GetFileVersionInfoW = _FakeWinFunction(lambda _path, _handle, _size, _buffer: True)
        self.VerQueryValueW = _FakeWinFunction(self._query)

    def _query(self, _buffer, _subblock, pointer_out, length_out):
        if self.mode != "null":
            ctypes.cast(
                pointer_out,
                ctypes.POINTER(ctypes.c_void_p),
            ).contents.value = ctypes.addressof(self.fixed)
        ctypes.cast(
            length_out,
            ctypes.POINTER(wintypes.UINT),
        ).contents.value = (
            ctypes.sizeof(self.fixed) - 1 if self.mode == "short" else ctypes.sizeof(self.fixed)
        )
        return True


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("valid", "6.4.0.293"),
        ("null", None),
        ("short", None),
        ("bad_signature", None),
    ],
)
@pytest.mark.skipif(os.name != "nt", reason="Windows version.dll ABI and pointer signatures")
def test_windows_file_version_validates_pointer_length_signature_and_api_types(
    monkeypatch,
    mode,
    expected,
):
    api = _FakeVersionApi(mode)
    monkeypatch.setattr(process_probe.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        process_probe.ctypes,
        "WinDLL",
        lambda _name, use_last_error: api,
    )

    assert process_probe._windows_file_version("C:/COMSOL/comsol.exe") == expected
    assert api.GetFileVersionInfoSizeW.argtypes == [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    assert api.GetFileVersionInfoW.restype is wintypes.BOOL
    assert api.VerQueryValueW.argtypes == [
        wintypes.LPCVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    ]


def test_mph_client_classification_requires_a_real_mph_module_token():
    unrelated = _record(30, 0, "python.exe", ["python.exe", "-m", "mphclient"])
    snapshot = collect_shared_preflight_snapshot(
        process_provider=lambda: [unrelated],
        listener_provider=list,
        window_provider=dict,
        version_provider=lambda path: None,
        clock=lambda: 1000.0,
    )

    assert snapshot["processes"] == []

    real_mph = _record(30, 0, "python.exe", ["python.exe", "-c", "import mph; mph.Client()"])
    snapshot = collect_shared_preflight_snapshot(
        process_provider=lambda: [real_mph],
        listener_provider=list,
        window_provider=dict,
        version_provider=lambda path: None,
        clock=lambda: 1000.0,
    )

    assert [item["kind"] for item in snapshot["processes"]] == ["mph_client"]


def test_raced_or_zombie_process_marks_inventory_incomplete(monkeypatch):
    class _RacingProcess:
        pid = 99

        def oneshot(self):
            return nullcontext()

        def cmdline(self):
            return ["comsol"]

        def exe(self):
            raise psutil.ZombieProcess(99)

        def ppid(self):
            raise psutil.NoSuchProcess(99)

    monkeypatch.setattr(
        process_probe.psutil, "process_iter", lambda *_args, **_kwargs: iter([_RacingProcess()])
    )

    records, complete = process_probe._process_records()

    # The process was silently omitted from the snapshot, so the inventory
    # must not claim completeness.
    assert records == []
    assert complete is False


def test_custom_process_provider_honors_the_bounded_maximum():
    records = [
        _record(index, index, "proc.exe", ["proc"])
        for index in range(process_probe.MAX_PROCESS_RECORDS + 1)
    ]

    with pytest.raises(RuntimeError, match="process inventory exceeds the bounded maximum"):
        collect_shared_preflight_snapshot(
            process_provider=lambda: records,
            listener_provider=lambda: [],
            window_provider=lambda: [],
            version_provider=lambda path: None,
            clock=lambda: 1000.0,
            exclude_pids=(),
        )


@pytest.mark.parametrize("name", ["comsol-mcp.exe", "comsol-mcp-settings.exe"])
def test_mcp_console_launchers_are_not_native_comsol_processes(name):
    snapshot = collect_shared_preflight_snapshot(
        process_provider=lambda: [_record(50, 1, name, [name])],
        listener_provider=list,
        window_provider=dict,
        version_provider=lambda _path: "0.8.1",
    )
    assert snapshot["inventory_complete"] is True
    assert snapshot["processes"] == []


@pytest.mark.parametrize(
    ("image", "expected_complete"),
    [
        ("C:/Windows/System32/LsaIso.exe", True),
        ("C:/Windows/System32/NgcIso.exe", True),
        ("D:/user/LsaIso.exe", False),
        ("C:/Windows/System32/python.exe", False),
    ],
)
def test_protected_security_image_exclusion_requires_exact_system_image(
    monkeypatch, image, expected_complete
):
    from pathlib import PureWindowsPath

    class ProtectedProcess:
        pid = 900

        def oneshot(self):
            return nullcontext()

        def cmdline(self):
            raise psutil.AccessDenied(self.pid)

        def exe(self):
            return image

        def name(self):
            return "LsaIso.exe"

        def ppid(self):
            return 500

        def create_time(self):
            return 100.0

    monkeypatch.setattr(process_probe, "_windows_kernel_process_pids", set)
    monkeypatch.setattr(
        process_probe, "_windows_system_directory", lambda: PureWindowsPath("C:/Windows/System32")
    )
    monkeypatch.setattr(psutil, "process_iter", lambda: iter([ProtectedProcess()]))
    records, complete = _process_records()
    assert complete is expected_complete
    assert len(records) == (0 if expected_complete else 1)


@pytest.mark.parametrize(
    ("parent", "name", "excluded"),
    [
        (0, "Registry", True),
        (4, "MemCompression", True),
        (500, "Registry", False),
        (0, "python.exe", False),
        (4, "python.exe", False),
    ],
)
def test_kernel_pseudo_process_exclusion_rechecks_identity_after_snapshot(
    monkeypatch, parent, name, excluded
):
    process = SimpleNamespace(
        pid=900,
        oneshot=nullcontext,
        ppid=lambda: parent,
        cmdline=lambda: [],
        exe=lambda: "python.exe",
        name=lambda: name,
        create_time=lambda: 100.0,
    )
    monkeypatch.setattr(process_probe, "_windows_kernel_process_pids", lambda: {900})
    monkeypatch.setattr(process_probe, "_windows_system_directory", lambda: None)
    monkeypatch.setattr(psutil, "process_iter", lambda: iter([process]))
    records, complete = _process_records()
    assert complete is True
    assert len(records) == (0 if excluded else 1)


@pytest.mark.parametrize("still_kernel", [True, False])
def test_unnamed_kernel_process_requires_fresh_native_identity(monkeypatch, still_kernel):
    snapshots = iter([{900}, {900} if still_kernel else set()])
    process = SimpleNamespace(
        pid=900,
        oneshot=nullcontext,
        ppid=lambda: 4,
        name=lambda: "",
        exe=lambda: "",
        cmdline=lambda: [],
        create_time=lambda: 100.0,
    )
    monkeypatch.setattr(process_probe, "_windows_kernel_process_pids", lambda: next(snapshots))
    monkeypatch.setattr(process_probe, "_windows_system_directory", lambda: None)
    monkeypatch.setattr(psutil, "process_iter", lambda: iter([process]))
    records, complete = _process_records()
    assert complete is still_kernel
    assert len(records) == (0 if still_kernel else 1)
