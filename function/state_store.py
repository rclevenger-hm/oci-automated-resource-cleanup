"""Conditional Object Storage persistence for live janitor runs.

Locks never expire automatically: a paused worker cannot be fenced out of OCI
Compute by an Object Storage lease. Crash recovery therefore requires an operator
to stop the old worker before releasing its lock.
"""

import hashlib
import json


class StateError(RuntimeError):
    pass


class RunLocked(StateError):
    pass


def etag_of(response):
    value = response.headers.get("etag")
    if not isinstance(value, str) or not value:
        raise StateError("Missing ETag; conditional operation cannot proceed")
    return value


class ObjectStateStore:
    def __init__(self, client, namespace, bucket, prefix, scope, retry_strategy):
        self.client = client
        self.namespace = namespace
        self.bucket = bucket
        self.scope = scope
        scope_id = hashlib.sha256(scope.encode("utf-8")).hexdigest()
        self.prefix = f"{prefix.rstrip('/')}/{scope_id}"
        self.retry_strategy = retry_strategy
        self.lock_etag = None
        self.state_etag = None
        self.report_etags = {}

    def _put(self, name, value, etag=None):
        condition = {"if_match": etag} if etag else {"if_none_match": "*"}
        try:
            result = self.client.put_object(
                self.namespace, self.bucket, f"{self.prefix}/{name}",
                json.dumps(value, sort_keys=True, allow_nan=False).encode("utf-8"),
                content_type="application/json",
                retry_strategy=self.retry_strategy,
                **condition,
            )
            return etag_of(result)
        except Exception as exc:
            raise StateError(f"Cannot persist {name}; execution stopped") from exc

    def acquire(self, run_id, now):
        try:
            self.lock_etag = self._put(
                "lock.json", {"run_id": run_id, "acquired_at": now, "scope": self.scope}
            )
        except StateError as exc:
            if getattr(exc.__cause__, "status", None) == 412:
                raise RunLocked("Another run owns this scope; inspect its lock before recovery") from exc
            raise

    def assert_owned(self):
        if not self.lock_etag:
            raise StateError("Run does not own the scope lock")
        try:
            self.client.head_object(
                self.namespace, self.bucket, f"{self.prefix}/lock.json",
                if_match=self.lock_etag, retry_strategy=self.retry_strategy,
            )
        except Exception as exc:
            raise StateError("Run lock was lost; execution stopped") from exc

    def load(self):
        self.assert_owned()
        try:
            result = self.client.get_object(
                self.namespace, self.bucket, f"{self.prefix}/state.json",
                retry_strategy=self.retry_strategy,
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 404 and getattr(exc, "code", None) == "ObjectNotFound":
                return {"schema_version": 1, "scope": self.scope, "resources": {}, "reservations": []}
            raise StateError("Cannot read shared lifecycle state") from exc
        try:
            self.state_etag = etag_of(result)
            state = json.loads(result.data.content)
            if (
                state["schema_version"] != 1 or state["scope"] != self.scope
                or not isinstance(state["resources"], dict)
                or not isinstance(state["reservations"], list)
            ):
                raise ValueError("Invalid state schema")
            return state
        except Exception as exc:
            raise StateError("Invalid shared lifecycle state") from exc

    def save(self, state):
        self.assert_owned()
        self.state_etag = self._put("state.json", state, self.state_etag)

    def write_report(self, report):
        self.assert_owned()
        run_id = report["run_id"]
        self.report_etags[run_id] = self._put(
            f"runs/{run_id}.json", report, self.report_etags.get(run_id)
        )

    def release(self):
        self.assert_owned()
        try:
            self.client.delete_object(
                self.namespace, self.bucket, f"{self.prefix}/lock.json",
                if_match=self.lock_etag, retry_strategy=self.retry_strategy,
            )
        except Exception as exc:
            raise StateError("Cannot release run lock; inspect before the next run") from exc
        self.lock_etag = None
