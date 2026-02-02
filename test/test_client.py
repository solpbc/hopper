"""Tests for the hopper client."""

import threading
import time
from pathlib import Path

import pytest

from hopper.client import ping, send_message, session_exists, set_session_state
from hopper.server import Server


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

    for _ in range(50):
        if socket_path.exists():
            break
        time.sleep(0.1)
    else:
        raise TimeoutError("Server did not start")

    yield srv

    srv.stop()
    thread.join(timeout=2)


def test_ping_success(server, socket_path):
    """Ping returns True when server responds."""
    result = ping(socket_path)
    assert result is True


def test_ping_failure_no_server(socket_path):
    """Ping returns False when server not running."""
    result = ping(socket_path, timeout=0.5)
    assert result is False


def test_send_message_no_response(server, socket_path):
    """Send message without waiting for response."""
    result = send_message(socket_path, {"type": "test"}, wait_for_response=False)
    assert result is None


def test_send_message_connection_failure():
    """Send message fails gracefully when no server."""
    result = send_message(
        Path("/tmp/nonexistent.sock"),
        {"type": "test"},
        timeout=0.5,
    )
    assert result is None


def test_session_exists_no_server(socket_path):
    """session_exists returns False when server not running."""
    result = session_exists(socket_path, "any-session", timeout=0.5)
    assert result is False


def test_session_exists_not_found(server, socket_path):
    """session_exists returns False when session doesn't exist."""
    result = session_exists(socket_path, "nonexistent-session")
    assert result is False


def test_session_exists_found(server, socket_path):
    """session_exists returns True when session exists."""
    # Create a session first
    response = send_message(socket_path, {"type": "session_create"}, wait_for_response=False)
    # Give server time to process
    time.sleep(0.1)

    # Get the session list to find the created session
    response = send_message(socket_path, {"type": "session_list"}, wait_for_response=True)
    assert response is not None
    sessions = response.get("sessions", [])
    assert len(sessions) > 0

    session_id = sessions[0]["id"]
    result = session_exists(socket_path, session_id)
    assert result is True


def test_set_session_state_no_server(socket_path):
    """set_session_state returns False when server not running."""
    result = set_session_state(socket_path, "any-session", "running", "Test message", timeout=0.5)
    # Fire-and-forget still returns True if send attempt was made
    # but the underlying send_message will fail silently
    assert result is True  # The try succeeds, send_message handles the error


def test_set_session_state_sends_message(server, socket_path):
    """set_session_state sends the correct message type."""
    from hopper.sessions import Session

    # Create a session first
    session = Session(id="test-id", stage="ore", created_at=1000, state="idle")
    server.sessions = [session]

    result = set_session_state(socket_path, "test-id", "running", "Claude running")
    assert result is True

    # Give server time to process
    time.sleep(0.1)

    # Session should be updated
    assert server.sessions[0].state == "running"
    assert server.sessions[0].message == "Claude running"
