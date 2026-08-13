# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Git utilities for hopper."""

import fcntl
import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from hopper import config

logger = logging.getLogger(__name__)

UPSTREAM_REMOTE = "origin"
DEFAULT_BRANCH_NAMES = ("main", "master")
UPSTREAM_FETCH_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"
SHIP_CLEANLINESS_TIMEOUT_SEC = 5.0
SHIP_HEAD_TIMEOUT_SEC = 5.0
SHIP_REMOTE_DETECTION_TIMEOUT_SEC = 5.0
SHIP_FETCH_TIMEOUT_SEC = 120.0
SHIP_DEFAULT_REF_TIMEOUT_SEC = 5.0
SHIP_ANCESTRY_TIMEOUT_SEC = 5.0
SHIP_REVALIDATION_TIMEOUT_SEC = 5.0
SHIP_LANDING_TIMEOUT_SEC = 155.0
UNPUSHED_PROBE_TIMEOUT_SEC = 2.0

ShipLandingCause = Literal[
    "worktree_unreadable",
    "worktree_missing",
    "cleanliness_deadline_expired",
    "cleanliness_unavailable",
    "cleanliness_failed",
    "cleanliness_dirty",
    "head_deadline_expired",
    "head_unavailable",
    "head_failed",
    "head_changed",
    "remote_detection_deadline_expired",
    "remote_detection_unavailable",
    "remote_detection_failed",
    "fetch_deadline_expired",
    "fetch_timed_out",
    "fetch_unavailable",
    "fetch_failed",
    "default_ref_deadline_expired",
    "default_ref_unavailable",
    "default_ref_failed",
    "default_ref_missing",
    "ancestry_deadline_expired",
    "ancestry_unavailable",
    "ancestry_contained",
    "ancestry_not_contained",
    "ancestry_failed",
]


@dataclass(frozen=True)
class ShipLandingVerdict:
    """The facts Hopper proved before allowing ship completion."""

    cleanliness: Literal["clean", "dirty", "indeterminate"]
    containment: Literal["contained", "not_contained", "indeterminate"]
    base_ref: str | None
    cause: ShipLandingCause
    detail: str


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
    repo_dir: str, *, allow_local: bool, timeout: float | None = None
) -> tuple[str | None, tuple[str, ...]]:
    """Resolve the first default-branch ref and report every candidate probed.

    Remote refs are preferred. Local refs are included only when ``allow_local``
    is true. This function never fetches. ``timeout`` bounds each probe for
    callers running inside someone else's deadline; a timeout raises, so those
    callers handle it as an unresolvable base.
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
            timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            return candidate, candidates
    return None, candidates


def git_common_dir_identity(repo_dir: str | Path) -> dict:
    """Resolve the shared Git directory and bind it to its filesystem identity."""
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(result.stderr.strip() or "git common directory is unavailable")
    path = Path(result.stdout.strip()).resolve(strict=True)
    stat = path.stat()
    return {
        "realpath": str(path),
        "identity": {"st_dev": stat.st_dev, "st_ino": stat.st_ino},
    }


@contextmanager
def repository_fetch_lock(repo_dir: str | Path):
    """Serialize shared-ref fetch workflows by exact Git common directory."""
    identity = git_common_dir_identity(repo_dir)
    key_bytes = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    key = hashlib.sha256(key_bytes).hexdigest()
    lock_dir = config.hopper_dir() / "git-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{key}.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # The path can be replaced while waiting; never share a lock across
        # different repositories merely because a pathname was reused.
        if git_common_dir_identity(repo_dir) != identity:
            raise RuntimeError("Git common directory identity changed while acquiring fetch lock")
        yield identity
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def create_worktree(
    repo_dir: str, worktree_path: Path, branch_name: str
) -> tuple[bool, str | None]:
    """Create a worktree while serializing shared remote-ref mutation."""
    try:
        with repository_fetch_lock(repo_dir):
            return _create_worktree_locked(repo_dir, worktree_path, branch_name)
    except (OSError, RuntimeError) as exc:
        error = f"Git fetch lock unavailable: {exc}"
        logger.error(error)
        return False, error


def _create_worktree_locked(
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
                    [
                        "git",
                        "fetch",
                        "--prune",
                        UPSTREAM_REMOTE,
                        UPSTREAM_FETCH_REFSPEC,
                    ],
                    cwd=repo_dir,
                    capture_output=True,
                    text=True,
                    timeout=SHIP_FETCH_TIMEOUT_SEC,
                )
            except subprocess.TimeoutExpired:
                error = (
                    f"git fetch {UPSTREAM_REMOTE} timed out after "
                    f"{SHIP_FETCH_TIMEOUT_SEC:g} seconds"
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


def _git_probe(
    worktree_path: str, args: list[str], timeout: float | None
) -> subprocess.CompletedProcess[str] | None:
    """Run one bounded git command, returning None if it could not run."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(f"git {' '.join(args)} failed in {worktree_path}: {exc}")
        return None


def _remaining_timeout(deadline: float, limit: float) -> float | None:
    """Return this probe's share of an aggregate monotonic deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return min(limit, remaining)


def _probe_worktree_cleanliness(
    worktree_path: str, timeout: float
) -> tuple[
    Literal["clean", "dirty", "indeterminate"],
    Literal["clean", "dirty", "unavailable", "failed"],
    str,
]:
    """Return tri-state worktree cleanliness using staged and untracked state."""
    result = _git_probe(worktree_path, ["status", "--porcelain"], timeout)
    if result is None:
        return (
            "indeterminate",
            "unavailable",
            "worktree cleanliness is indeterminate because git status failed or timed out",
        )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        return (
            "indeterminate",
            "failed",
            f"worktree cleanliness is indeterminate because git status failed: {detail}",
        )
    if result.stdout:
        return (
            "dirty",
            "dirty",
            "canonical worktree has staged, unstaged, or untracked changes",
        )
    return "clean", "clean", "canonical worktree is clean"


def ship_landing_verdict(worktree_dir: str | Path) -> ShipLandingVerdict:
    """Prove landing while serializing every shared-ref-dependent probe."""
    worktree_path = str(worktree_dir)
    try:
        if not Path(worktree_path).is_dir():
            return ShipLandingVerdict(
                "indeterminate",
                "indeterminate",
                None,
                "worktree_missing",
                f"canonical worktree is missing or not a directory: {worktree_path}",
            )
    except OSError as exc:
        return ShipLandingVerdict(
            "indeterminate",
            "indeterminate",
            None,
            "worktree_unreadable",
            f"canonical worktree could not be inspected at {worktree_path}: {exc}",
        )
    try:
        with repository_fetch_lock(worktree_path):
            return _ship_landing_verdict_locked(worktree_path)
    except (OSError, RuntimeError) as exc:
        return ShipLandingVerdict(
            "indeterminate",
            "indeterminate",
            None,
            "fetch_unavailable",
            f"Git fetch lock is unavailable: {exc}",
        )


def _ship_landing_verdict_locked(worktree_dir: str | Path) -> ShipLandingVerdict:
    """Prove one stable, clean HEAD is contained in the repository's default ref.

    Repositories with ``origin`` are checked against a freshly fetched upstream
    ref. Repositories without it must still land in local ``main`` or ``master``.
    Every uncertainty and every mid-proof HEAD or worktree change fails closed.
    """
    worktree_path = str(worktree_dir)
    deadline = time.monotonic() + SHIP_LANDING_TIMEOUT_SEC

    try:
        worktree_exists = Path(worktree_path).is_dir()
    except OSError as exc:
        return ShipLandingVerdict(
            "indeterminate",
            "indeterminate",
            None,
            "worktree_unreadable",
            f"canonical worktree could not be inspected at {worktree_path}: {exc}",
        )
    if not worktree_exists:
        return ShipLandingVerdict(
            "indeterminate",
            "indeterminate",
            None,
            "worktree_missing",
            f"canonical worktree is missing or not a directory: {worktree_path}",
        )

    timeout = _remaining_timeout(deadline, SHIP_CLEANLINESS_TIMEOUT_SEC)
    if timeout is None:
        return ShipLandingVerdict(
            "indeterminate",
            "indeterminate",
            None,
            "cleanliness_deadline_expired",
            "overall ship landing deadline expired before checking worktree cleanliness",
        )
    cleanliness, cleanliness_outcome, cleanliness_detail = _probe_worktree_cleanliness(
        worktree_path, timeout
    )
    if cleanliness != "clean":
        cleanliness_cause: ShipLandingCause = {
            "dirty": "cleanliness_dirty",
            "unavailable": "cleanliness_unavailable",
            "failed": "cleanliness_failed",
        }[cleanliness_outcome]
        return ShipLandingVerdict(
            cleanliness,
            "indeterminate",
            None,
            cleanliness_cause,
            cleanliness_detail,
        )

    timeout = _remaining_timeout(deadline, SHIP_HEAD_TIMEOUT_SEC)
    if timeout is None:
        return ShipLandingVerdict(
            "clean",
            "indeterminate",
            None,
            "head_deadline_expired",
            "overall ship landing deadline expired before capturing HEAD",
        )
    head = _git_probe(worktree_path, ["rev-parse", "--verify", "HEAD"], timeout)
    if head is None:
        return ShipLandingVerdict(
            "clean",
            "indeterminate",
            None,
            "head_unavailable",
            "canonical worktree HEAD is indeterminate because git failed or timed out",
        )
    expected_head = head.stdout.strip()
    if head.returncode != 0 or not expected_head:
        detail = head.stderr.strip() or f"exit code {head.returncode}"
        return ShipLandingVerdict(
            "clean",
            "indeterminate",
            None,
            "head_failed",
            f"canonical worktree HEAD could not be resolved: {detail}",
        )

    timeout = _remaining_timeout(deadline, SHIP_REMOTE_DETECTION_TIMEOUT_SEC)
    if timeout is None:
        return ShipLandingVerdict(
            "clean",
            "indeterminate",
            None,
            "remote_detection_deadline_expired",
            "overall ship landing deadline expired before detecting Git remotes",
        )
    remotes = _git_probe(worktree_path, ["remote"], timeout)
    if remotes is None:
        return ShipLandingVerdict(
            "clean",
            "indeterminate",
            None,
            "remote_detection_unavailable",
            "origin detection is indeterminate because git remote failed or timed out",
        )
    if remotes.returncode != 0:
        detail = remotes.stderr.strip() or f"exit code {remotes.returncode}"
        return ShipLandingVerdict(
            "clean",
            "indeterminate",
            None,
            "remote_detection_failed",
            f"origin detection is indeterminate because git remote failed: {detail}",
        )
    remote_names = {name.strip() for name in remotes.stdout.splitlines() if name.strip()}
    has_origin = UPSTREAM_REMOTE in remote_names
    if has_origin:
        timeout = _remaining_timeout(deadline, SHIP_FETCH_TIMEOUT_SEC)
        if timeout is None:
            return ShipLandingVerdict(
                "clean",
                "indeterminate",
                None,
                "fetch_deadline_expired",
                "overall ship landing deadline expired before fetching origin",
            )
        fetch_args = ["fetch", "--prune", UPSTREAM_REMOTE, UPSTREAM_FETCH_REFSPEC]
        try:
            fetch_result = subprocess.run(
                ["git", *fetch_args],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ShipLandingVerdict(
                "clean",
                "indeterminate",
                None,
                "fetch_timed_out",
                f"fetch from origin timed out after {timeout:g} seconds",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ShipLandingVerdict(
                "clean",
                "indeterminate",
                None,
                "fetch_unavailable",
                f"fetch from origin failed: {exc}",
            )
        if fetch_result.returncode != 0:
            detail = fetch_result.stderr.strip() or f"exit code {fetch_result.returncode}"
            return ShipLandingVerdict(
                "clean",
                "indeterminate",
                None,
                "fetch_failed",
                f"fetch from origin failed: {detail}",
            )

    resolution_deadline = min(deadline, time.monotonic() + SHIP_DEFAULT_REF_TIMEOUT_SEC)
    base_ref = None
    candidates = tuple(
        f"{UPSTREAM_REMOTE}/{branch_name}" if has_origin else branch_name
        for branch_name in DEFAULT_BRANCH_NAMES
    )
    for candidate in candidates:
        timeout = _remaining_timeout(resolution_deadline, SHIP_DEFAULT_REF_TIMEOUT_SEC)
        if timeout is None:
            return ShipLandingVerdict(
                "clean",
                "indeterminate",
                None,
                "default_ref_deadline_expired",
                "fresh default-branch resolution timed out",
            )
        result = _git_probe(
            worktree_path,
            ["rev-parse", "--verify", "--quiet", candidate],
            timeout,
        )
        if result is None:
            return ShipLandingVerdict(
                "clean",
                "indeterminate",
                None,
                "default_ref_unavailable",
                f"default-branch resolution is indeterminate while probing {candidate}",
            )
        if result.returncode == 0 and result.stdout.strip():
            base_ref = candidate
            break
        if result.returncode != 1:
            detail = result.stderr.strip() or f"exit code {result.returncode}"
            return ShipLandingVerdict(
                "clean",
                "indeterminate",
                None,
                "default_ref_failed",
                f"default-branch resolution failed while probing {candidate}: {detail}",
            )
    if base_ref is None:
        attempted = ", ".join(candidates)
        basis = "fresh upstream" if has_origin else "local"
        return ShipLandingVerdict(
            "clean",
            "indeterminate",
            None,
            "default_ref_missing",
            f"no {basis} default branch exists ({attempted})",
        )

    timeout = _remaining_timeout(deadline, SHIP_ANCESTRY_TIMEOUT_SEC)
    if timeout is None:
        return ShipLandingVerdict(
            "clean",
            "indeterminate",
            base_ref,
            "ancestry_deadline_expired",
            "overall ship landing deadline expired before checking ancestry",
        )
    ancestry = _git_probe(
        worktree_path,
        ["merge-base", "--is-ancestor", expected_head, base_ref],
        timeout,
    )
    if ancestry is None:
        return ShipLandingVerdict(
            "clean",
            "indeterminate",
            base_ref,
            "ancestry_unavailable",
            f"ancestry against {base_ref} is indeterminate because git failed or timed out",
        )
    if ancestry.returncode == 0:
        timeout = _remaining_timeout(deadline, SHIP_REVALIDATION_TIMEOUT_SEC)
        if timeout is None:
            return ShipLandingVerdict(
                "clean",
                "indeterminate",
                base_ref,
                "head_deadline_expired",
                "overall ship landing deadline expired before revalidating HEAD",
            )
        final_head = _git_probe(worktree_path, ["rev-parse", "--verify", "HEAD"], timeout)
        if final_head is None:
            return ShipLandingVerdict(
                "clean",
                "indeterminate",
                base_ref,
                "head_unavailable",
                "canonical worktree HEAD could not be revalidated",
            )
        observed_head = final_head.stdout.strip()
        if final_head.returncode != 0 or not observed_head:
            detail = final_head.stderr.strip() or f"exit code {final_head.returncode}"
            return ShipLandingVerdict(
                "clean",
                "indeterminate",
                base_ref,
                "head_failed",
                f"canonical worktree HEAD revalidation failed: {detail}",
            )
        if observed_head != expected_head:
            return ShipLandingVerdict(
                "clean",
                "indeterminate",
                base_ref,
                "head_changed",
                "canonical worktree HEAD changed while landing was being verified",
            )

        timeout = _remaining_timeout(deadline, SHIP_REVALIDATION_TIMEOUT_SEC)
        if timeout is None:
            return ShipLandingVerdict(
                "indeterminate",
                "indeterminate",
                base_ref,
                "cleanliness_deadline_expired",
                "overall ship landing deadline expired before rechecking cleanliness",
            )
        final_cleanliness, final_outcome, final_detail = _probe_worktree_cleanliness(
            worktree_path, timeout
        )
        if final_cleanliness != "clean":
            final_cause: ShipLandingCause = {
                "dirty": "cleanliness_dirty",
                "unavailable": "cleanliness_unavailable",
                "failed": "cleanliness_failed",
            }[final_outcome]
            return ShipLandingVerdict(
                final_cleanliness,
                "indeterminate",
                base_ref,
                final_cause,
                f"final {final_detail}",
            )

        basis = "freshly fetched " if has_origin else "local "
        return ShipLandingVerdict(
            "clean",
            "contained",
            base_ref,
            "ancestry_contained",
            f"stable HEAD is contained in {basis}{base_ref}",
        )
    if ancestry.returncode == 1:
        return ShipLandingVerdict(
            "clean",
            "not_contained",
            base_ref,
            "ancestry_not_contained",
            f"captured HEAD is not contained in {base_ref}",
        )
    detail = ancestry.stderr.strip() or f"exit code {ancestry.returncode}"
    return ShipLandingVerdict(
        "clean",
        "indeterminate",
        base_ref,
        "ancestry_failed",
        f"ancestry against {base_ref} is indeterminate: {detail}",
    )


def unpushed_commits(
    worktree_path: str, *, timeout: float = UNPUSHED_PROBE_TIMEOUT_SEC
) -> tuple[int | None, str | None]:
    """Count commits that exist only in this worktree.

    Returns ``(count, basis)``. ``count`` is None whenever the answer cannot be
    proven -- the path is not a directory, no basis resolves, or git fails or
    times out -- and a caller guarding a destructive operation must read None as
    "this may be losing work", never as zero.

    The question is durability, not landing: a commit reachable from any
    remote-tracking ref has a copy somewhere else, even if it has not been
    merged. Anything else lives on this disk alone. Counting against the default
    base instead would keep flagging a branch that was pushed precisely to make
    it safe, and a guard people routinely override stops being read.

    A project with no remote at all falls back to its local default branch,
    where "not merged" is the only durability question there is. This never
    fetches: a stale view can only over-count, which is the side that fails safe.

    ``timeout`` bounds each git invocation. This runs inside the same status
    call a remote host answers under an ssh deadline, and a loaded host that
    blew that deadline is how a live lode reads as unreadable -- so a slow
    answer degrades to UNKNOWN rather than spending the caller's budget.
    """
    try:
        if not Path(worktree_path).is_dir():
            return None, None
    except OSError as exc:
        logger.warning(f"failed to stat worktree {worktree_path}: {exc}")
        return None, None

    remotes = _git_probe(worktree_path, ["for-each-ref", "--count=1", "refs/remotes"], timeout)
    if remotes is None or remotes.returncode != 0:
        return None, None

    if remotes.stdout.strip():
        basis = "a remote branch"
        rev_list_args = ["rev-list", "--count", "HEAD", "--not", "--remotes"]
    else:
        try:
            base, _candidates = _resolve_default_branch(
                worktree_path, allow_local=True, timeout=timeout
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning(f"failed to resolve default branch in {worktree_path}: {exc}")
            return None, None
        if base is None:
            return None, None
        basis = base
        rev_list_args = ["rev-list", "--count", f"{base}..HEAD"]

    result = _git_probe(worktree_path, rev_list_args, timeout)
    if result is None:
        return None, basis
    if result.returncode != 0:
        logger.warning(f"unpushed count failed in {worktree_path}: {result.stderr.strip()}")
        return None, basis
    try:
        return int(result.stdout.strip()), basis
    except ValueError:
        logger.warning(f"unpushed count returned no number in {worktree_path}")
        return None, basis


def _required_git(cwd: str | Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _path_identity(path: str | Path) -> dict:
    resolved = Path(path).resolve(strict=True)
    stat = resolved.stat()
    return {
        "realpath": str(resolved),
        "identity": {"st_dev": stat.st_dev, "st_ino": stat.st_ino},
    }


def _path_matches(path: str | Path, expected: dict) -> bool | None:
    try:
        return _path_identity(path) == expected
    except FileNotFoundError:
        return False
    except OSError:
        return None


def worktree_registrations(repo_dir: str | Path) -> list[dict]:
    """Return strict records from ``git worktree list --porcelain -z``."""
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain", "-z"],
        cwd=str(repo_dir),
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(detail or "git worktree list failed")
    records: list[dict] = []
    current: dict[str, str | bool] = {}
    for raw in result.stdout.split(b"\0"):
        if not raw:
            if current:
                records.append(current)
                current = {}
            continue
        text = raw.decode("utf-8")
        key, separator, value = text.partition(" ")
        if key in current:
            raise ValueError(f"duplicate {key} in git worktree record")
        current[key] = value if separator else True
    if current:
        records.append(current)
    for record in records:
        if not isinstance(record.get("worktree"), str) or not isinstance(record.get("HEAD"), str):
            raise ValueError("git worktree record is missing worktree or HEAD")
    return records


def _matching_registration(records: list[dict], path: str, branch_ref: str, head_oid: str) -> str:
    matches = [record for record in records if record.get("worktree") == path]
    if not matches:
        return "absent"
    if len(matches) != 1:
        return "mismatch"
    record = matches[0]
    if record.get("HEAD") != head_oid or record.get("branch") != branch_ref:
        return "mismatch"
    return "match"


def capture_worktree_provenance(project_dir: str | Path, worktree_path: str | Path) -> dict:
    """Capture the immutable repository/worktree facts used by ship cleanup."""
    project = _path_identity(project_dir)
    worktree = _path_identity(worktree_path)
    common = git_common_dir_identity(worktree["realpath"])
    private_git_dir = _path_identity(
        _required_git(
            worktree["realpath"],
            "rev-parse",
            "--path-format=absolute",
            "--absolute-git-dir",
        )
    )
    branch_ref = _required_git(worktree["realpath"], "symbolic-ref", "-q", "HEAD")
    head_oid = _required_git(worktree["realpath"], "rev-parse", "HEAD")
    branch_oid = _required_git(worktree["realpath"], "rev-parse", branch_ref)
    records = worktree_registrations(project["realpath"])
    if _matching_registration(records, worktree["realpath"], branch_ref, head_oid) != "match":
        raise RuntimeError("accepted worktree registration is absent or ambiguous")
    return {
        "project": project,
        "git_common_dir": common,
        "worktree": worktree,
        "worktree_git_dir": private_git_dir,
        "branch_ref": branch_ref,
        "branch_oid": branch_oid,
        "head_oid": head_oid,
    }


def revalidate_worktree_provenance(provenance: dict, *, worktree_path: str | None = None) -> dict:
    """Revalidate every destructive ship identity and exact registration."""
    target = worktree_path or provenance["worktree"]["realpath"]
    for name in ("project", "git_common_dir", "worktree_git_dir"):
        matched = _path_matches(provenance[name]["realpath"], provenance[name])
        if matched is None:
            return {"state": "unknown", "error": f"{name} identity cannot be read"}
        if not matched:
            return {"state": "mismatch", "error": f"{name} identity changed"}
    target_expected = {
        "realpath": str(Path(target).resolve(strict=False)),
        "identity": provenance["worktree"]["identity"],
    }
    matched = _path_matches(target, target_expected)
    if matched is None:
        return {"state": "unknown", "error": "worktree identity cannot be read"}
    if not matched:
        return {"state": "mismatch", "error": "worktree identity changed or is absent"}
    try:
        branch_oid = _required_git(
            provenance["project"]["realpath"], "rev-parse", provenance["branch_ref"]
        )
        head_oid = _required_git(target, "rev-parse", "HEAD")
        records = worktree_registrations(provenance["project"]["realpath"])
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        return {"state": "unknown", "error": str(exc)}
    if branch_oid != provenance["branch_oid"] or head_oid != provenance["head_oid"]:
        return {"state": "mismatch", "error": "branch or HEAD identity changed"}
    registration = _matching_registration(
        records, target_expected["realpath"], provenance["branch_ref"], provenance["head_oid"]
    )
    if registration != "match":
        return {
            "state": "mismatch" if registration == "mismatch" else "unknown",
            "error": "exact worktree registration is absent or changed",
        }
    return {"state": "match", "error": None}


def quarantine_worktree(provenance: dict, quarantine: dict) -> dict:
    """Rename the exact accepted worktree to its deterministic sibling."""
    original = quarantine["original_path"]
    target = quarantine["quarantine_path"]
    expected_identity = quarantine["expected_identity"]
    original_expected = {"realpath": original, "identity": expected_identity}
    target_expected = {"realpath": target, "identity": expected_identity}
    original_match = _path_matches(original, original_expected)
    target_match = _path_matches(target, target_expected)
    if original_match is None or target_match is None:
        return {"state": "unknown", "error": "quarantine paths cannot be inspected"}
    if target_match and not original_match:
        return {"state": "renamed", "error": None}
    if not original_match or target_match or Path(target).exists():
        return {"state": "retained", "error": "quarantine path identities are ambiguous"}
    validation = revalidate_worktree_provenance(provenance)
    if validation["state"] != "match":
        return {"state": "retained", "error": validation["error"]}
    try:
        os.rename(original, target)
    except OSError as exc:
        return {"state": "retained", "error": f"quarantine rename failed: {exc}"}
    if _path_matches(target, target_expected) is not True or Path(original).exists():
        return {"state": "unknown", "error": "quarantine rename could not be verified"}
    return {"state": "renamed", "error": None}


def repair_quarantined_worktree(provenance: dict, quarantine: dict) -> dict:
    """Repair and verify the exact Git registration after quarantine rename."""
    original = quarantine["original_path"]
    target = quarantine["quarantine_path"]
    target_expected = {"realpath": target, "identity": quarantine["expected_identity"]}
    if _path_matches(target, target_expected) is not True or Path(original).exists():
        return {"state": "retained", "error": "quarantine rename identity is not exact"}
    for name in ("project", "git_common_dir", "worktree_git_dir"):
        if _path_matches(provenance[name]["realpath"], provenance[name]) is not True:
            return {"state": "retained", "error": f"{name} identity changed"}
    try:
        records = worktree_registrations(provenance["project"]["realpath"])
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        return {"state": "unknown", "error": str(exc)}
    target_state = _matching_registration(
        records, target, provenance["branch_ref"], provenance["head_oid"]
    )
    if target_state == "match":
        return {"state": "repaired", "error": None}
    original_state = _matching_registration(
        records, original, provenance["branch_ref"], provenance["head_oid"]
    )
    if target_state != "absent" or original_state != "match":
        return {"state": "retained", "error": "worktree registration is ambiguous"}
    try:
        result = subprocess.run(
            ["git", "-C", provenance["project"]["realpath"], "worktree", "repair", target],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return {"state": "unknown", "error": f"git worktree repair failed: {exc}"}
    if result.returncode != 0:
        return {
            "state": "retained",
            "error": result.stderr.strip() or "git worktree repair failed",
        }
    validation = revalidate_worktree_provenance(provenance, worktree_path=target)
    if validation["state"] != "match":
        return {"state": "retained", "error": validation["error"]}
    return {"state": "repaired", "error": None}


def authorize_quarantine_cleanup(
    provenance: dict, quarantine: dict, *, base_ref: str | None
) -> dict:
    """Re-probe every work-loss guard on the repaired quarantine."""
    target = quarantine["quarantine_path"]
    validation = revalidate_worktree_provenance(provenance, worktree_path=target)
    if validation["state"] != "match":
        return {"authorized": False, "error": validation["error"]}
    if is_dirty(target):
        return {"authorized": False, "error": "quarantined worktree is dirty or unreadable"}
    count, basis = unpushed_commits(target)
    if count is None:
        return {"authorized": False, "error": "unpushed commit count is unknown"}
    if count:
        return {
            "authorized": False,
            "error": f"quarantined worktree has {count} unpushed commit(s) against {basis}",
        }
    if base_ref is not None:
        result = _git_probe(target, ["merge-base", "--is-ancestor", "HEAD", base_ref], None)
        if result is None or result.returncode != 0:
            return {
                "authorized": False,
                "error": f"HEAD ancestry against {base_ref} is no longer proven",
            }
    return {"authorized": True, "error": None}


def remove_quarantined_worktree(repo_identity: dict, quarantine: dict) -> dict:
    """Remove only an exact quarantine via non-forcing Git worktree removal."""
    target = quarantine["quarantine_path"]
    project = repo_identity["project"]["realpath"]
    target_expected = {"realpath": target, "identity": quarantine["expected_identity"]}
    target_match = _path_matches(target, target_expected)
    try:
        records = worktree_registrations(project)
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        return {"state": "unknown", "error": str(exc)}
    registration = _matching_registration(
        records, target, repo_identity["branch_ref"], repo_identity["head_oid"]
    )
    if target_match is False and registration == "absent":
        return {"state": "already-absent", "error": None}
    if target_match is not True or registration != "match":
        return {"state": "retained", "error": "quarantine path or registration is ambiguous"}
    for name in ("project", "git_common_dir", "worktree_git_dir"):
        if _path_matches(repo_identity[name]["realpath"], repo_identity[name]) is not True:
            return {"state": "retained", "error": f"{name} identity changed"}
    try:
        result = subprocess.run(
            ["git", "worktree", "remove", target],
            cwd=project,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return {"state": "unknown", "error": f"git worktree remove failed: {exc}"}
    try:
        post_records = worktree_registrations(project)
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        return {"state": "unknown", "error": str(exc)}
    post_registration = _matching_registration(
        post_records, target, repo_identity["branch_ref"], repo_identity["head_oid"]
    )
    if not Path(target).exists() and post_registration == "absent":
        return {"state": "removed", "error": None}
    detail = result.stderr.strip() or "non-forcing git worktree removal did not complete"
    return {"state": "retained", "error": detail}


def delete_branch_if_unchanged(provenance: dict, *, base_ref: str | None) -> dict:
    """Delete the accepted branch only when its recorded OID is contained in the landing base_ref.

    Only exit 0 from git merge-base --is-ancestor authorizes an expected-OID
    compare-and-delete. A missing or unresolvable base_ref, a nonzero containment
    result, or an unavailable probe does not authorize deletion.
    """
    project = provenance["project"]["realpath"]
    for name in ("project", "git_common_dir"):
        if _path_matches(provenance[name]["realpath"], provenance[name]) is not True:
            return {"state": "anomalous", "error": f"{name} identity changed"}
    try:
        records = worktree_registrations(project)
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        return {"state": "unknown", "error": str(exc)}
    if any(record.get("branch") == provenance["branch_ref"] for record in records):
        return {"state": "anomalous", "error": "accepted branch is still checked out"}
    result = _git_probe(
        project,
        ["rev-parse", "--verify", "--quiet", provenance["branch_ref"]],
        None,
    )
    if result is None:
        return {"state": "unknown", "error": "accepted branch ref could not be read"}
    if result.returncode == 1:
        return {"state": "already-absent", "error": None}
    if result.returncode != 0 or result.stdout.strip() != provenance["branch_oid"]:
        return {"state": "anomalous", "error": "accepted branch ref is unknown or changed"}
    if base_ref is None:
        return {
            "state": "retained",
            "error": "branch deletion is not authorized without a landing base ref",
        }
    containment = _git_probe(
        project,
        ["merge-base", "--is-ancestor", provenance["branch_oid"], base_ref],
        None,
    )
    if containment is None:
        return {"state": "unknown", "error": "accepted branch containment could not be probed"}
    if containment.returncode != 0:
        return {
            "state": "retained",
            "error": f"accepted branch OID containment in {base_ref} is not proven",
        }
    deleted = _git_probe(
        project,
        ["update-ref", "-d", provenance["branch_ref"], provenance["branch_oid"]],
        None,
    )
    if deleted is None:
        return {"state": "unknown", "error": "safe branch deletion could not run"}
    if deleted.returncode != 0:
        return {
            "state": "anomalous",
            "error": (
                "accepted branch ref did not match expected OID at deletion time; "
                "nothing was deleted"
            ),
        }
    verify = _git_probe(
        project,
        ["rev-parse", "--verify", "--quiet", provenance["branch_ref"]],
        None,
    )
    if verify is None:
        return {"state": "unknown", "error": "branch deletion could not be verified"}
    if verify.returncode == 1:
        return {"state": "deleted", "error": None}
    return {"state": "unknown", "error": "branch deletion could not be verified"}


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
