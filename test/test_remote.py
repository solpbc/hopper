# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for remote hopper helpers."""

import fcntl
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hopper import config
from hopper.cli import main
from hopper.projects import Project
from hopper.remote import (
    REMOTE_CACHE_LOCK_POLL_INTERVAL_SEC,
    REMOTE_CACHE_LOCK_TIMEOUT_SEC,
    REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC,
    REMOTE_POOL_PROBE_TIMEOUT_SEC,
    CandidateProbe,
    HostDiscovery,
    LodeCacheError,
    discover_lodes,
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
    assert command[:7] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "--",
        "fedora.local",
    ]
    remote_command = command[7]
    assert remote_command.startswith('export HOP_NO_ROUTE=1; exec "$HOME/.local/bin/hop"')
    assert "$HOME" in remote_command
    assert "'abc 123'" in remote_command
    assert "'quote'\"'\"'arg'" in remote_command
    assert kwargs["input"] == "scope text"
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 12


def test_run_remote_devnulls_stdin_when_none(monkeypatch):
    """A probe-only call must never inherit this process's real stdin.

    Pooled create probes every pool member concurrently before the one
    authoritative create call reads the scope from stdin; if a probe
    inherited the real stdin fd, it could drain the scope before the create
    call ever sees it.
    """
    calls = []

    def fake_run(command, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    run_remote("suze.local", ["ping"])

    assert "input" not in calls[0]
    assert calls[0]["stdin"] is subprocess.DEVNULL


@pytest.mark.parametrize(
    "host",
    ["-oProxyCommand=touch /tmp/pwned", "local", "bad\nhost", "bad\x7fhost", "bad\x85host"],
)
def test_run_remote_rejects_unsafe_host_before_spawning(host, monkeypatch):
    spawn = MagicMock()
    monkeypatch.setattr(subprocess, "run", spawn)

    with pytest.raises(ValueError, match="invalid remote host"):
        run_remote(host, ["ping"])

    spawn.assert_not_called()


def test_run_remote_preserves_arbitrary_binary_stdin(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b"ok\n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_remote("suze.local", ["lode", "repair-output"], stdin_bytes=b"\xff\x00\n")

    assert calls[0]["input"] == b"\xff\x00\n"
    assert calls[0]["text"] is False
    assert result.stdout == "ok\n"
    assert result.stderr == ""


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
            REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC,
        ),
        (
            "ready.example",
            ["lode", "list", "--json"],
            REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC,
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
        assert timeout == REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC
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
        ({"lodes": ["not-an-object"]}, "malformed lode"),
        ({"lodes": [{}]}, "malformed lode"),
        ({"lodes": [{"active": 1}]}, "malformed lode"),
        ({"lodes": [{"active": "yes"}]}, "malformed lode"),
    ],
)
def test_probe_candidate_refuses_invalid_inventory_contract(
    emitted_project_json,
    inventory,
    reason,
):
    def runner(_host, args, *, timeout):
        assert timeout == REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC
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
    assert calls[-1][1] == REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC


def test_probe_candidate_shares_deadline_between_both_calls(emitted_project_json):
    calls = []
    clock = iter([0.0, 7.5])

    def runner(_host, args, *, timeout):
        calls.append((args, timeout))
        stdout = emitted_project_json if args[0] == "project" else '{"lodes": []}'
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    probe = probe_candidate("slow.example", "journal", runner, monotonic=lambda: next(clock))

    assert probe.eligible is True
    assert calls[0][1] == REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC
    assert calls[1][1] == 0.5


def test_probe_candidate_refuses_when_first_call_exhausts_shared_deadline(emitted_project_json):
    calls = []
    clock = iter([0.0, REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC])

    def runner(_host, args, *, timeout):
        calls.append((args, timeout))
        return subprocess.CompletedProcess([], 0, stdout=emitted_project_json, stderr="")

    probe = probe_candidate("slow.example", "journal", runner, monotonic=lambda: next(clock))

    assert probe.eligible is False
    assert "deadline expired before lode inventory" in probe.reason
    assert calls == [(["project", "list", "--json"], REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC)]


def test_probe_candidates_runs_large_pool_concurrently(
    emitted_project_json,
    emitted_lode_inventory_json,
):
    hosts = [f"host-{index}.example" for index in range(24)]
    lock = threading.Lock()
    release = threading.Event()
    active = 0
    peak = 0
    first_wave = 0

    def runner(_host, args, *, timeout):
        nonlocal active, peak, first_wave
        with lock:
            active += 1
            peak = max(peak, active)
            if args[0] == "project" and first_wave < 16:
                first_wave += 1
                if first_wave == 16:
                    release.set()
        try:
            assert release.wait(timeout=2)
            if args[0] == "project":
                assert timeout == REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC
                stdout = emitted_project_json
            else:
                assert 0 < timeout <= REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC
                stdout = emitted_lode_inventory_json
            return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
        finally:
            with lock:
                active -= 1

    probes = probe_candidates(hosts, "journal", runner)

    assert [probe.host for probe in probes] == hosts
    assert all(probe.eligible for probe in probes)
    assert peak == 16


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

    assert observed[0][1] == REMOTE_POOL_PROBE_TIMEOUT_SEC
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


def test_pooled_create_probing_never_consumes_stdin_meant_for_the_create_call(
    temp_config, monkeypatch, capsys
):
    """A pooled create across a multi-member pool must not drop the scope.

    Regression: probing every pool candidate (project list + lode list, one
    ssh call each) ran through run_remote with no stdin payload, and prior
    to the fix that meant subprocess.run inherited this process's real
    stdin for those calls. Concurrent probes across a multi-member pool
    could drain the local scope before the one authoritative create call
    ever read it. This exercises the real
    probe_candidates -> run_remote path end to end (only subprocess.run is
    mocked) with a two-host pool, which a test that stubs out pool selection
    entirely cannot see.
    """
    from io import StringIO

    scope_text = "this is a stdin scope that is long enough to pass the minimum length check"
    config_path = temp_config / "config.json"
    config_path.write_bytes(b'{"remote.journal": ["fedora.local", "suze.local"]}\n')
    monkeypatch.setattr(sys, "argv", ["hop", "implement", "journal"])
    monkeypatch.setattr(sys, "stdin", StringIO(scope_text))

    calls = []
    calls_lock = threading.Lock()

    def hop_args_of(command):
        remote_command = command[7]
        marker = 'hop"'
        tail = remote_command[remote_command.index(marker) + len(marker) :].strip()
        return shlex.split(tail) if tail else []

    def fake_run(command, **kwargs):
        with calls_lock:
            calls.append((command, kwargs))
        host = command[6]
        hop_args = hop_args_of(command)
        if hop_args[:2] == ["project", "list"]:
            payload = {
                "projects": [
                    {
                        "name": "journal",
                        "path": "/srv/journal",
                        "disabled": False,
                        "disabled_reason": "",
                    }
                ]
            }
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        if hop_args[:2] == ["lode", "list"]:
            active_row = {
                "id": "aaaaaaaa",
                "project": "journal",
                "stage": "mill",
                "state": "running",
                "status": "running",
                "active": True,
            }
            lodes = [active_row] if host == "fedora.local" else []
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps({"lodes": lodes}), stderr=""
            )
        if hop_args[:2] == ["implement", "journal"]:
            created = {"id": "abcdefgh", "project": "journal", "host": "local"}
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(created), stderr="")
        raise AssertionError(f"unexpected remote hop invocation: {hop_args}")

    with (
        patch("subprocess.run", side_effect=fake_run),
        patch("hopper.remote.remember_lode") as remember,
    ):
        assert main() == 0

    # fedora.local carries one active lode (load 1), suze.local carries none
    # (load 0), so the least-loaded, uniquely-eligible host is suze.local.
    create_calls = [
        (command, kwargs)
        for command, kwargs in calls
        if hop_args_of(command)[:2] == ["implement", "journal"]
    ]
    assert len(create_calls) == 1
    probe_only_calls = [(c, k) for c, k in calls if (c, k) not in create_calls]
    assert probe_only_calls, "expected at least one probe call across the two-host pool"
    for _command, kwargs in probe_only_calls:
        assert "input" not in kwargs
        assert kwargs.get("stdin") is subprocess.DEVNULL

    create_command, create_kwargs = create_calls[0]
    assert create_command[6] == "suze.local"
    assert create_kwargs["input"] == scope_text

    remember.assert_called_once_with("abcdefgh", "suze.local", "journal")
    assert capsys.readouterr().out == "Created lode abcdefgh (journal) on suze.local\n"


def test_discover_lodes_preserves_rows_and_every_unavailable_reason():
    hosts = [
        "ready.example",
        "timeout.example",
        "failed.example",
        "malformed.example",
        "weak-row.example",
    ]
    calls = []

    def runner(host, args, *, timeout):
        calls.append((host, args, timeout))
        if host == "timeout.example":
            raise subprocess.TimeoutExpired(["ssh", host], timeout)
        if host == "failed.example":
            return subprocess.CompletedProcess([], 7, stdout="", stderr="server failed")
        if host == "malformed.example":
            return subprocess.CompletedProcess([], 0, stdout="{", stderr="")
        if host == "weak-row.example":
            return subprocess.CompletedProcess(
                [], 0, stdout='{"lodes": [{"id": "abc23456"}]}', stderr=""
            )
        return subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {
                    "lodes": [
                        {
                            "id": "abc23456",
                            "project": "project",
                            "stage": "mill",
                            "state": "running",
                            "status": "working",
                            "active": True,
                        }
                    ]
                }
            ),
            stderr="",
        )

    discoveries = discover_lodes(hosts, ["lode", "list", "--json"], runner)

    assert discoveries == [
        HostDiscovery(
            "ready.example",
            (
                {
                    "id": "abc23456",
                    "project": "project",
                    "stage": "mill",
                    "state": "running",
                    "status": "working",
                    "active": True,
                },
            ),
            None,
        ),
        HostDiscovery("timeout.example", (), "lode listing timed out"),
        HostDiscovery("failed.example", (), "lode listing exited 7: server failed"),
        HostDiscovery("malformed.example", (), "lode listing returned malformed JSON"),
        HostDiscovery(
            "weak-row.example",
            (),
            "lode inventory contained a malformed lode record",
        ),
    ]
    assert len(calls) == len(hosts)
    assert all(call[2] == REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC for call in calls)


def test_discover_lodes_runs_large_host_union_concurrently():
    hosts = [f"host-{index}.example" for index in range(24)]
    lock = threading.Lock()
    release = threading.Event()
    active = 0
    peak = 0
    entered = 0

    def runner(_host, _args, *, timeout):
        nonlocal active, peak, entered
        assert timeout == REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC
        with lock:
            active += 1
            entered += 1
            peak = max(peak, active)
            if entered == 16:
                release.set()
        try:
            assert release.wait(timeout=2)
            return subprocess.CompletedProcess([], 0, stdout='{"lodes": []}', stderr="")
        finally:
            with lock:
                active -= 1

    discoveries = discover_lodes(hosts, ["lode", "list", "--json"], runner)

    assert [result.host for result in discoveries] == hosts
    assert all(result.reason is None for result in discoveries)
    assert peak == 16


def test_discover_lodes_pins_the_aggregate_deadline(monkeypatch):
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
        observed.append(timeout)
        return set(), set(futures)

    monkeypatch.setattr("hopper.remote.ThreadPoolExecutor", Executor)
    monkeypatch.setattr("hopper.remote.wait", fake_wait)

    discoveries = discover_lodes(
        ["one.example", "two.example"],
        ["lode", "list", "--json"],
        lambda *_args, **_kwargs: None,
        monotonic=lambda: 0.0,
    )

    assert observed == [REMOTE_POOL_PROBE_TIMEOUT_SEC]
    assert [result.reason for result in discoveries] == [
        "aggregate discovery deadline expired",
        "aggregate discovery deadline expired",
    ]


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
original_acquire = config._acquire_config_lock

def synchronized_acquire(lock_file, path):
    if role == "A":
        control_file.write(b"ACQUIRE_READY A\n")
        assert control_file.readline() == b"ACQUIRE\n"
    original_acquire(lock_file, path)

def synchronized_publish(data, path):
    payload = json.dumps(data, sort_keys=True)
    control_file.write(f"PUBLISH_READY {role} {payload}\n".encode())
    assert control_file.readline() == b"RELEASE\n"
    original_publish(data, path)

config._publish_config = synchronized_publish
config._acquire_config_lock = synchronized_acquire
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
        assert files["A"].readline() == b"ACQUIRE_READY A\n"
        out_of_band = json.loads(config_path.read_text())
        out_of_band["parent-published-before-a-lock"] = True
        config_path.write_text(json.dumps(out_of_band, indent=2) + "\n")
        files["A"].write(b"ACQUIRE\n")
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
    assert final["parent-published-before-a-lock"] is True
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
        '{"oldid234": {"host": "old.local", "project": "old", "created_ms": 1}}\n'
    )

    remember_lode("newid234", "fedora.local", "solstone", created_ms=fresh)

    cache = load_lode_cache()
    assert "oldid234" not in cache
    assert cache["newid234"]["host"] == "fedora.local"
    assert cache["newid234"]["project"] == "solstone"
    assert old < fresh


def test_pruning_uses_last_seen_not_original_creation_time():
    from hopper.remote import prune_lode_cache

    now = 4_000_000_000_000
    cache = {
        "abc23456": {
            "host": "fedora.local",
            "project": "journal",
            "created_ms": 1,
            "last_seen_ms": now - 1,
        }
    }

    assert prune_lode_cache(cache, now_ms=now) == cache


def test_cache_publish_fsyncs_file_and_parent_directory(temp_config, monkeypatch):
    fsync_calls = []
    real_fsync = os.fsync
    monkeypatch.setattr(
        "hopper.remote.os.fsync",
        lambda fd: (fsync_calls.append(fd), real_fsync(fd))[1],
    )

    remember_lode("abc23456", "fedora.local", "journal")

    assert len(fsync_calls) == 2


def test_remember_lode_same_host_refreshes_last_seen(temp_config, monkeypatch):
    existing = {
        "knownid2": {
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
    monkeypatch.setattr("hopper.remote._save_lode_cache", lambda cache: published.append(cache))

    remember_lode("knownid2", "fedora.local", "renamed-project")
    remember_lode("knownid2", "fedora.local", "another-project")

    assert [snapshot["knownid2"]["last_seen_ms"] for snapshot in published] == [200, 300]
    assert published[-1]["knownid2"]["project"] == "another-project"


def test_remember_lode_host_change_preserves_created_ms(temp_config, monkeypatch):
    existing = {
        "knownid2": {
            "host": "old.local",
            "project": "journal",
            "created_ms": 100,
        }
    }
    config.hopper_dir().mkdir(parents=True, exist_ok=True)
    (temp_config / "remote-lodes.json").write_text(json.dumps(existing) + "\n")
    monkeypatch.setattr("hopper.remote.current_time_ms", lambda: 200)

    remember_lode("knownid2", "new.local", "journal")

    entry = load_lode_cache()["knownid2"]
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

    with pytest.raises(LodeCacheError, match="malformed"):
        remember_lode("newid234", "fedora.local", "journal")

    assert cache_path.read_text() == corrupt


@pytest.mark.parametrize(
    ("contents", "reason"),
    [
        ("{", "malformed"),
        ("[]\n", "wrong_shape"),
    ],
)
def test_load_lode_cache_refuses_invalid_file_without_rewriting(
    temp_config,
    contents,
    reason,
):
    cache_path = temp_config / "remote-lodes.json"
    cache_path.write_text(contents)
    original = cache_path.read_bytes()

    with pytest.raises(LodeCacheError) as raised:
        load_lode_cache()

    assert raised.value.path == cache_path
    assert raised.value.reason == reason
    assert cache_path.read_bytes() == original


def test_load_lode_cache_reports_read_error(temp_config, monkeypatch):
    cache_path = temp_config / "remote-lodes.json"
    cache_path.write_text("{}\n")
    original = cache_path.read_bytes()
    original_read_text = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == cache_path:
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)

    with pytest.raises(LodeCacheError) as raised:
        load_lode_cache()

    assert raised.value.path == cache_path
    assert raised.value.reason == "unreadable"
    assert cache_path.read_bytes() == original


@pytest.mark.parametrize(
    "cache",
    [
        {"short": {"host": "a", "project": "p", "created_ms": 1}},
        {"abc23456": {"host": "", "project": "p", "created_ms": 1}},
        {"abc23456": {"host": " a ", "project": "p", "created_ms": 1}},
        {"abc23456": {"host": "a", "project": 1, "created_ms": 1}},
        {"abc23456": {"host": "a", "project": "p"}},
        {"abc23456": {"host": "a", "project": "p", "created_ms": True}},
        {"abc23456": {"host": "a", "project": "p", "created_ms": -1}},
        {
            "abc23456": {
                "host": "a",
                "project": "p",
                "created_ms": 1,
                "created_at": 1,
            }
        },
        {
            "abc23456": {
                "host": "a",
                "project": "p",
                "created_ms": 1,
                "last_seen_ms": False,
            }
        },
    ],
)
def test_load_lode_cache_refuses_invalid_entry_fields(temp_config, cache):
    cache_path = temp_config / "remote-lodes.json"
    cache_path.write_text(json.dumps(cache) + "\n")

    with pytest.raises(LodeCacheError) as raised:
        load_lode_cache()

    assert raised.value.reason == "wrong_shape"


def test_load_lode_cache_migrates_legacy_created_at_under_lock(temp_config):
    cache_path = temp_config / "remote-lodes.json"
    cache_path.write_text(
        json.dumps(
            {
                "abc23456": {
                    "host": "resident.example",
                    "project": "journal",
                    "created_at": 123,
                    "last_seen_ms": 456,
                }
            }
        )
        + "\n"
    )

    cache = load_lode_cache()

    assert cache == {
        "abc23456": {
            "host": "resident.example",
            "project": "journal",
            "created_ms": 123,
            "last_seen_ms": 456,
        }
    }
    assert json.loads(cache_path.read_text()) == cache


def test_legacy_cache_migration_failure_preserves_original_bytes(temp_config, monkeypatch):
    cache_path = temp_config / "remote-lodes.json"
    cache_path.write_text(
        '{"abc23456":{"host":"resident.example","project":"journal","created_at":123}}\n'
    )
    original = cache_path.read_bytes()
    monkeypatch.setattr(os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("no")))

    with pytest.raises(LodeCacheError) as raised:
        load_lode_cache()

    assert raised.value.reason == "unreadable"
    assert cache_path.read_bytes() == original


def test_lode_cache_lock_deadline_uses_named_timeout_and_poll(temp_config, monkeypatch):
    clock = iter([0.0, 0.0, REMOTE_CACHE_LOCK_TIMEOUT_SEC])
    sleeps = []
    monkeypatch.setattr("hopper.remote.time.monotonic", lambda: next(clock))
    monkeypatch.setattr(
        "hopper.remote.fcntl.flock",
        lambda *_args: (_ for _ in ()).throw(BlockingIOError()),
    )
    monkeypatch.setattr("hopper.remote.time.sleep", sleeps.append)

    with pytest.raises(LodeCacheError) as raised:
        remember_lode("abc23456", "resident.example", "journal")

    assert raised.value.reason == "locked"
    assert sleeps == [REMOTE_CACHE_LOCK_POLL_INTERVAL_SEC]


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

original_save = remote._save_lode_cache

def synchronized_save(cache):
    payload = json.dumps(cache, sort_keys=True)
    control_file.write(f"PUBLISH_READY {role} {payload}\n".encode())
    assert control_file.readline() == b"RELEASE\n"
    original_save(cache)

remote._save_lode_cache = synchronized_save
remote.remember_lode(lode_id, remote_host, f"project-{role.lower()}")
control_file.write(f"DONE {role}\n".encode())
"""
    xdg_home = tmp_path / "xdg"
    data_dir = xdg_home / "hopper"
    data_dir.mkdir(parents=True)
    existing = {
        "aaaaaaaa": {
            "host": "one.local",
            "project": "one",
            "created_ms": 4_000_000_000_000,
        },
        "bbbbbbbb": {
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
        ("A", "cccccccc", "alpha.local"),
        ("B", "dddddddd", "beta.local"),
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
            assert json.loads(payload) == {
                lode_id: {**entry, "last_seen_ms": entry["created_ms"]}
                for lode_id, entry in existing.items()
            }
            controls[role] = connection
            files[role] = control_file

        files["A"].write(b"GO\n")
        publish, role, payload = files["A"].readline().decode().strip().split(" ", 2)
        assert (publish, role) == ("PUBLISH_READY", "A")
        assert set(json.loads(payload)) == {*existing, "cccccccc"}

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
        assert set(json.loads(payload)) == {*existing, "cccccccc", "dddddddd"}
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
    assert cache["aaaaaaaa"] == {
        **existing["aaaaaaaa"],
        "last_seen_ms": existing["aaaaaaaa"]["created_ms"],
    }
    assert cache["bbbbbbbb"] == {
        **existing["bbbbbbbb"],
        "last_seen_ms": existing["bbbbbbbb"]["created_ms"],
    }
    assert cache["cccccccc"]["host"] == "alpha.local"
    assert cache["dddddddd"]["host"] == "beta.local"
    assert not list(data_dir.glob("remote-lodes*.tmp"))
