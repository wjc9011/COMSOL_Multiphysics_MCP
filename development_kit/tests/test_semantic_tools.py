"""Semantic feature public schema, configuration, and degradation gates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer
from src.knowledge.semantic_runtime import SemanticService, semantic_configuration
from src.server import create_server
from src.tools.semantic_docs import register_semantic_doc_tools

from development_kit.tests.mcp_test_support import decode_tool_result
from development_kit.tests.platform_fixtures import platform_test_root
from development_kit.tests.semantic_test_support import isolated_semantic_environment


def _decode_public_tool_result(result):
    return decode_tool_result(result)


@pytest.fixture
def lightweight_deployment(ascii_tmp_path):
    root = ascii_tmp_path / "semantic-profile"
    index = root / "indexes" / "corpus" / "model" / "build-1"
    model = root / "models" / "model" / "r1"
    lexical = root / "lexical" / "manuals.sqlite3"
    index.mkdir(parents=True)
    model.mkdir(parents=True)
    lexical.parent.mkdir(parents=True)
    lexical.write_bytes(b"not-opened-by-lightweight-status")
    manifest = {
        "build_id": "build-1",
        "corpus_fingerprint": "a" * 64,
        "model_id": "test/model",
        "model_revision": "r1",
        "model_fingerprint": "b" * 64,
    }
    manifest_bytes = json.dumps(manifest).encode("utf-8")
    (index / "manifest.json").write_bytes(manifest_bytes)
    (model / "model_manifest.json").write_text(
        json.dumps(
            {
                "model_sha256": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    (root / "current.json").write_text(
        json.dumps(
            {
                "index_path": str(index),
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "build_id": "build-1",
                "model_fingerprint": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    environment = {
        "COMSOL_SEMANTIC_ROOT": str(root),
        "COMSOL_SEMANTIC_LEXICAL_INDEX": str(lexical),
        "COMSOL_SEMANTIC_MODEL_PATH": str(model),
    }
    try:
        yield environment
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_configuration_is_ascii_static_and_missing_model_degrades(lightweight_deployment):
    configured = semantic_configuration(lightweight_deployment)
    missing = semantic_configuration(
        {
            "COMSOL_SEMANTIC_ROOT": lightweight_deployment["COMSOL_SEMANTIC_ROOT"],
            "COMSOL_SEMANTIC_LEXICAL_INDEX": lightweight_deployment[
                "COMSOL_SEMANTIC_LEXICAL_INDEX"
            ],
        }
    )

    assert configured["configured"] is True
    assert configured["missing"] == []
    assert missing["configured"] is False
    assert "model_path_configuration" in missing["missing"]
    with pytest.raises(ValueError, match="ASCII"):
        semantic_configuration(
            {"COMSOL_SEMANTIC_ROOT": str(Path.home() / "mcp_tests/陆星/semantic")}
        )
    with pytest.raises(ValueError, match="must be absolute"):
        semantic_configuration({"COMSOL_SEMANTIC_ROOT": "relative/semantic"})


def test_configuration_does_not_infer_paths_from_host_drive_layout():
    configuration = semantic_configuration({})

    assert configuration["configured"] is False
    assert configuration["root"] is None
    assert configuration["lexical_index"] is None
    assert set(configuration["missing"]) == {
        "root_configuration",
        "lexical_index_configuration",
        "model_path_configuration",
    }


def test_cold_status_is_solver_free_and_does_not_start_worker(lightweight_deployment):
    service = SemanticService(lightweight_deployment)
    status = service.status(warm=False)

    assert status["success"] is True
    assert status["configured"] is True
    assert status["deployment"]["lightweight_identity_match"] is True
    assert status["worker"] == {"state": "stopped", "health": None}
    assert status["health_gate_passed"] is False
    assert status["available"] is False
    assert status["solver_free"] is True
    assert service._manager is None


def test_cold_status_does_not_follow_current_pointer_outside_deployment(
    lightweight_deployment, tmp_path
):
    root = Path(lightweight_deployment["COMSOL_SEMANTIC_ROOT"])
    pointer_path = root / "current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["index_path"] = str((tmp_path / "outside-index").resolve())
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

    status = SemanticService(lightweight_deployment).status(warm=False)

    assert status["deployment"]["readable"] is False
    assert status["worker"] == {"state": "stopped", "health": None}


def test_failed_warm_health_degrades_without_leaving_worker(lightweight_deployment):
    service = SemanticService(lightweight_deployment)
    try:
        status = service.status(warm=True)

        assert status["success"] is True
        assert status["available"] is False
        assert status["health_gate_passed"] is False
        assert status["worker"]["health"]["success"] is False
        assert status["worker"]["state"] == "stopped"
        assert service._manager is not None
        assert service._manager.status()["state"] == "stopped"
    finally:
        cleanup = service.reset()
    assert cleanup["success"] is True


def test_successful_semantic_operations_clear_stale_errors(lightweight_deployment):
    class Manager:
        def health(self):
            return {"success": True}

        def status(self, *, probe=False):
            return {"state": "active"}

        def query(self, *_args, **_kwargs):
            return {"success": True, "results": []}

        def reset(self):
            return {"success": True, "reset": {"absent": True}}

    service = SemanticService(lightweight_deployment)
    service._manager = Manager()
    service._last_error = {"code": "stale"}

    assert service.status(warm=True)["last_error"] is None
    service._last_error = {"code": "stale"}
    assert service.search("CopyFace")["success"] is True
    assert service._last_error is None
    service._last_error = {"code": "stale"}
    assert service.reset()["success"] is True
    assert service._last_error is None


def test_unconfigured_search_returns_explicit_lexical_fallback():
    service = SemanticService(
        {
            "COMSOL_SEMANTIC_ROOT": str(platform_test_root("missing-semantic-root")),
            "COMSOL_SEMANTIC_LEXICAL_INDEX": str(
                platform_test_root("missing-semantic-lexical.sqlite3")
            ),
        }
    )
    result = service.search("CopyFace")

    assert result["success"] is False
    assert result["error"]["code"] == "semantic_unavailable"
    assert result["fallback_tool"] == "manual_search"
    assert service._manager is None
    assert service.reset()["reset"] is False


def test_public_tool_schemas_expose_queries_filters_and_controls_but_no_paths(monkeypatch):
    calls = []

    class FakeService:
        def search(self, query, **kwargs):
            calls.append(("search", query, kwargs))
            return {"success": True, "results": []}

        def status(self, *, warm=False):
            calls.append(("status", warm))
            return {"success": True, "available": warm}

        def reset(self):
            calls.append(("reset",))
            return {"success": True}

    import src.tools.semantic_docs as module

    monkeypatch.setattr(module, "get_semantic_service", lambda: FakeService())
    server = MCPServer("semantic-tools-test")
    register_semantic_doc_tools(server)

    async def exercise():
        listed = await server.list_tools()
        result = await server.call_tool(
            "semantic_search",
            {
                "query": "periodic port",
                "module": "Wave_Optics_Module",
                "limit": 3,
                "source": None,
                "page_start": 10,
                "page_end": 20,
            },
        )
        status = await server.call_tool("semantic_status", {"warm": True})
        reset = await server.call_tool("semantic_worker_reset", {})
        return listed, result, status, reset

    listed, result, status, reset = asyncio.run(exercise())
    result = _decode_public_tool_result(result)
    status = _decode_public_tool_result(status)
    reset = _decode_public_tool_result(reset)
    schemas = {tool.name: tool.input_schema for tool in listed}
    serialized = json.dumps(schemas, sort_keys=True)

    assert result["success"] is status["success"] is reset["success"] is True
    assert calls[0][0] == "search" and calls[0][2]["page_start"] == 10
    assert set(schemas) == {"semantic_search", "semantic_status", "semantic_worker_reset"}
    assert "deployment_root" not in serialized
    assert "model_path" not in serialized
    assert "index_path" not in serialized
    assert "rebuild" not in serialized
    assert "delete" not in serialized


def test_semantic_profile_discovery_is_static_and_parent_imports_no_ml_stack():
    code = """
import asyncio, json, sys
from src.server import create_server
from development_kit.tests.mcp_test_support import decode_tool_result as decode
server = create_server('semantic-feature-subprocess', profile='core')
tools = asyncio.run(server.list_tools())
names = sorted(tool.name for tool in tools)
assert len(names) == 55
assert {'semantic_search','semantic_status','semantic_worker_reset'} <= set(names)
for name in ('chromadb','torch','sentence_transformers'):
    assert name not in sys.modules, name
status = decode(asyncio.run(server.call_tool('semantic_status', {'warm': False})))
assert status['worker']['state'] == 'stopped'
print(json.dumps({'count': len(names), 'configured': status['configured']}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        env=isolated_semantic_environment({"COMSOL_MCP_ENABLE_SEMANTIC_DOCS": "true"}),
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["count"] == 55


def test_semantic_and_other_profile_counts_match_declared_discovery(monkeypatch):
    monkeypatch.setenv("COMSOL_MCP_ENABLE_SEMANTIC_DOCS", "false")
    monkeypatch.setenv("COMSOL_MCP_ENABLE_SHARED_SERVER", "false")
    counts = {}
    for profile in (
        "core",
        "basic_fem",
        "wave_optics",
        "electro_chemistry",
        "experimental",
        "full",
        "comsolless_read_only",
    ):
        server = create_server(f"semantic-profile-{profile}", profile=profile)
        counts[profile] = len(asyncio.run(server.list_tools()))

    assert counts == {
        "core": 52,
        "basic_fem": 110,
        "wave_optics": 77,
        "electro_chemistry": 57,
        "experimental": 98,
        "full": 151,
        "comsolless_read_only": 5,
    }


def test_search_refuses_a_mismatched_deployment_without_querying(lightweight_deployment, tmp_path):
    root = Path(lightweight_deployment["COMSOL_SEMANTIC_ROOT"])
    pointer_path = root / "current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["index_path"] = str((tmp_path / "outside-index").resolve())
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

    service = SemanticService(lightweight_deployment)

    class SpyManager:
        def query(self, *_args, **_kwargs):
            pytest.fail("search must not query a mismatched deployment")

    service._manager = SpyManager()
    result = service.search("CopyFace")

    assert result["success"] is False
    assert result["error"]["code"] == "semantic_unavailable"
    assert result["deployment"]["readable"] is False
    assert result["fallback_tool"] == "manual_search"
    assert service._health_gate_passed is False


def test_available_requires_a_live_worker_state_even_after_passed_gate(lightweight_deployment):
    class StaleManager:
        def status(self, *, probe=False):
            return {"state": "stale"}

        def health(self):
            return {"success": True}

        def query(self, *_args, **_kwargs):
            return {"success": True, "results": []}

        def reset(self):
            return {"success": True, "reset": {"absent": True}}

    service = SemanticService(lightweight_deployment)
    service._manager = StaleManager()
    service._health_gate_passed = True

    status = service.status(warm=False)

    assert status["worker"]["state"] == "stale"
    assert status["health_gate_passed"] is True
    assert status["available"] is False


@pytest.mark.parametrize("healthy", [False, True])
@pytest.mark.parametrize("code", ["invalid_query", "invalid_limit", "invalid_filters"])
def test_invalid_search_preserves_worker_health(lightweight_deployment, healthy, code):
    class Manager:
        def query(self, *_args, **_kwargs):
            return {"success": False, "error": {"code": code, "message": "invalid input"}}

        def status(self, *, probe=False):
            return {"state": "active"}

    service = SemanticService(lightweight_deployment)
    service._manager = Manager()
    service._health_gate_passed = healthy
    result = service.search(" ")
    assert result["error"]["code"] == code
    assert service.status(warm=False)["health_gate_passed"] is healthy


def test_transport_error_invalidates_worker_health(lightweight_deployment):
    class Manager:
        def query(self, *_args, **_kwargs):
            return {"success": False, "error": {"code": "worker_timeout"}}

    service = SemanticService(lightweight_deployment)
    service._manager = Manager()
    service._health_gate_passed = True
    service.search("CopyFace")
    assert service._health_gate_passed is False
