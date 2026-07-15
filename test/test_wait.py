# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for authoritative supervised lode waiting."""

import json
import threading
from collections import deque
from pathlib import Path

import pytest

import hopper.wait as wait


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeConnection:
    def __init__(self, on_start=None):
        self.on_start = on_start
        self.callback = None
        self.on_connect = None
        self.stopped = False

    def start(self, callback=None, on_connect=None):
        self.callback = callback
        self.on_connect = on_connect
        if self.on_start:
            self.on_start(callback, on_connect)
        elif on_connect:
            on_connect()

    def stop(self):
        self.stopped = True


def snapshot(lid="abc123", **overrides):
    return {
        "id": lid,
        "stage": "mill",
        "state": "running",
        "status": "Working",
        "active": True,
        "title": "",
        **overrides,
    }


def run_local_wait(
    monkeypatch,
    initial,
    observations=(),
    *,
    timeout_s=0,
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

    def condition_wait(condition, timeout):
        if wait_action:
            wait_action(clock, timeout, connection)
        else:
            clock.now += timeout

    monkeypatch.setattr(wait, "_condition_wait", condition_wait)
    monkeypatch.setattr(wait.client, "get_lode", lambda *args, **kwargs: dict(initial))
    monkeypatch.setattr(wait.client, "HopperConnection", lambda socket_path: connection)

    def read_snapshot(socket_path, lid):
        nonlocal last
        if scripted:
            current = scripted.popleft()
            if isinstance(current, Exception):
                raise current
            last = current
        return last

    monkeypatch.setattr(wait, "read_local_snapshot", read_snapshot)
    rc = wait.wait_for_lodes(
        Path("server.sock"),
        [initial["id"]],
        timeout_s=timeout_s,
        poll_s=30,
        observer_timeout_s=observer_timeout_s,
        json_output=json_output,
        lookup_local=lambda socket_path, lid: (None, f"Lode '{lid}' not found."),
        find_remote=lambda lid: (None, ""),
        probe_remote=lambda *args, **kwargs: (None, "unreadable"),
    )
    return rc, clock, connection


def install_synchronous_remote_driver(monkeypatch, clock):
    """Run one production worker iteration at each simulated poll deadline."""
    holder = {}

    def post_one(state, probe_remote, lid):
        record = state["records"][lid]
        one_shot = threading.Event()
        worker_state = {**state, "stop_event": one_shot, "workers": {}}

        def probe_once(*args, **kwargs):
            try:
                return probe_remote(*args, **kwargs)
            finally:
                one_shot.set()

        wait._remote_worker(
            worker_state,
            lid,
            record["host"],
            state["poll_s"],
            state["probe_timeout_s"],
            probe_once,
        )

    def start_workers(state, probe_remote):
        holder["state"] = state
        holder["probe"] = probe_remote
        for lid in list(state["pending"]):
            if state["records"][lid]["remote"]:
                post_one(state, probe_remote, lid)

    def condition_wait(condition, timeout):
        clock.now += timeout
        state = holder["state"]
        for lid in list(state["pending"]):
            record = state["records"][lid]
            if record["remote"] and clock.now >= record["next_reconcile_ts"]:
                post_one(state, holder["probe"], lid)

    monkeypatch.setattr(wait, "_start_remote_workers", start_workers)
    monkeypatch.setattr(wait, "_stop_remote_workers", lambda state: None)
    monkeypatch.setattr(wait, "_condition_wait", condition_wait)


def run_remote_wait(
    monkeypatch,
    initials,
    probes,
    *,
    timeout_s=0,
    observer_timeout_s=300,
    json_output=False,
    publish=True,
):
    clock = FakeClock()
    install_synchronous_remote_driver(monkeypatch, clock)
    monkeypatch.setattr(wait, "_monotonic", clock)
    monkeypatch.setattr(wait.client, "get_lode", lambda *args, **kwargs: None)
    if not publish:
        monkeypatch.setattr(wait, "_publish_remote_mappings", lambda records: None)

    def find_remote(lid):
        lode = initials[lid]
        return dict(lode), lode["host"]

    queues = {lid: deque(items) for lid, items in probes.items()}
    last = {}

    def probe_remote(host, lid, timeout):
        if queues[lid]:
            current = queues[lid].popleft()
            last[lid] = current
        else:
            current = last[lid]
        if isinstance(current, Exception):
            raise current
        return current

    rc = wait.wait_for_lodes(
        Path("server.sock"),
        list(initials),
        timeout_s=timeout_s,
        poll_s=30,
        observer_timeout_s=observer_timeout_s,
        json_output=json_output,
        lookup_local=lambda socket_path, lid: (None, f"Lode '{lid}' not found."),
        find_remote=find_remote,
        probe_remote=probe_remote,
    )
    return rc, clock


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"state": "error"}, ("error", 1)),
        ({"state": "gated"}, ("gated", 2)),
        ({"state": "stuck"}, ("stuck", 3)),
        ({"stage": "shipped", "active": False}, ("shipped", 0)),
        ({"active": False}, ("inactive", 1)),
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
    assert payload == {
        "id": "abc123",
        "outcome": outcome,
        "stage": initial["stage"],
        "state": initial["state"],
        "status": initial["status"],
        "active": initial["active"],
        "source": "local",
        "observed_age_s": 0.0,
    }


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {"id": "wrong", "stage": "mill", "state": "running", "status": "", "active": True},
        {"id": "abc123", "state": "running", "status": "", "active": True},
        {"id": "abc123", "stage": 1, "state": "running", "status": "", "active": True},
        {"id": "abc123", "stage": "mill", "state": "running", "status": "", "active": 1},
    ],
)
def test_validate_snapshot_rejects_malformed_or_wrong_lode(raw):
    assert wait.validate_snapshot(raw, "abc123") is None


def test_read_local_snapshot_returns_active_lode(monkeypatch):
    lode = snapshot()
    monkeypatch.setattr(
        wait.client,
        "connect",
        lambda *args, **kwargs: {"lode_found": True, "lode": lode},
    )

    assert wait.read_local_snapshot(Path("server.sock"), "abc123") == ("found", lode)


def test_read_local_snapshot_falls_back_to_archived(monkeypatch):
    archived = snapshot(stage="shipped", active=False)
    monkeypatch.setattr(wait.client, "connect", lambda *args, **kwargs: {"lode_found": False})
    monkeypatch.setattr(wait.client, "read_archived_lodes", lambda *args, **kwargs: [archived])

    assert wait.read_local_snapshot(Path("server.sock"), "abc123") == ("found", archived)


@pytest.mark.parametrize(
    ("connected", "archived", "expected"),
    [
        ({"lode_found": False}, [], "absent"),
        ({"lode_found": False}, None, "unreadable"),
        (None, [], "unreadable"),
    ],
)
def test_read_local_snapshot_distinguishes_absent_from_unreadable(
    monkeypatch, connected, archived, expected
):
    monkeypatch.setattr(wait.client, "connect", lambda *args, **kwargs: connected)
    monkeypatch.setattr(wait.client, "read_archived_lodes", lambda *args, **kwargs: archived)

    assert wait.read_local_snapshot(Path("server.sock"), "abc123") == (expected, None)


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
    assert out.count("shipped") == 1
    assert "Real result" in out
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
    assert "Recovered ship" not in capsys.readouterr().out


def test_later_inactive_snapshot_exits_with_recovery_guidance(monkeypatch, capsys):
    inactive = snapshot(state="paused", status="Stopped", active=False)
    rc, _, _ = run_local_wait(monkeypatch, snapshot(), [("found", inactive)])

    assert rc == 1
    out = capsys.readouterr().out
    assert "state=paused active=False status=Stopped" in out
    assert "hop lode resume abc123 or hop lode restart abc123" in out


def test_two_consecutive_not_found_win_observer_boundary(monkeypatch, capsys):
    initial = snapshot(host="fedora.local")
    rc, clock = run_remote_wait(
        monkeypatch,
        {"abc123": initial},
        {"abc123": [(None, "absent"), (None, "absent")]},
        observer_timeout_s=30,
        json_output=True,
        publish=False,
    )

    assert rc == 1
    assert clock.now == 30
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "not_found"
    assert payload["source"] == "fedora.local"


def test_not_found_streak_resets_on_observer_failure(monkeypatch, capsys):
    initial = snapshot(host="fedora.local")
    rc, _ = run_remote_wait(
        monkeypatch,
        {"abc123": initial},
        {
            "abc123": [
                (None, "absent"),
                (None, "unreadable"),
                (None, "absent"),
                (None, "absent"),
            ]
        },
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
        "wrong-id",
        "unexpected-exception",
    ],
)
def test_remote_failures_follow_observer_health_policy(monkeypatch, capsys, failure):
    initial = snapshot(host="fedora.local")
    rc, _ = run_remote_wait(
        monkeypatch,
        {"abc123": initial},
        {"abc123": [failure, failure]},
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
        {"abc123": initial},
        {
            "abc123": [
                (None, "unreadable"),
                (None, "unreadable"),
                (snapshot(host="fedora.local", status="Recovered"), "found"),
                (shipped, "found"),
            ]
        },
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
    failure = ("unreadable", None)
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
        [("unreadable", None)],
        timeout_s=5,
        observer_timeout_s=45,
        json_output=True,
    )

    assert rc == 4
    assert clock.now == 5
    assert json.loads(capsys.readouterr().out)["outcome"] == "timeout"


def test_disabled_observer_timeout_retries_until_overall_timeout(monkeypatch, capsys):
    failure = ("unreadable", None)
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
        {"abc123": stuck},
        {"abc123": [(stuck, "found")]},
        publish=False,
    )

    assert rc == 3
    assert clock.now == 150
    assert "abc123 stuck: No output" in capsys.readouterr().out


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
        {"abc123": initial},
        {"abc123": [(later, "found"), failure, failure]},
        observer_timeout_s=75,
        json_output=json_output,
        publish=False,
    )

    assert rc == 4
    assert clock.now == 75
    captured = capsys.readouterr()
    if json_output:
        payload = json.loads(captured.out)
        assert payload == {
            "id": "abc123",
            "outcome": "observer_unavailable",
            "stage": "refine",
            "state": "design",
            "status": "Later durable status",
            "active": True,
            "source": "fedora.local",
            "observed_age_s": 75.0,
            "host": "fedora.local",
        }
    else:
        assert "stage=refine state=design active=True" in captured.out
        assert "status=Later durable status source=fedora.local" in captured.out
        assert "observed_age_s=75.000" in captured.out


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
    monkeypatch.setattr(wait.remote, "load_lode_cache", lambda: {})
    monkeypatch.setattr(
        wait.remote,
        "remember_lode",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read-only cache")),
    )
    probes = {"abc123": [(shipped, "found")]}

    rc, _ = run_remote_wait(
        monkeypatch,
        {"abc123": initial},
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
        lambda: (_ for _ in ()).throw(OSError("unreadable cache")),
    )

    rc, _ = run_remote_wait(
        monkeypatch,
        {"abc123": initial},
        {"abc123": [(initial, "found")]},
    )

    assert rc == 0
    captured = capsys.readouterr()
    assert captured.err.count("warning: could not read remote lode cache") == 1
    assert "abc123 shipped" in captured.out


def test_unchanged_remote_mapping_is_not_republished(monkeypatch):
    initial = snapshot(host="fedora.local", stage="shipped", active=False)
    monkeypatch.setattr(
        wait.remote,
        "load_lode_cache",
        lambda: {"abc123": {"host": "fedora.local"}},
    )
    calls = []
    monkeypatch.setattr(wait.remote, "remember_lode", lambda *args: calls.append(args))

    rc, _ = run_remote_wait(
        monkeypatch,
        {"abc123": initial},
        {"abc123": [(initial, "found")]},
    )

    assert rc == 0
    assert calls == []


def test_remote_completed_stage_does_not_resolve_before_shipped(monkeypatch, capsys):
    initial = snapshot(host="fedora.local")
    completed = snapshot(host="fedora.local", stage="refine", state="completed")
    shipped = snapshot(host="fedora.local", stage="shipped", state="completed")
    rc, clock = run_remote_wait(
        monkeypatch,
        {"abc123": initial},
        {"abc123": [(completed, "found"), (shipped, "found")]},
        publish=False,
    )

    assert rc == 0
    assert clock.now == 30
    assert capsys.readouterr().out.count("shipped") == 1


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
    state = {
        "condition": condition,
        "pending": {"abc123"},
        "observations": deque(),
        "stop_event": stop_event,
        "shutdown": False,
    }

    def probe(host, lid, timeout):
        stop_event.set()
        if isinstance(probe_result, Exception):
            raise probe_result
        return probe_result

    wait._remote_worker(state, "abc123", "fedora.local", 30, 5, probe)

    assert len(state["observations"]) == 1
    assert state["observations"][0]["kind"] == expected_kind


def test_remote_worker_join_is_bounded_and_logs_timeout(caplog):
    class StuckThread:
        def __init__(self):
            self.timeout = None

        def join(self, timeout):
            self.timeout = timeout

        def is_alive(self):
            return True

    thread = StuckThread()
    state = {
        "stop_event": threading.Event(),
        "workers": {"abc123": thread},
        "probe_timeout_s": 5,
    }

    wait._stop_remote_workers(state)

    assert thread.timeout == 6
    assert "did not stop before join timeout" in caplog.text


def test_multi_lode_shipped_sibling_then_observer_failure_stops_all_workers(monkeypatch, capsys):
    initials = {
        "ship123": snapshot(lid="ship123", host="one.local"),
        "slow123": snapshot(lid="slow123", host="two.local"),
    }
    shipped = snapshot(lid="ship123", host="one.local", stage="shipped", active=False)

    monkeypatch.setattr(wait.client, "get_lode", lambda *args, **kwargs: None)
    monkeypatch.setattr(wait, "_publish_remote_mappings", lambda records: None)

    def find_remote(lid):
        return initials[lid], initials[lid]["host"]

    def probe_remote(host, lid, timeout):
        if lid == "ship123":
            return shipped, "found"
        return None, "unreadable"

    rc = wait.wait_for_lodes(
        Path("server.sock"),
        list(initials),
        timeout_s=1,
        poll_s=30,
        observer_timeout_s=0.05,
        lookup_local=lambda socket_path, lid: (None, f"Lode '{lid}' not found."),
        find_remote=find_remote,
        probe_remote=probe_remote,
    )

    assert rc == 4
    assert capsys.readouterr().out.count("ship123 shipped") == 1
    assert not any(thread.name.startswith("wait-remote-") for thread in threading.enumerate())


def test_jsonl_stdout_contains_only_terminal_records(monkeypatch, capsys):
    initial = snapshot(status="Latest")
    rc, _, _ = run_local_wait(
        monkeypatch,
        initial,
        [("unreadable", None), ("unreadable", None)],
        observer_timeout_s=45,
        json_output=True,
    )

    assert rc == 4
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert set(payload) == {
        "id",
        "outcome",
        "stage",
        "state",
        "status",
        "active",
        "source",
        "observed_age_s",
    }
    assert "warning:" not in captured.out
    assert "warning:" in captured.err
