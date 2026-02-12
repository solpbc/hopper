# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for backlog management."""

from hopper.backlog import (
    BacklogItem,
    add_backlog_item,
    find_by_prefix,
    load_backlog,
    remove_backlog_item,
    save_backlog,
    set_backlog_queued,
    update_backlog_item,
)


def test_backlog_item_to_dict_and_from_dict():
    """Test BacklogItem serialization roundtrip."""
    item = BacklogItem(
        id="abc12345",
        project="myproject",
        description="Fix the bug",
        created_at=1234567890,
        lode_id="sess4567",
    )
    data = item.to_dict()
    restored = BacklogItem.from_dict(data)

    assert restored.id == item.id
    assert restored.project == item.project
    assert restored.description == item.description
    assert restored.created_at == item.created_at
    assert restored.lode_id == item.lode_id


def test_backlog_item_no_lode_id():
    """BacklogItem without lode_id defaults to None."""
    data = {
        "id": "abc-123",
        "project": "myproject",
        "description": "Do a thing",
        "created_at": 1000,
    }
    item = BacklogItem.from_dict(data)
    assert item.lode_id is None


def test_backlog_item_queued_field(tmp_path, monkeypatch):
    """BacklogItem should support queued field."""
    monkeypatch.setattr("hopper.backlog.config.hopper_dir", lambda: tmp_path)
    items = []
    item = add_backlog_item(items, "proj", "desc")
    assert item.queued is None

    # Set queued
    updated = set_backlog_queued(items, item.id, "lode123")
    assert updated.queued == "lode123"

    # Roundtrip through dict
    d = updated.to_dict()
    assert d["queued"] == "lode123"
    restored = BacklogItem.from_dict(d)
    assert restored.queued == "lode123"

    # Clear queued
    cleared = set_backlog_queued(items, item.id, None)
    assert cleared.queued is None


def test_backlog_item_from_dict_missing_queued():
    """from_dict should handle missing queued key (backward compat)."""
    data = {
        "id": "abc",
        "project": "proj",
        "description": "desc",
        "created_at": 1000,
    }
    item = BacklogItem.from_dict(data)
    assert item.queued is None


def test_set_backlog_queued_not_found(tmp_path, monkeypatch):
    """set_backlog_queued should return None for unknown item."""
    monkeypatch.setattr("hopper.backlog.config.hopper_dir", lambda: tmp_path)
    items = []
    result = set_backlog_queued(items, "nonexistent", "lode123")
    assert result is None


def test_load_backlog_empty(temp_config):
    """Test loading when no file exists."""
    items = load_backlog()
    assert items == []


def test_save_and_load_backlog(temp_config):
    """Test save/load roundtrip."""
    items = [
        BacklogItem(id="id-1", project="proj-a", description="First", created_at=1000),
        BacklogItem(
            id="id-2",
            project="proj-b",
            description="Second",
            created_at=2000,
            lode_id="sess-1",
        ),
    ]
    save_backlog(items)

    loaded = load_backlog()
    assert len(loaded) == 2
    assert loaded[0].id == "id-1"
    assert loaded[0].project == "proj-a"
    assert loaded[1].id == "id-2"
    assert loaded[1].lode_id == "sess-1"


def test_save_backlog_atomic(temp_config):
    """Atomic write: no .tmp file left behind."""
    items = [BacklogItem(id="id-1", project="p", description="d", created_at=1000)]
    save_backlog(items)

    backlog_file = temp_config / "backlog.jsonl"
    assert backlog_file.exists()
    assert not backlog_file.with_suffix(".jsonl.tmp").exists()


def test_add_backlog_item(temp_config):
    """add_backlog_item creates and persists a new item."""
    items: list[BacklogItem] = []
    item = add_backlog_item(items, "myproject", "Do something")

    assert len(items) == 1
    assert item.project == "myproject"
    assert item.description == "Do something"
    assert item.lode_id is None
    assert item.created_at > 0

    # Verify persisted
    loaded = load_backlog()
    assert len(loaded) == 1
    assert loaded[0].id == item.id


def test_add_backlog_item_with_session(temp_config):
    """add_backlog_item records the session that added it."""
    items: list[BacklogItem] = []
    item = add_backlog_item(items, "proj", "Task", lode_id="sess-123")
    assert item.lode_id == "sess-123"


def test_remove_backlog_item(temp_config):
    """remove_backlog_item removes and persists."""
    items: list[BacklogItem] = []
    item = add_backlog_item(items, "proj", "To remove")

    removed = remove_backlog_item(items, item.id)
    assert removed is not None
    assert removed.id == item.id
    assert len(items) == 0

    loaded = load_backlog()
    assert len(loaded) == 0


def test_remove_backlog_item_not_found(temp_config):
    """remove_backlog_item returns None for unknown ID."""
    items: list[BacklogItem] = []
    add_backlog_item(items, "proj", "Keep")

    result = remove_backlog_item(items, "nonexistent-id")
    assert result is None
    assert len(items) == 1


def test_update_backlog_item(temp_config):
    """update_backlog_item updates description and persists."""
    items: list[BacklogItem] = []
    item = add_backlog_item(items, "proj", "Original text")

    updated = update_backlog_item(items, item.id, "Updated text")
    assert updated is not None
    assert updated.description == "Updated text"
    assert items[0].description == "Updated text"

    # Verify persisted
    loaded = load_backlog()
    assert loaded[0].description == "Updated text"


def test_update_backlog_item_not_found(temp_config):
    """update_backlog_item returns None for unknown ID."""
    items: list[BacklogItem] = []
    add_backlog_item(items, "proj", "Keep")

    result = update_backlog_item(items, "nonexistent-id", "New text")
    assert result is None
    assert items[0].description == "Keep"


def test_find_by_prefix():
    """find_by_prefix finds unique match."""
    items = [
        BacklogItem(id="aaaa1111", project="p", description="d", created_at=1000),
        BacklogItem(id="bbbb2222", project="p", description="d", created_at=2000),
    ]
    assert find_by_prefix(items, "aaaa1111") is not None
    assert find_by_prefix(items, "aaaa1111").id == "aaaa1111"


def test_find_by_prefix_not_found():
    """find_by_prefix returns None when no match."""
    items = [
        BacklogItem(id="aaaa1111", project="p", description="d", created_at=1000),
    ]
    assert find_by_prefix(items, "zzzz") is None


def test_find_by_prefix_ambiguous():
    """find_by_prefix returns None when multiple matches."""
    items = [
        BacklogItem(id="aaaa1111", project="p", description="d", created_at=1000),
        BacklogItem(id="aaaa1122", project="p", description="d", created_at=2000),
    ]
    assert find_by_prefix(items, "aaaa11") is None
