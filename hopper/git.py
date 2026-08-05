# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Git utilities for hopper."""

import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

UPSTREAM_REMOTE = "origin"
DEFAULT_BRANCH_NAMES = ("main", "master")
GIT_FETCH_TIMEOUT_SEC = 120


def _branch_exists(repo_dir: str, branch_name: str) -> bool | None:
    """Return branch existence, or None when it cannot be proven."""
    try:
        result = subprocess.run(
            [
                "git",
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{branch_name}",
            ],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        logger.warning(
            f"failed to check branch {branch_name}: git rev-parse exited {result.returncode}"
        )
        return None
    except OSError as exc:
        logger.warning(f"failed to check branch {branch_name}: {exc}")
        return None


def _force_delete_branch(repo_dir: str, branch_name: str) -> bool:
    """Force-delete a branch created by a failed worktree-add attempt."""
    try:
        result = subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(f"git branch -D failed: {result.stderr.strip()}")
            return False
        return True
    except OSError as exc:
        logger.warning(f"git branch -D failed: {exc}")
        return False


def _resolve_default_branch(
    repo_dir: str, *, allow_local: bool
) -> tuple[str | None, tuple[str, ...]]:
    """Resolve the first default-branch ref and report every candidate probed.

    Remote refs are preferred. Local refs are included only when ``allow_local``
    is true. This function never fetches.
    """
    remote_candidates = tuple(
        f"{UPSTREAM_REMOTE}/{branch_name}" for branch_name in DEFAULT_BRANCH_NAMES
    )
    candidates = remote_candidates + (DEFAULT_BRANCH_NAMES if allow_local else ())
    for candidate in candidates:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", candidate],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return candidate, candidates
    return None, candidates


def create_worktree(
    repo_dir: str, worktree_path: Path, branch_name: str
) -> tuple[bool, str | None]:
    """Create an untracked branch worktree from the current upstream base.

    When the upstream remote is configured, fetch it and require one of its
    default-branch refs. Without that remote, preserve the current checkout
    behavior by starting from ``HEAD``. The registered checkout is not moved.

    Args:
        repo_dir: Path to the main git repository.
        worktree_path: Where to place the worktree.
        branch_name: Name for the new branch.

    Returns:
        (True, None) on success, or (False, error detail) on failure.
    """
    try:
        remote_result = subprocess.run(
            ["git", "remote"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if remote_result.returncode != 0:
            detail = remote_result.stderr.strip() or f"exit code {remote_result.returncode}"
            error = f"git remote failed: {detail}"
            logger.error(error)
            return False, error

        remote_names = {name.strip() for name in remote_result.stdout.splitlines() if name.strip()}
        if UPSTREAM_REMOTE in remote_names:
            try:
                fetch_result = subprocess.run(
                    ["git", "fetch", UPSTREAM_REMOTE],
                    cwd=repo_dir,
                    capture_output=True,
                    text=True,
                    timeout=GIT_FETCH_TIMEOUT_SEC,
                )
            except subprocess.TimeoutExpired:
                error = (
                    f"git fetch {UPSTREAM_REMOTE} timed out after {GIT_FETCH_TIMEOUT_SEC} seconds"
                )
                logger.error(error)
                return False, error
            if fetch_result.returncode != 0:
                detail = fetch_result.stderr.strip() or f"exit code {fetch_result.returncode}"
                error = f"git fetch {UPSTREAM_REMOTE} failed: {detail}"
                logger.error(error)
                return False, error

            base_ref, candidates = _resolve_default_branch(repo_dir, allow_local=False)
            if base_ref is None:
                attempted = ", ".join(candidates)
                error = (
                    f"upstream default branch resolution failed after git fetch "
                    f"{UPSTREAM_REMOTE}: no candidate exists ({attempted})"
                )
                logger.error(error)
                return False, error
        else:
            base_ref = "HEAD"

        branch_absence_proven = _branch_exists(repo_dir, branch_name) is False
        path_existed = worktree_path.exists()
        result = subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "--no-track",
                "-b",
                branch_name,
                str(worktree_path),
                base_ref,
            ],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or f"exit code {result.returncode}"
            error = f"git worktree add failed: {detail}"
            logger.error(error)

            if not path_existed:
                try:
                    if not remove_worktree(repo_dir, str(worktree_path)):
                        logger.warning(f"failed to remove partial worktree {worktree_path}")
                except Exception as exc:
                    logger.warning(f"failed to remove partial worktree {worktree_path}: {exc}")
                try:
                    prune_result = subprocess.run(
                        ["git", "worktree", "prune"],
                        cwd=repo_dir,
                        capture_output=True,
                        text=True,
                    )
                    if prune_result.returncode != 0:
                        logger.warning(f"git worktree prune failed: {prune_result.stderr.strip()}")
                except OSError as exc:
                    logger.warning(f"git worktree prune failed: {exc}")

            if branch_absence_proven and _branch_exists(repo_dir, branch_name) is True:
                _force_delete_branch(repo_dir, branch_name)
            return False, error
        return True, None
    except FileNotFoundError:
        error = "git command not found"
        logger.error(error)
        return False, error


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
        if result.returncode != 0:
            logger.warning(
                f"git status --porcelain failed in {repo_dir} (exit {result.returncode})"
            )
            return True
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


def commit_all(repo_dir: str, message: str) -> tuple[bool, str | None]:
    """Commit all working tree changes in a git repo.

    Args:
        repo_dir: Path to the git repository.
        message: Commit message.

    Returns:
        (True, None) on success, or (False, error detail) on failure.
    """
    try:
        add_result = subprocess.run(
            ["git", "add", "-A"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if add_result.returncode != 0:
            detail = add_result.stderr.strip() or f"exit code {add_result.returncode}"
            error = f"git add -A failed: {detail}"
            logger.warning(error)
            return False, error
        commit_result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if commit_result.returncode != 0:
            detail = commit_result.stderr.strip() or f"exit code {commit_result.returncode}"
            error = f"git commit failed: {detail}"
            logger.warning(error)
            return False, error
        return True, None
    except FileNotFoundError:
        error = "git command not found"
        logger.warning(error)
        return False, error
    except Exception as err:
        error = f"git commit failed: {err}"
        logger.warning(error)
        return False, error


def head_sha(repo_dir: str) -> str | None:
    """Return the full SHA for HEAD, or None when it cannot be resolved."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        sha = result.stdout.strip()
        return sha or None
    except Exception:
        return None


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


def quarantine_dirty_repo(repo_dir: str, lode_id: str) -> str | None:
    """Move a dirty project repo's uncommitted changes onto a fresh quarantine
    branch, restoring a clean working tree at the original HEAD.

    Preconditions (all must hold, else returns None without touching the repo):
      - HEAD is not detached.
      - No merge/rebase/cherry-pick is in progress.

    On success, returns the name of the newly created quarantine branch. Returns
    None if a precondition fails or any step of the quarantine fails; in the
    failure case nothing is deleted or reset, so no uncommitted work is lost.
    """
    try:
        # Precondition: HEAD must not be detached.
        symref = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if symref.returncode != 0:
            logger.warning(f"quarantine skipped: detached HEAD in {repo_dir}")
            return None

        # Precondition: no merge/rebase/cherry-pick in progress.
        git_dir_result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if git_dir_result.returncode != 0:
            logger.warning(f"quarantine skipped: cannot resolve git dir in {repo_dir}")
            return None
        git_dir = (Path(repo_dir) / git_dir_result.stdout.strip()).resolve()
        in_progress = [
            git_dir / "MERGE_HEAD",
            git_dir / "REBASE_HEAD",
            git_dir / "CHERRY_PICK_HEAD",
            git_dir / "rebase-merge",
            git_dir / "rebase-apply",
        ]
        if any(p.exists() for p in in_progress):
            logger.warning(f"quarantine skipped: git operation in progress in {repo_dir}")
            return None

        original_branch = current_branch(repo_dir)
        if original_branch is None:
            logger.warning(f"quarantine skipped: no current branch in {repo_dir}")
            return None

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        branch = f"hopper-quarantine-{timestamp}"

        switch_result = subprocess.run(
            ["git", "switch", "-c", branch],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if switch_result.returncode != 0:
            logger.warning(
                f"quarantine failed: git switch -c {branch}: {switch_result.stderr.strip()}"
            )
            return None

        message = f"hopper: quarantined dirty project repo blocking lode {lode_id}"
        committed, commit_error = commit_all(repo_dir, message)
        if not committed:
            logger.warning(
                f"quarantine failed: could not commit dirty state in {repo_dir}: {commit_error}"
            )
            return None

        back_result = subprocess.run(
            ["git", "switch", original_branch],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if back_result.returncode != 0:
            logger.warning(
                f"quarantine failed: git switch {original_branch}: {back_result.stderr.strip()}"
            )
            return None

        if is_dirty(repo_dir):
            logger.warning(f"quarantine failed: {repo_dir} still dirty after switch-back")
            return None

        logger.warning(
            f"quarantined dirty project repo {repo_dir} onto branch {branch} (lode {lode_id})"
        )
        return branch
    except (FileNotFoundError, subprocess.SubprocessError) as err:
        logger.warning(f"quarantine failed for {repo_dir}: {err}")
        return None


def get_diff_stat(worktree_path: str) -> str:
    """Get diff stat output against the upstream-preferred default branch.

    Args:
        worktree_path: Path to the git worktree.

    Returns:
        The diff --stat output as a string, or empty string on error.
    """
    try:
        base_ref, _candidates = _resolve_default_branch(worktree_path, allow_local=True)
        if base_ref is None:
            return ""
        result = subprocess.run(
            ["git", "diff", "--stat", f"{base_ref}...HEAD"],
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
    """Get diff numstat output against the upstream-preferred default branch.

    Args:
        worktree_path: Path to the git worktree.

    Returns:
        The diff --numstat output as a string, or empty string on error.
    """
    try:
        base_ref, _candidates = _resolve_default_branch(worktree_path, allow_local=True)
        if base_ref is None:
            return ""
        result = subprocess.run(
            ["git", "diff", "--numstat", base_ref],
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
