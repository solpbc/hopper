"""Tests for the git utilities module."""

from unittest.mock import MagicMock, patch

from hopper.git import create_worktree, current_branch, is_dirty


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
            capture_output=True,
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
