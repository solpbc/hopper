# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Linux systemd/cgroup boundary for per-lode OOM handling."""

import logging
import shutil
import subprocess
import sys
import time
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

OOM_DEGRADED_WARNING = (
    "Hopper OOM guard degraded: per-lode cgroup memory events are unavailable; "
    "OOM priority is active, but group kill and systemd-confirmed OOM reporting are not."
)
OOM_SCORE_DEGRADED_WARNING = (
    "Hopper OOM guard degraded: failed to set and verify oom_score_adj=500; "
    "this lode has no Hopper-managed OOM protection."
)
SYSTEMCTL_TIMEOUT_SEC = 1.0
# Keep this below GUARDED_DISCONNECT_HOLD_SEC = 2.0 in hopper/server.py so the
# supervisor normally reports the settled result within the live server hold.
SCOPE_RESULT_SETTLE_SEC = 1.5
SCOPE_RESULT_POLL_SEC = 0.05


class OomCapability(Enum):
    """Observed OOM-guard capability for one runner launch."""

    SUPPORTED = "supported"
    DEGRADED_NO_CONTROLLER = "degraded-no-controller"
    DEGRADED_NO_SCORE = "degraded-no-score"
    NON_LINUX = "non-linux"


def is_linux() -> bool:
    """Return whether Linux-only OOM guarding is available in principle."""
    return sys.platform.startswith("linux")


def scope_unit_name(lode_id: str, run_generation: str) -> str:
    """Build the deterministic systemd scope name for one lode generation."""
    safe_parts: list[str] = []
    for byte in lode_id.lower().encode("utf-8"):
        char = chr(byte)
        safe_parts.append(char if char.isascii() and char.isalnum() else f"x{byte:02x}")
    safe_lode_id = "".join(safe_parts)
    if not safe_lode_id or len(safe_lode_id) > 32:
        raise ValueError("lode id cannot be represented in a bounded systemd unit name")
    if len(run_generation) != 32 or any(char not in "0123456789abcdef" for char in run_generation):
        raise ValueError("run generation must be 32 lowercase hexadecimal characters")
    return f"hopper-lode-{safe_lode_id}-{run_generation}.scope"


def find_scope_tools() -> tuple[str, str] | None:
    """Return systemd-run and systemctl paths, or None when unavailable."""
    if not is_linux():
        return None
    systemd_run = shutil.which("systemd-run")
    systemctl = find_systemctl()
    if not systemd_run or not systemctl:
        return None
    return systemd_run, systemctl


def find_systemctl() -> str | None:
    """Return the systemctl path used to inspect retained scope evidence."""
    if not is_linux():
        return None
    return shutil.which("systemctl")


def find_hop_executable() -> str | None:
    """Resolve the hop console script used by the inner worker command."""
    return shutil.which("hop")


def build_scope_argv(
    systemd_run: str,
    hop_executable: str,
    unit_name: str,
    lode_id: str,
) -> list[str]:
    """Build the exact transient-scope launch argument vector."""
    return [
        systemd_run,
        "--user",
        "--scope",
        f"--unit={unit_name}",
        "--property=OOMPolicy=kill",
        "--",
        hop_executable,
        "process-worker",
        lode_id,
    ]


def launch_scope(argv: list[str]) -> int:
    """Run a synchronous systemd scope, deliberately unbounded for the worker lifetime."""
    return subprocess.run(argv).returncode


def read_scope_result(
    systemctl: str,
    unit_name: str,
    *,
    timeout: float = SYSTEMCTL_TIMEOUT_SEC,
) -> str | None:
    """Read a scope's authoritative Result property."""
    try:
        result = subprocess.run(
            [
                systemctl,
                "--user",
                "show",
                unit_name,
                "--property=Result",
                "--value",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def settle_scope_result(systemctl: str, unit_name: str) -> str | None:
    """Wait briefly for a failed scope's authoritative Result property to settle.

    None and "success" are treated as transient because the caller invokes this
    only after a nonzero worker exit.
    """
    deadline = time.monotonic() + SCOPE_RESULT_SETTLE_SEC
    result = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return result
        result = read_scope_result(
            systemctl,
            unit_name,
            timeout=min(SYSTEMCTL_TIMEOUT_SEC, remaining),
        )
        if result not in (None, "success"):
            return result
        time.sleep(min(SCOPE_RESULT_POLL_SEC, remaining))


def release_scope(systemctl: str, unit_name: str) -> bool:
    """Reset a durably consumed failed transient unit."""
    try:
        result = subprocess.run(
            [systemctl, "--user", "reset-failed", unit_name],
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        logger.warning("failed to reset systemd unit %s: %s", unit_name, result.stderr.strip())
        return False
    return True


def _read_text(path: Path) -> str:
    """Read a procfs/cgroup text file through one mockable seam."""
    return path.read_text()


def _write_text(path: Path, value: str) -> None:
    """Write a procfs control value through one mockable seam."""
    path.write_text(value)


def _cgroup2_process_path(
    cgroup_text: str,
    mountinfo_text: str,
) -> Path | None:
    """Resolve the current process cgroup directory in a unified hierarchy."""
    relative = None
    for line in cgroup_text.splitlines():
        if line.startswith("0::"):
            relative = line[3:]
            break
    if relative is None:
        return None

    for line in mountinfo_text.splitlines():
        before, separator, after = line.partition(" - ")
        if not separator or not after.startswith("cgroup2 "):
            continue
        fields = before.split()
        if len(fields) < 5:
            continue
        mount_root = fields[3]
        mount_point = Path(fields[4])
        process_path = Path("/") / relative.lstrip("/")
        root_path = Path(mount_root)
        try:
            below_root = process_path.relative_to(root_path)
        except ValueError:
            continue
        resolved = mount_point / below_root
        try:
            resolved.relative_to(mount_point)
        except ValueError:
            continue
        return resolved
    return None


def _memory_oom_group_is_armed() -> bool:
    """Read memory.oom.group once for the current process cgroup."""
    try:
        cgroup_text = _read_text(Path("/proc/self/cgroup"))
        mountinfo_text = _read_text(Path("/proc/self/mountinfo"))
        cgroup_path = _cgroup2_process_path(cgroup_text, mountinfo_text)
        if cgroup_path is None:
            return False
        return _read_text(cgroup_path / "memory.oom.group").strip() == "1"
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError, ValueError):
        return False


def _set_oom_score() -> bool:
    """Write and verify the current worker's oom_score_adj."""
    score_path = Path("/proc/self/oom_score_adj")
    try:
        _write_text(score_path, "500")
        return int(_read_text(score_path).strip()) == 500
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError, ValueError):
        return False


def arm_worker(*, expect_scope: bool) -> OomCapability:
    """Arm OOM preference and, when expected, verify systemd group containment."""
    if not is_linux():
        return OomCapability.NON_LINUX
    group_armed = _memory_oom_group_is_armed() if expect_scope else False
    if not _set_oom_score():
        return OomCapability.DEGRADED_NO_SCORE
    if not group_armed:
        return OomCapability.DEGRADED_NO_CONTROLLER
    return OomCapability.SUPPORTED


def warning_for(capability: OomCapability) -> str | None:
    """Return the one operator-facing warning for a degraded Linux launch."""
    if capability is OomCapability.DEGRADED_NO_CONTROLLER:
        return OOM_DEGRADED_WARNING
    if capability is OomCapability.DEGRADED_NO_SCORE:
        return OOM_SCORE_DEGRADED_WARNING
    return None
