# Recovery and rollback runbook

The janitor is designed to reduce the probability and blast radius of an incorrect cleanup decision, but operators still need a defined response when a managed resource is stopped or terminated unexpectedly.

Version 0.2 requires shared Object Storage for live actions. For crashed workers,
retained locks, pending intents, or uncertain API responses, follow the
[durable execution recovery procedure](DURABLE_EXECUTION.md#crash-and-uncertain-result-recovery).
Prove the old worker cannot resume before releasing its lock. Preserve action
reservations and pending intent until OCI Audit evidence has been reconciled.

## First response

If a run appears wrong:

1. Disable the schedule/invocation source before changing policy.
2. Preserve the run report and function logs.
3. Record the effective action, dry-run state, policy source, action cap, target compartment, and affected resource OCIDs.
4. Do not immediately rerun the janitor with edited tags; first establish whether the prior action completed and what OCI state each resource is in.

## Stopped instance recovery

For an unintentionally stopped Compute instance:

- confirm the instance was stopped by the janitor report/action trail;
- remember that `submitted` records API acceptance, while current instance state and OCI Audit establish what happened;
- verify the resource is still intended to exist;
- add or restore `DoNotCleanup=true` before restart when there is any policy ambiguity;
- start the instance through the normal OCI control path;
- verify attached volumes, network interfaces, application health, and dependent services;
- correct the lifecycle policy/tag only after service recovery is understood.

A restart is not a complete recovery check. Application state and downstream dependencies may still need validation.

## Terminated instance recovery

Termination may be irreversible at the instance level. Recovery depends on infrastructure and data being reproducible outside the janitor.

Before enabling live termination in any environment, document:

- the IaC or provisioning source that can recreate the instance;
- whether boot/block volumes are preserved or deleted on termination;
- backup/snapshot expectations for persistent data;
- DNS/load-balancer/service-discovery behavior after recreation;
- secret/bootstrap dependencies needed to return the workload to service.

If those controls are not known, use `stop` rather than `terminate`.

The current termination call retains OCI's default boot-volume behavior (deletion);
this release does not add a backup or volume-preservation policy. Live termination
does require an accepted janitor stop, a recorded STOPPED observation, a complete
grace interval, and an unchanged ETag. Those gates are preventive controls, not a
backup strategy.

## Policy rollback

When a policy change caused the incident:

1. Revert the policy file/environment configuration through the normal reviewed deployment path.
2. Keep `OCI_JANITOR_DRY_RUN=true` or `action=report` while validating the corrected policy.
3. Compare the new report with the affected resource set.
4. Re-enable `stop` only after expected eligible/ineligible reasons are understood.
5. Re-enable `terminate` separately and only when the two-phase/termination interlocks are still appropriate.

Do not use a broader exclusion or threshold change as a permanent substitute for fixing incorrect ownership/expiry tags.

## Evidence and follow-up

Retain the report, function logs, policy revision, OCI audit events, affected resources, recovery actions, and root cause. Useful follow-ups include a regression test for the decision rule, narrower IAM permissions, a smaller action cap, or an additional explicit safety condition.

## Recovery readiness exercise

Periodically select a disposable managed instance and prove the complete sequence: report -> stop -> restart/recover -> stop -> terminate -> recreate from the documented source. This validates that the janitor's safety model and the surrounding infrastructure recovery model agree in practice.
