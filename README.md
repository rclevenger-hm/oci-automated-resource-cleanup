# OCI Ephemeral Resource Janitor

Safety-first lifecycle enforcement for temporary Oracle Cloud Infrastructure resources.

The janitor is designed for infrastructure that is **supposed to expire**: developer sandboxes, CI workers, QA machines, demos, experiments, short-lived troubleshooting hosts, and other ephemeral workloads. It evaluates explicit lifecycle policy and can report, stop, or terminate resources after their intended lifetime.

> **Current resource support:** OCI Compute instances. The policy and reporting model is intentionally generic so additional ephemeral OCI resource types can be added without turning the project into an indiscriminate "delete old things" script.

## Purpose

The project answers one operational question:

> **Which explicitly managed temporary resources have expired, and what lifecycle action should be taken safely?**

It is not an idle-resource detector and it is not a general-purpose OCI cost optimizer. CPU, network, and other utilization metrics are not currently used to infer whether a resource is safe to remove.

## Safety Model

The defaults are deliberately conservative:

1. **Explicit opt-in is mandatory.** A resource is ignored unless it has `JanitorManaged=true` (configurable key/value).
2. **Dry-run is enabled by default.**
3. **`stop` is the default action**, not termination.
4. **Only 10 resources are selected per run by default** to cap blast radius.
5. **`DoNotCleanup=true` always protects a managed resource** by default.
6. **Termination has a second interlock.** Live termination requires `dry_run=false`, `action=terminate`, and `allow_terminate=true`.
7. **Live termination requires janitor stop history and a grace period.** The default is 24 hours from the first recorded STOPPED observation, with an unchanged resource ETag.
8. **Malformed lifecycle tags fail closed.** Non-finite, out-of-range, or overflowing TTLs reject the individual resource.
9. **Live actions require durable Object Storage state.** A complete plan and each action intent/outcome are checkpointed; partial failures retain audit evidence.
10. **Concurrent runs share one scope lock and a rolling action budget**, defaulting to 10 reservations per hour across stop and terminate runs.
11. **Eligibility is refreshed before action**, and conditional ETags reject intervening changes.
12. **Function requests may only narrow operator policy.** They cannot enable termination, remove protections, expand action limits, or change the target compartment.

Read the [durable execution and migration guide](docs/DURABLE_EXECUTION.md) before enabling live actions or upgrading an existing live deployment. Dry-run and report-only use do not require state storage.

A typical production pattern is therefore:

```text
JanitorManaged=true
        |
        v
    expiration reached
        |
        v
   report / dry-run
        |
        v
       stop
        |
        v
 terminate on a later run
```

## Lifecycle Tags

Freeform tags provide per-resource policy.

| Tag | Default meaning |
| --- | --- |
| `JanitorManaged=true` | Explicitly opts the resource into janitor management. Required. |
| `DoNotCleanup=true` | Protects the resource from janitor actions. |
| `TTLHours=<number>` | Overrides the global TTL for this resource. |
| `ExpiresAt=<ISO-8601>` | Sets an absolute expiration time and takes precedence over `TTLHours`. |

Examples:

```text
JanitorManaged=true
TTLHours=8
```

A short-lived CI worker expires eight hours after instance creation.

```text
JanitorManaged=true
ExpiresAt=2026-09-08T18:00:00Z
```

A demo instance expires at an explicit deadline.

```text
JanitorManaged=true
TTLHours=24
DoNotCleanup=true
```

The instance remains protected regardless of age until the exclusion tag is removed.

## Eligibility Rules

For each OCI Compute instance in the configured compartment, the janitor:

1. checks whether the lifecycle state is actionable for the configured action;
2. requires the opt-in management tag;
3. checks the exclusion tag;
4. resolves expiration from `ExpiresAt`, then `TTLHours`, then the global threshold;
5. rejects malformed expiration policy;
6. marks the instance eligible only after expiration.

Actionable states differ by lifecycle action:

- `report`: expired `RUNNING` and `STOPPED` managed instances can be surfaced;
- `stop`: only expired `RUNNING` managed instances are eligible;
- `terminate`: only expired `STOPPED` managed instances are eligible by default.

Live termination additionally checks durable janitor stop history and the recovery grace period. Direct termination of running instances is rejected for live runs. A non-mutating preview can still use the legacy state override.

## Structured Reporting

Each run produces a report containing:

- schema version and generation timestamp;
- action and dry-run state;
- target compartment;
- supported resource types evaluated;
- scanned, eligible, and selected counts;
- whether the run was limited by the action cap;
- counts grouped by decision reason;
- one decision record per evaluated resource;
- schema 2 run ID, run status, and per-selected-resource outcomes;
- live execution guard, rolling budget, and partial-failure evidence.

The complete plan is persisted before live mutation, with an updated checkpoint after every action. `submitted` means OCI accepted the request; it does not claim the asynchronous operation has finished. A storage failure leaves the latest checkpoint and scope lock available for recovery.

Typical decision reasons include:

- `expired`
- `not_expired`
- `required_tag_missing`
- `excluded_tag_present`
- `invalid_ttl_tag`
- `invalid_expiration_tag`
- `lifecycle_state_not_actionable`

This makes dry-runs useful as audit output rather than simply logging "would delete" messages. See [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md) for the run-level metrics, alerting, dashboard, and incident-triage contract built around this report schema.

## Configuration

Operator configuration comes from environment variables over JSON policy-file defaults. Function requests can only select report mode, enable dry-run, increase the global TTL, reduce the per-run action cap, or repeat the configured action/compartment. All other request fields are rejected. See [request policy](docs/DURABLE_EXECUTION.md#request-policy).

### Primary environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `OCI_COMPARTMENT_ID` | required | OCI compartment to evaluate. |
| `OCI_JANITOR_THRESHOLD_HOURS` | `24` | Global TTL when no resource-specific tag is present. |
| `OCI_JANITOR_DRY_RUN` | `true` | Prevents mutations. |
| `OCI_JANITOR_ACTION` | `stop` | `report`, `stop`, or `terminate`. |
| `OCI_JANITOR_MAX_ACTIONS_PER_RUN` | `10` | Blast-radius cap. |
| `OCI_JANITOR_REQUIRED_TAG_KEY` | `JanitorManaged` | Required opt-in tag key. |
| `OCI_JANITOR_REQUIRED_TAG_VALUE` | `true` | Required opt-in tag value. |
| `OCI_JANITOR_EXCLUDED_TAG_KEY` | `DoNotCleanup` | Protection tag key. |
| `OCI_JANITOR_EXCLUDED_TAG_VALUE` | `true` | Protection tag value. |
| `OCI_JANITOR_TTL_TAG_KEY` | `TTLHours` | Per-resource TTL tag. |
| `OCI_JANITOR_EXPIRES_AT_TAG_KEY` | `ExpiresAt` | Absolute expiration tag. |
| `OCI_JANITOR_ALLOW_TERMINATE` | `false` | Required second interlock for live termination. |
| `OCI_JANITOR_TERMINATION_REQUIRES_STOPPED` | `true` | Mandatory for live termination; false is preview-only. |
| `OCI_JANITOR_TERMINATION_GRACE_HOURS` | `24` | Minimum interval after first STOPPED observation; positive and finite. |
| `OCI_JANITOR_STATE_NAMESPACE` | unset | Object Storage namespace; required for live actions. |
| `OCI_JANITOR_STATE_BUCKET` | unset | Shared private state/audit bucket; required for live actions. |
| `OCI_JANITOR_STATE_PREFIX` | `janitor/v1` | Must match across all writers for the same scope. |
| `OCI_JANITOR_MAX_ACTIONS_PER_WINDOW` | `10` | Shared rolling budget, including failed/unknown attempts. |
| `OCI_JANITOR_ACTION_WINDOW_SECONDS` | `3600` | Rolling reservation window. |
| `OCI_JANITOR_REPORT_FILE` | unset | Optional path for the structured JSON report. |
| `OCI_JANITOR_POLICY_FILE` | unset | Optional JSON policy file. |
| `OCI_AUTH_MODE` | `auto` | `auto`, `config`, or `resource_principal`. |
| `OCI_CONFIG_FILE` | OCI SDK default | Local OCI config path. |
| `OCI_CONFIG_PROFILE` | `DEFAULT` | OCI config profile. |
| `LOG_LEVEL` | `INFO` | Python logging level. |

Legacy `OCI_CLEANUP_*` environment variables from the previous project name remain supported where there is a direct equivalent.

## Policy File

See [`examples/policy.json`](examples/policy.json).

```json
{
  "compartment_id": "ocid1.compartment.oc1..exampleuniqueID",
  "threshold_hours": 72,
  "dry_run": true,
  "action": "report",
  "max_actions_per_run": 10,
  "max_actions_per_window": 10,
  "action_window_seconds": 3600,
  "termination_grace_hours": 24,
  "required_tag_key": "JanitorManaged",
  "required_tag_value": "true",
  "excluded_tag_key": "DoNotCleanup",
  "excluded_tag_value": "true",
  "ttl_tag_key": "TTLHours",
  "expires_at_tag_key": "ExpiresAt",
  "allow_terminate": false,
  "termination_requires_stopped": true,
  "auth_mode": "resource_principal"
}
```

## Run Locally

Install dependencies:

```bash
pip install -r function/requirements.txt
```

Start with a report-only dry run:

```bash
export OCI_COMPARTMENT_ID='ocid1.compartment.oc1..exampleuniqueID'
export OCI_JANITOR_ACTION='report'
export OCI_JANITOR_DRY_RUN='true'
python function/cleanup_resources.py
```

Configure the shared bucket and [required IAM](docs/DURABLE_EXECUTION.md#configure-storage-before-enabling-live-actions), then stop expired managed instances:

```bash
export OCI_COMPARTMENT_ID='ocid1.compartment.oc1..exampleuniqueID'
export OCI_JANITOR_ACTION='stop'
export OCI_JANITOR_DRY_RUN='false'
export OCI_JANITOR_STATE_NAMESPACE='<your-object-storage-namespace>'
export OCI_JANITOR_STATE_BUCKET='janitor-state'
python function/cleanup_resources.py
```

Using the same state configuration, evaluate termination of expired instances stopped by the janitor:

```bash
export OCI_COMPARTMENT_ID='ocid1.compartment.oc1..exampleuniqueID'
export OCI_JANITOR_ACTION='terminate'
export OCI_JANITOR_DRY_RUN='false'
export OCI_JANITOR_ALLOW_TERMINATE='true'
python function/cleanup_resources.py
```

The first terminate run observes STOPPED and starts the grace period. A later run may terminate after that interval, provided the resource still qualifies and its ETag is unchanged. Existing manually stopped resources are skipped. Stop/terminate API behavior and boot-volume preservation defaults remain unchanged; review [recovery prerequisites](docs/RECOVERY.md) before enabling termination.

## Deploy as an OCI Function

From the `function/` directory:

```bash
fn -v deploy --app <your_fn_app_name>
```

For OCI Functions, resource principals are recommended:

```bash
fn config function <your_fn_app_name> oci-ephemeral-resource-janitor OCI_AUTH_MODE resource_principal
```

Then configure the janitor policy on the Function/application and grant the resource principal the least privilege needed to list instances and perform only the actions you enable.

The function accepts only the restricted lower-case request fields, for example (TTL must be at least the operator threshold and cap no greater than its limit):

```json
{
  "compartment_id": "ocid1.compartment.oc1..exampleuniqueID",
  "action": "report",
  "dry_run": true,
  "threshold_hours": 72,
  "max_actions_per_run": 5
}
```

The response includes the run ID, scanned/eligible/selected counts, limit state, decision reasons, and outcome counts. Partial resource failures return 207, scope conflicts 409, and service/persistence failures 503. CLI partial/failed runs exit nonzero.

## Tests

```bash
cd function
python -m unittest discover -v -p 'test_*.py'
```

The test suite covers policy precedence and request narrowing, opt-in safety, malformed and overflowing TTLs, fresh eligibility and ETags, partial/uncertain action results, persistent plans, storage failures, concurrent locks, shared budgets, recovery intervals, CLI status, and the Function handler. CI runs on Python 3.11/3.12 with no dependencies, minimum supported dependencies, and current compatible dependencies. SDK/FDK contract tests mock transport and do not contact OCI.

## Repository Layout

```text
.github/workflows/test.yml       CI for Python 3.11 and 3.12
examples/policy.json             Safe example policy
function/cleanup_resources.py    Policy engine, OCI discovery, actions, reporting
function/execution.py            Durable live execution and recovery gates
function/state_store.py          Conditional shared Object Storage state
function/handler.py              OCI Functions entrypoint
function/func.yaml               OCI Functions manifest
function/test_cleanup_resources.py
function/test_handler.py
```

## Current Scope and Roadmap

Today the janitor supports OCI Compute instances. Natural extensions are other resource types that have a defensible ephemeral lifecycle, such as:

- unattached ephemeral block or boot volumes;
- temporary public IPs;
- short-lived snapshots or custom images;
- ephemeral load balancers or test-network resources;
- dashboard integration for persistent reports and structured runtime events;
- Notifications integration for action summaries and failures;
- optional OCI Monitoring signals as an additional safety condition, never as a replacement for explicit ownership policy.

Any additional resource handler should preserve the same design principle: **the janitor only manages resources that have explicitly opted into lifecycle management.**

## License

MIT. See [LICENSE](LICENSE).
