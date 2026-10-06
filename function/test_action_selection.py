import unittest
from unittest.mock import Mock, call, patch

import cleanup_resources


class ActionSelectionTests(unittest.TestCase):
    @patch("cleanup_resources.execute_cleanup_action")
    @patch("cleanup_resources.get_cleanup_decisions")
    @patch("cleanup_resources.get_compute_client")
    def test_action_cap_prioritizes_oldest_expiration(self, mock_client, mock_decisions, mock_action):
        client = Mock()
        mock_client.return_value = client
        mock_decisions.return_value = [
            cleanup_resources.CleanupDecision(
                "ocid-newer", "newer", "RUNNING", True, "expired", expires_at="2026-09-10T10:00:00Z"
            ),
            cleanup_resources.CleanupDecision(
                "ocid-oldest", "oldest", "RUNNING", True, "expired", expires_at="2026-09-08T10:00:00Z"
            ),
            cleanup_resources.CleanupDecision(
                "ocid-middle", "middle", "RUNNING", True, "expired", expires_at="2026-09-09T10:00:00Z"
            ),
        ]

        report = cleanup_resources.run_janitor(
            cleanup_resources.JanitorConfig(
                compartment_id="compartment",
                action="stop",
                dry_run=True,
                max_actions_per_run=2,
            )
        )

        self.assertEqual(report["candidate_count"], 3)
        self.assertEqual(report["selected_count"], 2)
        self.assertTrue(report["limited"])
        mock_action.assert_not_called()
        self.assertEqual(
            [item["resource_id"] for item in report["outcomes"]],
            ["ocid-oldest", "ocid-middle"],
        )

    def test_sort_key_is_stable_for_missing_expiration(self):
        first = cleanup_resources.CleanupDecision("ocid-b", "b", "RUNNING", True, "expired")
        second = cleanup_resources.CleanupDecision("ocid-a", "a", "RUNNING", True, "expired")
        ordered = sorted([first, second], key=cleanup_resources._candidate_sort_key)
        self.assertEqual([decision.resource_id for decision in ordered], ["ocid-a", "ocid-b"])


if __name__ == "__main__":
    unittest.main()
