import datetime
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cleanup_resources


def make_instance(
    *,
    instance_id="ocid1.instance.oc1..example",
    display_name="example-instance",
    lifecycle_state="RUNNING",
    hours_old=48,
    freeform_tags=None,
):
    created = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_old)
    return SimpleNamespace(
        id=instance_id,
        display_name=display_name,
        lifecycle_state=lifecycle_state,
        time_created=created,
        freeform_tags=freeform_tags or {},
    )


def config(**overrides):
    values = {
        "compartment_id": "compartment",
        "threshold_hours": 24,
        "dry_run": True,
        "action": "stop",
    }
    values.update(overrides)
    return cleanup_resources.JanitorConfig(**values)


class PolicyEvaluationTests(unittest.TestCase):
    def test_untagged_instance_is_not_managed_by_default(self):
        decision = cleanup_resources.evaluate_instance(
            make_instance(), datetime.datetime.now(datetime.timezone.utc), config()
        )
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, "required_tag_missing")

    def test_expired_opted_in_instance_is_eligible(self):
        decision = cleanup_resources.evaluate_instance(
            make_instance(freeform_tags={"JanitorManaged": "true"}),
            datetime.datetime.now(datetime.timezone.utc),
            config(),
        )
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.reason, "expired")
        self.assertEqual(decision.ttl_hours, 24.0)

    def test_tag_matching_is_case_insensitive(self):
        decision = cleanup_resources.evaluate_instance(
            make_instance(freeform_tags={"JanitorManaged": "TRUE"}),
            datetime.datetime.now(datetime.timezone.utc),
            config(),
        )
        self.assertTrue(decision.eligible)

    def test_exclusion_tag_wins(self):
        decision = cleanup_resources.evaluate_instance(
            make_instance(freeform_tags={"JanitorManaged": "true", "DoNotCleanup": "true"}),
            datetime.datetime.now(datetime.timezone.utc),
            config(),
        )
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, "excluded_tag_present")

    def test_per_resource_ttl_overrides_default(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        instance = make_instance(hours_old=10, freeform_tags={"JanitorManaged": "true", "TTLHours": "8"})
        decision = cleanup_resources.evaluate_instance(instance, now, config(threshold_hours=24))
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.ttl_hours, 8.0)

    def test_resource_with_longer_ttl_is_not_expired(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        instance = make_instance(hours_old=48, freeform_tags={"JanitorManaged": "true", "TTLHours": "72"})
        decision = cleanup_resources.evaluate_instance(instance, now, config())
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, "not_expired")

    def test_invalid_ttl_fails_closed(self):
        decision = cleanup_resources.evaluate_instance(
            make_instance(freeform_tags={"JanitorManaged": "true", "TTLHours": "soon"}),
            datetime.datetime.now(datetime.timezone.utc),
            config(),
        )
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, "invalid_ttl_tag")

    def test_absolute_expiration_overrides_ttl(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        expires_at = (now - datetime.timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        instance = make_instance(
            hours_old=1,
            freeform_tags={"JanitorManaged": "true", "TTLHours": "999", "ExpiresAt": expires_at},
        )
        decision = cleanup_resources.evaluate_instance(instance, now, config())
        self.assertTrue(decision.eligible)
        self.assertIsNone(decision.ttl_hours)
        self.assertEqual(decision.expires_at, expires_at)

    def test_invalid_absolute_expiration_fails_closed(self):
        decision = cleanup_resources.evaluate_instance(
            make_instance(freeform_tags={"JanitorManaged": "true", "ExpiresAt": "next-tuesday"}),
            datetime.datetime.now(datetime.timezone.utc),
            config(),
        )
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, "invalid_expiration_tag")

    def test_stop_only_targets_running_instances(self):
        decision = cleanup_resources.evaluate_instance(
            make_instance(lifecycle_state="STOPPED", freeform_tags={"JanitorManaged": "true"}),
            datetime.datetime.now(datetime.timezone.utc),
            config(action="stop"),
        )
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, "lifecycle_state_not_actionable")

    def test_terminate_requires_stopped_instance_by_default(self):
        running = cleanup_resources.evaluate_instance(
            make_instance(lifecycle_state="RUNNING", freeform_tags={"JanitorManaged": "true"}),
            datetime.datetime.now(datetime.timezone.utc),
            config(action="terminate"),
        )
        stopped = cleanup_resources.evaluate_instance(
            make_instance(lifecycle_state="STOPPED", freeform_tags={"JanitorManaged": "true"}),
            datetime.datetime.now(datetime.timezone.utc),
            config(action="terminate"),
        )
        self.assertFalse(running.eligible)
        self.assertEqual(running.reason, "lifecycle_state_not_actionable")
        self.assertTrue(stopped.eligible)

    def test_direct_termination_can_be_explicitly_enabled(self):
        decision = cleanup_resources.evaluate_instance(
            make_instance(lifecycle_state="RUNNING", freeform_tags={"JanitorManaged": "true"}),
            datetime.datetime.now(datetime.timezone.utc),
            config(action="terminate", termination_requires_stopped=False),
        )
        self.assertTrue(decision.eligible)

    def test_report_can_surface_expired_stopped_instances(self):
        decision = cleanup_resources.evaluate_instance(
            make_instance(lifecycle_state="STOPPED", freeform_tags={"JanitorManaged": "true"}),
            datetime.datetime.now(datetime.timezone.utc),
            config(action="report"),
        )
        self.assertTrue(decision.eligible)

    def test_compatibility_wrapper_remains_available(self):
        instance = make_instance(freeform_tags={"AutoCleanup": "true"})
        result = cleanup_resources.should_terminate_instance(
            instance,
            datetime.datetime.now(datetime.timezone.utc),
            threshold_hours=24,
            required_tag_key="AutoCleanup",
            required_tag_value="true",
        )
        self.assertTrue(result)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.original_env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.original_env)))
        for key in list(os.environ):
            if key.startswith("OCI_"):
                del os.environ[key]

    def test_safe_defaults(self):
        os.environ["OCI_COMPARTMENT_ID"] = "compartment"
        loaded = cleanup_resources.load_config()
        self.assertTrue(loaded.dry_run)
        self.assertEqual(loaded.action, "stop")
        self.assertEqual(loaded.max_actions_per_run, 10)
        self.assertEqual(loaded.required_tag_key, "JanitorManaged")
        self.assertEqual(loaded.required_tag_value, "true")
        self.assertFalse(loaded.allow_terminate)
        self.assertTrue(loaded.termination_requires_stopped)

    def test_live_termination_requires_second_interlock(self):
        with self.assertRaisesRegex(ValueError, "allow_terminate=true"):
            cleanup_resources.validate_config(config(action="terminate", dry_run=False, allow_terminate=False))

    def test_live_termination_can_be_explicitly_enabled(self):
        cleanup_resources.validate_config(config(action="terminate", dry_run=False, allow_terminate=True, state_namespace="ns", state_bucket="audit"))

    def test_required_tag_cannot_be_disabled(self):
        with self.assertRaisesRegex(ValueError, "explicit opt-in"):
            cleanup_resources.validate_config(config(required_tag_key=None))

    def test_policy_file_and_overrides(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            policy_path = os.path.join(temp_dir, "policy.json")
            with open(policy_path, "w", encoding="utf-8") as policy_handle:
                json.dump({"compartment_id": "policy", "threshold_hours": 96, "action": "report"}, policy_handle)
            loaded = cleanup_resources.load_config(
                {"policy_file": policy_path, "compartment_id": "override", "threshold_hours": 48}
            )
        self.assertEqual(loaded.compartment_id, "override")
        self.assertEqual(loaded.threshold_hours, 48)
        self.assertEqual(loaded.action, "report")

    def test_new_env_names_are_supported(self):
        os.environ.update(
            {
                "OCI_COMPARTMENT_ID": "compartment",
                "OCI_JANITOR_THRESHOLD_HOURS": "72",
                "OCI_JANITOR_ACTION": "report",
                "OCI_JANITOR_MAX_ACTIONS_PER_RUN": "3",
            }
        )
        loaded = cleanup_resources.load_config()
        self.assertEqual(loaded.threshold_hours, 72)
        self.assertEqual(loaded.action, "report")
        self.assertEqual(loaded.max_actions_per_run, 3)

    def test_legacy_cleanup_env_names_remain_supported(self):
        os.environ.update(
            {
                "OCI_COMPARTMENT_ID": "compartment",
                "OCI_CLEANUP_THRESHOLD_HOURS": "36",
                "OCI_CLEANUP_ACTION": "report",
                "OCI_CLEANUP_MAX_TERMINATIONS_PER_RUN": "4",
            }
        )
        loaded = cleanup_resources.load_config()
        self.assertEqual(loaded.threshold_hours, 36)
        self.assertEqual(loaded.action, "report")
        self.assertEqual(loaded.max_actions_per_run, 4)

    def test_invalid_boolean_is_rejected(self):
        with self.assertRaises(ValueError):
            cleanup_resources.parse_bool("maybe")


class ActionTests(unittest.TestCase):
    def test_dry_run_never_mutates(self):
        client = Mock()
        cleanup_resources.execute_cleanup_action(client, "ocid1", action="terminate", dry_run=True)
        client.terminate_instance.assert_not_called()
        client.instance_action.assert_not_called()

    def test_report_never_mutates_even_if_dry_run_is_false(self):
        client = Mock()
        cleanup_resources.execute_cleanup_action(client, "ocid1", action="report", dry_run=False)
        client.terminate_instance.assert_not_called()
        client.instance_action.assert_not_called()

    def test_stop_uses_instance_action(self):
        client = Mock()
        cleanup_resources.execute_cleanup_action(client, "ocid1", action="stop", dry_run=False, if_match="v1", retry_token="token")
        client.instance_action.assert_called_once_with("ocid1", "STOP", if_match="v1", opc_retry_token="token")

    def test_terminate_uses_terminate_instance(self):
        client = Mock()
        cleanup_resources.execute_cleanup_action(client, "ocid1", action="terminate", dry_run=False, if_match="v1")
        client.terminate_instance.assert_called_once_with("ocid1", if_match="v1")


class RunJanitorTests(unittest.TestCase):
    @patch("cleanup_resources.execute_cleanup_action")
    @patch("cleanup_resources.get_cleanup_decisions")
    @patch("cleanup_resources.get_compute_client")
    def test_action_cap_limits_blast_radius(self, mock_client, mock_decisions, mock_action):
        mock_client.return_value = Mock()
        mock_decisions.return_value = [
            cleanup_resources.CleanupDecision("ocid1", "one", "RUNNING", True, "expired"),
            cleanup_resources.CleanupDecision("ocid2", "two", "RUNNING", True, "expired"),
            cleanup_resources.CleanupDecision("ocid3", "three", "RUNNING", True, "expired"),
        ]
        report = cleanup_resources.run_janitor(config(max_actions_per_run=2))
        self.assertEqual(report["candidate_count"], 3)
        self.assertEqual(report["selected_count"], 2)
        self.assertTrue(report["limited"])
        mock_action.assert_not_called()
        self.assertEqual(len(report["outcomes"]), 2)

    @patch("cleanup_resources.execute_cleanup_action")
    @patch("cleanup_resources.get_cleanup_decisions")
    @patch("cleanup_resources.get_compute_client")
    def test_report_contains_reason_summary(self, mock_client, mock_decisions, mock_action):
        mock_client.return_value = Mock()
        mock_decisions.return_value = [
            cleanup_resources.CleanupDecision("ocid1", "one", "RUNNING", True, "expired"),
            cleanup_resources.CleanupDecision("ocid2", "two", "RUNNING", False, "required_tag_missing"),
            cleanup_resources.CleanupDecision("ocid3", "three", "RUNNING", False, "required_tag_missing"),
        ]
        report = cleanup_resources.run_janitor(config(action="report"))
        self.assertEqual(report["scanned_count"], 3)
        self.assertEqual(report["reason_counts"], {"expired": 1, "required_tag_missing": 2})
        self.assertEqual(report["resource_types"], ["compute_instance"])

    @patch("cleanup_resources.run_janitor")
    def test_main_returns_zero_on_success_even_when_resources_are_selected(self, mock_run):
        mock_run.return_value = {
            "scanned_count": 3,
            "candidate_count": 2,
            "selected_count": 2,
            "action": "stop",
            "dry_run": False,
        }
        self.assertEqual(cleanup_resources.main(), 0)
        mock_run.return_value["status"] = "partial"
        self.assertEqual(cleanup_resources.main(), 1)

    @patch("cleanup_resources.execute_cleanup_action")
    @patch("cleanup_resources.get_cleanup_decisions")
    @patch("cleanup_resources.get_compute_client")
    def test_structured_report_is_written(self, mock_client, mock_decisions, mock_action):
        mock_client.return_value = Mock()
        mock_decisions.return_value = [
            cleanup_resources.CleanupDecision("ocid1", "one", "RUNNING", True, "expired")
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "report.json")
            report = cleanup_resources.run_janitor(config(action="report", report_file=path))
            with open(path, "r", encoding="utf-8") as handle:
                written = json.load(handle)
        self.assertEqual(written["schema_version"], 2)
        self.assertEqual(written["candidate_count"], report["candidate_count"])


if __name__ == "__main__":
    unittest.main()
