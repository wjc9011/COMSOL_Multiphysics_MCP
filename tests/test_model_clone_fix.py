"""Behavioral tests for model_clone — spec mcp_pr_c_fix_spec.md §4.1.

These tests do NOT boot a real COMSOL/JVM. They use fake client/model
stubs to verify:
  * the createCopy() bug fix (spec mcp_model_clone_bugfix_spec.md §3 candidate B)
  * A1: ``new_name`` is honored — the clone is registered under that name
  * A2: the source's stored file path is not mutated by a clone
  * core: tempfile cleanup happens in the finally block
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


class _FakeJava:
    """Minimal mph.java surface for model_clone."""
    def __init__(self, file_path: str = "/orig/path.mph", tag: str = "Model"):
        self._file_path = file_path
        self._tag = tag
        self._label = "orig"
        # Capture every save invocation so tests can assert which overload
        # was hit.
        self.save_calls: list = []

    def getFilePath(self):
        return self._file_path

    def tag(self):
        return self._tag

    def label(self, *args):
        if args:
            self._label = args[0]
            return None
        return self._label

    def save(self, *args):
        # mph.Model.save calls model.java.save(str(file)) for normal save,
        # or the (str, bool|str) overload for save-as-copy / format. We
        # mirror COMSOL's documented behavior: when called with the
        # (str, True) savecopy overload, do NOT mutate _file_path. Any
        # other overload mutates.
        self.save_calls.append(args)
        # Materialize the .mph so client.load can re-read it.
        if args and isinstance(args[0], str):
            Path(args[0]).write_bytes(b"\x00FAKE_MPH\x00")
            if not (len(args) >= 2 and args[1] is True):
                # mutate, simulating COMSOL's normal-save behavior
                self._file_path = args[0]


class _FakeClient:
    def __init__(self):
        self.loaded_paths: list = []
        self.loaded_model = None

    def load(self, path):
        self.loaded_paths.append(path)
        assert Path(path).exists(), f"Temp .mph not present: {path}"
        m = _FakeModel("clone")
        self.loaded_model = m
        return m


class _FakeModel:
    def __init__(self, label: str, file_path: str | None = None):
        # Backing label/state held in the fake java
        self.java = _FakeJava(file_path=file_path or "/auto/path.mph")
        self.java._label = label
        # mph.Model.save fallback path also gets called in test_*_uses_*
        self.saved_paths: list = []

    def name(self):
        # Mirror mph.Model.name(): label() with trailing .mph stripped.
        lbl = self.java._label
        if lbl.endswith('.mph'):
            lbl = lbl.rsplit('.', maxsplit=1)[0]
        return lbl

    def file(self):
        return Path(self.java._file_path)

    def rename(self, name: str):
        # Mirror mph.Model.rename().
        self.java.label(name)

    def save(self, path=None, format=None):
        # mph wrapper-style fallback (only used if model.java.save raises)
        Path(path).write_bytes(b"\x00FAKE_MPH\x00")
        self.saved_paths.append(path)
        # mutate (the wrapper does)
        self.java._file_path = path


def _capture_model_tools():
    from src.tools.model import register_model_tools
    captured: dict = {}

    class _Stub:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    register_model_tools(_Stub())
    return captured


def _wire_session(monkeypatch, fake_client, fake_model):
    from src.tools.session import session_manager

    monkeypatch.setattr(
        type(session_manager), "client",
        property(lambda self: fake_client),
    )
    monkeypatch.setattr(
        session_manager, "get_model",
        lambda name=None: fake_model if name in (None, fake_model.name()) else None,
    )

    added: list = []
    last_name = {"value": None}

    def fake_add_model(model):
        added.append(model)
        nm = model.name()
        last_name["value"] = nm
        return nm

    monkeypatch.setattr(session_manager, "add_model", fake_add_model)
    monkeypatch.setattr(session_manager, "set_current_model", lambda n: True)
    monkeypatch.setattr(
        type(session_manager), "current_model",
        property(lambda self: last_name["value"]),
    )
    return added


def test_model_clone_uses_save_load_roundtrip(monkeypatch):
    """Bug-fix regression: model_clone must NOT call createCopy(); must
    use the temp save+load round-trip (spec candidate B)."""
    captured = _capture_model_tools()
    assert "model_clone" in captured

    fake_model = _FakeModel("orig", file_path="/orig/test.mph")
    fake_client = _FakeClient()
    _wire_session(monkeypatch, fake_client, fake_model)

    result = captured["model_clone"](
        model_name="orig", new_name="my_clone", set_current=False
    )

    assert result["success"] is True, result
    # Save was invoked (round-trip path).
    assert fake_model.java.save_calls or fake_model.saved_paths, \
        "neither java.save nor model.save was invoked"
    # client.load was called with that temp path.
    assert fake_client.loaded_paths
    saved_path = (
        fake_model.java.save_calls[0][0]
        if fake_model.java.save_calls else fake_model.saved_paths[0]
    )
    assert saved_path == fake_client.loaded_paths[0]
    # Tempfile cleanup ran.
    assert not Path(saved_path).exists(), \
        "Temp .mph should be cleaned up in the finally clause"


def test_model_clone_preserves_new_name(monkeypatch):
    """A1 regression: model_clone(new_name='ws') must register the clone
    under 'ws', not under the temp-file basename (spec §4.1.1)."""
    captured = _capture_model_tools()

    fake_model = _FakeModel("orig", file_path="/orig/test.mph")
    fake_client = _FakeClient()
    added = _wire_session(monkeypatch, fake_client, fake_model)

    result = captured["model_clone"](
        model_name="orig", new_name="ws", set_current=False
    )

    assert result["success"] is True, result
    assert result["clone"] == "ws", (
        f"clone name should be 'ws' (the new_name); got {result['clone']!r}"
    )
    # The model that was added to the session has its label set to 'ws'.
    assert added and added[-1].name() == "ws"


def test_model_clone_does_not_mutate_original_file(monkeypatch):
    """A2 regression: cloning must not change the source model's stored
    file path. Implementation uses model.java.save(<path>, true) — the
    boolean savecopy overload that COMSOL_ProgrammingReferenceManual
    documents as 'location of that copy is not remembered' (spec §4.1.2)."""
    captured = _capture_model_tools()

    fake_model = _FakeModel("orig", file_path="/orig/preserved.mph")
    orig_file_before = fake_model.java.getFilePath()
    fake_client = _FakeClient()
    _wire_session(monkeypatch, fake_client, fake_model)

    result = captured["model_clone"](
        model_name="orig", new_name="ws", set_current=False
    )

    assert result["success"] is True, result
    orig_file_after = fake_model.java.getFilePath()
    assert orig_file_before == orig_file_after, (
        f"source file path mutated: {orig_file_before!r} -> {orig_file_after!r}"
    )
    # Verify we used the savecopy=True overload (second arg is bool True)
    # rather than the path-mutating single-arg form.
    assert any(
        len(call) >= 2 and call[1] is True
        for call in fake_model.java.save_calls
    ), (
        f"model_clone should call java.save(path, True) (savecopy boolean); "
        f"saw {fake_model.java.save_calls!r}"
    )


def test_model_clone_response_includes_tags(monkeypatch):
    """Spec §4.1.3: response carries original/clone display names AND
    Java tags so callers can disambiguate."""
    captured = _capture_model_tools()

    fake_model = _FakeModel("orig", file_path="/orig/x.mph")
    fake_client = _FakeClient()
    _wire_session(monkeypatch, fake_client, fake_model)

    result = captured["model_clone"](
        model_name="orig", new_name="ws", set_current=False
    )

    assert result["success"] is True
    assert "original" in result and "clone" in result
    assert "original_tag" in result and "clone_tag" in result
