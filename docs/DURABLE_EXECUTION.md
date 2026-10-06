# Durable execution and migration

Live actions require shared OCI Object Storage. Report-only and dry-run operation
remain available without storage or write permissions. The Function request body
cannot enable live execution, change compartments, change lifecycle tags, replace
storage settings, or loosen deployment controls.

## Configure storage before enabling live actions

Create a dedicated private Standard-tier bucket in the same region as the janitor.
Configure the following on every Function/application or CLI installation that
targets the same compartment and region:

```bash
export OCI_JANITOR_STATE_NAMESPACE='<your-object-storage-namespace>'
export OCI_JANITOR_STATE_BUCKET='janitor-state'
export OCI_JANITOR_STATE_PREFIX='janitor/v1'
export OCI_JANITOR_MAX_ACTIONS_PER_WINDOW='10'
export OCI_JANITOR_ACTION_WINDOW_SECONDS='3600'
export OCI_JANITOR_TERMINATION_GRACE_HOURS='24'
```

The storage namespace, bucket, prefix, and shared budget settings must agree
between stop and terminate deployments and between Function and CLI runs. Separate
stores do not coordinate with one another. The state scope is derived from the
Compute endpoint and compartment, independently of action and per-run policy.
Changing any storage destination requires an explicit state migration while all
writers are stopped. Keep clocks synchronized.

Grant the janitor principal object read/create/overwrite permissions in its
dedicated state bucket and delete permission only for the exact lock object.
For the default prefix, replace the scope placeholder below with the SHA-256
hex digest of `Compute endpoint without trailing slash + newline + compartment OCID`:

```text
Allow dynamic-group janitor-functions to {OBJECT_READ, OBJECT_CREATE, OBJECT_OVERWRITE} in compartment janitor-operations where target.bucket.name='janitor-state'
Allow dynamic-group janitor-functions to {OBJECT_DELETE} in compartment janitor-operations where all {target.bucket.name='janitor-state', target.object.name='janitor/v1/<scope-sha256>/lock.json'}
```

Substitute your own dynamic group, compartment, bucket, and exact lock path.
The janitor needs no bucket creation, policy administration, or state/report
deletion permission. Compute list/get and the permitted lifecycle action still
require their own compartment-scoped permissions.

Keep state and locks out of lifecycle deletion rules. Versioning is recommended
for state and report recovery; retention rules that prevent overwrites or lock
deletion are incompatible with the mutable state prefix. Restrict administrative
state writes: changing history, deleting state, or selecting a new prefix can
invalidate the shared safety boundary.

References: [Object Storage IAM permissions](https://docs.oracle.com/en-us/iaas/Content/Identity/Reference/objectstoragepolicyreference.htm)
and [conditional object operations](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/object_storage/client/oci.object_storage.ObjectStorageClient.html).

## Request policy

The operator's environment/policy file is loaded first. These are the only accepted
Function request fields; unknown fields are rejected:

| Field | Accepted request change |
| --- | --- |
| `compartment_id` | Exactly the configured compartment. |
| `action` | The configured action, or `report`. |
| `dry_run` | JSON boolean; may turn live execution off, never turn deployment dry-run off. |
| `threshold_hours` | Integer at least as large as the operator's TTL; at most 87,600. |
| `max_actions_per_run` | Positive integer at most the configured cap; null is rejected. |

Tag keys/values, authentication, paths, termination permission, grace period,
storage destination, and rolling budget are operator-controlled. Increasing the
global TTL does not change an explicit resource TTL or absolute expiration.

## Execution and audit semantics

Objects under `<prefix>/<scope-sha256>/`:

- `lock.json`: owning run ID, acquisition time, and scope.
- `state.json`: schema version, scope, budget policy, reservations, and resource history.
- `runs/<run-id>.json`: the entire plan and the latest outcomes for that run.

Live runs acquire the scope lock using create-if-absent. Plans are written before
any mutation. For each selected resource, the janitor refreshes its tags, state,
and compartment, re-evaluates policy, then uses its fresh ETag in the mutation.
An intervening update causes a conditional failure instead of action against the
stale resource. Missing ETags fail closed.

The rolling budget counts action reservations across all runs and both lifecycle
actions. Each reservation and its pending intent are saved together before the
API call. Failed attempts remain charged, as do unknown results. A backward clock
shift does not discard future reservations. A conflicting shared budget policy
blocks execution until an operator reconciles the configuration.

Report schema 2 retains the aggregate field names and adds `run_id`,
`planned_count`, `status`, `outcomes`, and outcome counts/reasons. For live runs,
`planned_count` covers all tag/age candidates, while `selected_count` counts
durably reserved action attempts after the fresh safety gates. Preview selection
still describes the capped candidate set. Skips do not consume the per-run cap,
so unrelated stopped instances cannot starve later resources with valid history.
Each planned resource can be
`planned`, `pending`, `submitted`, `failed`, `unknown`, `skipped`, or
`not_attempted`. Dry runs use `dry_run`; report-only runs use `report_only`.
`submitted` means the API accepted the request, not that shutdown or termination
has finished. Aggregate `selected_count` is not a success count.

Resource-specific API rejections (400/404/409/412/422) are recorded and the run
continues. Authorization failures stop the run. Timeouts, throttling, server
errors, and unexpected mutation exceptions stop the run with an unknown result;
the pending intent blocks another mutation of that resource until reconciled.
The Compute client does not automatically retry mutations.

Successful runs return HTTP 200, partial failures 207, aborted service runs 503,
and lock conflicts 409. CLI partial/failed runs exit nonzero. Storage failures
stop immediately and retain the lock and latest durable checkpoint. If a result
cannot be persisted after mutation, the preceding pending intent remains the
source of truth. A local `report_file` is an optional atomic mirror, not the shared
state backend; failure to write the mirror also stops further actions.

Dry runs describe tag/age eligibility and selection, without locking, consuming
budget, updating history, or claiming to have verified live execution guards.
Their `execution_guards_checked` field is false.

## Grace period

Live termination always requires a stopped instance and a recorded successful
janitor stop submission. The legacy `termination_requires_stopped=false` option
is only usable for non-mutating previews.

The first terminate run that observes the instance stopped records the observation
time and ETag, then skips it. A later run can terminate only after the configured
grace period and only while the stopped resource's ETag still matches. An external
update restarts the interval; an unrelated manually stopped instance has no janitor
stop history and is skipped. Existing stopped instances are not automatically
grandfathered into deletion when upgrading.

An accepted stop whose outcome has not yet been observed is not repeatedly
submitted, even if a list/get temporarily still shows RUNNING. A permanently stuck
stop requires operator investigation. Termination is submitted asynchronously;
this release does not claim to verify its final state or provide an automatic
recovery/restart workflow.

## Crash and uncertain-result recovery

Locks deliberately have no automatic expiry. An expired lease cannot fence an
old worker out of Compute APIs, so takeover could permit two writers. A normal
completed or recorded partial/aborted run releases its lock. A killed process,
lost storage response, corrupt state, or persistence failure retains it.

1. Disable the schedule and prevent manual invocations. Stop the old worker and
   prove it cannot resume before modifying its lock. A lock's age alone is not proof.
2. Preserve the lock, state, report, their ETags/versions, and relevant OCI Audit
   events. Match the lock's run ID to the report and pending resource records.
3. Inspect each pending action. A timeout does not prove failure. Use OCI Audit
   evidence and actual resource state; do not infer successful termination from
   an ambiguous 404, which can also indicate insufficient access.
4. With all writers stopped, reconcile state using a conditional ETag write. For
   an action proven never accepted, remove its `pending` record. For an accepted
   stop, replace pending with a `stop` record containing the original run ID,
   submission timestamp, and instance creation timestamp. Omit observation fields
   so the next run starts a full grace period. If evidence is insufficient, leave
   pending in place and exclude the resource. Preserve charged reservations.
5. Inspect the preserved report alongside the reconciliation record. Delete only
   the original lock, using its current ETag. Never blindly delete all lock/state
   objects or reset the budget to resume processing.
6. Validate a report-only run, then restore the intended live schedule.

Budget-policy changes also require a coordinated migration. Stop every writer,
preserve the old state, and keep all reservations that remain relevant to the new
window before updating `budget_policy` and all deployment configurations together.

## Validation

CI covers Python 3.11/3.12 with no dependencies, minimum supported SDK/FDK versions,
and current compatible dependencies. Contract tests exercise real SDK parameter
validation/serialization and FDK responses with transport mocked. Fault tests use
an atomic conditional object API to exercise concurrent locks, partial failure,
unknown outcomes, persistent budgets, late tag changes, and grace periods.

These tests do not establish live-tenancy IAM or cloud service behavior. Before
production rollout, exercise stop, first observation, grace expiry, conditional
conflict, and crash recovery in a disposable compartment with the intended IAM
and bucket policies.
