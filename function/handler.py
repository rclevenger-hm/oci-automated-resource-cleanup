import io
import json
import logging
from typing import Any, Dict, Optional

import cleanup_resources
from state_store import RunLocked, StateError

try:
    from fdk import response
except ModuleNotFoundError:  # pragma: no cover - only needed in OCI Functions runtime
    response = None


LOGGER = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 64 * 1024
PROTECTED_RUNTIME_KEYS = {
    "auth_mode",
    "config_path",
    "config_profile",
    "policy_file",
    "report_file",
}


def _read_payload(data: io.BytesIO) -> Dict[str, Any]:
    if data is None:
        return {}

    raw = data.getvalue()
    if not raw:
        return {}
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError(f"Request body exceeds {MAX_REQUEST_BYTES} byte limit")

    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object")

    protected = sorted(PROTECTED_RUNTIME_KEYS.intersection(payload))
    if protected:
        raise ValueError(
            "Request body cannot override function runtime settings: " + ", ".join(protected)
        )
    return payload


def _request_id(ctx) -> Optional[str]:
    call_id = getattr(ctx, "CallID", None)
    if not callable(call_id):
        return None
    try:
        value = call_id()
    except Exception:  # pragma: no cover - runtime context implementations vary
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _correlated_body(body: Dict[str, Any], request_id: Optional[str]) -> Dict[str, Any]:
    if not request_id:
        return body
    return {**body, "request_id": request_id}


def _event(event: str, request_id: Optional[str], **fields: Any) -> str:
    payload = {"event": event, **fields}
    if request_id:
        payload["request_id"] = request_id
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _build_response(ctx, body: Dict[str, Any], status_code: int = 200):
    payload = json.dumps(body)
    if response is None:
        return {"status_code": status_code, "body": payload}

    return response.Response(
        ctx,
        response_data=payload,
        headers={"Content-Type": "application/json"},
        status_code=status_code,
    )


def handler(ctx, data: io.BytesIO = None):
    logging.basicConfig(level="INFO")
    request_id = _request_id(ctx)

    try:
        payload = _read_payload(data)
        config = cleanup_resources.load_request_config(payload)
        report = cleanup_resources.run_janitor(config)
        status = report.get("status", "completed")
        LOGGER.info(
            _event(
                "janitor.run." + status,
                request_id,
                scanned_count=report["scanned_count"],
                candidate_count=report["candidate_count"],
                selected_count=report["selected_count"],
                action=report["action"],
                dry_run=report["dry_run"],
                limited=report["limited"],
                reason_counts=report["reason_counts"],
                run_id=report.get("run_id"),
                outcome_counts=report.get("outcome_counts", {}),
                outcome_reason_counts=report.get("outcome_reason_counts", {}),
            )
        )
        return _build_response(
            ctx,
            _correlated_body(
                {
                    "status": "ok" if status == "completed" else status,
                    "run_id": report.get("run_id"),
                    "outcome_counts": report.get("outcome_counts", {}),
                    "outcome_reason_counts": report.get("outcome_reason_counts", {}),
                    "action": report["action"],
                    "dry_run": report["dry_run"],
                    "compartment_id": report["compartment_id"],
                    "scanned_count": report["scanned_count"],
                    "candidate_count": report["candidate_count"],
                    "selected_count": report["selected_count"],
                    "limited": report["limited"],
                    "reason_counts": report["reason_counts"],
                },
                request_id,
            ),
            status_code={"completed": 200, "partial": 207, "failed": 503}[status],
        )
    except (RunLocked, StateError) as exc:
        category = "concurrency" if isinstance(exc, RunLocked) else "persistence"
        LOGGER.exception(_event("janitor.run.failed", request_id, failure_category=category))
        return _build_response(
            ctx,
            _correlated_body({"status": "error", "message": category + " safeguard blocked execution"}, request_id),
            409 if isinstance(exc, RunLocked) else 503,
        )
    except (KeyError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        LOGGER.error(
            _event(
                "janitor.run.failed",
                request_id,
                failure_category="configuration",
                message=str(exc),
            )
        )
        return _build_response(
            ctx,
            _correlated_body({"status": "error", "message": str(exc)}, request_id),
            400,
        )
    except Exception:  # pragma: no cover - exercised in runtime integration
        LOGGER.exception(
            _event(
                "janitor.run.failed",
                request_id,
                failure_category="runtime",
            )
        )
        return _build_response(
            ctx,
            _correlated_body({"status": "error", "message": "internal error"}, request_id),
            500,
        )
