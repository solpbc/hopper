# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Client-side remote hopper helpers."""

import json
import os
import shlex
import subprocess

from hopper import config
from hopper.lodes import current_time_ms

REMOTE_CONFIG_PREFIX = "remote."
REMOTE_LODE_CACHE_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000


def run_remote(
    host: str,
    hop_args: list[str],
    stdin_text: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run hop on a remote host over ssh and return the completed process."""
    quoted_args = " ".join(shlex.quote(arg) for arg in hop_args)
    remote_command = 'export HOP_NO_ROUTE=1; exec "$HOME/.local/bin/hop"'
    if quoted_args:
        remote_command = f"{remote_command} {quoted_args}"
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        host,
        "--",
        remote_command,
    ]
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
    }
    if stdin_text is not None:
        kwargs["input"] = stdin_text
    return subprocess.run(command, **kwargs)


def remote_registry() -> dict[str, str]:
    """Return configured project -> remote host mappings."""
    cfg = config.load_config()
    registry: dict[str, str] = {}
    for key, value in cfg.items():
        if key.startswith(REMOTE_CONFIG_PREFIX) and isinstance(value, str):
            project = key.removeprefix(REMOTE_CONFIG_PREFIX)
            if project:
                registry[project] = value
    return registry


def set_remote(project: str, host: str) -> None:
    """Set a project -> remote host mapping."""
    cfg = config.load_config()
    cfg[f"{REMOTE_CONFIG_PREFIX}{project}"] = host
    config.save_config(cfg)


def remove_remote(project: str) -> bool:
    """Remove a project -> remote host mapping."""
    cfg = config.load_config()
    key = f"{REMOTE_CONFIG_PREFIX}{project}"
    if key not in cfg:
        return False
    del cfg[key]
    config.save_config(cfg)
    return True


def remote_lode_cache_path():
    """Return the remote lode cache path."""
    return config.hopper_dir() / "remote-lodes.json"


def load_lode_cache() -> dict[str, dict]:
    """Load lode id -> host cache."""
    path = remote_lode_cache_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def save_lode_cache(cache: dict[str, dict]) -> None:
    """Save the lode id -> host cache atomically."""
    data_dir = config.hopper_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    path = remote_lode_cache_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def prune_lode_cache(cache: dict[str, dict], now_ms: int | None = None) -> dict[str, dict]:
    """Drop cache entries older than the retention window."""
    now = current_time_ms() if now_ms is None else now_ms
    pruned: dict[str, dict] = {}
    for lode_id, entry in cache.items():
        created = entry.get("created_ms", entry.get("created_at", now))
        if not isinstance(created, int | float):
            created = now
        if now - int(created) < REMOTE_LODE_CACHE_MAX_AGE_MS:
            pruned[lode_id] = entry
    return pruned


def remember_lode(
    lode_id: str,
    host: str,
    project: str = "",
    created_ms: int | None = None,
) -> None:
    """Remember where a remote lode lives."""
    now = current_time_ms()
    cache = prune_lode_cache(load_lode_cache(), now)
    cache[lode_id] = {
        "host": host,
        "project": project,
        "created_ms": created_ms if created_ms is not None else now,
    }
    save_lode_cache(cache)
