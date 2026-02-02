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


def connect(socket_path: Path, session_id: str | None = None, timeout: float = 2.0) -> dict | None:
    """Connect to the server and get status information.

    This is the primary handshake for all client commands. It returns server
    status including tmux location, and optionally validates/retrieves a session.

    Args:
        socket_path: Path to the Unix socket
        session_id: Optional session ID to look up
        timeout: Timeout in seconds

    Returns:
        Connected response dict with keys:
        - tmux: {"session": str, "window": str} or None
        - session: Session dict if session_id provided and found, else None
        - session_found: bool if session_id was provided
        Returns None if server is unreachable.
    """
    message: dict = {"type": "connect", "ts": int(time.time() * 1000)}
    if session_id:
        message["session_id"] = session_id
    response = send_message(socket_path, message, timeout=timeout, wait_for_response=True)
    if response is None or response.get("type") != "connected":
        return None
    return response


def ping(socket_path: Path, timeout: float = 2.0) -> bool:
    """Check if server is running.

    Args:
        socket_path: Path to the Unix socket
        timeout: Timeout in seconds

    Returns:
        True if server responds, False otherwise
    """
    return connect(socket_path, timeout=timeout) is not None


def session_exists(socket_path: Path, session_id: str, timeout: float = 2.0) -> bool:
    """Check if a session exists and is active.

    Args:
        socket_path: Path to the Unix socket
        session_id: The session ID to check
        timeout: Timeout in seconds

    Returns:
        True if session exists and is active, False otherwise
    """
    response = connect(socket_path, session_id=session_id, timeout=timeout)
    if response is None:
        return False
    return response.get("session_found", False)


def get_session_state(socket_path: Path, session_id: str, timeout: float = 2.0) -> str | None:
    """Get a session's current state.

    Args:
        socket_path: Path to the Unix socket
        session_id: The session ID to query
        timeout: Timeout in seconds

    Returns:
        The session state ("new", "idle", "running", "error") or None if not found
    """
    response = connect(socket_path, session_id=session_id, timeout=timeout)
    if response is None:
        return None
    session = response.get("session")
    if session is None:
        return None
    return session.get("state")


def get_session(socket_path: Path, session_id: str, timeout: float = 2.0) -> dict | None:
    """Get a session's full data.

    Args:
        socket_path: Path to the Unix socket
        session_id: The session ID to query
        timeout: Timeout in seconds

    Returns:
        The full session dict or None if not found
    """
    response = connect(socket_path, session_id=session_id, timeout=timeout)
    if response is None:
        return None
    return response.get("session")


def set_session_state(
    socket_path: Path, session_id: str, state: str, message: str, timeout: float = 2.0
) -> bool:
    """Set a session's state and message (fire-and-forget).

    Args:
        socket_path: Path to the Unix socket
        session_id: The session ID to update
        state: New state ("new", "idle", "running", or "error")
        message: Human-readable status message
        timeout: Connection timeout in seconds

    Returns:
        True if message was sent successfully, False otherwise
    """
    msg = {
        "type": "session_set_state",
        "session_id": session_id,
        "state": state,
        "message": message,
        "ts": int(time.time() * 1000),
    }
    # Fire-and-forget: don't wait for response
    try:
        send_message(socket_path, msg, timeout=timeout, wait_for_response=False)
        return True
    except Exception:
        return False


def set_session_message(
    socket_path: Path, session_id: str, message: str, timeout: float = 2.0
) -> bool:
    """Set a session's message only (fire-and-forget).

    Args:
        socket_path: Path to the Unix socket
        session_id: The session ID to update
        message: Human-readable status message
        timeout: Connection timeout in seconds

    Returns:
        True if message was sent successfully, False otherwise
    """
    msg = {
        "type": "session_set_message",
        "session_id": session_id,
        "message": message,
        "ts": int(time.time() * 1000),
    }
    # Fire-and-forget: don't wait for response
    try:
        send_message(socket_path, msg, timeout=timeout, wait_for_response=False)
        return True
    except Exception:
        return False
