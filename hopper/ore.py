"""Ore runner - wraps Claude execution with session lifecycle management."""

import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from hopper.client import get_session_state, ping, set_session_state

logger = logging.getLogger(__name__)

RECONNECT_INTERVAL = 2.0  # seconds between reconnection attempts


class OreRunner:
    """Runs Claude for a session, managing active/inactive state."""

    def __init__(self, session_id: str, socket_path: Path):
        self.session_id = session_id
        self.socket_path = socket_path
        self.stop_event = threading.Event()
        self.server_connected = False
        self.background_thread: threading.Thread | None = None
        self.is_new_session = False  # Set during run() based on server state

    def run(self) -> int:
        """Run Claude for this session. Returns exit code."""
        # Set up signal handlers for graceful shutdown
        original_sigint = signal.signal(signal.SIGINT, self._handle_signal)
        original_sigterm = signal.signal(signal.SIGTERM, self._handle_signal)

        try:
            # Query server for session state to determine if this is a new session
            state = get_session_state(self.socket_path, self.session_id)
            self.is_new_session = state == "new"
            self.server_connected = state is not None

            # Start background thread for server connection management
            self.background_thread = threading.Thread(
                target=self._background_loop, name="ore-background", daemon=True
            )
            self.background_thread.start()

            # Run Claude (blocking) - notifies "running" after successful start
            exit_code, error_msg = self._run_claude()

            # Notify server we're done with appropriate message
            if exit_code == 0:
                self._notify_state("idle", "Completed successfully")
            elif exit_code == 127:
                self._notify_state("error", error_msg or "Command not found")
            elif exit_code == 130:
                self._notify_state("idle", "Interrupted")
            else:
                self._notify_state("error", f"Exited with code {exit_code}")

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
                if ping(self.socket_path, timeout=1.0):
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

    def _run_claude(self) -> tuple[int, str | None]:
        """Run Claude with the session ID. Returns (exit_code, error_message)."""
        # Set environment
        env = os.environ.copy()
        env["HOPPER_SID"] = self.session_id

        # Build command - use --resume for existing sessions
        if self.is_new_session:
            cmd = ["claude"]
        else:
            cmd = ["claude", "--resume", self.session_id]

        logger.debug(f"Running: {' '.join(cmd)}")

        try:
            # Start Claude process
            proc = subprocess.Popen(cmd, env=env)

            # Notify server we're running (after successful process start)
            self._notify_active()

            # Wait for Claude to complete
            proc.wait()
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
