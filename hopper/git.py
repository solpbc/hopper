# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Git utilities for hopper."""

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def create_worktree(repo_dir: str, worktree_path: Path, branch_name: str) -> bool:
    """Create a git worktree with a new branch.

    Args:
        repo_dir: Path to the main git repository.
        worktree_path: Where to place the worktree.
        branch_name: Name for the new branch.

    Returns:
        True on success, False on failure.
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", branch_name],
            cwd=repo_dir,
            text=True,
        )
        if result.returncode != 0:
            logger.error(f"git worktree add failed (exit {result.returncode})")
            return False
        return True
    except FileNotFoundError:
        logger.error("git command not found")
        return False


def is_dirty(repo_dir: str) -> bool:
    """Check if a git repo has uncommitted changes.

    Args:
        repo_dir: Path to the git repository.

    Returns:
        True if the repo has uncommitted changes, False if clean.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())
    except (FileNotFoundError, subprocess.SubprocessError):
        return True  # Assume dirty if we can't check


def dirty_status(repo_dir: str) -> str:
    """Get the porcelain status output for a git repo.

    Args:
        repo_dir: Path to the git repository.

    Returns:
        Porcelain output string if dirty, empty string if clean or on error.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""  # Fail open - process runner's is_dirty() is the safety net


def current_branch(repo_dir: str) -> str | None:
    """Get the current branch name of a git repo.

    Args:
        repo_dir: Path to the git repository.

    Returns:
        Branch name, or None if detached HEAD or error.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        branch = result.stdout.strip()
        return branch if branch != "HEAD" else None
    except (FileNotFoundError, subprocess.SubprocessError):
        return None


def get_diff_stat(worktree_path: str) -> str:
    """Get diff stat output comparing worktree branch to main/master.

    Args:
        worktree_path: Path to the git worktree.

    Returns:
        The diff --stat output as a string, or empty string on error.
    """
    # Try main first, fall back to master
    for base in ("main", "master"):
        try:
            result = subprocess.run(
                ["git", "diff", "--stat", f"{base}...HEAD"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.SubprocessError):
            pass
    return ""


def get_diff_numstat(worktree_path: str) -> str:
    """Get diff numstat output comparing worktree to main/master.

    Args:
        worktree_path: Path to the git worktree.

    Returns:
        The diff --numstat output as a string, or empty string on error.
    """
    for base in ("main", "master"):
        try:
            result = subprocess.run(
                ["git", "diff", "--numstat", base],
                cwd=worktree_path,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.SubprocessError):
            pass
    return ""


def remove_worktree(repo_dir: str, worktree_path: str) -> bool:
    """Remove a git worktree.

    Forces removal with ``git worktree remove --force`` and falls back to
    ``shutil.rmtree()`` plus ``git worktree prune`` if git leaves an on-disk
    orphan.

    Args:
        repo_dir: Path to the main git repository.
        worktree_path: Path to worktree to remove.

    Returns:
        True when cleanup succeeds. This is idempotent and returns True without
        invoking git if the path is already missing. Returns False if the git
        binary is missing (no shutil fallback is attempted) or if both git
        removal and the shutil fallback fail.
    """
    if not Path(worktree_path).exists():
        return True

    try:
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_path],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        logger.warning("git command not found")
        return False

    git_err = result.stderr.strip()
    git_failed = result.returncode != 0

    if not Path(worktree_path).exists():
        if git_failed:
            try:
                prune_result = subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=repo_dir,
                    capture_output=True,
                    text=True,
                )
                if prune_result.returncode != 0:
                    logger.warning(f"git worktree prune failed: {prune_result.stderr.strip()}")
            except FileNotFoundError:
                logger.warning("git command not found")
        return True

    try:
        shutil.rmtree(worktree_path)
        shutil_err = None
    except OSError as err:
        shutil_err = str(err)

    if shutil_err is not None:
        if git_failed:
            logger.warning(
                f"git worktree remove failed: {git_err}; shutil.rmtree failed: {shutil_err}"
            )
        else:
            logger.warning(f"shutil.rmtree failed: {shutil_err}")
        return False

    prune_err: str | None = None
    try:
        prune_result = subprocess.run(
            ["git", "worktree", "prune"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if prune_result.returncode != 0:
            prune_err = prune_result.stderr.strip()
    except FileNotFoundError:
        prune_err = "git command not found"

    if git_failed and prune_err:
        logger.warning(
            f"git worktree remove failed: {git_err}; "
            f"recovered via shutil.rmtree; git worktree prune failed: {prune_err}"
        )
    elif git_failed:
        logger.warning(f"git worktree remove failed: {git_err}; recovered via shutil.rmtree")
    elif prune_err:
        logger.warning(f"git worktree prune failed: {prune_err}")

    return True


def delete_branch(repo_dir: str, branch_name: str) -> bool:
    """Delete a git branch with safe mode (-d).

    Args:
        repo_dir: Path to the main git repository.
        branch_name: Branch name to delete.

    Returns:
        True on success, False on failure.
    """
    try:
        result = subprocess.run(
            ["git", "branch", "-d", branch_name],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(f"git branch -d failed: {result.stderr.strip()}")
            return False
        return True
    except FileNotFoundError:
        logger.warning("git command not found")
        return False
