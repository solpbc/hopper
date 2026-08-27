# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the git utilities module."""

import os
import shutil
import subprocess
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from hopper.git import (
    SHIP_ANCESTRY_TIMEOUT_SEC,
    SHIP_CLEANLINESS_TIMEOUT_SEC,
    SHIP_DEFAULT_REF_TIMEOUT_SEC,
    SHIP_FETCH_TIMEOUT_SEC,
    SHIP_HEAD_TIMEOUT_SEC,
    SHIP_LANDING_TIMEOUT_SEC,
    SHIP_REMOTE_DETECTION_TIMEOUT_SEC,
    SHIP_REVALIDATION_TIMEOUT_SEC,
    UPSTREAM_FETCH_REFSPEC,
    _resolve_default_branch,
    authorize_quarantine_cleanup,
    branch_exists,
    capture_worktree_provenance,
    commit_all,
    create_worktree,
    current_branch,
    delete_branch,
    delete_branch_if_unchanged,
    dirty_status,
    get_diff_numstat,
    get_diff_stat,
    head_sha,
    is_dirty,
    quarantine_dirty_repo,
    quarantine_worktree,
    remove_quarantined_worktree,
    remove_worktree,
    repair_quarantined_worktree,
    repository_fetch_lock,
    ship_landing_verdict,
    unpushed_commits,
)


@pytest.fixture(autouse=True)
def isolate_git_config(monkeypatch):
    """Keep real-git tests independent of user and system configuration."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


def _run_git(repo_dir, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )


def _probe_git(repo_dir, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )


def _init_git_repo(tmp_path, *, name="repo", branch="main", bare=False):
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")

    repo_dir = tmp_path / name
    if bare:
        _run_git(tmp_path, "init", "--bare", "-b", branch, str(repo_dir))
        return repo_dir

    repo_dir.mkdir()
    _run_git(repo_dir, "init", "-b", branch)
    _run_git(repo_dir, "config", "user.email", "test@example.com")
    _run_git(repo_dir, "config", "user.name", "Test User")
    (repo_dir / "README.md").write_text("init\n")
    _run_git(repo_dir, "add", ".")
    _run_git(repo_dir, "commit", "-m", "init")
    return repo_dir


@pytest.fixture
def stale_clone_factory(tmp_path):
    """Create a local clone whose upstream branch has advanced by one commit."""

    def create(branch):
        remote = _init_git_repo(
            tmp_path,
            name=f"{branch}-remote.git",
            branch=branch,
            bare=True,
        )
        publisher = tmp_path / f"{branch}-publisher"
        _run_git(tmp_path, "clone", str(remote), str(publisher))
        _run_git(publisher, "config", "user.email", "test@example.com")
        _run_git(publisher, "config", "user.name", "Test User")
        (publisher / "README.md").write_text("initial\n")
        _run_git(publisher, "add", ".")
        _run_git(publisher, "commit", "-m", "initial")
        _run_git(publisher, "push", "-u", "origin", branch)

        registered = tmp_path / f"{branch}-registered"
        _run_git(tmp_path, "clone", str(remote), str(registered))
        _run_git(registered, "config", "user.email", "test@example.com")
        _run_git(registered, "config", "user.name", "Test User")
        local_sha = _run_git(registered, "rev-parse", "HEAD").stdout.strip()

        (publisher / "upstream.txt").write_text("upstream\n")
        _run_git(publisher, "add", ".")
        _run_git(publisher, "commit", "-m", "advance upstream")
        _run_git(publisher, "push", "origin", branch)
        upstream_sha = _run_git(publisher, "rev-parse", "HEAD").stdout.strip()

        return registered, local_sha, upstream_sha

    return create


@pytest.fixture
def upstream_clone_factory(tmp_path):
    """Create a populated bare upstream, its publisher, and a registered clone."""

    def create(branch, *, name=None):
        prefix = name or branch
        remote = _init_git_repo(
            tmp_path,
            name=f"{prefix}-upstream.git",
            branch=branch,
            bare=True,
        )
        _run_git(remote, "config", "receive.denyDeleteCurrent", "ignore")
        publisher = tmp_path / f"{prefix}-publisher"
        _run_git(tmp_path, "clone", str(remote), str(publisher))
        _run_git(publisher, "config", "user.email", "test@example.com")
        _run_git(publisher, "config", "user.name", "Test User")
        (publisher / "README.md").write_text("initial\n")
        _run_git(publisher, "add", ".")
        _run_git(publisher, "commit", "-m", "initial")
        _run_git(publisher, "push", "-u", "origin", branch)

        registered = tmp_path / f"{prefix}-registered"
        _run_git(tmp_path, "clone", str(remote), str(registered))
        _run_git(registered, "config", "user.email", "test@example.com")
        _run_git(registered, "config", "user.name", "Test User")
        return remote, publisher, registered

    return create


def _remove_or_rename_upstream_default(publisher, branch, change):
    if change == "renamed":
        _run_git(publisher, "branch", "-m", "trunk")
        _run_git(publisher, "push", "-u", "origin", "trunk")
    _run_git(publisher, "push", "origin", "--delete", branch)


class TestCreateWorktree:
    @pytest.fixture(autouse=True)
    def _mock_fetch_lock(self, monkeypatch):
        monkeypatch.setattr("hopper.git.repository_fetch_lock", lambda _repo: nullcontext())

    def test_success(self, tmp_path):
        """Fetches origin and creates an untracked worktree from its first default ref."""
        worktree_path = tmp_path / "worktree"
        remote_result = MagicMock(returncode=0, stdout="origin\n")
        fetch_result = MagicMock(returncode=0)
        resolve_result = MagicMock(returncode=0, stdout="abc123\n")
        branch_result = MagicMock(returncode=1)
        add_result = MagicMock(returncode=0)

        with patch(
            "subprocess.run",
            side_effect=[remote_result, fetch_result, resolve_result, branch_result, add_result],
        ) as mock_run:
            result = create_worktree("/repo", worktree_path, "hopper-abc12345")

        assert result == (True, None)
        assert mock_run.call_args_list == [
            call(
                ["git", "remote"],
                cwd="/repo",
                capture_output=True,
                text=True,
            ),
            call(
                ["git", "fetch", "--prune", "origin", UPSTREAM_FETCH_REFSPEC],
                cwd="/repo",
                capture_output=True,
                text=True,
                timeout=SHIP_FETCH_TIMEOUT_SEC,
            ),
            call(
                ["git", "rev-parse", "--verify", "origin/main"],
                cwd="/repo",
                capture_output=True,
                text=True,
                timeout=None,
            ),
            call(
                [
                    "git",
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    "refs/heads/hopper-abc12345",
                ],
                cwd="/repo",
                capture_output=True,
                text=True,
                timeout=2.0,
            ),
            call(
                [
                    "git",
                    "worktree",
                    "add",
                    "--no-track",
                    "-b",
                    "hopper-abc12345",
                    str(worktree_path),
                    "origin/main",
                ],
                cwd="/repo",
                capture_output=True,
                text=True,
            ),
        ]

    def test_failure_returns_false(self, tmp_path):
        """Returns the captured detail when git worktree add fails."""
        worktree_path = tmp_path / "worktree"
        remote_result = MagicMock(returncode=0, stdout="")
        branch_result = MagicMock(returncode=1)
        add_result = MagicMock(returncode=1, stderr="fatal: already exists")
        prune_result = MagicMock(returncode=0)
        branch_after_result = MagicMock(returncode=1)

        with patch(
            "subprocess.run",
            side_effect=[
                remote_result,
                branch_result,
                add_result,
                prune_result,
                branch_after_result,
            ],
        ):
            result = create_worktree("/repo", worktree_path, "hopper-abc12345")

        assert result == (False, "git worktree add failed: fatal: already exists")

    def test_cleanup_failure_preserves_original_add_detail(self, tmp_path):
        """Cleanup errors do not replace the original worktree-add failure."""
        worktree_path = tmp_path / "worktree"
        results = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=1),
            MagicMock(returncode=1, stderr="fatal: original add failure"),
            MagicMock(returncode=0),
            MagicMock(returncode=1),
        ]

        with (
            patch("subprocess.run", side_effect=results),
            patch(
                "hopper.git.remove_worktree",
                side_effect=RuntimeError("cleanup failed"),
            ) as mock_remove,
        ):
            result = create_worktree("/repo", worktree_path, "hopper-abc12345")

        assert result == (False, "git worktree add failed: fatal: original add failure")
        mock_remove.assert_called_once_with("/repo", str(worktree_path))

    def test_git_not_found(self, tmp_path):
        """Returns False when git is not installed."""
        worktree_path = tmp_path / "worktree"

        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = create_worktree("/repo", worktree_path, "hopper-abc12345")

        assert result == (False, "git command not found")

    def test_uses_origin_master_when_main_is_missing(self, tmp_path):
        worktree_path = tmp_path / "worktree"
        results = [
            MagicMock(returncode=0, stdout="origin\n"),
            MagicMock(returncode=0),
            MagicMock(returncode=128, stdout=""),
            MagicMock(returncode=0, stdout="abc123\n"),
            MagicMock(returncode=1),
            MagicMock(returncode=0),
        ]

        with patch("subprocess.run", side_effect=results) as mock_run:
            result = create_worktree("/repo", worktree_path, "hopper-abc12345")

        assert result == (True, None)
        assert mock_run.call_args_list[-3:] == [
            call(
                ["git", "rev-parse", "--verify", "origin/master"],
                cwd="/repo",
                capture_output=True,
                text=True,
                timeout=None,
            ),
            call(
                [
                    "git",
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    "refs/heads/hopper-abc12345",
                ],
                cwd="/repo",
                capture_output=True,
                text=True,
                timeout=2.0,
            ),
            call(
                [
                    "git",
                    "worktree",
                    "add",
                    "--no-track",
                    "-b",
                    "hopper-abc12345",
                    str(worktree_path),
                    "origin/master",
                ],
                cwd="/repo",
                capture_output=True,
                text=True,
            ),
        ]

    def test_without_origin_uses_head_without_fetch(self, tmp_path):
        worktree_path = tmp_path / "worktree"
        remote_result = MagicMock(returncode=0, stdout="upstream\n")
        branch_result = MagicMock(returncode=1)
        add_result = MagicMock(returncode=0)

        with patch(
            "subprocess.run", side_effect=[remote_result, branch_result, add_result]
        ) as mock_run:
            result = create_worktree("/repo", worktree_path, "hopper-abc12345")

        assert result == (True, None)
        assert mock_run.call_args_list[-1] == call(
            [
                "git",
                "worktree",
                "add",
                "--no-track",
                "-b",
                "hopper-abc12345",
                str(worktree_path),
                "HEAD",
            ],
            cwd="/repo",
            capture_output=True,
            text=True,
        )
        assert not any(args[0][:2] == ["git", "fetch"] for args, _kwargs in mock_run.call_args_list)

    def test_git_remote_failure_is_hard_failure(self, tmp_path):
        worktree_path = tmp_path / "worktree"
        remote_result = MagicMock(returncode=128, stderr="fatal: not a git repository")

        with patch("subprocess.run", return_value=remote_result) as mock_run:
            result = create_worktree("/repo", worktree_path, "hopper-abc12345")

        assert result == (False, "git remote failed: fatal: not a git repository")
        assert mock_run.call_count == 1

    def test_fetch_failure_returns_detail_before_resolution_or_add(self, tmp_path):
        worktree_path = tmp_path / "worktree"
        results = [
            MagicMock(returncode=0, stdout="origin\n"),
            MagicMock(returncode=128, stderr="fatal: unavailable"),
        ]

        with patch("subprocess.run", side_effect=results) as mock_run:
            result = create_worktree("/repo", worktree_path, "hopper-abc12345")

        assert result == (False, "git fetch origin failed: fatal: unavailable")
        assert mock_run.call_count == 2

    def test_fetch_timeout_returns_detail_before_resolution_or_add(self, tmp_path):
        worktree_path = tmp_path / "worktree"
        remote_result = MagicMock(returncode=0, stdout="origin\n")

        with patch(
            "subprocess.run",
            side_effect=[
                remote_result,
                subprocess.TimeoutExpired(["git", "fetch", "origin"], SHIP_FETCH_TIMEOUT_SEC),
            ],
        ) as mock_run:
            result = create_worktree("/repo", worktree_path, "hopper-abc12345")

        assert result == (
            False,
            f"git fetch origin timed out after {SHIP_FETCH_TIMEOUT_SEC:g} seconds",
        )
        assert mock_run.call_count == 2

    def test_resolution_failure_returns_detail_before_add(self, tmp_path):
        worktree_path = tmp_path / "worktree"
        results = [
            MagicMock(returncode=0, stdout="origin\n"),
            MagicMock(returncode=0),
            MagicMock(returncode=128, stdout=""),
            MagicMock(returncode=128, stdout=""),
        ]

        with patch("subprocess.run", side_effect=results) as mock_run:
            result = create_worktree("/repo", worktree_path, "hopper-abc12345")

        assert result == (
            False,
            "upstream default branch resolution failed after git fetch origin: "
            "no candidate exists (origin/main, origin/master)",
        )
        assert mock_run.call_count == 4


class TestCreateWorktreeIntegration:
    def test_fetches_stale_origin_main(self, tmp_path, stale_clone_factory):
        registered, local_sha, upstream_sha = stale_clone_factory("main")
        worktree_path = tmp_path / "main-worktree"

        created, error = create_worktree(str(registered), worktree_path, "hopper-stale-main")

        assert (created, error) == (True, None)
        assert _run_git(worktree_path, "rev-parse", "HEAD").stdout.strip() == upstream_sha
        assert _run_git(registered, "rev-parse", "HEAD").stdout.strip() == local_sha

    def test_leaves_registered_checkout_unchanged(self, tmp_path, stale_clone_factory):
        registered, _local_sha, _upstream_sha = stale_clone_factory("main")
        branch_before = _run_git(registered, "branch", "--show-current").stdout
        head_before = _run_git(registered, "rev-parse", "HEAD").stdout
        status_before = _run_git(registered, "status", "--porcelain").stdout
        worktree_path = tmp_path / "unchanged-worktree"

        created, error = create_worktree(str(registered), worktree_path, "hopper-unchanged")

        assert (created, error) == (True, None)
        assert _run_git(registered, "branch", "--show-current").stdout == branch_before
        assert _run_git(registered, "rev-parse", "HEAD").stdout == head_before
        assert _run_git(registered, "status", "--porcelain").stdout == status_before

    def test_fetches_origin_master(self, tmp_path, stale_clone_factory):
        registered, local_sha, upstream_sha = stale_clone_factory("master")
        worktree_path = tmp_path / "master-worktree"

        created, error = create_worktree(str(registered), worktree_path, "hopper-stale-master")

        assert (created, error) == (True, None)
        assert _run_git(worktree_path, "rev-parse", "HEAD").stdout.strip() == upstream_sha
        assert _run_git(registered, "rev-parse", "HEAD").stdout.strip() == local_sha

    def test_without_origin_uses_current_head(self, tmp_path):
        registered = _init_git_repo(tmp_path, branch="topic")
        expected_sha = _run_git(registered, "rev-parse", "HEAD").stdout.strip()
        worktree_path = tmp_path / "topic-worktree"

        created, error = create_worktree(str(registered), worktree_path, "hopper-no-origin")

        assert (created, error) == (True, None)
        assert _run_git(worktree_path, "rev-parse", "HEAD").stdout.strip() == expected_sha

    def test_fetch_failure_leaves_no_worktree_or_branch(self, tmp_path, stale_clone_factory):
        registered, _local_sha, _upstream_sha = stale_clone_factory("main")
        missing_remote = tmp_path / "missing.git"
        _run_git(registered, "remote", "set-url", "origin", str(missing_remote))
        worktree_path = tmp_path / "fetch-failure-worktree"
        branch_name = "hopper-fetch-failure"

        created, error = create_worktree(str(registered), worktree_path, branch_name)

        assert created is False
        assert error is not None
        assert error.startswith("git fetch origin failed:")
        assert not worktree_path.exists()
        assert _run_git(registered, "branch", "--list", branch_name).stdout.strip() == ""

    def test_resolution_failure_leaves_no_worktree_or_branch(self, tmp_path, stale_clone_factory):
        registered, _local_sha, _upstream_sha = stale_clone_factory("develop")
        worktree_path = tmp_path / "resolve-failure-worktree"
        branch_name = "hopper-resolve-failure"

        created, error = create_worktree(str(registered), worktree_path, branch_name)

        assert created is False
        assert error == (
            "upstream default branch resolution failed after git fetch origin: "
            "no candidate exists (origin/main, origin/master)"
        )
        assert not worktree_path.exists()
        assert _run_git(registered, "branch", "--list", branch_name).stdout.strip() == ""

    @pytest.mark.parametrize("branch", ["main", "master"])
    @pytest.mark.parametrize("change", ["deleted", "renamed"])
    def test_pruned_fetch_rejects_stale_upstream_default(
        self, tmp_path, upstream_clone_factory, branch, change
    ):
        _remote, publisher, registered = upstream_clone_factory(
            branch, name=f"create-{branch}-{change}"
        )
        stale_sha = _run_git(registered, "rev-parse", f"origin/{branch}").stdout.strip()
        registered_branch = _run_git(registered, "branch", "--show-current").stdout
        registered_head = _run_git(registered, "rev-parse", "HEAD").stdout
        worktree_path = tmp_path / f"create-{branch}-{change}-worktree"
        branch_name = f"hopper-{branch}-{change}"

        _remove_or_rename_upstream_default(publisher, branch, change)
        assert _run_git(registered, "rev-parse", f"origin/{branch}").stdout.strip() == stale_sha

        created, error = create_worktree(str(registered), worktree_path, branch_name)

        assert created is False
        assert error == (
            "upstream default branch resolution failed after git fetch origin: "
            "no candidate exists (origin/main, origin/master)"
        )
        assert not worktree_path.exists()
        assert _run_git(registered, "branch", "--list", branch_name).stdout.strip() == ""
        assert _run_git(registered, "branch", "--show-current").stdout == registered_branch
        assert _run_git(registered, "rev-parse", "HEAD").stdout == registered_head
        missing = subprocess.run(
            ["git", "rev-parse", "--verify", f"origin/{branch}"],
            cwd=registered,
            capture_output=True,
            text=True,
        )
        assert missing.returncode != 0

    def test_partial_add_failure_removes_created_worktree_branch_and_registration(
        self, tmp_path, stale_clone_factory
    ):
        registered, local_sha, upstream_sha = stale_clone_factory("main")
        hook = registered / ".git" / "hooks" / "post-checkout"
        hook.write_text("#!/bin/sh\nexit 3\n")
        hook.chmod(0o755)
        worktree_path = tmp_path / "partial-worktree"
        branch_name = "hopper-partial"

        created, error = create_worktree(str(registered), worktree_path, branch_name)

        assert created is False
        assert error is not None
        assert error.startswith("git worktree add failed:")
        assert not worktree_path.exists()
        assert _run_git(registered, "branch", "--list", branch_name).stdout.strip() == ""
        worktree_list = _run_git(registered, "worktree", "list", "--porcelain").stdout
        assert str(worktree_path) not in worktree_list
        assert _run_git(registered, "rev-parse", "HEAD").stdout.strip() == local_sha
        assert _run_git(registered, "rev-parse", "origin/main").stdout.strip() == upstream_sha
        assert _run_git(registered, "status", "--porcelain").stdout.strip() == ""
        assert not (registered / "upstream.txt").exists()

    @pytest.mark.parametrize("artifact", ["branch", "path"])
    def test_add_failure_preserves_preexisting_artifact(self, tmp_path, artifact):
        registered = _init_git_repo(tmp_path)
        worktree_path = tmp_path / "preserved-worktree"
        branch_name = "hopper-preserved"
        if artifact == "branch":
            _run_git(registered, "branch", branch_name)
        else:
            worktree_path.mkdir()
            (worktree_path / "sentinel.txt").write_text("keep\n")

        created, error = create_worktree(str(registered), worktree_path, branch_name)

        assert created is False
        assert error is not None
        if artifact == "branch":
            assert _run_git(registered, "branch", "--list", branch_name).stdout.strip()
            assert not worktree_path.exists()
        else:
            assert (worktree_path / "sentinel.txt").read_text() == "keep\n"
            assert _run_git(registered, "branch", "--list", branch_name).stdout.strip() == ""

    def test_inconclusive_branch_probe_preserves_preexisting_branch(self, tmp_path):
        registered = _init_git_repo(tmp_path)
        worktree_path = tmp_path / "preserved-worktree"
        branch_name = "hopper-preserved"
        _run_git(registered, "branch", branch_name)
        real_run = subprocess.run

        def inconclusive_probe(command, **kwargs):
            if command == [
                "git",
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{branch_name}",
            ]:
                return subprocess.CompletedProcess(command, 128, stderr="fatal: cannot inspect")
            if command[:3] == ["git", "worktree", "add"]:
                return subprocess.CompletedProcess(command, 1, stderr="fatal: add rejected")
            return real_run(command, **kwargs)

        with patch("hopper.git.subprocess.run", side_effect=inconclusive_probe):
            created, error = create_worktree(str(registered), worktree_path, branch_name)

        assert created is False
        assert error == "git worktree add failed: fatal: add rejected"
        assert not worktree_path.exists()
        assert _run_git(registered, "branch", "--list", branch_name).stdout.strip() == branch_name

    def test_diff_helpers_use_origin_main_when_local_main_is_behind(
        self, tmp_path, stale_clone_factory
    ):
        registered, local_sha, _upstream_sha = stale_clone_factory("main")
        worktree_path = tmp_path / "diff-worktree"
        created, error = create_worktree(str(registered), worktree_path, "hopper-diff-base")

        assert (created, error) == (True, None)
        assert _run_git(registered, "rev-parse", "main").stdout.strip() == local_sha
        assert get_diff_stat(str(worktree_path)) == ""
        assert get_diff_numstat(str(worktree_path)) == ""

    def test_diff_helpers_report_only_feature_changes_from_origin_main(
        self, tmp_path, stale_clone_factory
    ):
        registered, _local_sha, _upstream_sha = stale_clone_factory("main")
        worktree_path = tmp_path / "feature-diff-worktree"
        created, error = create_worktree(str(registered), worktree_path, "hopper-feature-diff")
        assert (created, error) == (True, None)
        (worktree_path / "feature.txt").write_text("feature\n")
        _run_git(worktree_path, "add", ".")
        _run_git(worktree_path, "commit", "-m", "feature")

        assert "feature.txt" in get_diff_stat(str(worktree_path))
        numstat = get_diff_numstat(str(worktree_path))
        assert "feature.txt" in numstat
        assert "upstream.txt" not in numstat

    def test_diff_helpers_fall_back_to_local_default_without_remote_refs(self, tmp_path):
        registered = _init_git_repo(tmp_path)
        worktree_path = tmp_path / "local-diff-worktree"
        created, error = create_worktree(str(registered), worktree_path, "hopper-local-diff")
        assert (created, error) == (True, None)

        assert get_diff_stat(str(worktree_path)) == ""
        assert get_diff_numstat(str(worktree_path)) == ""


class TestShipLandingVerdictIntegration:
    @pytest.mark.parametrize("branch", ["main", "master"])
    @pytest.mark.parametrize("upstream_advanced", [False, True])
    def test_accepts_head_contained_in_fresh_default(
        self, upstream_clone_factory, branch, upstream_advanced
    ):
        _remote, publisher, registered = upstream_clone_factory(
            branch, name=f"landing-{branch}-{upstream_advanced}"
        )
        if upstream_advanced:
            (publisher / "upstream.txt").write_text("advanced\n")
            _run_git(publisher, "add", ".")
            _run_git(publisher, "commit", "-m", "advance upstream")
            _run_git(publisher, "push", "origin", branch)

        verdict = ship_landing_verdict(registered)

        assert verdict.cleanliness == "clean"
        assert verdict.containment == "contained"
        assert verdict.base_ref == f"origin/{branch}"

    def test_main_wins_when_main_and_master_exist(self, upstream_clone_factory):
        _remote, publisher, registered = upstream_clone_factory("main", name="both-contained")
        _run_git(publisher, "branch", "master")
        _run_git(publisher, "push", "origin", "master")

        verdict = ship_landing_verdict(registered)

        assert verdict.containment == "contained"
        assert verdict.base_ref == "origin/main"

    def test_main_wins_and_refuses_head_contained_only_in_master(self, upstream_clone_factory):
        _remote, publisher, registered = upstream_clone_factory("main", name="both-diverged")
        _run_git(publisher, "switch", "-c", "master")
        (publisher / "master.txt").write_text("master\n")
        _run_git(publisher, "add", ".")
        _run_git(publisher, "commit", "-m", "master-only")
        _run_git(publisher, "push", "origin", "master")
        _run_git(publisher, "switch", "main")
        (publisher / "main.txt").write_text("main\n")
        _run_git(publisher, "add", ".")
        _run_git(publisher, "commit", "-m", "main-only")
        _run_git(publisher, "push", "origin", "main")
        _run_git(registered, "fetch", "origin")
        _run_git(registered, "switch", "-c", "feature", "origin/master")

        verdict = ship_landing_verdict(registered)

        assert verdict.cleanliness == "clean"
        assert verdict.containment == "not_contained"
        assert verdict.base_ref == "origin/main"

    def test_feature_only_push_is_not_landed(self, upstream_clone_factory):
        _remote, _publisher, registered = upstream_clone_factory("main", name="feature-only")
        _run_git(registered, "switch", "-c", "feature")
        (registered / "feature.txt").write_text("feature\n")
        _run_git(registered, "add", ".")
        _run_git(registered, "commit", "-m", "feature")
        _run_git(registered, "push", "-u", "origin", "feature")

        verdict = ship_landing_verdict(registered)

        assert verdict.cleanliness == "clean"
        assert verdict.containment == "not_contained"
        assert verdict.base_ref == "origin/main"

    @pytest.mark.parametrize("branch", ["main", "master"])
    @pytest.mark.parametrize("change", ["deleted", "renamed"])
    def test_pruned_fetch_rejects_stale_upstream_default(
        self, upstream_clone_factory, branch, change
    ):
        _remote, publisher, registered = upstream_clone_factory(
            branch, name=f"landing-{branch}-{change}"
        )
        stale_sha = _run_git(registered, "rev-parse", f"origin/{branch}").stdout.strip()
        _remove_or_rename_upstream_default(publisher, branch, change)
        assert _run_git(registered, "rev-parse", f"origin/{branch}").stdout.strip() == stale_sha

        verdict = ship_landing_verdict(registered)

        assert verdict.cleanliness == "clean"
        assert verdict.containment == "indeterminate"
        assert verdict.base_ref is None
        assert "no fresh upstream default branch" in verdict.detail
        missing = subprocess.run(
            ["git", "rev-parse", "--verify", f"origin/{branch}"],
            cwd=registered,
            capture_output=True,
            text=True,
        )
        assert missing.returncode != 0

    def test_repo_without_origin_or_local_default_is_indeterminate(self, tmp_path):
        repo = _init_git_repo(tmp_path, name="different-remote", branch="topic")
        remote = _init_git_repo(tmp_path, name="different-upstream.git", branch="topic", bare=True)
        _run_git(repo, "remote", "add", "upstream", str(remote))

        verdict = ship_landing_verdict(repo)

        assert verdict.cleanliness == "clean"
        assert verdict.containment == "indeterminate"
        assert verdict.base_ref is None
        assert "no local default branch" in verdict.detail

    def test_repo_without_origin_requires_local_default_containment(self, tmp_path):
        repo = _init_git_repo(tmp_path, name="local-main", branch="main")

        contained = ship_landing_verdict(repo)

        assert contained.containment == "contained"
        assert contained.base_ref == "main"

        _run_git(repo, "switch", "-c", "feature")
        (repo / "feature.txt").write_text("feature\n")
        _run_git(repo, "add", ".")
        _run_git(repo, "commit", "-m", "feature")

        not_contained = ship_landing_verdict(repo)

        assert not_contained.containment == "not_contained"
        assert not_contained.base_ref == "main"

    @pytest.mark.parametrize("dirty_kind", ["staged", "unstaged", "untracked"])
    def test_canonical_changes_are_not_hidden_by_clean_cwd(self, tmp_path, monkeypatch, dirty_kind):
        canonical = _init_git_repo(tmp_path, name=f"canonical-{dirty_kind}")
        clean_cwd = _init_git_repo(tmp_path, name=f"clean-cwd-{dirty_kind}")
        if dirty_kind == "staged":
            (canonical / "staged.txt").write_text("staged\n")
            _run_git(canonical, "add", ".")
        elif dirty_kind == "unstaged":
            (canonical / "README.md").write_text("changed\n")
        else:
            (canonical / "untracked.txt").write_text("untracked\n")
        monkeypatch.chdir(clean_cwd)

        verdict = ship_landing_verdict(canonical)

        assert verdict.cleanliness == "dirty"
        assert verdict.containment == "indeterminate"

    def test_dirty_unrelated_cwd_does_not_taint_clean_canonical_tree(self, tmp_path, monkeypatch):
        canonical = _init_git_repo(tmp_path, name="clean-canonical", branch="main")
        dirty_cwd = _init_git_repo(tmp_path, name="dirty-cwd")
        (dirty_cwd / "untracked.txt").write_text("dirty\n")
        monkeypatch.chdir(dirty_cwd)

        verdict = ship_landing_verdict(canonical)

        assert verdict.cleanliness == "clean"
        assert verdict.containment == "contained"
        assert verdict.base_ref == "main"

    def test_missing_worktree_is_indeterminate(self, tmp_path):
        verdict = ship_landing_verdict(tmp_path / "missing")

        assert verdict.cleanliness == "indeterminate"
        assert verdict.containment == "indeterminate"
        assert "missing" in verdict.detail

    def test_unreadable_worktree_is_indeterminate(self, tmp_path):
        with patch.object(Path, "is_dir", side_effect=OSError("permission denied")):
            verdict = ship_landing_verdict(tmp_path)

        assert verdict.cleanliness == "indeterminate"
        assert verdict.containment == "indeterminate"
        assert verdict.cause == "worktree_unreadable"
        assert "permission denied" in verdict.detail


class TestShipLandingVerdictDeadlines:
    @pytest.fixture(autouse=True)
    def _mock_fetch_lock(self, monkeypatch):
        monkeypatch.setattr("hopper.git.repository_fetch_lock", lambda _repo: nullcontext())

    def test_each_stage_uses_its_named_deadline_and_total_budget(self, tmp_path):
        results = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout="abc123\n"),
            MagicMock(returncode=0, stdout="origin\n"),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout="base123\n"),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout="abc123\n"),
            MagicMock(returncode=0, stdout=""),
        ]
        with (
            patch("hopper.git.time.monotonic", return_value=0),
            patch("hopper.git.subprocess.run", side_effect=results) as run,
        ):
            verdict = ship_landing_verdict(tmp_path)

        assert verdict.containment == "contained"
        assert [item.kwargs["timeout"] for item in run.call_args_list] == [
            SHIP_CLEANLINESS_TIMEOUT_SEC,
            SHIP_HEAD_TIMEOUT_SEC,
            SHIP_REMOTE_DETECTION_TIMEOUT_SEC,
            SHIP_FETCH_TIMEOUT_SEC,
            SHIP_DEFAULT_REF_TIMEOUT_SEC,
            SHIP_ANCESTRY_TIMEOUT_SEC,
            SHIP_REVALIDATION_TIMEOUT_SEC,
            SHIP_REVALIDATION_TIMEOUT_SEC,
        ]
        assert (
            SHIP_CLEANLINESS_TIMEOUT_SEC
            + SHIP_HEAD_TIMEOUT_SEC
            + SHIP_REMOTE_DETECTION_TIMEOUT_SEC
            + SHIP_FETCH_TIMEOUT_SEC
            + SHIP_DEFAULT_REF_TIMEOUT_SEC
            + SHIP_ANCESTRY_TIMEOUT_SEC
            + 2 * SHIP_REVALIDATION_TIMEOUT_SEC
            == SHIP_LANDING_TIMEOUT_SEC
        )

    def test_cleanliness_timeout_is_immediately_indeterminate(self, tmp_path):
        with (
            patch("hopper.git.time.monotonic", return_value=0),
            patch(
                "hopper.git.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    ["git", "status", "--porcelain"],
                    SHIP_CLEANLINESS_TIMEOUT_SEC,
                ),
            ) as run,
        ):
            verdict = ship_landing_verdict(tmp_path)

        assert verdict.cleanliness == "indeterminate"
        assert verdict.containment == "indeterminate"
        assert run.call_args.kwargs["timeout"] == SHIP_CLEANLINESS_TIMEOUT_SEC

    def test_cleanliness_nonzero_is_indeterminate(self, tmp_path):
        with (
            patch("hopper.git.time.monotonic", return_value=0),
            patch(
                "hopper.git.subprocess.run",
                return_value=MagicMock(returncode=128, stdout="", stderr="fatal: status"),
            ),
        ):
            verdict = ship_landing_verdict(tmp_path)

        assert verdict.cleanliness == "indeterminate"
        assert "fatal: status" in verdict.detail

    def test_cleanliness_transport_error_is_indeterminate(self, tmp_path):
        with (
            patch("hopper.git.time.monotonic", return_value=0),
            patch("hopper.git.subprocess.run", side_effect=OSError("git unavailable")) as run,
        ):
            verdict = ship_landing_verdict(tmp_path)

        assert verdict.cleanliness == "indeterminate"
        assert verdict.containment == "indeterminate"
        assert verdict.cause == "cleanliness_unavailable"
        assert run.call_args.kwargs["timeout"] == SHIP_CLEANLINESS_TIMEOUT_SEC

    def test_remote_detection_timeout_fails_closed(self, tmp_path):
        with (
            patch("hopper.git.time.monotonic", return_value=0),
            patch(
                "hopper.git.subprocess.run",
                side_effect=[
                    MagicMock(returncode=0, stdout=""),
                    MagicMock(returncode=0, stdout="abc123\n"),
                    subprocess.TimeoutExpired(["git", "remote"], SHIP_REMOTE_DETECTION_TIMEOUT_SEC),
                ],
            ) as run,
        ):
            verdict = ship_landing_verdict(tmp_path)

        assert verdict.containment == "indeterminate"
        assert run.call_args.kwargs["timeout"] == SHIP_REMOTE_DETECTION_TIMEOUT_SEC

    def test_remote_detection_nonzero_fails_closed(self, tmp_path):
        with (
            patch("hopper.git.time.monotonic", return_value=0),
            patch(
                "hopper.git.subprocess.run",
                side_effect=[
                    MagicMock(returncode=0, stdout=""),
                    MagicMock(returncode=0, stdout="abc123\n"),
                    MagicMock(returncode=128, stdout="", stderr="fatal: remotes"),
                ],
            ),
        ):
            verdict = ship_landing_verdict(tmp_path)

        assert verdict.containment == "indeterminate"
        assert "fatal: remotes" in verdict.detail

    def test_fetch_timeout_fails_closed(self, tmp_path):
        with (
            patch("hopper.git.time.monotonic", return_value=0),
            patch(
                "hopper.git.subprocess.run",
                side_effect=[
                    MagicMock(returncode=0, stdout=""),
                    MagicMock(returncode=0, stdout="abc123\n"),
                    MagicMock(returncode=0, stdout="origin\n"),
                    subprocess.TimeoutExpired(["git", "fetch"], SHIP_FETCH_TIMEOUT_SEC),
                ],
            ) as run,
        ):
            verdict = ship_landing_verdict(tmp_path)

        assert verdict.containment == "indeterminate"
        assert "fetch from origin timed out" in verdict.detail
        assert run.call_args.kwargs["timeout"] == SHIP_FETCH_TIMEOUT_SEC

    def test_fetch_nonzero_fails_closed(self, tmp_path):
        with (
            patch("hopper.git.time.monotonic", return_value=0),
            patch(
                "hopper.git.subprocess.run",
                side_effect=[
                    MagicMock(returncode=0, stdout=""),
                    MagicMock(returncode=0, stdout="abc123\n"),
                    MagicMock(returncode=0, stdout="origin\n"),
                    MagicMock(returncode=128, stdout="", stderr="fatal: fetch"),
                ],
            ),
        ):
            verdict = ship_landing_verdict(tmp_path)

        assert verdict.containment == "indeterminate"
        assert "fatal: fetch" in verdict.detail

    def test_default_ref_timeout_fails_closed(self, tmp_path):
        with (
            patch("hopper.git.time.monotonic", return_value=0),
            patch(
                "hopper.git.subprocess.run",
                side_effect=[
                    MagicMock(returncode=0, stdout=""),
                    MagicMock(returncode=0, stdout="abc123\n"),
                    MagicMock(returncode=0, stdout="origin\n"),
                    MagicMock(returncode=0, stdout=""),
                    subprocess.TimeoutExpired(["git", "rev-parse"], SHIP_DEFAULT_REF_TIMEOUT_SEC),
                ],
            ) as run,
        ):
            verdict = ship_landing_verdict(tmp_path)

        assert verdict.containment == "indeterminate"
        assert verdict.base_ref is None
        assert run.call_args.kwargs["timeout"] == SHIP_DEFAULT_REF_TIMEOUT_SEC

    def test_ancestry_nonzero_other_than_one_is_indeterminate(self, tmp_path):
        results = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout="abc123\n"),
            MagicMock(returncode=0, stdout="origin\n"),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout="abc123\n"),
            MagicMock(returncode=128, stdout="", stderr="fatal: ancestry"),
        ]
        with (
            patch("hopper.git.time.monotonic", return_value=0),
            patch("hopper.git.subprocess.run", side_effect=results),
        ):
            verdict = ship_landing_verdict(tmp_path)

        assert verdict.containment == "indeterminate"
        assert verdict.base_ref == "origin/main"
        assert "fatal: ancestry" in verdict.detail

    def test_ancestry_timeout_fails_closed(self, tmp_path):
        results = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout="abc123\n"),
            MagicMock(returncode=0, stdout="origin\n"),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout="abc123\n"),
            subprocess.TimeoutExpired(["git", "merge-base"], SHIP_ANCESTRY_TIMEOUT_SEC),
        ]
        with (
            patch("hopper.git.time.monotonic", return_value=0),
            patch("hopper.git.subprocess.run", side_effect=results) as run,
        ):
            verdict = ship_landing_verdict(tmp_path)

        assert verdict.containment == "indeterminate"
        assert verdict.base_ref == "origin/main"
        assert run.call_args.kwargs["timeout"] == SHIP_ANCESTRY_TIMEOUT_SEC

    def test_head_change_after_ancestry_fails_closed(self, tmp_path):
        results = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout="first123\n"),
            MagicMock(returncode=0, stdout="origin\n"),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout="base123\n"),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout="second12\n"),
        ]
        with (
            patch("hopper.git.time.monotonic", return_value=0),
            patch("hopper.git.subprocess.run", side_effect=results),
        ):
            verdict = ship_landing_verdict(tmp_path)

        assert verdict.containment == "indeterminate"
        assert verdict.cause == "head_changed"

    def test_worktree_becoming_dirty_after_ancestry_fails_closed(self, tmp_path):
        results = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout="abc12345\n"),
            MagicMock(returncode=0, stdout="origin\n"),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout="base1234\n"),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout="abc12345\n"),
            MagicMock(returncode=0, stdout="?? late.txt\n"),
        ]
        with (
            patch("hopper.git.time.monotonic", return_value=0),
            patch("hopper.git.subprocess.run", side_effect=results),
        ):
            verdict = ship_landing_verdict(tmp_path)

        assert verdict.cleanliness == "dirty"
        assert verdict.containment == "indeterminate"
        assert verdict.cause == "cleanliness_dirty"

    def test_overall_monotonic_deadline_stops_before_next_probe(self, tmp_path):
        with (
            patch(
                "hopper.git.time.monotonic",
                side_effect=[0, 0, SHIP_LANDING_TIMEOUT_SEC + 1],
            ),
            patch(
                "hopper.git.subprocess.run", return_value=MagicMock(returncode=0, stdout="")
            ) as run,
        ):
            verdict = ship_landing_verdict(tmp_path)

        assert verdict.containment == "indeterminate"
        assert "overall ship landing deadline" in verdict.detail
        assert run.call_count == 1


class TestResolveDefaultBranch:
    def test_prefers_origin_main(self):
        result = MagicMock(returncode=0, stdout="abc123\n")

        with patch("subprocess.run", return_value=result) as mock_run:
            resolved, candidates = _resolve_default_branch("/repo", allow_local=True)

        assert resolved == "origin/main"
        assert candidates == ("origin/main", "origin/master", "main", "master")
        mock_run.assert_called_once()

    def test_falls_back_to_origin_master(self):
        results = [
            MagicMock(returncode=128, stdout=""),
            MagicMock(returncode=0, stdout="abc123\n"),
        ]

        with patch("subprocess.run", side_effect=results) as mock_run:
            resolved, candidates = _resolve_default_branch("/repo", allow_local=False)

        assert resolved == "origin/master"
        assert candidates == ("origin/main", "origin/master")
        assert [item.args[0][-1] for item in mock_run.call_args_list] == [
            "origin/main",
            "origin/master",
        ]

    def test_falls_back_to_local_main_when_allowed(self):
        results = [
            MagicMock(returncode=128, stdout=""),
            MagicMock(returncode=128, stdout=""),
            MagicMock(returncode=0, stdout="abc123\n"),
        ]

        with patch("subprocess.run", side_effect=results):
            resolved, candidates = _resolve_default_branch("/repo", allow_local=True)

        assert resolved == "main"
        assert candidates == ("origin/main", "origin/master", "main", "master")

    def test_falls_back_to_local_master(self):
        results = [
            MagicMock(returncode=128, stdout=""),
            MagicMock(returncode=128, stdout=""),
            MagicMock(returncode=128, stdout=""),
            MagicMock(returncode=0, stdout="abc123\n"),
        ]

        with patch("subprocess.run", side_effect=results):
            resolved, candidates = _resolve_default_branch("/repo", allow_local=True)

        assert resolved == "master"
        assert candidates == ("origin/main", "origin/master", "main", "master")

    def test_remote_only_does_not_probe_local_refs(self):
        result = MagicMock(returncode=128, stdout="")

        with patch("subprocess.run", return_value=result) as mock_run:
            resolved, candidates = _resolve_default_branch("/repo", allow_local=False)

        assert resolved is None
        assert candidates == ("origin/main", "origin/master")
        assert mock_run.call_count == 2


class TestIsDirty:
    def test_clean_repo(self):
        """Returns False for a clean repo."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""

        with patch("subprocess.run", return_value=mock_result):
            assert is_dirty("/repo") is False

    def test_dirty_repo(self):
        """Returns True when there are uncommitted changes."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = " M file.py\n"

        with patch("subprocess.run", return_value=mock_result):
            assert is_dirty("/repo") is True

    def test_git_not_found(self):
        """Returns None when git is not found."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert is_dirty("/repo") is None

    def test_nonzero_status_exit_is_unavailable(self, caplog):
        mock_result = MagicMock(returncode=128, stdout="", stderr="fatal")

        with patch("subprocess.run", return_value=mock_result):
            assert is_dirty("/repo") is None

        assert "git status --porcelain failed in /repo (exit 128)" in caplog.messages

    def test_timeout_is_unavailable_and_probe_is_bounded(self):
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git status", timeout=0.25),
        ) as run:
            assert is_dirty("/repo", timeout=0.25) is None

        assert run.call_args.kwargs["timeout"] == 0.25


class TestBranchExists:
    def test_returns_git_ref_existence(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            assert branch_exists("/repo", "hopper-test") is True
        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            assert branch_exists("/repo", "hopper-test") is False

    def test_timeout_is_unavailable_and_probe_is_bounded(self):
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git rev-parse", timeout=0.25),
        ) as run:
            assert branch_exists("/repo", "hopper-test", timeout=0.25) is None

        assert run.call_args.kwargs["timeout"] == 0.25


class TestDirtyStatus:
    def test_clean(self):
        mock_result = MagicMock()
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            assert dirty_status("/fake/repo") == ""

    def test_dirty(self):
        mock_result = MagicMock()
        mock_result.stdout = " M file.py\n M other.py\n"
        with patch("subprocess.run", return_value=mock_result):
            assert dirty_status("/fake/repo") == "M file.py\n M other.py"

    def test_git_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert dirty_status("/fake/repo") == ""


class TestCurrentBranch:
    def test_returns_branch_name(self):
        """Returns the current branch name."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "main\n"

        with patch("subprocess.run", return_value=mock_result):
            assert current_branch("/repo") == "main"

    def test_detached_head(self):
        """Returns None for detached HEAD."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "HEAD\n"

        with patch("subprocess.run", return_value=mock_result):
            assert current_branch("/repo") is None

    def test_failure(self):
        """Returns None on git command failure."""
        mock_result = MagicMock()
        mock_result.returncode = 128

        with patch("subprocess.run", return_value=mock_result):
            assert current_branch("/repo") is None

    def test_git_not_found(self):
        """Returns None when git is not found."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert current_branch("/repo") is None


class TestGetDiffStat:
    def test_returns_stat_output_for_main(self):
        """Returns diff --stat output for the resolved upstream base."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = (
            " file.py | 10 ++++------\n 1 file changed, 4 insertions(+), 6 deletions(-)\n"
        )

        with (
            patch(
                "hopper.git._resolve_default_branch",
                return_value=("origin/main", ("origin/main", "origin/master")),
            ),
            patch("subprocess.run", return_value=mock_result) as mock_run,
        ):
            result = get_diff_stat("/worktree")

        assert "file.py" in result
        assert "++++------" in result
        mock_run.assert_called_with(
            ["git", "diff", "--stat", "origin/main...HEAD"],
            cwd="/worktree",
            capture_output=True,
            text=True,
        )

    def test_falls_back_to_master(self):
        """Uses the fallback returned by default-branch resolution."""
        master_result = MagicMock()
        master_result.returncode = 0
        master_result.stdout = " file.py | 5 +++++\n"

        with (
            patch(
                "hopper.git._resolve_default_branch",
                return_value=("origin/master", ("origin/main", "origin/master")),
            ),
            patch("subprocess.run", return_value=master_result) as mock_run,
        ):
            result = get_diff_stat("/worktree")

        assert "file.py" in result
        mock_run.assert_called_once_with(
            ["git", "diff", "--stat", "origin/master...HEAD"],
            cwd="/worktree",
            capture_output=True,
            text=True,
        )

    def test_returns_empty_on_error(self):
        """Returns empty string when the diff command fails."""
        mock_result = MagicMock()
        mock_result.returncode = 128

        with (
            patch(
                "hopper.git._resolve_default_branch",
                return_value=("origin/main", ("origin/main", "origin/master")),
            ),
            patch("subprocess.run", return_value=mock_result),
        ):
            result = get_diff_stat("/worktree")

        assert result == ""

    def test_returns_empty_when_git_not_found(self):
        """Returns empty string when git is not found."""
        with patch("hopper.git._resolve_default_branch", side_effect=FileNotFoundError):
            result = get_diff_stat("/worktree")

        assert result == ""

    def test_returns_empty_for_no_changes(self):
        """Returns empty string when there are no changes."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""

        with (
            patch(
                "hopper.git._resolve_default_branch",
                return_value=("origin/main", ("origin/main", "origin/master")),
            ),
            patch("subprocess.run", return_value=mock_result),
        ):
            result = get_diff_stat("/worktree")

        assert result == ""

    def test_returns_empty_when_no_default_branch_resolves(self):
        with (
            patch("hopper.git._resolve_default_branch", return_value=(None, ())),
            patch("subprocess.run") as mock_run,
        ):
            result = get_diff_stat("/worktree")

        assert result == ""
        mock_run.assert_not_called()


class TestGetDiffNumstat:
    def test_returns_numstat_output_for_main(self):
        """Returns diff --numstat output for the resolved upstream base."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "10\t6\tfile.py\n"

        with (
            patch(
                "hopper.git._resolve_default_branch",
                return_value=("origin/main", ("origin/main", "origin/master")),
            ),
            patch("subprocess.run", return_value=mock_result) as mock_run,
        ):
            result = get_diff_numstat("/worktree")

        assert "file.py" in result
        mock_run.assert_called_with(
            ["git", "diff", "--numstat", "origin/main"],
            cwd="/worktree",
            capture_output=True,
            text=True,
        )

    def test_falls_back_to_master(self):
        """Uses the fallback returned by default-branch resolution."""
        master_result = MagicMock()
        master_result.returncode = 0
        master_result.stdout = "5\t0\tfile.py\n"

        with (
            patch(
                "hopper.git._resolve_default_branch",
                return_value=("origin/master", ("origin/main", "origin/master")),
            ),
            patch("subprocess.run", return_value=master_result) as mock_run,
        ):
            result = get_diff_numstat("/worktree")

        assert "file.py" in result
        mock_run.assert_called_once_with(
            ["git", "diff", "--numstat", "origin/master"],
            cwd="/worktree",
            capture_output=True,
            text=True,
        )

    def test_returns_empty_on_error(self):
        """Returns empty string when the diff command fails."""
        mock_result = MagicMock()
        mock_result.returncode = 128

        with (
            patch(
                "hopper.git._resolve_default_branch",
                return_value=("origin/main", ("origin/main", "origin/master")),
            ),
            patch("subprocess.run", return_value=mock_result),
        ):
            result = get_diff_numstat("/worktree")

        assert result == ""

    def test_returns_empty_when_git_not_found(self):
        """Returns empty string when git is not installed."""
        with patch("hopper.git._resolve_default_branch", side_effect=FileNotFoundError):
            result = get_diff_numstat("/worktree")

        assert result == ""

    def test_returns_empty_when_no_default_branch_resolves(self):
        with (
            patch("hopper.git._resolve_default_branch", return_value=(None, ())),
            patch("subprocess.run") as mock_run,
        ):
            result = get_diff_numstat("/worktree")

        assert result == ""
        mock_run.assert_not_called()


class TestRemoveWorktree:
    def test_success(self):
        """Removes worktree with correct git command."""
        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch("pathlib.Path.exists", side_effect=[True, False]),
            patch("subprocess.run", return_value=mock_result) as mock_run,
        ):
            result = remove_worktree("/repo", "/path/to/worktree")

        assert result is True
        mock_run.assert_called_once_with(
            ["git", "worktree", "remove", "--force", "/path/to/worktree"],
            cwd="/repo",
            capture_output=True,
            text=True,
        )

    def test_failure_returns_false(self, caplog):
        """Returns False when git and shutil cleanup both fail."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "fatal: not a working tree"

        caplog.set_level("WARNING")

        with (
            patch("pathlib.Path.exists", side_effect=[True, True]),
            patch("subprocess.run", return_value=mock_result),
            patch(
                "hopper.git.shutil.rmtree",
                side_effect=OSError("permission denied"),
            ),
        ):
            result = remove_worktree("/repo", "/path/to/worktree")

        assert result is False
        assert [record.getMessage() for record in caplog.records] == [
            "git worktree remove failed: fatal: not a working tree; "
            "shutil.rmtree failed: permission denied"
        ]

    def test_git_not_found(self):
        """Returns False when git is not installed."""
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("subprocess.run", side_effect=FileNotFoundError),
            patch("hopper.git.shutil.rmtree") as mock_rmtree,
        ):
            result = remove_worktree("/repo", "/path/to/worktree")

        assert result is False
        mock_rmtree.assert_not_called()

    def test_git_fails_shutil_succeeds_returns_true(self, caplog):
        """Returns True and warns once when shutil recovers a git failure."""
        git_result = MagicMock()
        git_result.returncode = 1
        git_result.stderr = "locked worktree"
        prune_result = MagicMock()
        prune_result.returncode = 0
        prune_result.stderr = ""

        caplog.set_level("WARNING")

        with (
            patch("pathlib.Path.exists", side_effect=[True, True]),
            patch("subprocess.run", side_effect=[git_result, prune_result]) as mock_run,
            patch("hopper.git.shutil.rmtree") as mock_rmtree,
        ):
            result = remove_worktree("/repo", "/path/to/worktree")

        assert result is True
        mock_rmtree.assert_called_once_with("/path/to/worktree")
        assert mock_run.call_count == 2
        assert mock_run.call_args_list == [
            call(
                ["git", "worktree", "remove", "--force", "/path/to/worktree"],
                cwd="/repo",
                capture_output=True,
                text=True,
            ),
            call(
                ["git", "worktree", "prune"],
                cwd="/repo",
                capture_output=True,
                text=True,
            ),
        ]
        assert len(caplog.records) == 1
        assert "locked worktree" in caplog.records[0].getMessage()
        assert "recovered" in caplog.records[0].getMessage()

    def test_git_fails_shutil_succeeds_prune_fails(self, caplog):
        """Returns True and logs one consolidated warning when recovery prune fails."""
        git_result = MagicMock()
        git_result.returncode = 1
        git_result.stderr = "locked worktree"
        prune_result = MagicMock()
        prune_result.returncode = 1
        prune_result.stderr = "stale metadata"

        caplog.set_level("WARNING")

        with (
            patch("pathlib.Path.exists", side_effect=[True, True]),
            patch("subprocess.run", side_effect=[git_result, prune_result]),
            patch("hopper.git.shutil.rmtree") as mock_rmtree,
        ):
            result = remove_worktree("/repo", "/path/to/worktree")

        assert result is True
        mock_rmtree.assert_called_once_with("/path/to/worktree")
        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "git worktree remove failed" in message
        assert "recovered via shutil.rmtree" in message
        assert "git worktree prune failed" in message

    def test_git_fails_shutil_fails_returns_false(self, caplog):
        """Returns False and logs one combined warning when both cleanup paths fail."""
        git_result = MagicMock()
        git_result.returncode = 1
        git_result.stderr = "fatal: cleanup blocked"

        caplog.set_level("WARNING")

        with (
            patch("pathlib.Path.exists", side_effect=[True, True]),
            patch("subprocess.run", return_value=git_result),
            patch("hopper.git.shutil.rmtree", side_effect=OSError("nope")),
        ):
            result = remove_worktree("/repo", "/path/to/worktree")

        assert result is False
        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "git worktree remove failed" in message
        assert "fatal: cleanup blocked" in message
        assert "shutil.rmtree failed" in message
        assert "nope" in message

    def test_path_does_not_exist_returns_true_no_git_call(self, caplog):
        """Returns True without calling git when the worktree path is already gone."""
        caplog.set_level("WARNING")

        with (
            patch("pathlib.Path.exists", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            result = remove_worktree("/repo", "/nonexistent")

        assert result is True
        mock_run.assert_not_called()
        assert caplog.records == []


class TestDurableShipQuarantine:
    @pytest.mark.parametrize(
        ("dirty", "unpushed", "expected"),
        [
            (True, (0, "a remote branch"), "dirty or unreadable"),
            (False, (None, None), "unknown"),
            (False, (2, "a remote branch"), "2 unpushed"),
        ],
    )
    def test_cleanup_authorization_retains_dirty_unpushed_or_unknown(
        self, dirty, unpushed, expected
    ):
        provenance = {"worktree": {"realpath": "/old"}}
        quarantine = {"quarantine_path": "/quarantine"}
        with (
            patch(
                "hopper.git.revalidate_worktree_provenance",
                return_value={"state": "match", "error": None},
            ),
            patch("hopper.git.is_dirty", return_value=dirty),
            patch("hopper.git.unpushed_commits", return_value=unpushed),
        ):
            result = authorize_quarantine_cleanup(provenance, quarantine, base_ref=None)

        assert result["authorized"] is False
        assert expected in result["error"]

    def test_quarantine_repairs_authorizes_and_removes_without_force_or_rmtree(self, tmp_path):
        project = _init_git_repo(tmp_path, name="quarantine-project")
        worktree = tmp_path / "quarantine-worktree"
        branch = "hopper-abcd2345"
        _run_git(project, "worktree", "add", "-b", branch, str(worktree), "main")
        provenance = capture_worktree_provenance(project, worktree)
        quarantine_path = tmp_path / f".abcd2345-quarantine-{'a' * 32}"
        quarantine = {
            "original_path": str(worktree),
            "quarantine_path": str(quarantine_path),
            "expected_identity": provenance["worktree"]["identity"],
        }

        assert quarantine_worktree(provenance, quarantine)["state"] == "renamed"
        assert repair_quarantined_worktree(provenance, quarantine)["state"] == "repaired"
        assert authorize_quarantine_cleanup(provenance, quarantine, base_ref="main") == {
            "authorized": True,
            "error": None,
        }

        real_run = subprocess.run
        with (
            patch("hopper.git.subprocess.run", side_effect=real_run) as run,
            patch("hopper.git.shutil.rmtree") as rmtree,
        ):
            result = remove_quarantined_worktree(provenance, quarantine)

        assert result == {"state": "removed", "error": None}
        rmtree.assert_not_called()
        commands = [item.args[0] for item in run.call_args_list]
        assert not any("--force" in command for command in commands)
        assert not any(
            command[:2] == ["git", "worktree"] and "prune" in command for command in commands
        )
        assert delete_branch_if_unchanged(provenance, base_ref="main")["state"] == "deleted"

    def test_repair_failure_does_not_expose_git_stderr(self, tmp_path):
        project = _init_git_repo(tmp_path, name="repair-failure-project")
        worktree = tmp_path / "repair-failure-worktree"
        _run_git(project, "worktree", "add", "-b", "hopper-abcd2345", str(worktree), "main")
        provenance = capture_worktree_provenance(project, worktree)
        quarantine_path = tmp_path / f".abcd2345-quarantine-{'a' * 32}"
        quarantine = {
            "original_path": str(worktree),
            "quarantine_path": str(quarantine_path),
            "expected_identity": provenance["worktree"]["identity"],
        }
        assert quarantine_worktree(provenance, quarantine)["state"] == "renamed"
        (quarantine_path / ".git").write_text("broken gitdir\n")

        raw_stderr = []
        real_run = subprocess.run

        def capture_repair_stderr(*args, **kwargs):
            result = real_run(*args, **kwargs)
            if args[0][3:5] == ["worktree", "repair"]:
                raw_stderr.append(result.stderr)
            return result

        with patch("hopper.git.subprocess.run", side_effect=capture_repair_stderr):
            result = repair_quarantined_worktree(provenance, quarantine)

        assert result == {"state": "retained", "error": "git worktree repair did not complete"}
        assert raw_stderr and "unable to locate repository" in raw_stderr[0]
        assert raw_stderr[0].strip() not in result["error"]

    def test_remove_failure_does_not_expose_git_stderr(self, tmp_path):
        project = _init_git_repo(tmp_path, name="remove-failure-project")
        worktree = tmp_path / "remove-failure-worktree"
        _run_git(project, "worktree", "add", "-b", "hopper-abcd2345", str(worktree), "main")
        provenance = capture_worktree_provenance(project, worktree)
        quarantine_path = tmp_path / f".abcd2345-quarantine-{'a' * 32}"
        quarantine = {
            "original_path": str(worktree),
            "quarantine_path": str(quarantine_path),
            "expected_identity": provenance["worktree"]["identity"],
        }
        assert quarantine_worktree(provenance, quarantine)["state"] == "renamed"
        assert repair_quarantined_worktree(provenance, quarantine)["state"] == "repaired"
        (quarantine_path / ".git").write_text("broken gitdir\n")

        raw_stderr = []
        real_run = subprocess.run

        def capture_remove_stderr(*args, **kwargs):
            result = real_run(*args, **kwargs)
            if args[0][:3] == ["git", "worktree", "remove"]:
                raw_stderr.append(result.stderr)
            return result

        with patch("hopper.git.subprocess.run", side_effect=capture_remove_stderr):
            result = remove_quarantined_worktree(provenance, quarantine)

        assert result == {
            "state": "retained",
            "error": "non-forcing git worktree removal did not complete",
        }
        assert raw_stderr and "validation failed" in raw_stderr[0]
        assert raw_stderr[0].strip() not in result["error"]

    def test_branch_delete_uses_landing_base_when_local_default_is_stale(self, stale_clone_factory):
        registered, local_sha, _upstream_sha = stale_clone_factory("main")
        branch = "hopper-ac1"
        _run_git(registered, "fetch", "origin")
        _run_git(registered, "switch", "-c", branch, local_sha)
        _run_git(registered, "commit", "--allow-empty", "-m", "feature")
        _run_git(registered, "merge", "--no-edit", "origin/main")
        provenance = capture_worktree_provenance(registered, registered)
        deleted_oid = provenance["branch_oid"]
        _run_git(registered, "push", "origin", f"{branch}:main")
        _run_git(registered, "fetch", "origin")
        assert (
            _probe_git(registered, "merge-base", "--is-ancestor", deleted_oid, "main").returncode
            == 1
        )
        assert (
            _probe_git(
                registered, "merge-base", "--is-ancestor", deleted_oid, "origin/main"
            ).returncode
            == 0
        )
        _run_git(registered, "switch", "main")

        result = delete_branch_if_unchanged(provenance, base_ref="origin/main")

        assert result == {"state": "deleted", "error": None}
        assert (
            _probe_git(registered, "rev-parse", "--verify", provenance["branch_ref"]).returncode
            != 0
        )
        assert (
            _probe_git(
                registered, "merge-base", "--is-ancestor", deleted_oid, "origin/main"
            ).returncode
            == 0
        )

    def test_branch_delete_uses_landing_base_when_branch_upstream_is_behind(
        self, upstream_clone_factory
    ):
        _remote, _publisher, registered = upstream_clone_factory("main", name="branch-behind")
        branch = "hopper-ac3"
        _run_git(registered, "switch", "-c", branch)
        _run_git(registered, "commit", "--allow-empty", "-m", "published feature")
        _run_git(registered, "push", "-u", "origin", branch)
        _run_git(registered, "commit", "--allow-empty", "-m", "later feature")
        provenance = capture_worktree_provenance(registered, registered)
        deleted_oid = provenance["branch_oid"]
        _run_git(registered, "push", "origin", f"{branch}:main")
        _run_git(registered, "fetch", "origin")
        assert (
            _probe_git(registered, "merge-base", "--is-ancestor", deleted_oid, "main").returncode
            == 1
        )
        assert (
            _probe_git(
                registered, "merge-base", "--is-ancestor", deleted_oid, f"origin/{branch}"
            ).returncode
            == 1
        )
        assert (
            _probe_git(
                registered, "merge-base", "--is-ancestor", deleted_oid, "origin/main"
            ).returncode
            == 0
        )
        _run_git(registered, "switch", "main")

        result = delete_branch_if_unchanged(provenance, base_ref="origin/main")

        assert result == {"state": "deleted", "error": None}
        assert (
            _probe_git(registered, "rev-parse", "--verify", provenance["branch_ref"]).returncode
            != 0
        )
        assert (
            _probe_git(
                registered, "merge-base", "--is-ancestor", deleted_oid, "origin/main"
            ).returncode
            == 0
        )

    def test_branch_delete_uses_local_landing_base_without_origin(self, tmp_path):
        project = _init_git_repo(tmp_path, name="local-landing-base")
        branch = "hopper-local-base"
        _run_git(project, "switch", "-c", branch)
        _run_git(project, "commit", "--allow-empty", "-m", "feature")
        provenance = capture_worktree_provenance(project, project)
        deleted_oid = provenance["branch_oid"]
        _run_git(project, "switch", "main")
        _run_git(project, "merge", "--ff-only", branch)

        result = delete_branch_if_unchanged(provenance, base_ref="main")

        assert result == {"state": "deleted", "error": None}
        assert (
            _probe_git(project, "rev-parse", "--verify", provenance["branch_ref"]).returncode != 0
        )
        assert (
            _probe_git(project, "merge-base", "--is-ancestor", deleted_oid, "main").returncode == 0
        )

    def test_branch_delete_retains_oid_not_contained_in_landing_base(self, tmp_path):
        project = _init_git_repo(tmp_path, name="not-contained")
        branch = "hopper-not-contained"
        _run_git(project, "switch", "-c", branch)
        _run_git(project, "commit", "--allow-empty", "-m", "feature")
        provenance = capture_worktree_provenance(project, project)
        _run_git(project, "switch", "main")

        result = delete_branch_if_unchanged(provenance, base_ref="main")

        assert result["state"] == "retained"
        assert (
            _run_git(project, "rev-parse", provenance["branch_ref"]).stdout.strip()
            == provenance["branch_oid"]
        )

    @pytest.mark.parametrize("change", ["deleted", "renamed"])
    def test_branch_delete_retains_when_landing_base_no_longer_resolves(
        self, upstream_clone_factory, change
    ):
        _remote, publisher, registered = upstream_clone_factory(
            "main", name=f"cleanup-base-{change}"
        )
        branch = f"hopper-base-{change}"
        _run_git(registered, "switch", "-c", branch)
        _run_git(registered, "commit", "--allow-empty", "-m", "feature")
        provenance = capture_worktree_provenance(registered, registered)
        _run_git(registered, "switch", "main")
        _remove_or_rename_upstream_default(publisher, "main", change)
        _run_git(registered, "fetch", "--prune", "origin")
        assert (
            _probe_git(
                registered,
                "merge-base",
                "--is-ancestor",
                provenance["branch_oid"],
                "origin/main",
            ).returncode
            == 128
        )

        result = delete_branch_if_unchanged(provenance, base_ref="origin/main")

        assert result["state"] == "retained"
        assert (
            _run_git(registered, "rev-parse", provenance["branch_ref"]).stdout.strip()
            == provenance["branch_oid"]
        )

    def test_branch_delete_retains_without_landing_base(self, tmp_path):
        project = _init_git_repo(tmp_path, name="missing-landing-base")
        branch = "hopper-no-base"
        _run_git(project, "switch", "-c", branch)
        provenance = capture_worktree_provenance(project, project)
        _run_git(project, "switch", "main")

        result = delete_branch_if_unchanged(provenance, base_ref=None)

        assert result["state"] == "retained"
        assert (
            _run_git(project, "rev-parse", provenance["branch_ref"]).stdout.strip()
            == provenance["branch_oid"]
        )

    def test_branch_delete_rejects_ref_advanced_to_another_contained_oid(self, tmp_path):
        project = _init_git_repo(tmp_path, name="advanced-ref")
        branch = "hopper-advanced"
        _run_git(project, "switch", "-c", branch)
        provenance = capture_worktree_provenance(project, project)
        _run_git(project, "commit", "--allow-empty", "-m", "advanced feature")
        advanced_oid = _run_git(project, "rev-parse", "HEAD").stdout.strip()
        _run_git(project, "switch", "main")
        _run_git(project, "merge", "--ff-only", branch)
        assert (
            _probe_git(project, "merge-base", "--is-ancestor", advanced_oid, "main").returncode == 0
        )

        result = delete_branch_if_unchanged(provenance, base_ref="main")

        assert result["state"] == "anomalous"
        assert result["error"] == "accepted branch ref is unknown or changed"
        assert (
            _run_git(project, "rev-parse", provenance["branch_ref"]).stdout.strip() == advanced_oid
        )

    def test_branch_delete_compare_and_delete_preserves_concurrently_moved_ref(self, tmp_path):
        project = _init_git_repo(tmp_path, name="concurrent-ref-move")
        branch = "hopper-race"
        _run_git(project, "switch", "-c", branch)
        _run_git(project, "commit", "--allow-empty", "-m", "feature")
        provenance = capture_worktree_provenance(project, project)
        _run_git(project, "switch", "main")
        _run_git(project, "merge", "--ff-only", branch)
        _run_git(project, "commit", "--allow-empty", "-m", "new ref target")
        moved_oid = _run_git(project, "rev-parse", "HEAD").stdout.strip()
        real_run = subprocess.run

        def move_before_delete(command, **kwargs):
            if command[1:] == [
                "update-ref",
                "-d",
                provenance["branch_ref"],
                provenance["branch_oid"],
            ]:
                real_run(
                    ["git", "update-ref", provenance["branch_ref"], moved_oid],
                    cwd=project,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            return real_run(command, **kwargs)

        with patch("hopper.git.subprocess.run", side_effect=move_before_delete):
            result = delete_branch_if_unchanged(provenance, base_ref="main")

        assert result["state"] == "anomalous"
        assert result["error"] != "accepted branch ref is unknown or changed"
        assert _run_git(project, "rev-parse", provenance["branch_ref"]).stdout.strip() == moved_oid

    def test_branch_delete_blocks_branch_registered_in_another_worktree(self, tmp_path):
        project = _init_git_repo(tmp_path, name="registered-branch")
        worktree = tmp_path / "registered-worktree"
        branch = "hopper-registered"
        _run_git(project, "worktree", "add", "-b", branch, str(worktree), "main")
        provenance = capture_worktree_provenance(project, worktree)

        result = delete_branch_if_unchanged(provenance, base_ref="main")

        assert result["state"] == "anomalous"
        assert (
            _run_git(project, "rev-parse", provenance["branch_ref"]).stdout.strip()
            == provenance["branch_oid"]
        )

    @pytest.mark.parametrize(
        ("failure_point", "ref_exists"),
        [
            ("initial_probe", True),
            ("containment_probe", True),
            ("compare_and_delete", True),
            ("post_delete_probe", False),
        ],
    )
    def test_branch_delete_returns_unknown_when_git_cannot_run(
        self, tmp_path, failure_point, ref_exists
    ):
        project = _init_git_repo(tmp_path, name=f"git-unavailable-{failure_point}")
        branch = "hopper-unavailable"
        _run_git(project, "switch", "-c", branch)
        provenance = capture_worktree_provenance(project, project)
        _run_git(project, "switch", "main")
        ref_probe_count = 0
        real_run = subprocess.run

        def fail_selected_command(command, **kwargs):
            nonlocal ref_probe_count
            if command[1:] == ["rev-parse", "--verify", "--quiet", provenance["branch_ref"]]:
                ref_probe_count += 1
                if failure_point == "initial_probe" and ref_probe_count == 1:
                    raise OSError("git unavailable")
                if failure_point == "post_delete_probe" and ref_probe_count == 2:
                    raise OSError("git unavailable")
            if failure_point == "containment_probe" and command[1:3] == [
                "merge-base",
                "--is-ancestor",
            ]:
                raise OSError("git unavailable")
            if failure_point == "compare_and_delete" and command[1] == "update-ref":
                raise OSError("git unavailable")
            return real_run(command, **kwargs)

        with patch("hopper.git.subprocess.run", side_effect=fail_selected_command):
            result = delete_branch_if_unchanged(provenance, base_ref="main")

        assert result["state"] == "unknown"
        ref = _probe_git(project, "rev-parse", "--verify", provenance["branch_ref"])
        assert (ref.returncode == 0) is ref_exists
        if ref_exists:
            assert ref.stdout.strip() == provenance["branch_oid"]

    def test_fetch_lock_identity_is_shared_by_linked_worktrees(self, tmp_path):
        project = _init_git_repo(tmp_path, name="lock-project")
        worktree = tmp_path / "lock-worktree"
        _run_git(project, "worktree", "add", "-b", "lock-branch", str(worktree), "main")

        with repository_fetch_lock(project) as project_identity:
            common = project_identity
        with repository_fetch_lock(worktree) as worktree_identity:
            assert worktree_identity == common

    def test_git_fails_but_path_gone_returns_true_with_prune(self, caplog):
        """Returns True and prunes stale metadata when git removed the path anyway."""
        git_result = MagicMock()
        git_result.returncode = 1
        git_result.stderr = "warning: already partially removed"
        prune_result = MagicMock()
        prune_result.returncode = 0
        prune_result.stderr = ""

        caplog.set_level("WARNING")

        with (
            patch("pathlib.Path.exists", side_effect=[True, False]),
            patch("subprocess.run", side_effect=[git_result, prune_result]) as mock_run,
        ):
            result = remove_worktree("/repo", "/path/to/worktree")

        assert result is True
        assert mock_run.call_args_list == [
            call(
                ["git", "worktree", "remove", "--force", "/path/to/worktree"],
                cwd="/repo",
                capture_output=True,
                text=True,
            ),
            call(
                ["git", "worktree", "prune"],
                cwd="/repo",
                capture_output=True,
                text=True,
            ),
        ]
        assert caplog.records == []


class TestDeleteBranch:
    def test_success(self):
        """Deletes branch with correct git command."""
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = delete_branch("/repo", "hopper-abc12345")

        assert result is True
        mock_run.assert_called_once_with(
            ["git", "branch", "-d", "hopper-abc12345"],
            cwd="/repo",
            capture_output=True,
            text=True,
        )

    def test_failure_returns_false(self):
        """Returns False when git command fails."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "error: branch not fully merged"

        with patch("subprocess.run", return_value=mock_result):
            result = delete_branch("/repo", "hopper-abc12345")

        assert result is False

    def test_git_not_found(self):
        """Returns False when git is not installed."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = delete_branch("/repo", "hopper-abc12345")

        assert result is False


class TestCommitAllIntegration:
    def test_commit_all_dirty_repo_creates_commit(self, tmp_path):
        repo_dir = _init_git_repo(tmp_path)
        (repo_dir / "README.md").write_text("changed\n")
        (repo_dir / "new.txt").write_text("new\n")

        assert commit_all(str(repo_dir), "snapshot dirty tree") == (True, None)

        message = _run_git(repo_dir, "log", "-1", "--pretty=%B").stdout.strip()
        assert message == "snapshot dirty tree"
        files = _run_git(repo_dir, "show", "--name-only", "--pretty=", "HEAD").stdout.splitlines()
        assert "README.md" in files
        assert "new.txt" in files

    def test_commit_all_clean_repo_returns_false(self, tmp_path):
        repo_dir = _init_git_repo(tmp_path)

        success, error = commit_all(str(repo_dir), "nothing to commit")

        assert success is False
        assert error == "git commit failed: exit code 1"

        message = _run_git(repo_dir, "log", "-1", "--pretty=%B").stdout.strip()
        assert message == "init"

    def test_unmerged_snapshotted_branch_survives_delete_branch(self, tmp_path):
        repo_dir = _init_git_repo(tmp_path)
        base_branch = _run_git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

        _run_git(repo_dir, "checkout", "-b", "hopper-snapshot")
        (repo_dir / "snapshot.txt").write_text("snapshot\n")
        assert commit_all(str(repo_dir), "hopper snapshot") == (True, None)
        _run_git(repo_dir, "checkout", base_branch)

        assert delete_branch(str(repo_dir), "hopper-snapshot") is False
        branches = _run_git(repo_dir, "branch", "--list", "hopper-snapshot").stdout
        assert "hopper-snapshot" in branches


class TestCommitAllFailures:
    def test_add_failure_returns_operation_detail(self):
        add_result = MagicMock(returncode=128, stderr="fatal: index.lock exists")

        with patch("subprocess.run", return_value=add_result):
            assert commit_all("/repo", "snapshot") == (
                False,
                "git add -A failed: fatal: index.lock exists",
            )

    def test_commit_failure_returns_operation_detail(self):
        add_result = MagicMock(returncode=0, stderr="")
        commit_result = MagicMock(returncode=1, stderr="commit rejected")

        with patch("subprocess.run", side_effect=[add_result, commit_result]):
            assert commit_all("/repo", "snapshot") == (
                False,
                "git commit failed: commit rejected",
            )

    def test_missing_git_returns_detail(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert commit_all("/repo", "snapshot") == (False, "git command not found")

    def test_subprocess_exception_returns_detail(self):
        with patch("subprocess.run", side_effect=subprocess.SubprocessError("boom")):
            assert commit_all("/repo", "snapshot") == (
                False,
                "git commit failed: boom",
            )


class TestHeadSha:
    def test_returns_full_head_sha(self, tmp_path):
        repo_dir = _init_git_repo(tmp_path)
        expected = _run_git(repo_dir, "rev-parse", "HEAD").stdout.strip()

        assert head_sha(str(repo_dir)) == expected
        assert len(expected) == 40

    def test_returns_none_when_rev_parse_fails(self):
        result = MagicMock(returncode=128, stdout="")

        with patch("subprocess.run", return_value=result):
            assert head_sha("/repo") is None

    def test_returns_none_when_git_is_missing(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert head_sha("/repo") is None


class TestQuarantineDirtyRepoIntegration:
    def test_quarantines_tracked_and_untracked(self, tmp_path):
        repo_dir = _init_git_repo(tmp_path)
        (repo_dir / "README.md").write_text("changed\n")
        (repo_dir / "new.txt").write_text("new\n")
        original_branch = _run_git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        original_head = _run_git(repo_dir, "rev-parse", "HEAD").stdout.strip()

        branch = quarantine_dirty_repo(str(repo_dir), "test-id")

        assert branch is not None
        assert branch.startswith("hopper-quarantine-")
        assert is_dirty(str(repo_dir)) is False
        assert (
            _run_git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            == original_branch
        )
        assert _run_git(repo_dir, "rev-parse", "HEAD").stdout.strip() == original_head
        _run_git(repo_dir, "rev-parse", "--verify", branch)
        files = _run_git(repo_dir, "show", "--name-only", "--pretty=format:", branch).stdout
        assert "README.md" in files.splitlines()
        assert "new.txt" in files.splitlines()
        message = _run_git(repo_dir, "log", "-1", "--format=%s", branch).stdout.strip()
        assert message == "hopper: quarantined dirty project repo blocking lode test-id"

    def test_precondition_merge_in_progress_returns_none(self, tmp_path):
        repo_dir = _init_git_repo(tmp_path)
        (repo_dir / "README.md").write_text("changed\n")
        head = _run_git(repo_dir, "rev-parse", "HEAD").stdout.strip()
        (repo_dir / ".git" / "MERGE_HEAD").write_text(f"{head}\n")

        assert quarantine_dirty_repo(str(repo_dir), "test-id") is None

        branches = _run_git(repo_dir, "branch", "--list", "hopper-quarantine-*").stdout.strip()
        assert branches == ""
        assert is_dirty(str(repo_dir)) is True
        assert (repo_dir / "README.md").read_text() == "changed\n"

    def test_precondition_detached_head_returns_none(self, tmp_path):
        repo_dir = _init_git_repo(tmp_path)
        head = _run_git(repo_dir, "rev-parse", "HEAD").stdout.strip()
        _run_git(repo_dir, "checkout", head)
        (repo_dir / "README.md").write_text("changed\n")

        assert quarantine_dirty_repo(str(repo_dir), "test-id") is None

        branches = _run_git(repo_dir, "branch", "--list", "hopper-quarantine-*").stdout.strip()
        assert branches == ""
        assert is_dirty(str(repo_dir)) is True
        assert (repo_dir / "README.md").read_text() == "changed\n"

    def test_commit_failure_leaves_state_intact(self, tmp_path):
        repo_dir = _init_git_repo(tmp_path)
        (repo_dir / "README.md").write_text("changed\n")
        (repo_dir / "new.txt").write_text("new\n")

        with patch("hopper.git.commit_all", return_value=(False, "commit failed")):
            assert quarantine_dirty_repo(str(repo_dir), "test-id") is None

        assert (repo_dir / "README.md").read_text() == "changed\n"
        assert (repo_dir / "new.txt").read_text() == "new\n"
        assert is_dirty(str(repo_dir)) is True
        current = _run_git(repo_dir, "branch", "--show-current").stdout.strip()
        assert current.startswith("hopper-quarantine-")
        branches = _run_git(repo_dir, "branch", "--list", "hopper-quarantine-*").stdout
        assert current in branches


class TestQuarantineDirtyRepo:
    def test_git_missing_returns_none(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert quarantine_dirty_repo("/repo", "test-id") is None


class TestRemoveWorktreeIntegration:
    def test_removes_dirty_worktree_end_to_end(self, tmp_path):
        if shutil.which("git") is None:
            pytest.skip("git not on PATH")

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )

        (repo_dir / "README.md").write_text("init")
        subprocess.run(
            ["git", "add", "."],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )

        worktree_path = tmp_path / "wt"
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", "feature-branch"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )

        (worktree_path / "untracked.txt").write_text("dirty")
        (worktree_path / "README.md").write_text("dirty tracked change")

        with patch("hopper.git.shutil.rmtree", wraps=shutil.rmtree) as rmtree_spy:
            result = remove_worktree(str(repo_dir), str(worktree_path))

        assert result is True
        rmtree_spy.assert_not_called()
        assert not worktree_path.exists()

        list_result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        assert str(worktree_path) not in list_result.stdout
        assert "prunable" not in list_result.stdout


class TestUnpushedCommits:
    """The count that decides whether killing a lode can strand a day of work."""

    def _clone_with_worktree(self, tmp_path, *, branch="main"):
        remote = _init_git_repo(tmp_path, name="origin.git", branch=branch, bare=True)
        seed = _init_git_repo(tmp_path, name="seed", branch=branch)
        _run_git(seed, "remote", "add", "origin", str(remote))
        _run_git(seed, "push", "-u", "origin", branch)

        clone = tmp_path / "clone"
        _run_git(tmp_path, "clone", str(remote), str(clone))
        _run_git(clone, "config", "user.email", "test@example.com")
        _run_git(clone, "config", "user.name", "Test User")

        worktree = tmp_path / "wt"
        _run_git(clone, "worktree", "add", str(worktree), "-b", "hopper-testid11")
        return clone, worktree

    def _commit(self, worktree, name):
        (worktree / name).write_text(name)
        _run_git(worktree, "add", ".")
        _run_git(worktree, "commit", "-m", name)

    def test_counts_commits_that_exist_only_in_the_worktree(self, tmp_path):
        _clone, worktree = self._clone_with_worktree(tmp_path)
        for name in ("one.txt", "two.txt", "three.txt"):
            self._commit(worktree, name)

        assert unpushed_commits(str(worktree)) == (3, "a remote branch")

    def test_zero_when_the_branch_adds_nothing(self, tmp_path):
        _clone, worktree = self._clone_with_worktree(tmp_path)

        assert unpushed_commits(str(worktree)) == (0, "a remote branch")

    def test_local_main_does_not_absolve_unpushed_work(self, tmp_path):
        """Merged-but-never-pushed is the dangerous case a local base would clear."""
        clone, worktree = self._clone_with_worktree(tmp_path)
        self._commit(worktree, "one.txt")
        _run_git(clone, "merge", "--ff-only", "hopper-testid11")

        assert unpushed_commits(str(worktree)) == (1, "a remote branch")

    def test_a_pushed_branch_clears_the_count_without_being_merged(self, tmp_path):
        """A branch pushed for safety is safe; refusing there trains people to --force."""
        _clone, worktree = self._clone_with_worktree(tmp_path)
        self._commit(worktree, "one.txt")
        _run_git(worktree, "push", "-u", "origin", "hopper-testid11")

        assert unpushed_commits(str(worktree)) == (0, "a remote branch")

    def test_falls_back_to_a_local_base_without_any_remote(self, tmp_path):
        repo = _init_git_repo(tmp_path, name="solo", branch="main")
        worktree = tmp_path / "solo-wt"
        _run_git(repo, "worktree", "add", str(worktree), "-b", "hopper-testid11")
        self._commit(worktree, "one.txt")

        assert unpushed_commits(str(worktree)) == (1, "main")

    def test_unknown_rather_than_zero_when_no_base_resolves(self, tmp_path):
        repo = _init_git_repo(tmp_path, name="odd", branch="trunk")
        worktree = tmp_path / "odd-wt"
        _run_git(repo, "worktree", "add", str(worktree), "-b", "hopper-testid11")
        self._commit(worktree, "one.txt")

        assert unpushed_commits(str(worktree)) == (None, None)

    def test_unknown_when_the_path_is_not_a_worktree(self, tmp_path):
        assert unpushed_commits(str(tmp_path / "gone")) == (None, None)

    def test_unknown_when_the_count_command_fails(self, tmp_path):
        _clone, worktree = self._clone_with_worktree(tmp_path)

        with patch("hopper.git.subprocess.run") as run:
            run.side_effect = [
                MagicMock(returncode=0, stdout="refs/remotes/origin/main\n"),
                MagicMock(returncode=128, stdout="", stderr="fatal: bad revision"),
            ]
            assert unpushed_commits(str(worktree)) == (None, "a remote branch")

    def test_a_slow_host_degrades_to_unknown_rather_than_spending_the_budget(self, tmp_path):
        """This runs inside an ssh deadline; blowing it is how a live lode reads gone."""
        _clone, worktree = self._clone_with_worktree(tmp_path)

        with patch("hopper.git.subprocess.run") as run:
            run.side_effect = [
                MagicMock(returncode=0, stdout="refs/remotes/origin/main\n"),
                subprocess.TimeoutExpired(cmd="git rev-list", timeout=2.0),
            ]
            assert unpushed_commits(str(worktree)) == (None, "a remote branch")

    def test_probe_calls_are_bounded(self, tmp_path):
        _clone, worktree = self._clone_with_worktree(tmp_path)

        with patch("hopper.git.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="1\n")
            unpushed_commits(str(worktree), timeout=0.25)

        assert [call.kwargs["timeout"] for call in run.call_args_list] == [0.25, 0.25]
