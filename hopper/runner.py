"""Base runner - shared logic for ore and refine runners."""

import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from hopper.client import HopperConnection, connect
from hopper.lodes import current_time_ms
from hopper.projects import find_project
from hopper.tmux import capture_pane, get_current_pane_id, rename_window, send_keys

logger = logging.getLogger(__name__)

ERROR_LINES = 5  # Number of stderr lines to capture on error
MONITOR_INTERVAL = 5.0  # Seconds between activity checks
MONITOR_INTERVAL_MS = int(MONITOR_INTERVAL * 1000)


def extract_error_message(stderr_bytes: bytes) -> str | None:
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

    tail = lines[-ERROR_LINES:]
    return "\n".join(tail)


class BaseRunner:
    """Base class for lode runners (ore, refine).

    Provides the full run lifecycle: signal handling, server communication,
    subprocess management, activity monitoring, completion detection, and
    auto-dismiss.

    Subclasses configure behavior via class attributes and implement:
    - _setup(): Pre-flight validation and setup. Return int to bail.
    - _build_command(): Return (cmd, cwd) for the Claude subprocess.
    """

    # Subclasses set these to customize behavior
    _done_label: str = "done"
    _first_run_state: str = "new"
    _done_status: str = "Done"
    _next_stage: str = ""
    _always_dismiss: bool = False

    def __init__(self, lode_id: str, socket_path: Path):
        self.lode_id = lode_id
        self.socket_path = socket_path
        self.connection: HopperConnection | None = None
        self.is_first_run = False
        self.project_name: str = ""
        self.project_dir: str = ""
        # Activity monitor state
        self._monitor_thread: threading.Thread | None = None
        self._monitor_stop = threading.Event()
        self._last_snapshot: str | None = None
        self._stuck_since: int | None = None
        self._pane_id: str | None = None
        # Completion tracking
        self._done = threading.Event()

    def run(self) -> int:
        """Run Claude for this lode. Returns exit code."""
        original_sigint = signal.signal(signal.SIGINT, self._handle_signal)
        original_sigterm = signal.signal(signal.SIGTERM, self._handle_signal)

        try:
            # Query server for lode state and project info
            response = connect(self.socket_path, lode_id=self.lode_id)
            if not response:
                print(f"Failed to connect to server for lode {self.lode_id}")
                return 1

            lode_data = response.get("lode")
            if not lode_data:
                print(f"Lode {self.lode_id} not found")
                return 1

            if lode_data.get("active", False):
                logger.error(f"Lode {self.lode_id} already has an active connection")
                print(f"Lode {self.lode_id} is already active")
                return 1

            state = lode_data.get("state")
            self.is_first_run = state == self._first_run_state

            project_name = lode_data.get("project", "")
            if project_name:
                self.project_name = project_name
                project = find_project(project_name)
                if project:
                    self.project_dir = project.path

            # Let subclass extract additional data
            self._load_lode_data(lode_data)

            # Subclass pre-flight validation and setup
            err = self._setup()
            if err is not None:
                return err

            # Start persistent connection and register ownership
            self.connection = HopperConnection(self.socket_path)
            self.connection.start(
                callback=self._on_server_message,
                on_connect=lambda: self.connection.emit(
                    "lode_register",
                    lode_id=self.lode_id,
                    tmux_pane=get_current_pane_id(),
                ),
            )

            # Run Claude (blocking)
            exit_code, error_msg = self._run_claude()

            if exit_code == 127:
                self._emit_state("error", error_msg or "Command not found")
            elif exit_code != 0 and exit_code != 130:
                msg = error_msg or f"Exited with code {exit_code}"
                self._emit_state("error", msg)
            elif exit_code == 0 and self._done.is_set():
                self._emit_state("ready", self._done_status)
                if self._next_stage:
                    self._emit_stage(self._next_stage)

            return exit_code

        finally:
            self._stop_monitor()
            signal.signal(signal.SIGINT, original_sigint)
            signal.signal(signal.SIGTERM, original_sigterm)
            if self.connection:
                self.connection.stop()

    def _load_lode_data(self, lode_data: dict) -> None:
        """Extract additional fields from lode data. Override in subclasses."""
        pass

    def _setup(self) -> int | None:
        """Pre-flight validation and setup. Return int exit code to bail, None to continue."""
        return None

    def _build_command(self) -> tuple[list[str], str | None]:
        """Build the Claude command and working directory.

        Returns:
            (cmd, cwd) tuple. Subclasses must implement this.
        """
        raise NotImplementedError

    def _get_subprocess_env(self) -> dict:
        """Build environment for subprocess. Subclasses can override to add venv."""
        env = os.environ.copy()
        env["HOPPER_LID"] = self.lode_id
        return env

    def _run_claude(self) -> tuple[int, str | None]:
        """Run Claude subprocess. Returns (exit_code, error_message)."""
        cmd, cwd = self._build_command()

        env = self._get_subprocess_env()

        logger.debug(f"Running: {' '.join(cmd[:3])}...")

        try:
            proc = subprocess.Popen(cmd, env=env, stderr=subprocess.PIPE, cwd=cwd)

            self._emit_state("running", "Claude running")
            self._start_monitor()

            # Start dismiss thread if configured
            should_dismiss = self._always_dismiss or self.is_first_run
            if should_dismiss and self._pane_id:
                threading.Thread(
                    target=self._wait_and_dismiss_claude,
                    name=f"{self._done_label.lower().replace(' ', '-')}-dismiss",
                    daemon=True,
                ).start()

            proc.wait()

            if proc.returncode != 0 and proc.stderr:
                stderr_bytes = proc.stderr.read()
                error_msg = extract_error_message(stderr_bytes)
                return proc.returncode, error_msg

            return proc.returncode, None
        except FileNotFoundError:
            logger.error("claude command not found")
            return 127, "claude command not found"
        except KeyboardInterrupt:
            return 130, None

    def _handle_signal(self, signum: int, frame) -> None:
        """Handle shutdown signals gracefully."""
        logger.debug(f"Received signal {signum}")
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        sys.exit(128 + signum)

    def _emit_state(self, state: str, status: str) -> None:
        """Emit state change to server via persistent connection."""
        if self.connection:
            self.connection.emit(
                "lode_set_state",
                lode_id=self.lode_id,
                state=state,
                status=status,
            )
            logger.debug(f"Emitted state: {state}, status: {status}")

    def _emit_stage(self, stage: str) -> None:
        """Emit stage change to server via persistent connection."""
        if self.connection:
            self.connection.emit(
                "lode_update",
                lode_id=self.lode_id,
                stage=stage,
            )
            logger.debug(f"Emitted stage: {stage}")

    def _on_server_message(self, message: dict) -> None:
        """Handle incoming server broadcast messages."""
        if message.get("type") != "lode_state_changed":
            return
        lode = message.get("lode", {})
        if lode.get("id") != self.lode_id:
            return
        if lode.get("state") == "completed":
            self._done.set()
            logger.debug(f"{self._done_label} signal received")

    def _wait_and_dismiss_claude(self) -> None:
        """Wait for completion, screen stability, then send Ctrl-D to exit Claude."""
        while not self._done.wait(timeout=1.0):
            if self._monitor_stop.is_set():
                return

        if not self._pane_id:
            return

        logger.debug(f"{self._done_label}, waiting for screen to stabilize")

        last_snapshot = None
        while not self._monitor_stop.is_set():
            self._monitor_stop.wait(MONITOR_INTERVAL)
            snapshot = capture_pane(self._pane_id)
            if snapshot is None:
                return
            if snapshot == last_snapshot:
                break
            last_snapshot = snapshot

        if self._monitor_stop.is_set():
            return

        logger.debug("Screen stable, sending Ctrl-D")
        send_keys(self._pane_id, "C-d")
        send_keys(self._pane_id, "C-d")

    def _start_monitor(self) -> None:
        """Start the activity monitor thread."""
        self._pane_id = get_current_pane_id()
        if not self._pane_id:
            logger.debug("Not in tmux, skipping activity monitor")
            return

        rename_window(self._pane_id, self.lode_id)
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="activity-monitor", daemon=True
        )
        self._monitor_thread.start()
        logger.debug(f"Started activity monitor for pane {self._pane_id}")

    def _stop_monitor(self) -> None:
        """Stop the activity monitor thread."""
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_stop.set()
            self._monitor_thread.join(timeout=1.0)
            logger.debug("Stopped activity monitor")

    def _monitor_loop(self) -> None:
        """Monitor loop that checks for activity every MONITOR_INTERVAL seconds."""
        while not self._monitor_stop.wait(MONITOR_INTERVAL):
            self._check_activity()

    def _check_activity(self) -> None:
        """Check tmux pane for activity and update state accordingly."""
        if not self._pane_id:
            return

        # Skip stuck detection once done — dismiss thread handles exit
        if self._done.is_set():
            return

        snapshot = capture_pane(self._pane_id)
        if snapshot is None:
            logger.debug("Failed to capture pane, stopping monitor")
            self._monitor_stop.set()
            return

        if snapshot == self._last_snapshot:
            now = current_time_ms()
            if self._stuck_since is None:
                self._stuck_since = now - MONITOR_INTERVAL_MS
            duration_sec = (now - self._stuck_since) // 1000
            self._emit_state("stuck", f"No output for {duration_sec}s")
        else:
            if self._stuck_since is not None:
                self._emit_state("running", "Claude running")
            self._stuck_since = None
            self._last_snapshot = snapshot
