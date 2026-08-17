# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the refine-stage coder dispatcher."""

import subprocess
from unittest.mock import patch

import pytest

from hopper.coder import (
    DEFAULT_CODER_PROVIDER,
    bootstrap_coder,
    coder_check,
    run_coder,
    validate_coder_provider,
)


def test_grok_is_the_default():
    assert DEFAULT_CODER_PROVIDER == "grok"


@pytest.mark.parametrize("provider", [None, "", "claude", 3])
def test_validate_coder_provider_rejects_unknown_values(provider):
    with pytest.raises(ValueError, match="codex, grok"):
        validate_coder_provider(provider)


def test_bootstrap_dispatches_to_grok_without_model_configuration():
    with patch("hopper.grok.bootstrap_grok", return_value=(0, "session", None)) as bootstrap:
        result = bootstrap_coder("grok", "prompt", "/tmp", env={"PATH": "/bin"})

    assert result == (0, "session", None)
    bootstrap.assert_called_once_with("prompt", "/tmp", env={"PATH": "/bin"})


def test_run_dispatches_to_codex_with_existing_contract():
    callback = object()
    with patch("hopper.codex.run_codex", return_value=(0, ["codex"])) as run:
        result = run_coder(
            "codex",
            "prompt",
            "/tmp",
            "/tmp/out.md",
            "session",
            on_event=callback,
        )

    assert result == (0, ["codex"])
    run.assert_called_once_with(
        "prompt",
        "/tmp",
        "/tmp/out.md",
        "session",
        env=None,
        on_event=callback,
    )


def test_coder_check_reports_missing_executable():
    with patch("hopper.coder.shutil.which", return_value=None):
        result = coder_check("grok")

    assert result == {
        "provider": "grok",
        "ready": False,
        "version": "",
        "error": "grok command not found",
    }


def test_grok_coder_check_uses_machine_readable_version_without_auth_or_model():
    completed = subprocess.CompletedProcess([], 0, stdout='{"version":"1.0.3"}\n', stderr="")
    with (
        patch("hopper.coder.shutil.which", return_value="/usr/bin/grok"),
        patch("hopper.coder.subprocess.run", return_value=completed) as run,
    ):
        result = coder_check("grok")

    assert result["ready"] is True
    assert result["version"] == '{"version":"1.0.3"}'
    command = run.call_args.args[0]
    assert command == ["grok", "version", "--json"]
    assert "--model" not in command
