# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for remote hopper helpers."""

import subprocess

from hopper import config
from hopper.remote import (
    load_lode_cache,
    remember_lode,
    remote_registry,
    remove_remote,
    run_remote,
    set_remote,
)


def test_run_remote_builds_ssh_command_and_passes_stdin(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 7, stdout="out", stderr="err")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_remote(
        "fedora.local",
        ["lode", "status", "abc 123", "quote'arg"],
        stdin_text="scope text",
        timeout=12,
    )

    assert result.returncode == 7
    command, kwargs = calls[0]
    assert command[:6] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "fedora.local",
    ]
    assert command[6] == "--"
    remote_command = command[7]
    assert remote_command.startswith('export HOP_NO_ROUTE=1; exec "$HOME/.local/bin/hop"')
    assert "$HOME" in remote_command
    assert "'abc 123'" in remote_command
    assert "'quote'\"'\"'arg'" in remote_command
    assert kwargs["input"] == "scope text"
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 12


def test_run_remote_inherits_stdin_when_none(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    run_remote("suze.local", ["ping"])

    assert "input" not in calls[0]


def test_run_remote_expands_preserved_tilde_on_remote(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    run_remote("fedora.local", ["project", "add", "~/src/my project"])

    remote_command = calls[0][7]
    assert '"$HOME"/' in remote_command
    assert "'src/my project'" in remote_command
    assert "~/src" not in remote_command


def test_remote_registry_set_remove():
    set_remote("solstone-android", "suze.local")

    assert remote_registry() == {"solstone-android": "suze.local"}
    assert remove_remote("solstone-android") is True
    assert remove_remote("solstone-android") is False
    assert remote_registry() == {}


def test_remember_lode_prunes_old_entries(temp_config):
    old = 1
    fresh = 30 * 24 * 60 * 60 * 1000
    config.hopper_dir().mkdir(parents=True, exist_ok=True)
    (temp_config / "remote-lodes.json").write_text(
        '{"oldid": {"host": "old.local", "created_ms": 1}}\n'
    )

    remember_lode("newid", "fedora.local", "solstone", created_ms=fresh)

    cache = load_lode_cache()
    assert "oldid" not in cache
    assert cache["newid"]["host"] == "fedora.local"
    assert cache["newid"]["project"] == "solstone"
    assert old < fresh
