# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Small provider dispatcher for Hopper's refine-stage coding agent."""

import shutil
import subprocess

from hopper import config

CODER_PROVIDERS = ("codex", "grok")
DEFAULT_CODER_PROVIDER = "codex"
CODER_CHECK_TIMEOUT_SEC = 5.0


class CoderDefaultRefusal(Exception):
    """A host coder-default request that Hopper must refuse."""

    def __init__(self, observed: str) -> None:
        self.observed = observed
        super().__init__(observed)


def coder_default_refusal_lines(error: CoderDefaultRefusal) -> list[str]:
    """Return the shared user-facing refusal for coder-default operations."""
    return [
        "error: refine coder default refused",
        f"observed: {error.observed}",
        "Hopper did not change config.json and did not select a coder.",
        "recover with: hop coder default codex",
    ]


def _is_supported_coder(value: object) -> bool:
    """Return whether a value names a supported refine coding provider."""
    return isinstance(value, str) and value in CODER_PROVIDERS


def resolve_coder_default() -> tuple[str, str]:
    """Return the saved coder default or the built-in fallback and its source."""
    settings = config.load_config()
    if "coder.default" not in settings:
        return DEFAULT_CODER_PROVIDER, "built in"
    provider = settings["coder.default"]
    if _is_supported_coder(provider):
        return provider, "saved"
    raise CoderDefaultRefusal(
        f"config key 'coder.default' in {config.config_path()} is {provider!r}, "
        "which is not a supported coder."
    )


def set_coder_default(provider: object) -> None:
    """Persist one supported host-local refine coder default."""
    if not _is_supported_coder(provider):
        raise CoderDefaultRefusal(
            f"requested coder {provider!r} is not supported; see `hop coder --help`."
        )
    with config.config_transaction() as settings:
        settings["coder.default"] = provider


def validate_coder_provider(provider: object) -> str:
    """Return a supported provider name or raise a user-facing ValueError."""
    if not _is_supported_coder(provider):
        choices = ", ".join(CODER_PROVIDERS)
        raise ValueError(f"coder must be one of: {choices}")
    return provider


def bootstrap_coder(provider: str, prompt: str, cwd: str, env: dict | None = None):
    """Bootstrap the selected provider and return its session result tuple."""
    provider = validate_coder_provider(provider)
    if provider == "codex":
        from hopper.codex import bootstrap_codex

        return bootstrap_codex(prompt, cwd, env=env)

    from hopper.grok import bootstrap_grok

    return bootstrap_grok(prompt, cwd, env=env)


def run_coder(
    provider: str,
    prompt: str,
    cwd: str,
    output_file: str,
    session_id: str,
    env: dict | None = None,
    on_event=None,
):
    """Resume the selected provider and return its process result tuple."""
    provider = validate_coder_provider(provider)
    if provider == "codex":
        from hopper.codex import run_codex

        return run_codex(
            prompt,
            cwd,
            output_file,
            session_id,
            env=env,
            on_event=on_event,
        )

    from hopper.grok import run_grok

    return run_grok(
        prompt,
        cwd,
        output_file,
        session_id,
        env=env,
        on_event=on_event,
    )


def coder_failure_message(provider: str, event: dict) -> str | None:
    """Return a provider failure message from one parsed stream event."""
    provider = validate_coder_provider(provider)
    if provider == "codex":
        from hopper.codex import turn_failed_message

        return turn_failed_message(event)

    from hopper.grok import grok_failure_message

    return grok_failure_message(event)


def coder_unavailable_message(provider: str, error: object) -> str:
    """Return a provider-specific readiness failure message."""
    provider = validate_coder_provider(provider)
    diagnostic = (
        error if isinstance(error, str) and error else "readiness check returned no diagnostic"
    )
    return f"{provider} unavailable: {diagnostic}"


def coder_check(provider: str) -> dict:
    """Check whether a provider executable is locally runnable without authenticating."""
    provider = validate_coder_provider(provider)
    executable = shutil.which(provider)
    if executable is None:
        return {
            "provider": provider,
            "ready": False,
            "version": "",
            "error": f"{provider} command not found",
        }

    command = [provider, "--version"] if provider == "codex" else [provider, "version", "--json"]
    try:
        result = subprocess.run(
            command,
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
