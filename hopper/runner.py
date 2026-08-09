# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Base runner - shared lifecycle logic for the process runner."""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from hopper.cleanup import reap_swiftpm_testing_helpers
from hopper.client import RUN_GENERATION_ENV, HopperConnection, connect
from hopper.lodes import current_time_ms, format_duration_ms, format_park_status, get_lode_dir
from hopper.projects import find_project
from hopper.tmux import (
    capture_pane,
    get_current_pane_id,
    pane_needs_answer,
    rename_window,
    send_keys,
)
from hopper.workspace_trust import WorkspaceTrustError, trust_claude_workspace

logger = logging.getLogger(__name__)

ERROR_LINES = 5  # Number of stderr lines to capture on error
MONITOR_INTERVAL = 5.0  # Seconds between activity checks
MONITOR_INTERVAL_MS = 5000
IDLE_THRESHOLD_MS = 50_000
STUCK_PARK_THRESHOLD_MS = 5 * 60_000
ABSOLUTE_CAP_MS = 60 * 60_000
DISMISS_STABILIZATION_TIMEOUT_SEC = 30.0
PANE_CAPTURE_FAILURE_LIMIT = 3
DISMISS_DEADLINE_MS = 5 * 60_000
DISMISS_DEADLINE_MIN = DISMISS_DEADLINE_MS // 60_000
PANE_ACTIVITY_EMIT_INTERVAL_MS = 30_000
DESCENDANT_TERM_GRACE_SEC = 5.0
DESCENDANT_POLL_INTERVAL_SEC = 0.1
REGISTRATION_TIMEOUT_SEC = 30.0
BRANCH_PERSIST_TIMEOUT_SEC = 5.0
PS_SCAN_TIMEOUT_SEC = 5.0


def _write_recovery_record(lode_id: str, record: dict) -> None:
    """Atomically persist a park recovery record for a lode."""
    lode_dir = get_lode_dir(lode_id)
    lode_dir.mkdir(parents=True, exist_ok=True)
    recovery_path = lode_dir / "recovery.json"
    tmp_path = recovery_path.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, recovery_path)


def _parse_ps_time(raw: str) -> float | None:
    """Parse ps CPU time into seconds."""
    try:
        text = raw.strip()
        if not text:
            return None
        days = 0
        if "-" in text:
            day_text, text = text.split("-", 1)
            days = int(day_text)
        parts = text.split(":")
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1])
            return days * 86400 + minutes * 60 + seconds
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
            return days * 86400 + hours * 3600 + minutes * 60 + seconds
    except (TypeError, ValueError):
        return None
    return None


def _walk_descendant_pids(root_pid: int, children: dict[int, list[int]]) -> list[int]:
    """Walk a parent-to-children map, excluding root_pid from the result."""
    descendants: list[int] = []
    seen = {root_pid}
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        descendants.append(pid)
        stack.extend(children.get(pid, []))
    return descendants


def _descendant_pids(root_pid: int) -> list[int]:
    """Return all descendant process IDs of root_pid."""
    try:
        result = subprocess.run(
            ["ps", "-Ao", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=PS_SCAN_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            f"ps failed; descendant cleanup degraded to parent-only ({type(exc).__name__}: {exc})"
        )
        return []
    if result.returncode != 0:
        logger.warning(
            f"ps failed; descendant cleanup degraded to parent-only (exit code {result.returncode})"
        )
        return []

    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    return _walk_descendant_pids(root_pid, children)


def _process_tree_cpu_ms(root_pid: int | None, *, include_root: bool) -> int | None:
    """Return cumulative CPU time for a process tree."""
    if root_pid is None:
        return None

    try:
        result = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,time="],
            capture_output=True,
            text=True,
            timeout=PS_SCAN_TIMEOUT_SEC,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    children: dict[int, list[int]] = {}
    times: dict[int, float] = {}
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) != 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        parsed = _parse_ps_time(parts[2])
        if parsed is None:
            continue
        children.setdefault(ppid, []).append(pid)
        times[pid] = parsed

    pids = _walk_descendant_pids(root_pid, children)
    if include_root:
        pids.append(root_pid)
    total = sum(times.get(pid, 0.0) for pid in pids)
    return int(total * 1000)


def _sum_descendant_cpu_ms(root_pid: int | None) -> int | None:
    """Return cumulative CPU time for descendants of root_pid, excluding root_pid."""
    return _process_tree_cpu_ms(root_pid, include_root=False)


def _sum_process_tree_cpu_ms(root_pid: int | None) -> int | None:
    """Return cumulative CPU time for root_pid and all descendants."""
    return _process_tree_cpu_ms(root_pid, include_root=True)


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
    """Base class for lode runners.

    Provides the full run lifecycle: signal handling, server communication,
    subprocess management, activity monitoring, completion detection, and
    auto-dismiss.

    Subclasses configure behavior via class attributes and implement:
    - _setup(): Pre-flight validation and setup. Return int to bail.
    - _build_command(): Return (cmd, cwd) for the Claude subprocess.
    """

    # Subclasses set these to customize behavior
    _done_label: str = "done"
    _claude_stage: str = ""  # Key into lode["claude"] dict ("mill", "refine", "ship")
    _done_status: str = "Done"
    _next_stage: str = ""

    def __init__(
        self,
        lode_id: str,
        socket_path: Path,
        *,
        run_generation: str | None = None,
        armed_mode: str = "non-linux",
        actual_unit: str | None = None,
    ):
        self.lode_id = lode_id
        self.socket_path = socket_path
        self.run_generation = run_generation
        self.armed_mode = armed_mode
        self.actual_unit = actual_unit
        self.connection: HopperConnection | None = None
        self._registration_complete = threading.Event()
        self._registration_accepted = False
        self._expected_lode_branch: str | None = None
        self._branch_persisted = threading.Event()
        self.is_first_run = False
        self.claude_session_id: str = ""
        self.project_name: str = ""
        self.project_dir: str = ""
        # Activity monitor state
        self._monitor_thread: threading.Thread | None = None
        self._monitor_stop = threading.Event()
        self._last_snapshot: str | None = None
        self._stuck_since: int | None = None
        self._last_descendant_cpu_ms: int | None = None
        self._last_cpu_activity_ms: int | None = None
        self._last_pane_activity_ms: int | None = None
        self._last_pane_activity_emitted_ms: int | None = None
        self._pane_capture_failures = 0
        self._pane_capture_outage_started_ms: int | None = None
        self._activity_capture_disabled = False
        self._pane_id: str | None = None
        self._claude_proc: subprocess.Popen | None = None
        # Completion tracking
        self._done = threading.Event()
        # _done_lock guards the timestamp/latch pair. Completion signalled while
        # parked re-bases the timestamp and clears the latch to re-arm the deadline.
        self._done_lock = threading.Lock()
        self._done_at_ms: int | None = None
        self._dismiss_deadline_parked = False
        self._dismiss_attempt = 0
        self._gated = threading.Event()
        # Gate resume detector: the pane as it settled *after* the gate opened,
        # and whether we have seen it hold still long enough to trust a change.
        self._gate_snapshot: str | None = None
        self._gate_armed = False
        self._gate_epoch = 0
        self._setup_error: str | None = None

    def run(self) -> int:
        """Run Claude for this lode. Returns exit code."""
        original_sigint = signal.signal(signal.SIGINT, self._handle_signal)
        original_sigterm = signal.signal(signal.SIGTERM, self._handle_signal)

        try:
            try:
                reap_swiftpm_testing_helpers()
                logger.info(f"run start lode={self.lode_id}")
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

                # Read per-stage Claude session info
                claude_info = lode_data.get("claude", {}).get(self._claude_stage, {})
                self.claude_session_id = claude_info.get("session_id", "")
                self.is_first_run = not claude_info.get("started", False)
                if self.is_first_run:
                    logger.debug(f"first run detected lode={self.lode_id}")

                project_name = lode_data.get("project", "")
                if project_name:
                    self.project_name = project_name
                    project = find_project(project_name)
                    if project:
                        self.project_dir = project.path

                # Let subclass extract additional data
                self._load_lode_data(lode_data)
                logger.info(f"lode loaded lode={self.lode_id} first_run={self.is_first_run}")

                # Start persistent connection and register ownership
                self.connection = HopperConnection(
                    self.socket_path,
                    run_generation=self.run_generation,
                )
                self.connection.start(
                    callback=self._on_server_message,
                    on_connect=lambda: self.connection.emit(
                        "lode_register",
                        lode_id=self.lode_id,
                        tmux_pane=get_current_pane_id(),
                        pid=os.getpid(),
                        armed_mode=self.armed_mode,
                        actual_unit=self.actual_unit,
                    ),
                )
                if not self._registration_complete.wait(REGISTRATION_TIMEOUT_SEC):
                    logger.error(f"registration timed out lode={self.lode_id}")
                    print(f"Failed to register lode {self.lode_id}: server response timed out")
                    return 1
                if not self._registration_accepted:
                    logger.error(f"registration refused lode={self.lode_id}")
                    print(f"Failed to register lode {self.lode_id}: server refused ownership")
                    return 1

                # Subclass pre-flight validation and setup
                err = self._setup()
                if err is not None:
                    logger.info(f"setup failed lode={self.lode_id}")
                    emitted = self._emit_state("error", self._setup_error or "Setup failed")
                    return 0 if emitted else 1
                logger.info(f"setup complete lode={self.lode_id}")

                # Run Claude (blocking)
                exit_code, error_msg = self._run_claude()
                logger.info(f"claude exited lode={self.lode_id} exit_code={exit_code}")

                if exit_code == 127:
                    logger.error(
                        f"claude error lode={self.lode_id} exit_code={exit_code}: {error_msg}"
                    )
                    msg = error_msg or "Command not found"
                    print(f"Error [{self.lode_id}]: {msg}")
                    emitted = self._emit_state("error", msg)
                    return 0 if emitted else 1
                elif exit_code != 0 and exit_code != 130:
                    logger.error(
                        f"claude error lode={self.lode_id} exit_code={exit_code}: {error_msg}"
                    )
                    msg = error_msg or f"Exited with code {exit_code}"
                    print(f"Error [{self.lode_id}]: {msg}")
                    emitted = self._emit_state("error", msg)
                    return 0 if emitted else 1
                elif exit_code == 0 and self._done.is_set():
                    logger.info(f"stage transition lode={self.lode_id}")
                    self._emit_state("ready", self._done_status)
                    if self._next_stage:
                        self._emit_stage(self._next_stage)

                return exit_code
            except Exception as exc:
                print(f"Error [{self.lode_id}]: {exc}")
                logger.exception(f"unexpected error lode={self.lode_id}")
                emitted = False
                try:
                    emitted = self._emit_state("error", str(exc))
                except Exception:
                    pass
                return 0 if emitted else 1

        finally:
            self._stop_monitor()
            try:
                self._terminate_claude_process()
            except Exception:
                logger.exception(f"child cleanup failed lode={self.lode_id}")
            reap_swiftpm_testing_helpers()
            signal.signal(signal.SIGINT, original_sigint)
            signal.signal(signal.SIGTERM, original_sigterm)
            if self.connection:
                self.connection.stop()
            logger.debug(f"cleanup complete lode={self.lode_id}")

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
        if self.run_generation:
            env[RUN_GENERATION_ENV] = self.run_generation
        # Hopper lodes are scoped by their prompt and repo context; do not let
        # Claude Code read/write project auto-memory during managed stages.
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        env["CLAUDE_CODE_DISABLE_MEMORY_PERIODIC_RESYNC"] = "1"
        env["CLAUDE_CODE_DISABLE_MEMORY_BULK_INFLATE"] = "1"
        # A lode pane is machine-read, so grayed-out prompt suggestions are scrape noise.
        env["CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION"] = "false"
        return env

    def _run_claude(self) -> tuple[int, str | None]:
        """Run Claude subprocess. Returns (exit_code, error_message)."""
        cmd, cwd = self._build_command()

        env = self._get_subprocess_env()

        logger.debug(f"Running: {' '.join(cmd[:3])}...")

        try:
            trust_root = trust_claude_workspace(cwd, env)
            if trust_root is not None:
                logger.debug(f"Claude workspace pre-trusted lode={self.lode_id} path={trust_root}")
        except WorkspaceTrustError as exc:
            message = f"Failed to pre-trust Claude workspace: {exc}"
            logger.error(f"workspace trust failed lode={self.lode_id}: {exc}")
            return 1, message

        try:
            proc = subprocess.Popen(
                cmd,
                env=env,
                stderr=subprocess.PIPE,
                cwd=cwd,
            )
            self._claude_proc = proc

            if self.is_first_run:
                self._emit_claude_started()
            self._emit_state("running", "Claude running")
            self._start_monitor()

            if self._pane_id:
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
        finally:
            if self._claude_proc is not None and self._claude_proc.poll() is not None:
                self._claude_proc = None

    def _handle_signal(self, signum: int, frame) -> None:
        """Handle shutdown signals gracefully."""
        logger.debug(f"Received signal {signum}")
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        sys.exit(128 + signum)

    def _persist_lode_branch(self, branch: str) -> bool:
        """Persist branch metadata and wait for its post-save broadcast."""
        self._branch_persisted.clear()
        self._expected_lode_branch = branch
        try:
            if not self.connection:
                return False
            if not self.connection.emit(
                "lode_set_branch",
                lode_id=self.lode_id,
                branch=branch,
            ):
                return False
            return self._branch_persisted.wait(BRANCH_PERSIST_TIMEOUT_SEC)
        finally:
            self._expected_lode_branch = None

    def _emit_state(
        self,
        state: str,
        status: str,
        *,
        gate_epoch: int | None = None,
    ) -> bool:
        """Emit state change to server via persistent connection."""
        if self.connection:
            fields = {"lode_id": self.lode_id, "state": state, "status": status}
            if gate_epoch is not None:
                fields["gate_epoch"] = gate_epoch
            emitted = self.connection.emit("lode_set_state", **fields)
            logger.debug(f"Emitted state: {state}, status: {status}")
            return emitted
        return False

    def _emit_stage(self, stage: str) -> None:
        """Emit stage change to server via persistent connection."""
        if self.connection:
            self.connection.emit(
                "lode_set_stage",
                lode_id=self.lode_id,
                stage=stage,
            )
            logger.debug(f"Emitted stage: {stage}")

    def _emit_claude_started(self) -> None:
        """Mark this stage's Claude session as started on the server."""
        if self.connection:
            self.connection.emit(
                "lode_set_claude_started",
                lode_id=self.lode_id,
                claude_stage=self._claude_stage,
            )
            logger.debug(f"Emitted claude started for stage: {self._claude_stage}")

    def _on_server_message(self, message: dict) -> None:
        """Handle incoming server broadcast messages."""
        if message.get("type") == "lode_registered":
            if message.get("lode_id") == self.lode_id:
                self._registration_accepted = True
                self._registration_complete.set()
            return
        if message.get("type") == "lode_register_refused":
            if message.get("lode_id") == self.lode_id:
                self._registration_accepted = False
                self._registration_complete.set()
            return
        if message.get("type") != "lode_updated":
            return
        lode = message.get("lode", {})
        if lode.get("id") != self.lode_id:
            return
        if "branch" in lode:
            observed_branch = lode["branch"]
            if (
                self._expected_lode_branch is not None
                and observed_branch == self._expected_lode_branch
            ):
                self._branch_persisted.set()
        if lode.get("state") == "completed":
            now = current_time_ms()
            with self._done_lock:
                if not self._done.is_set():
                    self._done_at_ms = now
                    self._done.set()
                elif self._dismiss_deadline_parked:
                    # Completion signalled a second time while parked proves the agent
                    # resumed and reached a fresh terminal point. Pane activity alone
                    # cannot distinguish resumed work from incidental terminal output.
                    self._done_at_ms = now
                    self._dismiss_deadline_parked = False
            logger.debug(f"{self._done_label} signal received lode={self.lode_id}")
        elif lode.get("state") == "gated":
            self._open_gate()
            logger.debug(f"gate signal received lode={self.lode_id}")
        elif lode.get("state") == "running":
            self._clear_gate()
        # Adopt the epoch only after state handling disarms the gate detector. Until then,
        # an armed monitor must emit the old epoch so the server rejects a stale resume.
        self._gate_epoch = lode.get("gate_epoch", 0)

    def _wait_and_dismiss_claude(self) -> None:
        """Wait for completion, then dismiss Claude with bounded keystroke retries."""
        while not self._done.is_set():
            self._done.wait(timeout=1.0)
            if self._monitor_stop.is_set():
                return

        if not self._pane_id:
            return

        while not self._monitor_stop.is_set():
            with self._done_lock:
                deadline_parked = self._dismiss_deadline_parked
                done_at_ms = self._done_at_ms
            while deadline_parked:
                if self._monitor_stop.wait(timeout=1.0):
                    return
                with self._done_lock:
                    deadline_parked = self._dismiss_deadline_parked
                    done_at_ms = self._done_at_ms
            logger.debug(f"{self._done_label}, waiting for screen to stabilize lode={self.lode_id}")

            last_snapshot = None
            capture_failures = 0
            parked_during_stabilization = False
            stabilization_deadline = time.monotonic() + DISMISS_STABILIZATION_TIMEOUT_SEC
            while not self._monitor_stop.is_set():
                with self._done_lock:
                    if self._dismiss_deadline_parked:
                        parked_during_stabilization = True
                        break
                remaining = stabilization_deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        f"screen did not stabilize before dismiss bound lode={self.lode_id}"
                    )
                    break
                self._monitor_stop.wait(min(MONITOR_INTERVAL, remaining))
                snapshot = capture_pane(self._pane_id)
                if snapshot is None:
                    capture_failures += 1
                    logger.debug(
                        "failed to capture pane while waiting to dismiss "
                        f"lode={self.lode_id} failures={capture_failures}"
                    )
                    if capture_failures >= PANE_CAPTURE_FAILURE_LIMIT:
                        break
                    continue
                capture_failures = 0
                if snapshot == last_snapshot:
                    break
                last_snapshot = snapshot

            if self._monitor_stop.is_set():
                return

            with self._done_lock:
                # A re-arm can clear the latch after this cycle's last check, so its
                # changed completion time keeps a pre-park snapshot from being trusted.
                may_dismiss = (
                    not parked_during_stabilization
                    and not self._dismiss_deadline_parked
                    and self._done_at_ms == done_at_ms
                )
            if may_dismiss:
                if last_snapshot is None:
                    logger.warning(f"dismissing without a stable pane capture lode={self.lode_id}")
                self._dismiss_attempt += 1
                if self._dismiss_attempt == 1:
                    logger.debug(f"sending Ctrl-C dismiss keystrokes lode={self.lode_id}")
                    send_keys(self._pane_id, "C-c")
                    send_keys(self._pane_id, "C-c")
                else:
                    logger.debug(f"sending Ctrl-D dismiss keystroke lode={self.lode_id}")
                    send_keys(self._pane_id, "C-d")

    def _start_monitor(self) -> None:
        """Start the activity monitor thread."""
        self._pane_id = get_current_pane_id()
        if not self._pane_id:
            logger.debug("Not in tmux, skipping activity monitor")
            return

        rename_window(self._pane_id, self.lode_id)
        self._last_pane_activity_ms = current_time_ms()
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="activity-monitor", daemon=True
        )
        self._monitor_thread.start()
        logger.debug(f"Started activity monitor for pane {self._pane_id}")

    def _stop_monitor(self) -> None:
        """Stop the activity monitor thread."""
        self._monitor_stop.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=1.0)
            logger.debug("Stopped activity monitor")

    def _monitor_loop(self) -> None:
        """Monitor loop that checks for activity every MONITOR_INTERVAL seconds."""
        while not self._monitor_stop.wait(MONITOR_INTERVAL):
            self._check_activity()

    def _open_gate(self) -> None:
        """Enter the gated state and disarm the pane resume detector."""
        self._gate_snapshot = None
        self._gate_armed = False
        self._gated.set()

    def _clear_gate(self) -> None:
        """Leave the gated state and disarm the pane resume detector."""
        self._gate_snapshot = None
        self._gate_armed = False
        self._gated.clear()

    def _check_activity(self) -> None:
        """Check tmux pane for activity and update state accordingly."""
        if not self._pane_id:
            return

        if self._done.is_set():
            now = current_time_ms()
            should_park = False
            with self._done_lock:
                if self._done_at_ms is None:
                    self._done_at_ms = now
                if (
                    not self._dismiss_deadline_parked
                    and now - self._done_at_ms >= DISMISS_DEADLINE_MS
                ):
                    self._dismiss_deadline_parked = True
                    should_park = True
            if should_park:
                reason = (
                    "completion was signalled, but the agent did not exit within "
                    f"{DISMISS_DEADLINE_MIN} min"
                )
                logger.warning(f"dismiss deadline reached lode={self.lode_id}: {reason}")
                self._park_idle(reason)
            return

        if self._gated.is_set():
            snapshot = self._capture_activity_pane()
            if snapshot is None:
                return
            self._stuck_since = None
            if not self._gate_armed:
                # A gate's own output arrives AFTER the gate opens: `hop gate`
                # prints "Gate set...", and Claude renders the end of its turn.
                # Those are pane changes, but they are not an operator resuming
                # anything -- so the resume detector must be armed against the
                # pane as it SETTLES, never against the pane from before the
                # gate. Re-baseline until the pane holds still across one
                # interval; only then can a change mean "a human touched this".
                #
                # Arming can only be delayed, never skipped, so the worst case is
                # a gate that only `hop gate feedback` can resume -- never a gate
                # that silently drops its protection and lets idle parking resume.
                if snapshot == self._gate_snapshot:
                    self._gate_armed = True
                self._gate_snapshot = snapshot
                self._record_pane_snapshot(snapshot, current_time_ms())
                return
            if snapshot != self._gate_snapshot:
                self._emit_state(
                    "running",
                    "Gate resumed",
                    gate_epoch=self._gate_epoch,
                )
                self._record_pane_snapshot(snapshot, current_time_ms())
                self._clear_gate()
            return

        snapshot = self._capture_activity_pane()
        if snapshot is None:
            return

        if pane_needs_answer(snapshot):
            self._record_pane_snapshot(snapshot, current_time_ms())
            self._stuck_since = None
            self._emit_state("gated", "Awaiting operator answer")
            self._open_gate()
            return

        now = current_time_ms()
        if snapshot != self._last_snapshot:
            self._record_pane_snapshot(snapshot, now)

        response = connect(self.socket_path, lode_id=self.lode_id)
        lode = response.get("lode") if response else None
        last_progress_at = lode.get("last_progress_at") if lode else None
        last_progress_summary = lode.get("last_progress_summary") if lode else None

        pane_activity = self._last_pane_activity_ms or 0
        heartbeat = last_progress_at or 0
        real_activity = max(pane_activity, heartbeat)
        real_quiet = now - real_activity > IDLE_THRESHOLD_MS
        if real_quiet:
            cpu = _sum_descendant_cpu_ms(self._claude_proc.pid if self._claude_proc else None)
            if cpu is not None:
                if self._last_descendant_cpu_ms is not None and cpu > self._last_descendant_cpu_ms:
                    self._last_cpu_activity_ms = now
                self._last_descendant_cpu_ms = cpu
        else:
            self._last_descendant_cpu_ms = None
            self._last_cpu_activity_ms = None

        cpu_activity = self._last_cpu_activity_ms or 0
        last_activity = max(real_activity, cpu_activity)

        if now - last_activity > IDLE_THRESHOLD_MS:
            if self._stuck_since is None:
                self._stuck_since = now
            duration_sec = (now - last_activity) // 1000
            self._emit_state("stuck", f"No output for {duration_sec}s")
            stuck_for = now - self._stuck_since
            if stuck_for > STUCK_PARK_THRESHOLD_MS and not self._gated.is_set():
                # NEVER terminate an idle stage. Park it and wait for an operator.
                self._park_idle(f"no pane output, heartbeat, or CPU activity for {duration_sec}s")
                return
        else:
            if (
                now - (self._last_pane_activity_ms or 0) > ABSOLUTE_CAP_MS
                and not self._gated.is_set()
            ):
                # Sustained only by heartbeat/CPU with a silent pane for an hour.
                # Surface it to an operator -- but do not kill it; it may be a long,
                # legitimately quiet build. The gate clears itself the moment the
                # pane moves again.
                self._park_idle(
                    f"no pane output for {ABSOLUTE_CAP_MS // 60_000} min "
                    "(sustained only by heartbeat/CPU activity)"
                )
                return
            if cpu_activity >= real_activity and real_quiet:
                self._emit_state(
                    "running",
                    f"background work active ({format_duration_ms(now - real_activity)})",
                )
            elif self._stuck_since is not None:
                status = (
                    last_progress_summary
                    if heartbeat > pane_activity and last_progress_summary
                    else "Claude running"
                )
                self._emit_state("running", status)
            self._stuck_since = None

    def _capture_activity_pane(self) -> str | None:
        """Capture the pane while keeping capture failures recoverable."""
        snapshot = capture_pane(self._pane_id)
        if snapshot is None:
            if self._pane_capture_failures == 0:
                self._pane_capture_outage_started_ms = current_time_ms()
            self._pane_capture_failures += 1
            logger.debug(
                f"failed to capture pane lode={self.lode_id} failures={self._pane_capture_failures}"
            )
            if self._pane_capture_failures >= PANE_CAPTURE_FAILURE_LIMIT:
                self._activity_capture_disabled = True
            return None

        capture_recovered = self._pane_capture_failures > 0
        recovered_at = current_time_ms() if capture_recovered else None
        if self._activity_capture_disabled:
            logger.info(f"pane capture recovered lode={self.lode_id}")
        self._activity_capture_disabled = False
        self._pane_capture_failures = 0
        if recovered_at is not None and self._pane_capture_outage_started_ms is not None:
            outage_ms = max(0, recovered_at - self._pane_capture_outage_started_ms)
            if self._last_pane_activity_ms is not None:
                self._last_pane_activity_ms += outage_ms
            if self._stuck_since is not None:
                self._stuck_since += outage_ms
        self._pane_capture_outage_started_ms = None
        return snapshot

    def _record_pane_snapshot(self, snapshot: str, observed_at: int) -> None:
        """Record and report a real change between two captured pane snapshots."""
        previous = self._last_snapshot
        self._last_snapshot = snapshot
        self._last_pane_activity_ms = observed_at
        if previous is None or previous == snapshot or not self.connection:
            return
        if (
            self._last_pane_activity_emitted_ms is not None
            and observed_at - self._last_pane_activity_emitted_ms < PANE_ACTIVITY_EMIT_INTERVAL_MS
        ):
            return
        if self.connection.emit(
            "lode_set_pane_activity",
            lode_id=self.lode_id,
            observed_at=observed_at,
        ):
            self._last_pane_activity_emitted_ms = observed_at

    def _park_idle(self, reason: str) -> None:
        """Park an idle stage as gated and wait for an operator. NEVER terminate it.

        Hopper cannot tell, from the outside, whether a quiet stage is blocked on a
        prompt, stalled on a model stream, or genuinely hung. Killing it destroys
        agent context that an operator can often resume with one keystroke -- and a
        stage that is merely waiting for an operator must never be executed for waiting.

        So a quiet stage is parked, not killed: the agent stays alive, the reason is
        recorded, and the lode waits. For an ordinary non-completion idle park, pane
        movement lets the existing gated branch clear the gate and carry on.

        Only an explicit operator action through the hop CLI may end a stage.
        """
        logger.warning(f"parking idle stage lode={self.lode_id}: {reason}")
        worktree_path = getattr(self, "worktree_path", None)
        record = {
            "parked_at": current_time_ms(),
            "state": "gated",
            "stage": self._claude_stage,
            "reason": reason,
            "branch": getattr(self, "lode_branch", None) or None,
            "worktree_path": str(worktree_path) if worktree_path else None,
            "terminated": False,
        }
        try:
            _write_recovery_record(self.lode_id, record)
        except Exception as exc:
            logger.error(f"failed to write park record lode={self.lode_id}: {exc}")

        self._stuck_since = None
        self._emit_state("gated", self._format_park_status(reason))
        self._open_gate()

    def _format_park_status(self, reason: str) -> str:
        """Prescriptive park status -- agents and operators both read this."""
        return format_park_status(reason, self.lode_id)

    def _terminate_claude_process(self) -> None:
        """Terminate the active Claude process and the descendants it launched."""
        proc = self._claude_proc
        if proc is None or proc.poll() is not None:
            return

        descendants = _descendant_pids(proc.pid)
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        except PermissionError:
            logger.debug(f"Permission denied terminating Claude process pid={proc.pid}")
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            except PermissionError:
                logger.debug(f"Permission denied killing Claude process pid={proc.pid}")
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.debug("Claude process did not exit after SIGKILL")

        survivors = []
        for pid in descendants:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except PermissionError:
                logger.warning(f"Permission denied sending SIGTERM to descendant pid={pid}")
            survivors.append(pid)

        deadline = time.monotonic() + DESCENDANT_TERM_GRACE_SEC
        while survivors:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            alive = []
            for pid in survivors:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                except PermissionError:
                    logger.warning(f"Permission denied probing descendant pid={pid}")
                alive.append(pid)
            survivors = alive
            if survivors:
                time.sleep(min(DESCENDANT_POLL_INTERVAL_SEC, remaining))

        for pid in survivors:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                logger.warning(f"Permission denied sending SIGKILL to descendant pid={pid}")
