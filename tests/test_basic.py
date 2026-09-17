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
    
    def test_generate_version_path_uses_structured_directory(self):
        from pathlib import Path

        from src.utils.versioning import generate_version_path, get_model_directory

        result = generate_version_path("model.mph")

        assert Path(result).parent == get_model_directory("model")
        assert Path(result).name.startswith("model_")
        assert result.endswith(".mph")

    def test_generate_version_path_honours_base_path(self):
        import shutil
        from pathlib import Path

        from src.utils.versioning import generate_version_path

        base = Path(__file__).resolve().parents[1] / "comsol_models" / "_test_base_path"
        shutil.rmtree(base, ignore_errors=True)
        try:
            result = generate_version_path("model.mph", base_path=str(base))

            assert Path(result).parent == base
            assert Path(result).name.startswith("model_")
            assert result.endswith(".mph")
        finally:
            shutil.rmtree(base, ignore_errors=True)
    
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
