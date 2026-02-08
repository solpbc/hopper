# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for lode management."""

import json
import uuid

from hopper.lodes import (
    ID_ALPHABET,
    ID_LEN,
    archive_lode,
    create_lode,
    current_time_ms,
    format_age,
    format_duration_ms,
    format_uptime,
    get_lode_dir,
    load_lodes,
    reset_lode_claude_stage,
    save_lodes,
    set_lode_claude_started,
    slugify,
    touch,
    update_lode_auto,
    update_lode_branch,
    update_lode_codex_thread,
    update_lode_stage,
    update_lode_state,
    update_lode_title,
)


def test_lode_dict_json_roundtrip():
    """Test lode dict serialization roundtrip."""
    lode = {
        "id": "abc12345",
        "stage": "mill",
        "created_at": 1234567890,
        "updated_at": 1234567890,
        "state": "new",
        "active": True,
        "tmux_pane": None,
        "project": "",
        "scope": "",
        "status": "",
        "title": "",
        "codex_thread_id": None,
        "backlog": None,
    }
    serialized = json.dumps(lode)
    restored = json.loads(serialized)

    assert restored["id"] == lode["id"]
    assert restored["stage"] == lode["stage"]
    assert restored["created_at"] == lode["created_at"]
    assert restored["updated_at"] == lode["updated_at"]
    assert restored["state"] == lode["state"]
    assert restored["active"] == lode["active"]
    assert restored["tmux_pane"] == lode["tmux_pane"]


def test_lode_dict_includes_codex_thread_id():
    """Lode dict includes codex_thread_id field."""
    lode = {
        "id": "abc12345",
        "stage": "refine",
        "created_at": 1000,
        "codex_thread_id": "codex-uuid-1234",
    }
    assert lode["codex_thread_id"] == "codex-uuid-1234"


def test_lode_codex_thread_id_roundtrip():
    """codex_thread_id survives json roundtrip."""
    lode = {
        "id": "abc12345",
        "stage": "refine",
        "created_at": 1000,
        "updated_at": 1000,
        "state": "running",
        "codex_thread_id": "thread-xyz",
    }
    restored = json.loads(json.dumps(lode))
    assert restored["codex_thread_id"] == "thread-xyz"


def test_load_lodes_empty(temp_config):
    """Test loading when no file exists."""
    lodes_list = load_lodes()
    assert lodes_list == []


def test_save_and_load_lodes(temp_config):
    """Test save/load roundtrip."""
    lodes_list = [
        {"id": "id111111", "stage": "mill", "created_at": 1000, "updated_at": 1000, "state": "new"},
        {
            "id": "id222222",
            "stage": "refine",
            "created_at": 2000,
            "updated_at": 2000,
            "state": "new",
        },
    ]
    save_lodes(lodes_list)

    loaded = load_lodes()
    assert len(loaded) == 2
    assert loaded[0]["id"] == "id111111"
    assert loaded[0]["stage"] == "mill"
    assert loaded[1]["id"] == "id222222"
    assert loaded[1]["stage"] == "refine"


def test_create_lode(temp_config):
    """Test lode creation."""
    lodes_list = []
    lode = create_lode(lodes_list, "test-project")

    # Verify 8-char base32 ID format
    assert len(lode["id"]) == ID_LEN
    assert all(c in ID_ALPHABET for c in lode["id"])

    assert lode["stage"] == "mill"
    assert lode["project"] == "test-project"
    assert lode["branch"] == ""
    assert lode["created_at"] > 0
    assert len(lodes_list) == 1
    assert lodes_list[0] is lode

    # Verify per-stage Claude sessions
    claude = lode["claude"]
    for stage in ("mill", "refine", "ship"):
        assert stage in claude
        # Valid UUID format
        import uuid

        uuid.UUID(claude[stage]["session_id"])
        assert claude[stage]["started"] is False

    # Verify directory was created
    assert get_lode_dir(lode["id"]).exists()

    # Verify persisted to file
    loaded = load_lodes()
    assert len(loaded) == 1
    assert loaded[0]["id"] == lode["id"]
    assert loaded[0]["project"] == "test-project"


def test_create_lode_with_scope(temp_config):
    """Test lode creation with scope parameter."""
    lodes_list = []
    lode = create_lode(lodes_list, "test-project", "Fix the login bug")

    assert lode["scope"] == "Fix the login bug"
    assert lode["project"] == "test-project"

    # Verify persisted to file
    loaded = load_lodes()
    assert len(loaded) == 1
    assert loaded[0]["scope"] == "Fix the login bug"


def test_update_lode_stage(temp_config):
    """Test updating lode stage."""
    lodes_list = [
        {"id": "testid11", "stage": "mill", "created_at": 1000, "updated_at": 1000, "state": "new"}
    ]
    save_lodes(lodes_list)

    updated = update_lode_stage(lodes_list, "testid11", "refine")

    assert updated is not None
    assert updated["stage"] == "refine"
    assert lodes_list[0]["stage"] == "refine"

    # Verify persisted
    loaded = load_lodes()
    assert loaded[0]["stage"] == "refine"


def test_update_lode_stage_not_found(temp_config):
    """Test updating non-existent lode."""
    lodes_list = []
    result = update_lode_stage(lodes_list, "nonexistent", "refine")
    assert result is None


def test_archive_lode(temp_config):
    """Test archiving a lode."""
    lodes_list = [
        {"id": "keepid11", "stage": "mill", "created_at": 1000, "updated_at": 1000, "state": "new"},
        {
            "id": "archivid",
            "stage": "refine",
            "created_at": 2000,
            "updated_at": 2000,
            "state": "new",
        },
    ]
    save_lodes(lodes_list)

    archived = archive_lode(lodes_list, "archivid")

    assert archived is not None
    assert archived["id"] == "archivid"
    assert len(lodes_list) == 1
    assert lodes_list[0]["id"] == "keepid11"

    # Verify active lodes file
    loaded = load_lodes()
    assert len(loaded) == 1
    assert loaded[0]["id"] == "keepid11"

    # Verify archived file
    archived_file = temp_config / "archived.jsonl"
    assert archived_file.exists()
    with open(archived_file) as f:
        lines = f.readlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["id"] == "archivid"


def test_archive_lode_not_found(temp_config):
    """Test archiving non-existent lode."""
    lodes_list = []
    result = archive_lode(lodes_list, "nonexistent")
    assert result is None


def test_archive_appends(temp_config):
    """Test that archive appends to existing file."""
    lodes_list = [
        {"id": "id111111", "stage": "mill", "created_at": 1000, "updated_at": 1000, "state": "new"},
        {"id": "id222222", "stage": "mill", "created_at": 2000, "updated_at": 2000, "state": "new"},
    ]
    save_lodes(lodes_list)

    archive_lode(lodes_list, "id111111")
    archive_lode(lodes_list, "id222222")

    archived_file = temp_config / "archived.jsonl"
    with open(archived_file) as f:
        lines = f.readlines()
    assert len(lines) == 2


def test_atomic_save(temp_config):
    """Test that save is atomic (no temp file left behind)."""
    lodes_list = [
        {"id": "testid11", "stage": "mill", "created_at": 1000, "updated_at": 1000, "state": "new"}
    ]
    save_lodes(lodes_list)

    # No .tmp file should exist
    tmp_file = temp_config / "active.jsonl.tmp"
    assert not tmp_file.exists()

    # Main file should exist
    main_file = temp_config / "active.jsonl"
    assert main_file.exists()


def test_get_lode_dir(temp_config):
    """Test lode directory path."""
    path = get_lode_dir("my-lode-id")
    assert path == temp_config / "lodes" / "my-lode-id"


# Tests for format_age


def test_format_age_now():
    """Timestamps less than 1 minute ago return 'now'."""
    now = current_time_ms()
    assert format_age(now) == "now"
    assert format_age(now - 30_000) == "now"  # 30 seconds ago


def test_format_age_minutes():
    """Timestamps 1-59 minutes ago return Xm."""
    now = current_time_ms()
    assert format_age(now - 60_000) == "1m"  # 1 minute
    assert format_age(now - 5 * 60_000) == "5m"  # 5 minutes
    assert format_age(now - 59 * 60_000) == "59m"  # 59 minutes


def test_format_age_hours():
    """Timestamps 1-23 hours ago return Xh."""
    now = current_time_ms()
    assert format_age(now - 60 * 60_000) == "1h"  # 1 hour
    assert format_age(now - 5 * 60 * 60_000) == "5h"  # 5 hours
    assert format_age(now - 23 * 60 * 60_000) == "23h"  # 23 hours


def test_format_age_days():
    """Timestamps 1-6 days ago return Xd."""
    now = current_time_ms()
    assert format_age(now - 24 * 60 * 60_000) == "1d"  # 1 day
    assert format_age(now - 3 * 24 * 60 * 60_000) == "3d"  # 3 days
    assert format_age(now - 6 * 24 * 60 * 60_000) == "6d"  # 6 days


def test_format_age_weeks():
    """Timestamps 7+ days ago return Xw."""
    now = current_time_ms()
    assert format_age(now - 7 * 24 * 60 * 60_000) == "1w"  # 1 week
    assert format_age(now - 14 * 24 * 60 * 60_000) == "2w"  # 2 weeks


def test_format_age_future():
    """Future timestamps return 'now'."""
    now = current_time_ms()
    assert format_age(now + 60_000) == "now"  # 1 minute in future


# Tests for format_uptime


def test_format_uptime_zero():
    """Very recent start returns '0m'."""
    now = current_time_ms()
    assert format_uptime(now) == "0m"
    assert format_uptime(now - 30_000) == "0m"  # 30 seconds


def test_format_uptime_minutes():
    """Minutes-old uptime shows minutes."""
    now = current_time_ms()
    assert format_uptime(now - 5 * 60_000) == "5m"
    assert format_uptime(now - 45 * 60_000) == "45m"


def test_format_uptime_hours():
    """Hours-old uptime shows hours and minutes."""
    now = current_time_ms()
    assert format_uptime(now - 2 * 60 * 60_000) == "2h"
    assert format_uptime(now - (2 * 60 + 15) * 60_000) == "2h 15m"


def test_format_uptime_days():
    """Days-old uptime shows days and hours, not minutes."""
    now = current_time_ms()
    assert format_uptime(now - 3 * 24 * 60 * 60_000) == "3d"
    assert format_uptime(now - (3 * 24 + 4) * 60 * 60_000) == "3d 4h"
    # Minutes not shown when days > 0
    assert format_uptime(now - (1 * 24 * 60 + 30) * 60_000) == "1d"


def test_slugify_empty():
    """slugify returns empty string for empty input."""
    assert slugify("") == ""


def test_slugify_all_special_chars():
    """slugify strips all special characters."""
    assert slugify("!!!@@@###") == ""


def test_slugify_basic():
    """slugify lowercases and hyphenates words."""
    assert slugify("Branch Naming") == "branch-naming"


def test_slugify_symbols():
    """slugify removes symbols and keeps separators normalized."""
    assert slugify("hello world! @#$ test") == "hello-world-test"


def test_slugify_truncates_to_40_chars():
    """slugify truncates to max length and avoids trailing hyphen."""
    result = slugify("a" * 60)
    assert len(result) == 40
    assert not result.endswith("-")


def test_slugify_strips_wrapping_hyphens():
    """slugify strips leading and trailing separators."""
    assert slugify("---hello---") == "hello"


def test_slugify_non_ascii():
    """slugify strips non-ascii characters."""
    assert slugify("名前テスト") == ""


def test_slugify_strips_lock_suffix():
    """slugify removes a trailing .lock suffix."""
    assert slugify("test.lock") == "test"


def test_touch():
    """touch() updates the updated_at timestamp."""
    lode = {"id": "testid11", "stage": "mill", "created_at": 1000, "updated_at": 1000}
    touch(lode)
    assert lode["updated_at"] > 1000


def test_update_lode_stage_touches(temp_config):
    """update_lode_stage updates the timestamp."""
    lodes_list = [
        {"id": "testid11", "stage": "mill", "created_at": 1000, "updated_at": 1000, "state": "new"}
    ]
    save_lodes(lodes_list)

    updated = update_lode_stage(lodes_list, "testid11", "refine")

    assert updated is not None
    assert updated["updated_at"] > 1000


def test_update_lode_state(temp_config):
    """update_lode_state changes state and message, touches timestamp."""
    lodes_list = [
        {"id": "testid11", "stage": "mill", "created_at": 1000, "updated_at": 1000, "state": "new"}
    ]
    save_lodes(lodes_list)

    updated = update_lode_state(lodes_list, "testid11", "running", "Claude running")

    assert updated is not None
    assert updated["state"] == "running"
    assert updated["status"] == "Claude running"
    assert updated["updated_at"] > 1000

    # Verify persistence
    loaded = load_lodes()
    assert loaded[0]["state"] == "running"
    assert loaded[0]["status"] == "Claude running"


def test_update_lode_state_not_found(temp_config):
    """update_lode_state returns None for unknown lode."""
    lodes_list = []

    result = update_lode_state(lodes_list, "nonexistent", "running", "Test")

    assert result is None


def test_update_lode_title(temp_config):
    """update_lode_title changes title and touches timestamp."""
    lodes_list = [
        {"id": "testid11", "stage": "mill", "created_at": 1000, "updated_at": 1000, "state": "new"}
    ]
    save_lodes(lodes_list)

    updated = update_lode_title(lodes_list, "testid11", "Auth Flow")

    assert updated is not None
    assert updated["title"] == "Auth Flow"
    assert updated["updated_at"] > 1000

    # Verify persistence
    loaded = load_lodes()
    assert loaded[0]["title"] == "Auth Flow"


def test_update_lode_branch(temp_config):
    """update_lode_branch changes branch and touches timestamp."""
    lodes_list = [
        {
            "id": "testid11",
            "stage": "mill",
            "created_at": 1000,
            "updated_at": 1000,
            "state": "new",
            "branch": "",
        }
    ]
    save_lodes(lodes_list)

    updated = update_lode_branch(lodes_list, "testid11", "hopper-testid11-auth-flow")

    assert updated is not None
    assert updated["branch"] == "hopper-testid11-auth-flow"
    assert updated["updated_at"] > 1000

    loaded = load_lodes()
    assert loaded[0]["branch"] == "hopper-testid11-auth-flow"


def test_update_lode_auto(temp_config):
    """update_lode_auto changes auto flag and touches timestamp."""
    lodes_list = [
        {
            "id": "testid11",
            "stage": "mill",
            "created_at": 1000,
            "updated_at": 1000,
            "state": "new",
            "auto": False,
        }
    ]
    save_lodes(lodes_list)

    updated = update_lode_auto(lodes_list, "testid11", True)

    assert updated is not None
    assert updated["auto"] is True
    assert updated["updated_at"] > 1000

    loaded = load_lodes()
    assert loaded[0]["auto"] is True


def test_update_lode_auto_not_found(temp_config):
    """update_lode_auto returns None for unknown lode."""
    result = update_lode_auto([], "nonexistent", True)
    assert result is None


def test_update_lode_codex_thread(temp_config):
    """update_lode_codex_thread changes thread ID and touches timestamp."""
    lodes_list = [
        {
            "id": "testid11",
            "stage": "refine",
            "created_at": 1000,
            "updated_at": 1000,
            "state": "running",
            "codex_thread_id": None,
        }
    ]
    save_lodes(lodes_list)

    updated = update_lode_codex_thread(lodes_list, "testid11", "thread-123")

    assert updated is not None
    assert updated["codex_thread_id"] == "thread-123"
    assert updated["updated_at"] > 1000

    loaded = load_lodes()
    assert loaded[0]["codex_thread_id"] == "thread-123"


def test_update_lode_codex_thread_not_found(temp_config):
    """update_lode_codex_thread returns None for unknown lode."""
    result = update_lode_codex_thread([], "nonexistent", "thread-123")
    assert result is None


def test_set_lode_claude_started(temp_config):
    """set_lode_claude_started marks stage as started and touches timestamp."""
    lodes_list = [
        {
            "id": "testid11",
            "stage": "mill",
            "created_at": 1000,
            "updated_at": 1000,
            "state": "running",
            "claude": {"mill": {"session_id": "session-1", "started": False}},
        }
    ]
    save_lodes(lodes_list)

    updated = set_lode_claude_started(lodes_list, "testid11", "mill")

    assert updated is not None
    assert updated["claude"]["mill"]["started"] is True
    assert updated["updated_at"] > 1000

    loaded = load_lodes()
    assert loaded[0]["claude"]["mill"]["started"] is True


def test_set_lode_claude_started_not_found(temp_config):
    """set_lode_claude_started returns None for unknown lode."""
    result = set_lode_claude_started([], "nonexistent", "mill")
    assert result is None


def test_set_lode_claude_started_invalid_stage(temp_config):
    """set_lode_claude_started returns None for unknown claude stage."""
    lodes_list = [
        {
            "id": "testid11",
            "stage": "mill",
            "created_at": 1000,
            "updated_at": 1000,
            "state": "running",
            "claude": {"mill": {"session_id": "session-1", "started": False}},
        }
    ]
    save_lodes(lodes_list)

    result = set_lode_claude_started(lodes_list, "testid11", "ship")

    assert result is None
    assert lodes_list[0]["claude"]["mill"]["started"] is False


def test_reset_lode_claude_stage(temp_config):
    """reset_lode_claude_stage resets session and started flag, then touches timestamp."""
    lodes_list = [
        {
            "id": "testid11",
            "stage": "mill",
            "created_at": 1000,
            "updated_at": 1000,
            "state": "running",
            "claude": {"mill": {"session_id": "session-1", "started": True}},
        }
    ]
    save_lodes(lodes_list)

    updated = reset_lode_claude_stage(lodes_list, "testid11", "mill")

    assert updated is not None
    assert updated["claude"]["mill"]["started"] is False
    assert updated["claude"]["mill"]["session_id"] != "session-1"
    uuid.UUID(updated["claude"]["mill"]["session_id"])
    assert updated["updated_at"] > 1000

    loaded = load_lodes()
    assert loaded[0]["claude"]["mill"]["started"] is False
    assert loaded[0]["claude"]["mill"]["session_id"] != "session-1"


def test_reset_lode_claude_stage_not_found(temp_config):
    """reset_lode_claude_stage returns None for unknown lode."""
    result = reset_lode_claude_stage([], "nonexistent", "mill")
    assert result is None


def test_reset_lode_claude_stage_invalid_stage(temp_config):
    """reset_lode_claude_stage returns None for unknown claude stage."""
    lodes_list = [
        {
            "id": "testid11",
            "stage": "mill",
            "created_at": 1000,
            "updated_at": 1000,
            "state": "running",
            "claude": {"mill": {"session_id": "session-1", "started": True}},
        }
    ]
    save_lodes(lodes_list)

    result = reset_lode_claude_stage(lodes_list, "testid11", "ship")

    assert result is None
    assert lodes_list[0]["claude"]["mill"]["started"] is True
    assert lodes_list[0]["claude"]["mill"]["session_id"] == "session-1"


def test_lode_backlog_field_roundtrip():
    """backlog field survives json roundtrip."""
    backlog_data = {
        "id": "bl123456",
        "project": "proj",
        "description": "Original task",
        "created_at": 1000,
        "lode_id": None,
    }
    lode = {
        "id": "abc12345",
        "stage": "mill",
        "created_at": 1000,
        "updated_at": 1000,
        "state": "new",
        "backlog": backlog_data,
    }
    restored = json.loads(json.dumps(lode))
    assert restored["backlog"] == backlog_data
    assert restored["backlog"]["project"] == "proj"


# Tests for format_duration_ms


def test_format_duration_ms_zero():
    """Durations less than 1 second return '0s'."""
    assert format_duration_ms(0) == "0s"
    assert format_duration_ms(500) == "0s"
    assert format_duration_ms(999) == "0s"


def test_format_duration_ms_seconds():
    """Durations 1-59 seconds return Xs."""
    assert format_duration_ms(1000) == "1s"
    assert format_duration_ms(5000) == "5s"
    assert format_duration_ms(42_000) == "42s"
    assert format_duration_ms(59_000) == "59s"


def test_format_duration_ms_minutes():
    """Durations 1-59 minutes return Xm."""
    assert format_duration_ms(60_000) == "1m"
    assert format_duration_ms(5 * 60_000) == "5m"
    assert format_duration_ms(59 * 60_000) == "59m"


def test_format_duration_ms_hours():
    """Durations 1+ hours return Xh."""
    assert format_duration_ms(60 * 60_000) == "1h"
    assert format_duration_ms(3 * 60 * 60_000) == "3h"
