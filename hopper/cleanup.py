# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Cleanup for platform helper processes that outlive their owning lode."""

import logging
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

SWIFTPM_TESTING_HELPER = "swiftpm-testing-helper"
PROCESS_SCAN_TIMEOUT_SEC = 5
ORPHAN_TERM_GRACE_SEC = 1.0


def _orphan_process_pids(executable_name: str) -> list[int]:
    """Return PPID-1 processes whose argv0 basename exactly matches a name."""
    try:
        result = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=PROCESS_SCAN_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("orphan process scan failed: %s: %s", type(exc).__name__, exc)
        return []
    if result.returncode != 0:
        logger.warning("orphan process scan failed with exit code %s", result.returncode)
        return []

    matches = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) != 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            argv = shlex.split(parts[2])
        except ValueError:
            # int() and shlex.split() both raise ValueError. shlex has no Error
            # attribute, so naming one here made this handler itself raise
            # AttributeError on the first ps line with an unbalanced quote —
            # which any lode's own argv supplies, since the prompt is embedded
            # in it and English prose contains apostrophes.
            continue
        if ppid != 1 or not argv:
            continue
        if Path(argv[0]).name == executable_name:
            matches.append(pid)
    return matches


def reap_swiftpm_testing_helpers() -> list[int]:
    """Reap orphaned SwiftPM testing helpers on macOS and return confirmed-gone PIDs."""
    if sys.platform != "darwin":
        return []

    targets = set(_orphan_process_pids(SWIFTPM_TESTING_HELPER))
    if not targets:
        return []
    # Narrow the signal set against a second authoritative scan. A helper can
    # exit between discovery and cleanup; never signal a reused PID on the
    # strength of an earlier process-list snapshot.
    targets.intersection_update(_orphan_process_pids(SWIFTPM_TESTING_HELPER))
    if not targets:
        return []

    for pid in sorted(targets):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            logger.warning("permission denied terminating SwiftPM helper pid=%s", pid)

    time.sleep(ORPHAN_TERM_GRACE_SEC)
    survivors = targets.intersection(_orphan_process_pids(SWIFTPM_TESTING_HELPER))
    for pid in sorted(survivors):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except PermissionError:
            logger.warning("permission denied killing SwiftPM helper pid=%s", pid)

    remaining = targets.intersection(_orphan_process_pids(SWIFTPM_TESTING_HELPER))
    reaped = targets - remaining
    if reaped:
        logger.warning(
            "reaped %s orphaned SwiftPM testing helper%s: %s",
            len(reaped),
            "" if len(reaped) == 1 else "s",
            ", ".join(str(pid) for pid in sorted(reaped)),
        )
    if remaining:
        logger.error(
            "SwiftPM testing helpers survived cleanup: %s",
            ", ".join(str(pid) for pid in sorted(remaining)),
        )
    return sorted(reaped)
