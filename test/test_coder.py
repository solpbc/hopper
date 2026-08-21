# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the refine-stage coder dispatcher."""

import json
import subprocess
from unittest.mock import patch

import pytest

from hopper.coder import (
    DEFAULT_CODER_PROVIDER,
    CoderDefaultRefusal,
    bootstrap_coder,
    coder_check,
    coder_default_refusal_lines,
    coder_unavailable_message,
    resolve_coder_default,
    run_coder,
    set_coder_default,
    validate_coder_provider,
)


def test_codex_is_the_default():
    assert DEFAULT_CODER_PROVIDER == "codex"


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        (None, ("codex", "built in")),
        ("codex", ("codex", "saved")),
        ("grok", ("grok", "saved")),
    ],
)
def test_resolve_coder_default_uses_builtin_or_saved_provider(temp_config, saved, expected):
    if saved is not None:
        (temp_config / "config.json").write_text(json.dumps({"coder.default": saved}))

    assert resolve_coder_default() == expected


@pytest.mark.parametrize("saved", ["unsupported", 42, "42"])
def test_resolve_coder_default_refuses_invalid_saved_values(temp_config, saved):
    path = temp_config / "config.json"
    path.write_text(json.dumps({"coder.default": saved}))

    with pytest.raises(CoderDefaultRefusal) as raised:
        resolve_coder_default()

    assert coder_default_refusal_lines(raised.value) == [
        "error: refine coder default refused",
        (
            f"observed: config key 'coder.default' in {path} is {saved!r}, "
            "which is not a supported coder."
        ),
        "Hopper did not change config.json and did not select a coder.",
        "recover with: hop coder default codex",
    ]


def test_set_coder_default_replaces_existing_non_string_value(temp_config):
    path = temp_config / "config.json"
    path.write_text('{"coder.default": 42}\n')

    set_coder_default("grok")

    assert json.loads(path.read_text()) == {"coder.default": "grok"}


@pytest.mark.parametrize("provider", ["unsupported", 42])
def test_set_coder_default_refuses_unsupported_provider_before_transaction(provider):
    with patch("hopper.coder.config.config_transaction") as transaction:
        with pytest.raises(CoderDefaultRefusal) as raised:
            set_coder_default(provider)

    assert raised.value.observed == (
        f"requested coder {provider!r} is not supported; see `hop coder --help`."
    )
    transaction.assert_not_called()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("codex command not found", "codex unavailable: codex command not found"),
        ("", "codex unavailable: readiness check returned no diagnostic"),
        (None, "codex unavailable: readiness check returned no diagnostic"),
    ],
)
def test_coder_unavailable_message_always_names_provider_and_diagnostic(error, expected):
    assert coder_unavailable_message("codex", error) == expected


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


def test_coder_check_reports_oserror_without_changing_contract():
    with (
        patch("hopper.coder.shutil.which", return_value="/usr/bin/codex"),
        patch("hopper.coder.subprocess.run", side_effect=OSError("permission denied")),
    ):
        result = coder_check("codex")

    assert result == {
        "provider": "codex",
        "ready": False,
        "version": "",
        "error": "version check failed: permission denied",
    }


def test_coder_check_reports_timeout_without_changing_contract():
    timeout = subprocess.TimeoutExpired(["codex", "--version"], 5.0)
    with (
        patch("hopper.coder.shutil.which", return_value="/usr/bin/codex"),
        patch("hopper.coder.subprocess.run", side_effect=timeout),
    ):
        result = coder_check("codex")

    assert result == {
        "provider": "codex",
        "ready": False,
        "version": "",
        "error": f"version check failed: {timeout}",
    }


def test_coder_check_reports_nonzero_without_changing_contract():
    completed = subprocess.CompletedProcess([], 7, stdout="", stderr="")
    with (
        patch("hopper.coder.shutil.which", return_value="/usr/bin/codex"),
        patch("hopper.coder.subprocess.run", return_value=completed),
    ):
        result = coder_check("codex")

    assert result == {
        "provider": "codex",
        "ready": False,
        "version": "",
        "error": "version check failed: exit 7",
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
