"""Backlog management for hopper."""

import json
import os
import uuid
from dataclasses import dataclass

from hopper import config
from hopper.sessions import SHORT_ID_LEN, current_time_ms


@dataclass
class BacklogItem:
    """A backlog item."""

    id: str
    project: str
    description: str
    created_at: int  # milliseconds since epoch
    session_id: str | None = None  # session that added it

    @property
    def short_id(self) -> str:
        """Return the 8-character short ID."""
        return self.id[:SHORT_ID_LEN]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project": self.project,
            "description": self.description,
            "created_at": self.created_at,
            "session_id": self.session_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BacklogItem":
        return cls(
            id=data["id"],
            project=data["project"],
            description=data["description"],
            created_at=data["created_at"],
            session_id=data.get("session_id"),
        )


def load_backlog() -> list[BacklogItem]:
    """Load backlog items from JSONL file."""
    backlog_file = config.hopper_dir() / "backlog.jsonl"
    if not backlog_file.exists():
        return []

    items = []
    with open(backlog_file) as f:
        for line in f:
            line = line.strip()
            if line:
                data = json.loads(line)
                items.append(BacklogItem.from_dict(data))
    return items


def save_backlog(items: list[BacklogItem]) -> None:
    """Atomically save backlog items to JSONL file."""
    backlog_file = config.hopper_dir() / "backlog.jsonl"
    backlog_file.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = backlog_file.with_suffix(".jsonl.tmp")
    with open(tmp_path, "w") as f:
        for item in items:
            f.write(json.dumps(item.to_dict()) + "\n")

    os.replace(tmp_path, backlog_file)


def add_backlog_item(
    items: list[BacklogItem],
    project: str,
    description: str,
    session_id: str | None = None,
) -> BacklogItem:
    """Create a new backlog item, add to list, and persist."""
    item = BacklogItem(
        id=str(uuid.uuid4()),
        project=project,
        description=description,
        created_at=current_time_ms(),
        session_id=session_id,
    )
    items.append(item)
    save_backlog(items)
    return item


def remove_backlog_item(items: list[BacklogItem], item_id: str) -> BacklogItem | None:
    """Remove a backlog item by ID. Returns the removed item or None."""
    for i, item in enumerate(items):
        if item.id == item_id:
            removed = items.pop(i)
            save_backlog(items)
            return removed
    return None


def update_backlog_item(
    items: list[BacklogItem], item_id: str, description: str
) -> BacklogItem | None:
    """Update a backlog item's description. Returns the updated item or None."""
    for item in items:
        if item.id == item_id:
            item.description = description
            save_backlog(items)
            return item
    return None


def find_by_short_id(items: list[BacklogItem], prefix: str) -> BacklogItem | None:
    """Find a backlog item by ID prefix. Returns None if not found or ambiguous."""
    matches = [item for item in items if item.id.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    return None
