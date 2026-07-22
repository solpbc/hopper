# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Base runner - shared lifecycle logic for the process runner."""

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from hopper.client import HopperConnection, connect
from hopper.lodes import current_time_ms, format_duration_ms, get_lode_dir
from hopper.projects import find_project
from hopper.tmux import capture_pane, get_current_pane_id, rename_window, send_keys

logger = logging.getLogger(__name__)

ERROR_LINES = 5  # Number of stderr lines to capture on error
MONITOR_INTERVAL = 5.0  # Seconds between activity checks
MONITOR_INTERVAL_MS = 5000
IDLE_THRESHOLD_MS = 50_000
STUCK_FAIL_THRESHOLD_MS = 5 * 60_000
ABSOLUTE_CAP_MS = 60 * 60_000
_QUESTION_SELECTOR_RE = re.compile(r"^\s*❯\s*\d+\.", re.MULTILINE)
_QUESTION_CHROME_RE = re.compile(
    r"(?:↑/↓ to navigate|Esc to cancel|Enter to select|Type something)", re.IGNORECASE
)
DESCENDANT_TERM_GRACE_SEC = 5.0
DESCENDANT_POLL_INTERVAL_SEC = 0.1
STUCK_FAILURE_WAIT_SEC = 60


def pane_needs_answer(snapshot: str) -> bool:
    """Return whether a Claude pane is visibly waiting on a numbered answer."""
    return bool(
        snapshot and _QUESTION_SELECTOR_RE.search(snapshot) and _QUESTION_CHROME_RE.search(snapshot)
    )


def _write_recovery_record(lode_id: str, record: dict) -> None:
    """Atomically persist a stuck-kill recovery record for a lode."""
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


def _sum_descendant_cpu_ms(root_pid: int | None) -> int | None:
    """Return cumulative CPU time for descendants of root_pid, excluding root_pid."""
    if root_pid is None:
        return None

    try:
        result = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,time="],
            capture_output=True,
            text=True,
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

    total = sum(times.get(pid, 0.0) for pid in _walk_descendant_pids(root_pid, children))
    return int(total * 1000)


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
    _always_dismiss: bool = False

    def __init__(self, lode_id: str, socket_path: Path):
        self.lode_id = lode_id
        self.socket_path = socket_path
        self.connection: HopperConnection | None = None
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
        self._pane_id: str | None = None
        self._claude_proc: subprocess.Popen | None = None
        self._stuck_error: str | None = None
        self._stuck_failure_complete = threading.Event()
        # Completion tracking
        self._done = threading.Event()
        self._gated = threading.Event()
        # Gate resume detector: the pane as it settled *after* the gate opened,
        # and whether we have seen it hold still long enough to trust a change.
        self._gate_snapshot: str | None = None
        self._gate_armed = False
        self._setup_error: str | None = None

    def run(self) -> int:
        """Run Claude for this lode. Returns exit code."""
        original_sigint = signal.signal(signal.SIGINT, self._handle_signal)
        original_sigterm = signal.signal(signal.SIGTERM, self._handle_signal)

        try:
            try:
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
                self.connection = HopperConnection(self.socket_path)
                self.connection.start(
                    callback=self._on_server_message,
                    on_connect=lambda: self.connection.emit(
                        "lode_register",
                        lode_id=self.lode_id,
                        tmux_pane=get_current_pane_id(),
                        pid=os.getpid(),
                    ),
                )

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
        # Hopper lodes are scoped by their prompt and repo context; do not let
        # Claude Code read/write project auto-memory during managed stages.
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        env["CLAUDE_CODE_DISABLE_MEMORY_PERIODIC_RESYNC"] = "1"
        env["CLAUDE_CODE_DISABLE_MEMORY_BULK_INFLATE"] = "1"
        return env

    def _run_claude(self) -> tuple[int, str | None]:
        """Run Claude subprocess. Returns (exit_code, error_message)."""
        cmd, cwd = self._build_command()

        env = self._get_subprocess_env()

        logger.debug(f"Running: {' '.join(cmd[:3])}...")

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

            # Start dismiss thread if configured
            should_dismiss = self._always_dismiss or self.is_first_run
            if should_dismiss and self._pane_id:
                threading.Thread(
                    target=self._wait_and_dismiss_claude,
                    name=f"{self._done_label.lower().replace(' ', '-')}-dismiss",
                    daemon=True,
                ).start()

            proc.wait()

            if self._stuck_error:
                if not self._stuck_failure_complete.wait(timeout=STUCK_FAILURE_WAIT_SEC):
                    logger.warning(f"timed out waiting for stuck recovery lode={self.lode_id}")
                return 1, self._stuck_error
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
            self._claude_proc = None

    def _handle_signal(self, signum: int, frame) -> None:
        """Handle shutdown signals gracefully."""
        logger.debug(f"Received signal {signum}")
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        sys.exit(128 + signum)

    def _emit_state(self, state: str, status: str) -> bool:
        """Emit state change to server via persistent connection."""
        if self.connection:
            emitted = self.connection.emit(
                "lode_set_state",
                lode_id=self.lode_id,
                state=state,
                status=status,
            )
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
        if message.get("type") != "lode_updated":
            return
        lode = message.get("lode", {})
        if lode.get("id") != self.lode_id:
            return
        if lode.get("state") == "completed":
            self._done.set()
            logger.debug(f"{self._done_label} signal received")
        elif lode.get("state") == "gated":
            self._open_gate()
            logger.debug(f"gate signal received lode={self.lode_id}")
        elif lode.get("state") == "running":
            self._clear_gate()

    def _wait_and_dismiss_claude(self) -> None:
        """Wait for completion or gate, screen stability, then send Ctrl-D to exit Claude.

        Retries if Claude doesn't exit after the first Ctrl-D (e.g. if it was
        sent during output rather than at the interactive prompt).
        """
        while not self._done.is_set():
            self._done.wait(timeout=1.0)
            if self._monitor_stop.is_set():
                return

        if not self._pane_id:
            return

        while not self._monitor_stop.is_set():
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

            logger.debug("Screen stable, sending Ctrl-C")
            send_keys(self._pane_id, "C-c")
            send_keys(self._pane_id, "C-c")

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
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_stop.set()
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
            return

        if self._gated.is_set():
            snapshot = capture_pane(self._pane_id)
            if snapshot is None:
                logger.debug("Failed to capture pane, stopping monitor")
                self._monitor_stop.set()
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
                # that silently drops its protection and lets the stuck-killer in.
                if snapshot == self._gate_snapshot:
                    self._gate_armed = True
                self._gate_snapshot = snapshot
                self._last_snapshot = snapshot
                self._last_pane_activity_ms = current_time_ms()
                return
            if snapshot != self._gate_snapshot:
                self._emit_state("running", "Gate resumed")
                self._last_snapshot = snapshot
                self._last_pane_activity_ms = current_time_ms()
                self._clear_gate()
            return

        snapshot = capture_pane(self._pane_id)
        if snapshot is None:
            logger.debug("Failed to capture pane, stopping monitor")
            self._monitor_stop.set()
            return

        if pane_needs_answer(snapshot):
            self._last_snapshot = snapshot
            self._stuck_since = None
            self._emit_state("gated", "Awaiting operator answer")
            self._open_gate()
            return

        now = current_time_ms()
        if snapshot != self._last_snapshot:
            self._last_snapshot = snapshot
            self._last_pane_activity_ms = now

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
            if stuck_for > STUCK_FAIL_THRESHOLD_MS and not self._gated.is_set():
                # NEVER terminate an idle stage. Park it and wait for a human.
                self._park_idle(f"no pane output, heartbeat, or CPU activity for {duration_sec}s")
                return
        else:
            if (
                now - (self._last_pane_activity_ms or 0) > ABSOLUTE_CAP_MS
                and not self._gated.is_set()
            ):
                # Sustained only by heartbeat/CPU with a silent pane for an hour.
                # Surface it to a human -- but do not kill it; it may be a long,
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

    def _snapshot_stuck_worktree(self) -> dict:
        """Overridden by runners that own a worktree."""
        return {"outcome": "no_worktree"}

    def _park_idle(self, reason: str) -> None:
        """Park an idle stage as gated and wait for a human. NEVER terminate it.

        Hopper cannot tell, from the outside, whether a quiet stage is blocked on a
        prompt, stalled on a model stream, or genuinely hung. Killing it destroys
        agent context that a human can often resume with one keystroke -- and a stage
        that is merely *waiting for a person* must never be executed for waiting.

        So a quiet stage is parked, not killed: the agent stays alive, the reason is
        recorded, and the lode waits. The monitor keeps watching, so the moment the
        pane changes (an operator answers, nudges, or the stage resumes on its own)
        the existing gated branch clears the gate and the stage carries on.

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
        return (
            f"Parked (idle): {reason}. The agent is ALIVE and was NOT terminated. "
            f"Inspect: hop lode peek {self.lode_id} | "
            f"Resume: hop lode nudge {self.lode_id} (or hop lode answer {self.lode_id} 1)"
        )

    def _format_stuck_error(self, reason: str, record: dict) -> str:
        """Add recovery details and the restart command to a stuck error."""
        snapshot = record["snapshot"]
        outcome = snapshot["outcome"]
        branch = record.get("branch") or "unavailable"
        stage = record.get("stage", "")
        if outcome == "committed":
            return (
                f"{reason} Recovery snapshot committed on branch {branch} at {snapshot['sha']}. "
                f"Restart with: hop lode restart {self.lode_id}"
            )
        if outcome == "clean":
            return (
                f"{reason} Recovery branch {branch}; worktree was clean, so no snapshot commit "
                f"was created. Restart with: hop lode restart {self.lode_id}"
            )
        if outcome == "no_worktree":
            return (
                f"{reason} Recovery branch unavailable; no worktree existed for stage {stage}, "
                f"so no snapshot was created. Restart with: hop lode restart {self.lode_id}"
            )
        worktree_path = record.get("worktree_path") or "unavailable"
        return (
            f"{reason} Recovery snapshot failed on branch {branch}: {snapshot['git_error']}. "
            f"Inspect {worktree_path} before restarting with: hop lode restart {self.lode_id}"
        )

    def _fail_stuck(self, reason: str) -> None:
        """Terminate a stuck runner and preserve failure state."""
        failed_at = current_time_ms()
        self._stuck_error = reason
        logger.error(f"stuck timeout lode={self.lode_id}: {reason}")
        try:
            self._terminate_claude_process()
            try:
                snapshot = self._snapshot_stuck_worktree()
            except Exception as exc:
                logger.exception(f"unexpected stuck snapshot failure lode={self.lode_id}")
                snapshot = {"outcome": "failed", "git_error": str(exc)}

            worktree_path = getattr(self, "worktree_path", None)
            record = {
                "failed_at": failed_at,
                "stage": self._claude_stage,
                "reason": reason,
                "branch": getattr(self, "lode_branch", None) or None,
                "worktree_path": str(worktree_path) if worktree_path else None,
                "snapshot": snapshot,
            }
            try:
                _write_recovery_record(self.lode_id, record)
            except Exception as exc:
                logger.error(f"failed to write recovery record lode={self.lode_id}: {exc}")
                self._stuck_error = (
                    f"{self._format_stuck_error(reason, record)} "
                    f"Recovery record could not be written: {exc}."
                )
            else:
                self._stuck_error = self._format_stuck_error(reason, record)
        finally:
            self._monitor_stop.set()
            self._stuck_failure_complete.set()

    def _terminate_claude_process(self) -> None:
        """Terminate the active Claude process after a stuck timeout."""
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
