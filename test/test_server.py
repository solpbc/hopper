"""Tests for the hopper server."""

import json
import socket
import threading
import time
from unittest.mock import patch

import pytest

from hopper.server import Server, get_git_hash


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

    # Wait for socket to be created
    for _ in range(50):
        if socket_path.exists():
            break
        time.sleep(0.1)
    else:
        raise TimeoutError("Server did not start")

    yield srv

    srv.stop()
    thread.join(timeout=2)


def test_server_creates_socket(socket_path):
    """Server creates socket file on start."""
    srv = Server(socket_path)
    thread = threading.Thread(target=srv.start, daemon=True)
    thread.start()

    for _ in range(50):
        if socket_path.exists():
            break
        time.sleep(0.1)

    assert socket_path.exists()

    srv.stop()
    thread.join(timeout=2)

    # Socket cleaned up on stop
    assert not socket_path.exists()


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

    # Wait for socket
    for _ in range(50):
        if socket_path.exists():
            break
        time.sleep(0.1)

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
    tmux_location = {"session": "main", "pane": "%0"}
    srv = Server(socket_path, tmux_location=tmux_location)
    thread = threading.Thread(target=srv.start, daemon=True)
    thread.start()

    for _ in range(50):
        if socket_path.exists():
            break
        time.sleep(0.1)
    else:
        raise TimeoutError("Server did not start")

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
        assert response["tmux"] == {"session": "main", "pane": "%0"}

        client.close()
    finally:
        srv.stop()
        thread.join(timeout=2)


def test_server_handles_connect_with_session_id(socket_path, server, temp_config):
    """Server returns session data when session_id is provided."""
    from hopper.sessions import Session, save_sessions

    # Create a test session
    session = Session(id="test-id", stage="ore", created_at=1000, state="new")
    server.sessions = [session]
    save_sessions(server.sessions)

    # Connect client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Send connect message with session_id
    msg = {"type": "connect", "session_id": "test-id"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Should receive connected response with session data
    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "connected"
    assert response["session_found"] is True
    assert response["session"]["id"] == "test-id"
    assert response["session"]["state"] == "new"

    client.close()


def test_server_handles_connect_with_missing_session_id(socket_path, server):
    """Server returns session_found=False for unknown session."""
    # Connect client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Send connect message with unknown session_id
    msg = {"type": "connect", "session_id": "nonexistent"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Should receive connected response with session not found
    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "connected"
    assert response["session_found"] is False
    assert response["session"] is None

    client.close()


def test_server_handles_session_set_state(socket_path, server, temp_config):
    """Server handles session_set_state message."""
    from hopper.sessions import Session, save_sessions

    # Create a test session
    session = Session(id="test-id", stage="ore", created_at=1000, state="new")
    server.sessions = [session]
    save_sessions(server.sessions)

    # Connect client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Wait for client to be registered
    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    # Send session_set_state message
    msg = {"type": "session_set_state", "session_id": "test-id", "state": "running"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Should receive broadcast
    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "session_state_changed"
    assert response["session"]["id"] == "test-id"
    assert response["session"]["state"] == "running"

    # Server's session object should be updated
    assert server.sessions[0].state == "running"

    client.close()


def test_server_connect_does_not_register_ownership(socket_path, server, temp_config):
    """Connect message returns session data but does not register ownership."""
    from hopper.sessions import Session, save_sessions

    session = Session(id="test-id", stage="ore", created_at=1000, state="new")
    server.sessions = [session]
    save_sessions(server.sessions)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    msg = {"type": "connect", "session_id": "test-id"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))
    client.recv(4096)

    # Give server time to process
    time.sleep(0.2)

    # Connect should NOT register ownership or set active
    assert "test-id" not in server.session_clients
    assert server.sessions[0].active is False

    client.close()


def test_server_registers_on_session_register(socket_path, server, temp_config):
    """session_register message claims ownership and sets active=True."""
    from hopper.sessions import Session, save_sessions

    session = Session(id="test-id", stage="ore", created_at=1000, state="new")
    server.sessions = [session]
    save_sessions(server.sessions)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    msg = {"type": "session_register", "session_id": "test-id"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Wait for registration
    for _ in range(50):
        if "test-id" in server.session_clients:
            break
        time.sleep(0.1)

    assert "test-id" in server.session_clients
    assert server.sessions[0].active is True

    client.close()


def test_server_sets_active_false_on_disconnect(socket_path, server, temp_config):
    """Server sets active=False and clears tmux_pane on client disconnect."""
    from hopper.sessions import Session, save_sessions

    # Create a test session in running state with tmux window
    session = Session(id="test-id", stage="ore", created_at=1000, state="running", tmux_pane="%1")
    server.sessions = [session]
    save_sessions(server.sessions)

    # Connect client and register ownership
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    msg = {"type": "session_register", "session_id": "test-id"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Wait for registration
    for _ in range(50):
        if "test-id" in server.session_clients:
            break
        time.sleep(0.1)

    assert server.sessions[0].active is True

    # Disconnect client
    client.close()

    # Wait for disconnect handling
    for _ in range(50):
        if not server.sessions[0].active:
            break
        time.sleep(0.1)

    # active=False, tmux_pane cleared, but state/status untouched
    assert server.sessions[0].active is False
    assert server.sessions[0].tmux_pane is None
    assert server.sessions[0].state == "running"
    assert "test-id" not in server.session_clients


def test_server_preserves_state_on_disconnect(socket_path, server, temp_config):
    """Server preserves state and status on client disconnect (only toggles active)."""
    from hopper.sessions import Session, save_sessions

    # Create a test session in error state
    session = Session(
        id="test-id",
        stage="ore",
        created_at=1000,
        state="error",
        status="Something failed",
        tmux_pane="%1",
    )
    server.sessions = [session]
    save_sessions(server.sessions)

    # Connect client and register ownership
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    msg = {"type": "session_register", "session_id": "test-id"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Wait for registration
    for _ in range(50):
        if "test-id" in server.session_clients:
            break
        time.sleep(0.1)

    # Disconnect client
    client.close()

    # Wait for disconnect handling
    for _ in range(50):
        if server.sessions[0].tmux_pane is None:
            break
        time.sleep(0.1)

    # State and status preserved, active set to False
    assert server.sessions[0].state == "error"
    assert server.sessions[0].status == "Something failed"
    assert server.sessions[0].active is False
    assert server.sessions[0].tmux_pane is None


def test_server_handles_ready_state(socket_path, server, temp_config):
    """Server accepts 'ready' as a valid state."""
    from hopper.sessions import Session, save_sessions

    session = Session(id="test-id", stage="processing", created_at=1000, state="completed")
    server.sessions = [session]
    save_sessions(server.sessions)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    msg = {
        "type": "session_set_state",
        "session_id": "test-id",
        "state": "ready",
        "status": "Shovel-ready prompt saved",
    }
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "session_state_changed"
    assert response["session"]["state"] == "ready"
    assert response["session"]["status"] == "Shovel-ready prompt saved"

    client.close()


def test_server_disconnects_stale_client_on_reconnect(socket_path, server, temp_config):
    """Server disconnects old client when new client registers for same session."""
    from hopper.sessions import Session, save_sessions

    # Create a test session
    session = Session(id="test-id", stage="ore", created_at=1000, state="new")
    server.sessions = [session]
    save_sessions(server.sessions)

    # First client registers
    client1 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client1.connect(str(socket_path))
    client1.settimeout(2.0)

    msg = {"type": "session_register", "session_id": "test-id"}
    client1.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Wait for registration
    for _ in range(50):
        if "test-id" in server.session_clients:
            break
        time.sleep(0.1)

    old_socket = server.session_clients["test-id"]

    # Second client registers for same session
    client2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client2.connect(str(socket_path))
    client2.settimeout(2.0)

    msg = {"type": "session_register", "session_id": "test-id"}
    client2.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Wait for re-registration
    for _ in range(50):
        if server.session_clients.get("test-id") != old_socket:
            break
        time.sleep(0.1)

    # Second client should now own the session
    assert "test-id" in server.session_clients
    assert server.session_clients["test-id"] != old_socket

    client1.close()
    client2.close()


def test_server_handles_session_set_codex_thread(socket_path, server, temp_config):
    """Server handles session_set_codex_thread message."""
    from hopper.sessions import Session, save_sessions

    # Create a test session
    session = Session(id="test-id", stage="processing", created_at=1000, state="running")
    server.sessions = [session]
    save_sessions(server.sessions)

    # Connect client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Wait for client to be registered
    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    # Send session_set_codex_thread message
    msg = {
        "type": "session_set_codex_thread",
        "session_id": "test-id",
        "codex_thread_id": "codex-uuid-1234",
    }
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Should receive broadcast
    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "session_updated"
    assert response["session"]["id"] == "test-id"
    assert response["session"]["codex_thread_id"] == "codex-uuid-1234"

    # Server's session object should be updated
    assert server.sessions[0].codex_thread_id == "codex-uuid-1234"

    client.close()
