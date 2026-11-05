"""Alpha6 Settings GUI public contract locks."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import comsol_mcp.settings as settings_module
from comsol_mcp import __version__
from comsol_mcp.server import create_server
from comsol_mcp.tools.catalog import TOOL_SPECS
from development_kit.tests.platform_fixtures import platform_test_root

RELEASED_SETTINGS_VERSION = "1.3.0"
RELEASED_SETTINGS_READABLE_VERSIONS = (
    "1.0.0",
    "1.1.0",
    "1.2.0",
    RELEASED_SETTINGS_VERSION,
)
RELEASED_PACKAGE_VERSION = "0.8.1"


def test_alpha6_settings_schema_and_defaults_are_current_and_backward_readable(tmp_path):
    assert settings_module.SETTINGS_VERSION == RELEASED_SETTINGS_VERSION
    assert settings_module.SETTINGS_READABLE_VERSIONS == RELEASED_SETTINGS_READABLE_VERSIONS

    defaults = settings_module.default_settings_document()
    assert defaults["schema_name"] == "comsol_mcp.settings"
    assert defaults["schema_version"] == RELEASED_SETTINGS_VERSION
    assert defaults["comsol"] == {"installation_root": None}
    assert defaults["gui"] == {"language": "zh-cn", "scale": "system"}
    user_root = tmp_path / "用户配置" / "comsol_mcp"
    program_root = platform_test_root("alpha6-program-data/comsol_mcp")
    user_defaults = settings_module.default_settings_document(
        user_root=user_root,
        program_root=program_root,
    )
    assert user_defaults["runtime"]["directory"] == str(program_root / "runtime")
    assert user_defaults["paths"]["model_read_roots"] == [str(user_root / "models")]
    assert user_defaults["paths"]["artifact_write_root"] == str(program_root / "artifacts")
    assert str(user_root / "models").isascii() is False
    assert str(program_root / "runtime").isascii() is True
    assert str(program_root / "artifacts").isascii() is True
    assert user_defaults["manuals"] == {"root": None}
    assert user_defaults["lexical_docs"] == {"enabled": False, "index_path": None}
    assert user_defaults["semantic_docs"] == {
        "enabled": False,
        "root": None,
        "model_path": None,
    }

    legacy = tmp_path / "legacy-settings.json"
    legacy.write_text(
        json.dumps(
            {
                "schema_name": "comsol_mcp.settings",
                "schema_version": "1.0.0",
                "profile": {"name": "core"},
            }
        ),
        encoding="utf-8",
    )
    report = settings_module.load_settings_report({settings_module.SETTINGS_PATH_ENV: str(legacy)})
    assert report["errors"] == []
    assert report["settings"]["schema_version"] == RELEASED_SETTINGS_VERSION
    assert report["settings"]["comsol"]["installation_root"] is None
    assert report["settings"]["gui"]["language"] == "zh-cn"


def test_legacy_profile_migration_preserves_explicit_feature_opt_outs(tmp_path):
    for profile, feature in (("semantic_docs", "semantic_docs"), ("desktop_shared", "shared_server")):
        legacy = tmp_path / f"legacy-{feature}.json"
        legacy.write_text(json.dumps({
            "schema_name": "comsol_mcp.settings",
            "schema_version": "1.0.0",
            "profile": {"name": profile},
            feature: {"enabled": False},
        }), encoding="utf-8")
        report = settings_module.load_settings_report({settings_module.SETTINGS_PATH_ENV: str(legacy)})
        assert report["errors"] == []
        assert report["settings"][feature]["enabled"] is False


def test_alpha6_installed_location_uses_localappdata_before_bundled_template(tmp_path):
    local_appdata = tmp_path / "non-ascii-user-配置"
    bundled = tmp_path / "site-packages" / "comsol_mcp" / "settings.json"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("{}", encoding="utf-8")

    missing = settings_module.resolve_settings_location(
        {},
        source_settings_path=tmp_path / "missing-source.json",
        bundled_settings_path=bundled,
        local_appdata=local_appdata,
    )
    assert missing.source == "bundled_template"
    assert missing.path == bundled.resolve()
    assert missing.writable_path == local_appdata / "comsol_mcp" / "settings.json"
    assert missing.setup_required is True

    user_settings = missing.writable_path
    user_settings.parent.mkdir(parents=True)
    user_settings.write_text("{}", encoding="utf-8")
    existing = settings_module.resolve_settings_location(
        {},
        source_settings_path=tmp_path / "missing-source.json",
        bundled_settings_path=bundled,
        local_appdata=local_appdata,
    )
    assert existing.source == "user_settings"
    assert existing.path == user_settings.resolve()
    assert existing.writable_path == user_settings.resolve()
    assert existing.setup_required is False


def test_settings_start_is_profile_independent_and_solver_free(monkeypatch):
    monkeypatch.setenv("COMSOL_MCP_ENABLE_SHARED_SERVER", "true")
    spec = TOOL_SPECS["settings.start"]
    # settings.start is a control-plane core tool. The offline-only
    # comsolless_read_only profile intentionally exposes none of it, so its
    # intended surface is exactly the five solver-bound profiles.
    assert set(spec.intended_profiles) == {
        "core",
        "basic_fem",
        "wave_optics",
        "electro_chemistry",
        "experimental",
        "full",
    }
    assert spec.side_effect_class == "process_lifecycle"
    assert spec.concurrency_class == "control_plane"
    assert spec.starts_solver is False
    assert spec.requires_model_revision is False

    for profile in (
        "core",
        "basic_fem",
        "wave_optics",
        "experimental",
        "full",
    ):
        server = create_server(f"alpha6-{profile}", profile=profile)
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        assert "settings.start" in tools
        schema = tools["settings.start"].input_schema
        assert schema["properties"] == {}
        assert schema["additionalProperties"] is False


def test_settings_gui_console_entry_and_packages_are_declared():
    root = Path(__file__).parents[2]
    pyproject = root.joinpath("pyproject.toml").read_text(encoding="utf-8")
    assert 'comsol-mcp-settings = "settings_gui.__main__:main"' in pyproject
    assert 'comsol-mcp-settings-gui = "settings_gui.__main__:main"' in pyproject
    assert 'packages = ["comsol_mcp", "settings_gui"]' in pyproject
    assert __version__ == RELEASED_PACKAGE_VERSION
