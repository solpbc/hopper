# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Unix socket JSONL server for hopper."""

import atexit
import base64
import binascii
import copy
import fcntl
import hashlib
import json
import logging
import os
import queue
import secrets
import signal
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from enum import Enum
from pathlib import Path

from hopper import completion, config, git, oom, teardown
from hopper.backlog import (
    BacklogItem,
    add_backlog_item,
    apply_completion_disposition,
    load_backlog,
    remove_backlog_item,
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
    archive_lode_for_action,
    create_lode,
    current_time_ms,
    find_lodes_by_prefix,
    format_terminal_failure_status,
    get_worktree_dir,
    is_terminal_failure_kind,
    load_archived_lodes,
    load_lodes,
    reserve_lode_id,
    reset_lode_claude_stage,
    save_archived_lodes,
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
    completion_action_panes,
    get_pane_pid,
    pane_identity,
    pane_liveness,
    pane_needs_answer,
    pane_title,
    paste_buffer,
    read_pane_input,
    send_keys,
)

logger = logging.getLogger(__name__)

PROGRESS_REJECT_STATES = frozenset({"new", "gated", "ready", "completed", "teardown", "error"})
PENDING_ACTION_FENCED_MUTATIONS = frozenset(
    {"lode_pause", "lode_resume", "lode_kill", "lode_archive", "lode_spawn"}
)
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


def _pending_completion_exists(lode_id: str) -> bool:
    """Return whether a schema-addressable lode has a durable completion fence."""
    try:
        return completion.pending_completion_path(lode_id).exists()
    except ValueError:
        # Legacy/corrupt lode IDs cannot name a valid pending-completion record.
        return False


def _capture_ship_acceptance(
    lode: dict,
    project_path: str,
    action_id: str,
) -> dict:
    """Capture immutable ship provenance without mutating Git state."""
    worktree = get_worktree_dir(lode["id"]).resolve(strict=True)
    provenance = git.capture_worktree_provenance(project_path, worktree)
    quarantine = worktree.parent / f".{lode['id']}-quarantine-{action_id}"
    return {
        "provenance": provenance,
        "landing": {"cause": None, "base_ref": None, "detail": None, "accepted": False},
        "backlog": {
            "planned": False,
            "selected_item_id": None,
            "promoted_lode_id": None,
            "remaining_item_ids": [],
            "applied": False,
        },
        "archive_published": False,
        "quarantine": {
            "original_path": str(worktree),
            "quarantine_path": str(quarantine),
            "expected_identity": provenance["worktree"]["identity"],
            "registration_repaired": False,
            "removal_outcome": "pending",
            "branch_outcome": "pending",
        },
        "cleanup_failure": None,
    }


def _reported_process_identity(message: dict, *, platform: str, boot_id: str | None) -> dict:
    """Read and verify the process identity reported by one launch process."""
    pid = message.get("pid")
    ppid = message.get("ppid")
    pgid = message.get("pgid")
    if (
        any(isinstance(value, bool) or not isinstance(value, int) for value in (pid, ppid, pgid))
        or pid < 1
        or ppid < 0
        or pgid < 1
    ):
        raise ValueError("reported process identity is invalid")
    kwargs = {"boot_id": boot_id} if platform == "linux" else {}
    observed = teardown.read_process_identity(pid, platform=platform, **kwargs)
    if observed["state"] != "alive":
        raise RuntimeError(observed["error"] or "reported process is not alive")
    identity = observed["identity"]
    if identity["ppid"] != ppid or identity["pgid"] != pgid:
        raise ValueError("reported process identity does not match the server observation")
    return identity


def _capture_supervisor_registration(lode: dict, message: dict) -> dict:
    """Capture the outside supervisor before it launches the stage worker."""
    pane_id = message.get("tmux_pane")
    if not isinstance(pane_id, str) or pane_id != lode.get("tmux_pane"):
        raise ValueError("outside supervisor pane does not match the current lode pane")
    pane = pane_identity(pane_id)
    if pane is None:
        raise RuntimeError("tmux pane identity is unavailable")
    platform = teardown.platform_name()
    proof_mode = message.get("proof_mode")
    expected_modes = {
        "linux": {"linux-strict", "linux-degraded"},
        "darwin": {"darwin-bounded"},
        "other": {"other-bounded-no-birth"},
    }[platform]
    if proof_mode not in expected_modes:
        raise ValueError("outside supervisor proof mode does not match this platform")
    degraded_reason = message.get("degraded_reason")
    unit_name = message.get("unit_name")
    if proof_mode == "linux-strict":
        if degraded_reason is not None or not unit_name or unit_name != lode.get("oom_scope"):
            raise ValueError("strict Linux ownership does not match the generation scope")
    elif not isinstance(degraded_reason, str) or not degraded_reason or unit_name is not None:
        raise ValueError("degraded ownership requires a reason and cannot claim a unit")

    if platform == "linux":
        boot_id = teardown.read_boot_id()
    else:
        boot_id = teardown.read_host_boot_identity(platform=platform)
    if not boot_id:
        raise RuntimeError("host boot identity is unavailable")
    process_boot_id = boot_id if platform == "linux" else None
    pane_root_observed = teardown.read_process_identity(
        pane["pane_pid"],
        platform=platform,
        **({"boot_id": process_boot_id} if platform == "linux" else {}),
    )
    if pane_root_observed["state"] != "alive":
        raise RuntimeError("tmux pane root identity is unavailable")
    supervisor = _reported_process_identity(
        message,
        platform=platform,
        boot_id=process_boot_id,
    )
    return {
        "schema_version": completion.SCHEMA_VERSION,
        "lode_id": lode["id"],
        "run_generation": lode["run_generation"],
        "registered_at_ms": completion.accepted_at_ms(),
        "boot_id": boot_id,
        "platform": platform,
        "proof_mode": proof_mode,
        "degraded_reason": degraded_reason,
        "pane": {
            "pane_id": pane["pane_id"],
            "window_id": pane["window_id"],
            "root_process": pane_root_observed["identity"],
        },
        "supervisor": supervisor,
        "worker": None,
        "process_group": supervisor["pgid"],
        "descendants": [],
        "unit": None,
        "cgroup": None,
        "unit_name": unit_name,
    }


def _capture_worker_registration(source: dict, message: dict) -> dict:
    """Complete and verify the durable launch ownership record."""
    proof_mode = source["proof_mode"]
    expected_armed = {
        "linux-strict": {oom.OomCapability.SUPPORTED.value},
        "linux-degraded": {
            oom.OomCapability.DEGRADED_NO_CONTROLLER.value,
            oom.OomCapability.DEGRADED_NO_SCORE.value,
        },
        "darwin-bounded": {oom.OomCapability.NON_LINUX.value},
        "other-bounded-no-birth": {oom.OomCapability.NON_LINUX.value},
    }[proof_mode]
    if message.get("armed_mode") not in expected_armed:
        raise ValueError("worker OOM mode does not match supervisor ownership mode")
    actual_unit = message.get("actual_unit")
    if proof_mode == "linux-strict":
        if actual_unit != source["unit_name"]:
            raise ValueError("worker did not enter the recorded systemd unit")
        systemctl = oom.find_systemctl()
        if not systemctl:
            raise RuntimeError("systemctl is unavailable while capturing strict ownership")
    else:
        if actual_unit is not None:
            raise ValueError("degraded worker cannot claim a systemd unit")
        systemctl = None

    captured = teardown.capture_ownership(
        pane_id=source["pane"]["pane_id"],
        supervisor_pid=source["supervisor"]["pid"],
        worker_pid=message.get("pid"),
        process_group=source["process_group"],
        unit_name=source["unit_name"],
        systemctl=systemctl,
        platform=source["platform"],
    )
    if captured["state"] != "captured":
        raise RuntimeError(captured["error"] or "generation ownership is unavailable")
    ownership = captured["ownership"]
    if ownership["proof_mode"] != proof_mode:
        raise ValueError("worker capture changed the supervisor proof mode")
    for key in ("pane", "supervisor", "process_group"):
        if ownership[key] != source[key]:
            raise ValueError(f"worker capture changed immutable {key} ownership")
    worker = ownership["worker"]
    for key in ("pid", "ppid", "pgid"):
        if message.get(key) != worker[key]:
            raise ValueError(f"reported worker {key} does not match server observation")
    if proof_mode != "linux-strict" and worker != source["supervisor"]:
        raise ValueError("bounded supervisor and worker must be the same process")

    record = {
        **source,
        "worker": worker,
        "descendants": ownership["descendants"],
        "unit": ownership["unit"],
        "cgroup": ownership["cgroup"],
    }
    completion.validate_run_ownership(record, require_worker=True)
    pidfd = None
    if proof_mode == "linux-strict":
        pidfd_interface = teardown.resolve_pidfd_interface()
        if pidfd_interface is not None:
            reopened = teardown.reopen_supervisor_pidfd(
                record["supervisor"], pidfd_interface=pidfd_interface
            )
            if reopened["state"] != "alive":
                raise RuntimeError(
                    reopened["error"] or "outside supervisor exited during registration"
                )
            pidfd = reopened["fd"]
    return {"record": record, "pidfd": pidfd}


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


def _log_state_change(lode_id: str, state: str, status: str, via: str) -> None:
    """Log every state mutation in one shape, whoever made it.

    `state` is the only field readers treat as current truth, and it is really
    just the last thing anything wrote. A mutation that leaves no log line is
    unreconstructable afterwards: `lode_send_feedback` wrote `running` over a
    park and the eight hours that followed had no record of why the lode looked
    alive. Every writer logs, and `via` says which one.
    """
    logger.info(f"Lode {lode_id} state={state} status={status} via={via}")


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
        self.completion_threads: dict[tuple[str, str], threading.Thread] = {}
        self.registration_threads: dict[str, threading.Thread] = {}
        self.completion_acceptances: dict[str, str] = {}
        self.supervisor_pidfds: dict[tuple[str, str], int] = {}
        self._startup_completion_actions: list[str] = []
        self._log_handler: logging.FileHandler | None = None
        self._lock_file = None
        self._socket_bound = False
        self.ready = threading.Event()
        self.startup_error: Exception | None = None

    def _find_lode(self, lode_id: str) -> dict | None:
        """Find a lode by ID."""
        return next((lode for lode in self.lodes if lode["id"] == lode_id), None)

    def _find_completion_lode(self, lode_id: str) -> dict | None:
        """Find the active or archived object owned by a pending action."""
        return self._find_lode(lode_id) or next(
            (lode for lode in self.archived_lodes if lode["id"] == lode_id), None
        )

    @staticmethod
    def _completion_spawn_target_id(record: dict) -> str | None:
        if record["stage"] != "ship":
            return record["lode_id"]
        return record["ship"]["backlog"]["promoted_lode_id"]

    def _pending_spawn_for(self, lode_id: str, generation: str) -> dict | None:
        """Find the sole pending action whose receipt owns this new runner."""
        matches = []
        seen = set()
        for source in [*self.lodes, *self.archived_lodes]:
            source_id = source["id"]
            if source_id in seen or not _pending_completion_exists(source_id):
                continue
            seen.add(source_id)
            try:
                record = self._load_pending(source_id)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if (
                record
                and record["spawn"] is not None
                and record["markers"]["spawn"]["state"] == "intent"
                and self._completion_spawn_target_id(record) == lode_id
                and record["spawn"]["target_generation"] == generation
            ):
                matches.append(record)
        return matches[0] if len(matches) == 1 else None

    def _adopt_completion_spawn_receipt(self, lode: dict, message: dict) -> bool:
        """Adopt only the pane named by the action's fsynced bootstrap receipt."""
        generation = message.get("run_generation")
        if not isinstance(generation, str):
            return False
        record = self._pending_spawn_for(lode["id"], generation)
        if record is None:
            return True
        try:
            receipt = completion.load_spawn_receipt(record["lode_id"], record["action_id"])
        except (OSError, ValueError, json.JSONDecodeError) as error:
            logger.error("Spawn receipt is invalid lode=%s: %s", record["lode_id"], error)
            return False
        pane_id = message.get("tmux_pane")
        if not isinstance(pane_id, str) or not pane_id:
            return False
        expected = {
            "action_id": record["action_id"],
            "source_lode_id": record["lode_id"],
            "target_lode_id": lode["id"],
            "target_generation": generation,
            "pane_id": pane_id,
        }
        if receipt is None or any(receipt.get(key) != value for key, value in expected.items()):
            return False
        recorded_pane = record["spawn"]["pane_id"]
        if recorded_pane not in {None, pane_id} or lode.get("tmux_pane") not in {None, pane_id}:
            return False
        record["spawn"]["pane_id"] = pane_id
        lode["tmux_pane"] = pane_id
        touch(lode)
        save_lodes(self.lodes)
        _log_state_change(
            lode["id"],
            lode.get("state", "teardown"),
            lode.get("status", ""),
            "completion_spawn_receipt_adoption",
        )
        self._persist_completion(record, via="completion_spawn:receipt_adopted")
        return True

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

    def _load_pending(self, lode_id: str) -> dict | None:
        """Load the canonical teardown fence, propagating malformed records."""
        return completion.load_pending_completion(lode_id)

    def _generation_is_fenced(self, lode_id: str, run_generation: str | None) -> bool:
        """Fail closed when a pending record exists for a generation."""
        if not _pending_completion_exists(lode_id):
            return False
        try:
            record = self._load_pending(lode_id)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            logger.error("Invalid completion fence lode=%s: %s", lode_id, error)
            return True
        return bool(record and record["run_generation"] == run_generation)

    def _project_completion(
        self,
        record: dict,
        *,
        via: str,
        active: bool | None = None,
    ) -> None:
        """Persist the sole pending record's display projection."""
        lode = self._find_completion_lode(record["lode_id"])
        if not lode:
            return
        lode["state"] = "teardown"
        lode["status"] = completion.completion_status(record)
        if active is not None:
            lode["active"] = active
        touch(lode)
        if any(item is lode for item in self.lodes):
            save_lodes(self.lodes)
        else:
            save_archived_lodes(self.archived_lodes)
        _log_state_change(lode["id"], lode["state"], lode["status"], via)
        self.broadcast({"type": "lode_updated", "lode": lode})

    def _cancel_generation_guard(self, lode_id: str, run_generation: str) -> None:
        key = (lode_id, run_generation)
        self.pending_disconnects.pop(key, None)
        self.runner_results.pop(key, None)

    def _start_registration_capture(
        self,
        kind: str,
        lode: dict,
        message: dict,
        conn: socket.socket | None,
    ) -> None:
        """Run launch identity inspection outside the serialized event loop."""
        generation = lode["run_generation"]
        key = f"{kind}:{lode['id']}:{generation}"
        if key in self.registration_threads:
            if conn:
                self._send_response(
                    conn,
                    {
                        "type": f"lode_{kind}_register_refused",
                        "lode_id": lode["id"],
                        "reason": "registration_in_progress",
                    },
                )
            return
        snapshot = copy.deepcopy(lode)
        request = copy.deepcopy(message)

        def capture() -> None:
            try:
                if kind == "supervisor":
                    result = {"record": _capture_supervisor_registration(snapshot, request)}
                else:
                    source = completion.load_run_ownership(
                        snapshot["id"], generation, require_worker=False
                    )
                    if source is None:
                        raise RuntimeError("outside supervisor ownership is not registered")
                    result = _capture_worker_registration(source, request)
                payload = {"ok": True, **result}
            except Exception as error:
                payload = {"ok": False, "error": str(error)}
            self._enqueue_event(
                {
                    "type": "_registration_capture_result",
                    "exchange_id": request.get("exchange_id"),
                    "kind": kind,
                    "key": key,
                    "lode_id": snapshot["id"],
                    "run_generation": generation,
                    "request": request,
                    "result": payload,
                },
                conn,
            )

        thread = threading.Thread(target=capture, name=f"hopper-{kind}-capture", daemon=True)
        self.registration_threads[key] = thread
        thread.start()

    def _handle_registration_capture_result(
        self, message: dict, conn: socket.socket | None
    ) -> None:
        """Commit a launch registration only if its generation is still current."""
        key = message.get("key")
        if isinstance(key, str):
            self.registration_threads.pop(key, None)
        lode_id = message.get("lode_id")
        generation = message.get("run_generation")
        kind = message.get("kind")
        lode = self._find_lode(lode_id) if isinstance(lode_id, str) else None
        result = message.get("result", {})
        response_type = "lode_registered" if kind == "worker" else "lode_supervisor_registered"
        refused_type = (
            "lode_register_refused" if kind == "worker" else "lode_supervisor_register_refused"
        )
        with self.lock:
            connection_live = conn is not None and conn in self.clients and conn in self.write_locks
        if (
            not lode
            or lode.get("run_generation") != generation
            or self._generation_is_fenced(lode_id, generation)
            or not connection_live
            or result.get("ok") is not True
        ):
            fd = result.get("pidfd")
            if isinstance(fd, int):
                os.close(fd)
            reason = result.get("error") or "stale_or_fenced_generation"
            logger.error("%s registration refused lode=%s: %s", kind, lode_id, reason)
            if conn:
                self._send_response(
                    conn,
                    {"type": refused_type, "lode_id": lode_id, "accepted": False, "reason": reason},
                )
            return
        try:
            completion.write_run_ownership(result["record"])
        except Exception as error:
            fd = result.get("pidfd")
            if isinstance(fd, int):
                os.close(fd)
            logger.error("%s ownership persistence failed lode=%s: %s", kind, lode_id, error)
            if conn:
                self._send_response(
                    conn,
                    {
                        "type": refused_type,
                        "lode_id": lode_id,
                        "accepted": False,
                        "reason": f"ownership persistence failed: {error}",
                    },
                )
            return

        if kind == "worker":
            pidfd = result.get("pidfd")
            if isinstance(pidfd, int):
                old = self.supervisor_pidfds.pop((lode_id, generation), None)
                if old is not None:
                    os.close(old)
                self.supervisor_pidfds[(lode_id, generation)] = pidfd
            request = message.get("request", {})
            accepted = bool(
                conn
                and self._register_lode_client(
                    lode_id,
                    conn,
                    result["record"]["pane"]["pane_id"],
                    result["record"]["worker"]["pid"],
                    generation,
                    request.get("armed_mode"),
                    request.get("actual_unit"),
                )
            )
            if not accepted:
                fd = self.supervisor_pidfds.pop((lode_id, generation), None)
                if fd is not None:
                    os.close(fd)
                if conn:
                    self._send_response(
                        conn,
                        {"type": refused_type, "lode_id": lode_id, "accepted": False},
                    )
                return
        elif result["record"]["proof_mode"] == "linux-degraded":
            lode["oom_scope"] = None
            touch(lode)
            save_lodes(self.lodes)
            status = result["record"]["degraded_reason"]
            _log_state_change(lode_id, lode.get("state", ""), status, "supervisor_capture_degraded")
            self.broadcast({"type": "lode_updated", "lode": lode})

        self._record_completion_spawn_adoption(lode_id, generation, kind)

        if conn:
            self._send_response(
                conn,
                {"type": response_type, "lode_id": lode_id, "accepted": True},
            )

    def _record_completion_spawn_adoption(self, lode_id: str, generation: str, kind: str) -> None:
        """Persist supervisor/worker adoption for an action-scoped spawn."""
        record = self._pending_spawn_for(lode_id, generation)
        if record is None:
            return
        field = "supervisor_adopted" if kind == "supervisor" else "worker_adopted"
        record["spawn"][field] = True
        marker = record["markers"]["spawn"]
        if record["spawn"]["supervisor_adopted"] and record["spawn"]["worker_adopted"]:
            completion.transition_marker(
                record, "spawn", "done", attempt_id=marker["attempt_id"], detail="runner adopted"
            )
            self._persist_completion(record, via="completion_result:spawn_adopted")
            self._continue_completion(record)
        else:
            self._persist_completion(record, via=f"completion_spawn:{kind}_adopted")

    def _send_completion_ack(
        self,
        conn: socket.socket | None,
        *,
        accepted: bool,
        reason: str,
        action_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        if conn:
            response = {"type": "lode_complete_ack", "accepted": accepted, "reason": reason}
            if action_id is not None:
                response["action_id"] = action_id
            if detail is not None:
                response["detail"] = detail
            self._send_response(conn, response)

    def _handle_lode_complete(self, message: dict, conn: socket.socket | None) -> None:
        """Validate completion and prepare server-owned bytes off-loop."""
        lode_id = message.get("lode_id")
        generation = message.get("run_generation")
        lode = self._find_lode(lode_id) if isinstance(lode_id, str) else None
        if not lode:
            self._send_completion_ack(conn, accepted=False, reason="lode_not_found")
            return
        if not generation:
            self._send_completion_ack(conn, accepted=False, reason="missing_run_generation")
            return
        if generation != lode.get("run_generation"):
            self._send_completion_ack(conn, accepted=False, reason="stale_run_generation")
            return
        if message.get("stage") != lode.get("stage") or lode.get("stage") not in STAGES:
            self._send_completion_ack(conn, accepted=False, reason="stage_mismatch")
            return

        if _pending_completion_exists(lode_id):
            try:
                pending = self._load_pending(lode_id)
            except (OSError, ValueError, json.JSONDecodeError):
                self._send_completion_ack(conn, accepted=False, reason="completion_pending")
                return
            same_submission = bool(
                pending
                and pending["run_generation"] == generation
                and pending["stage"] == message.get("stage")
                and pending["output"]["digest_hex"] == message.get("digest_hex")
                and pending["output"]["byte_length"] == message.get("byte_length")
            )
            self._send_completion_ack(
                conn,
                accepted=same_submission,
                reason="already_accepted" if same_submission else "completion_pending",
                action_id=pending["action_id"] if same_submission else None,
            )
            return
        if is_terminal_failure_kind(lode.get("failure_kind")):
            self._send_completion_ack(conn, accepted=False, reason="terminal_failure")
            return
        if not lode.get("active") or lode_id not in self.lode_clients:
            self._send_completion_ack(conn, accepted=False, reason="inactive_runner")
            return
        if lode_id in self.completion_acceptances:
            self._send_completion_ack(conn, accepted=False, reason="completion_pending")
            return
        if message.get("digest_algorithm") != completion.DIGEST_ALGORITHM:
            self._send_completion_ack(conn, accepted=False, reason="invalid_output")
            return
        length = message.get("byte_length")
        digest_hex = message.get("digest_hex")
        encoded = message.get("output_base64")
        if (
            isinstance(length, bool)
            or not isinstance(length, int)
            or length < 1
            or not isinstance(digest_hex, str)
            or len(digest_hex) != 64
            or not isinstance(encoded, str)
        ):
            self._send_completion_ack(conn, accepted=False, reason="invalid_output")
            return
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            self._send_completion_ack(conn, accepted=False, reason="invalid_output")
            return
        if (
            len(data) != length
            or hashlib.sha256(data).hexdigest() != digest_hex
            or not data.strip()
        ):
            self._send_completion_ack(conn, accepted=False, reason="invalid_output")
            return
        try:
            ownership = completion.load_run_ownership(lode_id, generation, require_worker=True)
        except (OSError, ValueError, json.JSONDecodeError):
            ownership = None
        if ownership is None:
            self._send_completion_ack(conn, accepted=False, reason="ownership_unavailable")
            return
        if ownership["proof_mode"] == "linux-strict" and teardown.resolve_pidfd_interface() is None:
            self._send_completion_ack(conn, accepted=False, reason="pidfd_unavailable")
            return
        project = find_project(lode.get("project", "")) if lode["stage"] == "ship" else None
        if lode["stage"] == "ship" and not project:
            self._send_completion_ack(conn, accepted=False, reason="ship_provenance_unavailable")
            return

        action_id = uuid.uuid4().hex
        self.completion_acceptances[lode_id] = action_id
        snapshot = copy.deepcopy(lode)

        def prepare() -> None:
            def finish(result: dict) -> None:
                self._enqueue_event(
                    {
                        "type": "_completion_acceptance_result",
                        "exchange_id": message.get("exchange_id"),
                        "lode_id": lode_id,
                        "run_generation": generation,
                        "stage": snapshot["stage"],
                        "action_id": action_id,
                        "result": result,
                    },
                    conn,
                )

            try:
                completion.collect_orphaned_staging(lode_id, None)
                output = completion.stage_output(
                    lode_id,
                    data,
                    expected_length=length,
                    expected_sha256=digest_hex,
                )
            except Exception as error:
                finish({"ok": False, "reason": "output_staging_unavailable", "error": str(error)})
                return
            try:
                source_path = completion.run_ownership_path(lode_id, generation)
                source_digest = completion.durable_json_sha256(source_path)
                source = completion.load_run_ownership(lode_id, generation, require_worker=True)
                if source is None:
                    raise RuntimeError("generation ownership disappeared")
            except Exception as error:
                finish({"ok": False, "reason": "ownership_unavailable", "error": str(error)})
                return
            try:
                ship = None
                if project is not None:
                    ship = _capture_ship_acceptance(snapshot, project.path, action_id)
            except Exception as error:
                finish(
                    {
                        "ok": False,
                        "reason": "ship_provenance_unavailable",
                        "error": str(error),
                    }
                )
                return
            finish(
                {
                    "ok": True,
                    "output": output,
                    "ownership": source,
                    "source_digest": source_digest,
                    "ship": ship,
                }
            )

        thread = threading.Thread(target=prepare, name="hopper-completion-accept", daemon=True)
        self.registration_threads[f"accept:{lode_id}:{generation}"] = thread
        thread.start()

    def _handle_completion_acceptance_result(
        self, message: dict, conn: socket.socket | None
    ) -> None:
        """Commit the durable record and install its generation fence."""
        lode_id = message.get("lode_id")
        generation = message.get("run_generation")
        action_id = message.get("action_id")
        self.registration_threads.pop(f"accept:{lode_id}:{generation}", None)
        if self.completion_acceptances.get(lode_id) != action_id:
            logger.info(
                "Discarding stale completion acceptance lode=%s action=%s", lode_id, action_id
            )
            return
        self.completion_acceptances.pop(lode_id, None)
        lode = self._find_lode(lode_id) if isinstance(lode_id, str) else None
        result = message.get("result", {})
        if not lode:
            self._send_completion_ack(conn, accepted=False, reason="lode_not_found")
            return
        if lode.get("run_generation") != generation:
            self._send_completion_ack(conn, accepted=False, reason="stale_run_generation")
            return
        if lode.get("stage") != message.get("stage"):
            self._send_completion_ack(conn, accepted=False, reason="stage_mismatch")
            return
        if is_terminal_failure_kind(lode.get("failure_kind")):
            self._send_completion_ack(conn, accepted=False, reason="terminal_failure")
            return
        if not lode.get("active") or lode_id not in self.lode_clients:
            self._send_completion_ack(conn, accepted=False, reason="inactive_runner")
            return
        if _pending_completion_exists(lode_id):
            self._send_completion_ack(conn, accepted=False, reason="completion_pending")
            return
        if result.get("ok") is not True:
            reason = result.get("reason", "ownership_unavailable")
            logger.error("Completion preparation failed lode=%s: %s", lode_id, result.get("error"))
            self._send_completion_ack(
                conn, accepted=False, reason=reason, detail=result.get("error")
            )
            return
        try:
            record = completion.new_pending_completion(
                lode_id=lode_id,
                stage=message["stage"],
                run_generation=generation,
                output_facts=result["output"],
                ownership_record=result["ownership"],
                source_record_sha256=result["source_digest"],
                ship=result["ship"],
                action_id=action_id,
            )
            completion.write_pending_completion(record)
        except Exception as error:
            logger.error("Completion record persistence failed lode=%s: %s", lode_id, error)
            self._send_completion_ack(
                conn,
                accepted=False,
                reason="completion_persistence_unavailable",
                detail=str(error),
            )
            return

        # The fsynced record above is the acceptance linearization point.
        self._cancel_generation_guard(lode_id, generation)
        self._project_completion(record, via="completion_acceptance")
        self._send_completion_ack(
            conn,
            accepted=True,
            reason="accepted",
            action_id=record["action_id"],
        )
        self._schedule_completion_step(record, "output_publish", "publishing_output")

    def _persist_completion(self, record: dict, *, via: str) -> None:
        completion.write_pending_completion(record)
        self._project_completion(record, via=via)

    def _schedule_completion_step(self, record: dict, marker_name: str, phase: str) -> None:
        """Persist an intent and start exactly one daemon worker for it."""
        marker = record["markers"][marker_name]
        if marker["state"] in {"not_started", "blocked"}:
            completion.transition_marker(record, marker_name, "intent")
        elif marker["state"] != "intent":
            return
        record["phase"] = phase
        record["recovery"] = {"kind": None, "message": None, "command": None}
        self._persist_completion(record, via=f"completion_intent:{marker_name}")
        key = (record["action_id"], phase)
        existing = self.completion_threads.get(key)
        if existing is not None and existing.is_alive():
            return
        snapshot = copy.deepcopy(record)
        attempt_id = marker["attempt_id"]
        retained_pidfd = self.supervisor_pidfds.get((record["lode_id"], record["run_generation"]))
        context = self._completion_step_context(record, marker_name)
        thread = threading.Thread(
            target=self._run_completion_step,
            args=(snapshot, marker_name, phase, attempt_id, retained_pidfd, context),
            name=f"hopper-completion-{phase}",
            daemon=True,
        )
        self.completion_threads[key] = thread
        thread.start()

    def _run_completion_step(
        self,
        record: dict,
        marker_name: str,
        phase: str,
        attempt_id: str,
        retained_pidfd: int | None,
        context: dict | None,
    ) -> None:
        """Perform one completion side effect without mutating server state."""
        try:
            if phase == "publishing_output":
                try:
                    completion.publish_output(record)
                except Exception as publish_error:
                    try:
                        completion.verify_staged_output(record)
                    except Exception as staged_error:
                        result = {
                            "ok": False,
                            "staged_bytes": "unrecoverable",
                            "error": (
                                f"output publication failed: {publish_error}; accepted staged "
                                f"bytes are unavailable: {staged_error}"
                            ),
                        }
                    else:
                        result = {
                            "ok": False,
                            "staged_bytes": "verified",
                            "error": f"output publication failed: {publish_error}",
                        }
                else:
                    result = {"ok": True}
            elif phase == "capturing_ownership":
                result = self._recapture_completion_ownership(record, retained_pidfd)
            elif phase == "closing_pane":
                closed = teardown.close_owned_pane(record["ownership"])
                result = {"ok": closed["state"] == "gone", "error": closed["error"]}
            elif phase in {"observing_containment", "force_killing"}:
                result = self._observe_completion_containment(record, retained_pidfd)
            elif phase == "proving_ship_landing":
                result = self._prove_ship_landing(record)
            elif phase == "quarantining":
                result = self._run_ship_cleanup_step(record, marker_name)
            elif phase == "spawning":
                result = self._spawn_completion_pane(record, context or {})
            else:
                result = {"ok": False, "error": f"unsupported completion phase {phase}"}
        except Exception as error:
            result = {"ok": False, "error": str(error)}
        self._enqueue_event(
            {
                "type": "_completion_step_result",
                "lode_id": record["lode_id"],
                "run_generation": record["run_generation"],
                "action_id": record["action_id"],
                "marker_name": marker_name,
                "phase": phase,
                "attempt_id": attempt_id,
                "result": result,
            }
        )

    def _completion_step_context(self, record: dict, marker_name: str) -> dict | None:
        """Snapshot serialized state needed by a completion worker."""
        if marker_name != "spawn":
            return None
        target_id = self._completion_spawn_target_id(record)
        target = self._find_lode(target_id) if target_id else None
        if target is None:
            return {"error": "completion spawn target is absent"}
        project = find_project(target.get("project", ""))
        return {
            "target": copy.deepcopy(target),
            "project_path": project.path if project else None,
        }

    def _prove_ship_landing(self, record: dict) -> dict:
        """Run the canonical fresh landing proof and first work-loss guards."""
        worktree = record["ship"]["quarantine"]["original_path"]
        verdict = git.ship_landing_verdict(worktree)
        accepted = verdict.cause == "ancestry_contained"
        landing = {
            "cause": verdict.cause,
            "base_ref": verdict.base_ref,
            "detail": verdict.detail,
            "accepted": accepted,
        }
        if not accepted:
            return {"ok": False, "landing": landing, "error": verdict.detail}
        if git.is_dirty(worktree):
            return {
                "ok": False,
                "landing": landing,
                "error": "ship worktree became dirty after landing proof",
            }
        count, basis = git.unpushed_commits(worktree)
        if count is None:
            return {
                "ok": False,
                "landing": landing,
                "error": "ship worktree unpushed commit count is unknown",
            }
        if count:
            return {
                "ok": False,
                "landing": landing,
                "error": f"ship worktree has {count} unpushed commit(s) against {basis}",
            }
        return {"ok": True, "landing": landing}

    @staticmethod
    def _run_ship_cleanup_step(record: dict, marker_name: str) -> dict:
        ship = record["ship"]
        provenance = ship["provenance"]
        quarantine = ship["quarantine"]
        if marker_name == "quarantine_rename":
            fact = git.quarantine_worktree(provenance, quarantine)
            return {"ok": fact["state"] == "renamed", "fact": fact, "error": fact["error"]}
        if marker_name == "worktree_repair":
            fact = git.repair_quarantined_worktree(provenance, quarantine)
            return {"ok": fact["state"] == "repaired", "fact": fact, "error": fact["error"]}
        if marker_name == "cleanup_authorization":
            fact = git.authorize_quarantine_cleanup(
                provenance, quarantine, base_ref=ship["landing"]["base_ref"]
            )
            return {"ok": fact["authorized"], "fact": fact, "error": fact["error"]}
        if marker_name == "worktree_remove":
            authorization = git.authorize_quarantine_cleanup(
                provenance, quarantine, base_ref=ship["landing"]["base_ref"]
            )
            if not authorization["authorized"]:
                return {"ok": False, "fact": authorization, "error": authorization["error"]}
            fact = git.remove_quarantined_worktree(provenance, quarantine)
            return {
                "ok": fact["state"] in {"removed", "already-absent"},
                "fact": fact,
                "error": fact["error"],
            }
        if marker_name == "branch_delete":
            fact = git.delete_branch_if_unchanged(provenance)
            return {
                "ok": fact["state"] in {"deleted", "already-absent"},
                "fact": fact,
                "error": fact["error"],
            }
        return {"ok": False, "error": f"unsupported ship cleanup marker {marker_name}"}

    @staticmethod
    def _spawn_completion_pane(record: dict, context: dict) -> dict:
        if context.get("error"):
            return {"ok": False, "error": context["error"]}
        target = context["target"]
        spawn = record["spawn"]
        try:
            receipt = completion.load_spawn_receipt(record["lode_id"], record["action_id"])
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return {"ok": False, "error": f"spawn receipt is invalid: {error}"}
        if receipt is not None:
            expected = {
                "action_id": record["action_id"],
                "source_lode_id": record["lode_id"],
                "target_lode_id": target["id"],
                "target_generation": spawn["target_generation"],
            }
            if any(receipt.get(key) != value for key, value in expected.items()):
                return {"ok": False, "error": "spawn receipt belongs to another action"}
            liveness = pane_liveness(receipt["pane_id"])
            if liveness is Liveness.ALIVE:
                return {"ok": True, "pane_id": receipt["pane_id"], "adopted": True}
            return {
                "ok": False,
                "error": "action-scoped pane receipt exists but its pane is not provably alive",
            }
        panes = completion_action_panes(record["action_id"])
        if panes is None:
            return {"ok": False, "error": "tmux action-pane inventory is unavailable"}
        if panes:
            return {"ok": False, "error": "action-tagged pane exists without a valid receipt"}
        pane_env = {RUN_GENERATION_ENV: spawn["target_generation"]}
        if target.get("oom_scope"):
            pane_env[OOM_SCOPE_ENV] = target["oom_scope"]
        receipt_facts = {
            "path": str(completion.spawn_receipt_path(record["lode_id"], record["action_id"])),
            "action_id": record["action_id"],
            "source_lode_id": record["lode_id"],
            "target_lode_id": target["id"],
            "target_generation": spawn["target_generation"],
        }
        pane_id = spawn_claude(
            target["id"],
            context.get("project_path"),
            foreground=False,
            env=pane_env,
            spawn_receipt=receipt_facts,
        )
        if not pane_id:
            return {"ok": False, "error": "tmux could not create the completion runner pane"}
        try:
            receipt = completion.load_spawn_receipt(record["lode_id"], record["action_id"])
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return {"ok": False, "error": f"spawn receipt is invalid: {error}"}
        if receipt is None or receipt.get("pane_id") != pane_id:
            return {"ok": False, "error": "pane bootstrap did not publish its exact receipt"}
        return {"ok": True, "pane_id": pane_id, "adopted": False}

    def _recapture_completion_ownership(self, record: dict, retained_pidfd: int | None) -> dict:
        """Reverify generation ownership immediately before pane closure."""
        ownership = record["ownership"]
        source_path = (
            completion.lode_dir(record["lode_id"]) / ownership["source_record_relative_path"]
        )
        if completion.durable_json_sha256(source_path) != ownership["source_record_sha256"]:
            raise RuntimeError("generation ownership record digest changed")
        source = completion.load_run_ownership(
            record["lode_id"], record["run_generation"], require_worker=True
        )
        if source is None:
            raise RuntimeError("generation ownership record is absent")
        systemctl = oom.find_systemctl() if source["proof_mode"] == "linux-strict" else None
        captured = teardown.capture_ownership(
            pane_id=source["pane"]["pane_id"],
            supervisor_pid=source["supervisor"]["pid"],
            worker_pid=source["worker"]["pid"],
            process_group=source["process_group"],
            unit_name=source["unit_name"],
            systemctl=systemctl,
            platform=source["platform"],
        )
        if captured["state"] != "captured":
            raise RuntimeError(captured["error"] or "generation ownership cannot be recaptured")
        current = captured["ownership"]
        for key in ("platform", "proof_mode", "pane", "supervisor", "worker", "process_group"):
            if current[key] != ownership[key]:
                raise RuntimeError(f"generation {key} identity changed before pane closure")
        final_ownership = {
            **ownership,
            **current,
            "captured": True,
            "captured_at_ms": completion.accepted_at_ms(),
        }
        pidfd = retained_pidfd
        pidfd_owned = False
        if current["proof_mode"] == "linux-strict" and pidfd is None:
            pidfd_interface = teardown.resolve_pidfd_interface()
            if pidfd_interface is None:
                raise RuntimeError("pidfd_open and pidfd_send_signal are unavailable")
            reopened = teardown.reopen_supervisor_pidfd(
                current["supervisor"], pidfd_interface=pidfd_interface
            )
            if reopened["state"] != "alive":
                raise RuntimeError(reopened["error"] or "outside supervisor identity is gone")
            pidfd = reopened["fd"]
            pidfd_owned = True
        return {
            "ok": True,
            "ownership": final_ownership,
            "pidfd": pidfd,
            "pidfd_owned": pidfd_owned,
        }

    def _observe_completion_containment(self, record: dict, retained_pidfd: int | None) -> dict:
        """Build identity-bound observers and run the bounded state machine."""
        ownership = record["ownership"]
        mode = ownership["proof_mode"]
        pidfd_owned = False
        if mode == "linux-strict":
            containment = record["containment"]
            if containment["state"] == "kill_pending":
                if (
                    containment["last_cgroup_observation"] == "populated"
                    and record["markers"]["scope_kill"]["state"] != "intent"
                ):
                    return {"ok": False, "error": "cgroup kill intent is not durable"}
                if (
                    containment["last_supervisor_observation"] == "alive"
                    and record["markers"]["supervisor_kill"]["state"] != "intent"
                ):
                    return {"ok": False, "error": "supervisor kill intent is not durable"}
            pidfd_interface = teardown.resolve_pidfd_interface()
            if pidfd_interface is None:
                return {
                    "ok": False,
                    "error": "pidfd_open and pidfd_send_signal are unavailable",
                }
            if retained_pidfd is None:
                reopened = teardown.reopen_supervisor_pidfd(
                    ownership["supervisor"], pidfd_interface=pidfd_interface
                )
                if reopened["state"] == "cannot-tell":
                    return {
                        "ok": False,
                        "error": reopened["error"]
                        or "verified outside-supervisor pidfd is unavailable",
                    }
                retained_pidfd = reopened["fd"]
                supervisor_gone = reopened["state"] == "gone"
                pidfd_owned = retained_pidfd is not None
            else:
                supervisor_gone = False
            systemctl = oom.find_systemctl()
            if not systemctl:
                return {
                    "ok": False,
                    "error": "systemctl is unavailable for containment proof",
                    "pidfd": retained_pidfd,
                    "pidfd_owned": pidfd_owned,
                }

            def unit_observation() -> dict:
                return oom.read_scope_control_group(systemctl, ownership["unit"]["name"])

            handles = {
                "observe_cgroup": lambda: teardown.observe_cgroup(
                    ownership["cgroup"], unit_observation()
                ),
                "observe_supervisor": (
                    (lambda: "gone")
                    if supervisor_gone
                    else lambda: teardown.observe_pidfd(
                        retained_pidfd, pidfd_interface=pidfd_interface
                    )
                ),
                "observe_pane_root": lambda: teardown.observe_pane_root_absence(ownership),
                "kill_cgroup": lambda: teardown.kill_cgroup(ownership["cgroup"]),
                "kill_supervisor": (
                    (lambda: True)
                    if supervisor_gone
                    else lambda: teardown.kill_supervisor_pidfd(
                        retained_pidfd, pidfd_interface=pidfd_interface
                    )
                ),
            }
        else:
            owned_by_pid = {
                process["pid"]: process
                for process in [
                    ownership["pane"]["root_process"],
                    ownership["supervisor"],
                    ownership["worker"],
                    *ownership["descendants"],
                ]
            }

            def observe_bounded() -> dict:
                observed = teardown.observe_bounded_processes(
                    list(owned_by_pid.values()),
                    platform=ownership["platform"],
                    process_table=teardown.read_process_table(platform=ownership["platform"]),
                )
                if observed["state"] != "cannot-tell":
                    owned_by_pid.clear()
                    owned_by_pid.update(
                        {process["pid"]: process for process in observed["identities"]}
                    )
                return observed

            def observe_pane() -> str:
                liveness = pane_liveness(ownership["pane"]["pane_id"])
                if liveness is Liveness.GONE:
                    return "gone"
                if liveness is Liveness.ALIVE:
                    return "alive"
                return "cannot-tell"

            handles = {"observe_bounded": observe_bounded, "observe_pane": observe_pane}
        containment = teardown.observe_containment(record, handles)
        release_error = None
        if mode == "linux-strict" and containment["state"] == "proven":
            unit_state = oom.read_scope_control_group(systemctl, ownership["unit"]["name"])
            if unit_state["state"] == "present" and not oom.release_scope(
                systemctl, ownership["unit"]["name"]
            ):
                release_error = "strict Linux scope evidence could not be released"
            elif unit_state["state"] == "cannot-tell":
                release_error = "strict Linux scope release state is ambiguous"
        return {
            "ok": containment["state"] != "blocked" and release_error is None,
            "containment": containment,
            "error": release_error or containment.get("last_error"),
            "pidfd": retained_pidfd if mode == "linux-strict" else None,
            "pidfd_owned": pidfd_owned,
        }

    def _block_completion(
        self,
        record: dict,
        marker_name: str,
        recovery_kind: str,
        error: str | None,
    ) -> None:
        marker = record["markers"][marker_name]
        if marker["state"] == "intent":
            completion.transition_marker(
                record,
                marker_name,
                "blocked",
                attempt_id=marker["attempt_id"],
                detail=error or "completion phase failed",
            )
        if marker_name == "containment":
            for kill_marker in ("scope_kill", "supervisor_kill"):
                current = record["markers"][kill_marker]
                if current["state"] == "intent":
                    completion.transition_marker(
                        record,
                        kill_marker,
                        "blocked",
                        attempt_id=current["attempt_id"],
                        detail=error or "verified kill failed",
                    )
        if marker_name == "output_publish":
            record["phase"] = "output_blocked"
        elif marker_name in {"ownership_capture", "pane_close", "containment"}:
            record["phase"] = "containment_blocked"
        elif marker_name == "ship_landing":
            record["phase"] = "ship_blocked"
        else:
            record["phase"] = "cleanup_blocked"
        record["recovery"] = {
            "kind": recovery_kind,
            "message": error or "completion phase failed",
            "command": completion.recovery_command(record, recovery_kind),
        }
        if marker_name == "output_publish":
            record["output"]["failure"] = error or "completion output publication failed"
        self._persist_completion(record, via=f"completion_blocked:{marker_name}")

    def _handle_completion_step_result(self, message: dict) -> None:
        """Accept one worker result only for its persisted action/phase/attempt."""
        action_id = message.get("action_id")
        phase = message.get("phase")
        self.completion_threads.pop((action_id, phase), None)
        lode_id = message.get("lode_id")
        try:
            record = self._load_pending(lode_id)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            logger.error("Cannot apply completion result lode=%s: %s", lode_id, error)
            result = message.get("result", {})
            if result.get("pidfd_owned") and isinstance(result.get("pidfd"), int):
                os.close(result["pidfd"])
            return
        marker_by_phase = {
            "publishing_output": "output_publish",
            "capturing_ownership": "ownership_capture",
            "closing_pane": "pane_close",
            "observing_containment": "containment",
            "force_killing": "containment",
            "proving_ship_landing": "ship_landing",
            "spawning": "spawn",
        }
        marker_name = message.get("marker_name") or marker_by_phase.get(phase)
        allowed_markers = {
            "publishing_output": {"output_publish"},
            "capturing_ownership": {"ownership_capture"},
            "closing_pane": {"pane_close"},
            "observing_containment": {"containment"},
            "force_killing": {"containment"},
            "proving_ship_landing": {"ship_landing"},
            "quarantining": {
                "quarantine_rename",
                "worktree_repair",
                "cleanup_authorization",
                "worktree_remove",
                "branch_delete",
            },
            "spawning": {"spawn"},
        }
        result = message.get("result", {})
        if (
            record is None
            or marker_name is None
            or marker_name not in allowed_markers.get(phase, set())
            or record["action_id"] != action_id
            or record["run_generation"] != message.get("run_generation")
            or record["phase"] != phase
            or record["markers"][marker_name]["state"] != "intent"
            or record["markers"][marker_name]["attempt_id"] != message.get("attempt_id")
        ):
            logger.info(
                "Discarding stale completion result lode=%s action=%s phase=%s",
                lode_id,
                action_id,
                phase,
            )
            if result.get("pidfd_owned") and isinstance(result.get("pidfd"), int):
                os.close(result["pidfd"])
            return

        pidfd = result.get("pidfd")
        if isinstance(pidfd, int):
            key = (record["lode_id"], record["run_generation"])
            prior = self.supervisor_pidfds.get(key)
            if result.get("pidfd_owned") and prior is not None and prior != pidfd:
                os.close(prior)
            self.supervisor_pidfds[key] = pidfd

        if result.get("ok") is not True:
            if "containment" in result:
                record["containment"] = result["containment"]
            recovery_kind = {
                "publishing_output": (
                    "publication" if result.get("staged_bytes") == "verified" else "output"
                ),
                "capturing_ownership": "ownership",
                "closing_pane": "ownership",
                "observing_containment": "containment",
                "force_killing": "containment",
                "proving_ship_landing": "landing",
                "quarantining": "cleanup",
                "spawning": "spawn",
            }[phase]
            if phase == "proving_ship_landing" and "landing" in result:
                record["ship"]["landing"] = result["landing"]
            if phase == "quarantining" and record.get("ship") is not None:
                record["ship"]["cleanup_failure"] = result.get("error")
                quarantine = record["ship"]["quarantine"]
                if marker_name in {"cleanup_authorization", "worktree_remove"}:
                    quarantine["removal_outcome"] = "retained"
                    quarantine["branch_outcome"] = "retained"
                elif marker_name == "branch_delete":
                    quarantine["branch_outcome"] = "retained"
            self._block_completion(record, marker_name, recovery_kind, result.get("error"))
            return

        marker = record["markers"][marker_name]
        if phase == "publishing_output":
            completion.transition_marker(
                record, marker_name, "done", attempt_id=marker["attempt_id"]
            )
            record["output"]["published"] = True
            record["output"]["failure"] = None
            self._persist_completion(record, via="completion_result:output_publish")
            self._schedule_completion_step(record, "ownership_capture", "capturing_ownership")
            return

        if phase == "capturing_ownership":
            record["ownership"] = result["ownership"]
            completion.transition_marker(
                record, marker_name, "done", attempt_id=marker["attempt_id"]
            )
            record["containment"]["state"] = "pane_close_pending"
            self._persist_completion(record, via="completion_result:ownership_capture")
            self._schedule_completion_step(record, "pane_close", "closing_pane")
            return

        if phase == "closing_pane":
            completion.transition_marker(
                record, marker_name, "done", attempt_id=marker["attempt_id"]
            )
            record["containment"] = teardown.start_containment(record)
            self._persist_completion(record, via="completion_result:pane_close")
            self._schedule_completion_step(record, "containment", "observing_containment")
            return

        if phase == "proving_ship_landing":
            record["ship"]["landing"] = result["landing"]
            completion.transition_marker(
                record,
                marker_name,
                "done",
                attempt_id=marker["attempt_id"],
                detail=result["landing"]["cause"],
            )
            self._persist_completion(record, via="completion_result:ship_landing")
            self._continue_completion(record)
            return

        if phase == "quarantining":
            fact = result["fact"]
            if marker_name == "worktree_repair":
                record["ship"]["quarantine"]["registration_repaired"] = True
            elif marker_name == "worktree_remove":
                record["ship"]["quarantine"]["removal_outcome"] = "removed"
            elif marker_name == "branch_delete":
                record["ship"]["quarantine"]["branch_outcome"] = (
                    "already_absent" if fact["state"] == "already-absent" else "deleted"
                )
            record["ship"]["cleanup_failure"] = None
            completion.transition_marker(
                record,
                marker_name,
                "done",
                attempt_id=marker["attempt_id"],
                detail=fact.get("state") or "authorized",
            )
            self._persist_completion(record, via=f"completion_result:{marker_name}")
            self._continue_completion(record)
            return

        if phase == "spawning":
            record["spawn"]["pane_id"] = result["pane_id"]
            self._persist_completion(record, via="completion_result:spawn_pane")
            self._reconcile_spawn_adoption(record)
            return

        containment = result["containment"]
        record["containment"] = containment
        if containment["state"] == "kill_pending":
            attempt = uuid.uuid4().hex
            if containment["last_cgroup_observation"] == "populated":
                completion.transition_marker(record, "scope_kill", "intent", attempt_id=attempt)
            if containment["last_supervisor_observation"] == "alive":
                completion.transition_marker(
                    record, "supervisor_kill", "intent", attempt_id=attempt
                )
            record["phase"] = "force_killing"
            self._persist_completion(record, via="completion_intent:force_killing")
            self._schedule_completion_step(record, "containment", "force_killing")
            return
        if containment["state"] != "proven":
            self._block_completion(
                record,
                marker_name,
                "containment",
                containment.get("last_error") or "containment proof is incomplete",
            )
            return
        completion.transition_marker(record, marker_name, "done", attempt_id=marker["attempt_id"])
        for kill_marker in ("scope_kill", "supervisor_kill"):
            current = record["markers"][kill_marker]
            if current["state"] == "intent":
                completion.transition_marker(
                    record, kill_marker, "done", attempt_id=current["attempt_id"]
                )
        record["phase"] = "publishing_next_action"
        self._persist_completion(record, via="completion_result:containment_proven")
        fd = self.supervisor_pidfds.pop((record["lode_id"], record["run_generation"]), None)
        if fd is not None:
            os.close(fd)
        self._continue_completion(record)

    def _continue_completion(self, record: dict) -> None:
        """Dispatch the first unfinished post-containment action."""
        if record["markers"]["containment"]["state"] != "done":
            return
        if record["stage"] == "ship":
            for marker_name in (
                "ship_landing",
                "quarantine_rename",
                "worktree_repair",
                "cleanup_authorization",
            ):
                if record["markers"][marker_name]["state"] != "done":
                    phase = (
                        "proving_ship_landing" if marker_name == "ship_landing" else "quarantining"
                    )
                    self._schedule_completion_step(record, marker_name, phase)
                    return
        if record["markers"]["stage_mutation"]["state"] != "done":
            if not self._apply_completion_stage(record):
                return
        if record["stage"] == "ship":
            if record["markers"]["archive"]["state"] != "done":
                if not self._apply_completion_archive(record):
                    return
            if record["markers"]["backlog"]["state"] != "done":
                if not self._apply_completion_backlog(record):
                    return
        needs_spawn = record["stage"] != "ship" or bool(
            record["ship"]["backlog"]["promoted_lode_id"]
        )
        if needs_spawn and record["markers"]["spawn"]["state"] != "done":
            if not self._prepare_completion_spawn(record):
                return
            self._reconcile_spawn_adoption(record)
            return
        if record["stage"] == "ship":
            for marker_name in ("worktree_remove", "branch_delete"):
                if record["markers"][marker_name]["state"] != "done":
                    self._schedule_completion_step(record, marker_name, "quarantining")
                    return
        self._clear_completed_action(record)

    def _apply_completion_stage(self, record: dict) -> bool:
        marker = record["markers"]["stage_mutation"]
        if marker["state"] in {"not_started", "blocked"}:
            completion.transition_marker(record, "stage_mutation", "intent")
            record["phase"] = "publishing_next_action"
            self._persist_completion(record, via="completion_intent:stage_mutation")
            marker = record["markers"]["stage_mutation"]
        lode = self._find_completion_lode(record["lode_id"])
        target = record["next_action"]["target_stage"] or "shipped"
        if lode is None:
            self._block_completion(record, "stage_mutation", "cleanup", "completion lode is absent")
            return False
        if lode.get("stage") == record["stage"]:
            lode["stage"] = target
            touch(lode)
            if any(item is lode for item in self.lodes):
                save_lodes(self.lodes)
            else:
                save_archived_lodes(self.archived_lodes)
            _log_state_change(
                lode["id"],
                lode.get("state", "teardown"),
                lode.get("status", ""),
                "completion_stage_mutation",
            )
        elif lode.get("stage") != target:
            self._block_completion(
                record,
                "stage_mutation",
                "cleanup",
                "lode stage conflicts with the accepted completion action",
            )
            return False
        completion.transition_marker(
            record,
            "stage_mutation",
            "done",
            attempt_id=marker["attempt_id"],
            detail=f"stage {target}",
        )
        self._persist_completion(record, via="completion_result:stage_mutation")
        return True

    def _apply_completion_archive(self, record: dict) -> bool:
        marker = record["markers"]["archive"]
        if marker["state"] in {"not_started", "blocked"}:
            completion.transition_marker(record, "archive", "intent")
            self._persist_completion(record, via="completion_intent:archive")
            marker = record["markers"]["archive"]
        try:
            archived = archive_lode_for_action(
                self.lodes,
                self.archived_lodes,
                record["lode_id"],
                record["action_id"],
            )
        except (OSError, ValueError) as error:
            self._block_completion(record, "archive", "cleanup", str(error))
            return False
        record["ship"]["archive_published"] = True
        completion.transition_marker(
            record,
            "archive",
            "done",
            attempt_id=marker["attempt_id"],
            detail="archive_action_id matched",
        )
        _log_state_change(
            archived["id"],
            archived.get("state", "teardown"),
            archived.get("status", ""),
            "completion_archive",
        )
        self._persist_completion(record, via="completion_result:archive")
        self.broadcast({"type": "lode_archived", "lode": archived})
        return True

    def _apply_completion_backlog(self, record: dict) -> bool:
        marker = record["markers"]["backlog"]
        if marker["state"] in {"not_started", "blocked"}:
            completion.transition_marker(record, "backlog", "intent")
            self._persist_completion(record, via="completion_intent:backlog")
            marker = record["markers"]["backlog"]
        plan = record["ship"]["backlog"]
        source = self._find_completion_lode(record["lode_id"])
        if source is None:
            self._block_completion(record, "backlog", "cleanup", "archived lode is absent")
            return False
        if not plan["planned"]:
            project = find_project(source.get("project", ""))
            queued = [item for item in self.backlog if item.queued == record["lode_id"]]
            candidates = [item for item in queued if item.project == source.get("project", "")]
            selected = (
                None
                if project and project.disabled
                else (min(candidates, key=lambda item: item.created_at) if candidates else None)
            )
            plan.update(
                planned=True,
                selected_item_id=selected.id if selected else None,
                promoted_lode_id=(reserve_lode_id(self.lodes) if selected is not None else None),
                remaining_item_ids=(
                    [item.id for item in queued if item.id != selected.id]
                    if selected is not None
                    else [item.id for item in queued]
                ),
            )
            self._persist_completion(record, via="completion_backlog:plan")
        planned_item_ids = set(plan["remaining_item_ids"])
        if plan["selected_item_id"] is not None:
            planned_item_ids.add(plan["selected_item_id"])
        newly_queued = [
            item.id
            for item in self.backlog
            if item.queued == record["lode_id"] and item.id not in planned_item_ids
        ]
        if newly_queued:
            plan["remaining_item_ids"].extend(newly_queued)
            self._persist_completion(record, via="completion_backlog:extend_plan")
        selected_id = plan["selected_item_id"]
        promoted_id = plan["promoted_lode_id"]
        if selected_id is not None and promoted_id is not None:
            promoted_matches = [
                lode
                for lode in [*self.lodes, *self.archived_lodes]
                if lode.get("id") == promoted_id
            ]
            selected = next((item for item in self.backlog if item.id == selected_id), None)
            selected_data = selected.to_dict() if selected is not None else None
            if promoted_matches:
                promoted = promoted_matches[0]
                if (
                    len(promoted_matches) != 1
                    or promoted.get("backlog", {}).get("id") != selected_id
                ):
                    self._block_completion(
                        record, "backlog", "cleanup", "promoted lode identity conflicts"
                    )
                    return False
            elif selected is not None:
                try:
                    promoted = create_lode(
                        self.lodes,
                        selected.project,
                        selected.description,
                        lode_id=promoted_id,
                    )
                except (OSError, RuntimeError, ValueError) as error:
                    self._block_completion(record, "backlog", "cleanup", str(error))
                    return False
                promoted["backlog"] = selected.to_dict()
                save_lodes(self.lodes)
                _log_state_change(
                    promoted["id"],
                    promoted["state"],
                    promoted["status"],
                    "completion_backlog_promote",
                )
                self.broadcast({"type": "lode_created", "lode": promoted})
            else:
                self._block_completion(
                    record,
                    "backlog",
                    "cleanup",
                    "selected backlog item disappeared before its disposition",
                )
                return False
        else:
            selected_data = None
        try:
            apply_completion_disposition(
                self.backlog,
                source_lode_id=record["lode_id"],
                source_project=source.get("project", ""),
                selected_item_id=selected_id,
                promoted_lode_id=promoted_id,
                remaining_item_ids=plan["remaining_item_ids"],
            )
        except (OSError, ValueError) as error:
            self._block_completion(record, "backlog", "cleanup", str(error))
            return False
        if any(item.queued == record["lode_id"] for item in self.backlog):
            self._block_completion(
                record,
                "backlog",
                "cleanup",
                "backlog changed while its recorded disposition was being applied",
            )
            return False
        if selected_data is not None:
            self.broadcast({"type": "backlog_removed", "item": selected_data})
        for item in self.backlog:
            if item.id in plan["remaining_item_ids"]:
                self.broadcast({"type": "backlog_updated", "item": item.to_dict()})
        plan["applied"] = True
        completion.transition_marker(
            record,
            "backlog",
            "done",
            attempt_id=marker["attempt_id"],
            detail="recorded backlog disposition applied",
        )
        self._persist_completion(record, via="completion_result:backlog")
        return True

    def _prepare_completion_spawn(self, record: dict) -> bool:
        marker = record["markers"]["spawn"]
        target_id = self._completion_spawn_target_id(record)
        target = self._find_lode(target_id) if target_id else None
        if target is None:
            self._block_completion(record, "spawn", "spawn", "completion spawn target is absent")
            return False
        if record["spawn"] is None:
            generation = uuid.uuid4().hex
            record["spawn"] = {
                "target_generation": generation,
                "receipt_relative_path": f"spawn-{record['action_id']}.json",
                "pane_id": None,
                "supervisor_adopted": False,
                "worker_adopted": False,
            }
        generation = record["spawn"]["target_generation"]
        if marker["state"] in {"not_started", "blocked"}:
            completion.transition_marker(record, "spawn", "intent")
            record["phase"] = "spawning"
            self._persist_completion(record, via="completion_intent:spawn")
            marker = record["markers"]["spawn"]
        current_generation = target.get("run_generation")
        allowed_prior = {None, record["run_generation"], generation}
        if current_generation not in allowed_prior:
            self._block_completion(
                record, "spawn", "spawn", "spawn target generation conflicts with this action"
            )
            return False
        try:
            receipt = completion.load_spawn_receipt(record["lode_id"], record["action_id"])
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self._block_completion(record, "spawn", "spawn", f"spawn receipt is invalid: {error}")
            return False
        if receipt is not None and (
            receipt["target_lode_id"] != target["id"] or receipt["target_generation"] != generation
        ):
            self._block_completion(
                record, "spawn", "spawn", "spawn receipt belongs to another action target"
            )
            return False
        pane_id = receipt["pane_id"] if receipt is not None else None
        current_pane = target.get("tmux_pane")
        retained_source_pane = bool(
            receipt is None
            and target["id"] == record["lode_id"]
            and current_generation == record["run_generation"]
            and current_pane == record["ownership"]["pane"]["pane_id"]
        )
        if current_pane not in {None, pane_id} and not retained_source_pane:
            self._block_completion(
                record, "spawn", "spawn", "spawn target pane conflicts with this action"
            )
            return False
        target["active"] = False
        target["pid"] = None
        target["tmux_pane"] = pane_id
        target["run_generation"] = generation
        target["oom_scope"] = (
            oom.scope_unit_name(target["id"], generation) if oom.is_linux() else None
        )
        touch(target)
        save_lodes(self.lodes)
        _log_state_change(
            target["id"],
            target.get("state", "teardown"),
            target.get("status", ""),
            "completion_spawn_prepare",
        )
        self._schedule_completion_step(record, "spawn", "spawning")
        return True

    def _reconcile_spawn_adoption(self, record: dict) -> None:
        if record["spawn"] is None or record["markers"]["spawn"]["state"] != "intent":
            return
        target_id = self._completion_spawn_target_id(record)
        target = self._find_lode(target_id) if target_id else None
        if target is None or target.get("run_generation") != record["spawn"]["target_generation"]:
            return
        try:
            receipt = completion.load_spawn_receipt(record["lode_id"], record["action_id"])
            ownership = completion.load_run_ownership(
                target["id"], record["spawn"]["target_generation"], require_worker=False
            )
            final_ownership = completion.load_run_ownership(
                target["id"], record["spawn"]["target_generation"], require_worker=True
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return
        if (
            receipt is None
            or receipt["target_lode_id"] != target["id"]
            or receipt["target_generation"] != record["spawn"]["target_generation"]
            or receipt["pane_id"] != record["spawn"]["pane_id"]
        ):
            return
        record["spawn"]["supervisor_adopted"] = ownership is not None
        record["spawn"]["worker_adopted"] = final_ownership is not None
        if not (record["spawn"]["supervisor_adopted"] and record["spawn"]["worker_adopted"]):
            self._persist_completion(record, via="completion_spawn:reconcile")
            return
        marker = record["markers"]["spawn"]
        completion.transition_marker(
            record, "spawn", "done", attempt_id=marker["attempt_id"], detail="runner adopted"
        )
        self._persist_completion(record, via="completion_result:spawn_reconcile")
        self._continue_completion(record)

    def _clear_completed_action(self, record: dict) -> None:
        marker = record["markers"]["pending_clear"]
        if marker["state"] == "not_started":
            completion.transition_marker(record, "pending_clear", "intent")
            self._persist_completion(record, via="completion_intent:pending_clear")
            marker = record["markers"]["pending_clear"]
        if marker["state"] == "intent":
            completion.transition_marker(
                record,
                "pending_clear",
                "done",
                attempt_id=marker["attempt_id"],
                detail="all durable side effects complete",
            )
        record["phase"] = "complete"
        if record["stage"] == "ship":
            self._persist_completion(record, via="completion_result:complete")
            lode = self._find_completion_lode(record["lode_id"])
            if lode is not None:
                lode["state"] = "ready"
                lode["status"] = completion.completion_status(record)
                lode["active"] = False
                lode["pid"] = None
                lode["tmux_pane"] = None
                lode["oom_scope"] = None
                touch(lode)
                save_archived_lodes(self.archived_lodes)
                _log_state_change(lode["id"], lode["state"], lode["status"], "completion_clear")
                self.broadcast({"type": "lode_updated", "lode": lode})
        else:
            completion.write_pending_completion(record)
            lode = self._find_lode(record["lode_id"])
            if lode is not None:
                lode["state"] = "running"
                lode["status"] = f"Starting {lode['stage']}"
                touch(lode)
                save_lodes(self.lodes)
                _log_state_change(lode["id"], lode["state"], lode["status"], "completion_clear")
                self.broadcast({"type": "lode_updated", "lode": lode})
        completion.clear_pending_completion(record)

    def _retry_completion(self, lode_id: str, conn: socket.socket | None) -> None:
        """Retry only the identity-bound phase named by a blocked record."""
        try:
            record = self._load_pending(lode_id)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            if conn:
                self._send_response(conn, {"type": "error", "error": str(error)})
            return
        if record is None:
            if conn:
                self._send_response(conn, {"type": "error", "error": "no pending completion"})
            return
        if record["phase"] == "output_blocked":
            if record["recovery"]["kind"] == "publication":
                self._schedule_completion_step(record, "output_publish", "publishing_output")
                if conn:
                    self._send_response(
                        conn, {"type": "lode_completion_retrying", "lode_id": lode_id}
                    )
                return
            if conn:
                self._send_response(
                    conn,
                    {"type": "error", "error": "accepted output requires repair-output"},
                )
            return
        recovery_kind = record["recovery"]["kind"]
        if record["phase"] not in {"containment_blocked", "ship_blocked", "cleanup_blocked"}:
            if conn:
                self._send_response(conn, {"type": "error", "error": "completion is not retryable"})
            return
        mapping = {
            "ownership": ("ownership_capture", "capturing_ownership"),
            "containment": ("containment", "observing_containment"),
            "landing": ("ship_landing", "proving_ship_landing"),
        }
        selected = mapping.get(recovery_kind)
        if recovery_kind in {"spawn", "cleanup"}:
            record["recovery"] = {"kind": None, "message": None, "command": None}
            self._continue_completion(record)
            selected = None
        if selected is None:
            if recovery_kind not in {"spawn", "cleanup"} and conn:
                self._send_response(conn, {"type": "error", "error": "completion is not retryable"})
            elif conn:
                self._send_response(conn, {"type": "lode_completion_retrying", "lode_id": lode_id})
            return
        marker_name, phase = selected
        if recovery_kind == "ownership" and record["markers"]["pane_close"]["state"] == "blocked":
            marker_name, phase = "pane_close", "closing_pane"
        if recovery_kind == "containment":
            completion.transition_marker(record, "containment", "intent")
            retrying_kill = False
            for kill_marker in ("scope_kill", "supervisor_kill"):
                if record["markers"][kill_marker]["state"] == "blocked":
                    completion.transition_marker(record, kill_marker, "intent")
                    retrying_kill = True
            record["containment"]["state"] = "kill_pending" if retrying_kill else "grace"
            record["containment"]["last_error"] = None
            phase = "force_killing" if retrying_kill else "observing_containment"
        self._schedule_completion_step(record, marker_name, phase)
        if conn:
            self._send_response(
                conn,
                {"type": "lode_completion_retrying", "lode_id": lode_id},
            )

    def _repair_completion_output(self, message: dict, conn: socket.socket | None) -> None:
        """Authenticate and replace only an accepted action's missing staged bytes."""

        def acknowledge(accepted: bool, reason: str) -> None:
            if conn:
                self._send_response(
                    conn,
                    {
                        "type": "lode_repair_output_ack",
                        "accepted": accepted,
                        "reason": reason,
                    },
                )

        lode_id = message.get("lode_id")
        if not isinstance(lode_id, str):
            acknowledge(False, "no_pending_output_failure")
            return
        try:
            record = self._load_pending(lode_id)
        except (OSError, ValueError, json.JSONDecodeError):
            acknowledge(False, "no_pending_output_failure")
            return
        if record is None:
            acknowledge(False, "no_pending_output_failure")
            return

        token = message.get("token")
        if not isinstance(token, str) or not secrets.compare_digest(
            token, record["output"]["repair_token"]
        ):
            acknowledge(False, "unauthenticated")
            return

        expected_identity = {
            "lode_id": record["lode_id"],
            "action_id": record["action_id"],
            "stage": record["stage"],
            "run_generation": record["run_generation"],
            "next_action": record["next_action"],
        }
        if any(message.get(key) != value for key, value in expected_identity.items()):
            acknowledge(False, "action_mismatch")
            return

        lode = self._find_completion_lode(lode_id)
        active_identity = bool(
            lode
            and any(item is lode for item in self.lodes)
            and lode.get("stage") == record["stage"]
            and lode.get("run_generation") == record["run_generation"]
        )
        archived_identity = bool(
            lode
            and any(item is lode for item in self.archived_lodes)
            and lode.get("archive_action_id") == record["action_id"]
        )
        if not active_identity and not archived_identity:
            acknowledge(False, "lode_identity_mismatch")
            return

        output = record["output"]
        marker = record["markers"]["output_publish"]
        if output["published"]:
            acknowledge(False, "already_published")
            return
        if (
            record["phase"] != "output_blocked"
            or record["recovery"]["kind"] != "output"
            or marker["state"] != "blocked"
        ):
            acknowledge(False, "no_pending_output_failure")
            return

        encoded = message.get("output_base64")
        if not isinstance(encoded, str):
            acknowledge(False, "output_mismatch")
            return
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            acknowledge(False, "output_mismatch")
            return
        length = message.get("byte_length")
        if (
            isinstance(length, bool)
            or not isinstance(length, int)
            or length != output["byte_length"]
            or len(data) != output["byte_length"]
        ):
            acknowledge(False, "output_mismatch")
            return
        digest_hex = message.get("digest_hex")
        if (
            message.get("digest_algorithm") != completion.DIGEST_ALGORITHM
            or not isinstance(digest_hex, str)
            or not secrets.compare_digest(digest_hex, output["digest_hex"])
            or not secrets.compare_digest(hashlib.sha256(data).hexdigest(), output["digest_hex"])
        ):
            acknowledge(False, "output_mismatch")
            return

        try:
            output["staged_identity"] = completion.repair_staged_output(record, data)
            output["failure"] = None
            self._schedule_completion_step(record, "output_publish", "publishing_output")
        except Exception as error:
            logger.error("Completion output repair failed lode=%s: %s", lode_id, error)
            acknowledge(False, "repair_failed")
            return
        acknowledge(True, "accepted")

    def _reconcile_completion_records(self) -> None:
        """Load durable fences before ordinary startup reconciliation."""
        self._startup_completion_actions = []
        seen = set()
        for lode in [*self.lodes, *self.archived_lodes]:
            if lode["id"] in seen:
                continue
            seen.add(lode["id"])
            if not _pending_completion_exists(lode["id"]):
                try:
                    completion.collect_orphaned_staging(lode["id"], None)
                except ValueError:
                    pass
                continue
            try:
                record = self._load_pending(lode["id"])
            except (OSError, ValueError, json.JSONDecodeError) as error:
                lode["state"] = "teardown"
                lode["status"] = f"Teardown blocked: invalid durable completion record: {error}"
                lode["active"] = False
                touch(lode)
                _log_state_change(
                    lode["id"], lode["state"], lode["status"], "completion_startup_invalid"
                )
                continue
            if record is None:
                continue
            self._cancel_generation_guard(lode["id"], record["run_generation"])
            if record["markers"]["pending_clear"]["state"] == "done":
                self._clear_completed_action(record)
                continue
            lode["state"] = "teardown"
            lode["status"] = completion.completion_status(record)
            lode["active"] = False
            touch(lode)
            _log_state_change(
                lode["id"], lode["state"], lode["status"], "completion_startup_reconcile"
            )
            completion.collect_orphaned_staging(lode["id"], record)
            self._startup_completion_actions.append(lode["id"])
        save_lodes(self.lodes)
        save_archived_lodes(self.archived_lodes)

    def _resume_completion(self, lode_id: str) -> None:
        try:
            record = self._load_pending(lode_id)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            logger.error("Completion startup reconciliation blocked lode=%s: %s", lode_id, error)
            return
        if record is None or record["phase"] in {
            "output_blocked",
            "containment_blocked",
            "ship_blocked",
            "cleanup_blocked",
        }:
            return
        if record["markers"]["output_publish"]["state"] != "done":
            selected = ("output_publish", "publishing_output")
        elif record["markers"]["ownership_capture"]["state"] != "done":
            selected = ("ownership_capture", "capturing_ownership")
        elif record["markers"]["pane_close"]["state"] != "done":
            selected = ("pane_close", "closing_pane")
        elif record["markers"]["containment"]["state"] != "done":
            phase = (
                "force_killing"
                if record["containment"]["state"] in {"kill_pending", "verify_after_kill"}
                else "observing_containment"
            )
            selected = ("containment", phase)
        else:
            if record["markers"]["pending_clear"]["state"] == "done":
                self._clear_completed_action(record)
                return
            self._continue_completion(record)
            return
        if record["markers"][selected[0]]["state"] != "blocked":
            self._schedule_completion_step(record, *selected)

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
            if _pending_completion_exists(lode["id"]):
                continue
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
                if _pending_completion_exists(lode["id"]):
                    continue
                if lode.get("oom_scope") and not is_terminal_failure_kind(lode.get("failure_kind")):
                    self._set_terminal_failure(
                        lode,
                        "runner_exit_unverified",
                        lode.get("run_generation"),
                        broadcast=False,
                    )
            return
        for lode in self.lodes:
            if _pending_completion_exists(lode["id"]):
                continue
            unit_name = lode.get("oom_scope")
            run_generation = lode.get("run_generation")
            if not unit_name or not run_generation:
                continue
            unit_result = oom.read_scope_result(systemctl, unit_name)
            if unit_result == "oom-kill":
                self._set_terminal_failure(lode, "oom", run_generation, broadcast=False)
            elif is_terminal_failure_kind(lode.get("failure_kind")):
                if unit_result not in (None, "success"):
                    oom.release_scope(systemctl, unit_name)
                continue
            elif unit_result not in (None, "success"):
                self._set_terminal_failure(
                    lode, "runner_exit_unverified", run_generation, broadcast=False
                )
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
        if generation and self._generation_is_fenced(lode["id"], generation):
            logger.warning("lode %s: spawn suppressed by accepted completion", lode["id"])
            return SpawnOutcome.FAILED, None
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
            if "state" in updates:
                _log_state_change(lode["id"], updates["state"], updates.get("status", ""), "spawn")
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

        self._reconcile_completion_records()
        self._consume_failed_oom_units()
        self._reconcile_startup_lodes()

        # Runs only while lock-held; UNKNOWN panes do not block shipped auto-archive.
        shipped = [
            lode
            for lode in self.lodes
            if lode.get("stage") == "shipped"
            and not _pending_completion_exists(lode["id"])
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

            for lode_id in self._startup_completion_actions:
                self._enqueue_event({"type": "_completion_reconcile", "lode_id": lode_id})

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
        if self._generation_is_fenced(lode_id, run_generation):
            self._cancel_generation_guard(lode_id, run_generation)
            lode["active"] = False
            touch(lode)
            save_lodes(self.lodes)
            _log_state_change(
                lode_id,
                lode.get("state", "teardown"),
                lode.get("status", ""),
                "expected_teardown_disconnect",
            )
            self.broadcast({"type": "lode_updated", "lode": lode})
            self._enqueue_event({"type": "_completion_reconcile", "lode_id": lode_id})
            return
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

    def _set_terminal_failure(
        self,
        lode: dict,
        failure_kind: str,
        run_generation: str | None,
        *,
        broadcast: bool = True,
    ) -> bool:
        """Persist one canonical terminal runner failure."""
        if self._generation_is_fenced(lode["id"], run_generation):
            logger.info(
                "Ignoring terminal failure for expected teardown lode=%s generation=%s",
                lode["id"],
                run_generation,
            )
            return False
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
        if changed:
            _log_state_change(lode["id"], "error", status, f"terminal_failure:{failure_kind}")
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
            if self._generation_is_fenced(lode_id, run_generation):
                self.runner_results.pop(key, None)
                continue
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
                self._set_terminal_failure(lode, "runner_exit_unverified", run_generation)

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
            _log_state_change(lode_id, "running", lode["status"], "register_recovery")
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

        if self._generation_is_fenced(lode_id, run_generation):
            self._cancel_generation_guard(lode_id, run_generation)
            acknowledge(True, False, "expected-teardown")
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
            self._set_terminal_failure(lode, "oom", run_generation)
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
                self._set_terminal_failure(lode, "runner_exit_unverified", run_generation)
            acknowledge(True, durable, "success")
            return

        self.pending_disconnects.pop(key, None)
        self.runner_results.pop(key, None)
        if lode.get("state") == "error" and not lode.get("failure_kind"):
            self._finalize_lode_disconnect(lode_id, run_generation)
            disposition = "runner-error"
        else:
            self._set_terminal_failure(lode, "runner_exit_unverified", run_generation)
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

        lode_id = message.get("lode_id")
        if (
            msg_type == "lode_set_state"
            and message.get("state") == "teardown"
            and (not isinstance(lode_id, str) or not _pending_completion_exists(lode_id))
        ):
            logger.warning(
                "Refusing teardown projection without pending completion lode=%s", lode_id
            )
            acknowledge_mutation(False, "teardown_requires_pending_completion")
            return
        if (
            msg_type in PENDING_ACTION_FENCED_MUTATIONS
            and isinstance(lode_id, str)
            and _pending_completion_exists(lode_id)
        ):
            logger.info(
                "Refusing manual mutation during pending completion type=%s lode=%s",
                msg_type,
                lode_id,
            )
            if conn:
                self._send_response(
                    conn,
                    {
                        "type": "error",
                        "error": "completion teardown is pending; use hop lode restart "
                        f"{lode_id} to retry its blocked phase",
                    },
                )
            return

        if (
            msg_type == "lode_reset_claude_stage"
            and isinstance(lode_id, str)
            and _pending_completion_exists(lode_id)
        ):
            acknowledge_mutation(False, "completion_pending")
            return

        if msg_type in RUNNER_MUTATION_TYPES and msg_type != "lode_complete":
            lode = self._find_lode(lode_id) if lode_id else None
            if not self._runner_generation_matches(lode, message):
                if msg_type in {"lode_register", "lode_supervisor_register"} and conn and lode_id:
                    response_type = (
                        "lode_register_refused"
                        if msg_type == "lode_register"
                        else "lode_supervisor_register_refused"
                    )
                    self._send_response(
                        conn,
                        {"type": response_type, "lode_id": lode_id, "accepted": False},
                    )
                if not lode:
                    reason = "lode_not_found"
                elif not message.get("run_generation"):
                    reason = "missing_run_generation"
                else:
                    reason = "stale_run_generation"
                acknowledge_mutation(False, reason)
                return
            if self._generation_is_fenced(lode_id, message.get("run_generation")):
                logger.info(
                    "Dropping runner mutation for expected teardown type=%s lode=%s",
                    msg_type,
                    lode_id,
                )
                if msg_type in {"lode_register", "lode_supervisor_register"} and conn:
                    response_type = (
                        "lode_register_refused"
                        if msg_type == "lode_register"
                        else "lode_supervisor_register_refused"
                    )
                    self._send_response(
                        conn,
                        {
                            "type": response_type,
                            "lode_id": lode_id,
                            "accepted": False,
                            "reason": "expected_teardown",
                        },
                    )
                acknowledge_mutation(False, "expected_teardown")
                return
            if msg_type not in {
                "lode_register",
                "lode_supervisor_register",
            } and is_terminal_failure_kind(lode.get("failure_kind")):
                logger.info(
                    "Dropping runner mutation for terminal lode type=%s lode=%s",
                    msg_type,
                    lode_id,
                )
                acknowledge_mutation(False, "terminal_failure")
                return
            if msg_type not in {"lode_register", "lode_supervisor_register", "lode_set_state"}:
                acknowledge_mutation(True, "accepted")

        if msg_type == "_client_disconnect":
            self._on_client_disconnect(conn)

        elif msg_type == "_registration_capture_result":
            self._handle_registration_capture_result(message, conn)

        elif msg_type == "_completion_acceptance_result":
            self._handle_completion_acceptance_result(message, conn)

        elif msg_type == "_completion_step_result":
            self._handle_completion_step_result(message)

        elif msg_type == "_completion_reconcile":
            lode_id = message.get("lode_id")
            if isinstance(lode_id, str):
                self._resume_completion(lode_id)

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

        elif msg_type == "lode_supervisor_register":
            lode_id = message.get("lode_id")
            lode = self._find_lode(lode_id) if lode_id else None
            if lode and self._adopt_completion_spawn_receipt(lode, message):
                self._start_registration_capture("supervisor", lode, message, conn)
            elif lode and conn:
                self._send_response(
                    conn,
                    {
                        "type": "lode_supervisor_register_refused",
                        "lode_id": lode_id,
                        "accepted": False,
                        "reason": "spawn_receipt_unavailable",
                    },
                )

        elif msg_type == "lode_register":
            lode_id = message.get("lode_id")
            if lode_id:
                lode = self._find_lode(lode_id)
                if lode and self._adopt_completion_spawn_receipt(lode, message):
                    self._start_registration_capture("worker", lode, message, conn)
                elif lode and conn:
                    self._send_response(
                        conn,
                        {
                            "type": "lode_register_refused",
                            "lode_id": lode_id,
                            "accepted": False,
                            "reason": "spawn_receipt_unavailable",
                        },
                    )

        elif msg_type == "lode_complete":
            self._handle_lode_complete(message, conn)

        elif msg_type == "lode_run_result":
            self._handle_lode_run_result(message, conn)

        elif msg_type == "lode_retry_completion":
            lode_id = message.get("lode_id")
            if isinstance(lode_id, str):
                self._retry_completion(lode_id, conn)

        elif msg_type == "lode_repair_output":
            self._repair_completion_output(message, conn)

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
            _log_state_change(lode_id, "paused", lode["status"], "pause")
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
                    _log_state_change(lode_id, "error", "Killed by user", "kill")
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
                    _log_state_change(lode_id, state, status, "lode_set_state")
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
                _log_state_change(lode_id, state, status, "feedback")
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

        for fd in set(self.supervisor_pidfds.values()):
            try:
                os.close(fd)
            except OSError:
                pass
        self.supervisor_pidfds.clear()

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
