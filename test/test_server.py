"""Tests for the hopper server."""

import json
import socket
import threading
import time
from unittest.mock import patch

import pytest

from hopper.lodes import save_lodes
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
    tmux_location = {"lode": "main", "pane": "%0"}
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

    assert response["type"] == "lode_state_changed"
    assert response["lode"]["id"] == "test-id"
    assert response["lode"]["state"] == "running"

    # Server's lode should be updated
    assert server.lodes[0]["state"] == "running"

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

    msg = {"type": "lode_register", "lode_id": "test-id"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Wait for registration
    for _ in range(50):
        if "test-id" in server.lode_clients:
            break
        time.sleep(0.1)

    assert "test-id" in server.lode_clients
    assert server.lodes[0]["active"] is True

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

    assert response["type"] == "lode_state_changed"
    assert response["lode"]["state"] == "ready"
    assert response["lode"]["status"] == "Mill output saved"

    client.close()


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
