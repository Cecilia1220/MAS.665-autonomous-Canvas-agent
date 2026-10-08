"""Offline tests for the incremental-value score gate."""

from __future__ import annotations

import unittest

from decision import Action, INSUFFICIENT_VALUE_REASON, decision_from_payload


class IncrementalValueGateTests(unittest.TestCase):
    def test_scores_zero_through_three_are_standardized_skips(self) -> None:
        for score in range(4):
            with self.subTest(score=score):
                decision = decision_from_payload(
                    {
                        "action": "REPLY",
                        "reason": "The model's detailed rationale.",
                        "reply_text": "A plausible reply that must not be posted.",
                        "target_entry_id": "thread-1",
                        "incremental_value_score": score,
                    },
                    {"thread-1"},
                )
                self.assertEqual(decision.action, Action.SKIP)
                self.assertEqual(decision.reason, INSUFFICIENT_VALUE_REASON)
                self.assertEqual(decision.model_rationale, "The model's detailed rationale.")

    def test_scores_four_and_five_can_reply(self) -> None:
        for score in (4, 5):
            with self.subTest(score=score):
                decision = decision_from_payload(
                    {
                        "action": "REPLY",
                        "reason": "This adds a concrete, testable implementation detail.",
                        "reply_text": "A concise, substantive course-related reply.",
                        "target_entry_id": "thread-1",
                        "incremental_value_score": score,
                    },
                    {"thread-1"},
                )
                self.assertEqual(decision.action, Action.REPLY)
                self.assertEqual(decision.incremental_value_score, score)


if __name__ == "__main__":
    unittest.main()
