"""Lode management for hopper."""

import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from hopper import config

ID_LEN = 8  # Lode ID length (8 hex chars = 4 bytes)


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


@dataclass
class Lode:
    """A hopper lode."""

    id: str
    stage: str  # "ore", "processing", or "ship"
    created_at: int  # milliseconds since epoch
    project: str = ""  # Project name this lode belongs to
    scope: str = ""  # User's task scope description
    updated_at: int = field(default=0)  # milliseconds since epoch, 0 means use created_at
    state: str = "new"  # Freeform: "new", "running", "stuck", "error", task names, etc.
    status: str = ""  # Human-readable status text
    active: bool = False  # Whether a hop ore client is connected
    tmux_pane: str | None = None  # tmux pane ID (e.g., "%1")
    codex_thread_id: str | None = None  # Codex thread ID for stage resumption
    backlog: dict | None = None  # Original backlog item data if promoted from backlog

    @property
    def effective_updated_at(self) -> int:
        """Return updated_at, falling back to created_at if not set."""
        return self.updated_at if self.updated_at else self.created_at

    def touch(self) -> None:
        """Update the updated_at timestamp to now."""
        self.updated_at = current_time_ms()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "stage": self.stage,
            "created_at": self.created_at,
            "project": self.project,
            "scope": self.scope,
            "updated_at": self.effective_updated_at,
            "state": self.state,
            "status": self.status,
            "active": self.active,
            "tmux_pane": self.tmux_pane,
            "codex_thread_id": self.codex_thread_id,
            "backlog": self.backlog,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Lode":
        return cls(
            id=data["id"],
            stage=data["stage"],
            created_at=data["created_at"],
            project=data.get("project", ""),  # Backwards compat
            scope=data.get("scope", ""),  # Backwards compat
            updated_at=data["updated_at"],
            state=data["state"],
            status=data.get("status", ""),
            active=data.get("active", False),  # Backwards compat
            tmux_pane=data.get("tmux_pane"),
            codex_thread_id=data.get("codex_thread_id"),
            backlog=data.get("backlog"),
        )


def get_lode_dir(lode_id: str) -> Path:
    """Get the directory for a lode."""
    return config.hopper_dir() / "lodes" / lode_id


def load_lodes() -> list[Lode]:
    """Load active lodes from JSONL file."""
    lodes_file = config.hopper_dir() / "active.jsonl"
    if not lodes_file.exists():
        return []

    lodes = []
    with open(lodes_file) as f:
        for line in f:
            line = line.strip()
            if line:
                data = json.loads(line)
                lodes.append(Lode.from_dict(data))
    return lodes


def save_lodes(lodes: list[Lode]) -> None:
    """Atomically save lodes to JSONL file."""
    lodes_file = config.hopper_dir() / "active.jsonl"
    lodes_file.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = lodes_file.with_suffix(".jsonl.tmp")
    with open(tmp_path, "w") as f:
        for lode in lodes:
            f.write(json.dumps(lode.to_dict()) + "\n")

    os.replace(tmp_path, lodes_file)


def _generate_lode_id(lodes: list[Lode]) -> str:
    """Generate a unique 8-character hex lode ID.

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
    active_ids = {lode.id for lode in lodes}

    # Generate until unique
    for _ in range(100):  # Safety limit
        new_id = secrets.token_hex(4)  # 8 hex chars
        if (
            new_id not in active_ids
            and new_id not in archived_ids
            and new_id not in existing_dir_names
        ):
            return new_id

    raise RuntimeError("Failed to generate unique lode ID after 100 attempts")


def create_lode(lodes: list[Lode], project: str, scope: str = "") -> Lode:
    """Create a new lode, add to list, and create its directory.

    Args:
        lodes: List of lodes to add to.
        project: Project name for this lode.
        scope: User's task scope description.

    Returns:
        The newly created lode.
    """
    now = current_time_ms()
    lode = Lode(
        id=_generate_lode_id(lodes),
        stage="ore",
        created_at=now,
        project=project,
        scope=scope,
        updated_at=now,
        state="new",
        status="Ready to start",
    )
    lodes.append(lode)
    get_lode_dir(lode.id).mkdir(parents=True, exist_ok=True)
    save_lodes(lodes)
    return lode


def update_lode_stage(lodes: list[Lode], lode_id: str, stage: str) -> Lode | None:
    """Update a lode's stage. Returns the updated lode or None if not found."""
    for lode in lodes:
        if lode.id == lode_id:
            lode.stage = stage
            lode.touch()
            save_lodes(lodes)
            return lode
    return None


def archive_lode(lodes: list[Lode], lode_id: str) -> Lode | None:
    """Archive a lode: append to archived.jsonl and remove from active list.

    The lode directory is left intact.
    Returns the archived lode or None if not found.
    """
    for i, lode in enumerate(lodes):
        if lode.id == lode_id:
            archived = lodes.pop(i)

            # Append to archive file
            archived_file = config.hopper_dir() / "archived.jsonl"
            archived_file.parent.mkdir(parents=True, exist_ok=True)
            with open(archived_file, "a") as f:
                f.write(json.dumps(archived.to_dict()) + "\n")

            save_lodes(lodes)
            return archived
    return None


def update_lode_state(lodes: list[Lode], lode_id: str, state: str, status: str) -> Lode | None:
    """Update a lode's state and status. Returns the updated lode or None if not found."""
    for lode in lodes:
        if lode.id == lode_id:
            lode.state = state
            lode.status = status
            lode.touch()
            save_lodes(lodes)
            return lode
    return None


def update_lode_status(lodes: list[Lode], lode_id: str, status: str) -> Lode | None:
    """Update a lode's status text only. Returns the updated lode or None if not found."""
    for lode in lodes:
        if lode.id == lode_id:
            lode.status = status
            lode.touch()
            save_lodes(lodes)
            return lode
    return None


def find_by_prefix(lodes: list[Lode], prefix: str) -> Lode | None:
    """Find a lode by ID prefix.

    Args:
        lodes: List of lodes to search
        prefix: ID prefix to match

    Returns:
        The matching lode, or None if not found or ambiguous (multiple matches)
    """
    matches = [s for s in lodes if s.id.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    return None
