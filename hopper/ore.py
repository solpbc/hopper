"""Ore runner - wraps Claude execution with session lifecycle management."""

import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from hopper import prompt
from hopper.client import connect, set_session_state
from hopper.projects import find_project

logger = logging.getLogger(__name__)

RECONNECT_INTERVAL = 2.0  # seconds between reconnection attempts
ERROR_LINES = 5  # Number of stderr lines to capture on error


def _extract_error_message(stderr_bytes: bytes) -> str | None:
    """Extract last N lines from stderr as error message.

    Args:
        stderr_bytes: Raw stderr output from subprocess

    Returns:
        Last ERROR_LINES lines joined with newlines, or None if empty
    """
    if not stderr_bytes:
        return None

    text = stderr_bytes.decode("utf-8", errors="replace")
    lines = text.strip().splitlines()
    if not lines:
        return None

    # Take last N lines, preserve newlines
    tail = lines[-ERROR_LINES:]
    return "\n".join(tail)


class OreRunner:
    """Runs Claude for a session, managing active/inactive state."""

    def __init__(self, session_id: str, socket_path: Path):
        self.session_id = session_id
        self.socket_path = socket_path
        self.stop_event = threading.Event()
        self.server_connected = False
        self.background_thread: threading.Thread | None = None
        self.is_new_session = False  # Set during run() based on server state
        self.project_name: str = ""  # Project name for prompt context
        self.project_dir: str = ""  # Project directory for prompt context

    def run(self) -> int:
        """Run Claude for this session. Returns exit code."""
        # Set up signal handlers for graceful shutdown
        original_sigint = signal.signal(signal.SIGINT, self._handle_signal)
        original_sigterm = signal.signal(signal.SIGTERM, self._handle_signal)

        try:
            # Query server for session to get state and project info
            response = connect(self.socket_path, session_id=self.session_id)
            if response:
                self.server_connected = True
                session_data = response.get("session")
                if session_data:
                    state = session_data.get("state")
                    self.is_new_session = state == "new"

                    # Get project info for prompt context
                    project_name = session_data.get("project", "")
                    if project_name:
                        self.project_name = project_name
                        project = find_project(project_name)
                        if project:
                            self.project_dir = project.path

            # Start background thread for server connection management
            self.background_thread = threading.Thread(
                target=self._background_loop, name="ore-background", daemon=True
            )
            self.background_thread.start()

            # Run Claude (blocking) - notifies "running" after successful start
            exit_code, error_msg = self._run_claude()

            # Notify server we're done with appropriate message (blocks until delivered)
            if exit_code == 0:
                self._notify_state_blocking("idle", "Completed successfully")
            elif exit_code == 127:
                self._notify_state_blocking("error", error_msg or "Command not found")
            elif exit_code == 130:
                self._notify_state_blocking("idle", "Interrupted")
            else:
                # Use captured stderr if available, otherwise generic message
                msg = error_msg or f"Exited with code {exit_code}"
                self._notify_state_blocking("error", msg)

            return exit_code

        finally:
            # Restore original signal handlers
            signal.signal(signal.SIGINT, original_sigint)
            signal.signal(signal.SIGTERM, original_sigterm)

            # Stop background thread
            self.stop_event.set()
            if self.background_thread and self.background_thread.is_alive():
                self.background_thread.join(timeout=1.0)

    def _handle_signal(self, signum: int, frame) -> None:
        """Handle shutdown signals gracefully."""
        logger.debug(f"Received signal {signum}")
        # Notify server we're going idle before exiting
        self._notify_state("idle", "Interrupted")
        self.stop_event.set()
        # Re-raise as KeyboardInterrupt so subprocess handling works correctly
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        sys.exit(128 + signum)

    def _background_loop(self) -> None:
        """Background thread for maintaining server connection."""
        while not self.stop_event.wait(timeout=RECONNECT_INTERVAL):
            if not self.server_connected:
                # Try to reconnect
                if connect(self.socket_path, timeout=1.0) is not None:
                    logger.debug("Reconnected to server")
                    self.server_connected = True
                    # Re-notify our state
                    self._notify_active()

    def _notify_active(self) -> None:
        """Notify server that session is now running."""
        self._notify_state("running", "Claude running")

    def _notify_state(self, state: str, message: str) -> None:
        """Notify server of state change with message."""
        if set_session_state(self.socket_path, self.session_id, state, message):
            self.server_connected = True
            logger.debug(f"Notified server: state={state}, message={message}")
        else:
            self.server_connected = False
            logger.debug(f"Failed to notify server: state={state}")

    def _notify_state_blocking(self, state: str, message: str) -> None:
        """Notify server of state change, retrying until success or interrupted."""
        while not self.stop_event.is_set():
            if set_session_state(self.socket_path, self.session_id, state, message):
                self.server_connected = True
                logger.debug(f"Notified server: state={state}, message={message}")
                return
            logger.debug(f"Failed to notify server, retrying in {RECONNECT_INTERVAL}s")
            self.stop_event.wait(timeout=RECONNECT_INTERVAL)

    def _run_claude(self) -> tuple[int, str | None]:
        """Run Claude with the session ID. Returns (exit_code, error_message)."""
        # Set environment
        env = os.environ.copy()
        env["HOPPER_SID"] = self.session_id

        # Build command - use --resume for existing sessions, prompt for new
        if self.is_new_session:
            # Pass project info as template context
            context = {}
            if self.project_name:
                context["project"] = self.project_name
            if self.project_dir:
                context["dir"] = self.project_dir
            initial_prompt = prompt.load("shovel", context=context if context else None)
            cmd = ["claude", "--session-id", self.session_id, initial_prompt]
        else:
            cmd = ["claude", "--resume", self.session_id]

        logger.debug(f"Running: {' '.join(cmd)}")

        try:
            # Start Claude process, capturing stderr for error messages
            proc = subprocess.Popen(cmd, env=env, stderr=subprocess.PIPE)

            # Notify server we're running (after successful process start)
            self._notify_active()

            # Wait for Claude to complete
            proc.wait()

            # On non-zero exit, extract last 5 lines of stderr as error message
            if proc.returncode != 0 and proc.stderr:
                stderr_bytes = proc.stderr.read()
                error_msg = _extract_error_message(stderr_bytes)
                return proc.returncode, error_msg

            return proc.returncode, None
        except FileNotFoundError:
            logger.error("claude command not found")
            return 127, "claude command not found"
        except KeyboardInterrupt:
            return 130, None  # Standard exit code for SIGINT


def run_ore(session_id: str, socket_path: Path) -> int:
    """Entry point for ore command."""
    runner = OreRunner(session_id, socket_path)
    return runner.run()
