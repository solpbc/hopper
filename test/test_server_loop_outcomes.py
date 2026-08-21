# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Causal first-loop observations without changing the public server lifecycle."""

import logging
import os
import queue
import signal
import threading
import time
from unittest.mock import patch

import pytest

from hopper.server import Server, start_server_with_tui
from hopper.transport import ACTIVE_ROOT, LIFECYCLE, ORDINARY


@pytest.fixture
def socket_path(tmp_path):
    return tmp_path / "test.sock"


class BarrierQueue(queue.Queue):
    """Real bounded queue whose get can be paused immediately before Queue.get."""

    def __init__(self, maxsize=0):
        super().__init__(maxsize=maxsize)
        self.entered = threading.Event()
        self.release = threading.Event()

    def get(self, block=True, timeout=None):
        self.entered.set()
        assert self.release.wait(5), "queue barrier was not released"
        return super().get(block=block, timeout=timeout)


class RaisingQueue(queue.Queue):
    def __init__(self, error):
        super().__init__()
        self.error = error

    def get(self, block=True, timeout=None):
        raise self.error


class NoExpiryCondition(threading.Condition):
    """Condition seam that can be released only by a real notification."""

    def __init__(self):
        super().__init__()
        self.wait_entered = threading.Event()

    def wait_for(self, predicate, timeout=None):
        self.wait_entered.set()
        while not predicate():
            self.wait()
        return True


def _start_target(target):
    errors = []

    def run():
        try:
            target()
        except BaseException as error:  # production targets preserve BaseException identity
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors


def _release_test_lock(server):
    if server._lock_file is not None:
        server._lock_file.close()
        server._lock_file = None


def _lifecycle_message(lode_id="observed"):
    return {
        "type": "lode_created",
        "lode": {
            "id": lode_id,
            "project": "project-a",
            "stage": "mill",
            "state": "new",
            "status": "",
            "active": True,
        },
    }


def _publish_lifecycle(server):
    server.transport.seed([], [])
    refusal = server.transport.publish(_lifecycle_message(), ACTIVE_ROOT, 1, lambda: ("cohort",))
    assert refusal is None


def _traceback_names(error):
    names = []
    traceback = error.__traceback__
    while traceback is not None:
        names.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    return names


def test_loop_outcome_snapshot_is_frozen_and_first_transition_wins(socket_path):
    server = Server(socket_path)
    pending = server._worker_loop_outcome_snapshot()

    assert pending.writer is None
    assert pending.event is None
    assert server._record_worker_loop_outcome("writer", "select_none") is True
    assert server._record_worker_loop_outcome("writer", "select_non_none") is False
    assert pending.writer is None
    assert pending.event is None
    assert server._worker_loop_outcome_snapshot().writer == "select_none"
    assert server._worker_loop_outcome_snapshot().event is None


def test_loop_outcome_simultaneous_transitions_have_one_winner(socket_path):
    server = Server(socket_path)
    barrier = threading.Barrier(3)
    outcomes = ["get_empty", "get_item"]
    results = {}

    def record(outcome):
        barrier.wait()
        results[outcome] = server._record_worker_loop_outcome("event", outcome)

    threads = [threading.Thread(target=record, args=(outcome,)) for outcome in outcomes]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert sum(results.values()) == 1
    winner = next(outcome for outcome, result in results.items() if result)
    assert server._worker_loop_outcome_snapshot().event == winner
    assert server._worker_loop_outcome_snapshot().writer is None


@pytest.mark.parametrize(
    ("worker", "outcome"),
    [
        ("missing", "select_none"),
        ("writer", "missing"),
        ("event", "missing"),
        ("writer", "get_empty"),
        ("event", "select_none"),
    ],
)
def test_loop_outcome_rejects_unknown_and_cross_vocabulary(socket_path, worker, outcome):
    server = Server(socket_path)

    with pytest.raises(ValueError):
        server._record_worker_loop_outcome(worker, outcome)

    assert server._worker_loop_outcome_snapshot() == (None, None)


def test_event_observation_waits_for_initial_drain(socket_path):
    server = Server(socket_path)
    drain_entered = threading.Event()
    drain_release = threading.Event()
    message = {"type": "probe"}
    server.event_queue.put_nowait((message, None))

    def drain():
        drain_entered.set()
        assert drain_release.wait(5)

    def handle(observed, conn):
        assert observed is message
        assert conn is None
        server.stop_event.set()

    with (
        patch.object(server, "_drain_due_disconnects", side_effect=drain),
        patch.object(server, "_handle_mutation", side_effect=handle) as mutation,
    ):
        thread, errors = _start_target(server._event_loop)
        assert drain_entered.wait(2)
        assert server._worker_loop_outcome_snapshot().event is None
        drain_release.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    assert server._worker_loop_outcome_snapshot().event == "get_item"
    mutation.assert_called_once_with(message, None)


def test_event_observation_waits_for_real_queue_get(socket_path):
    server = Server(socket_path)
    event_queue = BarrierQueue()
    message = {"type": "probe"}
    event_queue.put_nowait((message, None))
    server.event_queue = event_queue

    def handle(observed, conn):
        assert observed is message
        assert conn is None
        server.stop_event.set()

    with patch.object(server, "_handle_mutation", side_effect=handle) as mutation:
        thread, errors = _start_target(server._event_loop)
        assert event_queue.entered.wait(2)
        assert server._worker_loop_outcome_snapshot().event is None
        event_queue.release.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    assert server._worker_loop_outcome_snapshot().event == "get_item"
    mutation.assert_called_once_with(message, None)


def test_event_real_empty_records_get_empty(socket_path):
    server = Server(socket_path)
    thread, errors = _start_target(server._event_loop)

    deadline = time.monotonic() + 2
    while server._worker_loop_outcome_snapshot().event is None and time.monotonic() < deadline:
        time.sleep(0.005)
    server.stop_event.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    assert server._worker_loop_outcome_snapshot().event == "get_empty"


def test_event_initial_drain_exception_is_observed_and_bare_reraised(socket_path):
    server = Server(socket_path)
    error = RuntimeError("drain exploded")

    def raise_drain():
        raise error

    with patch.object(server, "_drain_due_disconnects", side_effect=raise_drain):
        with pytest.raises(RuntimeError) as caught:
            server._event_loop()

    assert caught.value is error
    assert "raise_drain" in _traceback_names(caught.value)
    assert server._worker_loop_outcome_snapshot() == (None, "drain_raised")


def test_event_first_get_exception_is_observed_and_bare_reraised(socket_path):
    server = Server(socket_path)
    error = OSError("get exploded")
    server.event_queue = RaisingQueue(error)

    with pytest.raises(OSError) as caught:
        server._event_loop()

    assert caught.value is error
    assert "get" in _traceback_names(caught.value)
    assert server._worker_loop_outcome_snapshot() == (None, "get_raised")


def test_event_pre_set_stop_records_return_before_operation(socket_path):
    server = Server(socket_path)
    server.stop_event.set()

    with (
        patch.object(server, "_drain_due_disconnects") as drain,
        patch.object(server.event_queue, "get") as get,
    ):
        server._event_loop()

    drain.assert_not_called()
    get.assert_not_called()
    assert server._worker_loop_outcome_snapshot().event == "target_returned_before_operation"


@pytest.mark.parametrize("with_item", [False, True])
def test_event_raw_stop_during_get_records_literal_completion(socket_path, with_item):
    server = Server(socket_path)
    event_queue = BarrierQueue()
    message = {"type": "probe"}
    if with_item:
        event_queue.put_nowait((message, None))
    server.event_queue = event_queue

    with patch.object(server, "_handle_mutation") as mutation:
        thread, errors = _start_target(server._event_loop)
        assert event_queue.entered.wait(2)
        server.stop_event.set()
        event_queue.release.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    expected = "get_item" if with_item else "get_empty"
    assert server._worker_loop_outcome_snapshot().event == expected
    if with_item:
        mutation.assert_called_once_with(message, None)
    else:
        mutation.assert_not_called()


def test_event_later_exception_does_not_rewrite_get_item(socket_path):
    server = Server(socket_path)
    message = {"type": "probe"}
    server.event_queue.put_nowait((message, None))
    error = RuntimeError("later drain exploded")
    drain_calls = 0

    def drain():
        nonlocal drain_calls
        drain_calls += 1
        if drain_calls == 2:
            raise error

    with (
        patch.object(server, "_drain_due_disconnects", side_effect=drain),
        patch.object(server, "_handle_mutation") as mutation,
    ):
        with pytest.raises(RuntimeError) as caught:
            server._event_loop()

    assert caught.value is error
    mutation.assert_called_once_with(message, None)
    assert server._worker_loop_outcome_snapshot().event == "get_item"


def test_event_malformed_returned_item_is_still_get_item(socket_path):
    server = Server(socket_path)
    server.event_queue.put_nowait(("one-item",))

    with pytest.raises(ValueError):
        server._event_loop()

    assert server._worker_loop_outcome_snapshot().event == "get_item"


def test_writer_observation_waits_for_real_select(socket_path):
    server = Server(socket_path)
    select_entered = threading.Event()
    select_release = threading.Event()
    real_select = server.transport.select

    def select(*args, **kwargs):
        select_entered.set()
        assert select_release.wait(5)
        return real_select(*args, **kwargs)

    with patch.object(server.transport, "select", side_effect=select):
        thread, errors = _start_target(server._writer_loop)
        assert select_entered.wait(2)
        assert server._worker_loop_outcome_snapshot().writer is None
        server.stop_event.set()
        server.transport.close()
        select_release.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    assert server._worker_loop_outcome_snapshot().writer == "select_none"


def test_writer_real_no_selection_records_select_none(socket_path):
    server = Server(socket_path)
    thread, errors = _start_target(server._writer_loop)

    deadline = time.monotonic() + 2
    while server._worker_loop_outcome_snapshot().writer is None and time.monotonic() < deadline:
        time.sleep(0.005)
    server.stop_event.set()
    server.transport.close()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    assert server._worker_loop_outcome_snapshot().writer == "select_none"


@pytest.mark.parametrize("kind", [ORDINARY, LIFECYCLE])
def test_writer_non_none_selection_sends_downstream_once(socket_path, kind):
    server = Server(socket_path)
    message = {"type": "ordinary-probe"}
    if kind == ORDINARY:
        server.broadcast_queue.put_nowait(message)
    else:
        _publish_lifecycle(server)

    def sent(*args):
        server.stop_event.set()

    with (
        patch.object(server, "_send_to_clients", side_effect=sent) as ordinary_send,
        patch.object(server, "_send_to_cohort", side_effect=sent) as lifecycle_send,
    ):
        server._writer_loop()

    assert server._worker_loop_outcome_snapshot().writer == "select_non_none"
    if kind == ORDINARY:
        ordinary_send.assert_called_once_with(message)
        lifecycle_send.assert_not_called()
    else:
        ordinary_send.assert_not_called()
        lifecycle_send.assert_called_once()
        data, cohort = lifecycle_send.call_args.args
        assert b'"type": "lode_created"' in data
        assert cohort == ("cohort",)


def test_writer_first_select_exception_is_observed_and_bare_reraised(socket_path):
    server = Server(socket_path)
    error = RuntimeError("select exploded")

    def raise_select(*args, **kwargs):
        raise error

    with patch.object(server.transport, "select", side_effect=raise_select):
        with pytest.raises(RuntimeError) as caught:
            server._writer_loop()

    assert caught.value is error
    assert "raise_select" in _traceback_names(caught.value)
    assert server._worker_loop_outcome_snapshot() == ("select_raised", None)


def test_writer_pre_set_stop_records_return_before_operation(socket_path):
    server = Server(socket_path)
    server.stop_event.set()

    with patch.object(server.transport, "select") as select:
        server._writer_loop()

    select.assert_not_called()
    assert server._worker_loop_outcome_snapshot().writer == "target_returned_before_operation"


@pytest.mark.parametrize("kind", [ORDINARY, LIFECYCLE])
def test_writer_raw_stop_preserves_preowned_selection(socket_path, kind):
    server = Server(socket_path)
    select_entered = threading.Event()
    select_release = threading.Event()
    real_select = server.transport.select
    message = {"type": "ordinary-probe"}
    if kind == ORDINARY:
        server.broadcast_queue.put_nowait(message)
    else:
        _publish_lifecycle(server)

    def select(*args, **kwargs):
        select_entered.set()
        assert select_release.wait(5)
        return real_select(*args, **kwargs)

    with (
        patch.object(server.transport, "select", side_effect=select),
        patch.object(server, "_send_to_clients") as ordinary_send,
        patch.object(server, "_send_to_cohort") as lifecycle_send,
    ):
        thread, errors = _start_target(server._writer_loop)
        assert select_entered.wait(2)
        server.stop_event.set()
        select_release.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    assert server._worker_loop_outcome_snapshot().writer == "select_non_none"
    if kind == ORDINARY:
        ordinary_send.assert_called_once_with(message)
        lifecycle_send.assert_not_called()
    else:
        ordinary_send.assert_not_called()
        lifecycle_send.assert_called_once()


def test_writer_transport_close_not_timeout_releases_first_select(socket_path):
    server = Server(socket_path)
    condition = NoExpiryCondition()
    server.transport._cond = condition
    thread, errors = _start_target(server._writer_loop)
    assert condition.wait_entered.wait(2)

    server.stop_event.set()
    assert server._worker_loop_outcome_snapshot().writer is None
    assert thread.is_alive()
    server.transport.close()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    assert server._worker_loop_outcome_snapshot().writer == "select_none"


def test_writer_later_exception_does_not_rewrite_first_selection(socket_path):
    server = Server(socket_path)
    message = {"type": "ordinary-probe"}
    server.broadcast_queue.put_nowait(message)
    error = RuntimeError("send exploded")

    with patch.object(server, "_send_to_clients", side_effect=error):
        with pytest.raises(RuntimeError) as caught:
            server._writer_loop()

    assert caught.value is error
    assert server._worker_loop_outcome_snapshot().writer == "select_non_none"


def test_enqueue_event_returns_true_with_identical_members(socket_path):
    server = Server(socket_path)
    message = {"type": "probe"}
    conn = object()
    before = dict(message)

    assert server._enqueue_event(message, conn) is True
    queued_message, queued_conn = server.event_queue.get_nowait()
    assert queued_message is message
    assert queued_conn is conn
    assert message == before


def test_enqueue_event_full_then_same_objects_accepted(socket_path, caplog):
    server = Server(socket_path)
    server.event_queue = queue.Queue(maxsize=1)
    server.event_queue.put_nowait(({"type": "sentinel"}, None))
    message = {"type": "probe"}
    conn = object()

    with caplog.at_level(logging.WARNING, logger="hopper.server"):
        assert server._enqueue_event(message, conn) is False

    warnings = [
        record
        for record in caplog.records
        if record.name == "hopper.server" and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    server.event_queue.get_nowait()
    assert server._enqueue_event(message, conn) is True
    queued_message, queued_conn = server.event_queue.get_nowait()
    assert queued_message is message
    assert queued_conn is conn


def test_enqueue_event_pre_set_stop_refuses_without_put(socket_path):
    server = Server(socket_path)
    server.stop_event.set()
    message = {"type": "probe"}

    with patch.object(server.event_queue, "put_nowait") as put:
        assert server._enqueue_event(message, None) is False

    put.assert_not_called()
    assert message == {"type": "probe"}


@pytest.mark.parametrize("event_type", ["_action_step_result", "_registration_capture_result"])
def test_accepted_descriptor_event_keeps_fd_until_production_stop_drain(socket_path, event_type):
    server = Server(socket_path)
    read_fd, write_fd = os.pipe()
    message = {
        "type": event_type,
        "result": {"pidfd": read_fd, "pidfd_owned": True},
    }
    try:
        assert server._enqueue_event(message, None) is True
        os.fstat(read_fd)
        with patch("hopper.server.os.close", wraps=os.close) as close:
            server.stop()
        close.assert_called_once_with(read_fd)
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        os.close(write_fd)
        try:
            os.close(read_fd)
        except OSError:
            pass


@pytest.mark.parametrize("refusal", ["stopped", "full"])
def test_refused_descriptor_event_closes_fd_once(socket_path, refusal):
    server = Server(socket_path)
    if refusal == "stopped":
        server.stop_event.set()
    else:
        server.event_queue = queue.Queue(maxsize=1)
        server.event_queue.put_nowait(({"type": "sentinel"}, None))
    read_fd, write_fd = os.pipe()
    message = {
        "type": "_action_step_result",
        "result": {"pidfd": read_fd, "pidfd_owned": True},
    }
    try:
        with patch("hopper.server.os.close", wraps=os.close) as close:
            assert server._enqueue_event(message, None) is False
        close.assert_called_once_with(read_fd)
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        os.close(write_fd)
        try:
            os.close(read_fd)
        except OSError:
            pass


@pytest.mark.parametrize("disposition", ["accepted", "stopped", "full"])
def test_public_enqueue_never_leaks_private_boolean(socket_path, disposition):
    server = Server(socket_path)
    if disposition == "stopped":
        server.stop_event.set()
    elif disposition == "full":
        server.event_queue = queue.Queue(maxsize=1)
        server.event_queue.put_nowait(({"type": "sentinel"}, None))

    assert server.enqueue({"type": "probe"}) is None


def _install_first_operation_barriers(server):
    event_queue = BarrierQueue()
    server.event_queue = event_queue
    select_entered = threading.Event()
    select_release = threading.Event()
    real_select = server.transport.select

    def select(*args, **kwargs):
        select_entered.set()
        assert select_release.wait(5)
        return real_select(*args, **kwargs)

    select_patch = patch.object(server.transport, "select", side_effect=select)
    select_patch.start()
    return event_queue, select_entered, select_release, select_patch


def test_real_server_start_keeps_existing_early_ready(socket_path):
    server = Server(socket_path)
    event_queue, select_entered, select_release, select_patch = _install_first_operation_barriers(
        server
    )
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    try:
        assert server.ready.wait(2)
        assert event_queue.entered.wait(2)
        assert select_entered.wait(2)
        assert server._worker_loop_outcome_snapshot() == (None, None)
    finally:
        event_queue.release.set()
        select_release.set()
        server.stop()
        thread.join(timeout=2)
        select_patch.stop()
        _release_test_lock(server)

    assert not thread.is_alive()


def test_public_wrapper_keeps_existing_early_tui(socket_path):
    server = Server(socket_path)
    event_queue, select_entered, select_release, select_patch = _install_first_operation_barriers(
        server
    )
    tui_entered = threading.Event()
    release_tui = threading.Event()
    helper_errors = []

    def run_tui(observed_server):
        assert observed_server is server
        tui_entered.set()
        assert release_tui.wait(5)
        return 7

    def verify_and_release():
        try:
            assert tui_entered.wait(2)
            assert event_queue.entered.wait(2)
            assert select_entered.wait(2)
            assert server._worker_loop_outcome_snapshot() == (None, None)
        except BaseException as error:
            helper_errors.append(error)
        finally:
            event_queue.release.set()
            select_release.set()
            release_tui.set()

    helper = threading.Thread(target=verify_and_release, daemon=True)
    helper.start()
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)
    try:
        with (
            patch("hopper.server.Server", return_value=server),
            patch("hopper.tui.run_tui", side_effect=run_tui),
        ):
            assert start_server_with_tui(socket_path) == 7
    finally:
        event_queue.release.set()
        select_release.set()
        release_tui.set()
        helper.join(timeout=2)
        select_patch.stop()
        _release_test_lock(server)
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)

    assert helper_errors == []
    assert not helper.is_alive()
