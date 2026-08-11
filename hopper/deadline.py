# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared monotonic deadline helpers for bounded operations."""

import math
import time
from collections.abc import Callable


def make_deadline(timeout_s: float, *, clock: Callable[[], float] = time.monotonic) -> dict:
    """Return one absolute monotonic deadline backed by an injectable clock."""
    timeout = float(timeout_s)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("deadline timeout must be finite and greater than zero")
    return {"expires_at": clock() + timeout, "clock": clock}


def remaining_seconds(deadline: dict) -> float:
    """Return signed seconds remaining on a deadline."""
    return float(deadline["expires_at"]) - float(deadline["clock"]())


def claim_call_budget(
    deadline: dict,
    operation: str,
    *,
    cap_s: float | None = None,
) -> float | None:
    """Authorize one blocking call and return its positive remaining budget."""
    del operation  # Stable test/debug label; authorization depends only on time.
    remaining = remaining_seconds(deadline)
    if remaining <= 0:
        return None
    if cap_s is None:
        return remaining
    cap = float(cap_s)
    if not math.isfinite(cap) or cap <= 0:
        return None
    return min(remaining, cap)


def shorten_deadline(deadline: dict, expires_at: float) -> dict:
    """Return a deadline that cannot outlive the supplied absolute expiration."""
    return {
        "expires_at": min(float(deadline["expires_at"]), float(expires_at)),
        "clock": deadline["clock"],
    }
