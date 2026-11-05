"""Durable operation ledger and recovery tests.

These tests are solver-free: the ledger is a journal plus replay, so crash,
restart, duplicate submission, lost transport, and cleanup uncertainty are all
expressible without a licensed host.

The recovery cases are the point. A restart that repeats completed work, a
second accepted copy of the same artifact, or an unknown cleanup state promoted
to verified on replay would each be a real defect that a happy-path test cannot
detect.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from comsol_mcp.durable.operation_ledger import (
    ALL_OUTCOMES,
    LEDGER_FAMILIES,
    LEDGER_FILENAME,
    NON_TERMINAL_OUTCOMES,
    TERMINAL_OUTCOMES,
    UNKNOWN,
    LedgerError,
    OperationLedger,
    bind_receipt,
    replay_ledger,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


@pytest.mark.parametrize("damage", [b'{"partial":', b"invalid-json\n", b"{}\n"])
@pytest.mark.parametrize("action", ["append", "accept", "accept_unknown", "resume"])
def test_damaged_journal_refuses_new_work_without_changing_bytes(tmp_path, damage, action):
    ledger = OperationLedger(tmp_path / "ledger")
    _append(ledger, subject="completed", artifact_sha256=DIGEST_A)
    with ledger.journal_path.open("ab") as handle:
        handle.write(damage)
    original = ledger.journal_path.read_bytes()
    reopened = OperationLedger(ledger.root)
    with pytest.raises(LedgerError, match="damaged"):
        if action == "append":
            _append(reopened, subject="next")
        elif action.startswith("accept"):
            reopened.evaluate_acceptance(
                subject="next", artifact_sha256=None if action == "accept_unknown" else DIGEST_A
            )
        else:
            reopened.evaluate_resume(subject="completed")
    assert ledger.journal_path.read_bytes() == original


class Clock:
    """A deterministic clock so ledger rows are reproducible."""

    def __init__(self) -> None:
        self.value = 1_000_000

    def __call__(self) -> int:
        self.value += 1000
        return self.value


@pytest.fixture()
def ledger(tmp_path: Path) -> OperationLedger:
    return OperationLedger(tmp_path / "ledger", clock_ms=Clock())


def _append(ledger: OperationLedger, **overrides: object):
    kwargs: dict[str, object] = {
        "family": "dbmodel",
        "operation": "open",
        "subject": "dbmodel://library/models/cell",
        "owner": "owner-a",
        "outcome": "succeeded",
    }
    kwargs.update(overrides)
    return ledger.append(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def test_the_ledger_vocabulary_is_closed() -> None:
    assert LEDGER_FAMILIES == ("dbmodel", "solver_owner", "local_download", "local_session")
    assert set(TERMINAL_OUTCOMES) | set(NON_TERMINAL_OUTCOMES) == set(ALL_OUTCOMES)
    assert not set(TERMINAL_OUTCOMES) & set(NON_TERMINAL_OUTCOMES)


@pytest.mark.parametrize("family", ["dbmodel", "solver_owner", "local_download", "local_session"])
def test_every_declared_family_is_recordable(ledger: OperationLedger, family: str) -> None:
    row = _append(ledger, family=family, subject=f"{family}/subject")
    assert row.family == family


@pytest.mark.parametrize("family", ["network", "", None, "dbmodel ", 7])
def test_an_undeclared_family_is_refused(ledger: OperationLedger, family: object) -> None:
    with pytest.raises(LedgerError) as excinfo:
        _append(ledger, family=family)
    assert excinfo.value.reason_code == "invalid_family"


@pytest.mark.parametrize("outcome", ["", "done", None, 5, "SUCCEEDED"])
def test_an_undeclared_outcome_is_refused(ledger: OperationLedger, outcome: object) -> None:
    with pytest.raises(LedgerError) as excinfo:
        _append(ledger, outcome=outcome)
    assert excinfo.value.reason_code == "invalid_outcome"


def test_a_non_ascii_row_is_refused(ledger: OperationLedger) -> None:
    """Rows are read back as evidence; an encoding surprise must not break them."""
    with pytest.raises(LedgerError) as excinfo:
        _append(ledger, subject="dbmodel://library/\u6a21\u578b")
    assert excinfo.value.reason_code == "invalid_row"


# ---------------------------------------------------------------------------
# Durability: a reported outcome always has a row behind it
# ---------------------------------------------------------------------------


def test_an_appended_row_is_durable_before_it_is_returned(ledger: OperationLedger) -> None:
    row = _append(ledger, subject="subject-1")
    # A fresh ledger object over the same root proves durability, not caching.
    reopened = OperationLedger(ledger.root)
    assert [r.subject for r in reopened.rows()] == ["subject-1"]
    assert row.sequence == 1


def test_sequences_are_monotonic_and_unique(ledger: OperationLedger) -> None:
    sequences = [_append(ledger, subject=f"subject-{i}").sequence for i in range(5)]
    assert sequences == [1, 2, 3, 4, 5]


def test_a_sequence_continues_after_a_reopen(ledger: OperationLedger) -> None:
    _append(ledger, subject="subject-1")
    reopened = OperationLedger(ledger.root, clock_ms=Clock())
    assert _append(reopened, subject="subject-2").sequence == 2


def test_the_journal_is_append_only_jsonl(ledger: OperationLedger) -> None:
    _append(ledger, subject="subject-1")
    _append(ledger, subject="subject-2")
    lines = ledger.journal_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        document = json.loads(line)
        assert document["schema_name"] == "comsol_mcp.operation_ledger_row"
        assert document["subject"]


def test_the_ledger_writes_only_its_owned_file(ledger: OperationLedger) -> None:
    _append(ledger, subject="subject-1")
    assert {p.name for p in ledger.root.iterdir()} == {LEDGER_FILENAME}


def test_an_oversized_row_is_refused_rather_than_written(ledger: OperationLedger) -> None:
    with pytest.raises(LedgerError) as excinfo:
        _append(ledger, subject="subject-1", detail={"blob": "x" * 300_000})
    assert excinfo.value.reason_code == "row_too_large"
    assert ledger.rows() == []


# ---------------------------------------------------------------------------
# Crash, damage, and partial writes
# ---------------------------------------------------------------------------


def test_a_partial_trailing_row_is_excluded_and_reported(ledger: OperationLedger) -> None:
    """A crash mid-write must degrade the newest row, not the earlier ones."""
    assert ledger.journal_state in {"absent", "complete"}
    _append(ledger, subject="subject-1")
    with ledger.journal_path.open("ab") as handle_file:
        handle_file.write(b'{"schema_name": "comsol_mcp.operation_ledger_row"')

    reopened = OperationLedger(ledger.root)
    assert [r.subject for r in reopened.rows()] == ["subject-1"]
    # The damage is visible rather than silently absorbed.
    assert reopened.journal_state == "incomplete"
    state = reopened.replay()
    assert state["row_count"] == 1
    assert state["journal_state"] == "incomplete"


def test_a_row_with_a_missing_envelope_is_skipped_not_trusted(ledger: OperationLedger) -> None:
    _append(ledger, subject="subject-1")
    # A well-formed line that is not a ledger row must not be counted.
    with ledger.journal_path.open("ab") as handle_file:
        handle_file.write(json.dumps({"subject": "impostor"}).encode("utf-8") + b"\n")

    reopened = OperationLedger(ledger.root)
    assert [r.subject for r in reopened.rows()] == ["subject-1"]
    state = reopened.replay()
    assert state["row_count"] == 1
    assert state["skipped_row_count"] == 1


def test_a_row_with_a_malformed_hash_is_skipped(ledger: OperationLedger) -> None:
    _append(ledger, subject="subject-1")
    bad = {
        "schema_name": "comsol_mcp.operation_ledger_row",
        "schema_version": "1.0.0",
        "sequence": 2,
        "recorded_at_ms": 1,
        "family": "dbmodel",
        "operation": "open",
        "subject": "subject-2",
        "owner": "owner-a",
        "outcome": "succeeded",
        "receipt_sha256": "not-a-hash",
        "artifact_sha256": UNKNOWN,
        "cleanup_state": UNKNOWN,
        "detail": {},
    }
    with ledger.journal_path.open("ab") as handle_file:
        handle_file.write(json.dumps(bad).encode("utf-8") + b"\n")
    assert [r.subject for r in OperationLedger(ledger.root).rows()] == ["subject-1"]


def test_replay_counts_damage_without_aborting(ledger: OperationLedger) -> None:
    _append(ledger, subject="subject-1")
    _append(ledger, subject="subject-2")
    state = replay_ledger([{"not": "a row"}, *[r.to_document() for r in ledger.rows()]])
    assert state["row_count"] == 2
    assert state["skipped_row_count"] == 1
    assert state["last_sequence"] == 2


# ---------------------------------------------------------------------------
# Restart recovery: no duplicated or repeated work
# ---------------------------------------------------------------------------


def test_a_restart_does_not_repeat_terminal_work(ledger: OperationLedger) -> None:
    _append(ledger, subject="subject-1", outcome="succeeded")
    reopened = OperationLedger(ledger.root)
    decision = reopened.evaluate_resume(subject="subject-1")
    assert decision["resume"] is False
    assert decision["reason"] == "prior_attempt_is_terminal"
    assert decision["prior_outcome"] == "succeeded"


@pytest.mark.parametrize("outcome", ["refused", "failed", "abandoned"])
def test_every_terminal_outcome_blocks_a_repeat(ledger: OperationLedger, outcome: str) -> None:
    _append(ledger, subject="subject-1", outcome=outcome)
    assert OperationLedger(ledger.root).evaluate_resume(subject="subject-1")["resume"] is False


@pytest.mark.parametrize("outcome", sorted(NON_TERMINAL_OUTCOMES))
def test_an_interrupted_attempt_may_resume(ledger: OperationLedger, outcome: str) -> None:
    _append(ledger, subject="subject-1", outcome=outcome)
    decision = OperationLedger(ledger.root).evaluate_resume(subject="subject-1")
    assert decision["resume"] is True
    assert decision["reason"] == "prior_attempt_was_interrupted"


def test_a_subject_with_no_history_may_start(ledger: OperationLedger) -> None:
    decision = ledger.evaluate_resume(subject="never-seen")
    assert decision["resume"] is True
    assert decision["reason"] == "no_prior_attempt"
    assert decision["prior_outcome"] is None


def test_the_latest_attempt_wins_for_recovery(ledger: OperationLedger) -> None:
    """A retried subject is judged by its newest row, not its first."""
    _append(ledger, subject="subject-1", outcome="failed")
    _append(ledger, subject="subject-1", outcome="succeeded")
    decision = OperationLedger(ledger.root).evaluate_resume(subject="subject-1")
    assert decision["resume"] is False
    assert decision["prior_outcome"] == "succeeded"


# ---------------------------------------------------------------------------
# Accepted artifacts are immutable
# ---------------------------------------------------------------------------


def test_a_new_artifact_may_be_accepted(ledger: OperationLedger) -> None:
    _append(ledger, subject="subject-1", artifact_sha256=DIGEST_A)
    decision = ledger.evaluate_acceptance(subject="subject-2", artifact_sha256=DIGEST_B)
    assert decision["accept"] is True
    assert decision["reason"] == "not_previously_accepted"
    assert decision["verified"] is True


def test_the_same_artifact_under_the_same_subject_is_not_accepted_twice(
    ledger: OperationLedger,
) -> None:
    _append(ledger, subject="subject-1", artifact_sha256=DIGEST_A)
    decision = ledger.evaluate_acceptance(subject="subject-1", artifact_sha256=DIGEST_A)
    assert decision["accept"] is False
    assert decision["reason"] == "already_accepted_same_subject"


def test_the_same_bytes_under_another_subject_are_refused(ledger: OperationLedger) -> None:
    """A retry under a new label must not create a second accepted copy."""
    _append(ledger, subject="subject-1", artifact_sha256=DIGEST_A)
    decision = ledger.evaluate_acceptance(subject="subject-other", artifact_sha256=DIGEST_A)
    assert decision["accept"] is False
    assert decision["reason"] == "already_accepted_other_subject"
    assert decision["existing_subject"] == "subject-1"


def test_an_unknown_artifact_identity_is_accepted_but_unverified(
    ledger: OperationLedger,
) -> None:
    """Accepting unverified bytes must not be reported as a verified match."""
    decision = ledger.evaluate_acceptance(subject="subject-1", artifact_sha256=None)
    assert decision["accept"] is True
    assert decision["reason"] == "identity_unknown"
    assert decision["verified"] is False


def test_a_refused_or_failed_attempt_does_not_reserve_an_artifact(
    ledger: OperationLedger,
) -> None:
    _append(ledger, subject="subject-1", outcome="failed", artifact_sha256=DIGEST_A)
    _append(ledger, subject="subject-2", outcome="refused", artifact_sha256=DIGEST_A)
    decision = ledger.evaluate_acceptance(subject="subject-3", artifact_sha256=DIGEST_A)
    assert decision["accept"] is True


# ---------------------------------------------------------------------------
# Uncertainty survives replay
# ---------------------------------------------------------------------------


def test_an_unknown_cleanup_state_replays_as_unknown(ledger: OperationLedger) -> None:
    _append(ledger, subject="subject-1", outcome="failed", cleanup_state=UNKNOWN)
    report = OperationLedger(ledger.root).recovery_report()
    assert "subject-1" in report["cleanup_state_unknown_subjects"]
    assert report["uncertainty_preserved"] is True


def test_a_verified_cleanup_state_is_not_reported_as_uncertain(ledger: OperationLedger) -> None:
    _append(ledger, subject="subject-1", cleanup_state="verified")
    report = OperationLedger(ledger.root).recovery_report()
    assert report["cleanup_state_unknown_subjects"] == []


def test_an_unresolved_hash_replays_as_unknown_not_as_a_digest(
    ledger: OperationLedger,
) -> None:
    row = _append(ledger, subject="subject-1", artifact_sha256=None)
    assert row.artifact_sha256 == UNKNOWN
    assert row.to_document()["artifact_sha256"] == UNKNOWN
    assert OperationLedger(ledger.root).replay()["accepted_artifacts"] == {}


def test_the_recovery_report_names_interrupted_subjects(ledger: OperationLedger) -> None:
    _append(ledger, subject="interrupted", outcome="in_progress")
    _append(ledger, subject="done", outcome="succeeded")
    report = OperationLedger(ledger.root).recovery_report()
    assert report["interrupted_subjects"] == ["interrupted"]
    assert report["accepted_artifact_count"] == 0
    assert report["report_sha256"]


def test_the_recovery_report_is_deterministic(ledger: OperationLedger) -> None:
    _append(ledger, subject="subject-1", outcome="in_progress")
    first = OperationLedger(ledger.root).recovery_report()
    second = OperationLedger(ledger.root).recovery_report()
    assert first == second


def test_the_journal_digest_binds_the_recovery_report(ledger: OperationLedger) -> None:
    _append(ledger, subject="subject-1")
    digest = ledger.journal_digest()
    assert digest != UNKNOWN
    assert ledger.recovery_report()["journal_sha256"] == digest


def test_recovery_digest_matches_the_replayed_snapshot(ledger, monkeypatch) -> None:
    _append(ledger, subject="subject-1")
    original_digest = ledger.journal_digest()
    read_report = ledger._read_report

    def append_after_snapshot():
        report = read_report()
        _append(OperationLedger(ledger.root), subject="subject-2")
        return report

    monkeypatch.setattr(ledger, "_read_report", append_after_snapshot)
    report = ledger.recovery_report()
    assert report["row_count"] == 1
    assert report["journal_sha256"] == original_digest
    assert report["journal_sha256"] != ledger.journal_digest()


def test_an_empty_journal_reports_unknown_rather_than_a_hash(tmp_path: Path) -> None:
    empty = OperationLedger(tmp_path / "absent")
    assert empty.journal_digest() == UNKNOWN
    assert empty.rows() == []
    report = empty.recovery_report()
    assert report["row_count"] == 0
    assert report["interrupted_subjects"] == []


def test_journal_digest_preserves_distinct_damaged_bytes(ledger: OperationLedger) -> None:
    _append(ledger, subject="subject-1")
    original = ledger.journal_path.read_bytes()
    digests = []
    for suffix in (b"\xff", b"\xfe"):
        payload = original + suffix
        ledger.journal_path.write_bytes(payload)
        digest = ledger.journal_digest()
        assert digest == hashlib.sha256(payload).hexdigest()
        digests.append(digest)
    assert digests[0] != digests[1]


# ---------------------------------------------------------------------------
# Receipt binding: a supplied receipt must be checkable
# ---------------------------------------------------------------------------


def test_a_self_consistent_receipt_binds_successfully() -> None:
    from comsol_mcp.durable import canonical_sha256_v1

    body = {"operation": "open", "outcome": "succeeded"}
    receipt = {**body, "receipt_sha256": canonical_sha256_v1(body)}
    binding = bind_receipt(
        family="dbmodel",
        operation="open",
        subject="subject-1",
        owner="owner-a",
        receipt=receipt,
    )
    assert binding["receipt_self_consistent"] is True
    assert binding["receipt_sha256"] == receipt["receipt_sha256"]


def test_a_tampered_receipt_is_reported_as_inconsistent() -> None:
    """A receipt supplied out of band must not be trusted on its own word."""
    from comsol_mcp.durable import canonical_sha256_v1

    body = {"operation": "open", "outcome": "succeeded"}
    receipt = {**body, "receipt_sha256": canonical_sha256_v1(body)}
    receipt["outcome"] = "failed"  # tampered after hashing
    binding = bind_receipt(
        family="dbmodel",
        operation="open",
        subject="subject-1",
        owner="owner-a",
        receipt=receipt,
    )
    assert binding["receipt_self_consistent"] is False
    assert binding["recomputed_receipt_sha256"] != binding["receipt_sha256"]


def test_binding_refuses_a_non_object_receipt() -> None:
    with pytest.raises(LedgerError) as excinfo:
        bind_receipt(
            family="dbmodel",
            operation="open",
            subject="subject-1",
            owner="owner-a",
            receipt=["not", "an", "object"],  # type: ignore[arg-type]
        )
    assert excinfo.value.reason_code == "invalid_receipt"


def test_binding_refuses_an_undeclared_family() -> None:
    with pytest.raises(LedgerError) as excinfo:
        bind_receipt(
            family="network",
            operation="open",
            subject="subject-1",
            owner="owner-a",
            receipt={},
        )
    assert excinfo.value.reason_code == "invalid_family"


def test_a_bound_receipt_can_be_journaled_with_its_hash(ledger: OperationLedger) -> None:
    """The binding closes the loop between a receipt and its durable row."""
    from comsol_mcp.durable import canonical_sha256_v1

    body = {"operation": "download", "outcome": "succeeded"}
    receipt = {**body, "receipt_sha256": canonical_sha256_v1(body)}
    binding = bind_receipt(
        family="local_download",
        operation="download",
        subject="local://downloads/cell.mph",
        owner="owner-a",
        receipt=receipt,
        artifact_sha256=DIGEST_A,
    )
    row = _append(
        ledger,
        family="local_download",
        operation="download",
        subject="local://downloads/cell.mph",
        receipt_sha256=binding["receipt_sha256"],
        artifact_sha256=DIGEST_A,
        outcome="succeeded",
        cleanup_state="verified",
    )
    replayed = OperationLedger(ledger.root).replay()["latest_by_subject"][row.subject]
    assert replayed["receipt_sha256"] == binding["receipt_sha256"]
    assert replayed["artifact_sha256"] == DIGEST_A


# ---------------------------------------------------------------------------
# Owner and session events use the same ledger
# ---------------------------------------------------------------------------


def test_solver_owner_events_are_journaled_in_the_same_ledger(ledger: OperationLedger) -> None:
    _append(
        ledger,
        family="solver_owner",
        operation="register",
        subject="owner-a",
        outcome="succeeded",
        cleanup_state="not_applicable",
    )
    _append(
        ledger,
        family="solver_owner",
        operation="release",
        subject="owner-a",
        outcome="succeeded",
        cleanup_state="verified",
    )
    state = OperationLedger(ledger.root).replay()
    assert state["family_counts"] == {"solver_owner": 2}
    assert state["latest_by_subject"]["owner-a"]["operation"] == "release"


def test_a_third_owner_refusal_is_journaled_with_its_reason(ledger: OperationLedger) -> None:
    _append(
        ledger,
        family="solver_owner",
        operation="register",
        subject="owner-c",
        outcome="refused",
        cleanup_state="not_applicable",
        detail={"reason": "owner_limit_reached"},
    )
    latest = OperationLedger(ledger.root).replay()["latest_by_subject"]["owner-c"]
    assert latest["outcome"] == "refused"
    assert latest["detail"]["reason"] == "owner_limit_reached"
