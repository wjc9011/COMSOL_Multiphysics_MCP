"""Durable, replayable operation ledger for Model Manager and owner events.

Why a separate ledger
---------------------

The operation modules decide *whether* something may happen and *what* a receipt
must say. This module makes those receipts durable and replayable, so a crash,
a restart, a lost transport, or a delayed file share can be diagnosed from
evidence instead of from memory.

It is append-only by construction. An operation is journaled once, and the
journal is the authority for what happened; the in-memory index exists only to
answer questions quickly and is rebuilt entirely from the journal on load. That
makes a restart replay exactly what was recorded rather than what the previous
process believed.

Frozen rules
------------

* **One row per attempt, written before the outcome is reported.** A crash after
  the write leaves a durable record of the attempt; a crash before it leaves no
  claim at all. There is no path where an operation is reported without a row.
* **Replay is the recovery mechanism.** A restart reconstructs state from the
  journal, so an operation that was already accepted is never performed twice.
* **Accepted artifacts are never overwritten.** A second attempt to accept the
  same artifact identity with different bytes is refused; the original stands.
* **Uncertainty survives the round trip.** An unresolved identity or an
  unverified cleanup state replays as ``unknown``, never promoted to a success.
* **The ledger cannot be silently truncated.** A partial trailing row is
  reported and excluded rather than treated as authoritative, and a corrupt
  journal is refused rather than partially trusted.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from comsol_mcp.durable import canonical_sha256_v1
from comsol_mcp.durable.io import append_jsonl_record, read_complete_jsonl

SCHEMA_NAME = "comsol_mcp.operation_ledger_row"
SCHEMA_VERSION = "1.0.0"

#: One row per recorded attempt, matching the append-only journal format.
LEDGER_FILENAME = "operations.jsonl"

#: Bound on rows replayed into the in-memory index.
MAX_LEDGER_ROWS = 100_000

#: Bound on the serialized size of one ledger row.
MAX_LEDGER_ROW_BYTES = 256 * 1024

#: The operation families this ledger records. Keeping the vocabulary closed
#: means a new event kind cannot be introduced by a caller's free-form string.
LEDGER_FAMILIES = ("dbmodel", "solver_owner", "local_download", "local_session")

#: Terminal dispositions an operation may record.
TERMINAL_OUTCOMES = ("succeeded", "refused", "failed", "abandoned")
NON_TERMINAL_OUTCOMES = ("pending", "in_progress")
ALL_OUTCOMES = NON_TERMINAL_OUTCOMES + TERMINAL_OUTCOMES

UNKNOWN = "unknown"


class LedgerError(RuntimeError):
    """One stable, bounded ledger failure with a reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _bounded_text(value: object, label: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError("invalid_row", f"{label} must be a nonempty string")
    if len(value) > maximum:
        raise LedgerError("invalid_row", f"{label} is out of range")
    if not value.isascii():
        # Rows are read back as protocol evidence; keeping them ASCII avoids an
        # encoding surprise turning a valid row into an unreadable one.
        raise LedgerError("invalid_row", f"{label} must be ASCII")
    return value.strip()


def _outcome(value: object) -> str:
    if not isinstance(value, str) or value not in ALL_OUTCOMES:
        raise LedgerError("invalid_outcome", f"outcome must be one of {', '.join(ALL_OUTCOMES)}")
    return value


def _family(value: object) -> str:
    if not isinstance(value, str) or value not in LEDGER_FAMILIES:
        raise LedgerError("invalid_family", f"family must be one of {', '.join(LEDGER_FAMILIES)}")
    return value


@dataclass(frozen=True)
class LedgerRow:
    """One durable operation attempt."""

    sequence: int
    recorded_at_ms: int
    family: str
    operation: str
    subject: str
    owner: str
    outcome: str
    receipt_sha256: str
    artifact_sha256: str
    cleanup_state: str
    detail: Mapping[str, Any]

    def to_document(self) -> dict[str, Any]:
        # Self-describing, so a journal read later does not depend on this
        # module's current constants.
        return {
            "schema_name": SCHEMA_NAME,
            "schema_version": SCHEMA_VERSION,
            "sequence": self.sequence,
            "recorded_at_ms": self.recorded_at_ms,
            "family": self.family,
            "operation": self.operation,
            "subject": self.subject,
            "owner": self.owner,
            "outcome": self.outcome,
            "receipt_sha256": self.receipt_sha256,
            "artifact_sha256": self.artifact_sha256,
            "cleanup_state": self.cleanup_state,
            "detail": dict(self.detail),
        }

    @property
    def is_terminal(self) -> bool:
        return self.outcome in TERMINAL_OUTCOMES


def _sha256_or_unknown(value: object) -> str:
    if value is None:
        return UNKNOWN
    if not isinstance(value, str):
        raise LedgerError("invalid_row", "a hash field must be a string or absent")
    text = value.strip().lower()
    if not text or text == UNKNOWN:
        return UNKNOWN
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise LedgerError("invalid_row", "a hash field must be 64 hexadecimal digits")
    return text


def replay_ledger(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Reconstruct ledger state from journal rows in order.

    Replay is total over the recorded rows and never invents a row that was not
    written. A row that fails validation is counted and skipped rather than
    allowed to abort the whole replay, because one damaged row must not hide the
    operations that were recorded correctly.
    """
    accepted: dict[str, dict[str, Any]] = {}
    latest: dict[str, dict[str, Any]] = {}
    family_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}
    skipped = 0
    last_sequence = 0

    for raw in rows:
        try:
            document = _validated(document=raw)
        except LedgerError:
            skipped += 1
            continue
        subject = document["subject"]
        family = document["family"]
        outcome = document["outcome"]
        latest[subject] = document
        family_counts[family] = family_counts.get(family, 0) + 1
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
        last_sequence = max(last_sequence, document["sequence"])
        if outcome == "succeeded":
            artifact = document["artifact_sha256"]
            if artifact != UNKNOWN:
                prior = accepted.get(artifact)
                if prior is None:
                    accepted[artifact] = document

    return {
        "row_count": sum(outcome_counts.values()),
        "skipped_row_count": skipped,
        "last_sequence": last_sequence,
        "family_counts": dict(sorted(family_counts.items())),
        "outcome_counts": dict(sorted(outcome_counts.items())),
        "accepted_artifacts": dict(sorted(accepted.items())),
        "latest_by_subject": dict(sorted(latest.items())),
    }


def _validated(*, document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one journal row into its normalized form."""
    if not isinstance(document, Mapping):
        raise LedgerError("invalid_row", "a ledger row must be an object")
    if document.get("schema_name") != SCHEMA_NAME:
        raise LedgerError("invalid_row", "a ledger row must declare its schema name")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise LedgerError("invalid_row", "a ledger row must declare its schema version")
    sequence = document.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise LedgerError("invalid_row", "sequence must be a positive integer")
    recorded = document.get("recorded_at_ms")
    if isinstance(recorded, bool) or not isinstance(recorded, int) or recorded < 0:
        raise LedgerError("invalid_row", "recorded_at_ms must be a nonnegative integer")
    detail = document.get("detail")
    if detail is not None and not isinstance(detail, Mapping):
        raise LedgerError("invalid_row", "detail must be an object")
    return {
        "sequence": sequence,
        "recorded_at_ms": recorded,
        "family": _family(document.get("family")),
        "operation": _bounded_text(document.get("operation"), "operation", 128),
        "subject": _bounded_text(document.get("subject"), "subject", 512),
        "owner": _bounded_text(document.get("owner"), "owner", 256),
        "outcome": _outcome(document.get("outcome")),
        "receipt_sha256": _sha256_or_unknown(document.get("receipt_sha256")),
        "artifact_sha256": _sha256_or_unknown(document.get("artifact_sha256")),
        "cleanup_state": _bounded_text(document.get("cleanup_state"), "cleanup_state", 64),
        "detail": dict(detail or {}),
    }


class OperationLedger:
    """Append-only operation ledger with replay-based recovery."""

    def __init__(self, root: str | Path, *, clock_ms: Any = None) -> None:
        self.root = Path(root)
        self.journal_path = self.root / LEDGER_FILENAME
        self._clock_ms = clock_ms

    def _now(self) -> int:
        if self._clock_ms is not None:
            return int(self._clock_ms())
        import time

        return int(time.time() * 1000)

    # -- writes ------------------------------------------------------------

    def append(
        self,
        *,
        family: object,
        operation: object,
        subject: object,
        owner: object,
        outcome: object,
        receipt_sha256: object = None,
        artifact_sha256: object = None,
        cleanup_state: object = UNKNOWN,
        detail: Mapping[str, Any] | None = None,
    ) -> LedgerRow:
        """Journal one attempt, returning the durable row.

        The row is written and flushed before it is returned, so a caller that
        reports an outcome has a durable record of the attempt behind it.
        """
        normalized_family = _family(family)
        normalized_outcome = _outcome(outcome)
        sequence = self._next_sequence()
        row = LedgerRow(
            sequence=sequence,
            recorded_at_ms=self._now(),
            family=normalized_family,
            operation=_bounded_text(operation, "operation", 128),
            subject=_bounded_text(subject, "subject", 512),
            owner=_bounded_text(owner, "owner", 256),
            outcome=normalized_outcome,
            receipt_sha256=_sha256_or_unknown(receipt_sha256),
            artifact_sha256=_sha256_or_unknown(artifact_sha256),
            cleanup_state=_bounded_text(cleanup_state, "cleanup_state", 64),
            detail=dict(detail or {}),
        )
        document = row.to_document()
        import json

        encoded = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(encoded) > MAX_LEDGER_ROW_BYTES:
            raise LedgerError("row_too_large", "ledger row exceeds its size bound")
        self.root.mkdir(parents=True, exist_ok=True)
        append_jsonl_record(self.journal_path, document)
        return row

    def _next_sequence(self) -> int:
        return int(self._recovery_state()["last_sequence"]) + 1

    def _recovery_state(self) -> dict[str, Any]:
        """Refuse new work when replay cannot prove the complete history."""
        state = self.replay()
        if state["journal_state"] not in {"absent", "current_valid"} or state["skipped_row_count"]:
            raise LedgerError(
                "ledger_damaged", "the operation ledger is damaged; new work is refused"
            )
        if state["row_count"] >= MAX_LEDGER_ROWS:
            raise LedgerError("ledger_oversized", "the operation ledger exceeds its row bound")
        return state

    # -- reads -------------------------------------------------------------

    def _read_report(self) -> dict[str, Any]:
        try:
            report = read_complete_jsonl(self.journal_path)
        except OSError as exc:
            raise LedgerError("ledger_unreadable", "the operation ledger is unreadable") from exc
        if report.get("state") == "oversized":
            raise LedgerError("ledger_oversized", "the operation ledger exceeds its size bound")
        return report

    def rows(self) -> list[LedgerRow]:
        """Return complete, well-formed rows in journal order.

        A damaged row is excluded rather than allowed to abort the read, so one
        bad write cannot make every earlier record unreadable.
        """
        report = self._read_report()
        records = report.get("records")
        if not isinstance(records, list):
            return []
        rows: list[LedgerRow] = []
        for raw in records[-MAX_LEDGER_ROWS:]:
            try:
                document = _validated(document=raw)
            except LedgerError:
                continue
            rows.append(
                LedgerRow(
                    sequence=document["sequence"],
                    recorded_at_ms=document["recorded_at_ms"],
                    family=document["family"],
                    operation=document["operation"],
                    subject=document["subject"],
                    owner=document["owner"],
                    outcome=document["outcome"],
                    receipt_sha256=document["receipt_sha256"],
                    artifact_sha256=document["artifact_sha256"],
                    cleanup_state=document["cleanup_state"],
                    detail=document["detail"],
                )
            )
        return rows

    @property
    def journal_state(self) -> str:
        """Report how the journal was read, so damage is visible not silent."""
        return str(self._read_report().get("state", "unknown"))

    def replay(self) -> dict[str, Any]:
        """Rebuild state from the journal; this is the recovery path."""
        report = self._read_report()
        records = report.get("records")
        state = replay_ledger(records if isinstance(records, list) else [])
        state["journal_state"] = report.get("state", "unknown")
        state["journal_sha256"] = report.get("source_sha256", UNKNOWN)
        return state

    def journal_digest(self) -> str:
        try:
            payload = self.journal_path.read_bytes()
        except FileNotFoundError:
            return UNKNOWN
        return hashlib.sha256(payload).hexdigest()

    # -- recovery decisions ------------------------------------------------

    def evaluate_acceptance(self, *, subject: str, artifact_sha256: object) -> dict[str, Any]:
        """Decide whether an artifact may be newly accepted.

        An already-accepted artifact is refused rather than replaced, matching
        the rule that accepted outputs are immutable once created. The check is
        against *artifact content*, so a retry under a different subject cannot
        quietly produce a second accepted copy of the same bytes.
        """
        normalized_subject = _bounded_text(subject, "subject", 512)
        digest = _sha256_or_unknown(artifact_sha256)
        state = self._recovery_state()
        if digest == UNKNOWN:
            return {
                "accept": True,
                "reason": "identity_unknown",
                "existing_subject": None,
                # An unknown identity is accepted but recorded as unverified, so
                # the caller cannot later mistake it for a confirmed match.
                "verified": False,
            }
        accepted = state["accepted_artifacts"]
        existing = accepted.get(digest)
        if existing is None:
            return {
                "accept": True,
                "reason": "not_previously_accepted",
                "existing_subject": None,
                "verified": True,
            }
        if existing["subject"] == normalized_subject:
            return {
                "accept": False,
                "reason": "already_accepted_same_subject",
                "existing_subject": existing["subject"],
                "verified": True,
            }
        return {
            "accept": False,
            "reason": "already_accepted_other_subject",
            "existing_subject": existing["subject"],
            "verified": True,
        }

    def evaluate_resume(self, *, subject: str) -> dict[str, Any]:
        """Decide what a restarted process may do for one subject.

        A terminal outcome means the work is done, so resuming must not repeat
        it. A non-terminal row means an attempt was interrupted, and the caller
        is told that the previous attempt's cleanup state is what it was
        recorded as — which may be ``unknown``.
        """
        normalized = _bounded_text(subject, "subject", 512)
        latest = self._recovery_state()["latest_by_subject"].get(normalized)
        if latest is None:
            return {
                "resume": True,
                "reason": "no_prior_attempt",
                "prior_outcome": None,
                "prior_cleanup_state": None,
            }
        if latest["outcome"] in TERMINAL_OUTCOMES:
            return {
                "resume": False,
                "reason": "prior_attempt_is_terminal",
                "prior_outcome": latest["outcome"],
                "prior_cleanup_state": latest["cleanup_state"],
            }
        return {
            "resume": True,
            "reason": "prior_attempt_was_interrupted",
            "prior_outcome": latest["outcome"],
            "prior_cleanup_state": latest["cleanup_state"],
        }

    def recovery_report(self) -> dict[str, Any]:
        """Summarize recovery state for a restart, without inventing anything."""
        state = self.replay()
        latest = state["latest_by_subject"]
        interrupted = sorted(
            subject
            for subject, document in latest.items()
            if document["outcome"] in NON_TERMINAL_OUTCOMES
        )
        uncertain_cleanup = sorted(
            subject for subject, document in latest.items() if document["cleanup_state"] == UNKNOWN
        )
        body = {
            "schema_name": "comsol_mcp.operation_recovery_report",
            "schema_version": SCHEMA_VERSION,
            "journal_state": state["journal_state"],
            "journal_sha256": state["journal_sha256"],
            "row_count": state["row_count"],
            "skipped_row_count": state["skipped_row_count"],
            "interrupted_subjects": interrupted,
            "cleanup_state_unknown_subjects": uncertain_cleanup,
            "accepted_artifact_count": len(state["accepted_artifacts"]),
            # A recovery report never claims more certainty than the journal has.
            "uncertainty_preserved": True,
        }
        return {**body, "report_sha256": canonical_sha256_v1(body)}


def bind_receipt(
    *,
    family: str,
    operation: str,
    subject: str,
    owner: str,
    receipt: Mapping[str, Any],
    cleanup_state: str = UNKNOWN,
    artifact_sha256: object = None,
) -> dict[str, Any]:
    """Bind one operation receipt to its ledger row identity.

    The binding records the receipt's own hash, so a later reader can tell
    whether the receipt it holds is the one this ledger recorded, rather than
    trusting a receipt supplied out of band.
    """
    normalized_family = _family(family)
    normalized_operation = _bounded_text(operation, "operation", 128)
    normalized_subject = _bounded_text(subject, "subject", 512)
    normalized_owner = _bounded_text(owner, "owner", 256)
    if not isinstance(receipt, Mapping):
        raise LedgerError("invalid_receipt", "a receipt must be an object")
    declared = receipt.get("receipt_sha256")
    recomputed = canonical_sha256_v1(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    matches = isinstance(declared, str) and declared == recomputed
    return {
        "schema_name": "comsol_mcp.operation_receipt_binding",
        "schema_version": SCHEMA_VERSION,
        "family": normalized_family,
        "operation": normalized_operation,
        "subject": normalized_subject,
        "owner": normalized_owner,
        "receipt_sha256": declared if isinstance(declared, str) else recomputed,
        "recomputed_receipt_sha256": recomputed,
        "receipt_self_consistent": matches,
        "artifact_sha256": _sha256_or_unknown(artifact_sha256),
        "cleanup_state": _bounded_text(cleanup_state, "cleanup_state", 64),
    }


__all__ = [
    "ALL_OUTCOMES",
    "LEDGER_FAMILIES",
    "LEDGER_FILENAME",
    "MAX_LEDGER_ROWS",
    "MAX_LEDGER_ROW_BYTES",
    "NON_TERMINAL_OUTCOMES",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "TERMINAL_OUTCOMES",
    "UNKNOWN",
    "LedgerError",
    "LedgerRow",
    "OperationLedger",
    "bind_receipt",
    "replay_ledger",
]
