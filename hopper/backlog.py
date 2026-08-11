# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Backlog management for hopper."""

import json
import os
import secrets
from dataclasses import dataclass

from hopper import config
from hopper.lodes import ID_ALPHABET, ID_LEN, current_time_ms


@dataclass
class BacklogItem:
    """A backlog item."""

    id: str
    project: str
    description: str
    created_at: int  # milliseconds since epoch
    lode_id: str | None = None  # lode that added it
    queued: str | None = None  # lode this item is queued behind

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project": self.project,
            "description": self.description,
            "created_at": self.created_at,
            "lode_id": self.lode_id,
            "queued": self.queued,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BacklogItem":
        return cls(
            id=data["id"],
            project=data["project"],
            description=data["description"],
            created_at=data["created_at"],
            lode_id=data.get("lode_id"),
            queued=data.get("queued"),
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
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, backlog_file)
    directory_fd = os.open(backlog_file.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def add_backlog_item(
    items: list[BacklogItem],
    project: str,
    description: str,
    lode_id: str | None = None,
) -> BacklogItem:
    """Create a new backlog item, add to list, and persist."""
    # Generate unique ID (collision unlikely but check anyway)
    existing_ids = {item.id for item in items}
    for _ in range(100):
        new_id = "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LEN))
        if new_id not in existing_ids:
            break
    else:
        raise RuntimeError("Failed to generate unique backlog ID")

    item = BacklogItem(
        id=new_id,
        project=project,
        description=description,
        created_at=current_time_ms(),
        lode_id=lode_id,
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


def set_backlog_queued(
    items: list[BacklogItem], item_id: str, queued: str | None
) -> BacklogItem | None:
    """Set the queued lode ID on a backlog item. Returns the updated item or None."""
    for item in items:
        if item.id == item_id:
            item.queued = queued
            save_backlog(items)
            return item
    return None


def apply_completion_disposition(
    items: list[BacklogItem],
    *,
    source_lode_id: str,
    source_project: str,
    selected_item_id: str | None,
    promoted_lode_id: str | None,
    remaining_item_ids: list[str],
) -> None:
    """Apply one persisted ship plan, promoting matches and detaching the rest."""
    by_id: dict[str, BacklogItem] = {}
    for item in items:
        if item.id in by_id:
            raise ValueError(f"backlog item {item.id} is duplicated")
        by_id[item.id] = item

    selected = by_id.get(selected_item_id)
    if selected is not None and selected.queued != source_lode_id:
        raise ValueError("selected backlog item no longer belongs to the completed lode")
    for item_id in remaining_item_ids:
        item = by_id.get(item_id)
        if item is None:
            raise ValueError(f"planned backlog item {item_id} is absent")
        destination = (
            promoted_lode_id if promoted_lode_id and item.project == source_project else None
        )
        if item.queued not in {source_lode_id, destination}:
            raise ValueError(f"planned backlog item {item_id} has conflicting queue ownership")

    if selected is not None:
        items.remove(selected)
    for item_id in remaining_item_ids:
        item = by_id[item_id]
        item.queued = (
            promoted_lode_id if promoted_lode_id and item.project == source_project else None
        )
    save_backlog(items)


def find_by_prefix(items: list[BacklogItem], prefix: str) -> BacklogItem | None:
    """Find a backlog item by ID prefix. Returns None if not found or ambiguous."""
    matches = [item for item in items if item.id.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    return None
