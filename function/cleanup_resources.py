import datetime
import logging
import os
from dataclasses import dataclass
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
    required_tag_key: Optional[str] = None
    required_tag_value: Optional[str] = None
    auth_mode: str = "auto"
    config_path: Optional[str] = None
    config_profile: str = "DEFAULT"


def parse_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() not in {"0", "false", "no"}


def load_config(overrides: Optional[Mapping[str, Any]] = None) -> CleanupConfig:
    override_values = dict(overrides or {})
    compartment_id = override_values.get("compartment_id") or os.environ["OCI_COMPARTMENT_ID"]
    threshold_hours = int(
        override_values.get("threshold_hours", os.environ.get("OCI_CLEANUP_THRESHOLD_HOURS", "24"))
    )
    dry_run = parse_bool(override_values.get("dry_run", os.environ.get("OCI_CLEANUP_DRY_RUN", "true")))
    required_tag_key = override_values.get("required_tag_key", os.environ.get("OCI_CLEANUP_REQUIRED_TAG_KEY"))
    required_tag_value = override_values.get(
        "required_tag_value",
        os.environ.get("OCI_CLEANUP_REQUIRED_TAG_VALUE"),
    )
    auth_mode = override_values.get("auth_mode", os.environ.get("OCI_AUTH_MODE", "auto"))
    config_path = override_values.get("config_path", os.environ.get("OCI_CONFIG_FILE"))
    config_profile = override_values.get("config_profile", os.environ.get("OCI_CONFIG_PROFILE", "DEFAULT"))

    return CleanupConfig(
        compartment_id=compartment_id,
        threshold_hours=threshold_hours,
        dry_run=dry_run,
        required_tag_key=required_tag_key,
        required_tag_value=required_tag_value,
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


def should_terminate_instance(
    instance,
    now: datetime.datetime,
    threshold_hours: int,
    required_tag_key: Optional[str] = None,
    required_tag_value: Optional[str] = None,
) -> bool:
    if getattr(instance, "lifecycle_state", None) != "RUNNING":
        return False

    if not has_required_tag(instance, required_tag_key, required_tag_value):
        return False

    launch_time = normalize_timestamp(instance.time_created)
    elapsed_time = now - launch_time
    return elapsed_time.total_seconds() / 3600 > threshold_hours


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
        ):
            candidates.append(instance)

    return candidates


def terminate_instance(compute_client, instance_id: str, dry_run: bool = True) -> None:
    if dry_run:
        LOGGER.info("Dry run enabled, skipping termination for %s", instance_id)
        return

    compute_client.terminate_instance(instance_id)


def handle_cleanup(config: Optional[CleanupConfig] = None) -> int:
    active_config = config or load_config_from_env()
    compute_client = get_compute_client(active_config)
    candidates = get_cleanup_candidates(compute_client, active_config)

    LOGGER.info(
        "Found %s candidate instance(s) in compartment %s",
        len(candidates),
        active_config.compartment_id,
    )

    for instance in candidates:
        LOGGER.info(
            "Processing instance %s (%s), threshold=%sh, dry_run=%s",
            instance.display_name,
            instance.id,
            active_config.threshold_hours,
            active_config.dry_run,
        )
        terminate_instance(compute_client, instance.id, dry_run=active_config.dry_run)

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
