# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for remote hopper helpers."""

import fcntl
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from hopper import config
from hopper.cli import main
from hopper.projects import Project
from hopper.remote import (
    REMOTE_CANDIDATE_PROBE_TIMEOUT_SECONDS,
    REMOTE_POOL_PROBE_TIMEOUT_SECONDS,
    CandidateProbe,
    load_lode_cache,
    probe_candidate,
    probe_candidates,
    remember_lode,
    remote_registry,
    remove_remote,
    run_remote,
    select_candidate,
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
    set_remote("solstone-android", ["suze.local"])

    assert remote_registry() == {"solstone-android": ["suze.local"]}
    assert remove_remote("solstone-android") is True
    assert remove_remote("solstone-android") is False
    assert remote_registry() == {}


def test_remove_remote_absent_preserves_original_bytes(temp_config):
    path = temp_config / "config.json"
    original = b'{\n  "z-last": true,\n  "remote.other": ["other.example"]\n}\n'
    path.write_bytes(original)

    assert remove_remote("absent") is False
    assert path.read_bytes() == original


def test_remote_registry_migrates_all_scalars_and_preserves_unrelated_values(temp_config):
    path = temp_config / "config.json"
    path.write_text(
        json.dumps(
            {
                "name": "sol",
                "nested": {"keep": True},
                "remote.alpha": " alpha.example ",
                "remote.beta": "beta.example",
            }
        )
        + "\n"
    )

    assert remote_registry() == {
        "alpha": ["alpha.example"],
        "beta": ["beta.example"],
    }
    assert json.loads(path.read_text()) == {
        "name": "sol",
        "nested": {"keep": True},
        "remote.alpha": ["alpha.example"],
        "remote.beta": ["beta.example"],
    }


def test_remote_registry_refuses_whitespace_in_pool_without_rewriting(temp_config):
    path = temp_config / "config.json"
    original = b'{"remote.alpha": ["a ", "b"]}\n'
    path.write_bytes(original)

    with pytest.raises(config.ConfigError) as raised:
        remote_registry()

    assert raised.value.reason == "wrong_shape"
    assert path.read_bytes() == original


def test_remote_registry_trims_legacy_scalar_during_migration(temp_config):
    path = temp_config / "config.json"
    path.write_bytes(b'{"remote.alpha": " a "}\n')

    assert remote_registry() == {"alpha": ["a"]}
    assert json.loads(path.read_text()) == {"remote.alpha": ["a"]}


@pytest.mark.parametrize(
    "pool",
    [[], [1], [""], ["same.example", "same.example"]],
    ids=["empty", "non-string", "blank", "duplicate"],
)
def test_remote_registry_refuses_invalid_pool_without_rewriting(temp_config, pool):
    path = temp_config / "config.json"
    path.write_text(json.dumps({"name": "sol", "remote.alpha": pool}) + "\n")
    before = path.read_bytes()

    with pytest.raises(config.ConfigError) as raised:
        remote_registry()

    assert raised.value.reason == "wrong_shape"
    assert path.read_bytes() == before


def test_remote_registry_refuses_empty_scalar_without_rewriting(temp_config):
    path = temp_config / "config.json"
    path.write_bytes(b'{"remote.alpha": "   ", "keep": true}\n')
    before = path.read_bytes()

    with pytest.raises(config.ConfigError) as raised:
        remote_registry()

    assert raised.value.reason == "wrong_shape"
    assert path.read_bytes() == before


@pytest.fixture
def emitted_project_json(monkeypatch, capsys):
    """Capture the real local project-list JSON emitter for remote probe fixtures."""
    projects = [
        Project(path="/srv/other", name="other"),
        Project(path="/srv/journal", name="journal", disabled=False, disabled_reason=""),
    ]
    monkeypatch.setattr("hopper.projects.load_projects", lambda: projects)
    monkeypatch.setattr(sys, "argv", ["hop", "project", "list", "--json"])

    assert main() == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "projects": [
            {
                "name": "other",
                "path": "/srv/other",
                "disabled": False,
                "disabled_reason": "",
            },
            {
                "name": "journal",
                "path": "/srv/journal",
                "disabled": False,
                "disabled_reason": "",
            },
        ]
    }
    return output


@pytest.fixture
def emitted_lode_inventory_json(monkeypatch, capsys, make_lode):
    """Capture the real local lode-list JSON emitter for remote probe fixtures."""
    lodes = [
        make_lode(id="aaaaaaaa", project="journal", active=True),
        make_lode(id="bbbbbbbb", project="other", active=True),
        make_lode(id="cccccccc", project="journal", active=False),
    ]
    with (
        patch("hopper.cli.require_server", return_value=None),
        patch("hopper.client.list_lodes", return_value=lodes),
    ):
        monkeypatch.setattr(sys, "argv", ["hop", "lode", "list", "--json"])
        assert main() == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert [row["active"] for row in payload["lodes"]] == [True, True, False]
    return output


def test_probe_candidate_accepts_real_local_json_and_counts_all_active_lodes(
    emitted_project_json,
    emitted_lode_inventory_json,
):
    calls = []

    def runner(host, args, *, timeout):
        calls.append((host, args, timeout))
        stdout = emitted_project_json if args[0] == "project" else emitted_lode_inventory_json
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    probe = probe_candidate("ready.example", "journal", runner, monotonic=lambda: 0.0)

    assert probe == CandidateProbe("ready.example", eligible=True, load=2, reason=None)
    assert calls == [
        (
            "ready.example",
            ["project", "list", "--json"],
            REMOTE_CANDIDATE_PROBE_TIMEOUT_SECONDS,
        ),
        (
            "ready.example",
            ["lode", "list", "--json"],
            REMOTE_CANDIDATE_PROBE_TIMEOUT_SECONDS,
        ),
    ]


@pytest.mark.parametrize(
    ("project_payload", "reason"),
    [
        ({}, "JSON contract"),
        ({"projects": "not-a-list"}, "JSON contract"),
        ({"projects": [{"name": "journal"}]}, "malformed project"),
        (
            {
                "projects": [
                    {
                        "name": "other",
                        "path": "/srv/other",
                        "disabled": False,
                        "disabled_reason": "",
                    }
                ]
            },
            "not registered",
        ),
        (
            {
                "projects": [
                    {
                        "name": "journal",
                        "path": "/one",
                        "disabled": False,
                        "disabled_reason": "",
                    },
                    {
                        "name": "journal",
                        "path": "/two",
                        "disabled": False,
                        "disabled_reason": "",
                    },
                ]
            },
            "more than once",
        ),
        (
            {
                "projects": [
                    {
                        "name": "journal",
                        "path": "/srv/journal",
                        "disabled": True,
                        "disabled_reason": "maintenance",
                    }
                ]
            },
            "was disabled",
        ),
    ],
)
def test_probe_candidate_refuses_invalid_project_contract(project_payload, reason):
    def runner(_host, args, *, timeout):
        assert args == ["project", "list", "--json"]
        assert timeout == REMOTE_CANDIDATE_PROBE_TIMEOUT_SECONDS
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(project_payload), stderr="")

    probe = probe_candidate("bad.example", "journal", runner, monotonic=lambda: 0.0)

    assert probe.eligible is False
    assert probe.load is None
    assert reason in probe.reason
    assert "hop -H bad.example" in probe.reason


@pytest.mark.parametrize(
    ("inventory", "reason"),
    [
        ({}, "JSON contract"),
        ({"lodes": "not-a-list"}, "JSON contract"),
        ({"lodes": ["not-an-object"]}, "boolean active"),
        ({"lodes": [{}]}, "boolean active"),
        ({"lodes": [{"active": 1}]}, "boolean active"),
        ({"lodes": [{"active": "yes"}]}, "boolean active"),
    ],
)
def test_probe_candidate_refuses_invalid_inventory_contract(
    emitted_project_json,
    inventory,
    reason,
):
    def runner(_host, args, *, timeout):
        assert timeout == REMOTE_CANDIDATE_PROBE_TIMEOUT_SECONDS
        stdout = emitted_project_json if args[0] == "project" else json.dumps(inventory)
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    probe = probe_candidate("bad.example", "journal", runner, monotonic=lambda: 0.0)

    assert probe.eligible is False
    assert probe.load is None
    assert reason in probe.reason


@pytest.mark.parametrize("failed_call", ["project", "lode"])
@pytest.mark.parametrize("failure", ["malformed", "nonzero", "transport", "timeout"])
def test_probe_candidate_classifies_every_command_failure(
    emitted_project_json,
    emitted_lode_inventory_json,
    failed_call,
    failure,
):
    calls = []

    def runner(host, args, *, timeout):
        calls.append((args[0], timeout))
        if args[0] == failed_call:
            if failure == "malformed":
                return subprocess.CompletedProcess([], 0, stdout="{", stderr="")
            if failure == "nonzero":
                return subprocess.CompletedProcess([], 7, stdout="", stderr="remote failed")
            if failure == "transport":
                raise OSError("network down")
            raise subprocess.TimeoutExpired(["ssh", host], timeout)
        stdout = emitted_project_json if args[0] == "project" else emitted_lode_inventory_json
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    probe = probe_candidate("bad.example", "journal", runner, monotonic=lambda: 0.0)

    assert probe.eligible is False
    assert probe.load is None
    expected = {
        "malformed": "malformed",
        "nonzero": "exited",
        "transport": "transport",
        "timeout": "timed out",
    }[failure]
    assert expected in probe.reason
    assert calls[-1][1] == REMOTE_CANDIDATE_PROBE_TIMEOUT_SECONDS


def test_probe_candidate_shares_deadline_between_both_calls(emitted_project_json):
    calls = []
    clock = iter([0.0, 7.5])

    def runner(_host, args, *, timeout):
        calls.append((args, timeout))
        stdout = emitted_project_json if args[0] == "project" else '{"lodes": []}'
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    probe = probe_candidate("slow.example", "journal", runner, monotonic=lambda: next(clock))

    assert probe.eligible is True
    assert calls[0][1] == REMOTE_CANDIDATE_PROBE_TIMEOUT_SECONDS
    assert calls[1][1] == 0.5


def test_probe_candidate_refuses_when_first_call_exhausts_shared_deadline(emitted_project_json):
    calls = []
    clock = iter([0.0, REMOTE_CANDIDATE_PROBE_TIMEOUT_SECONDS])

    def runner(_host, args, *, timeout):
        calls.append((args, timeout))
        return subprocess.CompletedProcess([], 0, stdout=emitted_project_json, stderr="")

    probe = probe_candidate("slow.example", "journal", runner, monotonic=lambda: next(clock))

    assert probe.eligible is False
    assert "deadline expired before lode inventory" in probe.reason
    assert calls == [(["project", "list", "--json"], REMOTE_CANDIDATE_PROBE_TIMEOUT_SECONDS)]


def test_probe_candidates_runs_large_pool_concurrently(
    emitted_project_json,
    emitted_lode_inventory_json,
):
    hosts = [f"host-{index}.example" for index in range(24)]
    barrier = threading.Barrier(len(hosts))

    def runner(_host, args, *, timeout):
        if args[0] == "project":
            assert timeout == REMOTE_CANDIDATE_PROBE_TIMEOUT_SECONDS
            barrier.wait(timeout=2)
            stdout = emitted_project_json
        else:
            assert 0 < timeout <= REMOTE_CANDIDATE_PROBE_TIMEOUT_SECONDS
            stdout = emitted_lode_inventory_json
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    probes = probe_candidates(hosts, "journal", runner)

    assert [probe.host for probe in probes] == hosts
    assert all(probe.eligible for probe in probes)


def test_probe_candidates_pins_aggregate_deadline(monkeypatch):
    observed = []

    class Pending:
        def cancel(self):
            return True

    class Executor:
        def __init__(self, **_kwargs):
            pass

        def submit(self, *_args, **_kwargs):
            return Pending()

        def shutdown(self, **_kwargs):
            pass

    def fake_wait(futures, *, timeout):
        observed.append((futures, timeout))
        return set(), set(futures)

    monkeypatch.setattr("hopper.remote.ThreadPoolExecutor", Executor)
    monkeypatch.setattr("hopper.remote.wait", fake_wait)

    probes = probe_candidates(
        ["one.example", "two.example"],
        "journal",
        lambda *_args, **_kwargs: None,
        monotonic=lambda: 0.0,
    )

    assert observed[0][1] == REMOTE_POOL_PROBE_TIMEOUT_SECONDS
    assert [probe.eligible for probe in probes] == [False, False]


def test_select_candidate_uses_minimum_load_and_injected_tie_break():
    probes = [
        CandidateProbe("busy.example", True, 4, None),
        CandidateProbe("first.example", True, 1, None),
        CandidateProbe("second.example", True, 1, None),
        CandidateProbe("down.example", False, None, "unavailable"),
    ]
    choices = []

    selected = select_candidate(probes, chooser=lambda tied: (choices.extend(tied), tied[-1])[1])

    assert [probe.host for probe in choices] == ["first.example", "second.example"]
    assert selected.host == "second.example"


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("migration", "config"),
        ("migration", "project"),
        ("migration", "remote"),
        ("config", "migration"),
        ("project", "migration"),
        ("remote", "migration"),
        ("config", "config"),
        ("project", "project"),
        ("remote", "remote"),
        ("config", "project"),
        ("project", "remote"),
        ("remote", "config"),
    ],
)
def test_config_transactions_serialize_process_interleavings(tmp_path, first, second):
    """Migration and all writer families read only while holding config.lock."""
    child_code = r"""
import json
import socket
import sys

import hopper.config as config
import hopper.projects as projects
import hopper.remote as remote

host, port, role, operation = sys.argv[1:]
control = socket.create_connection((host, int(port)), timeout=10)
control_file = control.makefile("rwb", buffering=0)
control_file.write(f"READY {role}\n".encode())
assert control_file.readline() == b"GO\n"
if role == "B":
    control_file.write(b"ATTEMPT B\n")

original_publish = config._publish_config

def synchronized_publish(data, path):
    payload = json.dumps(data, sort_keys=True)
    control_file.write(f"PUBLISH_READY {role} {payload}\n".encode())
    assert control_file.readline() == b"RELEASE\n"
    original_publish(data, path)

config._publish_config = synchronized_publish
projects.current_time_ms = lambda: 100 if role == "A" else 200

if operation == "migration":
    remote.remote_registry()
elif operation == "config":
    with config.config_transaction() as stored:
        stored[f"writer-{role.lower()}"] = f"value-{role.lower()}"
elif operation == "project":
    projects.touch_project(f"project-{role.lower()}")
elif operation == "remote":
    remote.set_remote(f"route-{role.lower()}", [f"{role.lower()}.example"])
else:
    raise AssertionError(operation)

control_file.write(f"DONE {role}\n".encode())
"""
    xdg_home = tmp_path / "xdg"
    data_dir = xdg_home / "hopper"
    data_dir.mkdir(parents=True)
    initial = {
        "name": "sol",
        "remote.legacy": "legacy.example",
        "projects": [
            {"path": "/tmp/a", "name": "project-a", "last_used_at": 0},
            {"path": "/tmp/b", "name": "project-b", "last_used_at": 0},
        ],
    }
    config_path = data_dir / "config.json"
    config_path.write_text(json.dumps(initial, indent=2) + "\n")

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
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", child_code, host, str(port), role, operation],
            cwd=repo_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for role, operation in [("A", first), ("B", second)]
    ]
    controls = {}
    files = {}
    try:
        for _ in processes:
            connection, _ = listener.accept()
            connection.settimeout(10)
            control_file = connection.makefile("rwb", buffering=0)
            ready, role = control_file.readline().decode().strip().split()
            assert ready == "READY"
            controls[role] = connection
            files[role] = control_file

        files["A"].write(b"GO\n")
        publish, role, _payload = files["A"].readline().decode().strip().split(" ", 2)
        assert (publish, role) == ("PUBLISH_READY", "A")

        lock_probe = open(data_dir / "config.lock", "a+")
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock_probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            lock_probe.close()

        files["B"].write(b"GO\n")
        assert files["B"].readline() == b"ATTEMPT B\n"
        files["A"].write(b"RELEASE\n")
        assert files["A"].readline() == b"DONE A\n"

        publish, role, payload = files["B"].readline().decode().strip().split(" ", 2)
        assert (publish, role) == ("PUBLISH_READY", "B")
        merged = json.loads(payload)
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

    final = json.loads(config_path.read_text())
    assert final == merged
    assert final["name"] == "sol"
    assert not list(data_dir.glob("config.json.*.tmp"))
    for operation, role in [(first, "a"), (second, "b")]:
        if operation == "migration":
            assert final["remote.legacy"] == ["legacy.example"]
        elif operation == "config":
            assert final[f"writer-{role}"] == f"value-{role}"
        elif operation == "project":
            project = next(item for item in final["projects"] if item["name"] == f"project-{role}")
            assert project["last_used_at"] == (100 if role == "a" else 200)
        elif operation == "remote":
            assert final[f"remote.route-{role}"] == [f"{role}.example"]


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


def test_remember_lode_rejects_corrupt_cache_without_rewriting(temp_config):
    config.hopper_dir().mkdir(parents=True, exist_ok=True)
    cache_path = temp_config / "remote-lodes.json"
    corrupt = '{"existing": '
    cache_path.write_text(corrupt)

    with pytest.raises(ValueError):
        remember_lode("newid", "fedora.local", "journal")

    assert cache_path.read_text() == corrupt


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
