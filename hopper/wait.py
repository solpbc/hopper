# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Authoritative supervised waiting for lode terminal states."""

import json
import logging
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

import hopper.client as client
import hopper.remote as remote
from hopper.lodes import (
    STATUS_ERROR,
    STATUS_SHIPPED,
    is_terminal_failure_kind,
    lode_status_for_display,
    lode_with_status_annotations,
)
from hopper.tmux import capture_pane

STUCK_GRACE_MS = 120_000
MIN_POLL_S = 10.0
WAIT_SUMMARY_ONE = "hop wait: {lode_id} {outcome} — exited {code}"
WAIT_SUMMARY_ITEM = "  {lode_id}: {outcome}"
WAIT_SUMMARY_UNRESOLVED = "  {lode_id}: not resolved — wait returned first"
WAIT_SUMMARY_MANY = "hop wait: {resolved} of {requested} lodes resolved — exited {code}"
WAIT_SUMMARY_INTERRUPT = "hop wait: interrupted — exited 130"
WAIT_SUMMARY_NO_TARGET = "hop wait: could not resolve target — exited 1"

_monotonic = time.monotonic

logger = logging.getLogger(__name__)


def validate_snapshot(raw, expected_lid: str) -> dict | None:
    """Return a copied, well-typed snapshot for exactly the expected lode."""
    if not isinstance(raw, dict) or raw.get("id") != expected_lid:
        return None
    if not all(isinstance(raw.get(field), str) for field in ("stage", "state", "status")):
        return None
    if not isinstance(raw.get("active"), bool):
        return None
    return dict(raw)


def classify(snapshot: dict) -> tuple[str, int] | None:
    """Apply the shared terminal policy; stuck remains subject to grace."""
    if snapshot["state"] == "error":
        return "error", 1
    if snapshot["state"] == "gated":
        return "gated", 2
    if snapshot["state"] == "stuck":
        return "stuck", 3
    if snapshot["stage"] == "shipped":
        return "shipped", 0
    # A clean stage exit briefly disconnects after publishing state=ready and
    # the next stage. The server auto-advances that durable handoff; it is not
    # an inactive failure. Other inactive states still require recovery.
    if not snapshot["active"] and snapshot["state"] != "ready":
        return "inactive", 1
    return None


def read_local_snapshot(
    socket_path: Path, lid: str, timeout: float = 2.0
) -> tuple[str, dict | None]:
    """Read exact active then archived state without conflating absence and failure."""
    response = client.connect(socket_path, lode_id=lid, timeout=timeout)
    if response is None or "lode_found" not in response:
        return "unreadable", None
    if response.get("lode_found") is True:
        return "found", response.get("lode")
    if response.get("lode_found") is not False:
        return "unreadable", None

    archived = client.read_archived_lodes(socket_path, timeout=timeout)
    if archived is None:
        return "unreadable", None
    for lode in archived:
        if isinstance(lode, dict) and lode.get("id") == lid:
            return "found", lode
    return "absent", None


def _new_record(lid: str, snapshot: dict, source: str, observed_ts: float, order: int) -> dict:
    """Create one plain supervisor record from an initial valid snapshot."""
    remote_source = source != "local"
    stuck_since = observed_ts if snapshot["state"] == "stuck" else None
    return {
        "id": lid,
        "order": order,
        "source": source,
        "host": source if remote_source else None,
        "remote": remote_source,
        "latest_snapshot": snapshot,
        "latest_snapshot_ts": observed_ts,
        "last_valid_ts": observed_ts,
        "next_reconcile_ts": observed_ts,
        "reconcile_requested": False,
        "stuck_since": stuck_since,
        "stuck_recheck_pending": False,
        "stuck_confirmed": False,
        "consecutive_failures": 0,
        "warned_failure_key": None,
        "not_found_count": 0,
    }


def _initial_error(message: str, json_output: bool) -> None:
    """Keep JSON stdout clean while preserving human lookup errors."""
    print(message, file=sys.stderr if json_output else sys.stdout)


def _resolve_targets(
    socket_path: Path,
    raw_ids: list[str],
    json_output: bool,
    resolver: Callable,
) -> dict[str, dict] | int:
    """Resolve all requested IDs to validated canonical local or remote snapshots."""
    records: dict[str, dict] = {}
    for order, raw_id in enumerate(raw_ids):
        result = resolver(socket_path, raw_id)
        if result.get("outcome") != "found":
            _initial_error(result.get("error", f"Lode '{raw_id}' not found."), json_output)
            return result.get("exit_code", 1)
        lode = result.get("lode")

        lid = lode.get("id") if isinstance(lode, dict) else None
        if not isinstance(lid, str):
            _initial_error(f"Lode status unavailable for '{raw_id}'.", json_output)
            return 2
        snapshot = validate_snapshot(lode, lid)
        if snapshot is None:
            _initial_error(f"Lode status unavailable for '{raw_id}'.", json_output)
            return 2
        if lid in records:
            continue
        source = result.get("host")
        if not isinstance(source, str):
            _initial_error(f"Lode status unavailable for '{raw_id}'.", json_output)
            return 2
        observed_ts = _monotonic()
        records[lid] = _new_record(lid, snapshot, source, observed_ts, order)
    return records


def _publish_remote_mappings(records: dict[str, dict]) -> None:
    """Publish only new or changed initial remote mappings, warning once on failure."""
    remote_records = [record for record in records.values() if record["remote"]]
    if not remote_records:
        return
    try:
        cache = remote.load_lode_cache()
    except Exception as error:
        print(f"warning: could not read remote lode cache: {error}", file=sys.stderr)
        return

    warned = False
    for record in remote_records:
        lid = record["id"]
        host = record["host"]
        if cache.get(lid, {}).get("host") == host:
            continue
        try:
            remote.remember_lode(
                lid,
                host,
                record["latest_snapshot"].get("project", ""),
            )
            cache[lid] = {"host": host}
        except Exception as error:
            if not warned:
                print(f"warning: could not update remote lode cache: {error}", file=sys.stderr)
                warned = True


def _post_observation(state: dict, observation: dict) -> None:
    """Append one observation and wake the supervisor."""
    with state["condition"]:
        if state["shutdown"]:
            return
        state["observations"].append(observation)
        state["condition"].notify()


def _request_local_reconcile(state: dict, lid: str | None = None) -> None:
    """Request main-thread authoritative reads for pending local records."""
    with state["condition"]:
        if state["shutdown"]:
            return
        for record_id in state["pending"]:
            record = state["records"][record_id]
            if not record["remote"] and (lid is None or lid == record_id):
                record["reconcile_requested"] = True
        state["condition"].notify()


def _remote_worker(
    state: dict,
    lid: str,
    host: str,
    interval_s: float,
    probe_timeout_s: float,
    probe_remote: Callable,
) -> None:
    """Post every bounded remote probe result without classifying it."""
    while not state["stop_event"].is_set():
        with state["condition"]:
            if state["shutdown"] or lid not in state["pending"]:
                return
        try:
            snapshot, probe_state = probe_remote(host, lid, timeout=probe_timeout_s)
            if probe_state == "found":
                kind = "found"
                detail = ""
            elif probe_state == "absent":
                kind = "absent"
                detail = "lode absent"
            else:
                kind = "unreadable"
                detail = "remote status unreadable"
            observation = {
                "id": lid,
                "kind": kind,
                "payload": snapshot,
                "detail": detail,
                "failure_key": kind,
                "observed_ts": _monotonic(),
            }
        except Exception as error:
            logger.debug("Unexpected remote observer error for %s on %s", lid, host, exc_info=True)
            observation = {
                "id": lid,
                "kind": "observer_error",
                "payload": None,
                "detail": f"unexpected {type(error).__name__}",
                "failure_key": f"observer_error:{type(error).__name__}",
                "observed_ts": _monotonic(),
            }
        _post_observation(state, observation)
        if state["stop_event"].wait(interval_s):
            return


def _start_remote_workers(state: dict, probe_remote: Callable) -> None:
    """Start one daemon observer for each pending remote lode."""
    for lid in list(state["pending"]):
        record = state["records"][lid]
        if not record["remote"]:
            continue
        thread = threading.Thread(
            target=_remote_worker,
            args=(
                state,
                lid,
                record["host"],
                state["poll_s"],
                state["probe_timeout_s"],
                probe_remote,
            ),
            daemon=True,
            name=f"wait-remote-{lid}",
        )
        thread.start()
        state["workers"][lid] = thread


def _stop_remote_workers(state: dict) -> None:
    """Interrupt and bounded-join every remote observer."""
    state["stop_event"].set()
    for lid, thread in state["workers"].items():
        thread.join(timeout=state["probe_timeout_s"] + 1.0)
        if thread.is_alive():
            logger.warning("Remote wait observer for %s did not stop before join timeout", lid)


def _record_observer_failure(record: dict, kind: str, detail: str, failure_key: str) -> str | None:
    """Track a failure streak and return at most one warning per repeated failure."""
    record["not_found_count"] = 0
    record["consecutive_failures"] += 1
    if record["consecutive_failures"] < 2 or record["warned_failure_key"] == failure_key:
        return None
    record["warned_failure_key"] = failure_key
    return (
        f"warning: status observer for {record['id']} ({record['source']}) failed: {detail or kind}"
    )


def _apply_observation(record: dict, observation: dict, poll_s: float) -> str | None:
    """Apply one observation to a record without emitting or finishing it."""
    observed_ts = observation["observed_ts"]
    record["next_reconcile_ts"] = observed_ts + poll_s
    kind = observation["kind"]
    if kind == "found":
        snapshot = validate_snapshot(observation.get("payload"), record["id"])
        if snapshot is None:
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
        if snapshot["state"] == "stuck":
            if record["stuck_since"] is None:
                record["stuck_since"] = observed_ts
            deadline = record["stuck_since"] + STUCK_GRACE_MS / 1000.0
            if record["stuck_recheck_pending"] and observed_ts >= deadline:
                record["stuck_confirmed"] = True
        else:
            record["stuck_since"] = None
            record["stuck_recheck_pending"] = False
            record["stuck_confirmed"] = False
        return None

    if kind == "absent":
        record["consecutive_failures"] = 0
        record["warned_failure_key"] = None
        record["not_found_count"] += 1
        return None

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
        record = state["records"].get(observation.get("id"))
        if not record or record["id"] not in state["pending"]:
            continue
        warning = _apply_observation(record, observation, state["poll_s"])
        if warning:
            warnings.append(warning)
    return warnings


def _mark_due_reconciliations(state: dict, now: float) -> None:
    """Turn expired grace and periodic deadlines into reconciliation work."""
    for lid in state["pending"]:
        record = state["records"][lid]
        if record["stuck_since"] is not None and not record["stuck_recheck_pending"]:
            stuck_deadline = record["stuck_since"] + STUCK_GRACE_MS / 1000.0
            if now >= stuck_deadline:
                record["stuck_recheck_pending"] = True
                if not record["remote"]:
                    record["reconcile_requested"] = True
        if record["remote"] and now >= record["next_reconcile_ts"]:
            record["next_reconcile_ts"] = now + state["poll_s"]


def _read_due_locals(state: dict, socket_path: Path, now: float) -> None:
    """Perform all due local reads on the supervisor thread."""
    due = []
    with state["condition"]:
        for lid in state["pending"]:
            record = state["records"][lid]
            if record["remote"]:
                continue
            if record["reconcile_requested"] or now >= record["next_reconcile_ts"]:
                record["reconcile_requested"] = False
                record["next_reconcile_ts"] = now + state["poll_s"]
                due.append(lid)

    for lid in due:
        try:
            kind, payload = read_local_snapshot(socket_path, lid)
            detail = "" if kind == "found" else f"local status {kind}"
            observation = {
                "id": lid,
                "kind": kind,
                "payload": payload,
                "detail": detail,
                "failure_key": kind,
                "observed_ts": _monotonic(),
            }
        except Exception as error:
            logger.debug("Unexpected local observer error for %s", lid, exc_info=True)
            observation = {
                "id": lid,
                "kind": "observer_error",
                "payload": None,
                "detail": f"unexpected {type(error).__name__}",
                "failure_key": f"observer_error:{type(error).__name__}",
                "observed_ts": _monotonic(),
            }
        _post_observation(state, observation)


def _next_deadline(state: dict) -> float:
    """Return the earliest active supervisor deadline."""
    deadlines = []
    if state["overall_deadline"] is not None:
        deadlines.append(state["overall_deadline"])
    for lid in state["pending"]:
        record = state["records"][lid]
        deadlines.append(record["next_reconcile_ts"])
        if state["observer_timeout_s"] > 0:
            deadlines.append(record["last_valid_ts"] + state["observer_timeout_s"])
        if record["stuck_since"] is not None and not record["stuck_recheck_pending"]:
            deadlines.append(record["stuck_since"] + STUCK_GRACE_MS / 1000.0)
    return min(deadlines)


def _collect_boundary_outcomes(state: dict, now: float) -> list[dict]:
    """Collect every terminal sibling known at this reconciliation boundary."""
    outcomes = []
    for lid in sorted(state["pending"], key=lambda item: state["records"][item]["order"]):
        record = state["records"][lid]
        terminal = classify(record["latest_snapshot"])
        if terminal and terminal[0] != "stuck":
            outcome, code = terminal
        elif record["not_found_count"] >= 2:
            outcome, code = "not_found", 1
        elif record["stuck_confirmed"]:
            outcome, code = "stuck", 3
        elif state["observer_timeout_s"] > 0 and now >= (
            record["last_valid_ts"] + state["observer_timeout_s"]
        ):
            outcome, code = "observer_unavailable", 4
        elif state["overall_deadline"] is not None and now >= state["overall_deadline"]:
            outcome, code = "timeout", 4
        else:
            continue
        outcomes.append({"record": record, "outcome": outcome, "code": code})
    return outcomes


def _observed_age(record: dict, now: float) -> float:
    return round(max(0.0, now - record["latest_snapshot_ts"]), 3)


def _json_event(record: dict, outcome: str, now: float) -> dict:
    """Build one additive, compatibility-preserving JSONL terminal record."""
    snapshot = record["latest_snapshot"]
    annotated = lode_with_status_annotations({**snapshot, "host": record["host"]})
    event = {
        "id": record["id"],
        "outcome": outcome,
        "stage": snapshot["stage"],
        "state": snapshot["state"],
        "status": snapshot["status"],
        "failure_kind": snapshot.get("failure_kind"),
        "active": snapshot["active"],
        "source": record["source"],
        "observed_age_s": _observed_age(record, now),
        "status_display": annotated["status_display"],
        "pane_liveness": annotated["pane_liveness"],
    }
    if record["host"]:
        event["host"] = record["host"]
    return event


def _stuck_diagnostic(record: dict, now: float) -> str:
    """Format a stuck record with source-appropriate inspection guidance."""
    snapshot = record["latest_snapshot"]
    lid = record["id"]
    status = snapshot["status"]
    lines = [f"{STATUS_ERROR} {lid} stuck: {status}" if status else f"{STATUS_ERROR} {lid} stuck"]
    lines.append(f"  {_snapshot_summary(record, now)}")
    tmux_pane = snapshot.get("tmux_pane")
    if record["remote"]:
        lines.append(f"  host: {record['host']}")
        lines.append(f"  pane: {tmux_pane or '<unknown>'}")
        lines.append(f"Inspect with: hop -H {record['host']} lode peek {lid}")
        return "\n".join(lines)
    if not tmux_pane:
        lines.append("  pane: <unknown>")
        return "\n".join(lines)
    lines.append(f"  pane: {tmux_pane}")
    lines.append("  --- last 50 lines of pane ---")
    pane_capture = capture_pane(tmux_pane)
    if pane_capture:
        lines.extend(f"  {line}" for line in pane_capture.split("\n")[-50:])
    else:
        lines.append("  <pane capture failed>")
    lines.append("  --- end pane ---")
    return "\n".join(lines)


def _snapshot_summary(record: dict, now: float) -> str:
    snapshot = record["latest_snapshot"]
    return (
        f"stage={snapshot['stage']} state={snapshot['state']} active={snapshot['active']} "
        f"status={snapshot['status']} source={record['source']} "
        f"observed_age_s={_observed_age(record, now):.3f}"
    )


def _emit_outcome(record: dict, outcome: str, json_output: bool, now: float) -> None:
    """Emit one non-timeout terminal outcome from the latest valid snapshot."""
    snapshot = record["latest_snapshot"]
    lid = record["id"]
    if json_output:
        print(json.dumps(_json_event(record, outcome, now)))
        if outcome == "stuck":
            print(_stuck_diagnostic(record, now), file=sys.stderr)
        return
    if outcome == "shipped":
        title = snapshot.get("title", "")
        suffix = f" ({title})" if title else ""
        print(f"{STATUS_SHIPPED} {lid} shipped{suffix}")
    elif outcome == "error":
        print(f"{STATUS_ERROR} {lid} error: {snapshot['status']}")
        print(f"  {_snapshot_summary(record, now)}")
        if not is_terminal_failure_kind(snapshot.get("failure_kind")):
            print(f"Lode {lid} entered error state. Restart with: hop lode restart {lid}")
    elif outcome == "gated":
        display_status = lode_status_for_display({**snapshot, "host": record["host"]})
        display_snapshot = {**snapshot, "status": display_status}
        display_record = {**record, "latest_snapshot": display_snapshot}
        print(f"Lode {lid} is gated. Review with: hop gate show {lid}")
        print(f"  {_snapshot_summary(display_record, now)}")
    elif outcome == "inactive":
        print(f"Lode '{lid}' is not active ({_snapshot_summary(record, now)})")
        print(f"Recover with: hop lode resume {lid} or hop lode restart {lid}")
    elif outcome == "stuck":
        print(_stuck_diagnostic(record, now))
    elif outcome == "not_found":
        print(f"Lode '{lid}' not found ({_snapshot_summary(record, now)})")
        print(f"Inspect with: hop lode status {lid}")
    elif outcome == "observer_unavailable":
        print(f"Status observer unavailable for {lid} ({_snapshot_summary(record, now)})")
        print(f"Check status with: hop lode status {lid}")
        print(f"Retry with: hop wait {lid}")


def _finish_boundary(state: dict, outcomes: list[dict], now: float) -> int | None:
    """Report a boundary, remove successes, and return its nonzero result."""
    timeout_items = [item for item in outcomes if item["outcome"] == "timeout"]
    if timeout_items and not state["json_output"]:
        ids = ", ".join(item["record"]["id"] for item in timeout_items)
        print(f"Timed out waiting for lode(s): {ids}")
        for item in timeout_items:
            print(f"  {item['record']['id']}: {_snapshot_summary(item['record'], now)}")

    result = 0
    for item in outcomes:
        record = item["record"]
        outcome = item["outcome"]
        if outcome == "timeout" and not state["json_output"]:
            pass
        else:
            _emit_outcome(record, outcome, state["json_output"], now)
        with state["condition"]:
            state["pending"].discard(record["id"])
        state["resolved"].append(item)
        result = max(result, item["code"])
    return result if result else None


def _emit_wait_summary(records: dict[str, dict], resolved: list[dict], code: int) -> None:
    """Emit the final wait result without risking the established exit code."""
    try:
        requested = len(records)
        if requested == 1:
            record = next(iter(records.values()))
            item = resolved[0]
            lines = [
                WAIT_SUMMARY_ONE.format(
                    lode_id=record["id"],
                    outcome=item["outcome"],
                    code=code,
                )
            ]
        else:
            resolved_by_id = {item["record"]["id"]: item for item in resolved}
            lines = []
            for record in sorted(records.values(), key=lambda record: record["order"]):
                item = resolved_by_id.get(record["id"])
                if item is None:
                    lines.append(WAIT_SUMMARY_UNRESOLVED.format(lode_id=record["id"]))
                else:
                    lines.append(
                        WAIT_SUMMARY_ITEM.format(
                            lode_id=record["id"],
                            outcome=item["outcome"],
                        )
                    )
            lines.append(
                WAIT_SUMMARY_MANY.format(
                    resolved=len(resolved),
                    requested=requested,
                    code=code,
                )
            )
        print("\n".join(lines), file=sys.stderr)
    except Exception:
        # The established wait exit code must win over an optional summary rendering failure.
        logger.debug("Could not render wait summary", exc_info=True)


def _condition_wait(condition: threading.Condition, timeout_s: float) -> None:
    """Wait on the supervisor condition; monkeypatched by deterministic tests."""
    condition.wait(timeout=timeout_s)


def wait_for_lodes(
    socket_path: Path,
    lode_ids: list[str],
    *,
    timeout_s: float = 0,
    poll_s: float = 30,
    observer_timeout_s: float = 300,
    json_output: bool = False,
    resolver: Callable,
    probe_remote: Callable,
) -> int:
    """Wait for lodes using one main-thread authoritative supervisor."""
    try:
        poll_s = max(MIN_POLL_S, float(poll_s or 30))
        records = _resolve_targets(socket_path, lode_ids, json_output, resolver)
        if not isinstance(records, dict):
            print(WAIT_SUMMARY_NO_TARGET, file=sys.stderr)
            return records if isinstance(records, int) else 1
        _publish_remote_mappings(records)

        start_ts = min(record["last_valid_ts"] for record in records.values())
        condition = threading.Condition()
        state = {
            "condition": condition,
            "records": records,
            "pending": set(records),
            "resolved": [],
            "observations": deque(),
            "overall_deadline": start_ts + timeout_s if timeout_s > 0 else None,
            "poll_s": poll_s,
            "observer_timeout_s": max(0.0, observer_timeout_s),
            "probe_timeout_s": max(5.0, min(poll_s, 30.0)),
            "stop_event": threading.Event(),
            "workers": {},
            "connection": None,
            "shutdown": False,
            "json_output": json_output,
        }

        initial_now = _monotonic()
        for record in records.values():
            record["next_reconcile_ts"] = initial_now + poll_s
        initial_outcomes = _collect_boundary_outcomes(state, initial_now)
        initial_result = _finish_boundary(state, initial_outcomes, initial_now)
        if initial_result is not None or not state["pending"]:
            code = initial_result or 0
            _emit_wait_summary(records, state["resolved"], code)
            return code
    except KeyboardInterrupt:
        print(WAIT_SUMMARY_INTERRUPT, file=sys.stderr)
        return 130

    try:
        local_pending = any(not records[lid]["remote"] for lid in state["pending"])
        if local_pending:
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
            )
        _start_remote_workers(state, probe_remote)

        while state["pending"]:
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
            result = _finish_boundary(state, outcomes, now)
            if result is not None:
                _emit_wait_summary(records, state["resolved"], result)
                return result
            if not state["pending"]:
                _emit_wait_summary(records, state["resolved"], 0)
                return 0
            with condition:
                deadline = _next_deadline(state)
                _condition_wait(condition, max(0.0, deadline - _monotonic()))
    except KeyboardInterrupt:
        print(WAIT_SUMMARY_INTERRUPT, file=sys.stderr)
        return 130
    finally:
        with condition:
            state["shutdown"] = True
            condition.notify_all()
        if state["connection"] is not None:
            state["connection"].stop()
        state["stop_event"].set()
        _stop_remote_workers(state)
