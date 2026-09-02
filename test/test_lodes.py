# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for lode management."""

import json
import os
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hopper import config
from hopper.git import create_worktree
from hopper.lodes import (
    _LAUNCH_ID_NAMESPACE,
    ID_ALPHABET,
    ID_LEN,
    OOM_KILLED_STATUS,
    PANE_LIVENESS_NOT_PROBED,
    PARK_LIVENESS_UNVERIFIED_SUFFIX,
    PARK_PANE_GONE_STATUS,
    RUNNER_EXIT_UNVERIFIED_STATUS,
    STATUS_NEW,
    STATUS_TEARDOWN,
    archive_lode,
    archive_lode_for_action,
    compute_runtime_ms,
    create_lode,
    current_time_ms,
    find_lode_by_prefix,
    find_lodes_by_prefix,
    format_age,
    format_duration_ms,
    format_park_status,
    format_terminal_failure_status,
    format_uptime,
    get_lode_dir,
    get_worktree_dir,
    is_terminal_failure_kind,
    load_archived_lodes,
    load_lodes,
    lode_coder,
    lode_driver,
    lode_icon,
    lode_stage_session,
    lode_status_for_display,
    lode_with_status_annotations,
    make_lode_stage_sessions,
    project_lode_claude_state,
    reset_lode_claude_stage,
    resolve_worktree_path,
    save_archived_lodes,
    save_lodes,
    set_lode_claude_started,
    set_lode_driver,
    touch,
    unarchive_lode,
    update_lode_branch,
    update_lode_coder_session,
    update_lode_codex_thread,
    update_lode_stage,
    update_lode_state,
    update_lode_title,
    update_lode_worktree_path,
    validate_lode_coder_data,
    validate_lode_driver_data,
)
from hopper.tmux import Liveness


def _durable_lode(lode_id: str = "testid11") -> dict:
    """Return a complete canonical lode record for durable lifecycle tests."""
    lode = {
        "id": lode_id,
        "stage": "mill",
        "created_at": 1000,
        "updated_at": 1000,
        "state": "running",
        "driver": "claude",
        "stage_sessions": make_lode_stage_sessions(
            lode_id,
            {
                "mill": "00000000-0000-0000-0000-000000000011",
                "refine": "00000000-0000-0000-0000-000000000012",
                "ship": "00000000-0000-0000-0000-000000000013",
            },
        ),
    }
    project_lode_claude_state(lode)
    return lode


@pytest.mark.parametrize("active", [True, False])
def test_teardown_icon_is_not_disconnected(active):
    assert lode_icon({"stage": "mill", "state": "teardown", "active": active}) == STATUS_TEARDOWN


def test_shipped_teardown_icon_remains_pending():
    assert lode_icon({"stage": "shipped", "state": "teardown", "active": False}) == STATUS_TEARDOWN


def test_reconnecting_icon_is_pending_not_disconnected():
    assert lode_icon({"stage": "refine", "state": "reconnecting", "active": False}) == STATUS_NEW


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


def test_archive_lode_for_action_reconciles_append_before_active_remove(
    tmp_path, monkeypatch, make_lode
):
    monkeypatch.setattr("hopper.lodes.config.hopper_dir", lambda: tmp_path)
    action_id = "a" * 32
    active = [make_lode(id="abcd2345", stage="shipped")]
    archived = []

    with (
        patch("hopper.lodes.save_lodes", side_effect=OSError("crash after append")),
        pytest.raises(OSError, match="crash after append"),
    ):
        archive_lode_for_action(active, archived, "abcd2345", action_id)
    assert len(active) == 1
    assert len(archived) == 1

    second = archive_lode_for_action(active, archived, "abcd2345", action_id)

    assert active == []
    assert len(archived) == 1
    assert second["archive_action_id"] == action_id
    assert load_lodes() == []
    assert len(load_archived_lodes()) == 1


def test_legacy_lode_defaults_to_codex_session():
    """Existing lode bytes remain the Codex provider/session source."""
    lode = {
        "id": "abc12345",
        "stage": "refine",
        "created_at": 1000,
        "codex_thread_id": "codex-uuid-1234",
    }
    assert lode_coder(lode) == ("codex", "codex-uuid-1234")


def test_lode_coder_session_roundtrip():
    """Coder selection and session survive a JSON roundtrip."""
    lode = {
        "id": "abc12345",
        "stage": "refine",
        "created_at": 1000,
        "updated_at": 1000,
        "state": "running",
        "coder": {"provider": "grok", "session_id": "thread-xyz"},
    }
    restored = json.loads(json.dumps(lode))
    assert restored["coder"] == {"provider": "grok", "session_id": "thread-xyz"}


def test_load_lodes_empty(temp_config):
    """Test loading when no file exists."""
    lodes_list = load_lodes()
    assert lodes_list == []


def test_save_and_load_lodes(temp_config):
    """Test save/load roundtrip."""
    first = _durable_lode("id111111")
    first["state"] = "new"
    second = _durable_lode("id222222")
    second.update({"stage": "refine", "created_at": 2000, "updated_at": 2000, "state": "new"})
    lodes_list = [first, second]
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
    assert lode["originating_extro_sid"] is None
    assert lode["branch"] == ""
    assert lode["worktree_path"] is None
    assert lode["worktree_reap"] is None
    assert lode["last_progress_at"] is None
    assert lode["last_progress_summary"] == ""
    assert lode["last_pane_activity_at"] is None
    assert lode["pane_title_observation"] is None
    assert lode["run_generation"] is None
    assert lode["oom_scope"] is None
    assert lode["failure_kind"] is None
    assert lode["shipped_at"] is None
    assert lode["errored_at"] is None
    assert lode["spawn_disposition"] is None
    assert lode["archive_action_id"] is None
    assert lode["codex_thread_id"] is None
    assert "coder" not in lode
    assert lode_coder(lode) == ("codex", None)
    assert lode["created_at"] > 0
    assert len(lodes_list) == 1
    assert lodes_list[0] is lode

    assert lode_driver(lode) == "claude"

    # Verify canonical per-stage sessions and their retained compatibility projection.
    for stage in ("mill", "refine", "ship"):
        session = lode_stage_session(lode, stage)
        uuid.UUID(session["provider_session_id"])
        uuid.UUID(session["launch_id"])
        assert session["started"] is False
        assert session["transcript_path"] is None
        assert session["start_attempt"] is None

    # Verify directory was created
    assert get_lode_dir(lode["id"]).exists()

    # Verify persisted to file
    loaded = load_lodes()
    assert len(loaded) == 1
    assert loaded[0]["id"] == lode["id"]
    assert loaded[0]["project"] == "test-project"


def test_create_lode_can_select_grok(temp_config):
    lode = create_lode([], "test-project", coder_provider="grok")

    assert lode["coder"] == {"provider": "grok", "session_id": None}
    assert lode["codex_thread_id"] is None
    assert lode_coder(lode) == ("grok", None)


@pytest.mark.parametrize("interactive_driver", ["codex", "grok"])
def test_internal_interactive_drivers_keep_stage_and_coder_sessions_independent(
    temp_config, interactive_driver
):
    lode = create_lode([], "test-project", coder_provider="grok", driver=interactive_driver)

    assert lode_driver(lode) == interactive_driver
    assert lode_coder(lode) == ("grok", None)
    for stage in ("mill", "refine", "ship"):
        assert lode_stage_session(lode, stage)["provider_session_id"]


def test_lode_driver_cannot_change_after_creation(temp_config):
    lode = create_lode([], "test-project")
    persisted = (temp_config / "active.jsonl").read_bytes()

    with pytest.raises(ValueError, match="immutable"):
        set_lode_driver(lode, "grok")

    assert lode_driver(lode) == "claude"
    assert (temp_config / "active.jsonl").read_bytes() == persisted


def test_validate_lode_driver_data_rejects_invalid_driver():
    lode = _durable_lode("badlode1")
    lode["driver"] = "other"
    with pytest.raises(ValueError, match="invalid driver"):
        validate_lode_driver_data([lode], "active")


def test_create_lode_rejects_unknown_coder_before_writing(temp_config):
    lodes = []

    with pytest.raises(ValueError, match="codex, grok, antigravity"):
        create_lode(lodes, "test-project", coder_provider="other")

    assert lodes == []
    assert not (temp_config / "active.jsonl").exists()


def test_validate_lode_coder_data_accepts_legacy_codex_records():
    validate_lode_coder_data([{"id": "oldlode1", "codex_thread_id": None}], "active")


def test_validate_lode_coder_data_rejects_malformed_optional_provider():
    with pytest.raises(ValueError, match="invalid coder data"):
        validate_lode_coder_data([{"id": "badlode1", "coder": "grok"}], "active")


def test_validate_lode_coder_data_rejects_reencoding_codex():
    with pytest.raises(ValueError, match="must use codex_thread_id"):
        validate_lode_coder_data(
            [{"id": "badlode1", "coder": {"provider": "codex", "session_id": None}}],
            "active",
        )


def test_terminal_failure_statuses_have_one_formatter():
    assert format_terminal_failure_status("oom", "abc12345") == OOM_KILLED_STATUS.replace(
        "{lode_id}", "abc12345"
    )
    assert format_terminal_failure_status(
        "runner_exit_unverified", "abc12345"
    ) == RUNNER_EXIT_UNVERIFIED_STATUS.replace("{lode_id}", "abc12345")
    assert is_terminal_failure_kind("oom") is True
    assert is_terminal_failure_kind("runner_exit_unverified") is True
    assert is_terminal_failure_kind("ordinary_error") is False


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
    lode = _durable_lode()
    lode["state"] = "new"
    lodes_list = [lode]
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


@pytest.mark.parametrize("interactive_driver", ["claude", "codex", "grok"])
def test_archive_lode(temp_config, interactive_driver):
    """Test archiving a lode."""
    keep = _durable_lode("keepid11")
    keep["state"] = "new"
    archived_lode = _durable_lode("archivid")
    archived_lode.update(
        {
            "stage": "refine",
            "created_at": 2000,
            "updated_at": 2000,
            "state": "new",
            "driver": interactive_driver,
        }
    )
    lodes_list = [keep, archived_lode]
    save_lodes(lodes_list)

    archived = archive_lode(lodes_list, "archivid")

    assert archived is not None
    assert archived["id"] == "archivid"
    assert lode_driver(archived) == interactive_driver
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
    first = _durable_lode("id111111")
    first["state"] = "new"
    second = _durable_lode("id222222")
    second.update({"created_at": 2000, "updated_at": 2000, "state": "new"})
    lodes_list = [first, second]
    save_lodes(lodes_list)

    archive_lode(lodes_list, "id111111")
    archive_lode(lodes_list, "id222222")

    archived_file = temp_config / "archived.jsonl"
    with open(archived_file) as f:
        lines = f.readlines()
    assert len(lines) == 2


def test_archive_lode_sets_archived_at(temp_config):
    """Test that archiving a lode sets archived_at timestamp."""
    lode = _durable_lode("archivid")
    lode["state"] = "new"
    lodes_list = [lode]
    save_lodes(lodes_list)

    archived = archive_lode(lodes_list, "archivid")

    assert archived is not None
    assert "archived_at" in archived
    assert isinstance(archived["archived_at"], int)
    assert archived["archived_at"] > 0

    # Verify it's persisted in the archived file
    archived_file = temp_config / "archived.jsonl"
    data = json.loads(archived_file.read_text().strip())
    assert "archived_at" in data


@pytest.mark.parametrize("interactive_driver", ["claude", "codex", "grok"])
def test_unarchive_lode(temp_config, interactive_driver):
    """Test unarchiving a lode moves it from archived to active."""
    restored_lode = _durable_lode("restorId")
    restored_lode.update({"state": "new", "archived_at": 5000, "driver": interactive_driver})
    archived_lodes = [restored_lode]
    active_lode = _durable_lode("activeid")
    active_lode.update({"stage": "refine", "created_at": 2000, "updated_at": 2000, "state": "new"})
    active_lodes = [active_lode]
    save_archived_lodes(archived_lodes)
    save_lodes(active_lodes)

    restored = unarchive_lode(archived_lodes, active_lodes, "restorId")

    assert restored is not None
    assert restored["id"] == "restorId"
    assert "archived_at" not in restored
    assert len(archived_lodes) == 0
    assert len(active_lodes) == 2
    assert active_lodes[1]["id"] == "restorId"
    assert lode_driver(restored) == interactive_driver

    # Verify persistence
    loaded_active = load_lodes()
    assert len(loaded_active) == 2
    loaded_archived = load_archived_lodes()
    assert len(loaded_archived) == 0


def test_originating_extro_sid_survives_archive_and_unarchive(temp_config, make_lode):
    active_lodes = [make_lode(id="extro111", originating_extro_sid="extro-session-1")]
    save_lodes(active_lodes)

    archived = archive_lode(active_lodes, "extro111")

    assert archived["originating_extro_sid"] == "extro-session-1"
    persisted_archived = load_archived_lodes()
    assert persisted_archived[0]["originating_extro_sid"] == "extro-session-1"

    restored = unarchive_lode(persisted_archived, active_lodes, "extro111")

    assert restored["originating_extro_sid"] == "extro-session-1"
    assert load_lodes()[0]["originating_extro_sid"] == "extro-session-1"


def test_originating_extro_sid_survives_action_archive(temp_config, make_lode):
    active_lodes = [
        make_lode(
            id="extro222",
            stage="shipped",
            originating_extro_sid="extro-session-2",
        )
    ]
    archived_lodes = []

    archived = archive_lode_for_action(
        active_lodes,
        archived_lodes,
        "extro222",
        "a" * 32,
    )

    assert archived["originating_extro_sid"] == "extro-session-2"
    assert archived_lodes[0]["originating_extro_sid"] == "extro-session-2"
    assert load_archived_lodes()[0]["originating_extro_sid"] == "extro-session-2"


def test_unarchive_lode_not_found(temp_config):
    """Test unarchiving non-existent lode."""
    archived_lodes = []
    active_lodes = []
    result = unarchive_lode(archived_lodes, active_lodes, "nonexistent")
    assert result is None


@pytest.mark.parametrize(
    ("saver", "filename"),
    [(save_lodes, "active.jsonl"), (save_archived_lodes, "archived.jsonl")],
)
def test_atomic_save(temp_config, saver, filename):
    """Successful active and archived snapshots leave no temporary file."""
    lodes_list = [
        {"id": "testid11", "stage": "mill", "created_at": 1000, "updated_at": 1000, "state": "new"}
    ]
    saver(lodes_list)

    assert not list(temp_config.glob(f"{filename}.*.tmp"))
    assert (temp_config / filename).read_text() == json.dumps(lodes_list[0]) + "\n"


@pytest.mark.parametrize(
    ("saver", "filename"),
    [(save_lodes, "active.jsonl"), (save_archived_lodes, "archived.jsonl")],
)
@pytest.mark.parametrize("phase", ["write", "flush", "close"])
def test_atomic_save_cleans_temp_after_stream_failure(temp_config, saver, filename, phase):
    """Write, flush, and close failures remove only the writer's temp and re-raise."""
    temp_path = temp_config / f"{filename}.fixed.tmp"
    temp_path.write_text("partial")
    stream = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = stream
    error = OSError(f"{phase} failed")
    if phase == "write":
        stream.write.side_effect = error
    elif phase == "flush":
        stream.flush.side_effect = error
    else:
        context.__exit__.side_effect = error

    with (
        patch("hopper.lodes.uuid.uuid4", return_value=SimpleNamespace(hex="fixed")),
        patch("builtins.open", return_value=context),
    ):
        with pytest.raises(OSError) as exc_info:
            saver([{"id": "payload"}])

    assert exc_info.value is error
    assert not temp_path.exists()


@pytest.mark.parametrize(
    ("saver", "filename"),
    [(save_lodes, "active.jsonl"), (save_archived_lodes, "archived.jsonl")],
)
def test_atomic_save_cleans_temp_after_replace_failure(temp_config, saver, filename):
    """A failed replacement is attempted once, cleaned up, and re-raised."""
    error = OSError("replace failed")
    with (
        patch("hopper.lodes.uuid.uuid4", return_value=SimpleNamespace(hex="fixed")),
        patch("hopper.lodes.os.replace", side_effect=error) as mock_replace,
    ):
        with pytest.raises(OSError) as exc_info:
            saver([{"id": "payload"}])

    assert exc_info.value is error
    mock_replace.assert_called_once()
    assert not (temp_config / f"{filename}.fixed.tmp").exists()


def test_concurrent_save_lodes_processes_use_independent_temps(tmp_path):
    """Regression: blocked-at-replace writers fail on unpatched main's shared temp."""
    child_code = r"""
import json
import socket
import sys

import hopper.lodes as lodes

host, port, payload_json = sys.argv[1:]
control = socket.create_connection((host, int(port)), timeout=10)
control_file = control.makefile("rwb", buffering=0)
control_file.write(b"READY\n")
assert control_file.readline() == b"GO\n"
original_replace = lodes.os.replace

def synchronized_replace(source, target):
    control_file.write(b"REPLACE_READY\n")
    assert control_file.readline() == b"RELEASE\n"
    original_replace(source, target)

lodes.os.replace = synchronized_replace
lodes.save_lodes(json.loads(payload_json))
control_file.write(b"DONE\n")
"""
    xdg_home = tmp_path / "xdg"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    listener.settimeout(10)
    host, port = listener.getsockname()
    payloads = [
        [{"id": "writer-a", "value": "A" * 4096}],
        [{"id": "writer-b", "value": "B" * 4096}],
    ]
    env = os.environ.copy()
    env["XDG_DATA_HOME"] = str(xdg_home)
    repo_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in [repo_root, env.get("PYTHONPATH", "")] if part
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", child_code, host, str(port), json.dumps(payload)],
            cwd=repo_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for payload in payloads
    ]
    controls = []
    files = []
    try:
        for _ in processes:
            connection, _ = listener.accept()
            connection.settimeout(10)
            controls.append(connection)
            files.append(connection.makefile("rwb", buffering=0))

        assert [file.readline() for file in files] == [b"READY\n", b"READY\n"]
        for file in files:
            file.write(b"GO\n")
        assert [file.readline() for file in files] == [
            b"REPLACE_READY\n",
            b"REPLACE_READY\n",
        ]
        for file in files:
            file.write(b"RELEASE\n")
        assert [file.readline() for file in files] == [b"DONE\n", b"DONE\n"]

        results = [process.communicate(timeout=10) for process in processes]
        assert [process.returncode for process in processes] == [0, 0], results
    finally:
        for file in files:
            file.close()
        for connection in controls:
            connection.close()
        listener.close()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()

    target = xdg_home / "hopper" / "active.jsonl"
    expected_payloads = {(json.dumps(payload[0]) + "\n").encode("utf-8") for payload in payloads}
    assert target.read_bytes() in expected_payloads
    assert not list((xdg_home / "hopper").glob("active.jsonl*.tmp"))


def test_get_lode_dir(temp_config):
    """Test lode directory path."""
    path = get_lode_dir("my-lode-id")
    assert path == temp_config / "lodes" / "my-lode-id"


def test_worktree_dir_uses_whitespace_free_root_without_moving_durable_state(tmp_path, monkeypatch):
    """Worktrees avoid spaced app-data paths while durable lode state stays there."""
    lode_id = "abc12345"
    app_data_path = tmp_path / "App Support" / "hopper"
    worktree_path = tmp_path / "worktrees"
    monkeypatch.setattr(config, "hopper_dir", lambda: app_data_path)
    monkeypatch.setattr(config, "worktree_root", lambda: worktree_path)

    assert " " not in str(get_worktree_dir(lode_id))
    assert get_lode_dir(lode_id).is_relative_to(app_data_path)
    assert (get_lode_dir(lode_id) / "diff.txt").is_relative_to(app_data_path)


def test_worktree_dir_resolves_legacy_worktree_in_place(tmp_path):
    """Existing legacy worktrees remain resolved until removed."""
    lode_id = "abc12345"
    legacy_path = get_lode_dir(lode_id) / "worktree"
    legacy_path.mkdir(parents=True)

    assert get_worktree_dir(lode_id) == legacy_path

    legacy_path.rmdir()
    assert get_worktree_dir(lode_id) == config.worktree_root() / lode_id


def test_resolve_worktree_path_uses_recorded_provenance_when_cleaned(make_lode):
    managed = config.worktree_root() / "testid11"
    lode = make_lode(worktree_path=str(managed))

    assert resolve_worktree_path(lode) == {
        "path": managed,
        "basis": "recorded",
        "reason": None,
    }


def test_resolve_worktree_path_discovers_one_managed_candidate(make_lode):
    managed = config.worktree_root() / "testid11"
    managed.mkdir(parents=True)

    assert resolve_worktree_path(make_lode()) == {
        "path": managed,
        "basis": "existing",
        "reason": None,
    }


def test_resolve_worktree_path_keeps_legacy_usable_without_provenance(make_lode):
    legacy = get_lode_dir("testid11") / "worktree"
    legacy.mkdir(parents=True)

    assert resolve_worktree_path(make_lode()) == {
        "path": legacy,
        "basis": "unavailable",
        "reason": "legacy_outside_root",
    }


def test_resolve_worktree_path_refuses_ambiguous_candidates(make_lode):
    (get_lode_dir("testid11") / "worktree").mkdir(parents=True)
    (config.worktree_root() / "testid11").mkdir(parents=True)

    assert resolve_worktree_path(make_lode()) == {
        "path": None,
        "basis": "unavailable",
        "reason": "ambiguous_candidates",
    }


def test_resolve_worktree_path_does_not_guess_cleaned_legacy_candidate(make_lode):
    assert resolve_worktree_path(make_lode()) == {
        "path": None,
        "basis": "unavailable",
        "reason": "no_existing_candidate",
    }


@pytest.mark.parametrize(
    ("recorded", "reason"),
    [
        ("relative/worktree", "recorded_not_absolute"),
        ("/outside/testid11", "recorded_outside_root"),
    ],
)
def test_resolve_worktree_path_rejects_invalid_recorded_provenance(make_lode, recorded, reason):
    assert resolve_worktree_path(make_lode(worktree_path=recorded)) == {
        "path": None,
        "basis": "unavailable",
        "reason": reason,
    }


def test_update_and_archive_preserve_worktree_path(make_lode):
    lodes = [make_lode()]
    managed = config.worktree_root() / "testid11"

    updated = update_lode_worktree_path(lodes, "testid11", str(managed))
    archived = archive_lode(lodes, "testid11")

    assert updated is not None
    assert archived is not None
    assert archived["worktree_path"] == str(managed)
    assert load_archived_lodes()[0]["worktree_path"] == str(managed)


def test_archive_does_not_eagerly_add_worktree_provenance():
    lodes = [{"id": "testid11", "stage": "mill", "created_at": 1}]

    archived = archive_lode(lodes, "testid11")

    assert archived is not None
    assert "worktree_path" not in archived


def test_create_worktree_at_resolved_whitespace_free_path(tmp_path, monkeypatch):
    """A real git checkout can be created at the resolved new-root path."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")

    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    def run_git(*args):
        return subprocess.run(
            ["git", *args],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    run_git("init", "-b", "main")
    run_git("config", "user.email", "test@example.com")
    run_git("config", "user.name", "Test User")
    (repo_path / "README.md").write_text("init\n")
    run_git("add", ".")
    run_git("commit", "-m", "init")

    lode_id = "abc12345"
    monkeypatch.setattr(config, "worktree_root", lambda: tmp_path / "worktrees")
    worktree_path = get_worktree_dir(lode_id)
    worktree_path.parent.mkdir(parents=True)

    created, error = create_worktree(str(repo_path), worktree_path, f"hopper-{lode_id}")
    assert created is True
    assert error is None
    assert worktree_path.is_dir()
    assert " " not in str(worktree_path)


def test_find_lode_prefix_helpers():
    """Prefix lookup helpers return unique, ambiguous, and empty results correctly."""
    lodes = [
        {"id": "abc12345"},
        {"id": "abc99999"},
        {"id": "def11111"},
    ]

    matches = find_lodes_by_prefix(lodes, "abc")
    assert len(matches) == 2
    assert [lode["id"] for lode in matches] == ["abc12345", "abc99999"]

    unique = find_lode_by_prefix(lodes, "def")
    assert unique is not None
    assert unique["id"] == "def11111"

    assert find_lode_by_prefix(lodes, "abc") is None
    assert find_lode_by_prefix(lodes, "zzz") is None


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


def test_touch():
    """touch() updates the updated_at timestamp."""
    lode = {"id": "testid11", "stage": "mill", "created_at": 1000, "updated_at": 1000}
    touch(lode)
    assert lode["updated_at"] > 1000


def test_update_lode_stage_touches(temp_config):
    """update_lode_stage updates the timestamp."""
    lode = _durable_lode()
    lode["state"] = "new"
    lodes_list = [lode]
    save_lodes(lodes_list)

    updated = update_lode_stage(lodes_list, "testid11", "refine")

    assert updated is not None
    assert updated["updated_at"] > 1000


def test_update_lode_state(temp_config):
    """update_lode_state changes state and message, touches timestamp."""
    lode = _durable_lode()
    lode.update({"state": "gated", "spawn_disposition": "unknown"})
    lodes_list = [lode]
    save_lodes(lodes_list)

    updated = update_lode_state(lodes_list, "testid11", "running", "Claude running")

    assert updated is not None
    assert updated["state"] == "running"
    assert updated["status"] == "Claude running"
    assert updated["spawn_disposition"] is None
    assert updated["updated_at"] > 1000

    # Verify persistence
    loaded = load_lodes()
    assert loaded[0]["state"] == "running"
    assert loaded[0]["status"] == "Claude running"
    assert loaded[0]["spawn_disposition"] is None


def test_update_lode_state_not_found(temp_config):
    """update_lode_state returns None for unknown lode."""
    lodes_list = []

    result = update_lode_state(lodes_list, "nonexistent", "running", "Test")

    assert result is None


def test_update_lode_title(temp_config):
    """update_lode_title changes title and touches timestamp."""
    lode = _durable_lode()
    lode["state"] = "new"
    lodes_list = [lode]
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
    lode = _durable_lode()
    lode.update({"state": "new", "branch": ""})
    lodes_list = [lode]
    save_lodes(lodes_list)

    updated = update_lode_branch(lodes_list, "testid11", "hopper-testid11-auth-flow")

    assert updated is not None
    assert updated["branch"] == "hopper-testid11-auth-flow"
    assert updated["updated_at"] > 1000

    loaded = load_lodes()
    assert loaded[0]["branch"] == "hopper-testid11-auth-flow"


def test_grok_coder_session_roundtrips_and_codex_path_is_refused(temp_config, monkeypatch):
    timestamps = iter((1000, 2000))
    monkeypatch.setattr("hopper.lodes.current_time_ms", lambda: next(timestamps))
    lodes_list = []
    lode = create_lode(lodes_list, "test-project", coder_provider="grok")
    persisted_before = (temp_config / "active.jsonl").read_bytes()
    memory_before = json.loads(json.dumps(lodes_list))

    assert update_lode_coder_session(lodes_list, lode["id"], "codex", "wrong-thread") is None
    assert lodes_list == memory_before
    assert (temp_config / "active.jsonl").read_bytes() == persisted_before

    updated = update_lode_coder_session(lodes_list, lode["id"], "grok", "thread-123")

    assert updated is lode
    assert updated["coder"]["session_id"] == "thread-123"
    assert updated["codex_thread_id"] is None
    assert updated["updated_at"] == 2000

    loaded = load_lodes()
    assert loaded[0]["coder"]["session_id"] == "thread-123"
    assert loaded[0]["codex_thread_id"] is None
    assert loaded[0]["updated_at"] == 2000


def test_update_lode_coder_session_not_found(temp_config):
    """update_lode_coder_session returns None for unknown lode."""
    result = update_lode_coder_session([], "nonexistent", "grok", "thread-123")
    assert result is None


def test_update_lode_codex_thread_preserves_legacy_field(temp_config):
    lodes_list = [{"id": "testid11", "updated_at": 1000, "codex_thread_id": None}]

    updated = update_lode_codex_thread(lodes_list, "testid11", "thread-123")

    assert updated["codex_thread_id"] == "thread-123"
    assert "coder" not in updated


def test_update_lode_codex_thread_refuses_grok_without_mutation(temp_config):
    lodes_list = []
    lode = create_lode(lodes_list, "test-project", coder_provider="grok")
    persisted_before = (temp_config / "active.jsonl").read_bytes()
    memory_before = json.loads(json.dumps(lodes_list))

    with pytest.raises(ValueError, match="only be stored on Codex lodes"):
        update_lode_codex_thread(lodes_list, lode["id"], "grok-session")

    assert lodes_list == memory_before
    assert (temp_config / "active.jsonl").read_bytes() == persisted_before


@pytest.mark.parametrize("session_id", [123, ""], ids=["non-string", "empty"])
def test_update_lode_coder_session_rejects_invalid_session_without_persisting(
    temp_config, session_id
):
    lodes_list = []
    lode = create_lode(lodes_list, "test-project", coder_provider="grok")
    persisted_before = (temp_config / "active.jsonl").read_bytes()
    memory_before = json.loads(json.dumps(lodes_list))

    with pytest.raises(ValueError, match="session_id must be a non-empty string"):
        update_lode_coder_session(lodes_list, lode["id"], "grok", session_id)

    assert lodes_list == memory_before
    assert (temp_config / "active.jsonl").read_bytes() == persisted_before


def test_generic_coder_session_path_does_not_reencode_codex(temp_config):
    lodes_list = [{"id": "testid11", "updated_at": 1000, "codex_thread_id": None}]

    assert update_lode_coder_session(lodes_list, "testid11", "codex", "thread-123") is None
    assert lodes_list[0]["codex_thread_id"] is None
    assert "coder" not in lodes_list[0]


def test_update_lode_coder_session_rejects_provider_mismatch(temp_config):
    lodes_list = [
        {
            "id": "testid11",
            "updated_at": 1000,
            "codex_thread_id": None,
        }
    ]

    assert update_lode_coder_session(lodes_list, "testid11", "grok", "session") is None
    assert lodes_list[0]["codex_thread_id"] is None


def test_set_lode_claude_started(temp_config):
    """set_lode_claude_started marks stage as started and touches timestamp."""
    lodes_list = [_durable_lode()]
    save_lodes(lodes_list)
    before_other_stages = {
        stage: (
            lode_stage_session(lodes_list[0], stage)["provider_session_id"],
            lode_stage_session(lodes_list[0], stage)["launch_id"],
            lode_stage_session(lodes_list[0], stage)["started"],
            dict(lodes_list[0]["claude"][stage]),
        )
        for stage in ("refine", "ship")
    }

    with patch("hopper.lodes.save_lodes", wraps=save_lodes) as save:
        updated = set_lode_claude_started(lodes_list, "testid11", "mill")

    assert updated is not None
    assert lode_stage_session(updated, "mill")["started"] is True
    assert updated["updated_at"] > 1000
    save.assert_called_once_with(lodes_list)
    written = save.call_args.args[0][0]
    written_session = lode_stage_session(written, "mill")
    written_legacy = written["claude"]["mill"]
    assert written_session["started"] is True
    assert written_legacy["started"] is True
    assert written_legacy["session_id"] == written_session["provider_session_id"]

    loaded = load_lodes()
    loaded_session = lode_stage_session(loaded[0], "mill")
    loaded_legacy = loaded[0]["claude"]["mill"]
    assert loaded_session["started"] is True
    assert loaded_legacy["started"] is True
    assert loaded_legacy["session_id"] == loaded_session["provider_session_id"]
    for stage in ("refine", "ship"):
        session = lode_stage_session(loaded[0], stage)
        legacy = loaded[0]["claude"][stage]
        assert (
            session["provider_session_id"],
            session["launch_id"],
            session["started"],
            legacy,
        ) == before_other_stages[stage]


def test_set_lode_claude_started_not_found(temp_config):
    """set_lode_claude_started returns None for unknown lode."""
    result = set_lode_claude_started([], "nonexistent", "mill")
    assert result is None


def test_stage_start_persistence_failure_keeps_previous_snapshot_loadable(temp_config):
    lodes_list = []
    lode = create_lode(lodes_list, "test-project")
    persisted = (temp_config / "active.jsonl").read_bytes()

    with (
        patch("hopper.lodes._write_jsonl_atomic", side_effect=OSError("injected write failure")),
        pytest.raises(OSError, match="injected write failure"),
    ):
        set_lode_claude_started(lodes_list, lode["id"], "mill")

    assert (temp_config / "active.jsonl").read_bytes() == persisted
    restored = load_lodes()[0]
    restored_session = lode_stage_session(restored, "mill")
    restored_legacy = restored["claude"]["mill"]
    assert restored_session["started"] is False
    assert restored_legacy["started"] is False
    assert restored_legacy["session_id"] == restored_session["provider_session_id"]


def test_set_lode_claude_started_invalid_stage(temp_config):
    """set_lode_claude_started returns None for unknown claude stage."""
    lodes_list = [_durable_lode()]
    save_lodes(lodes_list)

    result = set_lode_claude_started(lodes_list, "testid11", "other")

    assert result is None
    assert lode_stage_session(lodes_list[0], "mill")["started"] is False


@pytest.mark.parametrize("interactive_driver", ["claude", "codex", "grok"])
def test_reset_lode_claude_stage(temp_config, interactive_driver):
    """reset_lode_claude_stage resets session, start, and heartbeat fields."""
    lode = _durable_lode()
    lode["driver"] = interactive_driver
    lode_stage_session(lode, "mill")["started"] = True
    project_lode_claude_state(lode)
    lode.update(
        {
            "last_progress_at": 900,
            "last_progress_summary": "codex running",
            "last_pane_activity_at": 800,
            "pane_title_observation": {"title": "⠐ Working", "observed_at": 700},
        }
    )
    lodes_list = [lode]
    old_session = lode_stage_session(lode, "mill")["provider_session_id"]
    old_launch = lode_stage_session(lode, "mill")["launch_id"]
    unaffected_stages = {
        stage: (
            lode_stage_session(lode, stage)["provider_session_id"],
            lode_stage_session(lode, stage)["launch_id"],
            lode_stage_session(lode, stage)["started"],
            dict(lode["claude"][stage]),
        )
        for stage in ("refine", "ship")
    }
    save_lodes(lodes_list)
    new_session_id = "00000000-0000-0000-0000-000000000099"

    with patch("hopper.lodes.save_lodes", wraps=save_lodes) as save:
        updated = reset_lode_claude_stage(
            lodes_list,
            "testid11",
            "mill",
            session_id=new_session_id,
        )

    assert updated is not None
    session = lode_stage_session(updated, "mill")
    assert session["started"] is False
    assert session["provider_session_id"] == new_session_id
    assert session["provider_session_id"] != old_session
    assert session["launch_id"] != old_launch
    assert session["launch_id"] == str(
        uuid.uuid5(_LAUNCH_ID_NAMESPACE, f"testid11:mill:{new_session_id}")
    )
    assert lode_driver(updated) == interactive_driver
    assert updated["last_progress_at"] is None
    assert updated["last_progress_summary"] == ""
    assert updated["last_pane_activity_at"] is None
    assert updated["pane_title_observation"] is None
    uuid.UUID(session["provider_session_id"])
    assert updated["updated_at"] > 1000
    save.assert_called_once_with(lodes_list)
    written = save.call_args.args[0][0]
    written_session = lode_stage_session(written, "mill")
    written_legacy = written["claude"]["mill"]
    assert written_session["started"] is False
    assert written_legacy["started"] is False
    assert written_legacy["session_id"] == new_session_id
    assert written_legacy["session_id"] == written_session["provider_session_id"]

    loaded = load_lodes()
    loaded_session = lode_stage_session(loaded[0], "mill")
    loaded_legacy = loaded[0]["claude"]["mill"]
    assert loaded_session["started"] is False
    assert loaded_session["provider_session_id"] == new_session_id
    assert loaded_legacy["started"] is False
    assert loaded_legacy["session_id"] == new_session_id
    assert loaded_legacy["session_id"] == loaded_session["provider_session_id"]
    assert loaded[0]["last_progress_at"] is None
    assert loaded[0]["last_progress_summary"] == ""
    assert loaded[0]["last_pane_activity_at"] is None
    assert loaded[0]["pane_title_observation"] is None
    for stage in ("refine", "ship"):
        other_session = lode_stage_session(loaded[0], stage)
        other_legacy = loaded[0]["claude"][stage]
        assert (
            other_session["provider_session_id"],
            other_session["launch_id"],
            other_session["started"],
            other_legacy,
        ) == unaffected_stages[stage]


def test_reset_lode_claude_stage_not_found(temp_config):
    """reset_lode_claude_stage returns None for unknown lode."""
    result = reset_lode_claude_stage([], "nonexistent", "mill")
    assert result is None


def test_reset_lode_claude_stage_invalid_stage(temp_config):
    """reset_lode_claude_stage returns None for unknown claude stage."""
    lode = _durable_lode()
    lode_stage_session(lode, "mill")["started"] = True
    project_lode_claude_state(lode)
    lodes_list = [lode]
    old_session = lode_stage_session(lode, "mill")["provider_session_id"]
    save_lodes(lodes_list)

    result = reset_lode_claude_stage(lodes_list, "testid11", "other")

    assert result is None
    assert lode_stage_session(lodes_list[0], "mill")["started"] is True
    assert lode_stage_session(lodes_list[0], "mill")["provider_session_id"] == old_session


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


def test_compute_runtime_ms_no_runs():
    """compute_runtime_ms returns 0 when runs is empty."""
    lode = {"runs": {}}
    assert compute_runtime_ms(lode) == 0


def test_compute_runtime_ms_missing_runs_key():
    """compute_runtime_ms returns 0 when runs key is missing."""
    lode = {}
    assert compute_runtime_ms(lode) == 0


def test_compute_runtime_ms_completed_stage():
    """compute_runtime_ms sums completed stage duration."""
    lode = {"runs": {"mill": {"started_at": 1000, "stopped_at": 6000}}}
    assert compute_runtime_ms(lode) == 5000


def test_compute_runtime_ms_running_stage():
    """compute_runtime_ms uses now for running stage."""
    lode = {"runs": {"refine": {"started_at": 1000}}}
    assert compute_runtime_ms(lode, now=4000) == 3000


def test_compute_runtime_ms_multiple_stages():
    """compute_runtime_ms sums across multiple stages."""
    lode = {
        "runs": {
            "mill": {"started_at": 1000, "stopped_at": 3000},
            "refine": {"started_at": 5000, "stopped_at": 8000},
        }
    }
    assert compute_runtime_ms(lode) == 5000


def test_compute_runtime_ms_restart_clears_previous():
    """Restarting a stage replaces started_at, clearing previous time."""
    lode = {"runs": {"mill": {"started_at": 9000}}}
    assert compute_runtime_ms(lode, now=10000) == 1000


def test_compute_runtime_ms_empty_stage_run():
    """compute_runtime_ms skips stages with no started_at."""
    lode = {"runs": {"mill": {}}}
    assert compute_runtime_ms(lode) == 0


def test_update_lode_state_records_started_at(temp_config):
    """update_lode_state records started_at when state becomes running."""
    lodes_list = [
        {
            "id": "testid11",
            "stage": "mill",
            "created_at": 1000,
            "updated_at": 1000,
            "state": "new",
            "runs": {},
        }
    ]
    save_lodes(lodes_list)
    update_lode_state(lodes_list, "testid11", "running", "Claude running")
    runs = lodes_list[0]["runs"]
    assert "mill" in runs
    assert "started_at" in runs["mill"]
    assert "stopped_at" not in runs["mill"]


def test_update_lode_state_repeated_running_preserves_timer(temp_config):
    """Repeated running updates within one stage preserve its original start."""
    started_at = current_time_ms() - 5000
    lodes_list = [
        {
            "id": "testid11",
            "stage": "refine",
            "created_at": 1000,
            "updated_at": 1000,
            "state": "running",
            "runs": {"refine": {"started_at": started_at}},
        }
    ]
    save_lodes(lodes_list)
    update_lode_state(lodes_list, "testid11", "running", "Codex stage complete")
    assert lodes_list[0]["runs"]["refine"] == {"started_at": started_at}


def test_update_lode_state_records_stopped_at(temp_config):
    """update_lode_state records stopped_at when state becomes ready."""
    now = current_time_ms()
    lodes_list = [
        {
            "id": "testid11",
            "stage": "mill",
            "created_at": 1000,
            "updated_at": 1000,
            "state": "running",
            "runs": {"mill": {"started_at": now - 5000}},
        }
    ]
    save_lodes(lodes_list)
    update_lode_state(lodes_list, "testid11", "ready", "Done")
    runs = lodes_list[0]["runs"]
    assert "stopped_at" in runs["mill"]
    assert runs["mill"]["stopped_at"] >= runs["mill"]["started_at"]


def test_update_lode_state_error_stops_timer(temp_config):
    """update_lode_state records stopped_at when state becomes error."""
    now = current_time_ms()
    lodes_list = [
        {
            "id": "testid11",
            "stage": "refine",
            "created_at": 1000,
            "updated_at": 1000,
            "state": "running",
            "runs": {"refine": {"started_at": now - 3000}},
        }
    ]
    save_lodes(lodes_list)
    update_lode_state(lodes_list, "testid11", "error", "Failed")
    runs = lodes_list[0]["runs"]
    assert "stopped_at" in runs["refine"]


def test_update_lode_state_restart_resets_timer(temp_config):
    """Restarting (running again) replaces started_at and clears stopped_at."""
    lodes_list = [
        {
            "id": "testid11",
            "stage": "mill",
            "created_at": 1000,
            "updated_at": 1000,
            "state": "ready",
            "runs": {"mill": {"started_at": 1000, "stopped_at": 5000}},
        }
    ]
    save_lodes(lodes_list)
    update_lode_state(lodes_list, "testid11", "running", "Claude running")
    runs = lodes_list[0]["runs"]
    assert "started_at" in runs["mill"]
    assert "stopped_at" not in runs["mill"]
    assert runs["mill"]["started_at"] > 5000


def test_update_lode_state_stuck_no_timing_change(temp_config):
    """Stuck state does not affect timing."""
    now = current_time_ms()
    lodes_list = [
        {
            "id": "testid11",
            "stage": "mill",
            "created_at": 1000,
            "updated_at": 1000,
            "state": "running",
            "runs": {"mill": {"started_at": now - 5000}},
        }
    ]
    save_lodes(lodes_list)
    update_lode_state(lodes_list, "testid11", "stuck", "No output")
    runs = lodes_list[0]["runs"]
    assert "stopped_at" not in runs["mill"]
    assert runs["mill"]["started_at"] == now - 5000


def test_parked_alive_status_is_byte_for_byte_unchanged(make_lode):
    reason = "no pane output for 60 min (sustained only by heartbeat/CPU activity)"
    expected = "Parked (idle): no pane output for 60 min (sustained only by heartbeat/CPU activity). The agent is ALIVE and was NOT terminated. Inspect: hop lode peek testid11 | Resume: hop lode nudge testid11 (or hop lode answer testid11 1)"  # noqa: E501
    lode = make_lode(
        state="gated",
        status=format_park_status(reason, "testid11"),
        tmux_pane="%1",
    )

    with patch("hopper.lodes.pane_liveness", return_value=Liveness.ALIVE) as mock_liveness:
        assert lode_status_for_display(lode) == expected

    mock_liveness.assert_called_once_with("%1")


def test_parked_gone_with_branch_preserves_reason(make_lode):
    reason = "quiet: CPU/heartbeat {unchanged} — 61m"
    branch = "hopper-testid11-park-liveness"
    lode = make_lode(
        state="gated",
        status=format_park_status(reason, "testid11"),
        branch=branch,
        tmux_pane="%2",
    )
    expected = PARK_PANE_GONE_STATUS.format(
        reason=reason,
        lode_id="testid11",
        branch=branch,
    )

    with patch("hopper.lodes.pane_liveness", return_value=Liveness.GONE):
        assert lode_status_for_display(lode) == expected


@pytest.mark.parametrize("branch", [None, ""])
def test_parked_gone_without_branch_omits_entire_cherry_clause(make_lode, branch):
    reason = "no pane output"
    lode = make_lode(
        state="gated",
        status=format_park_status(reason, "testid11"),
        branch=branch,
        tmux_pane="%3",
    )
    expected = PARK_PANE_GONE_STATUS.rsplit(" (", 1)[0].format(
        reason=reason,
        lode_id="testid11",
    )

    with patch("hopper.lodes.pane_liveness", return_value=Liveness.GONE):
        output = lode_status_for_display(lode)

    assert output == expected
    assert "check first" not in output
    assert "git cherry" not in output
    assert "{branch}" not in output


def test_parked_without_pane_is_gone_without_probe(make_lode):
    reason = "no pane output"
    lode = make_lode(
        state="gated",
        status=format_park_status(reason, "testid11"),
        branch="hopper-testid11",
        tmux_pane=None,
    )
    expected = PARK_PANE_GONE_STATUS.format(
        reason=reason,
        lode_id="testid11",
        branch="hopper-testid11",
    )

    with patch(
        "hopper.lodes.pane_liveness",
        side_effect=AssertionError("pane_liveness must not be called"),
    ):
        assert lode_status_for_display(lode) == expected


def test_parked_unknown_appends_suffix_without_claiming_gone(make_lode):
    stored = format_park_status("quiet", "testid11")
    lode = make_lode(state="gated", status=stored, tmux_pane="%4")

    with patch("hopper.lodes.pane_liveness", return_value=Liveness.UNKNOWN):
        output = lode_status_for_display(lode)

    assert output == stored + PARK_LIVENESS_UNVERIFIED_SUFFIX
    assert "pane is GONE" not in output


def test_parked_probe_exception_is_unknown(make_lode):
    stored = format_park_status("quiet", "testid11")
    lode = make_lode(state="gated", status=stored, tmux_pane="%5")

    with patch("hopper.lodes.pane_liveness", side_effect=RuntimeError("tmux broke")):
        assert lode_status_for_display(lode) == stored + PARK_LIVENESS_UNVERIFIED_SUFFIX


@pytest.mark.parametrize(
    "overrides",
    [
        {"state": "running", "status": "Claude running"},
        {"stage": "shipped", "state": "ready", "status": "Shipped"},
        {"state": "error", "status": "Command failed"},
        {"state": "gated", "status": "Review required"},
        {"state": "gated", "status": "Awaiting operator answer"},
    ],
)
def test_non_parked_status_is_untouched_without_probe(make_lode, overrides):
    lode = make_lode(tmux_pane="%6", **overrides)

    with patch(
        "hopper.lodes.pane_liveness",
        side_effect=AssertionError("pane_liveness must not be called"),
    ):
        assert lode_status_for_display(lode) == overrides["status"]


@pytest.mark.parametrize("variant", ["truncated", "wrong-id"])
def test_parked_template_near_miss_is_untouched_without_probe(make_lode, variant):
    stored = format_park_status("quiet", "testid11")
    if variant == "truncated":
        stored = stored[:-1]
    else:
        stored = stored.replace("testid11", "other-id", 1)
    lode = make_lode(state="gated", status=stored, tmux_pane="%7")

    with patch(
        "hopper.lodes.pane_liveness",
        side_effect=AssertionError("pane_liveness must not be called"),
    ):
        assert lode_status_for_display(lode) == stored


def test_remote_parked_lode_is_untouched_without_probe(make_lode):
    stored = format_park_status("quiet", "testid11")
    lode = make_lode(
        state="gated",
        status=stored,
        host="builder.example",
        tmux_pane="%8",
    )

    with patch(
        "hopper.lodes.pane_liveness",
        side_effect=AssertionError("pane_liveness must not be called"),
    ):
        assert lode_status_for_display(lode) == stored


def test_missing_status_is_empty_without_probe(make_lode):
    lode = make_lode(tmux_pane="%missing")
    del lode["status"]

    with patch(
        "hopper.lodes.pane_liveness",
        side_effect=AssertionError("pane_liveness must not be called"),
    ):
        assert lode_status_for_display(lode) == ""


@pytest.mark.parametrize("host_fields", [{}, {"host": None}, {"host": ""}, {"host": "local"}])
def test_local_host_sentinels_are_probed(make_lode, host_fields):
    reason = "quiet"
    lode = make_lode(
        state="gated",
        status=format_park_status(reason, "testid11"),
        branch="hopper-testid11",
        tmux_pane="%9",
        **host_fields,
    )
    expected = PARK_PANE_GONE_STATUS.format(
        reason=reason,
        lode_id="testid11",
        branch="hopper-testid11",
    )

    with patch("hopper.lodes.pane_liveness", return_value=Liveness.GONE) as mock_liveness:
        assert lode_status_for_display(lode) == expected

    mock_liveness.assert_called_once_with("%9")


def test_lode_with_status_annotations_reports_alive_parked_lode(make_lode):
    stored = format_park_status("quiet", "testid11")
    lode = make_lode(
        state="gated",
        status=stored,
        tmux_pane="%10",
        last_pane_activity_at=42_000,
        pane_title_observation={"title": "⠐ Working", "observed_at": 40_000},
    )
    before = dict(lode)

    with patch("hopper.lodes.pane_liveness", return_value=Liveness.ALIVE) as mock_liveness:
        annotated = lode_with_status_annotations(lode)

    assert annotated is not lode
    assert lode == before
    assert annotated["status"] == stored
    assert annotated["status_display"] == stored
    assert annotated["pane_liveness"] == "alive"
    assert annotated["last_pane_activity_at"] == 42_000
    assert annotated["pane_title_observation"] == {
        "title": "⠐ Working",
        "observed_at": 40_000,
    }
    mock_liveness.assert_called_once_with("%10")


def test_lode_with_status_annotations_preserves_archived_storage_identity(make_lode):
    lode = make_lode(state="ready", stage="shipped", archived=True)
    before = dict(lode)

    annotated = lode_with_status_annotations(lode)

    assert annotated["archived"] is True
    assert annotated is not lode
    assert lode == before


@pytest.mark.parametrize(
    "probe_result",
    [Liveness.UNKNOWN, RuntimeError("tmux broke")],
    ids=["unknown", "exception"],
)
def test_lode_with_status_annotations_reports_unknown_parked_lode(make_lode, probe_result):
    stored = format_park_status("quiet", "testid11")
    lode = make_lode(state="gated", status=stored, tmux_pane="%11")
    probe = (
        patch("hopper.lodes.pane_liveness", side_effect=probe_result)
        if isinstance(probe_result, Exception)
        else patch("hopper.lodes.pane_liveness", return_value=probe_result)
    )

    with probe as mock_liveness:
        annotated = lode_with_status_annotations(lode)

    assert annotated["status"] == stored
    assert annotated["status_display"] == stored + PARK_LIVENESS_UNVERIFIED_SUFFIX
    assert "pane is GONE" not in annotated["status_display"]
    assert annotated["pane_liveness"] == "unknown"
    mock_liveness.assert_called_once_with("%11")


@pytest.mark.parametrize(
    "overrides",
    [
        {"state": "running", "status": "Claude running"},
        {"stage": "shipped", "state": "ready", "status": "Shipped"},
        {"state": "error", "status": "Command failed"},
        {"state": "gated", "status": "Review required"},
    ],
)
def test_lode_with_status_annotations_marks_non_parked_not_probed(make_lode, overrides):
    lode = make_lode(tmux_pane="%12", **overrides)

    with patch(
        "hopper.lodes.pane_liveness",
        side_effect=AssertionError("pane_liveness must not be called"),
    ):
        annotated = lode_with_status_annotations(lode)

    assert annotated["status_display"] == overrides["status"]
    assert annotated["pane_liveness"] == PANE_LIVENESS_NOT_PROBED


def test_lode_status_annotations_distinguish_unknown_from_not_probed(make_lode):
    stored = format_park_status("quiet", "testid11")
    parked = make_lode(state="gated", status=stored, tmux_pane="%13")
    running = make_lode(state="running", status="Working", tmux_pane="%14")

    with patch("hopper.lodes.pane_liveness", return_value=Liveness.UNKNOWN):
        unknown = lode_with_status_annotations(parked)
        not_probed = lode_with_status_annotations(running)

    assert unknown["pane_liveness"] == "unknown"
    assert not_probed["pane_liveness"] == PANE_LIVENESS_NOT_PROBED
    assert unknown["pane_liveness"] != not_probed["pane_liveness"]
