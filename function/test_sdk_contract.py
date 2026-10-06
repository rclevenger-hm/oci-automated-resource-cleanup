"""Exercise installed SDK/FDK interfaces without credentials or network calls."""

import json
import unittest
from unittest.mock import Mock, patch

import cleanup_resources as c
import handler
from state_store import ObjectStateStore

try:
    import oci
    from fdk import response
except ModuleNotFoundError:
    oci = None


@unittest.skipIf(oci is None, "Installed-dependency CI lane exercises SDK contracts")
class SDKContractTests(unittest.TestCase):
    def client(self, cls):
        signer = Mock(spec=oci.auth.signers.SecurityTokenSigner)
        return cls({"region": "us-ashburn-1"}, signer=signer, retry_strategy=oci.retry.NoneRetryStrategy())

    def test_compute_conditional_mutation_parameters(self):
        client = self.client(oci.core.ComputeClient)
        with patch.object(client.base_client, "call_api") as call:
            c.execute_cleanup_action(client, "ocid1.instance.example", "stop", False, "etag-v1", "retry-token")
            headers = call.call_args.kwargs["header_params"]
            self.assertEqual(headers["if-match"], "etag-v1")
            self.assertEqual(headers["opc-retry-token"], "retry-token")
            c.execute_cleanup_action(client, "ocid1.instance.example", "terminate", False, "etag-v2")
            self.assertEqual(call.call_args.kwargs["header_params"]["if-match"], "etag-v2")

    def test_object_storage_conditional_parameters(self):
        client = self.client(oci.object_storage.ObjectStorageClient)
        store = ObjectStateStore(client, "ns", "audit", "prefix", "scope", oci.retry.NoneRetryStrategy())
        with patch.object(client.base_client, "call_api", return_value=Mock(headers={"etag": "v1"})) as call:
            store.acquire("run", "2026-10-01T00:00:00Z")
            self.assertEqual(call.call_args.kwargs["header_params"]["if-none-match"], "*")
            store.save({"schema_version": 1})
            self.assertEqual(call.call_args.kwargs["header_params"]["if-none-match"], "*")
            store.save({"schema_version": 1})
            self.assertEqual(call.call_args.kwargs["header_params"]["if-match"], "v1")
            store.release()
            self.assertEqual(call.call_args.kwargs["header_params"]["if-match"], "v1")

    def test_real_fdk_response_contract(self):
        context = Mock()
        with patch.object(handler, "response", response):
            result = handler._build_response(context, {"status": "partial", "run_id": "run"}, 207)
        self.assertEqual(result.status(), 207)
        self.assertEqual(json.loads(result.body())["run_id"], "run")
        self.assertEqual(context.SetResponseHeaders.call_args.args[1], 207)


if __name__ == "__main__":
    unittest.main()
