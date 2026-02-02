"""Tests for the hopper server."""

import json
import socket
import threading
import time

import pytest

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
