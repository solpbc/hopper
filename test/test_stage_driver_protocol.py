# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Stage-driver protocol tests through Hopper's real Unix socket boundary."""

import json
import socket
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hopper import actions
from hopper.client import send_message
from hopper.lodes import lode_stage_session, save_lodes
from hopper.runner import StageDriverProtocol, classify_stage_driver_protocol
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


def _wait_for(predicate) -> None:
    """Wait briefly for the server event loop without relying on a mocked handler."""
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline, "server did not apply the socket mutation"
        time.sleep(0.01)


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


def test_frozen_markerless_connected_response_is_bounded_legacy_claude_shape():
    response = _fixture("legacy-connected-response.json")
    assert classify_stage_driver_protocol(response, "claude") is StageDriverProtocol.LEGACY_CLAUDE
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
        assert "interactive-stage driver is unavailable" in response["error"]
        assert server.lodes == []
        spawn.assert_not_called()
    finally:
        _stop_protocol_server(server, thread, release)
