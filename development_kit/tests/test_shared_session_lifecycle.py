"""Fake-client tests for non-owning shared attach and detach lifecycle."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import src.shared_session.lifecycle as lifecycle_module
from src.shared_session.contracts import SHARED_SERVER_FEATURE_ENV
from src.shared_session.lifecycle import (
    SharedSessionManager,
    _default_model_inventory_reader,
    _default_model_revision_reader,
    _default_save_copy_writer,
)
from src.tools.ownership import _command_signature


def _process(pid, kind, command, *, windows=0, created=None):
    return {
        "pid": pid,
        "parent_pid": 0,
        "kind": kind,
        "create_time": float(pid if created is None else created),
        "command_signature": _command_signature(command),
        "file_version": "6.4.0.293",
        "window_count": windows,
        "responding": True,
    }


def _snapshot(server_created=20.0, listener_host="127.0.0.1", observed=1000.0):
    return {
        "inventory_complete": True,
        "observed_at_epoch": observed,
        "processes": [
            _process(10, "comsol_desktop", ["comsol.exe"], windows=1),
            _process(
                20,
                "comsol_server",
                ["comsolmphserver.exe", "-port", "2036"],
                created=server_created,
            ),
        ],
        "listeners": [{"host": listener_host, "port": 2036, "pid": 20}],
    }


def _request():
    return {
        "endpoint": {"host": "127.0.0.1", "port": 2036},
        "user_confirmed": True,
    }


def _selector():
    return {
        "tag": "Model_1",
        "expected_label": "Shared",
        "expected_unsaved": True,
    }


class FakeOwnership:
    def __init__(self, root):
        self.lease_path = root / "solver_owner.json"
        self.releases = 0

    def acquire_attached(self, identity):
        payload = {
            "acquisition_id": "a" * 32,
            "attached_server": {"server_pid": identity.server_pid, "owned": False},
        }
        self.lease_path.write_text(json.dumps(payload), encoding="utf-8")
        return {"success": True, "lease": payload}

    def release(self):
        self.releases += 1
        self.lease_path.unlink(missing_ok=True)
        return {"success": True, "released": True}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.disconnected = False

    def disconnect(self):
        self.calls.append("disconnect")
        self.disconnected = True

    def clear(self):
        self.calls.append("clear")
        raise AssertionError("attached lifecycle must never clear models")


class FakeJavaModel:
    def __init__(self, tag, label, path):
        self._tag = tag
        self._label = label
        self._path = path
        self.save_calls = []

    def tag(self):
        return self._tag

    def label(self):
        return self._label

    def getFilePath(self):
        return self._path

    def save(self, *args):
        self.save_calls.append(args)


class FakeMphModel:
    def __init__(self, tag, label, path):
        self.java = FakeJavaModel(tag, label, path)


class InventoryClient:
    def __init__(self, models):
        self._models = models

    def models(self):
        return self._models


class RevisionNode:
    def __init__(self, path, tag, node_type, properties=None, children=None):
        self.path = path
        self._tag = tag
        self._type = node_type
        self._properties = properties or {}
        self._children = children or []

    def __str__(self):
        return self.path

    def tag(self):
        return self._tag

    def type(self):
        return self._type

    def properties(self):
        return self._properties

    def children(self):
        return self._children


class RevisionModel:
    __module__ = "mph.model"

    def __init__(self):
        self.java = FakeJavaModel("Model_1", "Shared", "")
        self.groups = {
            group: RevisionNode(group, None, None)
            for group in lifecycle_module.REVISION_TREE_GROUPS
        }
        self._parameters = {"gap": "period/10"}
        self._descriptions = {"gap": "geometry dependency"}

    def __truediv__(self, group):
        return self.groups[group]

    def parameters(self, evaluate=False):
        assert evaluate is False
        return dict(self._parameters)

    def descriptions(self):
        return dict(self._descriptions)


def _revision_tree_model():
    model = RevisionModel()
    for group, node_type in (
        ("geometries", "Block"),
        ("physics", "ElectromagneticWaves"),
        ("materials", "Common"),
        ("meshes", "FreeTri"),
        ("studies", "Frequency"),
        ("solutions", "SolverSequence"),
    ):
        model.groups[group]._children = [
            RevisionNode(
                f"{group}/{group[:-1]}1",
                f"{group[:3]}1",
                node_type,
                {"dependency": "gap", "setting": "baseline"},
            )
        ]
    return model


def _inventory(models=None):
    return (
        models
        if models is not None
        else [{"tag": "Model_1", "label": "Shared", "file_path": None, "unsaved": True}]
    )


def _manager(
    tmp_path,
    *,
    client=None,
    snapshots=None,
    models=None,
    client_factory=None,
    client_version="6.4.0.293",
    revision_state=None,
    snapshot_writer=None,
    bounded_snapshot_writer=True,
    manifest_writer=None,
    ownership=None,
):
    snapshots = snapshots or [_snapshot() for _ in range(10)]
    snapshots = [
        {**snapshot, "observed_at_epoch": 1000.0 + index}
        for index, snapshot in enumerate(snapshots)
    ]

    class ScriptedSnapshots:
        def __init__(self, values):
            self.values = values
            self.calls = 0

        def __call__(self):
            if self.calls >= len(self.values):
                raise AssertionError("unexpected extra shared-session snapshot read")
            value = self.values[self.calls]
            self.calls += 1
            return value

    class RecordingReader:
        def __init__(self, callback):
            self.callback = callback
            self.calls = 0

        def __call__(self, *args):
            self.calls += 1
            return self.callback(*args)

    provider = ScriptedSnapshots(snapshots)
    model_reader = RecordingReader(lambda _value: _inventory(models))
    revision_reader = RecordingReader(
        lambda _value, _tag: (revision_state["structural"], revision_state["state"])
    )
    ownership = ownership or FakeOwnership(tmp_path)
    client = client or FakeClient()
    revision_state = revision_state or {
        "structural": {"components": ["comp1"], "studies": ["std1"]},
        "state": {"parameters": {"gap": "10[nm]"}},
    }
    manager = SharedSessionManager(
        snapshot_provider=provider,
        ownership_factory=lambda: ownership,
        client_factory=client_factory or (lambda host, port: client),
        client_version_reader=lambda value: client_version,
        model_inventory_reader=model_reader,
        model_revision_reader=revision_reader,
        mcp_process_identity_provider=lambda: {
            "pid": 5000,
            "process_create_time": 900.0,
            "command_signature": "f" * 64,
        },
        snapshot_target_factory=lambda tag: tmp_path / f"{tag}-snapshot.mph",
        save_copy_writer=snapshot_writer
        or (lambda value, tag, target: target.write_bytes(b"snapshot fixture")),
        save_copy_writer_is_bounded=bounded_snapshot_writer,
        manifest_writer=manifest_writer
        or (lambda path, value: path.write_text(json.dumps(value), encoding="utf-8")),
        clock=lambda: 1100.0,
        attached_receipt_root=tmp_path / lifecycle_module.ATTACHED_HANDOFF_RECEIPT_DIRECTORY,
    )
    manager._test_snapshot_provider = provider
    manager._test_model_reader = model_reader
    manager._test_revision_reader = revision_reader
    return (manager, ownership, client)


def test_default_inventory_uses_raw_java_path_for_unsaved_models():
    inventory = _default_model_inventory_reader(
        InventoryClient(
            [
                FakeMphModel("Model_1", "Blank", ""),
                FakeMphModel("Model_2", "共享", "C:/研究/共享.mph"),
            ]
        )
    )

    assert inventory == [
        {"tag": "Model_1", "label": "Blank", "file_path": None, "unsaved": True},
        {
            "tag": "Model_2",
            "label": "共享",
            "file_path": "C:/研究/共享.mph",
            "unsaved": False,
        },
    ]


def test_default_snapshot_writer_uses_clientapi_save_copy_overload(tmp_path):
    model = FakeMphModel("Model_1", "Shared", "")
    target = tmp_path / "copy.mph"

    _default_save_copy_writer(InventoryClient([model]), "Model_1", target)

    assert model.java.save_calls == [(str(target), True)]


def test_default_revision_reader_hashes_consequential_model_tree_state():
    baseline_model = _revision_tree_model()
    baseline_structural, baseline_state = _default_model_revision_reader(
        InventoryClient([baseline_model]), "Model_1"
    )

    for group in (
        "geometries",
        "physics",
        "materials",
        "meshes",
        "studies",
        "solutions",
    ):
        changed_model = _revision_tree_model()
        changed_model.groups[group]._children[0]._properties["setting"] = "changed"
        changed_structural, changed_state = _default_model_revision_reader(
            InventoryClient([changed_model]), "Model_1"
        )
        assert changed_structural == baseline_structural
        assert changed_state["model_tree"][group] != (baseline_state["model_tree"][group])

    structural_model = _revision_tree_model()
    structural_model.groups["physics"]._children.append(
        RevisionNode("physics/ewfd2", "ewfd2", "ElectromagneticWaves")
    )
    changed_structural, _changed_state = _default_model_revision_reader(
        InventoryClient([structural_model]), "Model_1"
    )
    assert (
        changed_structural["model_tree"]["physics"]
        != (baseline_structural["model_tree"]["physics"])
    )

    parameter_model = _revision_tree_model()
    parameter_model._parameters["gap"] = "period/20"
    _structural, parameter_state = _default_model_revision_reader(
        InventoryClient([parameter_model]), "Model_1"
    )
    assert parameter_state != baseline_state


def test_default_revision_reader_fails_closed_at_tree_node_limit(monkeypatch):
    model = _revision_tree_model()
    monkeypatch.setattr(lifecycle_module, "MAX_REVISION_TREE_NODES", 1)

    with pytest.raises(ValueError, match="revision tree exceeds 1 nodes"):
        _default_model_revision_reader(InventoryClient([model]), "Model_1")


def test_attached_inventory_is_bounded_sorted_and_keeps_duplicate_metadata(tmp_path):
    models = [
        {"tag": "Model_2", "label": "Shared", "file_path": None, "unsaved": True},
        {"tag": "Model_1", "label": "Shared", "file_path": None, "unsaved": True},
    ]
    manager, _ownership, _client = _manager(tmp_path, models=models)
    assert (
        manager.attach(
            _request(),
            profile="core",
            environ={SHARED_SERVER_FEATURE_ENV: "true"},
        )["success"]
        is True
    )

    result = manager.models()

    assert result["success"] is True
    assert result["model_count"] == 2
    assert [item["tag"] for item in result["models"]] == ["Model_1", "Model_2"]
    assert [item["label"] for item in result["models"]] == ["Shared", "Shared"]
    assert result["model_inventory_sha256"] == result["attached_inventory_sha256"]


def test_attach_preserves_wildcard_listener_scope_in_server_identity(tmp_path):
    manager, _ownership, _client = _manager(
        tmp_path,
        snapshots=[_snapshot(listener_host="0.0.0.0") for _ in range(10)],
    )

    result = manager.attach(
        _request(),
        profile="core",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )

    assert result["success"] is True
    assert result["preflight"]["listener_bind_scope"] == "wildcard"
    assert result["preflight"]["warnings"] == ["listener_bind_scope=wildcard"]
    assert manager._server_identity.listener_bind_scope == "wildcard"


def test_exact_tag_adoption_allows_duplicate_unicode_labels_and_paths(tmp_path):
    shared_path = "C:/研究/共享.mph"
    models = [
        {
            "tag": "Model_2",
            "label": "共享",
            "file_path": shared_path,
            "unsaved": False,
        },
        {
            "tag": "Model_1",
            "label": "共享",
            "file_path": shared_path,
            "unsaved": False,
        },
    ]
    selector = {
        "tag": "Model_1",
        "expected_label": "共享",
        "expected_file_path": shared_path,
    }
    manager, _ownership, _client = _manager(tmp_path, models=models)

    attached = manager.attach(
        _request(),
        profile="core",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )
    adopted = manager.adopt_model(selector)

    assert attached["success"] is True
    assert adopted["selected_model"]["tag"] == "Model_1"
    assert adopted["selected_model"]["file_path"] == "C:\\研究\\共享.mph"
    assert attached["model_count"] == 2


def test_duplicate_server_model_tags_fail_attach_closed(tmp_path):
    models = [
        {"tag": "Model_1", "label": "First", "file_path": None, "unsaved": True},
        {"tag": "Model_1", "label": "Second", "file_path": None, "unsaved": True},
    ]
    manager, ownership, client = _manager(tmp_path, models=models)

    result = manager.attach(
        _request(),
        profile="core",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )

    assert result["success"] is False
    assert result["state"] == "attach_failed"
    assert "duplicate tags" in result["error"]
    assert client.calls == ["disconnect"]
    assert ownership.releases == 1


def test_model_inventory_requires_an_attached_client(tmp_path):
    manager, _ownership, _client = _manager(tmp_path)

    assert manager.models() == {
        "success": False,
        "state": "detached",
        "models": [],
        "model_count": 0,
    }


def _attach_and_lock(manager):
    assert (
        manager.attach(
            _request(),
            profile="core",
            environ={SHARED_SERVER_FEATURE_ENV: "true"},
        )["success"]
        is True
    )
    assert manager.adopt_model(_selector())["success"] is True
    locked = manager.lock_model(collaboration_mode="interactive_inspection")
    assert locked["success"] is True
    return locked["model_lock"]


def _attach_saved_and_lock(manager, source, *, collaboration_mode):
    assert (
        manager.attach(
            _request(),
            profile="core",
            environ={SHARED_SERVER_FEATURE_ENV: "true"},
        )["success"]
        is True
    )
    assert (
        manager.adopt_model(
            {
                "tag": "Model_1",
                "expected_label": "Working",
                "expected_file_path": "D:/models/working.mph",
            }
        )["success"]
        is True
    )
    locked = manager.lock_model(
        collaboration_mode=collaboration_mode,
        immutable_source={
            "path": str(source),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        },
    )
    assert locked["success"] is True
    return locked["model_lock"]


def _prepare_saved_handoff(manager, source):
    model_lock = _attach_saved_and_lock(
        manager,
        source,
        collaboration_mode="automation_exclusive",
    )
    handoff = manager.prepare_attached_job_handoff(
        expected_lock_sha256=model_lock["lock_sha256"],
        expected_revision_sha256=model_lock["revision"]["revision_sha256"],
        source_model_path=str(source),
        user_confirmed_automation_exclusive=True,
    )
    assert handoff["success"] is True
    return handoff


def test_attached_job_handoff_recovery_reattaches_and_re_adopts_exact_model(tmp_path):
    source = tmp_path / "immutable-source.mph"
    source.write_bytes(b"immutable source")
    models = [
        {
            "tag": "Model_1",
            "label": "Working",
            "file_path": "D:/models/working.mph",
            "unsaved": False,
        }
    ]
    manager, ownership, client = _manager(tmp_path, models=models)
    handoff = _prepare_saved_handoff(manager, source)

    recovered = manager.recover_attached_job_handoff(
        handoff["execution_backend"],
        profile="wave_optics",
        feature_enabled=True,
    )

    assert recovered["success"] is True
    assert recovered["state"] == "attached_handoff_reclaimed_pending_lock"
    assert (
        recovered["server_identity_sha256"]
        == (handoff["execution_backend"]["attached_server"]["identity_sha256"])
    )
    assert (
        recovered["model_identity_sha256"]
        == (handoff["execution_backend"]["model"]["identity_sha256"])
    )
    assert recovered["model_lock_restored"] is False
    assert manager.status()["state"] == "attached_model_pending_lock"
    assert client.calls == ["disconnect"]
    assert ownership.releases == 1


def test_attached_job_handoff_recovery_returns_structured_invalid_backend(tmp_path):
    manager, _ownership, _client = _manager(tmp_path)

    result = manager.recover_attached_job_handoff(
        {},
        profile="core",
        feature_enabled=True,
    )

    assert result["success"] is False
    assert result["state"] == "attached_handoff_backend_invalid"


@pytest.mark.parametrize("changed_identity", ["server", "model"])
def test_attached_job_handoff_recovery_fails_closed_and_detaches_changed_target(
    tmp_path,
    changed_identity,
):
    source = tmp_path / "immutable-source.mph"
    source.write_bytes(b"immutable source")
    models = [
        {
            "tag": "Model_1",
            "label": "Working",
            "file_path": "D:/models/working.mph",
            "unsaved": False,
        }
    ]
    manager, ownership, client = _manager(tmp_path, models=models)
    handoff = _prepare_saved_handoff(manager, source)
    if changed_identity == "server":
        observed = 2000.0

        def changed_server_snapshot():
            nonlocal observed
            snapshot = _snapshot(server_created=21.0, observed=observed)
            observed += 1.0
            return snapshot

        manager._snapshot_provider = changed_server_snapshot
        expected_state = "attached_handoff_server_identity_changed"
    else:
        manager._model_inventory_reader = lambda _client: [
            {
                "tag": "Model_1",
                "label": "Changed",
                "file_path": "D:/models/working.mph",
                "unsaved": False,
            }
        ]
        expected_state = "attached_handoff_model_recovery_failed"

    recovered = manager.recover_attached_job_handoff(
        handoff["execution_backend"],
        profile="wave_optics",
        feature_enabled=True,
    )

    assert recovered["success"] is False
    assert recovered["state"] == expected_state
    assert recovered["cleanup"]["success"] is True
    assert manager.status()["state"] == "detached"
    assert client.calls == ["disconnect", "disconnect"]
    assert ownership.releases == 2


def test_model_lock_binds_fresh_server_model_revision_and_process(tmp_path):
    manager, _ownership, _client = _manager(tmp_path)

    lock = _attach_and_lock(manager)
    status = manager.status()

    assert lock["attached_server"]["server_pid"] == 20
    assert lock["model"]["tag"] == "Model_1"
    assert lock["revision"]["sequence"] == 0
    assert lock["mcp_process"]["pid"] == 5000
    assert status["state"] == "attached_model_locked"
    assert status["model_lock"]["lock_sha256"] == lock["lock_sha256"]


def test_model_lock_verifies_immutable_source_bytes(tmp_path):
    manager, _ownership, _client = _manager(tmp_path)
    assert (
        manager.attach(
            _request(),
            profile="core",
            environ={SHARED_SERVER_FEATURE_ENV: "true"},
        )["success"]
        is True
    )
    assert manager.adopt_model(_selector())["success"] is True
    source = tmp_path / "source.mph"
    source.write_bytes(b"immutable model fixture")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()

    rejected = manager.lock_model(
        collaboration_mode="interactive_inspection",
        immutable_source={"path": str(source), "sha256": "0" * 64},
    )
    accepted = manager.lock_model(
        collaboration_mode="interactive_inspection",
        immutable_source={"path": str(source), "sha256": source_sha256},
    )

    assert rejected["success"] is False
    assert "does not match" in rejected["error"]
    assert accepted["success"] is True
    assert accepted["model_lock"]["immutable_source"]["sha256"] == source_sha256


def test_automation_handoff_verifies_target_then_detaches_without_clear(tmp_path):
    source = tmp_path / "immutable-source.mph"
    source.write_bytes(b"immutable source")
    models = [
        {
            "tag": "Model_1",
            "label": "Working",
            "file_path": "D:/models/working.mph",
            "unsaved": False,
        }
    ]
    manager, ownership, client = _manager(tmp_path, models=models)
    lock = _attach_saved_and_lock(manager, source, collaboration_mode="automation_exclusive")

    result = manager.prepare_attached_job_handoff(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        source_model_path=str(source),
        user_confirmed_automation_exclusive=True,
    )

    assert result["success"] is True
    assert result["state"] == "attached_job_handoff_ready"
    assert result["execution_backend"]["kind"] == "attached_shared_server"
    assert result["execution_backend"]["source_model_lock_sha256"] == (lock["lock_sha256"])
    assert result["execution_backend"]["model"]["file_path"] == ("D:\\models\\working.mph")
    assert result["detach"]["external_resources_preserved"] is True
    assert manager.status()["state"] == "detached"
    assert client.calls == ["disconnect"]
    assert ownership.releases == 1


def test_automation_handoff_requires_exclusive_lock_and_confirmation(tmp_path):
    source = tmp_path / "immutable-source.mph"
    source.write_bytes(b"immutable source")
    models = [
        {
            "tag": "Model_1",
            "label": "Working",
            "file_path": "D:/models/working.mph",
            "unsaved": False,
        }
    ]
    manager, ownership, client = _manager(tmp_path, models=models)
    lock = _attach_saved_and_lock(manager, source, collaboration_mode="interactive_inspection")

    confirmation = manager.prepare_attached_job_handoff(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        source_model_path=str(source),
        user_confirmed_automation_exclusive=False,
    )
    mode = manager.prepare_attached_job_handoff(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        source_model_path=str(source),
        user_confirmed_automation_exclusive=True,
    )

    assert confirmation["state"] == "automation_confirmation_required"
    assert mode["state"] == "automation_exclusive_lock_required"
    assert manager.status()["state"] == "attached_model_locked"
    assert client.calls == []
    assert ownership.releases == 0


def test_automation_handoff_rejects_mismatched_immutable_source_before_detach(
    tmp_path,
):
    source = tmp_path / "immutable-source.mph"
    other = tmp_path / "other-source.mph"
    source.write_bytes(b"immutable source")
    other.write_bytes(b"other source")
    models = [
        {
            "tag": "Model_1",
            "label": "Working",
            "file_path": "D:/models/working.mph",
            "unsaved": False,
        }
    ]
    manager, ownership, client = _manager(tmp_path, models=models)
    lock = _attach_saved_and_lock(manager, source, collaboration_mode="automation_exclusive")

    result = manager.prepare_attached_job_handoff(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        source_model_path=str(other),
        user_confirmed_automation_exclusive=True,
    )

    assert result["success"] is False
    assert result["state"] == "handoff_target_rejected"
    assert "does not match the locked immutable source" in result["error"]
    assert manager.status()["state"] == "attached_model_locked"
    assert client.calls == []
    assert ownership.releases == 0


def test_automation_handoff_rejects_stale_lock_and_revision_identities(tmp_path):
    source = tmp_path / "immutable-source.mph"
    source.write_bytes(b"immutable source")
    models = [
        {
            "tag": "Model_1",
            "label": "Working",
            "file_path": "D:/models/working.mph",
            "unsaved": False,
        }
    ]
    manager, ownership, client = _manager(tmp_path, models=models)
    lock = _attach_saved_and_lock(manager, source, collaboration_mode="automation_exclusive")

    stale_lock = manager.prepare_attached_job_handoff(
        expected_lock_sha256="0" * 64,
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        source_model_path=str(source),
        user_confirmed_automation_exclusive=True,
    )
    stale_revision = manager.prepare_attached_job_handoff(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256="1" * 64,
        source_model_path=str(source),
        user_confirmed_automation_exclusive=True,
    )

    assert stale_lock["state"] == "handoff_precondition_failed"
    assert stale_lock["model_lock_verification"]["changed_fields"] == ["expected_lock_sha256"]
    assert stale_revision["state"] == "handoff_precondition_failed"
    assert stale_revision["model_lock_verification"]["changed_fields"] == [
        "expected_revision_sha256"
    ]
    assert manager.status()["state"] == "attached_model_locked"
    assert client.calls == []
    assert ownership.releases == 0


def test_automation_handoff_rejects_immediate_server_identity_change(tmp_path):
    source = tmp_path / "immutable-source.mph"
    source.write_bytes(b"immutable source")
    models = [
        {
            "tag": "Model_1",
            "label": "Working",
            "file_path": "D:/models/working.mph",
            "unsaved": False,
        }
    ]
    snapshots = [_snapshot() for _ in range(4)] + [_snapshot(server_created=999.0)]
    manager, ownership, client = _manager(tmp_path, models=models, snapshots=snapshots)
    lock = _attach_saved_and_lock(manager, source, collaboration_mode="automation_exclusive")

    result = manager.prepare_attached_job_handoff(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        source_model_path=str(source),
        user_confirmed_automation_exclusive=True,
    )

    assert result["state"] == "handoff_precondition_failed"
    assert result["model_lock_verification"]["changed_fields"] == ["attached_server"]
    assert manager.status()["state"] == "attached_model_locked"
    assert client.calls == []
    assert ownership.releases == 0
    assert manager._test_snapshot_provider.calls == len(snapshots)


@pytest.mark.parametrize("changed_part", ["model", "revision"])
def test_automation_handoff_rejects_immediate_model_or_revision_change(tmp_path, changed_part):
    source = tmp_path / "immutable-source.mph"
    source.write_bytes(b"immutable source")
    models = [
        {
            "tag": "Model_1",
            "label": "Working",
            "file_path": "D:/models/working.mph",
            "unsaved": False,
        }
    ]
    revision_state = {
        "structural": {"components": ["comp1"], "studies": ["std1"]},
        "state": {"parameters": {"gap": "10[nm]"}},
    }
    manager, ownership, client = _manager(tmp_path, models=models, revision_state=revision_state)
    lock = _attach_saved_and_lock(manager, source, collaboration_mode="automation_exclusive")
    if changed_part == "model":
        models[0] = {**models[0], "label": "Changed in Desktop"}
        expected = ["model_identity"]
    else:
        revision_state["state"] = {"parameters": {"gap": "11[nm]"}}
        expected = ["state_readback"]

    result = manager.prepare_attached_job_handoff(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        source_model_path=str(source),
        user_confirmed_automation_exclusive=True,
    )

    assert result["state"] == "handoff_precondition_failed"
    assert result["model_lock_verification"]["changed_fields"] == expected
    assert manager.status()["state"] == "attached_model_locked"
    assert client.calls == []
    assert ownership.releases == 0
    if changed_part == "model":
        assert manager._test_model_reader.calls >= 2
        assert manager._test_revision_reader.calls >= 1
    else:
        assert manager._test_model_reader.calls >= 1
        assert manager._test_revision_reader.calls >= 2


def test_automation_handoff_preserves_model_guard_when_disconnect_fails(tmp_path):
    class FailingDisconnect(FakeClient):
        def disconnect(self):
            self.calls.append("disconnect")
            raise RuntimeError("disconnect uncertain")

    source = tmp_path / "immutable-source.mph"
    source.write_bytes(b"immutable source")
    models = [
        {
            "tag": "Model_1",
            "label": "Working",
            "file_path": "D:/models/working.mph",
            "unsaved": False,
        }
    ]
    client = FailingDisconnect()
    manager, ownership, _client = _manager(tmp_path, models=models, client=client)
    lock = _attach_saved_and_lock(manager, source, collaboration_mode="automation_exclusive")

    result = manager.prepare_attached_job_handoff(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        source_model_path=str(source),
        user_confirmed_automation_exclusive=True,
    )

    assert result["state"] == "handoff_detach_failed"
    assert result["model_guard_preserved"] is True
    assert result["detach"]["model_guard_preserved"] is True
    assert manager.status()["model_lock"]["lock_sha256"] == lock["lock_sha256"]
    assert ownership.releases == 0


def test_model_lock_verify_detects_desktop_readback_change(tmp_path):
    revision_state = {
        "structural": {"components": ["comp1"], "studies": ["std1"]},
        "state": {"parameters": {"gap": "10[nm]"}},
    }
    manager, _ownership, _client = _manager(tmp_path, revision_state=revision_state)
    lock = _attach_and_lock(manager)
    revision_state["state"] = {"parameters": {"gap": "11[nm]"}}

    result = manager.verify_model_lock(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
    )

    assert result["success"] is False
    assert result["state"] == "model_guard_mismatch"
    assert result["changed_fields"] == ["state_readback"]


def test_model_lock_verify_detects_changed_model_identity(tmp_path):
    models = [{"tag": "Model_1", "label": "Shared", "file_path": None, "unsaved": True}]
    manager, _ownership, _client = _manager(tmp_path, models=models)
    lock = _attach_and_lock(manager)
    models[0] = {
        "tag": "Model_1",
        "label": "Changed in Desktop",
        "file_path": None,
        "unsaved": True,
    }

    result = manager.verify_model_lock(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
    )

    assert result["success"] is False
    assert result["changed_fields"] == ["model_identity"]


def test_model_lock_verify_detects_changed_server_identity(tmp_path):
    snapshots = [
        _snapshot(),
        _snapshot(),
        _snapshot(),
        _snapshot(),
        _snapshot(server_created=999.0),
    ]
    manager, _ownership, _client = _manager(tmp_path, snapshots=snapshots)
    lock = _attach_and_lock(manager)

    result = manager.verify_model_lock(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
    )

    assert result["success"] is False
    assert result["changed_fields"] == ["attached_server"]
    assert manager._test_snapshot_provider.calls == len(snapshots)


def test_model_lock_verify_rejects_stale_caller_identities(tmp_path):
    manager, _ownership, _client = _manager(tmp_path)
    _attach_and_lock(manager)

    result = manager.verify_model_lock(
        expected_lock_sha256="0" * 64,
        expected_revision_sha256="1" * 64,
    )

    assert result["success"] is False
    assert result["changed_fields"] == [
        "expected_lock_sha256",
        "expected_revision_sha256",
    ]


def test_unlock_requires_reason_and_leaves_bounded_audit(tmp_path):
    manager, _ownership, _client = _manager(tmp_path)
    lock = _attach_and_lock(manager)

    missing = manager.unlock_model(expected_lock_sha256=lock["lock_sha256"], reason="  ")
    unlocked = manager.unlock_model(
        expected_lock_sha256=lock["lock_sha256"], reason="Return control to Desktop"
    )

    assert missing == {"success": False, "state": "unlock_reason_required"}
    assert unlocked["success"] is True
    assert unlocked["unlock_audit"]["reason"] == "Return control to Desktop"
    assert len(unlocked["unlock_audit"]["audit_sha256"]) == 64
    assert manager.status()["last_unlock_audit"] == unlocked["unlock_audit"]


def test_detach_refuses_while_model_lock_is_active(tmp_path):
    manager, ownership, client = _manager(tmp_path)
    lock = _attach_and_lock(manager)

    result = manager.detach()

    assert result["success"] is False
    assert result["state"] == "model_lock_active"
    assert client.calls == []
    assert ownership.releases == 0
    assert (
        manager.unlock_model(expected_lock_sha256=lock["lock_sha256"], reason="Detach")["success"]
        is True
    )


def test_save_copy_snapshot_commits_manifest_after_identity_verification(tmp_path):
    manager, _ownership, _client = _manager(tmp_path)
    lock = _attach_and_lock(manager)

    result = manager.snapshot_model(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        max_snapshot_bytes=1024,
    )
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))

    assert result["success"] is True
    assert result["state"] == "snapshot_complete"
    assert result["identity_preserved"] is True
    assert manifest["complete"] is True
    assert manifest["save_copy_api"] == "Model.java.save(path, True)"
    assert manifest["snapshot"]["sha256"] == result["snapshot_sha256"]
    assert manifest["model"]["file_path"] is None


def test_native_path_only_writer_fails_before_any_snapshot_write(tmp_path):
    def unbounded_writer(_client, _tag, _target):
        pytest.fail("an unbounded native Save Copy must not be attempted")

    manager, _ownership, _client = _manager(
        tmp_path,
        snapshot_writer=unbounded_writer,
        bounded_snapshot_writer=False,
    )
    lock = _attach_and_lock(manager)

    result = manager.snapshot_model(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        max_snapshot_bytes=1024,
    )

    assert result == {
        "success": False,
        "state": "snapshot_write_bound_unavailable",
        "write_attempted": False,
        "required_capability": "incremental_native_write_byte_limit",
    }
    assert list(tmp_path.glob("Model_1-snapshot*")) == []


def test_snapshot_rehashes_and_preserves_declared_immutable_source(tmp_path):
    manager, _ownership, _client = _manager(tmp_path)
    assert (
        manager.attach(
            _request(),
            profile="core",
            environ={SHARED_SERVER_FEATURE_ENV: "true"},
        )["success"]
        is True
    )
    assert manager.adopt_model(_selector())["success"] is True
    source = tmp_path / "source.mph"
    source.write_bytes(b"immutable source bytes")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    locked = manager.lock_model(
        collaboration_mode="interactive_inspection",
        immutable_source={"path": str(source), "sha256": source_sha256},
    )["model_lock"]

    result = manager.snapshot_model(
        expected_lock_sha256=locked["lock_sha256"],
        expected_revision_sha256=locked["revision"]["revision_sha256"],
        max_snapshot_bytes=1024,
    )

    assert result["success"] is True
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_sha256


def test_snapshot_writer_failure_never_commits_complete_manifest(tmp_path):
    def partial_failure(_client, _tag, target):
        target.write_bytes(b"partial")
        raise RuntimeError("simulated Save Copy failure")

    manager, _ownership, _client = _manager(tmp_path, snapshot_writer=partial_failure)
    lock = _attach_and_lock(manager)

    result = manager.snapshot_model(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        max_snapshot_bytes=1024,
    )

    assert result["success"] is False
    assert result["state"] == "snapshot_incomplete"
    assert result["partial_snapshot_exists"] is False
    assert result["complete_manifest_exists"] is False
    assert result["artifacts_removed"] is True
    assert result["cleanup_errors"] == []


def test_snapshot_rejects_size_overrun_without_complete_manifest(tmp_path):
    manager, _ownership, _client = _manager(tmp_path)
    lock = _attach_and_lock(manager)

    result = manager.snapshot_model(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        max_snapshot_bytes=4,
    )

    assert result["success"] is False
    assert "byte limit" in result["error"]
    assert result["partial_snapshot_exists"] is False
    assert result["complete_manifest_exists"] is False
    assert result["artifacts_removed"] is True


def test_snapshot_manifest_failure_removes_snapshot_and_partial_manifest(tmp_path):
    def partial_manifest(path, _value):
        path.write_text("{", encoding="utf-8")
        raise OSError("simulated manifest publication failure")

    manager, _ownership, _client = _manager(tmp_path, manifest_writer=partial_manifest)
    lock = _attach_and_lock(manager)

    result = manager.snapshot_model(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        max_snapshot_bytes=1024,
    )

    assert result["success"] is False
    assert "manifest publication failure" in result["error"]
    assert result["partial_snapshot_exists"] is False
    assert result["complete_manifest_exists"] is False
    assert result["artifacts_removed"] is True
    assert list(tmp_path.glob("Model_1-snapshot*")) == []


def test_snapshot_cleanup_reports_non_oserror_without_skipping_later_cleanup(tmp_path, monkeypatch):
    def partial_manifest(path, _value):
        path.write_text("{", encoding="utf-8")
        raise OSError("simulated manifest publication failure")

    manager, _ownership, _client = _manager(tmp_path, manifest_writer=partial_manifest)
    lock = _attach_and_lock(manager)
    original_unlink = Path.unlink

    def selective_unlink(path, *args, **kwargs):
        if path.name.endswith(".manifest.json"):
            raise ValueError("simulated non-OSError cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", selective_unlink)

    result = manager.snapshot_model(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        max_snapshot_bytes=1024,
    )
    assert result["success"] is False
    assert any("ValueError" in error for error in result["cleanup_errors"])
    assert result["partial_snapshot_exists"] is False


@pytest.mark.parametrize("collision", ["snapshot", "manifest"])
def test_snapshot_collision_never_deletes_preexisting_artifact(tmp_path, collision):
    snapshot = tmp_path / "Model_1-snapshot.mph"
    manifest = tmp_path / "Model_1-snapshot.manifest.json"
    existing = snapshot if collision == "snapshot" else manifest
    existing.write_bytes(b"preexisting bytes")
    manager, _ownership, _client = _manager(tmp_path)
    lock = _attach_and_lock(manager)

    result = manager.snapshot_model(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        max_snapshot_bytes=1024,
    )

    assert result["success"] is False
    assert "already exists" in result["error"]
    assert existing.read_bytes() == b"preexisting bytes"
    assert result["partial_snapshot_exists"] is False
    assert result["complete_manifest_exists"] is False
    assert result["artifacts_removed"] is True


def test_snapshot_detects_model_identity_change_during_save_copy(tmp_path):
    models = [{"tag": "Model_1", "label": "Shared", "file_path": None, "unsaved": True}]

    def change_identity(_client, _tag, target):
        target.write_bytes(b"snapshot fixture")
        models[0] = {
            "tag": "Model_1",
            "label": "Changed during Save Copy",
            "file_path": None,
            "unsaved": True,
        }

    manager, _ownership, _client = _manager(
        tmp_path, models=models, snapshot_writer=change_identity
    )
    lock = _attach_and_lock(manager)

    result = manager.snapshot_model(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        max_snapshot_bytes=1024,
    )

    assert result["success"] is False
    assert "identity or revision changed" in result["error"]
    assert result["complete_manifest_exists"] is False


def test_attach_and_detach_preserve_server_listener_and_model_inventory(tmp_path):
    manager, ownership, client = _manager(tmp_path)

    attached = manager.attach(
        _request(),
        profile="core",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )
    status = manager.status()
    detached = manager.detach()

    assert attached["success"] is True
    assert attached["state"] == "attached_model_pending_adoption"
    assert attached["ownership"] == "external_user_owned_server"
    assert attached["can_start_comsol"] is False
    assert attached["post_connect"] == {
        "clientapi_raw_version": "6.4.0.293",
        "clientapi_comsol_version": "6.4.0.293",
        "accepted_release_line": "6.4.0.*",
        "server_identity_verified": True,
        "warnings": [],
    }
    assert status["attached"] is True
    assert detached["success"] is True
    assert detached["external_resources_preserved"] is True
    assert detached["violations"] == []
    assert client.calls == ["disconnect"]
    assert ownership.releases == 1
    assert not ownership.lease_path.exists()


def test_post_connect_accepts_final_build_difference_with_warning(tmp_path):
    manager, _ownership, _client = _manager(
        tmp_path, client_version="COMSOL Multiphysics 6.4.0.310"
    )

    result = manager.attach(
        _request(),
        profile="core",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )

    assert result["success"] is True
    assert result["post_connect"]["clientapi_comsol_version"] == "6.4.0.310"
    assert result["post_connect"]["warnings"] == ["same_accepted_release_line_build_difference"]


def test_post_connect_correlates_localized_clientapi_build_to_file_version(tmp_path):
    manager, _ownership, _client = _manager(
        tmp_path,
        client_version="COMSOL Multiphysics 6.4 (开发版本: 293)",
    )

    result = manager.attach(
        _request(),
        profile="core",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )

    assert result["success"] is True
    assert result["post_connect"] == {
        "clientapi_raw_version": "COMSOL Multiphysics 6.4 (开发版本: 293)",
        "clientapi_comsol_version": "6.4.0.293",
        "accepted_release_line": "6.4.0.*",
        "server_identity_verified": True,
        "warnings": [],
    }


def test_post_connect_rejects_other_release_and_releases_lease(tmp_path):
    manager, ownership, client = _manager(tmp_path, client_version="6.4.1.12")

    result = manager.attach(
        _request(),
        profile="core",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )

    assert result["success"] is False
    assert result["state"] == "attach_failed"
    assert "outside the accepted 6.4.0.* line" in result["error"]
    assert client.calls == ["disconnect"]
    assert ownership.releases == 1
    assert not ownership.lease_path.exists()


def test_post_connect_rejects_localized_build_mismatch(tmp_path):
    manager, ownership, client = _manager(
        tmp_path,
        client_version="COMSOL Multiphysics 6.4 (Build: 294)",
    )

    result = manager.attach(
        _request(),
        profile="core",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )

    assert result["success"] is False
    assert "outside the accepted 6.4.0.* line" in result["error"]
    assert client.calls == ["disconnect"]
    assert ownership.releases == 1


def test_post_connect_rejects_changed_server_identity_before_inventory(tmp_path):
    snapshots = [_snapshot(), _snapshot(), _snapshot(server_created=999.0)]
    manager, ownership, client = _manager(tmp_path, snapshots=snapshots)

    result = manager.attach(
        _request(),
        profile="core",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )

    assert result["success"] is False
    assert "identity changed after client connection" in result["error"]
    assert client.calls == ["disconnect"]
    assert ownership.releases == 1
    assert manager._test_snapshot_provider.calls == len(snapshots)


def test_client_construction_failure_releases_only_mcp_lease(tmp_path):
    def fail_client(host, port):
        raise RuntimeError("connection refused")

    manager, ownership, _client = _manager(tmp_path, client_factory=fail_client)

    result = manager.attach(
        _request(),
        profile="core",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )

    assert result["success"] is False
    assert result["state"] == "attach_failed"
    assert result["client_disconnected"] is True
    assert ownership.releases == 1
    assert not ownership.lease_path.exists()


def test_attach_inventory_and_disconnect_failures_retain_cleanup_handles(tmp_path):
    class ToggleDisconnect(FakeClient):
        fail = True

        def disconnect(self):
            self.calls.append("disconnect")
            if self.fail:
                raise RuntimeError("disconnect uncertain")
            self.disconnected = True

    client = ToggleDisconnect()
    oversized_inventory = [
        {
            "tag": f"Model_{index}",
            "label": f"Shared {index}",
            "file_path": None,
            "unsaved": True,
        }
        for index in range(33)
    ]
    manager, ownership, _client = _manager(tmp_path, client=client, models=oversized_inventory)

    attached = manager.attach(
        _request(),
        profile="core",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )

    assert attached["success"] is False
    assert attached["state"] == "attach_cleanup_pending"
    assert attached["client_disconnected"] is False
    assert manager.status()["state"] == "attach_cleanup_pending"
    assert ownership.lease_path.exists()

    client.fail = False
    cleaned = manager.detach()

    assert cleaned["success"] is True
    assert cleaned["attach_cleanup_completed"] is True
    assert client.calls == ["disconnect", "disconnect"]
    assert ownership.releases == 1
    assert manager.status()["state"] == "detached"


def test_attach_release_failure_without_client_retains_ownership_for_retry(tmp_path):
    class ToggleRelease(FakeOwnership):
        fail = True

        def release(self):
            self.releases += 1
            if self.fail:
                return {
                    "success": False,
                    "released": False,
                    "error": "release uncertain",
                }
            self.lease_path.unlink(missing_ok=True)
            return {"success": True, "released": True}

    def fail_client(_host, _port):
        raise RuntimeError("connection refused")

    ownership = ToggleRelease(tmp_path)
    manager, _ownership, _client = _manager(
        tmp_path, ownership=ownership, client_factory=fail_client
    )

    attached = manager.attach(
        _request(),
        profile="core",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )

    assert attached["state"] == "attach_cleanup_pending"
    assert manager.status()["state"] == "attach_cleanup_pending"
    assert manager.status()["ownership"] == "external_user_owned_server"
    assert manager.models()["state"] == "attach_cleanup_pending"
    assert ownership.lease_path.exists()

    duplicate = manager.attach(
        _request(),
        profile="core",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )
    assert duplicate["state"] == "cleanup_pending"
    assert ownership.releases == 1

    ownership.fail = False
    cleaned = manager.detach()

    assert cleaned["success"] is True
    assert cleaned["attach_cleanup_completed"] is True
    assert ownership.releases == 2
    assert manager.status()["state"] == "detached"


def test_detach_release_failure_retains_ownership_for_retry(tmp_path):
    class ToggleRelease(FakeOwnership):
        fail = True

        def release(self):
            self.releases += 1
            if self.fail:
                return {
                    "success": False,
                    "released": False,
                    "error": "release uncertain",
                }
            self.lease_path.unlink(missing_ok=True)
            return {"success": True, "released": True}

    ownership = ToggleRelease(tmp_path)
    manager, _ownership, client = _manager(tmp_path, ownership=ownership)
    assert (
        manager.attach(
            _request(),
            profile="core",
            environ={SHARED_SERVER_FEATURE_ENV: "true"},
        )["success"]
        is True
    )

    first = manager.detach()

    assert first["state"] == "detach_cleanup_pending"
    assert first["client_disconnected"] is True
    assert ownership.lease_path.exists()
    assert manager.status()["state"] == "detach_cleanup_pending"

    ownership.fail = False
    second = manager.detach()

    assert second["success"] is True
    assert client.calls == ["disconnect"]
    assert ownership.releases == 2
    assert manager.status()["state"] == "detached"


def test_zero_models_attach_for_inventory_then_reject_adoption_without_clear(tmp_path):
    manager, ownership, client = _manager(tmp_path, models=[])

    attached = manager.attach(
        _request(),
        profile="core",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )

    adoption = manager.adopt_model(_selector())
    result = manager.detach()

    assert attached["success"] is True
    assert attached["model_count"] == 0
    assert adoption["success"] is False
    assert adoption["state"] == "no_server_models"
    assert result["success"] is True
    assert client.calls == ["disconnect"]
    assert ownership.releases == 1


def test_disconnect_failure_keeps_lease_and_reports_uncertain(tmp_path):
    class FailingDisconnect(FakeClient):
        def disconnect(self):
            self.calls.append("disconnect")
            raise RuntimeError("disconnect uncertain")

    client = FailingDisconnect()
    manager, ownership, _ = _manager(tmp_path, client=client)
    attached = manager.attach(
        _request(),
        profile="core",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )
    assert manager.adopt_model(_selector())["success"] is True

    result = manager.detach()

    assert attached["success"] is True
    assert result["success"] is False
    assert result["state"] == "detach_uncertain"
    assert result["lease_released"] is False
    assert ownership.releases == 0
    assert ownership.lease_path.exists()


def test_changed_server_identity_after_disconnect_fails_preservation(tmp_path):
    snapshots = [
        _snapshot(),
        _snapshot(),
        _snapshot(),
        _snapshot(server_created=999.0),
    ]
    manager, ownership, client = _manager(tmp_path, snapshots=snapshots)
    assert (
        manager.attach(
            _request(),
            profile="core",
            environ={SHARED_SERVER_FEATURE_ENV: "true"},
        )["success"]
        is True
    )

    result = manager.detach()

    assert result["success"] is False
    assert "external_server_identity_changed" in result["violations"]
    assert client.calls == ["disconnect"]
    assert ownership.releases == 1


@pytest.mark.parametrize(
    ("after_mutation", "expected_violation"),
    [
        ("listener_missing", "external_server_identity_unavailable_after_detach"),
        ("inventory_incomplete", "external_server_identity_unavailable_after_detach"),
        ("model_inventory", "server_model_inventory_changed"),
    ],
)
def test_detach_independently_checks_preservation_boundaries(
    tmp_path, after_mutation, expected_violation
):
    after = _snapshot()
    models = _inventory()
    if after_mutation == "listener_missing":
        after["listeners"] = []
    elif after_mutation == "inventory_incomplete":
        after["inventory_complete"] = False
    snapshots = [_snapshot(), _snapshot(), _snapshot(), after]
    manager, ownership, client = _manager(tmp_path, models=models, snapshots=snapshots)
    assert (
        manager.attach(
            _request(),
            profile="core",
            environ={SHARED_SERVER_FEATURE_ENV: "true"},
        )["success"]
        is True
    )
    if after_mutation == "model_inventory":
        models[0] = {**models[0], "label": "Changed in Desktop"}

    result = manager.detach()

    assert result["success"] is False
    assert expected_violation in result["violations"]
    assert client.calls == ["disconnect"]
    assert ownership.releases == 1


def test_interrupted_attached_admission_recovers_completed_handoff(ascii_tmp_path, monkeypatch):
    """c081i F02: an exception after the REAL lifecycle detach leaves a
    pending-attached reservation; the retry reconciles the completed
    handoff receipt instead of repeating the (now impossible) handoff."""
    import os

    from src.jobs.manager import JobManager
    from src.jobs.store import process_identity
    from src.tools.jobs import _submit_job

    tmp_path = ascii_tmp_path
    source = tmp_path / "interrupted-source.mph"
    source.write_bytes(b"no vendor")
    models = [
        {
            "tag": "Model_1",
            "label": "Working",
            "file_path": "D:/models/working.mph",
            "unsaved": False,
        }
    ]
    session, _ownership, _client = _manager(tmp_path, models=models)
    lock = _attach_saved_and_lock(session, source, collaboration_mode="automation_exclusive")
    raw = {
        "job_type": "staged_sweep",
        "source_model_path": str(source),
        "parameter_name": "wl",
        "parameter_values": [1.0],
        "expressions": ["R"],
        "execution_backend": {
            "kind": "attached_shared_server",
            "expected_lock_sha256": lock["lock_sha256"],
            "expected_revision_sha256": lock["revision"]["revision_sha256"],
            "user_confirmed_automation_exclusive": True,
        },
    }
    manager = JobManager(
        tmp_path / "jobs", preflight=lambda **_kwargs: {"ready": True}, reconcile_on_start=False
    )
    identity = process_identity(os.getpid())
    launches = []
    monkeypatch.setattr(
        manager,
        "_launch_worker",
        lambda job_id, module: launches.append(job_id) or identity,
    )

    original = session.prepare_attached_job_handoff
    observed = []

    def interrupt_after_detach(**kwargs):
        result = original(**kwargs)
        observed.append(result.get("state"))
        if result.get("success"):
            raise RuntimeError("injected interruption after actual detach")
        return result

    monkeypatch.setattr(session, "prepare_attached_job_handoff", interrupt_after_detach)

    def submit():
        return _submit_job(
            dict(raw),
            profile_name="wave_optics",
            shared_enabled=True,
            manager=manager,
            session_manager=session,
            request_id="interrupted-1",
        )

    with pytest.raises(RuntimeError, match="injected interruption"):
        submit()

    binding = manager._read_request_record("interrupted-1")
    assert binding["spec_fingerprint"] == "pending-attached"
    assert "attached_handoff" not in binding

    retry = submit()

    assert retry["success"] is True
    assert retry["job_id"] == binding["job_id"]
    assert retry["attached_handoff"]["state"] == "attached_job_handoff_ready"
    assert retry["attached_handoff"]["detach_state"] == "detached"
    # The lifecycle was entered exactly once: the retry reconciled the
    # completed handoff receipt and never repeated the detach.
    assert observed == ["attached_job_handoff_ready"]
    assert len(launches) == 1
    # A third identical submit now replays the published binding without
    # re-entering the lifecycle or launching anything.
    replay = submit()
    assert replay["success"] is True
    assert replay["duplicate"] is True
    assert observed == ["attached_job_handoff_ready"]
    assert len(launches) == 1


def test_interrupted_attached_admission_recovers_across_process_replacement(
    ascii_tmp_path, monkeypatch
):
    """c081j F02: a fresh session and manager reconstructed over retained
    storage recover an admission whose lifecycle detach completed before
    the flow could durably record it - the completed-handoff receipt is
    identity-bound durable evidence, not process memory."""
    import os

    from src.jobs.manager import JobManager
    from src.jobs.store import process_identity
    from src.tools.jobs import _submit_job

    tmp_path = ascii_tmp_path
    source = tmp_path / "restarted-source.mph"
    source.write_bytes(b"no vendor")
    models = [
        {
            "tag": "Model_1",
            "label": "Working",
            "file_path": "D:/models/working.mph",
            "unsaved": False,
        }
    ]
    session, _ownership, _client = _manager(tmp_path, models=models)
    lock = _attach_saved_and_lock(session, source, collaboration_mode="automation_exclusive")
    raw = {
        "job_type": "staged_sweep",
        "source_model_path": str(source),
        "parameter_name": "wl",
        "parameter_values": [1.0],
        "expressions": ["R"],
        "execution_backend": {
            "kind": "attached_shared_server",
            "expected_lock_sha256": lock["lock_sha256"],
            "expected_revision_sha256": lock["revision"]["revision_sha256"],
            "user_confirmed_automation_exclusive": True,
        },
    }

    def build_manager():
        manager = JobManager(
            tmp_path / "jobs", preflight=lambda **_k: {"ready": True}, reconcile_on_start=False
        )
        identity = process_identity(os.getpid())
        monkeypatch.setattr(
            manager,
            "_launch_worker",
            lambda job_id, module: launched_job_ids.append(job_id) or identity,
        )
        return manager

    launched_job_ids = []
    manager = build_manager()

    original = session.prepare_attached_job_handoff
    observed = []

    def interrupt_after_detach(**kwargs):
        result = original(**kwargs)
        observed.append(result.get("state"))
        if result.get("success"):
            raise RuntimeError("injected interruption after actual detach")
        return result

    monkeypatch.setattr(session, "prepare_attached_job_handoff", interrupt_after_detach)

    def submit(current_session, current_manager):
        return _submit_job(
            dict(raw),
            profile_name="wave_optics",
            shared_enabled=True,
            manager=current_manager,
            session_manager=current_session,
            request_id="restarted-1",
        )

    with pytest.raises(RuntimeError, match="injected interruption"):
        submit(session, manager)

    binding = manager._read_request_record("restarted-1")
    assert binding["spec_fingerprint"] == "pending-attached"

    # Process replacement: every in-memory structure is gone; only retained
    # storage (request binding + identity-bound durable receipt) survives.
    fresh_session, _ownership2, _client2 = _manager(tmp_path, models=models)
    fresh_entries = []
    monkeypatch.setattr(
        fresh_session,
        "prepare_attached_job_handoff",
        lambda **kwargs: fresh_entries.append("entered") or {"success": False},
    )
    fresh_manager = build_manager()

    retry = submit(fresh_session, fresh_manager)

    assert retry["success"] is True
    assert retry["job_id"] == binding["job_id"]
    assert retry["attached_handoff"]["state"] == "attached_job_handoff_ready"
    assert retry["attached_handoff"]["detach_state"] == "detached"
    # The reconstructed lifecycle was never entered: the durable receipt
    # reconciled the completed handoff and the admission completed under
    # the reserved id with exactly one launch in total.
    assert fresh_entries == []
    assert observed == ["attached_job_handoff_ready"]
    assert launched_job_ids == [binding["job_id"]]
