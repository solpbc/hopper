"""Tests for session management."""

import json
import uuid

import pytest

from hopper import sessions
from hopper.sessions import (
    SHORT_ID_LEN,
    Session,
    archive_session,
    create_session,
    find_by_short_id,
    get_session_dir,
    load_sessions,
    save_sessions,
    update_session_stage,
)


@pytest.fixture
def temp_config(tmp_path, monkeypatch):
    """Set up temporary paths for session files."""
    monkeypatch.setattr(sessions, "SESSIONS_FILE", tmp_path / "sessions.jsonl")
    monkeypatch.setattr(sessions, "ARCHIVED_FILE", tmp_path / "archived.jsonl")
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "sessions")
    return tmp_path


def test_session_to_dict_and_from_dict():
    """Test Session serialization roundtrip."""
    session = Session(id="abc-123", stage="ore", created_at=1234567890)
    data = session.to_dict()
    restored = Session.from_dict(data)

    assert restored.id == session.id
    assert restored.stage == session.stage
    assert restored.created_at == session.created_at


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
    session = create_session(sessions_list)

    # Verify UUID format
    uuid.UUID(session.id)  # Raises if invalid

    assert session.stage == "ore"
    assert session.created_at > 0
    assert len(sessions_list) == 1
    assert sessions_list[0] is session

    # Verify directory was created
    assert get_session_dir(session.id).exists()

    # Verify persisted to file
    loaded = load_sessions()
    assert len(loaded) == 1
    assert loaded[0].id == session.id


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
