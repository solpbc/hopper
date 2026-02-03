"""Tests for session management."""

import json
import uuid

from hopper.sessions import (
    SHORT_ID_LEN,
    Session,
    archive_session,
    create_session,
    current_time_ms,
    find_by_short_id,
    format_age,
    format_uptime,
    get_session_dir,
    load_sessions,
    save_sessions,
    update_session_stage,
    update_session_state,
)


def test_session_to_dict_and_from_dict():
    """Test Session serialization roundtrip."""
    session = Session(
        id="abc-123",
        stage="ore",
        created_at=1234567890,
        updated_at=1234567890,
        state="idle",
        tmux_window=None,
    )
    data = session.to_dict()
    restored = Session.from_dict(data)

    assert restored.id == session.id
    assert restored.stage == session.stage
    assert restored.created_at == session.created_at
    assert restored.updated_at == session.updated_at
    assert restored.state == session.state
    assert restored.tmux_window == session.tmux_window


def test_load_sessions_empty(temp_config):
    """Test loading when no file exists."""
    sessions_list = load_sessions()
    assert sessions_list == []


def test_save_and_load_sessions(temp_config):
    """Test save/load roundtrip."""
    sessions_list = [
        Session(id="id-1", stage="ore", created_at=1000),
        Session(id="id-2", stage="processing", created_at=2000),
    ]
    save_sessions(sessions_list)

    loaded = load_sessions()
    assert len(loaded) == 2
    assert loaded[0].id == "id-1"
    assert loaded[0].stage == "ore"
    assert loaded[1].id == "id-2"
    assert loaded[1].stage == "processing"


def test_create_session(temp_config):
    """Test session creation."""
    sessions_list = []
    session = create_session(sessions_list, "test-project")

    # Verify UUID format
    uuid.UUID(session.id)  # Raises if invalid

    assert session.stage == "ore"
    assert session.project == "test-project"
    assert session.created_at > 0
    assert len(sessions_list) == 1
    assert sessions_list[0] is session

    # Verify directory was created
    assert get_session_dir(session.id).exists()

    # Verify persisted to file
    loaded = load_sessions()
    assert len(loaded) == 1
    assert loaded[0].id == session.id
    assert loaded[0].project == "test-project"


def test_update_session_stage(temp_config):
    """Test updating session stage."""
    sessions_list = [Session(id="test-id", stage="ore", created_at=1000)]
    save_sessions(sessions_list)

    updated = update_session_stage(sessions_list, "test-id", "processing")

    assert updated is not None
    assert updated.stage == "processing"
    assert sessions_list[0].stage == "processing"

    # Verify persisted
    loaded = load_sessions()
    assert loaded[0].stage == "processing"


def test_update_session_stage_not_found(temp_config):
    """Test updating non-existent session."""
    sessions_list = []
    result = update_session_stage(sessions_list, "nonexistent", "processing")
    assert result is None


def test_archive_session(temp_config):
    """Test archiving a session."""
    sessions_list = [
        Session(id="keep-id", stage="ore", created_at=1000),
        Session(id="archive-id", stage="processing", created_at=2000),
    ]
    save_sessions(sessions_list)

    archived = archive_session(sessions_list, "archive-id")

    assert archived is not None
    assert archived.id == "archive-id"
    assert len(sessions_list) == 1
    assert sessions_list[0].id == "keep-id"

    # Verify active sessions file
    loaded = load_sessions()
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


def test_archive_session_not_found(temp_config):
    """Test archiving non-existent session."""
    sessions_list = []
    result = archive_session(sessions_list, "nonexistent")
    assert result is None


def test_archive_appends(temp_config):
    """Test that archive appends to existing file."""
    sessions_list = [
        Session(id="id-1", stage="ore", created_at=1000),
        Session(id="id-2", stage="ore", created_at=2000),
    ]
    save_sessions(sessions_list)

    archive_session(sessions_list, "id-1")
    archive_session(sessions_list, "id-2")

    archived_file = temp_config / "archived.jsonl"
    with open(archived_file) as f:
        lines = f.readlines()
    assert len(lines) == 2


def test_atomic_save(temp_config):
    """Test that save is atomic (no temp file left behind)."""
    sessions_list = [Session(id="test", stage="ore", created_at=1000)]
    save_sessions(sessions_list)

    # No .tmp file should exist
    tmp_file = temp_config / "sessions.jsonl.tmp"
    assert not tmp_file.exists()

    # Main file should exist
    main_file = temp_config / "sessions.jsonl"
    assert main_file.exists()


def test_get_session_dir(temp_config):
    """Test session directory path."""
    path = get_session_dir("my-session-id")
    assert path == temp_config / "sessions" / "my-session-id"


# Tests for short_id


def test_short_id_length():
    """short_id returns exactly SHORT_ID_LEN characters."""
    session = Session(id="abcd1234-5678-90ab-cdef-1234567890ab", stage="ore", created_at=1000)
    assert len(session.short_id) == SHORT_ID_LEN
    assert session.short_id == "abcd1234"


def test_short_id_is_prefix():
    """short_id is the first segment of the full ID."""
    session = Session(id="deadbeef-1234-5678-90ab-cdef12345678", stage="ore", created_at=1000)
    assert session.id.startswith(session.short_id)
    assert session.short_id == "deadbeef"


# Tests for find_by_short_id


def test_find_by_short_id_exact():
    """find_by_short_id matches full ID."""
    sessions = [
        Session(id="aaaa1111-0000-0000-0000-000000000000", stage="ore", created_at=1000),
        Session(id="bbbb2222-0000-0000-0000-000000000000", stage="ore", created_at=2000),
    ]
    result = find_by_short_id(sessions, "aaaa1111-0000-0000-0000-000000000000")
    assert result is sessions[0]


def test_find_by_short_id_prefix():
    """find_by_short_id matches unique prefix."""
    sessions = [
        Session(id="aaaa1111-0000-0000-0000-000000000000", stage="ore", created_at=1000),
        Session(id="bbbb2222-0000-0000-0000-000000000000", stage="ore", created_at=2000),
    ]
    result = find_by_short_id(sessions, "aaaa")
    assert result is sessions[0]


def test_find_by_short_id_short_id():
    """find_by_short_id matches 8-char short_id."""
    sessions = [
        Session(id="aaaa1111-0000-0000-0000-000000000000", stage="ore", created_at=1000),
        Session(id="bbbb2222-0000-0000-0000-000000000000", stage="ore", created_at=2000),
    ]
    result = find_by_short_id(sessions, "bbbb2222")
    assert result is sessions[1]


def test_find_by_short_id_ambiguous():
    """find_by_short_id returns None for ambiguous prefix."""
    sessions = [
        Session(id="aaaa1111-0000-0000-0000-000000000000", stage="ore", created_at=1000),
        Session(id="aaaa2222-0000-0000-0000-000000000000", stage="ore", created_at=2000),
    ]
    result = find_by_short_id(sessions, "aaaa")
    assert result is None


def test_find_by_short_id_not_found():
    """find_by_short_id returns None when no match."""
    sessions = [
        Session(id="aaaa1111-0000-0000-0000-000000000000", stage="ore", created_at=1000),
    ]
    result = find_by_short_id(sessions, "xxxx")
    assert result is None


def test_find_by_short_id_empty():
    """find_by_short_id returns None for empty list."""
    result = find_by_short_id([], "aaaa")
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


def test_session_updated_at_default():
    """Session with updated_at=0 uses created_at as effective value."""
    session = Session(id="test", stage="ore", created_at=1000, updated_at=0)
    assert session.effective_updated_at == 1000


def test_session_updated_at_set():
    """Session with updated_at set uses that value."""
    session = Session(id="test", stage="ore", created_at=1000, updated_at=2000)
    assert session.effective_updated_at == 2000


def test_session_touch():
    """touch() updates the updated_at timestamp."""
    session = Session(id="test", stage="ore", created_at=1000, updated_at=1000)
    session.touch()
    assert session.updated_at > 1000


def test_session_to_dict_includes_updated_at():
    """to_dict includes updated_at field."""
    session = Session(id="test", stage="ore", created_at=1000, updated_at=2000)
    data = session.to_dict()
    assert data["updated_at"] == 2000


def test_update_session_stage_touches(temp_config):
    """update_session_stage updates the timestamp."""
    sessions_list = [Session(id="test-id", stage="ore", created_at=1000, updated_at=1000)]
    save_sessions(sessions_list)

    updated = update_session_stage(sessions_list, "test-id", "processing")

    assert updated is not None
    assert updated.updated_at > 1000


def test_update_session_state(temp_config):
    """update_session_state changes state and message, touches timestamp."""
    sessions_list = [
        Session(id="test-id", stage="ore", created_at=1000, updated_at=1000, state="idle")
    ]
    save_sessions(sessions_list)

    updated = update_session_state(sessions_list, "test-id", "running", "Claude running")

    assert updated is not None
    assert updated.state == "running"
    assert updated.message == "Claude running"
    assert updated.updated_at > 1000

    # Verify persistence
    loaded = load_sessions()
    assert loaded[0].state == "running"
    assert loaded[0].message == "Claude running"


def test_update_session_state_not_found(temp_config):
    """update_session_state returns None for unknown session."""
    sessions_list = []

    result = update_session_state(sessions_list, "nonexistent", "running", "Test")

    assert result is None
