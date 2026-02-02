"""Unix socket JSONL client for hopper."""

import json
import logging
import socket
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def send_message(
    socket_path: Path,
    message: dict,
    timeout: float = 2.0,
    wait_for_response: bool = False,
) -> dict | None:
    """Send a message to the server.

    Args:
        socket_path: Path to the Unix socket
        message: Message dict to send (must have 'type' field)
        timeout: Connection/send timeout in seconds
        wait_for_response: If True, wait for a response and return it

    Returns:
        Response dict if wait_for_response=True and response received, else None
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(socket_path))

        line = json.dumps(message) + "\n"
        sock.sendall(line.encode("utf-8"))

        if wait_for_response:
            buffer = ""
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                buffer += data.decode("utf-8")
                if "\n" in buffer:
                    response_line, _ = buffer.split("\n", 1)
                    sock.close()
                    return json.loads(response_line)

        sock.close()
        return None
    except Exception as e:
        logger.debug(f"send_message failed: {e}")
        return None


def ping(socket_path: Path, timeout: float = 2.0) -> bool:
    """Send a ping to the server and wait for pong.

    Args:
        socket_path: Path to the Unix socket
        timeout: Timeout in seconds

    Returns:
        True if pong received, False otherwise
    """
    message = {"type": "ping", "ts": int(time.time() * 1000)}
    response = send_message(socket_path, message, timeout=timeout, wait_for_response=True)
    return response is not None and response.get("type") == "pong"


def session_exists(socket_path: Path, session_id: str, timeout: float = 2.0) -> bool:
    """Check if a session exists and is active.

    Args:
        socket_path: Path to the Unix socket
        session_id: The session ID to check
        timeout: Timeout in seconds

    Returns:
        True if session exists and is active, False otherwise
    """
    message = {"type": "session_list", "ts": int(time.time() * 1000)}
    response = send_message(socket_path, message, timeout=timeout, wait_for_response=True)
    if response is None or response.get("type") != "session_list":
        return False
    sessions = response.get("sessions", [])
    return any(s.get("id") == session_id for s in sessions)


def get_session_state(socket_path: Path, session_id: str, timeout: float = 2.0) -> str | None:
    """Get a session's current state.

    Args:
        socket_path: Path to the Unix socket
        session_id: The session ID to query
        timeout: Timeout in seconds

    Returns:
        The session state ("new", "idle", "running", "error") or None if not found
    """
    message = {"type": "session_list", "ts": int(time.time() * 1000)}
    response = send_message(socket_path, message, timeout=timeout, wait_for_response=True)
    if response is None or response.get("type") != "session_list":
        return None
    sessions = response.get("sessions", [])
    for s in sessions:
        if s.get("id") == session_id:
            return s.get("state")
    return None


def set_session_state(socket_path: Path, session_id: str, state: str, timeout: float = 2.0) -> bool:
    """Set a session's state (fire-and-forget).

    Args:
        socket_path: Path to the Unix socket
        session_id: The session ID to update
        state: New state ("idle", "running", or "error")
        timeout: Connection timeout in seconds

    Returns:
        True if message was sent successfully, False otherwise
    """
    message = {
        "type": "session_set_state",
        "session_id": session_id,
        "state": state,
        "ts": int(time.time() * 1000),
    }
    # Fire-and-forget: don't wait for response
    try:
        send_message(socket_path, message, timeout=timeout, wait_for_response=False)
        return True
    except Exception:
        return False
