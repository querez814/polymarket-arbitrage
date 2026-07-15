import json
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from core.execution_journal import ExecutionJournal, JournalOwnershipError
from core.two_leg_execution import (
    ExecutionPhase,
    LegIntent,
    LegPhase,
    LegSide,
    TwoLegExecution,
)


def execution(execution_id: str = "exec-42") -> TwoLegExecution:
    return TwoLegExecution(
        execution_id,
        LegIntent("poly", "polymarket", "condition-1", LegSide.BUY, 0.40, 10.0),
        LegIntent("kalshi", "kalshi", "ticker-1", LegSide.SELL, 0.65, 10.0),
    )


def test_private_journal_round_trip_preserves_stable_intent(tmp_path):
    path = tmp_path / "executions.sqlite3"
    expected = execution()

    with ExecutionJournal(path) as journal:
        created = journal.create_execution(expected)

    with ExecutionJournal(path) as restarted:
        loaded = restarted.load_execution(expected.execution_id)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert loaded.phase is ExecutionPhase.PLANNED
    assert loaded.legs["poly"].intent == expected.legs["poly"].intent
    assert (
        loaded.legs["poly"].idempotency_key
        == created.legs["poly"].idempotency_key
        == expected.legs["poly"].idempotency_key
    )


def test_journal_ownership_is_exclusive_across_processes(tmp_path):
    path = tmp_path / "executions.sqlite3"
    probe = """
import sys
from pathlib import Path
from core.execution_journal import ExecutionJournal, JournalOwnershipError

try:
    ExecutionJournal(Path(sys.argv[1]))
except JournalOwnershipError:
    raise SystemExit(0)
raise SystemExit(2)
"""

    with ExecutionJournal(path):
        with pytest.raises(JournalOwnershipError, match="owned by another process"):
            ExecutionJournal(path)
        result = subprocess.run(
            [sys.executable, "-c", probe, str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    assert result.returncode == 0, result.stderr

    with ExecutionJournal(path):
        pass

    assert stat.S_IMODE(Path(f"{path}.lock").stat().st_mode) == 0o600


def test_durable_transitions_replay_ambiguous_and_partial_state(tmp_path):
    path = tmp_path / "executions.sqlite3"
    with ExecutionJournal(path) as journal:
        journal.create_execution(execution())
        journal.start_submission("exec-42", "poly")
        journal.mark_submission_ambiguous("exec-42", "poly")

    with ExecutionJournal(path) as restarted:
        ambiguous = restarted.load_execution("exec-42")
        assert ambiguous.phase is ExecutionPhase.RECOVERY_REQUIRED
        assert ambiguous.potential_residual_range == (0.0, 10.0)
        reconciled = restarted.reconcile_leg(
            "exec-42",
            "poly",
            phase=LegPhase.CANCELLED,
            cumulative_filled_size=3.0,
            venue_order_id="poly-order-1",
        )

    assert reconciled.phase is ExecutionPhase.RESIDUAL_EXPOSURE
    assert reconciled.realized_residual_size == 3.0


def test_invalid_transition_rolls_back_without_appending(tmp_path):
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        journal.create_execution(execution())

        with pytest.raises(ValueError, match="cannot mark planned"):
            journal.mark_submission_ambiguous("exec-42", "poly")

        assert journal.event_count("exec-42") == 1
        assert journal.load_execution("exec-42").phase is ExecutionPhase.PLANNED


def test_duplicate_execution_id_fails_closed(tmp_path):
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        journal.create_execution(execution())

        with pytest.raises(ValueError, match="already exists"):
            journal.create_execution(execution())

        assert journal.event_count("exec-42") == 1


def test_hash_chain_detects_payload_tampering(tmp_path):
    path = tmp_path / "executions.sqlite3"
    with ExecutionJournal(path) as journal:
        journal.create_execution(execution())
        journal.start_submission("exec-42", "poly")

    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER execution_events_no_update")
    payload = json.dumps(
        {"event": "submission_started", "leg_id": "kalshi"},
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute(
        "UPDATE execution_events SET payload_json = ? WHERE sequence = 1", (payload,)
    )
    connection.commit()
    connection.close()

    with ExecutionJournal(path) as journal:
        with pytest.raises(ValueError, match="checksum mismatch"):
            journal.load_execution("exec-42")


def test_unfinished_scan_excludes_flat_terminal_execution(tmp_path):
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        journal.create_execution(execution("planned"))
        journal.create_execution(execution("complete"))
        for leg_id, order_id in (
            ("poly", "poly-order-1"),
            ("kalshi", "kalshi-order-1"),
        ):
            journal.start_submission("complete", leg_id)
            journal.reconcile_leg(
                "complete",
                leg_id,
                phase=LegPhase.CANCELLED,
                cumulative_filled_size=0.0,
                venue_order_id=order_id,
            )

        unfinished = journal.load_unfinished()

    assert [item.execution_id for item in unfinished] == ["planned"]


def test_journal_refuses_symlink_and_non_regular_destinations(tmp_path):
    target = tmp_path / "target.sqlite3"
    with ExecutionJournal(target):
        pass
    link = tmp_path / "link.sqlite3"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="regular file"):
        ExecutionJournal(link)
    with pytest.raises(ValueError, match="regular file"):
        ExecutionJournal(tmp_path)

    unsafe_path = tmp_path / "unsafe.sqlite3"
    lease_target = tmp_path / "lease-target"
    lease_target.touch()
    Path(f"{unsafe_path}.lock").symlink_to(lease_target)
    with pytest.raises(ValueError, match="ownership lease must be a regular file"):
        ExecutionJournal(unsafe_path)
