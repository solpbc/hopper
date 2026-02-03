"""Ore runner - wraps Claude execution with session lifecycle management."""

import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

from hopper import prompt
from hopper.client import HopperConnection, connect
from hopper.projects import find_project

logger = logging.getLogger(__name__)

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
        self.connection: HopperConnection | None = None
        self.is_new_session = False  # Set during run() based on server state
        self.project_name: str = ""  # Project name for prompt context
        self.project_dir: str = ""  # Project directory for prompt context
        self.scope: str = ""  # User's task scope description

    def run(self) -> int:
        """Run Claude for this session. Returns exit code."""
        # Set up signal handlers for graceful shutdown
        original_sigint = signal.signal(signal.SIGINT, self._handle_signal)
        original_sigterm = signal.signal(signal.SIGTERM, self._handle_signal)

        try:
            # Query server for session to get state and project info (one-shot)
            response = connect(self.socket_path, session_id=self.session_id)
            if response:
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

                    # Get scope for prompt context
                    self.scope = session_data.get("scope", "")

            # Start persistent connection for state updates
            self.connection = HopperConnection(self.socket_path)
            self.connection.start()

            # Run Claude (blocking) - notifies "running" after successful start
            exit_code, error_msg = self._run_claude()

            # Only emit error state explicitly - server handles idle on disconnect
            if exit_code == 127:
                self._emit_state("error", error_msg or "Command not found")
            elif exit_code != 0 and exit_code != 130:
                # Non-zero exit (except interrupt) - set error state
                msg = error_msg or f"Exited with code {exit_code}"
                self._emit_state("error", msg)
            # For success (0) or interrupt (130), let server handle via disconnect

            return exit_code

        finally:
            # Restore original signal handlers
            signal.signal(signal.SIGINT, original_sigint)
            signal.signal(signal.SIGTERM, original_sigterm)

            # Stop persistent connection (drains queue first)
            if self.connection:
                self.connection.stop()

    def _handle_signal(self, signum: int, frame) -> None:
        """Handle shutdown signals gracefully."""
        logger.debug(f"Received signal {signum}")
        # Server handles state transition to idle on disconnect
        # Re-raise as KeyboardInterrupt so subprocess handling works correctly
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        sys.exit(128 + signum)

    def _emit_state(self, state: str, status: str) -> None:
        """Emit state change to server via persistent connection."""
        if self.connection:
            self.connection.emit(
                "session_set_state",
                session_id=self.session_id,
                state=state,
                status=status,
            )
            logger.debug(f"Emitted state: {state}, status: {status}")

    def _run_claude(self) -> tuple[int, str | None]:
        """Run Claude with the session ID. Returns (exit_code, error_message)."""
        # Validate project directory exists
        cwd: str | None = None
        if self.project_dir:
            if not Path(self.project_dir).is_dir():
                return 1, f"Project directory not found: {self.project_dir}"
            cwd = self.project_dir

        # Set environment
        env = os.environ.copy()
        env["HOPPER_SID"] = self.session_id

        # Build command - use --resume for existing sessions, prompt for new
        if self.is_new_session:
            # Pass project and scope info as template context
            context = {}
            if self.project_name:
                context["project"] = self.project_name
            if self.project_dir:
                context["dir"] = self.project_dir
            if self.scope:
                context["scope"] = self.scope
            initial_prompt = prompt.load("shovel", context=context if context else None)
            cmd = ["claude", "--session-id", self.session_id, initial_prompt]
        else:
            cmd = ["claude", "--resume", self.session_id]

        logger.debug(f"Running: {' '.join(cmd)}")

        try:
            # Start Claude process, capturing stderr for error messages
            proc = subprocess.Popen(cmd, env=env, stderr=subprocess.PIPE, cwd=cwd)

            # Notify server we're running (after successful process start)
            self._emit_state("running", "Claude running")

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
