"""Session management for hopper."""

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from hopper.config import ARCHIVED_FILE, SESSIONS_DIR, SESSIONS_FILE

Stage = Literal["ore", "processing"]
State = Literal["idle", "running", "error"]


SHORT_ID_LEN = 8  # Standard short ID length (first segment of UUID)


@dataclass
class Session:
    """A hopper session."""

    id: str
    stage: Stage
    created_at: int  # milliseconds since epoch
    state: State = "idle"
    tmux_window: str | None = None  # tmux window ID (e.g., "@1")

    @property
    def short_id(self) -> str:
        """Return the 8-character short ID (first segment of UUID)."""
        return self.id[:SHORT_ID_LEN]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "stage": self.stage,
            "created_at": self.created_at,
            "state": self.state,
            "tmux_window": self.tmux_window,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(
            id=data["id"],
            stage=data["stage"],
            created_at=data["created_at"],
            state=data.get("state", "idle"),
            tmux_window=data.get("tmux_window"),
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
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = SESSIONS_FILE.with_suffix(".jsonl.tmp")
    with open(tmp_path, "w") as f:
        for session in sessions:
            f.write(json.dumps(session.to_dict()) + "\n")

    os.replace(tmp_path, SESSIONS_FILE)


def create_session(sessions: list[Session]) -> Session:
    """Create a new session, add to list, and create its directory."""
    session = Session(
        id=str(uuid.uuid4()),
        stage="ore",
        created_at=int(time.time() * 1000),
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
            ARCHIVED_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(ARCHIVED_FILE, "a") as f:
                f.write(json.dumps(archived.to_dict()) + "\n")

            save_sessions(sessions)
            return archived
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
