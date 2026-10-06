import datetime
import json
import logging
import math
import os
import tempfile
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable, Mapping, Optional

from state_store import ObjectStateStore

try:
    import oci
except ModuleNotFoundError:  # pragma: no cover - exercised in local test environments without OCI SDK
    oci = None


LOGGER = logging.getLogger(__name__)

DEFAULT_REQUIRED_TAG_KEY = "JanitorManaged"
DEFAULT_REQUIRED_TAG_VALUE = "true"
DEFAULT_EXCLUDED_TAG_KEY = "DoNotCleanup"
DEFAULT_EXCLUDED_TAG_VALUE = "true"
DEFAULT_TTL_TAG_KEY = "TTLHours"
DEFAULT_EXPIRES_AT_TAG_KEY = "ExpiresAt"
SUPPORTED_ACTIONS = {"report", "stop", "terminate"}
MAX_TTL_HOURS = 87600  # Ten years; also bound datetime arithmetic.


@dataclass
class JanitorConfig:
    compartment_id: str
    threshold_hours: int = 24
    dry_run: bool = True
    action: str = "stop"
    max_actions_per_run: Optional[int] = 10
    report_file: Optional[str] = None
    required_tag_key: Optional[str] = DEFAULT_REQUIRED_TAG_KEY
    required_tag_value: Optional[str] = DEFAULT_REQUIRED_TAG_VALUE
    excluded_tag_key: Optional[str] = DEFAULT_EXCLUDED_TAG_KEY
    excluded_tag_value: Optional[str] = DEFAULT_EXCLUDED_TAG_VALUE
    ttl_tag_key: Optional[str] = DEFAULT_TTL_TAG_KEY
    expires_at_tag_key: Optional[str] = DEFAULT_EXPIRES_AT_TAG_KEY
    allow_terminate: bool = False
    termination_requires_stopped: bool = True
    auth_mode: str = "auto"
    config_path: Optional[str] = None
    config_profile: str = "DEFAULT"
    state_namespace: Optional[str] = None
    state_bucket: Optional[str] = None
    state_prefix: str = "janitor/v1"
    termination_grace_hours: float = 24
    max_actions_per_window: int = 10
    action_window_seconds: int = 3600


# Backward-compatible import name for callers using the pre-janitor class name.
CleanupConfig = JanitorConfig


@dataclass
class CleanupDecision:
    resource_id: str
    display_name: str
    lifecycle_state: str
    eligible: bool
    reason: str
    age_hours: Optional[float] = None
    ttl_hours: Optional[float] = None
    expires_at: Optional[str] = None
    resource_type: str = "compute_instance"

    @property
    def instance_id(self) -> str:
        """Compatibility alias for the original compute-only decision shape."""
        return self.resource_id


def parse_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def load_policy_file(path: str) -> Mapping[str, Any]:
    with open(path, "r", encoding="utf-8") as policy_handle:
        policy = json.load(policy_handle)
    if not isinstance(policy, dict):
        raise ValueError("Policy file must contain a JSON object")
    return policy


def _value(
    overrides: Mapping[str, Any],
    policy: Mapping[str, Any],
    key: str,
    env_names: tuple[str, ...],
    default: Any = None,
) -> Any:
    if key in overrides:
        return overrides[key]
    for env_name in env_names:
        if env_name in os.environ:
            return os.environ[env_name]
    if key in policy:
        return policy[key]
    return default


def load_config(overrides: Optional[Mapping[str, Any]] = None) -> JanitorConfig:
    override_values = dict(overrides or {})
    policy_file = override_values.get("policy_file") or os.environ.get("OCI_JANITOR_POLICY_FILE") or os.environ.get(
        "OCI_CLEANUP_POLICY_FILE"
    )
    policy_values = dict(load_policy_file(policy_file)) if policy_file else {}

    compartment_id = _value(override_values, policy_values, "compartment_id", ("OCI_COMPARTMENT_ID",))
    if not compartment_id:
        raise KeyError("OCI_COMPARTMENT_ID")

    legacy_max = _value(
        override_values,
        policy_values,
        "max_terminations_per_run",
        ("OCI_CLEANUP_MAX_TERMINATIONS_PER_RUN",),
    )
    max_actions = _value(
        override_values,
        policy_values,
        "max_actions_per_run",
        ("OCI_JANITOR_MAX_ACTIONS_PER_RUN",),
        legacy_max if legacy_max is not None else 10,
    )

    config = JanitorConfig(
        compartment_id=str(compartment_id),
        threshold_hours=_integer(
            _value(
                override_values,
                policy_values,
                "threshold_hours",
                ("OCI_JANITOR_THRESHOLD_HOURS", "OCI_CLEANUP_THRESHOLD_HOURS"),
                24,
            )
        ),
        dry_run=parse_bool(
            _value(
                override_values,
                policy_values,
                "dry_run",
                ("OCI_JANITOR_DRY_RUN", "OCI_CLEANUP_DRY_RUN"),
                True,
            )
        ),
        action=str(
            _value(
                override_values,
                policy_values,
                "action",
                ("OCI_JANITOR_ACTION", "OCI_CLEANUP_ACTION"),
                "stop",
            )
        ).lower(),
        max_actions_per_run=_integer(max_actions) if max_actions is not None else None,
        report_file=_value(
            override_values,
            policy_values,
            "report_file",
            ("OCI_JANITOR_REPORT_FILE", "OCI_CLEANUP_REPORT_FILE"),
        ),
        required_tag_key=_value(
            override_values,
            policy_values,
            "required_tag_key",
            ("OCI_JANITOR_REQUIRED_TAG_KEY", "OCI_CLEANUP_REQUIRED_TAG_KEY"),
            DEFAULT_REQUIRED_TAG_KEY,
        ),
        required_tag_value=_value(
            override_values,
            policy_values,
            "required_tag_value",
            ("OCI_JANITOR_REQUIRED_TAG_VALUE", "OCI_CLEANUP_REQUIRED_TAG_VALUE"),
            DEFAULT_REQUIRED_TAG_VALUE,
        ),
        excluded_tag_key=_value(
            override_values,
            policy_values,
            "excluded_tag_key",
            ("OCI_JANITOR_EXCLUDED_TAG_KEY", "OCI_CLEANUP_EXCLUDED_TAG_KEY"),
            DEFAULT_EXCLUDED_TAG_KEY,
        ),
        excluded_tag_value=_value(
            override_values,
            policy_values,
            "excluded_tag_value",
            ("OCI_JANITOR_EXCLUDED_TAG_VALUE", "OCI_CLEANUP_EXCLUDED_TAG_VALUE"),
            DEFAULT_EXCLUDED_TAG_VALUE,
        ),
        ttl_tag_key=_value(
            override_values,
            policy_values,
            "ttl_tag_key",
            ("OCI_JANITOR_TTL_TAG_KEY",),
            DEFAULT_TTL_TAG_KEY,
        ),
        expires_at_tag_key=_value(
            override_values,
            policy_values,
            "expires_at_tag_key",
            ("OCI_JANITOR_EXPIRES_AT_TAG_KEY",),
            DEFAULT_EXPIRES_AT_TAG_KEY,
        ),
        allow_terminate=parse_bool(
            _value(
                override_values,
                policy_values,
                "allow_terminate",
                ("OCI_JANITOR_ALLOW_TERMINATE",),
                False,
            ),
            default=False,
        ),
        termination_requires_stopped=parse_bool(
            _value(
                override_values,
                policy_values,
                "termination_requires_stopped",
                ("OCI_JANITOR_TERMINATION_REQUIRES_STOPPED",),
                True,
            ),
            default=True,
        ),
        auth_mode=str(_value(override_values, policy_values, "auth_mode", ("OCI_AUTH_MODE",), "auto")),
        config_path=_value(override_values, policy_values, "config_path", ("OCI_CONFIG_FILE",)),
        config_profile=str(
            _value(override_values, policy_values, "config_profile", ("OCI_CONFIG_PROFILE",), "DEFAULT")
        ),
        state_namespace=_value(override_values, policy_values, "state_namespace", ("OCI_JANITOR_STATE_NAMESPACE",)),
        state_bucket=_value(override_values, policy_values, "state_bucket", ("OCI_JANITOR_STATE_BUCKET",)),
        state_prefix=_value(override_values, policy_values, "state_prefix", ("OCI_JANITOR_STATE_PREFIX",), "janitor/v1"),
        termination_grace_hours=_number(_value(
            override_values, policy_values, "termination_grace_hours", ("OCI_JANITOR_TERMINATION_GRACE_HOURS",), 24
        )),
        max_actions_per_window=_integer(_value(
            override_values, policy_values, "max_actions_per_window", ("OCI_JANITOR_MAX_ACTIONS_PER_WINDOW",), 10
        )),
        action_window_seconds=_integer(_value(
            override_values, policy_values, "action_window_seconds", ("OCI_JANITOR_ACTION_WINDOW_SECONDS",), 3600
        )),
    )
    validate_config(config)
    return config


def load_config_from_env() -> JanitorConfig:
    return load_config()


def _number(value):
    if isinstance(value, bool):
        raise ValueError("Numeric configuration cannot be a boolean")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid numeric configuration") from exc
    if not math.isfinite(number):
        raise ValueError("Numeric configuration must be finite")
    return number


def _integer(value):
    number = _number(value)
    if not number.is_integer():
        raise ValueError("Integer configuration cannot have a fractional part")
    return int(number)


def load_request_config(payload: Mapping[str, Any]) -> JanitorConfig:
    """Requests may narrow deployment policy, never redefine its safety boundary."""
    operator = load_config()
    allowed = {"compartment_id", "action", "dry_run", "threshold_hours", "max_actions_per_run"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError("Request cannot override operator policy: " + ", ".join(unknown))
    values = {}
    if "compartment_id" in payload and payload["compartment_id"] != operator.compartment_id:
        raise ValueError("Request compartment_id must match the operator compartment")
    if "action" in payload:
        if payload["action"] not in (operator.action, "report"):
            raise ValueError("Request action may only match operator policy or use report")
        values["action"] = payload["action"]
    if "dry_run" in payload:
        if not isinstance(payload["dry_run"], bool):
            raise ValueError("Request dry_run must be a boolean")
        if operator.dry_run and not payload["dry_run"]:
            raise ValueError("Request cannot disable operator dry_run")
        values["dry_run"] = payload["dry_run"]
    if "threshold_hours" in payload:
        threshold = _integer(payload["threshold_hours"])
        if threshold < operator.threshold_hours:
            raise ValueError("Request cannot shorten the operator TTL")
        values["threshold_hours"] = threshold
    if "max_actions_per_run" in payload:
        cap = _integer(payload["max_actions_per_run"])
        if cap <= 0 or (operator.max_actions_per_run is not None and cap > operator.max_actions_per_run):
            raise ValueError("Request action cap must be positive and no higher than operator policy")
        values["max_actions_per_run"] = cap
    result = replace(operator, **values)
    validate_config(result)
    return result


def validate_config(config: JanitorConfig) -> None:
    for name in ("dry_run", "allow_terminate", "termination_requires_stopped"):
        if not isinstance(getattr(config, name), bool):
            raise ValueError(f"{name} must be a boolean")
    if not config.compartment_id:
        raise ValueError("compartment_id is required")
    if not 0 < _number(config.threshold_hours) <= MAX_TTL_HOURS:
        raise ValueError(f"threshold_hours must be between zero and {MAX_TTL_HOURS}")
    if config.action not in SUPPORTED_ACTIONS:
        raise ValueError(f"Unsupported janitor action: {config.action}")
    if config.max_actions_per_run is not None and (
        _integer(config.max_actions_per_run) != config.max_actions_per_run or config.max_actions_per_run <= 0
    ):
        raise ValueError("max_actions_per_run must be greater than zero when set")
    if not config.required_tag_key:
        raise ValueError("required_tag_key cannot be disabled; janitor management must be explicit opt-in")
    if not config.dry_run and config.action == "terminate" and not config.allow_terminate:
        raise ValueError(
            "Live termination requires allow_terminate=true in addition to dry_run=false and action=terminate"
        )
    if config.auth_mode not in {"auto", "config", "resource_principal"}:
        raise ValueError("Unsupported auth_mode")
    if bool(config.state_namespace) != bool(config.state_bucket):
        raise ValueError("state_namespace and state_bucket must be configured together")
    if not isinstance(config.state_prefix, str) or not config.state_prefix.strip("/"):
        raise ValueError("state_prefix must not be empty")
    if not 0 < _number(config.termination_grace_hours) <= MAX_TTL_HOURS:
        raise ValueError("termination_grace_hours must be positive and at most ten years")
    for name in ("max_actions_per_window", "action_window_seconds"):
        value = getattr(config, name)
        if _integer(value) != value or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if not config.dry_run and config.action != "report":
        if not config.state_bucket or not config.state_namespace:
            raise ValueError("Live actions require durable state_namespace and state_bucket")
        if config.max_actions_per_run is None:
            raise ValueError("Live actions require a bounded max_actions_per_run")
        if config.action == "terminate" and not config.termination_requires_stopped:
            raise ValueError("Live termination requires janitor stop history and a stopped instance")


def require_oci_sdk() -> Any:
    if oci is None:
        raise RuntimeError("The OCI Python SDK is not installed. Run 'pip install -r function/requirements.txt'.")
    return oci


def get_compute_client(config: JanitorConfig):
    return _get_client(config, "compute")


def _get_client(config: JanitorConfig, service: str):
    sdk = require_oci_sdk()
    client_class = sdk.core.ComputeClient if service == "compute" else sdk.object_storage.ObjectStorageClient
    auth_mode = config.auth_mode.lower()

    if auth_mode == "resource_principal":
        signer = sdk.auth.signers.get_resource_principals_signer()
        return client_class({}, signer=signer, retry_strategy=sdk.retry.NoneRetryStrategy())

    try:
        if config.config_path:
            oci_config = sdk.config.from_file(config.config_path, config.config_profile)
        else:
            oci_config = sdk.config.from_file(profile_name=config.config_profile)
        return client_class(oci_config, retry_strategy=sdk.retry.NoneRetryStrategy())
    except Exception:
        if auth_mode == "config":
            raise

        LOGGER.info("Falling back to OCI resource principal signer")
        signer = sdk.auth.signers.get_resource_principals_signer()
        return client_class({}, signer=signer, retry_strategy=sdk.retry.NoneRetryStrategy())


def get_state_store(config, compute_client):
    sdk = require_oci_sdk()
    scope = compute_client.base_client.endpoint.rstrip("/") + "\n" + config.compartment_id
    return ObjectStateStore(
        _get_client(config, "object_storage"), config.state_namespace, config.state_bucket,
        config.state_prefix, scope, sdk.retry.NoneRetryStrategy(),
    )


def get_current_time() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def normalize_timestamp(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def format_timestamp(value: datetime.datetime) -> str:
    return normalize_timestamp(value).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> datetime.datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def list_instances(compute_client, compartment_id: str) -> Iterable:
    sdk = require_oci_sdk()
    response = sdk.pagination.list_call_get_all_results(
        compute_client.list_instances,
        compartment_id=compartment_id,
    )
    return response.data


def _freeform_tags(instance) -> Mapping[str, Any]:
    return getattr(instance, "freeform_tags", {}) or {}


def has_required_tag(instance, required_tag_key: Optional[str], required_tag_value: Optional[str]) -> bool:
    if not required_tag_key:
        return False
    tags = _freeform_tags(instance)
    if required_tag_key not in tags:
        return False
    if required_tag_value is None:
        return True
    return str(tags.get(required_tag_key)).lower() == str(required_tag_value).lower()


def has_excluded_tag(instance, excluded_tag_key: Optional[str], excluded_tag_value: Optional[str]) -> bool:
    if not excluded_tag_key:
        return False
    tags = _freeform_tags(instance)
    if excluded_tag_key not in tags:
        return False
    if excluded_tag_value is None:
        return True
    return str(tags.get(excluded_tag_key)).lower() == str(excluded_tag_value).lower()


def _allowed_lifecycle_states(config: JanitorConfig) -> set[str]:
    if config.action == "stop":
        return {"RUNNING"}
    if config.action == "terminate" and config.termination_requires_stopped:
        return {"STOPPED"}
    return {"RUNNING", "STOPPED"}


def evaluate_instance(instance, now: datetime.datetime, config: JanitorConfig) -> CleanupDecision:
    resource_id = instance.id
    display_name = getattr(instance, "display_name", resource_id)
    lifecycle_state = str(getattr(instance, "lifecycle_state", "UNKNOWN"))
    launch_time = normalize_timestamp(instance.time_created)
    age_hours = round((normalize_timestamp(now) - launch_time).total_seconds() / 3600, 3)

    base = {
        "resource_id": resource_id,
        "display_name": display_name,
        "lifecycle_state": lifecycle_state,
        "age_hours": age_hours,
    }

    if lifecycle_state not in _allowed_lifecycle_states(config):
        return CleanupDecision(**base, eligible=False, reason="lifecycle_state_not_actionable")

    if not has_required_tag(instance, config.required_tag_key, config.required_tag_value):
        return CleanupDecision(**base, eligible=False, reason="required_tag_missing")

    if has_excluded_tag(instance, config.excluded_tag_key, config.excluded_tag_value):
        return CleanupDecision(**base, eligible=False, reason="excluded_tag_present")

    tags = _freeform_tags(instance)
    ttl_hours: Optional[float] = float(config.threshold_hours)
    expires_at: Optional[datetime.datetime] = None

    if config.expires_at_tag_key and config.expires_at_tag_key in tags:
        try:
            expires_at = parse_timestamp(tags[config.expires_at_tag_key])
        except (TypeError, ValueError, OverflowError):
            return CleanupDecision(**base, eligible=False, reason="invalid_expiration_tag")
    elif config.ttl_tag_key and config.ttl_tag_key in tags:
        try:
            ttl_hours = float(tags[config.ttl_tag_key])
        except (TypeError, ValueError, OverflowError):
            return CleanupDecision(**base, eligible=False, reason="invalid_ttl_tag")
        if not math.isfinite(ttl_hours) or not 0 < ttl_hours <= MAX_TTL_HOURS:
            return CleanupDecision(**base, eligible=False, reason="invalid_ttl_tag")
        try:
            expires_at = launch_time + datetime.timedelta(hours=ttl_hours)
        except (ValueError, OverflowError):
            return CleanupDecision(**base, eligible=False, reason="invalid_ttl_tag")
    else:
        try:
            expires_at = launch_time + datetime.timedelta(hours=ttl_hours)
        except (ValueError, OverflowError):
            return CleanupDecision(**base, eligible=False, reason="invalid_ttl_tag")

    decision_values = {
        **base,
        "ttl_hours": ttl_hours if not (config.expires_at_tag_key and config.expires_at_tag_key in tags) else None,
        "expires_at": format_timestamp(expires_at),
    }

    if normalize_timestamp(now) <= expires_at:
        return CleanupDecision(**decision_values, eligible=False, reason="not_expired")

    return CleanupDecision(**decision_values, eligible=True, reason="expired")


def should_cleanup_instance(instance, now: datetime.datetime, config: JanitorConfig) -> bool:
    return evaluate_instance(instance, now, config).eligible


def should_terminate_instance(
    instance,
    now: datetime.datetime,
    threshold_hours: int,
    required_tag_key: Optional[str] = DEFAULT_REQUIRED_TAG_KEY,
    required_tag_value: Optional[str] = DEFAULT_REQUIRED_TAG_VALUE,
    excluded_tag_key: Optional[str] = DEFAULT_EXCLUDED_TAG_KEY,
    excluded_tag_value: Optional[str] = DEFAULT_EXCLUDED_TAG_VALUE,
) -> bool:
    """Compatibility wrapper for the original public helper."""
    config = JanitorConfig(
        compartment_id="compatibility-wrapper",
        threshold_hours=threshold_hours,
        action="terminate",
        required_tag_key=required_tag_key,
        required_tag_value=required_tag_value,
        excluded_tag_key=excluded_tag_key,
        excluded_tag_value=excluded_tag_value,
        termination_requires_stopped=False,
    )
    return should_cleanup_instance(instance, now, config)


def get_cleanup_decisions(compute_client, config: JanitorConfig) -> list[CleanupDecision]:
    now = get_current_time()
    return [evaluate_instance(instance, now, config) for instance in list_instances(compute_client, config.compartment_id)]


def write_cleanup_report(path: str, report: Mapping[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    descriptor, temporary = tempfile.mkstemp(prefix=".janitor-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as report_handle:
            json.dump(report, report_handle, indent=2, sort_keys=True, allow_nan=False)
            report_handle.write("\n")
            report_handle.flush()
            os.fsync(report_handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def execute_cleanup_action(
    compute_client,
    resource_id: str,
    action: str = "stop",
    dry_run: bool = True,
    if_match: Optional[str] = None,
    retry_token: Optional[str] = None,
):
    if dry_run or action == "report":
        LOGGER.info("No mutation performed: action=%s dry_run=%s resource=%s", action, dry_run, resource_id)
        return

    if action == "stop":
        if not if_match:
            raise ValueError("Live actions require a fresh resource ETag")
        return compute_client.instance_action(resource_id, "STOP", if_match=if_match, opc_retry_token=retry_token)

    if action == "terminate":
        if not if_match:
            raise ValueError("Live actions require a fresh resource ETag")
        return compute_client.terminate_instance(resource_id, if_match=if_match)

    raise ValueError(f"Unsupported janitor action: {action}")


def _candidate_sort_key(decision: CleanupDecision) -> tuple[bool, str, str]:
    return (decision.expires_at is None, decision.expires_at or "", decision.resource_id)


def run_janitor(config: Optional[JanitorConfig] = None) -> dict[str, Any]:
    active_config = config or load_config_from_env()
    validate_config(active_config)
    compute_client = get_compute_client(active_config)
    decisions = get_cleanup_decisions(compute_client, active_config)
    candidates = sorted(
        (decision for decision in decisions if decision.eligible),
        key=_candidate_sort_key,
    )
    candidate_count = len(candidates)
    live = not active_config.dry_run and active_config.action != "report"

    selected = candidates
    if not live and active_config.max_actions_per_run is not None:
        selected = candidates[: active_config.max_actions_per_run]
        if candidate_count > len(selected):
            LOGGER.warning(
                "Limiting janitor actions to %s of %s eligible resource(s)",
                len(selected),
                candidate_count,
            )

    reason_counts = Counter(decision.reason for decision in decisions)
    report = {
        "schema_version": 2,
        "run_id": str(uuid.uuid4()),
        "generated_at": format_timestamp(get_current_time()),
        "status": "planned",
        "action": active_config.action,
        "dry_run": active_config.dry_run,
        "compartment_id": active_config.compartment_id,
        "resource_types": ["compute_instance"],
        "scanned_count": len(decisions),
        "candidate_count": candidate_count,
        "selected_count": 0 if live else len(selected),
        "planned_count": len(selected),
        "limited": len(selected) < candidate_count,
        "reason_counts": dict(sorted(reason_counts.items())),
        "decisions": [asdict(decision) for decision in decisions],
        "outcomes": [
            {"resource_id": decision.resource_id, "status": "planned", "action": active_config.action}
            for decision in selected
        ],
        "execution_guards_checked": False,
    }

    if live:
        from execution import run_live
        return run_live(compute_client, active_config, report)

    report["status"] = "completed"
    for outcome in report["outcomes"]:
        outcome["status"] = "dry_run" if active_config.dry_run else "report_only"
    report["outcome_counts"] = dict(Counter(item["status"] for item in report["outcomes"]))
    report["outcome_reason_counts"] = {}
    if active_config.report_file:
        write_cleanup_report(active_config.report_file, report)

    return report


def handle_cleanup(config: Optional[JanitorConfig] = None) -> int:
    """Compatibility wrapper returning the number of selected janitor actions."""
    return int(run_janitor(config)["selected_count"])


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())

    try:
        report = run_janitor()
        LOGGER.info(
            "Janitor run completed: scanned=%s eligible=%s selected=%s action=%s dry_run=%s",
            report["scanned_count"],
            report["candidate_count"],
            report["selected_count"],
            report["action"],
            report["dry_run"],
        )
        return 0 if report.get("status", "completed") == "completed" else 1
    except KeyError as exc:
        LOGGER.error("Missing required configuration: %s", exc)
        return 1
    except Exception:
        LOGGER.exception("Janitor run failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
