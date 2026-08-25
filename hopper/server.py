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
import re
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

from hopper import actions, config, git, oom, teardown
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
from hopper.client import (
    OOM_SCOPE_ENV,
    RUN_GENERATION_ENV,
    RUNNER_MUTATION_TYPES,
)
from hopper.coder import (
    CODER_PROVIDERS,
    coder_check,
    coder_unavailable_message,
    validate_coder_provider,
)
from hopper.driver import (
    RUNNABLE_STAGE_DRIVERS,
    STAGE_DRIVER_CAPABILITIES_KEY,
    STAGE_DRIVER_PROTOCOL_VERSION,
    resolve_driver,
)
from hopper.git import branch_exists, delete_branch, is_dirty, remove_worktree
from hopper.lodes import (
    REFUSAL_STATUS_PREFIXES,
    archive_lode,
    archive_lode_for_action,
    begin_lode_gate_delivery,
    bind_lode_stage_session,
    clear_lode_gate,
    create_lode,
    current_time_ms,
    find_lodes_by_prefix,
    format_refusal_status,
    format_terminal_failure_status,
    format_worktree_reaped_status,
    get_worktree_dir,
    is_terminal_failure_kind,
    load_archived_lodes,
    load_lodes,
    lode_coder,
    lode_driver,
    lode_gate,
    lode_stage_session,
    publish_lode_gate,
    reserve_lode_id,
    reset_lode_claude_stage,
    resolve_worktree_path,
    save_archived_lodes,
    save_lodes,
    set_lode_claude_started,
    set_lode_gate_fields,
    stop_lode_runtime,
    touch,
    unarchive_lode,
    update_lode_branch,
    update_lode_coder_session,
    update_lode_codex_thread,
    update_lode_stage,
    update_lode_state,
    update_lode_status,
    update_lode_title,
    update_lode_worktree_path,
    validate_lode_coder_data,
    validate_lode_driver_data,
    write_lode_gate_artifact,
)
from hopper.process import STAGES
from hopper.projects import Project, disabled_project_message, find_project, get_active_projects
from hopper.supervisor import (
    supervisor_check,
    supervisor_unavailable_message,
    validate_supervisor_provider,
)
from hopper.tmux import (
    KeyboardOwnership,
    Liveness,
    PanePhase,
    WindowSpawnOutcome,
    capture_pane,
    classify_pane_phase,
    completion_action_panes,
    pane_answer_choices,
    pane_answer_identity,
    pane_identity,
    pane_keyboard_ownership,
    pane_liveness,
    pane_surface_readable,
    pane_title,
    paste_buffer,
    read_pane_input,
    send_keys,
    spawn_lode_processor,
)

# Retained module aliases for the established Claude parser test/diagnostic surface.
_CLAUDE_PANE_EXPORTS = (
    classify_pane_phase,
    pane_answer_choices,
    pane_answer_identity,
    pane_keyboard_ownership,
    pane_surface_readable,
    read_pane_input,
)

logger = logging.getLogger(__name__)

_CURRENT_EXCHANGE = object()

PROGRESS_REJECT_STATES = frozenset({"new", "gated", "ready", "reconnecting", "teardown", "error"})
SUPPORTED_LODE_STATES = frozenset(
    {"new", "running", "stuck", "error", "gated", "ready", "mill", "refine", "ship", "teardown"}
)
HELD_RUNNER_MUTATION_TYPES = RUNNER_MUTATION_TYPES - {
    "lode_register",
    "lode_supervisor_register",
}
LISTEN_BACKLOG = 64
PROCESS_GROUP_STATUS_TIMEOUT_SEC = 1.0
GUARDED_DISCONNECT_HOLD_SEC = 60.0
WORKTREE_REAP_SWEEP_INTERVAL_SEC = 60.0
SHIPPED_WORKTREE_REAP_GRACE_MS = 6 * 60 * 60 * 1000
ERROR_WORKTREE_REAP_GRACE_MS = 48 * 60 * 60 * 1000
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


def _validate_worktree_path_publication(lode: dict, message: dict) -> tuple[str | None, str | None]:
    """Validate runner-owned durable worktree provenance."""
    project = message.get("project")
    if not isinstance(project, str) or not project:
        return "invalid_project", None
    if project != lode.get("project"):
        return "project_mismatch", None

    raw_path = message.get("worktree_path")
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        return "invalid_worktree_path", None
    requested = Path(raw_path)
    if not requested.is_absolute():
        return "worktree_not_absolute", None
    try:
        canonical = requested.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return "worktree_missing", None
    if not canonical.is_dir():
        return "worktree_missing", None

    try:
        root = config.worktree_root().resolve(strict=False)
        canonical.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return "worktree_outside_root", None
    try:
        expected = root / lode["id"]
    except (OSError, RuntimeError, ValueError, KeyError):
        return "worktree_identity_mismatch", None
    if canonical != expected:
        return "worktree_identity_mismatch", None

    recorded = lode.get("worktree_path")
    if recorded is not None and recorded != "" and recorded != str(canonical):
        return "worktree_provenance_conflict", None
    return None, str(canonical)


def _collapse_lode_snapshot_matches(
    matches: list[tuple[dict, bool]],
) -> list[tuple[dict, bool]]:
    """Collapse same-ID storage twins, preferring archived identity."""
    grouped: dict[str, tuple[dict, bool]] = {}
    for lode, archived in matches:
        lode_id = lode["id"]
        if lode_id not in grouped or archived:
            grouped[lode_id] = lode, archived
    return list(grouped.values())


def _is_verified_ordinary_exit(unit_result: str | None, worker_returncode: int | None) -> bool:
    """Return whether the supervisor verified an ordinary successful exit."""
    return unit_result in (None, "success") and worker_returncode == 0


def _pending_action_file_exists(lode_id: str) -> bool:
    """Return whether a schema-addressable lode has a durable completion fence."""
    try:
        return actions.pending_action_path(lode_id).exists()
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
        "schema_version": actions.RUN_OWNERSHIP_SCHEMA_VERSION,
        "lode_id": lode["id"],
        "run_generation": lode["run_generation"],
        "registered_at_ms": actions.accepted_at_ms(),
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
        "linux-strict": {
            oom.OomCapability.SUPPORTED.value,
            oom.OomCapability.DEGRADED_NO_CONTROLLER.value,
            oom.OomCapability.DEGRADED_NO_SCORE.value,
        },
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
    actions.validate_run_ownership(record, require_worker=True)
    if proof_mode == "linux-strict":
        membership = teardown.capture_worker_cgroup_membership(
            record["worker"], record["cgroup"]["relative_path"]
        )
        if membership["state"] != "proven":
            raise RuntimeError(membership["error"] or "worker cgroup membership is unavailable")
    pidfd = None
    if proof_mode == "linux-strict":
        pidfd_interface = teardown.resolve_pidfd_interface()
        if pidfd_interface is not None:
            reopened = teardown.reopen_process_pidfd(
                record["supervisor"], pidfd_interface=pidfd_interface
            )
            if reopened["state"] != "alive":
                raise RuntimeError(
                    reopened["error"] or "outside supervisor exited during registration"
                )
            pidfd = reopened["fd"]
    return {"record": record, "pidfd": pidfd}


_ACCEPTED_DELIVERY_REASONS = frozenset(
    {
        "auto_submitted",
        "character_sent",
        "composer_cleared",
        "enter_accepted",
        "selector_changed",
    }
)
_DELIVERY_FAILURE_OUTCOMES = {
    "pane_unavailable": "pane_unavailable",
    "idle_timeout": "busy",
    "pane_state_unknown": "pane_state_unknown",
    "pane_blocked": "awaiting_operator",
    "pane_character_unsupported": "not_sent",
    "pane_frozen": "pane_frozen",
    "pane_awaiting_choice": "awaiting_choice",
    "pane_not_awaiting_choice": "not_sent",
    "choice_unavailable": "not_sent",
    "choice_requires_text": "not_sent",
    "choice_navigation_failed_unknown": "unverified",
    "choice_navigation_unverified": "unverified",
    "pane_lost_after_choice_navigation": "unverified",
    "choice_submit_failed": "unverified",
    "paste_failed": "not_sent",
    "paste_failed_unknown": "unverified",
    "paste_not_staged": "unverified",
    "pane_lost_after_paste": "unverified",
    "submit_failed": "not_sent",
    "acceptance_timeout": "unverified",
    "pane_lost_after_submit": "unverified",
    "gated_body_refused": "gated_character_only",
    "character_failed": "not_sent",
    "character_failed_unknown": "unverified",
}
_GATE_FEEDBACK_STATUSES = {
    "pane_unavailable": "Feedback blocked: pane unavailable",
    "busy": "Feedback blocked: pane busy",
    "not_sent": "Feedback not sent; gate remains blocked",
    "unverified": "Feedback outcome unknown; inspect pane",
    "pane_state_unknown": "Feedback blocked: pane state unrecognized",
    "pane_frozen": "Feedback blocked: pane appears frozen",
    "awaiting_choice": "Feedback blocked: pane awaiting a numbered choice",
    "awaiting_operator": "Feedback blocked: supervisor awaits operator input",
    "gated_character_only": ("Feedback blocked: gated lode accepts only a single character"),
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
    "pane_blocked": (
        "Feedback was not sent because pane {pane} is showing a supervisor menu, card, or "
        "authentication screen. Nothing was typed. Inspect with `hop lode peek {lode_id}` "
        "and resolve it in the pane."
    ),
    "pane_character_unsupported": (
        "The character was not sent because this lode's supervisor does not support Hopper's "
        "single-character shortcut. Nothing was typed. Inspect pane {pane} and respond there."
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
        "Hopper pressed Enter in pane {pane}, but acceptance could not be verified within "
        f"{_FEEDBACK_ACCEPTANCE_WAIT_SECONDS:.1f}s. "
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
    "gated_body_refused": (
        "Feedback was not sent because lode {lode_id} is gated, and while gated "
        "hop gate feedback only sends a single character. Nothing was pasted or "
        "submitted. Retry with exactly one character, for example: "
        "hop gate feedback {lode_id} y"
    ),
    "character_failed": (
        "The character was not sent because Hopper could not deliver it to pane {pane}. "
        "Nothing was typed or submitted. Retry the same single-character send."
    ),
    "character_failed_unknown": (
        "Hopper could not confirm the character reached pane {pane}. Some input may "
        "have reached the pane. Inspect with `hop lode peek {lode_id}` before retrying; "
        "do not send the character again unless the pane proves it was not accepted."
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
        "`hop lode answer {lode_id} <n>`. Hopper cannot drive the free-text entry "
        '("Type something"); inspect pane {pane} and enter that answer directly.'
    ),
    "pane_blocked": (
        "Input was not sent because pane {pane} is showing a supervisor menu, card, or "
        "authentication screen. Nothing was typed. Inspect with `hop lode peek {lode_id}` "
        "and resolve it in the pane."
    ),
    "pane_character_unsupported": (
        "Input was not sent because this lode's supervisor does not support Hopper's "
        "single-character shortcut. Nothing was typed. Inspect pane {pane} and respond there."
    ),
    "pane_not_awaiting_choice": (
        "Choice was not sent because pane {pane} is not a recognized numbered selector. "
        "Nothing was typed. Inspect with `hop lode peek {lode_id}` before retrying."
    ),
    "choice_unavailable": (
        "Choice was not sent because the requested option is not visible in pane {pane}. "
        "Nothing was typed or submitted. Read the available options with `hop lode peek "
        "{lode_id}`, then retry with one of those numbers."
    ),
    "choice_requires_text": (
        "Choice was not sent because the requested option is the free-text entry in pane "
        "{pane}, not a terminal numbered answer. Nothing was submitted. Hopper cannot drive "
        "that entry; inspect with `hop lode peek {lode_id}` and enter the text directly."
    ),
    "choice_navigation_failed_unknown": (
        "Hopper could not complete choice navigation in pane {pane}. The highlight may have "
        "moved, but Enter was not sent. Inspect with `hop lode peek {lode_id}` before retrying."
    ),
    "choice_navigation_unverified": (
        "Hopper moved the selector in pane {pane}, but could not verify that the requested row "
        "was highlighted. Enter was not sent. Inspect with `hop lode peek {lode_id}` before "
        "retrying."
    ),
    "pane_lost_after_choice_navigation": (
        "Hopper moved the selector in pane {pane}, but the pane became unavailable before the "
        "requested row could be verified. Enter was not sent. Resume and inspect lode "
        "{lode_id} before retrying."
    ),
    "choice_submit_failed": (
        "Hopper verified the requested row in pane {pane}, but could not press Enter. The "
        "choice was not submitted. Inspect with `hop lode peek {lode_id}` before retrying."
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
        "Hopper pressed Enter in pane {pane}, but acceptance could not be verified within "
        f"{_FEEDBACK_ACCEPTANCE_WAIT_SECONDS:.1f}s. "
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
        "gated_body_refused",
    }
)


def _single_character_payload(text: object) -> str | None:
    """Return the character when `text` is a one-character send, else None.

    A trailing CR/LF from stdin or `echo` does not count. Any other surrounding
    whitespace makes this a body, not a character.
    """
    if not isinstance(text, str):
        return None
    stripped = text.rstrip("\r\n")
    if len(stripped) == 1:
        return stripped
    return None


class ServerLockHeld(RuntimeError):
    """Raised when another hopper server holds the socket's singleton lock."""


class SpawnOutcome(Enum):
    """Result of a server-gated runner spawn request."""

    SPAWNED = "spawned"
    ALREADY_LIVE = "already_live"
    PROJECT_MISSING = "project_missing"
    PROVEN_NO_PANE = "proven_no_pane"
    UNKNOWN = "unknown"


def _containment_phase_for_cursor(cursor: str) -> str | None:
    """Map one durable containment cursor to its worker phase."""
    if cursor in {"not_started", "pane_close_pending", "grace"}:
        return "observing_containment"
    if cursor in {"kill_pending", "verify_after_kill"}:
        return "force_killing"
    return None


def _clear_spawn_refusal(lode: dict, *, clear_status: bool = True) -> bool:
    """Clear a refusal or failure after live runner evidence supersedes it."""
    changed = lode.get("spawn_disposition") is not None
    lode["spawn_disposition"] = None
    status = lode.get("status", "")
    if not clear_status or not status.startswith(REFUSAL_STATUS_PREFIXES[:2]):
        return changed
    lode["status"] = ""
    return True


def _persist_protocol_error(lode: dict, message: str) -> None:
    """Persist one visible protocol refusal that stale running updates cannot erase."""
    lode["protocol_error"] = message
    lode["status"] = format_refusal_status("protocol", message)
    touch(lode)


def _sync_gate_artifact(lode: dict) -> bool:
    """Refresh the derived gate artifact after its durable authority is saved."""
    try:
        write_lode_gate_artifact(lode)
    except OSError as error:
        # gate.md is deliberately non-authoritative. Its failure cannot make a
        # caller read stale gate text because every consumer reads gate_body.
        logger.warning("Could not write derived gate artifact lode=%s: %s", lode["id"], error)
        return False
    return True


def _lifecycle_grace_pending(lode: dict | None) -> bool:
    """Return whether worker mutations are held for registration authority."""
    return bool(
        lode
        and lode.get("active") is False
        and lode.get("pending_action") is None
        and lode.get("state") in {"new", "ready", "reconnecting"}
    )


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


def _observe_pane_acceptance(
    pane_id: str,
    latest_capture: str,
    observed_title: str | None,
    acceptance_evidence: tuple[str, object],
    *,
    driver_name: str = "claude",
) -> dict:
    """Verify that one submitted input started a new processing turn."""
    driver = resolve_driver(driver_name)
    evidence_kind, pre_enter_evidence = acceptance_evidence
    evidence_observed = False
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
        phase, _keyboard = driver.observe_pane(observed_title, latest_capture)
        if phase is PanePhase.BUSY:
            return {
                "reason": "enter_accepted",
                "capture": latest_capture,
                "title": observed_title,
            }
        if evidence_kind == "selector":
            selector_identity = driver.pane_blocked_identity(latest_capture)
            consumed = (
                bool(latest_capture.strip())
                and driver.pane_surface_readable(latest_capture)
                and (
                    (selector_identity is not None and selector_identity != pre_enter_evidence)
                    or (
                        selector_identity is None
                        and driver.read_pane_input(latest_capture) is not None
                    )
                )
            )
            accepted_reason = "selector_changed"
        else:
            consumed = (
                bool(latest_capture.strip())
                and driver.pane_surface_readable(latest_capture)
                and driver.read_pane_input(latest_capture) != pre_enter_evidence
            )
            accepted_reason = "composer_cleared"
        if consumed and evidence_observed:
            return {
                "reason": accepted_reason,
                "capture": latest_capture,
                "title": observed_title,
            }
        evidence_observed = consumed
    return {
        "reason": "acceptance_timeout",
        "capture": latest_capture,
        "title": observed_title,
    }


def _attempt_pane_delivery(
    pane_id: str | None,
    text: str,
    *,
    paste: bool,
    pane_title_observation: dict | None = None,
    driver_name: str = "claude",
) -> dict:
    """Attempt one pane delivery and return its reason and latest observations."""
    driver = resolve_driver(driver_name)
    observed_title = None
    if not pane_id:
        return {"reason": "pane_unavailable", "capture": None, "title": observed_title}

    latest_capture = capture_pane(pane_id, plain=True)
    if latest_capture is None:
        return {"reason": "pane_unavailable", "capture": None, "title": observed_title}

    pre_delivery_input = None
    answer_state = None
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
        phase, keyboard = driver.observe_pane(observed_title, latest_capture)
        if phase is PanePhase.BUSY:
            saw_processing = True
            processing_frozen = _observe_processing_pane_title(
                observed_title,
                pane_title_observation,
            )
        elif phase is PanePhase.IDLE:
            if pane_title_observation is not None:
                pane_title_observation.clear()
            if paste and keyboard is KeyboardOwnership.NUMBERED_CHOICE:
                # A numbered selector is not a text composer. Pasting free text into
                # one stages it with nothing able to submit it, and each retry appends
                # to what is already there -- so refuse before touching the pane and
                # name the verb that does work.
                return {
                    "reason": "pane_awaiting_choice",
                    "capture": latest_capture,
                    "title": observed_title,
                }
            if paste and keyboard is not KeyboardOwnership.COMPOSER:
                return {
                    "reason": "pane_state_unknown",
                    "capture": latest_capture,
                    "title": observed_title,
                }
            if not paste:
                if keyboard is KeyboardOwnership.UNKNOWN:
                    return {
                        "reason": "pane_state_unknown",
                        "capture": latest_capture,
                        "title": observed_title,
                    }
                answer_state = driver.pane_answer_choices(latest_capture)
                if answer_state is None:
                    return {
                        "reason": "pane_not_awaiting_choice",
                        "capture": latest_capture,
                        "title": observed_title,
                    }
            pre_delivery_input = driver.read_pane_input(latest_capture)
            break
        elif (
            phase is PanePhase.BLOCKED
            and driver_name == "claude"
            and keyboard is KeyboardOwnership.NUMBERED_CHOICE
        ):
            if paste:
                return {
                    "reason": "pane_awaiting_choice",
                    "capture": latest_capture,
                    "title": observed_title,
                }
            answer_state = driver.pane_answer_choices(latest_capture)
            if answer_state is None:
                return {
                    "reason": "pane_not_awaiting_choice",
                    "capture": latest_capture,
                    "title": observed_title,
                }
            break
        elif phase in {PanePhase.BLOCKED, PanePhase.AUTH}:
            return {
                "reason": "pane_blocked",
                "capture": latest_capture,
                "title": observed_title,
            }
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

    if not paste:
        try:
            target_choice = int(text)
        except (TypeError, ValueError):
            target_choice = -1
        selected_choice, visible_choices, free_text_choices = answer_state
        if target_choice not in visible_choices:
            return {
                "reason": "choice_unavailable",
                "capture": latest_capture,
                "title": observed_title,
            }
        if target_choice in free_text_choices:
            return {
                "reason": "choice_requires_text",
                "capture": latest_capture,
                "title": observed_title,
            }

        selected_position = visible_choices.index(selected_choice)
        target_position = visible_choices.index(target_choice)
        direction = "Down" if target_position > selected_position else "Up"
        for _ in range(abs(target_position - selected_position)):
            if not send_keys(pane_id, direction):
                return {
                    "reason": "choice_navigation_failed_unknown",
                    "capture": latest_capture,
                    "title": observed_title,
                }

        time.sleep(_FEEDBACK_POLL_INTERVAL)
        capture = capture_pane(pane_id, plain=True)
        if capture is None:
            return {
                "reason": "pane_lost_after_choice_navigation",
                "capture": latest_capture,
                "title": observed_title,
            }
        latest_capture = capture
        moved_state = driver.pane_answer_choices(latest_capture)
        if moved_state is None or moved_state[0] != target_choice:
            return {
                "reason": "choice_navigation_unverified",
                "capture": latest_capture,
                "title": observed_title,
            }
        selector_identity = driver.pane_blocked_identity(latest_capture)
        assert selector_identity is not None
        if not send_keys(pane_id, "Enter"):
            return {
                "reason": "choice_submit_failed",
                "capture": latest_capture,
                "title": observed_title,
            }
        return _observe_pane_acceptance(
            pane_id,
            latest_capture,
            observed_title,
            ("selector", selector_identity),
            driver_name=driver_name,
        )

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
        post_delivery_input = driver.read_pane_input(latest_capture)
        reason = (
            "paste_failed_unknown"
            if pre_delivery_input is None or post_delivery_input != pre_delivery_input
            else "paste_failed"
        )
        return {"reason": reason, "capture": latest_capture, "title": observed_title}

    staged_input = None
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
        phase, _keyboard = driver.observe_pane(observed_title, latest_capture)
        if phase is PanePhase.BUSY:
            return {
                "reason": "auto_submitted",
                "capture": latest_capture,
                "title": observed_title,
            }
        post_delivery_input = driver.read_pane_input(latest_capture)
        if phase is PanePhase.IDLE and post_delivery_input:
            staged_input = post_delivery_input
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

    assert staged_input is not None
    return _observe_pane_acceptance(
        pane_id,
        latest_capture,
        observed_title,
        ("composer", staged_input),
        driver_name=driver_name,
    )


def _attempt_character_delivery(
    pane_id: str | None,
    char: str,
    *,
    pane_title_observation: dict | None = None,
    driver_name: str = "claude",
) -> dict:
    """Send one character without waiting for the pane to go idle."""
    driver = resolve_driver(driver_name)
    observed_title = None
    if not pane_id:
        return {"reason": "pane_unavailable", "capture": None, "title": observed_title}

    latest_capture = capture_pane(pane_id, plain=True)
    if latest_capture is None:
        return {"reason": "pane_unavailable", "capture": None, "title": observed_title}

    if driver_name != "claude":
        return {
            "reason": "pane_character_unsupported",
            "capture": latest_capture,
            "title": pane_title(pane_id),
        }

    observed_title = pane_title(pane_id)
    phase, keyboard = driver.observe_pane(observed_title, latest_capture)
    if phase is PanePhase.UNKNOWN:
        return {
            "reason": "pane_state_unknown",
            "capture": latest_capture,
            "title": observed_title,
        }
    if phase is PanePhase.IDLE:
        if keyboard is KeyboardOwnership.NUMBERED_CHOICE:
            return {
                "reason": "pane_awaiting_choice",
                "capture": latest_capture,
                "title": observed_title,
            }
        if keyboard is KeyboardOwnership.UNKNOWN:
            return {
                "reason": "pane_state_unknown",
                "capture": latest_capture,
                "title": observed_title,
            }
    if phase is PanePhase.BUSY:
        if _observe_processing_pane_title(observed_title, pane_title_observation):
            return {
                "reason": "pane_frozen",
                "capture": latest_capture,
                "title": observed_title,
            }
    elif pane_title_observation is not None:
        pane_title_observation.clear()

    if not send_keys(pane_id, char, literal=True):
        capture = capture_pane(pane_id, plain=True)
        if capture is None:
            return {
                "reason": "character_failed_unknown",
                "capture": latest_capture,
                "title": observed_title,
            }
        return {
            "reason": "character_failed",
            "capture": capture,
            "title": observed_title,
        }

    if phase is PanePhase.BUSY:
        return {
            "reason": "character_sent",
            "capture": latest_capture,
            "title": observed_title,
        }

    staged_input = None
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
        settle_phase, _keyboard = driver.observe_pane(observed_title, latest_capture)
        if settle_phase is PanePhase.BUSY:
            return {
                "reason": "auto_submitted",
                "capture": latest_capture,
                "title": observed_title,
            }
        post_delivery_input = driver.read_pane_input(latest_capture)
        if settle_phase is PanePhase.IDLE and post_delivery_input:
            staged_input = post_delivery_input
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

    assert staged_input is not None
    return _observe_pane_acceptance(
        pane_id,
        latest_capture,
        observed_title,
        ("composer", staged_input),
        driver_name=driver_name,
    )


def _deliver_pane_input(
    lode_id: str,
    pane_id: str | None,
    text: str,
    *,
    paste: bool,
    character: bool = False,
    pane_title_observation: dict | None = None,
    driver_name: str = "claude",
) -> dict:
    """Deliver pane input and emit exactly one outcome record."""
    try:
        if character:
            result = _attempt_character_delivery(
                pane_id,
                text,
                pane_title_observation=pane_title_observation,
                driver_name=driver_name,
            )
        else:
            result = _attempt_pane_delivery(
                pane_id,
                text,
                paste=paste,
                pane_title_observation=pane_title_observation,
                driver_name=driver_name,
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


def _deliver_lode_pane_input(
    lodes: list[dict],
    lode: dict,
    text: str,
    *,
    paste: bool,
    character: bool = False,
) -> dict:
    """Deliver input and persist only cross-attempt pane-title evidence."""
    prior_observation = lode.get("pane_title_observation")
    observation = dict(prior_observation) if isinstance(prior_observation, dict) else {}
    result = _deliver_pane_input(
        lode["id"],
        lode.get("tmux_pane"),
        text,
        paste=paste,
        character=character,
        pane_title_observation=observation,
        driver_name=lode_driver(lode),
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


def _page_bound(value: object, *, default: int | None) -> int | None:
    """Read one non-negative page bound off the wire, or fall back to a default.

    A bound is either absent or trustworthy: `True` is an `int` in Python and a
    float is not a row count, so both are rejected rather than coerced. A
    rejected bound falls back to the default, never to a silently different
    page.
    """
    if type(value) is not int or value < 0:
        return default
    return value


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
        self._last_worktree_reap_sweep_at: float | None = None
        self.runner_results: dict[tuple[str, str], tuple[str | None, int]] = {}
        self.action_threads: dict[tuple[str, str], threading.Thread] = {}
        self.registration_threads: dict[str, threading.Thread] = {}
        self.action_acceptances: dict[
            str,
            tuple[
                str,
                tuple[str, str | None, str, str, bool],
                list[tuple[socket.socket, str | None]],
            ],
        ] = {}
        self.action_waiters: dict[str, list[tuple[socket.socket, str | None]]] = {}
        self.supervisor_pidfds: dict[tuple[str, str], int] = {}
        self.cgroup_fds: dict[tuple[str, str], int] = {}
        self.pane_root_pidfds: dict[tuple[str, str], int] = {}
        self.absent_cgroups: set[tuple[str, str]] = set()
        self._startup_actions: list[str] = []
        self._log_handler: logging.FileHandler | None = None
        self._lock_file = None
        self._socket_bound = False
        self.ready = threading.Event()
        self.startup_error: Exception | None = None

    def _find_lode(self, lode_id: str) -> dict | None:
        """Find a lode by ID."""
        return next((lode for lode in self.lodes if lode["id"] == lode_id), None)

    def _find_action_lode(self, lode_id: str) -> dict | None:
        """Find the active or archived object owned by a pending action."""
        return self._find_lode(lode_id) or next(
            (lode for lode in self.archived_lodes if lode["id"] == lode_id), None
        )

    @staticmethod
    def _containment_handle_key(record: dict) -> tuple[str, str]:
        return record["lode_id"], record["expected_generation"]

    def _close_containment_handles(self, record_or_key: dict | tuple[str, str]) -> None:
        """Close every retained descriptor for one accepted generation."""
        key = (
            self._containment_handle_key(record_or_key)
            if isinstance(record_or_key, dict)
            else record_or_key
        )
        descriptors = set()
        for handles in (
            self.supervisor_pidfds,
            self.cgroup_fds,
            self.pane_root_pidfds,
        ):
            fd = handles.pop(key, None)
            if fd is not None:
                descriptors.add(fd)
        self.absent_cgroups.discard(key)
        for fd in descriptors:
            try:
                os.close(fd)
            except OSError:
                pass

    @staticmethod
    def _close_action_result_handles(result: dict) -> None:
        """Close descriptors returned by a worker result that cannot be adopted."""
        descriptors = {
            result[name]
            for name in ("pidfd", "cgroup_fd", "pane_root_pidfd")
            if result.get(f"{name}_owned") and isinstance(result.get(name), int)
        }
        for fd in descriptors:
            try:
                os.close(fd)
            except OSError:
                pass

    @classmethod
    def _close_dropped_event_handles(cls, message: dict) -> None:
        """Release worker-owned descriptors when an internal result cannot be consumed."""
        result = message.get("result")
        if not isinstance(result, dict):
            return
        if message.get("type") == "_action_step_result":
            cls._close_action_result_handles(result)
        elif message.get("type") == "_registration_capture_result":
            fd = result.get("pidfd")
            if isinstance(fd, int):
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _adopt_action_result_handles(self, record: dict, result: dict) -> None:
        """Install newly opened descriptors, closing superseded handles once."""
        key = self._containment_handle_key(record)
        for name, handles in (
            ("pidfd", self.supervisor_pidfds),
            ("cgroup_fd", self.cgroup_fds),
            ("pane_root_pidfd", self.pane_root_pidfds),
        ):
            fd = result.get(name)
            if not isinstance(fd, int):
                continue
            prior = handles.get(key)
            if result.get(f"{name}_owned") and prior is not None and prior != fd:
                try:
                    os.close(prior)
                except OSError:
                    pass
            handles[key] = fd
        if result.get("cgroup_absent"):
            self.absent_cgroups.add(key)

    @staticmethod
    def _action_spawn_target_id(record: dict) -> str | None:
        if record["action_type"] == "restart" or record["stage"] != "ship":
            return record["lode_id"]
        return record["ship"]["backlog"]["promoted_lode_id"]

    def _pending_spawn_for(self, lode_id: str, generation: str) -> dict | None:
        """Find the sole pending action whose receipt owns this new runner."""
        matches = []
        seen = set()
        for source in [*self.lodes, *self.archived_lodes]:
            source_id = source["id"]
            if source_id in seen or not _pending_action_file_exists(source_id):
                continue
            seen.add(source_id)
            try:
                record = self._load_action_slot(source_id)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if (
                record
                and record["spawn"] is not None
                and record["markers"]["spawn"]["state"] == "intent"
                and self._action_spawn_target_id(record) == lode_id
                and record["spawn"]["target_generation"] == generation
            ):
                matches.append(record)
        return matches[0] if len(matches) == 1 else None

    def _adopt_action_spawn_receipt(self, lode: dict, message: dict) -> bool:
        """Adopt only the pane named by the action's fsynced bootstrap receipt."""
        generation = message.get("run_generation")
        if not isinstance(generation, str):
            return False
        record = self._pending_spawn_for(lode["id"], generation)
        if record is None:
            return True
        try:
            receipt = actions.load_spawn_receipt(record["lode_id"], record["action_id"])
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
            "action_spawn_receipt_adoption",
        )
        self._persist_action(record, via="action_spawn:receipt_adopted")
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

    def _load_action_slot(self, lode_id: str) -> dict | None:
        """Load the canonical teardown fence, propagating malformed records."""
        return actions.load_pending_action(lode_id)

    def _lode_has_pending_action(self, lode_id: str) -> bool:
        """Fence every ordinary spawn while the lode owns a durable action."""
        return _pending_action_file_exists(lode_id)

    def _generation_has_teardown_intent(self, lode_id: str, run_generation: str | None) -> bool:
        """Classify only the pending action's source generation as teardown."""
        if not _pending_action_file_exists(lode_id):
            return False
        try:
            record = self._load_action_slot(lode_id)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            logger.error("Invalid action fence lode=%s: %s", lode_id, error)
            return True
        return bool(record and record["expected_generation"] == run_generation)

    def _project_action(
        self,
        record: dict,
        *,
        via: str,
        active: bool | None = None,
    ) -> None:
        """Persist the sole pending record's display projection."""
        lode = self._find_action_lode(record["lode_id"])
        if not lode:
            return
        lode["state"] = "teardown"
        lode["status"] = actions.action_status(record)
        lode["pending_action"] = actions.pending_action_projection(record)
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
                    source = actions.load_run_ownership(
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
            or self._generation_has_teardown_intent(lode_id, generation)
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
            actions.write_run_ownership(result["record"])
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
                self._close_containment_handles((lode_id, generation))
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
                    result["record"]["proof_mode"],
                    request.get("actual_unit"),
                )
            )
            if not accepted:
                self._close_containment_handles((lode_id, generation))
                if conn:
                    self._send_response(
                        conn,
                        {
                            "type": refused_type,
                            "lode_id": lode_id,
                            "accepted": False,
                            "reason": (
                                "worker registration claim no longer matches the current generation"
                            ),
                        },
                    )
                return
            logger.info(
                "Worker registration accepted lode=%s generation=%s containment=%s oom_mode=%s",
                lode_id,
                generation,
                result["record"]["proof_mode"],
                request.get("armed_mode"),
            )
        elif result["record"]["proof_mode"] == "linux-degraded":
            lode["oom_scope"] = None
            touch(lode)
            save_lodes(self.lodes)
            status = result["record"]["degraded_reason"]
            _log_state_change(lode_id, lode.get("state", ""), status, "supervisor_capture_degraded")
            self.broadcast({"type": "lode_updated", "lode": lode})

        self._record_action_spawn_adoption(lode_id, generation, kind)

        if conn:
            self._send_response(
                conn,
                {"type": response_type, "lode_id": lode_id, "accepted": True},
            )

    def _record_action_spawn_adoption(self, lode_id: str, generation: str, kind: str) -> None:
        """Persist supervisor/worker adoption for an action-scoped spawn."""
        record = self._pending_spawn_for(lode_id, generation)
        if record is None:
            return
        field = "supervisor_adopted" if kind == "supervisor" else "worker_adopted"
        record["spawn"][field] = True
        marker = record["markers"]["spawn"]
        if record["spawn"]["supervisor_adopted"] and record["spawn"]["worker_adopted"]:
            actions.transition_marker(
                record, "spawn", "done", attempt_id=marker["attempt_id"], detail="runner adopted"
            )
            self._persist_action(record, via="action_result:spawn_adopted")
            self._continue_action(record)
        else:
            self._persist_action(record, via=f"action_spawn:{kind}_adopted")

    def _send_action_ack(
        self,
        conn: socket.socket | None,
        *,
        outcome: str,
        reason: str,
        action_id: str | None = None,
        action_type: str | None = None,
        disposition: str | None = None,
        detail: str | None = None,
        request: dict | None = None,
        record: dict | None = None,
        receipt: dict | None = None,
    ) -> None:
        request = request or getattr(self._request_context, "message", {})
        legacy_action = {
            "lode_complete": ("completion", None),
            "lode_archive": ("archive", "archived"),
            "lode_pause": ("pause", "paused"),
            "lode_kill": ("kill", "killed_archived"),
            "lode_reset_claude_stage": ("restart", "replacement_spawned"),
        }.get(request.get("type"))
        if legacy_action is not None:
            action_type = action_type or legacy_action[0]
            request = {
                **request,
                "action_type": action_type,
                "target_disposition": legacy_action[1],
                "expected_generation": request.get(
                    "expected_generation", request.get("run_generation")
                ),
                "force_consent": request.get("force", False),
            }
        lode_id = request.get("lode_id")
        owner = None
        if isinstance(lode_id, str):
            lode = self._find_action_lode(lode_id)
            if lode and isinstance(lode.get("pending_action"), dict):
                owner = lode["pending_action"]
            elif lode and _pending_action_file_exists(lode_id):
                try:
                    pending = self._load_action_slot(lode_id)
                except (OSError, ValueError, json.JSONDecodeError):
                    pending = None
                if pending is not None:
                    owner = actions.pending_action_projection(pending)
        response = actions.action_ack_projection(
            outcome=outcome,
            reason=reason,
            action_id=action_id or request.get("action_id"),
            lode_id=lode_id,
            expected_generation=request.get("expected_generation", request.get("run_generation")),
            action_type=action_type or request.get("action_type"),
            target_disposition=request.get("target_disposition"),
            force_consent=request.get("force_consent", False),
            disposition=disposition,
            detail=detail,
            record=record,
            receipt=receipt,
            owner=owner,
        )
        response["type"] = "lode_action_ack"
        if outcome == "refused" and conn is None and isinstance(lode_id, str):
            try:
                actions.validate_action_id(response.get("action_id"))
            except (TypeError, ValueError):
                pass
            else:
                self._set_action_refusal(lode_id, response["status"])
        waiters = (
            self.action_waiters.pop(action_id, []) if conn is None and action_id is not None else []
        )
        targets = waiters or (
            [
                (
                    conn,
                    request.get("exchange_id", getattr(self._request_context, "exchange_id", None)),
                )
            ]
            if conn is not None
            else []
        )
        for waiter, exchange_id in targets:
            self._send_response(waiter, dict(response), exchange_id=exchange_id)

    def _send_action_open_result(
        self,
        conn: socket.socket | None,
        action_type: str,
        opened: dict,
        request: dict | None = None,
    ) -> None:
        self._send_action_ack(
            conn,
            outcome=opened["outcome"],
            reason=opened["reason"],
            action_id=opened.get("action_id"),
            action_type=action_type,
            disposition=opened.get("disposition"),
            detail=opened.get("detail"),
            request=request,
            record=opened.get("record"),
            receipt=opened.get("receipt"),
        )

    def _set_action_refusal(self, lode_id: str, status_text: str) -> None:
        """Publish a pre-accept manual refusal without clobbering an action owner."""
        if _pending_action_file_exists(lode_id):
            return
        lode = self._find_action_lode(lode_id)
        if lode is None or lode.get("pending_action") is not None:
            return
        status = format_refusal_status("action", status_text)
        if lode.get("status") == status:
            return
        lode["status"] = status
        touch(lode)
        if any(item is lode for item in self.lodes):
            save_lodes(self.lodes)
        else:
            save_archived_lodes(self.archived_lodes)
        self.broadcast({"type": "lode_updated", "lode": lode})

    @staticmethod
    def _completion_target(stage: str) -> str | None:
        return {
            "mill": "advance_refine",
            "refine": "advance_ship",
            "ship": "shipped_archived",
        }.get(stage)

    def _open_lode_action(
        self,
        *,
        action_type: str,
        message: dict,
        prepared: dict,
    ) -> dict:
        """Validate and durably accept one action without pre-fsync side effects."""
        action_id = message.get("action_id")
        try:
            actions.validate_action_id(action_id)
            binding = actions.action_binding(
                {
                    "lode_id": message.get("lode_id"),
                    "expected_generation": message.get("expected_generation"),
                    "action_type": message.get("action_type"),
                    "target_disposition": message.get("target_disposition"),
                    "force_consent": message.get("force_consent"),
                }
            )
        except (KeyError, TypeError, ValueError) as error:
            return {
                "outcome": "refused",
                "reason": "invalid_action",
                "action_id": action_id if isinstance(action_id, str) else None,
                "detail": str(error),
            }
        if action_type != message["action_type"]:
            return {
                "outcome": "refused",
                "reason": "action_type_mismatch",
                "action_id": action_id,
            }

        lode_id, expected_generation, _, target_disposition, force_consent = binding
        lode = self._find_action_lode(lode_id)
        if lode is None:
            return {"outcome": "refused", "reason": "lode_not_found", "action_id": action_id}

        try:
            receipt = actions.find_action_result(lode, action_id)
        except ValueError as error:
            return {
                "outcome": "refused",
                "reason": "action_result_invalid",
                "action_id": action_id,
                "detail": str(error),
            }
        if receipt is not None:
            if actions.record_binding(receipt) != binding:
                return {
                    "outcome": "refused",
                    "reason": "action_identity_mismatch",
                    "action_id": action_id,
                }
            return {
                "outcome": "idempotent",
                "reason": "already_completed",
                "action_id": action_id,
                "disposition": receipt["terminal_disposition"],
                "receipt": receipt,
            }

        if _pending_action_file_exists(lode_id):
            try:
                pending = self._load_action_slot(lode_id)
            except actions.LegacyPendingActionError as error:
                return {
                    "outcome": "refused",
                    "reason": "legacy_pending_action",
                    "action_id": action_id,
                    "detail": str(error),
                }
            except (OSError, ValueError, json.JSONDecodeError) as error:
                return {
                    "outcome": "refused",
                    "reason": "invalid_pending_action",
                    "action_id": action_id,
                    "detail": f"pending action must be repaired or drained before upgrade: {error}",
                }
            if pending is not None:
                if pending["action_id"] != action_id:
                    return {
                        "outcome": "refused",
                        "reason": "action_conflict",
                        "action_id": action_id,
                        "detail": (
                            f"action {pending['action_id']} ({pending['action_type']}) owns "
                            f"generation {pending['expected_generation']}"
                        ),
                    }
                if actions.record_binding(pending) != binding:
                    return {
                        "outcome": "refused",
                        "reason": "action_identity_mismatch",
                        "action_id": action_id,
                    }
                return {
                    "outcome": "idempotent",
                    "reason": "already_accepted",
                    "action_id": action_id,
                    "record": pending,
                }

        if expected_generation != lode.get("run_generation"):
            return {
                "outcome": "refused",
                "reason": "stale_expected_generation",
                "action_id": action_id,
            }
        stage = message.get("stage")
        if stage != lode.get("stage") or stage not in STAGES:
            return {"outcome": "refused", "reason": "stage_mismatch", "action_id": action_id}
        if action_type != "completion" and not any(item is lode for item in self.lodes):
            return {"outcome": "refused", "reason": "lode_archived", "action_id": action_id}
        if action_type == "completion" and (
            target_disposition != self._completion_target(stage) or force_consent
        ):
            return {"outcome": "refused", "reason": "invalid_action", "action_id": action_id}
        if action_type in {"pause", "archive"} and force_consent:
            return {
                "outcome": "refused",
                "reason": "invalid_action",
                "action_id": action_id,
            }

        key = (lode_id, expected_generation)
        if is_terminal_failure_kind(lode.get("failure_kind")):
            return {"outcome": "refused", "reason": "terminal_failure", "action_id": action_id}
        if key in self.runner_results or key in self.pending_disconnects:
            return {
                "outcome": "refused",
                "reason": "runner_result_pending",
                "action_id": action_id,
            }
        if action_type == "completion" and (
            not lode.get("active") or lode_id not in self.lode_clients
        ):
            return {"outcome": "refused", "reason": "inactive_runner", "action_id": action_id}
        if action_type == "restart" and not force_consent:
            if lode.get("active") and lode_id in self.lode_clients:
                return {
                    "outcome": "refused",
                    "reason": "registered_runner_requires_force",
                    "action_id": action_id,
                    "detail": f"Cannot restart: lode {lode_id} has a registered runner.",
                }
            if lode_stage_session(lode, stage)["started"] and lode.get("state") != "error":
                return {
                    "outcome": "refused",
                    "reason": "started_stage_requires_force",
                    "action_id": action_id,
                    "detail": (
                        f"Lode {lode_id} has been started "
                        f"(claude[{stage}].started=True). Restarting discards in-progress work."
                    ),
                }
        if prepared.get("ok") is not True:
            return {
                "outcome": "refused",
                "reason": prepared.get("reason", "ownership_unavailable"),
                "action_id": action_id,
                "detail": prepared.get("error"),
            }
        ownership = prepared.get("ownership")
        already_empty = prepared.get("already_empty") is True
        if not already_empty and (
            not isinstance(ownership, dict)
            or ownership.get("run_generation") != expected_generation
        ):
            return {
                "outcome": "refused",
                "reason": "ownership_unavailable",
                "action_id": action_id,
            }
        if (
            ownership
            and ownership.get("proof_mode") == "linux-strict"
            and (teardown.resolve_pidfd_interface() is None)
        ):
            return {"outcome": "refused", "reason": "pidfd_unavailable", "action_id": action_id}
        if already_empty and action_type != "archive":
            return {"outcome": "refused", "reason": "ownership_unavailable", "action_id": action_id}
        durability = prepared.get("durability")
        if action_type in {"kill", "archive"}:
            if not isinstance(durability, dict):
                return {
                    "outcome": "refused",
                    "reason": "durability_unknown",
                    "action_id": action_id,
                }
            outcome = durability["preflight"]["outcome"]
            if outcome not in {"safe", "consent_override", "not_required"}:
                detail = durability["preflight"].get("error") or (
                    "worktree contains commits that are not durably published"
                    if outcome == "unpushed"
                    else "worktree durability could not be proven"
                )
                return {
                    "outcome": "refused",
                    "reason": f"durability_{outcome}",
                    "action_id": action_id,
                    "detail": detail,
                }

        # This final in-loop pass follows all off-loop preparation and precedes the
        # sole accepting mutation: the fsynced pending-action replace below.
        if self._find_action_lode(lode_id) is not lode:
            return {"outcome": "refused", "reason": "lode_not_found", "action_id": action_id}
        if (
            lode.get("run_generation") != expected_generation
            or lode.get("stage") != stage
            or is_terminal_failure_kind(lode.get("failure_kind"))
            or _pending_action_file_exists(lode_id)
            or (
                prepared.get("safety_snapshot") is not None
                and prepared["safety_snapshot"]
                != {
                    field: lode.get(field)
                    for field in (
                        "stage",
                        "run_generation",
                        "active",
                        "tmux_pane",
                        "pid",
                        "oom_scope",
                        "failure_kind",
                    )
                }
            )
        ):
            return {
                "outcome": "refused",
                "reason": "action_raced",
                "action_id": action_id,
            }
        try:
            record = actions.new_pending_action(
                lode_id=lode_id,
                stage=stage,
                expected_generation=expected_generation,
                action_type=action_type,
                target_disposition=target_disposition,
                force_consent=force_consent,
                output_facts=prepared.get("output"),
                ownership_record=ownership,
                source_record_sha256=prepared.get("source_digest"),
                ship=prepared.get("ship"),
                action_id=action_id,
                durability=durability,
                already_empty=already_empty,
            )
            actions.write_pending_action(record)
        except Exception as error:
            logger.error("Action record persistence failed lode=%s: %s", lode_id, error)
            return {
                "outcome": "refused",
                "reason": (
                    "completion_persistence_unavailable"
                    if action_type == "completion"
                    else "action_persistence_unavailable"
                ),
                "action_id": action_id,
                "detail": str(error),
            }

        self._cancel_generation_guard(lode_id, expected_generation)
        self._project_action(record, via="action_acceptance")
        return {
            "outcome": "accepted",
            "reason": "accepted",
            "action_id": action_id,
            "record": record,
        }

    def _handle_lode_action(self, message: dict, conn: socket.socket | None) -> None:
        """Validate completion and prepare server-owned bytes off-loop."""
        lode_id = message.get("lode_id")
        generation = message.get("expected_generation")
        action_id = message.get("action_id")
        action_type = message.get("action_type")
        try:
            actions.validate_action_id(action_id)
            binding = actions.action_binding(
                {
                    "lode_id": lode_id,
                    "expected_generation": generation,
                    "action_type": action_type,
                    "target_disposition": message.get("target_disposition"),
                    "force_consent": message.get("force_consent"),
                }
            )
        except (KeyError, TypeError, ValueError) as error:
            self._send_action_ack(
                conn, outcome="refused", reason="invalid_action", detail=str(error)
            )
            return
        lode = self._find_lode(lode_id) if isinstance(lode_id, str) else None
        if lode and generation == lode.get("run_generation") and _lifecycle_grace_pending(lode):
            if is_terminal_failure_kind(lode.get("failure_kind")):
                self._send_action_ack(
                    conn,
                    outcome="refused",
                    reason="terminal_failure",
                    action_id=action_id,
                    action_type=action_type,
                )
                return
            self._send_action_ack(
                conn,
                outcome="refused",
                reason="lifecycle_grace_pending",
                action_id=action_id,
                action_type=action_type,
            )
            return
        if action_type != "completion":
            self._handle_manual_lode_action(message, conn, binding)
            return
        if (
            message.get("stage") not in STAGES
            or binding[3] != self._completion_target(message["stage"])
            or binding[4]
        ):
            self._send_action_ack(
                conn,
                outcome="refused",
                reason="invalid_action",
                action_id=action_id,
                action_type=action_type,
            )
            return
        if not lode:
            result = self._open_lode_action(
                action_type=action_type, message=message, prepared={"ok": False}
            )
            self._send_action_open_result(conn, action_type, result, message)
            return
        if not generation:
            self._send_action_ack(
                conn, outcome="refused", reason="missing_expected_generation", action_id=action_id
            )
            return
        if generation != lode.get("run_generation"):
            result = self._open_lode_action(
                action_type=action_type, message=message, prepared={"ok": False}
            )
            self._send_action_open_result(conn, action_type, result, message)
            return
        if message.get("stage") != lode.get("stage") or lode.get("stage") not in STAGES:
            self._send_action_ack(
                conn, outcome="refused", reason="stage_mismatch", action_id=action_id
            )
            return

        if _pending_action_file_exists(lode_id):
            result = self._open_lode_action(
                action_type=action_type, message=message, prepared={"ok": False}
            )
            record = result.get("record")
            if (
                result["outcome"] == "idempotent"
                and record is not None
                and message.get("wait_for_disposition") is True
            ):
                if conn is not None:
                    self.action_waiters.setdefault(action_id, []).append(
                        (conn, message.get("exchange_id"))
                    )
                if record["phase"].endswith("_blocked"):
                    self._retry_action(lode_id, None)
                else:
                    self._continue_action(record)
            else:
                self._send_action_open_result(conn, action_type, result, message)
                if result["outcome"] == "idempotent" and record is not None:
                    self._continue_action(record)
            return
        if is_terminal_failure_kind(lode.get("failure_kind")):
            self._send_action_ack(
                conn, outcome="refused", reason="terminal_failure", action_id=action_id
            )
            return
        if not lode.get("active") or lode_id not in self.lode_clients:
            self._send_action_ack(
                conn, outcome="refused", reason="inactive_runner", action_id=action_id
            )
            return
        if lode_id in self.action_acceptances:
            owner_id, owner_binding, preparation_waiters = self.action_acceptances[lode_id]
            if owner_id == action_id and owner_binding == binding:
                if conn is not None:
                    preparation_waiters.append((conn, message.get("exchange_id")))
                return
            reason = "action_identity_mismatch" if owner_id == action_id else "action_conflict"
            self._send_action_ack(conn, outcome="refused", reason=reason, action_id=action_id)
            return
        if message.get("digest_algorithm") != actions.DIGEST_ALGORITHM:
            self._send_action_ack(
                conn, outcome="refused", reason="invalid_output", action_id=action_id
            )
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
            self._send_action_ack(
                conn, outcome="refused", reason="invalid_output", action_id=action_id
            )
            return
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            self._send_action_ack(
                conn, outcome="refused", reason="invalid_output", action_id=action_id
            )
            return
        if (
            len(data) != length
            or hashlib.sha256(data).hexdigest() != digest_hex
            or not data.strip()
        ):
            self._send_action_ack(
                conn, outcome="refused", reason="invalid_output", action_id=action_id
            )
            return
        try:
            ownership = actions.load_run_ownership(lode_id, generation, require_worker=True)
        except (OSError, ValueError, json.JSONDecodeError):
            ownership = None
        if ownership is None:
            self._send_action_ack(
                conn, outcome="refused", reason="ownership_unavailable", action_id=action_id
            )
            return
        if ownership["proof_mode"] == "linux-strict" and teardown.resolve_pidfd_interface() is None:
            self._send_action_ack(
                conn, outcome="refused", reason="pidfd_unavailable", action_id=action_id
            )
            return
        project = find_project(lode.get("project", "")) if lode["stage"] == "ship" else None
        if lode["stage"] == "ship" and not project:
            self._send_action_ack(
                conn,
                outcome="refused",
                reason="ship_provenance_unavailable",
                action_id=action_id,
            )
            return

        self.action_acceptances[lode_id] = (
            action_id,
            binding,
            ([(conn, message.get("exchange_id"))] if conn is not None else []),
        )
        snapshot = copy.deepcopy(lode)

        def prepare() -> None:
            def finish(result: dict) -> None:
                self._enqueue_event(
                    {
                        "type": "_action_acceptance_result",
                        "exchange_id": message.get("exchange_id"),
                        "request": {
                            "action_id": action_id,
                            "lode_id": lode_id,
                            "expected_generation": generation,
                            "action_type": action_type,
                            "target_disposition": message.get("target_disposition"),
                            "force_consent": message.get("force_consent"),
                            "stage": snapshot["stage"],
                            "digest_algorithm": message.get("digest_algorithm"),
                            "digest_hex": digest_hex,
                            "byte_length": length,
                        },
                        "result": result,
                    },
                    None,
                )

            try:
                actions.collect_orphaned_staging(lode_id, None)
                output = actions.stage_output(
                    lode_id,
                    data,
                    expected_length=length,
                    expected_sha256=digest_hex,
                )
            except Exception as error:
                finish({"ok": False, "reason": "output_staging_unavailable", "error": str(error)})
                return
            try:
                source_path = actions.run_ownership_path(lode_id, generation)
                source_digest = actions.durable_json_sha256(source_path)
                source = actions.load_run_ownership(lode_id, generation, require_worker=True)
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

        thread = threading.Thread(target=prepare, name="hopper-action-accept", daemon=True)
        self.registration_threads[f"accept:{lode_id}:{generation}"] = thread
        thread.start()

    @staticmethod
    def _durability_observation(lode_id: str, *, override: bool = False) -> dict:
        checked_at_ms = current_time_ms()
        if override:
            return {
                "outcome": "consent_override",
                "count": None,
                "basis": "kill --force",
                "error": None,
                "checked_at_ms": checked_at_ms,
            }
        worktree = get_worktree_dir(lode_id)
        count, basis = git.unpushed_commits(str(worktree))
        if count is None:
            return {
                "outcome": "unknown",
                "count": None,
                "basis": basis,
                "error": f"could not prove worktree durability at {worktree}",
                "checked_at_ms": checked_at_ms,
            }
        if count:
            return {
                "outcome": "unpushed",
                "count": count,
                "basis": basis,
                "error": f"{count} commit(s) exist only in worktree {worktree}",
                "checked_at_ms": checked_at_ms,
            }
        return {
            "outcome": "safe",
            "count": 0,
            "basis": basis,
            "error": None,
            "checked_at_ms": checked_at_ms,
        }

    def _handle_manual_lode_action(
        self,
        message: dict,
        conn: socket.socket | None,
        binding: tuple[str, str | None, str, str, bool],
    ) -> None:
        """Prepare one manual action without changing lode or containment state."""
        lode_id, generation, action_type, _, force_consent = binding
        action_id = message["action_id"]
        stage = message.get("stage")
        if stage not in STAGES:
            self._send_action_ack(
                conn,
                outcome="refused",
                reason="stage_mismatch",
                action_id=action_id,
                action_type=action_type,
            )
            return
        lode = self._find_action_lode(lode_id)
        has_receipt_identity = bool(
            lode
            and isinstance(lode.get("action_results", []), list)
            and any(
                isinstance(result, dict) and result.get("action_id") == action_id
                for result in lode.get("action_results", [])
            )
        )
        if (
            lode is None
            or generation != lode.get("run_generation")
            or stage != lode.get("stage")
            or _pending_action_file_exists(lode_id)
            or has_receipt_identity
        ):
            opened = self._open_lode_action(
                action_type=action_type, message=message, prepared={"ok": False}
            )
            record = opened.get("record")
            if opened["outcome"] == "idempotent" and record is not None:
                if conn is not None:
                    self.action_waiters.setdefault(action_id, []).append(
                        (conn, message.get("exchange_id"))
                    )
                if record["phase"].endswith("_blocked"):
                    self._retry_action(lode_id, None)
                else:
                    self._resume_action(lode_id)
            else:
                self._send_action_open_result(conn, action_type, opened, message)
            return
        if lode_id in self.action_acceptances:
            owner_id, owner_binding, waiters = self.action_acceptances[lode_id]
            if owner_id == action_id and owner_binding == binding:
                if conn is not None:
                    waiters.append((conn, message.get("exchange_id")))
                return
            reason = "action_identity_mismatch" if owner_id == action_id else "action_conflict"
            self._send_action_ack(
                conn,
                outcome="refused",
                reason=reason,
                action_id=action_id,
                action_type=action_type,
            )
            return

        already_empty = bool(
            action_type == "archive"
            and not lode.get("active")
            and lode_id not in self.lode_clients
            and all(lode.get(field) is None for field in ("tmux_pane", "pid", "oom_scope"))
        )
        self.action_acceptances[lode_id] = (
            action_id,
            binding,
            ([(conn, message.get("exchange_id"))] if conn is not None else []),
        )
        snapshot = copy.deepcopy(lode)

        def prepare() -> None:
            try:
                ownership = None
                source_digest = None
                if not already_empty:
                    if generation is None:
                        raise RuntimeError("active action has no run generation")
                    source_path = actions.run_ownership_path(lode_id, generation)
                    source_digest = actions.durable_json_sha256(source_path)
                    ownership = actions.load_run_ownership(lode_id, generation, require_worker=True)
                    if ownership is None:
                        raise RuntimeError("generation ownership is absent")
                durability = None
                if action_type in {"kill", "archive"}:
                    required = not already_empty
                    if required:
                        preflight = self._durability_observation(
                            lode_id,
                            override=action_type == "kill" and force_consent,
                        )
                        final = {
                            "outcome": "not_required",
                            "count": 0,
                            "basis": "pending post-containment recheck",
                            "error": None,
                            "checked_at_ms": None,
                        }
                    else:
                        preflight = final = {
                            "outcome": "not_required",
                            "count": 0,
                            "basis": "already-empty archive",
                            "error": None,
                            "checked_at_ms": current_time_ms(),
                        }
                    durability = {
                        "required": required,
                        "preflight": dict(preflight),
                        "final": dict(final),
                    }
            except Exception as error:
                result = {"ok": False, "reason": "ownership_unavailable", "error": str(error)}
            else:
                result = {
                    "ok": True,
                    "ownership": ownership,
                    "source_digest": source_digest,
                    "durability": durability,
                    "already_empty": already_empty,
                    "safety_snapshot": {
                        field: snapshot.get(field)
                        for field in (
                            "stage",
                            "run_generation",
                            "active",
                            "tmux_pane",
                            "pid",
                            "oom_scope",
                            "failure_kind",
                        )
                    },
                }
            self._enqueue_event(
                {
                    "type": "_action_acceptance_result",
                    "request": copy.deepcopy(message),
                    "result": result,
                }
            )

        thread = threading.Thread(target=prepare, name="hopper-action-accept", daemon=True)
        self.registration_threads[f"accept:{lode_id}:{generation}"] = thread
        thread.start()

    def _handle_action_acceptance_result(self, message: dict, conn: socket.socket | None) -> None:
        """Commit the durable record and install its generation fence."""
        request = message.get("request", {})
        lode_id = request.get("lode_id")
        generation = request.get("expected_generation")
        action_id = request.get("action_id")
        action_type = request.get("action_type")
        self.registration_threads.pop(f"accept:{lode_id}:{generation}", None)
        try:
            binding = actions.action_binding(
                {
                    "lode_id": request.get("lode_id"),
                    "expected_generation": request.get("expected_generation"),
                    "action_type": request.get("action_type"),
                    "target_disposition": request.get("target_disposition"),
                    "force_consent": request.get("force_consent"),
                }
            )
        except (KeyError, TypeError, ValueError):
            binding = None
        acceptance = self.action_acceptances.get(lode_id)
        if acceptance is None or acceptance[:2] != (action_id, binding):
            logger.info("Discarding stale action acceptance lode=%s action=%s", lode_id, action_id)
            return
        self.action_acceptances.pop(lode_id, None)
        preparation_waiters = acceptance[2]
        opened = self._open_lode_action(
            action_type=action_type,
            message=request,
            prepared=message.get("result", {}),
        )
        if opened["outcome"] in {"accepted", "idempotent"}:
            self.action_waiters[action_id] = preparation_waiters
            if action_type == "completion":
                self._send_action_open_result(None, action_type, opened, request)
        else:
            if action_type != "completion" and isinstance(lode_id, str) and not preparation_waiters:
                self._send_action_open_result(None, action_type, opened, request)
            for waiter, exchange_id in preparation_waiters:
                previous = getattr(self._request_context, "exchange_id", None)
                self._request_context.exchange_id = exchange_id
                try:
                    self._send_action_open_result(waiter, action_type, opened, request)
                finally:
                    self._request_context.exchange_id = previous
        record = opened.get("record")
        if opened["outcome"] == "accepted":
            if action_type == "completion":
                self._schedule_action_step(record, "output_publish", "publishing_output")
            elif record["markers"]["containment"]["state"] == "done":
                self._continue_action(record)
            else:
                self._schedule_action_step(record, "ownership_capture", "capturing_ownership")
        elif opened["outcome"] == "idempotent" and record is not None:
            if record["phase"].endswith("_blocked"):
                self._retry_action(record["lode_id"], None)
            else:
                self._resume_action(record["lode_id"])

    def _persist_action(self, record: dict, *, via: str) -> None:
        actions.write_pending_action(record)
        self._project_action(record, via=via)

    def _schedule_action_step(self, record: dict, marker_name: str, phase: str) -> None:
        """Persist an intent and start exactly one daemon worker for it."""
        marker = record["markers"][marker_name]
        if marker["state"] in {"not_started", "blocked"}:
            actions.transition_marker(record, marker_name, "intent")
        elif marker["state"] != "intent":
            return
        record["phase"] = phase
        record["recovery"] = {"kind": None, "message": None, "command": None}
        self._persist_action(record, via=f"action_intent:{marker_name}")
        key = (record["action_id"], phase)
        existing = self.action_threads.get(key)
        if existing is not None and existing.is_alive():
            return
        snapshot = copy.deepcopy(record)
        attempt_id = marker["attempt_id"]
        retained_pidfd = self.supervisor_pidfds.get(
            (record["lode_id"], record["expected_generation"])
        )
        context = self._action_step_context(record, marker_name)
        thread = threading.Thread(
            target=self._run_action_step,
            args=(snapshot, marker_name, phase, attempt_id, retained_pidfd, context),
            name=f"hopper-action-{phase}",
            daemon=True,
        )
        self.action_threads[key] = thread
        thread.start()

    def _run_action_step(
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
                    actions.publish_output(record)
                except Exception as publish_error:
                    try:
                        actions.verify_staged_output(record)
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
                ownership = record["ownership"]
                ownership.update(
                    captured=True,
                    captured_at_ms=actions.accepted_at_ms(),
                )
                result = {"ok": True, "ownership": ownership, "error": None}
            elif phase == "closing_pane":
                result = self._close_action_pane(record)
            elif phase in {"observing_containment", "force_killing"}:
                result = self._observe_action_containment(record, retained_pidfd)
            elif phase == "proving_ship_landing":
                result = self._prove_ship_landing(record)
            elif phase == "quarantining":
                result = self._run_ship_cleanup_step(record, marker_name)
            elif phase == "rechecking_durability":
                observation = self._durability_observation(
                    record["lode_id"],
                    override=(record["action_type"] == "kill" and record["force_consent"]),
                )
                result = {
                    "ok": observation["outcome"] in {"safe", "consent_override"},
                    "observation": observation,
                    "error": observation["error"],
                }
            elif phase == "spawning":
                result = self._spawn_action_successor(record, context or {})
            else:
                result = {"ok": False, "error": f"unsupported completion phase {phase}"}
        except Exception as error:
            result = {"ok": False, "error": str(error)}
        self._enqueue_event(
            {
                "type": "_action_step_result",
                "lode_id": record["lode_id"],
                "expected_generation": record["expected_generation"],
                "action_id": record["action_id"],
                "marker_name": marker_name,
                "phase": phase,
                "attempt_id": attempt_id,
                "result": result,
            }
        )

    def _action_step_context(self, record: dict, marker_name: str) -> dict | None:
        """Snapshot serialized state needed by a completion worker."""
        if marker_name != "spawn":
            return None
        target_id = self._action_spawn_target_id(record)
        target = self._find_lode(target_id) if target_id else None
        if target is None:
            return {"error": "completion spawn target is absent"}
        project = find_project(target.get("project", ""))
        if project is None:
            return {"error": "completion successor project is unavailable"}
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
        if git.is_dirty(worktree) is not False:
            return {
                "ok": False,
                "landing": landing,
                "error": (
                    "ship worktree is dirty or cleanliness could not be proven after landing proof"
                ),
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
            fact = git.delete_branch_if_unchanged(provenance, base_ref=ship["landing"]["base_ref"])
            return {
                "ok": fact["state"] in {"deleted", "already-absent", "retained"},
                "fact": fact,
                "error": fact["error"],
            }
        return {"ok": False, "error": f"unsupported ship cleanup marker {marker_name}"}

    @staticmethod
    def _spawn_action_successor(record: dict, context: dict) -> dict:
        if context.get("error"):
            return {"ok": False, "error": context["error"]}
        if not actions.containment_is_proven(record):
            missing = actions.missing_containment_proof(record) or "proof facts are inconsistent"
            return {"ok": False, "error": f"action successor refused: {missing}"}
        if (
            record["action_type"] not in {"completion", "restart"}
            or record["markers"]["containment"]["state"] != "done"
            or record["markers"]["lode_mutation"]["state"] != "done"
            or record["markers"]["spawn"]["state"] != "intent"
            or record["spawn"] is None
        ):
            return {"ok": False, "error": "action successor prerequisites are incomplete"}
        target = context["target"]
        spawn = record["spawn"]
        try:
            receipt = actions.load_spawn_receipt(record["lode_id"], record["action_id"])
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
            "path": str(actions.spawn_receipt_path(record["lode_id"], record["action_id"])),
            "action_id": record["action_id"],
            "source_lode_id": record["lode_id"],
            "target_lode_id": target["id"],
            "target_generation": spawn["target_generation"],
        }
        window_outcome, pane_id = spawn_lode_processor(
            target["id"],
            context.get("project_path"),
            foreground=False,
            env=pane_env,
            spawn_receipt=receipt_facts,
        )
        if window_outcome is WindowSpawnOutcome.PROVEN_NO_PANE:
            return {"ok": False, "error": "tmux declined the completion runner pane"}
        if window_outcome is WindowSpawnOutcome.UNKNOWN:
            return {
                "ok": False,
                "error": ("completion runner pane creation is unverified and a pane may be live"),
            }
        try:
            receipt = actions.load_spawn_receipt(record["lode_id"], record["action_id"])
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return {"ok": False, "error": f"spawn receipt is invalid: {error}"}
        if receipt is None or receipt.get("pane_id") != pane_id:
            return {"ok": False, "error": "pane bootstrap did not publish its exact receipt"}
        return {"ok": True, "pane_id": pane_id, "adopted": False}

    @staticmethod
    def _close_action_pane(record: dict) -> dict:
        """Discover the owned set before closing the recorded pane."""
        ownership = record["ownership"]
        discovered = teardown.discover_owned_set(ownership)
        if discovered["state"] != "discovered":
            if ownership["proof_mode"] != "linux-strict":
                return {
                    "ok": False,
                    "error": (
                        f"owned process enumeration before pane close failed: {discovered['error']}"
                    ),
                }
            # Deliberate source-spec polarity: strict containment proves the cgroup,
            # supervisor, and pane root and never reads descendants, so do not block
            # teardown on a /proc walk whose result would be discarded.
            logger.warning(
                "Owned process enumeration failed before pane close; strict "
                "containment will continue lode=%s error=%s",
                record["lode_id"],
                discovered["error"],
            )
        closed = teardown.close_owned_pane(ownership)
        result = {"ok": closed["state"] == "gone", "error": closed["error"]}
        if discovered["state"] == "discovered":
            result["descendants"] = discovered["descendants"]
        return result

    @staticmethod
    def _merge_observed_descendants(
        ownership: dict,
        observed: dict,
        *,
        excluded_pids: set[int] | None = None,
    ) -> list[dict]:
        """Persist only identity-resolved, schema-valid discovered descendants."""
        excluded_pids = set() if excluded_pids is None else excluded_pids
        roots = {
            (identity["pid"], json.dumps(identity["birth"], sort_keys=True))
            for identity in (
                ownership["pane"]["root_process"],
                ownership["supervisor"],
                ownership["worker"],
            )
        }
        root_pids = {pid for pid, _birth in roots}
        recorded_birth_by_pid = {
            identity["pid"]: json.dumps(identity["birth"], sort_keys=True)
            for identity in ownership["descendants"]
        }
        observed_identities = {
            (identity["pid"], json.dumps(identity["birth"], sort_keys=True)): identity
            for identity in observed["identities"]
            if identity["pgid"] > 0
            and identity["pid"] not in root_pids
            and identity["pid"] not in excluded_pids
            and (
                identity["pid"] not in recorded_birth_by_pid
                or recorded_birth_by_pid[identity["pid"]]
                == json.dumps(identity["birth"], sort_keys=True)
            )
        }
        if observed["resolution"] == "complete":
            merged = observed_identities
        else:
            retained = {
                (identity["pid"], json.dumps(identity["birth"], sort_keys=True)): identity
                for identity in ownership["descendants"]
                if (identity["pid"], json.dumps(identity["birth"], sort_keys=True))
                in observed_identities
            }
            merged = {**retained, **observed_identities}
        descendants = [identity for key, identity in merged.items() if key not in roots]
        unique_by_pid = {}
        for identity in sorted(descendants, key=lambda item: item["pid"]):
            unique_by_pid.setdefault(identity["pid"], identity)
        return list(unique_by_pid.values())

    def _release_proven_scope(
        self,
        record: dict,
        systemctl: str | None,
        *,
        now_ns: Callable[[], int],
        poll: Callable[[float], None],
    ) -> str | None:
        """Retry scope release within one bounded bookkeeping budget."""
        unit_name = record["ownership"]["unit"]["name"]
        if not systemctl:
            return unit_name
        deadline = now_ns() + int(oom.SCOPE_RESULT_SETTLE_SEC * 1_000_000_000)
        while True:
            observed = oom.read_scope_control_group(systemctl, unit_name)
            if observed["state"] == "absent":
                return None
            if observed["state"] == "present":
                oom.release_scope(systemctl, unit_name)
            current = now_ns()
            if current >= deadline:
                return unit_name
            remaining = max(0.0, (deadline - current) / 1_000_000_000)
            poll(min(oom.SCOPE_RESULT_POLL_SEC, remaining))

    def _observe_action_containment(
        self,
        record: dict,
        retained_pidfd: int | None,
        *,
        now_ns: Callable[[], int] = time.monotonic_ns,
        poll: Callable[[float], None] = time.sleep,
    ) -> dict:
        """Build identity-bound observers and run the bounded state machine."""
        result_handles = {}
        try:
            return self._observe_action_containment_impl(
                record,
                retained_pidfd,
                result_handles=result_handles,
                now_ns=now_ns,
                poll=poll,
            )
        except Exception:
            self._close_action_result_handles(result_handles)
            raise

    def _observe_action_containment_impl(
        self,
        record: dict,
        retained_pidfd: int | None,
        *,
        result_handles: dict,
        now_ns: Callable[[], int],
        poll: Callable[[float], None],
    ) -> dict:
        ownership = record["ownership"]
        mode = ownership["proof_mode"]
        key = self._containment_handle_key(record)
        host_boot_identity = teardown.read_host_boot_identity(platform=ownership["platform"])
        systemctl = None
        if mode == "linux-strict":
            containment = record["containment"]
            distinct_pane_root = (
                ownership["pane"]["root_process"]["pid"] != ownership["supervisor"]["pid"]
            )
            if containment["state"] == "kill_pending":
                if (
                    containment["last_cgroup_observation"] != "empty"
                    and record["markers"]["scope_kill"]["state"] != "intent"
                ):
                    return {"ok": False, "error": "cgroup kill intent is not durable"}
                if (
                    containment["last_supervisor_observation"] != "gone" or distinct_pane_root
                ) and record["markers"]["supervisor_kill"]["state"] != "intent":
                    return {"ok": False, "error": "supervisor kill intent is not durable"}

            pidfd_interface = teardown.resolve_pidfd_interface()
            supervisor_fd = retained_pidfd or self.supervisor_pidfds.get(key)
            supervisor_gone = False
            if supervisor_fd is None and pidfd_interface is not None:
                reopened = teardown.reopen_process_pidfd(
                    ownership["supervisor"], pidfd_interface=pidfd_interface
                )
                supervisor_fd = reopened["fd"]
                supervisor_gone = reopened["state"] == "gone"
                if supervisor_fd is not None:
                    result_handles.update(pidfd=supervisor_fd, pidfd_owned=True)

            pane_root_fd = self.pane_root_pidfds.get(key)
            pane_root_gone = not distinct_pane_root and supervisor_gone
            if distinct_pane_root and pane_root_fd is None and pidfd_interface is not None:
                reopened = teardown.reopen_process_pidfd(
                    ownership["pane"]["root_process"], pidfd_interface=pidfd_interface
                )
                pane_root_fd = reopened["fd"]
                pane_root_gone = reopened["state"] == "gone"
                if pane_root_fd is not None:
                    result_handles.update(
                        pane_root_pidfd=pane_root_fd,
                        pane_root_pidfd_owned=True,
                    )

            cgroup_fd = self.cgroup_fds.get(key)
            cgroup_absent = key in self.absent_cgroups
            if cgroup_fd is None and not cgroup_absent:
                cgroup_fd, cgroup_error = teardown._opened_cgroup(ownership["cgroup"])
                if cgroup_fd is not None:
                    result_handles.update(cgroup_fd=cgroup_fd, cgroup_fd_owned=True)
                elif cgroup_error == "absent":
                    systemctl = oom.find_systemctl()
                    if systemctl:
                        reconciled = oom.read_scope_control_group(
                            systemctl, ownership["unit"]["name"]
                        )
                        cgroup_absent = reconciled["state"] == "absent"
                        if cgroup_absent:
                            result_handles["cgroup_absent"] = True

            def observe_cgroup() -> str:
                if cgroup_absent:
                    return "empty"
                if cgroup_fd is None or host_boot_identity is None:
                    return "cannot-tell"
                return teardown.observe_retained_cgroup(
                    cgroup_fd,
                    ownership["cgroup"],
                    boot_id=host_boot_identity,
                )

            def observe_supervisor() -> str:
                if supervisor_gone:
                    return "gone"
                if supervisor_fd is None:
                    return "cannot-tell"
                return teardown.observe_pidfd(supervisor_fd, pidfd_interface=pidfd_interface)

            def observe_pane_root() -> str:
                if not distinct_pane_root:
                    return observe_supervisor()
                if pane_root_gone:
                    return "gone"
                if pane_root_fd is None:
                    return "cannot-tell"
                return teardown.observe_pidfd(pane_root_fd, pidfd_interface=pidfd_interface)

            def kill_cgroup() -> dict:
                if record["markers"]["scope_kill"]["state"] != "intent":
                    return {
                        "state": "unaddressable",
                        "error": "durable cgroup kill intent is unavailable",
                    }
                observed = observe_cgroup()
                if observed == "empty":
                    return {"state": "already-gone", "error": None}
                if observed != "populated":
                    return {"state": "unaddressable", "error": "cgroup identity is ambiguous"}
                return teardown.kill_cgroup(ownership["cgroup"], boot_id=host_boot_identity)

            def signal_identity(fd: int | None, identity: dict) -> dict:
                if record["markers"]["supervisor_kill"]["state"] != "intent":
                    return {
                        "state": "unaddressable",
                        "error": "durable process kill intent is unavailable",
                    }
                if fd is None:
                    return {"state": "unaddressable", "error": "verified pidfd is unavailable"}
                return teardown.signal_process_pidfd(
                    fd,
                    identity,
                    pidfd_interface=pidfd_interface,
                )

            handles = {
                "observe_cgroup": observe_cgroup,
                "observe_supervisor": observe_supervisor,
                "observe_pane_root": observe_pane_root,
                "kill_cgroup": kill_cgroup,
                "kill_supervisor": lambda: signal_identity(supervisor_fd, ownership["supervisor"]),
                "kill_pane_root": lambda: signal_identity(
                    supervisor_fd if not distinct_pane_root else pane_root_fd,
                    ownership["pane"]["root_process"],
                ),
            }
        else:
            replaced_descendant_pids = set()

            def observe_bounded() -> dict:
                owned = [
                    ownership["pane"]["root_process"],
                    ownership["supervisor"],
                    ownership["worker"],
                    *ownership["descendants"],
                ]
                observed = teardown.observe_bounded_processes(
                    owned,
                    platform=ownership["platform"],
                    process_table=teardown.read_process_table(platform=ownership["platform"]),
                )
                replaced_descendant_pids.update(observed.get("replaced_pids", ()))
                ownership["descendants"] = self._merge_observed_descendants(
                    ownership,
                    observed,
                    excluded_pids=replaced_descendant_pids,
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

        containment = teardown.observe_containment(
            record,
            handles,
            host_boot_identity=host_boot_identity,
            now_ns=now_ns,
            poll=poll,
        )
        if mode == "linux-strict" and containment["state"] == "proven":
            systemctl = systemctl or oom.find_systemctl()
            residual = self._release_proven_scope(
                record,
                systemctl,
                now_ns=now_ns,
                poll=poll,
            )
            if residual is not None:
                containment["proof_label"] = actions.append_scope_release_residual(
                    containment["proof_label"], residual
                )
                logger.warning(
                    "Containment proven but scope was not released lode=%s unit=%s",
                    record["lode_id"],
                    residual,
                )
        return {
            "ok": containment.get("last_error") is None,
            "containment": containment,
            "error": containment.get("last_error"),
            "descendants": ownership["descendants"],
            **result_handles,
        }

    def _block_action(
        self,
        record: dict,
        marker_name: str,
        recovery_kind: str,
        error: str | None,
    ) -> None:
        self._close_containment_handles(record)
        marker = record["markers"][marker_name]
        if marker["state"] == "intent":
            actions.transition_marker(
                record,
                marker_name,
                "blocked",
                attempt_id=marker["attempt_id"],
                detail=error or "action phase failed",
            )
        if marker_name == "containment":
            for kill_marker in ("scope_kill", "supervisor_kill"):
                current = record["markers"][kill_marker]
                if current["state"] == "intent":
                    actions.transition_marker(
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
        elif marker_name == "durability_recheck":
            record["phase"] = "durability_blocked"
        else:
            record["phase"] = "cleanup_blocked"
        record["recovery"] = {
            "kind": recovery_kind,
            "message": error or "action phase failed",
            "command": actions.recovery_command(record, recovery_kind),
        }
        if marker_name == "output_publish":
            record["output"]["failure"] = error or "completion output publication failed"
        self._persist_action(record, via=f"action_blocked:{marker_name}")
        if record["action_type"] != "completion":
            self._send_action_ack(
                None,
                outcome="blocked",
                reason=f"{recovery_kind}_blocked",
                action_id=record["action_id"],
                action_type=record["action_type"],
                disposition=record["target_disposition"],
                detail=record["recovery"]["message"],
                record=record,
            )

    def _handle_action_step_result(self, message: dict) -> None:
        """Accept one worker result only for its persisted action/phase/attempt."""
        action_id = message.get("action_id")
        phase = message.get("phase")
        self.action_threads.pop((action_id, phase), None)
        lode_id = message.get("lode_id")
        try:
            record = self._load_action_slot(lode_id)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            logger.error("Cannot apply action result lode=%s: %s", lode_id, error)
            result = message.get("result", {})
            self._close_action_result_handles(result)
            return
        marker_by_phase = {
            "publishing_output": "output_publish",
            "capturing_ownership": "ownership_capture",
            "closing_pane": "pane_close",
            "observing_containment": "containment",
            "force_killing": "containment",
            "proving_ship_landing": "ship_landing",
            "rechecking_durability": "durability_recheck",
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
            "rechecking_durability": {"durability_recheck"},
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
            or record["expected_generation"] != message.get("expected_generation")
            or record["phase"] != phase
            or record["markers"][marker_name]["state"] != "intent"
            or record["markers"][marker_name]["attempt_id"] != message.get("attempt_id")
        ):
            logger.info(
                "Discarding stale action result lode=%s action=%s phase=%s",
                lode_id,
                action_id,
                phase,
            )
            self._close_action_result_handles(result)
            return

        self._adopt_action_result_handles(record, result)
        if "descendants" in result:
            record["ownership"]["descendants"] = result["descendants"]

        if result.get("ok") is not True:
            if "containment" in result:
                record["containment"] = result["containment"]
            if phase == "rechecking_durability" and "observation" in result:
                record["durability"]["final"] = result["observation"]
            recovery_kind = {
                "publishing_output": (
                    "publication" if result.get("staged_bytes") == "verified" else "output"
                ),
                "capturing_ownership": "ownership",
                "closing_pane": "ownership",
                "observing_containment": "containment",
                "force_killing": "containment",
                "proving_ship_landing": "landing",
                "rechecking_durability": "durability",
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
            self._block_action(record, marker_name, recovery_kind, result.get("error"))
            return

        marker = record["markers"][marker_name]
        if phase == "publishing_output":
            actions.transition_marker(record, marker_name, "done", attempt_id=marker["attempt_id"])
            record["output"]["published"] = True
            record["output"]["failure"] = None
            self._persist_action(record, via="completion_result:output_publish")
            self._schedule_action_step(record, "ownership_capture", "capturing_ownership")
            return

        if phase == "capturing_ownership":
            record["ownership"] = result["ownership"]
            actions.transition_marker(record, marker_name, "done", attempt_id=marker["attempt_id"])
            record["containment"]["state"] = "pane_close_pending"
            self._persist_action(record, via="action_result:ownership_capture")
            self._schedule_action_step(record, "pane_close", "closing_pane")
            return

        if phase == "closing_pane":
            actions.transition_marker(record, marker_name, "done", attempt_id=marker["attempt_id"])
            record["containment"] = teardown.start_containment(record)
            self._persist_action(record, via="action_result:pane_close")
            self._schedule_action_step(record, "containment", "observing_containment")
            return

        if phase == "proving_ship_landing":
            record["ship"]["landing"] = result["landing"]
            actions.transition_marker(
                record,
                marker_name,
                "done",
                attempt_id=marker["attempt_id"],
                detail=result["landing"]["cause"],
            )
            self._persist_action(record, via="completion_result:ship_landing")
            self._continue_action(record)
            return

        if phase == "rechecking_durability":
            record["durability"]["final"] = result["observation"]
            actions.transition_marker(
                record,
                marker_name,
                "done",
                attempt_id=marker["attempt_id"],
                detail=result["observation"]["outcome"],
            )
            self._persist_action(record, via="action_result:durability_recheck")
            self._continue_action(record)
            return

        if phase == "quarantining":
            fact = result["fact"]
            marker_detail = fact.get("state") or "authorized"
            if marker_name == "worktree_repair":
                record["ship"]["quarantine"]["registration_repaired"] = True
            elif marker_name == "worktree_remove":
                record["ship"]["quarantine"]["removal_outcome"] = "removed"
            elif marker_name == "branch_delete":
                record["ship"]["quarantine"]["branch_outcome"] = {
                    "deleted": "deleted",
                    "already-absent": "already_absent",
                    "retained": "retained",
                }[fact["state"]]
                if fact["state"] == "retained":
                    marker_detail = fact["error"]
            record["ship"]["cleanup_failure"] = None
            actions.transition_marker(
                record,
                marker_name,
                "done",
                attempt_id=marker["attempt_id"],
                detail=marker_detail,
            )
            self._persist_action(record, via=f"completion_result:{marker_name}")
            self._continue_action(record)
            return

        if phase == "spawning":
            record["spawn"]["pane_id"] = result["pane_id"]
            self._persist_action(record, via="action_result:spawn_pane")
            self._reconcile_spawn_adoption(record)
            return

        containment = result["containment"]
        record["containment"] = containment
        if containment["state"] == "kill_pending":
            attempt = uuid.uuid4().hex
            distinct_pane_root = (
                record["ownership"]["pane"]["root_process"]["pid"]
                != record["ownership"]["supervisor"]["pid"]
            )
            required_kill_markers = []
            if containment["last_cgroup_observation"] != "empty":
                required_kill_markers.append("scope_kill")
            if containment["last_supervisor_observation"] != "gone" or distinct_pane_root:
                required_kill_markers.append("supervisor_kill")
            for kill_marker in required_kill_markers:
                state = record["markers"][kill_marker]["state"]
                if state in {"not_started", "blocked"}:
                    actions.transition_marker(
                        record,
                        kill_marker,
                        "intent",
                        attempt_id=attempt,
                    )
                elif state != "intent":
                    self._block_action(
                        record,
                        marker_name,
                        "containment",
                        f"{kill_marker} marker is {state}; durable force intent is unavailable",
                    )
                    return
            record["phase"] = "force_killing"
            self._persist_action(record, via="action_intent:force_killing")
            self._schedule_action_step(record, "containment", "force_killing")
            return
        if containment["state"] != "proven":
            self._block_action(
                record,
                marker_name,
                "containment",
                containment.get("last_error") or "containment proof is incomplete",
            )
            return
        actions.transition_marker(record, marker_name, "done", attempt_id=marker["attempt_id"])
        for kill_marker in ("scope_kill", "supervisor_kill"):
            current = record["markers"][kill_marker]
            if current["state"] == "intent":
                actions.transition_marker(
                    record, kill_marker, "done", attempt_id=current["attempt_id"]
                )
        record["phase"] = "publishing_terminal"
        self._persist_action(record, via="action_result:containment_proven")
        self._close_containment_handles(record)
        self._continue_action(record)

    def _continue_action(self, record: dict) -> None:
        """Dispatch the first unfinished post-containment action."""
        if record["markers"]["containment"]["state"] != "done":
            return
        if not self._require_containment_proof(record, "post-containment continuation"):
            return
        if record["action_type"] != "completion":
            self._continue_manual_action(record)
            return
        self._continue_completion_action(record)

    def _require_containment_proof(self, record: dict, entry_site: str) -> bool:
        """Turn an incomplete post-containment entry into a visible safety block."""
        if actions.containment_is_proven(record):
            return True
        missing = actions.missing_containment_proof(record) or "proof facts are inconsistent"
        self._block_action(
            record,
            "containment",
            "containment",
            f"{entry_site} refused: {missing}",
        )
        return False

    def _continue_completion_action(self, record: dict) -> None:
        """Dispatch completion-only terminal, ship, spawn, and cleanup work."""
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
                    self._schedule_action_step(record, marker_name, phase)
                    return
        if record["markers"]["lode_mutation"]["state"] != "done":
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
            if not self._prepare_action_spawn(record):
                return
            self._reconcile_spawn_adoption(record)
            return
        if record["stage"] == "ship":
            for marker_name in ("worktree_remove", "branch_delete"):
                if record["markers"][marker_name]["state"] != "done":
                    self._schedule_action_step(record, marker_name, "quarantining")
                    return
        self._clear_completed_action(record)

    def _continue_manual_action(self, record: dict) -> None:
        """Dispatch the accepted manual action after containment is proven empty."""
        if (
            record["durability"]["required"]
            and record["markers"]["durability_recheck"]["state"] != "done"
        ):
            self._schedule_action_step(record, "durability_recheck", "rechecking_durability")
            return
        if record["action_type"] in {"pause", "restart"}:
            if record["markers"]["lode_mutation"]["state"] != "done":
                if not self._apply_manual_lode_mutation(record):
                    return
        else:
            if record["markers"]["archive"]["state"] != "done":
                if not self._apply_manual_archive(record):
                    return
        if record["action_type"] == "restart" and record["markers"]["spawn"]["state"] != "done":
            if not self._prepare_action_spawn(record):
                return
            self._reconcile_spawn_adoption(record)
            return
        self._clear_completed_action(record)

    def _apply_manual_lode_mutation(self, record: dict) -> bool:
        marker = record["markers"]["lode_mutation"]
        if marker["state"] in {"not_started", "blocked"}:
            target_session = str(uuid.uuid4()) if record["action_type"] == "restart" else None
            actions.transition_marker(record, "lode_mutation", "intent", detail=target_session)
            record["phase"] = "publishing_terminal"
            self._persist_action(record, via="action_intent:lode_mutation")
            marker = record["markers"]["lode_mutation"]
        lode = self._find_lode(record["lode_id"])
        if lode is None or lode.get("stage") != record["stage"]:
            self._block_action(
                record, "lode_mutation", "cleanup", "accepted lode identity is absent"
            )
            return False
        stop_lode_runtime(lode)
        if record["action_type"] == "pause":
            lode["state"] = "paused"
            lode["status"] = "Paused by user; worktree retained"
        else:
            if (
                reset_lode_claude_stage(
                    self.lodes,
                    lode["id"],
                    record["stage"],
                    persist=False,
                    session_id=marker["detail"],
                )
                is None
            ):
                self._block_action(
                    record, "lode_mutation", "cleanup", "accepted Claude stage is absent"
                )
                return False
            lode["state"] = "teardown"
            lode["status"] = actions.action_status(record)
        lode["failure_kind"] = None
        lode["active"] = False
        lode["tmux_pane"] = None
        lode["pid"] = None
        lode["oom_scope"] = None
        touch(lode)
        save_lodes(self.lodes)
        actions.transition_marker(
            record,
            "lode_mutation",
            "done",
            attempt_id=marker["attempt_id"],
            detail=record["target_disposition"],
        )
        self._persist_action(record, via="action_result:lode_mutation")
        self.broadcast({"type": "lode_updated", "lode": lode})
        return True

    def _apply_manual_archive(self, record: dict) -> bool:
        marker = record["markers"]["archive"]
        if marker["state"] in {"not_started", "blocked"}:
            actions.transition_marker(record, "archive", "intent")
            record["phase"] = "publishing_terminal"
            self._persist_action(record, via="action_intent:archive")
            marker = record["markers"]["archive"]
        lode = self._find_action_lode(record["lode_id"])
        if lode is None:
            self._block_action(record, "archive", "cleanup", "accepted lode is absent")
            return False
        if any(item is lode for item in self.lodes):
            stop_lode_runtime(lode)
            if record["action_type"] == "kill":
                lode["state"] = "error"
                lode["status"] = "Killed by user; worktree retained"
            else:
                lode["status"] = "Archived by user; worktree retained"
            lode["failure_kind"] = None
            lode["active"] = False
            lode["tmux_pane"] = None
            lode["pid"] = None
            lode["oom_scope"] = None
            lode["pending_action"] = actions.pending_action_projection(record)
            touch(lode)
            save_lodes(self.lodes)
        try:
            archived = archive_lode_for_action(
                self.lodes,
                self.archived_lodes,
                record["lode_id"],
                record["action_id"],
            )
        except (OSError, ValueError) as error:
            self._block_action(record, "archive", "cleanup", str(error))
            return False
        actions.transition_marker(
            record,
            "archive",
            "done",
            attempt_id=marker["attempt_id"],
            detail="archive_action_id matched",
        )
        self._persist_action(record, via="action_result:archive")
        self.broadcast({"type": "lode_archived", "lode": archived})
        return True

    def _apply_completion_stage(self, record: dict) -> bool:
        marker = record["markers"]["lode_mutation"]
        if marker["state"] in {"not_started", "blocked"}:
            actions.transition_marker(record, "lode_mutation", "intent")
            record["phase"] = "publishing_terminal"
            self._persist_action(record, via="completion_intent:stage_mutation")
            marker = record["markers"]["lode_mutation"]
        lode = self._find_action_lode(record["lode_id"])
        target = record["next_action"]["target_stage"] or "shipped"
        if lode is None:
            self._block_action(record, "lode_mutation", "cleanup", "completion lode is absent")
            return False
        if lode.get("stage") == record["stage"]:
            stop_lode_runtime(lode)
            lode["stage"] = target
            if target == "shipped":
                lode["shipped_at"] = current_time_ms()
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
            self._block_action(
                record,
                "lode_mutation",
                "cleanup",
                "lode stage conflicts with the accepted completion action",
            )
            return False
        actions.transition_marker(
            record,
            "lode_mutation",
            "done",
            attempt_id=marker["attempt_id"],
            detail=f"stage {target}",
        )
        self._persist_action(record, via="completion_result:stage_mutation")
        return True

    def _apply_completion_archive(self, record: dict) -> bool:
        marker = record["markers"]["archive"]
        if marker["state"] in {"not_started", "blocked"}:
            actions.transition_marker(record, "archive", "intent")
            self._persist_action(record, via="completion_intent:archive")
            marker = record["markers"]["archive"]
        try:
            archived = archive_lode_for_action(
                self.lodes,
                self.archived_lodes,
                record["lode_id"],
                record["action_id"],
            )
        except (OSError, ValueError) as error:
            self._block_action(record, "archive", "cleanup", str(error))
            return False
        record["ship"]["archive_published"] = True
        actions.transition_marker(
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
        self._persist_action(record, via="completion_result:archive")
        self.broadcast({"type": "lode_archived", "lode": archived})
        return True

    def _apply_completion_backlog(self, record: dict) -> bool:
        marker = record["markers"]["backlog"]
        if marker["state"] in {"not_started", "blocked"}:
            actions.transition_marker(record, "backlog", "intent")
            self._persist_action(record, via="completion_intent:backlog")
            marker = record["markers"]["backlog"]
        plan = record["ship"]["backlog"]
        source = self._find_action_lode(record["lode_id"])
        if source is None:
            self._block_action(record, "backlog", "cleanup", "archived lode is absent")
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
            self._persist_action(record, via="completion_backlog:plan")
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
            self._persist_action(record, via="completion_backlog:extend_plan")
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
                    self._block_action(
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
                        coder_provider=lode_coder(source)[0],
                        driver=lode_driver(source),
                    )
                except (OSError, RuntimeError, ValueError) as error:
                    self._block_action(record, "backlog", "cleanup", str(error))
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
                self._block_action(
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
            self._block_action(record, "backlog", "cleanup", str(error))
            return False
        if any(item.queued == record["lode_id"] for item in self.backlog):
            self._block_action(
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
        actions.transition_marker(
            record,
            "backlog",
            "done",
            attempt_id=marker["attempt_id"],
            detail="recorded backlog disposition applied",
        )
        self._persist_action(record, via="completion_result:backlog")
        return True

    def _prepare_action_spawn(self, record: dict) -> bool:
        marker = record["markers"]["spawn"]
        target_id = self._action_spawn_target_id(record)
        target = self._find_lode(target_id) if target_id else None
        if target is None:
            self._block_action(record, "spawn", "spawn", "action spawn target is absent")
            return False
        if record["spawn"] is None:
            generation = uuid.uuid4().hex
            record["spawn"] = {
                "target_lode_id": target["id"],
                "target_generation": generation,
                "receipt_relative_path": f"spawn-{record['action_id']}.json",
                "pane_id": None,
                "supervisor_adopted": False,
                "worker_adopted": False,
            }
        generation = record["spawn"]["target_generation"]
        if marker["state"] in {"not_started", "blocked"}:
            actions.transition_marker(record, "spawn", "intent")
            record["phase"] = "spawning"
            self._persist_action(record, via="action_intent:spawn")
            marker = record["markers"]["spawn"]
        current_generation = target.get("run_generation")
        allowed_prior = {None, record["expected_generation"], generation}
        if current_generation not in allowed_prior:
            self._block_action(
                record, "spawn", "spawn", "spawn target generation conflicts with this action"
            )
            return False
        try:
            receipt = actions.load_spawn_receipt(record["lode_id"], record["action_id"])
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self._block_action(record, "spawn", "spawn", f"spawn receipt is invalid: {error}")
            return False
        if receipt is not None and (
            receipt["target_lode_id"] != target["id"] or receipt["target_generation"] != generation
        ):
            self._block_action(
                record, "spawn", "spawn", "spawn receipt belongs to another action target"
            )
            return False
        pane_id = receipt["pane_id"] if receipt is not None else None
        current_pane = target.get("tmux_pane")
        retained_source_pane = bool(
            receipt is None
            and target["id"] == record["lode_id"]
            and current_generation == record["expected_generation"]
            and current_pane == record["ownership"]["pane"]["pane_id"]
        )
        if current_pane not in {None, pane_id} and not retained_source_pane:
            self._block_action(
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
            "action_spawn_prepare",
        )
        self._schedule_action_step(record, "spawn", "spawning")
        return True

    def _reconcile_spawn_adoption(self, record: dict) -> None:
        if record["spawn"] is None or record["markers"]["spawn"]["state"] != "intent":
            return
        target_id = self._action_spawn_target_id(record)
        target = self._find_lode(target_id) if target_id else None
        if target is None or target.get("run_generation") != record["spawn"]["target_generation"]:
            return
        try:
            receipt = actions.load_spawn_receipt(record["lode_id"], record["action_id"])
            ownership = actions.load_run_ownership(
                target["id"], record["spawn"]["target_generation"], require_worker=False
            )
            final_ownership = actions.load_run_ownership(
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
            self._persist_action(record, via="action_spawn:reconcile")
            return
        marker = record["markers"]["spawn"]
        actions.transition_marker(
            record, "spawn", "done", attempt_id=marker["attempt_id"], detail="runner adopted"
        )
        self._persist_action(record, via="action_result:spawn_reconcile")
        self._continue_action(record)

    def _clear_completed_action(self, record: dict) -> None:
        self._close_containment_handles(record)
        if record["action_type"] != "completion":
            self._clear_completed_manual_action(record)
            return
        marker = record["markers"]["pending_clear"]
        if marker["state"] == "not_started":
            actions.transition_marker(record, "pending_clear", "intent")
            self._persist_action(record, via="completion_intent:pending_clear")
            marker = record["markers"]["pending_clear"]
        if marker["state"] == "intent":
            actions.transition_marker(
                record,
                "pending_clear",
                "done",
                attempt_id=marker["attempt_id"],
                detail="all durable side effects complete",
            )
        record["phase"] = "complete"
        if record["result"] is None:
            successor = None
            if record["spawn"] is not None:
                successor = {
                    "lode_id": record["spawn"]["target_lode_id"],
                    "generation": record["spawn"]["target_generation"],
                    "pane_id": record["spawn"]["pane_id"],
                }
            record["result"] = actions.new_action_result(
                record,
                retained={
                    "worktree": True,
                    "branch": True,
                    "session": record["stage"] != "ship",
                },
                successor=successor,
            )
        if record["stage"] == "ship":
            self._persist_action(record, via="completion_result:complete")
            lode = self._find_action_lode(record["lode_id"])
            if lode is not None:
                actions.append_action_result(lode, record["result"])
                lode["pending_action"] = None
                lode["state"] = "ready"
                lode["status"] = actions.action_status(record)
                lode["active"] = False
                lode["pid"] = None
                lode["tmux_pane"] = None
                lode["oom_scope"] = None
                touch(lode)
                save_archived_lodes(self.archived_lodes)
                _log_state_change(lode["id"], lode["state"], lode["status"], "completion_clear")
                self.broadcast({"type": "lode_updated", "lode": lode})
        else:
            actions.write_pending_action(record)
            lode = self._find_lode(record["lode_id"])
            if lode is not None:
                actions.append_action_result(lode, record["result"])
                lode["pending_action"] = None
                lode["state"] = "running"
                lode["status"] = f"Starting {lode['stage']}"
                touch(lode)
                save_lodes(self.lodes)
                _log_state_change(lode["id"], lode["state"], lode["status"], "completion_clear")
                self.broadcast({"type": "lode_updated", "lode": lode})
        actions.clear_pending_action(record)

    def _clear_completed_manual_action(self, record: dict) -> None:
        if record["result"] is None:
            successor = None
            if record["spawn"] is not None:
                successor = {
                    "lode_id": record["spawn"]["target_lode_id"],
                    "generation": record["spawn"]["target_generation"],
                    "pane_id": record["spawn"]["pane_id"],
                }
            record["result"] = actions.new_action_result(
                record,
                retained={
                    "worktree": True,
                    "branch": True,
                    "session": record["action_type"] != "restart",
                },
                successor=successor,
            )
        # Persist the terminal receipt before acknowledging it.  The pending
        # record remains the recovery source until the response boundary has
        # passed and pending-clear intent is itself durable.
        actions.write_pending_action(record)
        lode = self._find_action_lode(record["lode_id"])
        if lode is None:
            self._block_action(record, "pending_clear", "cleanup", "action lode is absent")
            return
        actions.append_action_result(lode, record["result"])
        self._set_manual_terminal_status(lode, record["action_type"])
        touch(lode)
        if any(item is lode for item in self.lodes):
            save_lodes(self.lodes)
        else:
            save_archived_lodes(self.archived_lodes)
        self.broadcast({"type": "lode_updated", "lode": lode})
        self._send_action_ack(
            None,
            outcome="completed",
            reason="completed",
            action_id=record["action_id"],
            action_type=record["action_type"],
            disposition=record["target_disposition"],
            record=record,
            receipt=record["result"],
        )

        marker = record["markers"]["pending_clear"]
        if marker["state"] == "not_started":
            actions.transition_marker(record, "pending_clear", "intent")
            actions.write_pending_action(record)
            marker = record["markers"]["pending_clear"]
        if marker["state"] == "intent":
            actions.transition_marker(
                record,
                "pending_clear",
                "done",
                attempt_id=marker["attempt_id"],
                detail="all durable side effects complete",
            )
        record["phase"] = "complete"
        actions.write_pending_action(record)
        actions.clear_pending_action(record)
        lode["pending_action"] = None
        touch(lode)
        if any(item is lode for item in self.lodes):
            save_lodes(self.lodes)
        else:
            save_archived_lodes(self.archived_lodes)
        self.broadcast({"type": "lode_updated", "lode": lode})

    @staticmethod
    def _set_manual_terminal_status(lode: dict, action_type: str) -> None:
        """Apply the receipt-backed terminal presentation for a manual action."""
        if action_type == "pause":
            lode["state"] = "paused"
            lode["status"] = "Paused by user; worktree retained"
        elif action_type == "restart":
            lode["state"] = "running"
            lode["status"] = f"Starting {lode['stage']}"
        elif action_type == "kill":
            lode["state"] = "error"
            lode["status"] = "Killed by user; worktree retained"
        else:
            lode["state"] = "ready"
            lode["status"] = "Archived by user; worktree retained"

    def _retry_action(self, lode_id: str, conn: socket.socket | None) -> None:
        """Retry only the identity-bound phase named by a blocked record."""
        try:
            record = self._load_action_slot(lode_id)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            if conn:
                self._send_response(conn, {"type": "error", "error": str(error)})
            return
        if record is None:
            if conn:
                self._send_response(conn, {"type": "error", "error": "no pending action"})
            return
        if record["phase"] == "output_blocked":
            if record["recovery"]["kind"] == "publication":
                self._schedule_action_step(record, "output_publish", "publishing_output")
                if conn:
                    self._send_response(conn, {"type": "lode_action_retrying", "lode_id": lode_id})
                return
            if conn:
                self._send_response(
                    conn,
                    {"type": "error", "error": "accepted output requires repair-output"},
                )
            return
        recovery_kind = record["recovery"]["kind"]
        if record["phase"] not in {
            "containment_blocked",
            "ship_blocked",
            "durability_blocked",
            "cleanup_blocked",
        }:
            if conn:
                self._send_response(conn, {"type": "error", "error": "action is not retryable"})
            return
        mapping = {
            "ownership": ("ownership_capture", "capturing_ownership"),
            "containment": ("containment", "observing_containment"),
            "landing": ("ship_landing", "proving_ship_landing"),
            "durability": ("durability_recheck", "rechecking_durability"),
        }
        selected = mapping.get(recovery_kind)
        if recovery_kind in {"spawn", "cleanup"}:
            if not self._require_containment_proof(record, f"{recovery_kind} retry"):
                if conn:
                    self._send_response(
                        conn,
                        {
                            "type": "error",
                            "error": record["recovery"]["message"],
                        },
                    )
                return
            record["recovery"] = {"kind": None, "message": None, "command": None}
            self._continue_action(record)
            selected = None
        if selected is None:
            if recovery_kind not in {"spawn", "cleanup"} and conn:
                self._send_response(conn, {"type": "error", "error": "action is not retryable"})
            elif conn:
                self._send_response(conn, {"type": "lode_action_retrying", "lode_id": lode_id})
            return
        marker_name, phase = selected
        if recovery_kind == "ownership" and record["markers"]["pane_close"]["state"] == "blocked":
            marker_name, phase = "pane_close", "closing_pane"
        if recovery_kind == "containment":
            if record["containment"]["state"] == "blocked":
                record["containment"] = teardown.normalize_legacy_blocked_containment(
                    record,
                    now_ns=time.monotonic_ns,
                )
            cursor = record["containment"]["state"]
            phase = _containment_phase_for_cursor(cursor)
            containment_marker = record["markers"]["containment"]
            if phase is None or containment_marker["state"] == "done":
                self._block_action(
                    record,
                    "containment",
                    "containment",
                    f"containment retry refused: cursor {cursor} with marker "
                    f"{containment_marker['state']}",
                )
                if conn:
                    self._send_response(
                        conn,
                        {"type": "error", "error": record["recovery"]["message"]},
                    )
                return
            distinct_pane_root = (
                record["ownership"]["pane"]["root_process"]["pid"]
                != record["ownership"]["supervisor"]["pid"]
            )
            required_kill_markers = []
            if cursor == "kill_pending":
                if record["containment"]["last_cgroup_observation"] != "empty":
                    required_kill_markers.append("scope_kill")
                if (
                    record["containment"]["last_supervisor_observation"] != "gone"
                    or distinct_pane_root
                ):
                    required_kill_markers.append("supervisor_kill")
            for kill_marker in required_kill_markers:
                state = record["markers"][kill_marker]["state"]
                if state in {"not_started", "blocked"}:
                    actions.transition_marker(record, kill_marker, "intent")
                elif state != "intent":
                    self._block_action(
                        record,
                        "containment",
                        "containment",
                        f"containment retry refused: {kill_marker} marker is {state}",
                    )
                    if conn:
                        self._send_response(
                            conn,
                            {"type": "error", "error": record["recovery"]["message"]},
                        )
                    return
            record["containment"]["last_error"] = None
        self._schedule_action_step(record, marker_name, phase)
        if conn:
            self._send_response(
                conn,
                {"type": "lode_action_retrying", "lode_id": lode_id},
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
            record = self._load_action_slot(lode_id)
        except (OSError, ValueError, json.JSONDecodeError):
            acknowledge(False, "no_pending_output_failure")
            return
        if record is None or record["action_type"] != "completion" or record["output"] is None:
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
            "expected_generation": record["expected_generation"],
            "next_action": record["next_action"],
        }
        if any(message.get(key) != value for key, value in expected_identity.items()):
            acknowledge(False, "action_mismatch")
            return

        lode = self._find_action_lode(lode_id)
        active_identity = bool(
            lode
            and any(item is lode for item in self.lodes)
            and lode.get("stage") == record["stage"]
            and lode.get("run_generation") == record["expected_generation"]
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
            message.get("digest_algorithm") != actions.DIGEST_ALGORITHM
            or not isinstance(digest_hex, str)
            or not secrets.compare_digest(digest_hex, output["digest_hex"])
            or not secrets.compare_digest(hashlib.sha256(data).hexdigest(), output["digest_hex"])
        ):
            acknowledge(False, "output_mismatch")
            return

        try:
            output["staged_identity"] = actions.repair_staged_output(record, data)
            output["failure"] = None
            self._schedule_action_step(record, "output_publish", "publishing_output")
        except Exception as error:
            logger.error("Completion output repair failed lode=%s: %s", lode_id, error)
            acknowledge(False, "repair_failed")
            return
        acknowledge(True, "accepted")

    def _reconcile_action_records(self) -> None:
        """Load durable fences before ordinary startup reconciliation."""
        self._startup_actions = []
        seen = set()
        for lode in [*self.lodes, *self.archived_lodes]:
            if lode["id"] in seen:
                continue
            seen.add(lode["id"])
            if not _pending_action_file_exists(lode["id"]):
                projection = lode.get("pending_action")
                action_id = projection.get("action_id") if isinstance(projection, dict) else None
                if isinstance(action_id, str):
                    try:
                        receipt = actions.find_action_result(lode, action_id)
                    except ValueError:
                        receipt = None
                    if receipt is not None and receipt["action_type"] != "completion":
                        lode["pending_action"] = None
                        self._set_manual_terminal_status(lode, receipt["action_type"])
                        touch(lode)
                try:
                    actions.collect_orphaned_staging(lode["id"], None)
                except ValueError:
                    pass
                continue
            try:
                record = self._load_action_slot(lode["id"])
            except actions.LegacyPendingActionError as error:
                lode["state"] = "teardown"
                lode["status"] = f"Teardown blocked: {error}"
                lode["pending_action"] = {
                    "action_type": "legacy-v1",
                    "phase": "blocked",
                    "status": lode["status"],
                }
                lode["active"] = False
                touch(lode)
                _log_state_change(
                    lode["id"], lode["state"], lode["status"], "action_startup_legacy"
                )
                continue
            except (OSError, ValueError, json.JSONDecodeError) as error:
                lode["state"] = "teardown"
                lode["status"] = (
                    "Teardown blocked: malformed pending action must be repaired or drained "
                    f"before upgrade: {error}"
                )
                lode["pending_action"] = {
                    "action_type": "invalid",
                    "phase": "blocked",
                    "status": lode["status"],
                }
                lode["active"] = False
                touch(lode)
                _log_state_change(
                    lode["id"], lode["state"], lode["status"], "completion_startup_invalid"
                )
                continue
            if record is None:
                continue
            self._cancel_generation_guard(lode["id"], record["expected_generation"])
            if record["markers"]["pending_clear"]["state"] == "done":
                self._clear_completed_action(record)
                continue
            lode["state"] = "teardown"
            lode["status"] = actions.action_status(record)
            lode["active"] = False
            touch(lode)
            _log_state_change(
                lode["id"], lode["state"], lode["status"], "completion_startup_reconcile"
            )
            actions.collect_orphaned_staging(lode["id"], record)
            self._startup_actions.append(lode["id"])
        save_lodes(self.lodes)
        save_archived_lodes(self.archived_lodes)

    def _resume_action(self, lode_id: str, *, startup: bool = False) -> None:
        try:
            record = self._load_action_slot(lode_id)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            logger.error("Completion startup reconciliation blocked lode=%s: %s", lode_id, error)
            return
        if record is None:
            return
        if (
            startup
            and record["action_type"] in {"kill", "archive"}
            and record["durability"]["required"]
            and record["markers"]["durability_recheck"]["state"] == "done"
            and record["markers"]["archive"]["state"] != "done"
            and self._find_lode(lode_id) is not None
        ):
            record["markers"]["durability_recheck"] = actions.new_marker()
            record["durability"]["final"] = {
                "outcome": "not_required",
                "count": 0,
                "basis": "pending post-containment recheck",
                "error": None,
                "checked_at_ms": None,
            }
            record["phase"] = "publishing_terminal"
            self._persist_action(record, via="action_startup:durability_recheck")
        if record["phase"] in {
            "output_blocked",
            "containment_blocked",
            "ship_blocked",
            "durability_blocked",
            "cleanup_blocked",
        }:
            return
        if (
            record["action_type"] == "completion"
            and record["markers"]["output_publish"]["state"] != "done"
        ):
            selected = ("output_publish", "publishing_output")
        elif record["markers"]["ownership_capture"]["state"] != "done":
            selected = ("ownership_capture", "capturing_ownership")
        elif record["markers"]["pane_close"]["state"] != "done":
            selected = ("pane_close", "closing_pane")
        elif record["markers"]["containment"]["state"] != "done":
            cursor = record["containment"]["state"]
            phase = _containment_phase_for_cursor(cursor)
            if phase is None:
                self._block_action(
                    record,
                    "containment",
                    "containment",
                    f"action resume refused: containment cursor {cursor} cannot be observed",
                )
                return
            selected = ("containment", phase)
        else:
            if record["markers"]["pending_clear"]["state"] == "done":
                self._clear_completed_action(record)
                return
            if not self._require_containment_proof(record, "action resume"):
                return
            self._continue_action(record)
            return
        if record["markers"][selected[0]]["state"] != "blocked":
            self._schedule_action_step(record, *selected)

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
        """Reconcile stale ownership against authoritative tmux evidence."""
        changed = False
        gate_artifacts: list[dict] = []
        for lode in self.lodes:
            before = copy.deepcopy(lode)
            generation = lode.get("run_generation")
            pane = lode.get("tmux_pane")
            lode["active"] = False

            action_owned = lode.get("pending_action") is not None or _pending_action_file_exists(
                lode["id"]
            )
            stronger_state = (
                lode.get("stage") == "shipped"
                or lode.get("state") in {"error", "gated", "stuck", "paused", "teardown"}
                or is_terminal_failure_kind(lode.get("failure_kind"))
                or action_owned
            )
            if generation and (lode["id"], generation) in self.pending_disconnects:
                pass
            elif stronger_state:
                pass
            elif isinstance(pane, str) and re.fullmatch(r"%[0-9]+", pane):
                liveness = pane_liveness(pane)
                if liveness is Liveness.ALIVE:
                    if lode.get("state") not in {"new", "ready"}:
                        if generation:
                            if lode.get("state") != "reconnecting":
                                lode["reconnect_prior_state"] = lode.get("state")
                                lode["reconnect_prior_status"] = lode.get("status", "")
                            lode["state"] = "reconnecting"
                            lode["status"] = (
                                f"Runner pane {pane} survived server replacement; "
                                "waiting for registration"
                            )
                            lode["spawn_disposition"] = None
                        else:
                            if self._gate_unknown_startup_lode(lode):
                                gate_artifacts.append(lode)
                elif liveness is Liveness.GONE:
                    lode["tmux_pane"] = None
                    lode["pid"] = None
                    lode["oom_scope"] = None
                    lode["spawn_disposition"] = None
                    if lode.get("state") == "ready":
                        lode["status"] = (
                            f"Recorded handoff pane {pane} is gone; "
                            "waiting for a deliberate handoff"
                        )
                    else:
                        lode["state"] = "error"
                        if type(lode.get("errored_at")) is not int:
                            lode["errored_at"] = current_time_ms()
                        lode["status"] = (
                            f"Recorded runner pane {pane} is gone after server replacement; "
                            f"inspect with: hop lode status {lode['id']} before starting "
                            "another runner"
                        )
                else:
                    if self._gate_unknown_startup_lode(lode):
                        gate_artifacts.append(lode)
            else:
                no_runner_identity = not generation and pane is None and lode.get("pid") is None
                intentionally_new = lode.get("state") == "new" and no_runner_identity
                bounded_ready = lode.get("state") == "ready" and no_runner_identity
                if not intentionally_new and not bounded_ready:
                    if self._gate_unknown_startup_lode(lode):
                        gate_artifacts.append(lode)
                if pane is None and not generation:
                    lode["pid"] = None

            if lode != before:
                touch(lode)
                changed = True

        if changed:
            save_lodes(self.lodes)
            for lode in gate_artifacts:
                _sync_gate_artifact(lode)

    @staticmethod
    def _gate_unknown_startup_lode(lode: dict) -> bool:
        """Persist inspection-only authority when pane liveness is unknowable."""
        status = (
            "Runner pane liveness is unverified after server replacement; inspect with: "
            f"tmux list-panes -a and hop lode status {lode['id']}. Do not restart."
        )
        changed = set_lode_gate_fields(lode, body=status, kind="explicit", status=status)
        lode["spawn_disposition"] = "unknown"
        return changed

    def _consume_failed_oom_units(self) -> None:
        """Consume retained failed scope evidence before startup reconciliation."""
        systemctl = oom.find_systemctl()
        if not systemctl:
            for lode in self.lodes:
                if self._generation_has_teardown_intent(lode["id"], lode.get("run_generation")):
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
            if self._generation_has_teardown_intent(lode["id"], lode.get("run_generation")):
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
        pending_state: str | None = None,
        foreground: bool = False,
        spawn_updates: dict | None = None,
        pre_spawn: Callable[[], None] | None = None,
        allow_terminal_recovery: bool = False,
    ) -> tuple[SpawnOutcome, str | None]:
        """Spawn only from authoritative pane evidence and persist its disposition."""
        if pending_state is None:
            pending_state = "new" if lode.get("state") == "new" else "ready"
        if pending_state not in {"new", "ready"}:
            raise ValueError("ordinary spawn pending state must be new or ready")

        def persist_outcome(outcome: SpawnOutcome, pane_id: str | None = None) -> None:
            lode["active"] = False
            lode["spawn_disposition"] = outcome.value
            gate_changed = False
            if outcome is SpawnOutcome.SPAWNED:
                if not terminal_recovery:
                    lode["state"] = pending_state
                    if pending_state == "new":
                        lode["status"] = (
                            f"Runner pane {pane_id} created; waiting for startup registration"
                        )
                    else:
                        lode["status"] = (
                            f"Runner pane {pane_id} created; waiting for handoff registration"
                        )
                lode["tmux_pane"] = pane_id
            elif outcome is SpawnOutcome.PROJECT_MISSING:
                project = lode.get("project", "")
                lode["state"] = "error"
                if type(lode.get("errored_at")) is not int:
                    lode["errored_at"] = current_time_ms()
                lode["status"] = (
                    f"Project '{project}' is unavailable; restore its registration/path, "
                    f"then run: hop lode restart {lode['id']}."
                )
            elif outcome is SpawnOutcome.ALREADY_LIVE:
                status = (
                    f"Runner pane {pane_id} is already live; attach with: "
                    f"hop lode peek {lode['id']}. No new pane was started."
                )
                gate_changed = set_lode_gate_fields(
                    lode, body=status, kind="explicit", status=status
                )
            elif outcome is SpawnOutcome.PROVEN_NO_PANE:
                lode["state"] = "error"
                if type(lode.get("errored_at")) is not int:
                    lode["errored_at"] = current_time_ms()
                lode["status"] = (
                    "tmux did not create a runner pane; repair or start tmux, then run: "
                    f"hop lode restart {lode['id']}."
                )
                lode["run_generation"] = None
                lode["oom_scope"] = None
                lode["tmux_pane"] = None
                lode["pid"] = None
            else:
                status = (
                    "Runner pane creation is unverified and a pane may be live; inspect with: "
                    f"tmux list-panes -a and hop lode status {lode['id']}. "
                    "Do not launch another runner."
                )
                gate_changed = set_lode_gate_fields(
                    lode, body=status, kind="explicit", status=status
                )
            # Gate publication uses the ordinary state helper, which clears a
            # stale disposition; this outcome remains the authoritative one.
            lode["spawn_disposition"] = outcome.value
            touch(lode)
            save_lodes(self.lodes)
            if gate_changed:
                _sync_gate_artifact(lode)
            _log_state_change(lode["id"], lode["state"], lode["status"], "spawn")
            self.broadcast({"type": "lode_updated", "lode": lode})

        generation = lode.get("run_generation")
        if self._lode_has_pending_action(lode["id"]):
            logger.warning("lode %s: spawn suppressed by accepted action", lode["id"])
            return SpawnOutcome.UNKNOWN, None
        if generation and (lode["id"], generation) in self.pending_disconnects:
            logger.warning("lode %s: spawn suppressed while scope result is pending", lode["id"])
            return SpawnOutcome.UNKNOWN, None
        terminal_recovery = is_terminal_failure_kind(lode.get("failure_kind"))
        if terminal_recovery and not allow_terminal_recovery:
            logger.warning("lode %s: automatic spawn suppressed by terminal failure", lode["id"])
            return SpawnOutcome.UNKNOWN, None

        pane = lode.get("tmux_pane")
        if pane:
            if re.fullmatch(r"%[0-9]+", pane) is None:
                logger.warning("lode %s: recorded pane identity is malformed", lode["id"])
                persist_outcome(SpawnOutcome.UNKNOWN, pane)
                return SpawnOutcome.UNKNOWN, None
            liveness = pane_liveness(pane)
            if liveness is Liveness.ALIVE:
                logger.warning(
                    "lode %s: runner already live in pane %s; attach instead of spawning",
                    lode["id"],
                    pane,
                )
                persist_outcome(SpawnOutcome.ALREADY_LIVE, pane)
                return SpawnOutcome.ALREADY_LIVE, None
            if liveness is Liveness.UNKNOWN:
                logger.warning(
                    "lode %s: tmux liveness unknown for pane %s; refusing spawn",
                    lode["id"],
                    pane,
                )
                persist_outcome(SpawnOutcome.UNKNOWN, pane)
                return SpawnOutcome.UNKNOWN, None
            lode["run_generation"] = None
            lode["oom_scope"] = None
            lode["tmux_pane"] = None
            lode["pid"] = None
        elif generation and terminal_recovery and allow_terminal_recovery:
            lode["run_generation"] = None
            lode["oom_scope"] = None
        elif generation:
            logger.warning("lode %s: generation exists without a pane identity", lode["id"])
            persist_outcome(SpawnOutcome.UNKNOWN)
            return SpawnOutcome.UNKNOWN, None

        if project_path is None:
            persist_outcome(SpawnOutcome.PROJECT_MISSING)
            return SpawnOutcome.PROJECT_MISSING, None

        lode["active"] = False
        lode["tmux_pane"] = None
        lode["pid"] = None
        _clear_spawn_refusal(lode)
        if spawn_updates:
            lode.update(spawn_updates)
        if not terminal_recovery:
            lode["state"] = pending_state
        run_generation = uuid.uuid4().hex
        lode["run_generation"] = run_generation
        lode.pop("protocol_error", None)
        lode["oom_scope"] = (
            oom.scope_unit_name(lode["id"], run_generation) if oom.is_linux() else None
        )
        touch(lode)
        save_lodes(self.lodes)
        if pre_spawn:
            pre_spawn()

        pane_env = {RUN_GENERATION_ENV: run_generation}
        if lode.get("oom_scope"):
            pane_env[OOM_SCOPE_ENV] = lode["oom_scope"]
        window_outcome, pane_id = spawn_lode_processor(
            lode["id"],
            project_path,
            foreground=foreground,
            env=pane_env,
        )
        if window_outcome is WindowSpawnOutcome.SPAWNED:
            outcome = SpawnOutcome.SPAWNED
        elif window_outcome is WindowSpawnOutcome.PROVEN_NO_PANE:
            outcome = SpawnOutcome.PROVEN_NO_PANE
        else:
            outcome = SpawnOutcome.UNKNOWN
        persist_outcome(outcome, pane_id)
        return outcome, pane_id if outcome is SpawnOutcome.SPAWNED else None

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
        validate_lode_coder_data(self.lodes, "active.jsonl")
        validate_lode_coder_data(self.archived_lodes, "archived.jsonl")
        validate_lode_driver_data(self.lodes, "active.jsonl")
        validate_lode_driver_data(self.archived_lodes, "archived.jsonl")
        self.backlog = load_backlog()
        self.projects = get_active_projects()

        self._reconcile_action_records()
        self._consume_failed_oom_units()
        self._reconcile_startup_lodes()

        # Runs only while lock-held; UNKNOWN panes do not block shipped auto-archive.
        shipped = [
            lode
            for lode in self.lodes
            if lode.get("stage") == "shipped"
            and not _pending_action_file_exists(lode["id"])
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
                save_archived_lodes(self.archived_lodes)

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

            for lode_id in self._startup_actions:
                self._enqueue_event({"type": "_action_reconcile", "lode_id": lode_id})

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
    _READ_ONLY_TYPES = frozenset(
        {
            "connect",
            "ping",
            "coder_capabilities",
            "lode_list",
            "backlog_list",
            "archived_list",
        }
    )

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
        if self._generation_has_teardown_intent(lode_id, run_generation):
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
            self._enqueue_event({"type": "_action_reconcile", "lode_id": lode_id})
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
        if self._generation_has_teardown_intent(lode["id"], run_generation):
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
            or type(lode.get("errored_at")) is not int
        )
        lode["state"] = "error"
        lode["status"] = status
        lode["failure_kind"] = failure_kind
        lode["active"] = False
        lode["tmux_pane"] = None
        lode["pid"] = None
        if type(lode.get("errored_at")) is not int:
            lode["errored_at"] = current_time_ms()
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
            if self._generation_has_teardown_intent(lode_id, run_generation):
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

    def _prepare_worktree_reap(self, lode: dict, trigger: str) -> Path | None:
        """Capture a reap path once for the cleanup primitive."""
        worktree_reap = lode.get("worktree_reap")
        if isinstance(worktree_reap, dict):
            raw_path = worktree_reap.get("path")
            if isinstance(raw_path, str) and raw_path:
                changed = False
                if worktree_reap.get("trigger") not in {"shipped", "killed", "error"}:
                    worktree_reap["trigger"] = trigger
                    changed = True
                if changed:
                    touch(lode)
                return Path(raw_path)
        else:
            worktree_reap = None

        resolution = resolve_worktree_path(lode)
        worktree_path = resolution["path"]
        if worktree_path is None or not worktree_path.is_dir():
            logger.warning("Worktree reap skipped for %s: worktree path is unavailable", lode["id"])
            return None

        if worktree_reap is None:
            worktree_reap = {}
            lode["worktree_reap"] = worktree_reap
        worktree_reap.update(
            {
                "trigger": trigger,
                "path": str(worktree_path),
                "worktree_removed_at": None,
                "reaped_at": None,
            }
        )
        touch(lode)
        return worktree_path

    def _cleanup_worktree(self, lode: dict, *, trigger: str = "shipped") -> bool:
        """Remove one confirmed-clean worktree and its branch, retrying partial cleanup."""
        lode_id = lode["id"]
        worktree_path = self._prepare_worktree_reap(lode, trigger)
        if worktree_path is None:
            return False
        worktree_reap = lode["worktree_reap"]
        project_name = lode.get("project", "")
        if not project_name:
            logger.warning("Worktree reap skipped for %s: project is unavailable", lode_id)
            return False
        project = find_project(project_name)
        if not project:
            logger.warning("Worktree reap skipped for %s: project not found", lode_id)
            return False

        if type(worktree_reap.get("worktree_removed_at")) is not int:
            if is_dirty(str(worktree_path)) is not False:
                logger.warning(
                    "Worktree reap skipped for %s: worktree is dirty or cleanliness could not be "
                    "proven; retaining %s",
                    lode_id,
                    worktree_path,
                )
                return False
            if not remove_worktree(project.path, str(worktree_path)):
                logger.warning(
                    "Worktree reap failed for %s: could not remove %s", lode_id, worktree_path
                )
                return False
            worktree_reap["worktree_removed_at"] = current_time_ms()
            touch(lode)

        branch = lode.get("branch", "") or f"hopper-{lode_id}"
        exists = branch_exists(project.path, branch)
        if exists is None:
            logger.warning(
                "Worktree reap incomplete for %s: branch existence is unverified", lode_id
            )
            return False
        if exists and not delete_branch(project.path, branch):
            logger.warning(
                "Worktree reap incomplete for %s: could not remove branch %s", lode_id, branch
            )
            return False

        worktree_reap["reaped_at"] = current_time_ms()
        touch(lode)
        logger.info("Worktree reaped for %s", lode_id)
        return True

    def _save_reap_progress(self, owner: list[dict]) -> None:
        """Persist one lode's reap progress in its owning collection."""
        if owner is self.lodes:
            save_lodes(owner)
        else:
            save_archived_lodes(owner)

    def _maybe_reap_worktrees(self) -> None:
        """Run the reap pass at a bounded cadence independent of event polling."""
        now = time.monotonic()
        if (
            self._last_worktree_reap_sweep_at is not None
            and now - self._last_worktree_reap_sweep_at < WORKTREE_REAP_SWEEP_INTERVAL_SEC
        ):
            return
        self._last_worktree_reap_sweep_at = now
        self._reap_eligible_worktrees()

    def _reap_eligible_worktrees(self) -> None:
        """Reap terminal, inactive lode worktrees that have met their retention policy."""
        now_ms = current_time_ms()
        candidates: dict[str, tuple[dict, list[dict]]] = {}
        for lode in self.lodes:
            lode_id = lode.get("id")
            if isinstance(lode_id, str):
                candidates[lode_id] = (lode, self.lodes)
        for lode in self.archived_lodes:
            lode_id = lode.get("id")
            if isinstance(lode_id, str):
                candidates[lode_id] = (lode, self.archived_lodes)

        for lode, owner in candidates.values():
            reap_before = copy.deepcopy(lode.get("worktree_reap"))
            completed = False
            try:
                worktree_reap = lode.get("worktree_reap")
                if isinstance(worktree_reap, dict) and type(worktree_reap.get("reaped_at")) is int:
                    continue
                if lode.get("pending_action") is not None:
                    continue
                lode_id = lode["id"]
                if lode.get("active") or lode_id in self.lode_clients:
                    continue
                generation = lode.get("run_generation")
                if generation and (lode_id, generation) in self.pending_disconnects:
                    continue

                trigger = None
                shipped_at = lode.get("shipped_at")
                errored_at = lode.get("errored_at")
                action_results = lode.get("action_results")
                last_action = (
                    action_results[-1]
                    if isinstance(action_results, list) and action_results
                    else None
                )
                if (
                    lode.get("stage") == "shipped"
                    and not is_terminal_failure_kind(lode.get("failure_kind"))
                    and type(shipped_at) is int
                    and now_ms - shipped_at >= SHIPPED_WORKTREE_REAP_GRACE_MS
                ):
                    trigger = "shipped"
                elif isinstance(last_action, dict) and last_action.get("action_type") == "kill":
                    trigger = "killed"
                elif (
                    lode.get("state") == "error"
                    and not is_terminal_failure_kind(lode.get("failure_kind"))
                    and type(errored_at) is int
                    and now_ms - errored_at >= ERROR_WORKTREE_REAP_GRACE_MS
                ):
                    trigger = "error"
                if trigger is None:
                    continue

                worktree_path = self._prepare_worktree_reap(lode, trigger)
                if worktree_path is None:
                    continue

                worktree_reap = lode["worktree_reap"]
                if (
                    trigger == "killed"
                    and type(worktree_reap.get("worktree_removed_at")) is not int
                ):
                    count, _basis = git.unpushed_commits(str(worktree_path))
                    if count != 0:
                        logger.warning(
                            "Worktree reap skipped for %s: unpushed commit count is %s",
                            lode_id,
                            "unknown" if count is None else count,
                        )
                        continue

                completed = self._cleanup_worktree(lode, trigger=trigger)
            except Exception:
                logger.exception("Worktree reap failed unexpectedly for lode=%s", lode.get("id"))
            finally:
                if lode.get("worktree_reap") != reap_before:
                    self._save_reap_progress(owner)
                if completed:
                    self.broadcast({"type": "lode_updated", "lode": lode})

    def _register_lode_client(
        self,
        lode_id: str,
        conn: socket.socket,
        tmux_pane: str | None = None,
        pid: int | None = None,
        run_generation: str | None = None,
        proof_mode: str | None = None,
        actual_unit: str | None = None,
    ) -> bool:
        """Register a client as owning a lode.

        Sets active=True on the lode and disconnects any stale owner.
        Runs on the event loop thread — no lock needed for state mutations.
        """
        lode = self._find_lode(lode_id)
        if not lode or run_generation != lode.get("run_generation"):
            return False
        registration_grace = _lifecycle_grace_pending(lode)
        terminal_recovery = is_terminal_failure_kind(lode.get("failure_kind"))
        if terminal_recovery and (not tmux_pane or tmux_pane != lode.get("tmux_pane")):
            return False
        if proof_mode == "linux-strict":
            if not actual_unit or actual_unit != lode.get("oom_scope"):
                return False
        elif proof_mode in {
            "linux-degraded",
            "darwin-bounded",
            "other-bounded-no-birth",
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

        if tmux_pane:
            lode["tmux_pane"] = tmux_pane
        if pid:
            lode["pid"] = pid
        _clear_spawn_refusal(
            lode,
            clear_status=registration_grace or terminal_recovery,
        )
        lode.pop("reconnect_prior_state", None)
        lode.pop("reconnect_prior_status", None)
        if terminal_recovery or registration_grace:
            if terminal_recovery:
                lode["failure_kind"] = None
            lode["state"] = "running"
            lode["status"] = f"Starting {lode.get('stage', '')}"
            cause = "register_recovery" if terminal_recovery else "register_grace"
            _log_state_change(lode_id, "running", lode["status"], cause)
        # Write active only after any eligible state transition, so new/active=True
        # and ready/active=True cannot be observed even through shared references.
        lode["active"] = True
        touch(lode)
        save_lodes(self.lodes)
        self.broadcast({"type": "lode_updated", "lode": lode})

        return True

    def _handle_read_only(self, message: dict, conn: socket.socket) -> None:
        """Handle read-only messages inline (from any client thread)."""
        self._request_context.exchange_id = message.get("exchange_id")
        self._request_context.message = message
        msg_type = message.get("type")

        if msg_type == "connect":
            lode_id = message.get("lode_id")
            response: dict = {
                "type": "connected",
                "tmux": self.tmux_location,
                STAGE_DRIVER_CAPABILITIES_KEY: {
                    "version": STAGE_DRIVER_PROTOCOL_VERSION,
                    "drivers": list(RUNNABLE_STAGE_DRIVERS),
                },
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

        elif msg_type == "coder_capabilities":
            self._send_response(
                conn,
                {"type": "coder_capabilities", "providers": list(CODER_PROVIDERS)},
            )

        elif msg_type == "lode_list":
            self._send_response(conn, {"type": "lode_list", "lodes": self.lodes})

        elif msg_type == "backlog_list":
            items_data = [item.to_dict() for item in self.backlog]
            self._send_response(conn, {"type": "backlog_list", "items": items_data})

        elif msg_type == "archived_list":
            self._send_response(conn, self._archived_list_page(message))

    def _archived_list_page(self, msg: dict) -> dict:
        """Return one newest-first page of archived lodes plus the unpaged total.

        The page is cut here rather than in the CLI because the archive grows
        without bound and the whole list crosses the socket either way. A
        request that names no limit is an older CLI and still receives every
        row, which is the behaviour it was written against.
        """
        rows: list[dict] = self.archived_lodes
        project = msg.get("project")
        if isinstance(project, str) and project:
            rows = [lode for lode in rows if lode.get("project") == project]
        total = len(rows)
        offset = _page_bound(msg.get("offset"), default=0)
        limit = _page_bound(msg.get("limit"), default=None)
        page = sorted(rows, key=lambda lode: lode.get("updated_at") or 0, reverse=True)[offset:]
        if limit is not None:
            page = page[:limit]
        return {
            "type": "archived_list",
            "lodes": page,
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def _promote_backlog_item(
        self,
        item: BacklogItem,
        scope: str = "",
        *,
        coder_provider: str,
        supervisor_provider: str = "claude",
    ) -> dict | None:
        """Promote a backlog item to a lode. Returns the new lode dict."""
        proj = find_project(item.project)
        if proj and proj.disabled:
            logger.warning(
                "Refusing to promote backlog %s for disabled project %s",
                item.id,
                item.project,
            )
            return None
        lode = create_lode(
            self.lodes,
            item.project,
            scope or item.description,
            coder_provider=coder_provider,
            driver=supervisor_provider,
        )
        lode["backlog"] = item.to_dict()
        save_lodes(self.lodes)
        logger.info(f"Lode {lode['id']} promoted from backlog {item.id}")
        self.broadcast({"type": "lode_created", "lode": lode})
        remove_backlog_item(self.backlog, item.id)
        self.broadcast({"type": "backlog_removed", "item": item.to_dict()})
        project_path = proj.path if proj else None
        self._gated_spawn(lode, project_path, pending_state="new", foreground=False)
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

        if self._generation_has_teardown_intent(lode_id, run_generation):
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
        self._request_context.message = message
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

        def refuse_stage_protocol(lode: dict | None, reason: str) -> None:
            """Record a visible fenced/legacy protocol refusal when a lode remains known."""
            if lode is None:
                return
            _persist_protocol_error(lode, reason)
            save_lodes(self.lodes)
            logger.warning("Stage protocol refusal lode=%s reason=%s", lode["id"], reason)
            self.broadcast({"type": "lode_updated", "lode": lode})

        lode_id = message.get("lode_id")
        runner_gate_publication = msg_type == "lode_publish_gate" and (
            "run_generation" in message or message.get("kind") in {"native_question", "idle_park"}
        )
        if (
            msg_type in RUNNER_MUTATION_TYPES
            and msg_type != "lode_action"
            and (msg_type != "lode_publish_gate" or runner_gate_publication)
        ):
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
                if msg_type in {"lode_set_claude_started", "lode_bind_stage_session"}:
                    refuse_stage_protocol(lode, reason)
                acknowledge_mutation(False, reason)
                return
            if self._generation_has_teardown_intent(lode_id, message.get("run_generation")):
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
                if msg_type in {"lode_set_claude_started", "lode_bind_stage_session"}:
                    refuse_stage_protocol(lode, "expected_teardown")
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
            if msg_type in HELD_RUNNER_MUTATION_TYPES and _lifecycle_grace_pending(lode):
                acknowledge_mutation(False, "lifecycle_grace_pending")
                return
            if msg_type == "lode_set_worktree_path":
                reason, canonical_path = _validate_worktree_path_publication(lode, message)
                if reason:
                    acknowledge_mutation(False, reason)
                    return
                message["worktree_path"] = canonical_path
            if msg_type not in {
                "lode_register",
                "lode_supervisor_register",
                "lode_set_state",
                "lode_set_claude_started",
                "lode_bind_stage_session",
                "lode_publish_gate",
            }:
                acknowledge_mutation(True, "accepted")

        if msg_type == "_client_disconnect":
            self._on_client_disconnect(conn)

        elif msg_type == "_registration_capture_result":
            self._handle_registration_capture_result(message, conn)

        elif msg_type == "_action_acceptance_result":
            self._handle_action_acceptance_result(message, conn)

        elif msg_type == "_action_step_result":
            self._handle_action_step_result(message)

        elif msg_type == "_action_reconcile":
            lode_id = message.get("lode_id")
            if isinstance(lode_id, str):
                self._resume_action(lode_id, startup=True)

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

            matches = _collapse_lode_snapshot_matches(
                [(lode, False) for lode in find_lodes_by_prefix(self.lodes, prefix)]
                + [(lode, True) for lode in find_lodes_by_prefix(self.archived_lodes, prefix)]
            )
            if not matches:
                response = {"type": "lode_snapshot", "result": "absent"}
            elif len(matches) == 1:
                lode, archived = matches[0]
                snapshot = dict(lode)
                snapshot["archived"] = archived
                response = {
                    "type": "lode_snapshot",
                    "result": "found",
                    "lode": snapshot,
                }
            else:
                response = {
                    "type": "lode_snapshot",
                    "result": "ambiguous",
                    "matches": [lode["id"] for lode, _archived in matches],
                }
            if conn:
                self._send_response(conn, response)

        elif msg_type == "lode_supervisor_register":
            lode_id = message.get("lode_id")
            lode = self._find_lode(lode_id) if lode_id else None
            if lode and self._adopt_action_spawn_receipt(lode, message):
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
                if lode and self._adopt_action_spawn_receipt(lode, message):
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

        elif msg_type == "lode_action":
            self._handle_lode_action(message, conn)

        elif msg_type == "lode_complete":
            self._send_action_ack(
                conn,
                outcome="refused",
                reason="protocol_upgrade_required",
            )

        elif msg_type == "lode_run_result":
            self._handle_lode_run_result(message, conn)

        elif msg_type == "lode_repair_output":
            self._repair_completion_output(message, conn)

        elif msg_type == "lode_create":
            if "coder_provider" not in message:
                if conn:
                    self._send_response(
                        conn,
                        {"type": "error", "error": "lode_create requires coder_provider"},
                    )
                return
            try:
                requested_driver = validate_supervisor_provider(message.get("driver", "claude"))
            except ValueError as error:
                if conn:
                    self._send_response(conn, {"type": "error", "error": str(error)})
                return
            project = message.get("project", "")
            scope = message.get("scope", "")
            originating_extro_sid = message.get("originating_extro_sid")
            try:
                coder_provider = validate_coder_provider(message["coder_provider"])
            except ValueError as error:
                if conn:
                    self._send_response(conn, {"type": "error", "error": str(error)})
                return
            readiness = coder_check(coder_provider)
            if not readiness["ready"]:
                if conn:
                    self._send_response(
                        conn,
                        {
                            "type": "error",
                            "error": coder_unavailable_message(
                                coder_provider, readiness.get("error")
                            ),
                        },
                    )
                return
            supervisor_readiness = supervisor_check(requested_driver)
            if not supervisor_readiness["ready"]:
                if conn:
                    self._send_response(
                        conn,
                        {
                            "type": "error",
                            "error": supervisor_unavailable_message(
                                requested_driver, supervisor_readiness.get("error")
                            ),
                        },
                    )
                return
            proj = find_project(project)
            if proj and proj.disabled:
                logger.warning("Refusing to create lode for disabled project %s", project)
                if conn:
                    self._send_response(
                        conn,
                        {"type": "error", "error": disabled_project_message(proj)},
                    )
                return
            create_kwargs = {
                "originating_extro_sid": originating_extro_sid,
                "coder_provider": coder_provider,
            }
            if requested_driver != "claude":
                create_kwargs["driver"] = requested_driver
            lode = create_lode(self.lodes, project, scope, **create_kwargs)
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
                self._gated_spawn(lode, project_path, pending_state="new", foreground=False)

        elif msg_type == "lode_set_stage":
            lode_id = message.get("lode_id")
            stage = message.get("stage")
            if lode_id and stage:
                lode = update_lode_stage(self.lodes, lode_id, stage)
                if lode:
                    logger.info(f"Lode {lode_id} stage={stage}")
                    self.broadcast({"type": "lode_updated", "lode": lode})

        elif msg_type == "lode_archive":
            self._send_action_ack(
                conn,
                outcome="refused",
                reason="protocol_upgrade_required",
            )

        elif msg_type == "lode_pause":
            self._send_action_ack(
                conn,
                outcome="refused",
                reason="protocol_upgrade_required",
            )

        elif msg_type == "lode_resume":
            lode_id = message.get("lode_id")
            lode = self._find_lode(lode_id) if lode_id else None
            if not lode:
                if conn:
                    self._send_response(
                        conn, {"type": "error", "error": f"lode {lode_id} not found"}
                    )
                return
            worktree_reap = lode.get("worktree_reap")
            if isinstance(worktree_reap, dict) and type(worktree_reap.get("reaped_at")) is int:
                if conn:
                    self._send_response(
                        conn,
                        {
                            "type": "error",
                            "error": format_worktree_reaped_status(lode_id, worktree_reap),
                        },
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
            gate = lode_gate(lode)
            if gate is not None:
                if gate["kind"] != "idle_park":
                    if conn:
                        self._send_response(
                            conn,
                            {"type": "error", "error": "current gate requires feedback"},
                        )
                    return
                cleared = clear_lode_gate(
                    self.lodes,
                    lode_id,
                    gate_epoch=gate["epoch"],
                    kind="idle_park",
                    state="ready",
                    status="Resuming parked lode",
                )
                if cleared is None:
                    if conn:
                        self._send_response(conn, {"type": "error", "error": "stale gate"})
                    return
                _sync_gate_artifact(cleared)
                self.broadcast({"type": "lode_updated", "lode": cleared})
            project = find_project(lode.get("project", ""))
            outcome, pane_id = self._gated_spawn(
                lode,
                project.path if project else None,
                pending_state="ready",
                foreground=False,
                allow_terminal_recovery=True,
            )
            if outcome is not SpawnOutcome.SPAWNED:
                if conn:
                    self._send_response(
                        conn,
                        {"type": "error", "error": lode.get("status", "spawn unavailable")},
                    )
                return
            if conn:
                self._send_response(
                    conn, {"type": "lode_resumed", "lode": lode, "tmux_pane": pane_id}
                )

        elif msg_type == "lode_kill":
            self._send_action_ack(
                conn,
                outcome="refused",
                reason="protocol_upgrade_required",
            )

        elif msg_type == "lode_unarchive":
            lode_id = message.get("lode_id")
            if lode_id:
                if message.get("spawn") and self._lode_has_pending_action(lode_id):
                    if conn:
                        self._send_response(
                            conn,
                            {
                                "type": "error",
                                "error": "pending action must finish before unarchive spawn",
                            },
                        )
                    return
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
                            pending_state=("new" if lode.get("state") == "new" else "ready"),
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
                    pending_state=("new" if lode.get("state") == "new" else "ready"),
                    foreground=message.get("foreground", False),
                )

        elif msg_type == "lode_set_state":
            lode_id = message.get("lode_id")
            state = message.get("state")
            status = message.get("status", "")
            if state == "teardown" and (
                not isinstance(lode_id, str) or not _pending_action_file_exists(lode_id)
            ):
                logger.warning(
                    "Refusing teardown projection without pending action lode=%s", lode_id
                )
                acknowledge_mutation(False, "teardown_requires_pending_completion")
                return
            if state not in SUPPORTED_LODE_STATES:
                logger.warning("Refusing unsupported lode state lode=%s state=%r", lode_id, state)
                acknowledge_mutation(False, "unsupported_state")
                return
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
                if (
                    lode
                    and state == "running"
                    and lode.get("protocol_error")
                    and message.get("run_generation") == lode.get("run_generation")
                ):
                    logger.warning(
                        "Dropping running state over protocol error lode=%s generation=%s",
                        lode_id,
                        message.get("run_generation"),
                    )
                    acknowledge_mutation(False, "protocol_error")
                    return
                gate = lode_gate(lode) if lode else None
                if lode and state == "gated" and gate is None:
                    logger.warning("Refusing status-only gate lode=%s", lode_id)
                    acknowledge_mutation(False, "gate_publication_required")
                    return
                if lode and state == "running" and gate is not None:
                    gate_kind = message.get("gate_kind")
                    if "gate_epoch" not in message:
                        logger.info(
                            "Dropping gate clear without epoch lode=%s kind=%s",
                            lode_id,
                            gate_kind,
                        )
                        acknowledge_mutation(False, "stale_gate_epoch")
                        return
                    if gate["kind"] == "explicit" or gate_kind != gate["kind"]:
                        logger.info(
                            "Dropping gate clear without matching authority lode=%s kind=%s",
                            lode_id,
                            gate_kind,
                        )
                        acknowledge_mutation(False, "gate_clear_refused")
                        return
                    cleared = clear_lode_gate(
                        self.lodes,
                        lode_id,
                        gate_epoch=gate["epoch"],
                        kind=gate["kind"],
                        state="running",
                        status=status,
                    )
                    if cleared is None:
                        acknowledge_mutation(False, "stale_gate_epoch")
                        return
                    _sync_gate_artifact(cleared)
                    _log_state_change(lode_id, state, status, "gate_observation")
                    self.broadcast({"type": "lode_updated", "lode": cleared})
                    acknowledge_mutation(True, "accepted")
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

        elif msg_type == "lode_publish_gate":
            lode_id = message.get("lode_id")
            lode = self._find_lode(lode_id) if isinstance(lode_id, str) else None
            body = message.get("body")
            kind = message.get("kind")
            status = message.get("status")
            if not isinstance(status, str) or not status:
                status = "Gate" if kind == "explicit" else "Awaiting operator response"
            if lode is None:
                acknowledge_mutation(False, "lode_not_found")
                if conn:
                    self._send_response(conn, {"type": "error", "error": "lode not found"})
                return
            try:
                published, changed = publish_lode_gate(
                    self.lodes,
                    lode_id,
                    body=body,
                    kind=kind,
                    status=status,
                )
            except (OSError, ValueError) as error:
                logger.warning("Refusing gate publication lode=%s: %s", lode_id, error)
                acknowledge_mutation(False, "gate_publication_refused")
                if conn:
                    self._send_response(conn, {"type": "error", "error": str(error)})
                return
            if published is None:
                acknowledge_mutation(False, "lode_not_found")
                return
            artifact_written = _sync_gate_artifact(published)
            if changed:
                _log_state_change(lode_id, "gated", published["status"], "gate_publication")
                self.broadcast({"type": "lode_updated", "lode": published})
            acknowledge_mutation(True, "committed")
            if conn:
                self._send_response(
                    conn,
                    {
                        "type": "lode_gate_published",
                        "lode": published,
                        "artifact_written": artifact_written,
                    },
                )

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

        elif msg_type == "lode_set_worktree_path":
            lode_id = message.get("lode_id")
            worktree_path = message.get("worktree_path")
            if lode_id and isinstance(worktree_path, str):
                lode = update_lode_worktree_path(self.lodes, lode_id, worktree_path)
                if lode:
                    logger.info(f"Lode {lode_id} worktree_path={worktree_path}")
                    self.broadcast({"type": "lode_updated", "lode": lode})

        elif msg_type == "lode_set_codex_thread":
            lode_id = message.get("lode_id")
            thread_id = message.get("codex_thread_id")
            if lode_id and thread_id:
                try:
                    lode = update_lode_codex_thread(self.lodes, lode_id, thread_id)
                except ValueError as error:
                    logger.warning("Refusing invalid Codex thread mutation: %s", error)
                    return
                if lode:
                    logger.info(f"Lode {lode_id} codex_thread={thread_id}")
                    self.broadcast({"type": "lode_updated", "lode": lode})

        elif msg_type == "lode_set_coder_session":
            lode_id = message.get("lode_id")
            provider = message.get("provider")
            session_id = message.get("session_id")
            if lode_id and provider and session_id:
                try:
                    lode = update_lode_coder_session(self.lodes, lode_id, provider, session_id)
                except ValueError as error:
                    logger.warning("Refusing invalid coder session mutation: %s", error)
                    return
                if lode:
                    logger.info(f"Lode {lode_id} coder={provider} session={session_id}")
                    self.broadcast({"type": "lode_updated", "lode": lode})

        elif msg_type == "lode_bind_stage_session":
            lode_id = message.get("lode_id")
            lode = self._find_lode(lode_id) if isinstance(lode_id, str) else None
            driver = message.get("driver")
            stage = message.get("stage")
            launch_id = message.get("launch_id")
            provider_session_id = message.get("provider_session_id")
            run_generation = message.get("run_generation")
            if not lode:
                acknowledge_mutation(False, "lode_not_found")
                return
            if (
                driver not in RUNNABLE_STAGE_DRIVERS
                or lode_driver(lode) != driver
                or stage != lode.get("stage")
                or not all(
                    isinstance(value, str) and value
                    for value in (stage, launch_id, provider_session_id, run_generation)
                )
            ):
                refuse_stage_protocol(
                    lode, "stage binding identity does not match the current lode"
                )
                acknowledge_mutation(False, "stage_identity_mismatch")
                return
            try:
                bound, outcome = bind_lode_stage_session(
                    self.lodes,
                    lode_id,
                    driver=driver,
                    stage=stage,
                    launch_id=launch_id,
                    provider_session_id=provider_session_id,
                    run_generation=run_generation,
                )
            except ValueError as error:
                refuse_stage_protocol(lode, str(error))
                acknowledge_mutation(False, "stage_binding_conflict")
                return
            if bound is None:
                acknowledge_mutation(False, "lode_not_found")
                return
            logger.info(
                "stage binding committed lode=%s driver=%s stage=%s launch_id=%s "
                "provider_session_id=%s run_generation=%s",
                lode_id,
                driver,
                stage,
                launch_id,
                provider_session_id,
                run_generation,
            )
            self.broadcast({"type": "lode_updated", "lode": bound})
            acknowledge_mutation(True, outcome)

        elif msg_type == "lode_set_claude_started":
            lode_id = message.get("lode_id")
            claude_stage = message.get("claude_stage")
            lode = self._find_lode(lode_id) if isinstance(lode_id, str) else None
            expected_fields = {"type", "lode_id", "claude_stage", "ts", "run_generation"}
            if (
                lode is None
                or set(message) != expected_fields
                or not isinstance(claude_stage, str)
                or lode_driver(lode) != "claude"
                or claude_stage != lode.get("stage")
            ):
                if lode is not None:
                    refuse_stage_protocol(
                        lode, "legacy Claude start does not match the current stage"
                    )
                acknowledge_mutation(False, "stage_identity_mismatch")
                return
            lode = set_lode_claude_started(self.lodes, lode_id, claude_stage)
            if lode:
                logger.info(
                    "legacy Claude start committed lode=%s stage=%s generation=%s",
                    lode_id,
                    claude_stage,
                    message["run_generation"],
                )
                self.broadcast({"type": "lode_updated", "lode": lode})
                acknowledge_mutation(True, "committed")
            else:
                acknowledge_mutation(False, "lode_not_found")

        elif msg_type == "lode_reset_claude_stage":
            self._send_action_ack(
                conn,
                outcome="refused",
                reason="protocol_upgrade_required",
            )

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
                        pending_state="ready",
                        foreground=False,
                        spawn_updates={
                            "stage": "refine",
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
            character = _single_character_payload(text)
            gate = lode_gate(lode)
            delivery_fence: tuple[int, int] | None = None
            if lode.get("state") == "gated" and character is None:
                result = {
                    "reason": "gated_body_refused",
                    "capture": None,
                    "title": None,
                }
            else:
                if gate is not None:
                    try:
                        _delivery_lode, delivery_fence = begin_lode_gate_delivery(
                            self.lodes, lode_id
                        )
                    except OSError as error:
                        logger.warning("Could not fence gate delivery lode=%s: %s", lode_id, error)
                        result = {
                            "reason": "acceptance_timeout",
                            "capture": None,
                            "title": None,
                        }
                    else:
                        result = None
                else:
                    result = None
                if result is None and character is not None:
                    result = _deliver_lode_pane_input(
                        self.lodes, lode, character, paste=False, character=True
                    )
                elif result is None:
                    result = _deliver_lode_pane_input(self.lodes, lode, text, paste=True)
            reason = result["reason"]
            accepted = reason in _ACCEPTED_DELIVERY_REASONS
            updated: dict | None = None
            if accepted:
                status = "Character sent" if reason == "character_sent" else "Feedback accepted"
                if gate is not None and gate["kind"] in {"explicit", "idle_park"}:
                    if delivery_fence is None:
                        accepted = False
                        reason = "acceptance_timeout"
                    else:
                        try:
                            updated = clear_lode_gate(
                                self.lodes,
                                lode_id,
                                gate_epoch=delivery_fence[0],
                                kind=gate["kind"],
                                delivery_epoch=delivery_fence[1],
                                state="running",
                                status=status,
                            )
                        except OSError as error:
                            logger.warning("Could not clear gate lode=%s: %s", lode_id, error)
                            updated = None
                        if updated is None:
                            accepted = False
                            reason = "acceptance_timeout"
                        else:
                            _sync_gate_artifact(updated)
                elif gate is not None:
                    # A native selector clears only when its runner observes the
                    # current selector consumed; a character is not that evidence.
                    updated = update_lode_state(self.lodes, lode_id, "gated", status)
                else:
                    updated = update_lode_state(self.lodes, lode_id, "running", status)
            if not accepted:
                outcome = _DELIVERY_FAILURE_OUTCOMES[reason]
                status = _GATE_FEEDBACK_STATUSES[outcome]
                if updated is None and gate is not None:
                    updated = update_lode_state(self.lodes, lode_id, "gated", status)
                elif updated is None:
                    updated, _changed = publish_lode_gate(
                        self.lodes,
                        lode_id,
                        body=status,
                        kind="explicit",
                        status=status,
                    )
                    if updated is not None:
                        _sync_gate_artifact(updated)
            if not accepted:
                outcome = _DELIVERY_FAILURE_OUTCOMES[reason]
                message_template = _GATE_FEEDBACK_MESSAGES[reason]
            if updated:
                _log_state_change(lode_id, updated["state"], updated["status"], "feedback")
                self.broadcast({"type": "lode_updated", "lode": updated})
            if conn:
                if accepted:
                    response = {
                        "type": "feedback_sent",
                        "lode_id": lode_id,
                        "tmux_pane": pane_id,
                    }
                    if reason == "character_sent":
                        response["character"] = True
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
            if "coder_provider" not in message:
                if conn:
                    self._send_response(
                        conn,
                        {
                            "type": "promote_error",
                            "error": "lode_promote_backlog requires coder_provider",
                        },
                    )
                return
            item_id = message.get("item_id", "")
            scope = message.get("scope", "")
            try:
                coder_provider = validate_coder_provider(message["coder_provider"])
                supervisor_provider = validate_supervisor_provider(message.get("driver", "claude"))
            except ValueError as error:
                if conn:
                    self._send_response(conn, {"type": "promote_error", "error": str(error)})
                return
            readiness = coder_check(coder_provider)
            if not readiness["ready"]:
                if conn:
                    self._send_response(
                        conn,
                        {
                            "type": "promote_error",
                            "error": coder_unavailable_message(
                                coder_provider, readiness.get("error")
                            ),
                        },
                    )
                return
            supervisor_readiness = supervisor_check(supervisor_provider)
            if not supervisor_readiness["ready"]:
                if conn:
                    self._send_response(
                        conn,
                        {
                            "type": "promote_error",
                            "error": supervisor_unavailable_message(
                                supervisor_provider, supervisor_readiness.get("error")
                            ),
                        },
                    )
                return
            item = find_backlog_by_prefix(self.backlog, item_id)
            if not item:
                if conn:
                    self._send_response(
                        conn,
                        {"type": "promote_error", "error": f"Backlog item '{item_id}' not found"},
                    )
            else:
                try:
                    lode = self._promote_backlog_item(
                        item,
                        scope,
                        coder_provider=coder_provider,
                        supervisor_provider=supervisor_provider,
                    )
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
            validate_lode_coder_data(self.lodes, "active.jsonl")
            validate_lode_coder_data(self.archived_lodes, "archived.jsonl")
            validate_lode_driver_data(self.lodes, "active.jsonl")
            validate_lode_driver_data(self.archived_lodes, "archived.jsonl")
            self.backlog = load_backlog()
            logger.info("Projects and lodes reloaded from disk")

        else:
            logger.warning(f"Unknown message type: {msg_type}")

    def _send_response(
        self,
        conn: socket.socket,
        message: dict,
        *,
        exchange_id: str | None | object = _CURRENT_EXCHANGE,
    ) -> None:
        """Send a response directly to a client."""
        if "ts" not in message:
            message["ts"] = current_time_ms()
        if exchange_id is _CURRENT_EXCHANGE:
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
            self._maybe_reap_worktrees()
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
                self._maybe_reap_worktrees()

    def _enqueue_event(self, message: dict, conn: socket.socket | None = None) -> None:
        """Enqueue a mutation event for the event loop thread."""
        if self.stop_event.is_set():
            self._close_dropped_event_handles(message)
            return
        try:
            self.event_queue.put_nowait((message, conn))
        except queue.Full:
            self._close_dropped_event_handles(message)
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
            self.event_thread.join()
        if self.writer_thread and self.writer_thread.is_alive():
            self.writer_thread.join()

        current_thread = threading.current_thread()
        workers = {
            *self.action_threads.values(),
            *self.registration_threads.values(),
        }
        for thread in workers:
            if thread is not current_thread and thread.is_alive():
                thread.join()

        while True:
            try:
                message, _conn = self.event_queue.get_nowait()
            except queue.Empty:
                break
            self._close_dropped_event_handles(message)

        descriptors = set()
        for handles in (
            self.supervisor_pidfds,
            self.cgroup_fds,
            self.pane_root_pidfds,
        ):
            descriptors.update(handles.values())
        for fd in descriptors:
            try:
                os.close(fd)
            except OSError:
                pass
        self.supervisor_pidfds.clear()
        self.cgroup_fds.clear()
        self.pane_root_pidfds.clear()
        self.absent_cgroups.clear()

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
