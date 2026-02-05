# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Python virtual environment management for worktrees."""

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def has_pyproject(worktree_path: Path) -> bool:
    """Check if worktree has a pyproject.toml file.

    Args:
        worktree_path: Path to the worktree directory.

    Returns:
        True if pyproject.toml exists, False otherwise.
    """
    return (worktree_path / "pyproject.toml").exists()


def create_venv(worktree_path: Path) -> bool:
    """Create a virtual environment in the worktree.

    Args:
        worktree_path: Path to the worktree directory.

    Returns:
        True on success, False on failure.
    """
    venv_path = worktree_path / ".venv"
    try:
        result = subprocess.run(
            [sys.executable, "-m", "venv", str(venv_path)],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(f"venv creation failed: {result.stderr.strip()}")
            return False
        return True
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.error(f"venv creation failed: {e}")
        return False


def install_editable(worktree_path: Path) -> bool:
    """Install the project in editable mode with dev dependencies.

    Args:
        worktree_path: Path to the worktree directory.

    Returns:
        True on success, False on failure.
    """
    venv_pip = worktree_path / ".venv" / "bin" / "pip"
    if not venv_pip.exists():
        logger.error(f"pip not found: {venv_pip}")
        return False

    try:
        result = subprocess.run(
            [str(venv_pip), "install", "-e", ".[dev]"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(f"pip install failed: {result.stderr.strip()}")
            return False
        return True
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.error(f"pip install failed: {e}")
        return False


def setup_worktree_venv(worktree_path: Path) -> bool:
    """Set up a virtual environment for a worktree.

    Creates .venv and installs the project in editable mode.
    Idempotent: returns True if venv already exists.

    Args:
        worktree_path: Path to the worktree directory.

    Returns:
        True on success, False on failure.
    """
    venv_path = worktree_path / ".venv"

    # Already set up
    if venv_path.is_dir():
        return True

    if not create_venv(worktree_path):
        return False

    if not install_editable(worktree_path):
        return False

    return True


def get_venv_env(worktree_path: Path, base_env: dict | None = None) -> dict:
    """Get environment dict with venv activated.

    Prepends .venv/bin to PATH and sets VIRTUAL_ENV.

    Args:
        worktree_path: Path to the worktree directory.
        base_env: Base environment dict. Uses os.environ if None.

    Returns:
        Modified environment dict.
    """
    import os

    env = dict(base_env) if base_env else os.environ.copy()

    venv_path = worktree_path / ".venv"
    venv_bin = venv_path / "bin"

    # Prepend venv bin to PATH
    current_path = env.get("PATH", "")
    env["PATH"] = f"{venv_bin}:{current_path}" if current_path else str(venv_bin)

    # Set VIRTUAL_ENV
    env["VIRTUAL_ENV"] = str(venv_path)

    return env
