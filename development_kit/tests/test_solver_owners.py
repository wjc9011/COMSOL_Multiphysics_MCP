"""Bounded parallel solver-owner registry tests.

These tests are solver-free: the registry is a pure admission authority that
holds no process handles and starts nothing. Every rule the plan freezes — two
owners at most, one core each, no third owner, no cross-owner access, and a
verified-absence release — is therefore provable here.

The refusals carry the value. A third owner admitted, a multi-core request
silently reduced, or a slot freed while its solver still ran would each be a
resource-safety defect that a happy-path test cannot see.
"""

from __future__ import annotations

import pytest

from comsol_mcp.shared_session.solver_owners import (
    MAX_REQUESTED_CORES,
    MAX_SOLVER_OWNERS,
    OWNER_CORES,
    OwnerAdmissionError,
    SolverOwnerRegistry,
    capability_document,
    normalize_owner_identity,
    normalize_requested_cores,
    owner_receipt,
)

LEASE_A = "a" * 64
LEASE_B = "b" * 64
LEASE_C = "c" * 64


def _register(
    registry: SolverOwnerRegistry,
    owner: str,
    *,
    cores: int = OWNER_CORES,
    process_id: int = 1000,
    process_create_time: float = 111.0,
    lease_sha256: str = LEASE_A,
    session_id: str = "session-1",
) -> object:
    return registry.register(
        owner=owner,
        cores=cores,
        process_id=process_id,
        process_create_time=process_create_time,
        lease_sha256=lease_sha256,
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Core policy: one core each, never silently reduced
# ---------------------------------------------------------------------------


def test_the_declared_limits_are_two_owners_at_one_core() -> None:
    assert MAX_SOLVER_OWNERS == 2
    assert OWNER_CORES == 1
    assert MAX_REQUESTED_CORES == 1


@pytest.mark.parametrize("value", [1])
def test_one_core_per_owner_is_admitted(value: int) -> None:
    assert normalize_requested_cores(value) == 1


@pytest.mark.parametrize("value", [2, 3, 8, 64])
def test_a_multi_core_request_is_refused_rather_than_reduced(value: int) -> None:
    """A silent reduction would misreport the resources actually used."""
    with pytest.raises(OwnerAdmissionError) as excinfo:
        normalize_requested_cores(value)
    assert excinfo.value.reason_code == "multi_core_refused"
    assert "not reduced silently" in str(excinfo.value)


@pytest.mark.parametrize("value", [0, -1, None, "1", 1.0, True])
def test_an_invalid_core_count_is_refused(value: object) -> None:
    with pytest.raises(OwnerAdmissionError) as excinfo:
        normalize_requested_cores(value)
    assert excinfo.value.reason_code == "invalid_cores"


def test_a_multi_core_registration_is_refused() -> None:
    registry = SolverOwnerRegistry()
    with pytest.raises(OwnerAdmissionError) as excinfo:
        _register(registry, "owner-a", cores=2)
    assert excinfo.value.reason_code == "multi_core_refused"
    assert registry.owner_count() == 0


# ---------------------------------------------------------------------------
# Two owners, no third
# ---------------------------------------------------------------------------


def test_two_independent_owners_may_run_in_parallel() -> None:
    registry = SolverOwnerRegistry()
    first = _register(registry, "owner-a", process_id=1000, lease_sha256=LEASE_A)
    second = _register(
        registry,
        "owner-b",
        process_id=2000,
        process_create_time=222.0,
        lease_sha256=LEASE_B,
        session_id="session-2",
    )
    assert first.cores == second.cores == 1
    assert registry.registered_owners() == ("owner-a", "owner-b")
    assert registry.remaining_slots() == 0


def test_a_third_owner_is_refused() -> None:
    registry = SolverOwnerRegistry()
    _register(registry, "owner-a", process_id=1000)
    _register(registry, "owner-b", process_id=2000, process_create_time=222.0)
    with pytest.raises(OwnerAdmissionError) as excinfo:
        _register(registry, "owner-c", process_id=3000, process_create_time=333.0)
    assert excinfo.value.reason_code == "owner_limit_reached"
    assert registry.owner_count() == 2
    assert "at most 2" in str(excinfo.value)


def test_the_registry_cannot_be_constructed_above_the_declared_ceiling() -> None:
    with pytest.raises(OwnerAdmissionError) as excinfo:
        SolverOwnerRegistry(maximum_owners=3)
    assert excinfo.value.reason_code == "invalid_limit"


def test_a_reduced_limit_is_permitted_but_never_raised() -> None:
    registry = SolverOwnerRegistry(maximum_owners=1)
    _register(registry, "owner-a", process_id=1000)
    with pytest.raises(OwnerAdmissionError):
        _register(registry, "owner-b", process_id=2000)


def test_the_verdict_is_pure_and_does_not_register() -> None:
    """A verdict must be recordable before any state changes."""
    registry = SolverOwnerRegistry()
    verdict = registry.evaluate_registration(
        owner="owner-a",
        cores=1,
        process_id=1000,
        process_create_time=111.0,
        lease_sha256=LEASE_A,
        session_id="session-1",
    )
    assert verdict["admitted"] is True
    assert verdict["reason"] == "admitted"
    assert verdict["owner_count"] == 1
    assert registry.owner_count() == 0


def test_the_refusal_verdict_names_the_reason_without_mutating() -> None:
    registry = SolverOwnerRegistry()
    _register(registry, "owner-a", process_id=1000)
    _register(registry, "owner-b", process_id=2000, process_create_time=222.0)
    verdict = registry.evaluate_registration(
        owner="owner-c",
        cores=1,
        process_id=3000,
        process_create_time=333.0,
        lease_sha256=LEASE_C,
        session_id="session-3",
    )
    assert verdict["admitted"] is False
    assert verdict["reason"] == "owner_limit_reached"
    assert registry.owner_count() == 2


# ---------------------------------------------------------------------------
# Idempotency and identity conflicts
# ---------------------------------------------------------------------------


def test_re_registering_the_same_owner_with_the_same_identity_is_idempotent() -> None:
    registry = SolverOwnerRegistry()
    first = _register(registry, "owner-a", process_id=1000)
    again = _register(registry, "owner-a", process_id=1000)
    assert first == again
    assert registry.owner_count() == 1


@pytest.mark.parametrize(
    "changed",
    [
        {"process_id": 9999},
        {"process_create_time": 999.0},
        {"lease_sha256": LEASE_B},
        {"session_id": "other-session"},
    ],
)
def test_a_label_reused_with_a_different_identity_is_refused(changed: dict[str, object]) -> None:
    """A recycled label must not silently inherit a live owner's slot."""
    registry = SolverOwnerRegistry()
    _register(registry, "owner-a", process_id=1000)
    kwargs: dict[str, object] = {
        "owner": "owner-a",
        "cores": 1,
        "process_id": 1000,
        "process_create_time": 111.0,
        "lease_sha256": LEASE_A,
        "session_id": "session-1",
        **changed,
    }
    with pytest.raises(OwnerAdmissionError) as excinfo:
        registry.register(**kwargs)  # type: ignore[arg-type]
    assert excinfo.value.reason_code == "owner_identity_conflict"
    # The original registration is untouched.
    assert registry.record("owner-a").process_id == 1000  # type: ignore[union-attr]


def test_a_reused_label_does_not_consume_a_second_slot() -> None:
    registry = SolverOwnerRegistry()
    _register(registry, "owner-a", process_id=1000)
    _register(registry, "owner-a", process_id=1000)
    assert registry.remaining_slots() == 1


# ---------------------------------------------------------------------------
# Process identity is a pair, not a pid
# ---------------------------------------------------------------------------


def test_process_identity_requires_pid_and_create_time() -> None:
    """A recycled pid alone must not identify an owner."""
    identity = normalize_owner_identity(
        owner="owner-a",
        process_id=1234,
        process_create_time=555.5,
        lease_sha256=LEASE_A,
        session_id="session-1",
    )
    assert identity["process_id"] == 1234
    assert identity["process_create_time"] == 555.5


@pytest.mark.parametrize(
    "kwargs",
    [
        {"process_id": 0},
        {"process_id": -5},
        {"process_id": None},
        {"process_id": "1234"},
        {"process_id": True},
        {"process_create_time": 0},
        {"process_create_time": -1.0},
        {"process_create_time": float("nan")},
        {"process_create_time": float("inf")},
        {"process_create_time": float("-inf")},
        {"process_create_time": 10**1000},
        {"process_create_time": "now"},
        {"process_create_time": None},
        {"lease_sha256": "short"},
        {"lease_sha256": "z" * 64},
        {"session_id": ""},
        {"owner": ""},
        {"owner": None},
    ],
)
def test_malformed_identity_evidence_is_refused(kwargs: dict[str, object]) -> None:
    base: dict[str, object] = {
        "owner": "owner-a",
        "process_id": 1234,
        "process_create_time": 555.5,
        "lease_sha256": LEASE_A,
        "session_id": "session-1",
        **kwargs,
    }
    with pytest.raises(OwnerAdmissionError) as excinfo:
        normalize_owner_identity(**base)  # type: ignore[arg-type]
    assert excinfo.value.reason_code in {"invalid_identity", "invalid_process_identity"}


# ---------------------------------------------------------------------------
# Release requires verified absence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["false", 1, [], None])
@pytest.mark.parametrize("field", ["observed_process_absent", "observed_lease_absent"])
def test_release_requires_boolean_absence_evidence(field, value) -> None:
    registry = SolverOwnerRegistry()
    _register(registry, "owner-a", process_id=1000)
    arguments = dict(
        owner="owner-a",
        process_id=1000,
        process_create_time=111.0,
        observed_process_absent=True,
        observed_lease_absent=True,
    )
    arguments[field] = value
    verdict = registry.release(**arguments)
    assert verdict["released"] is False
    assert registry.owner_count() == 1


@pytest.mark.parametrize("owner", [None, "", [], "x" * 257])
def test_release_returns_refusal_for_invalid_owner(owner) -> None:
    registry = SolverOwnerRegistry()
    _register(registry, "owner-a", process_id=1000)
    verdict = registry.release(
        owner=owner,
        process_id=1000,
        process_create_time=111.0,
        observed_process_absent=True,
        observed_lease_absent=True,
    )
    assert verdict["released"] is False
    assert verdict["reason"] == "invalid_identity"
    assert registry.owner_count() == 1


def test_a_verified_absent_owner_releases_its_slot() -> None:
    registry = SolverOwnerRegistry()
    _register(registry, "owner-a", process_id=1000)
    verdict = registry.release(
        owner="owner-a",
        process_id=1000,
        process_create_time=111.0,
        observed_process_absent=True,
        observed_lease_absent=True,
    )
    assert verdict["released"] is True
    assert verdict["remaining_slots"] == 2
    assert registry.owner_count() == 0


@pytest.mark.parametrize(
    ("process_absent", "lease_absent", "reason"),
    [
        (True, False, "lease_still_present"),
        (False, True, "process_still_present"),
        (False, False, "process_still_present"),
    ],
)
def test_release_is_refused_while_anything_is_still_present(
    process_absent: bool, lease_absent: bool, reason: str
) -> None:
    """Freeing a slot while its solver still runs would break the ceiling."""
    registry = SolverOwnerRegistry()
    _register(registry, "owner-a", process_id=1000)
    verdict = registry.release(
        owner="owner-a",
        process_id=1000,
        process_create_time=111.0,
        observed_process_absent=process_absent,
        observed_lease_absent=lease_absent,
    )
    assert verdict["released"] is False
    assert verdict["reason"] == reason
    assert registry.owner_count() == 1


def test_a_different_process_cannot_release_this_owners_slot() -> None:
    registry = SolverOwnerRegistry()
    _register(registry, "owner-a", process_id=1000)
    verdict = registry.release(
        owner="owner-a",
        process_id=9999,
        process_create_time=111.0,
        observed_process_absent=True,
        observed_lease_absent=True,
    )
    assert verdict["released"] is False
    assert verdict["reason"] == "process_identity_mismatch"
    assert registry.owner_count() == 1


def test_a_recycled_pid_cannot_release_a_slot() -> None:
    """Same pid, different creation time: a different process."""
    registry = SolverOwnerRegistry()
    _register(registry, "owner-a", process_id=1000, process_create_time=111.0)
    verdict = registry.release(
        owner="owner-a",
        process_id=1000,
        process_create_time=999.0,
        observed_process_absent=True,
        observed_lease_absent=True,
    )
    assert verdict["released"] is False
    assert verdict["reason"] == "process_identity_mismatch"


def test_releasing_an_unregistered_owner_is_refused() -> None:
    registry = SolverOwnerRegistry()
    verdict = registry.release(
        owner="owner-z",
        process_id=1000,
        process_create_time=111.0,
        observed_process_absent=True,
        observed_lease_absent=True,
    )
    assert verdict["released"] is False
    assert verdict["reason"] == "owner_not_registered"


def test_a_released_slot_can_be_reused() -> None:
    registry = SolverOwnerRegistry()
    _register(registry, "owner-a", process_id=1000)
    _register(registry, "owner-b", process_id=2000, process_create_time=222.0)
    registry.release(
        owner="owner-a",
        process_id=1000,
        process_create_time=111.0,
        observed_process_absent=True,
        observed_lease_absent=True,
    )
    _register(registry, "owner-c", process_id=3000, process_create_time=333.0, lease_sha256=LEASE_C)
    assert registry.registered_owners() == ("owner-b", "owner-c")


# ---------------------------------------------------------------------------
# Isolation: no cross-owner access, and no enumeration oracle
# ---------------------------------------------------------------------------


def test_an_owner_may_address_its_own_resource() -> None:
    registry = SolverOwnerRegistry()
    verdict = registry.evaluate_resource_access(owner="owner-a", resource_owner="owner-a")
    assert verdict["allowed"] is True
    assert verdict["reason"] == "same_owner"


def test_cross_owner_access_is_refused_indistinguishably_from_absence() -> None:
    """The refusal must not become an oracle for another owner's resources."""
    registry = SolverOwnerRegistry()
    _register(registry, "owner-a", process_id=1000)
    _register(registry, "owner-b", process_id=2000, process_create_time=222.0)

    cross = registry.evaluate_resource_access(owner="owner-b", resource_owner="owner-a")
    absent = registry.evaluate_resource_access(owner="owner-b", resource_owner="owner-ghost")
    assert cross["allowed"] is False
    assert cross["reason"] == "not_found"
    # No field distinguishes a held resource from an absent one, so the caller
    # cannot use this reply to enumerate another owner's work.
    assert cross == absent
    assert "cross_owner_access" not in cross


def test_the_cross_owner_refusal_is_byte_identical_to_the_absent_case() -> None:
    """Having a resource and not having it must be indistinguishable replies."""
    registry = SolverOwnerRegistry()
    _register(registry, "owner-a", process_id=1000)
    cross = registry.evaluate_model_access(
        owner="owner-b", model_owner="owner-a", model_tag="comp1"
    )
    absent = registry.evaluate_model_access(
        owner="owner-b", model_owner="owner-ghost", model_tag="comp1"
    )
    assert cross == absent


def test_model_access_follows_the_same_isolation_rule() -> None:
    registry = SolverOwnerRegistry()
    same = registry.evaluate_model_access(owner="owner-a", model_owner="owner-a", model_tag="comp1")
    cross = registry.evaluate_model_access(
        owner="owner-b", model_owner="owner-a", model_tag="comp1"
    )
    assert same["allowed"] is True and same["model_tag"] == "comp1"
    assert cross["allowed"] is False and cross["reported_as"] == "not_found"


def test_a_malformed_isolation_request_is_refused() -> None:
    registry = SolverOwnerRegistry()
    with pytest.raises(OwnerAdmissionError):
        registry.evaluate_resource_access(owner="", resource_owner="owner-a")
    with pytest.raises(OwnerAdmissionError):
        registry.evaluate_model_access(owner="owner-a", model_owner="owner-a", model_tag="")


# ---------------------------------------------------------------------------
# Receipts and capability metadata
# ---------------------------------------------------------------------------


def test_a_receipt_states_that_a_release_is_not_cleanup_evidence() -> None:
    """A slot release is not proof that a process was cleaned up."""
    registry = SolverOwnerRegistry()
    record = _register(registry, "owner-a", process_id=1000)
    verdict = registry.release(
        owner="owner-a",
        process_id=1000,
        process_create_time=111.0,
        observed_process_absent=True,
        observed_lease_absent=True,
    )
    receipt = owner_receipt(owner="owner-a", action="release", verdict=verdict, record=record)
    assert receipt["release_is_cleanup_evidence"] is False
    assert receipt["cleanup"] == {"state": "unknown"}
    assert receipt["receipt_sha256"]


def test_a_receipt_records_the_exact_identity_evidence() -> None:
    registry = SolverOwnerRegistry()
    record = _register(registry, "owner-a", process_id=1000)
    verdict = registry.evaluate_registration(
        owner="owner-a",
        cores=1,
        process_id=1000,
        process_create_time=111.0,
        lease_sha256=LEASE_A,
        session_id="session-1",
    )
    receipt = owner_receipt(owner="owner-a", action="register", verdict=verdict, record=record)
    assert receipt["process_id"] == 1000
    assert receipt["process_create_time"] == 111.0
    assert receipt["lease_sha256"] == LEASE_A
    assert receipt["cores"] == 1


def test_a_refusal_receipt_is_produced_without_a_record() -> None:
    """Every refusal is receiptable, including a malformed one."""
    registry = SolverOwnerRegistry()
    verdict = registry.evaluate_registration(
        owner="owner-a",
        cores=2,
        process_id=1000,
        process_create_time=111.0,
        lease_sha256=LEASE_A,
        session_id="session-1",
    )
    assert verdict["admitted"] is False
    assert verdict["reason"] == "multi_core_refused"
    receipt = owner_receipt(owner="owner-a", action="register", verdict=verdict)
    assert receipt["admitted"] is False
    assert receipt["receipt_sha256"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"owner": ""},
        {"process_id": 0},
        {"lease_sha256": "short"},
        {"cores": 3},
        {"cores": None},
    ],
)
def test_a_malformed_registration_yields_a_refusal_verdict(kwargs: dict[str, object]) -> None:
    """A malformed request is a refusal, not an unrecordable exception."""
    registry = SolverOwnerRegistry()
    base: dict[str, object] = {
        "owner": "owner-a",
        "cores": 1,
        "process_id": 1000,
        "process_create_time": 111.0,
        "lease_sha256": LEASE_A,
        "session_id": "session-1",
        **kwargs,
    }
    verdict = registry.evaluate_registration(**base)  # type: ignore[arg-type]
    assert verdict["admitted"] is False
    assert isinstance(verdict["reason"], str) and verdict["reason"]
    assert registry.owner_count() == 0


def test_register_raises_where_the_verdict_refuses() -> None:
    """A caller that asked to register cannot ignore the outcome."""
    registry = SolverOwnerRegistry()
    with pytest.raises(OwnerAdmissionError):
        _register(registry, "")
    assert registry.owner_count() == 0


def test_the_capability_document_states_every_frozen_limit() -> None:
    document = capability_document()
    assert document["maximum_independent_solver_owners"] == 2
    assert document["cores_per_owner"] == 1
    assert document["multi_core_solver_permitted"] is False
    assert document["third_owner_permitted"] is False
    assert document["cross_owner_model_access_permitted"] is False
    assert document["release_requires_verified_absence"] is True
    assert document["currently_registered"] == 0


def test_the_capability_document_reports_live_occupancy() -> None:
    registry = SolverOwnerRegistry()
    _register(registry, "owner-a", process_id=1000)
    assert capability_document(registry=registry)["currently_registered"] == 1


def test_a_record_round_trips_through_its_document() -> None:
    from comsol_mcp.shared_session.solver_owners import OwnerRecord

    registry = SolverOwnerRegistry()
    record = _register(registry, "owner-a", process_id=1000)
    document = record.to_document()
    assert document["schema_name"] == "comsol_mcp.solver_owner_registry"
    assert OwnerRecord.from_document(document) == record
