import datetime
import json
import logging
import os
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Optional

try:
    import oci
except ModuleNotFoundError:  # pragma: no cover - exercised in local test environments without OCI SDK
    oci = None


LOGGER = logging.getLogger(__name__)


@dataclass
class CleanupConfig:
    compartment_id: str
    threshold_hours: int = 24
    dry_run: bool = True
    action: str = "terminate"
    max_terminations_per_run: Optional[int] = None
    report_file: Optional[str] = None
    required_tag_key: Optional[str] = None
    required_tag_value: Optional[str] = None
    excluded_tag_key: Optional[str] = None
    excluded_tag_value: Optional[str] = None
    auth_mode: str = "auto"
    config_path: Optional[str] = None
    config_profile: str = "DEFAULT"


@dataclass
class CleanupDecision:
    instance_id: str
    display_name: str
    eligible: bool
    reason: str


def parse_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() not in {"0", "false", "no"}


def load_policy_file(path: str) -> Mapping[str, Any]:
    with open(path, "r", encoding="utf-8") as policy_handle:
        return json.load(policy_handle)


def load_config(overrides: Optional[Mapping[str, Any]] = None) -> CleanupConfig:
    override_values = dict(overrides or {})
    policy_file = override_values.get("policy_file", os.environ.get("OCI_CLEANUP_POLICY_FILE"))
    policy_values = dict(load_policy_file(policy_file)) if policy_file else {}

    merged_values = dict(policy_values)
    merged_values.update({key: value for key, value in os.environ.items() if key.startswith("OCI_")})
    merged_values.update(override_values)

    compartment_id = merged_values.get("compartment_id") or os.environ["OCI_COMPARTMENT_ID"]
    threshold_hours = int(
        merged_values.get("threshold_hours", os.environ.get("OCI_CLEANUP_THRESHOLD_HOURS", "24"))
    )
    dry_run = parse_bool(merged_values.get("dry_run", os.environ.get("OCI_CLEANUP_DRY_RUN", "true")))
    action = merged_values.get("action", os.environ.get("OCI_CLEANUP_ACTION", "terminate"))
    max_terminations_per_run = merged_values.get(
        "max_terminations_per_run",
        os.environ.get("OCI_CLEANUP_MAX_TERMINATIONS_PER_RUN"),
    )
    report_file = merged_values.get("report_file", os.environ.get("OCI_CLEANUP_REPORT_FILE"))
    required_tag_key = merged_values.get("required_tag_key", os.environ.get("OCI_CLEANUP_REQUIRED_TAG_KEY"))
    required_tag_value = merged_values.get(
        "required_tag_value",
        os.environ.get("OCI_CLEANUP_REQUIRED_TAG_VALUE"),
    )
    excluded_tag_key = merged_values.get("excluded_tag_key", os.environ.get("OCI_CLEANUP_EXCLUDED_TAG_KEY"))
    excluded_tag_value = merged_values.get(
        "excluded_tag_value",
        os.environ.get("OCI_CLEANUP_EXCLUDED_TAG_VALUE"),
    )
    auth_mode = merged_values.get("auth_mode", os.environ.get("OCI_AUTH_MODE", "auto"))
    config_path = merged_values.get("config_path", os.environ.get("OCI_CONFIG_FILE"))
    config_profile = merged_values.get("config_profile", os.environ.get("OCI_CONFIG_PROFILE", "DEFAULT"))

    return CleanupConfig(
        compartment_id=compartment_id,
        threshold_hours=threshold_hours,
        dry_run=dry_run,
        action=action,
        max_terminations_per_run=int(max_terminations_per_run) if max_terminations_per_run is not None else None,
        report_file=report_file,
        required_tag_key=required_tag_key,
        required_tag_value=required_tag_value,
        excluded_tag_key=excluded_tag_key,
        excluded_tag_value=excluded_tag_value,
        auth_mode=auth_mode,
        config_path=config_path,
        config_profile=config_profile,
    )


def load_config_from_env() -> CleanupConfig:
    return load_config()


def require_oci_sdk() -> Any:
    if oci is None:
        raise RuntimeError("The OCI Python SDK is not installed. Run 'pip install -r function/requirements.txt'.")
    return oci


def get_compute_client(config: CleanupConfig):
    sdk = require_oci_sdk()
    auth_mode = config.auth_mode.lower()

    if auth_mode == "resource_principal":
        signer = sdk.auth.signers.get_resource_principals_signer()
        return sdk.core.ComputeClient({}, signer=signer)

    try:
        if config.config_path:
            oci_config = sdk.config.from_file(config.config_path, config.config_profile)
        else:
            oci_config = sdk.config.from_file(profile_name=config.config_profile)
        return sdk.core.ComputeClient(oci_config)
    except Exception:
        if auth_mode == "config":
            raise

        LOGGER.info("Falling back to OCI resource principal signer")
        signer = sdk.auth.signers.get_resource_principals_signer()
        return sdk.core.ComputeClient({}, signer=signer)


def get_current_time() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def normalize_timestamp(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def list_instances(compute_client, compartment_id: str) -> Iterable:
    sdk = require_oci_sdk()
    response = sdk.pagination.list_call_get_all_results(
        compute_client.list_instances,
        compartment_id=compartment_id,
    )
    return response.data


def has_required_tag(instance, required_tag_key: Optional[str], required_tag_value: Optional[str]) -> bool:
    if not required_tag_key:
        return True

    freeform_tags = getattr(instance, "freeform_tags", {}) or {}
    if required_tag_key not in freeform_tags:
        return False

    if required_tag_value is None:
        return True

    return str(freeform_tags.get(required_tag_key)) == required_tag_value


def has_excluded_tag(instance, excluded_tag_key: Optional[str], excluded_tag_value: Optional[str]) -> bool:
    if not excluded_tag_key:
        return False

    freeform_tags = getattr(instance, "freeform_tags", {}) or {}
    if excluded_tag_key not in freeform_tags:
        return False

    if excluded_tag_value is None:
        return True

    return str(freeform_tags.get(excluded_tag_key)) == excluded_tag_value


def evaluate_instance(
    instance,
    now: datetime.datetime,
    threshold_hours: int,
    required_tag_key: Optional[str] = None,
    required_tag_value: Optional[str] = None,
    excluded_tag_key: Optional[str] = None,
    excluded_tag_value: Optional[str] = None,
) -> CleanupDecision:
    if getattr(instance, "lifecycle_state", None) != "RUNNING":
        return CleanupDecision(instance.id, instance.display_name, False, "lifecycle_state_not_running")

    if not has_required_tag(instance, required_tag_key, required_tag_value):
        return CleanupDecision(instance.id, instance.display_name, False, "required_tag_missing")

    if has_excluded_tag(instance, excluded_tag_key, excluded_tag_value):
        return CleanupDecision(instance.id, instance.display_name, False, "excluded_tag_present")

    launch_time = normalize_timestamp(instance.time_created)
    elapsed_time = now - launch_time
    if elapsed_time.total_seconds() / 3600 <= threshold_hours:
        return CleanupDecision(instance.id, instance.display_name, False, "below_age_threshold")

    return CleanupDecision(instance.id, instance.display_name, True, "eligible")


def should_terminate_instance(
    instance,
    now: datetime.datetime,
    threshold_hours: int,
    required_tag_key: Optional[str] = None,
    required_tag_value: Optional[str] = None,
    excluded_tag_key: Optional[str] = None,
    excluded_tag_value: Optional[str] = None,
) -> bool:
    decision = evaluate_instance(
        instance,
        now,
        threshold_hours,
        required_tag_key,
        required_tag_value,
        excluded_tag_key,
        excluded_tag_value,
    )
    return decision.eligible


def get_cleanup_candidates(compute_client, config: CleanupConfig):
    now = get_current_time()
    candidates = []

    for instance in list_instances(compute_client, config.compartment_id):
        if should_terminate_instance(
            instance,
            now,
            config.threshold_hours,
            config.required_tag_key,
            config.required_tag_value,
            config.excluded_tag_key,
            config.excluded_tag_value,
        ):
            candidates.append(instance)

    return candidates


def get_cleanup_decisions(compute_client, config: CleanupConfig):
    now = get_current_time()
    decisions = []

    for instance in list_instances(compute_client, config.compartment_id):
        decisions.append(
            evaluate_instance(
                instance,
                now,
                config.threshold_hours,
                config.required_tag_key,
                config.required_tag_value,
                config.excluded_tag_key,
                config.excluded_tag_value,
            )
        )

    return decisions


def write_cleanup_report(path: str, report: Mapping[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as report_handle:
        json.dump(report, report_handle, indent=2, sort_keys=True)
        report_handle.write("\n")


def execute_cleanup_action(
    compute_client,
    instance_id: str,
    action: str = "terminate",
    dry_run: bool = True,
) -> None:
    if dry_run:
        LOGGER.info("Dry run enabled, skipping %s for %s", action, instance_id)
        return

    if action == "stop":
        compute_client.instance_action(instance_id, "STOP")
        return

    if action != "terminate":
        raise ValueError(f"Unsupported cleanup action: {action}")

    compute_client.terminate_instance(instance_id)


def handle_cleanup(config: Optional[CleanupConfig] = None) -> int:
    active_config = config or load_config_from_env()
    compute_client = get_compute_client(active_config)
    decisions = get_cleanup_decisions(compute_client, active_config)
    candidates = [decision for decision in decisions if decision.eligible]
    candidate_count = len(candidates)
    if active_config.max_terminations_per_run is not None:
        candidates = candidates[: active_config.max_terminations_per_run]
        if candidate_count > len(candidates):
            LOGGER.warning(
                "Limiting cleanup to %s of %s candidate instance(s)",
                len(candidates),
                candidate_count,
            )

    LOGGER.info(
        "Found %s candidate instance(s) in compartment %s",
        candidate_count,
        active_config.compartment_id,
    )

    for instance in candidates:
        LOGGER.info(
            "Processing instance %s (%s), threshold=%sh, dry_run=%s",
            instance.display_name,
            instance.instance_id,
            active_config.threshold_hours,
            active_config.dry_run,
        )
        execute_cleanup_action(
            compute_client,
            instance.instance_id,
            action=active_config.action,
            dry_run=active_config.dry_run,
        )

    if active_config.report_file:
        report = {
            "action": active_config.action,
            "candidate_count": candidate_count,
            "compartment_id": active_config.compartment_id,
            "decisions": [asdict(decision) for decision in decisions],
            "dry_run": active_config.dry_run,
            "processed_count": len(candidates),
        }
        write_cleanup_report(active_config.report_file, report)

    return len(candidates)


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())

    try:
        return handle_cleanup()
    except KeyError as exc:
        LOGGER.error("Missing required environment variable: %s", exc)
        return 1
    except Exception:
        LOGGER.exception("Cleanup failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
