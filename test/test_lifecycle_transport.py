# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Behavior tests for the bounded, immutable lode lifecycle notification transport."""

import json
import logging
import socket
import threading
import time

import pytest

import hopper.server as server_module
import hopper.transport as transport_module
from hopper.lodes import (
    make_lode_stage_sessions,
    project_lode_claude_state,
    save_archived_lodes,
    save_lodes,
)
from hopper.server import Server
from hopper.transport import ACTIVE_ROOT, ARCHIVE_ROOT

LIFECYCLE_TYPES = ("lode_created", "lode_updated", "lode_archived", "lode_unarchived")


@pytest.fixture
def socket_path(tmp_path):
    return tmp_path / "test.sock"


@pytest.fixture
def srv(socket_path):
    """An unstarted server: real transport, no writer thread stealing envelopes."""
    return Server(socket_path)


def _lode(lode_id, marker, **overrides):
    """A complete lode whose marker appears only inside nested containers."""
    lode = {
        "id": lode_id,
        "stage": "mill",
        "created_at": 1000,
        "updated_at": 1000,
        "project": "proj",
        "scope": "",
        "state": "new",
        "status": "",
        "active": False,
        "worktree_path": None,
        "runs": {"gen-one": {"attempts": [{"tag": f"attempt-{marker}"}]}},
        "pending_action": {"action_id": f"action-{marker}"},
        "action_results": [{"receipt": f"receipt-{marker}"}],
        "backlog": None,
        "driver": "claude",
        "stage_sessions": make_lode_stage_sessions(
            lode_id,
            {
                "mill": "00000000-0000-0000-0000-000000000001",
                "refine": "00000000-0000-0000-0000-000000000002",
                "ship": "00000000-0000-0000-0000-000000000003",
            },
        ),
    }
    lode.update(overrides)
    project_lode_claude_state(lode)
    return lode


def _markers(event):
    """Every sentinel marker reachable inside the event's nested containers."""
    found = set()
    for container in (
        event["lode"]["runs"]["gen-one"]["attempts"][0]["tag"],
        event["lode"]["pending_action"]["action_id"],
        event["lode"]["action_results"][0]["receipt"],
    ):
        found.add(container.rsplit("-", 1)[1])
    return found


def _drain(server):
    """Take every pending lifecycle envelope through the production selector."""
    events = []
    while True:
        selection = server.transport.select(lambda: False, 0)
        if selection is None:
            break
        events.append(json.loads(selection.data.decode("utf-8")))
    return events


def _step(server):
    """One production writer step: real predicate, real selector, real FIFO."""
    selection = server.transport.select(lambda: not server.broadcast_queue.empty(), 0)
    if selection is None:
        return None
    if selection.kind == "ordinary":
        return ("ordinary", server.broadcast_queue.get_nowait())
    return ("lifecycle", json.loads(selection.data.decode("utf-8")))


def _register_socket(server):
    """Attach a real connected socket to the server exactly as a client connection is."""
    server_end, client_end = socket.socketpair()
    client_end.settimeout(2.0)
    with server.lock:
        server.clients.append(server_end)
        server.write_locks[server_end] = threading.Lock()
    return server_end, client_end


def _recv_until(client_end, done, timeout=5.0):
    """Read whole JSONL frames off a real socket until `done` accepts the collected list."""
    events = []
    buffer = ""
    deadline = time.time() + timeout
    while not done(events) and time.time() < deadline:
        try:
            chunk = client_end.recv(65536).decode("utf-8")
        except socket.timeout:
            continue
        if not chunk:
            break
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            if line:
                events.append(json.loads(line))
    return events


def _recv_events(client_end, count, timeout=3.0):
    """Read `count` whole JSONL frames off a real socket."""
    events = []
    buffer = ""
    deadline = time.time() + timeout
    while len(events) < count and time.time() < deadline:
        try:
            chunk = client_end.recv(65536).decode("utf-8")
        except socket.timeout:
            continue
        if not chunk:
            break
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            if line:
                events.append(json.loads(line))
    return events


# --- AC1 / AC10: one fallible step, before ownership ---------------------------------


def test_broadcast_refuses_every_inconsistency_without_touching_transport_state(srv):
    lode = _lode("abcd1234", "A")
    refusals = [
        ([], None),
        ({"data": "no type"}, None),
        ({"type": []}, None),
        ({"type": {}}, None),
        ({"type": "lode_updated", "lode": lode}, None),
        ({"type": "lode_updated", "lode": lode}, "somewhere-else"),
        ({"type": "lode_archived", "lode": lode}, ACTIVE_ROOT),
        ({"type": "lode_created", "lode": lode}, ARCHIVE_ROOT),
        ({"type": "lode_unarchived", "lode": lode}, ARCHIVE_ROOT),
        ({"type": "lode_updated", "lode": "not-a-dict"}, ACTIVE_ROOT),
        ({"type": "lode_updated", "lode": {"stage": "mill"}}, ACTIVE_ROOT),
        ({"type": "lode_updated", "lode": {"id": ""}}, ACTIVE_ROOT),
        ({"type": "lode_updated", "lode": {"id": 17}}, ACTIVE_ROOT),
        ({"type": "lode_updated", "lode": {"id": "abcd1234"}}, ACTIVE_ROOT),
        (
            {"type": "lode_updated", "lode": _lode("abcd1234", "A", active="yes")},
            ACTIVE_ROOT,
        ),
        ({"type": "lode_updated", "lode": {"id": "abcd1234", "runs": {1, 2}}}, ACTIVE_ROOT),
        (
            {"type": "lode_updated", "lode": _lode("abcd1234", "A", runs={"n": float("nan")})},
            ACTIVE_ROOT,
        ),
        (
            {"type": "lode_updated", "lode": _lode("abcd1234", "A", runs={"n": float("inf")})},
            ACTIVE_ROOT,
        ),
        (
            {
                "type": "lode_updated",
                "lode": _lode("abcd1234", "A", runs={"n": float("-inf")}),
            },
            ACTIVE_ROOT,
        ),
        (
            {"type": "lode_updated", "lode": _lode("abcd1234", "A", runs={1: "bad"})},
            ACTIVE_ROOT,
        ),
        ({"type": "backlog_added", "item": {}}, ACTIVE_ROOT),
    ]
    for message, root in refusals:
        assert srv.broadcast(message, root=root) is False, message
        assert srv.transport.pending_ids() == [], message
        assert srv.transport.claims() == [], message
        assert srv.broadcast_queue.empty(), message


@pytest.mark.parametrize("field", ["project", "stage", "state", "status"])
def test_broadcast_independently_refuses_each_wrong_typed_required_string(srv, field):
    assert (
        srv.broadcast(
            {"type": "lode_updated", "lode": _lode("abcd1234", "A", **{field: 17})},
            root=ACTIVE_ROOT,
        )
        is False
    )
    assert srv.transport.pending_ids() == []
    assert srv.transport.claims() == []


def test_strict_json_accepts_a_finite_nested_float(srv):
    assert srv.broadcast(
        {
            "type": "lode_created",
            "lode": _lode("abcd1234", "FINITE", runs={"ratio": 1.25}),
        },
        root=ACTIVE_ROOT,
    )

    (event,) = _drain(srv)
    assert event["lode"]["runs"] == {"ratio": 1.25}


def test_transport_survives_a_refusal_and_still_delivers_both_classes(srv):
    assert (
        srv.broadcast({"type": "lode_archived", "lode": _lode("abcd1234", "A")}, root=ACTIVE_ROOT)
        is False
    )
    assert srv.transport.pending_ids() == []

    assert srv.broadcast({"type": "lode_updated", "lode": _lode("abcd1234", "B")}, root=ACTIVE_ROOT)
    assert srv.broadcast({"type": "backlog_added", "item": {"id": "b1"}})

    assert _step(srv)[0] == "lifecycle"
    assert _step(srv) == ("ordinary", {"type": "backlog_added", "item": {"id": "b1"}})


def test_lode_updated_is_valid_against_either_root(srv):
    srv.lodes = [_lode("abcd1234", "A")]
    srv.archived_lodes = [_lode("efgh5678", "A")]
    srv.reseed_lifecycle_baseline()

    assert srv.broadcast({"type": "lode_updated", "lode": _lode("abcd1234", "B")}, root=ACTIVE_ROOT)
    assert srv.broadcast(
        {"type": "lode_updated", "lode": _lode("efgh5678", "B")}, root=ARCHIVE_ROOT
    )
    assert [event["type"] for event in _drain(srv)] == ["lode_updated", "lode_updated"]


def test_ordinary_broadcast_refuses_a_root_and_keeps_drop_on_full(srv):
    assert srv.broadcast({"type": "backlog_added", "item": {}}, root=ACTIVE_ROOT) is False
    assert srv.broadcast_queue.empty()

    while not srv.broadcast_queue.full():
        srv.broadcast_queue.put_nowait({"type": "filler"})
    assert srv.broadcast({"type": "backlog_added", "item": {}}) is False

    # The full ordinary FIFO cannot drop a lifecycle event: it does not use that queue.
    assert srv.broadcast({"type": "lode_created", "lode": _lode("abcd1234", "A")}, root=ACTIVE_ROOT)
    assert srv.transport.pending_ids() == ["abcd1234"]


# --- AC2: publisher-owned immutability ------------------------------------------------


def test_caller_mutation_after_return_cannot_change_the_wire(srv):
    lode = _lode("abcd1234", "A")
    message = {"type": "lode_created", "lode": lode}
    assert srv.broadcast(message, root=ACTIVE_ROOT)

    lode["stage_sessions"]["refine"]["started"] = True
    lode["runs"]["gen-one"]["attempts"].append({"tag": "attempt-TAMPERED"})
    lode["pending_action"] = None
    lode["status"] = "tampered"
    message["exchange_id"] = "xyz"
    message["ts"] = 1
    message["type"] = "lode_archived"

    (event,) = _drain(srv)
    assert event["type"] == "lode_created"
    assert _markers(event) == {"A"}
    assert event["lode"]["status"] == ""
    assert event["lode"]["stage_sessions"]["refine"]["started"] is False
    assert len(event["lode"]["runs"]["gen-one"]["attempts"]) == 1
    assert "exchange_id" not in event
    assert event["ts"] > 1


# --- AC3 / AC11: baseline seeding -----------------------------------------------------


def test_seeded_baseline_classifies_existing_lodes_as_updates(srv):
    srv.lodes = [_lode("abcd1234", "A")]
    srv.archived_lodes = [_lode("efgh5678", "A")]
    srv.reseed_lifecycle_baseline()

    assert srv.transport.baseline_root("abcd1234") == ACTIVE_ROOT
    assert srv.transport.baseline_root("efgh5678") == ARCHIVE_ROOT
    assert srv.transport.baseline_root("ijkl9012") is None

    srv.broadcast({"type": "lode_updated", "lode": _lode("abcd1234", "B")}, root=ACTIVE_ROOT)
    srv.broadcast({"type": "lode_updated", "lode": _lode("efgh5678", "B")}, root=ARCHIVE_ROOT)
    srv.broadcast({"type": "lode_unarchived", "lode": _lode("efgh5678", "C")}, root=ACTIVE_ROOT)
    srv.broadcast({"type": "lode_created", "lode": _lode("ijkl9012", "B")}, root=ACTIVE_ROOT)

    events = _drain(srv)
    assert [(event["type"], event["lode"]["id"]) for event in events] == [
        ("lode_updated", "abcd1234"),
        ("lode_unarchived", "efgh5678"),
        ("lode_created", "ijkl9012"),
    ]


def test_seeding_refuses_duplicate_and_cross_root_ids(srv):
    srv.lodes = [_lode("abcd1234", "A"), _lode("abcd1234", "B")]
    with pytest.raises(ValueError, match="not unique"):
        srv.reseed_lifecycle_baseline()

    srv.lodes = [_lode("abcd1234", "A")]
    srv.archived_lodes = [_lode("abcd1234", "A")]
    with pytest.raises(ValueError, match="not unique"):
        srv.reseed_lifecycle_baseline()


def test_projects_reload_preserves_an_unchanged_map_and_reseeds_a_changed_one(srv):
    active = [_lode("abcd1234", "A")]
    save_lodes(active)
    save_archived_lodes([_lode("efgh5678", "A")])
    srv.lodes = list(active)
    srv.archived_lodes = [_lode("efgh5678", "A")]
    srv.reseed_lifecycle_baseline()

    assert srv.broadcast(
        {"type": "lode_updated", "lode": _lode("abcd1234", "PENDING")}, root=ACTIVE_ROOT
    )
    srv._handle_mutation({"type": "projects_reload"}, None)
    assert srv.transport.refusal is None
    assert srv.transport.baseline_root("abcd1234") == ACTIVE_ROOT
    assert srv.transport.baseline_root("efgh5678") == ARCHIVE_ROOT
    assert srv.transport.pending_ids() == ["abcd1234"]
    assert [(event["type"], _markers(event)) for event in _drain(srv)] == [
        ("lode_updated", {"PENDING"})
    ]

    save_lodes([_lode("abcd1234", "A"), _lode("ijkl9012", "A")])
    srv._handle_mutation({"type": "projects_reload"}, None)
    assert srv.transport.refusal is None
    assert srv.transport.baseline_root("ijkl9012") == ACTIVE_ROOT

    srv.broadcast({"type": "lode_updated", "lode": _lode("ijkl9012", "B")}, root=ACTIVE_ROOT)
    assert [event["type"] for event in _drain(srv)] == ["lode_updated"]


def test_projects_reload_with_changed_map_discards_pending_lifecycle_state(srv, caplog):
    active = [_lode("abcd1234", "A")]
    srv.lodes = list(active)
    srv.archived_lodes = [_lode("efgh5678", "A")]
    srv.reseed_lifecycle_baseline()
    assert srv.broadcast(
        {"type": "lode_updated", "lode": _lode("abcd1234", "PENDING")}, root=ACTIVE_ROOT
    )

    save_lodes([_lode("abcd1234", "A"), _lode("ijkl9012", "A")])
    save_archived_lodes([_lode("efgh5678", "A")])
    with caplog.at_level(logging.WARNING, logger="hopper.transport"):
        srv._handle_mutation({"type": "projects_reload"}, None)

    assert srv.transport.pending_ids() == []
    assert srv.transport.claims() == []
    assert "Discarding 1 pending lifecycle lodes during baseline reseed" in caplog.text
    assert "abcd1234" in caplog.text


def test_projects_reload_visibly_refuses_a_cross_root_map_before_classifying_again(srv, caplog):
    srv.lodes = [_lode("abcd1234", "A")]
    srv.reseed_lifecycle_baseline()
    assert srv.broadcast(
        {"type": "lode_updated", "lode": _lode("abcd1234", "PENDING")}, root=ACTIVE_ROOT
    )
    save_lodes([_lode("abcd1234", "A")])
    save_archived_lodes([_lode("abcd1234", "A")])

    srv._handle_mutation({"type": "projects_reload"}, None)

    assert srv.transport.refusal is not None
    assert "reseed refused" in caplog.text
    assert (
        srv.broadcast({"type": "lode_updated", "lode": _lode("abcd1234", "B")}, root=ACTIVE_ROOT)
        is False
    )
    assert srv.transport.pending_ids() == []
    assert srv.transport.claims() == []
    assert "Discarding 1 pending lifecycle lodes during baseline refusal" in caplog.text


def test_closed_transport_refuses_lifecycle_broadcast_without_creating_a_claim(srv):
    srv.transport.close()

    assert (
        srv.broadcast({"type": "lode_created", "lode": _lode("abcd1234", "A")}, root=ACTIVE_ROOT)
        is False
    )
    assert srv.transport.pending_ids() == []
    assert srv.transport.claims() == []


def test_close_is_terminal_and_cannot_be_reopened_by_seed(srv):
    srv.transport.close()

    with pytest.raises(RuntimeError, match="closed"):
        srv.transport.seed(["abcd1234"], [])
    srv.transport.disable("must not replace the terminal refusal")

    assert srv.transport.refusal == "lifecycle transport is closed"
    assert (
        srv.broadcast({"type": "lode_created", "lode": _lode("abcd1234", "A")}, root=ACTIVE_ROOT)
        is False
    )
    assert srv.transport.pending_ids() == []


@pytest.mark.parametrize("transition", ["close", "disable", "reseed"])
def test_publish_linearizes_before_concurrent_transport_transition(srv, monkeypatch, transition):
    freeze_entered = threading.Event()
    release_freeze = threading.Event()
    transition_entered = threading.Event()
    transition_done = threading.Event()
    real_freeze = transport_module.freeze_lode
    broadcast_outcome = []

    def paused_freeze(lode):
        freeze_entered.set()
        assert release_freeze.wait(5)
        return real_freeze(lode)

    monkeypatch.setattr(transport_module, "freeze_lode", paused_freeze)
    publisher = threading.Thread(
        target=lambda: broadcast_outcome.append(
            srv.broadcast(
                {"type": "lode_created", "lode": _lode("abcd1234", "A")},
                root=ACTIVE_ROOT,
            )
        ),
        daemon=True,
    )
    publisher.start()
    assert freeze_entered.wait(5), "publish never reached the in-lock freeze coordinate"

    if transition == "close":
        action = srv.transport.close
    elif transition == "disable":

        def action():
            srv.transport.disable("test refusal")
    else:

        def action():
            srv.transport.seed(["efgh5678"], [])

    def run_transition():
        transition_entered.set()
        action()
        transition_done.set()

    transition_thread = threading.Thread(target=run_transition, daemon=True)
    transition_thread.start()
    assert transition_entered.wait(5), "transition thread never reached the lock attempt"
    assert not transition_done.wait(0.05), "transition bypassed the ownership linearization lock"

    release_freeze.set()
    publisher.join(timeout=5)
    transition_thread.join(timeout=5)
    assert not publisher.is_alive()
    assert not transition_thread.is_alive()
    assert transition_done.is_set()
    assert broadcast_outcome == [True]
    assert srv.transport.pending_ids() == []
    assert srv.transport.claims() == []


def test_stop_racing_a_reload_cannot_reopen_the_transport(srv, monkeypatch):
    load_entered = threading.Event()
    release_load = threading.Event()
    real_load = server_module.load_lodes

    def paused_load():
        load_entered.set()
        assert release_load.wait(5)
        return real_load()

    monkeypatch.setattr(server_module, "load_lodes", paused_load)
    reload_thread = threading.Thread(
        target=lambda: srv._handle_mutation({"type": "projects_reload"}, None), daemon=True
    )
    reload_thread.start()
    assert load_entered.wait(5), "reload never reached its disk-read barrier"

    srv.stop()
    release_load.set()
    reload_thread.join(timeout=5)
    assert not reload_thread.is_alive()
    assert (
        srv.broadcast({"type": "lode_created", "lode": _lode("abcd1234", "A")}, root=ACTIVE_ROOT)
        is False
    )
    assert srv.transport.refusal == "lifecycle transport is closed"
    assert srv.transport.pending_ids() == []


def test_restart_discards_pending_state_and_seeds_from_the_reloaded_roots(socket_path):
    save_lodes([_lode("abcd1234", "A")])
    save_archived_lodes([_lode("efgh5678", "A")])

    stale = Server(socket_path)
    stale.lodes = [_lode("abcd1234", "A")]
    stale.reseed_lifecycle_baseline()
    stale.broadcast({"type": "lode_updated", "lode": _lode("abcd1234", "STALE")}, root=ACTIVE_ROOT)
    assert stale.transport.pending_ids() == ["abcd1234"]

    restarted = Server(socket_path)
    thread = threading.Thread(target=restarted.start, daemon=True)
    thread.start()
    assert restarted.ready.wait(5)
    try:
        assert restarted.transport.pending_ids() == []
        assert restarted.transport.baseline_root("abcd1234") == ACTIVE_ROOT
        assert restarted.transport.baseline_root("efgh5678") == ARCHIVE_ROOT
    finally:
        restarted.stop()
        thread.join(timeout=5)


# --- AC5: the table oracle ------------------------------------------------------------


ORACLE = [
    (
        "existing-active update/update/archive",
        ACTIVE_ROOT,
        [("lode_updated", ACTIVE_ROOT, "A"), ("lode_updated", ACTIVE_ROOT, "B")],
        ("lode_archived", ARCHIVE_ROOT, "C"),
        [("lode_archived", "C")],
    ),
    (
        "existing-active archive/unarchive/update",
        ACTIVE_ROOT,
        [("lode_archived", ARCHIVE_ROOT, "A"), ("lode_unarchived", ACTIVE_ROOT, "B")],
        ("lode_updated", ACTIVE_ROOT, "C"),
        [("lode_updated", "C")],
    ),
    (
        "absent create/update/archive",
        None,
        [("lode_created", ACTIVE_ROOT, "A"), ("lode_updated", ACTIVE_ROOT, "B")],
        ("lode_archived", ARCHIVE_ROOT, "C"),
        [("lode_created", "B"), ("lode_archived", "C")],
    ),
    (
        "existing-active same membership throughout",
        ACTIVE_ROOT,
        [("lode_updated", ACTIVE_ROOT, "A"), ("lode_updated", ACTIVE_ROOT, "B")],
        ("lode_updated", ACTIVE_ROOT, "C"),
        [("lode_updated", "C")],
    ),
    (
        "existing-archived same membership throughout",
        ARCHIVE_ROOT,
        [("lode_updated", ARCHIVE_ROOT, "A"), ("lode_updated", ARCHIVE_ROOT, "B")],
        ("lode_updated", ARCHIVE_ROOT, "C"),
        [("lode_updated", "C")],
    ),
    (
        "archived baseline cycles archived/active/archived",
        ARCHIVE_ROOT,
        [("lode_unarchived", ACTIVE_ROOT, "A"), ("lode_updated", ACTIVE_ROOT, "B")],
        ("lode_archived", ARCHIVE_ROOT, "C"),
        [("lode_archived", "C")],
    ),
]


@pytest.mark.parametrize(
    "name,baseline,leading,final,expected",
    ORACLE,
    ids=[row[0] for row in ORACLE],
)
def test_reducer_matches_the_oracle(srv, name, baseline, leading, final, expected):
    lode_id = "abcd1234"
    if baseline == ACTIVE_ROOT:
        srv.lodes = [_lode(lode_id, "SEED")]
    elif baseline == ARCHIVE_ROOT:
        srv.archived_lodes = [_lode(lode_id, "SEED")]
    srv.reseed_lifecycle_baseline()

    for declared, root, marker in [*leading, final]:
        assert srv.broadcast({"type": declared, "lode": _lode(lode_id, marker)}, root=root)

    events = _drain(srv)
    assert events, name
    assert [(event["type"], sorted(_markers(event))[0]) for event in events] == expected, name

    # No superseded snapshot survives its successor.
    final_marker = final[2]
    assert _markers(events[-1]) == {final_marker}
    for event in events:
        assert len(_markers(event)) == 1
    expected_markers = {marker for _event_type, marker in expected}
    published_markers = {marker for _declared, _root, marker in [*leading, final]}
    serialized_events = json.dumps(events)
    for marker in published_markers - expected_markers:
        assert marker not in serialized_events
    assert "SEED" not in json.dumps(events)


# --- AC4 / AC8: bounded pending state -------------------------------------------------


def test_pending_state_is_bounded_by_lode_identity_not_mutation_volume(srv):
    srv.lodes = [_lode("abcd1234", "A")]
    srv.reseed_lifecycle_baseline()

    for index in range(500):
        assert srv.broadcast(
            {"type": "lode_updated", "lode": _lode("abcd1234", f"M{index}")}, root=ACTIVE_ROOT
        )

    assert srv.transport.pending_ids() == ["abcd1234"]
    assert srv.transport.claims() == ["abcd1234"]

    events = _drain(srv)
    assert [(event["type"], sorted(_markers(event))[0]) for event in events] == [
        ("lode_updated", "M499")
    ]
    assert srv.transport.pending_ids() == []
    assert srv.transport.claims() == []


def test_absent_to_archived_keeps_exactly_two_snapshots_and_drains_clean(srv):
    for declared, root, marker in (
        ("lode_created", ACTIVE_ROOT, "A"),
        ("lode_updated", ACTIVE_ROOT, "B"),
        ("lode_archived", ARCHIVE_ROOT, "C"),
    ):
        srv.broadcast({"type": declared, "lode": _lode("abcd1234", marker)}, root=root)

    assert srv.transport.pending_ids() == ["abcd1234"]
    assert srv.transport.claims() == ["abcd1234"]

    events = _drain(srv)
    assert [(event["type"], sorted(_markers(event))[0]) for event in events] == [
        ("lode_created", "B"),
        ("lode_archived", "C"),
    ]
    assert srv.transport.pending_ids() == []
    assert srv.transport.claims() == []


def test_repeated_fill_and_drain_cycles_accumulate_nothing(srv):
    srv.lodes = [_lode("abcd1234", "A")]
    srv.reseed_lifecycle_baseline()

    for cycle in range(5):
        for index in range(20):
            srv.broadcast(
                {"type": "lode_updated", "lode": _lode("abcd1234", f"c{cycle}x{index}")},
                root=ACTIVE_ROOT,
            )
        assert len(_drain(srv)) == 1
        assert srv.transport.pending_ids() == []
        assert srv.transport.claims() == []


def test_change_after_selection_leaves_the_selected_bytes_exact_and_rejoins_the_tail(srv):
    srv.lodes = [_lode("first111", "A"), _lode("second22", "A"), _lode("third333", "A")]
    srv.reseed_lifecycle_baseline()

    srv.broadcast({"type": "lode_updated", "lode": _lode("first111", "B")}, root=ACTIVE_ROOT)
    srv.broadcast({"type": "lode_updated", "lode": _lode("second22", "B")}, root=ACTIVE_ROOT)
    srv.broadcast({"type": "lode_updated", "lode": _lode("third333", "B")}, root=ACTIVE_ROOT)

    selection = srv.transport.select(lambda: False, 0)
    selected = json.loads(selection.data.decode("utf-8"))
    assert selected["lode"]["id"] == "first111"

    # The selected lode keeps changing while its envelope is in flight.
    srv.broadcast({"type": "lode_updated", "lode": _lode("first111", "C")}, root=ACTIVE_ROOT)
    srv.broadcast({"type": "lode_updated", "lode": _lode("first111", "D")}, root=ACTIVE_ROOT)

    assert json.loads(selection.data.decode("utf-8")) == selected
    assert _markers(selected) == {"B"}
    assert srv.transport.claims() == ["second22", "third333", "first111"]

    rest = _drain(srv)
    assert [(event["lode"]["id"], sorted(_markers(event))[0]) for event in rest] == [
        ("second22", "B"),
        ("third333", "B"),
        ("first111", "D"),
    ]


# --- AC6: bilateral fairness and per-lode round robin ---------------------------------


def test_selection_alternates_classes_while_both_stay_nonempty(srv):
    srv.lodes = [_lode(f"lode{index:04d}", "A") for index in range(6)]
    srv.reseed_lifecycle_baseline()

    for index in range(6):
        srv.broadcast(
            {"type": "lode_updated", "lode": _lode(f"lode{index:04d}", "B")}, root=ACTIVE_ROOT
        )
        srv.broadcast({"type": "backlog_updated", "item": {"id": f"item{index}"}})

    kinds = []
    while True:
        step = _step(srv)
        if step is None:
            break
        kinds.append(step[0])

    assert len(kinds) == 12
    assert kinds == ["lifecycle", "ordinary"] * 6


def test_a_lode_with_n_claims_ahead_is_chosen_as_selection_n_plus_one(srv):
    ids = [f"lode{index:04d}" for index in range(5)]
    srv.lodes = [_lode(lode_id, "A") for lode_id in ids]
    srv.reseed_lifecycle_baseline()

    for lode_id in ids:
        srv.broadcast({"type": "lode_updated", "lode": _lode(lode_id, "B")}, root=ACTIVE_ROOT)

    assert srv.transport.claims() == ids
    assert [event["lode"]["id"] for event in _drain(srv)] == ids


def test_ordinary_traffic_is_never_starved_by_a_continuously_changing_lode(srv):
    srv.lodes = [_lode("abcd1234", "A")]
    srv.reseed_lifecycle_baseline()
    srv.broadcast({"type": "backlog_added", "item": {"id": "b1"}})

    kinds = []
    for index in range(6):
        srv.broadcast(
            {"type": "lode_updated", "lode": _lode("abcd1234", f"M{index}")}, root=ACTIVE_ROOT
        )
        srv.broadcast({"type": "backlog_updated", "item": {"id": f"i{index}"}})
        step = _step(srv)
        kinds.append(step[0])

    assert "ordinary" in kinds
    assert "lifecycle" in kinds


# --- AC7: fixed cohort ----------------------------------------------------------------


def test_cohort_is_fixed_at_ownership_and_a_late_socket_receives_neither_envelope(srv):
    early_server_end, early_client = _register_socket(srv)
    try:
        for declared, root, marker in (
            ("lode_created", ACTIVE_ROOT, "A"),
            ("lode_updated", ACTIVE_ROOT, "B"),
        ):
            srv.broadcast({"type": declared, "lode": _lode("abcd1234", marker)}, root=root)

        # A socket that joins after ownership is outside this sequence's cohort.
        late_server_end, late_client = _register_socket(srv)
        try:
            srv.broadcast(
                {"type": "lode_archived", "lode": _lode("abcd1234", "C")}, root=ARCHIVE_ROOT
            )

            first = srv.transport.select(lambda: False, 0)
            srv._send_to_cohort(first.data, first.cohort)
            second = srv.transport.select(lambda: False, 0)
            srv._send_to_cohort(second.data, second.cohort)

            early = _recv_events(early_client, 2)
            assert [(event["type"], sorted(_markers(event))[0]) for event in early] == [
                ("lode_created", "B"),
                ("lode_archived", "C"),
            ]
            assert _recv_events(late_client, 1, timeout=0.5) == []
        finally:
            late_server_end.close()
            late_client.close()
    finally:
        early_server_end.close()
        early_client.close()


def test_cohort_captures_a_socket_that_connects_before_pending_ownership(srv):
    real_publish = srv.transport.publish
    publish_entered = threading.Event()
    release_publish = threading.Event()
    outcome = []

    def paused_publish(*args, **kwargs):
        publish_entered.set()
        release_publish.wait(5)
        return real_publish(*args, **kwargs)

    srv.transport.publish = paused_publish

    def broadcast_lifecycle():
        outcome.append(
            srv.broadcast(
                {"type": "lode_created", "lode": _lode("abcd1234", "A")}, root=ACTIVE_ROOT
            )
        )

    thread = threading.Thread(target=broadcast_lifecycle, daemon=True)
    thread.start()
    server_end = client_end = None
    try:
        assert publish_entered.wait(5), "lifecycle publish did not reach the ownership barrier"
        server_end, client_end = _register_socket(srv)
        release_publish.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert outcome == [True]

        selection = srv.transport.select(lambda: False, 0)
        assert server_end in selection.cohort
        srv._send_to_cohort(selection.data, selection.cohort)

        (event,) = _recv_events(client_end, 1)
        assert event["type"] == "lode_created"
        assert _markers(event) == {"A"}
    finally:
        release_publish.set()
        thread.join(timeout=5)
        if server_end is not None:
            with srv.lock:
                if server_end in srv.clients:
                    srv.clients.remove(server_end)
                srv.write_locks.pop(server_end, None)
            server_end.close()
        if client_end is not None:
            client_end.close()


def test_a_departed_cohort_member_is_attempted_once_without_blocking_the_others(srv):
    healthy_end, healthy_client = _register_socket(srv)
    departed_end, departed_client = _register_socket(srv)
    try:
        srv.broadcast({"type": "lode_created", "lode": _lode("abcd1234", "A")}, root=ACTIVE_ROOT)

        # Forget the departed member the way a disconnect does, after the cohort was captured.
        with srv.lock:
            srv.clients.remove(departed_end)
            srv.write_locks.pop(departed_end)

        selection = srv.transport.select(lambda: False, 0)
        assert departed_end in selection.cohort
        srv._send_to_cohort(selection.data, selection.cohort)

        (event,) = _recv_events(healthy_client, 1)
        assert event["type"] == "lode_created"
        with srv.lock:
            assert srv.clients == [healthy_end]
    finally:
        for handle in (healthy_end, healthy_client, departed_end, departed_client):
            handle.close()


def test_an_empty_cohort_still_advances_the_sequence(srv):
    srv.broadcast({"type": "lode_created", "lode": _lode("abcd1234", "A")}, root=ACTIVE_ROOT)
    selection = srv.transport.select(lambda: False, 0)
    assert selection.cohort == ()
    srv._send_to_cohort(selection.data, selection.cohort)

    assert srv.transport.pending_ids() == []
    assert srv.transport.baseline_root("abcd1234") == ACTIVE_ROOT


# --- AC9: the real writer thread over real sockets ------------------------------------


def test_real_writer_delivers_every_lifecycle_type_over_real_sockets(socket_path):
    save_lodes([_lode("abcd1234", "SEED"), _lode("efgh5678", "SEED")])
    server = Server(socket_path)
    writer_entered = threading.Event()
    release_writer = threading.Event()
    real_select = server.transport.select

    def paused_select(*args, **kwargs):
        writer_entered.set()
        assert release_writer.wait(5)
        return real_select(*args, **kwargs)

    server.transport.select = paused_select
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    assert server.ready.wait(5)
    assert writer_entered.wait(5), "production writer never reached the selector barrier"

    healthy = [_register_socket(server) for _ in range(2)]
    failing_end, failing_peer = socket.socketpair()
    failing_peer.close()
    failing_end.close()
    with server.lock:
        server.clients.append(failing_end)
        server.write_locks[failing_end] = threading.Lock()

    try:
        for index in range(server.broadcast_queue.maxsize):
            assert server.broadcast({"type": "backlog_updated", "item": {"id": f"i{index}"}})
        assert server.broadcast_queue.full(), "ordinary FIFO was not filled to production capacity"

        assert server.broadcast(
            {"type": "lode_updated", "lode": _lode("abcd1234", "U1")}, root=ACTIVE_ROOT
        )
        assert server.broadcast(
            {"type": "lode_archived", "lode": _lode("abcd1234", "AR")}, root=ARCHIVE_ROOT
        )
        assert server.broadcast(
            {"type": "lode_unarchived", "lode": _lode("abcd1234", "UN")}, root=ACTIVE_ROOT
        )
        assert server.broadcast(
            {"type": "lode_created", "lode": _lode("ijkl9012", "CR")}, root=ACTIVE_ROOT
        )
        release_writer.set()

        def saw_last_publish(events):
            return any(
                event["type"] == "lode_created" and event["lode"]["id"] == "ijkl9012"
                for event in events
            )

        def through_last_publish(client_end):
            """Everything up to the final publish; trailing frames depend only on read timing."""
            events = _recv_until(client_end, saw_last_publish)
            assert saw_last_publish(events), "the final publish never arrived"
            last = max(
                index
                for index, event in enumerate(events)
                if event["type"] == "lode_created" and event["lode"]["id"] == "ijkl9012"
            )
            return events[: last + 1]

        streams = [through_last_publish(client_end) for _server_end, client_end in healthy]
        assert streams[0] == streams[1], "cohort members received different bytes"

        for events in streams:
            lifecycle = [event for event in events if event["type"] in LIFECYCLE_TYPES]
            ordinary = [event for event in events if event["type"] == "backlog_updated"]

            assert ordinary, "ordinary traffic did not progress"
            assert (
                "lode_created",
                "ijkl9012",
                "CR",
            ) == (
                lifecycle[-1]["type"],
                lifecycle[-1]["lode"]["id"],
                sorted(_markers(lifecycle[-1]))[0],
            )
            cycled = [event for event in lifecycle if event["lode"]["id"] == "abcd1234"]
            assert cycled, "the cycled lode was never delivered"
            assert _markers(cycled[-1]) == {"UN"}
            # The consumer's membership view stays coherent under any coalescing the live
            # writer chose: it is told "unarchived" exactly when it was told "archived".
            told_archived = any(event["type"] == "lode_archived" for event in cycled)
            assert cycled[-1]["type"] == ("lode_unarchived" if told_archived else "lode_updated")
            # No snapshot outlives its successor.
            for event in cycled[:-1]:
                assert _markers(event) != {"UN"}
            assert "SEED" not in json.dumps(lifecycle)

        deadline = time.time() + 3.0
        while time.time() < deadline:
            with server.lock:
                if failing_end not in server.clients:
                    break
            time.sleep(0.05)
        with server.lock:
            assert failing_end not in server.clients
            assert failing_end not in server.write_locks

        # An all-failed cohort must not kill the writer or discard what follows.
        for server_end, client_end in healthy:
            with server.lock:
                server.clients.remove(server_end)
                server.write_locks.pop(server_end)
            server_end.close()
            client_end.close()
        assert server.broadcast(
            {"type": "lode_updated", "lode": _lode("efgh5678", "ORPHAN")}, root=ACTIVE_ROOT
        )

        survivor_end, survivor_client = _register_socket(server)
        try:
            assert server.broadcast(
                {"type": "lode_updated", "lode": _lode("abcd1234", "AFTER")}, root=ACTIVE_ROOT
            )
            assert server.broadcast({"type": "backlog_added", "item": {"id": "last"}})

            def saw_both(events):
                seen = {
                    (event["type"], event.get("lode", event.get("item", {}))["id"])
                    for event in events
                }
                return {("lode_updated", "abcd1234"), ("backlog_added", "last")} <= seen

            events = _recv_until(survivor_client, saw_both)
            assert saw_both(events), "the writer stopped after an all-failed cohort"
            resumed = [event for event in events if event["type"] == "lode_updated"]
            assert _markers(resumed[-1]) == {"AFTER"}
            assert server.writer_thread.is_alive()
        finally:
            survivor_end.close()
            survivor_client.close()
    finally:
        release_writer.set()
        server.stop()
        thread.join(timeout=5)


def test_wire_frame_matches_what_the_wait_and_cli_consumers_parse(socket_path):
    save_lodes([_lode("abcd1234", "SEED")])
    server = Server(socket_path)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    assert server.ready.wait(5)
    server_end, client_end = _register_socket(server)
    try:
        updated = _lode("abcd1234", "U1", state="running", status="Working")
        archived = _lode("abcd1234", "AR", state="ready", status="Archived")
        assert server.broadcast({"type": "lode_updated", "lode": updated}, root=ACTIVE_ROOT)
        events = _recv_events(client_end, 1, timeout=5.0)
        assert server.broadcast({"type": "lode_archived", "lode": archived}, root=ARCHIVE_ROOT)
        events += _recv_events(client_end, 1, timeout=5.0)

        assert [event["type"] for event in events] == ["lode_updated", "lode_archived"]
        for event, expected in zip(events, (updated, archived)):
            assert event["lode"] == expected
            assert event["lode"]["id"] == "abcd1234"
            assert "exchange_id" not in event
            assert isinstance(event["ts"], int)
            assert set(event) == {"type", "lode", "ts"}
    finally:
        server.stop()
        thread.join(timeout=5)
        server_end.close()
        client_end.close()


def test_stop_discards_pending_transport_state_and_releases_the_writer(socket_path):
    server = Server(socket_path)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    assert server.ready.wait(5)

    server.broadcast({"type": "lode_created", "lode": _lode("abcd1234", "A")}, root=ACTIVE_ROOT)
    server.stop()
    thread.join(timeout=5)

    assert server.transport.pending_ids() == []
    assert server.transport.claims() == []
    assert not server.writer_thread.is_alive()
