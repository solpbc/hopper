# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the git utilities module."""

from unittest.mock import MagicMock, patch

from hopper.git import (
    create_worktree,
    current_branch,
    delete_branch,
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

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = remove_worktree("/repo", "/path/to/worktree")

        assert result is True
        mock_run.assert_called_once_with(
            ["git", "worktree", "remove", "/path/to/worktree"],
            cwd="/repo",
            capture_output=True,
            text=True,
        )

    def test_failure_returns_false(self):
        """Returns False when git command fails."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "fatal: not a working tree"

        with patch("subprocess.run", return_value=mock_result):
            result = remove_worktree("/repo", "/path/to/worktree")

        assert result is False

    def test_git_not_found(self):
        """Returns False when git is not installed."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = remove_worktree("/repo", "/path/to/worktree")

        assert result is False


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
