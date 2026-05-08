"""Behavioral test for model_clone — verifies the createCopy bug fix
went through the spec's path B (save -> client.load -> session.add_model).

Spec: plans/mcp_model_clone_bugfix_spec.md §3 candidate B.

This test does NOT boot a real COMSOL/JVM. It uses a fake client/model
pair to verify the call sequence is correct (no createCopy, save+load
round trip used instead) and that the cloned model is registered.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


class _FakeClient:
    def __init__(self):
        self.loaded_paths: list = []
        self.loaded_model = None

    def load(self, path):
        self.loaded_paths.append(path)
        # Verify the temp file actually exists at load time.
        assert Path(path).exists(), f"Temp .mph not present: {path}"
        m = _FakeModel("clone")
        self.loaded_model = m
        return m


class _FakeModel:
    def __init__(self, name):
        self._name = name
        self.saved_paths: list = []
        self.java = MagicMock()

    def name(self):
        return self._name

    def save(self, path=None, format=None):
        # Mimic mph: writes a .mph at the given path.
        Path(path).write_bytes(b"\x00FAKE_MPH\x00")
        self.saved_paths.append(path)


def test_model_clone_uses_save_load_roundtrip(monkeypatch):
    from src.tools.session import session_manager
    from src.tools.model import register_model_tools

    # Capture all registered tools.
    captured: dict = {}

    class _Stub:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    register_model_tools(_Stub())
    assert "model_clone" in captured

    fake_client = _FakeClient()
    fake_model = _FakeModel("orig")

    monkeypatch.setattr(
        type(session_manager), "client",
        property(lambda self: fake_client),
    )

    real_get_model = session_manager.get_model

    def fake_get_model(name=None):
        if name in (None, "orig"):
            return fake_model
        return real_get_model(name)

    monkeypatch.setattr(session_manager, "get_model", fake_get_model)

    added: list = []

    def fake_add_model(model):
        added.append(model)
        return f"clone_{len(added)}"

    monkeypatch.setattr(session_manager, "add_model", fake_add_model)
    monkeypatch.setattr(session_manager, "set_current_model", lambda n: True)

    # Override current_model property to satisfy the is_current return key.
    monkeypatch.setattr(
        type(session_manager), "current_model",
        property(lambda self: "orig"),
    )

    result = captured["model_clone"](
        model_name="orig", new_name="my_clone", set_current=False
    )

    assert result["success"] is True, result
    # Ensure save was called (round-trip path), not createCopy.
    assert fake_model.saved_paths, "model.save() was not invoked"
    assert fake_client.loaded_paths, "client.load() was not invoked"
    # Saved file path == loaded file path == one of the temp .mph paths
    assert fake_model.saved_paths[0] == fake_client.loaded_paths[0]
    # The loaded clone was registered with session_manager.
    assert added == [fake_client.loaded_model]
    # Temp .mph cleaned up.
    assert not Path(fake_model.saved_paths[0]).exists(), \
        "Temp .mph should be cleaned up in the finally clause"
    # createCopy was never used.
    assert not fake_model.java.createCopy.called, \
        "Bug-fix regression: model_clone called the broken createCopy()."
