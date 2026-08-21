# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Stage-driver protocol tests through Hopper's real Unix socket boundary."""

import json
import socket
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hopper import actions
from hopper.client import HopperConnection, connect, send_message
from hopper.lodes import lode_stage_session, save_lodes
from hopper.runner import BaseRunner, StageDriverProtocol, classify_stage_driver_protocol
from hopper.server import Server

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "stage-driver-protocol"
GENERATION = "11111111111111111111111111111111"


@pytest.fixture
def socket_path(tmp_path):
    """Use one isolated Unix socket for each real protocol test."""
    return tmp_path / "stage-driver.sock"


def _start_protocol_server(socket_path, release_server_lock):
    """Start an isolated real server for one protocol test."""
    server = Server(socket_path)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    assert server.ready.wait(3), server.startup_error or "server did not become ready"
    return server, thread, release_server_lock


def _stop_protocol_server(server, thread, release_server_lock):
    """Stop a real protocol server and release its process-local singleton lock."""
    server.stop()
    thread.join(timeout=3)
    assert not thread.is_alive()
    release_server_lock(server)


def _fixture(name: str) -> dict:
    """Load one frozen wire object without rebuilding it in-process."""
    return json.loads((FIXTURE_DIR / name).read_text())


def _recv_json_line(client: socket.socket) -> dict:
    """Receive one JSONL message from the real server socket."""
    buffer = b""
    while b"\n" not in buffer:
        buffer += client.recv(4096)
    line, _separator, _rest = buffer.partition(b"\n")
    return json.loads(line.decode("utf-8"))


def _serve_frozen_connected_response(socket_path, response: dict) -> tuple[threading.Thread, dict]:
    """Serve one frozen connected response over a real legacy-shaped socket."""
    received: dict = {}
    ready = threading.Event()

    def serve() -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(socket_path))
            listener.listen(1)
            ready.set()
            conn, _address = listener.accept()
            with conn:
                request = _recv_json_line(conn)
                received["request"] = request
                matched_response = dict(response, exchange_id=request["exchange_id"])
                conn.sendall((json.dumps(matched_response) + "\n").encode("utf-8"))
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(2), "legacy socket did not start"
    return thread, received


def _wait_for(predicate) -> None:
    """Wait briefly for the server event loop without relying on a mocked handler."""
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline, "server did not apply the socket mutation"
        time.sleep(0.01)


def _current_binding_runner(lode: dict, socket_path, *, stage: str | None = None) -> BaseRunner:
    """Build a first-run current-protocol runner backed by a real socket connection."""
    runner = BaseRunner(
        lode["id"],
        socket_path,
        run_generation=lode["run_generation"],
    )
    runner._claude_stage = stage or lode["stage"]
    session = lode_stage_session(lode, runner._claude_stage)
    runner.is_first_run = True
    runner._stage_protocol = StageDriverProtocol.CURRENT
    runner.claude_session_id = session["provider_session_id"]
    runner.launch_id = session["launch_id"]
    connection = HopperConnection(socket_path, run_generation=lode["run_generation"])
    runner.connection = connection
    connected = threading.Event()
    connection.start(callback=runner._on_server_message, on_connect=connected.set)
    assert connected.wait(2), "runner connection did not reach the real server"
    return runner


def _attempt_current_binding_launch(runner: BaseRunner) -> tuple[tuple[int, str | None], MagicMock]:
    """Run a contained provider launch while the binding remains real-socket based."""
    proc = MagicMock(returncode=0, stderr=None)
    proc.poll.return_value = 0
    with (
        patch.object(runner, "_build_command", return_value=(["claude"], "/repo")),
        patch("hopper.runner.trust_claude_workspace"),
        patch("hopper.runner.subprocess.Popen", return_value=proc) as launch,
        patch.object(runner, "_start_monitor"),
    ):
        result = runner._run_claude()
    return result, launch


def test_frozen_connect_request_reaches_real_server_and_current_marker(
    socket_path, release_server_lock
):
    server, thread, release = _start_protocol_server(socket_path, release_server_lock)
    request = _fixture("legacy-connected-request.json")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    try:
        client.sendall((json.dumps(request) + "\n").encode("utf-8"))
        response = _recv_json_line(client)
    finally:
        client.close()

    try:
        assert response["type"] == "connected"
        assert response["exchange_id"] == request["exchange_id"]
        assert response["stage_driver_capabilities"] == {"version": 1, "drivers": ["claude"]}
    finally:
        _stop_protocol_server(server, thread, release)


def test_frozen_markerless_connected_response_is_bounded_legacy_claude_shape(tmp_path):
    response = _fixture("legacy-connected-response.json")
    thread, received = _serve_frozen_connected_response(tmp_path / "legacy.sock", response)
    connected = connect(tmp_path / "legacy.sock", lode_id="legacyab")
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert received["request"]["type"] == "connect"
    assert received["request"]["lode_id"] == "legacyab"
    assert connected is not None
    assert classify_stage_driver_protocol(connected, "claude") is StageDriverProtocol.LEGACY_CLAUDE
    assert classify_stage_driver_protocol(None, "claude") is StageDriverProtocol.UNKNOWN

    missing = dict(response)
    missing.pop("exchange_id")
    assert classify_stage_driver_protocol(missing, "claude") is StageDriverProtocol.UNKNOWN

    malformed = dict(response)
    malformed["ts"] = "not-an-int"
    assert classify_stage_driver_protocol(malformed, "claude") is StageDriverProtocol.UNKNOWN
    contradictory = dict(response, stage_driver_capabilities={"version": 1, "drivers": []})
    assert classify_stage_driver_protocol(contradictory, "claude") is StageDriverProtocol.UNKNOWN
    assert classify_stage_driver_protocol(response, "codex") is StageDriverProtocol.UNKNOWN


def test_frozen_legacy_start_mutation_updates_only_current_stage_over_real_socket(
    socket_path, make_lode, release_server_lock
):
    server, thread, release = _start_protocol_server(socket_path, release_server_lock)
    lode = make_lode(
        id="legacyab",
        stage="mill",
        active=True,
        run_generation=GENERATION,
    )
    server.lodes[:] = [lode]
    save_lodes(server.lodes)
    message = _fixture("legacy-lode-set-claude-started.json")

    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        try:
            client.sendall((json.dumps(message) + "\n").encode("utf-8"))
            response = _recv_json_line(client)
        finally:
            client.close()

        assert response["type"] == "lode_updated"
        assert lode_stage_session(server.lodes[0], "mill")["started"] is True
        assert lode_stage_session(server.lodes[0], "refine")["started"] is False
        assert lode_stage_session(server.lodes[0], "ship")["started"] is False
    finally:
        _stop_protocol_server(server, thread, release)


def test_fenced_binding_acknowledges_exact_duplicate_and_refuses_conflict_over_socket(
    socket_path, make_lode, release_server_lock
):
    server, thread, release = _start_protocol_server(socket_path, release_server_lock)
    lode = make_lode(id="boundabc", stage="mill", active=True, run_generation="generation-1")
    server.lodes[:] = [lode]
    save_lodes(server.lodes)
    session = lode_stage_session(lode, "mill")
    message = {
        "type": "lode_bind_stage_session",
        "lode_id": lode["id"],
        "driver": "claude",
        "stage": "mill",
        "launch_id": session["launch_id"],
        "provider_session_id": session["provider_session_id"],
        "run_generation": "generation-1",
        "ack_requested": True,
        "ts": 1,
    }

    try:
        first = send_message(socket_path, message, wait_for_response=True)
        duplicate = send_message(socket_path, message, wait_for_response=True)
        conflicting = dict(message, provider_session_id="44444444-4444-4444-4444-444444444444")
        refused = send_message(socket_path, conflicting, wait_for_response=True)

        assert first["accepted"] is True and first["reason"] == "committed"
        assert duplicate["accepted"] is True and duplicate["reason"] == "committed"
        assert refused["accepted"] is False and refused["reason"] == "stage_binding_conflict"
        current = lode_stage_session(server.lodes[0], "mill")
        assert current["started"] is True
        assert current["provider_session_id"] == session["provider_session_id"]
        assert server.lodes[0]["status"].startswith("protocol error: ")
    finally:
        _stop_protocol_server(server, thread, release)


def test_fenced_binding_activity_log_records_identity_without_request_bodies(
    isolate_config, socket_path, make_lode, release_server_lock
):
    server, thread, release = _start_protocol_server(socket_path, release_server_lock)
    lode = make_lode(id="diagbind", stage="mill", active=True, run_generation="generation-1")
    server.lodes[:] = [lode]
    save_lodes(server.lodes)
    session = lode_stage_session(lode, "mill")
    message = {
        "type": "lode_bind_stage_session",
        "lode_id": lode["id"],
        "driver": "claude",
        "stage": "mill",
        "launch_id": session["launch_id"],
        "provider_session_id": session["provider_session_id"],
        "run_generation": "generation-1",
        "ack_requested": True,
        "ts": 1,
    }

    try:
        response = send_message(socket_path, message, wait_for_response=True)
        assert response["accepted"] is True

        log_path = isolate_config / "activity.log"
        _wait_for(lambda: session["launch_id"] in log_path.read_text())
        content = log_path.read_text()
        for field, value in {
            "lode": lode["id"],
            "driver": "claude",
            "stage": "mill",
            "launch_id": session["launch_id"],
            "provider_session_id": session["provider_session_id"],
            "run_generation": "generation-1",
        }.items():
            assert f"{field}={value}" in content
        assert "secret prompt body" not in content
        assert "gate body" not in content
    finally:
        _stop_protocol_server(server, thread, release)


def test_pending_stage_binding_never_launches_or_publishes_running_over_socket(
    monkeypatch, socket_path, make_lode, release_server_lock
):
    server, thread, release = _start_protocol_server(socket_path, release_server_lock)
    lode = make_lode(
        id="pendingab", stage="mill", state="ready", active=True, run_generation="generation-1"
    )
    server.lodes[:] = [lode]
    save_lodes(server.lodes)
    original_handle_mutation = server._handle_mutation

    def hold_binding(message, conn):
        if message.get("type") == "lode_bind_stage_session":
            return
        original_handle_mutation(message, conn)

    server._handle_mutation = hold_binding
    monkeypatch.setattr("hopper.runner.DURABLE_CONFIRMATION_TIMEOUT_SEC", 0.3)
    runner = _current_binding_runner(lode, socket_path)
    try:
        result, launch = _attempt_current_binding_launch(runner)

        assert result[0] == 1
        launch.assert_not_called()
        assert server.lodes[0]["state"] == "ready"
        assert lode_stage_session(server.lodes[0], "mill")["started"] is False
    finally:
        runner.connection.stop()
        server._handle_mutation = original_handle_mutation
        _stop_protocol_server(server, thread, release)


def test_refused_stage_binding_never_launches_or_publishes_running_over_socket(
    socket_path, make_lode, release_server_lock
):
    server, thread, release = _start_protocol_server(socket_path, release_server_lock)
    lode = make_lode(
        id="refusedab", stage="mill", state="ready", active=True, run_generation="generation-1"
    )
    server.lodes[:] = [lode]
    save_lodes(server.lodes)
    runner = _current_binding_runner(lode, socket_path, stage="refine")
    try:
        result, launch = _attempt_current_binding_launch(runner)

        assert result[0] == 1
        launch.assert_not_called()
        assert server.lodes[0]["state"] == "ready"
        assert lode_stage_session(server.lodes[0], "mill")["started"] is False
    finally:
        runner.connection.stop()
        _stop_protocol_server(server, thread, release)


def test_unknown_stage_binding_never_launches_or_publishes_running_over_socket(
    monkeypatch, socket_path, make_lode, release_server_lock
):
    server, thread, release = _start_protocol_server(socket_path, release_server_lock)
    lode = make_lode(
        id="unknownab", stage="mill", state="ready", active=True, run_generation="generation-1"
    )
    server.lodes[:] = [lode]
    save_lodes(server.lodes)
    original_send_response = server._send_response

    def lose_binding_ack(conn, response):
        if (
            response.get("type") == "mutation_ack"
            and response.get("mutation_type") == "lode_bind_stage_session"
        ):
            return
        original_send_response(conn, response)

    server._send_response = lose_binding_ack
    monkeypatch.setattr("hopper.runner.DURABLE_CONFIRMATION_TIMEOUT_SEC", 0.3)
    runner = _current_binding_runner(lode, socket_path)
    try:
        with patch("hopper.runner.load_lodes", side_effect=OSError("durability unavailable")):
            result, launch = _attempt_current_binding_launch(runner)

        assert result[0] == 1
        launch.assert_not_called()
        assert server.lodes[0]["state"] == "ready"
    finally:
        runner.connection.stop()
        server._send_response = original_send_response
        _stop_protocol_server(server, thread, release)


def test_lost_binding_ack_requires_exact_durable_reconciliation_before_running(
    monkeypatch, socket_path, make_lode, release_server_lock
):
    server, thread, release = _start_protocol_server(socket_path, release_server_lock)
    lode = make_lode(
        id="reconcileab", stage="mill", state="ready", active=True, run_generation="generation-1"
    )
    server.lodes[:] = [lode]
    save_lodes(server.lodes)
    original_send_response = server._send_response

    def lose_binding_ack(conn, response):
        if (
            response.get("type") == "mutation_ack"
            and response.get("mutation_type") == "lode_bind_stage_session"
        ):
            return
        original_send_response(conn, response)

    server._send_response = lose_binding_ack
    monkeypatch.setattr("hopper.runner.DURABLE_CONFIRMATION_TIMEOUT_SEC", 2.0)
    runner = _current_binding_runner(lode, socket_path)
    try:
        result, launch = _attempt_current_binding_launch(runner)

        assert result == (0, None)
        launch.assert_called_once()
        _wait_for(lambda: server.lodes[0]["state"] == "running")
        assert lode_stage_session(server.lodes[0], "mill")["start_attempt"] == {
            "driver": "claude",
            "stage": "mill",
            "launch_id": lode_stage_session(lode, "mill")["launch_id"],
            "provider_session_id": lode_stage_session(lode, "mill")["provider_session_id"],
            "run_generation": "generation-1",
            "outcome": "committed",
        }
    finally:
        runner.connection.stop()
        server._send_response = original_send_response
        _stop_protocol_server(server, thread, release)


def test_legacy_start_inside_restart_window_is_refused_without_rebinding_over_socket(
    socket_path, make_lode, release_server_lock
):
    server, thread, release = _start_protocol_server(socket_path, release_server_lock)
    lode = make_lode(
        id="legacyab",
        stage="mill",
        active=True,
        run_generation=GENERATION,
    )
    record = actions.new_pending_action(
        lode_id=lode["id"],
        stage="mill",
        expected_generation=GENERATION,
        action_type="restart",
        target_disposition="replacement_spawned",
        force_consent=True,
        already_empty=True,
    )
    actions.write_pending_action(record)
    lode["pending_action"] = actions.pending_action_projection(record)
    server.lodes[:] = [lode]
    save_lodes(server.lodes)
    before = lode_stage_session(lode, "mill").copy()
    message = _fixture("legacy-lode-set-claude-started.json")
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        try:
            client.sendall((json.dumps(message) + "\n").encode("utf-8"))
            response = _recv_json_line(client)
        finally:
            client.close()

        assert response["type"] == "lode_updated"
        assert lode_stage_session(server.lodes[0], "mill") == before
        assert server.lodes[0]["status"].startswith("protocol error: expected_teardown")
    finally:
        _stop_protocol_server(server, thread, release)


def test_protocol_error_blocks_late_running_update_from_same_generation(
    socket_path, make_lode, release_server_lock
):
    server, thread, release = _start_protocol_server(socket_path, release_server_lock)
    lode = make_lode(id="errorabc", stage="mill", active=True, run_generation="generation-2")
    server.lodes[:] = [lode]
    save_lodes(server.lodes)
    session = lode_stage_session(lode, "mill")
    wrong = {
        "type": "lode_bind_stage_session",
        "lode_id": lode["id"],
        "driver": "claude",
        "stage": "refine",
        "launch_id": session["launch_id"],
        "provider_session_id": session["provider_session_id"],
        "run_generation": "generation-2",
        "ack_requested": True,
        "ts": 1,
    }
    try:
        refused = send_message(socket_path, wrong, wait_for_response=True)
        running = send_message(
            socket_path,
            {
                "type": "lode_set_state",
                "lode_id": lode["id"],
                "state": "running",
                "status": "Claude running",
                "run_generation": "generation-2",
                "ack_requested": True,
                "ts": 2,
            },
            wait_for_response=True,
        )

        assert refused["accepted"] is False
        assert running["accepted"] is False and running["reason"] == "protocol_error"
        assert server.lodes[0]["protocol_error"]
        assert server.lodes[0]["status"].startswith("protocol error: ")
    finally:
        _stop_protocol_server(server, thread, release)


def test_non_claude_wire_creation_refuses_before_a_lode_or_processor_exists(
    socket_path, release_server_lock
):
    server, thread, release = _start_protocol_server(socket_path, release_server_lock)
    try:
        with patch("hopper.server.spawn_lode_processor") as spawn:
            response = send_message(
                socket_path,
                {
                    "type": "lode_create",
                    "project": "project-one",
                    "scope": "internal coverage only",
                    "spawn": True,
                    "coder_provider": "codex",
                    "driver": "codex",
                },
                wait_for_response=True,
            )

        assert response["type"] == "error"
        assert server.lodes == []
        spawn.assert_not_called()
    finally:
        _stop_protocol_server(server, thread, release)
