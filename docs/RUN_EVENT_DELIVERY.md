# Structured run-event delivery

The janitor already emits a bounded structured report for every run. The next observability step should publish that same report to an operator-visible sink without changing lifecycle-selection behavior.

## Delivery contract

A delivery adapter should receive the completed run report after policy evaluation and action selection. Delivery failure must not silently change which resources are selected, and it must never cause an additional cleanup pass.

Required properties:

- preserve the existing report schema as the source of truth;
- keep resource identifiers available for audit while avoiding credentials or local configuration content;
- use bounded retries for transient sink failures;
- emit one delivery failure to the normal function log after retries are exhausted;
- leave dry-run semantics unchanged;
- allow delivery to be disabled for local development and tests.

## Recommended first sink

OCI Logging is the simplest first production sink because the Function already writes operational logs and the report is JSON-shaped. Object Storage is a reasonable second sink when long-term immutable run history is more important than near-real-time search.

## Metrics derived from reports

Keep metric dimensions low-cardinality. Useful run-level signals are:

- scanned resources;
- eligible resources;
- selected actions;
- whether the action cap limited the run;
- counts by decision reason;
- run failures;
- delivery failures.

Do not use resource OCIDs or display names as metric dimensions.

## Acceptance criteria

1. Existing unit tests continue to pass with delivery disabled.
2. A mocked sink receives exactly one completed report per successful run.
3. A sink failure does not trigger a second lifecycle action.
4. Retry count and timeout are bounded and testable.
5. Dry-run reports are delivered with the same schema as live runs.
6. The operations guide documents how to distinguish cleanup failure from telemetry-delivery failure.

This keeps observability additive: operators gain durable evidence without making the cleanup control path depend on the monitoring destination.
