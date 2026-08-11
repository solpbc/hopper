# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Lode management for hopper.

Lodes are plain dicts with these fields:
- id: str - 8-character base32 ID
- stage: str - "mill", "refine", "ship", or "shipped"
- created_at: int - milliseconds since epoch
- project: str - project name (default "")
- scope: str - user's task scope description (default "")
- updated_at: int - milliseconds since epoch (default 0, meaning use created_at)
- state: str - server-validated lifecycle state (default "new")
- status: str - human-readable status text (default "")
- title: str - short human-readable label (default "")
- branch: str - git branch name for this lode's worktree (default "")
- active: bool - whether a runner client is connected (default False)
- tmux_pane: str | None - tmux pane ID (default None)
- pid: int | None - process ID of active runner (default None)
- run_generation: str | None - generation owning runner mutations (default None)
- oom_scope: str | None - guarded systemd scope unit name (default None)
- failure_kind: str | None - durable terminal runner failure discriminator (default None)
- archive_action_id: str | None - action that published this archive (default None)
- codex_thread_id: str | None - Codex thread ID for stage resumption (default None)
- last_progress_at: int | None - timestamp of most recent progress heartbeat
- last_progress_summary: str - short progress summary for UI display
- last_pane_activity_at: int | None - timestamp of most recent real pane change (default None)
- pane_title_observation: dict | None - cross-attempt processing-title observation (default None)
- backlog: dict | None - original backlog item data if promoted (default None)
- archived_at: int | None - milliseconds since epoch when archived
  (default None, set by archive_lode)
- runs: dict - per-stage runtime tracking {"stage": {"started_at": ms, "stopped_at": ms}}
- claude: dict - per-stage Claude session tracking:
    {"mill": {"session_id": "<uuid>", "started": false},
     "refine": {"session_id": "<uuid>", "started": false},
     "ship": {"session_id": "<uuid>", "started": false}}
"""

import json
import os
import secrets
import time
import uuid
from pathlib import Path

from hopper import config
from hopper.tmux import Liveness, pane_liveness

ID_LEN = 8  # Lode ID length (8 base32 chars)
ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"  # lowercase base32
REFUSAL_STATUS_PREFIXES = ("spawn refused: ", "spawn failed: ", "action refused: ")


def is_canonical_lode_id(value: object) -> bool:
    """Return whether a value is a canonical Hopper lode ID."""
    return (
        isinstance(value, str)
        and len(value) == ID_LEN
        and all(character in ID_ALPHABET for character in value)
    )


def current_time_ms() -> int:
    """Return current time in milliseconds since epoch."""
    return int(time.time() * 1000)


def format_age(timestamp_ms: int) -> str:
    """Format a timestamp as a friendly age string.

    Args:
        timestamp_ms: Timestamp in milliseconds since epoch

    Returns:
        Friendly string like "now", "3m", "4h", "2d", "1w"
    """
    now = current_time_ms()
    diff_ms = now - timestamp_ms

    # Handle future timestamps or very recent
    if diff_ms < 60_000:  # < 1 minute
        return "now"

    minutes = diff_ms // 60_000
    if minutes < 60:
        return f"{minutes}m"

    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"

    days = hours // 24
    if days < 7:
        return f"{days}d"

    weeks = days // 7
    return f"{weeks}w"


def format_uptime(started_at_ms: int) -> str:
    """Format uptime as a friendly duration string.

    Args:
        started_at_ms: Start timestamp in milliseconds since epoch

    Returns:
        Friendly string like "5m", "2h 15m", "3d 4h"
    """
    now = current_time_ms()
    diff_ms = now - started_at_ms

    if diff_ms < 60_000:  # < 1 minute
        return "0m"

    minutes = diff_ms // 60_000
    hours = minutes // 60
    days = hours // 24

    minutes = minutes % 60
    hours = hours % 24

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 and days == 0:  # Only show minutes if less than a day
        parts.append(f"{minutes}m")

    return " ".join(parts) if parts else "0m"


def format_duration_ms(duration_ms: int) -> str:
    """Format a duration in milliseconds as a friendly string.

    Args:
        duration_ms: Duration in milliseconds

    Returns:
        Friendly string like "5s", "1m", "2m", "1h"
    """
    if duration_ms < 1000:
        return "0s"

    seconds = duration_ms // 1000
    if seconds < 60:
        return f"{seconds}s"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"

    hours = minutes // 60
    return f"{hours}h"


def compute_runtime_ms(lode: dict, now: int | None = None) -> int:
    """Sum runtime across all stages in a lode's runs dict."""
    if now is None:
        now = current_time_ms()
    total = 0
    for stage_run in lode.get("runs", {}).values():
        started = stage_run.get("started_at")
        if started is None:
            continue
        stopped = stage_run.get("stopped_at")
        if stopped is not None:
            total += stopped - started
        else:
            total += now - started
    return max(0, total)


def touch(lode: dict) -> None:
    """Update a lode's updated_at timestamp to now."""
    lode["updated_at"] = current_time_ms()


def get_lode_dir(lode_id: str) -> Path:
    """Get the directory for a lode."""
    return config.hopper_dir() / "lodes" / lode_id


def get_worktree_dir(lode_id: str) -> Path:
    """Return the on-disk git worktree location for a lode.

    New worktrees live under config.worktree_root() (whitespace-free)
    so downstream project tooling that mishandles spaces in paths does
    not break. A worktree already created at the legacy location
    (get_lode_dir(lode_id)/"worktree") is returned in place so existing
    lodes stay resumable, inspectable, and cleanable with no filesystem
    surgery.
    """
    legacy = get_lode_dir(lode_id) / "worktree"
    if legacy.is_dir():
        return legacy
    return config.worktree_root() / lode_id


def parse_diff_numstat_totals(text: str) -> tuple[int, int]:
    """Parse git numstat output and return (total_additions, total_deletions)."""
    total_additions = 0
    total_deletions = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        additions, deletions, _filename = parts
        if not additions.isdigit() or not deletions.isdigit():
            continue
        total_additions += int(additions)
        total_deletions += int(deletions)
    return (total_additions, total_deletions)


def parse_diff_numstat(text: str) -> str:
    """Parse git numstat output and return a compact summary."""
    additions, deletions = parse_diff_numstat_totals(text)
    if additions == 0 and deletions == 0:
        return ""
    return f"+{additions} -{deletions}"


def read_diff_totals(lode_id: str) -> tuple[int, int]:
    """Read a lode's diff.txt and return (additions, deletions) totals."""
    diff_path = get_lode_dir(lode_id) / "diff.txt"
    try:
        return parse_diff_numstat_totals(diff_path.read_text())
    except Exception:
        return (0, 0)


def load_lodes() -> list[dict]:
    """Load active lodes from JSONL file."""
    lodes_file = config.hopper_dir() / "active.jsonl"
    if not lodes_file.exists():
        return []

    lodes = []
    with open(lodes_file) as f:
        for line in f:
            line = line.strip()
            if line:
                lodes.append(json.loads(line))
    return lodes


def load_archived_lodes() -> list[dict]:
    """Load archived lodes from archived.jsonl."""
    archived_file = config.hopper_dir() / "archived.jsonl"
    if not archived_file.exists():
        return []
    lodes = []
    with open(archived_file) as f:
        for line in f:
            line = line.strip()
            if line:
                lodes.append(json.loads(line))
    return lodes


def _write_jsonl_atomic(path: Path, items: list[dict]) -> None:
    """Atomically write a complete JSONL snapshot using a writer-unique temp file."""
    # Unique temps prevent concurrent writers from corrupting each other's
    # snapshots, but writes remain last-writer-wins. A live-server
    # `hop project rename` can still lose updates; this is sound only because
    # the flock singleton removes server-vs-server concurrency.
    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "w") as f:
            for item in items:
                f.write(json.dumps(item) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def save_archived_lodes(lodes: list[dict]) -> None:
    """Atomically save archived lodes to JSONL file."""
    _write_jsonl_atomic(config.hopper_dir() / "archived.jsonl", lodes)


def save_lodes(lodes: list[dict]) -> None:
    """Atomically save lodes to JSONL file."""
    _write_jsonl_atomic(config.hopper_dir() / "active.jsonl", lodes)


def _generate_lode_id(lodes: list[dict]) -> str:
    """Generate a unique 8-character base32 lode ID.

    Checks for collisions against active lodes, archived lodes, and existing
    lode directories.
    """
    # Load archived IDs for collision check
    archived_ids: set[str] = set()
    archived_file = config.hopper_dir() / "archived.jsonl"
    if archived_file.exists():
        with open(archived_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    archived_ids.add(data["id"])

    # Get existing lode directories
    lodes_dir = config.hopper_dir() / "lodes"
    existing_dirs = set(lodes_dir.iterdir()) if lodes_dir.exists() else set()
    existing_dir_names = {d.name for d in existing_dirs}

    # Active lode IDs
    active_ids = {lode["id"] for lode in lodes}

    # Generate until unique
    for _ in range(100):  # Safety limit
        new_id = "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LEN))
        if (
            new_id not in active_ids
            and new_id not in archived_ids
            and new_id not in existing_dir_names
        ):
            return new_id

    raise RuntimeError("Failed to generate unique lode ID after 100 attempts")


def _make_claude_sessions() -> dict:
    """Generate per-stage Claude session tracking with fresh UUIDs."""
    return {
        stage: {"session_id": str(uuid.uuid4()), "started": False}
        for stage in ("mill", "refine", "ship")
    }


def create_lode(
    lodes: list[dict], project: str, scope: str = "", *, lode_id: str | None = None
) -> dict:
    """Create a new lode, add to list, and create its directory.

    Args:
        lodes: List of lodes to add to.
        project: Project name for this lode.
        scope: User's task scope description.

    Returns:
        The newly created lode dict.
    """
    if lode_id is not None:
        if len(lode_id) != ID_LEN or any(character not in ID_ALPHABET for character in lode_id):
            raise ValueError("reserved lode ID has an invalid format")
        if any(existing.get("id") == lode_id for existing in lodes):
            raise ValueError("reserved lode ID is already active")
        if get_lode_dir(lode_id).exists():
            raise ValueError("reserved lode ID directory already exists")
        archived_path = config.hopper_dir() / "archived.jsonl"
        if archived_path.exists():
            with open(archived_path) as source:
                if any(json.loads(line).get("id") == lode_id for line in source if line.strip()):
                    raise ValueError("reserved lode ID is already archived")
    now = current_time_ms()
    lode = {
        "id": lode_id or _generate_lode_id(lodes),
        "stage": "mill",
        "created_at": now,
        "project": project,
        "scope": scope,
        "updated_at": now,
        "state": "new",
        "status": "Ready to start",
        "title": "",
        "branch": "",
        "active": False,
        "tmux_pane": None,
        "pid": None,
        "run_generation": None,
        "oom_scope": None,
        "failure_kind": None,
        "archive_action_id": None,
        "codex_thread_id": None,
        "last_progress_at": None,
        "last_progress_summary": "",
        "last_pane_activity_at": None,
        "pane_title_observation": None,
        "backlog": None,
        "pending_action": None,
        "action_results": [],
        "runs": {},
        "claude": _make_claude_sessions(),
    }
    lodes.append(lode)
    get_lode_dir(lode["id"]).mkdir(parents=True, exist_ok=True)
    save_lodes(lodes)
    return lode


def reserve_lode_id(lodes: list[dict]) -> str:
    """Return a collision-checked lode ID without publishing a lode."""
    return _generate_lode_id(lodes)


def _update_lode_field(lodes: list[dict], lode_id: str, field: str, value) -> dict | None:
    """Find a lode by ID, set a single field, touch, and save."""
    for lode in lodes:
        if lode["id"] == lode_id:
            lode[field] = value
            touch(lode)
            save_lodes(lodes)
            return lode
    return None


def update_lode_stage(lodes: list[dict], lode_id: str, stage: str) -> dict | None:
    """Update a lode's stage. Returns the updated lode or None if not found."""
    return _update_lode_field(lodes, lode_id, "stage", stage)


def archive_lode(lodes: list[dict], lode_id: str) -> dict | None:
    """Archive a lode: append to archived.jsonl and remove from active list.

    The lode directory is left intact; git worktree and branch cleanup
    is handled by the caller.
    Returns the archived lode or None if not found.
    """
    for i, lode in enumerate(lodes):
        if lode["id"] == lode_id:
            archived = lodes.pop(i)
            archived["archived_at"] = current_time_ms()

            # Append to archive file
            archived_file = config.hopper_dir() / "archived.jsonl"
            archived_file.parent.mkdir(parents=True, exist_ok=True)
            with open(archived_file, "a") as f:
                f.write(json.dumps(archived) + "\n")

            save_lodes(lodes)
            return archived
    return None


def archive_lode_for_action(
    active_lodes: list[dict], archived_lodes: list[dict], lode_id: str, action_id: str
) -> dict:
    """Publish exactly one archive record for a durable lode action.

    The archive snapshot is persisted before the active twin is removed. A
    retry therefore converges after a crash on either side of the active-file
    rewrite.
    """
    archived_matches = [lode for lode in archived_lodes if lode.get("id") == lode_id]
    if any(lode.get("archive_action_id") != action_id for lode in archived_matches):
        raise ValueError("lode is archived by a different action")
    active_matches = [lode for lode in active_lodes if lode.get("id") == lode_id]
    if len(active_matches) > 1:
        raise ValueError("active lode identity is duplicated")
    if active_matches and active_matches[0].get("archive_action_id") not in {None, action_id}:
        raise ValueError("active lode belongs to a different archive action")
    if not archived_matches and not active_matches:
        raise ValueError("action lode is absent from active and archived storage")

    if archived_matches:
        archived = archived_matches[0]
    else:
        archived = dict(active_matches[0])
        archived["archive_action_id"] = action_id
        archived["archived_at"] = current_time_ms()

    prior_archived = list(archived_lodes)
    archived_lodes[:] = [lode for lode in archived_lodes if lode.get("id") != lode_id] + [archived]
    try:
        save_archived_lodes(archived_lodes)
    except Exception:
        archived_lodes[:] = prior_archived
        raise

    prior_active = list(active_lodes)
    active_lodes[:] = [lode for lode in active_lodes if lode.get("id") != lode_id]
    try:
        save_lodes(active_lodes)
    except Exception:
        active_lodes[:] = prior_active
        raise
    return archived


def unarchive_lode(
    archived_lodes: list[dict], active_lodes: list[dict], lode_id: str
) -> dict | None:
    """Unarchive a lode: move from archived list back to active list.

    Returns the unarchived lode or None if not found.
    """
    for i, lode in enumerate(archived_lodes):
        if lode["id"] == lode_id:
            restored = archived_lodes.pop(i)
            restored.pop("archived_at", None)
            active_lodes.append(restored)

            save_archived_lodes(archived_lodes)
            save_lodes(active_lodes)
            return restored
    return None


def update_lode_state(lodes: list[dict], lode_id: str, state: str, status: str) -> dict | None:
    """Update a lode's state and status. Returns the updated lode or None if not found."""
    for lode in lodes:
        if lode["id"] == lode_id:
            lode["state"] = state
            lode["status"] = status
            # Record run timing
            stage = lode.get("stage", "")
            if stage in ("mill", "refine", "ship"):
                runs = lode.setdefault("runs", {})
                now = current_time_ms()
                if state == "running":
                    stage_run = runs.get(stage, {})
                    if "started_at" not in stage_run or "stopped_at" in stage_run:
                        runs[stage] = {"started_at": now}
                elif state in ("error", "ready"):
                    stage_run = runs.get(stage, {})
                    if "started_at" in stage_run:
                        stage_run["stopped_at"] = now
                        runs[stage] = stage_run
            touch(lode)
            save_lodes(lodes)
            return lode
    return None


def update_lode_status(lodes: list[dict], lode_id: str, status: str) -> dict | None:
    """Update a lode's status text only. Returns the updated lode or None if not found."""
    return _update_lode_field(lodes, lode_id, "status", status)


def update_lode_title(lodes: list[dict], lode_id: str, title: str) -> dict | None:
    """Update a lode's title only. Returns the updated lode or None if not found."""
    return _update_lode_field(lodes, lode_id, "title", title)


def update_lode_branch(lodes: list[dict], lode_id: str, branch: str) -> dict | None:
    """Update a lode's branch only. Returns the updated lode or None if not found."""
    return _update_lode_field(lodes, lode_id, "branch", branch)


def update_lode_codex_thread(lodes: list[dict], lode_id: str, codex_thread_id: str) -> dict | None:
    """Update the codex thread ID on a lode."""
    return _update_lode_field(lodes, lode_id, "codex_thread_id", codex_thread_id)


def set_lode_claude_started(lodes: list[dict], lode_id: str, claude_stage: str) -> dict | None:
    """Mark a claude stage as started on a lode."""
    for lode in lodes:
        if lode["id"] == lode_id:
            if claude_stage not in lode.get("claude", {}):
                return None
            lode["claude"][claude_stage]["started"] = True
            touch(lode)
            save_lodes(lodes)
            return lode
    return None


def reset_lode_claude_stage(
    lodes: list[dict],
    lode_id: str,
    claude_stage: str,
    *,
    persist: bool = True,
    session_id: str | None = None,
) -> dict | None:
    """Reset a claude stage (new session_id, started=False)."""
    for lode in lodes:
        if lode["id"] == lode_id:
            if claude_stage not in lode.get("claude", {}):
                return None
            lode["claude"][claude_stage]["session_id"] = session_id or str(uuid.uuid4())
            lode["claude"][claude_stage]["started"] = False
            lode["last_progress_at"] = None
            lode["last_progress_summary"] = ""
            lode["last_pane_activity_at"] = None
            lode["pane_title_observation"] = None
            if persist:
                touch(lode)
                save_lodes(lodes)
            return lode
    return None


def find_lodes_by_prefix(lodes: list[dict], prefix: str) -> list[dict]:
    """Find all lodes matching an ID prefix."""
    return [lode for lode in lodes if lode["id"].startswith(prefix)]


def find_lode_by_prefix(lodes: list[dict], prefix: str) -> dict | None:
    """Find a lode by ID prefix. Returns None if not found or ambiguous."""
    matches = find_lodes_by_prefix(lodes, prefix)
    if len(matches) == 1:
        return matches[0]
    return None


# --- Status rendering ---

PARK_STATUS_TEMPLATE = """Parked (idle): {reason}. The agent is ALIVE and was NOT terminated. Inspect: hop lode peek {lode_id} | Resume: hop lode nudge {lode_id} (or hop lode answer {lode_id} 1)"""  # noqa: E501

PARK_PANE_GONE_STATUS = """Parked (idle), but the pane is GONE: {reason}. The agent did NOT survive — nudge and answer cannot reach a dead pane. Recover: hop lode restart {lode_id} (check first that the work did not already land: git cherry origin/main {branch})"""  # noqa: E501

OOM_KILLED_STATUS = """OOM-KILLED: the operating system killed this Hopper lode's process group after an out-of-memory event. Automatic restart is suppressed. Inspect the worktree and branch, then recover explicitly: hop lode resume {lode_id} (preserve the stage session) or hop lode restart {lode_id} (fresh stage session)."""  # noqa: E501

RUNNER_EXIT_UNVERIFIED_STATUS = """Runner exit UNVERIFIED: Hopper lost the guarded lode runner before it could classify the scope result. Automatic restart is suppressed. Inspect the worktree and branch, then recover explicitly: hop lode resume {lode_id} (preserve the stage session) or hop lode restart {lode_id} (fresh stage session)."""  # noqa: E501

TERMINAL_FAILURE_KINDS = frozenset({"oom", "runner_exit_unverified"})

# The branch advice is the final parenthetical in the constant.
_PARK_PANE_GONE_WITHOUT_BRANCH = PARK_PANE_GONE_STATUS.rsplit(" (", 1)[0]

PARK_LIVENESS_UNVERIFIED_SUFFIX = (
    """ (pane liveness UNVERIFIED — could not probe tmux; treat ALIVE as unconfirmed)"""
)

PANE_LIVENESS_NOT_PROBED = "not_probed"


def format_park_status(reason: str, lode_id: str) -> str:
    """Format the durable status written when an idle lode is parked."""
    return PARK_STATUS_TEMPLATE.format(reason=reason, lode_id=lode_id)


def is_terminal_failure_kind(failure_kind: str | None) -> bool:
    """Return whether failure_kind latches automatic runner launch."""
    return failure_kind in TERMINAL_FAILURE_KINDS


def format_terminal_failure_status(failure_kind: str, lode_id: str) -> str:
    """Format the canonical durable status for a terminal runner failure."""
    if failure_kind == "oom":
        template = OOM_KILLED_STATUS
    elif failure_kind == "runner_exit_unverified":
        template = RUNNER_EXIT_UNVERIFIED_STATUS
    else:
        raise ValueError(f"unknown terminal failure kind: {failure_kind}")
    return template.format(lode_id=lode_id)


def _lode_status_and_liveness(lode: dict) -> tuple[str, Liveness | None]:
    """Return display status and current local pane evidence, if probed."""
    status = lode.get("status", "")
    if not status:
        return status, None

    lode_id = lode.get("id", "")
    reason_prefix, suffix_template = PARK_STATUS_TEMPLATE.split("{reason}", 1)
    status_suffix = suffix_template.format(lode_id=lode_id)
    if not status.startswith(reason_prefix) or not status.endswith(status_suffix):
        return status, None

    reason = status[len(reason_prefix) : -len(status_suffix)]
    if format_park_status(reason, lode_id) != status:
        return status, None

    if lode.get("host") not in (None, "", "local"):
        return status, None

    pane = lode.get("tmux_pane")
    if not pane:
        liveness = Liveness.GONE
    else:
        try:
            liveness = pane_liveness(pane)
        except Exception:
            liveness = Liveness.UNKNOWN

    if liveness is Liveness.ALIVE:
        return status, liveness
    elif liveness is Liveness.GONE:
        branch = lode.get("branch")
        template = PARK_PANE_GONE_STATUS if branch else _PARK_PANE_GONE_WITHOUT_BRANCH
        return template.format(reason=reason, lode_id=lode_id, branch=branch), liveness
    else:
        return status + PARK_LIVENESS_UNVERIFIED_SUFFIX, liveness


def lode_status_for_display(lode: dict) -> str:
    """Return a lode's human-readable status with current local pane evidence."""
    status, _liveness = _lode_status_and_liveness(lode)
    return status


def lode_with_status_annotations(lode: dict) -> dict:
    """Return a copied lode with additive display status and pane liveness."""
    status, liveness = _lode_status_and_liveness(lode)
    annotated = dict(lode)
    annotated["status_display"] = status
    annotated["pane_liveness"] = (
        liveness.value if liveness is not None else PANE_LIVENESS_NOT_PROBED
    )
    return annotated


# --- Status icon constants ---

STATUS_RUNNING = "●"  # filled circle
STATUS_STUCK = "◐"  # half-filled circle
STATUS_NEW = "○"  # empty circle
STATUS_ERROR = "✗"  # x mark
STATUS_SHIPPED = "✓"
STATUS_GATED = "◇"  # open diamond — paused at gate, awaiting user review
STATUS_TEARDOWN = "◌"  # dotted circle — accepted action is proving containment
STATUS_DISCONNECTED = "⊘"  # circled division slash — runner not connected


def lode_icon(lode: dict) -> str:
    """Derive the status icon for a lode based on its state, stage, and active flag."""
    stage = lode.get("stage", "mill")
    state = lode.get("state", "new")
    if state == "teardown":
        icon = STATUS_TEARDOWN
    elif stage == "shipped":
        icon = STATUS_SHIPPED
    elif state == "new":
        icon = STATUS_NEW
    elif state == "error":
        icon = STATUS_ERROR
    elif state == "gated":
        icon = STATUS_GATED
    elif state == "stuck":
        icon = STATUS_STUCK
    else:
        icon = STATUS_RUNNING
    if not lode.get("active", False) and stage != "shipped" and state not in {"gated", "teardown"}:
        icon = STATUS_DISCONNECTED
    return icon
