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
    lifecycle_state="RUNNING",
    hours_old=48,
    freeform_tags=None,
):
    created = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_old)
    return SimpleNamespace(
        id="ocid1.instance.oc1..example",
        display_name="example-instance",
        lifecycle_state=lifecycle_state,
        time_created=created,
        freeform_tags=freeform_tags or {},
    )


class ShouldTerminateInstanceTests(unittest.TestCase):
    def test_evaluate_instance_records_skip_reason(self):
        instance = make_instance(lifecycle_state="STOPPED")
        now = datetime.datetime.now(datetime.timezone.utc)

        decision = cleanup_resources.evaluate_instance(instance, now, threshold_hours=24)

        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, "lifecycle_state_not_running")

    def test_rejects_non_running_instance(self):
        instance = make_instance(lifecycle_state="STOPPED")
        now = datetime.datetime.now(datetime.timezone.utc)

        result = cleanup_resources.should_terminate_instance(instance, now, threshold_hours=24)

        self.assertFalse(result)

    def test_rejects_instance_without_required_tag(self):
        instance = make_instance(freeform_tags={"Name": "demo"})
        now = datetime.datetime.now(datetime.timezone.utc)

        result = cleanup_resources.should_terminate_instance(
            instance,
            now,
            threshold_hours=24,
            required_tag_key="AutoCleanup",
            required_tag_value="true",
        )

        self.assertFalse(result)

    def test_accepts_old_running_instance_with_required_tag(self):
        instance = make_instance(freeform_tags={"AutoCleanup": "true"})
        now = datetime.datetime.now(datetime.timezone.utc)

        result = cleanup_resources.should_terminate_instance(
            instance,
            now,
            threshold_hours=24,
            required_tag_key="AutoCleanup",
            required_tag_value="true",
        )

        self.assertTrue(result)

    def test_rejects_instance_with_exclusion_tag(self):
        instance = make_instance(freeform_tags={"AutoCleanup": "true", "DoNotCleanup": "true"})
        now = datetime.datetime.now(datetime.timezone.utc)

        result = cleanup_resources.should_terminate_instance(
            instance,
            now,
            threshold_hours=24,
            required_tag_key="AutoCleanup",
            required_tag_value="true",
            excluded_tag_key="DoNotCleanup",
            excluded_tag_value="true",
        )

        self.assertFalse(result)


class TerminateInstanceTests(unittest.TestCase):
    def test_dry_run_skips_termination(self):
        compute_client = Mock()

        cleanup_resources.terminate_instance(compute_client, "ocid1.instance.oc1..example", dry_run=True)

        compute_client.terminate_instance.assert_not_called()

    def test_live_mode_terminates_instance(self):
        compute_client = Mock()

        cleanup_resources.terminate_instance(compute_client, "ocid1.instance.oc1..example", dry_run=False)

        compute_client.terminate_instance.assert_called_once_with("ocid1.instance.oc1..example")


class LoadConfigTests(unittest.TestCase):
    def test_load_config_applies_overrides(self):
        original = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(original)))
        os.environ["OCI_COMPARTMENT_ID"] = "env-compartment"

        config = cleanup_resources.load_config(
            {
                "compartment_id": "payload-compartment",
                "threshold_hours": 72,
                "dry_run": False,
                "max_terminations_per_run": 5,
                "report_file": "cleanup-report.json",
                "required_tag_key": "AutoCleanup",
                "required_tag_value": "true",
                "excluded_tag_key": "DoNotCleanup",
                "excluded_tag_value": "true",
                "auth_mode": "resource_principal",
            }
        )

        self.assertEqual(config.compartment_id, "payload-compartment")
        self.assertEqual(config.threshold_hours, 72)
        self.assertFalse(config.dry_run)
        self.assertEqual(config.max_terminations_per_run, 5)
        self.assertEqual(config.report_file, "cleanup-report.json")
        self.assertEqual(config.required_tag_key, "AutoCleanup")
        self.assertEqual(config.required_tag_value, "true")
        self.assertEqual(config.excluded_tag_key, "DoNotCleanup")
        self.assertEqual(config.excluded_tag_value, "true")
        self.assertEqual(config.auth_mode, "resource_principal")

    def test_load_config_reads_policy_file_defaults(self):
        original = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(original)))
        with tempfile.TemporaryDirectory() as temp_dir:
            policy_path = os.path.join(temp_dir, "policy.json")
            with open(policy_path, "w", encoding="utf-8") as policy_handle:
                json.dump(
                    {
                        "compartment_id": "policy-compartment",
                        "threshold_hours": 96,
                        "dry_run": False,
                        "required_tag_key": "AutoCleanup",
                    },
                    policy_handle,
                )

            config = cleanup_resources.load_config({"policy_file": policy_path})

        self.assertEqual(config.compartment_id, "policy-compartment")
        self.assertEqual(config.threshold_hours, 96)
        self.assertFalse(config.dry_run)
        self.assertEqual(config.required_tag_key, "AutoCleanup")

    def test_load_config_prefers_explicit_overrides_over_policy_file(self):
        original = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(original)))
        with tempfile.TemporaryDirectory() as temp_dir:
            policy_path = os.path.join(temp_dir, "policy.json")
            with open(policy_path, "w", encoding="utf-8") as policy_handle:
                json.dump(
                    {
                        "compartment_id": "policy-compartment",
                        "threshold_hours": 96,
                    },
                    policy_handle,
                )

            config = cleanup_resources.load_config(
                {
                    "policy_file": policy_path,
                    "compartment_id": "override-compartment",
                    "threshold_hours": 48,
                }
            )

        self.assertEqual(config.compartment_id, "override-compartment")
        self.assertEqual(config.threshold_hours, 48)


class HandleCleanupTests(unittest.TestCase):
    @patch("cleanup_resources.terminate_instance")
    @patch("cleanup_resources.get_cleanup_decisions")
    @patch("cleanup_resources.get_compute_client")
    def test_handle_cleanup_limits_number_of_terminations(
        self,
        mock_get_compute_client,
        mock_get_cleanup_decisions,
        mock_terminate_instance,
    ):
        mock_get_compute_client.return_value = Mock()
        mock_get_cleanup_decisions.return_value = [
            cleanup_resources.CleanupDecision("ocid1", "instance-1", True, "eligible"),
            cleanup_resources.CleanupDecision("ocid2", "instance-2", True, "eligible"),
            cleanup_resources.CleanupDecision("ocid3", "instance-3", True, "eligible"),
        ]

        config = cleanup_resources.CleanupConfig(
            compartment_id="compartment",
            max_terminations_per_run=2,
        )

        result = cleanup_resources.handle_cleanup(config)

        self.assertEqual(result, 2)
        self.assertEqual(mock_terminate_instance.call_count, 2)

    @patch("cleanup_resources.terminate_instance")
    @patch("cleanup_resources.get_compute_client")
    def test_handle_cleanup_writes_structured_report(
        self,
        mock_get_compute_client,
        mock_terminate_instance,
    ):
        mock_get_compute_client.return_value = Mock()
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = os.path.join(temp_dir, "cleanup-report.json")
            with patch(
                "cleanup_resources.get_cleanup_decisions",
                return_value=[
                    cleanup_resources.CleanupDecision("ocid1", "instance-1", True, "eligible"),
                    cleanup_resources.CleanupDecision("ocid2", "instance-2", False, "required_tag_missing"),
                ],
            ):
                config = cleanup_resources.CleanupConfig(
                    compartment_id="compartment",
                    dry_run=True,
                    report_file=report_path,
                )

                result = cleanup_resources.handle_cleanup(config)

            self.assertEqual(result, 1)
            self.assertEqual(mock_terminate_instance.call_count, 1)
            with open(report_path, "r", encoding="utf-8") as report_handle:
                report = json.load(report_handle)

        self.assertEqual(report["candidate_count"], 1)
        self.assertEqual(report["processed_count"], 1)
        self.assertEqual(report["decisions"][1]["reason"], "required_tag_missing")


if __name__ == "__main__":
    unittest.main()
