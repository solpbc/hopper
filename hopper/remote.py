# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Client-side remote hopper helpers."""

import fcntl
import json
import os
import random
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from hopper import config
from hopper.lodes import ID_ALPHABET, ID_LEN, current_time_ms

REMOTE_CONFIG_PREFIX = "remote."
REMOTE_LODE_CACHE_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000
REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC = 8.0
REMOTE_POOL_PROBE_TIMEOUT_SEC = 10.0
REMOTE_CREATE_TIMEOUT_SEC = 180.0
REMOTE_CACHE_LOCK_TIMEOUT_SEC = 5.0
REMOTE_CACHE_LOCK_POLL_INTERVAL_SEC = 0.05

RemoteRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class CandidateProbe:
    """Readiness and load observed for one host in a configured pool."""

    host: str
    eligible: bool
    load: int | None
    reason: str | None


@dataclass(frozen=True)
class HostDiscovery:
    """Lodes proven by one host, or why that host was unavailable."""

    host: str
    lodes: tuple[dict, ...]
    reason: str | None


LodeCacheErrorReason = Literal["malformed", "wrong_shape", "unreadable", "locked"]


class LodeCacheError(Exception):
    """A resident-route cache that cannot safely be treated as absent."""

    def __init__(self, path: Path, reason: LodeCacheErrorReason):
        self.path = path
        self.reason = reason
        super().__init__(f"resident-route cache at {path} is {reason}")


def run_remote(
    host: str,
    hop_args: list[str],
    stdin_text: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run hop on a remote host over ssh and return the completed process."""
    command = _remote_command(host, hop_args)
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
    }
    if stdin_text is not None:
        kwargs["input"] = stdin_text
    return subprocess.run(command, **kwargs)


def run_remote_streaming(host: str, hop_args: list[str]) -> int:
    """Run remote hop while forwarding stdout and inheriting stderr live."""
    process = subprocess.Popen(
        _remote_command(host, hop_args, unbuffered=True),
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        if process.stdout is not None:
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        return 130


def _remote_command(host: str, hop_args: list[str], *, unbuffered: bool = False) -> list[str]:
    """Build the ssh argv for one remote hop invocation."""
    quoted_args = " ".join(_quote_remote_arg(arg) for arg in hop_args)
    remote_command = 'export HOP_NO_ROUTE=1; exec "$HOME/.local/bin/hop"'
    if unbuffered:
        remote_command = (
            'export HOP_NO_ROUTE=1; export PYTHONUNBUFFERED=1; exec "$HOME/.local/bin/hop"'
        )
    if quoted_args:
        remote_command = f"{remote_command} {quoted_args}"
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        host,
        "--",
        remote_command,
    ]


def _quote_remote_arg(arg: str) -> str:
    """Quote one hop arg, expanding an explicitly preserved tilde remotely."""
    if arg == "~":
        return '"$HOME"'
    if arg.startswith("~/"):
        return f'"$HOME"/{shlex.quote(arg[2:])}'
    return shlex.quote(arg)


def _normalized_pool(value: object) -> list[str] | None:
    """Validate one configured host pool without rewriting its entries."""
    if not isinstance(value, list) or not value:
        return None
    hosts: list[str] = []
    for host in value:
        if not isinstance(host, str):
            return None
        normalized = host.strip()
        if not normalized or host != normalized:
            return None
        hosts.append(host)
    if len(set(hosts)) != len(hosts):
        return None
    return hosts


def _remote_pools(cfg: dict[str, object]) -> dict[str, list[str]]:
    """Validate every remote config value and return normalized pools."""
    registry: dict[str, list[str]] = {}
    for key, value in cfg.items():
        if not key.startswith(REMOTE_CONFIG_PREFIX):
            continue
        project = key.removeprefix(REMOTE_CONFIG_PREFIX)
        pool = _normalized_pool(value)
        if not project or pool is None:
            raise config.ConfigError(config.config_path(), "wrong_shape")
        registry[project] = pool
    return registry


def remote_registry() -> dict[str, list[str]]:
    """Return configured host pools, migrating legacy scalar routes once."""
    cfg = config.load_config()
    has_scalar = any(
        key.startswith(REMOTE_CONFIG_PREFIX) and isinstance(value, str)
        for key, value in cfg.items()
    )
    if not has_scalar:
        return _remote_pools(cfg)

    with config.config_transaction() as locked:
        for key, value in tuple(locked.items()):
            if key.startswith(REMOTE_CONFIG_PREFIX) and isinstance(value, str):
                # Trimming is an intentional one-time legacy scalar migration;
                # established list entries must already be normalized.
                locked[key] = [value.strip()]
        return _remote_pools(locked)


def set_remote(project: str, hosts: list[str]) -> None:
    """Replace a project's configured host pool with a canonical ordered pool."""
    project = project.strip()
    if not project or _normalized_pool(hosts) is None:
        raise ValueError("project and hosts must be non-empty and normalized")
    with config.config_transaction() as cfg:
        cfg[f"{REMOTE_CONFIG_PREFIX}{project}"] = list(hosts)


def remove_remote(project: str) -> bool:
    """Remove a project -> remote host mapping."""
    key = f"{REMOTE_CONFIG_PREFIX}{project.strip()}"
    with config.config_transaction() as cfg:
        if key not in cfg:
            return False
        del cfg[key]
        return True


def _probe_command(host: str, args: list[str]) -> str:
    return shlex.join(["hop", "-H", host, *args])


def _unavailable(host: str, observed: str, recovery_args: list[str]) -> CandidateProbe:
    reason = f"{observed}; inspect with: {_probe_command(host, recovery_args)}"
    return CandidateProbe(host=host, eligible=False, load=None, reason=reason)


def _run_candidate_probe(
    host: str,
    args: list[str],
    *,
    label: str,
    timeout: float,
    runner: RemoteRunner,
) -> tuple[dict[str, object] | None, CandidateProbe | None]:
    try:
        result = runner(host, args, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, _unavailable(host, f"{label} timed out", args)
    except OSError as error:
        return None, _unavailable(host, f"{label} transport failed: {error}", args)

    if result.returncode != 0:
        diagnostic = (result.stderr or result.stdout or "").strip()
        detail = diagnostic.splitlines()[0] if diagnostic else "no diagnostic"
        return None, _unavailable(
            host,
            f"{label} exited {result.returncode}: {detail}",
            args,
        )
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None, _unavailable(host, f"{label} returned malformed JSON", args)
    if not isinstance(payload, dict):
        return None, _unavailable(host, f"{label} returned a non-object JSON value", args)
    return payload, None


def _validate_project_readiness(
    host: str,
    project: str,
    payload: dict[str, object],
) -> CandidateProbe | None:
    args = ["project", "list", "--json"]
    rows = payload.get("projects")
    fields = {"name", "path", "disabled", "disabled_reason"}
    if set(payload) != {"projects"} or not isinstance(rows, list):
        return _unavailable(host, "project listing violated its JSON contract", args)

    matches = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != fields
            or not isinstance(row.get("name"), str)
            or not isinstance(row.get("path"), str)
            or not isinstance(row.get("disabled"), bool)
            or not isinstance(row.get("disabled_reason"), str)
        ):
            return _unavailable(host, "project listing contained a malformed project", args)
        if row["name"] == project:
            matches.append(row)

    if not matches:
        return _unavailable(host, f"project {project!r} was not registered", args)
    if len(matches) != 1:
        return _unavailable(host, f"project {project!r} appeared more than once", args)
    if matches[0]["disabled"]:
        recovery = ["project", "enable", project]
        return _unavailable(host, f"project {project!r} was disabled", recovery)
    return None


def _inventory_load(
    host: str,
    payload: dict[str, object],
) -> tuple[int | None, CandidateProbe | None]:
    args = ["lode", "list", "--json"]
    lodes = payload.get("lodes")
    if set(payload) != {"lodes"} or not isinstance(lodes, list):
        return None, _unavailable(host, "lode inventory violated its JSON contract", args)

    load = 0
    for lode in lodes:
        if not isinstance(lode, dict) or not isinstance(lode.get("active"), bool):
            return None, _unavailable(
                host,
                "lode inventory contained a record without a boolean active field",
                args,
            )
        if lode["active"] is True:
            load += 1
    return load, None


def probe_candidate(
    host: str,
    project: str,
    runner: RemoteRunner,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> CandidateProbe:
    """Probe one pool member within one shared per-candidate deadline."""
    started = monotonic()
    project_args = ["project", "list", "--json"]
    payload, failure = _run_candidate_probe(
        host,
        project_args,
        label="project listing",
        timeout=REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC,
        runner=runner,
    )
    if failure is not None:
        return failure
    assert payload is not None
    if failure := _validate_project_readiness(host, project, payload):
        return failure

    remaining = REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC - (monotonic() - started)
    inventory_args = ["lode", "list", "--json"]
    if remaining <= 0:
        return _unavailable(
            host,
            "candidate deadline expired before lode inventory",
            inventory_args,
        )
    payload, failure = _run_candidate_probe(
        host,
        inventory_args,
        label="lode inventory",
        timeout=remaining,
        runner=runner,
    )
    if failure is not None:
        return failure
    assert payload is not None
    load, failure = _inventory_load(host, payload)
    if failure is not None:
        return failure
    assert load is not None
    return CandidateProbe(host=host, eligible=True, load=load, reason=None)


def probe_candidates(
    hosts: Sequence[str],
    project: str,
    runner: RemoteRunner,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[CandidateProbe]:
    """Probe unique pool members concurrently under one aggregate deadline."""
    ordered_hosts = list(dict.fromkeys(hosts))
    if not ordered_hosts:
        return []

    deadline = monotonic() + REMOTE_POOL_PROBE_TIMEOUT_SEC
    executor = ThreadPoolExecutor(max_workers=len(ordered_hosts))
    futures = {
        executor.submit(probe_candidate, host, project, runner): host for host in ordered_hosts
    }
    try:
        remaining = max(0.0, deadline - monotonic())
        done, pending = wait(futures, timeout=remaining)
        results: dict[str, CandidateProbe] = {}
        for future in done:
            host = futures[future]
            try:
                results[host] = future.result()
            except Exception as error:
                results[host] = _unavailable(
                    host,
                    f"candidate probe failed unexpectedly: {error}",
                    ["project", "list", "--json"],
                )
        for future in pending:
            host = futures[future]
            future.cancel()
            results[host] = _unavailable(
                host,
                "aggregate pool probe deadline expired",
                ["project", "list", "--json"],
            )
        return [results[host] for host in ordered_hosts]
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def select_candidate(
    probes: Sequence[CandidateProbe],
    chooser: Callable[[Sequence[CandidateProbe]], CandidateProbe] = random.choice,
) -> CandidateProbe | None:
    """Choose among eligible candidates tied at the minimum observed load."""
    eligible = [probe for probe in probes if probe.eligible and probe.load is not None]
    if not eligible:
        return None
    minimum = min(probe.load for probe in eligible if probe.load is not None)
    tied = [probe for probe in eligible if probe.load == minimum]
    return chooser(tied)


def _discover_host(
    host: str,
    args: list[str],
    runner: RemoteRunner,
) -> HostDiscovery:
    """Read and validate one remote lode-list response."""
    try:
        result = runner(host, args, timeout=REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return HostDiscovery(host, (), "lode listing timed out")
    except OSError as error:
        return HostDiscovery(host, (), f"lode listing transport failed: {error}")

    if result.returncode != 0:
        diagnostic = (result.stderr or result.stdout or "").strip()
        detail = diagnostic.splitlines()[0] if diagnostic else "no diagnostic"
        return HostDiscovery(
            host,
            (),
            f"lode listing exited {result.returncode}: {detail}",
        )
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return HostDiscovery(host, (), "lode listing returned malformed JSON")
    if not isinstance(payload, dict) or set(payload) != {"lodes"}:
        return HostDiscovery(host, (), "lode listing violated its JSON contract")
    rows = payload["lodes"]
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return HostDiscovery(host, (), "lode listing contained a malformed lode")
    return HostDiscovery(host, tuple(dict(row) for row in rows), None)


def discover_lodes(
    hosts: Sequence[str],
    args: list[str],
    runner: RemoteRunner,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[HostDiscovery]:
    """Discover lodes concurrently under one pool-wide deadline."""
    ordered_hosts = list(dict.fromkeys(hosts))
    if not ordered_hosts:
        return []

    deadline = monotonic() + REMOTE_POOL_PROBE_TIMEOUT_SEC
    executor = ThreadPoolExecutor(max_workers=len(ordered_hosts))
    futures = {executor.submit(_discover_host, host, args, runner): host for host in ordered_hosts}
    try:
        done, pending = wait(futures, timeout=max(0.0, deadline - monotonic()))
        results: dict[str, HostDiscovery] = {}
        for future in done:
            host = futures[future]
            try:
                results[host] = future.result()
            except Exception as error:
                results[host] = HostDiscovery(
                    host,
                    (),
                    f"lode listing failed unexpectedly: {error}",
                )
        for future in pending:
            host = futures[future]
            future.cancel()
            results[host] = HostDiscovery(
                host,
                (),
                "aggregate discovery deadline expired",
            )
        return [results[host] for host in ordered_hosts]
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def remote_lode_cache_path() -> Path:
    """Return the remote lode cache path."""
    return config.hopper_dir() / "remote-lodes.json"


def remote_lode_cache_lock_path() -> Path:
    """Return the lock path for remote lode cache transactions."""
    return config.hopper_dir() / "remote-lodes.lock"


@contextmanager
def _lode_cache_lock() -> Iterator[None]:
    """Serialize cache transactions with a bounded persistent lock."""
    lock_path = remote_lode_cache_lock_path()
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(lock_path, "a+")
    except OSError as error:
        raise LodeCacheError(lock_path, "unreadable") from error
    try:
        deadline = time.monotonic() + REMOTE_CACHE_LOCK_TIMEOUT_SEC
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LodeCacheError(lock_path, "locked") from error
                time.sleep(min(REMOTE_CACHE_LOCK_POLL_INTERVAL_SEC, remaining))
            except OSError as error:
                raise LodeCacheError(lock_path, "unreadable") from error
        yield
    finally:
        lock_file.close()


def load_lode_cache() -> dict[str, dict]:
    """Load the resident-route cache strictly and migrate legacy timestamps."""
    cache, needs_migration = _read_lode_cache()
    if not needs_migration:
        return cache

    with _lode_cache_lock():
        cache, needs_migration = _read_lode_cache()
        if needs_migration:
            migrated = _migrate_lode_cache(cache)
            try:
                save_lode_cache(migrated)
            except OSError as error:
                raise LodeCacheError(remote_lode_cache_path(), "unreadable") from error
            cache = migrated
    return cache


def _valid_lode_id(lode_id: object) -> bool:
    return (
        isinstance(lode_id, str)
        and len(lode_id) == ID_LEN
        and all(character in ID_ALPHABET for character in lode_id)
    )


def _valid_timestamp(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _read_lode_cache() -> tuple[dict[str, dict], bool]:
    """Read and validate cache bytes without acquiring the transaction lock."""
    path = remote_lode_cache_path()
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}, False
    except OSError as error:
        raise LodeCacheError(path, "unreadable") from error
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise LodeCacheError(path, "malformed") from error
    if not isinstance(raw, dict):
        raise LodeCacheError(path, "wrong_shape")

    cache: dict[str, dict] = {}
    needs_migration = False
    for lode_id, entry in raw.items():
        if not _valid_lode_id(lode_id) or not isinstance(entry, dict):
            raise LodeCacheError(path, "wrong_shape")
        host = entry.get("host")
        project = entry.get("project")
        if (
            not isinstance(host, str)
            or not host.strip()
            or host != host.strip()
            or not isinstance(project, str)
        ):
            raise LodeCacheError(path, "wrong_shape")
        has_created_ms = "created_ms" in entry
        has_created_at = "created_at" in entry
        if has_created_ms == has_created_at:
            raise LodeCacheError(path, "wrong_shape")
        created = entry.get("created_ms") if has_created_ms else entry.get("created_at")
        if not _valid_timestamp(created):
            raise LodeCacheError(path, "wrong_shape")
        if "last_seen_ms" in entry and not _valid_timestamp(entry["last_seen_ms"]):
            raise LodeCacheError(path, "wrong_shape")
        needs_migration = needs_migration or has_created_at
        cache[lode_id] = dict(entry)
    return cache, needs_migration


def _migrate_lode_cache(cache: dict[str, dict]) -> dict[str, dict]:
    """Return a copy with legacy created_at fields converted to created_ms."""
    migrated: dict[str, dict] = {}
    for lode_id, entry in cache.items():
        migrated_entry = dict(entry)
        if "created_at" in migrated_entry:
            migrated_entry["created_ms"] = migrated_entry.pop("created_at")
        migrated[lode_id] = migrated_entry
    return migrated


def save_lode_cache(cache: dict[str, dict]) -> None:
    """Save the lode id -> host cache atomically."""
    data_dir = config.hopper_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    path = remote_lode_cache_path()
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=data_dir)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(cache, indent=2, sort_keys=True) + "\n")
            stream.flush()
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def prune_lode_cache(cache: dict[str, dict], now_ms: int | None = None) -> dict[str, dict]:
    """Drop cache entries older than the retention window."""
    now = current_time_ms() if now_ms is None else now_ms
    pruned: dict[str, dict] = {}
    for lode_id, entry in cache.items():
        created = entry["created_ms"]
        if now - created < REMOTE_LODE_CACHE_MAX_AGE_MS:
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
    created = now if created_ms is None else created_ms
    if (
        not _valid_lode_id(lode_id)
        or not isinstance(host, str)
        or not host.strip()
        or host != host.strip()
        or not isinstance(project, str)
        or not _valid_timestamp(created)
    ):
        raise LodeCacheError(remote_lode_cache_path(), "wrong_shape")
    with _lode_cache_lock():
        cache, needs_migration = _read_lode_cache()
        migrated = _migrate_lode_cache(cache)
        cache = prune_lode_cache(migrated, now)
        changed = needs_migration or cache != migrated
        existing = cache.get(lode_id)
        if existing and existing.get("host") == host:
            if changed:
                save_lode_cache(cache)
            return

        if existing and "created_ms" in existing:
            created = existing["created_ms"]
        cache[lode_id] = {
            "host": host,
            "project": project,
            "created_ms": created,
            "last_seen_ms": now,
        }
        save_lode_cache(cache)
