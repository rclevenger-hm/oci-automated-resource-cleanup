# Automated Resource Cleanup

This repository contains a small OCI cleanup tool for identifying long-running compute instances that match explicit cleanup policy controls. It is safe by default: the cleanup logic runs in dry-run mode unless you explicitly disable that behavior.

The project now supports both of these execution modes:

- A local Python job you can run from a workstation, CI job, or scheduler.
- An OCI Functions deployment using the handler and `func.yaml` in the `function/` directory.

## What It Does

- Lists compute instances in a target compartment using OCI pagination.
- Filters candidates by lifecycle state, age threshold, and optional freeform tag requirements.
- Logs the instances it would terminate.
- Skips actual termination unless `OCI_CLEANUP_DRY_RUN=false`.

## Current Safety Model

The script does not claim to detect true idleness from monitoring data. Today it selects instances that are:

- In the `RUNNING` lifecycle state.
- Older than the configured threshold.
- Optionally marked with a required freeform tag such as `AutoCleanup=true`.

That makes it much safer than deleting all old instances, but you should still treat it as policy-driven cleanup rather than intelligent idle detection.

## Configuration

Set these environment variables before running the script:

- `OCI_COMPARTMENT_ID` (required): Target compartment OCID.
- `OCI_CLEANUP_THRESHOLD_HOURS` (optional): Minimum age in hours before an instance is considered a cleanup candidate. Default: `24`.
- `OCI_CLEANUP_DRY_RUN` (optional): `true` by default. Set to `false` to allow real termination.
- `OCI_CLEANUP_REQUIRED_TAG_KEY` (optional): Freeform tag key an instance must have to be eligible.
- `OCI_CLEANUP_REQUIRED_TAG_VALUE` (optional): If set, the tag value must match exactly.
- `OCI_AUTH_MODE` (optional): `auto` by default. Use `config` to force OCI config-file auth or `resource_principal` for OCI Functions.
- `OCI_CONFIG_FILE` (optional): Path to an OCI config file.
- `OCI_CONFIG_PROFILE` (optional): OCI config profile name. Default: `DEFAULT`.
- `LOG_LEVEL` (optional): Logging level. Default: `INFO`.

## OCI Credentials

Use a normal OCI CLI configuration outside the repository when possible. A local placeholder config file exists in `.oci/config`, but production credentials should not be committed to source control.

## Install

```bash
pip install -r function/requirements.txt
```

## Project Layout

- [function/cleanup_resources.py](/C:/Users/cuder/OneDrive/Documents/GitHub/oci-automated-resource-cleanup/function/cleanup_resources.py:1): Shared cleanup logic used by both local and serverless execution.
- [function/handler.py](/C:/Users/cuder/OneDrive/Documents/GitHub/oci-automated-resource-cleanup/function/handler.py:1): OCI Functions entrypoint.
- [function/func.yaml](/C:/Users/cuder/OneDrive/Documents/GitHub/oci-automated-resource-cleanup/function/func.yaml:1): OCI Functions manifest.

## Run Locally

Dry run:

```bash
set OCI_COMPARTMENT_ID=<your_compartment_ocid>
set OCI_CLEANUP_REQUIRED_TAG_KEY=AutoCleanup
set OCI_CLEANUP_REQUIRED_TAG_VALUE=true
python function/cleanup_resources.py
```

Real termination:

```bash
set OCI_COMPARTMENT_ID=<your_compartment_ocid>
set OCI_CLEANUP_REQUIRED_TAG_KEY=AutoCleanup
set OCI_CLEANUP_REQUIRED_TAG_VALUE=true
set OCI_CLEANUP_DRY_RUN=false
python function/cleanup_resources.py
```

## Deploy As An OCI Function

From the `function/` directory:

```bash
fn -v deploy --app <your_fn_app_name>
```

After deployment, configure the same environment variables on the function in OCI:

- `OCI_COMPARTMENT_ID`
- `OCI_CLEANUP_THRESHOLD_HOURS`
- `OCI_CLEANUP_DRY_RUN`
- `OCI_CLEANUP_REQUIRED_TAG_KEY`
- `OCI_CLEANUP_REQUIRED_TAG_VALUE`
- `OCI_AUTH_MODE`
- `OCI_CONFIG_PROFILE`
- `LOG_LEVEL`

For OCI Functions, set `OCI_AUTH_MODE=resource_principal` so the runtime uses resource principals instead of a local OCI config file.

## Invoke The Function

The OCI Functions handler accepts an optional JSON body. Any of these keys can override the environment-backed defaults for a single invocation:

- `compartment_id`
- `threshold_hours`
- `dry_run`
- `required_tag_key`
- `required_tag_value`
- `auth_mode`
- `config_path`
- `config_profile`

Example payload:

```json
{
  "compartment_id": "ocid1.compartment.oc1..exampleuniqueID",
  "threshold_hours": 72,
  "dry_run": true,
  "required_tag_key": "AutoCleanup",
  "required_tag_value": "true"
}
```

## Tests

```bash
cd function
python -m unittest test_cleanup_resources.py
```

## Suggested Next Enhancements

- Add OCI Monitoring-based CPU or network checks to support real idle detection.
- Add support for multiple resource types instead of compute instances only.
- Emit structured audit output to a file or object storage.
- Switch the OCI Function path from local config loading to resource principals for production use.

## License

This project is licensed under the [MIT License](LICENSE).
