"""Unix socket JSONL server for hopper."""

import atexit
import json
import logging
import queue
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

from hopper.backlog import (
    BacklogItem,
    add_backlog_item,
    load_backlog,
    remove_backlog_item,
)
from hopper.backlog import (
    find_by_short_id as find_backlog_by_short_id,
)
from hopper.sessions import (
    Session,
    archive_session,
    create_session,
    current_time_ms,
    load_sessions,
    save_sessions,
    update_session_stage,
    update_session_state,
    update_session_status,
)

logger = logging.getLogger(__name__)


def get_git_hash() -> str | None:
    """Get the short git hash of the current HEAD."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    return None


class Server:
    """Broadcast message server over Unix domain socket.

    Uses a single writer thread to serialize all broadcasts, preventing
    race conditions when multiple client handler threads send concurrently.

    Tracks which clients own which sessions. Sets active=False and clears
    tmux_pane on disconnect; state/status are client-driven.
    """

    def __init__(self, socket_path: Path, tmux_location: dict | None = None):
        self.socket_path = socket_path
        self.tmux_location = tmux_location
        self.git_hash = get_git_hash()
        self.started_at = current_time_ms()
        self.clients: list[socket.socket] = []
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.server_socket: socket.socket | None = None
        self.broadcast_queue: queue.Queue = queue.Queue(maxsize=10000)
        self.writer_thread: threading.Thread | None = None
        self.sessions: list[Session] = []
        self.backlog: list[BacklogItem] = []
        # Session ownership tracking: session_id -> socket, socket -> session_id
        self.session_clients: dict[str, socket.socket] = {}
        self.client_sessions: dict[socket.socket, str] = {}

    def start(self) -> None:
        """Start the server (blocking)."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.sessions = load_sessions()
        self.backlog = load_backlog()

        # Clear stale active flags from previous run (no clients connected yet)
        stale = False
        for session in self.sessions:
            if session.active or session.tmux_pane:
                session.active = False
                session.tmux_pane = None
                stale = True
        if stale:
            save_sessions(self.sessions)

        # Remove stale socket file
        if self.socket_path.exists():
            self.socket_path.unlink()

        self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_socket.bind(str(self.socket_path))
        self.server_socket.listen(5)
        self.server_socket.settimeout(1.0)

        # Start writer thread
        self.writer_thread = threading.Thread(
            target=self._writer_loop, name="server-writer", daemon=True
        )
        self.writer_thread.start()

        logger.info(f"Server listening on {self.socket_path}")

        try:
            while not self.stop_event.is_set():
                try:
                    conn, _ = self.server_socket.accept()
                    threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()
                except socket.timeout:
                    continue
                except Exception as e:
                    if not self.stop_event.is_set():
                        logger.error(f"Accept error: {e}")
        finally:
            self.server_socket.close()
            if self.socket_path.exists():
                self.socket_path.unlink()

    def _handle_client(self, conn: socket.socket) -> None:
        """Handle a client connection."""
        with self.lock:
            self.clients.append(conn)

        logger.debug(f"Client connected ({len(self.clients)} total)")

        try:
            conn.settimeout(2.0)
            buffer = ""
            while not self.stop_event.is_set():
                try:
                    data = conn.recv(4096)
                    if not data:
                        break

                    buffer += data.decode("utf-8")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        if line.strip():
                            try:
                                message = json.loads(line)
                                self._handle_message(message, conn)
                            except json.JSONDecodeError:
                                pass
                except socket.timeout:
                    continue
        except Exception as e:
            logger.debug(f"Client error: {e}")
        finally:
            self._on_client_disconnect(conn)
            with self.lock:
                if conn in self.clients:
                    self.clients.remove(conn)
            try:
                conn.close()
            except Exception:
                pass
            logger.debug(f"Client disconnected ({len(self.clients)} remaining)")

    def _on_client_disconnect(self, conn: socket.socket) -> None:
        """Handle client disconnect - set active=False and clear tmux_pane."""
        with self.lock:
            session_id = self.client_sessions.pop(conn, None)
            if session_id:
                self.session_clients.pop(session_id, None)

        if not session_id:
            return

        session = next((s for s in self.sessions if s.id == session_id), None)
        if not session:
            return

        session.active = False
        session.tmux_pane = None
        session.touch()
        save_sessions(self.sessions)

        logger.debug(f"Session {session_id[:8]} disconnected, active=False")
        self.broadcast({"type": "session_updated", "session": session.to_dict()})

    def _register_session_client(
        self, session_id: str, conn: socket.socket, tmux_pane: str | None = None
    ) -> None:
        """Register a client as owning a session.

        Sets active=True on the session and disconnects any stale owner.
        """
        with self.lock:
            # Check for existing owner
            existing_conn = self.session_clients.get(session_id)
            if existing_conn and existing_conn != conn:
                # Disconnect stale client
                old_session_id = self.client_sessions.pop(existing_conn, None)
                if old_session_id:
                    self.session_clients.pop(old_session_id, None)
                try:
                    existing_conn.close()
                except Exception:
                    pass
                logger.debug(f"Disconnected stale client for session {session_id[:8]}")

            # Register new owner
            self.session_clients[session_id] = conn
            self.client_sessions[conn] = session_id

        # Set active on the session
        session = next((s for s in self.sessions if s.id == session_id), None)
        if session:
            session.active = True
            if tmux_pane:
                session.tmux_pane = tmux_pane
            session.touch()
            save_sessions(self.sessions)
            self.broadcast({"type": "session_updated", "session": session.to_dict()})

        logger.debug(f"Registered client for session {session_id[:8]}, active=True")

    def _handle_message(self, message: dict, conn: socket.socket) -> None:
        """Handle an incoming message, responding directly if needed."""
        msg_type = message.get("type")

        if msg_type == "connect":
            # Read-only handshake: returns session data without claiming ownership
            session_id = message.get("session_id")
            response: dict = {
                "type": "connected",
                "tmux": self.tmux_location,
            }
            if session_id:
                session = next((s for s in self.sessions if s.id == session_id), None)
                response["session"] = session.to_dict() if session else None
                response["session_found"] = session is not None

            self._send_response(conn, response)

        elif msg_type == "session_register":
            # Persistent connection claims ownership of a session (sets active=True)
            session_id = message.get("session_id")
            if session_id:
                session = next((s for s in self.sessions if s.id == session_id), None)
                if session:
                    tmux_pane = message.get("tmux_pane")
                    self._register_session_client(session_id, conn, tmux_pane)

        elif msg_type == "ping":
            self._send_response(conn, {"type": "pong"})

        elif msg_type == "session_list":
            sessions_data = [s.to_dict() for s in self.sessions]
            self._send_response(conn, {"type": "session_list", "sessions": sessions_data})

        elif msg_type == "session_create":
            project = message.get("project", "")
            session = create_session(self.sessions, project)
            self.broadcast({"type": "session_created", "session": session.to_dict()})

        elif msg_type == "session_update":
            session_id = message.get("session_id")
            stage = message.get("stage")
            if session_id and stage:
                session = update_session_stage(self.sessions, session_id, stage)
                if session:
                    self.broadcast({"type": "session_updated", "session": session.to_dict()})

        elif msg_type == "session_archive":
            session_id = message.get("session_id")
            if session_id:
                session = archive_session(self.sessions, session_id)
                if session:
                    self.broadcast({"type": "session_archived", "session": session.to_dict()})

        elif msg_type == "session_set_state":
            session_id = message.get("session_id")
            state = message.get("state")
            status = message.get("status", "")
            if session_id and state:
                session = update_session_state(self.sessions, session_id, state, status)
                if session:
                    self.broadcast({"type": "session_state_changed", "session": session.to_dict()})

        elif msg_type == "session_set_status":
            session_id = message.get("session_id")
            status = message.get("status", "")
            if session_id:
                session = update_session_status(self.sessions, session_id, status)
                if session:
                    self.broadcast({"type": "session_status_changed", "session": session.to_dict()})

        elif msg_type == "session_set_codex_thread":
            session_id = message.get("session_id")
            thread_id = message.get("codex_thread_id")
            if session_id and thread_id:
                session = next((s for s in self.sessions if s.id == session_id), None)
                if session:
                    session.codex_thread_id = thread_id
                    session.touch()
                    save_sessions(self.sessions)
                    self.broadcast({"type": "session_updated", "session": session.to_dict()})

        elif msg_type == "backlog_list":
            items_data = [item.to_dict() for item in self.backlog]
            self._send_response(conn, {"type": "backlog_list", "items": items_data})

        elif msg_type == "backlog_add":
            project = message.get("project", "")
            description = message.get("description", "")
            session_id = message.get("session_id")
            if project and description:
                item = add_backlog_item(self.backlog, project, description, session_id)
                self.broadcast({"type": "backlog_added", "item": item.to_dict()})

        elif msg_type == "backlog_remove":
            item_id = message.get("item_id", "")
            item = find_backlog_by_short_id(self.backlog, item_id)
            if item:
                remove_backlog_item(self.backlog, item.id)
                self.broadcast({"type": "backlog_removed", "item": item.to_dict()})

        else:
            # Broadcast other messages
            self.broadcast(message)

    def _send_response(self, conn: socket.socket, message: dict) -> None:
        """Send a response directly to a client."""
        if "ts" not in message:
            message["ts"] = int(time.time() * 1000)
        response = json.dumps(message) + "\n"
        try:
            conn.sendall(response.encode("utf-8"))
        except Exception as e:
            logger.debug(f"Failed to send response: {e}")

    def _writer_loop(self) -> None:
        """Dedicated writer thread that serializes all broadcasts."""
        while not self.stop_event.is_set():
            try:
                message = self.broadcast_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            self._send_to_clients(message)

    def _send_to_clients(self, message: dict) -> None:
        """Send a message to all connected clients."""
        if "ts" not in message:
            message["ts"] = int(time.time() * 1000)

        data = (json.dumps(message) + "\n").encode("utf-8")

        with self.lock:
            clients_to_send = list(self.clients)

        dead_clients = []
        for client in clients_to_send:
            try:
                client.settimeout(2.0)
                client.sendall(data)
            except Exception as e:
                logger.debug(f"Failed to send to client: {e}")
                dead_clients.append(client)

        if dead_clients:
            with self.lock:
                for client in dead_clients:
                    if client in self.clients:
                        self.clients.remove(client)
                    try:
                        client.close()
                    except Exception:
                        pass

    def broadcast(self, message: dict) -> bool:
        """Queue message for broadcast to all connected clients."""
        if "type" not in message:
            logger.warning("Skipping message without type field")
            return False

        try:
            self.broadcast_queue.put_nowait(message)
            return True
        except queue.Full:
            logger.warning(f"Broadcast queue full, dropping: {message.get('type')}")
            return False

    def stop(self) -> None:
        """Stop the server gracefully.

        Sends shutdown message to clients, closes all connections, then stops threads.
        """
        logger.info("Server stopping")

        # Send shutdown message to all clients (bypass queue for immediate delivery)
        self._send_to_clients({"type": "shutdown"})

        # Close all client connections
        with self.lock:
            for client in self.clients:
                try:
                    client.close()
                except Exception:
                    pass
            self.clients.clear()

        # Signal threads to stop
        self.stop_event.set()

        # Close server socket to unblock accept()
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass

        # Wait for writer thread
        if self.writer_thread and self.writer_thread.is_alive():
            self.writer_thread.join(timeout=1.0)

        # Clean up socket file
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except Exception:
                pass

        logger.info("Server stopped")


def start_server_with_tui(socket_path: Path, tmux_location: dict | None = None) -> int:
    """Start the server in a background thread and run the TUI."""
    from hopper.tui import run_tui

    server = Server(socket_path, tmux_location=tmux_location)
    shutdown_initiated = threading.Event()

    def handle_shutdown_signal(signum, frame):
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        if not shutdown_initiated.is_set():
            shutdown_initiated.set()
            raise KeyboardInterrupt

    # Register signal handlers
    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)

    # Register atexit handler for socket cleanup (backup for abnormal exit)
    def cleanup_socket():
        if socket_path.exists():
            try:
                socket_path.unlink()
            except Exception:
                pass

    atexit.register(cleanup_socket)

    # Start server in background thread
    server_thread = threading.Thread(target=server.start, name="server", daemon=True)
    server_thread.start()

    # Wait for socket to be ready
    for _ in range(50):
        if socket_path.exists():
            break
        time.sleep(0.1)
    else:
        print("Server failed to start")
        server.stop()
        return 1

    # Run Textual TUI in main thread
    try:
        return run_tui(server)
    except KeyboardInterrupt:
        return 0
    finally:
        logger.info("Shutting down server")
        server.stop()
        server_thread.join(timeout=2.0)
        atexit.unregister(cleanup_socket)
