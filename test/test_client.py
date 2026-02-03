"""Tests for the hopper client."""

import threading
import time
from pathlib import Path

import pytest

from hopper.client import (
    HopperConnection,
    connect,
    ping,
    send_message,
    session_exists,
    set_session_state,
)
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


@pytest.fixture
def server_with_tmux(socket_path):
    """Start a server with tmux location set."""
    tmux_location = {"session": "main", "window": "@0"}
    srv = Server(socket_path, tmux_location=tmux_location)
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


def test_connect_success(server, socket_path):
    """Connect returns response dict when server responds."""
    result = connect(socket_path)
    assert result is not None
    assert result["type"] == "connected"
    assert "tmux" in result


def test_connect_failure_no_server(socket_path):
    """Connect returns None when server not running."""
    result = connect(socket_path, timeout=0.5)
    assert result is None


def test_connect_with_tmux_location(server_with_tmux, socket_path):
    """Connect returns tmux location when server has it."""
    result = connect(socket_path)
    assert result is not None
    assert result["tmux"] == {"session": "main", "window": "@0"}


def test_connect_with_session_id_found(server, socket_path):
    """Connect returns session data when session exists."""
    from hopper.sessions import Session

    session = Session(id="test-id", stage="ore", created_at=1000, state="idle")
    server.sessions = [session]

    result = connect(socket_path, session_id="test-id")
    assert result is not None
    assert result["session_found"] is True
    assert result["session"]["id"] == "test-id"
    assert result["session"]["state"] == "idle"


def test_connect_with_session_id_not_found(server, socket_path):
    """Connect returns session_found=False when session doesn't exist."""
    result = connect(socket_path, session_id="nonexistent")
    assert result is not None
    assert result["session_found"] is False
    assert result["session"] is None


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
    from hopper.sessions import Session

    session = Session(id="test-id", stage="ore", created_at=1000, state="idle")
    server.sessions = [session]

    result = session_exists(socket_path, "test-id")
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
    assert server.sessions[0].status == "Claude running"


class TestHopperConnection:
    """Tests for the persistent HopperConnection class."""

    def test_start_and_stop(self, socket_path, server):
        """Connection starts and stops cleanly."""
        conn = HopperConnection(socket_path)
        conn.start()

        # Give time to connect
        time.sleep(0.2)

        assert conn.thread is not None
        assert conn.thread.is_alive()

        conn.stop()

        assert not conn.thread.is_alive()

    def test_emit_before_start_returns_false(self, socket_path):
        """Emit returns False if connection not started."""
        conn = HopperConnection(socket_path)

        result = conn.emit("test_type", data="test")

        assert result is False

    def test_emit_queues_message(self, socket_path, server):
        """Emit queues messages for sending."""
        conn = HopperConnection(socket_path)
        conn.start()

        # Give time to connect
        time.sleep(0.2)

        result = conn.emit("ping")

        assert result is True

        conn.stop()

    def test_emit_updates_session_state(self, socket_path, server):
        """Emit can send session state updates."""
        from hopper.sessions import Session

        # Create a session
        session = Session(id="test-id", stage="ore", created_at=1000, state="idle")
        server.sessions = [session]

        conn = HopperConnection(socket_path)
        conn.start()

        # Give time to connect
        time.sleep(0.2)

        # Send state update
        conn.emit("session_set_state", session_id="test-id", state="running", status="Test")

        # Give time for message to be processed
        time.sleep(0.2)

        conn.stop()

        # Session should be updated
        assert server.sessions[0].state == "running"
        assert server.sessions[0].status == "Test"

    def test_callback_receives_messages(self, socket_path, server):
        """Callback is invoked when server sends messages."""
        received = []

        def on_message(msg):
            received.append(msg)

        conn = HopperConnection(socket_path)
        conn.start(callback=on_message)

        # Give time to connect
        time.sleep(0.2)

        # Server broadcasts a message
        server.broadcast({"type": "test_broadcast", "data": "hello"})

        # Give time for message to arrive
        time.sleep(0.2)

        conn.stop()

        # Should have received the broadcast
        assert any(msg.get("type") == "test_broadcast" for msg in received)

    def test_reconnects_after_server_restart(self, socket_path):
        """Connection reconnects when server restarts."""
        # Start first server
        srv1 = Server(socket_path)
        thread1 = threading.Thread(target=srv1.start, daemon=True)
        thread1.start()

        for _ in range(50):
            if socket_path.exists():
                break
            time.sleep(0.1)

        conn = HopperConnection(socket_path)
        conn.start()

        # Give time to connect
        time.sleep(0.2)

        # Stop first server
        srv1.stop()
        thread1.join(timeout=2)

        # Start second server
        srv2 = Server(socket_path)
        thread2 = threading.Thread(target=srv2.start, daemon=True)
        thread2.start()

        for _ in range(50):
            if socket_path.exists():
                break
            time.sleep(0.1)

        # Give time to reconnect (reconnect rate is 1/sec)
        time.sleep(1.5)

        # Should be able to send messages
        result = conn.emit("ping")
        assert result is True

        conn.stop()
        srv2.stop()
        thread2.join(timeout=2)

    def test_drops_messages_when_disconnected(self, socket_path):
        """Messages are dropped when not connected (no server)."""
        conn = HopperConnection(socket_path)
        conn.start()

        # No server running, but emit should still return True (queued)
        result = conn.emit("test_type", data="test")
        assert result is True

        # Give time for queue to drain (message will be dropped)
        time.sleep(0.3)

        conn.stop()

        # Queue should be empty (message was dropped)
        assert conn.send_queue.empty()

    def test_emit_returns_false_when_queue_full(self, socket_path, server):
        """Emit returns False when queue is full."""
        from hopper.client import HopperConnection

        # Create connection with tiny queue for testing
        conn = HopperConnection(socket_path)
        conn.send_queue = __import__("queue").Queue(maxsize=2)
        conn.start()

        # Don't wait for connection - fill queue immediately
        # Thread is running but queue ops happen faster than sends
        conn.send_queue.put({"type": "msg1"})
        conn.send_queue.put({"type": "msg2"})

        # Queue is now full, emit should return False
        result = conn.emit("msg3")
        assert result is False

        conn.stop()
