# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for strict, transactional Hopper configuration."""

import fcntl
import json
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from hopper import config
from hopper.projects import Project, save_projects
from hopper.remote import set_remote


def test_missing_config_loads_as_empty():
    assert config.load_config() == {}


@pytest.mark.parametrize(
    ("contents", "reason"),
    [
        ('{"name":', "malformed"),
        ("[]\n", "wrong_shape"),
    ],
)
def test_load_config_distinguishes_invalid_content(temp_config, contents, reason):
    path = temp_config / "config.json"
    path.write_text(contents)

    with pytest.raises(config.ConfigError) as raised:
        config.load_config()

    assert raised.value.path == path
    assert raised.value.reason == reason


def test_load_config_distinguishes_read_error(temp_config):
    path = temp_config / "config.json"
    path.write_text('{"name": "sol"}\n')

    with (
        patch.object(Path, "read_text", side_effect=PermissionError("denied")),
        pytest.raises(config.ConfigError) as raised,
    ):
        config.load_config()

    assert raised.value.path == path
    assert raised.value.reason == "unreadable"


def _generic_writer() -> None:
    with config.config_transaction() as stored:
        stored["writer"] = "generic"


def _project_writer() -> None:
    save_projects([Project(path="/tmp/project", name="project")])


def _remote_writer() -> None:
    set_remote("project", ["host.example"])


@pytest.mark.parametrize(
    "writer",
    [_generic_writer, _project_writer, _remote_writer],
    ids=["generic", "project", "remote"],
)
@pytest.mark.parametrize(
    ("contents", "reason", "read_error"),
    [
        (b'{"name":', "malformed", False),
        (b"[]\n", "wrong_shape", False),
        (b'{"name": "sol"}\n', "unreadable", True),
    ],
)
def test_every_writer_family_preserves_original_bytes_on_strict_read_refusal(
    temp_config, writer, contents, reason, read_error
):
    path = temp_config / "config.json"
    path.write_bytes(contents)
    before = path.read_bytes()
    read_patch = (
        patch.object(Path, "read_text", side_effect=PermissionError("denied"))
        if read_error
        else nullcontext()
    )

    with read_patch, pytest.raises(config.ConfigError) as raised:
        writer()

    assert raised.value.reason == reason
    assert path.read_bytes() == before


def test_exception_inside_transaction_publishes_nothing(temp_config):
    path = temp_config / "config.json"
    path.write_bytes(b'{"name": "before"}\n')
    before = path.read_bytes()

    with pytest.raises(RuntimeError, match="stop"):
        with config.config_transaction() as stored:
            stored["name"] = "after"
            raise RuntimeError("stop")

    assert path.read_bytes() == before


def test_no_op_transaction_preserves_original_bytes(temp_config):
    path = temp_config / "config.json"
    original = b'{\n  "z-last": true,\n  "a-first": { "nested": 1 }\n}\n'
    path.write_bytes(original)

    with config.config_transaction() as stored:
        assert stored == {"z-last": True, "a-first": {"nested": 1}}

    assert path.read_bytes() == original


def test_changed_transaction_still_publishes(temp_config):
    path = temp_config / "config.json"
    original = b'{ "z-last": true, "a-first": 1 }\n'
    path.write_bytes(original)

    with config.config_transaction() as stored:
        stored["new"] = "value"

    assert path.read_bytes() != original
    assert config.load_config() == {"z-last": True, "a-first": 1, "new": "value"}


@pytest.mark.parametrize("failure", ["write", "fsync", "replace"])
def test_publication_failure_preserves_original_bytes(temp_config, monkeypatch, failure):
    path = temp_config / "config.json"
    path.write_bytes(b'{"name": "before"}\n')
    before = path.read_bytes()

    def fail(*_args, **_kwargs):
        raise OSError(failure)

    if failure == "write":
        monkeypatch.setattr(config.json, "dumps", fail)
    elif failure == "fsync":
        monkeypatch.setattr(config.os, "fsync", fail)
    else:
        monkeypatch.setattr(config.os, "replace", fail)

    with pytest.raises(config.ConfigError) as raised:
        with config.config_transaction() as stored:
            stored["name"] = "after"

    assert raised.value.reason == "unreadable"
    assert path.read_bytes() == before
    assert not list(temp_config.glob("config.json.*.tmp"))


def test_transaction_fsyncs_and_publishes_unique_temp(temp_config, monkeypatch):
    fsync_calls = []
    real_fsync = config.os.fsync
    monkeypatch.setattr(
        config.os,
        "fsync",
        lambda fd: (fsync_calls.append(fd), real_fsync(fd))[1],
    )

    with config.config_transaction() as stored:
        stored["name"] = "sol"

    assert config.load_config() == {"name": "sol"}
    assert len(fsync_calls) == 1
    assert not list(temp_config.glob("config.json.*.tmp"))


def test_lock_deadline_uses_named_timeout_and_poll_without_sleeping(temp_config, monkeypatch):
    temp_config.mkdir(parents=True, exist_ok=True)
    held = open(config.config_lock_path(), "a+")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    clock = iter([0.0, 0.0, config.CONFIG_LOCK_TIMEOUT_SEC])
    sleeps = []
    monkeypatch.setattr(config.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(config.time, "sleep", sleeps.append)

    try:
        with pytest.raises(config.ConfigError) as raised:
            with config.config_transaction():
                pytest.fail("transaction acquired a held lock")
    finally:
        held.close()

    assert raised.value.reason == "locked"
    assert sleeps == [config.CONFIG_LOCK_POLL_INTERVAL_SEC]


def test_transaction_preserves_unrelated_values(temp_config):
    path = temp_config / "config.json"
    path.write_text(json.dumps({"name": "sol", "unrelated": {"nested": True}}) + "\n")

    with config.config_transaction() as stored:
        stored["new"] = "value"

    assert config.load_config() == {
        "name": "sol",
        "unrelated": {"nested": True},
        "new": "value",
    }
