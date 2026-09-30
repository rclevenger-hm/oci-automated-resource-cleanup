# Structured event routing runbook

The function emits compact JSON events such as `janitor.run.completed` and `janitor.run.failed`. Those events are useful only if operators can route and alert on them without parsing free-form exception text.

## Recommended OCI path

Use OCI Logging as the durable log source for the function. From there, use a Service Connector Hub connector when events need to feed another OCI service or an external sink. Keep the application event schema stable and perform destination-specific transformation outside the function when possible.

## Stable fields

Treat these fields as the operational contract:

- `event`
- `request_id` when the Functions runtime supplies one
- `action`
- `dry_run`
- `scanned_count`
- `candidate_count`
- `selected_count`
- `limited`
- `reason_counts`
- `failure_category` on failed runs

Do not add resource names, OCIDs, or raw exception payloads as high-cardinality metric dimensions.

## Suggested metrics

Derive or publish low-cardinality counters for completed runs, failed runs, selected resources, and limited runs. A dashboard should show run success rate, resources selected by action, dry-run versus active runs, and failure category.

## Alerts

Page-worthy conditions should remain narrow:

- repeated runtime failures across consecutive scheduled runs;
- no successful run within the expected schedule window;
- active cleanup selecting an unexpectedly large number of resources.

A single configuration error should normally create an operator-visible warning rather than an urgent page.

## Validation

Before routing is considered production-ready, exercise one dry run, one intentionally invalid request, and one simulated runtime failure. Confirm that all three can be correlated by `request_id`, that no credential material appears in the log payload, and that alert routing can be disabled in a development deployment.
