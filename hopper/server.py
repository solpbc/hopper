# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

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

from hopper import config
from hopper.backlog import (
    BacklogItem,
    add_backlog_item,
    load_backlog,
    remove_backlog_item,
)
from hopper.backlog import (
    find_by_prefix as find_backlog_by_prefix,
)
from hopper.claude import spawn_claude
from hopper.lodes import (
    archive_lode,
    create_lode,
    current_time_ms,
    load_archived_lodes,
    load_lodes,
    reset_lode_claude_stage,
    save_lodes,
    set_lode_claude_started,
    touch,
    update_lode_auto,
    update_lode_codex_thread,
    update_lode_stage,
    update_lode_state,
    update_lode_status,
    update_lode_title,
)
from hopper.process import STAGES
from hopper.projects import Project, find_project, get_active_projects

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

    Tracks which clients own which lodes. Sets active=False and clears
    tmux_pane and pid on disconnect; state/status are client-driven.
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
        self.lodes: list[dict] = []
        self.archived_lodes: list[dict] = []
        self.backlog: list[BacklogItem] = []
        self.projects: list[Project] = []
        # Lode ownership tracking: lode_id -> socket, socket -> lode_id
        self.lode_clients: dict[str, socket.socket] = {}
        self.client_lodes: dict[socket.socket, str] = {}
        self._log_handler: logging.FileHandler | None = None

    def _find_lode(self, lode_id: str) -> dict | None:
        """Find a lode by ID."""
        return next((lode for lode in self.lodes if lode["id"] == lode_id), None)

    def start(self) -> None:
        """Start the server (blocking)."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        # Configure file logging for all hopper modules
        log_path = config.hopper_dir() / "activity.log"
        handler = logging.FileHandler(log_path)
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            "%(asctime)s.%(msecs)03d %(name)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        hopper_logger = logging.getLogger("hopper")
        hopper_logger.setLevel(logging.DEBUG)
        hopper_logger.addHandler(handler)
        self._log_handler = handler
        self.lodes = load_lodes()
        self.archived_lodes = load_archived_lodes()
        self.backlog = load_backlog()
        self.projects = get_active_projects()

        # Clear stale active flags from previous run (no clients connected yet)
        stale = False
        for lode in self.lodes:
            if lode.get("active") or lode.get("tmux_pane") or lode.get("pid"):
                lode["active"] = False
                lode["tmux_pane"] = None
                lode["pid"] = None
                stale = True
        if stale:
            save_lodes(self.lodes)

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
        """Handle client disconnect - set active=False and clear tmux_pane and pid."""
        with self.lock:
            lode_id = self.client_lodes.pop(conn, None)
            if lode_id:
                self.lode_clients.pop(lode_id, None)

        if not lode_id:
            return

        lode = self._find_lode(lode_id)
        if not lode:
            return

        lode["active"] = False
        lode["tmux_pane"] = None
        lode["pid"] = None
        touch(lode)
        save_lodes(self.lodes)

        logger.info(f"Lode {lode_id} disconnected, active=False")
        self.broadcast({"type": "lode_updated", "lode": lode})

        stage = lode.get("stage", "")
        if (
            lode.get("auto")
            and lode.get("state") == "ready"
            and stage in STAGES
            and lode.get("status") != STAGES[stage]["done_status"]
        ):
            project = find_project(lode.get("project", ""))
            project_path = project.path if project else None
            if project_path:
                logger.info(f"Auto-advancing lode {lode_id} to {stage}")
                spawn_claude(lode_id, project_path, foreground=False)
            else:
                logger.warning(f"Auto-advance skipped for {lode_id}: project not found")

    def _register_lode_client(
        self,
        lode_id: str,
        conn: socket.socket,
        tmux_pane: str | None = None,
        pid: int | None = None,
    ) -> None:
        """Register a client as owning a lode.

        Sets active=True on the lode and disconnects any stale owner.
        """
        with self.lock:
            # Check for existing owner
            existing_conn = self.lode_clients.get(lode_id)
            if existing_conn and existing_conn != conn:
                # Disconnect stale client
                old_lode_id = self.client_lodes.pop(existing_conn, None)
                if old_lode_id:
                    self.lode_clients.pop(old_lode_id, None)
                try:
                    existing_conn.close()
                except Exception:
                    pass
                logger.debug(f"Disconnected stale client for lode {lode_id}")

            # Register new owner
            self.lode_clients[lode_id] = conn
            self.client_lodes[conn] = lode_id

        # Set active on the lode
        lode = self._find_lode(lode_id)
        if lode:
            lode["active"] = True
            if tmux_pane:
                lode["tmux_pane"] = tmux_pane
            if pid:
                lode["pid"] = pid
            touch(lode)
            save_lodes(self.lodes)
            self.broadcast({"type": "lode_updated", "lode": lode})

        logger.info(f"Registered client for lode {lode_id}, active=True")

    def _handle_message(self, message: dict, conn: socket.socket) -> None:
        """Handle an incoming message, responding directly if needed."""
        msg_type = message.get("type")

        if msg_type == "connect":
            # Read-only handshake: returns lode data without claiming ownership
            lode_id = message.get("lode_id")
            response: dict = {
                "type": "connected",
                "tmux": self.tmux_location,
            }
            if lode_id:
                lode = self._find_lode(lode_id)
                response["lode"] = lode if lode else None
                response["lode_found"] = lode is not None

            self._send_response(conn, response)

        elif msg_type == "lode_register":
            # Persistent connection claims ownership of a lode (sets active=True)
            lode_id = message.get("lode_id")
            if lode_id:
                lode = self._find_lode(lode_id)
                if lode:
                    tmux_pane = message.get("tmux_pane")
                    pid = message.get("pid")
                    self._register_lode_client(lode_id, conn, tmux_pane, pid)

        elif msg_type == "ping":
            self._send_response(conn, {"type": "pong"})

        elif msg_type == "lode_list":
            self._send_response(conn, {"type": "lode_list", "lodes": self.lodes})

        elif msg_type == "lode_create":
            project = message.get("project", "")
            lode = create_lode(self.lodes, project)
            logger.info(f"Lode {lode['id']} created project={project}")
            self.broadcast({"type": "lode_created", "lode": lode})

        elif msg_type == "lode_set_stage":
            lode_id = message.get("lode_id")
            stage = message.get("stage")
            if lode_id and stage:
                lode = update_lode_stage(self.lodes, lode_id, stage)
                if lode:
                    logger.info(f"Lode {lode_id} stage={stage}")
                    self.broadcast({"type": "lode_updated", "lode": lode})

        elif msg_type == "lode_archive":
            lode_id = message.get("lode_id")
            if lode_id:
                lode = archive_lode(self.lodes, lode_id)
                if lode:
                    self.archived_lodes.append(lode)
                    logger.info(f"Lode {lode_id} archived")
                    self.broadcast({"type": "lode_archived", "lode": lode})

        elif msg_type == "lode_set_state":
            lode_id = message.get("lode_id")
            state = message.get("state")
            status = message.get("status", "")
            if lode_id and state:
                lode = update_lode_state(self.lodes, lode_id, state, status)
                if lode:
                    logger.info(f"Lode {lode_id} state={state} status={status}")
                    self.broadcast({"type": "lode_updated", "lode": lode})

        elif msg_type == "lode_set_status":
            lode_id = message.get("lode_id")
            status = message.get("status", "")
            if lode_id:
                lode = update_lode_status(self.lodes, lode_id, status)
                if lode:
                    logger.info(f"Lode {lode_id} status={status}")
                    self.broadcast({"type": "lode_updated", "lode": lode})

        elif msg_type == "lode_set_title":
            lode_id = message.get("lode_id")
            title = message.get("title", "")
            if lode_id:
                lode = update_lode_title(self.lodes, lode_id, title)
                if lode:
                    logger.info(f"Lode {lode_id} title={title}")
                    self.broadcast({"type": "lode_updated", "lode": lode})

        elif msg_type == "lode_set_auto":
            lode_id = message.get("lode_id")
            auto = bool(message.get("auto", False))
            if lode_id:
                lode = update_lode_auto(self.lodes, lode_id, auto)
                if lode:
                    logger.info(f"Lode {lode_id} auto={auto}")
                    self.broadcast({"type": "lode_updated", "lode": lode})

        elif msg_type == "lode_set_codex_thread":
            lode_id = message.get("lode_id")
            thread_id = message.get("codex_thread_id")
            if lode_id and thread_id:
                lode = update_lode_codex_thread(self.lodes, lode_id, thread_id)
                if lode:
                    logger.info(f"Lode {lode_id} codex_thread={thread_id}")
                    self.broadcast({"type": "lode_updated", "lode": lode})

        elif msg_type == "lode_set_claude_started":
            lode_id = message.get("lode_id")
            claude_stage = message.get("claude_stage")
            if lode_id and claude_stage:
                lode = set_lode_claude_started(self.lodes, lode_id, claude_stage)
                if lode:
                    logger.info(f"Lode {lode_id} claude_started stage={claude_stage}")
                    self.broadcast({"type": "lode_updated", "lode": lode})

        elif msg_type == "lode_reset_claude_stage":
            lode_id = message.get("lode_id")
            claude_stage = message.get("claude_stage")
            if lode_id and claude_stage:
                lode = reset_lode_claude_stage(self.lodes, lode_id, claude_stage)
                if lode:
                    logger.info(f"Lode {lode_id} claude_reset stage={claude_stage}")
                    self.broadcast({"type": "lode_updated", "lode": lode})

        elif msg_type == "backlog_list":
            items_data = [item.to_dict() for item in self.backlog]
            self._send_response(conn, {"type": "backlog_list", "items": items_data})

        elif msg_type == "backlog_add":
            project = message.get("project", "")
            description = message.get("description", "")
            lode_id = message.get("lode_id")
            if project and description:
                item = add_backlog_item(self.backlog, project, description, lode_id)
                logger.info(f"Backlog {item.id} added project={project}")
                self.broadcast({"type": "backlog_added", "item": item.to_dict()})

        elif msg_type == "backlog_remove":
            item_id = message.get("item_id", "")
            item = find_backlog_by_prefix(self.backlog, item_id)
            if item:
                remove_backlog_item(self.backlog, item.id)
                logger.info(
                    f"Backlog {item.id} removed"
                    f" project={item.project} description={item.description}"
                )
                self.broadcast({"type": "backlog_removed", "item": item.to_dict()})

        elif msg_type == "projects_reload":
            self.projects = get_active_projects()
            logger.info("Projects reloaded from disk")

        else:
            logger.warning(f"Unknown message type: {msg_type}")

    def _send_response(self, conn: socket.socket, message: dict) -> None:
        """Send a response directly to a client."""
        if "ts" not in message:
            message["ts"] = current_time_ms()
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
            message["ts"] = current_time_ms()

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
        # Close log file handler
        if self._log_handler:
            hopper_logger = logging.getLogger("hopper")
            hopper_logger.removeHandler(self._log_handler)
            self._log_handler.close()
            self._log_handler = None


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
