# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Unix socket JSONL server for hopper."""

import atexit
import copy
import fcntl
import json
import logging
import os
import queue
import signal
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from enum import Enum
from pathlib import Path

from hopper import config, oom
from hopper.backlog import (
    BacklogItem,
    add_backlog_item,
    load_backlog,
    remove_backlog_item,
    save_backlog,
    set_backlog_queued,
    update_backlog_item,
)
from hopper.backlog import (
    find_by_prefix as find_backlog_by_prefix,
)
from hopper.claude import spawn_claude
from hopper.client import (
    OOM_SCOPE_ENV,
    RUN_GENERATION_ENV,
    RUNNER_MUTATION_TYPES,
)
from hopper.git import delete_branch, is_dirty, remove_worktree
from hopper.lodes import (
    archive_lode,
    create_lode,
    current_time_ms,
    find_lodes_by_prefix,
    format_terminal_failure_status,
    get_worktree_dir,
    is_terminal_failure_kind,
    load_archived_lodes,
    load_lodes,
    reset_lode_claude_stage,
    save_lodes,
    set_lode_claude_started,
    touch,
    unarchive_lode,
    update_lode_branch,
    update_lode_codex_thread,
    update_lode_stage,
    update_lode_state,
    update_lode_status,
    update_lode_title,
)
from hopper.process import STAGES
from hopper.projects import Project, disabled_project_message, find_project, get_active_projects
from hopper.tmux import (
    Liveness,
    PanePhase,
    capture_pane,
    classify_pane_phase,
    get_pane_pid,
    pane_liveness,
    pane_needs_answer,
    pane_title,
    paste_buffer,
    read_pane_input,
    send_keys,
)

logger = logging.getLogger(__name__)

PROGRESS_REJECT_STATES = frozenset({"new", "gated", "ready", "completed", "error"})
LISTEN_BACKLOG = 64
PAUSE_TERM_GRACE_SEC = 0.75
PAUSE_KILL_GRACE_SEC = 0.5
PAUSE_PROCESS_POLL_SEC = 0.02
PROCESS_GROUP_STATUS_TIMEOUT_SEC = 1.0
GUARDED_DISCONNECT_HOLD_SEC = 60.0
assert oom.SCOPE_RESULT_SETTLE_SEC < GUARDED_DISCONNECT_HOLD_SEC
FROZEN_PANE_THRESHOLD_MS = 10 * 60_000
FROZEN_PANE_THRESHOLD_MIN = FROZEN_PANE_THRESHOLD_MS // 60_000
_FEEDBACK_POLL_INTERVAL = 0.25
_FEEDBACK_IDLE_POLL_COUNT = 12
_FEEDBACK_SETTLE_POLL_COUNT = 4
_FEEDBACK_ACCEPTANCE_POLL_COUNT = 12
_FEEDBACK_IDLE_WAIT_SECONDS = _FEEDBACK_POLL_INTERVAL * _FEEDBACK_IDLE_POLL_COUNT
_FEEDBACK_SETTLE_WAIT_SECONDS = _FEEDBACK_POLL_INTERVAL * _FEEDBACK_SETTLE_POLL_COUNT
_FEEDBACK_ACCEPTANCE_WAIT_SECONDS = _FEEDBACK_POLL_INTERVAL * _FEEDBACK_ACCEPTANCE_POLL_COUNT


def _is_verified_ordinary_exit(unit_result: str | None, worker_returncode: int | None) -> bool:
    """Return whether the supervisor verified an ordinary successful exit."""
    return unit_result in (None, "success") and worker_returncode == 0


_ACCEPTED_DELIVERY_REASONS = frozenset({"auto_submitted", "enter_accepted"})
_DELIVERY_FAILURE_OUTCOMES = {
    "pane_unavailable": "pane_unavailable",
    "idle_timeout": "busy",
    "pane_state_unknown": "pane_state_unknown",
    "pane_frozen": "pane_frozen",
    "pane_awaiting_choice": "awaiting_choice",
    "paste_failed": "not_sent",
    "paste_failed_unknown": "unverified",
    "paste_not_staged": "unverified",
    "pane_lost_after_paste": "unverified",
    "submit_failed": "not_sent",
    "acceptance_timeout": "unverified",
    "pane_lost_after_submit": "unverified",
}
_GATE_FEEDBACK_STATUSES = {
    "pane_unavailable": "Feedback blocked: pane unavailable",
    "busy": "Feedback blocked: pane busy",
    "not_sent": "Feedback not sent; gate remains blocked",
    "unverified": "Feedback outcome unknown; inspect pane",
    "pane_state_unknown": "Feedback blocked: pane state unrecognized",
    "pane_frozen": "Feedback blocked: pane appears frozen",
    "awaiting_choice": "Feedback blocked: pane awaiting a numbered choice",
}
_GATE_FEEDBACK_MESSAGES = {
    "pane_unavailable": (
        "Feedback was not sent because pane {pane} is unavailable. No feedback was pasted "
        "or submitted. Run `hop lode resume {lode_id}`, wait for the prompt, then retry "
        "the same feedback."
    ),
    "idle_timeout": (
        f"Feedback was not sent because pane {{pane}} did not become idle within "
        f"{_FEEDBACK_IDLE_WAIT_SECONDS:.1f}s. No feedback was pasted or submitted, and "
        "Hopper does not know when the pane will be ready. Wait for the current turn to "
        "finish, then retry the same feedback."
    ),
    "pane_state_unknown": (
        "Feedback was not sent because Hopper does not recognize the pane state reported "
        "for pane {pane}: {title}. Inspect with `hop lode peek {lode_id}`. It is safe to "
        "retry the same feedback after the pane reaches a recognized idle state."
    ),
    "pane_frozen": (
        "Feedback was not sent because pane {pane} has reported the same processing title "
        f"for at least {FROZEN_PANE_THRESHOLD_MIN} min. Inspect with `hop lode peek "
        "{lode_id}` before deciding whether to retry."
    ),
    "pane_awaiting_choice": (
        "Feedback was not sent. Pane {pane} is waiting on a numbered choice, which is a "
        "selector rather than a text box. Nothing was pasted. Read the options with "
        "`hop lode peek {lode_id}`, then answer with `hop lode answer {lode_id} <n>`."
    ),
    "paste_failed": (
        "Feedback was not sent because Hopper could not paste it into pane {pane}. Nothing "
        "was submitted. Retry the same feedback."
    ),
    "paste_failed_unknown": (
        "Hopper could not complete the paste into pane {pane}, but some feedback text may "
        "have reached the pane. The delivery outcome is unknown. Inspect with `hop lode "
        "peek {lode_id}` before deciding whether to retry; do not paste the feedback again "
        "unless the pane proves it was not accepted or staged."
    ),
    "paste_not_staged": (
        "Hopper pasted feedback into pane {pane}, but no new user turn was observed within "
        f"{_FEEDBACK_SETTLE_WAIT_SECONDS:.1f}s. The delivery outcome is unknown. Inspect "
        "with `hop lode peek {lode_id}` before deciding whether to retry; do not paste the "
        "feedback again unless the pane proves it was not accepted or staged."
    ),
    "pane_lost_after_paste": (
        "Hopper pasted feedback into pane {pane}, but the pane became unavailable before a "
        "new user turn was observed. The delivery outcome is unknown. Run `hop lode resume "
        "{lode_id}`, then inspect with `hop lode peek {lode_id}` before deciding whether to "
        "retry; do not paste the feedback again unless the pane proves it was not accepted "
        "or staged."
    ),
    "submit_failed": (
        "Feedback was not submitted because Hopper could not press Enter in pane {pane}. "
        "The feedback is still staged. Inspect with `hop lode peek {lode_id}`, then submit "
        "it once instead of pasting it again."
    ),
    "acceptance_timeout": (
        "Hopper pressed Enter in pane {pane}, but did not observe the required "
        f"idle-to-processing transition within {_FEEDBACK_ACCEPTANCE_WAIT_SECONDS:.1f}s. "
        "The delivery outcome is unknown. Inspect with `hop lode peek {lode_id}` before "
        "deciding whether to retry; do not paste the feedback again unless the pane proves "
        "it was not accepted or staged."
    ),
    "pane_lost_after_submit": (
        "Hopper pressed Enter in pane {pane}, then the pane became unavailable before "
        "acceptance could be verified. The delivery outcome is unknown. Run `hop lode "
        "resume {lode_id}`, then inspect with `hop lode peek {lode_id}` before deciding "
        "whether to retry; do not paste the feedback again unless the pane proves it was "
        "not accepted or staged."
    ),
}
_PANE_INPUT_MESSAGES = {
    "pane_unavailable": (
        "Input was not sent because pane {pane} is unavailable. Run `hop lode resume "
        "{lode_id}`, wait for the prompt, then retry."
    ),
    "idle_timeout": (
        f"Input was not sent because pane {{pane}} did not become idle within "
        f"{_FEEDBACK_IDLE_WAIT_SECONDS:.1f}s. Hopper observed the pane processing a turn. "
        "Wait for the turn to finish, then retry."
    ),
    "pane_state_unknown": (
        "Input was not sent because Hopper does not recognize the pane state reported for "
        "pane {pane}: {title}. Inspect with `hop lode peek {lode_id}`. It is safe to retry "
        "after the pane reaches a recognized idle state."
    ),
    "pane_frozen": (
        "Input was not sent because pane {pane} has reported the same processing title for "
        f"at least {FROZEN_PANE_THRESHOLD_MIN} min. Inspect with `hop lode peek "
        "{lode_id}` before deciding whether to retry."
    ),
    "pane_awaiting_choice": (
        "Input was not sent. Pane {pane} is waiting on a numbered choice, which is a "
        "selector rather than a text box -- pasted text would sit in it with nothing able "
        "to submit it, and each retry would append to what is already there. Nothing was "
        "pasted. Read the options with `hop lode peek {lode_id}`, then answer with "
        "`hop lode answer {lode_id} <n>`. If the option you want is free-text "
        '("Type something"), Hopper cannot drive it: select it with `hop lode answer`, '
        "then type into pane {pane} directly."
    ),
    "paste_failed": (
        "Input was not sent because Hopper could not deliver it to pane {pane}. Retry the "
        "same input."
    ),
    "paste_failed_unknown": (
        "Hopper could not complete delivery to pane {pane}, but some input may have reached "
        "the pane. Inspect with `hop lode peek {lode_id}` before retrying; do not send it "
        "again unless the pane shows it was not accepted or staged."
    ),
    "paste_not_staged": (
        f"Hopper delivered input to pane {{pane}}, but no new user turn or staged input was "
        f"observed within {_FEEDBACK_SETTLE_WAIT_SECONDS:.1f}s. Inspect with `hop lode peek "
        "{lode_id}` before retrying; do not send it again unless the pane shows it was not "
        "accepted or staged."
    ),
    "pane_lost_after_paste": (
        "Hopper delivered input to pane {pane}, but the pane became unavailable before a "
        "new user turn was observed. Run `hop lode resume {lode_id}`, then inspect with "
        "`hop lode peek {lode_id}` before retrying; do not send it again unless the pane "
        "shows it was not accepted or staged."
    ),
    "submit_failed": (
        "Input is staged in pane {pane}, but Hopper could not press Enter. Inspect with "
        "`hop lode peek {lode_id}`, then submit it once instead of sending it again."
    ),
    "acceptance_timeout": (
        "Hopper pressed Enter in pane {pane}, but did not observe the required "
        f"idle-to-processing transition within {_FEEDBACK_ACCEPTANCE_WAIT_SECONDS:.1f}s. "
        "Inspect with `hop lode peek {lode_id}` before retrying; do not send it again unless "
        "the pane shows it was not accepted or staged."
    ),
    "pane_lost_after_submit": (
        "Hopper pressed Enter in pane {pane}, then the pane became unavailable before "
        "acceptance could be verified. Run `hop lode resume {lode_id}`, then inspect with "
        "`hop lode peek {lode_id}` before retrying; do not send it again unless the pane "
        "shows it was not accepted or staged."
    ),
}
# Any future reason returned before delivery must be added here.
_PRE_PASTE_REASONS = frozenset(
    {
        "pane_unavailable",
        "idle_timeout",
        "pane_state_unknown",
        "pane_frozen",
        "pane_awaiting_choice",
    }
)


class ServerLockHeld(RuntimeError):
    """Raised when another hopper server holds the socket's singleton lock."""


class SpawnOutcome(Enum):
    """Result of a server-gated runner spawn request."""

    SPAWNED = "spawned"
    ALREADY_LIVE = "already_live"
    REFUSED_UNKNOWN = "refused_unknown"
    FAILED = "failed"


SPAWN_STATUS_PREFIXES = ("spawn refused: ", "spawn failed: ")


def _process_group_has_live_members(process_group: int) -> bool | None:
    """Return whether a process group has non-zombie members, or None if unknown."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pgid=,stat="],
            capture_output=True,
            text=True,
            timeout=PROCESS_GROUP_STATUS_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.error("cannot inspect runner process group %s: %s", process_group, error)
        return None
    if result.returncode != 0:
        logger.error(
            "cannot inspect runner process group %s: ps exited %s: %s",
            process_group,
            result.returncode,
            result.stderr.strip(),
        )
        return None

    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) != 2:
            continue
        raw_group, status = fields
        try:
            member_group = int(raw_group)
        except ValueError:
            continue
        if member_group == process_group and not status.startswith("Z"):
            return True
    return False


def _process_group_exited(process_group: int, timeout: float) -> bool:
    """Wait until a process group has no live members, failing closed when unknown."""
    deadline = time.monotonic() + timeout
    while True:
        live_members = _process_group_has_live_members(process_group)
        if live_members is False:
            return True
        if live_members is None:
            return False

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(PAUSE_PROCESS_POLL_SEC, remaining))


def _terminate_runner_process_group(pid: int, *, process_group: int | None = None) -> bool:
    """Terminate a runner and every child that can retain its Claude session."""
    if process_group is None:
        try:
            process_group = os.getpgid(pid)
        except ProcessLookupError:
            return True
        except PermissionError:
            logger.error("cannot inspect runner pid %s: permission denied", pid)
            return False

    if process_group == os.getpgrp():
        logger.error(
            "refusing to terminate runner pid %s in hopper server process group %s",
            pid,
            process_group,
        )
        return False

    for shutdown_signal, grace in (
        (signal.SIGTERM, PAUSE_TERM_GRACE_SEC),
        (signal.SIGKILL, PAUSE_KILL_GRACE_SEC),
    ):
        try:
            os.killpg(process_group, shutdown_signal)
        except ProcessLookupError:
            return True
        except PermissionError:
            logger.error("cannot signal runner process group %s: permission denied", process_group)
            return False
        if _process_group_exited(process_group, grace):
            return True

    logger.error("runner process group %s remained alive after SIGKILL", process_group)
    return False


def _corroborated_runner_process_group(runner_pid: int, pane_pid: int) -> int | None:
    """Resolve the shared process group for a registered runner and pane owner."""
    try:
        runner_group = os.getpgid(runner_pid)
        if runner_group == os.getpgid(pane_pid):
            return runner_group
    except (OSError, TypeError):
        pass
    return None


def _runner_process_exited(pid: int) -> bool:
    """Return whether a registered runner PID is definitely gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, TypeError):
        return False
    return False


def _set_spawn_refusal(lode: dict, message: str) -> bool:
    """Set a visible spawn refusal without changing workflow or runner state."""
    status = f"spawn refused: {message}"
    if lode.get("status") == status:
        return False
    lode["status"] = status
    return True


def _clear_spawn_refusal(lode: dict) -> bool:
    """Clear a refusal or failure after live runner evidence supersedes it."""
    status = lode.get("status", "")
    if not status.startswith(SPAWN_STATUS_PREFIXES):
        return False
    lode["status"] = ""
    return True


def _render_observed_title(title: str | None) -> str:
    """Render a pane title without losing or inventing title content."""
    return f'"{title}"' if title is not None else "<no title reported>"


def _observe_processing_pane_title(title: str, observation: dict | None) -> bool:
    """Update cross-attempt processing evidence and report a frozen pane."""
    if observation is None:
        return False
    now = current_time_ms()
    if observation.get("title") != title or not isinstance(observation.get("observed_at"), int):
        observation.clear()
        observation.update({"title": title, "observed_at": now})
        return False
    return now - observation["observed_at"] >= FROZEN_PANE_THRESHOLD_MS


def _attempt_pane_delivery(
    pane_id: str | None,
    text: str,
    *,
    paste: bool,
    pane_title_observation: dict | None = None,
) -> dict:
    """Attempt one pane delivery and return its reason and latest observations."""
    observed_title = None
    if not pane_id:
        return {"reason": "pane_unavailable", "capture": None, "title": observed_title}

    latest_capture = capture_pane(pane_id, plain=True)
    if latest_capture is None:
        return {"reason": "pane_unavailable", "capture": None, "title": observed_title}

    pre_delivery_input = None
    saw_processing = False
    processing_frozen = False
    for _ in range(_FEEDBACK_IDLE_POLL_COUNT):
        time.sleep(_FEEDBACK_POLL_INTERVAL)
        capture = capture_pane(pane_id, plain=True)
        if capture is None:
            return {
                "reason": "pane_unavailable",
                "capture": latest_capture,
                "title": observed_title,
            }
        latest_capture = capture
        observed_title = pane_title(pane_id)
        phase = classify_pane_phase(observed_title)
        if phase is PanePhase.PROCESSING:
            saw_processing = True
            processing_frozen = _observe_processing_pane_title(
                observed_title,
                pane_title_observation,
            )
        elif phase is PanePhase.IDLE:
            if pane_title_observation is not None:
                pane_title_observation.clear()
            if paste and pane_needs_answer(latest_capture):
                # A numbered selector is not a text composer. Pasting free text into
                # one stages it with nothing able to submit it, and each retry appends
                # to what is already there -- so refuse before touching the pane and
                # name the verb that does work.
                return {
                    "reason": "pane_awaiting_choice",
                    "capture": latest_capture,
                    "title": observed_title,
                }
            pre_delivery_input = read_pane_input(latest_capture)
            break
        else:
            processing_frozen = False
            if pane_title_observation is not None:
                pane_title_observation.clear()
    else:
        if not saw_processing:
            reason = "pane_state_unknown"
        elif processing_frozen:
            reason = "pane_frozen"
        else:
            reason = "idle_timeout"
        return {"reason": reason, "capture": latest_capture, "title": observed_title}

    delivered = paste_buffer(pane_id, text) if paste else send_keys(pane_id, text)
    if not delivered:
        capture = capture_pane(pane_id, plain=True)
        if capture is None:
            return {
                "reason": "paste_failed_unknown",
                "capture": latest_capture,
                "title": observed_title,
            }
        latest_capture = capture
        post_delivery_input = read_pane_input(latest_capture)
        reason = (
            "paste_failed_unknown"
            if pre_delivery_input is None or post_delivery_input != pre_delivery_input
            else "paste_failed"
        )
        return {"reason": reason, "capture": latest_capture, "title": observed_title}

    for _ in range(_FEEDBACK_SETTLE_POLL_COUNT):
        time.sleep(_FEEDBACK_POLL_INTERVAL)
        capture = capture_pane(pane_id, plain=True)
        if capture is None:
            return {
                "reason": "pane_lost_after_paste",
                "capture": latest_capture,
                "title": observed_title,
            }
        latest_capture = capture
        observed_title = pane_title(pane_id)
        phase = classify_pane_phase(observed_title)
        if phase is PanePhase.PROCESSING:
            return {
                "reason": "auto_submitted",
                "capture": latest_capture,
                "title": observed_title,
            }
        if phase is PanePhase.IDLE and read_pane_input(latest_capture):
            break
    else:
        return {
            "reason": "paste_not_staged",
            "capture": latest_capture,
            "title": observed_title,
        }

    if not send_keys(pane_id, "Enter"):
        return {
            "reason": "submit_failed",
            "capture": latest_capture,
            "title": observed_title,
        }

    for _ in range(_FEEDBACK_ACCEPTANCE_POLL_COUNT):
        time.sleep(_FEEDBACK_POLL_INTERVAL)
        capture = capture_pane(pane_id, plain=True)
        if capture is None:
            return {
                "reason": "pane_lost_after_submit",
                "capture": latest_capture,
                "title": observed_title,
            }
        latest_capture = capture
        observed_title = pane_title(pane_id)
        if classify_pane_phase(observed_title) is PanePhase.PROCESSING:
            return {
                "reason": "enter_accepted",
                "capture": latest_capture,
                "title": observed_title,
            }
    return {
        "reason": "acceptance_timeout",
        "capture": latest_capture,
        "title": observed_title,
    }


def _deliver_pane_input(
    lode_id: str,
    pane_id: str | None,
    text: str,
    *,
    paste: bool,
    pane_title_observation: dict | None = None,
) -> dict:
    """Deliver pane input and emit exactly one outcome record."""
    try:
        result = _attempt_pane_delivery(
            pane_id,
            text,
            paste=paste,
            pane_title_observation=pane_title_observation,
        )
    except Exception:
        logger.warning(
            "Pane delivery failed lode=%s pane=%s reason=%s outcome=%s title=%s",
            lode_id,
            pane_id or "<unknown>",
            "delivery_exception",
            "unverified",
            _render_observed_title(None),
        )
        # delivery_exception is logging-only; no taxonomy response exists because this re-raises.
        raise
    reason = result["reason"]
    accepted = reason in _ACCEPTED_DELIVERY_REASONS
    outcome = "accepted" if accepted else _DELIVERY_FAILURE_OUTCOMES[reason]
    rendered_title = _render_observed_title(result["title"])
    if accepted:
        logger.info(
            "Pane delivery accepted lode=%s pane=%s reason=%s outcome=%s title=%s",
            lode_id,
            pane_id or "<unknown>",
            reason,
            outcome,
            rendered_title,
        )
    else:
        logger.warning(
            "Pane delivery failed lode=%s pane=%s reason=%s outcome=%s title=%s",
            lode_id,
            pane_id or "<unknown>",
            reason,
            outcome,
            rendered_title,
        )
    return result


def _deliver_lode_pane_input(lodes: list[dict], lode: dict, text: str, *, paste: bool) -> dict:
    """Deliver input and persist only cross-attempt pane-title evidence."""
    prior_observation = lode.get("pane_title_observation")
    observation = dict(prior_observation) if isinstance(prior_observation, dict) else {}
    result = _deliver_pane_input(
        lode["id"],
        lode.get("tmux_pane"),
        text,
        paste=paste,
        pane_title_observation=observation,
    )
    updated_observation = observation or None
    if updated_observation != prior_observation:
        lode["pane_title_observation"] = updated_observation
        save_lodes(lodes)
    return result


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

    Tracks which clients own which lodes. Ordinary disconnects clear runner
    identity immediately; guarded disconnects defer until scope classification.
    """

    def __init__(self, socket_path: Path, tmux_location: dict | None = None):
        self.socket_path = Path(socket_path)
        self.tmux_location = tmux_location
        self.git_hash = get_git_hash()
        self.started_at = current_time_ms()
        self.clients: list[socket.socket] = []
        self.write_locks: dict[socket.socket, threading.Lock] = {}
        self.lock = threading.RLock()
        self._request_context = threading.local()
        self.stop_event = threading.Event()
        self.server_socket: socket.socket | None = None
        self.broadcast_queue: queue.Queue = queue.Queue(maxsize=10000)
        self.event_queue: queue.Queue = queue.Queue(maxsize=10000)
        self.writer_thread: threading.Thread | None = None
        self.event_thread: threading.Thread | None = None
        self.lodes: list[dict] = []
        self.archived_lodes: list[dict] = []
        self.backlog: list[BacklogItem] = []
        self.projects: list[Project] = []
        # Lode ownership tracking: lode_id -> socket, socket -> lode_id
        self.lode_clients: dict[str, socket.socket] = {}
        self.client_lodes: dict[socket.socket, str] = {}
        self.client_generations: dict[socket.socket, str] = {}
        self.pending_disconnects: dict[tuple[str, str], dict] = {}
        self.runner_results: dict[tuple[str, str], tuple[str | None, int]] = {}
        self._log_handler: logging.FileHandler | None = None
        self._lock_file = None
        self._socket_bound = False
        self.ready = threading.Event()
        self.startup_error: Exception | None = None

    def _find_lode(self, lode_id: str) -> dict | None:
        """Find a lode by ID."""
        return next((lode for lode in self.lodes if lode["id"] == lode_id), None)

    def _runner_generation_matches(self, lode: dict | None, message: dict) -> bool:
        """Admit a runner mutation only for the lode's current run generation."""
        if not lode:
            return False
        expected = lode.get("run_generation")
        observed = message.get("run_generation")
        matched = bool(expected and observed and observed == expected)
        if not matched:
            logger.info(
                "Dropping stale runner mutation type=%s lode=%s generation=%s current=%s",
                message.get("type"),
                lode.get("id"),
                observed,
                expected,
            )
        return matched

    def _acquire_server_lock(self) -> None:
        """Acquire and retain the singleton lock colocated with the server socket."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.socket_path.with_suffix(".pid")
        lock_file = open(lock_path, "a+")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            try:
                lock_file.seek(0)
                contents = lock_file.read().strip()
            except (OSError, UnicodeError):
                contents = ""
            lock_file.close()
            try:
                parsed_pid = int(contents)
            except ValueError:
                parsed_pid = 0
            pid = str(parsed_pid) if parsed_pid > 0 else "unavailable"
            raise ServerLockHeld(
                f"a live hopper server (pid {pid}) holds the lock; "
                "attach to it or stop it before starting another"
            ) from error
        except Exception:
            lock_file.close()
            raise

        try:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(str(os.getpid()))
            lock_file.flush()
        except Exception:
            lock_file.close()
            raise
        self._lock_file = lock_file

    def _unlink_owned_socket(self) -> None:
        """Unlink the socket only if this server successfully bound it."""
        with self.lock:
            if not self._socket_bound:
                return
            try:
                self.socket_path.unlink(missing_ok=True)
            except OSError as error:
                logger.warning("Failed to remove server socket %s: %s", self.socket_path, error)
            else:
                self._socket_bound = False

    def _reconcile_startup_lodes(self) -> None:
        """Reconcile recorded runner identity against tmux without guessing."""
        changed = False
        for lode in self.lodes:
            generation = lode.get("run_generation")
            if generation and (lode["id"], generation) in self.pending_disconnects:
                continue
            if is_terminal_failure_kind(lode.get("failure_kind")):
                if lode.get("active") or lode.get("tmux_pane") or lode.get("pid"):
                    lode["active"] = False
                    lode["tmux_pane"] = None
                    lode["pid"] = None
                    changed = True
                continue
            pane = lode.get("tmux_pane")
            if pane:
                liveness = pane_liveness(pane)
                if liveness is Liveness.ALIVE:
                    changed = _clear_spawn_refusal(lode) or changed
                elif liveness is Liveness.GONE:
                    if lode.get("active") or lode.get("tmux_pane") or lode.get("pid"):
                        lode["active"] = False
                        lode["tmux_pane"] = None
                        lode["pid"] = None
                        changed = True
                    changed = _clear_spawn_refusal(lode) or changed
                else:
                    changed = (
                        _set_spawn_refusal(
                            lode,
                            "tmux unreachable — verify tmux is running, then retry",
                        )
                        or changed
                    )
                    logger.warning(
                        "lode %s: tmux liveness unknown for pane %s; preserving runner identity",
                        lode["id"],
                        pane,
                    )
            else:
                if lode.get("active") or lode.get("pid"):
                    lode["active"] = False
                    lode["tmux_pane"] = None
                    lode["pid"] = None
                    changed = True
                changed = _clear_spawn_refusal(lode) or changed

        if changed:
            save_lodes(self.lodes)

    def _consume_failed_oom_units(self) -> None:
        """Consume retained failed scope evidence before startup reconciliation."""
        systemctl = oom.find_systemctl()
        if not systemctl:
            for lode in self.lodes:
                if lode.get("oom_scope") and not is_terminal_failure_kind(lode.get("failure_kind")):
                    self._set_terminal_failure(
                        lode,
                        "runner_exit_unverified",
                        broadcast=False,
                    )
            return
        for lode in self.lodes:
            unit_name = lode.get("oom_scope")
            run_generation = lode.get("run_generation")
            if not unit_name or not run_generation:
                continue
            unit_result = oom.read_scope_result(systemctl, unit_name)
            if unit_result == "oom-kill":
                self._set_terminal_failure(lode, "oom", broadcast=False)
            elif is_terminal_failure_kind(lode.get("failure_kind")):
                if unit_result not in (None, "success"):
                    oom.release_scope(systemctl, unit_name)
                continue
            elif unit_result not in (None, "success"):
                self._set_terminal_failure(lode, "runner_exit_unverified", broadcast=False)
            elif unit_result is None:
                self.pending_disconnects[(lode["id"], run_generation)] = {
                    "deadline": time.monotonic() + GUARDED_DISCONNECT_HOLD_SEC,
                    "unit_name": unit_name,
                }
                logger.warning(
                    "Startup scope result unavailable lode=%s generation=%s",
                    lode["id"],
                    run_generation,
                )
                continue
            else:
                continue
            oom.release_scope(systemctl, unit_name)

    def _gated_spawn(
        self,
        lode: dict,
        project_path: str | None,
        *,
        foreground: bool = False,
        spawn_updates: dict | None = None,
        pre_spawn: Callable[[], None] | None = None,
        allow_terminal_recovery: bool = False,
    ) -> tuple[SpawnOutcome, str | None]:
        """Spawn a runner only when its recorded pane is absent or gone."""
        generation = lode.get("run_generation")
        if generation and (lode["id"], generation) in self.pending_disconnects:
            logger.warning("lode %s: spawn suppressed while scope result is pending", lode["id"])
            return SpawnOutcome.FAILED, None
        terminal_recovery = is_terminal_failure_kind(lode.get("failure_kind"))
        if terminal_recovery and not allow_terminal_recovery:
            logger.warning("lode %s: automatic spawn suppressed by terminal failure", lode["id"])
            return SpawnOutcome.FAILED, None

        pane = lode.get("tmux_pane")
        if pane:
            liveness = pane_liveness(pane)
            if liveness is Liveness.ALIVE:
                logger.warning(
                    "lode %s: runner already live in pane %s; attach instead of spawning",
                    lode["id"],
                    pane,
                )
                if _set_spawn_refusal(
                    lode,
                    f"runner already live in pane {pane} — attach instead",
                ):
                    save_lodes(self.lodes)
                    self.broadcast({"type": "lode_updated", "lode": lode})
                return SpawnOutcome.ALREADY_LIVE, None
            if liveness is Liveness.UNKNOWN:
                logger.warning(
                    "lode %s: tmux liveness unknown for pane %s; refusing spawn",
                    lode["id"],
                    pane,
                )
                if _set_spawn_refusal(
                    lode,
                    "tmux unreachable — verify tmux is running, then retry",
                ):
                    save_lodes(self.lodes)
                    self.broadcast({"type": "lode_updated", "lode": lode})
                return SpawnOutcome.REFUSED_UNKNOWN, None

        prior_lode = copy.deepcopy(lode)
        lode["active"] = False
        lode["tmux_pane"] = None
        lode["pid"] = None
        _clear_spawn_refusal(lode)
        if spawn_updates:
            updates = dict(spawn_updates)
            if terminal_recovery:
                for field in ("state", "status", "failure_kind"):
                    updates.pop(field, None)
            lode.update(updates)
        run_generation = uuid.uuid4().hex
        lode["run_generation"] = run_generation
        lode["oom_scope"] = (
            oom.scope_unit_name(lode["id"], run_generation) if oom.is_linux() else None
        )
        touch(lode)
        save_lodes(self.lodes)
        if pre_spawn:
            pre_spawn()

        launch_error = None
        try:
            pane_env = {RUN_GENERATION_ENV: run_generation}
            if lode.get("oom_scope"):
                pane_env[OOM_SCOPE_ENV] = lode["oom_scope"]
            pane_id = spawn_claude(
                lode["id"],
                project_path,
                foreground=foreground,
                env=pane_env,
            )
        except OSError as error:
            logger.error("lode %s: failed to create tmux runner pane: %s", lode["id"], error)
            launch_error = error
            pane_id = None
        if not pane_id:
            if launch_error is None:
                logger.error("lode %s: failed to create tmux runner pane", lode["id"])
            lode.clear()
            lode.update(prior_lode)
            lode["active"] = False
            lode["tmux_pane"] = None
            lode["pid"] = None
            if not terminal_recovery:
                lode["status"] = (
                    "spawn failed: tmux could not create a runner pane — "
                    "verify tmux is running, then retry"
                )
            touch(lode)
            save_lodes(self.lodes)
            self.broadcast({"type": "lode_updated", "lode": lode})
            return SpawnOutcome.FAILED, None

        lode["tmux_pane"] = pane_id
        touch(lode)
        save_lodes(self.lodes)
        self.broadcast({"type": "lode_updated", "lode": lode})
        return SpawnOutcome.SPAWNED, pane_id

    def start(self) -> None:
        """Start the server (blocking)."""
        self._acquire_server_lock()
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

        self._consume_failed_oom_units()
        self._reconcile_startup_lodes()

        # Runs only while lock-held; UNKNOWN panes do not block shipped auto-archive.
        shipped = [
            lode
            for lode in self.lodes
            if lode.get("stage") == "shipped"
            and not is_terminal_failure_kind(lode.get("failure_kind"))
            and (
                not lode.get("run_generation")
                or (lode["id"], lode["run_generation"]) not in self.pending_disconnects
            )
        ]
        for lode in shipped:
            archived = archive_lode(self.lodes, lode["id"])
            if archived:
                self.archived_lodes.append(archived)
                logger.info(f"Startup: auto-archived shipped lode {lode['id']}")
                self._cleanup_worktree(archived)

        # Safe only because the singleton lock proves no live server owns it.
        if self.socket_path.exists():
            self.socket_path.unlink()

        self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.server_socket.bind(str(self.socket_path))
            self._socket_bound = True
            self.server_socket.listen(LISTEN_BACKLOG)
            self.ready.set()
            self.server_socket.settimeout(1.0)

            # Start writer thread (serializes broadcasts)
            self.writer_thread = threading.Thread(
                target=self._writer_loop, name="server-writer", daemon=True
            )
            self.writer_thread.start()

            # Start event loop thread (serializes all state mutations)
            self.event_thread = threading.Thread(
                target=self._event_loop, name="server-events", daemon=True
            )
            self.event_thread.start()

            logger.info(f"Server listening on {self.socket_path}")

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
            self._unlink_owned_socket()

    # Message types that only read state and send a response (safe from any thread)
    _READ_ONLY_TYPES = frozenset({"connect", "ping", "lode_list", "backlog_list", "archived_list"})

    def _handle_client(self, conn: socket.socket) -> None:
        """Handle a client connection.

        Read-only messages are handled inline. Mutations are enqueued
        to the event loop thread for serialized processing.
        """
        with self.lock:
            self.clients.append(conn)
            self.write_locks[conn] = threading.Lock()

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
                                msg_type = message.get("type")
                                if msg_type in self._READ_ONLY_TYPES:
                                    self._handle_read_only(message, conn)
                                else:
                                    self._enqueue_event(message, conn)
                            except json.JSONDecodeError:
                                pass
                except socket.timeout:
                    continue
        except Exception as e:
            logger.debug(f"Client error: {e}")
        finally:
            self._enqueue_event({"type": "_client_disconnect"}, conn)
            with self.lock:
                if conn in self.clients:
                    self.clients.remove(conn)
                self.write_locks.pop(conn, None)
            try:
                conn.close()
            except Exception:
                pass
            logger.debug(f"Client disconnected ({len(self.clients)} remaining)")

    def _on_client_disconnect(self, conn: socket.socket) -> None:
        """Handle client disconnect - deactivate lode, auto-advance, or auto-archive.

        Runs on the event loop thread — no lock needed for state mutations.
        """
        lode_id = self.client_lodes.pop(conn, None)
        run_generation = self.client_generations.pop(conn, None)
        owned = bool(lode_id and self.lode_clients.get(lode_id) is conn)
        if owned:
            self.lode_clients.pop(lode_id, None)

        if not lode_id or not owned:
            return

        lode = self._find_lode(lode_id)
        if not lode:
            return

        if run_generation != lode.get("run_generation"):
            return

        key = (lode_id, run_generation)
        observed_result = self.runner_results.pop(key, None)
        if is_terminal_failure_kind(lode.get("failure_kind")):
            self.pending_disconnects.pop(key, None)
            return
        if observed_result is not None and _is_verified_ordinary_exit(*observed_result):
            self._finalize_lode_disconnect(lode_id, run_generation)
            return

        if lode.get("oom_scope"):
            self.pending_disconnects[key] = {
                "deadline": time.monotonic() + GUARDED_DISCONNECT_HOLD_SEC,
                "unit_name": lode["oom_scope"],
            }
            logger.info("Holding guarded disconnect lode=%s generation=%s", lode_id, run_generation)
            return

        self._finalize_lode_disconnect(lode_id, run_generation)

    def _finalize_lode_disconnect(self, lode_id: str, run_generation: str | None) -> None:
        """Apply ordinary disconnect state after guarded-result resolution."""
        lode = self._find_lode(lode_id)
        if not lode or lode.get("run_generation") != run_generation:
            return
        if lode_id in self.lode_clients:
            return

        lode["active"] = False
        lode["tmux_pane"] = None
        lode["pid"] = None
        lode["oom_scope"] = None
        touch(lode)
        save_lodes(self.lodes)

        logger.info(f"Lode {lode_id} disconnected, active=False")
        self.broadcast({"type": "lode_updated", "lode": lode})

        stage = lode.get("stage", "")
        if (
            lode.get("state") == "ready"
            and stage in STAGES
            and lode.get("status") != STAGES[stage]["done_status"]
            and not is_terminal_failure_kind(lode.get("failure_kind"))
        ):
            project = find_project(lode.get("project", ""))
            project_path = project.path if project else None
            if project_path:
                logger.info(f"Auto-advancing lode {lode_id} to {stage}")
                self._gated_spawn(lode, project_path, foreground=False)
            else:
                logger.warning(f"Auto-advance skipped for {lode_id}: project not found")

        # Auto-archive shipped lodes
        if stage == "shipped" and not is_terminal_failure_kind(lode.get("failure_kind")):
            archived = archive_lode(self.lodes, lode_id)
            if archived:
                self.archived_lodes.append(archived)
                logger.info(f"Lode {lode_id} auto-archived")
                self.broadcast({"type": "lode_archived", "lode": archived})
                self._cleanup_worktree(archived)

    def _set_terminal_failure(
        self,
        lode: dict,
        failure_kind: str,
        *,
        broadcast: bool = True,
    ) -> bool:
        """Persist one canonical terminal runner failure."""
        if lode.get("failure_kind") == "oom" and failure_kind != "oom":
            return False
        status = format_terminal_failure_status(failure_kind, lode["id"])
        changed = (
            lode.get("failure_kind") != failure_kind
            or lode.get("status") != status
            or lode.get("state") != "error"
            or lode.get("active", False)
            or lode.get("tmux_pane") is not None
            or lode.get("pid") is not None
        )
        lode["state"] = "error"
        lode["status"] = status
        lode["failure_kind"] = failure_kind
        lode["active"] = False
        lode["tmux_pane"] = None
        lode["pid"] = None
        if changed:
            touch(lode)
        save_lodes(self.lodes)
        if changed and broadcast:
            self.broadcast({"type": "lode_updated", "lode": lode})
        return changed

    def _drain_due_disconnects(self) -> None:
        """Resolve guarded disconnects whose non-blocking hold expired."""
        now = time.monotonic()
        due = [
            key for key, pending in self.pending_disconnects.items() if pending["deadline"] <= now
        ]
        for key in due:
            pending = self.pending_disconnects.pop(key, None)
            if pending is None:
                continue
            lode_id, run_generation = key
            lode = self._find_lode(lode_id)
            if (
                not lode
                or lode.get("run_generation") != run_generation
                or lode.get("oom_scope") != pending["unit_name"]
                or lode_id in self.lode_clients
            ):
                continue
            observed_result = self.runner_results.pop(key, None)
            if observed_result is not None and _is_verified_ordinary_exit(*observed_result):
                self._finalize_lode_disconnect(lode_id, run_generation)
                continue
            if lode.get("state") == "error" and not lode.get("failure_kind"):
                self._finalize_lode_disconnect(lode_id, run_generation)
            else:
                self._set_terminal_failure(lode, "runner_exit_unverified")

    def _cleanup_worktree(self, lode: dict) -> None:
        """Remove git worktree and branch for an archived lode."""
        lode_id = lode["id"]
        worktree_path = get_worktree_dir(lode_id)
        if not worktree_path.is_dir():
            return
        project_name = lode.get("project", "")
        if not project_name:
            return
        project = find_project(project_name)
        if not project:
            logger.warning(f"Cleanup skipped for {lode_id}: project not found")
            return
        if is_dirty(str(worktree_path)):
            logger.warning(
                f"Cleanup skipped for {lode_id}: worktree has uncommitted changes; "
                f"retaining {worktree_path}"
            )
            return
        remove_worktree(project.path, str(worktree_path))
        branch = lode.get("branch", "") or f"hopper-{lode_id}"
        delete_branch(project.path, branch)

    def _register_lode_client(
        self,
        lode_id: str,
        conn: socket.socket,
        tmux_pane: str | None = None,
        pid: int | None = None,
        run_generation: str | None = None,
        armed_mode: str | None = None,
        actual_unit: str | None = None,
    ) -> bool:
        """Register a client as owning a lode.

        Sets active=True on the lode and disconnects any stale owner.
        Runs on the event loop thread — no lock needed for state mutations.
        """
        lode = self._find_lode(lode_id)
        if not lode or run_generation != lode.get("run_generation"):
            return False
        terminal_recovery = is_terminal_failure_kind(lode.get("failure_kind"))
        if terminal_recovery and (not tmux_pane or tmux_pane != lode.get("tmux_pane")):
            return False
        if armed_mode == oom.OomCapability.SUPPORTED.value:
            if not actual_unit or actual_unit != lode.get("oom_scope"):
                return False
        elif armed_mode in {
            oom.OomCapability.DEGRADED_NO_CONTROLLER.value,
            oom.OomCapability.DEGRADED_NO_SCORE.value,
            oom.OomCapability.NON_LINUX.value,
        }:
            if actual_unit is not None:
                return False
            lode["oom_scope"] = None
        else:
            return False

        self.pending_disconnects.pop((lode_id, run_generation), None)

        # Check for existing owner
        existing_conn = self.lode_clients.get(lode_id)
        if existing_conn and existing_conn != conn:
            # Disconnect stale client
            old_lode_id = self.client_lodes.pop(existing_conn, None)
            self.client_generations.pop(existing_conn, None)
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
        self.client_generations[conn] = run_generation

        # Set active on the lode
        lode["active"] = True
        if tmux_pane:
            lode["tmux_pane"] = tmux_pane
        if pid:
            lode["pid"] = pid
        _clear_spawn_refusal(lode)
        if terminal_recovery:
            lode["failure_kind"] = None
            lode["state"] = "running"
            lode["status"] = f"Starting {lode.get('stage', '')}"
        touch(lode)
        save_lodes(self.lodes)
        self.broadcast({"type": "lode_updated", "lode": lode})

        logger.info(f"Registered client for lode {lode_id}, active=True")
        return True

    def _handle_read_only(self, message: dict, conn: socket.socket) -> None:
        """Handle read-only messages inline (from any client thread)."""
        self._request_context.exchange_id = message.get("exchange_id")
        msg_type = message.get("type")

        if msg_type == "connect":
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

        elif msg_type == "ping":
            self._send_response(
                conn,
                {"type": "pong", "pid": os.getpid(), "started_at": self.started_at},
            )

        elif msg_type == "lode_list":
            self._send_response(conn, {"type": "lode_list", "lodes": self.lodes})

        elif msg_type == "backlog_list":
            items_data = [item.to_dict() for item in self.backlog]
            self._send_response(conn, {"type": "backlog_list", "items": items_data})

        elif msg_type == "archived_list":
            self._send_response(conn, {"type": "archived_list", "lodes": self.archived_lodes})

    def _promote_backlog_item(self, item: BacklogItem, scope: str = "") -> dict | None:
        """Promote a backlog item to a lode. Returns the new lode dict."""
        proj = find_project(item.project)
        if proj and proj.disabled:
            logger.warning(
                "Refusing to promote backlog %s for disabled project %s",
                item.id,
                item.project,
            )
            return None
        lode = create_lode(self.lodes, item.project, scope or item.description)
        lode["backlog"] = item.to_dict()
        save_lodes(self.lodes)
        logger.info(f"Lode {lode['id']} promoted from backlog {item.id}")
        self.broadcast({"type": "lode_created", "lode": lode})
        remove_backlog_item(self.backlog, item.id)
        self.broadcast({"type": "backlog_removed", "item": item.to_dict()})
        project_path = proj.path if proj else None
        self._gated_spawn(lode, project_path, foreground=False)
        return lode

    def _handle_lode_run_result(self, message: dict, conn: socket.socket | None) -> None:
        """Classify one outside-supervisor observation and acknowledge durability."""
        lode_id = message.get("lode_id")
        run_generation = message.get("run_generation")
        unit_name = message.get("unit_name")
        unit_result = message.get("unit_result")
        worker_returncode = message.get("worker_returncode")
        lode = self._find_lode(lode_id) if lode_id else None

        def acknowledge(accepted: bool, durable: bool, disposition: str) -> None:
            if conn:
                self._send_response(
                    conn,
                    {
                        "type": "lode_run_result_ack",
                        "accepted": accepted,
                        "durable": durable,
                        "disposition": disposition,
                    },
                )

        if not lode:
            acknowledge(False, True, "not-found")
            return
        if run_generation != lode.get("run_generation"):
            acknowledge(False, True, "stale-generation")
            return
        if lode.get("oom_scope") is None:
            acknowledge(False, True, "unguarded")
            return
        if unit_name != lode.get("oom_scope"):
            acknowledge(False, False, "unit-mismatch")
            return

        key = (lode_id, run_generation)
        if unit_result is None and worker_returncode != 0:
            self.runner_results[key] = (unit_result, worker_returncode)
            if lode_id not in self.lode_clients:
                self.pending_disconnects.setdefault(
                    key,
                    {
                        "deadline": time.monotonic() + GUARDED_DISCONNECT_HOLD_SEC,
                        "unit_name": unit_name,
                    },
                )
            acknowledge(True, False, "result-unavailable")
            return
        if unit_result == "oom-kill":
            self.pending_disconnects.pop(key, None)
            self.runner_results.pop(key, None)
            self._set_terminal_failure(lode, "oom")
            acknowledge(True, True, "oom")
            return

        if is_terminal_failure_kind(lode.get("failure_kind")):
            save_lodes(self.lodes)
            acknowledge(True, True, "already-terminal")
            return

        if _is_verified_ordinary_exit(unit_result, worker_returncode):
            durable = True
            if lode_id in self.lode_clients:
                self.runner_results[key] = (unit_result, worker_returncode)
                durable = False
            elif key in self.pending_disconnects:
                self.pending_disconnects.pop(key, None)
                self._finalize_lode_disconnect(lode_id, run_generation)
            else:
                self._set_terminal_failure(lode, "runner_exit_unverified")
            acknowledge(True, durable, "success")
            return

        self.pending_disconnects.pop(key, None)
        self.runner_results.pop(key, None)
        if lode.get("state") == "error" and not lode.get("failure_kind"):
            self._finalize_lode_disconnect(lode_id, run_generation)
            disposition = "runner-error"
        else:
            self._set_terminal_failure(lode, "runner_exit_unverified")
            disposition = "unverified"
        acknowledge(True, True, disposition)

    def _handle_mutation(self, message: dict, conn: socket.socket | None) -> None:
        """Handle a serialized state message. Runs on the event loop thread."""
        self._request_context.exchange_id = message.get("exchange_id")
        msg_type = message.get("type")
        ack_requested = message.get("ack_requested") is True

        def acknowledge_mutation(accepted: bool, reason: str) -> None:
            if conn and ack_requested:
                self._send_response(
                    conn,
                    {
                        "type": "mutation_ack",
                        "mutation_type": msg_type,
                        "lode_id": message.get("lode_id"),
                        "accepted": accepted,
                        "reason": reason,
                    },
                )

        if msg_type in RUNNER_MUTATION_TYPES:
            lode_id = message.get("lode_id")
            lode = self._find_lode(lode_id) if lode_id else None
            if not self._runner_generation_matches(lode, message):
                if msg_type == "lode_register" and conn and lode_id:
                    self._send_response(
                        conn,
                        {"type": "lode_register_refused", "lode_id": lode_id},
                    )
                if not lode:
                    reason = "lode_not_found"
                elif not message.get("run_generation"):
                    reason = "missing_run_generation"
                else:
                    reason = "stale_run_generation"
                acknowledge_mutation(False, reason)
                return
            if msg_type != "lode_register" and is_terminal_failure_kind(lode.get("failure_kind")):
                logger.info(
                    "Dropping runner mutation for terminal lode type=%s lode=%s",
                    msg_type,
                    lode_id,
                )
                acknowledge_mutation(False, "terminal_failure")
                return
            if msg_type not in {"lode_register", "lode_set_state"}:
                acknowledge_mutation(True, "accepted")

        if msg_type == "_client_disconnect":
            self._on_client_disconnect(conn)

        elif msg_type == "lode_snapshot":
            prefix = message.get("prefix")
            if not isinstance(prefix, str):
                if conn:
                    self._send_response(
                        conn,
                        {
                            "type": "error",
                            "error": "lode_snapshot requires a string prefix",
                        },
                    )
                return

            matches = find_lodes_by_prefix(self.lodes + self.archived_lodes, prefix)
            if not matches:
                response = {"type": "lode_snapshot", "result": "absent"}
            elif len(matches) == 1:
                response = {
                    "type": "lode_snapshot",
                    "result": "found",
                    "lode": dict(matches[0]),
                }
            else:
                response = {
                    "type": "lode_snapshot",
                    "result": "ambiguous",
                    "matches": [match["id"] for match in matches],
                }
            if conn:
                self._send_response(conn, response)

        elif msg_type == "lode_register":
            lode_id = message.get("lode_id")
            if lode_id:
                lode = self._find_lode(lode_id)
                if lode:
                    tmux_pane = message.get("tmux_pane")
                    pid = message.get("pid")
                    accepted = self._register_lode_client(
                        lode_id,
                        conn,
                        tmux_pane,
                        pid,
                        message.get("run_generation"),
                        message.get("armed_mode"),
                        message.get("actual_unit"),
                    )
                    self._send_response(
                        conn,
                        {
                            "type": "lode_registered" if accepted else "lode_register_refused",
                            "lode_id": lode_id,
                        },
                    )

        elif msg_type == "lode_run_result":
            self._handle_lode_run_result(message, conn)

        elif msg_type == "lode_create":
            project = message.get("project", "")
            scope = message.get("scope", "")
            proj = find_project(project)
            if proj and proj.disabled:
                logger.warning("Refusing to create lode for disabled project %s", project)
                if conn:
                    self._send_response(
                        conn,
                        {"type": "error", "error": disabled_project_message(proj)},
                    )
                return
            lode = create_lode(self.lodes, project, scope)
            backlog_data = message.get("backlog")
            if backlog_data:
                lode["backlog"] = backlog_data
                save_lodes(self.lodes)
            logger.info(f"Lode {lode['id']} created project={project}")
            self.broadcast({"type": "lode_created", "lode": lode})
            if conn:
                self._send_response(conn, {"type": "lode_created", "lode": lode})
            # Auto-spawn if requested
            if message.get("spawn"):
                project_path = proj.path if proj else None
                self._gated_spawn(lode, project_path, foreground=False)

        elif msg_type == "lode_set_stage":
            lode_id = message.get("lode_id")
            stage = message.get("stage")
            if lode_id and stage:
                lode = update_lode_stage(self.lodes, lode_id, stage)
                if lode:
                    logger.info(f"Lode {lode_id} stage={stage}")
                    self.broadcast({"type": "lode_updated", "lode": lode})
                    # Auto-promote oldest queued backlog item when a lode ships
                    if stage == "shipped":
                        lode_project = lode.get("project", "")
                        candidates = [
                            item
                            for item in self.backlog
                            if item.queued == lode_id and item.project == lode_project
                        ]
                        if candidates:
                            oldest = min(candidates, key=lambda x: x.created_at)
                            new_lode = self._promote_backlog_item(oldest)
                            # Re-queue remaining items behind the new lode
                            if new_lode:
                                remaining = [
                                    item for item in self.backlog if item.queued == lode_id
                                ]
                                for item in remaining:
                                    item.queued = new_lode["id"]
                                    self.broadcast(
                                        {"type": "backlog_updated", "item": item.to_dict()}
                                    )
                                if remaining:
                                    save_backlog(self.backlog)
                                    logger.info(
                                        "Re-queued %d backlog items behind %s",
                                        len(remaining),
                                        new_lode["id"],
                                    )

        elif msg_type == "lode_archive":
            lode_id = message.get("lode_id")
            if lode_id:
                lode = archive_lode(self.lodes, lode_id)
                if lode:
                    self.archived_lodes.append(lode)
                    logger.info(f"Lode {lode_id} archived")
                    self.broadcast({"type": "lode_archived", "lode": lode})

        elif msg_type == "lode_pause":
            lode_id = message.get("lode_id")
            lode = self._find_lode(lode_id) if lode_id else None
            if not lode:
                if conn:
                    self._send_response(
                        conn, {"type": "error", "error": f"lode {lode_id} not found"}
                    )
                return
            pid = lode.get("pid")
            pane = lode.get("tmux_pane")
            runner_pid = pid or (get_pane_pid(pane) if pane else None)
            if pane and runner_pid is None and pane_liveness(pane) is not Liveness.GONE:
                error = (
                    f"lode {lode_id} runner identity is unknown; pause refused because "
                    "tmux cannot prove the existing pane is gone"
                )
                logger.error(error)
                if conn:
                    self._send_response(conn, {"type": "error", "error": error})
                return
            lode["oom_scope"] = None
            generation = lode.get("run_generation")
            if generation:
                self.pending_disconnects.pop((lode_id, generation), None)
                self.runner_results.pop((lode_id, generation), None)
            save_lodes(self.lodes)
            if runner_pid and not _terminate_runner_process_group(runner_pid):
                error = (
                    f"lode {lode_id} runner did not exit; pause refused so resume cannot "
                    "collide with the existing stage session"
                )
                logger.error(error)
                if conn:
                    self._send_response(conn, {"type": "error", "error": error})
                return
            if pane:
                from hopper.tmux import kill_pane

                kill_pane(pane)
            lode["state"] = "paused"
            lode["status"] = "Paused by user; worktree retained"
            lode["failure_kind"] = None
            lode["active"] = False
            lode["tmux_pane"] = None
            lode["pid"] = None
            touch(lode)
            save_lodes(self.lodes)
            self.broadcast({"type": "lode_updated", "lode": lode})
            if conn:
                self._send_response(conn, {"type": "lode_paused", "lode": lode})

        elif msg_type == "lode_resume":
            lode_id = message.get("lode_id")
            lode = self._find_lode(lode_id) if lode_id else None
            if not lode:
                if conn:
                    self._send_response(
                        conn, {"type": "error", "error": f"lode {lode_id} not found"}
                    )
                return
            stage = lode.get("stage", "")
            if stage not in STAGES:
                if conn:
                    self._send_response(
                        conn,
                        {"type": "error", "error": f"lode {lode_id} stage {stage} cannot resume"},
                    )
                return
            project = find_project(lode.get("project", ""))
            if not project:
                if conn:
                    self._send_response(
                        conn,
                        {
                            "type": "error",
                            "error": f"project {lode.get('project', '')} not found",
                        },
                    )
                return
            outcome, pane_id = self._gated_spawn(
                lode,
                project.path,
                foreground=False,
                spawn_updates={"state": "running", "status": f"Resuming {stage}"},
                allow_terminal_recovery=True,
            )
            if outcome is not SpawnOutcome.SPAWNED:
                if conn:
                    if outcome is SpawnOutcome.ALREADY_LIVE:
                        error = (
                            f"lode {lode_id} already has a live runner; attach instead of spawning"
                        )
                    elif outcome is SpawnOutcome.REFUSED_UNKNOWN:
                        error = (
                            f"lode {lode_id} spawn refused because tmux is unreachable; "
                            "verify tmux is running, then retry"
                        )
                    else:
                        error = "failed to resume claude pane; verify tmux is running, then retry"
                    self._send_response(
                        conn,
                        {"type": "error", "error": error},
                    )
                return
            if conn:
                self._send_response(
                    conn, {"type": "lode_resumed", "lode": lode, "tmux_pane": pane_id}
                )

        elif msg_type == "lode_kill":
            lode_id = message.get("lode_id")
            if lode_id:
                lode = self._find_lode(lode_id)
                if lode:
                    lode["oom_scope"] = None
                    generation = lode.get("run_generation")
                    if generation:
                        self.pending_disconnects.pop((lode_id, generation), None)
                        self.runner_results.pop((lode_id, generation), None)
                    pid = lode.get("pid")
                    if pid:
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except (ProcessLookupError, PermissionError):
                            pass
                    tmux_pane = lode.get("tmux_pane")
                    if tmux_pane:
                        from hopper.tmux import kill_pane

                        kill_pane(tmux_pane)
                    lode["state"] = "error"
                    lode["status"] = "Killed by user"
                    lode["failure_kind"] = None
                    lode["active"] = False
                    lode["tmux_pane"] = None
                    lode["pid"] = None
                    touch(lode)
                    save_lodes(self.lodes)
                    logger.info(f"Lode {lode_id} killed by user")
                    self.broadcast({"type": "lode_updated", "lode": lode})
                    archived = archive_lode(self.lodes, lode_id)
                    if archived:
                        self.archived_lodes.append(archived)
                        logger.info(f"Lode {lode_id} archived after kill")
                        self.broadcast({"type": "lode_archived", "lode": archived})

        elif msg_type == "lode_unarchive":
            lode_id = message.get("lode_id")
            if lode_id:
                lode = unarchive_lode(self.archived_lodes, self.lodes, lode_id)
                if lode:
                    logger.info(f"Lode {lode_id} unarchived")
                    self.broadcast({"type": "lode_unarchived", "lode": lode})
                    if message.get("spawn"):
                        proj = find_project(lode.get("project", ""))
                        project_path = proj.path if proj else None
                        self._gated_spawn(
                            lode,
                            project_path,
                            foreground=message.get("foreground", False),
                        )

        elif msg_type == "lode_spawn":
            lode_id = message.get("lode_id")
            lode = self._find_lode(lode_id) if lode_id else None
            if lode:
                proj = find_project(lode.get("project", ""))
                project_path = proj.path if proj else None
                self._gated_spawn(
                    lode,
                    project_path,
                    foreground=message.get("foreground", False),
                )

        elif msg_type == "lode_set_state":
            lode_id = message.get("lode_id")
            state = message.get("state")
            status = message.get("status", "")
            if lode_id and state:
                lode = self._find_lode(lode_id)
                message_epoch = message.get("gate_epoch")
                current_epoch = lode.get("gate_epoch", 0) if lode else 0
                if lode and "gate_epoch" in message and message_epoch != current_epoch:
                    logger.info(
                        "Dropping stale state update lode=%s gate_epoch=%s current_gate_epoch=%s",
                        lode_id,
                        message_epoch,
                        current_epoch,
                    )
                    acknowledge_mutation(False, "stale_gate_epoch")
                    return
                lode = update_lode_state(self.lodes, lode_id, state, status)
                if lode:
                    logger.info(f"Lode {lode_id} state={state} status={status}")
                    self.broadcast({"type": "lode_updated", "lode": lode})
                    acknowledge_mutation(True, "accepted")
                else:
                    acknowledge_mutation(False, "lode_not_found")
            else:
                acknowledge_mutation(False, "invalid_mutation")

        elif msg_type == "lode_set_progress":
            lode_id = message.get("lode_id")
            if lode_id:
                lode = self._find_lode(lode_id)
                if lode:
                    state = lode.get("state", "new")
                    if state in PROGRESS_REJECT_STATES:
                        logger.debug(
                            f"Ignoring progress heartbeat for lode {lode_id} in state={state}"
                        )
                        return
                    summary = message.get("summary", "")
                    lode["last_progress_at"] = current_time_ms()
                    lode["last_progress_summary"] = (summary or "")[:120]
                    touch(lode)
                    save_lodes(self.lodes)
                    self.broadcast({"type": "lode_updated", "lode": lode})
                    logger.info(f"Lode {lode_id} progress: {lode['last_progress_summary']}")

        elif msg_type == "lode_set_pane_activity":
            lode_id = message.get("lode_id")
            observed_at = message.get("observed_at")
            if lode_id and isinstance(observed_at, int) and not isinstance(observed_at, bool):
                lode = self._find_lode(lode_id)
                if lode:
                    lode["last_pane_activity_at"] = observed_at
                    touch(lode)
                    save_lodes(self.lodes)
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

        elif msg_type == "lode_set_branch":
            lode_id = message.get("lode_id")
            branch = message.get("branch", "")
            if lode_id:
                lode = update_lode_branch(self.lodes, lode_id, branch)
                if lode:
                    logger.info(f"Lode {lode_id} branch={branch}")
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
                lode = self._find_lode(lode_id)
                if not lode or claude_stage not in lode.get("claude", {}):
                    acknowledge_mutation(
                        False,
                        "lode_not_found" if not lode else "invalid_stage",
                    )
                    return
                if message.get("spawn"):
                    force = message.get("force") is True
                    generation = lode.get("run_generation")
                    if force and generation and (lode_id, generation) in self.pending_disconnects:
                        acknowledge_mutation(False, "pending_runner_result")
                        return
                    if force:
                        pane = lode.get("tmux_pane")
                        runner_pid = lode.get("pid")
                        process_group = None
                        if pane:
                            liveness = pane_liveness(pane)
                            if liveness is Liveness.ALIVE:
                                pane_pid = get_pane_pid(pane)
                                if runner_pid is None or pane_pid is None:
                                    acknowledge_mutation(False, "runner_identity_unverified")
                                    return
                                process_group = _corroborated_runner_process_group(
                                    runner_pid, pane_pid
                                )
                                if process_group is None:
                                    acknowledge_mutation(False, "runner_identity_unverified")
                                    return
                            elif (
                                liveness is Liveness.UNKNOWN
                                or runner_pid is not None
                                or lode.get("active")
                            ):
                                acknowledge_mutation(False, "runner_identity_unverified")
                                return
                        elif runner_pid is not None or lode.get("active"):
                            acknowledge_mutation(False, "runner_identity_unverified")
                            return
                        if runner_pid and not _terminate_runner_process_group(
                            runner_pid, process_group=process_group
                        ):
                            acknowledge_mutation(False, "termination_failed")
                            return
                        if runner_pid:
                            runner_exited = _runner_process_exited(runner_pid)
                            pane_gone = not pane or pane_liveness(pane) is Liveness.GONE
                            if not runner_exited or not pane_gone:
                                acknowledge_mutation(False, "termination_failed")
                                return
                        if generation:
                            self.runner_results.pop((lode_id, generation), None)
                        lode["active"] = False
                        lode["tmux_pane"] = None
                        lode["pid"] = None
                    proj = find_project(lode.get("project", ""))
                    project_path = proj.path if proj else None

                    def reset_before_spawn() -> None:
                        reset_lode_claude_stage(
                            self.lodes,
                            lode_id,
                            claude_stage,
                            persist=False,
                        )

                    outcome, _ = self._gated_spawn(
                        lode,
                        project_path,
                        pre_spawn=reset_before_spawn,
                        allow_terminal_recovery=True,
                    )
                    if outcome is SpawnOutcome.SPAWNED:
                        logger.info(f"Lode {lode_id} claude_reset stage={claude_stage}")
                        acknowledge_mutation(True, "spawned")
                    elif outcome is SpawnOutcome.ALREADY_LIVE:
                        acknowledge_mutation(False, "already_live")
                    elif outcome is SpawnOutcome.REFUSED_UNKNOWN:
                        acknowledge_mutation(False, "tmux_unreachable")
                    else:
                        acknowledge_mutation(False, "spawn_failed")
                else:
                    reset_lode_claude_stage(self.lodes, lode_id, claude_stage)
                    logger.info(f"Lode {lode_id} claude_reset stage={claude_stage}")
                    self.broadcast({"type": "lode_updated", "lode": lode})
                    acknowledge_mutation(True, "reset")

        elif msg_type == "lode_resume_refine":
            # Compound: apply refine state only when the gated spawn is allowed.
            lode_id = message.get("lode_id")
            if lode_id:
                lode = self._find_lode(lode_id)
                if lode:
                    proj = find_project(lode.get("project", ""))
                    project_path = proj.path if proj else None
                    outcome, _ = self._gated_spawn(
                        lode,
                        project_path,
                        foreground=False,
                        spawn_updates={
                            "stage": "refine",
                            "state": "running",
                            "status": "Resuming refine",
                        },
                    )
                    if outcome is SpawnOutcome.SPAWNED:
                        logger.info(f"Lode {lode_id} resumed refine")

        elif msg_type == "lode_send_feedback":
            lode_id = message.get("lode_id")
            text = message.get("text", "")
            if not lode_id:
                if conn:
                    self._send_response(
                        conn,
                        {
                            "type": "error",
                            "error": (
                                "No lode ID was provided. No pane was touched. Supply a lode "
                                "ID, then retry."
                            ),
                            "outcome": "unknown_lode",
                        },
                    )
                return

            lode = self._find_lode(lode_id)
            if not lode:
                if conn:
                    self._send_response(
                        conn,
                        {
                            "type": "error",
                            "error": (
                                f"Lode {lode_id} was not found on this server. No pane was "
                                "touched. Check the lode ID, then retry."
                            ),
                            "outcome": "unknown_lode",
                        },
                    )
                return

            pane_id = lode.get("tmux_pane")
            result = _deliver_lode_pane_input(self.lodes, lode, text, paste=True)
            reason = result["reason"]
            accepted = reason in _ACCEPTED_DELIVERY_REASONS
            paste_attempted = reason not in _PRE_PASTE_REASONS
            if paste_attempted:
                lode["gate_epoch"] = lode.get("gate_epoch", 0) + 1

            if accepted:
                state = "running"
                status = "Feedback accepted"
            else:
                outcome = _DELIVERY_FAILURE_OUTCOMES[reason]
                status = _GATE_FEEDBACK_STATUSES[outcome]
                message_template = _GATE_FEEDBACK_MESSAGES[reason]
                state = "gated"
            updated = update_lode_state(self.lodes, lode_id, state, status)
            if updated:
                self.broadcast({"type": "lode_updated", "lode": updated})
            if conn:
                if accepted:
                    response = {
                        "type": "feedback_sent",
                        "lode_id": lode_id,
                        "tmux_pane": pane_id,
                    }
                else:
                    response = {
                        "type": "error",
                        "error": message_template.format(
                            pane=pane_id or "<unknown>",
                            lode_id=lode_id,
                            title=_render_observed_title(result["title"]),
                        ),
                        "outcome": outcome,
                    }
                    if result["capture"] is not None:
                        response["tail"] = "\n".join(result["capture"].splitlines()[-10:])
                self._send_response(conn, response)

        elif msg_type == "lode_send_pane_input":
            lode_id = message.get("lode_id")
            text = message.get("text", "")
            paste = message.get("paste")
            if not lode_id:
                if conn:
                    self._send_response(
                        conn,
                        {
                            "type": "error",
                            "error": (
                                "No lode ID was provided. No pane was touched. Supply a lode "
                                "ID, then retry."
                            ),
                            "outcome": "unknown_lode",
                        },
                    )
                return
            if not isinstance(paste, bool):
                if conn:
                    self._send_response(
                        conn,
                        {
                            "type": "error",
                            "error": (
                                "No pane input method was provided. No pane was touched. Retry "
                                "with `hop lode nudge` or `hop lode answer`."
                            ),
                            "outcome": "invalid_request",
                        },
                    )
                return

            lode = self._find_lode(lode_id)
            if not lode:
                if conn:
                    self._send_response(
                        conn,
                        {
                            "type": "error",
                            "error": (
                                f"Lode {lode_id} was not found on this server. No pane was "
                                "touched. Check the lode ID, then retry."
                            ),
                            "outcome": "unknown_lode",
                        },
                    )
                return

            pane_id = lode.get("tmux_pane")
            result = _deliver_lode_pane_input(self.lodes, lode, text, paste=paste)
            reason = result["reason"]
            if reason in _ACCEPTED_DELIVERY_REASONS:
                response = {
                    "type": "pane_input_sent",
                    "lode_id": lode_id,
                    "tmux_pane": pane_id,
                }
            else:
                response = {
                    "type": "error",
                    "error": _PANE_INPUT_MESSAGES[reason].format(
                        pane=pane_id or "<unknown>",
                        lode_id=lode_id,
                        title=_render_observed_title(result["title"]),
                    ),
                    "outcome": _DELIVERY_FAILURE_OUTCOMES[reason],
                }
                if result["capture"] is not None:
                    response["tail"] = "\n".join(result["capture"].splitlines()[-10:])
            if conn:
                self._send_response(conn, response)

        elif msg_type == "lode_promote_backlog":
            # Compound: create lode from backlog item, remove backlog item
            item_id = message.get("item_id", "")
            scope = message.get("scope", "")
            item = find_backlog_by_prefix(self.backlog, item_id)
            if not item:
                if conn:
                    self._send_response(
                        conn,
                        {"type": "promote_error", "error": f"Backlog item '{item_id}' not found"},
                    )
            else:
                try:
                    lode = self._promote_backlog_item(item, scope)
                    if lode and conn:
                        self._send_response(conn, {"type": "lode_promoted", "lode": lode})
                    elif conn:
                        proj = find_project(item.project)
                        self._send_response(
                            conn,
                            {"type": "promote_error", "error": disabled_project_message(proj)},
                        )
                except Exception:
                    logger.exception(f"Promote failed for backlog item {item_id}")
                    if conn:
                        self._send_response(
                            conn, {"type": "promote_error", "error": "Promote failed unexpectedly"}
                        )

        elif msg_type == "backlog_add":
            project = message.get("project", "")
            description = message.get("description", "")
            lode_id = message.get("lode_id")
            if project and description:
                item = add_backlog_item(self.backlog, project, description, lode_id)
                logger.info(f"Backlog {item.id} added project={project}")
                self.broadcast({"type": "backlog_added", "item": item.to_dict()})

        elif msg_type == "backlog_update":
            item_id = message.get("item_id", "")
            description = message.get("description", "")
            if item_id and description:
                update_backlog_item(self.backlog, item_id, description)
                logger.info(f"Backlog {item_id} updated")

        elif msg_type == "backlog_set_queued":
            item_id = message.get("item_id", "")
            queued = message.get("queued")
            if item_id:
                item = set_backlog_queued(self.backlog, item_id, queued)
                if item:
                    logger.info(f"Backlog {item_id} queued={queued}")
                    self.broadcast({"type": "backlog_updated", "item": item.to_dict()})

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
            self.lodes = load_lodes()
            self.archived_lodes = load_archived_lodes()
            self.backlog = load_backlog()
            logger.info("Projects and lodes reloaded from disk")

        else:
            logger.warning(f"Unknown message type: {msg_type}")

    def _send_response(self, conn: socket.socket, message: dict) -> None:
        """Send a response directly to a client."""
        if "ts" not in message:
            message["ts"] = current_time_ms()
        exchange_id = getattr(self._request_context, "exchange_id", None)
        if exchange_id is not None:
            message["exchange_id"] = exchange_id
        response = json.dumps(message) + "\n"
        with self.lock:
            write_lock = self.write_locks.get(conn)
        if write_lock is None:
            logger.debug("Failed to send response: client disconnected")
            return
        try:
            with write_lock:
                conn.sendall(response.encode("utf-8"))
        except Exception as e:
            logger.debug(f"Failed to send response: {e}")

    def _event_loop(self) -> None:
        """Dedicated thread that serializes state mutations and snapshot reads.

        Dequeues (message, conn) pairs and processes them one at a time,
        ensuring no concurrent access to serialized lode/backlog state or save_lodes.
        """
        while not self.stop_event.is_set():
            self._drain_due_disconnects()
            try:
                message, conn = self.event_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                self._handle_mutation(message, conn)
            except Exception:
                logger.exception(f"Event loop error: {message.get('type')}")
            finally:
                self._drain_due_disconnects()

    def _enqueue_event(self, message: dict, conn: socket.socket | None = None) -> None:
        """Enqueue a mutation event for the event loop thread."""
        try:
            self.event_queue.put_nowait((message, conn))
        except queue.Full:
            logger.warning(f"Event queue full, dropping: {message.get('type')}")

    def enqueue(self, message: dict) -> None:
        """Public API for in-process callers (TUI) to submit mutations."""
        self._enqueue_event(message)

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
        message.pop("exchange_id", None)
        if "ts" not in message:
            message["ts"] = current_time_ms()

        data = (json.dumps(message) + "\n").encode("utf-8")

        with self.lock:
            clients_to_send = [(client, self.write_locks[client]) for client in self.clients]

        dead_clients = []
        for client, write_lock in clients_to_send:
            try:
                with write_lock:
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
                    self.write_locks.pop(client, None)
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
            self.write_locks.clear()

        # Signal threads to stop
        self.stop_event.set()

        # Close server socket to unblock accept()
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass

        # Wait for threads
        if self.event_thread and self.event_thread.is_alive():
            self.event_thread.join(timeout=1.0)
        if self.writer_thread and self.writer_thread.is_alive():
            self.writer_thread.join(timeout=1.0)

        self._unlink_owned_socket()

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
        server._unlink_owned_socket()

    atexit.register(cleanup_socket)

    # Start server in background thread
    def start_server():
        try:
            server.start()
        except Exception as error:
            server.startup_error = error
        finally:
            server.ready.set()

    server_thread = threading.Thread(target=start_server, name="server", daemon=True)
    server_thread.start()

    if not server.ready.wait(5.0):
        print("Server failed to start")
        server.stop()
        server_thread.join(timeout=2.0)
        atexit.unregister(cleanup_socket)
        return 1

    if server.startup_error is not None:
        if isinstance(server.startup_error, ServerLockHeld):
            print(server.startup_error)
        else:
            print(f"Server failed to start: {server.startup_error}")
        server.stop()
        server_thread.join(timeout=2.0)
        atexit.unregister(cleanup_socket)
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
