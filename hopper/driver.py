# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Small interactive-stage driver selector."""

from types import ModuleType

# This set is runnable adapters, not every durable record value.
RUNNABLE_STAGE_DRIVERS = ("claude", "codex", "grok")
STAGE_DRIVER_CAPABILITIES_KEY = "stage_driver_capabilities"
STAGE_DRIVER_PROTOCOL_VERSION = 1


class DriverRefusal(ValueError):
    """Raised when Hopper has no runnable interactive-stage adapter."""


def resolve_driver(name: object) -> ModuleType:
    """Return the selected interactive-stage adapter or refuse before launch."""
    if name == "claude":
        from hopper import claude

        return claude
    if name == "codex":
        from hopper import codex

        return codex
    if name == "grok":
        from hopper import grok

        return grok
    raise DriverRefusal(
        f"interactive-stage driver {name!r} is unavailable; inspect the server version and lode."
    )
