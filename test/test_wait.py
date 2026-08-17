# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for authoritative supervised lode waiting."""

import copy
import json
import subprocess
import threading
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import hopper.wait as wait
from hopper import actions, remote
from hopper import client as hopper_client
from hopper import deadline as deadline_utils
from hopper.deadline import make_deadline
from hopper.lodes import PARK_PANE_GONE_STATUS, format_park_status, format_terminal_failure_status
from hopper.tmux import Liveness


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeConnection:
    def __init__(self, on_start=None):
        self.on_start = on_start
        self.callback = None
        self.on_connect = None
        self.stopped = False

    def start(self, callback=None, on_connect=None, *, deadline=None):
        del deadline
        self.callback = callback
        self.on_connect = on_connect
        if self.on_start:
            self.on_start(callback, on_connect)
        elif on_connect:
            on_connect()

    def stop(self, *, deadline=None):
        del deadline
        self.stopped = True


def snapshot(lid="abc123", **overrides):
    return {
        "id": lid,
        "stage": "mill",
        "state": "running",
        "status": "Working",
        "active": True,
        "archived": False,
        "title": "",
        **overrides,
    }


def configured_probe(
    route="local",
    *,
    kind=None,
    outcome="found",
    candidate_id=None,
    attempts=1,
):
    return {
        "kind": kind or ("local" if route == "local" else "routed"),
        "server": "test-host" if route == "local" else route,
        "route": route,
        "candidate_id": candidate_id,
        "outcome": outcome,
        "detail": None,
        "attempts": attempts,
        "observed_age_s": 0.0 if attempts else None,
    }


def test_record_construction_requires_configured_source_evidence():
    with pytest.raises(ValueError, match="configured-source probe evidence"):
        wait._new_record("abc123", snapshot(), "local", 0.0, probes=[])
    with pytest.raises(ValueError, match="configured-source probe evidence"):
        wait._resolution_failure_record("abc123", {"outcome": "absent"}, 0.0)


def pending_action_projection(phase: str, *, recovery_command: str | None = None) -> dict:
    record = actions.new_pending_action(
        lode_id="abcd2345",
        stage="mill",
        expected_generation=None,
        action_type="restart",
        target_disposition="replacement_spawned",
        force_consent=True,
        action_id="a" * 32,
        accepted_ms=1_000,
        already_empty=True,
    )
    record["phase"] = phase
    if recovery_command is not None:
        record["recovery"] = {
            "kind": "spawn",
            "message": "durable action requires attention",
            "command": recovery_command,
        }
    return actions.pending_action_projection(record)


def run_local_wait(
    monkeypatch,
    initial,
    observations=(),
    *,
    timeout_s=3600,
    observer_timeout_s=300,
    json_output=False,
    on_start=None,
    wait_action=None,
):
    clock = FakeClock()
    scripted = deque(observations)
    last = ("found", initial)
    connection = FakeConnection(on_start)

    monkeypatch.setattr(wait, "_monotonic", clock)

    def condition_wait(condition, deadline, wake_at):
        timeout = max(0.0, wake_at - clock())
        if wait_action:
            wait_action(clock, timeout, connection)
        else:
            clock.now += timeout

    monkeypatch.setattr(wait, "_condition_wait", condition_wait)
    monkeypatch.setattr(wait.client, "get_lode", lambda *args, **kwargs: dict(initial))
    monkeypatch.setattr(wait.client, "HopperConnection", lambda socket_path: connection)

    def read_snapshot(socket_path, lid, timeout=2.0, *, deadline=None):
        del deadline, timeout
        nonlocal last
        if scripted:
            current = scripted.popleft()
            if isinstance(current, Exception):
                raise current
            last = current
        return last

    monkeypatch.setattr(wait.client, "read_lode_snapshot", read_snapshot)
    rc = wait.wait_for_lode(
        Path("server.sock"),
        initial["id"],
        deadline=make_deadline(timeout_s, clock=clock),
        poll_s=30,
        observer_timeout_s=observer_timeout_s,
        json_output=json_output,
        resolver=lambda socket_path, lid, **_kwargs: {
            "outcome": "found",
            "lode": dict(initial),
            "host": "local",
            "canonical_id": initial["id"],
            "probes": [configured_probe()],
            "exit_code": 0,
        },
        probe_remote=lambda *args, **kwargs: (None, "unreadable"),
    )
    return rc, clock, connection


def install_synchronous_remote_driver(monkeypatch, clock):
    """Run one production worker iteration at each simulated poll deadline."""
    holder = {}

    def post_one(state, probe_remote):
        one_shot = threading.Event()
        worker_state = {**state, "stop_event": one_shot}

        def probe_once(*args, **kwargs):
            try:
                return probe_remote(*args, **kwargs)
            finally:
                one_shot.set()

        wait._remote_worker_group(
            worker_state,
            state["poll_s"],
            probe_once,
        )

    def start_workers(state, probe_remote):
        holder["state"] = state
        holder["probe"] = probe_remote
        if state["record"]["remote"]:
            post_one(state, probe_remote)

    def condition_wait(condition, deadline, wake_at):
        timeout = max(0.0, wake_at - clock())
        clock.now += timeout
        state = holder["state"]
        record = state["record"]
        if not state["resolved"] and record["remote"] and clock.now >= record["next_reconcile_ts"]:
            post_one(state, holder["probe"])

    monkeypatch.setattr(wait, "_start_remote_workers", start_workers)
    monkeypatch.setattr(wait, "_stop_remote_workers", lambda state: state["stop_event"].set())
    monkeypatch.setattr(wait, "_condition_wait", condition_wait)


def run_remote_wait(
    monkeypatch,
    initial,
    probes,
    *,
    timeout_s=3600,
    observer_timeout_s=300,
    json_output=False,
    publish=True,
):
    clock = FakeClock()
    install_synchronous_remote_driver(monkeypatch, clock)
    monkeypatch.setattr(wait, "_monotonic", clock)
    monkeypatch.setattr(wait.client, "get_lode", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        wait.remote,
        "run_remote",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", ""),
    )
    if not publish:
        monkeypatch.setattr(wait, "_publish_resident_routes", lambda record, **_kwargs: None)

    def resolver(socket_path, lid, **_kwargs):
        return {
            "outcome": "found",
            "lode": dict(initial),
            "host": initial["host"],
            "canonical_id": initial["id"],
            "probes": [configured_probe(initial["host"])],
            "exit_code": 0,
        }

    queue = deque(probes)
    last = None

    def probe_remote(host, lid, **_kwargs):
        nonlocal last
        if queue:
            current = queue.popleft()
            last = current
        else:
            current = last
        if isinstance(current, Exception):
            raise current
        return current

    rc = wait.wait_for_lode(
        Path("server.sock"),
        initial["id"],
        deadline=make_deadline(timeout_s, clock=clock),
        poll_s=30,
        observer_timeout_s=observer_timeout_s,
        json_output=json_output,
        resolver=resolver,
        probe_remote=probe_remote,
    )
    return rc, clock


def test_deadline_call_budget_ledger_covers_wait_blockers(monkeypatch, tmp_path):
    clock = FakeClock()
    ledger = []
    expirations = []
    refused = []
    original_claim = deadline_utils.claim_call_budget

    def record_claim(deadline, operation, *, cap_s=None):
        start = deadline["clock"]()
        budget = original_claim(deadline, operation, cap_s=cap_s)
        if budget is None:
            refused.append((operation, start))
        else:
            ledger.append((operation, start, budget))
            expirations.append(deadline["expires_at"])
        return budget

    monkeypatch.setattr(deadline_utils, "claim_call_budget", record_claim)
    monkeypatch.setattr(wait, "_monotonic", clock)

    resolution_deadline = make_deadline(0.01, clock=clock)

    def nearly_exhausted_resolver(_socket_path, _lid, **_kwargs):
        clock.advance(0.009)
        return {
            "outcome": "found",
            "lode": snapshot(),
            "host": "local",
            "canonical_id": "abc123",
            "probes": [configured_probe()],
            "exit_code": 0,
        }

    record = wait._resolve_target(
        Path("server.sock"),
        "abc123",
        nearly_exhausted_resolver,
        deadline=resolution_deadline,
        child_control=remote.make_child_registry(),
    )
    assert isinstance(record, dict)

    exchange_calls = []

    def exchange(*_args, **_kwargs):
        exchange_calls.append(clock())
        clock.advance(0.005)
        return {"type": "lode_snapshot", "result": "absent"}

    monkeypatch.setattr(hopper_client, "_exchange_message", exchange)
    snapshot_deadline = make_deadline(0.01, clock=clock)
    assert hopper_client.read_lode_snapshot(
        Path("server.sock"),
        "missing",
        deadline=snapshot_deadline,
    ) == ("absent", None)
    clock.advance(0.005)
    assert (
        hopper_client.read_lode_snapshot(
            Path("server.sock"),
            "missing",
            deadline=snapshot_deadline,
        )[0]
        == "unavailable"
    )
    assert len(exchange_calls) == 1

    route_deadline = make_deadline(1.0, clock=clock)
    monkeypatch.setattr(remote.config, "hopper_dir", lambda: tmp_path)
    with remote._lode_cache_lock(deadline=route_deadline):
        pass
    assert remote._read_lode_cache(deadline=route_deadline) == ({}, False)
    remote._save_lode_cache({}, deadline=route_deadline)

    remote_record = wait._new_record(
        "remote1",
        snapshot(lid="remote1", host="worker.example", project="project"),
        "worker.example",
        clock(),
        probes=[configured_probe("worker.example")],
    )
    route_calls = []
    monkeypatch.setattr(
        wait.remote,
        "load_lode_cache",
        lambda **_kwargs: route_calls.append("load") or {},
    )
    monkeypatch.setattr(
        wait.remote,
        "remember_lode",
        lambda *_args, **_kwargs: route_calls.append("write"),
    )
    wait._publish_resident_routes(remote_record, deadline=route_deadline)
    assert route_calls == ["load", "write"]

    probe_deadline = make_deadline(0.01, clock=clock)
    probe_calls = []

    def blocked_probe(*_args, **_kwargs):
        probe_calls.append(clock())
        clock.advance(0.01)
        return None, "unreadable"

    wait._probe_remote_observation(
        "remote1",
        "worker.example",
        probe_deadline,
        blocked_probe,
        remote.make_child_registry(),
    )
    wait._probe_remote_observation(
        "remote1",
        "worker.example",
        probe_deadline,
        blocked_probe,
        remote.make_child_registry(),
    )
    assert len(probe_calls) == 1

    enrichment_deadline = make_deadline(0.01, clock=clock)
    enrich_calls = []

    def annotate(lode, **_kwargs):
        enrich_calls.append(lode["id"])
        clock.advance(0.01)
        return {**lode, "status_display": lode["status"], "pane_liveness": "alive"}

    monkeypatch.setattr(wait, "lode_with_status_annotations", annotate)
    wait._final_record(
        remote_record,
        "shipped",
        0,
        clock(),
        None,
        deadline=enrichment_deadline,
        child_control=remote.make_child_registry(),
    )
    wait._final_record(
        remote_record,
        "shipped",
        0,
        clock(),
        None,
        deadline=enrichment_deadline,
        child_control=remote.make_child_registry(),
    )
    assert enrich_calls == ["remote1"]

    class NoWaitCondition:
        def wait(self, timeout):
            raise AssertionError(f"condition wait started after expiry: {timeout}")

    wait._condition_wait(NoWaitCondition(), enrichment_deadline, clock() + 1.0)

    class NoStartThread:
        def __init__(self, **_kwargs):
            raise AssertionError("worker created after expiry")

    monkeypatch.setattr(wait.threading, "Thread", NoStartThread)
    worker_state = {
        "record": remote_record,
        "resolved": False,
        "poll_s": 30,
        "probe_timeout_s": 5,
        "deadline": enrichment_deadline,
        "child_control": remote.make_child_registry(),
        "stop_event": threading.Event(),
    }
    wait._start_remote_workers(worker_state, blocked_probe)
    wait._stop_remote_workers(worker_state)
    assert worker_state["stop_event"].is_set()

    connection = hopper_client.HopperConnection(Path("server.sock"))

    class NoJoinThread:
        def join(self, timeout):
            raise AssertionError(f"connection joined after expiry: {timeout}")

        def is_alive(self):
            return True

    connection.thread = NoJoinThread()
    connection.stop(deadline=enrichment_deadline)

    class CleanupChild:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

        def kill(self):
            self.returncode = -9

        def wait(self, timeout):
            assert timeout == 0
            return self.returncode

    child_control = remote.make_child_registry()
    cleanup_child = CleanupChild()
    child_control["children"].add(cleanup_child)
    remote.cancel_owned_children(child_control, enrichment_deadline)
    assert cleanup_child.poll() == -9
    assert child_control["children"] == set()

    assert ledger
    assert all(budget > 0 for _operation, _start, budget in ledger)
    assert all(
        budget <= expires_at - start
        for (_operation, start, budget), expires_at in zip(ledger, expirations, strict=True)
    )
    assert {
        "client.read_lode_snapshot",
        "remote.cache_lock",
        "remote.cache_read",
        "remote.cache_write",
        "wait.route_cache_load",
        "wait.route_cache_remember",
        "wait.remote_probe",
        "wait.pane_liveness",
    } <= {operation for operation, _start, _budget in ledger}
    assert {
        "client.read_lode_snapshot",
        "wait.remote_probe",
        "wait.pane_liveness",
        "wait.condition_wait",
        "wait.remote_worker_start",
        "client.connection_stop",
        "remote.child_reap_after_terminate",
    } <= {operation for operation, _start in refused}


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"state": "error"}, ("error", 1, None)),
        ({"state": "gated"}, ("gated", 2, None)),
        ({"state": "stuck"}, ("stuck", 3, None)),
        ({"stage": "shipped", "active": False}, ("shipped", 0, None)),
        ({"active": False}, ("inactive", 1, None)),
        ({"state": "new", "active": False}, None),
        ({"state": "ready", "active": False}, None),
        ({"state": "design"}, None),
        ({"state": "completed", "stage": "refine"}, None),
    ],
)
def test_classify_uses_shared_terminal_policy(changes, expected):
    assert wait.classify(snapshot(**changes)) == expected


@pytest.mark.parametrize(
    ("changes", "outcome", "code"),
    [
        ({"stage": "shipped", "active": False}, "shipped", 0),
        ({"state": "error", "status": "Failed"}, "error", 1),
        ({"state": "gated"}, "gated", 2),
        ({"active": False, "state": "paused"}, "inactive", 1),
    ],
)
def test_json_terminal_records_have_stable_and_additive_fields(
    monkeypatch, capsys, changes, outcome, code
):
    initial = snapshot(**changes)
    rc, _, _ = run_local_wait(monkeypatch, initial, json_output=True)

    assert rc == code
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == wait.FINAL_RECORD_KEYS
    assert {"host", "source"}.isdisjoint(payload)
    assert payload["id"] == "abc123"
    assert payload["outcome"] == outcome
    assert payload["exit_code"] == code
    assert payload["reason_code"] == wait.REASON_CODES[outcome]
    assert payload["reason"] == wait.REASON_TEXT[payload["reason_code"]]
    if outcome == "shipped":
        assert payload["recovery"] is None
    else:
        assert isinstance(payload["recovery"], str)
    assert payload["stage"] == initial["stage"]
    assert payload["state"] == initial["state"]
    assert payload["status"] == initial["status"]
    assert payload["server"] == "test-host"
    assert payload["route"] == "local"
    assert payload["observed_age_s"] == 0.0
    assert payload["status_display"] == initial["status"]
    assert payload["pane_liveness"] == "not_probed"


def test_every_final_reason_code_has_plain_language_text():
    assert set(wait.REASON_TEXT) == set(wait.REASON_CODES.values())


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {"id": "wrong", "stage": "mill", "state": "running", "status": "", "active": True},
        {"id": "abc123", "state": "running", "status": "", "active": True},
        {"id": "abc123", "stage": 1, "state": "running", "status": "", "active": True},
        {"id": "abc123", "stage": "mill", "state": "running", "status": "", "active": 1},
        {
            "id": "abc123",
            "stage": "mill",
            "state": "running",
            "status": "",
            "active": True,
            "archived": "false",
        },
    ],
)
def test_validate_snapshot_rejects_malformed_or_wrong_lode(raw):
    assert wait.validate_snapshot(raw, "abc123") is None


@pytest.mark.parametrize(
    ("result", "expected_kind", "expected_payload", "expected_detail"),
    [
        (("found", snapshot()), "found", snapshot(), ""),
        (("absent", None), "absent", None, "local status absent"),
        (
            ("unavailable", "server timed out"),
            "unavailable",
            None,
            "local status unavailable: server timed out",
        ),
        (
            ("ambiguous", ["abc123", "abc999"]),
            "ambiguous",
            None,
            "local status ambiguous: abc123, abc999",
        ),
    ],
)
def test_read_due_locals_uses_one_bounded_snapshot(
    monkeypatch, result, expected_kind, expected_payload, expected_detail
):
    record = wait._new_record("abc123", snapshot(), "local", 0.0, probes=[configured_probe()])
    record["reconcile_requested"] = True
    state = {
        "condition": threading.Condition(),
        "record": record,
        "resolved": False,
        "observations": deque(),
        "poll_s": 30.0,
        "shutdown": False,
        "deadline": make_deadline(60),
    }
    monkeypatch.setattr(wait, "_monotonic", lambda: 12.5)
    read_snapshot = MagicMock(return_value=result)
    monkeypatch.setattr(wait.client, "read_lode_snapshot", read_snapshot)
    monkeypatch.setattr(
        wait.client,
        "read_archived_lodes",
        MagicMock(side_effect=AssertionError("wait must not scan archived lodes")),
    )
    wait._read_due_locals(state, Path("server.sock"), 0.0)

    read_snapshot.assert_called_once_with(
        Path("server.sock"),
        "abc123",
        timeout=2.0,
        deadline=state["deadline"],
    )
    observation = state["observations"].popleft()
    assert observation["kind"] == expected_kind
    assert observation["payload"] == expected_payload
    assert observation["detail"] == expected_detail


@pytest.mark.parametrize(
    "archived",
    [None, "false", 0, 1],
    ids=["missing", "string", "zero", "one"],
)
def test_validate_snapshot_requires_boolean_archived(archived):
    raw = snapshot()
    if archived is None:
        raw.pop("archived")
    else:
        raw["archived"] = archived

    assert wait.validate_snapshot(raw, "abc123") is None


@pytest.mark.parametrize("archived", [None, "false"], ids=["missing", "wrong-type"])
def test_initial_snapshot_with_invalid_archived_is_unavailable(archived, capsys):
    raw = snapshot()
    if archived is None:
        raw.pop("archived")
    else:
        raw["archived"] = archived

    record = wait._resolve_target(
        Path("server.sock"),
        "abc123",
        resolver=lambda socket_path, lid, **_kwargs: {
            "outcome": "found",
            "lode": raw,
            "host": "local",
            "canonical_id": "abc123",
            "probes": [configured_probe()],
            "exit_code": 0,
        },
        deadline=make_deadline(60),
        child_control=remote.make_child_registry(),
    )

    assert record["preset_outcome"] == "status_unavailable"
    assert record["preset_code"] == 4
    assert capsys.readouterr().out == ""


def test_ambiguous_local_observation_never_reaches_snapshot_validation(monkeypatch):
    record = wait._new_record("abc123", snapshot(), "local", 0.0, probes=[configured_probe()])
    state = {
        "condition": threading.Condition(),
        "record": record,
        "resolved": False,
        "observations": deque(
            [
                {
                    "id": "abc123",
                    "kind": "ambiguous",
                    "payload": None,
                    "detail": "local status ambiguous: abc123, abc999",
                    "failure_key": "ambiguous:abc123, abc999",
                    "observed_ts": 1.0,
                }
            ]
        ),
        "poll_s": 30.0,
    }
    monkeypatch.setattr(
        wait,
        "validate_snapshot",
        MagicMock(side_effect=AssertionError("ambiguous IDs are diagnostics, not a snapshot")),
    )

    assert wait._drain_observations(state) == []
    assert record["consecutive_failures"] == 1
    assert record["not_found_count"] == 0


@pytest.mark.parametrize(
    "state",
    ["new", "running", "ready", "teardown", "paused", "gated", "stuck", "error", "legacy"],
)
def test_archived_non_shipped_state_is_terminal_before_underlying_state(state):
    archived = snapshot(state=state, active=True, archived=True, status=f"preserved {state}")

    assert wait.classify(archived) == ("archived", 1, "archived_before_shipping")


@pytest.mark.parametrize("state", ["error", "gated", "stuck"])
def test_archived_shipped_state_is_success_before_underlying_state(state):
    archived = snapshot(stage="shipped", state=state, active=True, archived=True)

    assert wait.classify(archived) == ("shipped", 0, None)


def test_archived_before_shipping_renders_preserved_diagnostics(monkeypatch, capsys):
    archived = snapshot(
        state="legacy",
        status="Preserved legacy diagnostic",
        active=True,
        archived=True,
    )

    rc, _, _ = run_local_wait(monkeypatch, archived)

    assert rc == 1
    assert capsys.readouterr().out.splitlines() == [
        "✗ abc123 archived before shipping: Preserved legacy diagnostic",
        (
            "  stage=mill state=legacy active=True status=Preserved legacy diagnostic "
            "route=local observed_age_s=0.000"
        ),
        "  why: The lode was archived before it reached shipping. [archived_before_shipping]",
        "  server: test-host (route: local)",
        "  status: Preserved legacy diagnostic (stage=mill state=legacy active=True archived=True)",
        "  pane: <unavailable>",
        "  worktree: <unavailable> (basis=unavailable exists=<unavailable>)",
        "  recovery: Observed outcome archived for 'abc123'. Hopper did not proceed. "
        "Recover with: hop lode unarchive abc123.",
    ]


def test_archived_before_shipping_json_has_fixed_reason(monkeypatch, capsys):
    archived = snapshot(state="error", status="Preserved error", archived=True)

    rc, _, _ = run_local_wait(monkeypatch, archived, json_output=True)

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "archived"
    assert payload["reason_code"] == "archived_before_shipping"
    assert payload["archived"] is True
    assert payload["state"] == "error"
    assert payload["status"] == "Preserved error"


def test_initial_local_unavailable_can_resolve_remotely(monkeypatch):
    remote_snapshot = snapshot(host="fedora.local")

    record = wait._resolve_target(
        Path("server.sock"),
        "abc123",
        resolver=lambda socket_path, lid, **_kwargs: {
            "outcome": "found",
            "lode": remote_snapshot,
            "host": "fedora.local",
            "canonical_id": "abc123",
            "probes": [configured_probe("fedora.local")],
            "exit_code": 0,
        },
        deadline=make_deadline(60),
        child_control=remote.make_child_registry(),
    )

    assert record["remote"] is True
    assert record["route"] == "fedora.local"


def test_initial_local_unavailable_surfaces_original_error(monkeypatch, capsys):
    error = "Lode status unavailable for 'abc123': server not running"

    record = wait._resolve_target(
        Path("server.sock"),
        "abc123",
        resolver=lambda socket_path, lid, **_kwargs: {
            "outcome": "unavailable",
            "error": error,
            "probes": [configured_probe(outcome="unavailable")],
            "exit_code": 2,
        },
        deadline=make_deadline(60),
        child_control=remote.make_child_registry(),
    )

    assert record["preset_outcome"] == "status_unavailable"
    assert record["preset_code"] == 4
    assert capsys.readouterr().out == ""


def test_initial_unavailable_has_wait_only_status_outcome(monkeypatch, capsys):
    record = wait._resolution_failure_record(
        "abc123",
        {"outcome": "unavailable", "probes": [configured_probe(outcome="unavailable")]},
        0.0,
    )
    monkeypatch.setattr(
        wait,
        "_resolve_target",
        lambda *args, **kwargs: record,
    )

    rc, _, _ = run_local_wait(monkeypatch, snapshot())

    captured = capsys.readouterr()
    assert rc == 4
    assert captured.err == "hop wait: abc123 status_unavailable — exited 4\n"
    assert "abc123 status_unavailable" in captured.out


def test_initial_unavailable_json_keeps_stdout_clean(capsys):
    error = "Lode status unavailable for 'abc123'."

    record = wait._resolve_target(
        Path("server.sock"),
        "abc123",
        resolver=lambda socket_path, lid, **_kwargs: {
            "outcome": "unavailable",
            "error": error,
            "probes": [configured_probe(outcome="unavailable")],
            "exit_code": 2,
        },
        deadline=make_deadline(60),
        child_control=remote.make_child_registry(),
    )

    captured = capsys.readouterr()
    assert record["preset_outcome"] == "status_unavailable"
    assert record["preset_code"] == 4
    assert captured.out == ""
    assert captured.err == ""


def test_local_event_is_only_a_reconciliation_hint(monkeypatch, capsys):
    initial = snapshot()
    authoritative = [
        ("found", snapshot(status="Still working")),
        ("found", snapshot(stage="shipped", status="Done", title="Real result")),
    ]

    def on_start(callback, on_connect):
        callback(
            {
                "type": "lode_archived",
                "lode": snapshot(stage="shipped", status="Fabricated"),
            }
        )

    rc, _, connection = run_local_wait(
        monkeypatch,
        initial,
        authoritative,
        on_start=on_start,
    )

    assert rc == 0
    assert connection.stopped
    out = capsys.readouterr().out
    assert out.splitlines().count("✓ abc123 shipped") == 1
    assert "Fabricated" not in out


def test_reconnect_requests_immediate_authoritative_read(monkeypatch, capsys):
    initial = snapshot()
    observations = [
        ("found", snapshot(status="Before disconnect")),
        ("found", snapshot(stage="shipped", status="Recovered ship")),
    ]

    def wait_action(clock, timeout, connection):
        assert timeout > 20
        clock.now += 1
        connection.on_connect()

    rc, clock, _ = run_local_wait(
        monkeypatch,
        initial,
        observations,
        on_start=lambda callback, on_connect: on_connect(),
        wait_action=wait_action,
    )

    assert rc == 0
    assert clock.now == 1
    out = capsys.readouterr().out
    assert "Before disconnect" not in out
    assert out.count("Recovered ship") == 1


def test_later_inactive_snapshot_has_prescriptive_recovery(monkeypatch, capsys):
    inactive = snapshot(state="paused", status="Stopped", active=False)
    rc, _, _ = run_local_wait(monkeypatch, snapshot(), [("found", inactive)])

    assert rc == 1
    out = capsys.readouterr().out
    assert "state=paused active=False status=Stopped" in out
    assert "Hopper did not proceed" in out
    assert "Recover with: hop lode resume abc123" in out


def test_initial_ready_inactive_handoff_waits_for_shipped(monkeypatch, capsys):
    handoff = snapshot(stage="ship", state="ready", status="Refine complete", active=False)
    shipped = snapshot(stage="shipped", state="ready", status="Ship complete", active=False)

    rc, _, _ = run_local_wait(monkeypatch, handoff, [("found", shipped)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "not active" not in out
    assert "✓ abc123 shipped" in out


def test_inactive_teardown_waits_without_reporting_failure():
    assert (
        wait.classify(
            snapshot(
                state="teardown", status="Teardown: waiting for Linux containment", active=False
            )
        )
        is None
    )


def test_shipped_teardown_waits_until_the_pending_action_clears():
    assert wait.classify(snapshot(stage="shipped", state="teardown", active=False)) is None


def test_later_ready_inactive_handoff_waits_for_shipped(monkeypatch, capsys):
    handoff = snapshot(stage="ship", state="ready", status="Refine complete", active=False)
    shipped = snapshot(stage="shipped", state="ready", status="Ship complete", active=False)

    rc, _, _ = run_local_wait(monkeypatch, snapshot(), [("found", handoff), ("found", shipped)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "not active" not in out
    assert "✓ abc123 shipped" in out


def test_two_consecutive_not_found_win_observer_boundary(monkeypatch, capsys):
    initial = snapshot(host="fedora.local")
    rc, clock = run_remote_wait(
        monkeypatch,
        initial,
        [(None, "absent"), (None, "absent")],
        observer_timeout_s=30,
        json_output=True,
        publish=False,
    )

    assert rc == 1
    assert clock.now == 30
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "not_found"
    assert payload["route"] == "fedora.local"


def test_not_found_human_output_includes_inspection_guidance(monkeypatch, capsys):
    initial = snapshot(host="fedora.local")
    rc, _ = run_remote_wait(
        monkeypatch,
        initial,
        [(None, "absent"), (None, "absent")],
        publish=False,
    )

    assert rc == 1
    out = capsys.readouterr().out
    assert "route=fedora.local" in out
    assert "Recover with: hop -H fedora.local lode status abc123" in out


def test_not_found_streak_resets_on_observer_failure(monkeypatch, capsys):
    initial = snapshot(host="fedora.local")
    rc, _ = run_remote_wait(
        monkeypatch,
        initial,
        [
            (None, "absent"),
            (None, "unreadable"),
            (None, "absent"),
            (None, "absent"),
        ],
        observer_timeout_s=200,
        json_output=True,
        publish=False,
    )

    assert rc == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "not_found"


@pytest.mark.parametrize(
    "failure",
    [
        (None, "unreadable"),
        (None, "unreadable"),
        ("not an object", "found"),
        ({"id": "abc123"}, "found"),
        (snapshot(stage=1), "found"),
        (snapshot(active="yes"), "found"),
        ({key: value for key, value in snapshot().items() if key != "archived"}, "found"),
        (snapshot(archived="false"), "found"),
        (snapshot(lid="wrong"), "found"),
        RuntimeError("injected observer failure"),
    ],
    ids=[
        "ssh-or-server-failure",
        "malformed-json",
        "non-object-json",
        "missing-fields",
        "wrong-string-type",
        "wrong-bool-type",
        "missing-archived",
        "wrong-archived-type",
        "wrong-id",
        "unexpected-exception",
    ],
)
def test_remote_failures_follow_observer_health_policy(monkeypatch, capsys, failure):
    initial = snapshot(host="fedora.local")
    rc, _ = run_remote_wait(
        monkeypatch,
        initial,
        [failure, failure],
        observer_timeout_s=45,
        json_output=True,
        publish=False,
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err.count("warning: status observer") == 1
    payload = json.loads(captured.out)
    assert payload["outcome"] == "observer_unavailable"
    assert payload["stage"] == "mill"
    assert payload["state"] == "running"


def test_temporary_unreadability_recovers_without_losing_lode(monkeypatch, capsys):
    initial = snapshot(host="fedora.local")
    shipped = snapshot(host="fedora.local", stage="shipped", status="Done")
    rc, _ = run_remote_wait(
        monkeypatch,
        initial,
        [
            (None, "unreadable"),
            (None, "unreadable"),
            (snapshot(host="fedora.local", status="Recovered"), "found"),
            (shipped, "found"),
        ],
        observer_timeout_s=100,
        publish=False,
    )

    assert rc == 0
    assert "abc123 shipped" in capsys.readouterr().out


def test_healthy_snapshots_outlive_observer_timeout(monkeypatch, capsys):
    initial = snapshot()
    running = ("found", snapshot(state="design", status="Still healthy"))
    rc, clock, _ = run_local_wait(
        monkeypatch,
        initial,
        [running, running, running],
        timeout_s=70,
        observer_timeout_s=35,
    )

    assert rc == 4
    assert clock.now == 70
    assert "Timed out waiting" in capsys.readouterr().out


def test_observer_timeout_precedes_unlimited_overall_timeout(monkeypatch, capsys):
    failure = ("unavailable", "server timed out")
    rc, clock, _ = run_local_wait(
        monkeypatch,
        snapshot(),
        [failure, failure],
        observer_timeout_s=45,
        json_output=True,
    )

    assert rc == 4
    assert clock.now == 45
    assert json.loads(capsys.readouterr().out)["outcome"] == "observer_unavailable"


def test_shorter_overall_timeout_wins_observer_timeout(monkeypatch, capsys):
    rc, clock, _ = run_local_wait(
        monkeypatch,
        snapshot(),
        [("unavailable", "server timed out")],
        timeout_s=5,
        observer_timeout_s=45,
        json_output=True,
    )

    assert rc == 4
    assert clock.now == 5
    assert json.loads(capsys.readouterr().out)["outcome"] == "timeout"


def test_disabled_observer_timeout_retries_until_overall_timeout(monkeypatch, capsys):
    failure = ("unavailable", "server timed out")
    rc, clock, _ = run_local_wait(
        monkeypatch,
        snapshot(),
        [failure, failure, failure],
        timeout_s=95,
        observer_timeout_s=0,
        json_output=True,
    )

    assert rc == 4
    assert clock.now == 95
    assert json.loads(capsys.readouterr().out)["outcome"] == "timeout"


def test_initial_stuck_uses_grace_and_authoritative_confirmation(monkeypatch, capsys):
    stuck = snapshot(state="stuck", status="No output", tmux_pane=None)
    rc, clock, _ = run_local_wait(monkeypatch, stuck, [("found", stuck), ("found", stuck)])

    assert wait.STUCK_GRACE_MS == 120_000
    assert rc == 3
    assert clock.now == 120
    assert "abc123 stuck: No output" in capsys.readouterr().out


def test_initial_new_uses_shared_grace_and_authoritative_confirmation(monkeypatch, capsys):
    starting = snapshot(state="new", active=False, status="Waiting for registration")
    rc, clock, _ = run_local_wait(
        monkeypatch,
        starting,
        [("found", starting), ("found", starting)],
    )

    assert rc == 1
    assert clock.now == 120
    captured = capsys.readouterr()
    assert "startup registration stalled" in captured.out
    assert "hop wait: abc123 startup_stalled — exited 1" in captured.err


def test_reconnecting_uses_shared_grace_and_authoritative_confirmation(monkeypatch, capsys):
    reconnecting = snapshot(
        state="reconnecting",
        active=False,
        status="Waiting for runner registration",
    )
    rc, clock, _ = run_local_wait(
        monkeypatch,
        reconnecting,
        [("found", reconnecting), ("found", reconnecting)],
        json_output=True,
    )

    assert rc == 1
    assert clock.now == 120
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "reconnect_stalled"
    assert payload["reason_code"] == "runner_reregistration_stalled"


def test_reconnect_stalled_human_output_has_prescriptive_recovery(monkeypatch, capsys):
    reconnecting = snapshot(
        state="reconnecting",
        active=False,
        status="Runner pane %8 survived server replacement; waiting for registration",
        tmux_pane="%8",
    )

    rc, _, _ = run_local_wait(
        monkeypatch,
        reconnecting,
        [("found", reconnecting), ("found", reconnecting)],
    )

    output = capsys.readouterr().out
    assert rc == 1
    assert "runner reregistration stalled" in output
    assert "Hopper did not proceed" in output
    assert "Recover with: hop lode restart abc123" in output


def test_shared_grace_away_and_back_gets_fresh_origin(monkeypatch, capsys):
    starting = snapshot(state="new", active=False, status="Waiting for registration")
    running = snapshot(state="running", active=True, status="Connected")
    rc, clock, _ = run_local_wait(
        monkeypatch,
        starting,
        [
            ("found", running),
            ("found", starting),
            ("found", starting),
            ("found", starting),
            ("found", starting),
            ("found", starting),
        ],
    )

    assert rc == 1
    assert clock.now == 150
    assert "startup registration stalled" in capsys.readouterr().out


def test_confirmed_stuck_still_beats_expired_observer_deadline():
    record = wait._new_record(
        "abc123", snapshot(state="stuck"), "local", 0, probes=[configured_probe()]
    )
    record["grace"].update(recheck_pending=True, confirmed=True)
    state = {
        "record": record,
        "resolved": False,
        "observer_timeout_s": 45,
        "overall_deadline": float("inf"),
    }

    outcomes = wait._collect_boundary_outcomes(state, 120)

    assert [(item["outcome"], item["code"]) for item in outcomes] == [("stuck", 3)]


def test_inactive_ready_uses_handoff_grace(monkeypatch, capsys):
    ready = snapshot(state="ready", active=False, status="Waiting for successor")
    rc, clock, _ = run_local_wait(monkeypatch, ready, [("found", ready), ("found", ready)])

    assert rc == 1
    assert clock.now == 120
    captured = capsys.readouterr()
    assert "ready handoff stalled" in captured.out
    assert "hop wait: abc123 handoff_stalled — exited 1" in captured.err


@pytest.mark.parametrize(
    ("phase", "category"),
    [
        (
            phase,
            "blocked"
            if phase.endswith("_blocked")
            else "spawning"
            if phase == "spawning"
            else "pending",
        )
        for phase in sorted(actions.PHASES)
    ],
)
def test_every_valid_action_phase_has_one_wait_boundary(phase, category):
    pending_action = pending_action_projection(
        phase,
        recovery_command="hop lode restart abcd2345" if category == "blocked" else None,
    )
    current = snapshot(
        lid="abcd2345",
        state="teardown",
        active=False,
        pending_action=pending_action,
    )
    record = wait._new_record("abcd2345", current, "local", 0, probes=[configured_probe()])
    state = {
        "record": record,
        "resolved": False,
        "observer_timeout_s": 300,
        "overall_deadline": float("inf"),
    }

    outcomes = wait._collect_boundary_outcomes(state, 0)

    assert wait.classify(current) is None
    if category == "blocked":
        assert [(item["outcome"], item["code"]) for item in outcomes] == [("action_blocked", 2)]
        assert record["grace"] is None
    elif category == "spawning":
        assert outcomes == []
        assert record["grace"]["kind"] == "action_spawn"
    else:
        assert outcomes == []
        assert record["grace"] is None


@pytest.mark.parametrize("action_type", ["invalid", "legacy-v1"])
def test_structured_startup_action_projection_is_blocked(action_type):
    current = snapshot(
        state="teardown",
        active=False,
        pending_action={"phase": "blocked", "action_type": action_type, "status": "Blocked"},
    )
    record = wait._new_record("abc123", current, "local", 0, probes=[configured_probe()])
    state = {
        "record": record,
        "resolved": False,
        "observer_timeout_s": 300,
        "overall_deadline": float("inf"),
    }

    outcomes = wait._collect_boundary_outcomes(state, 0)

    assert [(item["outcome"], item["code"], item["reason"]) for item in outcomes] == [
        ("action_blocked", 2, "durable_action_blocked")
    ]


def test_any_pending_action_excludes_handoff_and_generic_inactive():
    current = snapshot(
        state="paused",
        active=False,
        pending_action=pending_action_projection("accepted"),
    )

    assert wait.classify(current) is None
    assert wait._snapshot_grace_kind(current) is None


def test_action_spawn_grace_origin_ignores_projection_churn(monkeypatch, capsys):
    first = pending_action_projection("spawning")
    second = copy.deepcopy(first)
    second.update(status="pane reported", containment={"state": "proven"})
    third = copy.deepcopy(second)
    third.update(
        status="worker registration observed",
        recovery={"kind": "spawn", "message": "still adopting", "command": "inspect"},
    )
    initial = snapshot(lid="abcd2345", state="teardown", active=False, pending_action=first)
    observed = snapshot(lid="abcd2345", state="teardown", active=False, pending_action=second)
    confirmed = snapshot(lid="abcd2345", state="teardown", active=False, pending_action=third)

    rc, clock, _ = run_local_wait(
        monkeypatch,
        initial,
        [("found", observed), ("found", confirmed)],
        json_output=True,
    )

    assert rc == 2
    assert clock.now == 120
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "action_stalled"
    assert payload["reason_code"] == "successor_adoption_stalled"


def test_action_spawn_grace_leaving_and_reentering_gets_fresh_origin(monkeypatch, capsys):
    spawning = snapshot(
        lid="abcd2345",
        state="teardown",
        active=False,
        pending_action=pending_action_projection("spawning"),
    )
    publishing = snapshot(
        lid="abcd2345",
        state="teardown",
        active=False,
        pending_action=pending_action_projection("publishing_terminal"),
    )

    rc, clock, _ = run_local_wait(
        monkeypatch,
        spawning,
        [
            ("found", publishing),
            ("found", spawning),
            ("found", spawning),
            ("found", spawning),
            ("found", spawning),
            ("found", spawning),
        ],
        json_output=True,
    )

    assert rc == 2
    assert clock.now == 150
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "action_stalled"
    assert payload["reason_code"] == "successor_adoption_stalled"


def test_action_blocked_uses_structured_recovery_command(monkeypatch, capsys):
    command = "hop lode restart abcd2345 --force"
    blocked = snapshot(
        lid="abcd2345",
        state="teardown",
        active=False,
        pending_action=pending_action_projection("cleanup_blocked", recovery_command=command),
    )

    rc, _, _ = run_local_wait(monkeypatch, blocked)

    assert rc == 2
    captured = capsys.readouterr()
    assert "action blocked" in captured.out
    assert f"Recover with: {command}" in captured.out
    assert "hop wait: abcd2345 action_blocked — exited 2" in captured.err


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"archived": True}, ("archived", 1)),
        ({"state": "error"}, ("error", 1)),
        ({"state": "gated"}, ("gated", 2)),
    ],
)
def test_storage_error_and_gate_precede_action_attention(changes, expected):
    current = snapshot(
        **{
            "state": "teardown",
            "active": False,
            "pending_action": pending_action_projection(
                "cleanup_blocked", recovery_command="hop lode restart abcd2345"
            ),
            **changes,
        }
    )
    record = wait._new_record("abc123", current, "local", 0, probes=[configured_probe()])
    state = {
        "record": record,
        "resolved": False,
        "observer_timeout_s": 300,
        "overall_deadline": float("inf"),
    }

    outcomes = wait._collect_boundary_outcomes(state, 0)

    assert [(item["outcome"], item["code"]) for item in outcomes] == [expected]


@pytest.mark.parametrize(
    ("observer_timeout_s", "overall_deadline", "expected"),
    [
        (45, float("inf"), "observer_unavailable"),
        (0, 30, "timeout"),
    ],
)
def test_shorter_deadline_precedes_confirmed_action_spawn_grace(
    observer_timeout_s, overall_deadline, expected
):
    current = snapshot(
        state="teardown",
        active=False,
        pending_action=pending_action_projection("spawning"),
    )
    record = wait._new_record("abc123", current, "local", 0, probes=[configured_probe()])
    record["grace"].update(recheck_pending=True, confirmed=True)
    state = {
        "record": record,
        "resolved": False,
        "observer_timeout_s": observer_timeout_s,
        "overall_deadline": overall_deadline,
    }

    outcomes = wait._collect_boundary_outcomes(state, 120)

    assert [item["outcome"] for item in outcomes] == [expected]


@pytest.mark.parametrize(
    ("action_type", "guidance"),
    [
        ("invalid", "Repair or drain the malformed pending action"),
        ("legacy-v1", "Drain the legacy pending action"),
    ],
)
def test_startup_action_blocked_uses_structured_projection_guidance(
    monkeypatch, capsys, action_type, guidance
):
    blocked = snapshot(
        state="teardown",
        active=False,
        status="untrusted status text",
        pending_action={"phase": "blocked", "action_type": action_type, "status": "Blocked"},
    )

    rc, _, _ = run_local_wait(monkeypatch, blocked)

    assert rc == 2
    assert guidance in capsys.readouterr().out


def test_json_stuck_record_keeps_diagnostic_on_stderr(monkeypatch, capsys):
    stuck = snapshot(state="stuck", status="No output", tmux_pane=None)
    rc, _, _ = run_local_wait(
        monkeypatch,
        stuck,
        [("found", stuck), ("found", stuck)],
        json_output=True,
    )

    assert rc == 3
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1
    payload = json.loads(captured.out)
    assert payload["outcome"] == "stuck"
    assert "abc123 stuck: No output" in captured.err
    assert "pane: <unknown>" in captured.err


def test_stuck_recovery_clears_grace(monkeypatch, capsys):
    stuck = snapshot(state="stuck", status="No output")
    running = snapshot(state="design", status="Recovered")
    shipped = snapshot(stage="shipped", status="Done")
    rc, _, _ = run_local_wait(
        monkeypatch,
        snapshot(),
        [("found", stuck), ("found", running), ("found", shipped)],
    )

    assert rc == 0
    assert "stuck" not in capsys.readouterr().out


def test_remote_stuck_confirms_on_poll_after_grace(monkeypatch, capsys):
    stuck = snapshot(host="fedora.local", state="stuck", status="No output", tmux_pane=None)
    rc, clock = run_remote_wait(
        monkeypatch,
        stuck,
        [(stuck, "found")],
        publish=False,
    )

    assert rc == 3
    assert clock.now == 150
    assert "abc123 stuck: No output" in capsys.readouterr().out


def test_remote_stuck_human_uses_remote_peek_without_local_capture(monkeypatch, capsys):
    initial = snapshot(
        host="fedora.local",
        state="stuck",
        status="Initial wedge",
        tmux_pane="%83",
    )
    confirmed = snapshot(
        host="fedora.local",
        stage="refine",
        state="stuck",
        status="Confirmed wedge",
        tmux_pane="%83",
    )

    def fail_capture(target):
        raise AssertionError(f"unexpected local pane capture: {target}")

    monkeypatch.setattr(wait, "capture_pane", fail_capture)
    rc, _ = run_remote_wait(
        monkeypatch,
        initial,
        [(confirmed, "found")],
        publish=False,
    )

    assert rc == 3
    out = capsys.readouterr().out
    assert "fedora.local" in out
    assert "%83" in out
    assert "hop -H fedora.local lode peek abc123" in out
    assert "stage=refine" in out
    assert "state=stuck" in out
    assert "active=True" in out
    assert "status=Confirmed wedge" in out
    assert "route=fedora.local" in out
    assert "observed_age_s=" in out
    assert "Initial wedge" not in out


def test_remote_stuck_human_without_pane_uses_remote_peek(monkeypatch, capsys):
    stuck = snapshot(host="fedora.local", state="stuck", status="Confirmed wedge")

    def fail_capture(target):
        raise AssertionError(f"unexpected local pane capture: {target}")

    monkeypatch.setattr(wait, "capture_pane", fail_capture)
    rc, _ = run_remote_wait(
        monkeypatch,
        stuck,
        [(stuck, "found")],
        publish=False,
    )

    assert rc == 3
    out = capsys.readouterr().out
    assert "route=fedora.local" in out
    assert "pane: <unknown>" in out
    assert "hop -H fedora.local lode peek abc123" in out


def test_remote_stuck_json_keeps_guidance_on_stderr_without_capture(monkeypatch, capsys):
    initial = snapshot(
        host="fedora.local",
        state="stuck",
        status="Initial wedge",
        tmux_pane="%83",
    )
    confirmed = snapshot(
        host="fedora.local",
        stage="refine",
        state="stuck",
        status="Confirmed wedge",
        tmux_pane="%83",
    )

    def fail_capture(target):
        raise AssertionError(f"unexpected local pane capture: {target}")

    monkeypatch.setattr(wait, "capture_pane", fail_capture)
    rc, _ = run_remote_wait(
        monkeypatch,
        initial,
        [(confirmed, "found")],
        json_output=True,
        publish=False,
    )

    assert rc == 3
    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.splitlines()]
    assert len(payloads) == 1
    payload = payloads[0]
    assert set(payload) == wait.FINAL_RECORD_KEYS
    assert {"host", "source"}.isdisjoint(payload)
    assert payload["reason"] == wait.REASON_TEXT[payload["reason_code"]]
    assert payload["outcome"] == "stuck"
    assert payload["server"] == "fedora.local"
    assert payload["route"] == "fedora.local"
    assert payload["stage"] == "refine"
    assert payload["state"] == "stuck"
    assert payload["status"] == "Confirmed wedge"
    assert payload["status_display"] == "Confirmed wedge"
    assert payload["pane_liveness"] == "not_probed"
    assert "fedora.local" in captured.err
    assert "%83" in captured.err
    assert "hop -H fedora.local lode peek abc123" in payload["recovery"]
    assert "--- last 50 lines" not in captured.err
    assert "Inspect with" not in captured.out


def test_local_stuck_human_captures_only_last_50_pane_lines(monkeypatch, capsys):
    stuck = snapshot(state="stuck", status="Local wedge", tmux_pane="%7")
    pane_capture = "\n".join(f"line {number}" for number in range(1, 61))
    captured_targets = []

    def capture(target, **_kwargs):
        captured_targets.append(target)
        return pane_capture

    monkeypatch.setattr(wait, "capture_pane", capture)
    rc, _, _ = run_local_wait(
        monkeypatch,
        stuck,
        [("found", stuck), ("found", stuck)],
    )

    assert rc == 3
    assert captured_targets == ["%7"]
    out_lines = capsys.readouterr().out.splitlines()
    start = out_lines.index("  --- last 50 lines of pane ---") + 1
    end = out_lines.index("  --- end pane ---")
    assert out_lines[start:end] == [f"  line {number}" for number in range(11, 61)]
    assert "  line 10" not in out_lines[start:end]
    assert "  line 11" in out_lines[start:end]
    assert "  line 60" in out_lines[start:end]
    assert any(
        "stage=mill state=stuck active=True status=Local wedge route=local observed_age_s=" in line
        for line in out_lines
    )


def test_gated_human_output_includes_latest_snapshot_summary(monkeypatch, capsys):
    gated = snapshot(state="gated", status="Review required")
    rc, _, _ = run_local_wait(monkeypatch, gated)

    assert rc == 2
    out = capsys.readouterr().out
    assert "hop gate show abc123" in out
    assert "stage=mill" in out
    assert "state=gated" in out
    assert "active=True" in out
    assert "status=Review required" in out
    assert "route=local" in out
    assert "observed_age_s=" in out


def test_gated_parked_gone_human_output_reports_restart_and_exits_two(monkeypatch, capsys):
    reason = "no pane output"
    branch = "hopper-abc123"
    gated = snapshot(
        state="gated",
        status=format_park_status(reason, "abc123"),
        tmux_pane="%21",
        branch=branch,
    )
    expected = PARK_PANE_GONE_STATUS.format(
        reason=reason,
        lode_id="abc123",
        branch=branch,
    )

    with patch("hopper.lodes.pane_liveness", return_value=Liveness.GONE):
        rc, _, _ = run_local_wait(monkeypatch, gated)

    assert rc == 2
    assert capsys.readouterr().out.splitlines() == [
        "Lode abc123 is gated.",
        f"  stage=mill state=gated active=True status={expected} route=local observed_age_s=0.000",
        "  why: The lode is gated for operator review. [gate_requires_review]",
        "  server: test-host (route: local)",
        f"  status: {format_park_status(reason, 'abc123')} "
        "(stage=mill state=gated active=True archived=False)",
        "  pane: %21",
        "  worktree: <unavailable> (basis=unavailable exists=<unavailable>)",
        "  recovery: Observed outcome gated for 'abc123'. Hopper did not proceed. "
        "Recover with: hop gate show abc123.",
    ]


def test_gated_display_copy_does_not_mutate_wait_record(capsys):
    reason = "no pane output"
    branch = "hopper-abc123"
    snapshot_data = snapshot(
        state="gated",
        status=format_park_status(reason, "abc123"),
        tmux_pane="%22",
        branch=branch,
    )
    record = wait._new_record("abc123", snapshot_data, "local", 0.0, probes=[configured_probe()])
    before = copy.deepcopy(record)
    expected = PARK_PANE_GONE_STATUS.format(
        reason=reason,
        lode_id="abc123",
        branch=branch,
    )

    with patch("hopper.lodes.pane_liveness", return_value=Liveness.GONE):
        deadline = make_deadline(60)
        final_record = wait._final_record(
            record,
            "gated",
            2,
            0.0,
            None,
            deadline=deadline,
            child_control=remote.make_child_registry(),
        )
        wait._emit_outcome(
            final_record,
            False,
            deadline=deadline,
        )

    assert f"status={expected}" in capsys.readouterr().out
    assert record == before


def test_gated_parked_json_reports_gone_status(monkeypatch, capsys):
    reason = "no pane output"
    branch = "hopper-abc123"
    stored = format_park_status(reason, "abc123")
    gated = snapshot(
        state="gated",
        status=stored,
        tmux_pane="%23",
        branch=branch,
    )
    expected = PARK_PANE_GONE_STATUS.format(
        reason=reason,
        lode_id="abc123",
        branch=branch,
    )

    with patch("hopper.lodes.pane_liveness", return_value=Liveness.GONE) as mock_liveness:
        rc, _, _ = run_local_wait(monkeypatch, gated, json_output=True)

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == stored
    assert payload["status_display"] == expected
    assert payload["pane_liveness"] == "gone"
    mock_liveness.assert_called_once()
    assert mock_liveness.call_args.args == ("%23",)
    assert mock_liveness.call_args.kwargs["timeout"] > 0


def test_remote_gated_wait_json_is_not_probed(monkeypatch, capsys):
    stored = format_park_status("no pane output", "abc123")
    gated = snapshot(
        state="gated",
        status=stored,
        host="fedora.local",
        tmux_pane="%remote",
        branch="hopper-abc123",
        status_display="remote-computed correction",
        pane_liveness="gone",
    )

    with patch(
        "hopper.lodes.pane_liveness",
        side_effect=AssertionError("pane_liveness must not be called"),
    ):
        rc, _ = run_remote_wait(
            monkeypatch,
            gated,
            [],
            json_output=True,
            publish=False,
        )

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["server"] == "fedora.local"
    assert payload["route"] == "fedora.local"
    assert payload["status"] == stored
    assert payload["status_display"] == stored
    assert payload["pane_liveness"] == "not_probed"


@pytest.mark.parametrize("json_output", [False, True], ids=["human", "jsonl"])
def test_observer_failure_reports_latest_valid_snapshot(monkeypatch, capsys, json_output):
    initial = snapshot(host="fedora.local", status="Initial")
    later = snapshot(
        host="fedora.local",
        stage="refine",
        state="design",
        active=True,
        status="Later durable status",
    )
    failure = (None, "unreadable")
    rc, clock = run_remote_wait(
        monkeypatch,
        initial,
        [(later, "found"), failure, failure],
        observer_timeout_s=75,
        json_output=json_output,
        publish=False,
    )

    assert rc == 4
    assert clock.now == 75
    captured = capsys.readouterr()
    if json_output:
        payload = json.loads(captured.out)
        assert set(payload) == wait.FINAL_RECORD_KEYS
        assert payload["id"] == "abc123"
        assert payload["outcome"] == "observer_unavailable"
        assert payload["reason_code"] == "observer_freshness_expired"
        assert payload["server"] == "fedora.local"
        assert payload["route"] == "fedora.local"
        assert payload["stage"] == "refine"
        assert payload["state"] == "design"
        assert payload["status"] == "Later durable status"
        assert payload["observed_age_s"] == 75.0
        assert payload["tmux_pane"] is None
    else:
        assert "stage=refine state=design active=True" in captured.out
        assert "status=Later durable status route=fedora.local" in captured.out
        assert "observed_age_s=75.000" in captured.out
        assert "Hopper did not proceed" in captured.out
        assert "Recover with: hop -H fedora.local wait abc123" in captured.out


@pytest.mark.parametrize("initial_state", ["running", "shipped"])
def test_cache_failure_warns_once_and_does_not_override_snapshot(
    monkeypatch, capsys, initial_state
):
    initial = snapshot(
        host="fedora.local",
        stage="shipped" if initial_state == "shipped" else "mill",
        active=initial_state != "shipped",
    )
    shipped = snapshot(host="fedora.local", stage="shipped", active=False, status="Done")
    monkeypatch.setattr(wait.remote, "load_lode_cache", lambda **_kwargs: {})
    monkeypatch.setattr(
        wait.remote,
        "remember_lode",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read-only cache")),
    )
    probes = [(shipped, "found")]

    rc, _ = run_remote_wait(
        monkeypatch,
        initial,
        probes,
        publish=True,
    )

    assert rc == 0
    captured = capsys.readouterr()
    assert captured.err.count("warning: could not update remote lode cache") == 1
    assert "abc123 shipped" in captured.out


def test_cache_read_failure_warns_once_and_shipped_still_succeeds(monkeypatch, capsys):
    initial = snapshot(host="fedora.local", stage="shipped", active=False)
    monkeypatch.setattr(
        wait.remote,
        "load_lode_cache",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("unreadable cache")),
    )

    rc, _ = run_remote_wait(
        monkeypatch,
        initial,
        [(initial, "found")],
    )

    assert rc == 0
    captured = capsys.readouterr()
    assert captured.err.count("warning: could not read remote lode cache") == 1
    assert "abc123 shipped" in captured.out


def test_jsonl_cache_warning_stays_on_stderr(monkeypatch, capsys):
    initial = snapshot(host="fedora.local", stage="shipped", active=False)
    monkeypatch.setattr(wait.remote, "load_lode_cache", lambda **_kwargs: {})
    monkeypatch.setattr(
        wait.remote,
        "remember_lode",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read-only cache")),
    )

    rc, _ = run_remote_wait(
        monkeypatch,
        initial,
        [(initial, "found")],
        json_output=True,
    )

    assert rc == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert [json.loads(line)["outcome"] for line in lines] == ["shipped"]
    assert "warning:" not in captured.out
    assert captured.err.count("warning: could not update remote lode cache") == 1


def test_unchanged_remote_mapping_is_not_republished(monkeypatch):
    initial = snapshot(host="fedora.local", stage="shipped", active=False)
    monkeypatch.setattr(
        wait.remote,
        "load_lode_cache",
        lambda **_kwargs: {"abc123": {"host": "fedora.local"}},
    )
    calls = []
    monkeypatch.setattr(wait.remote, "remember_lode", lambda *args: calls.append(args))

    rc, _ = run_remote_wait(
        monkeypatch,
        initial,
        [(initial, "found")],
    )

    assert rc == 0
    assert calls == []


def test_remote_completed_stage_does_not_resolve_before_shipped(monkeypatch, capsys):
    initial = snapshot(host="fedora.local")
    completed = snapshot(host="fedora.local", stage="refine", state="completed")
    shipped = snapshot(host="fedora.local", stage="shipped", state="completed")
    rc, clock = run_remote_wait(
        monkeypatch,
        initial,
        [(completed, "found"), (shipped, "found")],
        publish=False,
    )

    assert rc == 0
    assert clock.now == 30
    assert capsys.readouterr().out.splitlines().count("✓ abc123 shipped") == 1


@pytest.mark.parametrize(
    ("probe_result", "expected_kind"),
    [
        ((snapshot(host="fedora.local"), "found"), "found"),
        ((None, "absent"), "absent"),
        ((None, "unreadable"), "unreadable"),
        (RuntimeError("boom"), "observer_error"),
    ],
)
def test_remote_worker_converts_every_outcome_to_observation(probe_result, expected_kind):
    condition = threading.Condition()
    stop_event = threading.Event()
    record = wait._new_record(
        "abc123",
        snapshot(host="fedora.local"),
        "fedora.local",
        0.0,
        probes=[configured_probe("fedora.local")],
    )
    state = {
        "condition": condition,
        "record": record,
        "resolved": False,
        "observations": deque(),
        "stop_event": stop_event,
        "shutdown": False,
        "deadline": make_deadline(60),
        "probe_timeout_s": 5,
        "child_control": wait.remote.make_child_registry(),
    }

    def probe(host, lid, **_kwargs):
        stop_event.set()
        if isinstance(probe_result, Exception):
            raise probe_result
        return probe_result

    wait._remote_worker_group(state, 30, probe)

    assert len(state["observations"]) == 1
    assert state["observations"][0]["kind"] == expected_kind


def test_remote_worker_stop_only_sets_cancellation():
    state = {"stop_event": threading.Event()}

    wait._stop_remote_workers(state)

    assert state["stop_event"].is_set()


def test_keyboard_interrupt_returns_130_and_cancels_remote_workers(monkeypatch):
    initial = snapshot(host="fedora.local")
    stopped_states = []
    original_stop = wait._stop_remote_workers

    monkeypatch.setattr(wait.client, "get_lode", lambda *args, **kwargs: None)
    monkeypatch.setattr(wait, "_publish_resident_routes", lambda record, **_kwargs: None)
    monkeypatch.setattr(
        wait,
        "_condition_wait",
        lambda condition, deadline, wake_at: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    def stop_workers(state):
        original_stop(state)
        stopped_states.append(state)

    monkeypatch.setattr(wait, "_stop_remote_workers", stop_workers)

    rc = wait.wait_for_lode(
        Path("server.sock"),
        "abc123",
        deadline=make_deadline(60),
        resolver=lambda socket_path, lid, **_kwargs: {
            "outcome": "found",
            "lode": initial,
            "host": "fedora.local",
            "canonical_id": "abc123",
            "probes": [configured_probe("fedora.local")],
            "exit_code": 0,
        },
        probe_remote=lambda host, lid, **_kwargs: (initial, "found"),
    )

    assert rc == 130
    assert stopped_states
    assert stopped_states[-1]["stop_event"].is_set()


def test_interrupt_at_3599_cannot_extend_cleanup_past_original_3600_deadline():
    clock = FakeClock()
    clock.now = 3599.0
    original = {"expires_at": 3600.0, "clock": clock}

    cleanup = wait._interrupt_cleanup_deadline(original)

    assert cleanup["expires_at"] == 3600.0


def test_jsonl_stdout_contains_only_terminal_records(monkeypatch, capsys):
    initial = snapshot(status="Latest")
    rc, _, _ = run_local_wait(
        monkeypatch,
        initial,
        [("unavailable", "server timed out"), ("unavailable", "server timed out")],
        observer_timeout_s=45,
        json_output=True,
    )

    assert rc == 4
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert set(payload) == wait.FINAL_RECORD_KEYS
    assert {"host", "source"}.isdisjoint(payload)
    assert payload["reason"] == wait.REASON_TEXT[payload["reason_code"]]
    assert "warning:" not in captured.out
    assert "warning:" in captured.err


@pytest.mark.parametrize("failure_kind", ["oom", "runner_exit_unverified"])
def test_terminal_runner_failure_json_and_human_output(monkeypatch, capsys, failure_kind):
    status = format_terminal_failure_status(failure_kind, "abc123")
    initial = snapshot(
        state="error",
        active=False,
        status=status,
        failure_kind=failure_kind,
    )

    rc, _, _ = run_local_wait(monkeypatch, initial, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["failure_kind"] == failure_kind
    assert payload["status"] == status

    rc, _, _ = run_local_wait(monkeypatch, initial)
    output = capsys.readouterr().out
    assert rc == 1
    assert status in output
    assert "Restart with:" not in output


@pytest.mark.parametrize(
    ("changes", "expected_code", "expected_outcome"),
    [
        ({"stage": "shipped", "active": False}, 0, "shipped"),
        ({"state": "error", "status": "Failed"}, 1, "error"),
        ({"state": "gated", "status": "Review required"}, 2, "gated"),
        ({"state": "paused", "active": False}, 1, "inactive"),
    ],
)
def test_wait_summary_for_initial_terminal_outcome(
    monkeypatch,
    capsys,
    changes,
    expected_code,
    expected_outcome,
):
    rc, _, _ = run_local_wait(monkeypatch, snapshot(**changes))

    captured = capsys.readouterr()
    assert rc == expected_code
    assert captured.err == f"hop wait: abc123 {expected_outcome} — exited {expected_code}\n"


def test_wait_summary_for_all_resolved_loop_exit(monkeypatch, capsys):
    shipped = snapshot(stage="shipped", active=False)

    rc, _, _ = run_local_wait(
        monkeypatch,
        snapshot(),
        [("found", shipped)],
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == "hop wait: abc123 shipped — exited 0\n"


def test_wait_summary_for_not_found(monkeypatch, capsys):
    initial = snapshot(host="fedora.local")
    rc, _ = run_remote_wait(
        monkeypatch,
        initial,
        [(None, "absent"), (None, "absent")],
        publish=False,
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err == "hop wait: abc123 not_found — exited 1\n"


def test_wait_summary_for_stuck(monkeypatch, capsys):
    stuck = snapshot(state="stuck", status="No output")
    rc, _, _ = run_local_wait(
        monkeypatch,
        stuck,
        [("found", stuck), ("found", stuck)],
    )

    captured = capsys.readouterr()
    assert rc == 3
    assert captured.err == "hop wait: abc123 stuck — exited 3\n"


def test_wait_summary_for_timeout(monkeypatch, capsys):
    rc, _, _ = run_local_wait(monkeypatch, snapshot(), timeout_s=5)

    captured = capsys.readouterr()
    assert rc == 4
    assert captured.err == "hop wait: abc123 timeout — exited 4\n"


def test_wait_summary_for_observer_unavailable(monkeypatch, capsys):
    rc, _, _ = run_local_wait(
        monkeypatch,
        snapshot(),
        observer_timeout_s=5,
    )

    captured = capsys.readouterr()
    assert rc == 4
    assert captured.err == "hop wait: abc123 observer_unavailable — exited 4\n"


def test_wait_summary_for_resolution_failure(monkeypatch, capsys):
    record = wait._resolution_failure_record(
        "abc123",
        {"outcome": "absent", "probes": [configured_probe(outcome="absent")]},
        0.0,
    )
    monkeypatch.setattr(
        wait,
        "_resolve_target",
        lambda *args, **kwargs: record,
    )

    rc, _, _ = run_local_wait(monkeypatch, snapshot())

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err == "hop wait: abc123 target_absent — exited 1\n"


def test_wait_summary_for_loop_interrupt(monkeypatch, capsys):
    def interrupt(clock, timeout, connection):
        raise KeyboardInterrupt

    rc, _, _ = run_local_wait(monkeypatch, snapshot(), wait_action=interrupt)

    captured = capsys.readouterr()
    assert rc == 130
    assert captured.err == "hop wait: abc123 interrupted — exited 130\n"
    assert "abc123 interrupted" in captured.out


def test_wait_summary_for_pre_supervisor_interrupt(monkeypatch, capsys):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(wait, "_resolve_target", interrupt)

    rc, _, _ = run_local_wait(monkeypatch, snapshot())

    captured = capsys.readouterr()
    assert rc == 130
    assert captured.err == "hop wait: abc123 interrupted — exited 130\n"
    assert "abc123 interrupted" in captured.out


def test_wait_summary_keeps_json_stdout_parseable(monkeypatch, capsys):
    rc, _, _ = run_local_wait(
        monkeypatch,
        snapshot(stage="shipped", active=False),
        json_output=True,
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert json.loads(captured.out)["outcome"] == "shipped"
    assert captured.err == "hop wait: abc123 shipped — exited 0\n"


def test_wait_summary_uses_collector_outcome(monkeypatch, capsys):
    def collect_synthetic(state, now):
        return [{"record": state["record"], "outcome": "shipped", "code": 0}]

    monkeypatch.setattr(wait, "_collect_boundary_outcomes", collect_synthetic)

    rc, _, _ = run_local_wait(monkeypatch, snapshot())

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == "hop wait: abc123 shipped — exited 0\n"


def test_wait_summary_format_failure_preserves_exit_code(monkeypatch, capsys):
    initial = snapshot(stage="shipped", active=False)
    expected_rc, _, _ = run_local_wait(monkeypatch, initial)
    capsys.readouterr()
    real_finish_boundary = wait._finish_boundary

    def finish_with_malformed_resolved_item(state, outcomes, now):
        result = real_finish_boundary(state, outcomes, now)
        del state["resolved_outcome"]["final_record"]["outcome"]
        return result

    monkeypatch.setattr(wait, "_finish_boundary", finish_with_malformed_resolved_item)

    rc, _, _ = run_local_wait(monkeypatch, initial)

    captured = capsys.readouterr()
    assert rc == expected_rc == 0
    assert captured.err == ""


FINAL_OUTCOME_CASES = [
    ("shipped", 0, "shipping_completed"),
    ("archived", 1, "archived_before_shipping"),
    ("error", 1, "lode_error"),
    ("inactive", 1, "runner_inactive"),
    ("gated", 2, "gate_requires_review"),
    ("stuck", 3, "progress_stalled"),
    ("not_found", 1, "target_disappeared"),
    ("action_blocked", 2, "durable_action_blocked"),
    ("action_stalled", 2, "successor_adoption_stalled"),
    ("startup_stalled", 1, "startup_registration_stalled"),
    ("handoff_stalled", 1, "ready_handoff_stalled"),
    ("reconnect_stalled", 1, "runner_reregistration_stalled"),
    ("observer_unavailable", 4, "observer_freshness_expired"),
    ("timeout", 4, "overall_timeout"),
    ("target_absent", 1, "target_absent"),
    ("target_ambiguous", 1, "target_ambiguous"),
    ("status_unavailable", 4, "initial_status_unavailable"),
    ("interrupted", 130, "user_interrupted"),
]


def _scenario_probe_rows(topology):
    if topology == "local":
        return [
            {
                "kind": "local",
                "server": "test-host",
                "route": "local",
                "candidate_id": None,
                "outcome": "found",
                "detail": None,
                "attempts": 1,
                "observed_age_s": 0.0,
            }
        ]
    if topology == "explicit":
        return [
            {
                "kind": "explicit",
                "server": "explicit.example",
                "route": "explicit.example",
                "candidate_id": None,
                "outcome": "found",
                "detail": None,
                "attempts": 1,
                "observed_age_s": 0.0,
            }
        ]
    return [
        {
            "kind": "local",
            "server": "test-host",
            "route": "local",
            "candidate_id": None,
            "outcome": "absent",
            "detail": None,
            "attempts": 1,
            "observed_age_s": 0.0,
        },
        {
            "kind": "resident",
            "server": "resident.example",
            "route": "resident.example",
            "candidate_id": "case1234",
            "outcome": "found",
            "detail": None,
            "attempts": 1,
            "observed_age_s": 0.0,
        },
        {
            "kind": "pool",
            "server": "pool.example",
            "route": "pool.example",
            "candidate_id": None,
            "outcome": "not_attempted",
            "detail": None,
            "attempts": 0,
            "observed_age_s": None,
        },
    ]


@pytest.mark.parametrize(("outcome", "code", "reason_code"), FINAL_OUTCOME_CASES)
@pytest.mark.parametrize("json_output", [False, True], ids=["human", "json"])
@pytest.mark.parametrize("topology", ["local", "resident", "explicit"])
def test_closed_final_record_scenario_matrix(
    monkeypatch,
    capsys,
    outcome,
    code,
    reason_code,
    json_output,
    topology,
):
    """Every renderer consumes one closed record for every outcome and topology."""
    route = {
        "local": "local",
        "resident": "resident.example",
        "explicit": "explicit.example",
    }[topology]
    probes = _scenario_probe_rows(topology)
    if outcome in {"target_absent", "target_ambiguous", "status_unavailable"}:
        resolution_outcome = {
            "target_absent": "absent",
            "target_ambiguous": "ambiguous",
            "status_unavailable": "unavailable",
        }[outcome]
        result = {
            "outcome": resolution_outcome,
            "host": route if route != "local" else None,
            "resident_owner": topology == "resident",
            "probes": probes,
            "match_tuples": (
                [("one.example", "case1111"), (None, "case2222")]
                if outcome == "target_ambiguous"
                else []
            ),
        }
        record = wait._resolution_failure_record("case", result, 0.0)
    else:
        initial = snapshot(
            lid="case1234",
            tmux_pane="%7",
            archived=outcome == "archived",
        )
        record = wait._new_record(
            "case1234",
            initial,
            route,
            0.0,
            probes=probes,
        )
    monkeypatch.setattr(
        wait.remote,
        "run_remote",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", ""),
    )
    monkeypatch.setattr(wait, "capture_pane", lambda *_args, **_kwargs: None)
    deadline = make_deadline(60)
    final_record = wait._final_record(
        record,
        outcome,
        code,
        0.0,
        reason_code,
        deadline=deadline,
        child_control=remote.make_child_registry(),
    )
    wait._emit_outcome(
        final_record,
        json_output,
        deadline=deadline,
    )
    item = {
        "record": record,
        "outcome": outcome,
        "code": code,
        "reason": reason_code,
        "final_record": final_record,
    }
    wait._emit_wait_summary(item, code)

    captured = capsys.readouterr()
    assert set(final_record) == wait.FINAL_RECORD_KEYS
    assert final_record["reason_code"] == reason_code
    assert final_record["reason"] == wait.REASON_TEXT[reason_code]
    if outcome == "shipped":
        assert final_record["recovery"] is None
    else:
        assert isinstance(final_record["recovery"], str)
    assert [row["kind"] for row in final_record["probes"]] == {
        "local": ["local"],
        "resident": ["local", "resident", "pool"],
        "explicit": ["explicit"],
    }[topology]
    assert all(
        set(row)
        == {
            "kind",
            "server",
            "route",
            "candidate_id",
            "outcome",
            "detail",
            "attempts",
            "observed_age_s",
        }
        for row in final_record["probes"]
    )
    if outcome == "target_ambiguous":
        assert final_record["matches"] == [
            {"server": "one.example", "id": "case1111"},
            {"server": None, "id": "case2222"},
        ]
    if outcome == "observer_unavailable":
        assert final_record["tmux_pane"] is None
        assert final_record["last_tmux_pane"] == "%7"
    if json_output:
        assert len(captured.out.splitlines()) == 1
        assert json.loads(captured.out) == final_record
    else:
        assert captured.out
        assert not captured.out.lstrip().startswith("{")
    assert captured.err.endswith(f"hop wait: {final_record['id']} {outcome} — exited {code}\n")


def test_human_renderer_uses_only_constructed_record_outcome(monkeypatch, capsys):
    record = wait._new_record(
        "case1234",
        snapshot(lid="case1234", stage="shipped", active=False),
        "local",
        0.0,
        probes=[configured_probe()],
    )
    final_record = wait._final_record(
        record,
        "shipped",
        0,
        0.0,
        None,
        deadline=make_deadline(60),
        child_control=remote.make_child_registry(),
    )
    stale_boundary_item = {"outcome": "error", "final_record": final_record}

    wait._emit_outcome(
        stale_boundary_item["final_record"],
        False,
        deadline=make_deadline(60),
    )

    assert capsys.readouterr().out == (
        "✓ case1234 shipped\n"
        "  why: Shipping completed. [shipping_completed]\n"
        "  server: test-host (route: local)\n"
        "  status: Working (stage=shipped state=running active=False archived=False)\n"
        "  pane: <unavailable>\n"
        "  worktree: <unavailable> (basis=unavailable exists=<unavailable>)\n"
        "  recovery: <none>\n"
    )


def _boundary_state(record, *, observer_timeout_s=300):
    return {
        "record": record,
        "resolved": False,
        "observer_timeout_s": observer_timeout_s,
        "overall_deadline": 100.0,
    }


def test_definitive_state_at_boundary_beats_observer_and_overall_deadlines():
    record = wait._new_record(
        "ship1",
        snapshot(lid="ship1", stage="shipped", active=False),
        "local",
        0.0,
        probes=[configured_probe()],
    )
    state = _boundary_state(record, observer_timeout_s=10)
    state["overall_deadline"] = 10.0

    outcomes = wait._collect_boundary_outcomes(state, 10.0)

    assert [(item["outcome"], item["code"]) for item in outcomes] == [("shipped", 0)]


def test_observer_deadline_wins_exact_tie_with_overall_deadline():
    record = wait._new_record(
        "run1", snapshot(lid="run1"), "local", 0.0, probes=[configured_probe()]
    )
    state = _boundary_state(record, observer_timeout_s=10)
    state["overall_deadline"] = 10.0

    outcomes = wait._collect_boundary_outcomes(state, 10.0)

    assert [(item["outcome"], item["code"]) for item in outcomes] == [("observer_unavailable", 4)]


def test_final_enrichment_failures_degrade_fields_without_changing_outcome(monkeypatch):
    record = wait._new_record(
        "error1",
        snapshot(lid="error1", state="error", tmux_pane="%7"),
        "local",
        0.0,
        probes=[configured_probe()],
    )
    monkeypatch.setattr(
        wait.config,
        "hostname",
        lambda: (_ for _ in ()).throw(OSError("hostname unavailable")),
    )
    monkeypatch.setattr(
        wait,
        "lode_with_status_annotations",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("tmux unavailable")),
    )
    monkeypatch.setattr(
        wait,
        "resolve_worktree_path",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("path unavailable")),
    )

    final_record = wait._final_record(
        record,
        "error",
        1,
        0.0,
        None,
        deadline=make_deadline(60),
        child_control=remote.make_child_registry(),
    )

    assert final_record["outcome"] == "error"
    assert final_record["exit_code"] == 1
    assert final_record["server"] is None
    assert final_record["probes"][0]["server"] is None
    assert final_record["pane_liveness"] == "unknown"
    assert final_record["worktree_path"] is None
    assert final_record["worktree_path_basis"] == "unavailable"
    assert final_record["worktree_exists"] is None


def test_final_local_worktree_check_is_fresh_and_recorded_when_cleaned(tmp_path):
    path = tmp_path / "worktrees" / "local123"
    record = wait._new_record(
        "local123",
        snapshot(lid="local123", worktree_path=str(path), worktree_exists=True),
        "local",
        0.0,
        probes=[configured_probe()],
    )

    final_record = wait._final_record(
        record,
        "error",
        1,
        0.0,
        None,
        deadline=make_deadline(60),
        child_control=remote.make_child_registry(),
    )

    assert final_record["worktree_path"] == str(path)
    assert final_record["worktree_path_basis"] == "recorded"
    assert final_record["worktree_exists"] is False
    assert final_record["worktree_exists_observed_age_s"] == 0.0


def test_final_remote_worktree_check_uses_frozen_path_payload(monkeypatch):
    path = "/srv/worktrees/remote123"
    record = wait._new_record(
        "remote123",
        snapshot(lid="remote123", worktree_path=path, worktree_exists=True),
        "owner.example",
        0.0,
        probes=[configured_probe("owner.example")],
    )
    remote_result = subprocess.CompletedProcess(
        [],
        0,
        json.dumps(
            {
                "id": "remote123",
                "host": "local",
                "path": path,
                "exists": False,
            }
        ),
        "",
    )
    monkeypatch.setattr(wait.remote, "run_remote", MagicMock(return_value=remote_result))

    final_record = wait._final_record(
        record,
        "error",
        1,
        0.0,
        None,
        deadline=make_deadline(60),
        child_control=remote.make_child_registry(),
    )

    assert final_record["worktree_path"] == path
    assert final_record["worktree_path_basis"] == "recorded"
    assert final_record["worktree_exists"] is False
    assert final_record["worktree_exists_observed_age_s"] is not None
    assert wait.remote.run_remote.call_args.args[:2] == (
        "owner.example",
        ["lode", "path", "remote123", "--json"],
    )


def test_probe_rows_update_in_place_instead_of_appending():
    record = wait._new_record("abc123", snapshot(), "local", 0.0, probes=[configured_probe()])

    wait._apply_observation(
        record,
        {
            "id": "abc123",
            "kind": "unreadable",
            "payload": None,
            "detail": "first failure",
            "failure_key": "first",
            "observed_ts": 1.0,
        },
        30.0,
    )
    wait._apply_observation(
        record,
        {
            "id": "abc123",
            "kind": "found",
            "payload": snapshot(status="Recovered"),
            "detail": "",
            "failure_key": "found",
            "observed_ts": 2.0,
        },
        30.0,
    )

    assert len(record["probes"]) == 1
    assert record["probes"][0]["outcome"] == "found"
    assert record["probes"][0]["attempts"] == 3
