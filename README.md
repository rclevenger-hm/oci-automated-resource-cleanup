# OCI Automated Resource Cleanup

Safe-by-default cleanup automation for Oracle Cloud Infrastructure. This project finds OCI compute instances that match explicit cleanup policy rules and then either reports them, stops them, or terminates them.

The repository supports two execution modes:

- Local or scheduled Python execution
- OCI Functions deployment via the `function/` directory

## Why This Exists

Cloud cleanup scripts are easy to get wrong. A simple "delete anything older than 24 hours" rule is risky, especially in shared or long-lived environments.

This project adds guardrails around cleanup actions:

- Dry-run mode is the default
- Instances can require an opt-in tag
- Instances can be protected by an exclusion tag
- A per-run action cap limits blast radius
- Every evaluated instance can be recorded in a structured report
- A safer `stop` action is available before full termination

## Current Behavior

The cleanup policy currently targets OCI compute instances that are:

- In the `RUNNING` lifecycle state
- Older than a configured age threshold
- Optionally marked with a required freeform tag such as `AutoCleanup=true`
- Not marked with an exclusion tag such as `DoNotCleanup=true`

Important: this is still policy-driven cleanup, not true idle detection. The project does not yet use OCI Monitoring metrics like CPU or network utilization to decide whether an instance is idle.

## Repository Layout

- `function/cleanup_resources.py`: shared cleanup logic, config loading, policy evaluation, reporting, and action execution
- `function/handler.py`: OCI Functions entrypoint
- `function/func.yaml`: OCI Functions manifest
- `function/test_cleanup_resources.py`: unit tests for cleanup policy and execution behavior

## Features

- Pagination-aware OCI instance discovery
- Dry-run by default
- Required-tag opt-in
- Exclusion-tag protection
- `terminate` and `stop` cleanup actions
- Per-run processing cap
- JSON policy file support
- Structured JSON report output
- OCI config-file auth and OCI resource principal auth support

## Configuration

The cleanup logic can be configured with environment variables, an optional JSON policy file, or a request payload when invoked as an OCI Function.

### Environment Variables

- `OCI_COMPARTMENT_ID` required. Target compartment OCID.
- `OCI_CLEANUP_THRESHOLD_HOURS` optional. Minimum resource age before it becomes eligible. Default: `24`.
- `OCI_CLEANUP_DRY_RUN` optional. Default: `true`.
- `OCI_CLEANUP_ACTION` optional. `terminate` by default. Set to `stop` for quarantine-style runs.
- `OCI_CLEANUP_MAX_TERMINATIONS_PER_RUN` optional. Maximum number of eligible resources to process in a single run.
- `OCI_CLEANUP_REQUIRED_TAG_KEY` optional. Freeform tag key that must exist for a resource to be eligible.
- `OCI_CLEANUP_REQUIRED_TAG_VALUE` optional. If set, the required tag must match this value exactly.
- `OCI_CLEANUP_EXCLUDED_TAG_KEY` optional. Freeform tag key that protects a resource from cleanup.
- `OCI_CLEANUP_EXCLUDED_TAG_VALUE` optional. If set, the exclusion tag must match this value exactly.
- `OCI_CLEANUP_REPORT_FILE` optional. Writes a JSON report to this path after a run.
- `OCI_CLEANUP_POLICY_FILE` optional. Path to a JSON file containing cleanup defaults.
- `OCI_AUTH_MODE` optional. `auto` by default. Valid values are `auto`, `config`, and `resource_principal`.
- `OCI_CONFIG_FILE` optional. OCI config file path for local execution.
- `OCI_CONFIG_PROFILE` optional. OCI config profile name. Default: `DEFAULT`.
- `LOG_LEVEL` optional. Default: `INFO`.

### Policy File Example

```json
{
  "compartment_id": "ocid1.compartment.oc1..exampleuniqueID",
  "threshold_hours": 72,
  "dry_run": true,
  "action": "stop",
  "max_terminations_per_run": 5,
  "required_tag_key": "AutoCleanup",
  "required_tag_value": "true",
  "excluded_tag_key": "DoNotCleanup",
  "excluded_tag_value": "true",
  "report_file": "cleanup-report.json",
  "auth_mode": "resource_principal"
}
```

## Install

```bash
pip install -r function/requirements.txt
```

## Run Locally

Example dry run on Windows PowerShell:

```powershell
$env:OCI_COMPARTMENT_ID = "ocid1.compartment.oc1..exampleuniqueID"
$env:OCI_CLEANUP_REQUIRED_TAG_KEY = "AutoCleanup"
$env:OCI_CLEANUP_REQUIRED_TAG_VALUE = "true"
$env:OCI_CLEANUP_EXCLUDED_TAG_KEY = "DoNotCleanup"
$env:OCI_CLEANUP_EXCLUDED_TAG_VALUE = "true"
$env:OCI_CLEANUP_ACTION = "stop"
python function/cleanup_resources.py
```

Example real termination run:

```powershell
$env:OCI_COMPARTMENT_ID = "ocid1.compartment.oc1..exampleuniqueID"
$env:OCI_CLEANUP_REQUIRED_TAG_KEY = "AutoCleanup"
$env:OCI_CLEANUP_REQUIRED_TAG_VALUE = "true"
$env:OCI_CLEANUP_DRY_RUN = "false"
$env:OCI_CLEANUP_ACTION = "terminate"
python function/cleanup_resources.py
```

## Deploy As An OCI Function

From the `function/` directory:

```bash
fn -v deploy --app <your_fn_app_name>
```

For OCI Functions, prefer resource principals:

```bash
fn config function <your_fn_app_name> oci-automated-resource-cleanup OCI_AUTH_MODE resource_principal
```

You will typically also set function configuration values for:

- `OCI_COMPARTMENT_ID`
- `OCI_CLEANUP_THRESHOLD_HOURS`
- `OCI_CLEANUP_DRY_RUN`
- `OCI_CLEANUP_ACTION`
- `OCI_CLEANUP_REQUIRED_TAG_KEY`
- `OCI_CLEANUP_REQUIRED_TAG_VALUE`
- `OCI_CLEANUP_EXCLUDED_TAG_KEY`
- `OCI_CLEANUP_EXCLUDED_TAG_VALUE`
- `OCI_CLEANUP_MAX_TERMINATIONS_PER_RUN`
- `LOG_LEVEL`

## Invoke The OCI Function

The function accepts an optional JSON body. Request values override defaults from the environment or policy file for that invocation.

Supported request fields:

- `compartment_id`
- `threshold_hours`
- `dry_run`
- `action`
- `max_terminations_per_run`
- `required_tag_key`
- `required_tag_value`
- `excluded_tag_key`
- `excluded_tag_value`
- `report_file`
- `policy_file`
- `auth_mode`
- `config_path`
- `config_profile`

Example request body:

```json
{
  "compartment_id": "ocid1.compartment.oc1..exampleuniqueID",
  "threshold_hours": 72,
  "dry_run": true,
  "action": "stop",
  "required_tag_key": "AutoCleanup",
  "required_tag_value": "true",
  "excluded_tag_key": "DoNotCleanup",
  "excluded_tag_value": "true",
  "max_terminations_per_run": 5
}
```

## Tests

```bash
cd function
python -m unittest test_cleanup_resources.py
```

## Known Limitations

- Cleanup eligibility is still based on age and tag policy, not OCI utilization metrics
- Only compute instances are supported today
- Reports are written locally; there is no Object Storage or Notifications integration yet

## Recommended Next Steps

- Add OCI Monitoring-based idle detection
- Support more OCI resource types
- Publish reports to OCI Object Storage or OCI Logging
- Add notifications for non-dry-run executions and failures

## License

This project is licensed under the [MIT License](LICENSE).
