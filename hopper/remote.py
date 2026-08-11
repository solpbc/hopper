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
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar

from hopper import config
from hopper.lodes import current_time_ms, is_canonical_lode_id

REMOTE_CONFIG_PREFIX = "remote."
REMOTE_LODE_CACHE_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000
REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC = 8.0
REMOTE_POOL_PROBE_TIMEOUT_SEC = 10.0
REMOTE_CREATE_TIMEOUT_SEC = 180.0
REMOTE_SET_PING_TIMEOUT_SEC = 15.0
REMOTE_CACHE_LOCK_TIMEOUT_SEC = 5.0
REMOTE_CACHE_LOCK_POLL_INTERVAL_SEC = 0.05
REMOTE_MAX_WORKERS = 16

RemoteRunner = Callable[..., subprocess.CompletedProcess[str]]
FanoutResult = TypeVar("FanoutResult")


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
    stdin_bytes: bytes | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run hop on a remote host over ssh and return the completed process."""
    if stdin_text is not None and stdin_bytes is not None:
        raise ValueError("remote stdin must be either text or bytes")
    command = _remote_command(host, hop_args)
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": stdin_bytes is None,
        "timeout": timeout,
    }
    if stdin_bytes is not None:
        kwargs["input"] = stdin_bytes
    elif stdin_text is not None:
        kwargs["input"] = stdin_text
    else:
        # Without an explicit stdin payload, ssh otherwise inherits this
        # process's real stdin and forwards it to the remote command. A pooled
        # create probes every candidate host before the one authoritative
        # create call reads the scope, and concurrent probes racing to drain
        # the same inherited pipe is what silently emptied it.
        kwargs["stdin"] = subprocess.DEVNULL
    result = subprocess.run(command, **kwargs)
    if stdin_bytes is None:
        return result
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        stdout=result.stdout.decode("utf-8", errors="replace"),
        stderr=result.stderr.decode("utf-8", errors="replace"),
    )


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
    if not _valid_host(host):
        raise ValueError(f"invalid remote host: {host!r}")
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
        "--",
        host,
        remote_command,
    ]


def _quote_remote_arg(arg: str) -> str:
    """Quote one hop arg, expanding an explicitly preserved tilde remotely."""
    if arg == "~":
        return '"$HOME"'
    if arg.startswith("~/"):
        return f'"$HOME"/{shlex.quote(arg[2:])}'
    return shlex.quote(arg)


def _valid_host(host: object) -> bool:
    """Reject SSH option-like, sentinel-conflicting, or control-bearing hosts."""
    return (
        isinstance(host, str)
        and bool(host)
        and host == host.strip()
        and not host.startswith("-")
        and host != "local"
        and not any(unicodedata.category(character) == "Cc" for character in host)
    )


def _normalized_pool(value: object) -> list[str] | None:
    """Validate one configured host pool without rewriting its entries."""
    if not isinstance(value, list) or not value:
        return None
    hosts: list[str] = []
    for host in value:
        if not _valid_host(host):
            return None
        hosts.append(host)  # type: ignore[arg-type]
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
    """Remove a project's configured host pool."""
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


def validate_lode_record(
    raw: object,
    *,
    expected_id: str | None = None,
    canonical_id: bool = True,
) -> dict | None:
    """Return one copied lode only when its routing-critical fields are typed."""
    if not isinstance(raw, dict):
        return None
    lode_id = raw.get("id")
    valid_id = (
        is_canonical_lode_id(lode_id)
        if canonical_id
        else isinstance(lode_id, str) and bool(lode_id)
    )
    if not valid_id or (expected_id is not None and lode_id != expected_id):
        return None
    if not all(
        isinstance(raw.get(field), str) for field in ("project", "stage", "state", "status")
    ):
        return None
    if not isinstance(raw.get("active"), bool):
        return None
    return dict(raw)


def validate_lode_records(raw: object, *, canonical_ids: bool = True) -> list[dict] | None:
    """Validate a complete lode collection; one malformed row rejects the source."""
    if not isinstance(raw, list):
        return None
    validated: list[dict] = []
    for row in raw:
        lode = validate_lode_record(row, canonical_id=canonical_ids)
        if lode is None:
            return None
        validated.append(lode)
    return validated


def _validate_lode_inventory(
    payload: dict[str, object],
    *,
    canonical_ids: bool = True,
) -> tuple[list[dict] | None, str | None]:
    """Validate the strict lode-list contract shared by probing and discovery."""
    lodes = payload.get("lodes")
    if set(payload) != {"lodes"} or not isinstance(lodes, list):
        return None, "lode inventory violated its JSON contract"

    validated = validate_lode_records(lodes, canonical_ids=canonical_ids)
    if validated is None:
        return None, "lode inventory contained a malformed lode record"
    return validated, None


def _inventory_load(
    host: str,
    payload: dict[str, object],
) -> tuple[int | None, CandidateProbe | None]:
    args = ["lode", "list", "--json"]
    lodes, reason = _validate_lode_inventory(payload)
    if reason is not None:
        return None, _unavailable(host, reason, args)
    assert lodes is not None
    return sum(lode["active"] is True for lode in lodes), None


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
    return _bounded_host_fanout(
        hosts,
        lambda host: probe_candidate(host, project, runner),
        lambda host, error: _unavailable(
            host,
            f"candidate probe failed unexpectedly: {error}",
            ["project", "list", "--json"],
        ),
        lambda host: _unavailable(
            host,
            "aggregate pool probe deadline expired",
            ["project", "list", "--json"],
        ),
        monotonic=monotonic,
    )


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
    if not isinstance(payload, dict):
        return HostDiscovery(host, (), "lode inventory violated its JSON contract")
    rows, reason = _validate_lode_inventory(payload, canonical_ids=host != "local")
    if reason is not None:
        return HostDiscovery(host, (), reason)
    assert rows is not None
    return HostDiscovery(host, tuple(rows), None)


def _bounded_host_fanout(
    hosts: Sequence[str],
    operation: Callable[[str], FanoutResult],
    unexpected: Callable[[str, Exception], FanoutResult],
    deadline_expired: Callable[[str], FanoutResult],
    *,
    monotonic: Callable[[], float],
) -> list[FanoutResult]:
    """Run one operation per unique host within the shared pool deadline."""
    ordered_hosts = list(dict.fromkeys(hosts))
    if not ordered_hosts:
        return []

    deadline = monotonic() + REMOTE_POOL_PROBE_TIMEOUT_SEC
    executor = ThreadPoolExecutor(max_workers=min(REMOTE_MAX_WORKERS, len(ordered_hosts)))
    futures = {}
    results: dict[str, FanoutResult] = {}
    try:
        for host in ordered_hosts:
            try:
                futures[executor.submit(operation, host)] = host
            except Exception as error:
                results[host] = unexpected(host, error)
        done, pending = wait(futures, timeout=max(0.0, deadline - monotonic()))
        for future in done:
            host = futures[future]
            try:
                results[host] = future.result()
            except Exception as error:
                results[host] = unexpected(host, error)
        for future in pending:
            host = futures[future]
            future.cancel()
            results[host] = deadline_expired(host)
        return [results[host] for host in ordered_hosts]
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def discover_lodes(
    hosts: Sequence[str],
    args: list[str],
    runner: RemoteRunner,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[HostDiscovery]:
    """Discover lodes concurrently under one pool-wide deadline."""
    return _bounded_host_fanout(
        hosts,
        lambda host: _discover_host(host, args, runner),
        lambda host, error: HostDiscovery(
            host,
            (),
            f"lode listing failed unexpectedly: {error}",
        ),
        lambda host: HostDiscovery(
            host,
            (),
            "aggregate discovery deadline expired",
        ),
        monotonic=monotonic,
    )


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
                _save_lode_cache(migrated)
            except OSError as error:
                raise LodeCacheError(remote_lode_cache_path(), "unreadable") from error
            cache = migrated
    return cache


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
        if not is_canonical_lode_id(lode_id) or not isinstance(entry, dict):
            raise LodeCacheError(path, "wrong_shape")
        host = entry.get("host")
        project = entry.get("project")
        if not _valid_host(host) or not isinstance(project, str):
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
        needs_migration = needs_migration or has_created_at or "last_seen_ms" not in entry
        cache[lode_id] = dict(entry)
    return cache, needs_migration


def _migrate_lode_cache(cache: dict[str, dict]) -> dict[str, dict]:
    """Return a copy with legacy created_at fields converted to created_ms."""
    migrated: dict[str, dict] = {}
    for lode_id, entry in cache.items():
        migrated_entry = dict(entry)
        if "created_at" in migrated_entry:
            migrated_entry["created_ms"] = migrated_entry.pop("created_at")
        migrated_entry.setdefault("last_seen_ms", migrated_entry["created_ms"])
        migrated[lode_id] = migrated_entry
    return migrated


def _save_lode_cache(cache: dict[str, dict]) -> None:
    """Publish the resident-route cache while its transaction lock is held."""
    data_dir = config.hopper_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    path = remote_lode_cache_path()
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=data_dir)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(cache, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(data_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
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
        last_seen = entry.get("last_seen_ms", entry["created_ms"])
        if now - last_seen < REMOTE_LODE_CACHE_MAX_AGE_MS:
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
        not is_canonical_lode_id(lode_id)
        or not _valid_host(host)
        or not isinstance(project, str)
        or not _valid_timestamp(created)
    ):
        raise LodeCacheError(remote_lode_cache_path(), "wrong_shape")
    with _lode_cache_lock():
        cache, _needs_migration = _read_lode_cache()
        migrated = _migrate_lode_cache(cache)
        cache = prune_lode_cache(migrated, now)
        existing = cache.get(lode_id)
        if existing and existing.get("host") == host:
            existing["project"] = project
            existing["last_seen_ms"] = now
            _save_lode_cache(cache)
            return

        if existing and "created_ms" in existing:
            created = existing["created_ms"]
        cache[lode_id] = {
            "host": host,
            "project": project,
            "created_ms": created,
            "last_seen_ms": now,
        }
        _save_lode_cache(cache)


def forget_lode(lode_id: str) -> bool:
    """Forget one confirmed-stale resident route under the cache lock."""
    if not is_canonical_lode_id(lode_id):
        raise LodeCacheError(remote_lode_cache_path(), "wrong_shape")
    with _lode_cache_lock():
        cache, _needs_migration = _read_lode_cache()
        cache = _migrate_lode_cache(cache)
        if lode_id not in cache:
            return False
        del cache[lode_id]
        _save_lode_cache(cache)
        return True
