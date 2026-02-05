"""Tests for lode management."""

import json

from hopper.lodes import (
    ID_LEN,
    Lode,
    archive_lode,
    create_lode,
    current_time_ms,
    find_by_prefix,
    format_age,
    format_duration_ms,
    format_uptime,
    get_lode_dir,
    load_lodes,
    save_lodes,
    update_lode_stage,
    update_lode_state,
)


def test_lode_to_dict_and_from_dict():
    """Test Lode serialization roundtrip."""
    lode = Lode(
        id="abc-123",
        stage="ore",
        created_at=1234567890,
        updated_at=1234567890,
        state="new",
        active=True,
        tmux_pane=None,
    )
    data = lode.to_dict()
    restored = Lode.from_dict(data)

    assert restored.id == lode.id
    assert restored.stage == lode.stage
    assert restored.created_at == lode.created_at
    assert restored.updated_at == lode.updated_at
    assert restored.state == lode.state
    assert restored.active == lode.active
    assert restored.tmux_pane == lode.tmux_pane


def test_lode_to_dict_includes_codex_thread_id():
    """to_dict includes codex_thread_id field."""
    lode = Lode(
        id="abc-123",
        stage="processing",
        created_at=1000,
        codex_thread_id="codex-uuid-1234",
    )
    data = lode.to_dict()
    assert data["codex_thread_id"] == "codex-uuid-1234"


def test_lode_codex_thread_id_roundtrip():
    """codex_thread_id survives to_dict/from_dict roundtrip."""
    lode = Lode(
        id="abc-123",
        stage="processing",
        created_at=1000,
        updated_at=1000,
        state="running",
        codex_thread_id="thread-xyz",
    )
    restored = Lode.from_dict(lode.to_dict())
    assert restored.codex_thread_id == "thread-xyz"


def test_lode_codex_thread_id_default_none():
    """codex_thread_id defaults to None."""
    lode = Lode(id="abc-123", stage="ore", created_at=1000)
    assert lode.codex_thread_id is None
    assert lode.to_dict()["codex_thread_id"] is None


def test_lode_from_dict_backwards_compat_codex_thread_id():
    """Lodes without 'codex_thread_id' field default to None."""
    data = {
        "id": "abc-123",
        "stage": "ore",
        "created_at": 1000,
        "updated_at": 1000,
        "state": "new",
    }
    lode = Lode.from_dict(data)
    assert lode.codex_thread_id is None


def test_lode_from_dict_backwards_compat_active():
    """Lodes without 'active' field default to False."""
    data = {
        "id": "abc-123",
        "stage": "ore",
        "created_at": 1000,
        "updated_at": 1000,
        "state": "new",
    }
    lode = Lode.from_dict(data)
    assert lode.active is False


def test_load_lodes_empty(temp_config):
    """Test loading when no file exists."""
    lodes_list = load_lodes()
    assert lodes_list == []


def test_save_and_load_lodes(temp_config):
    """Test save/load roundtrip."""
    lodes_list = [
        Lode(id="id-1", stage="ore", created_at=1000),
        Lode(id="id-2", stage="processing", created_at=2000),
    ]
    save_lodes(lodes_list)

    loaded = load_lodes()
    assert len(loaded) == 2
    assert loaded[0].id == "id-1"
    assert loaded[0].stage == "ore"
    assert loaded[1].id == "id-2"
    assert loaded[1].stage == "processing"


def test_create_lode(temp_config):
    """Test lode creation."""
    lodes_list = []
    lode = create_lode(lodes_list, "test-project")

    # Verify 8-char hex ID format
    assert len(lode.id) == ID_LEN
    int(lode.id, 16)  # Raises if not valid hex

    assert lode.stage == "ore"
    assert lode.project == "test-project"
    assert lode.created_at > 0
    assert len(lodes_list) == 1
    assert lodes_list[0] is lode

    # Verify directory was created
    assert get_lode_dir(lode.id).exists()

    # Verify persisted to file
    loaded = load_lodes()
    assert len(loaded) == 1
    assert loaded[0].id == lode.id
    assert loaded[0].project == "test-project"


def test_create_lode_with_scope(temp_config):
    """Test lode creation with scope parameter."""
    lodes_list = []
    lode = create_lode(lodes_list, "test-project", "Fix the login bug")

    assert lode.scope == "Fix the login bug"
    assert lode.project == "test-project"

    # Verify persisted to file
    loaded = load_lodes()
    assert len(loaded) == 1
    assert loaded[0].scope == "Fix the login bug"


def test_update_lode_stage(temp_config):
    """Test updating lode stage."""
    lodes_list = [Lode(id="test-id", stage="ore", created_at=1000)]
    save_lodes(lodes_list)

    updated = update_lode_stage(lodes_list, "test-id", "processing")

    assert updated is not None
    assert updated.stage == "processing"
    assert lodes_list[0].stage == "processing"

    # Verify persisted
    loaded = load_lodes()
    assert loaded[0].stage == "processing"


def test_update_lode_stage_not_found(temp_config):
    """Test updating non-existent lode."""
    lodes_list = []
    result = update_lode_stage(lodes_list, "nonexistent", "processing")
    assert result is None


def test_archive_lode(temp_config):
    """Test archiving a lode."""
    lodes_list = [
        Lode(id="keep-id", stage="ore", created_at=1000),
        Lode(id="archive-id", stage="processing", created_at=2000),
    ]
    save_lodes(lodes_list)

    archived = archive_lode(lodes_list, "archive-id")

    assert archived is not None
    assert archived.id == "archive-id"
    assert len(lodes_list) == 1
    assert lodes_list[0].id == "keep-id"

    # Verify active lodes file
    loaded = load_lodes()
    assert len(loaded) == 1
    assert loaded[0].id == "keep-id"

    # Verify archived file
    archived_file = temp_config / "archived.jsonl"
    assert archived_file.exists()
    with open(archived_file) as f:
        lines = f.readlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["id"] == "archive-id"


def test_archive_lode_not_found(temp_config):
    """Test archiving non-existent lode."""
    lodes_list = []
    result = archive_lode(lodes_list, "nonexistent")
    assert result is None


def test_archive_appends(temp_config):
    """Test that archive appends to existing file."""
    lodes_list = [
        Lode(id="id-1", stage="ore", created_at=1000),
        Lode(id="id-2", stage="ore", created_at=2000),
    ]
    save_lodes(lodes_list)

    archive_lode(lodes_list, "id-1")
    archive_lode(lodes_list, "id-2")

    archived_file = temp_config / "archived.jsonl"
    with open(archived_file) as f:
        lines = f.readlines()
    assert len(lines) == 2


def test_atomic_save(temp_config):
    """Test that save is atomic (no temp file left behind)."""
    lodes_list = [Lode(id="test", stage="ore", created_at=1000)]
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


# Tests for find_by_prefix


def test_find_by_prefix_exact():
    """find_by_prefix matches full ID."""
    lodes = [
        Lode(id="aaaa1111", stage="ore", created_at=1000),
        Lode(id="bbbb2222", stage="ore", created_at=2000),
    ]
    result = find_by_prefix(lodes, "aaaa1111")
    assert result is lodes[0]


def test_find_by_prefix_partial():
    """find_by_prefix matches unique prefix."""
    lodes = [
        Lode(id="aaaa1111", stage="ore", created_at=1000),
        Lode(id="bbbb2222", stage="ore", created_at=2000),
    ]
    result = find_by_prefix(lodes, "aaaa")
    assert result is lodes[0]


def test_find_by_prefix_ambiguous():
    """find_by_prefix returns None for ambiguous prefix."""
    lodes = [
        Lode(id="aaaa1111", stage="ore", created_at=1000),
        Lode(id="aaaa2222", stage="ore", created_at=2000),
    ]
    result = find_by_prefix(lodes, "aaaa")
    assert result is None


def test_find_by_prefix_not_found():
    """find_by_prefix returns None when no match."""
    lodes = [
        Lode(id="aaaa1111", stage="ore", created_at=1000),
    ]
    result = find_by_prefix(lodes, "xxxx")
    assert result is None


def test_find_by_prefix_empty():
    """find_by_prefix returns None for empty list."""
    result = find_by_prefix([], "aaaa")
    assert result is None


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


# Tests for updated_at


def test_lode_updated_at_default():
    """Lode with updated_at=0 uses created_at as effective value."""
    lode = Lode(id="test", stage="ore", created_at=1000, updated_at=0)
    assert lode.effective_updated_at == 1000


def test_lode_updated_at_set():
    """Lode with updated_at set uses that value."""
    lode = Lode(id="test", stage="ore", created_at=1000, updated_at=2000)
    assert lode.effective_updated_at == 2000


def test_lode_touch():
    """touch() updates the updated_at timestamp."""
    lode = Lode(id="test", stage="ore", created_at=1000, updated_at=1000)
    lode.touch()
    assert lode.updated_at > 1000


def test_lode_to_dict_includes_updated_at():
    """to_dict includes updated_at field."""
    lode = Lode(id="test", stage="ore", created_at=1000, updated_at=2000)
    data = lode.to_dict()
    assert data["updated_at"] == 2000


def test_update_lode_stage_touches(temp_config):
    """update_lode_stage updates the timestamp."""
    lodes_list = [Lode(id="test-id", stage="ore", created_at=1000, updated_at=1000)]
    save_lodes(lodes_list)

    updated = update_lode_stage(lodes_list, "test-id", "processing")

    assert updated is not None
    assert updated.updated_at > 1000


def test_update_lode_state(temp_config):
    """update_lode_state changes state and message, touches timestamp."""
    lodes_list = [Lode(id="test-id", stage="ore", created_at=1000, updated_at=1000, state="new")]
    save_lodes(lodes_list)

    updated = update_lode_state(lodes_list, "test-id", "running", "Claude running")

    assert updated is not None
    assert updated.state == "running"
    assert updated.status == "Claude running"
    assert updated.updated_at > 1000

    # Verify persistence
    loaded = load_lodes()
    assert loaded[0].state == "running"
    assert loaded[0].status == "Claude running"


def test_update_lode_state_not_found(temp_config):
    """update_lode_state returns None for unknown lode."""
    lodes_list = []

    result = update_lode_state(lodes_list, "nonexistent", "running", "Test")

    assert result is None


def test_lode_backlog_field_roundtrip():
    """backlog field survives to_dict/from_dict roundtrip."""
    backlog_data = {
        "id": "bl123456",
        "project": "proj",
        "description": "Original task",
        "created_at": 1000,
        "lode_id": None,
    }
    lode = Lode(
        id="abc-123",
        stage="ore",
        created_at=1000,
        updated_at=1000,
        state="new",
        backlog=backlog_data,
    )
    restored = Lode.from_dict(lode.to_dict())
    assert restored.backlog == backlog_data
    assert restored.backlog["project"] == "proj"


def test_lode_backlog_field_default_none():
    """backlog defaults to None."""
    lode = Lode(id="abc-123", stage="ore", created_at=1000)
    assert lode.backlog is None
    assert lode.to_dict()["backlog"] is None


def test_lode_from_dict_backwards_compat_backlog():
    """Lodes without 'backlog' field default to None."""
    data = {
        "id": "abc-123",
        "stage": "ore",
        "created_at": 1000,
        "updated_at": 1000,
        "state": "new",
    }
    lode = Lode.from_dict(data)
    assert lode.backlog is None


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
