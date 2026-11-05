"""Bounded parallel solver-owner registry for shared local sessions.

Why this exists
---------------

The shared-session plan permits at most two independent COMSOL solver owners to
run in parallel, each fixed to one core, so a caller can view and work on
multiple models without serializing everything. It also forbids three things
that a naive "just allow another client" implementation would permit: a third
owner, multi-core solver use, and cross-owner model access.

This module is the admission authority for those three rules. It is a pure
registry: it holds no process handles, starts nothing, and terminates nothing.
That separation is deliberate — a policy decision must be testable without a
solver, and a bug in admission must not be able to kill someone else's process.

Frozen rules
------------

* **At most two owners**, and each occupies exactly one core. A request for two
  cores is refused rather than silently reduced, because silently reducing a
  caller's declared resource policy would misrepresent what was run.
* **One core per owner is the ceiling, not a default.** The registry has no
  multi-core mode.
* **No cross-owner access.** A model, task, or job owned by one owner is
  invisible to another; the refusal is indistinguishable from "not found" so the
  registry cannot be used to enumerate another owner's work.
* **Admission is per owner, not per request.** A registered owner holds its slot
  until it is explicitly released with a verified release, so a client cannot
  multiply its concurrency by opening more sessions.
* **Verdicts are pure.** Nothing here observes the machine; the caller supplies
  the facts, and the registry states the decision and its reason.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from comsol_mcp.durable import canonical_sha256_v1

SCHEMA_NAME = "comsol_mcp.solver_owner_registry"
SCHEMA_VERSION = "1.0.0"
RECEIPT_SCHEMA_NAME = "comsol_mcp.solver_owner_receipt"
RECEIPT_SCHEMA_VERSION = "1.0.0"

#: The hard ceiling on concurrently registered independent solver owners.
MAX_SOLVER_OWNERS = 2

#: The only permitted per-owner core allocation. One owner, one core.
OWNER_CORES = 1

#: Hard ceiling on any single request's declared core count.
MAX_REQUESTED_CORES = OWNER_CORES


class OwnerAdmissionError(RuntimeError):
    """One stable, bounded refusal with a machine-readable reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class OwnerRecord:
    """One registered solver owner and the evidence that identifies it."""

    owner: str
    cores: int
    process_id: int
    process_create_time: float
    lease_sha256: str
    session_id: str
    registered_at_ms: int

    def to_document(self) -> dict[str, Any]:
        # The record carries its own schema identity so a journal read later
        # does not depend on this module's current constants.
        return {
            "schema_name": SCHEMA_NAME,
            "schema_version": SCHEMA_VERSION,
            "owner": self.owner,
            "cores": self.cores,
            "process_id": self.process_id,
            "process_create_time": self.process_create_time,
            "lease_sha256": self.lease_sha256,
            "session_id": self.session_id,
            "registered_at_ms": self.registered_at_ms,
        }

    @staticmethod
    def from_document(document: Mapping[str, Any]) -> OwnerRecord:
        return OwnerRecord(
            owner=str(document["owner"]),
            cores=int(document["cores"]),
            process_id=int(document["process_id"]),
            process_create_time=float(document["process_create_time"]),
            lease_sha256=str(document["lease_sha256"]),
            session_id=str(document["session_id"]),
            registered_at_ms=int(document["registered_at_ms"]),
        )


def normalize_requested_cores(value: object) -> int:
    """Admit exactly one core per owner, refusing anything else.

    A silent reduction would misreport the resources actually used, so a larger
    request is refused with an accurate reason instead.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise OwnerAdmissionError("invalid_cores", "cores must be an integer")
    if value < 1:
        raise OwnerAdmissionError("invalid_cores", "cores must be at least 1")
    if value > MAX_REQUESTED_CORES:
        raise OwnerAdmissionError(
            "multi_core_refused",
            f"each owner may use at most {MAX_REQUESTED_CORES} core; "
            f"{value} requested and not reduced silently",
        )
    return value


def _bounded_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OwnerAdmissionError("invalid_identity", f"{label} must be a nonempty string")
    if len(value) > 256:
        raise OwnerAdmissionError("invalid_identity", f"{label} is out of range")
    return value.strip()


def normalize_owner_identity(
    *,
    owner: object,
    process_id: object,
    process_create_time: object,
    lease_sha256: object,
    session_id: object,
) -> dict[str, Any]:
    """Validate the exact evidence one owner is registered against.

    Process identity is the pair ``(pid, create_time)``, not the pid alone: a
    recycled pid would otherwise let a dead owner's slot be inherited by an
    unrelated process.
    """
    normalized_owner = _bounded_identifier(owner, "owner")
    if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
        raise OwnerAdmissionError(
            "invalid_process_identity", "process_id must be a positive integer"
        )
    if isinstance(process_create_time, bool) or not isinstance(process_create_time, (int, float)):
        raise OwnerAdmissionError("invalid_process_identity", "process_create_time must be numeric")
    try:
        normalized_create_time = float(process_create_time)
    except OverflowError as exc:
        raise OwnerAdmissionError(
            "invalid_process_identity", "process_create_time exceeds the finite numeric range"
        ) from exc
    if not math.isfinite(normalized_create_time) or normalized_create_time <= 0:
        raise OwnerAdmissionError(
            "invalid_process_identity", "process_create_time must be finite and positive"
        )
    normalized_lease = _bounded_identifier(lease_sha256, "lease_sha256")
    if len(normalized_lease) != 64 or any(
        c not in "0123456789abcdef" for c in normalized_lease.lower()
    ):
        raise OwnerAdmissionError(
            "invalid_process_identity", "lease_sha256 must be 64 hexadecimal digits"
        )
    return {
        "owner": normalized_owner,
        "process_id": process_id,
        "process_create_time": normalized_create_time,
        "lease_sha256": normalized_lease.lower(),
        "session_id": _bounded_identifier(session_id, "session_id"),
    }


class SolverOwnerRegistry:
    """In-memory admission registry for at most two one-core solver owners.

    The registry is intentionally not persisted: it describes *live* owners, and
    a live owner is exactly what a durable file cannot represent. Durable
    evidence for an operation lives in the job and receipt journals instead.
    """

    def __init__(self, *, maximum_owners: int = MAX_SOLVER_OWNERS) -> None:
        if isinstance(maximum_owners, bool) or not isinstance(maximum_owners, int):
            raise OwnerAdmissionError("invalid_limit", "maximum_owners must be an integer")
        if not 1 <= maximum_owners <= MAX_SOLVER_OWNERS:
            raise OwnerAdmissionError(
                "invalid_limit",
                f"maximum_owners must be within [1, {MAX_SOLVER_OWNERS}]",
            )
        self._maximum_owners = maximum_owners
        self._owners: dict[str, OwnerRecord] = {}

    @property
    def maximum_owners(self) -> int:
        return self._maximum_owners

    def registered_owners(self) -> tuple[str, ...]:
        return tuple(sorted(self._owners))

    def record(self, owner: str) -> OwnerRecord | None:
        return self._owners.get(owner)

    def owner_count(self) -> int:
        return len(self._owners)

    def remaining_slots(self) -> int:
        return self._maximum_owners - len(self._owners)

    # -- admission ---------------------------------------------------------

    def evaluate_registration(
        self,
        *,
        owner: object,
        cores: object,
        process_id: object,
        process_create_time: object,
        lease_sha256: object,
        session_id: object,
    ) -> dict[str, Any]:
        """Return a pure admission verdict without mutating the registry.

        Separating the verdict from the mutation lets a caller record the
        decision, including a refusal reason, before any state changes.

        A malformed request yields a *refusal verdict* rather than an exception,
        so every refusal is receiptable. Only :meth:`register` raises, because a
        caller that asked to register must not be able to ignore the outcome.
        """
        try:
            identity = normalize_owner_identity(
                owner=owner,
                process_id=process_id,
                process_create_time=process_create_time,
                lease_sha256=lease_sha256,
                session_id=session_id,
            )
            admitted_cores = normalize_requested_cores(cores)
        except OwnerAdmissionError as exc:
            return {
                "admitted": False,
                "reason": exc.reason_code,
                "cores": cores if isinstance(cores, int) and not isinstance(cores, bool) else None,
                "identity": None,
                "owner_count": self.owner_count(),
            }

        existing = self._owners.get(identity["owner"])

        if existing is not None:
            # Re-registering the same owner is idempotent only when every
            # identifying fact matches. A changed fact is a different owner
            # reusing a label, which must not silently inherit the slot.
            same = (
                existing.process_id == identity["process_id"]
                and existing.process_create_time == identity["process_create_time"]
                and existing.lease_sha256 == identity["lease_sha256"]
                and existing.session_id == identity["session_id"]
            )
            if same:
                return {
                    "admitted": True,
                    "reason": "already_registered",
                    "cores": admitted_cores,
                    "identity": identity,
                    "owner_count": self.owner_count(),
                }
            return {
                "admitted": False,
                "reason": "owner_identity_conflict",
                "cores": admitted_cores,
                "identity": identity,
                "owner_count": self.owner_count(),
            }

        if len(self._owners) >= self._maximum_owners:
            return {
                "admitted": False,
                "reason": "owner_limit_reached",
                "cores": admitted_cores,
                "identity": identity,
                "owner_count": self.owner_count(),
            }

        return {
            "admitted": True,
            "reason": "admitted",
            "cores": admitted_cores,
            "identity": identity,
            "owner_count": self.owner_count() + 1,
        }

    def register(
        self,
        *,
        owner: object,
        cores: object,
        process_id: object,
        process_create_time: object,
        lease_sha256: object,
        session_id: object,
        registered_at_ms: int = 0,
    ) -> OwnerRecord:
        """Admit one owner or refuse with a reason code."""
        verdict = self.evaluate_registration(
            owner=owner,
            cores=cores,
            process_id=process_id,
            process_create_time=process_create_time,
            lease_sha256=lease_sha256,
            session_id=session_id,
        )
        if not verdict["admitted"]:
            raise OwnerAdmissionError(verdict["reason"], _refusal_message(verdict["reason"]))
        identity = verdict["identity"]
        if not isinstance(identity, dict):
            # An admitted verdict without an identity would be a bug in this
            # module, not caller input; refuse rather than register a partial row.
            raise OwnerAdmissionError(
                "invalid_identity", "an admitted registration must carry its identity"
            )
        existing = self._owners.get(identity["owner"])
        if existing is not None:
            return existing
        record = OwnerRecord(
            owner=identity["owner"],
            cores=verdict["cores"],
            process_id=identity["process_id"],
            process_create_time=identity["process_create_time"],
            lease_sha256=identity["lease_sha256"],
            session_id=identity["session_id"],
            registered_at_ms=int(registered_at_ms),
        )
        self._owners[record.owner] = record
        return record

    def evaluate_release(
        self,
        *,
        owner: object,
        process_id: object,
        process_create_time: object,
        observed_process_absent: bool,
        observed_lease_absent: bool,
    ) -> dict[str, Any]:
        """Decide whether an owner's slot may be released.

        A release frees a concurrency slot, so it requires the same proof a
        terminal cancellation does: the exact owned process and lease must be
        observed absent. An unverified release would leak capacity or, worse,
        free a slot while the previous solver is still running.
        """
        try:
            normalized_owner = _bounded_identifier(owner, "owner")
        except OwnerAdmissionError as exc:
            return {"released": False, "reason": exc.reason_code}
        record = self._owners.get(normalized_owner)
        if record is None:
            return {"released": False, "reason": "owner_not_registered"}
        if isinstance(process_id, bool) or not isinstance(process_id, int):
            return {"released": False, "reason": "invalid_process_identity"}
        if isinstance(process_create_time, bool) or not isinstance(
            process_create_time, (int, float)
        ):
            return {"released": False, "reason": "invalid_process_identity"}
        if record.process_id != process_id or record.process_create_time != float(
            process_create_time
        ):
            # A different process cannot release this owner's slot.
            return {"released": False, "reason": "process_identity_mismatch"}
        if observed_process_absent is not True:
            return {"released": False, "reason": "process_still_present"}
        if observed_lease_absent is not True:
            return {"released": False, "reason": "lease_still_present"}
        return {"released": True, "reason": "released"}

    def release(
        self,
        *,
        owner: object,
        process_id: object,
        process_create_time: object,
        observed_process_absent: bool,
        observed_lease_absent: bool,
    ) -> dict[str, Any]:
        """Release one verified-absent owner slot, or refuse."""
        verdict = self.evaluate_release(
            owner=owner,
            process_id=process_id,
            process_create_time=process_create_time,
            observed_process_absent=observed_process_absent,
            observed_lease_absent=observed_lease_absent,
        )
        if verdict["released"]:
            self._owners.pop(str(owner).strip(), None)
            verdict["remaining_slots"] = self.remaining_slots()
        else:
            verdict["remaining_slots"] = self.remaining_slots()
        return verdict

    # -- isolation ---------------------------------------------------------

    def evaluate_resource_access(self, *, owner: object, resource_owner: object) -> dict[str, Any]:
        """Decide whether one owner may address another owner's resource.

        The refusal is byte-identical to the reply for a resource that does not
        exist at all, and carries no field describing *why* it was refused. That
        is deliberate: any distinguishing field — including one naming
        cross-owner access — would turn this method into an oracle for
        enumerating another owner's resources.
        """
        normalized = _bounded_identifier(owner, "owner")
        target = _bounded_identifier(resource_owner, "resource_owner")
        if normalized == target:
            return {
                "allowed": True,
                "reason": "same_owner",
                "reported_as": "found",
            }
        return {
            "allowed": False,
            # Indistinguishable from an absent resource, by design.
            "reason": "not_found",
            "reported_as": "not_found",
        }

    def evaluate_model_access(
        self, *, owner: object, model_owner: object, model_tag: object
    ) -> dict[str, Any]:
        """Same rule, made explicit for model access."""
        verdict = self.evaluate_resource_access(owner=owner, resource_owner=model_owner)
        verdict["model_tag"] = _bounded_identifier(model_tag, "model_tag")
        return verdict


def _refusal_message(reason: str) -> str:
    messages = {
        "owner_identity_conflict": (
            "this owner label is already registered to a different process or lease"
        ),
        "owner_limit_reached": (
            f"at most {MAX_SOLVER_OWNERS} independent one-core solver owners may be registered"
        ),
    }
    return messages.get(reason, "solver owner admission was refused")


def owner_receipt(
    *,
    owner: str,
    action: str,
    verdict: Mapping[str, Any],
    record: OwnerRecord | None = None,
    cleanup: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the receipt for one admission or release decision.

    A prompt acknowledgement is not cleanup evidence, so ``cleanup`` states what
    was actually verified and defaults to ``unknown`` rather than to success.
    """
    body: dict[str, Any] = {
        "schema_name": RECEIPT_SCHEMA_NAME,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "owner": owner,
        "action": action,
        "admitted": bool(verdict.get("admitted", verdict.get("released", False))),
        "reason": verdict.get("reason"),
        "cores": verdict.get("cores", record.cores if record else None),
        "process_id": record.process_id if record else None,
        "process_create_time": record.process_create_time if record else None,
        "lease_sha256": record.lease_sha256 if record else None,
        "session_id": record.session_id if record else None,
        "cleanup": dict(cleanup or {"state": "unknown"}),
        # Stated explicitly so a reader cannot mistake a slot release for a
        # verified process cleanup.
        "release_is_cleanup_evidence": False,
    }
    return {**body, "receipt_sha256": canonical_sha256_v1(body)}


def capability_document(*, registry: SolverOwnerRegistry | None = None) -> dict[str, Any]:
    """Truthful metadata about the owner limits this build enforces."""
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "maximum_independent_solver_owners": MAX_SOLVER_OWNERS,
        "cores_per_owner": OWNER_CORES,
        "multi_core_solver_permitted": False,
        "third_owner_permitted": False,
        "cross_owner_model_access_permitted": False,
        "release_requires_verified_absence": True,
        "currently_registered": registry.owner_count() if registry else 0,
    }


__all__ = [
    "MAX_REQUESTED_CORES",
    "MAX_SOLVER_OWNERS",
    "OWNER_CORES",
    "RECEIPT_SCHEMA_NAME",
    "RECEIPT_SCHEMA_VERSION",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "OwnerAdmissionError",
    "OwnerRecord",
    "SolverOwnerRegistry",
    "capability_document",
    "normalize_owner_identity",
    "normalize_requested_cores",
    "owner_receipt",
]
