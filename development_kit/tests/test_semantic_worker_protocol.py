"""semantic worker protocol containment and protocol gates for the isolated fake worker."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from types import SimpleNamespace

import pytest
from src.jobs.manager import JobManager
from src.knowledge.lexical_manual import build_index_from_records, search_index
from src.knowledge.semantic_contracts import PUBLIC_LIMITS, WORKER_PROTOCOL_SCHEMA_VERSION
from src.knowledge.semantic_process import SemanticWorkerManager, _command_signature
from src.knowledge.semantic_runtime import _lightweight_deployment_identity
from src.knowledge.semantic_worker import _RequestHandler, _WorkerServer, _WorkerState
from src.tools.capabilities import get_capabilities
from src.tools.ownership import SolverOwnership

from development_kit.tests.platform_fixtures import platform_test_root
from development_kit.tests.semantic_test_support import isolated_semantic_environment


@pytest.mark.skipif(os.name == "nt", reason="POSIX process group cleanup")
@pytest.mark.parametrize("startup_failure", [False, True])
@pytest.mark.parametrize("detached", [False, True])
def test_posix_reset_reclaims_worker_descendants(tmp_path, startup_failure, detached):
    import psutil

    pid_path = tmp_path / "descendant.json"
    code = (
        "import json,os,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
        f"start_new_session={detached}); "
        f"open({str(pid_path)!r},'w').write(json.dumps([child.pid])); "
        "print(json.dumps({'schema_version':'1','event':'ready','pid':os.getpid(),"
        "'host':'127.0.0.1','port':9}),flush=True); time.sleep(30)"
    )
    manager = SemanticWorkerManager(startup_deadline=5)
    manager._command = lambda: [sys.executable, "-c", code]
    child = None
    try:
        started = manager.start()
        assert started["success"], started
        child = psutil.Process(json.loads(pid_path.read_text())[0])
        assert child.is_running() and child.status() != psutil.STATUS_ZOMBIE
        assert (os.getpgid(child.pid) == manager._process.pid) is not detached
        assert os.getpgid(child.pid) != os.getpgrp()
        if startup_failure:
            result = manager._terminate_unverified_spawn("injected_startup_failure")
        else:
            result = manager.reset()["reset"]
        assert result["absent"], result
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
    finally:
        manager.reset()
        if child is not None and child.is_running():
            child.kill()


def test_short_startup_line_returns_while_worker_pipe_remains_open():
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'ready\\n'); "
            "sys.stdout.flush(); sys.stdin.read()",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=isolated_semantic_environment(),
    )
    result = queue.Queue(maxsize=1)
    reader = threading.Thread(
        target=lambda: result.put(SemanticWorkerManager._read_startup_line(process.stdout)),
        daemon=True,
    )
    reader.start()
    try:
        assert result.get(timeout=5.0) == b"ready\n"
        assert process.poll() is None
    finally:
        process.kill()
        process.communicate(timeout=5.0)
        reader.join(timeout=5.0)
    assert not reader.is_alive()


def _raw_request(
    port: int,
    payload: dict,
    *,
    maximum: int = 200_000,
    timeout: float = 3.0,
) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        connection.sendall(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
        data = bytearray()
        while not data.endswith(b"\n"):
            block = connection.recv(4096)
            if not block:
                break
            data.extend(block)
            if len(data) > maximum:
                break
    return json.loads(bytes(data).decode("utf-8"))


def _request(manager: SemanticWorkerManager, request_id: str, **fields: object) -> dict:
    return {
        "schema_version": WORKER_PROTOCOL_SCHEMA_VERSION,
        "request_id": request_id,
        "token": manager._token,
        **fields,
    }


def test_happy_path_reuses_one_worker_and_reset_verifies_absence():
    with SemanticWorkerManager(startup_deadline=10.0, query_deadline=5.0) as manager:
        first = manager.query("CopyFace source destination", limit=2)
        pid = manager.status(probe=False)["identity"]["pid"]
        second = manager.query("alpha1_inc", limit=1)
        health = manager.health()

        assert first["success"] is second["success"] is health["success"] is True
        assert len(first["results"]) == 2
        assert health["status"]["query_count"] == 2
        assert health["status"]["load_count"] == 0
        assert manager.status(probe=False)["identity"]["pid"] == pid
        process = manager._process
        assert process is not None and process.stdout is not None and process.stderr is not None
        reset = manager.reset()
        assert reset["success"] is True
        assert reset["reset"]["absent"] is True
        assert process.stdout.closed is True
        assert process.stderr.closed is True
        assert manager.status()["state"] == "stopped"


@pytest.mark.parametrize(
    "fault",
    [
        "query_hang",
        "invalid_json",
        "oversized_json",
        "wrong_request_id",
        "crash_before_response",
    ],
)
def test_query_protocol_faults_are_contained_without_retry(fault: str):
    with SemanticWorkerManager(startup_deadline=10.0, query_deadline=0.25, fault=fault) as manager:
        assert manager.start()["success"] is True
        result = manager.query("bounded fault probe")

        assert result["success"] is False
        assert result["error"]["code"] == "worker_protocol_failure"
        assert result["retried"] is False
        assert result["cleanup"]["absent"] is True
        assert manager.status()["state"] == "stopped"


def test_startup_hang_and_port_collision_are_contained():
    with SemanticWorkerManager(startup_deadline=0.2, fault="startup_hang") as hanging:
        result = hanging.start()
        assert result["success"] is False
        assert result["cleanup"]["absent"] is True


def test_non_object_startup_handshake_is_contained_and_reaped(monkeypatch):
    manager = SemanticWorkerManager(startup_deadline=10.0)
    command = [
        sys.executable,
        "-c",
        "import time; print('[]', flush=True); time.sleep(30)",
    ]
    monkeypatch.setattr(manager, "_command", lambda: command)
    result = None
    try:
        result = manager.start()
    finally:
        manager.reset()

    assert result is not None
    assert result["success"] is False
    assert result["error"]["code"] == "startup_failed"
    assert result["cleanup"]["absent"] is True


@pytest.mark.parametrize("size", [65_535, 65_536, 65_537])
def test_startup_handshake_line_byte_limit_includes_newline(size):
    payload = b'{"event":"ready"}'
    line = payload + b" " * (size - len(payload) - 1) + b"\n"
    assert json.loads(line) == {"event": "ready"}
    if size > 65_536:
        with pytest.raises(ValueError, match="exceeds the byte limit"):
            SemanticWorkerManager._read_startup_line(BytesIO(line))
    else:
        assert SemanticWorkerManager._read_startup_line(BytesIO(line)) == line


def test_startup_handshake_limit_excludes_bytes_after_first_newline():
    line = b'{"event":"ready"}\n'
    assert (
        SemanticWorkerManager._read_startup_line(BytesIO(line + b"x" * 4096), maximum=len(line))
        == line
    )


def test_oversized_startup_handshake_is_rejected_before_json_decode(monkeypatch):
    manager = SemanticWorkerManager(startup_deadline=10.0)
    command = [
        sys.executable,
        "-c",
        "import sys,time; sys.stdout.write('x'*70000+'\\n'); sys.stdout.flush(); time.sleep(30)",
    ]
    monkeypatch.setattr(manager, "_command", lambda: command)
    try:
        result = manager.start()
    finally:
        manager.reset()
    assert result["success"] is False
    assert result["error"]["code"] == "startup_failed"
    assert result["cleanup"]["absent"] is True


@pytest.mark.parametrize("port", [True, 0, 65_536, "1234"])
def test_invalid_startup_port_is_contained_and_reaped(monkeypatch, port):
    manager = SemanticWorkerManager(startup_deadline=10.0)
    handshake = json.dumps(
        {
            "schema_version": WORKER_PROTOCOL_SCHEMA_VERSION,
            "event": "ready",
            "pid": "__PID__",
            "host": "127.0.0.1",
            "port": port,
        }
    )
    command = [
        sys.executable,
        "-c",
        (
            "import json,os,time; "
            f"payload=json.loads({handshake!r}); payload['pid']=os.getpid(); "
            "print(json.dumps(payload), flush=True); "
            "time.sleep(30)"
        ),
    ]
    monkeypatch.setattr(manager, "_command", lambda: command)

    result = manager.start()

    assert result["success"] is False
    assert result["error"]["code"] == "startup_failed"
    assert result["cleanup"]["absent"] is True


def test_nested_query_filters_are_rejected_before_worker_start(monkeypatch):
    manager = SemanticWorkerManager()
    starts = []
    monkeypatch.setattr(
        manager,
        "start",
        lambda: starts.append(True) or {"success": True},
    )

    result = manager.query("bounded", filters={"module": {"nested": object()}})

    assert result["success"] is False
    assert result["error"]["code"] == "invalid_filters"
    assert starts == []


@pytest.mark.parametrize("invalid_document", [[], "manifest", 1])
def test_lightweight_identity_rejects_non_object_decoded_manifests(tmp_path, invalid_document):
    root = tmp_path / "deployment"
    index = root / "index"
    model = root / "model"
    index.mkdir(parents=True)
    model.mkdir()
    (root / "current.json").write_text(
        json.dumps(
            {
                "index_path": str(index),
                "manifest_sha256": "a" * 64,
                "build_id": "build",
                "model_fingerprint": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    (index / "manifest.json").write_text(json.dumps(invalid_document), encoding="utf-8")
    (model / "model_manifest.json").write_text(
        json.dumps({"model_sha256": "b" * 64}), encoding="utf-8"
    )

    result = _lightweight_deployment_identity(
        {
            "configured": True,
            "root": str(root),
            "model_path": str(model),
        }
    )

    assert result is not None
    assert result["readable"] is False


def test_lightweight_identity_rejects_non_object_model_manifest(tmp_path):
    root = tmp_path / "deployment"
    index = root / "index"
    model = root / "model"
    index.mkdir(parents=True)
    model.mkdir()
    (root / "current.json").write_text(
        json.dumps(
            {
                "index_path": str(index),
                "manifest_sha256": "a" * 64,
                "build_id": "build",
                "model_fingerprint": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    (index / "manifest.json").write_text(
        json.dumps(
            {
                "build_id": "build",
                "model_fingerprint": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    (model / "model_manifest.json").write_text("[]", encoding="utf-8")

    result = _lightweight_deployment_identity(
        {
            "configured": True,
            "root": str(root),
            "model_path": str(model),
        }
    )

    assert result is not None
    assert result["readable"] is False

    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        with SemanticWorkerManager(
            startup_deadline=1.0, forced_port=occupied.getsockname()[1]
        ) as collision:
            result = collision.start()
    assert result["success"] is False
    assert result["cleanup"]["absent"] is True


def test_lightweight_identity_hashes_loaded_manifest_and_contains_invalid_utf8(ascii_tmp_path):
    root = ascii_tmp_path / "semantic-worker"
    index = root / "indexes" / "corpus" / "model" / "build"
    model = root / "model"
    index.mkdir(parents=True)
    model.mkdir()
    manifest = {
        "build_id": "build",
        "corpus_fingerprint": "c" * 64,
        "model_id": "test/model",
        "model_revision": "r1",
        "model_fingerprint": "b" * 64,
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
    (index / "manifest.json").write_bytes(manifest_bytes)
    (model / "model_manifest.json").write_text(
        json.dumps({"model_sha256": "b" * 64}), encoding="utf-8"
    )
    (root / "current.json").write_text(
        json.dumps(
            {
                "index_path": str(index),
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "build_id": "build",
                "model_fingerprint": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    configuration = {
        "configured": True,
        "root": str(root),
        "model_path": str(model),
    }

    assert _lightweight_deployment_identity(configuration)["lightweight_identity_match"] is True
    (index / "manifest.json").write_bytes(manifest_bytes + b" ")
    assert _lightweight_deployment_identity(configuration)["lightweight_identity_match"] is False
    (index / "manifest.json").write_bytes(b"\xff")
    assert _lightweight_deployment_identity(configuration)["readable"] is False


@pytest.mark.parametrize(
    ("document_name", "field"),
    [
        ("pointer", "manifest_sha256"),
        ("pointer", "build_id"),
        ("pointer", "model_fingerprint"),
        ("manifest", "build_id"),
        ("manifest", "model_fingerprint"),
        ("model", "model_sha256"),
    ],
)
def test_lightweight_identity_rejects_missing_required_fields(request, document_name, field):
    root = platform_test_root("a65b15id") / uuid.uuid4().hex
    request.addfinalizer(lambda: shutil.rmtree(root, ignore_errors=True))
    index = root / "indexes" / "corpus" / "model" / "build"
    model_root = root / "model"
    index.mkdir(parents=True)
    model_root.mkdir()
    documents = {
        "manifest": {
            "build_id": "build",
            "model_fingerprint": "b" * 64,
        },
        "model": {"model_sha256": "b" * 64},
    }
    manifest_bytes = json.dumps(documents["manifest"]).encode("utf-8")
    documents["pointer"] = {
        "index_path": str(index),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "build_id": "build",
        "model_fingerprint": "b" * 64,
    }
    documents[document_name].pop(field)
    (index / "manifest.json").write_text(json.dumps(documents["manifest"]), encoding="utf-8")
    (model_root / "model_manifest.json").write_text(
        json.dumps(documents["model"]), encoding="utf-8"
    )
    (root / "current.json").write_text(json.dumps(documents["pointer"]), encoding="utf-8")

    result = _lightweight_deployment_identity(
        {"configured": True, "root": str(root), "model_path": str(model_root)}
    )

    assert result["readable"] is False


def test_authentication_schema_and_message_bounds_do_not_kill_worker():
    with SemanticWorkerManager(startup_deadline=10.0) as manager:
        assert manager.start()["success"] is True
        port = int(manager._port)
        wrong = _raw_request(
            port,
            {
                "schema_version": WORKER_PROTOCOL_SCHEMA_VERSION,
                "request_id": "wrong-token",
                "token": "0" * 64,
                "operation": "health",
            },
        )
        schema = _raw_request(
            port,
            {
                "schema_version": "999",
                "request_id": "wrong-schema",
                "token": manager._token,
                "operation": "health",
            },
        )
        assert wrong["error"]["code"] == "unauthorized"
        assert schema["error"]["code"] == "invalid_schema"
        assert manager.health()["success"] is True

        with socket.create_connection(("127.0.0.1", port), timeout=2.0) as connection:
            connection.sendall(b"{" + b"x" * 17_000 + b"\n")
            oversized = json.loads(connection.makefile("rb").readline().decode("utf-8"))
        assert oversized["error"]["code"] == "invalid_request"
        assert manager.health()["success"] is True


def test_queue_overflow_is_bounded_and_worker_recovers():
    participants = (PUBLIC_LIMITS["maximum_queue_depth"] + 1) * 4
    with SemanticWorkerManager(startup_deadline=10.0, query_delay=0.75) as manager:
        assert manager.start()["success"] is True
        port = int(manager._port)
        barrier = threading.Barrier(participants)

        def call(index: int) -> dict:
            barrier.wait(timeout=10.0)
            return _raw_request(
                port,
                _request(
                    manager,
                    f"burst-{index}",
                    operation="query",
                    query=f"query {index}",
                    limit=1,
                ),
                timeout=15.0,
            )

        with ThreadPoolExecutor(max_workers=participants) as pool:
            responses = list(pool.map(call, range(participants)))
        busy = [
            item for item in responses if not item["success"] and item["error"]["code"] == "busy"
        ]
        assert busy
        assert manager.health()["success"] is True


def test_queue_capacity_returns_after_handler_setup_failure():
    from comsol_mcp.knowledge.semantic_worker import _RequestHandler

    class FailedSetupHandler(_RequestHandler):
        def setup(self):
            raise OSError("request stream setup failed")

    state = SimpleNamespace(capacity=threading.BoundedSemaphore(1))
    assert state.capacity.acquire(blocking=False)
    server = object.__new__(_WorkerServer)
    server.state = state
    server.RequestHandlerClass = FailedSetupHandler
    errors = []
    closed = []
    server.handle_error = lambda request, address: errors.append(address)
    server.shutdown_request = lambda request: closed.append(request)
    request = object()
    server.process_request_thread(request, ("127.0.0.1", 1))
    assert errors == [("127.0.0.1", 1)]
    assert closed == [request]
    assert state.capacity.acquire(blocking=False)


def test_queue_capacity_is_reserved_before_handler_thread_creation():
    state = SimpleNamespace(capacity=threading.BoundedSemaphore(1))
    assert state.capacity.acquire(blocking=False)
    server = object.__new__(_WorkerServer)
    server.state = state
    server.shutdown_request = lambda _request: None

    class Request:
        def __init__(self):
            self.payload = b""
            self.timeout = None
            self.request = b'{"request_id":"rejected"}\n'

        def settimeout(self, timeout):
            self.timeout = timeout

        def recv(self, maximum):
            block = self.request[:maximum]
            self.request = self.request[maximum:]
            return block

        def sendall(self, payload):
            self.payload += payload

    request = Request()
    server.process_request(request, ("127.0.0.1", 1))

    assert json.loads(request.payload)["error"]["code"] == "busy"
    assert request.timeout == 0.1
    assert request.request == b""


def test_unserializable_worker_response_uses_stable_json_fallback():
    handler = object.__new__(_RequestHandler)
    handler.wfile = BytesIO()

    handler._write({"request_id": "response-1", "success": True, "value": object()})

    response = json.loads(handler.wfile.getvalue())
    assert response["success"] is False
    assert response["error"]["code"] == "invalid_response"


def test_response_envelope_keys_cannot_be_overridden_by_payload_fields():
    from src.knowledge.semantic_worker import _response

    payload = {"schema_version": "999", "status": {"backend": "test"}}
    response = _response("request-1", success=True, **payload)

    assert response["schema_version"] == WORKER_PROTOCOL_SCHEMA_VERSION
    assert response["request_id"] == "request-1"
    assert response["success"] is True
    assert response["status"] == {"backend": "test"}


def test_query_payload_reserved_keys_are_stripped_before_envelope_build():
    class Backend:
        def query(self, *_args, **_kwargs):
            return {
                "results": [],
                "count": 1,
                "success": False,
                "status": {"stale": True},
                "schema_version": "999",
                "request_id": "evil",
            }

        def status(self):
            return {"backend": "test"}

    state = _WorkerState("0" * 64, None, 0.0, backend=Backend())
    handler = object.__new__(_RequestHandler)
    handler.server = SimpleNamespace(state=state)
    handler.wfile = BytesIO()

    handler._dispatch(
        "envelope-safe",
        {
            "operation": "query",
            "query": "bounded query",
            "limit": 1,
            "filters": None,
            "retrieval_mode": "hybrid",
        },
    )

    response = json.loads(handler.wfile.getvalue())
    assert response["success"] is True
    assert response["request_id"] == "envelope-safe"
    assert response["schema_version"] == WORKER_PROTOCOL_SCHEMA_VERSION
    assert response["status"]["backend"] == "test"
    assert response["count"] == 1
    serialized = json.dumps(response)
    assert "stale" not in serialized
    assert '"evil"' not in serialized


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects only")
def test_kill_on_close_job_error_path_closes_handle_without_truncation(monkeypatch):
    import src.knowledge.semantic_process as process_module

    class FakeKernel32:
        def __init__(self):
            self.closed = []
            self.CreateJobObjectW = lambda *_args, **_kwargs: 2**40  # above c_int range
            self.SetInformationJobObject = lambda *_args, **_kwargs: False
            self.AssignProcessToJobObject = lambda *_args, **_kwargs: False

        def CloseHandle(self, handle):
            self.closed.append(handle)
            return True

    fake = FakeKernel32()
    monkeypatch.setattr(process_module.ctypes, "WinDLL", lambda *_args, **_kwargs: fake)

    result = process_module._KillOnCloseJob.assign(1234)

    assert result is None
    assert len(fake.closed) == 1
    assert fake.closed[0].value == 2**40


def test_backend_exception_returns_structured_failure_without_killing_handler():
    class Backend:
        def query(self, *_args, **_kwargs):
            raise RuntimeError("private backend detail")

        def status(self):
            return {"backend": "test"}

    state = _WorkerState("0" * 64, None, 0.0, backend=Backend())
    handler = object.__new__(_RequestHandler)
    handler.server = SimpleNamespace(state=state)
    handler.wfile = BytesIO()

    handler._dispatch(
        "backend-error",
        {
            "operation": "query",
            "query": "bounded failure",
            "limit": 1,
            "filters": None,
            "retrieval_mode": "hybrid",
        },
    )

    response = json.loads(handler.wfile.getvalue())
    assert response["error"] == {
        "code": "backend_failure",
        "message": "semantic backend query failed",
    }
    assert "private backend detail" not in json.dumps(response)
    assert state.last_error == "RuntimeError: backend query failed"


@pytest.mark.parametrize("operation", ["health", "status"])
def test_backend_status_exception_returns_structured_failure(operation):
    class Backend:
        def status(self):
            raise RuntimeError("private status detail")

    state = _WorkerState("0" * 64, None, 0.0, backend=Backend())
    handler = object.__new__(_RequestHandler)
    handler.server = SimpleNamespace(state=state)
    handler.wfile = BytesIO()

    handler._dispatch("backend-status-error", {"operation": operation})

    response = json.loads(handler.wfile.getvalue())
    assert response["error"] == {
        "code": "backend_failure",
        "message": "semantic backend status failed",
    }
    assert "private status detail" not in json.dumps(response)
    assert state.last_error == "RuntimeError: backend status failed"


def test_health_remains_observable_while_query_holds_backend_lock():
    with SemanticWorkerManager(startup_deadline=10.0, query_delay=0.75) as manager:
        assert manager.start()["success"] is True
        port = int(manager._port)
        result: dict = {}
        query_thread = threading.Thread(
            target=lambda: result.update(
                _raw_request(
                    port,
                    _request(
                        manager,
                        "slow-query",
                        operation="query",
                        query="slow",
                        limit=1,
                    ),
                )
            )
        )
        query_thread.start()
        time.sleep(0.1)

        started = time.monotonic()
        health = _raw_request(
            port,
            _request(manager, "health-during-query", operation="health"),
        )
        elapsed = time.monotonic() - started
        query_thread.join(timeout=3.0)

        assert health["success"] is True
        assert elapsed < 0.5
        assert not query_thread.is_alive()
        assert result["success"] is True


def test_worker_start_os_error_is_structured_and_leaves_no_state(monkeypatch):
    manager = SemanticWorkerManager(python_executable="missing-python")
    monkeypatch.setattr(
        "src.knowledge.semantic_process.subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            FileNotFoundError("private executable path")
        ),
    )

    result = manager.start()

    assert result["success"] is False
    assert result["error"] == {
        "code": "startup_failed",
        "message": "FileNotFoundError: semantic worker process could not be started",
    }
    assert result["cleanup"]["absent"] is True
    assert manager.status(probe=False)["state"] == "stopped"


def test_request_uses_one_monotonic_deadline_across_trickled_receives(monkeypatch):
    class Connection:
        def __init__(self):
            self.timeout = 1.0
            self.blocks = [b'{"schema_version":"1",', b'"request_id":"late",', b'"success":true}\n']

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, timeout):
            self.timeout = timeout

        def sendall(self, _payload):
            return None

        def recv(self, _maximum):
            delay = 0.03
            if self.timeout < delay:
                raise TimeoutError("deadline exhausted")
            time.sleep(delay)
            return self.blocks.pop(0)

    manager = SemanticWorkerManager(query_deadline=0.05)
    manager._port = 1
    manager._token = "0" * 64
    monkeypatch.setattr(manager, "start", lambda: {"success": True})
    monkeypatch.setattr(
        "src.knowledge.semantic_process.socket.create_connection",
        lambda *_args, **_kwargs: Connection(),
    )
    monkeypatch.setattr(
        manager,
        "_terminate_owned",
        lambda reason: {"reason": reason, "absent": True},
    )

    started = time.monotonic()
    result = manager.query("trickle")
    elapsed = time.monotonic() - started

    assert result["success"] is False
    assert result["error"]["code"] == "worker_protocol_failure"
    assert result["cleanup"]["absent"] is True
    assert elapsed < 0.08


def test_stale_identity_cleanup_reclaims_the_exact_spawned_process():
    with SemanticWorkerManager(startup_deadline=10.0) as manager:
        assert manager.start()["success"] is True
        process = manager._process
        manager._identity["process_create_time"] -= 10.0

        cleanup = manager.reset()

        assert cleanup["success"] is True
        assert cleanup["reset"]["identity_state"] == "uncertain"
        assert cleanup["reset"]["absent"] is True
        assert process is not None and process.poll() is not None


def test_query_limit_applies_to_the_transmitted_trimmed_value(monkeypatch):
    manager = SemanticWorkerManager()
    observed = []
    monkeypatch.setattr(
        manager,
        "_request",
        lambda operation, fields, deadline: (
            observed.append((operation, fields, deadline)) or {"success": True}
        ),
    )
    query = " " + "x" * PUBLIC_LIMITS["maximum_query_characters"] + " "

    result = manager.query(query)

    assert result["success"] is True
    assert observed[0][1]["query"] == query.strip()


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"operation": []}, "unknown_operation"),
        (
            {
                "operation": "query",
                "query": "bounded",
                "limit": 1,
                "filters": None,
                "retrieval_mode": {},
            },
            "invalid_retrieval_mode",
        ),
    ],
)
def test_unhashable_worker_discriminators_return_structured_errors(payload, code):
    handler = object.__new__(_RequestHandler)
    handler.server = SimpleNamespace(state=_WorkerState("0" * 64, None, 0.0))
    handler.wfile = BytesIO()

    handler._dispatch("malformed-discriminator", payload)

    response = json.loads(handler.wfile.getvalue())
    assert response["success"] is False
    assert response["error"]["code"] == code


def test_crash_after_response_is_observed_without_process_leak():
    with SemanticWorkerManager(startup_deadline=10.0, fault="crash_after_response") as manager:
        response = manager.query("respond then crash")
        assert response["success"] is True
        deadline = time.monotonic() + 2.0
        while (
            manager._process is not None
            and manager._process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert manager._process is not None and manager._process.poll() is not None
        assert manager.reset()["success"] is True


def test_idle_ttl_stops_worker_lazily_without_wall_clock_margin():
    with SemanticWorkerManager(startup_deadline=10.0, idle_ttl=300.0) as manager:
        assert manager.health()["success"] is True
        manager.idle_ttl = 1.0
        manager._last_activity = float(manager._last_activity) - manager.idle_ttl
        assert manager.status()["state"] == "stopped"


def test_worker_pipes_are_drained_after_startup_handshake():
    with SemanticWorkerManager(
        startup_deadline=10.0, query_deadline=5.0, fault="stderr_flood"
    ) as manager:
        response = manager.query("CopyFace")

        assert response["success"] is True
        assert len(manager._pipe_threads) == 2


def test_context_manager_reaps_worker_when_test_body_raises():
    manager = SemanticWorkerManager(startup_deadline=10.0)
    process = None
    with pytest.raises(RuntimeError, match="injected polling failure"):
        with manager:
            assert manager.start()["success"] is True
            process = manager._process
            raise RuntimeError("injected polling failure")

    assert process is not None and process.poll() is not None
    assert manager.status(probe=False)["state"] == "stopped"
    assert process.stdout is not None and process.stdout.closed is True
    assert process.stderr is not None and process.stderr.closed is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process identity gate")
def test_missing_process_create_time_rejects_and_reaps_spawn(monkeypatch):
    monkeypatch.setattr(
        "src.knowledge.semantic_process._windows_process_create_time",
        lambda _handle: None,
    )
    with SemanticWorkerManager(startup_deadline=10.0) as manager:
        result = manager.start()

        assert result["success"] is False
        assert result["error"]["code"] == "startup_failed"
        assert result["cleanup"]["absent"] is True
        assert manager.status(probe=False)["state"] == "stopped"


def test_termination_exceptions_still_clear_finished_process_resources(monkeypatch):
    class Process:
        pid = 123
        _handle = 456

        def __init__(self):
            self.stdout = BytesIO(b"stdout")
            self.stderr = BytesIO(b"stderr")
            self.ended = False

        def poll(self):
            return 0 if self.ended else None

        def terminate(self):
            raise OSError("injected terminate failure")

        def wait(self, timeout):
            if not self.ended:
                raise subprocess.TimeoutExpired("semantic", timeout)
            return 0

        def kill(self):
            self.ended = True

    class Job:
        handle = 1

        def close(self):
            raise OSError("injected job close failure")

    manager = SemanticWorkerManager()
    process = Process()
    manager._process = process
    manager._identity = {
        "pid": process.pid,
        "process_create_time": 10.0,
        "command_signature": _command_signature(manager._command()),
    }
    manager._job = Job()
    manager._token = "0" * 64
    manager._port = 1
    monkeypatch.setattr(
        "src.knowledge.semantic_process._windows_process_create_time",
        lambda _handle: 10.0,
    )

    result = manager.reset()

    assert result["success"] is True
    assert result["reset"]["absent"] is True
    assert result["reset"]["cleanup_errors"]
    assert process.stdout.closed is True
    assert process.stderr.closed is True
    assert manager._process is manager._identity is manager._job is None


def test_hanging_semantic_worker_does_not_delay_control_plane_or_lexical_search():
    root = platform_test_root("semantic_worker") / uuid.uuid4().hex
    index = root / "manuals.sqlite3"
    runtime = root / "runtime"
    build_index_from_records(
        [
            {
                "source": "fake/manual.pdf",
                "module": "fake",
                "page": 1,
                "heading": "CopyFace",
                "text": "CopyFace source destination mesh",
            }
        ],
        index,
        corpus_fingerprint="semantic-worker-test",
    )
    try:
        with SemanticWorkerManager(
            startup_deadline=10.0, query_deadline=0.5, fault="query_hang"
        ) as manager:
            result: dict = {}

            job_manager = JobManager(root / "jobs", reconcile_on_start=False)
            job_id = job_manager.store.create(
                {"schema_version": "2", "job_type": "test_sequence"},
                {"schema_version": "2", "status": "completed", "worker_pid": None},
            )
            baseline_started = time.perf_counter()
            assert get_capabilities()["success"] is True
            assert SolverOwnership(runtime_dir=runtime).status()["lease"]["state"] == "absent"
            assert job_manager.status(job_id)["status"] == "completed"
            baseline_elapsed = time.perf_counter() - baseline_started

            thread = threading.Thread(
                target=lambda: result.update(manager.query("hang")), daemon=True
            )
            thread.start()
            time.sleep(0.1)
            control_started = time.perf_counter()
            capabilities = get_capabilities()
            ownership = SolverOwnership(runtime_dir=runtime).status()
            job_status = job_manager.status(job_id)
            control_elapsed = time.perf_counter() - control_started
            lexical_started = time.perf_counter()
            lexical = search_index("CopyFace", index_path=index)
            lexical_elapsed = time.perf_counter() - lexical_started
            join_budget = min(8.0, max(4.0, baseline_elapsed * 2.0 + 1.0))
            thread.join(timeout=join_budget)

            assert capabilities["success"] is True
            assert ownership["lease"]["state"] == "absent"
            # External solver discovery is host-wide. A user-owned standalone solve may
            # legitimately be present; this containment test requires responsiveness
            # and lease isolation, not an otherwise idle host.
            assert isinstance(ownership["external_solver_processes"], list)
            assert job_status["success"] is True and job_status["status"] == "completed"
            assert lexical["success"] is True and lexical["results"]
            # Host-wide process inventory latency changes with unrelated host load.
            # Keep an absolute containment ceiling and observable behavior checks;
            # a relative wall-clock comparison to an earlier baseline is unstable.
            assert control_elapsed < 8.0
            assert lexical_elapsed < 4.0
            assert not thread.is_alive()
            assert result["success"] is False and result["cleanup"]["absent"] is True
            assert not (runtime / "solver_owner.json").exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_parent_import_is_stdlib_only_and_spawns_nothing():
    code = """
import json, sys
process_launch_events = []
sys.addaudithook(
    lambda event, args: process_launch_events.append(event)
    if event in {
        'os.system', 'os.startfile', 'os.spawn',
        'os.posix_spawn', 'subprocess.Popen',
    } else None
)
import src.knowledge.semantic_process
for name in ('chromadb', 'torch', 'sentence_transformers', 'mph', 'psutil'):
    assert name not in sys.modules, name
assert process_launch_events == [], process_launch_events
print(json.dumps({'ok': True, 'launches': process_launch_events}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
        env=isolated_semantic_environment(),
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["ok"] is True


def test_failed_reset_replaces_stale_last_error(monkeypatch):
    import threading as threading_module
    from types import SimpleNamespace as _Namespace

    from src.knowledge.semantic_runtime import SemanticService

    service = object.__new__(SemanticService)
    service._lock = threading_module.Lock()
    service._manager = _Namespace(
        reset=lambda: {"success": False, "error": {"message": "worker pipe broken"}}
    )
    service._health_gate_passed = True
    service._last_error = {"message": "stale query failure"}

    result = service.reset()

    assert result["success"] is False
    assert service._health_gate_passed is False
    assert service._last_error == {"message": "worker pipe broken"}


def test_pid_matches_launcher_process_accepts_shallow_trampolines_only(monkeypatch):
    """3.15 venv trampolines re-exec the base interpreter as a waiting
    launcher's child, so the worker handshake must accept the launcher's
    executing descendants - but only within trampoline depth, never
    unrelated, unreadable, or dead pids."""
    import psutil
    from src.knowledge import semantic_process

    match = semantic_process._pid_matches_launcher_process
    launcher_pid = 10_000

    def fake_process_tree(chain):
        """psutil.Process stub whose parent() walks the given pid chain."""
        parents = {chain[index]: chain[index + 1] for index in range(len(chain) - 1)}
        terminals = set(chain)

        class _FakeProcess:
            def __init__(self, pid):
                if pid not in terminals:
                    raise psutil.NoSuchProcess(pid)
                object.__setattr__(self, "pid", pid)

            def parent(self):
                upstream = parents.get(object.__getattribute__(self, "pid"))
                if upstream is None:
                    return None
                return _FakeProcess(upstream)

        return _FakeProcess

    real_process = psutil.Process
    # chain: worker(10001) -> trampoline child(10002) -> launcher(10000)
    monkeypatch.setattr(psutil, "Process", fake_process_tree([10001, 10002, launcher_pid]))
    assert match(10001, launcher_pid) is True
    assert match(10002, launcher_pid) is True
    # The launcher itself matches (Python <= 3.14 in-place interpreters).
    assert match(launcher_pid, launcher_pid) is True
    # An unrelated live pid in the same stubbed world refuses.
    monkeypatch.setattr(psutil, "Process", fake_process_tree([20001, 20002, 30000]))
    assert match(20001, launcher_pid) is False
    # A chain deeper than any trampoline refuses.
    monkeypatch.setattr(
        psutil,
        "Process",
        fake_process_tree([10001, 10002, 10003, 10004, launcher_pid]),
    )
    assert match(10001, launcher_pid) is False
    # Unreadable (dead) pids refuse.
    monkeypatch.setattr(psutil, "Process", fake_process_tree([]))
    assert match(39_999, launcher_pid) is False
    monkeypatch.undo()
    # Malformed and non-positive values never reach the process table.
    assert match(True, launcher_pid) is False
    assert match("10001", launcher_pid) is False
    assert match(1.5, launcher_pid) is False
    assert match(0, launcher_pid) is False
    assert match(-10001, launcher_pid) is False
    # A real unrelated process against the real table still refuses.
    assert match(os.getpid(), launcher_pid) is False
    del real_process
