# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for Hopper-managed Claude workspace trust."""

import json
import os
import stat
import time
from pathlib import Path

import pytest

from hopper import config
from hopper.workspace_trust import (
    WorkspaceTrustError,
    _claude_config_lock,
    _held_for_hint,
    claude_config_path,
    trust_claude_workspace,
)


def test_default_claude_config_path_uses_subprocess_home(tmp_path):
    env = {"HOME": str(tmp_path)}

    assert claude_config_path(env) == tmp_path / ".claude.json"


def test_custom_claude_config_dir_expands_against_subprocess_home(tmp_path):
    env = {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": "~/.claude-work"}

    assert claude_config_path(env) == tmp_path / ".claude-work" / ".claude.json"


def test_trust_project_preserves_global_and_project_state(tmp_path):
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    config_path = claude_dir / ".claude.json"
    project = tmp_path / "project"
    project.mkdir()
    config_path.write_text(
        json.dumps(
            {
                "oauthAccount": {"accountUuid": "keep-me"},
                "projects": {
                    str(project): {
                        "allowedTools": ["Bash(git status)"],
                        "hasTrustDialogAccepted": False,
                    }
                },
            }
        )
    )

    trust_root = trust_claude_workspace(
        str(project),
        {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(claude_dir)},
    )

    saved = json.loads(config_path.read_text())
    assert trust_root == project
    assert saved["oauthAccount"] == {"accountUuid": "keep-me"}
    assert saved["projects"][str(project)] == {
        "allowedTools": ["Bash(git status)"],
        "hasTrustDialogAccepted": True,
    }
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_lode_worktree_trusts_exact_worktree(tmp_path):
    worktree_root = config.worktree_root()
    worktree = worktree_root / "abcdefgh"
    worktree.mkdir(parents=True)
    env = {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}
    config_path = claude_config_path(env)
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps(
            {
                "projects": {
                    str(worktree_root): {"hasTrustDialogAccepted": True},
                }
            }
        )
    )

    trust_root = trust_claude_workspace(str(worktree), env)

    saved = json.loads(config_path.read_text())
    assert trust_root == worktree
    assert saved["projects"] == {
        str(worktree_root): {"hasTrustDialogAccepted": True},
        str(worktree): {"hasTrustDialogAccepted": True},
    }


def test_existing_true_trust_does_not_rewrite_config(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    env = {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}
    config_path = claude_config_path(env)
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps(
            {
                "projects": {
                    str(project): {"hasTrustDialogAccepted": True},
                }
            }
        )
    )

    def fail_replace(source: Path, target: Path) -> None:
        raise AssertionError(f"unexpected rewrite: {source} -> {target}")

    monkeypatch.setattr("hopper.workspace_trust.os.replace", fail_replace)

    assert trust_claude_workspace(str(project), env) == project


def test_invalid_config_fails_without_overwriting(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    env = {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}
    config_path = claude_config_path(env)
    config_path.parent.mkdir()
    original = "{not-json\n"
    config_path.write_text(original)

    with pytest.raises(WorkspaceTrustError, match="cannot parse Claude config"):
        trust_claude_workspace(str(project), env)

    assert config_path.read_text() == original


def test_atomic_write_failure_preserves_existing_config(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    env = {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}
    config_path = claude_config_path(env)
    config_path.parent.mkdir()
    original = json.dumps(
        {
            "oauthAccount": {"accountUuid": "keep-me"},
            "projects": {},
        }
    )
    config_path.write_text(original)

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError(f"replace failed: {source} -> {target}")

    monkeypatch.setattr("hopper.workspace_trust.os.replace", fail_replace)

    with pytest.raises(WorkspaceTrustError, match="cannot write Claude config"):
        trust_claude_workspace(str(project), env)

    assert config_path.read_text() == original
    assert list(config_path.parent.glob(".*.hopper.*.tmp")) == []


def test_lock_contention_fails_without_writing(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    env = {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}
    config_path = claude_config_path(env)
    config_path.parent.mkdir()
    Path(f"{config_path}.lock").mkdir()

    with pytest.raises(WorkspaceTrustError, match="timed out waiting"):
        trust_claude_workspace(
            str(project),
            env,
            lock_timeout_sec=0,
            lock_poll_sec=0,
        )

    assert not config_path.exists()


def test_lock_timeout_reports_how_long_the_lock_has_existed(tmp_path):
    """An orphaned lock is indistinguishable from a busy one without its age."""
    config = tmp_path / ".claude.json"
    lock = tmp_path / ".claude.json.lock"
    lock.mkdir()
    old = time.time() - (18 * 3600 + 32 * 60)
    os.utime(lock, (old, old))

    with pytest.raises(WorkspaceTrustError) as excinfo:
        with _claude_config_lock(config, timeout_sec=0.0, poll_sec=0.0):
            pass

    message = str(excinfo.value)
    assert "18h32m" in message
    assert "remove this directory" in message


def test_lock_timeout_degrades_cleanly_when_age_is_unknowable(tmp_path, monkeypatch):
    """A diagnostic that cannot be computed must not replace the real error."""
    config = tmp_path / ".claude.json"
    lock = tmp_path / ".claude.json.lock"
    lock.mkdir()

    def refuse(self):
        raise OSError("stat refused")

    monkeypatch.setattr(Path, "lstat", refuse)

    with pytest.raises(WorkspaceTrustError) as excinfo:
        with _claude_config_lock(config, timeout_sec=0.0, poll_sec=0.0):
            pass

    message = str(excinfo.value)
    assert "timed out waiting for Claude config lock" in message
    assert "present for" not in message


def test_lock_age_hint_ignores_a_future_dated_lock(tmp_path):
    """A forward clock jump must not manufacture an age."""
    lock = tmp_path / ".claude.json.lock"
    lock.mkdir()
    ahead = time.time() + 10_000
    os.utime(lock, (ahead, ahead))

    assert _held_for_hint(lock) == ""
