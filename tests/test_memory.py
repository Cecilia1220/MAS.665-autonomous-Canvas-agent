"""Deterministic, offline coverage for local accepted-write accounting."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memory import MemoryState, MemoryStore


NOW = 1_000_000.0
PENDING_ACCEPTED_ACTION = {
    "target_entry_id": "root-entry",
    "reply_text": "A short, verified-safe discussion reply.",
    "created_at": str(NOW - 60),
    "post_attempted": True,
    "post_counted": True,
    "recovery_attempts": 0,
}


class ParticipationBudgetTests(unittest.TestCase):
    def test_two_accepted_posts_in_window_do_not_trigger_budget(self) -> None:
        state = MemoryState(
            successful_write_timestamps=[NOW - 8 * 3600, NOW - 60],
        )

        self.assertEqual(state.writes_in_last_eight_hours(NOW), 2)
        self.assertFalse(state.writes_in_last_eight_hours(NOW) >= 3)

    def test_three_accepted_posts_in_window_trigger_budget(self) -> None:
        state = MemoryState(
            successful_write_timestamps=[NOW - 8 * 3600, NOW - 4 * 3600, NOW - 60],
        )

        self.assertEqual(state.writes_in_last_eight_hours(NOW), 3)
        self.assertTrue(state.writes_in_last_eight_hours(NOW) >= 3)

    def test_accepted_unverified_post_counts_toward_budget(self) -> None:
        state = MemoryState(
            successful_write_timestamps=[NOW - 4 * 3600, NOW - 2 * 3600, NOW - 60],
            pending_action=dict(PENDING_ACCEPTED_ACTION),
        )

        self.assertTrue(state.pending_action is not None)
        self.assertEqual(state.writes_in_last_eight_hours(NOW), 3)
        self.assertTrue(state.writes_in_last_eight_hours(NOW) >= 3)

    def test_reconciliation_does_not_erase_accepted_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.json")
            state = MemoryState(
                successful_write_timestamps=[NOW - 60],
                pending_action=dict(PENDING_ACCEPTED_ACTION),
            )
            store.save(state)

            # This mirrors successful GET-only reconciliation: the pending
            # action clears, but accepted-write history remains intact.
            reconciled = store.load()
            reconciled.record_own("reply-id", "reply")
            reconciled.pending_action = None
            store.save(reconciled)

            recovered = store.load()
            self.assertIsNone(recovered.pending_action)
            self.assertEqual(recovered.successful_write_timestamps, [NOW - 60])
            self.assertEqual(recovered.writes_in_last_eight_hours(NOW), 1)


if __name__ == "__main__":
    unittest.main()
