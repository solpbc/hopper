"""Session management for hopper."""

import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from hopper.config import ARCHIVED_FILE, SESSIONS_DIR, SESSIONS_FILE


def _check_test_isolation() -> None:
    """Raise an error if running under pytest without path isolation.

    This is a safety guard to prevent test code from accidentally writing
    to the real user config directory. If pytest is detected and the paths
    still point to the real config dir, something is wrong with test setup.
    """
    if "pytest" not in sys.modules:
        return

    # Check if paths are pointing to the real user config dir
    # If properly isolated, they should be pointing to a tmp_path
    from platformdirs import user_data_dir

    real_data_dir = Path(user_data_dir("hopper"))
    if SESSIONS_FILE.is_relative_to(real_data_dir):
        raise RuntimeError(
            "Test isolation failure: sessions.py is trying to write to the real "
            f"config directory ({real_data_dir}). Ensure the isolate_config fixture "
            "from conftest.py is active. This usually means a test is missing the "
            "fixture or running outside pytest."
        )


Stage = Literal["ore", "processing"]
State = Literal["new", "idle", "running", "stuck", "error"]


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
    stage: Stage
    created_at: int  # milliseconds since epoch
    project: str = ""  # Project name this session belongs to
    scope: str = ""  # User's task scope description
    updated_at: int = field(default=0)  # milliseconds since epoch, 0 means use created_at
    state: State = "idle"
    status: str = ""  # Human-readable status text
    tmux_window: str | None = None  # tmux window ID (e.g., "@1")

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
            "tmux_window": self.tmux_window,
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
            status=data.get("status") or data.get("message", ""),  # Backwards compat
            tmux_window=data.get("tmux_window"),  # Backwards compat
        )


def get_session_dir(session_id: str) -> Path:
    """Get the directory for a session."""
    return SESSIONS_DIR / session_id


def load_sessions() -> list[Session]:
    """Load active sessions from JSONL file."""
    if not SESSIONS_FILE.exists():
        return []

    sessions = []
    with open(SESSIONS_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                data = json.loads(line)
                sessions.append(Session.from_dict(data))
    return sessions


def save_sessions(sessions: list[Session]) -> None:
    """Atomically save sessions to JSONL file."""
    _check_test_isolation()
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = SESSIONS_FILE.with_suffix(".jsonl.tmp")
    with open(tmp_path, "w") as f:
        for session in sessions:
            f.write(json.dumps(session.to_dict()) + "\n")

    os.replace(tmp_path, SESSIONS_FILE)


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


def update_session_stage(sessions: list[Session], session_id: str, stage: Stage) -> Session | None:
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
    _check_test_isolation()
    for i, session in enumerate(sessions):
        if session.id == session_id:
            archived = sessions.pop(i)

            # Append to archive file
            ARCHIVED_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(ARCHIVED_FILE, "a") as f:
                f.write(json.dumps(archived.to_dict()) + "\n")

            save_sessions(sessions)
            return archived
    return None


def update_session_state(
    sessions: list[Session], session_id: str, state: State, status: str
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
