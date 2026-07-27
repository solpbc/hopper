# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for Hopper-managed Claude workspace trust."""

import json
import stat
from pathlib import Path

import pytest

from hopper import config
from hopper.workspace_trust import (
    WorkspaceTrustError,
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


def test_lode_worktree_trusts_stable_worktree_root(tmp_path):
    worktree_root = config.worktree_root()
    worktree = worktree_root / "abcdefgh"
    worktree.mkdir(parents=True)
    env = {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}

    trust_root = trust_claude_workspace(str(worktree), env)

    saved = json.loads(claude_config_path(env).read_text())
    assert trust_root == worktree_root
    assert saved["projects"] == {
        str(worktree_root): {"hasTrustDialogAccepted": True},
    }
    assert str(worktree) not in saved["projects"]


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
