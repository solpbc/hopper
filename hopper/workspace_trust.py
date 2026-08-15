# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pre-trust workspaces that Hopper deliberately opens with Claude Code."""

import json
import os
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

LOCK_TIMEOUT_SEC = 5.0
LOCK_POLL_SEC = 0.05


class WorkspaceTrustError(RuntimeError):
    """Raised when Hopper cannot safely persist a Claude workspace trust grant."""


def _held_for_hint(lock_path: Path) -> str:
    """Describe how long a lock has existed, or nothing when that is unknowable.

    Diagnostic only. Hopper never acts on this value: the lock belongs to Claude
    Code's protocol and records no owner, so age is not evidence of abandonment.
    It is reported because an orphaned lock is otherwise indistinguishable from a
    busy one, and the operator is the one who can tell them apart.
    """
    try:
        age = time.time() - lock_path.lstat().st_mtime
    except OSError:
        return ""
    if age < 0:
        return ""
    if age < 60:
        held = f"{int(age)}s"
    elif age < 3600:
        held = f"{int(age // 60)}m"
    else:
        held = f"{int(age // 3600)}h{int((age % 3600) // 60)}m"
    return f" (present for {held}; if no claude process holds it, remove this directory)"


def claude_config_path(env: Mapping[str, str]) -> Path:
    """Return the global Claude project-state file used by a subprocess environment."""
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return _expand_path(config_dir, env) / ".claude.json"
    return _expand_path(env.get("HOME", str(Path.home())), env) / ".claude.json"


def trust_claude_workspace(
    cwd: str | None,
    env: Mapping[str, str],
    *,
    lock_timeout_sec: float = LOCK_TIMEOUT_SEC,
    lock_poll_sec: float = LOCK_POLL_SEC,
) -> Path | None:
    """Persist trust for a workspace Hopper is about to open with Claude.

    Trust is keyed to the exact workspace Claude will open. In particular,
    Hopper worktrees must not rely on a parent-directory trust grant.
    """
    if cwd is None:
        return None

    workspace = Path(cwd).expanduser().resolve()
    trust_root = workspace
    config_path = claude_config_path(env)

    try:
        config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceTrustError(
            f"cannot create Claude config directory {config_path.parent}: {exc}"
        ) from exc

    with _claude_config_lock(
        config_path,
        timeout_sec=lock_timeout_sec,
        poll_sec=lock_poll_sec,
    ):
        data = _load_claude_config(config_path)
        projects = data.get("projects")
        if projects is None:
            projects = {}
            data["projects"] = projects
        if not isinstance(projects, dict):
            raise WorkspaceTrustError(f"{config_path} has a non-object projects field")

        key = str(trust_root)
        project = projects.get(key)
        if project is None:
            project = {}
            projects[key] = project
        if not isinstance(project, dict):
            raise WorkspaceTrustError(f"{config_path} has a non-object project entry for {key}")

        if project.get("hasTrustDialogAccepted") is True:
            return trust_root

        project["hasTrustDialogAccepted"] = True
        try:
            _write_claude_config(config_path, data)
        except OSError as exc:
            raise WorkspaceTrustError(f"cannot write Claude config {config_path}: {exc}") from exc
        return trust_root


def _expand_path(value: str, env: Mapping[str, str]) -> Path:
    """Expand a leading tilde using the subprocess HOME, not the current process."""
    if value == "~":
        return Path(env.get("HOME", str(Path.home())))
    if value.startswith("~/"):
        return Path(env.get("HOME", str(Path.home()))) / value[2:]
    return Path(value)


@contextmanager
def _claude_config_lock(
    config_path: Path,
    *,
    timeout_sec: float,
    poll_sec: float,
) -> Iterator[None]:
    """Acquire Claude Code's `${config}.lock` directory protocol."""
    lock_path = Path(f"{config_path}.lock")
    deadline = time.monotonic() + timeout_sec

    while True:
        try:
            lock_path.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise WorkspaceTrustError(
                    f"timed out waiting for Claude config lock {lock_path}"
                    f"{_held_for_hint(lock_path)}"
                ) from None
            time.sleep(poll_sec)
        except OSError as exc:
            raise WorkspaceTrustError(
                f"cannot acquire Claude config lock {lock_path}: {exc}"
            ) from exc

    try:
        yield
    finally:
        try:
            lock_path.rmdir()
        except OSError as exc:
            raise WorkspaceTrustError(
                f"cannot release Claude config lock {lock_path}: {exc}"
            ) from exc


def _load_claude_config(config_path: Path) -> dict:
    """Load Claude's global state without treating malformed data as empty."""
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise WorkspaceTrustError(f"cannot read Claude config {config_path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkspaceTrustError(f"cannot parse Claude config {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkspaceTrustError(f"{config_path} is not a JSON object")
    return data


def _write_claude_config(config_path: Path, data: dict) -> None:
    """Atomically replace Claude's global state with owner-only permissions."""
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.hopper.",
        suffix=".tmp",
        dir=config_path.parent,
    )
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(data, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, config_path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
