import io
import json
import logging
from typing import Any, Dict

import cleanup_resources

try:
    from fdk import response
except ModuleNotFoundError:  # pragma: no cover - only needed in OCI Functions runtime
    response = None


LOGGER = logging.getLogger(__name__)


def _read_payload(data: io.BytesIO) -> Dict[str, Any]:
    if data is None:
        return {}

    raw = data.getvalue()
    if not raw:
        return {}

    return json.loads(raw.decode("utf-8"))


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

    try:
        payload = _read_payload(data)
        config = cleanup_resources.load_config(payload)
        terminated_count = cleanup_resources.handle_cleanup(config)
        return _build_response(
            ctx,
            {
                "status": "ok",
                "dry_run": config.dry_run,
                "compartment_id": config.compartment_id,
                "threshold_hours": config.threshold_hours,
                "required_tag_key": config.required_tag_key,
                "required_tag_value": config.required_tag_value,
                "candidate_count": terminated_count,
            },
        )
    except KeyError as exc:
        LOGGER.error("Missing required configuration: %s", exc)
        return _build_response(ctx, {"status": "error", "message": f"Missing configuration: {exc}"}, 400)
    except Exception as exc:  # pragma: no cover - exercised in runtime integration
        LOGGER.exception("Function invocation failed")
        return _build_response(ctx, {"status": "error", "message": str(exc)}, 500)
