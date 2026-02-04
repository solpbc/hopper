"""Session management for hopper."""

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from hopper import config

SHORT_ID_LEN = 8  # Standard short ID length (first segment of UUID)


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


@dataclass
class Session:
    """A hopper session."""

    id: str
    stage: str  # "ore" or "processing"
    created_at: int  # milliseconds since epoch
    project: str = ""  # Project name this session belongs to
    scope: str = ""  # User's task scope description
    updated_at: int = field(default=0)  # milliseconds since epoch, 0 means use created_at
    state: str = "new"  # Freeform: "new", "running", "stuck", "error", task names, etc.
    status: str = ""  # Human-readable status text
    active: bool = False  # Whether a hop ore client is connected
    tmux_pane: str | None = None  # tmux pane ID (e.g., "%1")
    codex_thread_id: str | None = None  # Codex session ID for task resumption

    @property
    def short_id(self) -> str:
        """Return the 8-character short ID (first segment of UUID)."""
        return self.id[:SHORT_ID_LEN]

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
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
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
        )


def get_session_dir(session_id: str) -> Path:
    """Get the directory for a session."""
    return config.hopper_dir() / "sessions" / session_id


def load_sessions() -> list[Session]:
    """Load active sessions from JSONL file."""
    sessions_file = config.hopper_dir() / "sessions.jsonl"
    if not sessions_file.exists():
        return []

    sessions = []
    with open(sessions_file) as f:
        for line in f:
            line = line.strip()
            if line:
                data = json.loads(line)
                sessions.append(Session.from_dict(data))
    return sessions


def save_sessions(sessions: list[Session]) -> None:
    """Atomically save sessions to JSONL file."""
    sessions_file = config.hopper_dir() / "sessions.jsonl"
    sessions_file.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = sessions_file.with_suffix(".jsonl.tmp")
    with open(tmp_path, "w") as f:
        for session in sessions:
            f.write(json.dumps(session.to_dict()) + "\n")

    os.replace(tmp_path, sessions_file)


def create_session(sessions: list[Session], project: str, scope: str = "") -> Session:
    """Create a new session, add to list, and create its directory.

    Args:
        sessions: List of sessions to add to.
        project: Project name for this session.
        scope: User's task scope description.

    Returns:
        The newly created session.
    """
    now = current_time_ms()
    session = Session(
        id=str(uuid.uuid4()),
        stage="ore",
        created_at=now,
        project=project,
        scope=scope,
        updated_at=now,
        state="new",
        status="Ready to start",
    )
    sessions.append(session)
    get_session_dir(session.id).mkdir(parents=True, exist_ok=True)
    save_sessions(sessions)
    return session


def update_session_stage(sessions: list[Session], session_id: str, stage: str) -> Session | None:
    """Update a session's stage. Returns the updated session or None if not found."""
    for session in sessions:
        if session.id == session_id:
            session.stage = stage
            session.touch()
            save_sessions(sessions)
            return session
    return None


def archive_session(sessions: list[Session], session_id: str) -> Session | None:
    """Archive a session: append to archived.jsonl and remove from active list.

    The session directory is left intact.
    Returns the archived session or None if not found.
    """
    for i, session in enumerate(sessions):
        if session.id == session_id:
            archived = sessions.pop(i)

            # Append to archive file
            archived_file = config.hopper_dir() / "archived.jsonl"
            archived_file.parent.mkdir(parents=True, exist_ok=True)
            with open(archived_file, "a") as f:
                f.write(json.dumps(archived.to_dict()) + "\n")

            save_sessions(sessions)
            return archived
    return None


def update_session_state(
    sessions: list[Session], session_id: str, state: str, status: str
) -> Session | None:
    """Update a session's state and status. Returns the updated session or None if not found."""
    for session in sessions:
        if session.id == session_id:
            session.state = state
            session.status = status
            session.touch()
            save_sessions(sessions)
            return session
    return None


def update_session_status(sessions: list[Session], session_id: str, status: str) -> Session | None:
    """Update a session's status text only. Returns the updated session or None if not found."""
    for session in sessions:
        if session.id == session_id:
            session.status = status
            session.touch()
            save_sessions(sessions)
            return session
    return None


def find_by_short_id(sessions: list[Session], prefix: str) -> Session | None:
    """Find a session by ID prefix.

    Args:
        sessions: List of sessions to search
        prefix: ID prefix to match (can be full ID or short ID)

    Returns:
        The matching session, or None if not found or ambiguous (multiple matches)
    """
    matches = [s for s in sessions if s.id.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    return None
