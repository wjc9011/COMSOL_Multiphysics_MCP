"""Basic tests for COMSOL MCP Server."""

import pytest


class TestVersioning:
    """Tests for version naming utilities."""
    
    def test_generate_version_name(self):
        from src.utils.versioning import generate_version_name
        
        result = generate_version_name("model.mph")
        assert result.startswith("model_")
        assert result.endswith(".mph")
        assert len(result) > len("model.mph")
    
    def test_generate_version_name_no_extension(self):
        from src.utils.versioning import generate_version_name
        
        result = generate_version_name("model")
        assert result.startswith("model_")
        assert result.endswith(".mph")
    
    def test_generate_version_path(self):
        from src.utils.versioning import generate_version_path
        
        result = generate_version_path("/path/to/model.mph")
        assert "/path/to/model_" in result
        assert result.endswith(".mph")
    
    def test_parse_version_info_valid(self):
        from src.utils.versioning import parse_version_info
        
        result = parse_version_info("model_20260215_143022.mph")
        assert result is not None
        assert result["base_name"] == "model"
        assert result["timestamp"] == "20260215_143022"
    
    def test_parse_version_info_invalid(self):
        from src.utils.versioning import parse_version_info
        
        result = parse_version_info("model.mph")
        assert result is None
        
        result = parse_version_info("model_20260215.mph")
        assert result is None


class TestSessionManager:
    """Tests for session manager (without actual COMSOL)."""
    
    def test_session_manager_singleton(self):
        from src.tools.session import SessionManager
        
        sm1 = SessionManager()
        sm2 = SessionManager()
        assert sm1 is sm2
    
    def test_session_manager_initial_state(self):
        from src.tools.session import SessionManager
        
        sm = SessionManager()
        assert sm.client is None
        assert not sm.is_connected
        assert sm.current_model is None
        assert sm.models == {}
    
    def test_get_status_disconnected(self):
        from src.tools.session import SessionManager
        
        sm = SessionManager()
        status = sm.get_status()
        assert status["connected"] is False


class FakeClient:
    """Stand-in for mph.Client that records its constructor arguments."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.version = kwargs.get("version") or "6.3"
        self.cores = kwargs.get("cores") or 1
        self.standalone = True

    def clear(self):
        self.cleared = True


@pytest.fixture
def session_env(monkeypatch):
    """Isolate the SessionManager singleton and capture mph.Client construction."""
    from src.tools import session as session_module

    created = []

    def fake_client(**kwargs):
        client = FakeClient(**kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(session_module.mph, "Client", fake_client)
    monkeypatch.setattr(session_module.mph_session, "client", None, raising=False)

    manager = session_module.SessionManager()
    monkeypatch.setattr(manager, "_client", None)
    monkeypatch.setattr(manager, "_models", {})
    monkeypatch.setattr(manager, "_current_model", None)
    return manager, created


class TestDefaultVersion:
    """COMSOL_MCP_VERSION selects the back-end when no version is passed."""

    def test_env_version_is_used(self, session_env, monkeypatch):
        monkeypatch.setenv("COMSOL_MCP_VERSION", "6.3")
        manager, created = session_env

        result = manager.start(cores=4)

        assert result["success"] is True
        assert created[0].kwargs == {"cores": 4, "version": "6.3"}

    def test_explicit_version_wins_over_env(self, session_env, monkeypatch):
        monkeypatch.setenv("COMSOL_MCP_VERSION", "6.1")
        manager, created = session_env

        manager.start(version="6.3")

        assert created[0].kwargs["version"] == "6.3"

    def test_version_omitted_without_env(self, session_env, monkeypatch):
        monkeypatch.delenv("COMSOL_MCP_VERSION", raising=False)
        manager, created = session_env

        manager.start()

        assert "version" not in created[0].kwargs

    def test_blank_env_falls_back_to_default(self, session_env, monkeypatch):
        monkeypatch.setenv("COMSOL_MCP_VERSION", "")
        manager, created = session_env

        manager.start()

        assert "version" not in created[0].kwargs
