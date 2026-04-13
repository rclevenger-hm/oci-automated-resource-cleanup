import datetime
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

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
                "required_tag_key": "AutoCleanup",
                "required_tag_value": "true",
                "auth_mode": "resource_principal",
            }
        )

        self.assertEqual(config.compartment_id, "payload-compartment")
        self.assertEqual(config.threshold_hours, 72)
        self.assertFalse(config.dry_run)
        self.assertEqual(config.required_tag_key, "AutoCleanup")
        self.assertEqual(config.required_tag_value, "true")
        self.assertEqual(config.auth_mode, "resource_principal")


if __name__ == "__main__":
    unittest.main()
