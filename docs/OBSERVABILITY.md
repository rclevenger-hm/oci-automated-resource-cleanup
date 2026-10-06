# Observability contract

The janitor is destructive-capable automation, so its telemetry should answer three questions quickly: **what did it evaluate, what did it intend to change, and did the run remain inside policy?** The structured report and run events are the source contracts for those signals.

Schema 2 live runs persist their plan and per-resource outcomes in Object Storage.
See [durable execution](DURABLE_EXECUTION.md) for setup, locking, budgets, and recovery.
Selection counts describe intent; outcome counts distinguish submitted, failed,
unknown, skipped, and unattempted actions. Submission is not completion confirmation.

## Run-level signals

Emit one completion event for every successful invocation, including dry runs. Derive these fields directly from the report returned by `run_janitor`:

| Signal | Source | Operational meaning |
| --- | --- | --- |
| `janitor.scanned` | `scanned_count` | Resources evaluated in the target compartment. |
| `janitor.candidates` | `candidate_count` | Expired resources that passed policy checks. |
| `janitor.selected` | `selected_count` | Live reserved attempts after safety gates/caps; capped candidates in previews. |
| `janitor.limited` | `limited` | Whether blast-radius limiting suppressed eligible actions. |
| `janitor.action` | `action` | `report`, `stop`, or `terminate`. |
| `janitor.dry_run` | `dry_run` | Whether mutation was disabled. |
| `janitor.reason.<reason>` | `reason_counts` | Distribution of eligibility and rejection decisions. |

Do not use resource display names, OCIDs, policy-file contents, or request bodies as metric labels. Those values create high cardinality and can expose infrastructure identifiers. Keep per-resource detail in the bounded JSON report and protected logs instead.

## Structured runtime events

The OCI Function handler emits compact JSON on each successful or failed invocation. These records are intended to be queryable without parsing prose.

A successful invocation emits `janitor.run.completed` with:

```json
{
  "event": "janitor.run.completed",
  "request_id": "<OCI call id when available>",
  "scanned_count": 10,
  "candidate_count": 2,
  "selected_count": 2,
  "action": "report",
  "dry_run": true,
  "limited": false,
  "reason_counts": {
    "expired": 2,
    "required_tag_missing": 8
  }
}
```

A failed invocation emits `janitor.run.failed` with a stable `failure_category`. Configuration failures may include the bounded validation message. Unexpected runtime failures retain exception detail in the protected stack trace while the JSON event and client response do not echo the exception text.

Example runtime failure event:

```json
{
  "event": "janitor.run.failed",
  "request_id": "<OCI call id when available>",
  "failure_category": "runtime"
}
```

Pre-execution handler failure categories include `configuration`, `runtime`,
`concurrency`, and `persistence`. Execution outcomes additionally distinguish
resource-specific rejection, authorization failure, and service/unknown results.

Recorded live runs emit `janitor.run.completed`, `janitor.run.partial`, or
`janitor.run.failed`, with `run_id`, `outcome_counts`, and
`outcome_reason_counts` alongside the existing fields. Grace periods, changed
eligibility, shared-budget exhaustion, and unresolved prior actions appear in the
outcome reasons, independently of discovery reason counts. Use run IDs to locate
protected audit reports; do not add them as metric labels.

## Alerts

Recommended initial alerts are intentionally small and actionable:

1. **Run failure:** any scheduled invocation emits `janitor.run.failed`.
2. **Unexpected live mutation:** `dry_run=false` outside the approved schedule/environment.
3. **Termination enabled:** `action=terminate` outside a reviewed maintenance window.
4. **Blast-radius cap reached:** `limited=true`; review why more resources expired than the configured action budget permits.
5. **Discovery collapse:** `scanned_count` drops unexpectedly to zero or materially below its normal range; treat this as an auth/discovery signal, not evidence that nothing requires cleanup.
6. **Policy rejection spike:** sudden growth in `invalid_ttl_tag` or `invalid_expiration_tag` decisions indicates malformed ownership/lifecycle metadata.
7. **Uncertain action:** an `unknown` outcome or `unresolved_previous_action` reason requires reconciliation before that resource can be changed again.
8. **Persistent scope lock:** repeated concurrency failures or missing scheduled completions may indicate a crashed worker. Follow the recovery procedure; never clear a lock based only on its age.
9. **Partial run:** `janitor.run.partial` means some selected resources failed or remained unresolved, even though HTTP 207 is in the success class.

Do not page merely because `candidate_count > 0`; discovering expired managed resources is the normal purpose of the janitor.

## Suggested service objectives

For a scheduled janitor, useful reliability objectives are about execution rather than deletion volume:

- **Scheduled-run success:** at least 99% of expected runs complete successfully over 30 days.
- **Audit completeness:** every successful run emits exactly one report with `schema_version`, `generated_at`, action state, aggregate counts, reason counts, and decisions, plus one `janitor.run.completed` event.
- **Mutation accountability:** every live selected resource has a corresponding decision record from the same run.
- **Duplicate scheduling:** live invocations use one conditional scope lock; duplicates return 409. All writers in a region/compartment must share the configured state destination and budget policy.

These objectives intentionally avoid treating a high deletion count as success.

## Dashboard view

A compact operational dashboard should show:

- scheduled runs and failures over time;
- scanned / candidates / selected as separate series;
- dry-run versus live runs;
- report / stop / terminate counts;
- `limited=true` occurrences;
- decision reasons, especially protection and malformed-policy reasons;
- most recent successful run timestamp.

A healthy graph can legitimately show many scanned resources and zero selected actions.

## Incident triage

When a run behaves unexpectedly:

1. stop the schedule or force `dry_run=true` before changing policy;
2. preserve the structured report and runtime event from the affected run;
3. compare `reason_counts`, selected resources, action, and policy inputs with the previous known-good run;
4. follow [`RECOVERY.md`](RECOVERY.md) if a stop or termination was incorrect;
5. correct policy/tests before restoring live execution.

The observability contract should evolve with the report schema. A schema change that renames or removes these fields is an operational API change and should receive explicit review.
