# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Unix socket JSONL client for hopper."""

import json
import logging
import queue
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Literal

from hopper.lodes import current_time_ms

logger = logging.getLogger(__name__)


class InvalidServerResponse(ValueError):
    """Raised when a server response is not a JSON object."""


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
        self.on_connect: Callable[[], Any] | None = None
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()

    def start(
        self,
        callback: Callable[[dict[str, Any]], Any] | None = None,
        on_connect: Callable[[], Any] | None = None,
    ) -> None:
        """Start background thread for sending and receiving.

        Thread will auto-connect with retry and drain the send queue even when
        disconnected (dropping messages with debug logging).

        Args:
            callback: Optional function to process received messages
            on_connect: Optional function called on each successful connection
                (initial and reconnects). Runs on the background thread.
        """
        if self.thread and self.thread.is_alive():
            return  # Already started

        self.callback = callback
        self.on_connect = on_connect
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
                    if self.on_connect:
                        try:
                            self.on_connect()
                        except Exception as e:
                            logger.error(f"on_connect callback failed: {e}")
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
            msg_type: Message type (e.g., "lode_set_state")
            **fields: Additional message fields

        Returns:
            True if queued successfully, False if thread not running or queue full
        """
        if not self.thread or not self.thread.is_alive():
            logger.debug(f"Thread not running, dropping emit: {msg_type}")
            return False

        message = {"type": msg_type, "ts": current_time_ms(), **fields}
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


def _exchange_message(
    socket_path: Path,
    message: dict,
    timeout: float = 2.0,
    wait_for_response: bool = False,
) -> dict | None:
    """Exchange one message with the server, raising transport/protocol errors."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(socket_path))

        line = json.dumps(message) + "\n"
        sock.sendall(line.encode("utf-8"))

        if not wait_for_response:
            return None

        buffer = ""
        while "\n" not in buffer:
            data = sock.recv(4096)
            if not data:
                raise ConnectionError("server closed connection before responding")
            buffer += data.decode("utf-8")
        response_line, _ = buffer.split("\n", 1)
        response = json.loads(response_line)
        if not isinstance(response, dict):
            raise InvalidServerResponse("server response is not a JSON object")
        return response


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
        return _exchange_message(socket_path, message, timeout, wait_for_response)
    except (OSError, UnicodeError, json.JSONDecodeError, InvalidServerResponse) as e:
        logger.debug(f"send_message failed: {e}")
        return None


def connect(socket_path: Path, lode_id: str | None = None, timeout: float = 2.0) -> dict | None:
    """Connect to the server and get status information.

    This is the primary handshake for all client commands. It returns server
    status including tmux location, and optionally validates/retrieves a lode.

    Args:
        socket_path: Path to the Unix socket
        lode_id: Optional lode ID to look up
        timeout: Timeout in seconds

    Returns:
        Connected response dict with keys:
        - tmux: {"session": str, "pane": str} or None
        - lode: lode dict if lode_id provided and found, else None
        - lode_found: bool if lode_id was provided
        Returns None if server is unreachable.
    """
    message: dict = {"type": "connect", "ts": current_time_ms()}
    if lode_id:
        message["lode_id"] = lode_id
    response = send_message(socket_path, message, timeout=timeout, wait_for_response=True)
    if response is None or response.get("type") != "connected":
        return None
    return response


ProbeStatus = Literal["up", "down", "unresponsive"]


def probe_server(socket_path: Path, timeout: float = 2.0) -> ProbeStatus:
    """Classify a server socket as up, down, or listening but unresponsive."""
    try:
        response = _exchange_message(
            socket_path,
            {"type": "ping", "ts": current_time_ms()},
            timeout,
            wait_for_response=True,
        )
    except (FileNotFoundError, ConnectionRefusedError):
        return "down"
    except (
        TimeoutError,
        BlockingIOError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        InvalidServerResponse,
    ):
        return "unresponsive"

    # Liveness only requires a well-formed pong; pid/started_at are identity
    # data for consumers that read them, not liveness criteria.
    if response is None or response.get("type") != "pong":
        return "unresponsive"
    return "up"


def ping(socket_path: Path, timeout: float = 2.0) -> bool:
    """Check if server is running.

    Args:
        socket_path: Path to the Unix socket
        timeout: Timeout in seconds

    Returns:
        True if server responds, False otherwise
    """
    return probe_server(socket_path, timeout=timeout) == "up"


def lode_exists(socket_path: Path, lode_id: str, timeout: float = 2.0) -> bool:
    """Check if a lode exists in the active lodes list.

    Note: This checks existence only, not whether a client is connected.
    Use get_lode() and check the 'active' field for connection status.

    Args:
        socket_path: Path to the Unix socket
        lode_id: The lode ID to check
        timeout: Timeout in seconds

    Returns:
        True if lode exists in the lodes list, False otherwise
    """
    response = connect(socket_path, lode_id=lode_id, timeout=timeout)
    if response is None:
        return False
    return response.get("lode_found", False)


def get_lode(socket_path: Path, lode_id: str, timeout: float = 2.0) -> dict | None:
    """Get a lode's full data.

    Args:
        socket_path: Path to the Unix socket
        lode_id: The lode ID to query
        timeout: Timeout in seconds

    Returns:
        The full lode dict or None if not found
    """
    response = connect(socket_path, lode_id=lode_id, timeout=timeout)
    if response is None:
        return None
    return response.get("lode")


def list_lodes(socket_path: Path, timeout: float = 2.0) -> list[dict]:
    """List all active lodes from the server."""
    response = send_message(
        socket_path, {"type": "lode_list"}, timeout=timeout, wait_for_response=True
    )
    if response and response.get("type") == "lode_list":
        return response.get("lodes", [])
    return []


def read_archived_lodes(socket_path: Path, timeout: float = 2.0) -> list[dict] | None:
    """Read archived lodes, preserving an unreachable server as None."""
    response = send_message(
        socket_path,
        {"type": "archived_list"},
        timeout=timeout,
        wait_for_response=True,
    )
    if response and response.get("type") == "archived_list":
        lodes = response.get("lodes")
        return lodes if isinstance(lodes, list) else None
    return None


def list_archived_lodes(socket_path: Path, timeout: float = 2.0) -> list[dict]:
    """List archived lodes, returning an empty list when unavailable."""
    return read_archived_lodes(socket_path, timeout=timeout) or []


def create_lode(
    socket_path: Path,
    project: str,
    scope: str,
    spawn: bool = True,
    timeout: float = 5.0,
) -> dict | None:
    """Create a new lode via the server. Returns the created lode dict or None."""
    response = send_message(
        socket_path,
        {"type": "lode_create", "project": project, "scope": scope, "spawn": spawn},
        timeout=timeout,
        wait_for_response=True,
    )
    if response and response.get("type") == "lode_created":
        return response.get("lode")
    return None


def send_gate_feedback(
    socket_path: Path, lode_id: str, text: str, timeout: float = 15.0
) -> dict | None:
    """Send feedback to a gated lode's pane. Returns the server response dict."""
    return send_message(
        socket_path,
        {"type": "lode_send_feedback", "lode_id": lode_id, "text": text},
        timeout=timeout,
        wait_for_response=True,
    )


def get_gate(socket_path: Path, lode_id: str, timeout: float = 2.0) -> dict | None:
    """Look up a lode and read its gate.md. Returns None if the lode doesn't exist."""
    from hopper.lodes import get_lode_dir

    response = connect(socket_path, lode_id=lode_id, timeout=timeout)
    if response is None or not response.get("lode_found"):
        return None

    lode = response.get("lode")
    if not lode:
        return None

    gate_path = get_lode_dir(lode_id) / "gate.md"
    gate_text = gate_path.read_text() if gate_path.exists() else ""
    return {"lode": lode, "gate": gate_text}


def restart_lode(socket_path: Path, lode_id: str, stage: str, timeout: float = 2.0) -> bool:
    """Restart a lode's current stage session. Fire-and-forget."""
    return _fire_and_forget(
        socket_path,
        {
            "type": "lode_reset_claude_stage",
            "lode_id": lode_id,
            "claude_stage": stage,
            "spawn": True,
        },
        timeout=timeout,
    )


def kill_lode(socket_path: Path, lode_id: str, timeout: float = 2.0) -> bool:
    """Kill a running lode. Fire-and-forget."""
    return _fire_and_forget(
        socket_path,
        {"type": "lode_kill", "lode_id": lode_id},
        timeout=timeout,
    )


def pause_lode(socket_path: Path, lode_id: str, timeout: float = 2.0) -> dict | None:
    """Pause a lode without archiving or removing its worktree."""
    return send_message(
        socket_path,
        {"type": "lode_pause", "lode_id": lode_id},
        timeout=timeout,
        wait_for_response=True,
    )


def resume_lode(socket_path: Path, lode_id: str, timeout: float = 2.0) -> dict | None:
    """Resume a paused or dead-pane lode with its existing stage session."""
    return send_message(
        socket_path,
        {"type": "lode_resume", "lode_id": lode_id},
        timeout=timeout,
        wait_for_response=True,
    )


def _fire_and_forget(socket_path: Path, msg: dict, timeout: float = 2.0) -> bool:
    """Send a message to the server without waiting for a response."""
    try:
        send_message(socket_path, msg, timeout=timeout, wait_for_response=False)
        return True
    except Exception:
        return False


def set_lode_state(
    socket_path: Path, lode_id: str, state: str, status: str, timeout: float = 2.0
) -> bool:
    """Set a lode's state and status (fire-and-forget).

    Args:
        socket_path: Path to the Unix socket
        lode_id: The lode ID to update
        state: New state (freeform string, e.g. "new", "running", "error", task names, etc.)
        status: Human-readable status text
        timeout: Connection timeout in seconds

    Returns:
        True if message was sent successfully, False otherwise
    """
    msg = {
        "type": "lode_set_state",
        "lode_id": lode_id,
        "state": state,
        "status": status,
        "ts": current_time_ms(),
    }
    return _fire_and_forget(socket_path, msg, timeout)


def set_lode_status(socket_path: Path, lode_id: str, status: str, timeout: float = 2.0) -> bool:
    """Set a lode's status text only (fire-and-forget).

    Args:
        socket_path: Path to the Unix socket
        lode_id: The lode ID to update
        status: Human-readable status text
        timeout: Connection timeout in seconds

    Returns:
        True if message was sent successfully, False otherwise
    """
    msg = {
        "type": "lode_set_status",
        "lode_id": lode_id,
        "status": status,
        "ts": current_time_ms(),
    }
    return _fire_and_forget(socket_path, msg, timeout)


def set_lode_progress(socket_path: Path, lode_id: str, summary: str, timeout: float = 1.0) -> bool:
    """Set a lode's best-effort progress heartbeat."""
    try:
        if not socket_path.exists():
            return False
    except Exception:
        return False
    msg = {
        "type": "lode_set_progress",
        "lode_id": lode_id,
        "summary": summary,
        "ts": current_time_ms(),
    }
    return _fire_and_forget(socket_path, msg, timeout)


def set_lode_title(socket_path: Path, lode_id: str, title: str, timeout: float = 2.0) -> bool:
    """Set a lode's title only (fire-and-forget).

    Args:
        socket_path: Path to the Unix socket
        lode_id: The lode ID to update
        title: Short human-readable title
        timeout: Connection timeout in seconds

    Returns:
        True if message was sent successfully, False otherwise
    """
    msg = {
        "type": "lode_set_title",
        "lode_id": lode_id,
        "title": title,
        "ts": current_time_ms(),
    }
    return _fire_and_forget(socket_path, msg, timeout)


def set_lode_branch(socket_path: Path, lode_id: str, branch: str, timeout: float = 2.0) -> bool:
    """Set a lode's branch only (fire-and-forget).

    Args:
        socket_path: Path to the Unix socket
        lode_id: The lode ID to update
        branch: Git branch name for this lode
        timeout: Connection timeout in seconds

    Returns:
        True if message was sent successfully, False otherwise
    """
    msg = {
        "type": "lode_set_branch",
        "lode_id": lode_id,
        "branch": branch,
        "ts": current_time_ms(),
    }
    return _fire_and_forget(socket_path, msg, timeout)


def set_codex_thread_id(
    socket_path: Path, lode_id: str, codex_thread_id: str, timeout: float = 2.0
) -> bool:
    """Set a lode's Codex thread ID (fire-and-forget).

    Args:
        socket_path: Path to the Unix socket
        lode_id: The lode ID to update
        codex_thread_id: The Codex thread UUID to store
        timeout: Connection timeout in seconds

    Returns:
        True if message was sent successfully, False otherwise
    """
    msg = {
        "type": "lode_set_codex_thread",
        "lode_id": lode_id,
        "codex_thread_id": codex_thread_id,
        "ts": current_time_ms(),
    }
    return _fire_and_forget(socket_path, msg, timeout)


def add_backlog(
    socket_path: Path,
    project: str,
    description: str,
    lode_id: str | None = None,
    timeout: float = 2.0,
) -> bool:
    """Add a backlog item via the server (fire-and-forget).

    Args:
        socket_path: Path to the Unix socket
        project: Project name
        description: Item description
        lode_id: Optional lode that added it
        timeout: Connection timeout in seconds

    Returns:
        True if message was sent successfully, False otherwise
    """
    msg: dict = {
        "type": "backlog_add",
        "project": project,
        "description": description,
        "ts": current_time_ms(),
    }
    if lode_id:
        msg["lode_id"] = lode_id
    return _fire_and_forget(socket_path, msg, timeout)


def remove_backlog(socket_path: Path, item_id: str, timeout: float = 2.0) -> bool:
    """Remove a backlog item via the server (fire-and-forget).

    Args:
        socket_path: Path to the Unix socket
        item_id: ID or prefix of the item to remove
        timeout: Connection timeout in seconds

    Returns:
        True if message was sent successfully, False otherwise
    """
    msg = {
        "type": "backlog_remove",
        "item_id": item_id,
        "ts": current_time_ms(),
    }
    return _fire_and_forget(socket_path, msg, timeout)


def promote_backlog(
    socket_path: Path,
    item_id: str,
    scope: str = "",
    timeout: float = 5.0,
) -> dict | None:
    """Promote a backlog item to a lode via the server. Returns the created lode dict or None."""
    msg: dict = {
        "type": "lode_promote_backlog",
        "item_id": item_id,
        "ts": current_time_ms(),
    }
    if scope:
        msg["scope"] = scope
    response = send_message(socket_path, msg, timeout=timeout, wait_for_response=True)
    if response and response.get("type") == "lode_promoted":
        return response.get("lode")
    if response and response.get("type") == "promote_error":
        logger.warning(f"promote_backlog failed: {response.get('error', 'unknown error')}")
    return None


def set_backlog_queued(
    socket_path: Path,
    item_id: str,
    queued: str | None,
    timeout: float = 2.0,
) -> bool:
    """Set or clear queued assignment for a backlog item. Fire-and-forget."""
    return _fire_and_forget(
        socket_path,
        {
            "type": "backlog_set_queued",
            "item_id": item_id,
            "queued": queued,
            "ts": current_time_ms(),
        },
        timeout,
    )


def reload_projects(socket_path: Path, timeout: float = 2.0) -> bool:
    """Ask server to reload projects from disk (fire-and-forget).

    Args:
        socket_path: Path to the Unix socket
        timeout: Connection timeout in seconds

    Returns:
        True if message was sent successfully, False otherwise
    """
    msg = {
        "type": "projects_reload",
        "ts": current_time_ms(),
    }
    return _fire_and_forget(socket_path, msg, timeout)
