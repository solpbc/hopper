# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Host-local defaults and readiness checks for interactive supervisors."""

import shutil
import subprocess

from hopper import config
from hopper.coder import CODER_CHECK_TIMEOUT_SEC, coder_check

SUPERVISOR_PROVIDERS = ("claude", "codex", "grok")
DEFAULT_SUPERVISOR_PROVIDER = "claude"


class SupervisorDefaultRefusal(Exception):
    """A host supervisor-default request that Hopper must refuse."""

    def __init__(self, observed: str) -> None:
        self.observed = observed
        super().__init__(observed)


def supervisor_default_refusal_lines(error: SupervisorDefaultRefusal) -> list[str]:
    """Return the shared user-facing refusal for supervisor-default operations."""
    return [
        "error: supervisor default refused",
        f"observed: {error.observed}",
        "Hopper did not change config.json and did not select a supervisor.",
        "recover with: hop supervisor default claude",
    ]


def _is_supported_supervisor(value: object) -> bool:
    return isinstance(value, str) and value in SUPERVISOR_PROVIDERS


def resolve_supervisor_default() -> tuple[str, str]:
    """Return the saved supervisor default or the built-in fallback and its source."""
    settings = config.load_config()
    if "supervisor.default" not in settings:
        return DEFAULT_SUPERVISOR_PROVIDER, "built in"
    provider = settings["supervisor.default"]
    if _is_supported_supervisor(provider):
        return provider, "saved"
    raise SupervisorDefaultRefusal(
        f"config key 'supervisor.default' in {config.config_path()} is {provider!r}, "
        "which is not a supported supervisor."
    )


def set_supervisor_default(provider: object) -> None:
    """Persist one supported host-local supervisor default."""
    if not _is_supported_supervisor(provider):
        raise SupervisorDefaultRefusal(
            f"requested supervisor {provider!r} is not supported; see `hop supervisor --help`."
        )
    with config.config_transaction() as settings:
        settings["supervisor.default"] = provider


def validate_supervisor_provider(provider: object) -> str:
    """Return a supported supervisor name or raise a user-facing ValueError."""
    if not _is_supported_supervisor(provider):
        choices = ", ".join(SUPERVISOR_PROVIDERS)
        raise ValueError(f"supervisor must be one of: {choices}")
    return str(provider)


def supervisor_unavailable_message(provider: str, error: object) -> str:
    """Return a provider-specific readiness failure message."""
    provider = validate_supervisor_provider(provider)
    diagnostic = (
        error if isinstance(error, str) and error else "readiness check returned no diagnostic"
    )
    return f"{provider} supervisor unavailable: {diagnostic}"


def supervisor_check(provider: str) -> dict:
    """Check whether one supervisor executable is locally runnable."""
    provider = validate_supervisor_provider(provider)
    if provider != "claude":
        return coder_check(provider)

    executable = shutil.which("claude")
    if executable is None:
        return {
            "provider": provider,
            "ready": False,
            "version": "",
            "error": "claude command not found",
        }
    try:
        result = subprocess.run(
            [provider, "--version"],
            capture_output=True,
            text=True,
            timeout=CODER_CHECK_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "provider": provider,
            "ready": False,
            "version": "",
            "error": f"version check failed: {error}",
        }
    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        detail = output.splitlines()[0] if output else f"exit {result.returncode}"
        return {
            "provider": provider,
            "ready": False,
            "version": "",
            "error": f"version check failed: {detail}",
        }
    return {"provider": provider, "ready": True, "version": output, "error": ""}
