# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the pyenv module."""

import os
from unittest.mock import patch

from hopper.pyenv import (
    create_venv,
    get_venv_env,
    has_pyproject,
    install_editable,
    setup_worktree_venv,
)


class TestHasPyproject:
    def test_returns_true_when_exists(self, tmp_path):
        """Returns True when pyproject.toml exists."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        assert has_pyproject(tmp_path) is True

    def test_returns_false_when_missing(self, tmp_path):
        """Returns False when pyproject.toml does not exist."""
        assert has_pyproject(tmp_path) is False


class TestCreateVenv:
    def test_creates_venv_directory(self, tmp_path):
        """Creates .venv directory in worktree."""
        result = create_venv(tmp_path)
        assert result is True
        assert (tmp_path / ".venv").is_dir()
        assert (tmp_path / ".venv" / "bin" / "python").exists()

    def test_returns_false_on_failure(self, tmp_path):
        """Returns False when venv creation fails."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "error"
            result = create_venv(tmp_path)
        assert result is False

    def test_returns_false_on_exception(self, tmp_path):
        """Returns False when subprocess raises exception."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = create_venv(tmp_path)
        assert result is False


class TestInstallEditable:
    def test_returns_false_when_pip_missing(self, tmp_path):
        """Returns False when pip is not found."""
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        # No pip executable
        result = install_editable(tmp_path)
        assert result is False

    def test_calls_pip_install(self, tmp_path):
        """Calls pip install with correct arguments."""
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        pip_path = venv_bin / "pip"
        pip_path.write_text("#!/bin/sh\n")
        pip_path.chmod(0o755)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            result = install_editable(tmp_path)

        assert result is True
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == [str(pip_path), "install", "-e", ".[dev]"]
        assert call_args[1]["cwd"] == str(tmp_path)

    def test_returns_false_on_pip_failure(self, tmp_path):
        """Returns False when pip install fails."""
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        pip_path = venv_bin / "pip"
        pip_path.write_text("#!/bin/sh\n")
        pip_path.chmod(0o755)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "error"
            result = install_editable(tmp_path)

        assert result is False


class TestSetupWorktreeVenv:
    def test_returns_true_if_venv_exists(self, tmp_path):
        """Returns True immediately if .venv already exists."""
        (tmp_path / ".venv").mkdir()
        result = setup_worktree_venv(tmp_path)
        assert result is True

    def test_creates_and_installs(self, tmp_path):
        """Creates venv and installs editable package."""
        with (
            patch("hopper.pyenv.create_venv", return_value=True) as mock_create,
            patch("hopper.pyenv.install_editable", return_value=True) as mock_install,
        ):
            result = setup_worktree_venv(tmp_path)

        assert result is True
        mock_create.assert_called_once_with(tmp_path)
        mock_install.assert_called_once_with(tmp_path)

    def test_returns_false_if_create_fails(self, tmp_path):
        """Returns False if venv creation fails."""
        with patch("hopper.pyenv.create_venv", return_value=False):
            result = setup_worktree_venv(tmp_path)
        assert result is False

    def test_returns_false_if_install_fails(self, tmp_path):
        """Returns False if pip install fails."""
        with (
            patch("hopper.pyenv.create_venv", return_value=True),
            patch("hopper.pyenv.install_editable", return_value=False),
        ):
            result = setup_worktree_venv(tmp_path)
        assert result is False


class TestGetVenvEnv:
    def test_prepends_venv_bin_to_path(self, tmp_path):
        """Prepends .venv/bin to PATH."""
        base_env = {"PATH": "/usr/bin:/bin"}
        result = get_venv_env(tmp_path, base_env)

        expected_bin = str(tmp_path / ".venv" / "bin")
        assert result["PATH"].startswith(expected_bin + ":")
        assert result["PATH"] == f"{expected_bin}:/usr/bin:/bin"

    def test_sets_virtual_env(self, tmp_path):
        """Sets VIRTUAL_ENV to the venv path."""
        base_env = {"PATH": "/usr/bin"}
        result = get_venv_env(tmp_path, base_env)

        assert result["VIRTUAL_ENV"] == str(tmp_path / ".venv")

    def test_uses_os_environ_if_no_base(self, tmp_path):
        """Uses os.environ if base_env is None."""
        with patch.dict(os.environ, {"PATH": "/system/bin", "HOME": "/home/test"}):
            result = get_venv_env(tmp_path, None)

        assert "/system/bin" in result["PATH"]
        assert result["HOME"] == "/home/test"

    def test_handles_empty_path(self, tmp_path):
        """Handles empty PATH gracefully."""
        base_env = {"OTHER": "value"}
        result = get_venv_env(tmp_path, base_env)

        expected_bin = str(tmp_path / ".venv" / "bin")
        assert result["PATH"] == expected_bin

    def test_preserves_other_vars(self, tmp_path):
        """Preserves other environment variables."""
        base_env = {"PATH": "/usr/bin", "HOPPER_SID": "test-123", "CUSTOM": "value"}
        result = get_venv_env(tmp_path, base_env)

        assert result["HOPPER_SID"] == "test-123"
        assert result["CUSTOM"] == "value"
