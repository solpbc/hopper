# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the git utilities module."""

import shutil
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from hopper.git import (
    commit_all,
    create_worktree,
    current_branch,
    delete_branch,
    dirty_status,
    get_diff_numstat,
    get_diff_stat,
    head_sha,
    is_dirty,
    quarantine_dirty_repo,
    remove_worktree,
)


def _run_git(repo_dir, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git_repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _run_git(repo_dir, "init")
    _run_git(repo_dir, "config", "user.email", "test@example.com")
    _run_git(repo_dir, "config", "user.name", "Test User")
    (repo_dir / "README.md").write_text("init\n")
    _run_git(repo_dir, "add", ".")
    _run_git(repo_dir, "commit", "-m", "init")
    return repo_dir


class TestCreateWorktree:
    def test_success(self, tmp_path):
        """Creates worktree with correct git command."""
        worktree_path = tmp_path / "worktree"
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = create_worktree("/repo", worktree_path, "hopper-abc12345")

        assert result is True
        mock_run.assert_called_once_with(
            ["git", "worktree", "add", str(worktree_path), "-b", "hopper-abc12345"],
            cwd="/repo",
            text=True,
        )

    def test_failure_returns_false(self, tmp_path):
        """Returns False when git command fails."""
        worktree_path = tmp_path / "worktree"
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "fatal: already exists"

        with patch("subprocess.run", return_value=mock_result):
            result = create_worktree("/repo", worktree_path, "hopper-abc12345")

        assert result is False

    def test_git_not_found(self, tmp_path):
        """Returns False when git is not installed."""
        worktree_path = tmp_path / "worktree"

        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = create_worktree("/repo", worktree_path, "hopper-abc12345")

        assert result is False


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
        """Returns True (assumes dirty) when git is not found."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert is_dirty("/repo") is True

    def test_nonzero_status_exit_is_conservatively_dirty(self, caplog):
        mock_result = MagicMock(returncode=128, stdout="", stderr="fatal")

        with patch("subprocess.run", return_value=mock_result):
            assert is_dirty("/repo") is True

        assert "git status --porcelain failed in /repo (exit 128)" in caplog.messages


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
        """Returns diff --stat output when comparing to main."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = (
            " file.py | 10 ++++------\n 1 file changed, 4 insertions(+), 6 deletions(-)\n"
        )

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = get_diff_stat("/worktree")

        assert "file.py" in result
        assert "++++------" in result
        mock_run.assert_called_with(
            ["git", "diff", "--stat", "main...HEAD"],
            cwd="/worktree",
            capture_output=True,
            text=True,
        )

    def test_falls_back_to_master(self):
        """Falls back to master when main doesn't exist."""
        main_result = MagicMock()
        main_result.returncode = 128  # main doesn't exist

        master_result = MagicMock()
        master_result.returncode = 0
        master_result.stdout = " file.py | 5 +++++\n"

        def mock_run(cmd, **kwargs):
            if "main...HEAD" in cmd:
                return main_result
            return master_result

        with patch("subprocess.run", side_effect=mock_run):
            result = get_diff_stat("/worktree")

        assert "file.py" in result

    def test_returns_empty_on_error(self):
        """Returns empty string when both main and master fail."""
        mock_result = MagicMock()
        mock_result.returncode = 128

        with patch("subprocess.run", return_value=mock_result):
            result = get_diff_stat("/worktree")

        assert result == ""

    def test_returns_empty_when_git_not_found(self):
        """Returns empty string when git is not found."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = get_diff_stat("/worktree")

        assert result == ""

    def test_returns_empty_for_no_changes(self):
        """Returns empty string when there are no changes."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""

        with patch("subprocess.run", return_value=mock_result):
            result = get_diff_stat("/worktree")

        assert result == ""


class TestGetDiffNumstat:
    def test_returns_numstat_output_for_main(self):
        """Returns diff --numstat output when comparing to main."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "10\t6\tfile.py\n"

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = get_diff_numstat("/worktree")

        assert "file.py" in result
        mock_run.assert_called_with(
            ["git", "diff", "--numstat", "main"],
            cwd="/worktree",
            capture_output=True,
            text=True,
        )

    def test_falls_back_to_master(self):
        """Falls back to master when main doesn't exist."""
        main_result = MagicMock()
        main_result.returncode = 128

        master_result = MagicMock()
        master_result.returncode = 0
        master_result.stdout = "5\t0\tfile.py\n"

        def mock_run(cmd, **kwargs):
            if "main" in cmd:
                return main_result
            return master_result

        with patch("subprocess.run", side_effect=mock_run):
            result = get_diff_numstat("/worktree")

        assert "file.py" in result

    def test_returns_empty_on_error(self):
        """Returns empty string when both main and master fail."""
        mock_result = MagicMock()
        mock_result.returncode = 128

        with patch("subprocess.run", return_value=mock_result):
            result = get_diff_numstat("/worktree")

        assert result == ""

    def test_returns_empty_when_git_not_found(self):
        """Returns empty string when git is not installed."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = get_diff_numstat("/worktree")

        assert result == ""


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
            ["git", "init"],
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
