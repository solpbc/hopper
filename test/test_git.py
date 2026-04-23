# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the git utilities module."""

import shutil
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from hopper.git import (
    create_worktree,
    current_branch,
    delete_branch,
    dirty_status,
    get_diff_numstat,
    get_diff_stat,
    is_dirty,
    remove_worktree,
)


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
        mock_result.stdout = ""

        with patch("subprocess.run", return_value=mock_result):
            assert is_dirty("/repo") is False

    def test_dirty_repo(self):
        """Returns True when there are uncommitted changes."""
        mock_result = MagicMock()
        mock_result.stdout = " M file.py\n"

        with patch("subprocess.run", return_value=mock_result):
            assert is_dirty("/repo") is True

    def test_git_not_found(self):
        """Returns True (assumes dirty) when git is not found."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert is_dirty("/repo") is True


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
