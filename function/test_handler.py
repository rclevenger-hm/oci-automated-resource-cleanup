import io
import json
import unittest
from unittest.mock import patch

import handler
from state_store import RunLocked, StateError


class FunctionContext:
    def __init__(self, call_id):
        self._call_id = call_id

    def CallID(self):
        return self._call_id


class HandlerTests(unittest.TestCase):
    def setUp(self):
        fallback = patch.object(handler, "response", None)
        fallback.start()
        self.addCleanup(fallback.stop)

    def test_rejects_non_object_json(self):
        result = handler.handler(None, io.BytesIO(b"[]"))
        self.assertEqual(result["status_code"], 400)

    def test_rejects_oversized_payload_before_configuration(self):
        result = handler.handler(None, io.BytesIO(b"x" * (handler.MAX_REQUEST_BYTES + 1)))
        self.assertEqual(result["status_code"], 400)
        body = json.loads(result["body"])
        self.assertIn("exceeds", body["message"])

    def test_rejects_request_level_runtime_overrides(self):
        for key in sorted(handler.PROTECTED_RUNTIME_KEYS):
            with self.subTest(key=key):
                result = handler.handler(None, io.BytesIO(json.dumps({key: "override"}).encode("utf-8")))
                self.assertEqual(result["status_code"], 400)
                body = json.loads(result["body"])
                self.assertIn(key, body["message"])
                self.assertIn("runtime settings", body["message"])

    @patch.object(handler.LOGGER, "info")
    @patch("handler.cleanup_resources.run_janitor")
    @patch("handler.cleanup_resources.load_request_config")
    def test_returns_run_summary_and_logs_completion(self, mock_load_config, mock_run_janitor, mock_log_info):
        mock_load_config.return_value = object()
        mock_run_janitor.return_value = {
            "action": "report",
            "dry_run": True,
            "compartment_id": "compartment",
            "scanned_count": 10,
            "candidate_count": 2,
            "selected_count": 2,
            "limited": False,
            "reason_counts": {"expired": 2, "required_tag_missing": 8},
        }
        result = handler.handler(FunctionContext("call-123"), io.BytesIO(b"{}"))
        self.assertEqual(result["status_code"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["candidate_count"], 2)
        self.assertEqual(body["reason_counts"]["required_tag_missing"], 8)
        self.assertEqual(body["request_id"], "call-123")

        event = json.loads(mock_log_info.call_args.args[0])
        self.assertEqual(event["event"], "janitor.run.completed")
        self.assertEqual(event["request_id"], "call-123")
        self.assertEqual(event["scanned_count"], 10)
        self.assertEqual(event["candidate_count"], 2)
        self.assertEqual(event["selected_count"], 2)
        self.assertEqual(event["action"], "report")
        self.assertTrue(event["dry_run"])
        self.assertFalse(event["limited"])
        self.assertEqual(event["reason_counts"]["required_tag_missing"], 8)

    @patch.object(handler.LOGGER, "error")
    def test_client_error_includes_request_id_and_failure_category(self, mock_log_error):
        result = handler.handler(FunctionContext("call-invalid"), io.BytesIO(b"[]"))
        self.assertEqual(result["status_code"], 400)
        body = json.loads(result["body"])
        self.assertEqual(body["request_id"], "call-invalid")

        event = json.loads(mock_log_error.call_args.args[0])
        self.assertEqual(event["event"], "janitor.run.failed")
        self.assertEqual(event["request_id"], "call-invalid")
        self.assertEqual(event["failure_category"], "configuration")
        self.assertIn("JSON object", event["message"])

    @patch.object(handler.LOGGER, "exception")
    @patch("handler.cleanup_resources.run_janitor", side_effect=RuntimeError("internal tenancy detail"))
    @patch("handler.cleanup_resources.load_request_config", return_value=object())
    def test_internal_failure_does_not_leak_exception_detail(
        self,
        mock_load_config,
        mock_run_janitor,
        mock_log_exception,
    ):
        result = handler.handler(FunctionContext("call-error"), io.BytesIO(b"{}"))
        self.assertEqual(result["status_code"], 500)
        body = json.loads(result["body"])
        self.assertEqual(body, {"status": "error", "message": "internal error", "request_id": "call-error"})

        event = json.loads(mock_log_exception.call_args.args[0])
        self.assertEqual(event, {
            "event": "janitor.run.failed",
            "failure_category": "runtime",
            "request_id": "call-error",
        })
        self.assertNotIn("internal tenancy detail", mock_log_exception.call_args.args[0])

    @patch("handler.cleanup_resources.load_request_config", return_value=object())
    def test_partial_and_aborted_runs_have_distinct_statuses(self, mock_config):
        summary = {
            "run_id": "run-123", "action": "stop", "dry_run": False,
            "compartment_id": "compartment", "scanned_count": 2,
            "candidate_count": 2, "selected_count": 2, "limited": False,
            "reason_counts": {"expired": 2},
            "outcome_counts": {"submitted": 1, "failed": 1},
        }
        for status, code in (("partial", 207), ("failed", 503)):
            with self.subTest(status=status), patch(
                "handler.cleanup_resources.run_janitor", return_value={**summary, "status": status}
            ):
                result = handler.handler(None, io.BytesIO(b"{}"))
                self.assertEqual(result["status_code"], code)
                body = json.loads(result["body"])
                self.assertEqual(body["run_id"], "run-123")
                self.assertEqual(body["outcome_counts"]["submitted"], 1)

    @patch("handler.cleanup_resources.load_request_config", return_value=object())
    @patch.object(handler.LOGGER, "exception")
    def test_lock_and_storage_errors_are_bounded(self, mock_log, mock_config):
        for error, code in ((RunLocked("private lock path"), 409), (StateError("private bucket"), 503)):
            with self.subTest(code=code), patch("handler.cleanup_resources.run_janitor", side_effect=error):
                result = handler.handler(None, io.BytesIO(b"{}"))
                self.assertEqual(result["status_code"], code)
                self.assertNotIn("private", result["body"])


if __name__ == "__main__":
    unittest.main()
