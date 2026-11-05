"""Read-only Windows process, window, listener, and file-version inventory."""

from __future__ import annotations

import ctypes
import hashlib
import platform
import time
from ctypes import wintypes
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterable

import psutil

from .contracts import normalize_shared_listener_bind_host

MAX_COMMAND_PARTS = 64
MAX_PROCESS_RECORDS = 4096
MAX_LISTENER_RECORDS = 128
_DESKTOP_EXECUTABLES = frozenset(
    {
        "comsol.exe",
        "comsolmphclient.exe",
        "comsolui.exe",
    }
)
_SERVER_EXECUTABLES = frozenset(
    {
        "comsolmphserver.exe",
        "comsolserver.exe",
    }
)
_AUXILIARY_WINDOW_CLASSES = frozenset(
    {
        "actiprowindowchromeshadow",
        "pseudoconsolewindow",
    }
)
_MAX_WINDOW_CLASS_CHARACTERS = 256
_WINDOWS_KERNEL_PROCESS_NAMES = frozenset(
    {
        "[System Process]",
        "System",
        "Secure System",
        "Registry",
        "MemCompression",
        "Memory Compression",
    }
)


def _command_signature(command_line: Iterable[Any]) -> str:
    canonical = "\0".join(str(part) for part in list(command_line)[:MAX_COMMAND_PARTS])
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()


def _windows_kernel_process_pids() -> set[int]:
    """Read kernel pseudo-process names through the native process snapshot."""
    if platform.system() != "Windows":
        return set()

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        function.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        pids = set()
        while True:
            if entry.szExeFile in _WINDOWS_KERNEL_PROCESS_NAMES and entry.th32ParentProcessID in {
                0,
                4,
            }:
                pids.add(int(entry.th32ProcessID))
            if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                if ctypes.get_last_error() != 18:
                    raise ctypes.WinError(ctypes.get_last_error())
                return pids
    finally:
        kernel.CloseHandle(snapshot)


def _windows_system_directory() -> PureWindowsPath | None:
    if platform.system() != "Windows":
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetWindowsDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel.GetWindowsDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetWindowsDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return PureWindowsPath(buffer.value) / "System32"


def _process_records() -> tuple[list[dict[str, Any]], bool]:
    records: list[dict[str, Any]] = []
    complete = True
    kernel_pids = _windows_kernel_process_pids()
    system_directory = _windows_system_directory()
    for process in psutil.process_iter():
        if process.pid in kernel_pids and process.pid in {0, 4}:
            continue
        try:
            with process.oneshot():
                if process.pid in kernel_pids and process.ppid() in {0, 4}:
                    name = process.name()
                    if name in _WINDOWS_KERNEL_PROCESS_NAMES:
                        continue
                    if not name and not process.exe():
                        # Secure System can have no psutil name or image.
                        # Recheck its native kernel identity before exclusion.
                        if process.pid in _windows_kernel_process_pids():
                            continue
                        complete = False
                command_denied = False
                try:
                    command_line = list(process.cmdline())[:MAX_COMMAND_PARTS]
                except psutil.AccessDenied:
                    command_line = []
                    command_denied = True
                except psutil.ZombieProcess:
                    command_line = []
                try:
                    executable = process.exe()
                except psutil.AccessDenied:
                    executable = None
                    complete = False
                except psutil.ZombieProcess:
                    executable = None
                if command_denied:
                    # Windows isolated-security images cannot host COMSOL.
                    # Require the actual system-directory image, not its name.
                    if (
                        system_directory is not None
                        and executable
                        and PureWindowsPath(executable)
                        in {
                            system_directory / "LsaIso.exe",
                            system_directory / "NgcIso.exe",
                        }
                    ):
                        continue
                    complete = False
                records.append(
                    {
                        "pid": process.pid,
                        "parent_pid": process.ppid(),
                        "name": process.name(),
                        "create_time": process.create_time(),
                        "command_line": command_line,
                        "executable": executable,
                    }
                )
        except psutil.AccessDenied:
            complete = False
            continue
        except psutil.NoSuchProcess, psutil.ZombieProcess:
            # The process raced away or became a zombie mid-inspection: the
            # snapshot silently omits it, so completeness must stay truthful.
            complete = False
            continue
        if len(records) > MAX_PROCESS_RECORDS:
            raise RuntimeError("process inventory exceeds the bounded maximum")
    return records, complete


def _listener_records() -> tuple[list[dict[str, Any]], bool]:
    listeners: list[dict[str, Any]] = []
    complete = True
    try:
        connections = psutil.net_connections(kind="tcp")
    except psutil.AccessDenied:
        return [], False
    for connection in connections:
        if connection.status != psutil.CONN_LISTEN:
            continue
        if connection.pid is None:
            complete = False
            continue
        host = getattr(connection.laddr, "ip", None)
        port = getattr(connection.laddr, "port", None)
        if host is None or port is None:
            complete = False
            continue
        try:
            normalized_host, _bind_scope = normalize_shared_listener_bind_host(host)
        except ValueError:
            continue
        listeners.append(
            {
                "host": normalized_host,
                "port": int(port),
                "pid": int(connection.pid),
            }
        )
        if len(listeners) > MAX_LISTENER_RECORDS:
            raise RuntimeError("listener inventory exceeds the bounded maximum")
    return listeners, complete


def _is_primary_desktop_window(*, title: str, class_name: str) -> bool:
    """Reject visible helper windows that do not represent a Desktop model view."""
    normalized_title = title.strip().casefold()
    return bool(normalized_title) and not (
        {normalized_title, class_name.strip().casefold()} & _AUXILIARY_WINDOW_CLASSES
    )


def _window_state_by_pid() -> dict[int, dict[str, Any]]:
    if platform.system() != "Windows":
        return {}
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    states: dict[int, dict[str, Any]] = {}
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    ]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    ]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsHungAppWindow.argtypes = [wintypes.HWND]
    user32.IsHungAppWindow.restype = wintypes.BOOL

    @callback_type
    def visit(window, _parameter):
        if not user32.IsWindowVisible(window):
            return True
        title_length = user32.GetWindowTextLengthW(window)
        title_buffer = ctypes.create_unicode_buffer(max(1, title_length + 1))
        user32.GetWindowTextW(window, title_buffer, len(title_buffer))
        class_buffer = ctypes.create_unicode_buffer(_MAX_WINDOW_CLASS_CHARACTERS)
        user32.GetClassNameW(window, class_buffer, len(class_buffer))
        if not _is_primary_desktop_window(
            title=title_buffer.value,
            class_name=class_buffer.value,
        ):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if not pid.value:
            return True
        state = states.setdefault(int(pid.value), {"window_count": 0, "responding": True})
        state["window_count"] += 1
        if user32.IsHungAppWindow(window):
            state["responding"] = False
        return True

    if not user32.EnumWindows(visit, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return states


class _VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [
        ("dwSignature", wintypes.DWORD),
        ("dwStrucVersion", wintypes.DWORD),
        ("dwFileVersionMS", wintypes.DWORD),
        ("dwFileVersionLS", wintypes.DWORD),
        ("dwProductVersionMS", wintypes.DWORD),
        ("dwProductVersionLS", wintypes.DWORD),
        ("dwFileFlagsMask", wintypes.DWORD),
        ("dwFileFlags", wintypes.DWORD),
        ("dwFileOS", wintypes.DWORD),
        ("dwFileType", wintypes.DWORD),
        ("dwFileSubtype", wintypes.DWORD),
        ("dwFileDateMS", wintypes.DWORD),
        ("dwFileDateLS", wintypes.DWORD),
    ]


def _windows_file_version(executable: str | None) -> str | None:
    if platform.system() != "Windows" or not executable:
        return None
    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    version.GetFileVersionInfoW.restype = wintypes.BOOL
    version.VerQueryValueW.argtypes = [
        wintypes.LPCVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    ]
    version.VerQueryValueW.restype = wintypes.BOOL
    size = version.GetFileVersionInfoSizeW(str(executable), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(executable), 0, size, buffer):
        return None
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        return None
    if not pointer.value or length.value < ctypes.sizeof(_VS_FIXEDFILEINFO):
        return None
    fixed = ctypes.cast(pointer, ctypes.POINTER(_VS_FIXEDFILEINFO)).contents
    if fixed.dwSignature != 0xFEEF04BD:
        return None
    parts = (
        fixed.dwFileVersionMS >> 16,
        fixed.dwFileVersionMS & 0xFFFF,
        fixed.dwFileVersionLS >> 16,
        fixed.dwFileVersionLS & 0xFFFF,
    )
    return ".".join(str(part) for part in parts)


def _is_descendant(pid: int, parent_map: dict[int, int], ancestors: set[int]) -> bool:
    seen: set[int] = set()
    current = pid
    while current and current not in seen:
        if current in ancestors:
            return True
        seen.add(current)
        current = parent_map.get(current, 0)
    return False


def _kind(record: dict[str, Any], window_count: int) -> str | None:
    name = str(record.get("name") or "").casefold()
    executable_name = Path(str(record.get("executable") or "")).name.casefold()
    process_names = {name, executable_name} - {""}
    if process_names and process_names <= {"comsol-mcp.exe", "comsol-mcp-settings.exe"}:
        return None
    command_parts = [str(part).casefold() for part in record.get("command_line") or []]
    command = " ".join(command_parts)
    command_basenames = {Path(part).name.casefold() for part in command_parts}
    explicit_server_command = bool(command_basenames & _SERVER_EXECUTABLES)
    if process_names & _SERVER_EXECUTABLES or (
        process_names & {"java", "java.exe", "javaw", "javaw.exe"} and explicit_server_command
    ):
        return "comsol_server"
    if "mph.client" in command or (
        "mph" in command_parts and {"-m", "import", "from"} & set(command_parts)
    ):
        return "mph_client"
    if window_count > 0 and process_names & _DESKTOP_EXECUTABLES:
        return "comsol_desktop"
    if any(value.startswith("comsol") for value in process_names):
        return "other_comsol"
    return None


def collect_shared_preflight_snapshot(
    *,
    process_provider: Callable[
        [], list[dict[str, Any]] | tuple[list[dict[str, Any]], bool]
    ] = _process_records,
    listener_provider: Callable[
        [], list[dict[str, Any]] | tuple[list[dict[str, Any]], bool]
    ] = _listener_records,
    window_provider: Callable[[], dict[int, dict[str, Any]]] = _window_state_by_pid,
    version_provider: Callable[[str | None], str | None] = _windows_file_version,
    clock: Callable[[], float] = time.time,
    exclude_pids: Iterable[int] = (),
) -> dict[str, Any]:
    """Collect one bounded redacted snapshot without importing or starting MPh."""
    provided_records = process_provider()
    if isinstance(provided_records, tuple):
        records, inventory_complete = provided_records
    else:
        records = provided_records
        inventory_complete = True
    provided_listeners = listener_provider()
    if isinstance(provided_listeners, tuple):
        listeners, listener_inventory_complete = provided_listeners
        inventory_complete = inventory_complete and listener_inventory_complete
    else:
        listeners = provided_listeners
    if len(records) > MAX_PROCESS_RECORDS:
        raise RuntimeError("process inventory exceeds the bounded maximum")
    if len(listeners) > MAX_LISTENER_RECORDS:
        raise RuntimeError("listener inventory exceeds the bounded maximum")
    windows = window_provider()
    excluded = {int(pid) for pid in exclude_pids}
    valid_records = []
    for record in records:
        pid = record.get("pid")
        create_time = record.get("create_time")
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or isinstance(create_time, bool)
            or not isinstance(create_time, (int, float))
        ):
            inventory_complete = False
            continue
        valid_records.append(record)
    records = valid_records
    parent_map = {
        int(record["pid"]): int(record.get("parent_pid") or 0)
        for record in records
        if record.get("pid") is not None
    }
    preliminary: list[tuple[dict[str, Any], str]] = []
    allowed_roots: set[int] = set()
    for record in records:
        pid = int(record["pid"])
        state = windows.get(pid, {"window_count": 0, "responding": True})
        kind = _kind(record, int(state["window_count"]))
        if kind is None:
            continue
        if kind in {"comsol_desktop", "comsol_server"}:
            allowed_roots.add(pid)
        if pid in excluded:
            continue
        preliminary.append((record, kind))

    candidates: list[dict[str, Any]] = []
    for record, kind in preliminary:
        pid = int(record["pid"])
        if kind == "other_comsol" and _is_descendant(pid, parent_map, allowed_roots):
            continue
        state = windows.get(pid, {"window_count": 0, "responding": True})
        version = None if kind == "mph_client" else version_provider(record.get("executable"))
        candidates.append(
            {
                "pid": pid,
                "parent_pid": int(record.get("parent_pid") or 0),
                "kind": kind,
                "create_time": float(record["create_time"]),
                "command_signature": _command_signature(record.get("command_line") or []),
                "file_version": version or "unreadable",
                "window_count": int(state["window_count"]),
                "responding": bool(state["responding"]),
            }
        )
    return {
        "inventory_complete": inventory_complete,
        "observed_at_epoch": float(clock()),
        "processes": sorted(candidates, key=lambda item: item["pid"]),
        "listeners": sorted(
            listeners, key=lambda item: (str(item["host"]), int(item["port"]), int(item["pid"]))
        ),
    }


__all__ = ["collect_shared_preflight_snapshot"]
