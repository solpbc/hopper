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
- state: str - "new", "running", "stuck", "error", etc. (default "new")
- status: str - human-readable status text (default "")
- title: str - short human-readable label (default "")
- branch: str - git branch name for this lode's worktree (default "")
- active: bool - whether a runner client is connected (default False)
- tmux_pane: str | None - tmux pane ID (default None)
- pid: int | None - process ID of active runner (default None)
- codex_thread_id: str | None - Codex thread ID for stage resumption (default None)
- backlog: dict | None - original backlog item data if promoted (default None)
- claude: dict - per-stage Claude session tracking:
    {"mill": {"session_id": "<uuid>", "started": false},
     "refine": {"session_id": "<uuid>", "started": false},
     "ship": {"session_id": "<uuid>", "started": false}}
"""

import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path

from hopper import config

ID_LEN = 8  # Lode ID length (8 base32 chars)
ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"  # lowercase base32


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


def slugify(title: str) -> str:
    """Convert a title to a git-branch-safe slug.

    Lowercase, alphanumeric + hyphens only, no leading/trailing/consecutive
    hyphens, truncated to 40 chars, no '.lock' suffix.
    """
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    s = re.sub(r"-{2,}", "-", s)
    s = s[:40].rstrip("-")
    if s.endswith("-lock"):
        s = s[:-5]
    return s


def touch(lode: dict) -> None:
    """Update a lode's updated_at timestamp to now."""
    lode["updated_at"] = current_time_ms()


def get_lode_dir(lode_id: str) -> Path:
    """Get the directory for a lode."""
    return config.hopper_dir() / "lodes" / lode_id


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


def save_lodes(lodes: list[dict]) -> None:
    """Atomically save lodes to JSONL file."""
    lodes_file = config.hopper_dir() / "active.jsonl"
    lodes_file.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = lodes_file.with_suffix(".jsonl.tmp")
    with open(tmp_path, "w") as f:
        for lode in lodes:
            f.write(json.dumps(lode) + "\n")

    os.replace(tmp_path, lodes_file)


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


def create_lode(lodes: list[dict], project: str, scope: str = "") -> dict:
    """Create a new lode, add to list, and create its directory.

    Args:
        lodes: List of lodes to add to.
        project: Project name for this lode.
        scope: User's task scope description.

    Returns:
        The newly created lode dict.
    """
    now = current_time_ms()
    lode = {
        "id": _generate_lode_id(lodes),
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
        "codex_thread_id": None,
        "backlog": None,
        "claude": _make_claude_sessions(),
    }
    lodes.append(lode)
    get_lode_dir(lode["id"]).mkdir(parents=True, exist_ok=True)
    save_lodes(lodes)
    return lode


def update_lode_stage(lodes: list[dict], lode_id: str, stage: str) -> dict | None:
    """Update a lode's stage. Returns the updated lode or None if not found."""
    for lode in lodes:
        if lode["id"] == lode_id:
            lode["stage"] = stage
            touch(lode)
            save_lodes(lodes)
            return lode
    return None


def archive_lode(lodes: list[dict], lode_id: str) -> dict | None:
    """Archive a lode: append to archived.jsonl and remove from active list.

    The lode directory is left intact; git worktree and branch cleanup
    is handled by the caller.
    Returns the archived lode or None if not found.
    """
    for i, lode in enumerate(lodes):
        if lode["id"] == lode_id:
            archived = lodes.pop(i)

            # Append to archive file
            archived_file = config.hopper_dir() / "archived.jsonl"
            archived_file.parent.mkdir(parents=True, exist_ok=True)
            with open(archived_file, "a") as f:
                f.write(json.dumps(archived) + "\n")

            save_lodes(lodes)
            return archived
    return None


def update_lode_state(lodes: list[dict], lode_id: str, state: str, status: str) -> dict | None:
    """Update a lode's state and status. Returns the updated lode or None if not found."""
    for lode in lodes:
        if lode["id"] == lode_id:
            lode["state"] = state
            lode["status"] = status
            touch(lode)
            save_lodes(lodes)
            return lode
    return None


def update_lode_status(lodes: list[dict], lode_id: str, status: str) -> dict | None:
    """Update a lode's status text only. Returns the updated lode or None if not found."""
    for lode in lodes:
        if lode["id"] == lode_id:
            lode["status"] = status
            touch(lode)
            save_lodes(lodes)
            return lode
    return None


def update_lode_title(lodes: list[dict], lode_id: str, title: str) -> dict | None:
    """Update a lode's title only. Returns the updated lode or None if not found."""
    for lode in lodes:
        if lode["id"] == lode_id:
            lode["title"] = title
            touch(lode)
            save_lodes(lodes)
            return lode
    return None


def update_lode_branch(lodes: list[dict], lode_id: str, branch: str) -> dict | None:
    """Update a lode's branch only. Returns the updated lode or None if not found."""
    for lode in lodes:
        if lode["id"] == lode_id:
            lode["branch"] = branch
            touch(lode)
            save_lodes(lodes)
            return lode
    return None


def update_lode_codex_thread(lodes: list[dict], lode_id: str, codex_thread_id: str) -> dict | None:
    """Update the codex thread ID on a lode."""
    for lode in lodes:
        if lode["id"] == lode_id:
            lode["codex_thread_id"] = codex_thread_id
            touch(lode)
            save_lodes(lodes)
            return lode
    return None


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


def reset_lode_claude_stage(lodes: list[dict], lode_id: str, claude_stage: str) -> dict | None:
    """Reset a claude stage (new session_id, started=False)."""
    for lode in lodes:
        if lode["id"] == lode_id:
            if claude_stage not in lode.get("claude", {}):
                return None
            lode["claude"][claude_stage]["session_id"] = str(uuid.uuid4())
            lode["claude"][claude_stage]["started"] = False
            touch(lode)
            save_lodes(lodes)
            return lode
    return None
