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
from collections.abc import Callable
from enum import Enum
from pathlib import Path

from hopper.cleanup import reap_swiftpm_testing_helpers
from hopper.client import RUN_GENERATION_ENV, HopperConnection, connect
from hopper.driver import (
    STAGE_DRIVER_CAPABILITIES_KEY,
    STAGE_DRIVER_PROTOCOL_VERSION,
    DriverRefusal,
    resolve_driver,
)
from hopper.lodes import (
    current_time_ms,
    format_duration_ms,
    format_park_status,
    get_lode_dir,
    load_lodes,
    lode_driver,
    lode_stage_session,
)
from hopper.projects import find_project
from hopper.tmux import (
    KeyboardOwnership,
    PanePhase,
    capture_pane,
    get_current_pane_id,
    observe_pane,
    pane_answer_identity,
    rename_window,
)
from hopper.workspace_trust import WorkspaceTrustError, trust_claude_workspace

logger = logging.getLogger(__name__)

ERROR_LINES = 5  # Number of stderr lines to capture on error
MONITOR_INTERVAL = 5.0  # Seconds between activity checks
MONITOR_INTERVAL_MS = 5000
IDLE_THRESHOLD_MS = 50_000
STUCK_PARK_THRESHOLD_MS = 5 * 60_000
ABSOLUTE_CAP_MS = 60 * 60_000
PANE_CAPTURE_FAILURE_LIMIT = 3
PANE_ACTIVITY_EMIT_INTERVAL_MS = 30_000
DESCENDANT_TERM_GRACE_SEC = 5.0
DESCENDANT_POLL_INTERVAL_SEC = 0.1
REGISTRATION_TIMEOUT_SEC = 30.0
BRANCH_PERSIST_TIMEOUT_SEC = 10.0
WORKTREE_PUBLICATION_TIMEOUT_SEC = 15.0
DURABLE_CONFIRMATION_TIMEOUT_SEC = 1.0
DURABLE_CONFIRMATION_POLL_INTERVAL_SEC = 0.05
PS_SCAN_TIMEOUT_SEC = 5.0


class StageDriverProtocol(Enum):
    """Compatibility outcome for one interactive-stage connection handshake."""

    CURRENT = "current"
    LEGACY_CLAUDE = "legacy_claude"
    UNKNOWN = "unknown"


def classify_stage_driver_protocol(response: object, driver: object) -> StageDriverProtocol:
    """Classify the bounded current-or-legacy stage-driver handshake.

    A marker is positive negotiation. Markerless support is intentionally
    limited to the exact pre-foundation connected response that this runner
    sent with a lode_id; every malformed or partial response remains unknown.
    """
    if not isinstance(response, dict):
        return StageDriverProtocol.UNKNOWN
    if STAGE_DRIVER_CAPABILITIES_KEY in response:
        marker = response[STAGE_DRIVER_CAPABILITIES_KEY]
        if not isinstance(marker, dict) or set(marker) != {"version", "drivers"}:
            return StageDriverProtocol.UNKNOWN
        providers = marker.get("drivers")
        if (
            marker.get("version") == STAGE_DRIVER_PROTOCOL_VERSION
            and isinstance(providers, list)
            and all(isinstance(provider, str) for provider in providers)
            and driver in providers
        ):
            return StageDriverProtocol.CURRENT
        return StageDriverProtocol.UNKNOWN

    expected_fields = {"type", "tmux", "ts", "exchange_id", "lode", "lode_found"}
    if set(response) != expected_fields:
        return StageDriverProtocol.UNKNOWN
    if response.get("type") != "connected":
        return StageDriverProtocol.UNKNOWN
    if response.get("tmux") is not None and not isinstance(response.get("tmux"), dict):
        return StageDriverProtocol.UNKNOWN
    if type(response.get("ts")) is not int or not isinstance(response.get("exchange_id"), str):
        return StageDriverProtocol.UNKNOWN
    if not isinstance(response.get("lode_found"), bool):
        return StageDriverProtocol.UNKNOWN
    if response["lode_found"] != isinstance(response.get("lode"), dict):
        return StageDriverProtocol.UNKNOWN
    if driver == "claude":
        return StageDriverProtocol.LEGACY_CLAUDE
    return StageDriverProtocol.UNKNOWN


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


def _confirm_durable_lode_mutation(
    lode_id: str,
    field: str,
    emitted_value: str,
    run_generation: str | None,
    emitted_at_ms: int,
    *,
    normalize: Callable[[str], str],
) -> str | None:
    """Return the durable value when a timed-out runner mutation is attributable."""
    if not isinstance(run_generation, str) or not run_generation:
        return None
    try:
        normalized_emitted = normalize(emitted_value)
    except (OSError, RuntimeError, ValueError):
        return None
    if not isinstance(normalized_emitted, str):
        return None

    expires_at = time.monotonic() + DURABLE_CONFIRMATION_TIMEOUT_SEC
    while True:
        try:
            # Snapshot replies use the same event loop and per-connection write lock as
            # the delayed broadcast, so a query can block behind the same stall. The
            # durable file is the only confirmation path the server cannot block.
            lodes = load_lodes()
        except (OSError, RuntimeError, ValueError):
            return None
        if not isinstance(lodes, list):
            return None

        matches = [lode for lode in lodes if isinstance(lode, dict) and lode.get("id") == lode_id]
        if len(matches) > 1:
            return None
        if len(matches) == 1:
            lode = matches[0]
            durable_value = lode.get(field)
            updated_at = lode.get("updated_at")
            # updated_at is lode-scoped, not field-scoped. Freshness only attributes a
            # matching value when the record also belongs to this runner generation.
            attributable = (
                isinstance(durable_value, str)
                and lode.get("run_generation") == run_generation
                and type(updated_at) is int
                and updated_at >= emitted_at_ms
            )
            if attributable:
                try:
                    normalized_durable = normalize(durable_value)
                except (OSError, RuntimeError, ValueError):
                    return None
                if isinstance(normalized_durable, str) and normalized_durable == normalized_emitted:
                    return durable_value

        remaining = expires_at - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(DURABLE_CONFIRMATION_POLL_INTERVAL_SEC, remaining))


def _confirm_durable_stage_attempt(
    lode_id: str,
    *,
    driver: str,
    stage: str,
    launch_id: str,
    provider_session_id: str,
    run_generation: str | None,
    emitted_at_ms: int,
    require_attempt: bool,
) -> bool:
    """Confirm a fenced start through the durable record after a lost acknowledgement."""
    if not isinstance(run_generation, str) or not run_generation:
        return False
    expires_at = time.monotonic() + DURABLE_CONFIRMATION_TIMEOUT_SEC
    while True:
        try:
            lodes = load_lodes()
        except (OSError, RuntimeError, ValueError):
            return False
        matches = [lode for lode in lodes if lode.get("id") == lode_id]
        if len(matches) == 1:
            lode = matches[0]
            try:
                session = lode_stage_session(lode, stage)
            except ValueError:
                return False
            attempt = session["start_attempt"]
            expected_attempt = {
                "driver": driver,
                "stage": stage,
                "launch_id": launch_id,
                "provider_session_id": provider_session_id,
                "run_generation": run_generation,
                "outcome": "committed",
            }
            confirmed = (
                lode.get("run_generation") == run_generation
                and type(lode.get("updated_at")) is int
                and lode["updated_at"] >= emitted_at_ms
                and session["started"] is True
                and session["launch_id"] == launch_id
                and session["provider_session_id"] == provider_session_id
                and (attempt == expected_attempt if require_attempt else attempt is None)
            )
            if confirmed:
                return True
        remaining = expires_at - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(DURABLE_CONFIRMATION_POLL_INTERVAL_SEC, remaining))


class BaseRunner:
    """Base class for lode runners.

    Provides the full run lifecycle: signal handling, server communication,
    subprocess management and activity monitoring.

    Subclasses configure behavior via class attributes and implement:
    - _setup(): Pre-flight validation and setup. Return int to bail.
    - _build_command(): Return (cmd, cwd) for the Claude subprocess.
    """

    # Subclasses set these to customize behavior
    _claude_stage: str = ""  # Interactive stage key ("mill", "refine", "ship")

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
        self._registration_refusal_reason: str | None = None
        self._expected_lode_branch: str | None = None
        self._branch_persisted = threading.Event()
        self._confirmed_lode_branch: str | None = None
        self._worktree_publication_condition = threading.Condition()
        self._expected_worktree_path: str | None = None
        self._worktree_publication_ack: dict | None = None
        self._confirmed_worktree_path: str | None = None
        self.is_first_run = False
        self.claude_session_id: str = ""
        self.launch_id: str = ""
        self.driver_name: str = "claude"
        self.driver = resolve_driver("claude")
        self.driver_label = self.driver.LABEL
        self._stage_protocol = StageDriverProtocol.UNKNOWN
        self._stage_binding_condition = threading.Condition()
        self._expected_stage_attempt: dict | None = None
        self._stage_binding_ack: dict | None = None
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
        self._gated = threading.Event()
        # Gate resume detector: the pane as it settled *after* the gate opened,
        # and whether we have seen it hold still long enough to trust a change.
        self._gate_snapshot: str | None = None
        self._gate_armed = False
        self._gate_epoch = 0
        self._gate_kind: str | None = None
        self._native_gate_identity: tuple[str, tuple[tuple[int, str], ...]] | None = None
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
                    message = (
                        "Interactive-stage protocol is unavailable; inspect connectivity and "
                        "the Hopper server version before retrying."
                    )
                    logger.error("stage protocol unavailable lode=%s", self.lode_id)
                    print(message)
                    return 1

                lode_data = response.get("lode")
                if not lode_data:
                    print(f"Lode {self.lode_id} not found")
                    return 1

                if lode_data.get("active", False):
                    logger.error(f"Lode {self.lode_id} already has an active connection")
                    print(f"Lode {self.lode_id} is already active")
                    return 1

                # Read the canonical per-stage provider session info.
                stage_session = lode_stage_session(lode_data, self._claude_stage)
                self.claude_session_id = stage_session["provider_session_id"]
                self.launch_id = stage_session["launch_id"]
                if self.run_generation is None and isinstance(lode_data.get("run_generation"), str):
                    self.run_generation = lode_data["run_generation"]
                self.is_first_run = not stage_session["started"]
                self.driver_name = lode_driver(lode_data)
                self._stage_protocol = classify_stage_driver_protocol(response, self.driver_name)
                if self._stage_protocol is StageDriverProtocol.UNKNOWN:
                    message = (
                        "Interactive-stage protocol is unavailable; inspect connectivity and "
                        "the Hopper server version before retrying."
                    )
                    logger.error(
                        "stage protocol unknown lode=%s driver=%s", self.lode_id, self.driver_name
                    )
                    print(message)
                    return 1
                try:
                    self.driver = resolve_driver(self.driver_name)
                except DriverRefusal as error:
                    logger.error(
                        "stage driver refused lode=%s driver=%s", self.lode_id, self.driver_name
                    )
                    print(str(error))
                    return 1
                self.driver_label = self.driver.LABEL
                logger.info(
                    "stage protocol negotiated lode=%s driver=%s path=%s",
                    self.lode_id,
                    self.driver_name,
                    self._stage_protocol.value,
                )
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
                        ppid=os.getppid(),
                        pgid=os.getpgid(0),
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
                    reason = self._registration_refusal_reason or "server refused ownership"
                    print(f"Failed to register lode {self.lode_id}: {reason}")
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

        if not self._admit_stage_start_before_launch():
            return 1, "Stage launch was not durably acknowledged; inspect before retrying"

        try:
            proc = subprocess.Popen(
                cmd,
                env=env,
                stderr=subprocess.PIPE,
                cwd=cwd,
            )
            self._claude_proc = proc

            if not self._admit_stage_start_after_launch():
                self._terminate_claude_process()
                return 1, "Stage launch was not durably acknowledged; inspect before retrying"
            self._emit_state("running", f"{self.driver_label} running")
            self._start_monitor()

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

    def _stage_attempt(self) -> dict:
        """Return the exact fenced binding tuple for this runner's stage launch."""
        return {
            "driver": self.driver_name,
            "stage": self._claude_stage,
            "launch_id": self.launch_id,
            "provider_session_id": self.claude_session_id,
            "run_generation": self.run_generation,
        }

    def _await_stage_binding(self, attempt: dict, emitted_at_ms: int) -> bool:
        """Wait for a mutation acknowledgement or reconcile its committed attempt."""
        if (
            not self.connection
            or not isinstance(self.run_generation, str)
            or not self.run_generation
        ):
            return False
        with self._stage_binding_condition:
            self._expected_stage_attempt = attempt
            self._stage_binding_ack = None
        try:
            if not self.connection.emit(
                "lode_bind_stage_session",
                lode_id=self.lode_id,
                ack_requested=True,
                **attempt,
            ):
                return False
            expires_at = time.monotonic() + DURABLE_CONFIRMATION_TIMEOUT_SEC
            with self._stage_binding_condition:
                while self._stage_binding_ack is None:
                    remaining = expires_at - time.monotonic()
                    if remaining <= 0:
                        break
                    self._stage_binding_condition.wait(remaining)
                ack = self._stage_binding_ack
            if ack is not None:
                return ack.get("accepted") is True and ack.get("reason") == "committed"
            return _confirm_durable_stage_attempt(
                self.lode_id,
                emitted_at_ms=emitted_at_ms,
                require_attempt=True,
                **attempt,
            )
        finally:
            with self._stage_binding_condition:
                self._expected_stage_attempt = None

    def _admit_stage_start_before_launch(self) -> bool:
        """Commit current-protocol first starts before a provider process exists."""
        if self.connection is None:
            return True
        if self._stage_protocol is not StageDriverProtocol.CURRENT or not self.is_first_run:
            return True
        attempt = self._stage_attempt()
        if not attempt["launch_id"]:
            return False
        logger.info("stage start attempt lode=%s %s", self.lode_id, attempt)
        return self._await_stage_binding(attempt, current_time_ms())

    def _admit_stage_start_after_launch(self) -> bool:
        """Apply legacy compatibility confirmation after a provider process starts."""
        if self.connection is None:
            return True
        if self._stage_protocol is StageDriverProtocol.CURRENT:
            return True
        emitted_at_ms = current_time_ms()
        attempt = self._stage_attempt()
        if self.is_first_run:
            self._emit_claude_started()
            return _confirm_durable_stage_attempt(
                self.lode_id,
                emitted_at_ms=emitted_at_ms,
                require_attempt=False,
                **attempt,
            )
        # Legacy servers cannot bind a resume. Persist a non-running launch
        # marker and require a fresh current-generation durable transition.
        self._emit_state("ready", f"{self.driver_label} launch confirming")
        return (
            _confirm_durable_lode_mutation(
                self.lode_id,
                "status",
                f"{self.driver_label} launch confirming",
                self.run_generation,
                emitted_at_ms,
                normalize=lambda value: value,
            )
            is not None
        )

    def _handle_signal(self, signum: int, frame) -> None:
        """Handle shutdown signals gracefully."""
        logger.debug(f"Received signal {signum}")
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        sys.exit(128 + signum)

    def _persist_lode_branch(self, branch: str) -> dict:
        """Persist branch metadata and wait for its post-save broadcast."""
        self._branch_persisted.clear()
        self._confirmed_lode_branch = None
        self._expected_lode_branch = branch
        try:
            if not self.connection:
                return {
                    "accepted": False,
                    "reason": "transport_unavailable",
                    "branch": branch,
                }
            emitted_at_ms = current_time_ms()
            if not self.connection.emit(
                "lode_set_branch",
                lode_id=self.lode_id,
                branch=branch,
            ):
                return {
                    "accepted": False,
                    "reason": "transport_unavailable",
                    "branch": branch,
                }
            if self._branch_persisted.wait(BRANCH_PERSIST_TIMEOUT_SEC):
                confirmed = self._confirmed_lode_branch
                if isinstance(confirmed, str):
                    return {"accepted": True, "reason": "persisted", "branch": confirmed}
            confirmed = _confirm_durable_lode_mutation(
                self.lode_id,
                "branch",
                branch,
                self.run_generation,
                emitted_at_ms,
                normalize=lambda value: value,
            )
            if confirmed is not None:
                logger.warning(
                    "Handshake confirmed from durable state after timeout "
                    "lode=%s handshake=%s value=%r",
                    self.lode_id,
                    "lode_set_branch",
                    confirmed,
                )
                return {
                    "accepted": True,
                    "reason": "durable_confirmed_after_timeout",
                    "branch": confirmed,
                }
            return {
                "accepted": False,
                "reason": "persistence_unconfirmed",
                "branch": branch,
            }
        finally:
            self._expected_lode_branch = None

    def _publish_lode_worktree_path(self, worktree_path: str) -> dict:
        """Publish provenance and wait for admission plus post-save evidence."""
        expires_at = time.monotonic() + WORKTREE_PUBLICATION_TIMEOUT_SEC
        with self._worktree_publication_condition:
            self._expected_worktree_path = worktree_path
            self._worktree_publication_ack = None
            self._confirmed_worktree_path = None
        try:
            if not self.connection:
                return {
                    "accepted": False,
                    "reason": "transport_unavailable",
                    "worktree_path": worktree_path,
                }
            try:
                emitted_at_ms = current_time_ms()
                emitted = self.connection.emit(
                    "lode_set_worktree_path",
                    lode_id=self.lode_id,
                    project=self.project_name,
                    worktree_path=worktree_path,
                    ack_requested=True,
                )
            except (OSError, RuntimeError):
                logger.exception("worktree publication transport failed lode=%s", self.lode_id)
                return {
                    "accepted": False,
                    "reason": "transport_loss",
                    "worktree_path": worktree_path,
                }
            if not emitted:
                return {
                    "accepted": False,
                    "reason": "transport_unavailable",
                    "worktree_path": worktree_path,
                }

            with self._worktree_publication_condition:
                while True:
                    ack = self._worktree_publication_ack
                    if ack is not None and ack.get("accepted") is not True:
                        reason = ack.get("reason")
                        if not isinstance(reason, str) or not reason:
                            reason = "server_refused"
                        return {
                            "accepted": False,
                            "reason": reason,
                            "worktree_path": worktree_path,
                        }
                    if ack is not None and self._confirmed_worktree_path is not None:
                        return {
                            "accepted": True,
                            "reason": "persisted",
                            "worktree_path": self._confirmed_worktree_path,
                        }
                    remaining = expires_at - time.monotonic()
                    if remaining <= 0:
                        reason = (
                            "persistence_unconfirmed"
                            if ack is not None and ack.get("accepted") is True
                            else "mutation_ack_timeout"
                        )
                        break
                    self._worktree_publication_condition.wait(remaining)

            confirmed = _confirm_durable_lode_mutation(
                self.lode_id,
                "worktree_path",
                worktree_path,
                self.run_generation,
                emitted_at_ms,
                normalize=lambda value: str(Path(value).resolve(strict=True)),
            )
            if confirmed is not None:
                logger.warning(
                    "Handshake confirmed from durable state after timeout "
                    "lode=%s handshake=%s value=%r",
                    self.lode_id,
                    "lode_set_worktree_path",
                    confirmed,
                )
                return {
                    "accepted": True,
                    "reason": "durable_confirmed_after_timeout",
                    "worktree_path": confirmed,
                }
            return {
                "accepted": False,
                "reason": reason,
                "worktree_path": worktree_path,
            }
        finally:
            with self._worktree_publication_condition:
                self._expected_worktree_path = None

    def _emit_state(
        self,
        state: str,
        status: str,
        *,
        gate_epoch: int | None = None,
        gate_kind: str | None = None,
    ) -> bool:
        """Emit state change to server via persistent connection."""
        if self.connection:
            fields = {"lode_id": self.lode_id, "state": state, "status": status}
            if gate_epoch is not None:
                fields["gate_epoch"] = gate_epoch
            if gate_kind is not None:
                fields["gate_kind"] = gate_kind
            emitted = self.connection.emit("lode_set_state", **fields)
            logger.debug(f"Emitted state: {state}, status: {status}")
            return emitted
        return False

    def _emit_gate(self, kind: str, body: str, status: str) -> bool:
        """Publish one coherent durable gate through the current runner connection."""
        if not self.connection:
            return False
        emitted = self.connection.emit(
            "lode_publish_gate",
            lode_id=self.lode_id,
            kind=kind,
            body=body,
            status=status,
        )
        logger.debug("Emitted gate kind=%s lode=%s", kind, self.lode_id)
        return emitted

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
        if message.get("type") == "mutation_ack":
            if (
                message.get("mutation_type") == "lode_bind_stage_session"
                and message.get("lode_id") == self.lode_id
            ):
                with self._stage_binding_condition:
                    attempt = self._expected_stage_attempt
                    if attempt is not None:
                        self._stage_binding_ack = message
                        self._stage_binding_condition.notify_all()
                return
            if (
                message.get("mutation_type") == "lode_set_worktree_path"
                and message.get("lode_id") == self.lode_id
            ):
                with self._worktree_publication_condition:
                    if self._expected_worktree_path is not None:
                        self._worktree_publication_ack = message
                        self._worktree_publication_condition.notify_all()
            return
        if message.get("type") == "lode_registered":
            if message.get("lode_id") == self.lode_id:
                self._registration_refusal_reason = None
                self._registration_accepted = True
                self._registration_complete.set()
            return
        if message.get("type") == "lode_register_refused":
            if message.get("lode_id") == self.lode_id:
                reason = message.get("reason")
                self._registration_refusal_reason = (
                    reason if isinstance(reason, str) and reason else None
                )
                self._registration_accepted = False
                self._registration_complete.set()
            return
        if message.get("type") != "lode_updated":
            return
        lode = message.get("lode", {})
        if lode.get("id") != self.lode_id:
            return
        with self._worktree_publication_condition:
            if (
                self._expected_worktree_path is not None
                and lode.get("worktree_path") == self._expected_worktree_path
            ):
                self._confirmed_worktree_path = lode["worktree_path"]
                self._worktree_publication_condition.notify_all()
        if "branch" in lode:
            observed_branch = lode["branch"]
            if (
                self._expected_lode_branch is not None
                and observed_branch == self._expected_lode_branch
            ):
                self._confirmed_lode_branch = observed_branch
                self._branch_persisted.set()
        if lode.get("state") == "gated":
            self._open_gate()
            logger.debug(f"gate signal received lode={self.lode_id}")
        elif lode.get("state") == "running":
            self._clear_gate()
        # Adopt the epoch only after state handling disarms the gate detector. Until then,
        # an armed monitor must emit the old epoch so the server rejects a stale resume.
        self._gate_epoch = lode.get("gate_epoch", 0)
        self._gate_kind = lode.get("gate_kind")
        if self._gate_kind != "native_question":
            self._native_gate_identity = None

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
                self._record_pane_snapshot(snapshot, current_time_ms())
                if self._gate_kind == "native_question":
                    phase, keyboard = observe_pane(None, snapshot)
                    selector_identity = pane_answer_identity(snapshot)
                    if (
                        self._native_gate_identity is not None
                        and phase is not PanePhase.BLOCKED
                        and keyboard is not KeyboardOwnership.UNKNOWN
                        and selector_identity is None
                    ):
                        self._emit_state(
                            "running",
                            "Gate resumed",
                            gate_epoch=self._gate_epoch,
                            gate_kind="native_question",
                        )
                        self._clear_gate()
                elif self._gate_kind == "idle_park":
                    self._emit_state(
                        "running",
                        "Gate resumed",
                        gate_epoch=self._gate_epoch,
                        gate_kind="idle_park",
                    )
                    self._clear_gate()
            return

        snapshot = self._capture_activity_pane()
        if snapshot is None:
            return

        phase, _keyboard = observe_pane(None, snapshot)
        if phase is PanePhase.BLOCKED:
            self._record_pane_snapshot(snapshot, current_time_ms())
            self._stuck_since = None
            selector_identity = pane_answer_identity(snapshot)
            if selector_identity is not None:
                question, choices = selector_identity
                body = "\n".join([question, *(f"{number}. {label}" for number, label in choices)])
                self._native_gate_identity = selector_identity
                self._emit_gate("native_question", body, "Awaiting operator answer")
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
            background_phase, _keyboard = observe_pane(
                None,
                snapshot,
                background_work_active=cpu_activity >= real_activity and real_quiet,
            )
            if background_phase is PanePhase.BACKGROUND:
                self._emit_state(
                    "running",
                    f"background work active ({format_duration_ms(now - real_activity)})",
                )
            elif self._stuck_since is not None:
                status = (
                    last_progress_summary
                    if heartbeat > pane_activity and last_progress_summary
                    else f"{self.driver_label} running"
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
        recorded, and the lode waits. Pane movement lets the existing gated branch
        clear the gate and carry on.

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
        status = self._format_park_status(reason)
        self._emit_gate("idle_park", status, status)
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
