"""Audited live execution with shared budgets and observed recovery intervals."""

import datetime
import logging
import uuid
from collections import Counter

import cleanup_resources as policy
from state_store import StateError, etag_of

LOGGER = logging.getLogger(__name__)


def _checkpoint(store, config, report):
    report["updated_at"] = policy.format_timestamp(policy.get_current_time())
    report["outcome_counts"] = dict(Counter(item["status"] for item in report["outcomes"]))
    report["outcome_reason_counts"] = dict(Counter(
        item["reason"] for item in report["outcomes"] if "reason" in item
    ))
    store.write_report(report)
    if config.report_file:
        try:
            policy.write_cleanup_report(config.report_file, report)
        except Exception as exc:
            raise StateError("Local report publication failed; durable report retained") from exc


def _budget(state, config, now):
    expected = {"limit": config.max_actions_per_window, "seconds": config.action_window_seconds}
    if state.get("budget_policy", expected) != expected:
        raise StateError("Shared budget policy differs; reconcile operator configuration before running")
    state["budget_policy"] = expected
    cutoff = now - datetime.timedelta(seconds=config.action_window_seconds)
    try:
        # Future timestamps remain charged if a host's clock moves backward.
        state["reservations"] = [
            reservation for reservation in state["reservations"]
            if policy.parse_timestamp(reservation["at"]) > cutoff
        ]
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise StateError("Invalid shared action budget") from exc
    return len(state["reservations"]) < config.max_actions_per_window


def _lifecycle_reason(state, instance, etag, config, now):
    record = state["resources"].setdefault(instance.id, {})
    if record.get("pending"):
        return "unresolved_previous_action"
    if record.get("termination_submitted"):
        return "termination_already_submitted"
    stop = record.get("stop")
    if config.action == "stop":
        if stop and not stop.get("observed_stopped_at"):
            return "stop_already_submitted"
        # A restarted resource needs a new accepted stop and recovery interval.
        record.pop("stop", None)
        return None
    if not stop or stop.get("created_at") != policy.format_timestamp(instance.time_created):
        return "no_janitor_stop_history"
    if stop.get("stopped_etag") != etag:
        # Begin (or restart) the grace period on observed STOPPED, never on intent.
        stop["stopped_etag"] = etag
        stop["observed_stopped_at"] = policy.format_timestamp(now)
        return "termination_grace_period"
    try:
        deadline = policy.parse_timestamp(stop["observed_stopped_at"]) + datetime.timedelta(
            hours=config.termination_grace_hours
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise StateError("Invalid stop history") from exc
    if now < deadline:
        return "termination_grace_period"
    return None


def _error_kind(exc):
    status = getattr(exc, "status", None)
    if status in {400, 404, 409, 412, 422}:
        return "resource"
    if status in {401, 403}:
        return "authorization"
    return "service"


def _finish(report, fatal):
    if fatal:
        report["status"] = "failed"
    elif any(
        item["status"] == "failed" or item.get("reason") == "unresolved_previous_action"
        for item in report["outcomes"]
    ):
        report["status"] = "partial"
    else:
        report["status"] = "completed"
    for item in report["outcomes"]:
        if item["status"] == "planned":
            item.update(status="not_attempted", reason="run_aborted")


def run_live(client, config, report):
    store = policy.get_state_store(config, client)
    store.acquire(report["run_id"], policy.format_timestamp(policy.get_current_time()))
    # Release only after durable completion. Crashes and persistence failures leave
    # a non-expiring lock and the last checkpoint for operator reconciliation.
    try:
        state = store.load()
        _budget(state, config, policy.get_current_time())
        report["execution_guards_checked"] = True
        report["policy"] = {
            "termination_grace_hours": config.termination_grace_hours,
            "max_actions_per_run": config.max_actions_per_run,
            "max_actions_per_window": config.max_actions_per_window,
            "action_window_seconds": config.action_window_seconds,
            "threshold_hours": config.threshold_hours,
            "required_tag_key": config.required_tag_key,
            "required_tag_value": config.required_tag_value,
            "excluded_tag_key": config.excluded_tag_key,
            "excluded_tag_value": config.excluded_tag_value,
            "ttl_tag_key": config.ttl_tag_key,
            "expires_at_tag_key": config.expires_at_tag_key,
        }
        _checkpoint(store, config, report)  # Entire plan precedes every mutation.
        fatal = False
        for outcome in report["outcomes"]:
            if report["selected_count"] >= config.max_actions_per_run:
                outcome.update(status="skipped", reason="run_action_cap_reached")
                report["limited"] = True
                continue
            resource_id = outcome["resource_id"]
            try:
                fresh = client.get_instance(resource_id)
                instance = fresh.data
                now = policy.get_current_time()
                if instance.id != resource_id or instance.compartment_id != config.compartment_id:
                    outcome.update(status="skipped", reason="resource_scope_changed")
                    _checkpoint(store, config, report)
                    continue
                decision = policy.evaluate_instance(instance, now, config)
                if not decision.eligible:
                    # Invalidate old stop evidence when a previously confirmed
                    # stopped instance has since restarted.
                    if instance.lifecycle_state == "RUNNING":
                        old = state["resources"].get(resource_id, {})
                        if old.get("stop", {}).get("observed_stopped_at"):
                            old.pop("stop", None)
                            store.save(state)
                    outcome.update(status="skipped", reason=decision.reason)
                    _checkpoint(store, config, report)
                    continue
                etag = etag_of(fresh)
            except StateError:
                raise
            except Exception as exc:
                kind = _error_kind(exc)
                outcome.update(status="failed", reason="refresh_failed", failure_category=kind)
                _checkpoint(store, config, report)
                if kind != "resource":
                    fatal = True
                    break
                continue

            reason = _lifecycle_reason(state, instance, etag, config, now)
            store.save(state)  # Persist new grace observations even on a skip.
            if reason:
                outcome.update(status="skipped", reason=reason)
                _checkpoint(store, config, report)
                continue
            now = policy.get_current_time()
            if not _budget(state, config, now):
                outcome.update(status="skipped", reason="shared_action_budget_exhausted")
                report["limited"] = True
                _checkpoint(store, config, report)
                continue

            token = str(uuid.uuid4())
            record = state["resources"][resource_id]
            record["pending"] = {
                "run_id": report["run_id"], "action": config.action,
                "at": policy.format_timestamp(now), "etag": etag, "retry_token": token,
            }
            state["reservations"].append({
                "run_id": report["run_id"], "resource_id": resource_id,
                "action": config.action, "at": policy.format_timestamp(now),
            })
            # Reservation and intent are one conditional write. Never refund an
            # attempted operation, including definitive failures.
            store.save(state)
            report["selected_count"] += 1
            outcome.update(status="pending", attempted_at=policy.format_timestamp(now), etag=etag)
            _checkpoint(store, config, report)
            store.assert_owned()
            try:
                response = policy.execute_cleanup_action(
                    client, resource_id, config.action, False, if_match=etag, retry_token=token
                )
            except Exception as exc:
                kind = _error_kind(exc)
                if kind in {"resource", "authorization"}:
                    record.pop("pending", None)  # Definitive API rejection.
                    outcome.update(status="failed", reason="action_rejected", failure_category=kind)
                else:
                    # Timeout/5xx may follow an accepted mutation. Do not retry
                    # or invent a successful result; preserve unresolved intent.
                    outcome.update(status="unknown", reason="action_result_unknown", failure_category=kind)
                store.save(state)
                _checkpoint(store, config, report)
                if kind != "resource":
                    fatal = True
                    break
                continue

            record.pop("pending", None)
            if config.action == "stop":
                record["stop"] = {
                    "run_id": report["run_id"],
                    "submitted_at": policy.format_timestamp(now),
                    "created_at": policy.format_timestamp(instance.time_created),
                }
            else:
                record["termination_submitted"] = {
                    "run_id": report["run_id"], "at": policy.format_timestamp(now),
                }
            store.save(state)
            outcome.update(status="submitted")
            request_id = getattr(response, "headers", {}).get("opc-request-id")
            if isinstance(request_id, str):
                outcome["opc_request_id"] = request_id
            _checkpoint(store, config, report)

        _finish(report, fatal)
        _checkpoint(store, config, report)
        store.release()
        return report
    except Exception:
        # Never replace an uncertain durable checkpoint with an invented success.
        LOGGER.exception("Live execution interrupted; retain lock and audit evidence for run %s", report["run_id"])
        raise
