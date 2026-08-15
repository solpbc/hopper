# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the offline per-lode coder schema migration."""

import json

import pytest

from scripts.migrate_coder_schema import migrate_directory


def _write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_dry_run_validates_both_stores_without_writing(tmp_path):
    active = tmp_path / "active.jsonl"
    archived = tmp_path / "archived.jsonl"
    _write(active, [{"id": "active11", "codex_thread_id": "codex-session"}])
    _write(archived, [{"id": "archive1", "codex_thread_id": None}])
    before = {active: active.read_bytes(), archived: archived.read_bytes()}

    assert migrate_directory(tmp_path, apply=False) == (2, 2)
    assert {path: path.read_bytes() for path in before} == before
    assert list(tmp_path.glob("*.pre-coder-schema")) == []


def test_apply_migrates_active_and_archived_with_recovery_backups(tmp_path):
    active = tmp_path / "active.jsonl"
    archived = tmp_path / "archived.jsonl"
    _write(active, [{"id": "active11", "codex_thread_id": "codex-session"}])
    _write(archived, [{"id": "archive1", "codex_thread_id": None}])
    old_active = active.read_bytes()
    old_archived = archived.read_bytes()

    assert migrate_directory(tmp_path, apply=True) == (2, 2)

    active_row = json.loads(active.read_text())
    archived_row = json.loads(archived.read_text())
    assert active_row["coder"] == {"provider": "codex", "session_id": "codex-session"}
    assert archived_row["coder"] == {"provider": "codex", "session_id": None}
    assert "codex_thread_id" not in active_row
    assert "codex_thread_id" not in archived_row
    assert (tmp_path / "active.jsonl.pre-coder-schema").read_bytes() == old_active
    assert (tmp_path / "archived.jsonl.pre-coder-schema").read_bytes() == old_archived


def test_invalid_second_store_prevents_any_write_or_backup(tmp_path):
    active = tmp_path / "active.jsonl"
    archived = tmp_path / "archived.jsonl"
    _write(active, [{"id": "active11", "codex_thread_id": None}])
    archived.write_text("not json\n")
    before = active.read_bytes()

    with pytest.raises(ValueError, match="invalid JSON"):
        migrate_directory(tmp_path, apply=True)

    assert active.read_bytes() == before
    assert list(tmp_path.glob("*.pre-coder-schema")) == []


def test_existing_backup_prevents_all_writes(tmp_path):
    active = tmp_path / "active.jsonl"
    archived = tmp_path / "archived.jsonl"
    _write(active, [{"id": "active11", "codex_thread_id": None}])
    _write(archived, [{"id": "archive1", "codex_thread_id": None}])
    (tmp_path / "archived.jsonl.pre-coder-schema").write_text("prior backup")
    before = {active: active.read_bytes(), archived: archived.read_bytes()}

    with pytest.raises(ValueError, match="backup already exists"):
        migrate_directory(tmp_path, apply=True)

    assert {path: path.read_bytes() for path in before} == before


def test_already_migrated_data_is_idempotent(tmp_path):
    active = tmp_path / "active.jsonl"
    _write(
        active,
        [{"id": "active11", "coder": {"provider": "grok", "session_id": "grok-session"}}],
    )

    assert migrate_directory(tmp_path, apply=True) == (0, 0)
    assert list(tmp_path.glob("*.pre-coder-schema")) == []
