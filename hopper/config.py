# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared configuration for hopper."""

import fcntl
import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Literal

from platformdirs import user_data_dir

CONFIG_LOCK_TIMEOUT_SEC = 5.0
CONFIG_LOCK_POLL_INTERVAL_SEC = 0.05

ConfigErrorReason = Literal["malformed", "wrong_shape", "unreadable", "locked"]


class ConfigError(Exception):
    """A config file that Hopper cannot safely treat as empty."""

    def __init__(self, path: Path, reason: ConfigErrorReason):
        self.path = path
        self.reason = reason
        super().__init__(f"config at {path} is {reason}")


def hopper_dir() -> Path:
    """Return the hopper data directory for this user/OS."""
    return Path(user_data_dir("hopper"))


def server_socket_path() -> Path:
    """Return the hopper server socket path."""
    return hopper_dir() / "server.sock"


def worktree_root() -> Path:
    """Return the whitespace-free root for lode git worktrees.

    Kept separate from hopper_dir() (which on macOS is under
    "Application Support" and contains a space) because downstream
    project tooling breaks on spaces in the worktree path.
    """
    return Path.home() / ".hopper" / "worktrees"


def config_path() -> Path:
    """Return the user config file path."""
    return hopper_dir() / "config.json"


def config_lock_path() -> Path:
    """Return the persistent inter-process config lock path."""
    return hopper_dir() / "config.lock"


def load_config() -> dict[str, object]:
    """Load the user config strictly, treating only absence as empty."""
    path = config_path()
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(path, "unreadable") from exc

    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(path, "malformed") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(path, "wrong_shape")
    return loaded


def _acquire_config_lock(lock_file, path: Path) -> None:
    deadline = time.monotonic() + CONFIG_LOCK_TIMEOUT_SEC
    while True:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConfigError(path, "locked") from exc
            time.sleep(min(CONFIG_LOCK_POLL_INTERVAL_SEC, remaining))
        except OSError as exc:
            raise ConfigError(path, "unreadable") from exc


def _publish_config(data: dict[str, object], path: Path) -> None:
    fd = -1
    tmp: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w") as stream:
            fd = -1
            stream.write(json.dumps(data, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException as exc:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, OSError):
            raise ConfigError(path, "unreadable") from exc
        raise


@contextmanager
def config_transaction() -> Iterator[dict[str, object]]:
    """Serialize a strict read-modify-write and publish changes atomically.

    The lock covers the entire read-modify-write operation. An unchanged
    transaction remains serialized against concurrent writers, but skips
    publication so it does not rewrite the existing config bytes.
    """
    data_dir = hopper_dir()
    path = config_path()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        lock_file = open(config_lock_path(), "a+")
    except OSError as exc:
        raise ConfigError(path, "unreadable") from exc

    try:
        _acquire_config_lock(lock_file, path)
        data = load_config()
        original = deepcopy(data)
        yield data
        if data != original:
            _publish_config(data, path)
    finally:
        lock_file.close()
