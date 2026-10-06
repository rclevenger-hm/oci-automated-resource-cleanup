import copy
import datetime
import io
import json
import os
import threading
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cleanup_resources as c
import handler
from state_store import ObjectStateStore, RunLocked, StateError

NOW = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc)


class ServiceFailure(Exception):
    def __init__(self, status, code="Rejected"):
        self.status = status
        self.code = code


class FakeObjects:
    """An atomic conditional object API, shared by independent store instances."""
    def __init__(self):
        self.objects = {}
        self.counter = 0
        self.mutex = threading.Lock()
        self.writes = []
        self.fail_write = lambda name, value: False

    def put_object(self, namespace, bucket, name, body, **kwargs):
        value = json.loads(body)
        with self.mutex:
            if self.fail_write(name, value):
                raise ServiceFailure(503)
            old = self.objects.get(name)
            if kwargs.get("if_none_match") == "*" and old:
                raise ServiceFailure(412)
            if kwargs.get("if_match") and (not old or kwargs["if_match"] != old[0]):
                raise ServiceFailure(412)
            self.counter += 1
            etag = str(self.counter)
            self.objects[name] = (etag, bytes(body))
            self.writes.append((name, value))
            return SimpleNamespace(headers={"etag": etag})

    def get_object(self, namespace, bucket, name, **kwargs):
        with self.mutex:
            if name not in self.objects:
                raise ServiceFailure(404, "ObjectNotFound")
            etag, data = self.objects[name]
            return SimpleNamespace(headers={"etag": etag}, data=SimpleNamespace(content=data))

    def head_object(self, namespace, bucket, name, **kwargs):
        response = self.get_object(namespace, bucket, name)
        if kwargs.get("if_match") != response.headers["etag"]:
            raise ServiceFailure(412)
        return response

    def delete_object(self, namespace, bucket, name, **kwargs):
        with self.mutex:
            if name not in self.objects or self.objects[name][0] != kwargs.get("if_match"):
                raise ServiceFailure(412)
            del self.objects[name]

    def state(self):
        return json.loads(next(value[1] for key, value in self.objects.items() if key.endswith("/state.json")))

    def reports(self):
        return [json.loads(value[1]) for key, value in self.objects.items() if "/runs/" in key]

    def locked(self):
        return any(key.endswith("/lock.json") for key in self.objects)


def instance(resource_id="one", state="RUNNING", **tags):
    return SimpleNamespace(
        id=resource_id, display_name=resource_id, compartment_id="compartment",
        time_created=NOW - datetime.timedelta(days=2), lifecycle_state=state,
        freeform_tags={"JanitorManaged": "true", **tags},
    )


class FakeCompute:
    def __init__(self, *instances):
        self.instances = {item.id: item for item in instances}
        self.etags = {item.id: "v1" for item in instances}
        self.calls = []
        self.before_action = lambda resource_id: None
        self.failure = {}
        self.base_client = SimpleNamespace(endpoint="https://iaas.example.test")
        self.on_get = lambda resource_id: None

    def get_instance(self, resource_id):
        self.on_get(resource_id)
        return SimpleNamespace(data=copy.deepcopy(self.instances[resource_id]), headers={"etag": self.etags[resource_id]})

    def _action(self, resource_id, action, kwargs):
        self.before_action(resource_id)
        self.calls.append((resource_id, action, kwargs))
        if kwargs.get("if_match") != self.etags[resource_id]:
            raise ServiceFailure(412)
        if resource_id in self.failure:
            raise self.failure[resource_id]
        self.instances[resource_id].lifecycle_state = "STOPPED" if action == "STOP" else "TERMINATED"
        self.etags[resource_id] += "-changed"
        return SimpleNamespace(headers={"opc-request-id": "oci-request"}, status=202)

    def instance_action(self, resource_id, action, **kwargs):
        return self._action(resource_id, action, kwargs)

    def terminate_instance(self, resource_id, **kwargs):
        return self._action(resource_id, "TERMINATE", kwargs)


class RequestPolicyTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"OCI_COMPARTMENT_ID": "compartment"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_rejects_all_safety_escalations(self):
        payloads = [
            {"dry_run": False}, {"action": "terminate"}, {"action": []},
            {"compartment_id": "other"}, {"max_actions_per_run": None},
            {"max_actions_per_run": 11}, {"max_actions_per_run": 0},
            {"max_actions_per_run": 1.5}, {"max_actions_per_run": True},
            {"max_terminations_per_run": 100},
            {"excluded_tag_key": None}, {"required_tag_value": None},
            {"allow_terminate": True}, {"termination_requires_stopped": False},
            {"threshold_hours": 1}, {"threshold_hours": None},
            {"dry_run": "false"}, {"state_bucket": "other"},
            {"state_prefix": "other"}, {"max_actions_per_window": 100},
            {"termination_grace_hours": 0}, {"typo": "ignored?"},
        ]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                c.load_request_config(payload)

    def test_accepts_narrower_policy_and_report(self):
        config = c.load_request_config({
            "action": "report", "dry_run": True, "threshold_hours": 72,
            "max_actions_per_run": 2, "compartment_id": "compartment",
        })
        self.assertEqual((config.action, config.max_actions_per_run, config.threshold_hours), ("report", 2, 72))

    def test_live_operator_can_be_forced_to_dry_run_but_not_retargeted(self):
        os.environ.update({
            "OCI_JANITOR_DRY_RUN": "false", "OCI_JANITOR_ACTION": "terminate",
            "OCI_JANITOR_ALLOW_TERMINATE": "true", "OCI_JANITOR_STATE_NAMESPACE": "ns",
            "OCI_JANITOR_STATE_BUCKET": "bucket",
        })
        self.assertTrue(c.load_request_config({"dry_run": True}).dry_run)
        with self.assertRaises(ValueError):
            c.load_request_config({"action": "stop"})

    def test_handler_rejects_escalation_without_constructing_cloud_client(self):
        with patch.object(handler, "response", None), patch.object(c, "get_compute_client") as client:
            result = handler.handler(None, io.BytesIO(b'{"dry_run": false}'))
        self.assertEqual(result["status_code"], 400)
        client.assert_not_called()

    def test_nonfinite_and_extreme_ttls_are_individual_rejections(self):
        for ttl in ("NaN", "Infinity", "-Infinity", "1e100", "-1", "0", "87601", "soon"):
            with self.subTest(ttl=ttl):
                result = c.evaluate_instance(instance(TTLHours=ttl), NOW, c.JanitorConfig("compartment"))
                self.assertFalse(result.eligible)
                self.assertEqual(result.reason, "invalid_ttl_tag")
        with patch.object(c, "get_current_time", return_value=NOW), patch.object(
            c, "list_instances", return_value=[instance("bad", TTLHours="NaN"), instance("good")]
        ):
            decisions = c.get_cleanup_decisions(Mock(), c.JanitorConfig("compartment"))
        self.assertEqual([item.eligible for item in decisions], [False, True])

    def test_datetime_overflow_fails_closed(self):
        resource = instance(TTLHours="87600")
        resource.time_created = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
        self.assertEqual(c.evaluate_instance(resource, NOW, c.JanitorConfig("compartment")).reason, "invalid_ttl_tag")

    def test_live_execution_requires_state_and_cannot_disable_stop_gate(self):
        with self.assertRaisesRegex(ValueError, "durable"):
            c.validate_config(c.JanitorConfig("compartment", dry_run=False))
        with self.assertRaisesRegex(ValueError, "stop history"):
            c.validate_config(c.JanitorConfig(
                "compartment", dry_run=False, action="terminate", allow_terminate=True,
                termination_requires_stopped=False, state_bucket="audit", state_namespace="ns",
            ))


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.objects = FakeObjects()
        self.client = FakeCompute(instance())
        self.now = NOW
        self.config = c.JanitorConfig(
            "compartment", dry_run=False, state_namespace="ns", state_bucket="audit",
            termination_grace_hours=1,
        )
        self.patches = [
            patch.object(c, "get_current_time", side_effect=lambda: self.now),
            patch.object(c, "get_compute_client", side_effect=lambda config: self.client),
            patch.object(c, "list_instances", side_effect=lambda *args: copy.deepcopy(list(self.client.instances.values()))),
            patch.object(c, "get_state_store", side_effect=lambda *args: self.store()),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        quiet = patch("execution.LOGGER.exception")
        quiet.start()
        self.addCleanup(quiet.stop)

    def store(self):
        return ObjectStateStore(self.objects, "ns", "audit", "janitor/v1", "test-scope", object())

    def run_cleanup(self, **overrides):
        return c.run_janitor(replace(self.config, **overrides))

    def test_plan_and_reserved_intent_exist_before_first_mutation(self):
        def check(resource_id):
            self.assertEqual(self.objects.reports()[0]["outcomes"][0]["status"], "pending")
            state = self.objects.state()
            self.assertEqual(len(state["reservations"]), 1)
            self.assertEqual(state["resources"][resource_id]["pending"]["action"], "stop")
        self.client.before_action = check
        report = self.run_cleanup()
        self.assertEqual(report["outcomes"][0]["status"], "submitted")
        self.assertFalse(self.objects.locked())
        self.assertEqual(self.client.calls[0][2]["if_match"], "v1")

    def test_resource_failure_preserves_partial_results_and_continues(self):
        self.client = FakeCompute(instance("a"), instance("b"), instance("c"))
        self.client.failure["b"] = ServiceFailure(409)
        report = self.run_cleanup()
        self.assertEqual(report["status"], "partial")
        self.assertEqual([item["status"] for item in report["outcomes"]], ["submitted", "failed", "submitted"])
        self.assertEqual(self.objects.reports()[0], report)
        self.assertEqual(len(self.objects.state()["reservations"]), 3)

    def test_partial_report_is_also_written_to_local_mirror(self):
        self.client = FakeCompute(instance("a"), instance("b"))
        self.client.failure["b"] = ServiceFailure(409)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "report.json")
            report = self.run_cleanup(report_file=path)
            with open(path, encoding="utf-8") as stream:
                saved = json.load(stream)
        self.assertEqual(saved, report)
        self.assertEqual(saved["outcome_counts"], {"submitted": 1, "failed": 1})

    def test_unwritable_local_mirror_stops_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(StateError):
                self.run_cleanup(report_file=os.path.join(directory, "missing", "report.json"))
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.objects.reports()[0]["status"], "planned")

    def test_ambiguous_action_aborts_and_is_not_retried_next_run(self):
        self.client = FakeCompute(instance("a"), instance("b"), instance("c"))
        self.client.failure["b"] = TimeoutError()
        report = self.run_cleanup()
        self.assertEqual(report["status"], "failed")
        self.assertEqual([item["status"] for item in report["outcomes"]], ["submitted", "unknown", "not_attempted"])
        self.assertIn("pending", self.objects.state()["resources"]["b"])
        del self.client.failure["b"]
        retry = self.run_cleanup()
        self.assertEqual(retry["outcomes"][0]["reason"], "unresolved_previous_action")
        self.assertEqual([item[0] for item in self.client.calls], ["a", "b", "c"])

    def test_authorization_failure_aborts_remaining_actions(self):
        self.client = FakeCompute(instance("a"), instance("b"))
        self.client.failure["a"] = ServiceFailure(403)
        report = self.run_cleanup()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(report["outcomes"][1]["status"], "not_attempted")

    def test_revalidation_honors_new_protection_tag(self):
        self.client.on_get = lambda rid: self.client.instances[rid].freeform_tags.update(DoNotCleanup="true")
        report = self.run_cleanup()
        self.assertEqual(report["outcomes"][0]["reason"], "excluded_tag_present")
        self.assertEqual(self.client.calls, [])

    def test_revalidation_honors_extended_expiration_and_scope_change(self):
        self.client.on_get = lambda rid: self.client.instances[rid].freeform_tags.update(TTLHours="72")
        self.assertEqual(self.run_cleanup()["outcomes"][0]["reason"], "not_expired")
        self.client.instances["one"].freeform_tags.pop("TTLHours")
        self.client.on_get = lambda rid: setattr(self.client.instances[rid], "compartment_id", "other")
        self.assertEqual(self.run_cleanup()["outcomes"][0]["reason"], "resource_scope_changed")
        self.assertEqual(self.client.calls, [])

    def test_change_after_refresh_is_rejected_conditionally(self):
        self.client.before_action = lambda rid: self.client.etags.update({rid: "v2"})
        report = self.run_cleanup()
        self.assertEqual(report["status"], "partial")
        self.assertEqual(self.client.instances["one"].lifecycle_state, "RUNNING")
        self.assertNotIn("stop", self.objects.state()["resources"]["one"])

    def test_missing_fresh_etag_fails_closed(self):
        self.client.etags["one"] = ""
        with self.assertRaises(StateError):
            self.run_cleanup()
        self.assertEqual(self.client.calls, [])
        self.assertTrue(self.objects.locked())

    def test_unrelated_stopped_instance_never_qualifies_for_termination(self):
        self.client = FakeCompute(instance(state="STOPPED"))
        report = self.run_cleanup(action="terminate", allow_terminate=True)
        self.assertEqual(report["outcomes"][0]["reason"], "no_janitor_stop_history")
        self.assertEqual(self.client.calls, [])

    def test_skipped_resources_do_not_starve_later_eligible_resources(self):
        self.client = FakeCompute(instance("z-managed"))
        self.run_cleanup()
        self.run_cleanup(action="terminate", allow_terminate=True)
        self.now += datetime.timedelta(hours=1)
        unrelated = instance("a-unrelated", state="STOPPED")
        self.client.instances[unrelated.id] = unrelated
        self.client.etags[unrelated.id] = "unrelated-etag"
        report = self.run_cleanup(action="terminate", allow_terminate=True, max_actions_per_run=1)
        self.assertEqual(report["outcomes"][0]["reason"], "no_janitor_stop_history")
        self.assertEqual(report["outcomes"][1]["status"], "submitted")
        self.assertEqual(report["selected_count"], 1)

    def test_grace_starts_when_stopped_is_observed_and_survives_runs(self):
        self.run_cleanup()
        self.now += datetime.timedelta(days=1)
        first = self.run_cleanup(action="terminate", allow_terminate=True)
        self.assertEqual(first["outcomes"][0]["reason"], "termination_grace_period")
        self.now += datetime.timedelta(minutes=59)
        self.assertEqual(
            self.run_cleanup(action="terminate", allow_terminate=True)["outcomes"][0]["reason"],
            "termination_grace_period",
        )
        self.now += datetime.timedelta(minutes=1)
        report = self.run_cleanup(action="terminate", allow_terminate=True)
        self.assertEqual(report["outcomes"][0]["status"], "submitted")
        self.assertEqual(self.client.instances["one"].lifecycle_state, "TERMINATED")

    def test_changed_stopped_etag_restarts_grace(self):
        self.run_cleanup()
        self.run_cleanup(action="terminate", allow_terminate=True)
        self.now += datetime.timedelta(hours=2)
        self.client.etags["one"] = "externally-updated"
        report = self.run_cleanup(action="terminate", allow_terminate=True)
        self.assertEqual(report["outcomes"][0]["reason"], "termination_grace_period")
        self.assertEqual(len(self.client.calls), 1)

    def test_accepted_stop_is_not_repeated_during_eventual_consistency(self):
        self.run_cleanup()
        self.client.instances["one"].lifecycle_state = "RUNNING"
        report = self.run_cleanup()
        self.assertEqual(report["outcomes"][0]["reason"], "stop_already_submitted")
        self.assertEqual(len(self.client.calls), 1)

    def test_shared_rolling_budget_bounds_separate_runs(self):
        self.client = FakeCompute(instance("a"), instance("b"), instance("c"))
        self.config = replace(self.config, max_actions_per_run=1, max_actions_per_window=2)
        self.run_cleanup()
        self.run_cleanup()
        third = self.run_cleanup()
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(third["outcomes"][0]["reason"], "shared_action_budget_exhausted")
        self.now += datetime.timedelta(seconds=3600)
        self.run_cleanup()
        self.assertEqual(len(self.client.calls), 3)

    def test_stop_and_terminate_share_budget(self):
        self.config = replace(self.config, max_actions_per_window=1, action_window_seconds=86400)
        self.run_cleanup()
        self.run_cleanup(action="terminate", allow_terminate=True)
        self.now += datetime.timedelta(hours=1)
        report = self.run_cleanup(action="terminate", allow_terminate=True)
        self.assertEqual(report["outcomes"][0]["reason"], "shared_action_budget_exhausted")

    def test_inconsistent_budget_configuration_fails_closed(self):
        self.run_cleanup()
        with self.assertRaisesRegex(StateError, "budget policy differs"):
            self.run_cleanup(max_actions_per_window=100)

    def test_plan_write_failure_prevents_all_mutation(self):
        self.objects.fail_write = lambda name, value: "/runs/" in name
        with self.assertRaises(StateError):
            self.run_cleanup()
        self.assertEqual(self.client.calls, [])
        self.assertTrue(self.objects.locked())

    def test_budget_reservation_failure_prevents_mutation(self):
        self.objects.fail_write = lambda name, value: bool(value.get("reservations"))
        with self.assertRaises(StateError):
            self.run_cleanup()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.objects.reports()[0]["outcomes"][0]["status"], "planned")

    def test_result_persistence_failure_keeps_intent_and_blocks_next_run(self):
        self.objects.fail_write = lambda name, value: any(
            record.get("stop") for record in value.get("resources", {}).values()
        )
        with self.assertRaises(StateError):
            self.run_cleanup()
        self.assertEqual(len(self.client.calls), 1)
        self.assertIn("pending", self.objects.state()["resources"]["one"])
        with self.assertRaises(RunLocked):
            self.run_cleanup()
        self.assertEqual(len(self.client.calls), 1)

    def test_overlapping_run_is_blocked_before_action(self):
        owner = self.store()
        owner.acquire("other-run", c.format_timestamp(NOW))
        with self.assertRaises(RunLocked):
            self.run_cleanup()
        self.assertEqual(self.client.calls, [])
        owner.release()
        self.run_cleanup()
        self.assertEqual(len(self.client.calls), 1)

    def test_dry_run_needs_no_storage_and_does_not_consume_budget(self):
        report = self.run_cleanup(dry_run=True, state_namespace=None, state_bucket=None)
        self.assertEqual(report["outcomes"][0]["status"], "dry_run")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.objects.objects, {})


class ObjectStoreTests(unittest.TestCase):
    def test_simultaneous_lock_acquisition_has_exactly_one_winner(self):
        objects = FakeObjects()
        barrier = threading.Barrier(2)
        outcomes = []

        def acquire(run_id):
            store = ObjectStateStore(objects, "ns", "bucket", "prefix", "scope", object())
            barrier.wait()
            try:
                store.acquire(run_id, c.format_timestamp(NOW))
                outcomes.append("owned")
            except RunLocked:
                outcomes.append("blocked")
        threads = [threading.Thread(target=acquire, args=(str(i),)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertCountEqual(outcomes, ["owned", "blocked"])

    def test_replaced_lock_cannot_be_released_by_old_worker(self):
        objects = FakeObjects()
        store = ObjectStateStore(objects, "ns", "bucket", "prefix", "scope", object())
        store.acquire("run", c.format_timestamp(NOW))
        lock = store.prefix + "/lock.json"
        objects.objects[lock] = ("new-owner", b"{}")
        with self.assertRaises(StateError):
            store.release()
        self.assertIn(lock, objects.objects)

    def test_ambiguous_not_found_never_initializes_empty_state(self):
        objects = FakeObjects()
        store = ObjectStateStore(objects, "ns", "bucket", "prefix", "scope", object())
        store.acquire("run", c.format_timestamp(NOW))
        original = objects.get_object
        def read(ns, bucket, name, **kwargs):
            if name.endswith("/state.json"):
                raise ServiceFailure(404, "NotAuthorizedOrNotFound")
            return original(ns, bucket, name, **kwargs)
        objects.get_object = read
        with self.assertRaises(StateError):
            store.load()

    def test_conditional_state_write_detects_external_change(self):
        objects = FakeObjects()
        store = ObjectStateStore(objects, "ns", "bucket", "prefix", "scope", object())
        store.acquire("run", c.format_timestamp(NOW))
        state = store.load()
        store.save(state)
        name = store.prefix + "/state.json"
        objects.objects[name] = ("new-version", objects.objects[name][1])
        with self.assertRaises(StateError):
            store.save(state)


if __name__ == "__main__":
    unittest.main()
