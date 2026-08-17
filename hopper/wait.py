# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Authoritative supervised waiting for lode terminal states."""

import json
import logging
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

import hopper.client as client
import hopper.remote as remote
from hopper import config
from hopper import deadline as deadline_utils
from hopper.actions import PHASES
from hopper.lodes import (
    STATUS_ERROR,
    STATUS_SHIPPED,
    lode_with_status_annotations,
    resolve_worktree_path,
)
from hopper.tmux import capture_pane

STUCK_GRACE_MS = 120_000
MIN_POLL_S = 10.0
WAIT_SUMMARY = "hop wait: {lode_id} {outcome} — exited {code}"
WAIT_SUMMARY_NO_TARGET = "hop wait: could not resolve target — exited 1"

_monotonic = time.monotonic

logger = logging.getLogger(__name__)

REASON_CODES = {
    "shipped": "shipping_completed",
    "archived": "archived_before_shipping",
    "error": "lode_error",
    "inactive": "runner_inactive",
    "gated": "gate_requires_review",
    "stuck": "progress_stalled",
    "not_found": "target_disappeared",
    "action_blocked": "durable_action_blocked",
    "action_stalled": "successor_adoption_stalled",
    "startup_stalled": "startup_registration_stalled",
    "handoff_stalled": "ready_handoff_stalled",
    "reconnect_stalled": "runner_reregistration_stalled",
    "observer_unavailable": "observer_freshness_expired",
    "timeout": "overall_timeout",
    "target_absent": "target_absent",
    "target_ambiguous": "target_ambiguous",
    "status_unavailable": "initial_status_unavailable",
    "interrupted": "user_interrupted",
}

REASON_TEXT = {
    "shipping_completed": "Shipping completed.",
    "archived_before_shipping": "The lode was archived before it reached shipping.",
    "lode_error": "The lode entered its terminal error state.",
    "runner_inactive": "The runner became inactive before the lode shipped.",
    "gate_requires_review": "The lode is gated for operator review.",
    "progress_stalled": "The lode remained stuck after the progress grace period.",
    "target_disappeared": "The resolved lode disappeared before the wait completed.",
    "durable_action_blocked": "A durable completion action is blocked.",
    "successor_adoption_stalled": (
        "The successor runner was not adopted before its grace period expired."
    ),
    "startup_registration_stalled": (
        "The initial runner did not register before its grace period expired."
    ),
    "ready_handoff_stalled": (
        "The next-stage runner did not take over before the handoff grace period expired."
    ),
    "runner_reregistration_stalled": (
        "The runner did not re-register after the server restart before its grace period expired."
    ),
    "observer_freshness_expired": (
        "Authoritative status remained unavailable beyond the observer freshness deadline."
    ),
    "overall_timeout": "The configured overall wait deadline expired.",
    "target_absent": "No lode matched the requested target.",
    "target_ambiguous": "More than one lode matched the requested target.",
    "initial_status_unavailable": (
        "Authoritative status was unavailable during initial resolution."
    ),
    "user_interrupted": "The operator interrupted the wait.",
}

FINAL_RECORD_KEYS = {
    "id",
    "outcome",
    "exit_code",
    "reason_code",
    "reason",
    "recovery",
    "stage",
    "state",
    "status",
    "status_display",
    "failure_kind",
    "active",
    "archived",
    "archived_at",
    "server",
    "route",
    "probes",
    "matches",
    "observed_age_s",
    "pane_liveness",
    "tmux_pane",
    "last_tmux_pane",
    "worktree_path",
    "worktree_path_basis",
    "worktree_exists",
    "worktree_exists_observed_age_s",
}


def validate_snapshot(raw, expected_lid: str) -> dict | None:
    """Return a copied, well-typed snapshot for exactly the expected lode."""
    if not isinstance(raw, dict) or raw.get("id") != expected_lid:
        return None
    if not all(isinstance(raw.get(field), str) for field in ("stage", "state", "status")):
        return None
    if not isinstance(raw.get("active"), bool):
        return None
    if not isinstance(raw.get("archived"), bool):
        return None
    return dict(raw)


def classify(snapshot: dict) -> tuple[str, int, str | None] | None:
    """Apply the shared terminal policy; stuck remains subject to grace."""
    if snapshot.get("archived") is True:
        if snapshot["stage"] == "shipped":
            return "shipped", 0, None
        return "archived", 1, "archived_before_shipping"
    if snapshot["state"] == "error":
        return "error", 1, None
    if snapshot["state"] == "gated":
        return "gated", 2, None
    if snapshot["state"] == "stuck":
        return "stuck", 3, None
    if snapshot.get("pending_action") is not None:
        return None
    if snapshot["state"] == "teardown":
        return None
    if snapshot["stage"] == "shipped":
        return "shipped", 0, None
    if not snapshot["active"] and snapshot["state"] in {"new", "ready", "reconnecting"}:
        return None
    if not snapshot["active"]:
        return "inactive", 1, None
    return None


def _snapshot_grace_kind(snapshot: dict) -> str | None:
    """Return the one semantic grace continuously selected by a snapshot."""
    if snapshot.get("archived") is True:
        return None
    pending_action = snapshot.get("pending_action")
    if pending_action is not None:
        if isinstance(pending_action, dict) and pending_action.get("phase") == "spawning":
            return "action_spawn"
        return None
    if snapshot["state"] == "stuck":
        return "stuck"
    if snapshot["state"] == "new" and snapshot["active"] is False:
        return "startup"
    if snapshot["state"] == "ready" and snapshot["active"] is False:
        return "handoff"
    if snapshot["state"] == "reconnecting" and snapshot["active"] is False:
        return "reconnect"
    return None


def _new_grace(kind: str | None, origin_ts: float) -> dict | None:
    if kind is None:
        return None
    return {
        "kind": kind,
        "origin_ts": origin_ts,
        "recheck_pending": False,
        "confirmed": False,
    }


def _new_record(
    lid: str,
    snapshot: dict,
    route: str,
    observed_ts: float,
    *,
    probes: list[dict],
) -> dict:
    """Create one plain supervisor record from an initial valid snapshot."""
    if not probes:
        raise ValueError("resolved wait target is missing configured-source probe evidence")
    remote_source = route != "local"
    probe_rows = [dict(probe) for probe in probes]
    for probe in probe_rows:
        if probe.get("attempts", 0):
            probe["_observed_ts"] = observed_ts
    return {
        "id": lid,
        "query": lid,
        "route": route,
        "server": route if remote_source else None,
        "remote": remote_source,
        "probes": probe_rows,
        "matches": [],
        "preset_outcome": None,
        "preset_code": None,
        "preset_reason": None,
        "latest_snapshot": snapshot,
        "latest_snapshot_ts": observed_ts,
        "last_valid_ts": observed_ts,
        "next_reconcile_ts": observed_ts,
        "reconcile_requested": False,
        "grace": _new_grace(_snapshot_grace_kind(snapshot), observed_ts),
        "consecutive_failures": 0,
        "warned_failure_key": None,
        "not_found_count": 0,
        "generation_timed_out": False,
    }


def _resolution_failure_record(query: str, result: dict, now: float) -> dict:
    """Build a supervisor record for one completed resolution failure."""
    result_outcome = result.get("outcome")
    outcome, code, reason = {
        "absent": ("target_absent", 1, "target_absent"),
        "ambiguous": ("target_ambiguous", 1, "target_ambiguous"),
        "unavailable": ("status_unavailable", 4, "initial_status_unavailable"),
    }.get(result_outcome, ("status_unavailable", 4, "initial_status_unavailable"))
    route = result.get("host") if isinstance(result.get("host"), str) else None
    probes = result.get("probes")
    if not isinstance(probes, list) or not probes:
        raise ValueError("failed wait resolution is missing configured-source probe evidence")
    probe_rows = [dict(probe) for probe in probes]
    for probe in probe_rows:
        if probe.get("attempts", 0):
            probe["_observed_ts"] = now
    matches = []
    for match in result.get("match_tuples", []):
        if not isinstance(match, (list, tuple)) or len(match) != 2:
            continue
        server, lode_id = match
        if isinstance(lode_id, str) and (server is None or isinstance(server, str)):
            matches.append({"server": server, "id": lode_id})
    return {
        "id": query,
        "query": query,
        "route": route,
        "server": (
            route
            if outcome == "status_unavailable" and route and result.get("resident_owner") is True
            else None
        ),
        "remote": route not in {None, "local"},
        "probes": probe_rows,
        "matches": matches,
        "preset_outcome": outcome,
        "preset_code": code,
        "preset_reason": reason,
        "latest_snapshot": None,
        "latest_snapshot_ts": now,
        "last_valid_ts": now,
        "next_reconcile_ts": now,
        "reconcile_requested": False,
        "grace": None,
        "consecutive_failures": 0,
        "warned_failure_key": None,
        "not_found_count": 0,
        "generation_timed_out": False,
    }


def _resolve_target(
    socket_path: Path,
    raw_id: str,
    resolver: Callable,
    *,
    deadline: dict,
    child_control: dict,
) -> dict:
    """Resolve one query into a supervisor record."""
    result = resolver(
        socket_path,
        raw_id,
        deadline=deadline,
        child_control=child_control,
    )
    if result.get("outcome") != "found":
        return _resolution_failure_record(raw_id, result, _monotonic())
    lode = result.get("lode")
    lid = lode.get("id") if isinstance(lode, dict) else None
    if not isinstance(lid, str):
        return _resolution_failure_record(
            raw_id,
            {"outcome": "unavailable", "probes": result.get("probes", [])},
            _monotonic(),
        )
    snapshot = validate_snapshot(lode, lid)
    if snapshot is None:
        return _resolution_failure_record(
            raw_id,
            {"outcome": "unavailable", "probes": result.get("probes", [])},
            _monotonic(),
        )
    route = result.get("host")
    if not isinstance(route, str):
        return _resolution_failure_record(
            raw_id,
            {"outcome": "unavailable", "probes": result.get("probes", [])},
            _monotonic(),
        )
    observed_ts = _monotonic()
    record = _new_record(
        lid,
        snapshot,
        route,
        observed_ts,
        probes=result["probes"],
    )
    record["query"] = raw_id
    return record


def _publish_resident_routes(record: dict, *, deadline: dict) -> None:
    """Publish a new or changed initial resident route, warning on failure."""
    if not record["remote"] or not isinstance(record.get("latest_snapshot"), dict):
        return
    if deadline_utils.claim_call_budget(deadline, "wait.route_cache_load") is None:
        return
    try:
        cache = remote.load_lode_cache(deadline=deadline)
    except Exception as error:
        print(f"warning: could not read remote lode cache: {error}", file=sys.stderr)
        return

    lid = record["id"]
    host = record["route"]
    if cache.get(lid, {}).get("host") == host:
        return
    if deadline_utils.claim_call_budget(deadline, "wait.route_cache_remember") is None:
        return
    try:
        remote.remember_lode(
            lid,
            host,
            record["latest_snapshot"].get("project", ""),
            deadline=deadline,
        )
    except Exception as error:
        print(f"warning: could not update remote lode cache: {error}", file=sys.stderr)


def _post_observation(state: dict, observation: dict) -> None:
    """Append one observation and wake the supervisor."""
    with state["condition"]:
        if state["shutdown"]:
            return
        state["observations"].append(observation)
        state["condition"].notify()


def _request_local_reconcile(state: dict, lid: str | None = None) -> None:
    """Request a main-thread authoritative read for the local record."""
    with state["condition"]:
        if state["shutdown"]:
            return
        record = state["record"]
        if not state["resolved"] and not record["remote"] and (lid is None or lid == record["id"]):
            record["reconcile_requested"] = True
        state["condition"].notify()


def _probe_remote_observation(
    lid: str,
    host: str,
    probe_deadline: dict,
    probe_remote: Callable,
    child_control: dict,
) -> dict:
    """Return one normalized remote observation without mutating supervisor state."""
    if deadline_utils.claim_call_budget(probe_deadline, "wait.remote_probe") is None:
        return {
            "id": lid,
            "kind": "unreadable",
            "payload": None,
            "detail": "remote probe deadline expired",
            "failure_key": "deadline_expired",
            "observed_ts": _monotonic(),
        }
    try:
        snapshot, probe_state = probe_remote(
            host,
            lid,
            deadline=probe_deadline,
            child_control=child_control,
        )
        if probe_state == "found":
            kind = "found"
            detail = ""
        elif probe_state == "absent":
            kind = "absent"
            detail = "lode absent"
        else:
            kind = "unreadable"
            detail = "remote status unreadable"
        return {
            "id": lid,
            "kind": kind,
            "payload": snapshot,
            "detail": detail,
            "failure_key": kind,
            "observed_ts": _monotonic(),
        }
    except Exception as error:
        logger.debug("Unexpected remote observer error for %s on %s", lid, host, exc_info=True)
        return {
            "id": lid,
            "kind": "observer_error",
            "payload": None,
            "detail": f"unexpected {type(error).__name__}",
            "failure_key": f"observer_error:{type(error).__name__}",
            "observed_ts": _monotonic(),
        }


def _remote_worker_group(
    state: dict,
    interval_s: float,
    probe_remote: Callable,
) -> None:
    """Observe the remote lode from one reusable worker."""
    while not state["stop_event"].is_set():
        with state["condition"]:
            if state["shutdown"] or state["stop_event"].is_set() or state["resolved"]:
                return
            record = state["record"]
            lid = record["id"]
            host = record["route"]
        budget = deadline_utils.claim_call_budget(
            state["deadline"],
            "wait.remote_probe_generation",
            cap_s=state["probe_timeout_s"],
        )
        if budget is None:
            return
        probe_deadline = deadline_utils.shorten_deadline(
            state["deadline"],
            state["deadline"]["clock"]() + budget,
        )
        _post_observation(
            state,
            _probe_remote_observation(
                lid,
                host,
                probe_deadline,
                probe_remote,
                state["child_control"],
            ),
        )
        wait_budget = deadline_utils.claim_call_budget(
            state["deadline"],
            "wait.remote_worker_poll",
            cap_s=interval_s,
        )
        if wait_budget is None or state["stop_event"].wait(wait_budget):
            return


def _start_remote_workers(state: dict, probe_remote: Callable) -> None:
    """Start one daemon observer for the remote lode."""
    if state["resolved"] or not state["record"]["remote"]:
        return
    if deadline_utils.claim_call_budget(state["deadline"], "wait.remote_worker_start") is None:
        return
    thread = threading.Thread(
        target=_remote_worker_group,
        args=(state, state["poll_s"], probe_remote),
        daemon=True,
        name="wait-remote",
    )
    thread.start()


def _stop_remote_workers(state: dict) -> None:
    """Cancel remote observers without serially joining daemon workers."""
    state["stop_event"].set()


def _record_observer_failure(record: dict, kind: str, detail: str, failure_key: str) -> str | None:
    """Track a failure streak and return at most one warning per repeated failure."""
    record["not_found_count"] = 0
    record["consecutive_failures"] += 1
    if record["consecutive_failures"] < 2 or record["warned_failure_key"] == failure_key:
        return None
    record["warned_failure_key"] = failure_key
    return (
        f"warning: status observer for {record['id']} ({record['route']}) failed: {detail or kind}"
    )


def _update_record_probe(record: dict, outcome: str, detail: object, observed_ts: float) -> None:
    """Update the selected configured-source row in place."""
    selected = None
    for probe in record["probes"]:
        if probe.get("route") == record["route"] and probe.get("outcome") == "found":
            selected = probe
            break
    if selected is None:
        selected = next(
            (probe for probe in record["probes"] if probe.get("route") == record["route"]),
            record["probes"][0],
        )
    selected["outcome"] = outcome
    if isinstance(detail, (list, tuple)):
        selected["detail"] = [str(item) for item in detail]
    elif detail is None:
        selected["detail"] = None
    else:
        selected["detail"] = " ".join(str(detail).split())
    selected["attempts"] = int(selected.get("attempts", 0)) + 1
    selected["observed_age_s"] = 0.0
    selected["_observed_ts"] = observed_ts


def _apply_observation(record: dict, observation: dict, poll_s: float) -> str | None:
    """Apply one observation to a record without emitting or finishing it."""
    observed_ts = observation["observed_ts"]
    record["next_reconcile_ts"] = observed_ts + poll_s
    kind = observation["kind"]
    if kind == "found":
        snapshot = validate_snapshot(observation.get("payload"), record["id"])
        if snapshot is None:
            _update_record_probe(record, "malformed", "malformed status snapshot", observed_ts)
            return _record_observer_failure(
                record,
                "malformed",
                "malformed status snapshot",
                "malformed",
            )
        record["latest_snapshot"] = snapshot
        record["latest_snapshot_ts"] = observed_ts
        record["last_valid_ts"] = observed_ts
        record["consecutive_failures"] = 0
        record["warned_failure_key"] = None
        record["not_found_count"] = 0
        _update_record_probe(record, "found", None, observed_ts)
        grace_kind = _snapshot_grace_kind(snapshot)
        grace = record["grace"]
        if grace_kind is None:
            record["grace"] = None
        elif grace is None or grace["kind"] != grace_kind:
            record["grace"] = _new_grace(grace_kind, observed_ts)
        else:
            deadline = grace["origin_ts"] + STUCK_GRACE_MS / 1000.0
            if grace["recheck_pending"] and observed_ts >= deadline:
                grace["confirmed"] = True
        return None

    if kind == "absent":
        record["consecutive_failures"] = 0
        record["warned_failure_key"] = None
        record["not_found_count"] += 1
        _update_record_probe(record, "absent", None, observed_ts)
        return None

    _update_record_probe(record, kind, observation.get("detail"), observed_ts)
    return _record_observer_failure(
        record,
        kind,
        observation.get("detail", "status unavailable"),
        observation.get("failure_key", kind),
    )


def _drain_observations(state: dict) -> list[str]:
    """Apply all queued observations and collect new warning lines."""
    warnings = []
    while state["observations"]:
        observation = state["observations"].popleft()
        record = state["record"]
        if state["resolved"] or observation.get("id") != record["id"]:
            continue
        warning = _apply_observation(record, observation, state["poll_s"])
        if warning:
            warnings.append(warning)
    return warnings


def _mark_due_reconciliations(state: dict, now: float) -> None:
    """Turn expired grace and periodic deadlines into reconciliation work."""
    if state["resolved"]:
        return
    record = state["record"]
    grace = record["grace"]
    if grace is not None and not grace["recheck_pending"]:
        grace_deadline = grace["origin_ts"] + STUCK_GRACE_MS / 1000.0
        if now >= grace_deadline:
            grace["recheck_pending"] = True
            if not record["remote"]:
                record["reconcile_requested"] = True
    if record["remote"] and now >= record["next_reconcile_ts"]:
        record["next_reconcile_ts"] = now + state["poll_s"]


def _read_due_locals(state: dict, socket_path: Path, now: float) -> None:
    """Perform a due local read on the supervisor thread."""
    lid = None
    with state["condition"]:
        record = state["record"]
        if (
            not state["resolved"]
            and not record["remote"]
            and (record["reconcile_requested"] or now >= record["next_reconcile_ts"])
        ):
            record["reconcile_requested"] = False
            record["next_reconcile_ts"] = now + state["poll_s"]
            lid = record["id"]

    if lid is not None:
        observation = _local_probe_observation(socket_path, lid, state["deadline"])
        _post_observation(state, observation)


def _next_deadline(state: dict) -> float:
    """Return the earliest active supervisor deadline."""
    deadlines = [state["overall_deadline"]]
    record = state["record"]
    deadlines.append(record["next_reconcile_ts"])
    if state["observer_timeout_s"] > 0:
        deadlines.append(record["last_valid_ts"] + state["observer_timeout_s"])
    grace = record["grace"]
    if grace is not None and not grace["recheck_pending"]:
        deadlines.append(grace["origin_ts"] + STUCK_GRACE_MS / 1000.0)
    return min(deadlines)


def _collect_boundary_outcomes(state: dict, now: float) -> list[dict]:
    """Collect the terminal outcome known at this reconciliation boundary."""
    outcomes = []
    for record in () if state["resolved"] else (state["record"],):
        if record["preset_outcome"] is not None:
            outcomes.append(
                {
                    "record": record,
                    "outcome": record["preset_outcome"],
                    "code": record["preset_code"],
                    "reason": record["preset_reason"],
                }
            )
            continue
        if not isinstance(record.get("latest_snapshot"), dict):
            continue
        terminal = classify(record["latest_snapshot"])
        if terminal and terminal[0] != "stuck":
            outcome, code, reason = terminal
        elif _action_is_blocked(record["latest_snapshot"]):
            outcome, code, reason = "action_blocked", 2, "durable_action_blocked"
        elif record["not_found_count"] >= 2:
            outcome, code = "not_found", 1
            reason = None
        elif (
            record["grace"] is not None
            and record["grace"]["kind"] == "stuck"
            and record["grace"]["confirmed"]
        ):
            outcome, code = "stuck", 3
            reason = None
        else:
            timed = []
            if state["observer_timeout_s"] > 0:
                timed.append(
                    (
                        record["last_valid_ts"] + state["observer_timeout_s"],
                        0,
                        "observer_unavailable",
                        4,
                        None,
                    )
                )
            timed.append((state["overall_deadline"], 1, "timeout", 4, None))
            if record["generation_timed_out"]:
                timed.append((now, 1, "timeout", 4, "overall_timeout"))
            grace = record["grace"]
            if grace is not None and grace["confirmed"]:
                grace_outcomes = {
                    "startup": ("startup_stalled", 1, "startup_registration_stalled"),
                    "handoff": ("handoff_stalled", 1, "ready_handoff_stalled"),
                    "action_spawn": ("action_stalled", 2, "successor_adoption_stalled"),
                    "reconnect": ("reconnect_stalled", 1, "runner_reregistration_stalled"),
                }
                grace_outcome = grace_outcomes.get(grace["kind"])
                if grace_outcome is not None:
                    timed.append(
                        (
                            grace["origin_ts"] + STUCK_GRACE_MS / 1000.0,
                            2,
                            *grace_outcome,
                        )
                    )
            due = [candidate for candidate in timed if now >= candidate[0]]
            if not due:
                continue
            _deadline, _rank, outcome, code, reason = min(due)
        outcomes.append({"record": record, "outcome": outcome, "code": code, "reason": reason})
    return outcomes


def _local_probe_observation(
    socket_path: Path,
    lid: str,
    probe_deadline: dict,
) -> dict:
    """Return one normalized authoritative local observation."""
    budget = deadline_utils.claim_call_budget(
        probe_deadline,
        "wait.local_snapshot",
        cap_s=2.0,
    )
    if budget is None:
        return {
            "id": lid,
            "kind": "unreadable",
            "payload": None,
            "detail": "local probe deadline expired",
            "failure_key": "deadline_expired",
            "observed_ts": _monotonic(),
        }
    try:
        kind, payload = client.read_lode_snapshot(
            socket_path,
            lid,
            timeout=budget,
            deadline=probe_deadline,
        )
        if kind == "found":
            detail = ""
            failure_key = kind
        elif kind == "ambiguous":
            match_ids = payload if isinstance(payload, list) else []
            matches = ", ".join(match_ids) or "<invalid match list>"
            detail = f"local status ambiguous: {matches}"
            failure_key = f"ambiguous:{matches}"
            payload = None
        elif kind == "unavailable":
            detail = f"local status unavailable: {payload}"
            failure_key = f"unavailable:{payload}"
            payload = None
        else:
            detail = f"local status {kind}"
            failure_key = kind
        return {
            "id": lid,
            "kind": kind,
            "payload": payload,
            "detail": detail,
            "failure_key": failure_key,
            "observed_ts": _monotonic(),
        }
    except Exception as error:
        logger.debug("Unexpected local observer error for %s", lid, exc_info=True)
        return {
            "id": lid,
            "kind": "observer_error",
            "payload": None,
            "detail": f"unexpected {type(error).__name__}",
            "failure_key": f"observer_error:{type(error).__name__}",
            "observed_ts": _monotonic(),
        }


def _action_is_blocked(snapshot: dict) -> bool:
    """Return whether a structured pending-action projection requires attention."""
    pending_action = snapshot.get("pending_action")
    if not isinstance(pending_action, dict):
        return False
    phase = pending_action.get("phase")
    if isinstance(phase, str) and phase in PHASES and phase.endswith("_blocked"):
        return True
    return phase == "blocked" and pending_action.get("action_type") in {"invalid", "legacy-v1"}


def _observed_age(record: dict, now: float) -> float:
    return round(max(0.0, now - record["latest_snapshot_ts"]), 3)


def _recovery_for(record: dict, outcome: str) -> str | None:
    """Return one prescriptive recovery sentence for a final outcome."""
    if outcome == "shipped":
        return None
    lid = record["id"]
    route = record.get("route")
    hop = f"hop -H {route}" if route not in {None, "local"} else "hop"
    snapshot = record.get("latest_snapshot")
    pending_action = snapshot.get("pending_action") if isinstance(snapshot, dict) else None
    pending_recovery = pending_action.get("recovery") if isinstance(pending_action, dict) else None
    pending_command = (
        pending_recovery.get("command") if isinstance(pending_recovery, dict) else None
    )
    commands = {
        "archived": f"{hop} lode unarchive {lid}",
        "error": f"{hop} lode restart {lid}",
        "inactive": f"{hop} lode resume {lid}",
        "gated": f"{hop} gate show {lid}",
        "stuck": f"{hop} lode peek {lid}",
        "not_found": f"{hop} lode status {lid}",
        "action_blocked": f"{hop} lode status {lid}",
        "action_stalled": f"{hop} lode status {lid}",
        "startup_stalled": f"{hop} lode restart {lid}",
        "handoff_stalled": f"{hop} lode restart {lid}",
        "reconnect_stalled": f"{hop} lode restart {lid}",
        "observer_unavailable": f"{hop} wait {lid}",
        "timeout": f"{hop} wait {lid}",
        "target_absent": "hop lode list --all-hosts --json",
        "target_ambiguous": "hop lode list --all-hosts --json",
        "status_unavailable": f"{hop} lode list --json",
        "interrupted": f"{hop} wait {lid}",
    }
    command = (
        pending_command
        if outcome in {"action_blocked", "action_stalled"}
        and isinstance(pending_command, str)
        and pending_command
        else commands[outcome]
    )
    recovery = (
        f"Observed outcome {outcome} for '{lid}'. Hopper did not proceed. Recover with: {command}."
    )
    if outcome in {"action_blocked", "action_stalled"} and isinstance(pending_action, dict):
        if pending_action.get("action_type") == "invalid":
            recovery += " Repair or drain the malformed pending action before upgrading this host."
        elif pending_action.get("action_type") == "legacy-v1":
            recovery += " Drain the legacy pending action before upgrading this host."
    return recovery


def _final_probe_rows(record: dict, now: float, local_server: str | None) -> list[dict]:
    """Return closed configured-source evidence with current observation ages."""
    rows = []
    for raw in record["probes"]:
        observed_at = raw.get("_observed_ts")
        attempts = int(raw.get("attempts", 0))
        age = (
            round(max(0.0, now - observed_at), 3)
            if attempts and isinstance(observed_at, (int, float))
            else raw.get("observed_age_s")
            if attempts
            else None
        )
        server = raw.get("server")
        if raw.get("route") == "local":
            server = local_server
        rows.append(
            {
                "kind": raw.get("kind"),
                "server": server,
                "route": raw.get("route"),
                "candidate_id": raw.get("candidate_id"),
                "outcome": raw.get("outcome", "not_attempted"),
                "detail": raw.get("detail"),
                "attempts": attempts,
                "observed_age_s": age,
            }
        )
    return rows


def _fresh_worktree_enrichment(record: dict, deadline: dict, child_control: dict) -> dict:
    """Observe worktree provenance and existence once at finalization."""
    snapshot = record.get("latest_snapshot")
    if not isinstance(snapshot, dict):
        return {
            "worktree_path": None,
            "worktree_path_basis": "unavailable",
            "worktree_exists": None,
            "worktree_exists_observed_age_s": None,
        }
    if record["route"] == "local":
        if deadline_utils.claim_call_budget(deadline, "wait.worktree_path") is None:
            return {
                "worktree_path": None,
                "worktree_path_basis": "unavailable",
                "worktree_exists": None,
                "worktree_exists_observed_age_s": None,
            }
        resolution = resolve_worktree_path(snapshot)
        path = resolution["path"]
        if path is None:
            return {
                "worktree_path": None,
                "worktree_path_basis": resolution["basis"],
                "worktree_exists": None,
                "worktree_exists_observed_age_s": None,
            }
        if deadline_utils.claim_call_budget(deadline, "wait.worktree_exists") is None:
            exists = None
            age = None
        else:
            observed_at = _monotonic()
            try:
                exists = path.is_dir()
            except OSError:
                exists = None
            age = round(max(0.0, _monotonic() - observed_at), 3) if exists is not None else None
        return {
            "worktree_path": str(path),
            "worktree_path_basis": resolution["basis"],
            "worktree_exists": exists,
            "worktree_exists_observed_age_s": age,
        }

    budget = deadline_utils.claim_call_budget(
        deadline,
        "wait.remote_worktree_path",
        cap_s=8.0,
    )
    if budget is None:
        return {
            "worktree_path": None,
            "worktree_path_basis": "unavailable",
            "worktree_exists": None,
            "worktree_exists_observed_age_s": None,
        }
    observed_at = _monotonic()
    try:
        result = remote.run_remote(
            record["route"],
            ["lode", "path", record["id"], "--json"],
            timeout=budget,
            deadline=deadline,
            child_control=child_control,
        )
        payload = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        payload = None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"id", "host", "path", "exists"}
        or payload.get("id") != record["id"]
        or not isinstance(payload.get("path"), str)
        or not Path(payload["path"]).is_absolute()
        or not isinstance(payload.get("exists"), bool)
    ):
        return {
            "worktree_path": None,
            "worktree_path_basis": "unavailable",
            "worktree_exists": None,
            "worktree_exists_observed_age_s": None,
        }
    recorded_path = snapshot.get("worktree_path")
    if isinstance(recorded_path, str) and recorded_path:
        if payload["path"] != recorded_path:
            return {
                "worktree_path": None,
                "worktree_path_basis": "unavailable",
                "worktree_exists": None,
                "worktree_exists_observed_age_s": None,
            }
        basis = "recorded"
    elif payload["exists"]:
        basis = "existing"
    else:
        return {
            "worktree_path": None,
            "worktree_path_basis": "unavailable",
            "worktree_exists": None,
            "worktree_exists_observed_age_s": None,
        }
    return {
        "worktree_path": payload["path"],
        "worktree_path_basis": basis,
        "worktree_exists": payload["exists"],
        "worktree_exists_observed_age_s": round(max(0.0, _monotonic() - observed_at), 3),
    }


def _final_record(
    record: dict,
    outcome: str,
    exit_code: int,
    now: float,
    reason_code: str | None = None,
    *,
    deadline: dict,
    child_control: dict,
) -> dict:
    """Build the one closed final record consumed by every renderer."""
    snapshot = record.get("latest_snapshot")
    local_server = None
    if record.get("route") == "local" and isinstance(snapshot, dict):
        try:
            local_server = config.hostname()
        except Exception:
            pass
    if not isinstance(snapshot, dict):
        annotated = {
            "status_display": None,
            "pane_liveness": "not_probed",
        }
    else:
        pane_budget = deadline_utils.claim_call_budget(deadline, "wait.pane_liveness")
        if pane_budget is None:
            annotated = {
                "status_display": snapshot["status"],
                "pane_liveness": "not_probed",
            }
        else:
            try:
                annotated = lode_with_status_annotations(
                    snapshot,
                    pane_timeout=pane_budget,
                )
            except Exception:
                annotated = {
                    "status_display": snapshot["status"],
                    "pane_liveness": "unknown",
                }
    try:
        worktree = _fresh_worktree_enrichment(record, deadline, child_control)
    except Exception:
        logger.debug("Could not enrich worktree details for %s", record["id"], exc_info=True)
        worktree = {
            "worktree_path": None,
            "worktree_path_basis": "unavailable",
            "worktree_exists": None,
            "worktree_exists_observed_age_s": None,
        }
    stale_pane = snapshot.get("tmux_pane") if isinstance(snapshot, dict) else None
    current_pane = None if outcome == "observer_unavailable" else stale_pane
    server = record.get("server")
    if isinstance(snapshot, dict):
        server = local_server if record.get("route") == "local" else record.get("route")
    selected_reason_code = reason_code or REASON_CODES[outcome]
    final = {
        "id": record["id"],
        "outcome": outcome,
        "exit_code": exit_code,
        "reason_code": selected_reason_code,
        "reason": REASON_TEXT[selected_reason_code],
        "recovery": _recovery_for(record, outcome),
        "stage": snapshot.get("stage") if isinstance(snapshot, dict) else None,
        "state": snapshot.get("state") if isinstance(snapshot, dict) else None,
        "status": snapshot.get("status") if isinstance(snapshot, dict) else None,
        "status_display": annotated["status_display"],
        "failure_kind": snapshot.get("failure_kind") if isinstance(snapshot, dict) else None,
        "active": snapshot.get("active") if isinstance(snapshot, dict) else None,
        "archived": snapshot.get("archived") if isinstance(snapshot, dict) else None,
        "archived_at": snapshot.get("archived_at") if isinstance(snapshot, dict) else None,
        "server": server,
        "route": record.get("route"),
        "probes": _final_probe_rows(record, now, local_server),
        "matches": [dict(match) for match in record["matches"]],
        "observed_age_s": (_observed_age(record, now) if isinstance(snapshot, dict) else None),
        "pane_liveness": annotated["pane_liveness"],
        "tmux_pane": current_pane,
        "last_tmux_pane": stale_pane,
        **worktree,
    }
    assert set(final) == FINAL_RECORD_KEYS
    return final


def _stuck_diagnostic(final_record: dict, *, deadline: dict) -> str:
    """Format a stuck record with source-appropriate inspection guidance."""
    lid = final_record["id"]
    status = final_record["status"]
    lines = [f"{STATUS_ERROR} {lid} stuck: {status}" if status else f"{STATUS_ERROR} {lid} stuck"]
    lines.append(f"  {_snapshot_summary(final_record)}")
    tmux_pane = final_record["tmux_pane"]
    if final_record["route"] != "local":
        lines.append(f"  host: {final_record['route']}")
        lines.append(f"  pane: {tmux_pane or '<unknown>'}")
        return "\n".join(lines)
    if not tmux_pane:
        lines.append("  pane: <unknown>")
        return "\n".join(lines)
    lines.append(f"  pane: {tmux_pane}")
    lines.append("  --- last 50 lines of pane ---")
    pane_budget = deadline_utils.claim_call_budget(deadline, "wait.capture_pane")
    pane_capture = capture_pane(tmux_pane, timeout=pane_budget) if pane_budget is not None else None
    if pane_capture:
        lines.extend(f"  {line}" for line in pane_capture.split("\n")[-50:])
    else:
        lines.append("  <pane capture failed>")
    lines.append("  --- end pane ---")
    return "\n".join(lines)


def _snapshot_summary(final_record: dict) -> str:
    """Format only fields from the closed final record."""
    return (
        f"stage={final_record['stage']} state={final_record['state']} "
        f"active={final_record['active']} status={final_record['status_display']} "
        f"route={final_record['route']} "
        f"observed_age_s={final_record['observed_age_s']:.3f}"
    )


def _emit_human_final_details(final_record: dict) -> None:
    """Render the required final context from the closed record."""
    unavailable = "<unavailable>"

    def display(value: object) -> str:
        return unavailable if value is None else str(value)

    server = final_record["server"] or unavailable
    route = final_record["route"] or unavailable
    status = final_record["status"] or unavailable
    pane = final_record["tmux_pane"]
    last_pane = final_record["last_tmux_pane"]
    if pane is not None:
        pane_display = pane
    elif last_pane is not None:
        pane_display = f"{unavailable} (last known: {last_pane})"
    else:
        pane_display = unavailable
    worktree = final_record["worktree_path"] or unavailable
    exists = final_record["worktree_exists"]
    exists_display = unavailable if exists is None else str(exists).lower()
    print(
        f"  why: {final_record['reason']} [{final_record['reason_code']}]\n"
        f"  server: {server} (route: {route})\n"
        f"  status: {status} (stage={display(final_record['stage'])} "
        f"state={display(final_record['state'])} active={display(final_record['active'])} "
        f"archived={display(final_record['archived'])})\n"
        f"  pane: {pane_display}\n"
        f"  worktree: {worktree} (basis={final_record['worktree_path_basis']} "
        f"exists={exists_display})"
    )
    recovery = final_record["recovery"]
    print(f"  recovery: {recovery if recovery is not None else '<none>'}")


def _emit_outcome(
    final_record: dict,
    json_output: bool,
    *,
    deadline: dict,
) -> None:
    """Render one constructed final record without a parallel outcome channel."""
    lid = final_record["id"]
    outcome = final_record["outcome"]
    if json_output:
        print(json.dumps(final_record))
        if outcome == "stuck":
            print(
                _stuck_diagnostic(final_record, deadline=deadline),
                file=sys.stderr,
            )
        return
    if outcome in {
        "target_absent",
        "target_ambiguous",
        "status_unavailable",
        "interrupted",
    }:
        print(f"{STATUS_ERROR} {lid} {outcome}")
    elif outcome == "timeout":
        print(f"Timed out waiting for lode(s): {lid}")
        print(f"  {_snapshot_summary(final_record)}")
    elif outcome == "shipped":
        print(f"{STATUS_SHIPPED} {lid} shipped")
    elif outcome == "archived":
        status = f": {final_record['status']}" if final_record["status"] else ""
        print(f"{STATUS_ERROR} {lid} archived before shipping{status}")
        print(f"  {_snapshot_summary(final_record)}")
    elif outcome in {"startup_stalled", "handoff_stalled", "reconnect_stalled"}:
        label = {
            "startup_stalled": "startup registration stalled",
            "handoff_stalled": "ready handoff stalled",
            "reconnect_stalled": "runner reregistration stalled",
        }[outcome]
        print(f"{STATUS_ERROR} {lid} {label}: {final_record['status']}")
        print(f"  {_snapshot_summary(final_record)}")
    elif outcome in {"action_blocked", "action_stalled"}:
        label = "action blocked" if outcome == "action_blocked" else "successor adoption stalled"
        print(f"{STATUS_ERROR} {lid} {label}: {final_record['status']}")
        print(f"  {_snapshot_summary(final_record)}")
    elif outcome == "error":
        print(f"{STATUS_ERROR} {lid} error: {final_record['status']}")
        print(f"  {_snapshot_summary(final_record)}")
    elif outcome == "gated":
        print(f"Lode {lid} is gated.")
        print(f"  {_snapshot_summary(final_record)}")
    elif outcome == "inactive":
        print(f"Lode '{lid}' is not active ({_snapshot_summary(final_record)})")
    elif outcome == "stuck":
        print(_stuck_diagnostic(final_record, deadline=deadline))
    elif outcome == "not_found":
        print(f"Lode '{lid}' not found ({_snapshot_summary(final_record)})")
    elif outcome == "observer_unavailable":
        print(f"Status observer unavailable for {lid} ({_snapshot_summary(final_record)})")
    _emit_human_final_details(final_record)


def _finish_boundary(state: dict, outcomes: list[dict], now: float) -> int | None:
    """Finalize and render the outcome at one boundary."""
    if not outcomes:
        return None
    item = outcomes[0]
    record = item["record"]
    outcome = item["outcome"]
    final_record = _final_record(
        record,
        outcome,
        item["code"],
        now,
        item.get("reason"),
        deadline=state["deadline"],
        child_control=state["child_control"],
    )
    item["final_record"] = final_record
    _emit_outcome(
        final_record,
        state["json_output"],
        deadline=state["deadline"],
    )
    with state["condition"]:
        state["resolved"] = True
        state["resolved_outcome"] = item
    return item["code"]


def _emit_wait_summary(item: dict, code: int) -> None:
    """Emit the final wait result without risking the established exit code."""
    try:
        final_record = item["final_record"]
        print(
            WAIT_SUMMARY.format(
                lode_id=final_record["id"],
                outcome=final_record["outcome"],
                code=code,
            ),
            file=sys.stderr,
        )
    except Exception:
        # The established wait exit code must win over an optional summary rendering failure.
        logger.debug("Could not render wait summary", exc_info=True)


def _condition_wait(condition: threading.Condition, deadline: dict, wake_at: float) -> None:
    """Wait only when the shared deadline authorizes the blocking call."""
    timeout_s = max(0.0, wake_at - deadline["clock"]())
    budget = deadline_utils.claim_call_budget(
        deadline,
        "wait.condition_wait",
        cap_s=timeout_s,
    )
    if budget is not None:
        condition.wait(timeout=budget)


def _synthesize_interrupted_records(state: dict) -> None:
    """Finalize established truth or interrupt the unresolved record."""
    if state["resolved_outcome"] is None:
        now = _monotonic()
        outcomes = _collect_boundary_outcomes(state, now)
        if not outcomes:
            outcomes = [
                {
                    "record": state["record"],
                    "outcome": "interrupted",
                    "code": 130,
                    "reason": "user_interrupted",
                }
            ]
        _finish_boundary(state, outcomes, now)
    _emit_wait_summary(state["resolved_outcome"], 130)


def _interrupt_cleanup_deadline(deadline: dict) -> dict:
    """Bound interrupt cleanup by both the original deadline and five seconds."""
    interrupt_at = deadline["clock"]()
    return deadline_utils.shorten_deadline(deadline, interrupt_at + 5.0)


def _interrupt_wait(cleanup_deadline: dict, child_control: dict, state: dict) -> int:
    """Cancel owned work within the interrupt bound and return the shell exit."""
    child_control["cancel_event"].set()
    state["stop_event"].set()
    state["shutdown"] = True
    state["deadline"] = cleanup_deadline
    remote.cancel_owned_children(child_control, cleanup_deadline)
    if state.get("connection") is not None:
        state["connection"].stop(deadline=cleanup_deadline)
    _synthesize_interrupted_records(state)
    return 130


def wait_for_lode(
    socket_path: Path,
    lode_id: str,
    *,
    deadline: dict,
    poll_s: float = 30,
    observer_timeout_s: float = 300,
    json_output: bool = False,
    resolver: Callable,
    probe_remote: Callable,
) -> int:
    """Wait for one lode using a main-thread authoritative supervisor."""
    child_control = remote.make_child_registry()
    state: dict | None = None
    condition: threading.Condition | None = None
    cleanup_deadline = deadline
    record: dict | None = None
    try:
        poll_s = max(MIN_POLL_S, float(poll_s or 30))
        record = _resolve_target(
            socket_path,
            lode_id,
            resolver,
            deadline=deadline,
            child_control=child_control,
        )
        _publish_resident_routes(record, deadline=deadline)

        condition = threading.Condition()
        state = {
            "condition": condition,
            "record": record,
            "resolved": False,
            "resolved_outcome": None,
            "observations": deque(),
            "deadline": deadline,
            "overall_deadline": deadline["expires_at"],
            "poll_s": poll_s,
            "observer_timeout_s": max(0.0, observer_timeout_s),
            "probe_timeout_s": max(5.0, min(poll_s, 30.0)),
            "stop_event": threading.Event(),
            "connection": None,
            "child_control": child_control,
            "shutdown": False,
            "json_output": json_output,
        }

        initial_now = _monotonic()
        record["next_reconcile_ts"] = initial_now + poll_s
        initial_outcomes = _collect_boundary_outcomes(state, initial_now)
        if initial_outcomes:
            code = _finish_boundary(state, initial_outcomes, initial_now) or 0
            _emit_wait_summary(state["resolved_outcome"], code)
            return code
        if not record["remote"]:
            connection = client.HopperConnection(socket_path)
            state["connection"] = connection

            def on_message(message: dict) -> None:
                if message.get("type") not in ("lode_updated", "lode_archived"):
                    return
                payload = message.get("lode")
                lid = payload.get("id") if isinstance(payload, dict) else None
                if isinstance(lid, str):
                    _request_local_reconcile(state, lid)

            connection.start(
                callback=on_message,
                on_connect=lambda: _request_local_reconcile(state),
                deadline=deadline,
            )
        _start_remote_workers(state, probe_remote)

        while not state["resolved"]:
            now = _monotonic()
            with condition:
                warnings = _drain_observations(state)
                _mark_due_reconciliations(state, now)
            _read_due_locals(state, socket_path, now)
            now = _monotonic()
            with condition:
                warnings.extend(_drain_observations(state))
                outcomes = _collect_boundary_outcomes(state, now)
            for warning in warnings:
                print(warning, file=sys.stderr)
            if outcomes:
                result = _finish_boundary(state, outcomes, now) or 0
                _emit_wait_summary(state["resolved_outcome"], result)
                return result
            with condition:
                wake_at = _next_deadline(state)
                _condition_wait(condition, deadline, wake_at)
    except KeyboardInterrupt:
        cleanup_deadline = _interrupt_cleanup_deadline(deadline)
        if state is None:
            if record is None:
                record = _resolution_failure_record(
                    lode_id,
                    {
                        "outcome": "unavailable",
                        "probes": [
                            {
                                "kind": "resolution",
                                "server": None,
                                "route": None,
                                "candidate_id": None,
                                "outcome": "not_attempted",
                                "detail": "interrupted before resolution completed",
                                "attempts": 0,
                                "observed_age_s": None,
                            }
                        ],
                    },
                    _monotonic(),
                )
                record["preset_outcome"] = None
                record["preset_code"] = None
                record["preset_reason"] = None
            condition = threading.Condition()
            state = {
                "condition": condition,
                "record": record,
                "resolved": False,
                "resolved_outcome": None,
                "observations": deque(),
                "deadline": deadline,
                "overall_deadline": deadline["expires_at"],
                "poll_s": poll_s,
                "observer_timeout_s": max(0.0, observer_timeout_s),
                "probe_timeout_s": max(5.0, min(poll_s, 30.0)),
                "stop_event": threading.Event(),
                "connection": None,
                "child_control": child_control,
                "shutdown": False,
                "json_output": json_output,
            }
        return _interrupt_wait(cleanup_deadline, child_control, state)
    finally:
        if state is not None and condition is not None:
            with condition:
                state["shutdown"] = True
                condition.notify_all()
            _stop_remote_workers(state)
            if state["connection"] is not None:
                state["connection"].stop(deadline=cleanup_deadline)
        remote.cancel_owned_children(child_control, cleanup_deadline)
