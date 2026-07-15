# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for remote hopper helpers."""

import fcntl
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_remember_lode_same_host_does_not_publish(temp_config, monkeypatch):
    existing = {
        "knownid": {
            "host": "fedora.local",
            "project": "journal",
            "created_ms": 100,
        }
    }
    config.hopper_dir().mkdir(parents=True, exist_ok=True)
    (temp_config / "remote-lodes.json").write_text(json.dumps(existing) + "\n")
    published = []
    times = iter([200, 300])
    monkeypatch.setattr("hopper.remote.current_time_ms", lambda: next(times))
    monkeypatch.setattr("hopper.remote.save_lode_cache", lambda cache: published.append(cache))

    remember_lode("knownid", "fedora.local", "renamed-project")
    remember_lode("knownid", "fedora.local", "another-project")

    assert published == []
    assert load_lode_cache() == existing


def test_remember_lode_host_change_preserves_created_ms(temp_config, monkeypatch):
    existing = {
        "knownid": {
            "host": "old.local",
            "project": "journal",
            "created_ms": 100,
        }
    }
    config.hopper_dir().mkdir(parents=True, exist_ok=True)
    (temp_config / "remote-lodes.json").write_text(json.dumps(existing) + "\n")
    monkeypatch.setattr("hopper.remote.current_time_ms", lambda: 200)

    remember_lode("knownid", "new.local", "journal")

    entry = load_lode_cache()["knownid"]
    assert entry == {
        "host": "new.local",
        "project": "journal",
        "created_ms": 100,
        "last_seen_ms": 200,
    }


def test_concurrent_remember_lode_processes_preserve_complete_cache(tmp_path):
    """Concurrent cache transactions serialize their read/merge/publish steps."""
    child_code = r"""
import json
import socket
import sys

import hopper.remote as remote

host, port, role, lode_id, remote_host = sys.argv[1:]
control = socket.create_connection((host, int(port)), timeout=10)
control_file = control.makefile("rwb", buffering=0)
initial = remote.load_lode_cache()
control_file.write(f"READY {role} {json.dumps(initial, sort_keys=True)}\n".encode())
assert control_file.readline() == b"GO\n"
if role == "B":
    control_file.write(b"ATTEMPT B\n")

original_save = remote.save_lode_cache

def synchronized_save(cache):
    payload = json.dumps(cache, sort_keys=True)
    control_file.write(f"PUBLISH_READY {role} {payload}\n".encode())
    assert control_file.readline() == b"RELEASE\n"
    original_save(cache)

remote.save_lode_cache = synchronized_save
remote.remember_lode(lode_id, remote_host, f"project-{role.lower()}")
control_file.write(f"DONE {role}\n".encode())
"""
    xdg_home = tmp_path / "xdg"
    data_dir = xdg_home / "hopper"
    data_dir.mkdir(parents=True)
    existing = {
        "existing-a": {
            "host": "one.local",
            "project": "one",
            "created_ms": 4_000_000_000_000,
        },
        "existing-b": {
            "host": "two.local",
            "project": "two",
            "created_ms": 4_000_000_000_001,
        },
    }
    cache_path = data_dir / "remote-lodes.json"
    cache_path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    listener.settimeout(10)
    host, port = listener.getsockname()
    repo_root = str(Path(__file__).resolve().parents[1])
    env = os.environ.copy()
    env["XDG_DATA_HOME"] = str(xdg_home)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in [repo_root, env.get("PYTHONPATH", "")] if part
    )
    child_args = [
        ("A", "writer-a", "alpha.local"),
        ("B", "writer-b", "beta.local"),
    ]
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", child_code, host, str(port), *args],
            cwd=repo_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for args in child_args
    ]
    controls = {}
    files = {}
    try:
        for _ in processes:
            connection, _ = listener.accept()
            connection.settimeout(10)
            control_file = connection.makefile("rwb", buffering=0)
            ready, role, payload = control_file.readline().decode().strip().split(" ", 2)
            assert ready == "READY"
            assert json.loads(payload) == existing
            controls[role] = connection
            files[role] = control_file

        files["A"].write(b"GO\n")
        publish, role, payload = files["A"].readline().decode().strip().split(" ", 2)
        assert (publish, role) == ("PUBLISH_READY", "A")
        assert set(json.loads(payload)) == {*existing, "writer-a"}

        lock_path = data_dir / "remote-lodes.lock"
        held_probe = open(lock_path, "a+")
        try:
            try:
                fcntl.flock(held_probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                pytest.fail("remote cache transaction did not hold remote-lodes.lock")
        finally:
            held_probe.close()

        files["B"].write(b"GO\n")
        assert files["B"].readline() == b"ATTEMPT B\n"
        files["A"].write(b"RELEASE\n")
        assert files["A"].readline() == b"DONE A\n"

        publish, role, payload = files["B"].readline().decode().strip().split(" ", 2)
        assert (publish, role) == ("PUBLISH_READY", "B")
        assert set(json.loads(payload)) == {*existing, "writer-a", "writer-b"}
        files["B"].write(b"RELEASE\n")
        assert files["B"].readline() == b"DONE B\n"

        results = [process.communicate(timeout=10) for process in processes]
        assert [process.returncode for process in processes] == [0, 0], results
    finally:
        for control_file in files.values():
            control_file.close()
        for connection in controls.values():
            connection.close()
        listener.close()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()

    cache = json.loads(cache_path.read_text())
    assert cache["existing-a"] == existing["existing-a"]
    assert cache["existing-b"] == existing["existing-b"]
    assert cache["writer-a"]["host"] == "alpha.local"
    assert cache["writer-b"]["host"] == "beta.local"
    assert not list(data_dir.glob("remote-lodes*.tmp"))
