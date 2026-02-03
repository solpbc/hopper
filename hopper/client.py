"""Unix socket JSONL client for hopper."""

import json
import logging
import queue
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


class HopperConnection:
    """Persistent bidirectional connection to the hopper server.

    Messages are sent via a queue to avoid blocking. A background thread handles
    connection management, queue draining, and message receiving. Messages are
    dropped (with debug logging) when disconnected.
    """

    def __init__(self, socket_path: Path):
        """Initialize connection (does not connect immediately).

        Args:
            socket_path: Path to Unix socket
        """
        self.socket_path = socket_path
        self.send_queue: queue.Queue = queue.Queue(maxsize=1000)
        self.callback: Callable[[dict[str, Any]], Any] | None = None
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()

    def start(self, callback: Callable[[dict[str, Any]], Any] | None = None) -> None:
        """Start background thread for sending and receiving.

        Thread will auto-connect with retry and drain the send queue even when
        disconnected (dropping messages with debug logging).

        Args:
            callback: Optional function to process received messages
        """
        if self.thread and self.thread.is_alive():
            return  # Already started

        self.callback = callback
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self) -> None:
        """Main loop: drain queue, connect/reconnect, receive when connected."""
        sock: socket.socket | None = None
        buffer = ""
        last_connect_attempt = 0.0

        while True:
            # Try to connect if not connected (rate limited to 1/sec)
            if not sock and time.time() - last_connect_attempt > 1.0:
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect(str(self.socket_path))
                    sock.settimeout(0.1)  # Short timeout for responsive queue draining
                    logger.debug(f"Connected to {self.socket_path}")
                except Exception as e:
                    logger.debug(f"Connection attempt failed: {e}")
                    if sock:
                        try:
                            sock.close()
                        except Exception:
                            pass
                        sock = None
                    last_connect_attempt = time.time()

            # ALWAYS drain queue (send if connected, drop if not)
            try:
                msg = self.send_queue.get(timeout=0.1)
                if sock:
                    try:
                        line = json.dumps(msg) + "\n"
                        sock.sendall(line.encode("utf-8"))
                    except Exception as e:
                        logger.debug(f"Send failed for {msg.get('type')}: {e}")
                        try:
                            sock.close()
                        except Exception:
                            pass
                        sock = None
                else:
                    # Not connected, drop message
                    logger.debug(f"Dropping message (not connected): {msg.get('type')}")
            except queue.Empty:
                # Queue is empty - check if we should exit
                if self.stop_event.is_set():
                    break
                # Otherwise continue to receive

            # Receive incoming messages (only if connected)
            if sock:
                try:
                    data = sock.recv(4096)
                    if not data:
                        # Connection closed by server
                        logger.debug("Connection closed by server")
                        try:
                            sock.close()
                        except Exception:
                            pass
                        sock = None
                        buffer = ""  # Clear partial data from old connection
                        continue

                    buffer += data.decode("utf-8")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        if line.strip() and self.callback:
                            try:
                                message = json.loads(line)
                                self.callback(message)
                            except json.JSONDecodeError:
                                pass
                            except Exception as e:
                                logger.error(f"Callback error: {e}")
                except socket.timeout:
                    continue  # Normal, just loop back to drain queue
                except Exception as e:
                    logger.debug(f"Receive error: {e}")
                    try:
                        sock.close()
                    except Exception:
                        pass
                    sock = None
                    buffer = ""  # Clear partial data from old connection

        # Cleanup on stop
        if sock:
            try:
                sock.close()
            except Exception:
                pass

    def emit(self, msg_type: str, **fields) -> bool:
        """Emit message via send queue.

        Returns immediately after queueing. Requires start() to be called first.

        Args:
            msg_type: Message type (e.g., "session_set_state")
            **fields: Additional message fields

        Returns:
            True if queued successfully, False if thread not running or queue full
        """
        if not self.thread or not self.thread.is_alive():
            logger.debug(f"Thread not running, dropping emit: {msg_type}")
            return False

        message = {"type": msg_type, "ts": int(time.time() * 1000), **fields}
        try:
            self.send_queue.put_nowait(message)
            return True
        except queue.Full:
            logger.warning(f"Queue full, dropping emit: {msg_type}")
            return False

    def stop(self) -> None:
        """Stop background thread gracefully, draining queue first."""
        if not self.thread:
            return

        self.stop_event.set()
        self.thread.join(timeout=0.5)

        if self.thread.is_alive():
            logger.warning("Background thread did not stop cleanly")


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
    socket_path: Path, session_id: str, state: str, status: str, timeout: float = 2.0
) -> bool:
    """Set a session's state and status (fire-and-forget).

    Args:
        socket_path: Path to the Unix socket
        session_id: The session ID to update
        state: New state ("new", "idle", "running", "stuck", or "error")
        status: Human-readable status text
        timeout: Connection timeout in seconds

    Returns:
        True if message was sent successfully, False otherwise
    """
    msg = {
        "type": "session_set_state",
        "session_id": session_id,
        "state": state,
        "status": status,
        "ts": int(time.time() * 1000),
    }
    # Fire-and-forget: don't wait for response
    try:
        send_message(socket_path, msg, timeout=timeout, wait_for_response=False)
        return True
    except Exception:
        return False


def set_session_status(
    socket_path: Path, session_id: str, status: str, timeout: float = 2.0
) -> bool:
    """Set a session's status text only (fire-and-forget).

    Args:
        socket_path: Path to the Unix socket
        session_id: The session ID to update
        status: Human-readable status text
        timeout: Connection timeout in seconds

    Returns:
        True if message was sent successfully, False otherwise
    """
    msg = {
        "type": "session_set_status",
        "session_id": session_id,
        "status": status,
        "ts": int(time.time() * 1000),
    }
    # Fire-and-forget: don't wait for response
    try:
        send_message(socket_path, msg, timeout=timeout, wait_for_response=False)
        return True
    except Exception:
        return False
