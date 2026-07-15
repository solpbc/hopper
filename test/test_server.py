# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the hopper server."""

import fcntl
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import hopper.server as hopper_server
from hopper.backlog import BacklogItem
from hopper.client import read_lode_snapshot, send_message
from hopper.config import save_config
from hopper.lodes import save_archived_lodes, save_lodes
from hopper.projects import Project, touch_project
from hopper.server import (
    LISTEN_BACKLOG,
    PROGRESS_REJECT_STATES,
    Server,
    ServerLockHeld,
    SpawnOutcome,
    get_git_hash,
    start_server_with_tui,
)
from hopper.tmux import Liveness


class TestGetGitHash:
    def test_returns_short_hash(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "abc1234\n"
            result = get_git_hash()
            assert result == "abc1234"
            mock_run.assert_called_once_with(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
            )

    def test_returns_none_when_git_fails(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 128
            mock_run.return_value.stdout = ""
            result = get_git_hash()
            assert result is None

    def test_returns_none_when_git_not_installed(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = get_git_hash()
            assert result is None


def test_server_stores_git_hash():
    """Server captures git hash at initialization."""
    with patch("hopper.server.get_git_hash", return_value="abc1234"):
        srv = Server(socket_path="/tmp/unused.sock")
        assert srv.git_hash == "abc1234"


@pytest.fixture
def socket_path(tmp_path):
    """Provide a temporary socket path."""
    return tmp_path / "test.sock"


@pytest.fixture
def server(socket_path):
    """Start a server in a background thread."""
    srv = Server(socket_path)
    thread = threading.Thread(target=srv.start, daemon=True)
    thread.start()
    assert srv.ready.wait(5), "Server did not start"

    yield srv

    srv.stop()
    thread.join(timeout=2)


def _recv_messages_until(
    client: socket.socket, expected_types: set[str], timeout: float = 2.0
) -> list[dict]:
    """Receive broadcast messages until expected types are observed or timeout."""
    messages: list[dict] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        seen_types = {msg.get("type") for msg in messages}
        if expected_types.issubset(seen_types):
            break
        try:
            data = client.recv(4096).decode("utf-8")
        except socket.timeout:
            continue
        for line in data.strip().split("\n"):
            if line:
                messages.append(json.loads(line))
    return messages


def _decode_mock_response(conn: MagicMock) -> dict:
    """Decode the last JSON response sent through a mocked socket."""
    payload = conn.sendall.call_args.args[0].decode("utf-8").strip()
    return json.loads(payload)


def test_server_lode_snapshot_found_absent_and_ambiguous(socket_path, make_lode):
    active = make_lode(id="same-active", active=True)
    other_active = make_lode(id="active-only", active=True)
    archived = make_lode(id="same-archived", active=False)
    other_archived = make_lode(id="archived-only", active=False)
    server = Server(socket_path)
    server.lodes = [active, other_active]
    server.archived_lodes = [archived, other_archived]
    conn = MagicMock()

    with (
        patch.object(server, "_send_response") as send_response,
        patch.object(server, "broadcast") as broadcast,
        patch("hopper.server.save_lodes") as save_active,
    ):
        server._handle_mutation({"type": "lode_snapshot", "prefix": "active-o"}, conn)
        found = send_response.call_args.args[1]
        assert found == {"type": "lode_snapshot", "result": "found", "lode": other_active}
        assert found["lode"] is not other_active
        found["lode"]["status"] = "changed"
        assert other_active["status"] == ""

        send_response.reset_mock()
        server._handle_mutation({"type": "lode_snapshot", "prefix": "archived-o"}, conn)
        assert send_response.call_args.args[1]["lode"]["id"] == "archived-only"

        send_response.reset_mock()
        server._handle_mutation({"type": "lode_snapshot", "prefix": "missing"}, conn)
        assert send_response.call_args.args[1] == {"type": "lode_snapshot", "result": "absent"}

        send_response.reset_mock()
        server._handle_mutation({"type": "lode_snapshot", "prefix": "same-"}, conn)
        assert send_response.call_args.args[1] == {
            "type": "lode_snapshot",
            "result": "ambiguous",
            "matches": ["same-active", "same-archived"],
        }

    broadcast.assert_not_called()
    save_active.assert_not_called()
    assert server.lodes == [active, other_active]
    assert server.archived_lodes == [archived, other_archived]


@pytest.mark.parametrize("prefix", [None, 1, [], {}], ids=["missing", "integer", "list", "dict"])
def test_server_lode_snapshot_rejects_invalid_prefix(socket_path, prefix):
    server = Server(socket_path)
    conn = MagicMock()
    message = {"type": "lode_snapshot"}
    if prefix is not None:
        message["prefix"] = prefix

    server._handle_mutation(message, conn)

    assert _decode_mock_response(conn) == {
        "type": "error",
        "error": "lode_snapshot requires a string prefix",
        "ts": _decode_mock_response(conn)["ts"],
    }


def test_server_lode_snapshot_empty_prefix_is_valid(socket_path, make_lode):
    server = Server(socket_path)
    server.lodes = [make_lode(id="only-lode")]
    conn = MagicMock()

    server._handle_mutation({"type": "lode_snapshot", "prefix": ""}, conn)

    response = _decode_mock_response(conn)
    assert response["type"] == "lode_snapshot"
    assert response["result"] == "found"
    assert response["lode"]["id"] == "only-lode"


def _run_snapshot_burst(socket_path: Path, prefixes: list[str]) -> list[tuple[str, object]]:
    barrier = threading.Barrier(len(prefixes) + 1)
    results: list[tuple[str, object] | BaseException | None] = [None] * len(prefixes)

    def read_snapshot(index: int, prefix: str) -> None:
        try:
            barrier.wait(timeout=5)
            results[index] = read_lode_snapshot(socket_path, prefix, timeout=5)
        except BaseException as error:
            results[index] = error

    threads = [
        threading.Thread(target=read_snapshot, args=(index, prefix), daemon=True)
        for index, prefix in enumerate(prefixes)
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert not [result for result in results if isinstance(result, BaseException)]
    assert all(result is not None for result in results)
    return results


def test_lode_snapshot_handles_twelve_simultaneous_clients(socket_path, server, make_lode):
    active = [make_lode(id=f"active-{index:02d}", active=True) for index in range(6)]
    archived = [make_lode(id=f"archived-{index:02d}", active=False) for index in range(6)]
    server.lodes = active
    server.archived_lodes = archived
    targets = [lode["id"] for pair in zip(active, archived) for lode in pair]

    results = _run_snapshot_burst(socket_path, targets)

    assert len(results) == 12
    for target, result in zip(targets, results):
        assert result[0] == "found"
        assert result[1]["id"] == target


def test_server_uses_configured_listen_backlog(socket_path, monkeypatch):
    assert LISTEN_BACKLOG == 64
    assert LISTEN_BACKLOG >= 32
    real_socket = socket.socket
    listen_calls = []

    class SocketProxy:
        def __init__(self, *args, **kwargs):
            self._socket = real_socket(*args, **kwargs)

        def listen(self, backlog):
            listen_calls.append(backlog)
            return self._socket.listen(backlog)

        def __getattr__(self, name):
            return getattr(self._socket, name)

    monkeypatch.setattr(hopper_server.socket, "socket", SocketProxy)
    server = Server(socket_path)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    assert server.ready.wait(5), "Server did not start"

    try:
        assert listen_calls == [LISTEN_BACKLOG]
    finally:
        server.stop()
        thread.join(timeout=2)


def test_parallel_lode_snapshots_do_not_write_state(socket_path, server, temp_config, make_lode):
    active = [
        make_lode(id="active-only", active=True),
        make_lode(id="shared-active", active=True),
    ]
    archived = [
        make_lode(id="archived-only", active=False),
        make_lode(id="shared-archived", active=False),
    ]
    server.lodes = active
    server.archived_lodes = archived
    save_lodes(active)
    save_archived_lodes(archived)
    active_path = temp_config / "active.jsonl"
    archived_path = temp_config / "archived.jsonl"
    active_before = active_path.read_bytes()
    archived_before = archived_path.read_bytes()
    cases = [
        ("active-o", "found", "active-only"),
        ("archived-o", "found", "archived-only"),
        ("missing", "absent", None),
        ("shared-", "ambiguous", ["shared-active", "shared-archived"]),
    ] * 6

    with patch.object(server, "broadcast") as broadcast:
        results = _run_snapshot_burst(socket_path, [case[0] for case in cases])

    for (_, expected_result, expected_payload), result in zip(cases, results):
        assert result[0] == expected_result
        if expected_result == "found":
            assert result[1]["id"] == expected_payload
        else:
            assert result[1] == expected_payload
    assert active_path.read_bytes() == active_before
    assert archived_path.read_bytes() == archived_before
    broadcast.assert_not_called()


def test_lode_snapshot_serializes_with_archive_transition(
    socket_path, server, make_lode, monkeypatch
):
    lode = make_lode(id="transition-id", active=True)
    server.lodes = [lode]
    save_lodes(server.lodes)
    before = read_lode_snapshot(socket_path, "transition")
    assert before[0] == "found"
    assert before[1]["id"] == "transition-id"

    real_archive_lode = hopper_server.archive_lode
    mid_transition = threading.Event()
    release_transition = threading.Event()

    def blocking_archive_lode(lodes, lode_id):
        archived = real_archive_lode(lodes, lode_id)
        mid_transition.set()
        assert release_transition.wait(5)
        return archived

    monkeypatch.setattr(hopper_server, "archive_lode", blocking_archive_lode)
    send_message(socket_path, {"type": "lode_archive", "lode_id": "transition-id"})
    assert mid_transition.wait(5), "archive did not reach the transition pause"
    result = []

    def read_during_transition():
        result.append(read_lode_snapshot(socket_path, "transition", timeout=5))

    snapshot_thread = threading.Thread(target=read_during_transition, daemon=True)
    snapshot_thread.start()
    deadline = time.time() + 5
    snapshot_queued = False
    while time.time() < deadline:
        with server.event_queue.mutex:
            snapshot_queued = any(
                message.get("type") == "lode_snapshot"
                for message, _conn in server.event_queue.queue
            )
        if snapshot_queued:
            break
        time.sleep(0.01)

    try:
        assert snapshot_queued
        assert result == []
    finally:
        release_transition.set()

    snapshot_thread.join(timeout=10)
    assert not snapshot_thread.is_alive()
    assert result[0][0] == "found"
    assert result[0][1]["id"] == "transition-id"
    assert server.lodes == []
    assert [archived["id"] for archived in server.archived_lodes] == ["transition-id"]


def test_lode_create_disabled_project_noop_without_conn(socket_path):
    """lode_create refuses disabled projects before creating or broadcasting."""
    srv = Server(socket_path)
    disabled = Project(path="/fake/repo", name="P", disabled=True, disabled_reason="wip")

    with (
        patch("hopper.server.find_project", return_value=disabled),
        patch.object(srv, "broadcast") as mock_broadcast,
    ):
        srv._handle_mutation({"type": "lode_create", "project": "P", "scope": "scope"}, None)

    assert srv.lodes == []
    assert not any(
        call.args[0].get("type") == "lode_created" for call in mock_broadcast.call_args_list
    )


def test_server_creates_socket(socket_path):
    """Server creates socket file on start."""
    srv = Server(socket_path)
    thread = threading.Thread(target=srv.start, daemon=True)
    thread.start()
    assert srv.ready.wait(5), "Server did not start"

    assert socket_path.exists()

    srv.stop()
    thread.join(timeout=2)

    # Socket cleaned up on stop
    assert not socket_path.exists()


def test_second_server_refuses_without_disturbing_original(socket_path):
    """A lock loser leaves the original socket and ping identity unchanged."""
    winner = Server(socket_path)
    thread = threading.Thread(target=winner.start, daemon=True)
    thread.start()
    assert winner.ready.wait(5), "Winner did not start"

    try:
        socket_inode = socket_path.stat().st_ino
        first_ping = send_message(socket_path, {"type": "ping"}, wait_for_response=True)

        loser = Server(socket_path)
        with pytest.raises(ServerLockHeld, match="a live hopper server"):
            loser.start()

        second_ping = send_message(socket_path, {"type": "ping"}, wait_for_response=True)
        assert socket_path.stat().st_ino == socket_inode
        assert second_ping["pid"] == first_ping["pid"] == os.getpid()
        assert second_ping["started_at"] == first_ping["started_at"] == winner.started_at
        assert socket_path.with_suffix(".pid").read_text() == str(os.getpid())
    finally:
        winner.stop()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("pidfile_contents", "expected_pid"),
    [("1234", "1234"), ("", "unavailable"), ("not-a-pid", "unavailable")],
)
def test_lock_refusal_happens_before_any_startup_mutation(
    socket_path, temp_config, make_lode, pidfile_contents, expected_pid
):
    """A lock loser cannot load, mutate, clean, spawn, or unlink startup state."""
    lode = make_lode(id="locked", active=True, tmux_pane="%9", pid=999)
    save_lodes([lode])
    active_path = temp_config / "active.jsonl"
    active_before = active_path.read_bytes()
    socket_path.write_text("foreign socket sentinel")
    socket_before = socket_path.read_bytes()
    worktree = temp_config / "lodes" / "locked" / "worktree"
    worktree.mkdir(parents=True)

    pidfile = socket_path.with_suffix(".pid")
    pidfile.write_text(pidfile_contents)
    held = open(pidfile, "a+")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    server = Server(socket_path)

    try:
        with (
            patch("hopper.server.load_lodes") as mock_load_lodes,
            patch("hopper.server.load_archived_lodes") as mock_load_archived,
            patch("hopper.server.load_backlog") as mock_load_backlog,
            patch("hopper.server.get_active_projects") as mock_projects,
            patch("hopper.server.save_lodes") as mock_save,
            patch("hopper.server.remove_worktree") as mock_remove,
            patch("hopper.server.delete_branch") as mock_delete,
            patch("hopper.server.spawn_claude") as mock_spawn,
        ):
            with pytest.raises(ServerLockHeld) as exc_info:
                server.start()

        assert f"(pid {expected_pid})" in str(exc_info.value)
        mock_load_lodes.assert_not_called()
        mock_load_archived.assert_not_called()
        mock_load_backlog.assert_not_called()
        mock_projects.assert_not_called()
        mock_save.assert_not_called()
        mock_remove.assert_not_called()
        mock_delete.assert_not_called()
        mock_spawn.assert_not_called()
        assert active_path.read_bytes() == active_before
        assert socket_path.read_bytes() == socket_before
        assert worktree.is_dir()
        assert lode["active"] is True
        assert lode["tmux_pane"] == "%9"
        assert lode["pid"] == 999
    finally:
        held.close()


@pytest.mark.parametrize("old_pid", [999_999_999, os.getpid()])
def test_unlocked_stale_pidfile_does_not_block_start(socket_path, old_pid):
    """Pidfile contents are display-only when no process holds the lock."""
    socket_path.with_suffix(".pid").write_text(str(old_pid))
    server = Server(socket_path)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    assert server.ready.wait(5), "Server did not start"

    try:
        assert socket_path.with_suffix(".pid").read_text() == str(os.getpid())
    finally:
        server.stop()
        thread.join(timeout=2)


def test_stale_socket_without_listener_does_not_block_start(socket_path):
    """The lock holder replaces a stale socket path before binding."""
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()
    stale_inode = socket_path.stat().st_ino

    server = Server(socket_path)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    assert server.ready.wait(5), "Server did not start"

    try:
        assert socket_path.stat().st_ino != stale_inode
        assert send_message(socket_path, {"type": "ping"}, wait_for_response=True)["type"] == "pong"
    finally:
        server.stop()
        thread.join(timeout=2)


def test_server_that_never_bound_cannot_unlink_foreign_socket(socket_path):
    """Socket cleanup is gated by successful bind ownership."""
    socket_path.write_text("foreign")
    server = Server(socket_path)

    server.stop()

    assert socket_path.read_text() == "foreign"


def test_start_server_with_tui_reports_lock_refusal(socket_path, capsys):
    """A startup lock refusal exits before entering the TUI."""
    error = ServerLockHeld(
        "a live hopper server (pid 1234) holds the lock; "
        "attach to it or stop it before starting another"
    )
    with (
        patch.object(Server, "start", side_effect=error),
        patch("hopper.tui.run_tui") as mock_tui,
    ):
        assert start_server_with_tui(socket_path) == 1

    mock_tui.assert_not_called()
    assert str(error) in capsys.readouterr().out


def test_start_server_with_tui_reports_other_startup_error(socket_path, capsys):
    """A non-lock startup exception uses the generic failure path."""
    with (
        patch.object(Server, "start", side_effect=RuntimeError("bind exploded")),
        patch("hopper.tui.run_tui") as mock_tui,
    ):
        assert start_server_with_tui(socket_path) == 1

    mock_tui.assert_not_called()
    assert "Server failed to start: bind exploded" in capsys.readouterr().out


def test_two_process_server_start_race_has_one_stable_winner(tmp_path):
    """Two barrier-released processes yield one binder and one lock refusal."""
    child_code = r"""
import os
import socket
import sys
import threading

from hopper import config
from hopper.server import Server

host, port, label = sys.argv[1:]
control = socket.create_connection((host, int(port)), timeout=10)
control_file = control.makefile("rwb", buffering=0)
control_file.write(f"READY {label}\n".encode())
assert control_file.readline() == b"GO\n"
server = Server(config.server_socket_path())

def run_server():
    try:
        server.start()
    except Exception as error:
        server.startup_error = error
    finally:
        server.ready.set()

thread = threading.Thread(target=run_server, daemon=True)
thread.start()
assert server.ready.wait(5)
if server.startup_error is not None:
    message = str(server.startup_error)
    control_file.write(f"ERROR {label} {message}\n".encode())
    print(message)
    sys.exit(1)

control_file.write(
    f"BOUND {label} {os.getpid()} {server.started_at}\n".encode()
)
assert control_file.readline() == b"STOP\n"
server.stop()
thread.join(timeout=2)
"""
    xdg_home = tmp_path / "xdg"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    listener.settimeout(10)
    host, port = listener.getsockname()
    repo_root = str(Path(__file__).resolve().parents[1])
    env = os.environ.copy()
    env["XDG_DATA_HOME"] = str(xdg_home)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in [repo_root, env.get("PYTHONPATH", "")] if part
    )
    processes = {
        label: subprocess.Popen(
            [sys.executable, "-c", child_code, host, str(port), label],
            cwd=repo_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for label in ("a", "b")
    }
    controls = {}
    connections = []
    try:
        for _ in processes:
            connection, _ = listener.accept()
            connection.settimeout(10)
            connections.append(connection)
            control_file = connection.makefile("rwb", buffering=0)
            ready = control_file.readline().decode().strip().split()
            assert ready[0] == "READY"
            controls[ready[1]] = control_file

        for control_file in controls.values():
            control_file.write(b"GO\n")
        reports = {
            label: control_file.readline().decode().strip()
            for label, control_file in controls.items()
        }
        bound = [label for label, report in reports.items() if report.startswith("BOUND ")]
        refused = [label for label, report in reports.items() if report.startswith("ERROR ")]
        assert len(bound) == 1, reports
        assert len(refused) == 1, reports
        winner_label = bound[0]
        loser_label = refused[0]
        assert "a live hopper server (pid " in reports[loser_label]
        assert "attach to it or stop it before starting another" in reports[loser_label]

        socket_path = xdg_home / "hopper" / "server.sock"
        first_ping = send_message(socket_path, {"type": "ping"}, wait_for_response=True)
        loser_output = processes[loser_label].communicate(timeout=10)
        assert processes[loser_label].returncode != 0
        assert "a live hopper server (pid " in loser_output[0]
        second_ping = send_message(socket_path, {"type": "ping"}, wait_for_response=True)
        assert second_ping["pid"] == first_ping["pid"]
        assert second_ping["started_at"] == first_ping["started_at"]

        bound_parts = reports[winner_label].split()
        assert first_ping["pid"] == int(bound_parts[2])
        assert first_ping["started_at"] == int(bound_parts[3])
        controls[winner_label].write(b"STOP\n")
        winner_output = processes[winner_label].communicate(timeout=10)
        assert processes[winner_label].returncode == 0, winner_output
    finally:
        for control_file in controls.values():
            control_file.close()
        for connection in connections:
            connection.close()
        listener.close()
        for process in processes.values():
            if process.poll() is None:
                process.kill()
                process.communicate()


def test_server_lock_releases_after_sigkill(tmp_path):
    """A killed server needs no reaper before a replacement can bind."""
    child_code = r"""
import os
import socket
import sys
import threading

from hopper import config
from hopper.server import Server

host, port = sys.argv[1:]
control = socket.create_connection((host, int(port)), timeout=10)
control_file = control.makefile("rwb", buffering=0)
server = Server(config.server_socket_path())

def run_server():
    try:
        server.start()
    except Exception as error:
        server.startup_error = error
    finally:
        server.ready.set()

thread = threading.Thread(target=run_server, daemon=True)
thread.start()
assert server.ready.wait(5)
if server.startup_error is not None:
    control_file.write(f"ERROR {server.startup_error}\n".encode())
    sys.exit(1)
control_file.write(f"BOUND {os.getpid()} {server.started_at}\n".encode())
assert control_file.readline() == b"STOP\n"
server.stop()
thread.join(timeout=2)
"""
    xdg_home = tmp_path / "xdg"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(10)
    host, port = listener.getsockname()
    repo_root = str(Path(__file__).resolve().parents[1])
    env = os.environ.copy()
    env["XDG_DATA_HOME"] = str(xdg_home)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in [repo_root, env.get("PYTHONPATH", "")] if part
    )
    processes = []
    controls = []
    connections = []

    def start_child():
        process = subprocess.Popen(
            [sys.executable, "-c", child_code, host, str(port)],
            cwd=repo_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(process)
        connection, _ = listener.accept()
        connection.settimeout(10)
        connections.append(connection)
        control_file = connection.makefile("rwb", buffering=0)
        controls.append(control_file)
        report = control_file.readline().decode().strip().split()
        assert report[0] == "BOUND", report
        return process, control_file, int(report[1])

    try:
        first, first_control, first_pid = start_child()
        assert first_pid == first.pid
        os.kill(first_pid, signal.SIGKILL)
        first_output = first.communicate(timeout=10)
        assert first.returncode == -signal.SIGKILL, first_output
        first_control.close()

        second, second_control, second_pid = start_child()
        assert second_pid == second.pid
        socket_path = xdg_home / "hopper" / "server.sock"
        response = send_message(socket_path, {"type": "ping"}, wait_for_response=True)
        assert response["pid"] == second_pid
        assert socket_path.with_suffix(".pid").read_text() == str(second_pid)

        second_control.write(b"STOP\n")
        second_output = second.communicate(timeout=10)
        assert second.returncode == 0, second_output
    finally:
        for control_file in controls:
            if not control_file.closed:
                control_file.close()
        for connection in connections:
            connection.close()
        listener.close()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()


def test_startup_reconciliation_alive_preserves_identity_and_clears_refusal(socket_path, make_lode):
    lode = make_lode(
        id="alive-id",
        active=True,
        tmux_pane="%1",
        pid=1234,
        status="spawn refused: tmux unreachable — verify tmux is running, then retry",
    )
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch("hopper.server.pane_liveness", return_value=Liveness.ALIVE),
        patch("hopper.server.save_lodes") as mock_save,
    ):
        server._reconcile_startup_lodes()

    assert lode["active"] is True
    assert lode["tmux_pane"] == "%1"
    assert lode["pid"] == 1234
    assert lode["status"] == ""
    assert lode["updated_at"] == 1000
    mock_save.assert_called_once_with(server.lodes)


def test_startup_reconciliation_gone_clears_identity_and_refusal(socket_path, make_lode):
    lode = make_lode(
        id="gone-id",
        active=True,
        tmux_pane="%2",
        pid=2345,
        status="spawn refused: tmux unreachable — verify tmux is running, then retry",
    )
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch("hopper.server.pane_liveness", return_value=Liveness.GONE),
        patch("hopper.server.save_lodes") as mock_save,
    ):
        server._reconcile_startup_lodes()

    assert lode["active"] is False
    assert lode["tmux_pane"] is None
    assert lode["pid"] is None
    assert lode["status"] == ""
    assert lode["updated_at"] == 1000
    mock_save.assert_called_once_with(server.lodes)


def test_startup_reconciliation_unknown_preserves_identity_and_warns(
    socket_path, make_lode, caplog
):
    lode = make_lode(
        id="unknown-id",
        state="running",
        active=True,
        tmux_pane="%3",
        pid=3456,
    )
    server = Server(socket_path)
    server.lodes = [lode]
    caplog.set_level(logging.WARNING)

    with (
        patch("hopper.server.pane_liveness", return_value=Liveness.UNKNOWN),
        patch("hopper.server.save_lodes") as mock_save,
    ):
        server._reconcile_startup_lodes()

    assert lode["state"] == "running"
    assert lode["active"] is True
    assert lode["tmux_pane"] == "%3"
    assert lode["pid"] == 3456
    assert lode["status"] == (
        "spawn refused: tmux unreachable — verify tmux is running, then retry"
    )
    assert lode["updated_at"] == 1000
    assert "unknown-id" in caplog.text
    mock_save.assert_called_once_with(server.lodes)


def test_startup_reconciliation_without_pane_clears_unsupported_identity(socket_path, make_lode):
    lode = make_lode(
        id="no-pane",
        active=True,
        tmux_pane=None,
        pid=4567,
        status="spawn failed: tmux could not create a runner pane — verify tmux is running",
    )
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch("hopper.server.pane_liveness") as mock_liveness,
        patch("hopper.server.save_lodes") as mock_save,
    ):
        server._reconcile_startup_lodes()

    mock_liveness.assert_not_called()
    assert lode["active"] is False
    assert lode["tmux_pane"] is None
    assert lode["pid"] is None
    assert lode["status"] == ""
    assert lode["updated_at"] == 1000
    mock_save.assert_called_once_with(server.lodes)


def test_startup_reconciliation_mixed_lodes_saves_once(socket_path, make_lode):
    server = Server(socket_path)
    server.lodes = [
        make_lode(id="alive-id", active=True, tmux_pane="%1", pid=1),
        make_lode(id="gone-id", active=True, tmux_pane="%2", pid=2),
        make_lode(id="unknown-id", active=True, tmux_pane="%3", pid=3),
        make_lode(id="no-pane", active=True, pid=4),
    ]

    with (
        patch(
            "hopper.server.pane_liveness",
            side_effect=[Liveness.ALIVE, Liveness.GONE, Liveness.UNKNOWN],
        ),
        patch("hopper.server.save_lodes") as mock_save,
    ):
        server._reconcile_startup_lodes()

    mock_save.assert_called_once_with(server.lodes)


def test_gated_spawn_without_recorded_pane_spawns(socket_path, make_lode):
    server = Server(socket_path)
    lode = make_lode(id="fresh-id", active=False, tmux_pane=None, pid=None)
    server.lodes = [lode]

    with (
        patch("hopper.server.spawn_claude", return_value="%10") as mock_spawn,
        patch.object(server, "broadcast") as mock_broadcast,
    ):
        outcome, pane = server._gated_spawn(lode, "/repo", foreground=False)

    assert outcome is SpawnOutcome.SPAWNED
    assert pane == "%10"
    assert lode["tmux_pane"] == "%10"
    assert lode["active"] is False
    assert lode["pid"] is None
    mock_spawn.assert_called_once_with("fresh-id", "/repo", foreground=False)
    mock_broadcast.assert_called_once_with({"type": "lode_updated", "lode": lode})


def test_gated_spawn_alive_refuses_even_when_active_is_false(socket_path, make_lode, caplog):
    lode = make_lode(
        id="incident-id",
        stage="ship",
        state="running",
        status="Ship ready",
        active=False,
        tmux_pane="%11",
        pid=1111,
    )
    server = Server(socket_path)
    server.lodes = [lode]
    caplog.set_level(logging.WARNING)

    with (
        patch("hopper.server.pane_liveness", return_value=Liveness.ALIVE),
        patch("hopper.server.spawn_claude") as mock_spawn,
    ):
        outcome, pane = server._gated_spawn(
            lode,
            "/repo",
            spawn_updates={"stage": "refine", "state": "running"},
        )

    assert outcome is SpawnOutcome.ALREADY_LIVE
    assert pane is None
    mock_spawn.assert_not_called()
    assert lode["stage"] == "ship"
    assert lode["state"] == "running"
    assert lode["active"] is False
    assert lode["tmux_pane"] == "%11"
    assert lode["pid"] == 1111
    assert lode["updated_at"] == 1000
    assert lode["status"] == ("spawn refused: runner already live in pane %11 — attach instead")
    assert lode["state"] not in PROGRESS_REJECT_STATES
    assert "attach instead of spawning" in caplog.text


def test_gated_spawn_gone_clears_stale_identity_then_spawns(socket_path, make_lode):
    lode = make_lode(id="gone-id", active=True, tmux_pane="%12", pid=1212)
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch("hopper.server.pane_liveness", return_value=Liveness.GONE),
        patch("hopper.server.spawn_claude", return_value="%13") as mock_spawn,
    ):
        outcome, pane = server._gated_spawn(lode, "/repo")

    assert outcome is SpawnOutcome.SPAWNED
    assert pane == "%13"
    assert lode["active"] is False
    assert lode["tmux_pane"] == "%13"
    assert lode["pid"] is None
    mock_spawn.assert_called_once()


def test_gated_spawn_unknown_preserves_identity_and_refuses(socket_path, make_lode, caplog):
    lode = make_lode(
        id="unknown-id",
        state="running",
        active=True,
        tmux_pane="%14",
        pid=1414,
    )
    server = Server(socket_path)
    server.lodes = [lode]
    caplog.set_level(logging.WARNING)

    with (
        patch("hopper.server.pane_liveness", return_value=Liveness.UNKNOWN),
        patch("hopper.server.spawn_claude") as mock_spawn,
    ):
        outcome, pane = server._gated_spawn(lode, "/repo")

    assert outcome is SpawnOutcome.REFUSED_UNKNOWN
    assert pane is None
    mock_spawn.assert_not_called()
    assert lode["state"] == "running"
    assert lode["active"] is True
    assert lode["tmux_pane"] == "%14"
    assert lode["pid"] == 1414
    assert lode["updated_at"] == 1000
    assert lode["status"] == (
        "spawn refused: tmux unreachable — verify tmux is running, then retry"
    )
    assert "unknown-id" in caplog.text


def test_reset_with_spawn_alive_refuses_without_resetting_lode(socket_path, make_lode):
    lode = make_lode(
        id="reset-id",
        stage="refine",
        state="running",
        status="Working",
        project="proj",
        active=False,
        tmux_pane="%live",
        pid=1415,
        last_progress_at=1234,
        last_progress_summary="still working",
    )
    lode["claude"]["refine"]["started"] = True
    old_session_id = lode["claude"]["refine"]["session_id"]
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/repo", name="proj"),
        ),
        patch("hopper.server.pane_liveness", return_value=Liveness.ALIVE),
        patch("hopper.server.spawn_claude") as mock_spawn,
    ):
        server._handle_mutation(
            {
                "type": "lode_reset_claude_stage",
                "lode_id": "reset-id",
                "claude_stage": "refine",
                "spawn": True,
            },
            None,
        )

    mock_spawn.assert_not_called()
    assert lode["claude"]["refine"]["session_id"] == old_session_id
    assert lode["claude"]["refine"]["started"] is True
    assert lode["last_progress_at"] == 1234
    assert lode["last_progress_summary"] == "still working"
    assert lode["stage"] == "refine"
    assert lode["state"] == "running"
    assert lode["active"] is False
    assert lode["tmux_pane"] == "%live"
    assert lode["pid"] == 1415
    assert lode["updated_at"] == 1000
    assert lode["status"] == ("spawn refused: runner already live in pane %live — attach instead")


def test_reset_with_spawn_applies_reset_only_when_spawn_succeeds(socket_path, make_lode):
    lode = make_lode(
        id="reset-id",
        stage="refine",
        state="running",
        project="proj",
        last_progress_at=1234,
        last_progress_summary="still working",
    )
    lode["claude"]["refine"]["started"] = True
    old_session_id = lode["claude"]["refine"]["session_id"]
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/repo", name="proj"),
        ),
        patch("hopper.server.spawn_claude", return_value="%new"),
    ):
        server._handle_mutation(
            {
                "type": "lode_reset_claude_stage",
                "lode_id": "reset-id",
                "claude_stage": "refine",
                "spawn": True,
            },
            None,
        )

    assert lode["claude"]["refine"]["session_id"] != old_session_id
    assert lode["claude"]["refine"]["started"] is False
    assert lode["last_progress_at"] is None
    assert lode["last_progress_summary"] == ""
    assert lode["tmux_pane"] == "%new"


def test_reset_with_failed_spawn_restores_reset_fields(socket_path, make_lode):
    lode = make_lode(
        id="reset-id",
        stage="refine",
        state="running",
        status="Working",
        project="proj",
        last_progress_at=1234,
        last_progress_summary="still working",
    )
    lode["claude"]["refine"]["started"] = True
    old_session_id = lode["claude"]["refine"]["session_id"]
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/repo", name="proj"),
        ),
        patch("hopper.server.spawn_claude", return_value=None),
    ):
        server._handle_mutation(
            {
                "type": "lode_reset_claude_stage",
                "lode_id": "reset-id",
                "claude_stage": "refine",
                "spawn": True,
            },
            None,
        )

    assert lode["claude"]["refine"]["session_id"] == old_session_id
    assert lode["claude"]["refine"]["started"] is True
    assert lode["last_progress_at"] == 1234
    assert lode["last_progress_summary"] == "still working"
    assert lode["stage"] == "refine"
    assert lode["state"] == "running"
    assert lode["status"].startswith("spawn failed: ")


def test_gated_spawn_failure_sets_visible_status(socket_path, make_lode, caplog):
    lode = make_lode(id="failed-id")
    server = Server(socket_path)
    server.lodes = [lode]
    caplog.set_level(logging.ERROR)

    with patch("hopper.server.spawn_claude", return_value=None):
        outcome, pane = server._gated_spawn(lode, None)

    assert outcome is SpawnOutcome.FAILED
    assert pane is None
    assert lode["status"] == (
        "spawn failed: tmux could not create a runner pane — verify tmux is running, then retry"
    )
    assert "failed-id" in caplog.text


def test_gated_spawn_oserror_is_failed_and_restores_updates(socket_path, make_lode, caplog):
    lode = make_lode(id="permission-id", stage="ship", state="ready", status="Ready")
    server = Server(socket_path)
    server.lodes = [lode]
    caplog.set_level(logging.ERROR)

    with patch("hopper.server.spawn_claude", side_effect=PermissionError("tmux denied")):
        outcome, pane = server._gated_spawn(
            lode,
            "/repo",
            spawn_updates={"stage": "refine", "state": "running", "status": "Resuming"},
        )

    assert outcome is SpawnOutcome.FAILED
    assert pane is None
    assert lode["stage"] == "ship"
    assert lode["state"] == "ready"
    assert lode["active"] is False
    assert lode["tmux_pane"] is None
    assert lode["pid"] is None
    assert lode["status"].startswith("spawn failed: ")
    assert "tmux denied" in caplog.text


def test_two_queued_spawn_requests_create_one_runner(socket_path, make_lode):
    lode = make_lode(id="queued-id", project="proj")
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch("hopper.server.find_project", return_value=None),
        patch("hopper.server.pane_liveness", return_value=Liveness.ALIVE),
        patch("hopper.server.spawn_claude", return_value="%20") as mock_spawn,
    ):
        event_thread = threading.Thread(target=server._event_loop, daemon=True)
        event_thread.start()
        server.enqueue({"type": "lode_spawn", "lode_id": "queued-id"})
        server.enqueue({"type": "lode_spawn", "lode_id": "queued-id"})

        deadline = time.monotonic() + 2
        while not lode["status"].startswith("spawn refused: ") and time.monotonic() < deadline:
            time.sleep(0.01)
        server.stop_event.set()
        event_thread.join(timeout=1)

    mock_spawn.assert_called_once_with("queued-id", None, foreground=False)
    assert lode["tmux_pane"] == "%20"


def test_lode_spawn_action_passes_foreground_to_gate(socket_path, make_lode):
    lode = make_lode(id="action-id", project="proj")
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/repo", name="proj"),
        ),
        patch.object(server, "_gated_spawn") as mock_gate,
    ):
        server._handle_mutation(
            {"type": "lode_spawn", "lode_id": "action-id", "foreground": True},
            None,
        )

    mock_gate.assert_called_once_with(lode, "/repo", foreground=True)


def test_unarchive_and_spawn_is_one_server_action(socket_path, make_lode):
    lode = make_lode(id="restore-id", project="proj")
    lode["archived_at"] = 2000
    server = Server(socket_path)
    server.archived_lodes = [lode]

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/repo", name="proj"),
        ),
        patch.object(server, "_gated_spawn") as mock_gate,
    ):
        server._handle_mutation(
            {
                "type": "lode_unarchive",
                "lode_id": "restore-id",
                "spawn": True,
                "foreground": False,
            },
            None,
        )

    assert server.archived_lodes == []
    assert server.lodes == [lode]
    assert "archived_at" not in lode
    mock_gate.assert_called_once_with(lode, "/repo", foreground=False)


def test_resume_refine_applies_updates_before_allowed_spawn(socket_path, make_lode):
    lode = make_lode(id="refine-id", stage="ship", state="ready", project="proj")
    server = Server(socket_path)
    server.lodes = [lode]

    def assert_updated_before_spawn(*args, **kwargs):
        assert lode["stage"] == "refine"
        assert lode["state"] == "running"
        assert lode["status"] == "Resuming refine"
        return "%21"

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/repo", name="proj"),
        ),
        patch("hopper.server.spawn_claude", side_effect=assert_updated_before_spawn),
    ):
        server._handle_mutation({"type": "lode_resume_refine", "lode_id": "refine-id"}, None)

    assert lode["stage"] == "refine"
    assert lode["state"] == "running"
    assert lode["tmux_pane"] == "%21"
    assert "refine" not in lode["runs"]


def test_resume_refine_failed_spawn_restores_workflow_and_clears_gone_identity(
    socket_path, make_lode
):
    lode = make_lode(
        id="refine-id",
        stage="ship",
        state="ready",
        status="Ready to refine",
        project="proj",
        active=True,
        tmux_pane="%gone",
        pid=2222,
    )
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/repo", name="proj"),
        ),
        patch("hopper.server.pane_liveness", return_value=Liveness.GONE),
        patch("hopper.server.spawn_claude", return_value=None),
        patch("hopper.server.save_lodes") as mock_save,
        patch.object(server, "broadcast") as mock_broadcast,
    ):
        server._handle_mutation({"type": "lode_resume_refine", "lode_id": "refine-id"}, None)

    assert lode["stage"] == "ship"
    assert lode["state"] == "ready"
    assert lode["active"] is False
    assert lode["tmux_pane"] is None
    assert lode["pid"] is None
    assert lode["status"].startswith("spawn failed: ")
    mock_save.assert_called_once_with(server.lodes)
    mock_broadcast.assert_called_once_with({"type": "lode_updated", "lode": lode})


def test_resume_refine_refusal_leaves_stage_and_state_unchanged(socket_path, make_lode):
    lode = make_lode(
        id="refine-id",
        stage="ship",
        state="ready",
        project="proj",
        active=False,
        tmux_pane="%22",
    )
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch("hopper.server.find_project", return_value=None),
        patch("hopper.server.pane_liveness", return_value=Liveness.ALIVE),
        patch("hopper.server.spawn_claude") as mock_spawn,
    ):
        server._handle_mutation({"type": "lode_resume_refine", "lode_id": "refine-id"}, None)

    mock_spawn.assert_not_called()
    assert lode["stage"] == "ship"
    assert lode["state"] == "ready"
    assert lode["tmux_pane"] == "%22"


def test_resume_uses_gate_without_signaling_recorded_pid(socket_path, make_lode):
    lode = make_lode(
        id="resume-id",
        stage="refine",
        state="paused",
        project="proj",
        active=False,
        tmux_pane="%dead",
        pid=4242,
    )
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/repo", name="proj"),
        ),
        patch("hopper.server.pane_liveness", return_value=Liveness.GONE),
        patch("hopper.server.spawn_claude", return_value="%26"),
        patch("hopper.server.os.kill") as mock_kill,
    ):
        server._handle_mutation(
            {"type": "lode_resume", "lode_id": "resume-id"},
            None,
        )

    mock_kill.assert_not_called()
    assert lode["state"] == "running"
    assert lode["tmux_pane"] == "%26"
    assert lode["pid"] is None


def test_fresh_backlog_promotion_spawns_through_gate(socket_path):
    item = BacklogItem(
        id="backlog1",
        project="proj",
        description="Fresh work",
        created_at=1000,
    )
    server = Server(socket_path)
    server.backlog = [item]

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/repo", name="proj"),
        ),
        patch("hopper.server.spawn_claude", return_value="%23") as mock_spawn,
    ):
        lode = server._promote_backlog_item(item)

    assert lode["tmux_pane"] == "%23"
    mock_spawn.assert_called_once_with(lode["id"], "/repo", foreground=False)


def test_fresh_lode_create_spawns_through_gate(socket_path):
    server = Server(socket_path)

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/repo", name="proj"),
        ),
        patch("hopper.server.spawn_claude", return_value="%24") as mock_spawn,
    ):
        server._handle_mutation(
            {"type": "lode_create", "project": "proj", "scope": "work", "spawn": True},
            None,
        )

    lode = server.lodes[0]
    assert lode["tmux_pane"] == "%24"
    mock_spawn.assert_called_once_with(lode["id"], "/repo", foreground=False)


@pytest.mark.parametrize(
    "status",
    [
        "spawn refused: tmux unreachable — verify tmux is running, then retry",
        "spawn failed: tmux could not create a runner pane — verify tmux is running, then retry",
    ],
)
def test_runner_registration_clears_spawn_status(socket_path, make_lode, status):
    lode = make_lode(id="register-id", status=status)
    server = Server(socket_path)
    server.lodes = [lode]

    server._register_lode_client("register-id", MagicMock(), tmux_pane="%25", pid=2525)

    assert lode["status"] == ""
    assert lode["active"] is True
    assert lode["tmux_pane"] == "%25"
    assert lode["pid"] == 2525


def test_spawn_claude_has_one_production_caller():
    hopper_dir = Path(__file__).resolve().parents[1] / "hopper"
    source = (hopper_dir / "server.py").read_text()
    assert source.count("spawn_claude(") == 1
    assert sum(path.read_text().count("spawn_claude(") for path in hopper_dir.glob("*.py")) == 2


def test_startup_archives_shipped_lodes(socket_path, temp_config, make_lode):
    """Server startup migrates shipped lodes from active to archived."""
    shipped_lode = make_lode(
        id="test-id",
        stage="shipped",
        active=True,
        tmux_pane="%1",
        pid=1234,
    )
    save_lodes([shipped_lode])

    srv = Server(socket_path)
    with patch("hopper.server.pane_liveness", return_value=Liveness.ALIVE):
        thread = threading.Thread(target=srv.start, daemon=True)
        thread.start()

        try:
            assert srv.ready.wait(5), "Server did not start"

            assert srv.lodes == []
            assert len(srv.archived_lodes) == 1
            assert srv.archived_lodes[0]["id"] == "test-id"
            assert "archived_at" in srv.archived_lodes[0]

            archived_file = temp_config / "archived.jsonl"
            assert archived_file.exists()
            archived_entries = [
                json.loads(line) for line in archived_file.read_text().splitlines() if line.strip()
            ]
            assert len(archived_entries) == 1
            assert archived_entries[0]["id"] == "test-id"
            assert "archived_at" in archived_entries[0]
        finally:
            srv.stop()
            thread.join(timeout=2)


def test_startup_archives_shipped_lode_when_tmux_is_unknown(
    socket_path, temp_config, make_lode, caplog
):
    """UNKNOWN runner evidence does not block lock-held shipped auto-archive."""
    shipped_lode = make_lode(
        id="unknown-shipped",
        stage="shipped",
        active=True,
        tmux_pane="%9",
        pid=9999,
    )
    save_lodes([shipped_lode])
    server = Server(socket_path)
    caplog.set_level(logging.WARNING)

    with patch("hopper.server.pane_liveness", return_value=Liveness.UNKNOWN):
        thread = threading.Thread(target=server.start, daemon=True)
        thread.start()
        try:
            assert server.ready.wait(5), "Server did not start"
            assert server.lodes == []
            assert server.archived_lodes[0]["id"] == "unknown-shipped"
            assert server.archived_lodes[0]["status"].startswith("spawn refused: ")
            assert "unknown-shipped" in caplog.text
        finally:
            server.stop()
            thread.join(timeout=2)


def test_cleanup_worktree_on_startup_archive(socket_path, temp_config, make_lode):
    """Startup archive triggers worktree and branch cleanup."""
    shipped_lode = make_lode(
        id="test-id",
        stage="shipped",
        project="myproject",
        branch="hopper-test-id",
    )
    save_lodes([shipped_lode])
    worktree_dir = temp_config / "lodes" / shipped_lode["id"] / "worktree"
    worktree_dir.mkdir(parents=True)

    with (
        patch(
            "hopper.server.find_project", return_value=Project(path="/fake/repo", name="myproject")
        ),
        patch("hopper.server.is_dirty", return_value=False),
        patch("hopper.server.remove_worktree") as mock_remove_worktree,
        patch("hopper.server.delete_branch") as mock_delete_branch,
    ):
        srv = Server(socket_path)
        thread = threading.Thread(target=srv.start, daemon=True)
        thread.start()

        try:
            assert srv.ready.wait(5), "Server did not start"

            for _ in range(50):
                if mock_remove_worktree.called and mock_delete_branch.called:
                    break
                time.sleep(0.1)

            mock_remove_worktree.assert_called_once_with("/fake/repo", str(worktree_dir))
            mock_delete_branch.assert_called_once_with("/fake/repo", shipped_lode["branch"])
        finally:
            srv.stop()
            thread.join(timeout=2)


def test_cleanup_dirty_worktree_skips_remove_and_branch(
    socket_path, temp_config, make_lode, caplog
):
    """Dirty worktree cleanup retains path and skips branch deletion."""
    lode = make_lode(
        id="test-id",
        stage="shipped",
        project="myproject",
        branch="hopper-test-id",
    )
    worktree_dir = temp_config / "lodes" / lode["id"] / "worktree"
    worktree_dir.mkdir(parents=True)
    srv = Server(socket_path)
    caplog.set_level("WARNING")

    with (
        patch(
            "hopper.server.find_project", return_value=Project(path="/fake/repo", name="myproject")
        ),
        patch("hopper.server.is_dirty", return_value=True) as mock_dirty,
        patch("hopper.server.remove_worktree") as mock_remove_worktree,
        patch("hopper.server.delete_branch") as mock_delete_branch,
    ):
        srv._cleanup_worktree(lode)

    mock_dirty.assert_called_once_with(str(worktree_dir))
    mock_remove_worktree.assert_not_called()
    mock_delete_branch.assert_not_called()
    assert worktree_dir.exists()
    assert any(
        "worktree has uncommitted changes" in record.getMessage() for record in caplog.records
    )


def test_cleanup_clean_worktree_removes_and_deletes_branch(socket_path, temp_config, make_lode):
    """Clean worktree cleanup removes the worktree and deletes the branch."""
    lode = make_lode(
        id="test-id",
        stage="shipped",
        project="myproject",
        branch="hopper-test-id",
    )
    worktree_dir = temp_config / "lodes" / lode["id"] / "worktree"
    worktree_dir.mkdir(parents=True)
    srv = Server(socket_path)

    with (
        patch(
            "hopper.server.find_project", return_value=Project(path="/fake/repo", name="myproject")
        ),
        patch("hopper.server.is_dirty", return_value=False) as mock_dirty,
        patch("hopper.server.remove_worktree") as mock_remove_worktree,
        patch("hopper.server.delete_branch") as mock_delete_branch,
    ):
        srv._cleanup_worktree(lode)

    mock_dirty.assert_called_once_with(str(worktree_dir))
    mock_remove_worktree.assert_called_once_with("/fake/repo", str(worktree_dir))
    mock_delete_branch.assert_called_once_with("/fake/repo", lode["branch"])


def test_cleanup_skipped_without_worktree_dir(socket_path, temp_config, make_lode):
    """Cleanup is skipped when archived lode has no worktree directory."""
    shipped_lode = make_lode(id="test-id", stage="shipped", project="myproject")
    save_lodes([shipped_lode])

    with (
        patch(
            "hopper.server.find_project", return_value=Project(path="/fake/repo", name="myproject")
        ),
        patch("hopper.server.is_dirty", return_value=False),
        patch("hopper.server.remove_worktree") as mock_remove_worktree,
        patch("hopper.server.delete_branch") as mock_delete_branch,
    ):
        srv = Server(socket_path)
        thread = threading.Thread(target=srv.start, daemon=True)
        thread.start()

        try:
            assert srv.ready.wait(5), "Server did not start"

            for _ in range(50):
                if not srv.lodes:
                    break
                time.sleep(0.1)

            mock_remove_worktree.assert_not_called()
            mock_delete_branch.assert_not_called()
        finally:
            srv.stop()
            thread.join(timeout=2)


def test_cleanup_skipped_when_project_not_found(socket_path, temp_config, make_lode):
    """Cleanup is skipped when archived lode project cannot be found."""
    shipped_lode = make_lode(id="test-id", stage="shipped", project="myproject")
    save_lodes([shipped_lode])
    worktree_dir = temp_config / "lodes" / shipped_lode["id"] / "worktree"
    worktree_dir.mkdir(parents=True)

    with (
        patch("hopper.server.find_project", return_value=None),
        patch("hopper.server.remove_worktree") as mock_remove_worktree,
        patch("hopper.server.delete_branch") as mock_delete_branch,
    ):
        srv = Server(socket_path)
        thread = threading.Thread(target=srv.start, daemon=True)
        thread.start()

        try:
            assert srv.ready.wait(5), "Server did not start"

            for _ in range(50):
                if not srv.lodes:
                    break
                time.sleep(0.1)

            mock_remove_worktree.assert_not_called()
            mock_delete_branch.assert_not_called()
        finally:
            srv.stop()
            thread.join(timeout=2)


def test_server_broadcast_requires_type():
    """Broadcast rejects messages without type field."""
    srv = Server(socket_path="/tmp/unused.sock")

    result = srv.broadcast({"data": "test"})

    assert result is False
    assert srv.broadcast_queue.qsize() == 0


def test_server_broadcast_queues_valid_message():
    """Broadcast queues messages with type field."""
    srv = Server(socket_path="/tmp/unused.sock")

    result = srv.broadcast({"type": "test", "data": "hello"})

    assert result is True
    assert srv.broadcast_queue.qsize() == 1
    msg = srv.broadcast_queue.get_nowait()
    assert msg["type"] == "test"
    assert msg["data"] == "hello"


def test_server_sends_shutdown_to_clients(socket_path):
    """Server sends shutdown message to connected clients on stop."""
    srv = Server(socket_path)
    thread = threading.Thread(target=srv.start, daemon=True)
    thread.start()

    assert srv.ready.wait(5), "Server did not start"

    # Connect a client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Wait for client to be registered by server
    for _ in range(50):
        if len(srv.clients) > 0:
            break
        time.sleep(0.1)

    # Stop server (should send shutdown message)
    srv.stop()

    # Client should receive shutdown message (may get connection reset after)
    try:
        data = client.recv(4096).decode("utf-8")
        messages = [json.loads(line) for line in data.strip().split("\n") if line]
        assert any(msg.get("type") == "shutdown" for msg in messages)
    except ConnectionResetError:
        # If we get reset, the shutdown was sent but connection closed quickly
        # This is acceptable - the important thing is stop() completes cleanly
        pass

    client.close()
    thread.join(timeout=2)


def test_server_handles_connect(socket_path, server):
    """Server handles connect message and returns connected response."""
    # Connect client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Send connect message
    msg = {"type": "connect"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Should receive connected response
    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "connected"
    assert "tmux" in response
    assert response["tmux"] is None  # No tmux location set

    client.close()


def test_server_handles_connect_with_tmux_location(socket_path, temp_config):
    """Server includes tmux location in connect response."""
    tmux_location = {"lode": "main", "pane": "%0"}
    srv = Server(socket_path, tmux_location=tmux_location)
    thread = threading.Thread(target=srv.start, daemon=True)
    thread.start()

    assert srv.ready.wait(5), "Server did not start"

    try:
        # Connect client
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)

        # Send connect message
        msg = {"type": "connect"}
        client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

        # Should receive connected response with tmux location
        data = client.recv(4096).decode("utf-8")
        response = json.loads(data.strip().split("\n")[0])

        assert response["type"] == "connected"
        assert response["tmux"] == {"lode": "main", "pane": "%0"}

        client.close()
    finally:
        srv.stop()
        thread.join(timeout=2)


def test_server_handles_connect_with_lode_id(socket_path, server, temp_config, make_lode):
    """Server returns lode data when lode_id is provided."""
    lode = make_lode(id="test-id")
    server.lodes = [lode]
    save_lodes(server.lodes)

    # Connect client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Send connect message with lode_id
    msg = {"type": "connect", "lode_id": "test-id"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Should receive connected response with lode data
    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "connected"
    assert response["lode_found"] is True
    assert response["lode"]["id"] == "test-id"
    assert response["lode"]["state"] == "new"

    client.close()


def test_server_handles_connect_with_missing_lode_id(socket_path, server):
    """Server returns lode_found=False for unknown lode."""
    # Connect client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Send connect message with unknown lode_id
    msg = {"type": "connect", "lode_id": "nonexistent"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Should receive connected response with lode not found
    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "connected"
    assert response["lode_found"] is False
    assert response["lode"] is None

    client.close()


def test_server_handles_lode_set_state(socket_path, server, temp_config, make_lode):
    """Server handles lode_set_state message."""
    lode = make_lode(id="test-id")
    server.lodes = [lode]
    save_lodes(server.lodes)

    # Connect client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Wait for client to be registered
    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    # Send lode_set_state message
    msg = {"type": "lode_set_state", "lode_id": "test-id", "state": "running"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Should receive broadcast
    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "lode_updated"
    assert response["lode"]["id"] == "test-id"
    assert response["lode"]["state"] == "running"

    # Server's lode should be updated
    assert server.lodes[0]["state"] == "running"

    client.close()


def test_server_handles_lode_set_progress(socket_path, server, temp_config, make_lode):
    """Server stores truncated progress heartbeats and broadcasts lode_updated."""
    lode = make_lode(id="test-id", state="running")
    server.lodes = [lode]
    save_lodes(server.lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    summary = "x" * 200
    msg = {"type": "lode_set_progress", "lode_id": "test-id", "summary": summary}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "lode_updated"
    assert response["lode"]["id"] == "test-id"
    assert response["lode"]["last_progress_summary"] == "x" * 120
    assert response["lode"]["last_progress_at"] is not None
    assert server.lodes[0]["last_progress_summary"] == "x" * 120
    assert server.lodes[0]["last_progress_at"] is not None

    client.close()


@pytest.mark.parametrize("state", ["running", "stuck", "audit"])
def test_server_accepts_progress_for_live_states(socket_path, make_lode, state):
    """Running, stuck, and freeform task states accept progress heartbeats."""
    srv = Server(socket_path)
    lode = make_lode(id="test-id", state=state)
    srv.lodes = [lode]

    with (
        patch("hopper.server.save_lodes") as mock_save,
        patch.object(srv, "broadcast") as mock_broadcast,
    ):
        srv._handle_mutation(
            {"type": "lode_set_progress", "lode_id": "test-id", "summary": "working"},
            None,
        )

    assert lode["last_progress_at"] is not None
    assert lode["last_progress_summary"] == "working"
    mock_save.assert_called_once_with(srv.lodes)
    mock_broadcast.assert_called_once_with({"type": "lode_updated", "lode": lode})


@pytest.mark.parametrize("state", ["new", "gated", "ready", "completed", "error"])
def test_server_rejects_progress_for_terminal_or_inactive_states(
    socket_path, make_lode, state, caplog
):
    """Zombie heartbeats cannot mutate, persist, or broadcast inactive lodes."""
    srv = Server(socket_path)
    lode = make_lode(
        id="test-id",
        state=state,
        updated_at=234,
        last_progress_at=123,
        last_progress_summary="existing",
    )
    srv.lodes = [lode]

    with (
        caplog.at_level(logging.DEBUG, logger="hopper.server"),
        patch("hopper.server.save_lodes") as mock_save,
        patch.object(srv, "broadcast") as mock_broadcast,
    ):
        srv._handle_mutation(
            {"type": "lode_set_progress", "lode_id": "test-id", "summary": "zombie"},
            None,
        )

    assert lode["last_progress_at"] == 123
    assert lode["last_progress_summary"] == "existing"
    assert lode["updated_at"] == 234
    mock_save.assert_not_called()
    mock_broadcast.assert_not_called()
    assert f"Ignoring progress heartbeat for lode test-id in state={state}" in caplog.messages


def test_server_handles_lode_set_title(socket_path, server, temp_config, make_lode):
    """Server handles lode_set_title message."""
    lode = make_lode(id="test-id")
    server.lodes = [lode]
    save_lodes(server.lodes)

    # Connect client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Wait for client to be registered
    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    # Send lode_set_title message
    msg = {"type": "lode_set_title", "lode_id": "test-id", "title": "Auth Flow"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Should receive broadcast
    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "lode_updated"
    assert response["lode"]["id"] == "test-id"
    assert response["lode"]["title"] == "Auth Flow"

    # Server's lode should be updated
    assert server.lodes[0]["title"] == "Auth Flow"

    client.close()


def test_server_handles_lode_set_branch(socket_path, server, temp_config, make_lode):
    """Server handles lode_set_branch message."""
    lode = make_lode(id="test-id")
    server.lodes = [lode]
    save_lodes(server.lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    msg = {"type": "lode_set_branch", "lode_id": "test-id", "branch": "hopper-test-id-auth-flow"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "lode_updated"
    assert response["lode"]["id"] == "test-id"
    assert response["lode"]["branch"] == "hopper-test-id-auth-flow"
    assert server.lodes[0]["branch"] == "hopper-test-id-auth-flow"

    client.close()


def test_server_handles_lode_kill(socket_path, temp_config, make_lode):
    """lode_kill terminates the process, kills the pane, and archives the lode."""
    srv = Server(socket_path)
    lode = make_lode(
        id="test-id",
        stage="mill",
        state="running",
        status="Working",
        active=True,
        tmux_pane="%1",
        pid=12345,
        project="myproject",
        branch="hopper-test-id",
    )
    srv.lodes = [lode]
    save_lodes(srv.lodes)

    with (
        patch("hopper.server.os.kill") as mock_os_kill,
        patch("hopper.tmux.kill_pane", return_value=True) as mock_kill_pane,
        patch.object(srv, "broadcast") as mock_broadcast,
        patch.object(srv, "_cleanup_worktree") as mock_cleanup,
    ):
        srv._handle_mutation({"type": "lode_kill", "lode_id": "test-id"}, None)

    mock_os_kill.assert_called_once_with(12345, signal.SIGTERM)
    mock_kill_pane.assert_called_once_with("%1")
    assert srv.lodes == []
    assert len(srv.archived_lodes) == 1
    archived = srv.archived_lodes[0]
    assert archived["id"] == "test-id"
    assert archived["state"] == "error"
    assert archived["status"] == "Killed by user"
    assert archived["active"] is False
    assert archived["tmux_pane"] is None
    assert archived["pid"] is None
    mock_broadcast.assert_any_call({"type": "lode_updated", "lode": archived})
    mock_broadcast.assert_any_call({"type": "lode_archived", "lode": archived})
    mock_cleanup.assert_not_called()


def test_server_pauses_lode_without_archiving(socket_path, temp_config, make_lode):
    """Pause terminates the runner but retains the lode and worktree state."""
    srv = Server(socket_path)
    lode = make_lode(id="test-id", state="running", active=True, tmux_pane="%1", pid=12345)
    srv.lodes = [lode]
    conn = MagicMock()

    with (
        patch("hopper.server.os.kill") as mock_os_kill,
        patch("hopper.tmux.kill_pane", return_value=True) as mock_kill_pane,
        patch.object(srv, "broadcast"),
    ):
        srv._handle_mutation({"type": "lode_pause", "lode_id": "test-id"}, conn)

    mock_os_kill.assert_called_once_with(12345, signal.SIGTERM)
    mock_kill_pane.assert_called_once_with("%1")
    assert srv.archived_lodes == []
    assert srv.lodes[0]["state"] == "paused"
    assert srv.lodes[0]["active"] is False
    assert _decode_mock_response(conn)["type"] == "lode_paused"


def test_server_resumes_paused_lode_with_existing_stage(socket_path, temp_config, make_lode):
    """Resume spawns the preserved stage session without resetting it."""
    srv = Server(socket_path)
    lode = make_lode(id="test-id", stage="refine", state="paused", active=False, project="proj")
    srv.lodes = [lode]
    conn = MagicMock()

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/fake/repo", name="proj"),
        ),
        patch("hopper.server.spawn_claude", return_value="%2") as mock_spawn,
        patch.object(srv, "broadcast"),
    ):
        srv._handle_mutation({"type": "lode_resume", "lode_id": "test-id"}, conn)

    mock_spawn.assert_called_once_with("test-id", "/fake/repo", foreground=False)
    assert srv.lodes[0]["state"] == "running"
    assert srv.lodes[0]["status"] == "Resuming refine"
    response = _decode_mock_response(conn)
    assert response["type"] == "lode_resumed"
    assert response["tmux_pane"] == "%2"


@pytest.mark.parametrize(
    ("outcome", "guidance"),
    [
        (SpawnOutcome.ALREADY_LIVE, "attach instead of spawning"),
        (SpawnOutcome.REFUSED_UNKNOWN, "verify tmux is running, then retry"),
        (SpawnOutcome.FAILED, "verify tmux is running, then retry"),
    ],
)
def test_server_resume_failure_response_is_prescriptive(socket_path, make_lode, outcome, guidance):
    server = Server(socket_path)
    server.lodes = [make_lode(id="test-id", stage="refine", state="paused", project="proj")]
    conn = MagicMock()

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/fake/repo", name="proj"),
        ),
        patch.object(server, "_gated_spawn", return_value=(outcome, None)),
    ):
        server._handle_mutation({"type": "lode_resume", "lode_id": "test-id"}, conn)

    response = _decode_mock_response(conn)
    assert response["type"] == "error"
    assert guidance in response["error"]


def test_server_handles_lode_kill_missing_process(socket_path, temp_config, make_lode):
    """lode_kill ignores already-dead processes and still archives the lode."""
    srv = Server(socket_path)
    lode = make_lode(id="test-id", state="running", active=True, tmux_pane="%1", pid=12345)
    srv.lodes = [lode]
    save_lodes(srv.lodes)

    with (
        patch("hopper.server.os.kill", side_effect=ProcessLookupError) as mock_os_kill,
        patch("hopper.tmux.kill_pane", return_value=True) as mock_kill_pane,
        patch.object(srv, "broadcast"),
        patch.object(srv, "_cleanup_worktree"),
    ):
        srv._handle_mutation({"type": "lode_kill", "lode_id": "test-id"}, None)

    mock_os_kill.assert_called_once_with(12345, signal.SIGTERM)
    mock_kill_pane.assert_called_once_with("%1")
    assert srv.lodes == []
    assert len(srv.archived_lodes) == 1


def test_server_handles_backlog_set_queued(socket_path, server):
    """Server handles backlog_set_queued and broadcasts backlog_updated."""
    item = BacklogItem(
        id="bl111111",
        project="myproj",
        description="Queued item",
        created_at=1000,
    )
    server.backlog = [item]

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    msg = {"type": "backlog_set_queued", "item_id": "bl111111", "queued": "lode1234"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "backlog_updated"
    assert response["item"]["id"] == "bl111111"
    assert response["item"]["queued"] == "lode1234"
    assert server.backlog[0].queued == "lode1234"

    client.close()


def test_auto_promote_backlog_on_ship_stage(socket_path, server, temp_config, make_lode):
    """Shipping a lode auto-promotes the oldest queued backlog item for that project."""
    lode = make_lode(id="lode1234", project="myproj", stage="ship")
    server.lodes = [lode]
    save_lodes(server.lodes)
    server.backlog = [
        BacklogItem(
            id="bl111111",
            project="myproj",
            description="Promote me",
            created_at=1000,
            queued="lode1234",
        )
    ]

    with patch("hopper.server.spawn_claude", return_value="%30") as mock_spawn:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)

        for _ in range(50):
            if len(server.clients) > 0:
                break
            time.sleep(0.1)

        msg = {"type": "lode_set_stage", "lode_id": "lode1234", "stage": "shipped"}
        client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

        messages = _recv_messages_until(client, {"lode_updated", "lode_created", "backlog_removed"})

        updated = next(msg for msg in messages if msg.get("type") == "lode_updated")
        created = next(msg for msg in messages if msg.get("type") == "lode_created")
        removed = next(msg for msg in messages if msg.get("type") == "backlog_removed")

        assert updated["lode"]["id"] == "lode1234"
        assert updated["lode"]["stage"] == "shipped"
        assert created["lode"]["project"] == "myproj"
        assert created["lode"]["scope"] == "Promote me"
        assert removed["item"]["id"] == "bl111111"
        assert server.lodes[0]["stage"] == "shipped"
        assert len(server.backlog) == 0
        mock_spawn.assert_called_once_with(created["lode"]["id"], None, foreground=False)

        client.close()


def test_promote_backlog_item_disabled_project_returns_none(socket_path):
    """Disabled project backlog items are not promoted or removed."""
    srv = Server(socket_path)
    item = BacklogItem(
        id="bl111111",
        project="P",
        description="Promote me",
        created_at=1000,
    )
    srv.backlog = [item]
    disabled = Project(path="/fake/repo", name="P", disabled=True, disabled_reason="wip")

    with (
        patch("hopper.server.find_project", return_value=disabled),
        patch("hopper.server.spawn_claude") as mock_spawn,
        patch.object(srv, "broadcast") as mock_broadcast,
    ):
        result = srv._promote_backlog_item(item)

    assert result is None
    assert srv.lodes == []
    assert srv.backlog == [item]
    mock_spawn.assert_not_called()
    mock_broadcast.assert_not_called()


def test_auto_promote_on_ship_disabled_project_does_not_promote(socket_path, make_lode):
    """Auto-promote leaves a disabled project's queued item in place."""
    srv = Server(socket_path)
    lode = make_lode(id="lode1234", project="P", stage="ship")
    item = BacklogItem(
        id="bl111111",
        project="P",
        description="Promote me",
        created_at=1000,
        queued="lode1234",
    )
    srv.lodes = [lode]
    srv.backlog = [item]
    disabled = Project(path="/fake/repo", name="P", disabled=True, disabled_reason="wip")

    with (
        patch("hopper.server.find_project", return_value=disabled),
        patch("hopper.server.spawn_claude") as mock_spawn,
        patch.object(srv, "broadcast") as mock_broadcast,
    ):
        srv._handle_mutation(
            {"type": "lode_set_stage", "lode_id": "lode1234", "stage": "shipped"},
            None,
        )

    assert len(srv.lodes) == 1
    assert srv.backlog == [item]
    assert item.queued == "lode1234"
    assert not any(
        call.args[0].get("type") == "lode_created" for call in mock_broadcast.call_args_list
    )
    mock_spawn.assert_not_called()


def test_lode_promote_backlog_disabled_sends_promote_error(socket_path):
    """Manual promote reports disabled projects with promote_error."""
    srv = Server(socket_path)
    item = BacklogItem(
        id="bl111111",
        project="P",
        description="Promote me",
        created_at=1000,
    )
    srv.backlog = [item]
    conn = MagicMock()
    disabled = Project(path="/fake/repo", name="P", disabled=True, disabled_reason="wip")

    with (
        patch("hopper.server.find_project", return_value=disabled),
        patch("hopper.server.spawn_claude") as mock_spawn,
    ):
        srv._handle_mutation(
            {"type": "lode_promote_backlog", "item_id": "bl111111", "scope": ""},
            conn,
        )

    response = _decode_mock_response(conn)
    assert response["type"] == "promote_error"
    assert "error: project 'P' is disabled" in response["error"]
    assert "  reason: wip" in response["error"]
    assert srv.lodes == []
    assert srv.backlog == [item]
    mock_spawn.assert_not_called()


def test_auto_promote_backlog_on_ship_stage_uses_oldest(
    socket_path, server, temp_config, make_lode
):
    """When multiple items are queued behind a shipped lode, only oldest is promoted."""
    lode = make_lode(id="lode1234", project="myproj", stage="ship")
    server.lodes = [lode]
    save_lodes(server.lodes)
    older = BacklogItem(
        id="bl111111",
        project="myproj",
        description="Older",
        created_at=1000,
        queued="lode1234",
    )
    newer = BacklogItem(
        id="bl222222",
        project="myproj",
        description="Newer",
        created_at=2000,
        queued="lode1234",
    )
    server.backlog = [newer, older]

    with patch("hopper.server.spawn_claude", return_value="%31") as mock_spawn:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)

        for _ in range(50):
            if len(server.clients) > 0:
                break
            time.sleep(0.1)

        msg = {"type": "lode_set_stage", "lode_id": "lode1234", "stage": "shipped"}
        client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

        messages = _recv_messages_until(
            client, {"lode_updated", "lode_created", "backlog_removed", "backlog_updated"}
        )

        created_msgs = [msg for msg in messages if msg.get("type") == "lode_created"]
        removed_msgs = [msg for msg in messages if msg.get("type") == "backlog_removed"]
        assert len(created_msgs) == 1
        assert len(removed_msgs) == 1
        assert removed_msgs[0]["item"]["id"] == "bl111111"
        assert len(server.backlog) == 1
        assert server.backlog[0].id == "bl222222"
        # Remaining item re-queued behind the new lode
        assert server.backlog[0].queued == created_msgs[0]["lode"]["id"]
        updated_msgs = [msg for msg in messages if msg.get("type") == "backlog_updated"]
        assert len(updated_msgs) == 1
        assert updated_msgs[0]["item"]["id"] == "bl222222"
        assert updated_msgs[0]["item"]["queued"] == created_msgs[0]["lode"]["id"]
        mock_spawn.assert_called_once_with(created_msgs[0]["lode"]["id"], None, foreground=False)

        client.close()


def test_auto_promote_chains_multiple_queued_items(socket_path, server, temp_config, make_lode):
    """When 3 items are queued behind a shipped lode, oldest is promoted and
    remaining 2 re-queue behind the new lode.
    """
    lode = make_lode(id="lode1234", project="myproj", stage="ship")
    server.lodes = [lode]
    save_lodes(server.lodes)
    item_a = BacklogItem(
        id="bl_aaaaaa",
        project="myproj",
        description="A oldest",
        created_at=1000,
        queued="lode1234",
    )
    item_b = BacklogItem(
        id="bl_bbbbbb",
        project="myproj",
        description="B middle",
        created_at=2000,
        queued="lode1234",
    )
    item_c = BacklogItem(
        id="bl_cccccc",
        project="myproj",
        description="C newest",
        created_at=3000,
        queued="lode1234",
    )
    server.backlog = [item_c, item_b, item_a]  # intentionally out of order

    with patch("hopper.server.spawn_claude", return_value="%32") as mock_spawn:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)

        for _ in range(50):
            if len(server.clients) > 0:
                break
            time.sleep(0.1)

        msg = {"type": "lode_set_stage", "lode_id": "lode1234", "stage": "shipped"}
        client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

        messages = _recv_messages_until(
            client, {"lode_updated", "lode_created", "backlog_removed", "backlog_updated"}
        )
        client.settimeout(0.1)
        drain_deadline = time.time() + 1.0
        while time.time() < drain_deadline:
            try:
                data = client.recv(4096).decode("utf-8")
            except socket.timeout:
                break
            for line in data.strip().split("\n"):
                if line:
                    messages.append(json.loads(line))
        client.settimeout(2.0)

        created_msgs = [msg for msg in messages if msg.get("type") == "lode_created"]
        removed_msgs = [msg for msg in messages if msg.get("type") == "backlog_removed"]
        updated_msgs = [msg for msg in messages if msg.get("type") == "backlog_updated"]

        # Oldest promoted
        assert len(created_msgs) == 1
        assert len(removed_msgs) == 1
        assert removed_msgs[0]["item"]["id"] == "bl_aaaaaa"

        # Two remaining items re-queued behind the new lode
        new_lode_id = created_msgs[0]["lode"]["id"]
        assert len(server.backlog) == 2
        assert len(updated_msgs) == 2
        for item in server.backlog:
            assert item.queued == new_lode_id
        for umsg in updated_msgs:
            assert umsg["item"]["queued"] == new_lode_id

        mock_spawn.assert_called_once()
        client.close()


def test_server_connect_does_not_register_ownership(socket_path, server, temp_config, make_lode):
    """Connect message returns lode data but does not register ownership."""
    lode = make_lode(id="test-id")
    server.lodes = [lode]
    save_lodes(server.lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    msg = {"type": "connect", "lode_id": "test-id"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))
    client.recv(4096)

    # Give server time to process
    time.sleep(0.2)

    # Connect should NOT register ownership or set active
    assert "test-id" not in server.lode_clients
    assert server.lodes[0]["active"] is False

    client.close()


def test_server_registers_on_lode_register(socket_path, server, temp_config, make_lode):
    """lode_register message claims ownership and sets active=True."""
    lode = make_lode(id="test-id")
    server.lodes = [lode]
    save_lodes(server.lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    msg = {"type": "lode_register", "lode_id": "test-id", "pid": 12345}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Wait for registration
    for _ in range(50):
        if "test-id" in server.lode_clients:
            break
        time.sleep(0.1)

    assert "test-id" in server.lode_clients
    assert server.lodes[0]["active"] is True
    assert server.lodes[0]["pid"] == 12345

    client.close()


def test_server_sets_active_false_on_disconnect(socket_path, server, temp_config, make_lode):
    """Server sets active=False and clears tmux_pane on client disconnect."""
    lode = make_lode(id="test-id", state="running", tmux_pane="%1")
    server.lodes = [lode]
    save_lodes(server.lodes)

    # Connect client and register ownership
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    msg = {"type": "lode_register", "lode_id": "test-id"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Wait for registration
    for _ in range(50):
        if "test-id" in server.lode_clients:
            break
        time.sleep(0.1)

    assert server.lodes[0]["active"] is True

    # Disconnect client
    client.close()

    # Wait for disconnect handling
    for _ in range(50):
        if not server.lodes[0]["active"]:
            break
        time.sleep(0.1)

    # active=False, tmux_pane cleared, but state/status untouched
    assert server.lodes[0]["active"] is False
    assert server.lodes[0]["tmux_pane"] is None
    assert server.lodes[0]["state"] == "running"
    assert "test-id" not in server.lode_clients


def test_server_clears_pid_on_disconnect(socket_path, server, temp_config, make_lode):
    """Server clears pid on client disconnect."""
    lode = make_lode(id="test-id", state="running", pid=54321)
    server.lodes = [lode]
    save_lodes(server.lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    msg = {"type": "lode_register", "lode_id": "test-id", "pid": 12345}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    for _ in range(50):
        if server.lodes[0]["pid"] == 12345:
            break
        time.sleep(0.1)

    assert server.lodes[0]["pid"] == 12345

    client.close()

    for _ in range(50):
        if server.lodes[0]["pid"] is None:
            break
        time.sleep(0.1)

    assert server.lodes[0]["active"] is False
    assert server.lodes[0]["pid"] is None


def test_server_preserves_state_on_disconnect(socket_path, server, temp_config, make_lode):
    """Server preserves state and status on client disconnect (only toggles active)."""
    lode = make_lode(id="test-id", state="error", status="Something failed", tmux_pane="%1")
    server.lodes = [lode]
    save_lodes(server.lodes)

    # Connect client and register ownership
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    msg = {"type": "lode_register", "lode_id": "test-id"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Wait for registration
    for _ in range(50):
        if "test-id" in server.lode_clients:
            break
        time.sleep(0.1)

    # Disconnect client
    client.close()

    # Wait for disconnect handling
    for _ in range(50):
        if server.lodes[0]["tmux_pane"] is None:
            break
        time.sleep(0.1)

    # State and status preserved, active set to False
    assert server.lodes[0]["state"] == "error"
    assert server.lodes[0]["status"] == "Something failed"
    assert server.lodes[0]["active"] is False
    assert server.lodes[0]["tmux_pane"] is None


def test_server_handles_ready_state(socket_path, server, temp_config, make_lode):
    """Server accepts 'ready' as a valid state."""
    lode = make_lode(id="test-id", stage="refine", state="completed")
    server.lodes = [lode]
    save_lodes(server.lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    msg = {
        "type": "lode_set_state",
        "lode_id": "test-id",
        "state": "ready",
        "status": "Mill output saved",
    }
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "lode_updated"
    assert response["lode"]["state"] == "ready"
    assert response["lode"]["status"] == "Mill output saved"

    client.close()


def test_auto_spawn_on_disconnect(socket_path, server, temp_config, make_lode):
    """Auto-advance spawns next stage runner on disconnect."""
    lode = make_lode(
        id="test-id",
        state="ready",
        stage="ship",
        status="Refine complete",
        project="my-project",
    )
    server.lodes = [lode]
    save_lodes(server.lodes)

    with (
        patch("hopper.server.find_project") as mock_find,
        patch("hopper.server.spawn_claude", return_value="%33") as mock_spawn,
    ):
        mock_find.return_value = MagicMock(path="/some/path")

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)
        msg = {"type": "lode_register", "lode_id": "test-id"}
        client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

        for _ in range(50):
            if "test-id" in server.lode_clients:
                break
            time.sleep(0.1)

        client.close()

        for _ in range(20):
            time.sleep(0.1)
            if not server.lodes[0]["active"]:
                break

        mock_spawn.assert_called_once_with("test-id", "/some/path", foreground=False)


def test_auto_archive_shipped_on_disconnect(socket_path, server, temp_config, make_lode):
    """Shipped lodes are auto-archived when their client disconnects."""
    lode = make_lode(
        id="test-id",
        stage="shipped",
        state="ready",
        status="Ship complete",
    )
    server.lodes = [lode]
    save_lodes(server.lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)
    msg = {"type": "lode_register", "lode_id": "test-id"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    for _ in range(50):
        if "test-id" in server.lode_clients:
            break
        time.sleep(0.1)

    client.close()

    for _ in range(20):
        time.sleep(0.1)
        if not server.lodes:
            break

    assert server.lodes == []
    assert len(server.archived_lodes) == 1
    assert server.archived_lodes[0]["id"] == "test-id"
    assert "archived_at" in server.archived_lodes[0]

    archived_file = temp_config / "archived.jsonl"
    assert archived_file.exists()
    archived_entries = [
        json.loads(line) for line in archived_file.read_text().splitlines() if line.strip()
    ]
    assert len(archived_entries) == 1
    assert archived_entries[0]["id"] == "test-id"
    assert "archived_at" in archived_entries[0]


def test_lode_unarchive(socket_path, server, temp_config, make_lode):
    """Unarchive moves lode from archived to active and broadcasts."""
    lode = make_lode(id="test-id", stage="mill", state="new")
    lode["archived_at"] = 5000
    server.archived_lodes = [lode]
    save_archived_lodes(server.archived_lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    msg = {"type": "lode_unarchive", "lode_id": "test-id"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    for _ in range(50):
        if server.lodes:
            break
        time.sleep(0.1)

    assert len(server.lodes) == 1
    assert server.lodes[0]["id"] == "test-id"
    assert "archived_at" not in server.lodes[0]
    assert server.archived_lodes == []

    client.close()


def test_cleanup_worktree_on_disconnect_archive(socket_path, server, temp_config, make_lode):
    """Disconnect archive triggers worktree and branch cleanup."""
    lode = make_lode(
        id="test-id",
        stage="shipped",
        state="ready",
        status="Ship complete",
        project="myproject",
        branch="hopper-test-id",
    )
    server.lodes = [lode]
    save_lodes(server.lodes)
    worktree_dir = temp_config / "lodes" / lode["id"] / "worktree"
    worktree_dir.mkdir(parents=True)

    with (
        patch(
            "hopper.server.find_project", return_value=Project(path="/fake/repo", name="myproject")
        ),
        patch("hopper.server.is_dirty", return_value=False),
        patch("hopper.server.remove_worktree") as mock_remove_worktree,
        patch("hopper.server.delete_branch") as mock_delete_branch,
    ):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)
        msg = {"type": "lode_register", "lode_id": "test-id"}
        client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

        for _ in range(50):
            if "test-id" in server.lode_clients:
                break
            time.sleep(0.1)

        client.close()

        for _ in range(20):
            time.sleep(0.1)
            if not server.lodes:
                break

        mock_remove_worktree.assert_called_once_with("/fake/repo", str(worktree_dir))
        mock_delete_branch.assert_called_once_with("/fake/repo", lode["branch"])


def test_no_auto_archive_non_shipped_on_disconnect(socket_path, server, temp_config, make_lode):
    """Non-shipped lodes are not auto-archived on disconnect."""
    lode = make_lode(
        id="test-id",
        stage="ship",
        state="ready",
        status="Ship complete",
    )
    server.lodes = [lode]
    save_lodes(server.lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)
    msg = {"type": "lode_register", "lode_id": "test-id"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    for _ in range(50):
        if "test-id" in server.lode_clients:
            break
        time.sleep(0.1)

    client.close()

    for _ in range(20):
        time.sleep(0.1)
        if not server.lodes[0]["active"]:
            break

    assert len(server.lodes) == 1
    assert server.lodes[0]["id"] == "test-id"
    assert server.archived_lodes == []


def test_auto_spawn_skipped_when_stage_done(socket_path, server, temp_config, make_lode):
    """Auto-advance does not spawn when current stage is already complete."""
    lode = make_lode(
        id="test-id",
        state="ready",
        stage="ship",
        status="Ship complete",
        project="my-project",
    )
    server.lodes = [lode]
    save_lodes(server.lodes)

    with (
        patch("hopper.server.find_project") as mock_find,
        patch("hopper.server.spawn_claude") as mock_spawn,
    ):
        mock_find.return_value = MagicMock(path="/some/path")

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)
        msg = {"type": "lode_register", "lode_id": "test-id"}
        client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

        for _ in range(50):
            if "test-id" in server.lode_clients:
                break
            time.sleep(0.1)

        client.close()

        for _ in range(20):
            time.sleep(0.1)
            if not server.lodes[0]["active"]:
                break

        mock_spawn.assert_not_called()


def test_server_disconnects_stale_client_on_reconnect(socket_path, server, temp_config, make_lode):
    """Server disconnects old client when new client registers for same lode."""
    lode = make_lode(id="test-id")
    server.lodes = [lode]
    save_lodes(server.lodes)

    # First client registers
    client1 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client1.connect(str(socket_path))
    client1.settimeout(2.0)

    msg = {"type": "lode_register", "lode_id": "test-id"}
    client1.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Wait for registration
    for _ in range(50):
        if "test-id" in server.lode_clients:
            break
        time.sleep(0.1)

    old_socket = server.lode_clients["test-id"]

    # Second client registers for same lode
    client2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client2.connect(str(socket_path))
    client2.settimeout(2.0)

    msg = {"type": "lode_register", "lode_id": "test-id"}
    client2.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Wait for re-registration
    for _ in range(50):
        if server.lode_clients.get("test-id") != old_socket:
            break
        time.sleep(0.1)

    # Second client should now own the lode
    assert "test-id" in server.lode_clients
    assert server.lode_clients["test-id"] != old_socket

    client1.close()
    client2.close()


def test_server_handles_lode_set_codex_thread(socket_path, server, temp_config, make_lode):
    """Server handles lode_set_codex_thread message."""
    lode = make_lode(id="test-id", stage="refine", state="running")
    server.lodes = [lode]
    save_lodes(server.lodes)

    # Connect client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Wait for client to be registered
    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    # Send lode_set_codex_thread message
    msg = {
        "type": "lode_set_codex_thread",
        "lode_id": "test-id",
        "codex_thread_id": "codex-uuid-1234",
    }
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Should receive broadcast
    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "lode_updated"
    assert response["lode"]["id"] == "test-id"
    assert response["lode"]["codex_thread_id"] == "codex-uuid-1234"

    # Server's lode should be updated
    assert server.lodes[0]["codex_thread_id"] == "codex-uuid-1234"

    client.close()


def test_server_handles_lode_set_claude_started(socket_path, server, temp_config, make_lode):
    """Server handles lode_set_claude_started message."""
    lode = make_lode(id="test-id", stage="mill", state="running")
    assert lode["claude"]["mill"]["started"] is False
    server.lodes = [lode]
    save_lodes(server.lodes)

    # Connect client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Wait for client to be registered
    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    # Send lode_set_claude_started message
    msg = {
        "type": "lode_set_claude_started",
        "lode_id": "test-id",
        "claude_stage": "mill",
    }
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Should receive broadcast
    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "lode_updated"
    assert response["lode"]["id"] == "test-id"
    assert response["lode"]["claude"]["mill"]["started"] is True

    # Server's lode should be updated
    assert server.lodes[0]["claude"]["mill"]["started"] is True
    # Other stages unchanged
    assert server.lodes[0]["claude"]["refine"]["started"] is False

    client.close()


def test_server_handles_lode_reset_claude_stage(socket_path, server, temp_config, make_lode):
    """Server handles lode_reset_claude_stage message."""
    lode = make_lode(id="test-id", stage="mill", state="running")
    lode["claude"]["mill"]["started"] = True
    old_session_id = lode["claude"]["mill"]["session_id"]
    server.lodes = [lode]
    save_lodes(server.lodes)

    # Connect client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Wait for client to be registered
    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    # Send lode_reset_claude_stage message
    msg = {
        "type": "lode_reset_claude_stage",
        "lode_id": "test-id",
        "claude_stage": "mill",
    }
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Should receive broadcast
    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "lode_updated"
    assert response["lode"]["claude"]["mill"]["started"] is False
    assert response["lode"]["claude"]["mill"]["session_id"] != old_session_id

    # Server's lode should be updated
    assert server.lodes[0]["claude"]["mill"]["started"] is False
    assert server.lodes[0]["claude"]["mill"]["session_id"] != old_session_id
    # Other stages unchanged
    assert server.lodes[0]["claude"]["refine"]["started"] is False

    client.close()


def test_lode_send_feedback_alive_pane_sends_keys(socket_path, make_lode):
    """Alive pane feedback sends text plus Enter and resumes running state."""
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", stage="refine", state="gated", tmux_pane="%1")]
    conn = MagicMock()

    with (
        patch("hopper.server.capture_pane", return_value="prompt ready"),
        patch("hopper.server.paste_buffer", return_value=True) as mock_paste_buffer,
        patch("hopper.server.send_keys", return_value=True) as mock_send_keys,
        patch("hopper.server.time.sleep"),
    ):
        srv._handle_mutation(
            {"type": "lode_send_feedback", "lode_id": "test-id", "text": "Looks good"},
            conn,
        )

    mock_paste_buffer.assert_called_once_with("%1", "Looks good")
    assert [call.args for call in mock_send_keys.call_args_list] == [("%1", "Enter")]
    assert srv.lodes[0]["state"] == "running"
    assert srv.lodes[0]["status"] == "Feedback sent"
    broadcast = srv.broadcast_queue.get_nowait()
    assert broadcast["type"] == "lode_updated"
    response = _decode_mock_response(conn)
    assert response["type"] == "feedback_sent"
    assert response["lode_id"] == "test-id"
    assert response["tmux_pane"] == "%1"
    assert response["submitted"] is True


def test_lode_send_feedback_dead_pane_fails_closed(socket_path, make_lode):
    """Dead pane feedback stays gated and requires an explicit resume."""
    srv = Server(socket_path)
    lode = make_lode(
        id="test-id",
        stage="refine",
        state="gated",
        project="proj",
        tmux_pane="%dead",
    )
    srv.lodes = [lode]
    conn = MagicMock()

    with (
        patch("hopper.server.capture_pane", return_value=None),
        patch("hopper.server.spawn_claude") as mock_spawn,
        patch("hopper.server.paste_buffer", return_value=True) as mock_paste_buffer,
        patch("hopper.server.send_keys", return_value=True) as mock_send_keys,
    ):
        srv._handle_mutation(
            {"type": "lode_send_feedback", "lode_id": "test-id", "text": "Please revise"},
            conn,
        )

    mock_spawn.assert_not_called()
    mock_paste_buffer.assert_not_called()
    mock_send_keys.assert_not_called()
    assert srv.lodes[0]["state"] == "gated"
    assert srv.lodes[0]["status"] == "Feedback blocked: pane unavailable"
    response = _decode_mock_response(conn)
    assert response["type"] == "error"
    assert "hop lode resume test-id" in response["error"]


def test_lode_send_feedback_unverified_paste_remains_gated(socket_path, make_lode):
    """A paste race cannot advance the lode to running."""
    srv = Server(socket_path)
    srv.lodes = [
        make_lode(
            id="test-id",
            stage="refine",
            state="gated",
            project="proj",
            tmux_pane="%1",
        )
    ]
    conn = MagicMock()

    with (
        patch("hopper.server.capture_pane", return_value="prompt ready"),
        patch("hopper.server.paste_buffer", return_value=False) as mock_paste_buffer,
        patch("hopper.server.send_keys", return_value=True) as mock_send_keys,
        patch("hopper.server.time.sleep"),
    ):
        srv._handle_mutation(
            {"type": "lode_send_feedback", "lode_id": "test-id", "text": "Please revise"},
            conn,
        )

    mock_paste_buffer.assert_called_once_with("%1", "Please revise")
    mock_send_keys.assert_not_called()
    assert srv.lodes[0]["state"] == "gated"
    assert srv.lodes[0]["status"] == "Feedback not submitted; gate remains blocked"
    response = _decode_mock_response(conn)
    assert response["type"] == "error"
    assert "gate remains blocked" in response["error"]


def test_lode_send_feedback_pane_disappears_after_paste(socket_path, make_lode):
    """A pane death after paste cannot be mistaken for successful submission."""
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", stage="refine", state="gated", tmux_pane="%1")]
    conn = MagicMock()

    with (
        patch("hopper.server.capture_pane", side_effect=["prompt ready", None]),
        patch("hopper.server.paste_buffer", return_value=True),
        patch("hopper.server.send_keys", return_value=True),
        patch("hopper.server.time.sleep"),
    ):
        srv._handle_mutation(
            {"type": "lode_send_feedback", "lode_id": "test-id", "text": "Please revise"},
            conn,
        )

    assert srv.lodes[0]["state"] == "gated"
    response = _decode_mock_response(conn)
    assert response["type"] == "error"
    assert "gate remains blocked" in response["error"]


def test_lode_send_feedback_missing_lode(socket_path):
    """Missing lode feedback request returns an error response."""
    srv = Server(socket_path)
    conn = MagicMock()

    srv._handle_mutation(
        {"type": "lode_send_feedback", "lode_id": "missing", "text": "feedback"},
        conn,
    )

    response = _decode_mock_response(conn)
    assert response["type"] == "error"
    assert response["error"] == "lode missing not found"


class TestActivityLog:
    def test_activity_log_created_on_start(self, isolate_config, server):
        """Server start creates activity.log with listening message."""
        log_path = isolate_config / "activity.log"
        assert log_path.exists()
        deadline = time.monotonic() + 2
        content = ""
        while "Server listening" not in content and time.monotonic() < deadline:
            content = log_path.read_text()
            time.sleep(0.01)
        assert "Server listening" in content

    def test_lode_mutation_logged(self, isolate_config, server, socket_path, make_lode):
        """Lode state change produces a log line with lode ID and new state."""
        server.lodes = [make_lode(id="test-log")]
        save_lodes(server.lodes)

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)
        try:
            msg = {
                "type": "lode_set_state",
                "lode_id": "test-log",
                "state": "running",
                "status": "doing stuff",
            }
            client.sendall((json.dumps(msg) + "\n").encode("utf-8"))
            client.recv(4096)
        finally:
            client.close()

        time.sleep(0.1)
        log_path = isolate_config / "activity.log"
        content = log_path.read_text()
        assert "test-log" in content
        assert "state=running" in content

    def test_backlog_mutation_logged(self, isolate_config, server, socket_path):
        """Backlog add produces a log line with item ID."""
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)
        try:
            msg = {"type": "backlog_add", "project": "myproj", "description": "do thing"}
            client.sendall((json.dumps(msg) + "\n").encode("utf-8"))
            client.recv(4096)
        finally:
            client.close()

        time.sleep(0.1)
        log_path = isolate_config / "activity.log"
        content = log_path.read_text()
        assert "added project=myproj" in content

    def test_projects_reload(self, isolate_config, server, socket_path):
        """projects_reload reloads project list from disk."""
        # Server starts with empty projects
        assert server.projects == []

        # Send projects_reload message
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)
        try:
            msg = {"type": "projects_reload"}
            client.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        finally:
            client.close()

        time.sleep(0.1)
        # Projects reloaded (empty since no config, but handler ran)
        log_path = isolate_config / "activity.log"
        content = log_path.read_text()
        assert "Projects and lodes reloaded" in content

    def test_projects_reload_refreshes_project_order_after_touch(self, socket_path):
        """projects_reload updates in-memory project recency order after touch_project."""
        save_config(
            {
                "projects": [
                    {"path": "/tmp/A", "name": "A", "disabled": False, "last_used_at": 200},
                    {"path": "/tmp/B", "name": "B", "disabled": False, "last_used_at": 100},
                ]
            }
        )

        srv = Server(socket_path)
        thread = threading.Thread(target=srv.start, daemon=True)
        thread.start()

        try:
            assert srv.ready.wait(5), "Server did not start"

            assert [project.name for project in srv.projects] == ["A", "B"]

            touch_project("B")
            srv.enqueue({"type": "projects_reload"})

            for _ in range(50):
                if [project.name for project in srv.projects] == ["B", "A"]:
                    break
                time.sleep(0.02)

            assert [project.name for project in srv.projects] == ["B", "A"]
        finally:
            srv.stop()
            thread.join(timeout=2)

    def test_server_stop_closes_handler(self, isolate_config, socket_path):
        """Server stop removes and closes the file handler."""
        srv = Server(socket_path)
        thread = threading.Thread(target=srv.start, daemon=True)
        thread.start()

        assert srv.ready.wait(5), "Server did not start"

        assert srv._log_handler is not None
        handler = srv._log_handler
        stream = handler.stream

        srv.stop()
        thread.join(timeout=2)

        assert srv._log_handler is None
        assert stream is None or stream.closed
