# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the hopper CLI."""

import base64
import copy
import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

import hopper.cli as hopper_cli
import hopper.code as hopper_code
from hopper import __version__, actions, config
from hopper.cli import (
    HELP_SKILL_REMINDER,
    _CheckProgress,
    _socket,
    cmd_backlog,
    cmd_check,
    cmd_code,
    cmd_config,
    cmd_feedback,
    cmd_gate,
    cmd_implement,
    cmd_list,
    cmd_lode,
    cmd_ping,
    cmd_process,
    cmd_processed,
    cmd_project,
    cmd_projects,
    cmd_remote,
    cmd_restart,
    cmd_screenshot,
    cmd_show,
    cmd_status,
    cmd_submit,
    cmd_up,
    cmd_wait,
    cmd_watch,
    detect_coding_agent,
    format_lode_detail,
    format_lode_line,
    get_hopper_lid,
    main,
    require_config_name,
    require_no_server,
    require_not_coding_agent,
    require_server,
    validate_hopper_lid,
)
from hopper.client import RUN_GENERATION_ENV
from hopper.lodes import (
    PARK_PANE_GONE_STATUS,
    current_time_ms,
    format_age,
    format_park_status,
    format_terminal_failure_status,
    get_lode_dir,
    get_worktree_dir,
    save_lodes,
)
from hopper.projects import Project, load_projects, save_projects
from hopper.remote import (
    REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC,
    REMOTE_CREATE_TIMEOUT_SEC,
    REMOTE_SET_PING_TIMEOUT_SEC,
    CandidateProbe,
)
from hopper.server import Server
from hopper.tmux import Liveness

LONG_SCOPE = "this is a stdin scope that is long enough to pass the minimum character validation"


@pytest.fixture(autouse=True)
def clear_hopper_lid_env(monkeypatch):
    """Default tests to not running inside a lode unless explicitly set."""
    monkeypatch.delenv("HOPPER_LID", raising=False)


def test_main_is_callable():
    assert callable(main)


def test_server_socket_path_is_shared_with_cli(temp_config):
    """The CLI and config agree on the late-bound production socket path."""
    expected = temp_config / "server.sock"
    assert config.server_socket_path() == expected
    assert _socket() == expected


# Tests for help and version


def test_no_args_shows_help(capsys):
    """No arguments shows help and returns 0."""
    with patch.object(sys, "argv", ["hopper"]):
        result = main()
    assert result == 0
    captured = capsys.readouterr()
    assert "Usage:" in captured.out
    assert "Commands:" in captured.out
    assert captured.out.count(HELP_SKILL_REMINDER) == 1


def test_help_flag(capsys):
    """-h flag shows help and returns 0."""
    with patch.object(sys, "argv", ["hopper", "-h"]):
        result = main()
    assert result == 0
    captured = capsys.readouterr()
    assert "Usage:" in captured.out


def test_help_long_flag(capsys):
    """--help flag shows help and returns 0."""
    with patch.object(sys, "argv", ["hopper", "--help"]):
        result = main()
    assert result == 0
    captured = capsys.readouterr()
    assert "Usage:" in captured.out


def test_help_command(capsys):
    """help command shows help and returns 0."""
    with patch.object(sys, "argv", ["hopper", "help"]):
        result = main()
    assert result == 0
    captured = capsys.readouterr()
    assert "Usage:" in captured.out


def test_version_flag(capsys):
    """--version flag shows version and returns 0."""
    with patch.object(sys, "argv", ["hopper", "--version"]):
        result = main()
    assert result == 0
    captured = capsys.readouterr()
    assert __version__ in captured.out


def test_unknown_command(capsys):
    """Unknown command returns 1 and shows help."""
    with patch.object(sys, "argv", ["hopper", "unknown"]):
        result = main()
    assert result == 1
    captured = capsys.readouterr()
    assert "unknown command: unknown" in captured.out
    assert "Usage:" in captured.out
    assert captured.out.count(HELP_SKILL_REMINDER) == 1


# Tests for subcommand help


def test_ping_help(capsys):
    """ping --help shows help and returns 0."""
    result = cmd_ping(["--help"])
    assert result == 0
    captured = capsys.readouterr()
    assert "usage: hop ping" in captured.out
    assert "Check if the hopper server is running" in captured.out
    assert captured.out.count(HELP_SKILL_REMINDER) == 1


def test_up_help(capsys):
    """up --help shows help and returns 0."""
    result = cmd_up(["--help"])
    assert result == 0
    captured = capsys.readouterr()
    assert "usage: hop up" in captured.out
    assert "Start the hopper server and TUI" in captured.out


def test_process_help(capsys):
    """process --help shows help and returns 0."""
    result = cmd_process(["--help"])
    assert result == 0
    captured = capsys.readouterr()
    assert "usage: hop process" in captured.out
    assert "lode_id" in captured.out


def test_status_help(capsys):
    """status --help shows help and returns 0."""
    result = cmd_status(["--help"])
    assert result == 0
    captured = capsys.readouterr()
    assert "usage: hop status" in captured.out
    assert "status" in captured.out


# Tests for subcommand unknown args


def test_ping_unknown_arg(capsys):
    """ping rejects unknown arguments."""
    result = cmd_ping(["--unknown"])
    assert result == 1
    captured = capsys.readouterr()
    assert "error: unrecognized arguments: --unknown" in captured.out
    assert "usage: hop ping" in captured.out
    assert captured.out.count(HELP_SKILL_REMINDER) == 1


def test_up_unknown_arg(capsys):
    """up rejects unknown arguments."""
    result = cmd_up(["--unknown"])
    assert result == 1
    captured = capsys.readouterr()
    assert "error: unrecognized arguments: --unknown" in captured.out
    assert "usage: hop up" in captured.out


def test_process_unknown_arg(capsys):
    """process rejects unknown arguments."""
    result = cmd_process(["session-123", "--unknown"])
    assert result == 1
    captured = capsys.readouterr()
    assert "error: unrecognized arguments: --unknown" in captured.out
    assert "usage: hop process" in captured.out


def test_status_unknown_arg(capsys):
    """status rejects unknown arguments."""
    result = cmd_status(["--unknown"])
    assert result == 1
    captured = capsys.readouterr()
    assert "error: unrecognized arguments: --unknown" in captured.out
    assert "usage: hop status" in captured.out


def test_process_missing_lode_id(capsys):
    """process requires lode_id argument."""
    result = cmd_process([])
    assert result == 1
    captured = capsys.readouterr()
    assert "error:" in captured.out
    assert "lode_id" in captured.out


def test_process_delegates_to_runner(capsys):
    """process delegates to run_process after server check."""
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.process.run_process_supervisor", return_value=0) as mock_run:
            result = cmd_process(["test-1234-session"])
    assert result == 0
    mock_run.assert_called_once()


# Tests for ping command


def test_ping_command_no_server(capsys):
    """Ping command returns 1 when server not running."""
    with patch.object(sys, "argv", ["hopper", "ping"]):
        with patch("hopper.client.connect", return_value=None):
            result = main()
    assert result == 1
    captured = capsys.readouterr()
    assert "Server not running" in captured.out


def test_ping_command_validates_hopper_lid(capsys):
    """Ping command validates HOPPER_LID if set."""
    # connect returns session_found=False for invalid session
    mock_response = {"type": "connected", "tmux": None, "lode": None, "lode_found": False}
    with patch.object(sys, "argv", ["hopper", "ping"]):
        with patch("hopper.client.connect", return_value=mock_response):
            with patch.dict(os.environ, {"HOPPER_LID": "bad-session"}):
                result = main()
    assert result == 1
    captured = capsys.readouterr()
    assert "bad-session" in captured.out
    assert "not found or archived" in captured.out


def test_ping_command_success(capsys):
    """Ping command returns 0 when server running and no HOPPER_LID."""
    mock_response = {"type": "connected", "tmux": None}
    with patch.object(sys, "argv", ["hopper", "ping"]):
        with patch("hopper.client.connect", return_value=mock_response):
            env = os.environ.copy()
            env.pop("HOPPER_LID", None)
            with patch.dict(os.environ, env, clear=True):
                result = main()
    assert result == 0
    captured = capsys.readouterr()
    assert "pong" in captured.out


# Tests for up command


def test_up_command_requires_tmux(capsys):
    """Up command returns 1 when not inside tmux."""
    with patch.object(sys, "argv", ["hopper", "up"]):
        with patch("hopper.cli.require_not_coding_agent", return_value=None):
            with patch("hopper.cli.require_no_server", return_value=None):
                with patch("hopper.cli.require_config_name", return_value=None):
                    with patch("hopper.cli.require_projects", return_value=None):
                        with patch("hopper.tmux.is_inside_tmux", return_value=False):
                            with patch("hopper.tmux.get_tmux_sessions", return_value=[]):
                                result = main()
    assert result == 1
    captured = capsys.readouterr()
    assert "hop up must run inside tmux" in captured.out
    assert "tmux new 'hop up'" in captured.out


def test_up_command_shows_existing_lodes(capsys):
    """Up command shows existing sessions when tmux is running."""
    with patch.object(sys, "argv", ["hopper", "up"]):
        with patch("hopper.cli.require_not_coding_agent", return_value=None):
            with patch("hopper.cli.require_no_server", return_value=None):
                with patch("hopper.cli.require_config_name", return_value=None):
                    with patch("hopper.cli.require_projects", return_value=None):
                        with patch("hopper.tmux.is_inside_tmux", return_value=False):
                            with patch(
                                "hopper.tmux.get_tmux_sessions", return_value=["main", "dev"]
                            ):
                                result = main()
    assert result == 1
    captured = capsys.readouterr()
    assert "tmux attach -t main" in captured.out
    assert "tmux attach -t dev" in captured.out


def test_up_command_fails_if_server_running(capsys):
    """Up command returns 1 if server already running."""
    with patch.object(sys, "argv", ["hopper", "up"]):
        with patch("hopper.cli.require_not_coding_agent", return_value=None):
            with patch("hopper.client.probe_server", return_value="up"):
                result = main()
    assert result == 1
    captured = capsys.readouterr()
    assert "a hopper server is already running on" in captured.out
    assert "attach to the existing hopper session" in captured.out


def test_up_command_requires_name_config(capsys):
    """Up command returns 1 if name not configured."""
    with patch.object(sys, "argv", ["hopper", "up"]):
        with patch("hopper.cli.require_not_coding_agent", return_value=None):
            with patch("hopper.cli.require_no_server", return_value=None):
                result = main()
    assert result == 1
    captured = capsys.readouterr()
    assert "Please set your name first" in captured.out
    assert "hop config set name" in captured.out


# Tests for require_config_name


def test_require_config_name_success(temp_config):
    """require_config_name returns None when name is set."""
    config_file = temp_config / "config.json"
    config_file.write_text('{"name": "jer"}')

    result = require_config_name()
    assert result is None


def test_require_config_name_failure(capsys):
    """require_config_name returns 1 when name not set."""
    result = require_config_name()
    assert result == 1
    captured = capsys.readouterr()
    assert "Please set your name first" in captured.out
    assert "hop config set name" in captured.out


# Tests for require_server


def test_require_server_success():
    """require_server returns None when server is running."""
    with patch("hopper.client.probe_server", return_value="up"):
        result = require_server()
    assert result is None


def test_require_server_failure(capsys):
    """require_server returns 1 when server not running."""
    with patch("hopper.client.probe_server", return_value="down"):
        result = require_server()
    assert result == 1
    captured = capsys.readouterr()
    assert "Server not running" in captured.out
    assert "hop up" in captured.out


# Tests for require_no_server


def test_require_no_server_success():
    """require_no_server returns None when server is not running."""
    with patch("hopper.client.probe_server", return_value="down"):
        result = require_no_server()
    assert result is None


def test_require_no_server_failure(capsys):
    """require_no_server returns 1 when server is running."""
    with patch("hopper.client.probe_server", return_value="up"):
        result = require_no_server()
    assert result == 1
    captured = capsys.readouterr()
    assert "a hopper server is already running on" in captured.out
    assert "stop that server before running hop up" in captured.out


@pytest.mark.parametrize("require", [require_server, require_no_server])
def test_require_helpers_refuse_unresponsive_server(require, capsys):
    with patch("hopper.client.probe_server", return_value="unresponsive"):
        assert require(timeout=0.25) == 1

    output = capsys.readouterr().out
    assert "a hopper server is listening on" in output
    assert "did not answer within 0.25s" in output
    assert "retry, or stop it if wedged" in output


def test_require_no_server_refuses_real_unresponsive_listener(tmp_path, monkeypatch, capsys):
    """Regression: this fails on unpatched main's existence-only/down probe."""
    socket_path = tmp_path / "unresponsive.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    monkeypatch.setattr("hopper.cli._socket", lambda: socket_path)

    try:
        assert require_no_server(timeout=0.05) == 1
    finally:
        listener.close()

    assert "it may be busy" in capsys.readouterr().out


# Tests for detect_coding_agent


def test_detect_coding_agent_clean_env():
    """detect_coding_agent returns None with no agent env vars."""
    env = os.environ.copy()
    for var in ("CLAUDECODE", "GEMINI_CLI", "CODEX_CI"):
        env.pop(var, None)
    with patch.dict(os.environ, env, clear=True):
        result = detect_coding_agent()
    assert result is None


def test_detect_coding_agent_claude_code():
    """detect_coding_agent returns 'Claude Code' when CLAUDECODE=1."""
    with patch.dict(os.environ, {"CLAUDECODE": "1"}, clear=True):
        result = detect_coding_agent()
    assert result == "Claude Code"


def test_detect_coding_agent_gemini_cli():
    """detect_coding_agent returns 'Gemini CLI' when GEMINI_CLI=1."""
    with patch.dict(os.environ, {"GEMINI_CLI": "1"}, clear=True):
        result = detect_coding_agent()
    assert result == "Gemini CLI"


def test_detect_coding_agent_codex():
    """detect_coding_agent returns 'Codex' when CODEX_CI=1."""
    with patch.dict(os.environ, {"CODEX_CI": "1"}, clear=True):
        result = detect_coding_agent()
    assert result == "Codex"


def test_detect_coding_agent_ignores_non_one():
    """detect_coding_agent returns None when env var is not '1'."""
    with patch.dict(os.environ, {"CLAUDECODE": "0"}, clear=True):
        result = detect_coding_agent()
    assert result is None


def test_detect_coding_agent_ignores_empty():
    """detect_coding_agent returns None when env var is empty string."""
    with patch.dict(os.environ, {"CLAUDECODE": ""}, clear=True):
        result = detect_coding_agent()
    assert result is None


# Tests for require_not_coding_agent


def test_require_not_coding_agent_success():
    """require_not_coding_agent returns None when no agent detected."""
    with patch("hopper.cli.detect_coding_agent", return_value=None):
        result = require_not_coding_agent()
    assert result is None


def test_require_not_coding_agent_failure(capsys):
    """require_not_coding_agent returns 1 with message when agent detected."""
    with patch.dict(os.environ, {"CLAUDECODE": "1"}, clear=True):
        result = require_not_coding_agent()
    assert result == 1
    captured = capsys.readouterr()
    assert "Claude Code" in captured.out
    assert "CLAUDECODE=1" in captured.out
    assert "TUI" in captured.out


def test_require_not_inside_lode_blocks(monkeypatch):
    """require_not_inside_lode() returns 1 when HOPPER_LID is set."""
    monkeypatch.setenv("HOPPER_LID", "test-lode-123")
    from hopper.cli import require_not_inside_lode

    assert require_not_inside_lode() == 1


def test_require_not_inside_lode_allows(monkeypatch):
    """require_not_inside_lode() returns None when HOPPER_LID is not set."""
    monkeypatch.delenv("HOPPER_LID", raising=False)
    from hopper.cli import require_not_inside_lode

    assert require_not_inside_lode() is None


# Tests for cmd_up agent guard


def test_up_command_rejects_coding_agent():
    """Up command returns 1 when inside a coding agent."""
    with patch.object(sys, "argv", ["hopper", "up"]):
        with patch("hopper.cli.require_not_coding_agent", return_value=1):
            result = main()
    assert result == 1


# Tests for get_hopper_lid


def test_get_hopper_lid_set():
    """get_hopper_lid returns value when set."""
    with patch.dict(os.environ, {"HOPPER_LID": "test-session-123"}):
        result = get_hopper_lid()
    assert result == "test-session-123"


def test_get_hopper_lid_not_set():
    """get_hopper_lid returns None when not set."""
    env = os.environ.copy()
    env.pop("HOPPER_LID", None)
    with patch.dict(os.environ, env, clear=True):
        result = get_hopper_lid()
    assert result is None


# Tests for validate_hopper_lid


def test_validate_hopper_lid_not_set():
    """validate_hopper_lid returns None when HOPPER_LID not set."""
    env = os.environ.copy()
    env.pop("HOPPER_LID", None)
    with patch.dict(os.environ, env, clear=True):
        result = validate_hopper_lid()
    assert result is None


def test_validate_hopper_lid_valid():
    """validate_hopper_lid returns None when session exists."""
    with patch.dict(os.environ, {"HOPPER_LID": "valid-session"}):
        with patch("hopper.client.lode_exists", return_value=True):
            result = validate_hopper_lid()
    assert result is None


def test_validate_hopper_lid_invalid(capsys):
    """validate_hopper_lid returns 1 when session doesn't exist."""
    with patch.dict(os.environ, {"HOPPER_LID": "invalid-session"}):
        with patch("hopper.client.lode_exists", return_value=False):
            result = validate_hopper_lid()
    assert result == 1
    captured = capsys.readouterr()
    assert "invalid-session" in captured.out
    assert "not found or archived" in captured.out
    assert "unset HOPPER_LID" in captured.out


# Tests for status command


def test_status_without_lode_prefers_missing_target_to_server_check(capsys):
    """Bare outside status rejects the missing target without probing the server."""
    with patch("hopper.client.probe_server", return_value="down"):
        result = cmd_status([])
    assert result == 1
    captured = capsys.readouterr()
    assert captured.out == "HOPPER_LID not set. Run this from within a hopper lode.\n"


def test_status_no_hopper_lid(capsys):
    """status command returns 1 when HOPPER_LID not set."""
    env = os.environ.copy()
    env.pop("HOPPER_LID", None)
    with patch.dict(os.environ, env, clear=True):
        with patch("hopper.client.probe_server", return_value="up"):
            result = cmd_status([])
    assert result == 1
    captured = capsys.readouterr()
    assert "HOPPER_LID not set" in captured.out


def test_status_invalid_session(capsys):
    """status command returns 1 when session doesn't exist."""
    with patch.dict(os.environ, {"HOPPER_LID": "bad-session"}):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.client.lode_exists", return_value=False):
                result = cmd_status([])
    assert result == 1
    captured = capsys.readouterr()
    assert "bad-session" in captured.out
    assert "not found or archived" in captured.out


def test_status_show(capsys):
    """status command shows current status when no args."""
    session_data = {"id": "test-session", "status": "Working on feature X"}
    with patch.dict(os.environ, {"HOPPER_LID": "test-session"}):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.get_lode", return_value=session_data):
                    result = cmd_status([])
    assert result == 0
    captured = capsys.readouterr()
    assert "Working on feature X" in captured.out


def test_status_show_title(capsys):
    """status command shows title when present."""
    session_data = {"id": "test-session", "title": "Auth Flow", "status": "Working on feature X"}
    with patch.dict(os.environ, {"HOPPER_LID": "test-session"}):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.get_lode", return_value=session_data):
                    result = cmd_status([])
    assert result == 0
    captured = capsys.readouterr()
    assert "Title: Auth Flow" in captured.out
    assert "Working on feature X" in captured.out


def test_status_show_empty(capsys):
    """status command shows placeholder when no status set."""
    session_data = {"id": "test-session", "status": ""}
    with patch.dict(os.environ, {"HOPPER_LID": "test-session"}):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.get_lode", return_value=session_data):
                    result = cmd_status([])
    assert result == 0
    captured = capsys.readouterr()
    assert "(no status)" in captured.out


def test_status_update(capsys):
    """status command updates status when args provided."""
    session_data = {"id": "test-session", "status": "Old status"}
    with patch.dict(os.environ, {"HOPPER_LID": "test-session"}):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.get_lode", return_value=session_data):
                    with patch("hopper.client.set_lode_status", return_value=True):
                        result = cmd_status(["New", "status", "text"])
    assert result == 0
    captured = capsys.readouterr()
    assert "Updated from 'Old status' to 'New status text'" in captured.out


def test_status_set_title(capsys):
    """status -t sets title only."""
    with patch.dict(os.environ, {"HOPPER_LID": "test-session"}):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.set_lode_title", return_value=True) as mock_set_title:
                    result = cmd_status(["-t", "Auth Flow"])
    assert result == 0
    mock_set_title.assert_called_once()
    assert mock_set_title.call_args.args[1:] == ("test-session", "Auth Flow")
    captured = capsys.readouterr()
    assert "Title set to 'Auth Flow'" in captured.out


def test_status_set_title_and_text(capsys):
    """status -t with text sets both title and status."""
    session_data = {"id": "test-session", "status": "Old status"}
    with patch.dict(os.environ, {"HOPPER_LID": "test-session"}):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.get_lode", return_value=session_data):
                    with patch("hopper.client.set_lode_title", return_value=True) as mock_set_title:
                        with patch(
                            "hopper.client.set_lode_status", return_value=True
                        ) as mock_set_status:
                            result = cmd_status(["-t", "New", "updated", "text"])
    assert result == 0
    mock_set_title.assert_called_once()
    assert mock_set_title.call_args.args[1:] == ("test-session", "New")
    mock_set_status.assert_called_once()
    assert mock_set_status.call_args.args[1:] == ("test-session", "updated text")
    captured = capsys.readouterr()
    assert "Title set to 'New'" in captured.out
    assert "Updated from 'Old status' to 'updated text'" in captured.out


def test_status_update_from_empty(capsys):
    """status command shows simpler message when updating from empty."""
    session_data = {"id": "test-session", "status": ""}
    with patch.dict(os.environ, {"HOPPER_LID": "test-session"}):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.get_lode", return_value=session_data):
                    with patch("hopper.client.set_lode_status", return_value=True):
                        result = cmd_status(["New status"])
    assert result == 0
    captured = capsys.readouterr()
    assert "Updated to 'New status'" in captured.out
    assert "from" not in captured.out


def test_status_empty_text_error(capsys):
    """status command returns 1 when given empty text."""
    with patch.dict(os.environ, {"HOPPER_LID": "test-session"}):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.client.lode_exists", return_value=True):
                result = cmd_status(["", "  "])
    assert result == 1
    captured = capsys.readouterr()
    assert "Status text required" in captured.out


# --- cmd_backlog tests ---


def test_backlog_add_reads_description_from_stdin(capsys):
    """backlog add accepts description from stdin when text args are omitted."""
    from io import StringIO

    with patch("hopper.client.probe_server", return_value="down"):
        with patch("hopper.backlog.load_backlog", return_value=[]):
            with patch("hopper.backlog.add_backlog_item", return_value=MagicMock()) as mock_add:
                with patch("sys.stdin", StringIO("Backlog from stdin")):
                    assert cmd_backlog(["add", "-p", "myproj"]) == 0

    mock_add.assert_called_once()
    _, project, description = mock_add.call_args.args[:3]
    assert project == "myproj"
    assert description == "Backlog from stdin"
    out = capsys.readouterr().out
    assert "Added: [myproj] Backlog from stdin" in out


def test_backlog_add_requires_description_or_stdin(capsys):
    """backlog add returns 1 when both args and stdin description are empty."""
    from io import StringIO

    with patch("sys.stdin", StringIO(" \n")):
        assert cmd_backlog(["add", "-p", "myproj"]) == 1

    out = capsys.readouterr().out
    assert "Error: no description provided" in out
    assert "Use: hop backlog add [-p project] <text...>" in out


def test_backlog_add_refuses_unresponsive_server(capsys):
    with (
        patch("hopper.client.probe_server", return_value="unresponsive"),
        patch("hopper.backlog.add_backlog_item") as mock_local_add,
        patch("hopper.client.add_backlog") as mock_remote_add,
    ):
        # NOTE: text must precede --project. `backlog add -p proj "text"` is a real
        # argparse allocation bug (an optional between two positionals swallows the
        # trailing `text`); tracked separately. Use the working order so this test
        # actually exercises the unresponsive-server refusal instead of an arg error.
        assert cmd_backlog(["add", "Do work", "--project", "myproj"]) == 1

    mock_local_add.assert_not_called()
    mock_remote_add.assert_not_called()
    assert "it may be busy" in capsys.readouterr().out


def test_backlog_remove_refuses_unresponsive_server(capsys):
    item = _mock_backlog_item()
    with (
        patch("hopper.client.probe_server", return_value="unresponsive"),
        patch("hopper.backlog.load_backlog", return_value=[item]),
        patch("hopper.backlog.find_by_prefix", return_value=item),
        patch("hopper.backlog.remove_backlog_item") as mock_local_remove,
        patch("hopper.client.remove_backlog") as mock_remote_remove,
    ):
        assert cmd_backlog(["remove", "abc"]) == 1

    mock_local_remove.assert_not_called()
    mock_remote_remove.assert_not_called()
    assert "it may be busy" in capsys.readouterr().out


def _mock_backlog_item(id="abc123", project="myproj", description="Fix bug"):
    item = MagicMock()
    item.id = id
    item.project = project
    item.description = description
    return item


def test_backlog_promote_success(capsys):
    item = _mock_backlog_item()
    socket_path = MagicMock()

    with patch("hopper.cli._socket", return_value=socket_path):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.backlog.load_backlog", return_value=[item]):
                with patch("hopper.backlog.find_by_prefix", return_value=item):
                    with patch(
                        "hopper.client.promote_backlog",
                        return_value={"id": "newlode1"},
                    ) as mock_promote:
                        assert cmd_backlog(["promote", "abc"]) == 0

    mock_promote.assert_called_once_with(socket_path, "abc123", scope="")
    out = capsys.readouterr().out
    assert "Promoted: newlode1" in out


def test_backlog_promote_with_scope(capsys):
    item = _mock_backlog_item()
    socket_path = MagicMock()

    with patch("hopper.cli._socket", return_value=socket_path):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.backlog.load_backlog", return_value=[item]):
                with patch("hopper.backlog.find_by_prefix", return_value=item):
                    with patch(
                        "hopper.client.promote_backlog",
                        return_value={"id": "newlode1"},
                    ) as mock_promote:
                        assert cmd_backlog(["promote", "abc", "custom", "scope"]) == 0

    mock_promote.assert_called_once_with(socket_path, "abc123", scope="custom scope")
    out = capsys.readouterr().out
    assert "custom scope" in out


def test_backlog_promote_not_found(capsys):
    with patch("hopper.client.probe_server", return_value="up"):
        with patch("hopper.backlog.load_backlog", return_value=[]):
            with patch("hopper.backlog.find_by_prefix", return_value=None):
                assert cmd_backlog(["promote", "abc"]) == 1

    out = capsys.readouterr().out
    assert "No unique backlog item matching" in out


def test_backlog_promote_requires_server(capsys):
    with patch("hopper.client.probe_server", return_value="down"):
        assert cmd_backlog(["promote", "abc"]) == 1

    out = capsys.readouterr().out
    assert "Server not running" in out


def test_backlog_queue_success(capsys):
    item = _mock_backlog_item()
    socket_path = MagicMock()

    with patch("hopper.cli._socket", return_value=socket_path):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.backlog.load_backlog", return_value=[item]):
                with patch("hopper.backlog.find_by_prefix", return_value=item):
                    with patch("hopper.client.set_backlog_queued", return_value=True):
                        assert cmd_backlog(["queue", "abc", "lode42"]) == 0

    out = capsys.readouterr().out
    assert "Queued:" in out
    assert "→ lode42" in out


def test_backlog_queue_clear(capsys):
    item = _mock_backlog_item()
    socket_path = MagicMock()

    with patch("hopper.cli._socket", return_value=socket_path):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.backlog.load_backlog", return_value=[item]):
                with patch("hopper.backlog.find_by_prefix", return_value=item):
                    with patch(
                        "hopper.client.set_backlog_queued",
                        return_value=True,
                    ) as mock_set_queued:
                        assert cmd_backlog(["queue", "abc", "--clear"]) == 0

    mock_set_queued.assert_called_once_with(socket_path, "abc123", None)
    out = capsys.readouterr().out
    assert "Cleared queue for:" in out


def test_backlog_queue_not_found(capsys):
    with patch("hopper.client.probe_server", return_value="up"):
        with patch("hopper.backlog.load_backlog", return_value=[]):
            with patch("hopper.backlog.find_by_prefix", return_value=None):
                assert cmd_backlog(["queue", "abc", "lode42"]) == 1

    out = capsys.readouterr().out
    assert "No unique backlog item matching" in out


def test_backlog_queue_missing_lode_id(capsys):
    item = _mock_backlog_item()

    with patch("hopper.client.probe_server", return_value="up"):
        with patch("hopper.backlog.load_backlog", return_value=[item]):
            with patch("hopper.backlog.find_by_prefix", return_value=item):
                assert cmd_backlog(["queue", "abc"]) == 1

    out = capsys.readouterr().out
    assert "lode ID required" in out


# --- cmd_lode tests ---


def test_lode_help(capsys):
    """--help prints usage and exits."""
    assert cmd_lode(["--help"]) == 0
    out = capsys.readouterr().out
    assert "list" in out
    assert "create" in out
    assert out.count(HELP_SKILL_REMINDER) == 1


def test_lode_list_help_documents_partial_all_host_results(capsys):
    assert cmd_lode(["list", "--help"]) == 0

    out = capsys.readouterr().out
    assert "every pool host" in out
    assert "unavailable_hosts" in out
    assert "exit 2" in out


def test_lode_no_server(capsys):
    """All actions fail gracefully when server is not running."""
    with patch("hopper.cli.require_server", return_value=1):
        assert cmd_lode([]) == 1


def test_lode_list_empty(capsys):
    """List with no active lodes prints empty message."""
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.list_lodes", return_value=[]):
            assert cmd_lode([]) == 0
    out = capsys.readouterr().out
    assert "No active lodes" in out


@pytest.mark.parametrize("rows", [None, [{"id": "abc23456", "active": False}]])
def test_lode_list_refuses_unavailable_or_malformed_server_rows(rows, capsys):
    with (
        patch("hopper.cli.require_server", return_value=None),
        patch("hopper.client.list_lodes", return_value=rows),
    ):
        assert cmd_lode(["list", "--json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "server response was missing or malformed" in captured.err


def test_lode_list_with_lodes(capsys):
    """List shows lodes sorted by stage order with correct icons."""
    lodes = [
        {
            "id": "refine01",
            "stage": "refine",
            "state": "running",
            "active": True,
            "project": "proj-a",
            "title": "do stuff",
            "status": "Working...",
        },
        {
            "id": "mill0001",
            "stage": "mill",
            "state": "new",
            "active": False,
            "project": "proj-b",
            "title": "new task",
            "status": "Ready",
        },
    ]
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.list_lodes", return_value=lodes):
            assert cmd_lode([]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.strip().split("\n") if line.strip()]
    assert "mill0001" in lines[0]
    assert "refine01" in lines[1]
    # mill0001 is not active and not shipped -> disconnected icon ⊘
    assert "⊘" in lines[0]
    # refine01 is active and running -> running icon ●
    assert "●" in lines[1]


def test_lode_list_disconnected_icon(capsys):
    """List shows disconnected icon for inactive non-shipped lode."""
    lodes = [
        {
            "id": "test0001",
            "stage": "refine",
            "state": "running",
            "active": False,
            "project": "proj",
            "title": "test",
            "status": "Waiting",
        },
    ]
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.list_lodes", return_value=lodes):
            assert cmd_lode([]) == 0
    out = capsys.readouterr().out
    assert "⊘" in out


def test_lode_list_archived_empty(capsys):
    """List --archived with no lodes prints empty message."""
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.list_archived_lodes", return_value=[]):
            assert cmd_lode(["list", "-a"]) == 0
    out = capsys.readouterr().out
    assert "No archived lodes" in out


def test_lode_list_archived_sorted(capsys):
    """List --archived sorts lodes by updated_at descending."""
    lodes = [
        {
            "id": "old00001",
            "stage": "shipped",
            "state": "shipped",
            "active": False,
            "project": "proj-a",
            "title": "old",
            "status": "Done",
            "updated_at": 1000,
            "created_at": 900,
        },
        {
            "id": "new00001",
            "stage": "shipped",
            "state": "shipped",
            "active": False,
            "project": "proj-b",
            "title": "new",
            "status": "Done",
            "updated_at": 2000,
            "created_at": 1800,
        },
    ]
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.list_archived_lodes", return_value=lodes):
            assert cmd_lode(["list", "--archived"]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.strip().split("\n") if line.strip()]
    # new00001 (updated_at=2000) should appear first
    assert "new00001" in lines[0]
    assert "old00001" in lines[1]


def test_lode_list_project_filter(capsys):
    """List -p filters active lodes by project."""
    lodes = [
        {
            "id": "hop00001",
            "stage": "mill",
            "state": "new",
            "active": False,
            "project": "hopper",
            "title": "",
            "status": "",
        },
        {
            "id": "oth00001",
            "stage": "refine",
            "state": "running",
            "active": True,
            "project": "other",
            "title": "",
            "status": "",
        },
    ]
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.list_lodes", return_value=lodes):
            assert cmd_lode(["list", "-p", "hopper"]) == 0
    out = capsys.readouterr().out
    assert "hop00001" in out
    assert "oth00001" not in out


def test_lode_list_project_filter_no_match(capsys):
    """List -p with no matches prints the standard empty message."""
    lodes = [
        {
            "id": "oth00001",
            "stage": "refine",
            "state": "running",
            "active": True,
            "project": "other",
            "title": "",
            "status": "",
        },
    ]
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.list_lodes", return_value=lodes):
            assert cmd_lode(["list", "-p", "nonexistent"]) == 0
    out = capsys.readouterr().out
    assert "No active lodes" in out


def test_lode_list_archived_project_filter(capsys):
    """List -a -p filters archived lodes by project."""
    lodes = [
        {
            "id": "hop00001",
            "stage": "shipped",
            "state": "ready",
            "active": False,
            "project": "hopper",
            "title": "",
            "status": "",
            "updated_at": 2000,
            "created_at": 1900,
        },
        {
            "id": "oth00001",
            "stage": "shipped",
            "state": "ready",
            "active": False,
            "project": "other",
            "title": "",
            "status": "",
            "updated_at": 1000,
            "created_at": 900,
        },
    ]
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.list_archived_lodes", return_value=lodes):
            assert cmd_lode(["list", "-a", "-p", "hopper"]) == 0
    out = capsys.readouterr().out
    assert "hop00001" in out
    assert "oth00001" not in out


def test_lode_create_happy(capsys):
    """Create reads scope from stdin, sends correct message, prints confirmation."""
    from io import StringIO

    created_lode = {"id": "abc12345", "project": "myproj", "stage": "mill"}
    project = Project(path="/fake/repo", name="myproj")
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.projects.find_project", return_value=project):
            with patch("hopper.git.dirty_status", return_value=""):
                with patch("hopper.client.create_lode", return_value=created_lode) as mock_create:
                    with patch("sys.stdin", StringIO(LONG_SCOPE)):
                        assert cmd_lode(["create", "myproj"]) == 0
                    mock_create.assert_called_once()
                    assert mock_create.call_args.kwargs["spawn"] is True
    out = capsys.readouterr().out
    assert "abc12345" in out
    assert "myproj" in out


def test_lode_create_dirty_repo_rejected(capsys):
    from io import StringIO

    project = Project(path="/fake/repo", name="myproj")
    with patch("hopper.cli.require_not_inside_lode", return_value=None):
        with patch("hopper.projects.find_project", return_value=project):
            with patch("hopper.git.dirty_status", return_value=" M file.py"):
                with patch("sys.stdin", StringIO("A" * 50)):
                    rc = cmd_lode(["create", "myproj"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "uncommitted changes" in out
    assert "use --force to override" in out


def test_lode_create_rejects_disabled_project_before_dirty_check(capsys):
    """Disabled projects are rejected before dirty checks or create RPC."""
    from io import StringIO

    project = Project(path="/fake", name="P", disabled=True, disabled_reason="wip")
    with (
        patch("hopper.projects.find_project", return_value=project),
        patch("hopper.git.dirty_status") as mock_dirty_status,
        patch("hopper.client.create_lode") as mock_create_lode,
        patch("sys.stdin", StringIO(LONG_SCOPE)),
    ):
        result = cmd_lode(["create", "P"])

    assert result == 1
    captured = capsys.readouterr()
    assert "error: project 'P' is disabled" in captured.out
    assert "  reason: wip" in captured.out
    mock_dirty_status.assert_not_called()
    mock_create_lode.assert_not_called()


def test_lode_create_dirty_hint_before_files(capsys):
    from io import StringIO

    project = Project(path="/fake/repo", name="myproj")
    with patch("hopper.cli.require_not_inside_lode", return_value=None):
        with patch("hopper.projects.find_project", return_value=project):
            with patch("hopper.git.dirty_status", return_value=" M file.py\n?? new.txt"):
                with patch("sys.stdin", StringIO(LONG_SCOPE)):
                    rc = cmd_lode(["create", "myproj"])
    assert rc == 1
    lines = capsys.readouterr().out.splitlines()
    hint_index = lines.index("hint: commit or stash changes first, or use --force to override.")
    file_index = lines.index("   M file.py")
    assert hint_index < file_index


def test_lode_create_dirty_repo_force_override(capsys):
    from io import StringIO

    created_lode = {"id": "abc12345", "project": "myproj", "stage": "mill"}
    project = Project(path="/fake/repo", name="myproj")
    with patch("hopper.cli.require_not_inside_lode", return_value=None):
        with patch("hopper.projects.find_project", return_value=project):
            with patch("hopper.git.dirty_status", return_value=" M file.py"):
                with patch("hopper.cli.require_server", return_value=None):
                    with patch(
                        "hopper.client.create_lode", return_value=created_lode
                    ) as mock_create:
                        with patch("sys.stdin", StringIO("A" * 50)):
                            rc = cmd_lode(["create", "--force", "myproj"])
    assert rc == 0
    mock_create.assert_called_once()


def test_lode_create_rejects_inside_lode(monkeypatch, capsys):
    from io import StringIO

    monkeypatch.setenv("HOPPER_LID", "test-lode-123")

    with patch("sys.stdin", StringIO(LONG_SCOPE)):
        rc = cmd_lode(["create", "proj"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "Cannot run this command inside lode test-lode-123." in out
    assert "hop backlog add" in out


def test_lode_create_missing_project(capsys):
    """Create with no project arg shows error and full help."""
    assert cmd_lode(["create"]) == 1
    out = capsys.readouterr().out
    assert "error:" in out
    assert "required" in out
    assert "scope is read from stdin" in out


def test_lode_create_reads_scope_from_stdin(capsys):
    """Create accepts scope from stdin when positional scope is omitted."""
    from io import StringIO

    created_lode = {"id": "abc12345", "project": "myproj", "stage": "mill"}
    project = Project(path="/fake/repo", name="myproj")
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.projects.find_project", return_value=project):
            with patch("hopper.git.dirty_status", return_value=""):
                with patch("hopper.client.create_lode", return_value=created_lode) as mock_create:
                    with patch(
                        "sys.stdin",
                        StringIO(LONG_SCOPE),
                    ):
                        assert cmd_lode(["create", "myproj"]) == 0
                    mock_create.assert_called_once()
                    assert mock_create.call_args.args[2] == LONG_SCOPE


def test_lode_create_missing_scope(capsys):
    """Create with empty stdin returns a helpful error."""
    from io import StringIO

    with patch("sys.stdin", StringIO("")):
        assert cmd_lode(["create", "myproj"]) == 1
    out = capsys.readouterr().out
    assert "no scope provided" in out


def test_lode_create_invalid_project(capsys):
    """Create with unknown project prints error with project list."""
    from io import StringIO
    from unittest.mock import MagicMock

    fake_proj = MagicMock()
    fake_proj.name = "proj-a"

    with patch("hopper.projects.find_project", return_value=None):
        with patch("hopper.projects.get_active_projects", return_value=[fake_proj]):
            with patch("sys.stdin", StringIO(LONG_SCOPE)):
                assert cmd_lode(["create", "badproj"]) == 1
    out = capsys.readouterr().out
    assert "Project 'badproj' not found." in out
    assert "Registered projects: proj-a" in out


def test_lode_create_scope_too_short(capsys):
    """Create with scope shorter than 42 chars from stdin shows error."""
    from io import StringIO

    with patch("sys.stdin", StringIO("short scope")):
        assert cmd_lode(["create", "myproj"]) == 1
    out = capsys.readouterr().out
    assert "scope too short" in out
    assert "11 chars" in out
    assert "minimum 42" in out


def test_lode_create_scope_too_short_stdin(capsys):
    """Scope from stdin under 42 chars shows error."""
    from io import StringIO

    with patch("sys.stdin", StringIO("short scope")):
        assert cmd_lode(["create", "myproj"]) == 1
    out = capsys.readouterr().out
    assert "scope too short" in out


def test_lode_create_requires_stdin(capsys):
    """Create on a TTY (no stdin pipe) shows a helpful error."""
    from io import StringIO

    tty_stdin = StringIO("")
    tty_stdin.isatty = lambda: True
    with patch("sys.stdin", tty_stdin):
        assert cmd_lode(["create", "myproj"]) == 1
    out = capsys.readouterr().out
    assert "scope must be provided via stdin" in out


def test_implement_delegates_to_lode_create(capsys):
    """hop implement delegates to hop lode create."""
    from io import StringIO

    created_lode = {"id": "abc12345", "project": "myproj", "stage": "mill"}
    project = Project(path="/fake/repo", name="myproj")
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.projects.find_project", return_value=project):
            with patch("hopper.git.dirty_status", return_value=""):
                with patch("hopper.client.create_lode", return_value=created_lode) as mock_create:
                    with patch("sys.stdin", StringIO(LONG_SCOPE)):
                        assert cmd_implement(["myproj"]) == 0
                    mock_create.assert_called_once()
                    assert mock_create.call_args.kwargs["spawn"] is True
    out = capsys.readouterr().out
    assert "abc12345" in out
    assert "myproj" in out


def test_implement_warns_with_registered_runner_count_and_still_creates(capsys):
    from io import StringIO

    created_lode = {"id": "abc12345", "project": "myproj", "stage": "mill"}
    project = Project(path="/fake/repo", name="myproj")
    with (
        patch("hopper.cli.require_server", return_value=None),
        patch("hopper.projects.find_project", return_value=project),
        patch("hopper.git.dirty_status", return_value=""),
        patch("hopper.cli.os.getloadavg", return_value=(56.0, 28.0, 14.0)),
        patch("hopper.cli.os.cpu_count", return_value=16),
        patch(
            "hopper.client.list_lodes",
            return_value=[{"active": True}, {"active": True}, {"active": False}],
        ),
        patch("hopper.client.create_lode", return_value=created_lode) as create,
        patch("sys.stdin", StringIO(LONG_SCOPE)),
    ):
        assert cmd_implement(["myproj"]) == 0

    create.assert_called_once()
    assert capsys.readouterr().err == (
        "warning: target load 1m=56.00 5m=28.00 15m=14.00 across 16 logical CPUs; "
        "lodes with a registered runner=2; creating anyway\n"
    )


def test_load_report_failure_is_swallowed():
    with patch("hopper.cli.os.getloadavg", side_effect=OSError("unavailable")):
        hopper_cli._warn_target_load(Path("/tmp/test.sock"))


def test_implement_rejects_inside_lode(monkeypatch, capsys):
    """hop implement rejects when inside a lode."""
    from io import StringIO

    monkeypatch.setenv("HOPPER_LID", "test-lode-123")

    with patch("sys.stdin", StringIO(LONG_SCOPE)):
        rc = cmd_implement(["proj"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "Cannot run this command inside lode test-lode-123." in out
    assert "hop backlog add" in out


def test_implement_reads_stdin(capsys):
    """hop implement reads scope from stdin when omitted."""
    from io import StringIO

    created_lode = {"id": "abc12345", "project": "myproj", "stage": "mill"}
    project = Project(path="/fake/repo", name="myproj")
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.projects.find_project", return_value=project):
            with patch("hopper.git.dirty_status", return_value=""):
                with patch("hopper.client.create_lode", return_value=created_lode) as mock_create:
                    with patch(
                        "sys.stdin",
                        StringIO(LONG_SCOPE),
                    ):
                        assert cmd_implement(["myproj"]) == 0
                    mock_create.assert_called_once()
                    assert mock_create.call_args.args[2] == LONG_SCOPE


def test_implement_no_args_shows_help(capsys):
    """hop implement with no args shows implement help, not lode help."""
    assert cmd_implement([]) == 1
    out = capsys.readouterr().out
    assert "hop implement" in out
    assert "scope is read from stdin" in out


def test_implement_scope_too_short(capsys):
    """hop implement with short scope from stdin shows error."""
    from io import StringIO

    with patch("sys.stdin", StringIO("short")):
        assert cmd_implement(["myproj"]) == 1
    out = capsys.readouterr().out
    assert "scope too short" in out


def test_implement_help_shows_epilog(capsys):
    """hop implement --help includes stdin usage epilog."""
    assert cmd_implement(["--help"]) == 0
    out = capsys.readouterr().out
    assert "scope is read from stdin" in out
    assert "42 characters" in out
    assert "hop implement" in out


def test_implement_requires_stdin(capsys):
    """hop implement on a TTY (no stdin pipe) shows a helpful error."""
    from io import StringIO

    tty_stdin = StringIO("")
    tty_stdin.isatty = lambda: True
    with patch("sys.stdin", tty_stdin):
        assert cmd_implement(["myproj"]) == 1
    out = capsys.readouterr().out
    assert "scope must be provided via stdin" in out


def test_lode_create_help_shows_epilog(capsys):
    """hop lode create --help includes stdin usage epilog."""
    assert cmd_lode(["create", "--help"]) == 0
    out = capsys.readouterr().out
    assert "scope is read from stdin" in out
    assert "42 characters" in out


def test_lode_restart_happy(capsys):
    """Restart sends correct message and prints confirmation."""
    generation = "1" * 32
    lode = {
        "id": "test1234",
        "stage": "mill",
        "state": "new",
        "active": False,
        "run_generation": generation,
    }
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
            with patch(
                "hopper.client.restart_lode",
                return_value={
                    "type": "lode_action_ack",
                    "outcome": "completed",
                    "disposition": "replacement_spawned",
                },
            ) as mock_restart:
                assert cmd_lode(["restart", "test1234"]) == 0
                mock_restart.assert_called_once()
    out = capsys.readouterr().out
    assert "test1234" in out
    assert "mill" in out


def test_lode_restart_pending_completion_uses_retry_not_stage_reset(capsys):
    action_id = "a" * 32
    generation = "b" * 32
    lode = {
        "id": "abcd2345",
        "stage": "mill",
        "state": "teardown",
        "active": True,
        "run_generation": generation,
        "pending_action": {
            "action_id": action_id,
            "expected_generation": generation,
            "action_type": "completion",
            "target_disposition": "advance_refine",
        },
    }
    with (
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch(
            "hopper.client.submit_lode_action",
            return_value={
                "type": "lode_action_ack",
                "outcome": "completed",
                "disposition": "advance_refine",
            },
        ) as retry,
        patch("hopper.client.restart_lode") as restart,
    ):
        assert cmd_lode(["restart", "abcd2345"]) == 0

    retry.assert_called_once_with(
        config.server_socket_path(),
        action_id=action_id,
        lode_id="abcd2345",
        expected_generation=generation,
        action_type="completion",
        target_disposition="advance_refine",
        force_consent=False,
        stage="mill",
    )
    restart.assert_not_called()
    assert "Completed durable teardown" in capsys.readouterr().out


def test_lode_repair_output_sends_exact_bytes_and_record_identity(capsys):
    data = b"accepted bytes\n"
    record = {
        "action_id": "a" * 32,
        "stage": "mill",
        "expected_generation": "b" * 32,
        "next_action": {"kind": "advance", "target_stage": "refine"},
    }
    lode = {"id": "abcd2345", "stage": "mill", "state": "teardown", "active": False}
    with (
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch("hopper.actions.load_pending_action", return_value=record),
        patch(
            "hopper.client.repair_lode_output",
            return_value={"type": "lode_repair_output_ack", "accepted": True},
        ) as repair,
        patch("sys.stdin", _processed_stdin(data.decode())),
    ):
        assert cmd_lode(["repair-output", "abcd2345", "-", "--token", "T" * 43]) == 0

    repair.assert_called_once_with(
        config.server_socket_path(),
        lode_id="abcd2345",
        action_id="a" * 32,
        stage="mill",
        expected_generation="b" * 32,
        next_action={"kind": "advance", "target_stage": "refine"},
        token="T" * 43,
        output_base64=base64.b64encode(data).decode("ascii"),
        byte_length=len(data),
        digest_hex=hashlib.sha256(data).hexdigest(),
    )
    assert "Accepted exact replacement bytes" in capsys.readouterr().out


def test_remote_lode_repair_output_preserves_non_utf8_bytes(capsys):
    import io

    data = b"\xff\x00accepted\n"
    stdin = MagicMock()
    stdin.buffer = io.BytesIO(data)
    with (
        patch(
            "hopper.cli._resolve_lode",
            return_value={
                "outcome": "found",
                "canonical_id": "abcd2345",
                "host": "remote.example",
            },
        ),
        patch("hopper.cli._run_remote_cli", return_value=0) as run_remote,
        patch("sys.stdin", stdin),
    ):
        assert cmd_lode(["repair-output", "abcd2345", "-", "--token", "T" * 43]) == 0

    run_remote.assert_called_once_with(
        "remote.example",
        ["lode", "repair-output", "abcd2345", "-", "--token", "T" * 43],
        reason="lode abcd2345",
        stdin_bytes=data,
    )


def test_lode_repair_output_refusal_is_nonzero_and_names_unchanged_canonical(capsys):
    lode = {"id": "abcd2345", "stage": "mill", "state": "teardown", "active": False}
    with (
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch("hopper.actions.load_pending_action", return_value=None),
        patch(
            "hopper.client.repair_lode_output",
            return_value={
                "type": "lode_repair_output_ack",
                "accepted": False,
                "reason": "unauthenticated",
            },
        ),
        patch("sys.stdin", _processed_stdin("candidate\n")),
    ):
        assert cmd_lode(["repair-output", "abcd2345", "-", "--token", "wrong"]) == 1

    output = capsys.readouterr().out
    assert "unauthenticated" in output
    assert "Canonical output is unchanged" in output


def test_lode_restart_rejects_inside_lode(monkeypatch, capsys):
    monkeypatch.setenv("HOPPER_LID", "test-lode-123")

    rc = cmd_lode(["restart", "some-id"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "Cannot run this command inside lode test-lode-123." in out
    assert "hop backlog add" in out


def test_lode_restart_not_found(capsys):
    """Restart with unknown lode ID prints error."""
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.read_lode_snapshot", return_value=("absent", None)):
            assert cmd_lode(["restart", "bad_id"]) == 1
    out = capsys.readouterr().out
    assert "not found" in out.lower()


def test_lode_restart_active(capsys):
    """The server's active-runner refusal is rendered with its recovery command."""
    lode = {"id": "test1234", "stage": "mill", "state": "running", "active": True}
    with (
        patch("hopper.cli.require_server", return_value=None),
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch(
            "hopper.client.restart_lode",
            return_value={
                "type": "lode_action_ack",
                "outcome": "refused",
                "reason": "registered_runner_requires_force",
                "detail": "Cannot restart: lode test1234 has a registered runner.",
                "recovery_command": "hop lode restart test1234 --force",
            },
        ),
    ):
        assert cmd_lode(["restart", "test1234"]) == 1
    out = capsys.readouterr().out
    assert "registered runner" in out.lower()
    assert "--force" in out


def test_lode_restart_unknown_ack_gives_status_next_step(capsys):
    lode = {
        "id": "test1234",
        "stage": "mill",
        "state": "new",
        "active": False,
        "run_generation": "b" * 32,
    }
    minted = MagicMock(hex="a" * 32)
    with (
        patch("hopper.cli.require_server", return_value=None),
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch("hopper.cli.uuid.uuid4", return_value=minted) as mint,
        patch("hopper.client.restart_lode", return_value=None),
    ):
        assert cmd_lode(["restart", "test1234", "--force"]) == 1

    out = capsys.readouterr().out
    mint.assert_called_once_with()
    assert "disposition is UNKNOWN" in out
    assert f"Action ID: {'a' * 32}" in out
    assert f"Expected generation: {'b' * 32}" in out
    assert "hop lode status test1234" in out
    assert (
        f"hop lode restart test1234 --force --action-id {'a' * 32} "
        f"--expected-generation {'b' * 32}" in out
    )


@pytest.mark.parametrize(
    "reason",
    [
        "lode_not_found",
        "invalid_stage",
        "pending_runner_result",
        "runner_identity_unknown",
        "runner_identity_unverified",
        "termination_failed",
        "already_live",
        "tmux_unreachable",
        "spawn_failed",
        "future_refusal",
    ],
)
def test_lode_restart_refusal_names_operator_next_steps(reason, capsys):
    lode = {"id": "test1234", "stage": "mill", "state": "new", "active": False}
    with (
        patch("hopper.cli.require_server", return_value=None),
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch(
            "hopper.client.restart_lode",
            return_value={
                "type": "lode_action_ack",
                "outcome": "refused",
                "reason": reason,
                "recovery_command": "hop lode status test1234",
            },
        ),
    ):
        assert cmd_lode(["restart", "test1234", "--force"]) == 1

    out = capsys.readouterr().out
    assert reason in out
    assert "hop lode status test1234" in out


def test_lode_restart_shipped(capsys):
    """Restart of shipped lode prints error."""
    lode = {"id": "test1234", "stage": "shipped", "state": "shipped", "active": False}
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
            assert cmd_lode(["restart", "test1234"]) == 1
    out = capsys.readouterr().out
    assert "shipped" in out.lower()


def test_lode_restart_missing_id(capsys):
    """Restart with no lode ID reports missing required argument."""
    assert cmd_lode(["restart"]) == 1
    out = capsys.readouterr().out
    assert "required" in out


def test_lode_restart_renders_server_started_stage_refusal(capsys):
    """Restart delegates the started-stage safety rule to the raw server boundary."""
    lode = {
        "id": "test1234",
        "stage": "mill",
        "state": "new",
        "active": False,
        "claude": {"mill": {"started": True}},
    }
    with (
        patch("hopper.cli.require_server", return_value=None),
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch(
            "hopper.client.restart_lode",
            return_value={
                "type": "lode_action_ack",
                "outcome": "refused",
                "reason": "started_stage_requires_force",
                "detail": "Lode test1234 has been started (claude[mill].started=True).",
                "recovery_command": "hop lode restart test1234 --force",
            },
        ) as mock_restart,
    ):
        result = cmd_lode(["restart", "test1234"])
    assert result == 1
    mock_restart.assert_called_once()
    output = capsys.readouterr().out
    assert "started_stage_requires_force" in output
    assert "hop lode restart test1234 --force" in output


def test_lode_restart_force_proceeds_when_started(capsys):
    """Restart --force bypasses the started guard."""
    lode = {
        "id": "test1234",
        "stage": "mill",
        "state": "new",
        "active": False,
        "claude": {"mill": {"started": True}},
    }
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
            with patch(
                "hopper.client.restart_lode",
                return_value={
                    "type": "lode_action_ack",
                    "outcome": "completed",
                    "disposition": "replacement_spawned",
                },
            ) as mock_restart:
                result = cmd_lode(["restart", "test1234", "--force"])
    assert result == 0
    mock_restart.assert_called_once()
    assert "Restarted mill for test1234" in capsys.readouterr().out


def test_lode_restart_error_proceeds_when_started_without_force(capsys):
    """An inactive failed stage can restart without forcing work discard."""
    lode = {
        "id": "test1234",
        "stage": "mill",
        "state": "error",
        "active": False,
        "claude": {"mill": {"started": True}},
    }
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
            with patch(
                "hopper.client.restart_lode",
                return_value={
                    "type": "lode_action_ack",
                    "outcome": "completed",
                    "disposition": "replacement_spawned",
                },
            ) as mock_restart:
                result = cmd_lode(["restart", "test1234"])
    assert result == 0
    mock_restart.assert_called_once()
    assert "Restarted mill for test1234" in capsys.readouterr().out


def test_lode_log_happy(temp_config, monkeypatch, capsys):
    monkeypatch.setattr(
        "hopper.client.read_lode_snapshot",
        lambda _socket, prefix: ("found", {"id": prefix}),
    )
    log_file = temp_config / "activity.log"
    log_file.write_text(
        "\n".join(
            [
                "2026-01-01 12:00:00.123 hopper.server INFO Lode test1234 created project=myproj",
                "2026-01-01 12:00:01.123 hopper.server INFO lode=test1234 state=running",
                "2026-01-01 12:00:01.456 hopper.server WARNING Pane delivery failed "
                "lode=test1234 pane=%1 reason=pane_state_unknown "
                'outcome=pane_state_unknown title="_ Working"',
                "2026-01-01 12:00:02.123 hopper.server INFO Lode other999 ignored",
            ]
        )
        + "\n"
    )

    rc = cmd_lode(["log", "test1234"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Lode test1234 created" in out
    assert "lode=test1234 state=running" in out
    assert "reason=pane_state_unknown" in out
    assert "other999" not in out


def test_lode_log_json(temp_config, monkeypatch, capsys):
    monkeypatch.setattr(
        "hopper.client.read_lode_snapshot",
        lambda _socket, prefix: ("found", {"id": prefix}),
    )
    log_file = temp_config / "activity.log"
    log_file.write_text(
        "2026-01-01 12:00:00.123 hopper.server INFO Lode test1234 created project=myproj\n"
    )

    rc = cmd_lode(["log", "test1234", "--json"])

    assert rc == 0
    entries = json.loads(capsys.readouterr().out)
    assert len(entries) == 1
    assert set(entries[0]) == {"timestamp", "level", "message"}
    assert entries[0]["timestamp"] == "2026-01-01 12:00:00.123"
    assert entries[0]["level"] == "INFO"
    assert "Lode test1234 created project=myproj" in entries[0]["message"]


def test_lode_log_tail(temp_config, monkeypatch, capsys):
    monkeypatch.setattr(
        "hopper.client.read_lode_snapshot",
        lambda _socket, prefix: ("found", {"id": prefix}),
    )
    log_file = temp_config / "activity.log"
    log_file.write_text(
        "\n".join(
            [
                f"2026-01-01 12:00:{index:02d}.123 hopper.server INFO lode=test1234 step={index}"
                for index in range(10)
            ]
        )
        + "\n"
    )

    rc = cmd_lode(["log", "test1234", "-n", "3"])

    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert "step=7" in lines[0]
    assert "step=9" in lines[-1]


def test_lode_log_no_matches(temp_config, monkeypatch, capsys):
    monkeypatch.setattr(
        "hopper.client.read_lode_snapshot",
        lambda _socket, prefix: ("found", {"id": prefix}),
    )
    (temp_config / "activity.log").write_text(
        "2026-01-01 12:00:00.123 hopper.server INFO Lode other999 created\n"
    )

    rc = cmd_lode(["log", "test1234"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "No log entries found for lode test1234" in out


def test_lode_log_no_file(monkeypatch, capsys):
    monkeypatch.setattr(
        "hopper.client.read_lode_snapshot",
        lambda _socket, prefix: ("found", {"id": prefix}),
    )
    rc = cmd_lode(["log", "test1234"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "No activity log found." in out


def test_lode_log_missing_id(capsys):
    assert cmd_lode(["log"]) == 1
    out = capsys.readouterr().out
    assert "required" in out


def test_lode_kill_happy(capsys):
    lode = {
        "id": "test1234",
        "stage": "mill",
        "state": "running",
        "active": True,
        "run_generation": "1" * 32,
    }
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
            with patch(
                "hopper.client.kill_lode",
                return_value={
                    "type": "lode_action_ack",
                    "outcome": "completed",
                    "disposition": "killed_archived",
                },
            ) as mock_kill:
                rc = cmd_lode(["kill", "test1234"])

    assert rc == 0
    mock_kill.assert_called_once()
    out = capsys.readouterr().out
    assert "Killed lode test1234" in out
    assert "worktree and branch retained" in out


def test_lode_kill_reports_delivery_failure(capsys):
    lode = {"id": "test1234", "stage": "mill", "state": "running", "active": True}
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
            with patch("hopper.client.kill_lode", return_value=None):
                rc = cmd_lode(["kill", "test1234"])

    assert rc == 1
    output = capsys.readouterr().out
    assert "disposition is UNKNOWN" in output
    assert "hop lode status test1234" in output


def test_lode_pause_waits_for_terminal_disposition(capsys):
    lode = {"id": "test1234", "active": True, "stage": "mill", "state": "running"}
    response = {
        "type": "lode_action_ack",
        "outcome": "completed",
        "disposition": "paused",
    }
    with (
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch("hopper.client.pause_lode", return_value=response) as operation,
    ):
        assert cmd_lode(["pause", "test1234"]) == 0

    assert operation.call_args.args[:2] == (ANY, "test1234")
    assert "Paused lode test1234" in capsys.readouterr().out


def test_lode_resume_uses_existing_resume_protocol(capsys):
    lode = {"id": "test1234", "active": False, "stage": "mill", "state": "gated"}
    response = {
        "type": "lode_resumed",
        "lode": {"id": "test1234"},
        "tmux_pane": "%2",
    }
    with (
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch("hopper.client.resume_lode", return_value=response) as operation,
    ):
        assert cmd_lode(["resume", "test1234"]) == 0

    operation.assert_called_once_with(ANY, "test1234")
    assert "Resuming lode test1234" in capsys.readouterr().out


def test_lode_kill_shipped(capsys):
    lode = {"id": "test1234", "stage": "shipped", "state": "shipped", "active": False}
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
            with patch("hopper.client.kill_lode") as mock_kill:
                rc = cmd_lode(["kill", "test1234"])

    assert rc == 1
    mock_kill.assert_not_called()
    out = capsys.readouterr().out
    assert "stage is shipped" in out


def test_lode_kill_archived(capsys):
    archived = [
        {
            "id": "test1234",
            "stage": "mill",
            "state": "error",
            "active": False,
            "archived_at": 1,
        }
    ]
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", archived[0])):
            with patch(
                "hopper.client.kill_lode",
                return_value={
                    "type": "lode_action_ack",
                    "outcome": "refused",
                    "reason": "lode_archived",
                    "recovery_command": "hop lode status test1234",
                },
            ):
                rc = cmd_lode(["kill", "test1234"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "lode_archived" in out


def test_lode_kill_not_found(capsys):
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.read_lode_snapshot", return_value=("absent", None)):
            with patch("hopper.client.list_archived_lodes", return_value=[]):
                rc = cmd_lode(["kill", "missing"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "Observed: lode 'missing' was not found." in out
    assert "Hopper did not route or mutate a lode." in out
    assert "Recover with: hop lode list --all-hosts --json." in out


def test_lode_kill_missing_id(capsys):
    assert cmd_lode(["kill"]) == 1
    out = capsys.readouterr().out
    assert "required" in out


def _watch_resolution(lode, host="local"):
    return {
        "outcome": "found",
        "lode": {**lode, "host": host},
        "host": host,
        "canonical_id": lode["id"],
        "error": None,
        "probe_summary": f"{host}=found {lode['id']}",
        "exit_code": 0,
    }


def _watch_connection(messages=()):
    connection = MagicMock()

    def start(callback=None, on_connect=None):
        connection.callback = callback
        connection.on_connect = on_connect
        for message in messages:
            callback(message)

    connection.start = start
    return connection


def test_lode_watch_post_subscribe_reconcile_observes_shipped(capsys):
    """Watch exits 0 after a durable shipped reconciliation."""
    lode = {
        "id": "abc123",
        "stage": "refine",
        "state": "running",
        "status": "Working...",
        "active": True,
    }
    shipped = {**lode, "stage": "shipped", "status": "Done", "active": False}
    with (
        patch("hopper.cli._resolve_lode", return_value=_watch_resolution(lode)),
        patch(
            "hopper.cli._read_watch_snapshot",
            return_value=("found", shipped, "local=found abc123"),
        ),
        patch("hopper.client.HopperConnection", return_value=_watch_connection()),
    ):
        result = cmd_lode(["watch", "abc123"])

    assert result == 0
    assert capsys.readouterr().out.splitlines() == [
        "● abc123 refine  Working...",
        "✓ abc123 shipped  Done",
    ]


def test_lode_watch_on_connect_reconciles_before_interval(monkeypatch, capsys):
    initial = {
        "id": "abc123",
        "stage": "refine",
        "state": "running",
        "status": "Working",
        "active": True,
    }
    after_immediate = {**initial, "status": "Still working"}
    shipped = {**initial, "stage": "shipped", "status": "Done", "active": False}
    connection = _watch_connection()
    clock = [0.0]

    monkeypatch.setattr("hopper.cli._watch_monotonic", lambda: clock[0])

    def condition_wait(condition, timeout_s):
        assert timeout_s == 30.0
        assert connection.on_connect is not None
        connection.on_connect()

    monkeypatch.setattr("hopper.cli._watch_condition_wait", condition_wait)
    with (
        patch("hopper.cli._resolve_lode", return_value=_watch_resolution(initial)),
        patch(
            "hopper.cli._read_watch_snapshot",
            side_effect=[
                ("found", after_immediate, "local=found abc123"),
                ("found", shipped, "local=found abc123"),
            ],
        ) as read_snapshot,
        patch("hopper.client.HopperConnection", return_value=connection),
    ):
        assert cmd_lode(["watch", "abc123"]) == 0

    assert read_snapshot.call_count == 2
    assert clock[0] == 0.0
    assert capsys.readouterr().out.splitlines()[-1] == "✓ abc123 shipped  Done"


def test_lode_watch_rejects_inside_lode(monkeypatch, capsys):
    monkeypatch.setenv("HOPPER_LID", "test-lode-123")

    rc = cmd_lode(["watch", "some-id"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "Cannot run this command inside lode test-lode-123." in out
    assert "hop backlog add" in out


def test_lode_list_allowed_inside_lode(monkeypatch, capsys):
    """hop lode list should work inside a lode (read-only, no guard)."""
    monkeypatch.setenv("HOPPER_LID", "test-lode-123")

    with patch("hopper.cli.require_server", return_value=1) as mock_require_server:
        rc = cmd_lode(["list"])
    assert rc == 1
    mock_require_server.assert_called_once()
    out = capsys.readouterr().out
    assert "Cannot run this command inside lode" not in out


def test_lode_watch_error_exit(capsys):
    """Watch exits 1 when durable state enters error."""
    lode = {
        "id": "abc123",
        "stage": "mill",
        "state": "running",
        "status": "Working",
        "active": True,
    }
    failed = {**lode, "state": "error", "status": "Failed", "active": False}
    with (
        patch("hopper.cli._resolve_lode", return_value=_watch_resolution(lode)),
        patch(
            "hopper.cli._read_watch_snapshot",
            return_value=("found", failed, "local=found abc123"),
        ),
        patch("hopper.client.HopperConnection", return_value=_watch_connection()),
    ):
        result = cmd_lode(["watch", "abc123"])
    assert result == 1
    out = capsys.readouterr().out
    assert "error: lode abc123 is in error state" in out
    assert "to retry: hop lode restart abc123" in out


def test_lode_watch_archived_exit(capsys):
    """An already archived shipped lode prints once and exits 0."""
    lode = {
        "id": "abc123",
        "stage": "shipped",
        "state": "ready",
        "status": "Shipped",
        "active": False,
        "archived_at": 1234,
    }
    with (
        patch("hopper.cli._resolve_lode", return_value=_watch_resolution(lode)),
        patch("hopper.client.HopperConnection") as connection,
    ):
        result = cmd_lode(["watch", "abc123"])
    assert result == 0
    connection.assert_not_called()
    assert capsys.readouterr().out == "✓ abc123 shipped  Shipped\n"


def test_lode_watch_not_found(capsys):
    """Watch preserves authoritative absence."""
    resolution = {
        "outcome": "absent",
        "lode": None,
        "host": None,
        "canonical_id": None,
        "error": "Lode 'bogus' not found. Probes: local=absent.",
        "probe_summary": "local=absent",
        "exit_code": 1,
    }
    with patch("hopper.cli._resolve_lode", return_value=resolution):
        result = cmd_lode(["watch", "bogus"])
    assert result == 1
    assert capsys.readouterr().out == "Lode 'bogus' not found. Probes: local=absent.\n"


def test_lode_watch_not_active(capsys):
    """watch fails when lode is not active."""
    lode = {"id": "abc123", "active": False, "stage": "mill", "state": "new", "status": ""}
    with patch("hopper.cli._resolve_lode", return_value=_watch_resolution(lode)):
        result = cmd_lode(["watch", "abc123"])
    assert result == 1
    assert capsys.readouterr().out == (
        "⊘ abc123 mill  \nLode 'abc123' is not active.\nResume with: hop lode resume abc123\n"
    )


def test_lode_watch_error_state_at_start(capsys):
    lode = {
        "id": "abc123",
        "active": True,
        "stage": "mill",
        "state": "error",
        "status": "Something failed",
    }
    with patch("hopper.cli._resolve_lode", return_value=_watch_resolution(lode)):
        result = cmd_lode(["watch", "abc123"])
    assert result == 1
    out = capsys.readouterr().out
    assert "error state" in out
    assert "hop lode restart abc123" in out


def test_lode_watch_initial_state(capsys):
    """watch prints initial lode state before streaming."""
    lode = {
        "id": "abc123",
        "stage": "mill",
        "state": "running",
        "status": "Starting",
        "active": True,
    }
    shipped = {**lode, "stage": "shipped", "status": "Done", "active": False}
    with (
        patch("hopper.cli._resolve_lode", return_value=_watch_resolution(lode)),
        patch(
            "hopper.cli._read_watch_snapshot",
            return_value=("found", shipped, "local=found abc123"),
        ),
        patch("hopper.client.HopperConnection", return_value=_watch_connection()),
    ):
        cmd_lode(["watch", "abc123"])
    out = capsys.readouterr().out
    lines = out.strip().split("\n")
    assert len(lines) >= 2  # initial + at least one update
    assert "Starting" in lines[0]  # initial state
    assert "shipped" in lines[-1]  # final state


def test_watch_prints_banner_on_gate_in_and_out(capsys):
    """watch prints gate enter/exit banners without exiting early."""
    lode = {
        "id": "abc123",
        "stage": "refine",
        "state": "running",
        "status": "Working",
        "active": True,
    }
    messages = [
        {"type": "lode_updated", "lode": {**lode, "state": "gated", "status": "Gate"}},
        {
            "type": "lode_updated",
            "lode": {**lode, "state": "running", "status": "Resumed"},
        },
    ]
    shipped = {**lode, "stage": "shipped", "status": "Done", "active": False}
    with (
        patch("hopper.cli._resolve_lode", return_value=_watch_resolution(lode)),
        patch(
            "hopper.cli._read_watch_snapshot",
            return_value=("found", shipped, "local=found abc123"),
        ),
        patch(
            "hopper.client.HopperConnection",
            return_value=_watch_connection(messages),
        ),
    ):
        result = cmd_lode(["watch", "abc123"])
    assert result == 0
    out = capsys.readouterr().out
    assert "Lode abc123 is gated. Review with: hop gate show abc123" in out
    assert "Lode abc123 gate resumed." in out


def test_lode_wait_shipped(capsys):
    """wait exits 0 and prints shipped status when lode reaches shipped stage."""
    lode = {
        "id": "abc123",
        "stage": "refine",
        "state": "running",
        "status": "Working...",
        "active": True,
    }
    shipped = {**lode, "stage": "shipped", "status": "Done", "title": "Done task"}
    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
            mock_conn = MagicMock()

            def fake_start(callback, on_connect=None):
                callback(
                    {
                        "type": "lode_updated",
                        "lode": {
                            **lode,
                            "stage": "shipped",
                            "status": "Done",
                            "title": "Done task",
                        },
                    }
                )

            mock_conn.start = fake_start
            with patch("hopper.wait.read_local_snapshot", return_value=("found", shipped)):
                with patch("hopper.client.HopperConnection", return_value=mock_conn):
                    result = cmd_lode(["wait", "abc123"])
    assert result == 0
    assert "✓ abc123 shipped (Done task)" in capsys.readouterr().out


def test_lode_wait_shipped_no_title(capsys):
    """wait prints shipped line without parenthetical when title is empty."""
    lode = {
        "id": "abc123",
        "stage": "refine",
        "state": "running",
        "status": "Working...",
        "active": True,
        "title": "",
    }
    shipped = {**lode, "stage": "shipped"}
    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
            mock_conn = MagicMock()

            def fake_start(callback, on_connect=None):
                callback({"type": "lode_updated", "lode": {**lode, "stage": "shipped"}})

            mock_conn.start = fake_start
            with patch("hopper.wait.read_local_snapshot", return_value=("found", shipped)):
                with patch("hopper.client.HopperConnection", return_value=mock_conn):
                    result = cmd_lode(["wait", "abc123"])
    assert result == 0
    out = capsys.readouterr().out
    assert "✓ abc123 shipped" in out
    assert "(" not in out


def test_lode_wait_already_shipped(capsys):
    """wait exits 0 and prints summary when lode is already shipped."""
    lode = {
        "id": "abc123",
        "stage": "shipped",
        "state": "ready",
        "status": "Shipped",
        "active": False,
        "project": "proj",
        "title": "Done",
        "created_at": 1000,
        "updated_at": 2000,
    }
    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
            result = cmd_lode(["wait", "abc123"])
    assert result == 0
    out = capsys.readouterr().out
    assert "✓ abc123 shipped (Done)" in out


def test_lode_wait_archived_lode(capsys):
    """wait exits 0 and prints summary for archived lodes found via lookup."""
    archived = {
        "id": "arc12345",
        "stage": "shipped",
        "state": "ready",
        "status": "Done",
        "active": False,
        "project": "proj",
        "title": "Archive task",
        "created_at": 1000,
        "updated_at": 2000,
    }
    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", archived)):
            result = cmd_lode(["wait", "arc12345"])
    assert result == 0
    out = capsys.readouterr().out
    assert "✓ arc12345 shipped (Archive task)" in out


def test_lode_wait_prefix_match(capsys):
    """wait resolves prefix to an active lode ID and waits on that lode."""
    lode = {
        "id": "abc12345",
        "stage": "refine",
        "state": "running",
        "status": "Working",
        "active": True,
    }
    shipped = {**lode, "stage": "shipped", "status": "Done", "title": "Prefix task"}
    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
            mock_conn = MagicMock()

            def fake_start(callback, on_connect=None):
                callback(
                    {
                        "type": "lode_updated",
                        "lode": {
                            **lode,
                            "stage": "shipped",
                            "status": "Done",
                            "title": "Prefix task",
                        },
                    }
                )

            mock_conn.start = fake_start
            with patch("hopper.wait.read_local_snapshot", return_value=("found", shipped)):
                with patch("hopper.client.HopperConnection", return_value=mock_conn):
                    result = cmd_lode(["wait", "abc"])
    assert result == 0
    assert "✓ abc12345 shipped (Prefix task)" in capsys.readouterr().out


def test_lode_wait_prefix_not_active(capsys):
    """wait with prefix fails when matched lode is inactive and not shipped."""
    lode = {
        "id": "abc12345",
        "stage": "refine",
        "state": "new",
        "status": "",
        "active": False,
    }
    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
            result = cmd_lode(["wait", "abc"])
    assert result == 1
    assert "not active" in capsys.readouterr().out


def test_lode_wait_error(capsys):
    """wait exits 1 with message when lode enters error state."""
    lode = {
        "id": "abc123",
        "stage": "mill",
        "state": "running",
        "status": "Working",
        "active": True,
    }
    failed = {**lode, "state": "error", "status": "Failed"}
    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
            mock_conn = MagicMock()

            def fake_start(callback, on_connect=None):
                callback(
                    {"type": "lode_updated", "lode": {**lode, "state": "error", "status": "Failed"}}
                )

            mock_conn.start = fake_start
            with patch("hopper.wait.read_local_snapshot", return_value=("found", failed)):
                with patch("hopper.client.HopperConnection", return_value=mock_conn):
                    result = cmd_lode(["wait", "abc123"])
    assert result == 1
    out = capsys.readouterr().out
    assert "✗ abc123 error: Failed" in out
    assert "stage=mill state=error active=True status=Failed source=local" in out
    assert "observed_age_s=" in out
    assert "Restart with: hop lode restart abc123" in out


def test_wait_exits_2_on_gate_transition(capsys):
    """wait exits 2 and prints the gate review banner when a lode gates."""
    lode = {
        "id": "abc123",
        "stage": "refine",
        "state": "running",
        "status": "Working",
        "active": True,
    }
    gated = {**lode, "state": "gated", "status": "Gate"}
    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
            mock_conn = MagicMock()

            def fake_start(callback, on_connect=None):
                callback(
                    {"type": "lode_updated", "lode": {**lode, "state": "gated", "status": "Gate"}}
                )

            mock_conn.start = fake_start
            with patch("hopper.wait.read_local_snapshot", return_value=("found", gated)):
                with patch("hopper.client.HopperConnection", return_value=mock_conn):
                    result = cmd_lode(["wait", "abc123"])
    assert result == 2
    assert "Lode abc123 is gated. Review with: hop gate show abc123" in capsys.readouterr().out


def test_lode_wait_error_state_at_start(capsys):
    lode = {
        "id": "abc123",
        "active": True,
        "stage": "mill",
        "state": "error",
        "status": "Something failed",
    }
    with patch("hopper.cli.require_not_inside_lode", return_value=None):
        with patch("hopper.cli.require_server", return_value=None):
            with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
                result = cmd_lode(["wait", "abc123"])
    assert result == 1
    out = capsys.readouterr().out
    assert "error state" in out
    assert "hop lode restart abc123" in out


def _run_scripted_cli_wait(monkeypatch, initial_by_id, observations_by_id, args):
    now = [0.0]
    queues = {lid: list(items) for lid, items in observations_by_id.items()}
    latest = dict(initial_by_id)
    connection = MagicMock()

    def resolve_initial(socket_path, lid, timeout=2.0):
        lode = initial_by_id.get(lid)
        return ("found", lode) if lode is not None else ("absent", None)

    def read_snapshot(socket_path, lid):
        if queues.get(lid):
            latest[lid] = queues[lid].pop(0)
        return "found", latest[lid]

    def start(callback, on_connect=None):
        if on_connect:
            on_connect()

    def condition_wait(condition, timeout):
        now[0] += timeout

    connection.start = start
    monkeypatch.setattr("hopper.client.read_lode_snapshot", resolve_initial)
    monkeypatch.setattr("hopper.client.HopperConnection", lambda socket_path: connection)
    monkeypatch.setattr("hopper.wait.read_local_snapshot", read_snapshot)
    monkeypatch.setattr("hopper.wait._monotonic", lambda: now[0])
    monkeypatch.setattr("hopper.wait._condition_wait", condition_wait)
    return cmd_lode(["wait", *args]), now[0]


def test_wait_stuck_at_start(monkeypatch, capsys):
    lode = {
        "id": "abc123",
        "active": True,
        "stage": "mill",
        "state": "stuck",
        "status": "No output for 60s",
        "tmux_pane": "hopper:7",
    }
    monkeypatch.setattr("hopper.wait.capture_pane", lambda pane: "line1\nline2\nline3")
    result, elapsed = _run_scripted_cli_wait(
        monkeypatch,
        {"abc123": lode},
        {"abc123": [lode, lode]},
        ["abc123"],
    )
    assert result == 3
    assert elapsed == 120
    out = capsys.readouterr().out
    assert "✗ abc123 stuck: No output for 60s" in out
    assert "  pane: hopper:7" in out
    assert "  --- last 50 lines of pane ---" in out
    assert "  line1\n  line2\n  line3" in out
    assert "  --- end pane ---" in out


def test_wait_stuck_at_start_no_pane(monkeypatch, capsys):
    lode = {
        "id": "abc123",
        "active": True,
        "stage": "mill",
        "state": "stuck",
        "status": "No output for 60s",
        "tmux_pane": None,
    }
    mock_capture = MagicMock()
    monkeypatch.setattr("hopper.wait.capture_pane", mock_capture)
    result, elapsed = _run_scripted_cli_wait(
        monkeypatch,
        {"abc123": lode},
        {"abc123": [lode, lode]},
        ["abc123"],
    )
    assert result == 3
    assert elapsed == 120
    out = capsys.readouterr().out
    assert "  pane: <unknown>" in out
    assert "  --- last 50 lines of pane ---" not in out
    mock_capture.assert_not_called()


def test_wait_stuck_at_start_capture_fails(monkeypatch, capsys):
    lode = {
        "id": "abc123",
        "active": True,
        "stage": "mill",
        "state": "stuck",
        "status": "No output for 60s",
        "tmux_pane": "hopper:7",
    }
    monkeypatch.setattr("hopper.wait.capture_pane", lambda pane: None)
    result, elapsed = _run_scripted_cli_wait(
        monkeypatch,
        {"abc123": lode},
        {"abc123": [lode, lode]},
        ["abc123"],
    )
    assert result == 3
    assert elapsed == 120
    out = capsys.readouterr().out
    assert "  --- last 50 lines of pane ---" in out
    assert "  <pane capture failed>" in out
    assert "  --- end pane ---" in out


def test_wait_stuck_at_start_empty_status(monkeypatch, capsys):
    lode = {
        "id": "abc123",
        "active": True,
        "stage": "mill",
        "state": "stuck",
        "status": "",
        "tmux_pane": None,
    }
    result, elapsed = _run_scripted_cli_wait(
        monkeypatch,
        {"abc123": lode},
        {"abc123": [lode, lode]},
        ["abc123"],
    )
    assert result == 3
    assert elapsed == 120
    first_line = capsys.readouterr().out.splitlines()[0]
    assert first_line == "✗ abc123 stuck"


def test_wait_stuck_at_start_pane_truncated_to_50(monkeypatch, capsys):
    lode = {
        "id": "abc123",
        "active": True,
        "stage": "mill",
        "state": "stuck",
        "status": "No output for 60s",
        "tmux_pane": "hopper:7",
    }
    pane_capture = "\n".join(f"line{i}" for i in range(75))
    monkeypatch.setattr("hopper.wait.capture_pane", lambda pane: pane_capture)
    result, elapsed = _run_scripted_cli_wait(
        monkeypatch,
        {"abc123": lode},
        {"abc123": [lode, lode]},
        ["abc123"],
    )
    assert result == 3
    assert elapsed == 120
    out_lines = capsys.readouterr().out.splitlines()
    start = out_lines.index("  --- last 50 lines of pane ---") + 1
    end = out_lines.index("  --- end pane ---")
    assert out_lines[start:end] == [f"  line{i}" for i in range(25, 75)]


def test_wait_stuck_transition_grace_expires(monkeypatch, capsys):
    initial_lode = {
        "id": "abc123",
        "active": True,
        "stage": "mill",
        "state": "running",
        "status": "Working",
    }
    stuck_lode = {
        **initial_lode,
        "state": "stuck",
        "status": "No output for 60s",
        "tmux_pane": "hopper:7",
    }
    monkeypatch.setattr("hopper.wait.STUCK_GRACE_MS", 50)
    monkeypatch.setattr("hopper.wait.capture_pane", lambda pane: "line1")
    result, elapsed = _run_scripted_cli_wait(
        monkeypatch,
        {"abc123": initial_lode},
        {"abc123": [stuck_lode, stuck_lode]},
        ["abc123", "--timeout", "1"],
    )
    assert result == 3
    assert elapsed == pytest.approx(0.05)
    out = capsys.readouterr().out
    assert "✗ abc123 stuck: No output for 60s" in out
    assert "  line1" in out


def test_wait_stuck_transition_recovers_within_grace(monkeypatch, capsys):
    lode = {
        "id": "abc123",
        "active": True,
        "stage": "mill",
        "state": "running",
        "status": "Working",
    }
    stuck_lode = {**lode, "state": "stuck", "status": "No output for 60s"}
    running_lode = {**lode, "state": "running", "status": "Claude running"}
    shipped_lode = {**lode, "stage": "shipped", "status": "Done"}
    monkeypatch.setattr("hopper.wait.STUCK_GRACE_MS", 200)
    result, _ = _run_scripted_cli_wait(
        monkeypatch,
        {"abc123": lode},
        {"abc123": [stuck_lode, running_lode, shipped_lode]},
        ["abc123"],
    )
    assert result == 0
    out = capsys.readouterr().out
    assert "stuck:" not in out
    assert "  --- last 50 lines of pane ---" not in out
    assert "✓ abc123 shipped" in out


def test_wait_stuck_flap_rearms(monkeypatch, capsys):
    initial_lode = {
        "id": "abc123",
        "active": True,
        "stage": "mill",
        "state": "running",
        "status": "Working",
    }
    stuck_lode = {
        **initial_lode,
        "state": "stuck",
        "status": "No output for 60s",
        "tmux_pane": "hopper:7",
    }
    running_lode = {**initial_lode, "state": "running", "status": "Claude running"}
    monkeypatch.setattr("hopper.wait.STUCK_GRACE_MS", 100)
    monkeypatch.setattr("hopper.wait.capture_pane", lambda pane: "line1")
    result, _ = _run_scripted_cli_wait(
        monkeypatch,
        {"abc123": initial_lode},
        {"abc123": [stuck_lode, running_lode, stuck_lode, stuck_lode]},
        ["abc123", "--timeout", "60"],
    )
    assert result == 3
    out = capsys.readouterr().out
    assert out.count("✗ abc123 stuck: No output for 60s") == 1
    assert out.count("  --- last 50 lines of pane ---") == 1


def test_wait_stuck_multi_lode_first_wins(monkeypatch, capsys):
    stuck_lode = {
        "id": "aaa111",
        "active": True,
        "stage": "mill",
        "state": "stuck",
        "status": "No output for 60s",
        "tmux_pane": "hopper:7",
    }
    running_lode = {
        "id": "bbb222",
        "active": True,
        "stage": "refine",
        "state": "running",
        "status": "Working",
    }
    monkeypatch.setattr("hopper.wait.STUCK_GRACE_MS", 50)
    monkeypatch.setattr("hopper.wait.capture_pane", lambda pane: "line1")
    result, _ = _run_scripted_cli_wait(
        monkeypatch,
        {"aaa111": stuck_lode, "bbb222": running_lode},
        {"aaa111": [stuck_lode, stuck_lode], "bbb222": [running_lode]},
        ["aaa111", "bbb222"],
    )
    assert result == 3
    out = capsys.readouterr().out
    assert "✗ aaa111 stuck: No output for 60s" in out
    assert "bbb222" not in out


def test_wait_timeout_shorter_than_grace(monkeypatch, capsys):
    lode = {
        "id": "abc123",
        "active": True,
        "stage": "mill",
        "state": "running",
        "status": "Working",
    }
    stuck_lode = {**lode, "state": "stuck", "status": "No output for 60s"}
    monkeypatch.setattr("hopper.wait.STUCK_GRACE_MS", 200)
    result, elapsed = _run_scripted_cli_wait(
        monkeypatch,
        {"abc123": lode},
        {"abc123": [stuck_lode]},
        ["abc123", "--timeout", "0.05"],
    )
    assert result == 4
    assert elapsed == pytest.approx(0.05)
    out = capsys.readouterr().out
    assert "Timed out waiting for lode(s): abc123" in out
    assert "✗ abc123 stuck" not in out


def test_lode_wait_not_found(capsys):
    """wait fails when lode not found."""
    with patch(
        "hopper.cli._resolve_lode",
        return_value={
            "outcome": "absent",
            "error": "Lode 'bogus' not found.",
            "exit_code": 1,
        },
    ):
        result = cmd_lode(["wait", "bogus"])
    assert result == 1
    assert "not found" in capsys.readouterr().out


def test_lode_wait_not_active(capsys):
    """wait fails when lode is not active."""
    lode = {"id": "abc123", "active": False, "stage": "mill", "state": "new", "status": ""}
    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
            result = cmd_lode(["wait", "abc123"])
    assert result == 1
    assert "not active" in capsys.readouterr().out


def test_lode_wait_timeout(capsys):
    """wait exits 4 with timeout message when no terminal event arrives."""
    lode = {
        "id": "abc123",
        "stage": "mill",
        "state": "running",
        "status": "Working",
        "active": True,
    }
    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
            mock_conn = MagicMock()

            def fake_start(callback, on_connect=None):
                return None

            mock_conn.start = fake_start
            with patch("hopper.client.HopperConnection", return_value=mock_conn):
                result = cmd_lode(["wait", "abc123", "--timeout", "0.01"])
    assert result == 4
    assert "Timed out waiting for lode(s): abc123" in capsys.readouterr().out


def test_lode_wait_multi_all_ship(capsys):
    """wait exits 0 when multiple lodes all ship."""
    lode1 = {
        "id": "aaa111",
        "stage": "refine",
        "state": "running",
        "status": "Working",
        "active": True,
        "title": "First",
    }
    lode2 = {
        "id": "bbb222",
        "stage": "mill",
        "state": "running",
        "status": "Starting",
        "active": True,
        "title": "Second",
    }

    def fake_snapshot(socket_path, lode_id, timeout=2.0):
        if lode_id == "aaa111":
            return "found", lode1
        if lode_id == "bbb222":
            return "found", lode2
        return "absent", None

    shipped = {
        "aaa111": {**lode1, "stage": "shipped", "title": "First"},
        "bbb222": {**lode2, "stage": "shipped", "title": "Second"},
    }

    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.read_lode_snapshot", side_effect=fake_snapshot):
            mock_conn = MagicMock()

            def fake_start(callback, on_connect=None):
                callback(
                    {
                        "type": "lode_updated",
                        "lode": {**lode1, "stage": "shipped", "title": "First"},
                    }
                )
                callback(
                    {
                        "type": "lode_updated",
                        "lode": {**lode2, "stage": "shipped", "title": "Second"},
                    }
                )

            mock_conn.start = fake_start
            with patch(
                "hopper.wait.read_local_snapshot",
                side_effect=lambda socket_path, lid: ("found", shipped[lid]),
            ):
                with patch("hopper.client.HopperConnection", return_value=mock_conn):
                    result = cmd_lode(["wait", "aaa111", "bbb222"])
    assert result == 0
    out = capsys.readouterr().out
    assert "✓ aaa111 shipped (First)" in out
    assert "✓ bbb222 shipped (Second)" in out


def test_lode_wait_multi_one_errors(capsys):
    """wait exits 1 on first error when watching multiple lodes."""
    lode1 = {
        "id": "aaa111",
        "stage": "refine",
        "state": "running",
        "status": "Working",
        "active": True,
    }
    lode2 = {
        "id": "bbb222",
        "stage": "mill",
        "state": "running",
        "status": "Starting",
        "active": True,
    }

    def fake_snapshot(socket_path, lode_id, timeout=2.0):
        if lode_id == "aaa111":
            return "found", lode1
        if lode_id == "bbb222":
            return "found", lode2
        return "absent", None

    current = {
        "aaa111": {**lode1, "state": "error", "status": "Crashed"},
        "bbb222": lode2,
    }

    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.read_lode_snapshot", side_effect=fake_snapshot):
            mock_conn = MagicMock()

            def fake_start(callback, on_connect=None):
                callback(
                    {
                        "type": "lode_updated",
                        "lode": {**lode1, "state": "error", "status": "Crashed"},
                    }
                )

            mock_conn.start = fake_start
            with patch(
                "hopper.wait.read_local_snapshot",
                side_effect=lambda socket_path, lid: ("found", current[lid]),
            ):
                with patch("hopper.client.HopperConnection", return_value=mock_conn):
                    result = cmd_lode(["wait", "aaa111", "bbb222"])
    assert result == 1
    out = capsys.readouterr().out
    assert "✗ aaa111 error: Crashed" in out


def test_lode_wait_multi_mixed_shipped_and_pending(capsys):
    """wait handles mix of already-shipped and pending lodes."""
    shipped_lode = {
        "id": "aaa111",
        "stage": "shipped",
        "state": "shipped",
        "status": "Done",
        "active": False,
        "title": "Already done",
    }
    pending_lode = {
        "id": "bbb222",
        "stage": "refine",
        "state": "running",
        "status": "Working",
        "active": True,
        "title": "Still going",
    }

    def fake_snapshot(socket_path, lode_id, timeout=2.0):
        if lode_id == "aaa111":
            return "found", shipped_lode
        if lode_id == "bbb222":
            return "found", pending_lode
        return "absent", None

    finished_pending = {
        **pending_lode,
        "stage": "shipped",
        "title": "Still going",
    }

    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.read_lode_snapshot", side_effect=fake_snapshot):
            mock_conn = MagicMock()

            def fake_start(callback, on_connect=None):
                callback(
                    {
                        "type": "lode_updated",
                        "lode": {**pending_lode, "stage": "shipped", "title": "Still going"},
                    }
                )

            mock_conn.start = fake_start
            with patch("hopper.wait.read_local_snapshot", return_value=("found", finished_pending)):
                with patch("hopper.client.HopperConnection", return_value=mock_conn):
                    result = cmd_lode(["wait", "aaa111", "bbb222"])
    assert result == 0
    out = capsys.readouterr().out
    assert "✓ aaa111 shipped (Already done)" in out
    assert "✓ bbb222 shipped (Still going)" in out


def test_lode_wait_multi_timeout(capsys):
    """wait exits 4 with remaining IDs on timeout."""
    lode1 = {
        "id": "aaa111",
        "stage": "refine",
        "state": "running",
        "status": "Working",
        "active": True,
    }
    lode2 = {
        "id": "bbb222",
        "stage": "mill",
        "state": "running",
        "status": "Starting",
        "active": True,
    }

    def fake_snapshot(socket_path, lode_id, timeout=2.0):
        if lode_id == "aaa111":
            return "found", lode1
        if lode_id == "bbb222":
            return "found", lode2
        return "absent", None

    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.read_lode_snapshot", side_effect=fake_snapshot):
            mock_conn = MagicMock()
            mock_conn.start = MagicMock()
            with patch("hopper.client.HopperConnection", return_value=mock_conn):
                result = cmd_lode(["wait", "aaa111", "bbb222", "--timeout", "0.01"])
    assert result == 4
    out = capsys.readouterr().out
    assert "Timed out waiting for lode(s): aaa111, bbb222" in out


def test_lode_wait_multi_one_not_found(capsys):
    """wait fails fast if any lode ID is not found."""
    lode1 = {
        "id": "aaa111",
        "stage": "refine",
        "state": "running",
        "status": "Working",
        "active": True,
    }

    def resolve(socket_path, lode_id):
        if lode_id == "aaa111":
            return _watch_resolution(lode1)
        return {"outcome": "absent", "error": "Lode 'bogus' not found.", "exit_code": 1}

    with (
        patch("hopper.cli._resolve_lode", side_effect=resolve),
        patch("hopper.client.HopperConnection") as mock_conn_cls,
    ):
        result = cmd_lode(["wait", "aaa111", "bogus"])
    assert result == 1
    assert "not found" in capsys.readouterr().out
    mock_conn_cls.assert_not_called()


def test_lode_wait_remote_poll_json_shipped(capsys):
    initial = {
        "id": "abc23456",
        "stage": "mill",
        "state": "running",
        "status": "Working",
        "active": True,
        "project": "journal",
        "host": "fedora.local",
    }
    shipped = {**initial, "stage": "shipped", "state": "ready", "status": "Done"}
    with (
        patch(
            "hopper.cli._resolve_lode",
            return_value=_watch_resolution(initial, "fedora.local"),
        ),
        patch(
            "hopper.remote.run_remote",
            return_value=subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(shipped) + "\n",
                stderr="",
            ),
        ),
    ):
        result = cmd_lode(["wait", "abc23456", "--json", "--timeout", "1"])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "shipped"
    assert payload["host"] == "fedora.local"


def test_lode_wait_rejects_inside_lode(monkeypatch, capsys):
    monkeypatch.setenv("HOPPER_LID", "test-lode-123")

    rc = cmd_lode(["wait", "some-id"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "Cannot run this command inside lode test-lode-123." in out
    assert "hop backlog add" in out


def test_wait_summary_for_inside_lode_refusal(monkeypatch, capsys):
    monkeypatch.setenv("HOPPER_LID", "test-lode-123")

    result, _ = _run_scripted_cli_wait(monkeypatch, {}, {}, ["some-id"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.err == "hop wait: could not resolve target — exited 1\n"


def test_wait_summary_for_lode_wait_argument_error(monkeypatch, capsys):
    result, _ = _run_scripted_cli_wait(monkeypatch, {}, {}, [])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.err == "hop wait: could not resolve target — exited 1\n"


def test_wait_summary_for_wait_alias_argument_error(capsys):
    result = cmd_wait([])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.err == "hop wait: could not resolve target — exited 1\n"


def test_wait_summary_not_emitted_for_other_lode_argument_error(capsys):
    result = cmd_lode(["restart"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.err == ""


# Tests for config command


def test_config_help(capsys):
    """config --help shows help and returns 0."""
    result = cmd_config(["--help"])
    assert result == 0
    captured = capsys.readouterr()
    assert "usage: hop config" in captured.out
    assert "$variables" in captured.out


def test_config_list_empty(temp_config, capsys):
    """config with no args and no config shows dir and help message."""
    result = cmd_config([])
    assert result == 0
    captured = capsys.readouterr()
    assert f"config: {temp_config}" in captured.out
    assert "No config set" in captured.out


def test_config_list_values(temp_config, capsys):
    """config with no args lists simple values with dir header."""
    config_file = temp_config / "config.json"
    config_file.write_text('{"name": "jer", "org": "acme"}')

    result = cmd_config([])
    assert result == 0
    captured = capsys.readouterr()
    assert f"config: {temp_config}" in captured.out
    assert "name=jer" in captured.out
    assert "org=acme" in captured.out


def test_config_list_hides_complex_values(temp_config, capsys):
    """config listing filters out complex values like lists and dicts."""
    import json

    config_file = temp_config / "config.json"
    config_file.write_text(json.dumps({"name": "jer", "projects": [{"path": "/tmp", "name": "x"}]}))

    result = cmd_config([])
    assert result == 0
    captured = capsys.readouterr()
    assert "name=jer" in captured.out
    assert "projects" not in captured.out


def test_config_json(temp_config, capsys):
    """config json dumps full config including complex values."""
    import json

    config_file = temp_config / "config.json"
    data = {"name": "jer", "projects": [{"path": "/tmp", "name": "x"}]}
    config_file.write_text(json.dumps(data))

    result = cmd_config(["json"])
    assert result == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed == data


def test_config_path(temp_config, capsys):
    """config path prints the config directory."""
    result = cmd_config(["path"])
    assert result == 0
    captured = capsys.readouterr()
    assert str(temp_config) in captured.out


def test_config_delete(temp_config, capsys):
    """config delete removes a key."""
    import json

    config_file = temp_config / "config.json"
    config_file.write_text('{"name": "jer", "org": "acme"}')

    result = cmd_config(["delete", "org"])
    assert result == 0
    captured = capsys.readouterr()
    assert "Deleted 'org'" in captured.out

    saved = json.loads(config_file.read_text())
    assert saved == {"name": "jer"}


def test_config_delete_missing(temp_config, capsys):
    """config delete on missing key returns error."""
    config_file = temp_config / "config.json"
    original = b'{\n  "z-last": true,\n  "a-first": "keep"\n}\n'
    config_file.write_bytes(original)

    result = cmd_config(["delete", "nope"])
    assert result == 1
    assert config_file.read_bytes() == original
    assert capsys.readouterr().out.splitlines() == [
        "error: config deletion refused",
        "observed: config key 'nope' is not set",
        "Hopper did not change config.json.",
        "recover with: hop config list",
    ]


def test_config_delete_missing_key_arg(capsys):
    """config delete without a key shows error."""
    result = cmd_config(["delete"])
    assert result == 1
    captured = capsys.readouterr()
    assert "key required" in captured.out


def test_config_delete_complex_blocked(temp_config, capsys):
    """config delete refuses to delete complex values."""
    config_file = temp_config / "config.json"
    original = b'{\n  "projects": [ { "path": "/tmp", "name": "x" } ],\n  "keep": true\n}\n'
    config_file.write_bytes(original)

    result = cmd_config(["delete", "projects"])
    assert result == 1
    assert config_file.read_bytes() == original
    assert capsys.readouterr().out.splitlines() == [
        "error: config deletion refused",
        "observed: config key 'projects' contains a complex value",
        "Hopper did not change config.json.",
        "recover with: hop config json",
    ]


def test_config_get_existing(temp_config, capsys):
    """config get returns value when set."""
    config_file = temp_config / "config.json"
    config_file.write_text('{"name": "jer"}')

    result = cmd_config(["get", "name"])
    assert result == 0
    captured = capsys.readouterr()
    assert "jer" in captured.out


def test_config_get_missing(capsys):
    """config get returns error when not set."""
    result = cmd_config(["get", "name"])
    assert result == 1
    captured = capsys.readouterr()
    assert "Config 'name' not set" in captured.out


def test_config_get_missing_key_arg(capsys):
    """config get without a key shows error."""
    result = cmd_config(["get"])
    assert result == 1
    captured = capsys.readouterr()
    assert "key required" in captured.out


def test_config_set_value(temp_config, capsys):
    """config set stores a value."""
    config_file = temp_config / "config.json"

    result = cmd_config(["set", "name", "jer"])
    assert result == 0
    captured = capsys.readouterr()
    assert "name=jer" in captured.out

    # Verify file was written
    import json

    saved = json.loads(config_file.read_text())
    assert saved == {"name": "jer"}


@pytest.mark.parametrize(
    ("args", "recovery"),
    [
        (["set", "remote.journal", "host.example"], "hop remote set journal <host>"),
        (["delete", "remote.journal"], "hop remote rm journal"),
        (["set", "remote.", "host.example"], "hop remote set <project> <host>"),
        (["delete", "remote."], "hop remote rm <project>"),
    ],
)
def test_config_refuses_reserved_remote_keys_without_mutation(temp_config, capsys, args, recovery):
    path = temp_config / "config.json"
    original = b'{"name": "sol", "remote.journal": ["old.example"]}\n'
    path.write_bytes(original)

    assert cmd_config(args) == 1

    assert path.read_bytes() == original
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "error: config mutation refused",
        f"observed: '{args[1]}' is reserved for remote routing",
        "Hopper did not change config.json.",
        f"recover with: {recovery}",
    ]


@pytest.mark.parametrize("failure", ["write", "fsync", "replace"])
def test_migration_publication_failure_stops_predispatch_routing(
    temp_config, monkeypatch, capsys, failure
):
    path = temp_config / "config.json"
    original = b'{"name": "sol", "remote.journal": "host.example"}\n'
    path.write_bytes(original)

    def fail(*_args, **_kwargs):
        raise OSError(failure)

    if failure == "write":
        monkeypatch.setattr(config.json, "dumps", fail)
    elif failure == "fsync":
        monkeypatch.setattr(config.os, "fsync", fail)
    else:
        monkeypatch.setattr(config.os, "replace", fail)
    monkeypatch.setattr(sys, "argv", ["hop", "implement", "journal"])

    with patch("hopper.remote.run_remote") as remote_probe:
        assert main() == 2

    remote_probe.assert_not_called()
    assert path.read_bytes() == original
    lines = capsys.readouterr().err.strip().splitlines()
    assert len(lines) == 4
    assert lines[0] == "error: Hopper config is unavailable"
    assert str(path) in lines[1]
    assert "did not treat the config as empty" in lines[2]
    assert lines[3].startswith("recover with: `ls -ld ")


@pytest.mark.parametrize(
    "argv",
    [
        ["hop", "implement", "journal"],
        ["hop", "remote", "list"],
        ["hop", "lode", "list", "--all-hosts"],
    ],
    ids=["predispatch-project-routing", "remote-list", "all-hosts"],
)
def test_invalid_config_fails_unavailable_without_remote_contact(
    temp_config, monkeypatch, capsys, argv
):
    path = temp_config / "config.json"
    path.write_bytes(b'{"remote.journal": []}\n')
    monkeypatch.setattr(sys, "argv", argv)

    with (
        patch("hopper.cli.require_server", return_value=None),
        patch("hopper.client.list_lodes", return_value=[]),
        patch("hopper.remote.run_remote") as remote_probe,
    ):
        assert main() == 2

    remote_probe.assert_not_called()
    assert str(path) in capsys.readouterr().err


def test_wrong_shaped_projects_stop_pooled_routing_without_contact(
    temp_config, monkeypatch, capsys
):
    path = temp_config / "config.json"
    original = b'{"projects":[{"path":"/srv/journal","name":7}],"remote.journal":["a"]}\n'
    path.write_bytes(original)
    monkeypatch.setattr(sys, "argv", ["hop", "implement", "journal"])

    with patch("hopper.remote.run_remote") as remote_probe:
        assert main() == 2

    remote_probe.assert_not_called()
    assert path.read_bytes() == original
    assert str(path) in capsys.readouterr().err


@pytest.mark.parametrize(
    ("reason", "recovery"),
    [
        ("malformed", "python -m json.tool"),
        ("wrong_shape", "vi"),
        ("unreadable", "ls -ld"),
        ("locked", "fuser"),
    ],
)
def test_main_renders_every_config_error_reason_as_four_line_unavailable_refusal(
    temp_config, capsys, reason, recovery
):
    error = config.ConfigError(temp_config / "config.json", reason)

    with patch("hopper.cli._main", side_effect=error):
        assert main() == 2

    lines = capsys.readouterr().err.strip().splitlines()
    assert len(lines) == 4
    assert lines[0] == "error: Hopper config is unavailable"
    assert str(error.path) in lines[1]
    assert "did not treat the config as empty" in lines[2]
    assert lines[3].startswith(f"recover with: `{recovery} ")


def test_config_set_updates_existing(temp_config, capsys):
    """config set updates existing config."""
    config_file = temp_config / "config.json"
    config_file.write_text('{"name": "old", "other": "keep"}')

    result = cmd_config(["set", "name", "new"])
    assert result == 0

    import json

    saved = json.loads(config_file.read_text())
    assert saved == {"name": "new", "other": "keep"}


def test_config_set_missing_args(capsys):
    """config set without key and value shows error."""
    result = cmd_config(["set"])
    assert result == 1
    captured = capsys.readouterr()
    assert "key and value required" in captured.out


# Tests for require_projects


def test_require_projects_success(tmp_path, monkeypatch):
    """require_projects returns None when projects exist."""
    from hopper.cli import require_projects
    from hopper.projects import Project

    monkeypatch.setattr(
        "hopper.projects.get_active_projects",
        lambda: [Project(path="/path", name="proj")],
    )
    result = require_projects()
    assert result is None


def test_require_projects_failure(tmp_path, monkeypatch, capsys):
    """require_projects returns 1 when no projects."""
    from hopper.cli import require_projects

    monkeypatch.setattr("hopper.projects.get_active_projects", lambda: [])
    result = require_projects()
    assert result == 1
    captured = capsys.readouterr()
    assert "No projects configured" in captured.out
    assert "hop project add" in captured.out


# Tests for project command


def test_project_help(capsys):
    """project --help shows help and returns 0."""
    from hopper.cli import cmd_project

    result = cmd_project(["--help"])
    assert result == 0
    captured = capsys.readouterr()
    assert "usage: hop project" in captured.out
    assert "name, path, disabled, and disabled_reason" in " ".join(captured.out.split())


def test_remote_help_documents_pool_syntax_and_json_shape(capsys):
    assert cmd_remote(["--help"]) == 0

    out = " ".join(capsys.readouterr().out.split())
    assert "hop remote set <project> <host> [host ...]" in out
    assert "hosts for the complete pool" in out


def test_project_list_empty(tmp_path, monkeypatch, capsys):
    """project list shows message when no projects."""
    from hopper.cli import cmd_project

    monkeypatch.setattr("hopper.projects.load_projects", lambda: [])
    result = cmd_project(["list"])
    assert result == 0
    captured = capsys.readouterr()
    assert "No projects configured" in captured.out


def test_project_list_shows_projects(tmp_path, monkeypatch, capsys):
    """project list shows all projects."""
    from hopper.cli import cmd_project
    from hopper.projects import Project

    projects = [
        Project(path="/path/to/foo", name="foo"),
        Project(path="/path/to/bar", name="bar", disabled=True),
        Project(path="/path/to/baz", name="baz", disabled=True, disabled_reason="paused"),
    ]
    monkeypatch.setattr("hopper.projects.load_projects", lambda: projects)
    result = cmd_project(["list"])
    assert result == 0
    assert capsys.readouterr().out == (
        "foo\n"
        "  /path/to/foo\n"
        "bar (disabled)\n"
        "  /path/to/bar\n"
        "baz (disabled: paused)\n"
        "  /path/to/baz\n"
    )


@pytest.mark.parametrize("command", [cmd_project, cmd_projects], ids=["project-list", "projects"])
def test_project_list_json_exact_contract(monkeypatch, capsys, command):
    projects = [
        Project(
            path="/path/to/foo",
            name="foo",
            disabled=True,
            disabled_reason="maintenance",
            last_used_at=12345,
        )
    ]
    monkeypatch.setattr("hopper.projects.load_projects", lambda: projects)

    args = ["list", "--json"] if command is cmd_project else ["--json"]
    assert command(args) == 0

    assert json.loads(capsys.readouterr().out) == {
        "projects": [
            {
                "name": "foo",
                "path": "/path/to/foo",
                "disabled": True,
                "disabled_reason": "maintenance",
            }
        ]
    }


def test_project_list_json_empty_is_still_an_envelope(monkeypatch, capsys):
    monkeypatch.setattr("hopper.projects.load_projects", lambda: [])

    assert cmd_project(["list", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == {"projects": []}


def test_project_add_missing_path(capsys):
    """project add without path shows error."""
    from hopper.cli import cmd_project

    result = cmd_project(["add"])
    assert result == 1
    captured = capsys.readouterr()
    assert "path required" in captured.out


def test_project_remove_missing_name(capsys):
    """project remove without name shows error."""
    from hopper.cli import cmd_project

    result = cmd_project(["remove"])
    assert result == 1
    captured = capsys.readouterr()
    assert "name required" in captured.out


def test_project_remove_not_found(tmp_path, monkeypatch, capsys):
    """project remove with unknown name shows error."""
    from hopper.cli import cmd_project

    monkeypatch.setattr("hopper.projects.remove_project", lambda name: False)
    result = cmd_project(["remove", "unknown"])
    assert result == 1
    captured = capsys.readouterr()
    assert "not found" in captured.out


def test_project_disable_with_reason(capsys):
    """project disable stores reason and prints it."""
    from hopper.cli import cmd_project

    save_projects([Project(path="/path/to/P", name="P")])

    result = cmd_project(["disable", "P", "maintenance"])

    assert result == 0
    captured = capsys.readouterr()
    assert "Disabled project: P" in captured.out
    assert "  reason: maintenance" in captured.out
    project = load_projects()[0]
    assert project.disabled is True
    assert project.disabled_reason == "maintenance"


def test_project_disable_without_reason(capsys):
    """project disable stores empty reason when none is provided."""
    from hopper.cli import cmd_project

    save_projects([Project(path="/path/to/P", name="P")])

    result = cmd_project(["disable", "P"])

    assert result == 0
    captured = capsys.readouterr()
    assert "Disabled project: P" in captured.out
    assert "  reason:" not in captured.out
    project = load_projects()[0]
    assert project.disabled is True
    assert project.disabled_reason == ""


def test_project_disable_not_found(capsys):
    """project disable returns 1 when project is missing."""
    from hopper.cli import cmd_project

    result = cmd_project(["disable", "NOPE", "reason"])

    assert result == 1
    captured = capsys.readouterr()
    assert "Project not found: NOPE" in captured.out


def test_project_enable_clears_reason(capsys):
    """project enable clears disabled state and reason."""
    from hopper.cli import cmd_project

    save_projects(
        [Project(path="/path/to/P", name="P", disabled=True, disabled_reason="maintenance")]
    )

    result = cmd_project(["enable", "P"])

    assert result == 0
    captured = capsys.readouterr()
    assert "Enabled project: P" in captured.out
    project = load_projects()[0]
    assert project.disabled is False
    assert project.disabled_reason == ""


def test_project_enable_not_found(capsys):
    """project enable returns 1 when project is missing."""
    from hopper.cli import cmd_project

    result = cmd_project(["enable", "NOPE"])

    assert result == 1
    captured = capsys.readouterr()
    assert "Project not found: NOPE" in captured.out


def test_project_disable_unquoted_multiword_reason(capsys):
    """Unquoted reason tokens are joined with a single space."""
    from hopper.cli import cmd_project

    save_projects([Project(path="/path/to/P", name="P")])

    result = cmd_project(["disable", "P", "foo", "bar"])

    assert result == 0
    captured = capsys.readouterr()
    assert "  reason: foo bar" in captured.out
    assert load_projects()[0].disabled_reason == "foo bar"


def test_project_add_notifies_server(tmp_path, monkeypatch, capsys):
    """project add sends reload_projects to server."""
    from hopper.cli import cmd_project
    from hopper.projects import Project

    mock_project = Project(path="/path/to/repo", name="repo")
    monkeypatch.setattr("hopper.projects.add_project", lambda path: mock_project)
    calls = []
    monkeypatch.setattr("hopper.client.reload_projects", lambda sock: calls.append(sock) or True)
    result = cmd_project(["add", "/path/to/repo"])
    assert result == 0
    assert len(calls) == 1


def test_project_remove_notifies_server(tmp_path, monkeypatch, capsys):
    """project remove sends reload_projects to server."""
    from hopper.cli import cmd_project

    monkeypatch.setattr("hopper.projects.remove_project", lambda name: True)
    calls = []
    monkeypatch.setattr("hopper.client.reload_projects", lambda sock: calls.append(sock) or True)
    result = cmd_project(["remove", "myproj"])
    assert result == 0
    assert len(calls) == 1


def test_project_add_works_without_server(tmp_path, monkeypatch, capsys):
    """project add succeeds even if server notification fails."""
    from hopper.cli import cmd_project
    from hopper.projects import Project

    mock_project = Project(path="/path/to/repo", name="repo")
    monkeypatch.setattr("hopper.projects.add_project", lambda path: mock_project)
    monkeypatch.setattr(
        "hopper.client.reload_projects",
        lambda sock: (_ for _ in ()).throw(ConnectionRefusedError()),
    )
    result = cmd_project(["add", "/path/to/repo"])
    assert result == 0


def test_project_rename_success(tmp_path, monkeypatch, capsys):
    """project rename updates name and notifies server."""
    from hopper.cli import cmd_project

    monkeypatch.setattr("hopper.projects.rename_project", lambda cur, new: None)
    monkeypatch.setattr("hopper.projects.rename_project_in_data", lambda cur, new: None)
    calls = []
    monkeypatch.setattr("hopper.client.reload_projects", lambda sock: calls.append(sock) or True)
    result = cmd_project(["rename", "old-name", "new-name"])
    assert result == 0
    captured = capsys.readouterr()
    assert "old-name" in captured.out
    assert "new-name" in captured.out
    assert len(calls) == 1


def test_project_rename_missing_current(capsys):
    """project rename without current name shows error."""
    from hopper.cli import cmd_project

    result = cmd_project(["rename"])
    assert result == 1
    captured = capsys.readouterr()
    assert "current name required" in captured.out


def test_project_rename_missing_new(capsys):
    """project rename without new name shows error."""
    from hopper.cli import cmd_project

    result = cmd_project(["rename", "old-name"])
    assert result == 1
    captured = capsys.readouterr()
    assert "new name required" in captured.out


def test_project_rename_error(tmp_path, monkeypatch, capsys):
    """project rename shows error on ValueError."""
    from hopper.cli import cmd_project

    monkeypatch.setattr(
        "hopper.projects.rename_project",
        lambda cur, new: (_ for _ in ()).throw(ValueError("Project not found: old")),
    )
    result = cmd_project(["rename", "old", "new"])
    assert result == 1
    captured = capsys.readouterr()
    assert "not found" in captured.out


def test_project_rename_works_without_server(tmp_path, monkeypatch, capsys):
    """project rename succeeds even if server notification fails."""
    from hopper.cli import cmd_project

    monkeypatch.setattr("hopper.projects.rename_project", lambda cur, new: None)
    monkeypatch.setattr("hopper.projects.rename_project_in_data", lambda cur, new: None)
    monkeypatch.setattr(
        "hopper.client.reload_projects",
        lambda sock: (_ for _ in ()).throw(ConnectionRefusedError()),
    )
    result = cmd_project(["rename", "old", "new"])
    assert result == 0


def test_project_add_rejects_extra_arg(capsys):
    """project add with extra arg shows error."""
    from hopper.cli import cmd_project

    result = cmd_project(["add", "/some/path", "extra"])
    assert result == 1
    captured = capsys.readouterr()
    assert "unexpected argument" in captured.out


def test_project_rename_rejects_stray_fourth_arg(capsys):
    """project rename rejects a stray fourth arg after trailing reason parser change."""
    from hopper.cli import cmd_project

    result = cmd_project(["rename", "old", "new", "junk"])

    assert result == 1
    captured = capsys.readouterr()
    assert "unexpected argument: junk" in captured.out


# Tests for screenshot command


def test_screenshot_help(capsys):
    """screenshot --help shows help and returns 0."""
    result = cmd_screenshot(["--help"])
    assert result == 0
    captured = capsys.readouterr()
    assert "usage: hop screenshot" in captured.out


def test_screenshot_no_server(capsys):
    """screenshot returns 1 when server not running."""
    with patch("hopper.client.probe_server", return_value="down"):
        result = cmd_screenshot([])
    assert result == 1
    captured = capsys.readouterr()
    assert "Server not running" in captured.out


def test_screenshot_no_tmux_location(capsys):
    """screenshot returns 1 when server has no tmux location."""
    mock_response = {"type": "connected", "tmux": None}
    with patch("hopper.client.probe_server", return_value="up"):
        with patch("hopper.client.connect", return_value=mock_response):
            result = cmd_screenshot([])
    assert result == 1
    captured = capsys.readouterr()
    assert "not started inside tmux" in captured.out


def test_screenshot_capture_fails(capsys):
    """screenshot returns 1 when capture_pane fails."""
    mock_response = {"type": "connected", "tmux": {"lode": "main", "pane": "%0"}}
    with patch("hopper.client.probe_server", return_value="up"):
        with patch("hopper.client.connect", return_value=mock_response):
            with patch("hopper.tmux.capture_pane", return_value=None):
                result = cmd_screenshot([])
    assert result == 1
    captured = capsys.readouterr()
    assert "Failed to capture" in captured.out


def test_screenshot_success(capsys):
    """screenshot prints captured content on success."""
    mock_response = {"type": "connected", "tmux": {"lode": "main", "pane": "%0"}}
    ansi_content = "\x1b[32mGreen text\x1b[0m\nMore lines\n"
    with patch("hopper.client.probe_server", return_value="up"):
        with patch("hopper.client.connect", return_value=mock_response):
            with patch("hopper.tmux.capture_pane", return_value=ansi_content):
                result = cmd_screenshot([])
    assert result == 0
    captured = capsys.readouterr()
    assert captured.out == ansi_content


# Tests for processed command


def _processed_stdin(value: str):
    import io

    stdin = MagicMock()
    stdin.buffer = io.BytesIO(value.encode())
    return stdin


def _processed_ownership(lode_id: str, generation: str) -> dict:
    process = {
        "pid": 101,
        "ppid": 100,
        "pgid": 100,
        "birth": {
            "kind": "linux-proc-starttime",
            "boot_id": "boot-one",
            "value": "1010",
        },
    }
    return {
        "schema_version": 1,
        "lode_id": lode_id,
        "run_generation": generation,
        "registered_at_ms": 1_000,
        "boot_id": "boot-one",
        "platform": "linux",
        "proof_mode": "linux-degraded",
        "degraded_reason": "systemd scope unavailable",
        "pane": {"pane_id": "%1", "window_id": "@1", "root_process": process},
        "supervisor": process,
        "worker": process,
        "process_group": 100,
        "descendants": [],
        "unit": None,
        "cgroup": None,
        "unit_name": None,
    }


def test_processed_submits_exact_bytes_without_writing_canonical(temp_config, capsys):
    import io

    lode_id = "abcd2345"
    output = b"# Mill output\n\nExact bytes.\n"
    stdin = MagicMock()
    stdin.buffer = io.BytesIO(output)
    with (
        patch.dict(
            os.environ,
            {"HOPPER_LID": lode_id, "HOPPER_RUN_GENERATION": "a" * 32},
        ),
        patch("hopper.client.probe_server", return_value="up"),
        patch("hopper.client.lode_exists", return_value=True),
        patch(
            "hopper.client.get_lode",
            return_value={"id": lode_id, "stage": "mill"},
        ),
        patch(
            "hopper.client.complete_lode",
            return_value={"accepted": True, "reason": "accepted", "action_id": "b" * 32},
        ) as complete,
        patch("sys.stdin", stdin),
    ):
        assert cmd_processed([]) == 0

    complete.assert_called_once()
    args = complete.call_args.args
    assert len(args[1]) == 32
    assert args[2:5] == (lode_id, "a" * 32, "mill")
    assert base64.b64decode(args[5]) == output
    assert args[6] == len(output)
    assert args[7] == hashlib.sha256(output).hexdigest()
    assert not (temp_config / "lodes" / lode_id / "mill_out.md").exists()
    assert "Accepted mill output" in capsys.readouterr().out


def test_processed_server_refusal_leaves_existing_canonical_unchanged(temp_config, capsys):
    import io

    lode_id = "test-session"
    canonical = temp_config / "lodes" / lode_id / "mill_out.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"known good\n")
    stdin = MagicMock()
    stdin.buffer = io.BytesIO(b"new output\n")
    with (
        patch.dict(
            os.environ,
            {"HOPPER_LID": lode_id, "HOPPER_RUN_GENERATION": "a" * 32},
        ),
        patch("hopper.client.probe_server", return_value="up"),
        patch("hopper.client.lode_exists", return_value=True),
        patch(
            "hopper.client.get_lode",
            return_value={"id": lode_id, "stage": "mill"},
        ),
        patch(
            "hopper.client.complete_lode",
            return_value={"accepted": False, "reason": "ownership_unavailable"},
        ),
        patch("sys.stdin", stdin),
    ):
        assert cmd_processed([]) == 1

    assert canonical.read_bytes() == b"known good\n"
    assert "hop lode restart" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("reason", "detail", "guidance"),
    [
        ("output_staging_unavailable", "disk full", "Free space or fix permissions"),
        (
            "completion_persistence_unavailable",
            "read-only filesystem",
            "could not durably record ownership",
        ),
    ],
)
def test_processed_preparation_refusal_names_phase_and_remedy(
    temp_config, capsys, reason, detail, guidance
):
    import io

    lode_id = "test-session"
    stdin = MagicMock()
    stdin.buffer = io.BytesIO(b"new output\n")
    with (
        patch.dict(
            os.environ,
            {"HOPPER_LID": lode_id, "HOPPER_RUN_GENERATION": "a" * 32},
        ),
        patch("hopper.client.probe_server", return_value="up"),
        patch("hopper.client.lode_exists", return_value=True),
        patch("hopper.client.get_lode", return_value={"id": lode_id, "stage": "mill"}),
        patch(
            "hopper.client.complete_lode",
            return_value={"accepted": False, "reason": reason, "detail": detail},
        ),
        patch("sys.stdin", stdin),
    ):
        assert cmd_processed([]) == 1

    error = capsys.readouterr().err
    assert guidance in error
    assert detail in error
    assert "retry `hop processed`" in error


def test_processed_pidfd_capability_refusal_is_prescriptive(temp_config, capsys):
    import io

    lode_id = "test-session"
    canonical = temp_config / "lodes" / lode_id / "mill_out.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"known good\n")
    stdin = MagicMock()
    stdin.buffer = io.BytesIO(b"new output\n")
    with (
        patch.dict(
            os.environ,
            {"HOPPER_LID": lode_id, "HOPPER_RUN_GENERATION": "a" * 32},
        ),
        patch("hopper.client.probe_server", return_value="up"),
        patch("hopper.client.lode_exists", return_value=True),
        patch(
            "hopper.client.get_lode",
            return_value={"id": lode_id, "stage": "mill"},
        ),
        patch(
            "hopper.client.complete_lode",
            return_value={"accepted": False, "reason": "pidfd_unavailable"},
        ),
        patch("sys.stdin", stdin),
    ):
        assert cmd_processed([]) == 1

    assert canonical.read_bytes() == b"known good\n"
    error = capsys.readouterr().err
    assert "pidfd_open and pidfd_send_signal" in error
    assert "libc" in error
    assert "retry `hop processed`" in error


def test_processed_help(capsys):
    """processed --help shows help and returns 0."""
    result = cmd_processed(["--help"])
    assert result == 0
    captured = capsys.readouterr()
    assert "usage: hop processed" in captured.out
    help_text = " ".join(captured.out.split())
    assert "durable server acceptance" in help_text
    assert "owns staged bytes before acknowledging" in help_text
    assert "re-proved by the server after containment" in help_text


def test_processed_no_server(capsys):
    """processed returns 1 when server not running."""
    with patch("hopper.client.probe_server", return_value="down"):
        result = cmd_processed([])
    assert result == 1
    captured = capsys.readouterr()
    assert "Server not running" in captured.out


def test_processed_no_hopper_lid(capsys):
    """processed returns 1 when HOPPER_LID not set."""
    env = os.environ.copy()
    env.pop("HOPPER_LID", None)
    with patch.dict(os.environ, env, clear=True):
        with patch("hopper.client.probe_server", return_value="up"):
            result = cmd_processed([])
    assert result == 1
    captured = capsys.readouterr()
    assert "HOPPER_LID not set" in captured.out


def test_processed_invalid_session(capsys):
    """processed returns 1 when session doesn't exist."""
    with patch.dict(os.environ, {"HOPPER_LID": "bad-session"}):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.client.lode_exists", return_value=False):
                result = cmd_processed([])
    assert result == 1
    captured = capsys.readouterr()
    assert "bad-session" in captured.out
    assert "not found or archived" in captured.out


def test_processed_empty_stdin(capsys):
    """processed returns 1 on empty stdin."""
    lode_data = {"id": "test-session", "stage": "mill"}
    with patch.dict(os.environ, {"HOPPER_LID": "test-session"}):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.get_lode", return_value=lode_data):
                    with patch("sys.stdin", _processed_stdin("")):
                        result = cmd_processed([])
    assert result == 1
    captured = capsys.readouterr()
    assert "No input received" in captured.out


def test_processed_saves_file(temp_config, capsys):
    """The moved contract submits bytes without writing canonical output."""
    lode_id = "test-session-1234"
    lode_dir = temp_config / "lodes" / lode_id
    output_text = "# Mill output\n\nDo the thing.\n"
    lode_data = {"id": lode_id, "stage": "mill"}

    with patch.dict(os.environ, {"HOPPER_LID": lode_id}):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.get_lode", return_value=lode_data):
                    with patch(
                        "hopper.client.complete_lode",
                        return_value={
                            "accepted": True,
                            "reason": "accepted",
                            "action_id": "b" * 32,
                        },
                    ) as complete:
                        with patch("sys.stdin", _processed_stdin(output_text)):
                            result = cmd_processed([])

    assert result == 0
    captured = capsys.readouterr()
    assert "Accepted mill output for durable teardown" in captured.out
    output_path = lode_dir / "mill_out.md"
    assert not output_path.exists()
    complete.assert_called_once()
    assert base64.b64decode(complete.call_args.args[5]) == output_text.encode()


def test_processed_no_stage(capsys):
    """processed returns 1 when lode has no stage."""
    lode_data = {"id": "test-session", "stage": ""}
    with patch.dict(os.environ, {"HOPPER_LID": "test-session"}):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.get_lode", return_value=lode_data):
                    result = cmd_processed([])
    assert result == 1
    captured = capsys.readouterr()
    assert "no stage" in captured.out


def test_processed_refine_stage(temp_config, capsys):
    """Refine output also crosses the durable completion boundary."""
    lode_id = "test-refine-1234"
    lode_dir = temp_config / "lodes" / lode_id
    output_text = "# Refine summary\n\nFeature implemented.\n"
    lode_data = {"id": lode_id, "stage": "refine"}

    with patch.dict(os.environ, {"HOPPER_LID": lode_id}):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.get_lode", return_value=lode_data):
                    with patch(
                        "hopper.client.complete_lode",
                        return_value={
                            "accepted": True,
                            "reason": "accepted",
                            "action_id": "b" * 32,
                        },
                    ) as complete:
                        with patch("sys.stdin", _processed_stdin(output_text)):
                            result = cmd_processed([])

    assert result == 0

    output_path = lode_dir / "refine_out.md"
    assert not output_path.exists()
    complete.assert_called_once()
    assert complete.call_args.args[4] == "refine"
    assert base64.b64decode(complete.call_args.args[5]) == output_text.encode()


@pytest.mark.parametrize("stage", ["mill", "refine"])
def test_processed_non_ship_stage_never_runs_landing_proof(temp_config, stage):
    lode_id = f"test-{stage}-proof"
    with (
        patch.dict(os.environ, {"HOPPER_LID": lode_id}),
        patch("hopper.client.probe_server", return_value="up"),
        patch("hopper.client.lode_exists", return_value=True),
        patch(
            "hopper.client.get_lode",
            return_value={"id": lode_id, "stage": stage},
        ),
        patch(
            "hopper.client.complete_lode",
            return_value={"accepted": True, "reason": "accepted", "action_id": "b" * 32},
        ) as complete,
        patch("hopper.git.ship_landing_verdict") as landing_proof,
        patch("sys.stdin", _processed_stdin("stage output\n")),
    ):
        assert cmd_processed([]) == 0

    landing_proof.assert_not_called()
    complete.assert_called_once()
    assert not (temp_config / "lodes" / lode_id / f"{stage}_out.md").exists()


@pytest.mark.parametrize(
    "server_ack",
    [
        {"accepted": True, "reason": "accepted", "action_id": "b" * 32},
        {"accepted": True, "reason": "already_accepted", "action_id": "b" * 32},
    ],
)
def test_processed_ship_accepts_proven_landing_without_extra_claims(
    temp_config, capsys, monkeypatch, server_ack
):
    lode_id = "test-ship-proven"
    canonical = temp_config / "canonical-worktree"
    canonical.mkdir()
    unrelated_cwd = temp_config / "unrelated-cwd"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    with (
        patch.dict(os.environ, {"HOPPER_LID": lode_id}),
        patch("hopper.client.probe_server", return_value="up"),
        patch("hopper.client.lode_exists", return_value=True),
        patch(
            "hopper.client.get_lode",
            return_value={"id": lode_id, "stage": "ship"},
        ),
        patch("hopper.git.ship_landing_verdict") as landing_proof,
        patch(
            "hopper.client.complete_lode",
            return_value=server_ack,
        ) as complete,
        patch("sys.stdin", _processed_stdin("ship output\n")),
    ):
        assert cmd_processed([]) == 0

    landing_proof.assert_not_called()
    complete.assert_called_once()
    assert not (temp_config / "lodes" / lode_id / "ship_out.md").exists()
    captured = capsys.readouterr()
    assert "push" not in captured.out
    assert captured.err == ""


def test_processed_ship_refusal_writes_nothing_and_sends_no_mutation(temp_config, capsys):
    """A server refusal is authoritative; the CLI never writes accepted bytes."""
    lode_id = "test-ship-refused"
    canonical = temp_config / "canonical-worktree"
    canonical.mkdir()
    sentinel = canonical / "sentinel.txt"
    sentinel.write_text("keep\n")
    with (
        patch.dict(os.environ, {"HOPPER_LID": lode_id}),
        patch("hopper.client.probe_server", return_value="up"),
        patch("hopper.client.lode_exists", return_value=True),
        patch(
            "hopper.client.get_lode",
            return_value={"id": lode_id, "stage": "ship"},
        ),
        patch("hopper.git.ship_landing_verdict") as landing_proof,
        patch(
            "hopper.client.complete_lode",
            return_value={"accepted": False, "reason": "ship_provenance_unavailable"},
        ) as complete,
        patch("sys.stdin", _processed_stdin("ship output\n")),
    ):
        assert cmd_processed([]) == 1

    landing_proof.assert_not_called()
    complete.assert_called_once()
    assert not (temp_config / "lodes" / lode_id / "ship_out.md").exists()
    assert sentinel.read_text() == "keep\n"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "could not capture the ship repository identity" in captured.err


def test_processed_no_ack_is_unknown_and_keeps_canonical_unchanged(temp_config, capsys):
    lode_id = "test-session"
    output = "completed output\n"
    with (
        patch.dict(os.environ, {"HOPPER_LID": lode_id}),
        patch("hopper.client.probe_server", return_value="up"),
        patch("hopper.client.lode_exists", return_value=True),
        patch("hopper.client.get_lode", return_value={"id": lode_id, "stage": "mill"}),
        patch("hopper.client.complete_lode", return_value=None),
        patch("sys.stdin", _processed_stdin(output)),
    ):
        assert cmd_processed([]) == 1

    assert not (temp_config / "lodes" / lode_id / "mill_out.md").exists()
    captured = capsys.readouterr()
    assert "disposition is UNKNOWN" in captured.err
    assert f"hop lode status {lode_id}" in captured.err


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        ("lode_not_found", "Lode test-session not found or archived."),
        (
            "missing_expected_generation",
            "Completion was refused because this command has no runner generation. "
            "Run it inside the current lode runner, then retry.",
        ),
        (
            "stale_expected_generation",
            "Completion was refused because this runner generation is stale. "
            "Check `hop lode status test-session` and use the current runner.",
        ),
        (
            "terminal_failure",
            "Completion was refused because this lode has a terminal failure. "
            "Check `hop lode status test-session` before recovering it.",
        ),
    ],
)
def test_processed_each_explicit_refusal_is_distinct_and_nonzero(
    temp_config, capsys, reason, message
):
    lode_id = "test-session"
    output = "completed output\n"
    with (
        patch.dict(os.environ, {"HOPPER_LID": lode_id}),
        patch("hopper.client.probe_server", return_value="up"),
        patch("hopper.client.lode_exists", return_value=True),
        patch("hopper.client.get_lode", return_value={"id": lode_id, "stage": "mill"}),
        patch(
            "hopper.client.complete_lode",
            return_value={"accepted": False, "reason": reason},
        ),
        patch("sys.stdin", _processed_stdin(output)),
    ):
        assert cmd_processed([]) == 1

    assert not (temp_config / "lodes" / lode_id / "mill_out.md").exists()
    assert capsys.readouterr().err.strip() == message


def test_processed_acknowledges_over_real_server_socket(
    check_server, make_lode, temp_config, monkeypatch, capsys
):
    server, socket_path = check_server
    generation = "a" * 32
    lode_id = "abcd2345"
    lode = make_lode(
        id=lode_id,
        state="running",
        active=True,
        tmux_pane="%1",
        pid=101,
        run_generation=generation,
    )
    server.lodes = [lode]
    owner = MagicMock()
    server.lode_clients[lode_id] = owner
    server.client_lodes[owner] = lode_id
    server.client_generations[owner] = generation
    actions.write_run_ownership(_processed_ownership(lode_id, generation))
    monkeypatch.setattr(server, "_schedule_action_step", MagicMock())
    monkeypatch.setattr(config, "server_socket_path", lambda: socket_path)
    monkeypatch.setenv("HOPPER_LID", lode_id)
    monkeypatch.setenv("HOPPER_RUN_GENERATION", generation)
    monkeypatch.setattr(sys, "stdin", _processed_stdin("real boundary output\n"))

    assert cmd_processed([]) == 0

    assert server.lodes[0]["state"] == "teardown"
    assert actions.load_pending_action(lode_id) is not None
    assert not (temp_config / "lodes" / lode_id / "mill_out.md").exists()
    captured = capsys.readouterr()
    assert "UNKNOWN" not in captured.err


# Tests for gate command


def test_gate_help(capsys):
    """gate --help shows help and returns 0."""
    result = cmd_gate(["--help"])
    assert result == 0
    captured = capsys.readouterr()
    assert "usage: hop gate" in captured.out


def test_gate_show_prints_verbatim_format(capsys):
    """gate show prints the expected header, contents, and response hint."""
    gate_data = {
        "lode": {"id": "gate1234", "stage": "refine", "state": "gated"},
        "gate": "# Design Review\nLooks good",
    }
    with patch("hopper.cli.require_server", return_value=None):
        with patch(
            "hopper.client.read_lode_snapshot",
            return_value=("found", gate_data["lode"]),
        ):
            with patch("hopper.client.get_gate", return_value=gate_data):
                result = cmd_gate(["show", "gate1234"])
    assert result == 0
    assert (
        capsys.readouterr().out == "Lode: gate1234\n"
        "Stage: refine\n"
        "State: gated\n\n"
        "--- gate.md ---\n"
        "# Design Review\n"
        "Looks good\n"
        "---\n\n"
        'Respond with: hop gate feedback gate1234 "<your response>"\n'
    )


def test_gate_show_reports_when_no_gate_is_set(capsys):
    """gate show distinguishes a missing gate from an unavailable lode."""
    resolution = {
        "outcome": "found",
        "host": "local",
        "canonical_id": "gate1234",
        "lode": {"id": "gate1234"},
        "error": None,
        "probe_summary": [],
        "exit_code": 0,
    }
    with (
        patch("hopper.cli._resolve_lode", return_value=resolution),
        patch("hopper.client.get_gate", return_value=None),
    ):
        assert cmd_gate(["show", "gate1234"]) == 1

    assert capsys.readouterr().out.splitlines() == [
        "error: gate display refused",
        "observed: no gate is set for lode gate1234",
        "Hopper did not display gate feedback or change the lode.",
        "recover with: hop lode status gate1234",
    ]


def test_gate_feedback_with_text_arg_calls_client(capsys):
    """gate feedback sends inline feedback text to the client helper."""
    response = {"type": "feedback_sent", "lode_id": "gate1234", "tmux_pane": "%9"}
    with patch("hopper.cli.require_server", return_value=None):
        with patch(
            "hopper.client.read_lode_snapshot",
            return_value=("found", {"id": "gate1234"}),
        ):
            with patch("hopper.client.send_gate_feedback", return_value=response) as mock_send:
                result = cmd_gate(["feedback", "gate1234", "Needs work"])
    assert result == 0
    mock_send.assert_called_once()
    assert mock_send.call_args.args[1:] == ("gate1234", "Needs work")
    assert "Feedback sent to gate1234 (pane %9)" in capsys.readouterr().out


def test_gate_feedback_reads_stdin_when_no_text_arg(capsys):
    """gate feedback falls back to stdin when text is omitted."""
    from io import StringIO

    response = {"type": "feedback_sent", "lode_id": "gate1234", "tmux_pane": "%9"}
    with patch("hopper.cli.require_server", return_value=None):
        with patch(
            "hopper.client.read_lode_snapshot",
            return_value=("found", {"id": "gate1234"}),
        ):
            with patch("hopper.client.send_gate_feedback", return_value=response) as mock_send:
                with patch("sys.stdin", StringIO("Needs more tests")):
                    result = cmd_gate(["feedback", "gate1234"])
    assert result == 0
    mock_send.assert_called_once()
    assert mock_send.call_args.args[1:] == ("gate1234", "Needs more tests")


def test_gate_feedback_treats_dash_as_stdin_sentinel(capsys):
    """gate feedback reads stdin when the text arg is a dash sentinel."""
    from io import StringIO

    response = {"type": "feedback_sent", "lode_id": "gate1234", "tmux_pane": "%9"}
    with patch("hopper.cli.require_server", return_value=None):
        with patch(
            "hopper.client.read_lode_snapshot",
            return_value=("found", {"id": "gate1234"}),
        ):
            with patch("hopper.client.send_gate_feedback", return_value=response) as mock_send:
                with patch("sys.stdin", StringIO("approved, ship it")):
                    result = cmd_gate(["feedback", "gate1234", "-"])
    assert result == 0
    mock_send.assert_called_once()
    assert mock_send.call_args.args[1:] == ("gate1234", "approved, ship it")


def test_feedback_alias_dispatches_to_gate_feedback():
    """feedback alias delegates directly to gate feedback handler."""
    with patch("hopper.cli._cmd_gate_feedback", return_value=0) as mock_feedback:
        assert cmd_feedback(["gate1234", "Looks good"]) == 0
    mock_feedback.assert_called_once_with(["gate1234", "Looks good"])


def test_feedback_alias_treats_dash_as_stdin_sentinel(capsys):
    """feedback alias reads stdin when the text arg is a dash sentinel."""
    from io import StringIO

    response = {"type": "feedback_sent", "lode_id": "gate1234", "tmux_pane": "%9"}
    with patch(
        "hopper.cli._resolve_lode",
        return_value=_watch_resolution({"id": "gate1234"}),
    ):
        with patch("hopper.client.send_gate_feedback", return_value=response) as mock_send:
            with patch("sys.stdin", StringIO("approved, ship it")):
                result = cmd_feedback(["gate1234", "-"])
    assert result == 0
    mock_send.assert_called_once()
    assert mock_send.call_args.args[1:] == ("gate1234", "approved, ship it")


def test_gate_feedback_resident_route_bypasses_local_mutation():
    remote = {"id": "gate1234", "host": "remote.example"}
    with (
        patch(
            "hopper.cli._resolve_lode",
            return_value=_watch_resolution(remote, "remote.example"),
        ),
        patch("hopper.client.send_gate_feedback") as local_feedback,
        patch("hopper.cli._run_remote_cli", return_value=0) as mock_remote,
    ):
        assert cmd_gate(["feedback", "gate1234", "Needs work"]) == 0

    mock_remote.assert_called_once_with(
        "remote.example",
        ["gate", "feedback", "gate1234", "-"],
        reason="lode gate1234",
        stdin_text="Needs work",
    )
    local_feedback.assert_not_called()


@pytest.mark.parametrize("outcome", ["pane_unavailable", "busy", "not_sent", "unverified"])
def test_gate_feedback_local_delivery_failure_never_probes_remote(outcome, capsys):
    response = {"type": "error", "outcome": outcome, "error": "Safe local recovery."}
    with (
        patch(
            "hopper.cli._resolve_lode",
            return_value=_watch_resolution({"id": "gate1234"}),
        ),
        patch("hopper.client.send_gate_feedback", return_value=response),
    ):
        assert cmd_gate(["feedback", "gate1234", "Needs work"]) == 1

    assert capsys.readouterr().err == "Safe local recovery.\n"


def test_gate_feedback_unverified_prints_framed_pane_tail(capsys):
    response = {
        "type": "error",
        "outcome": "unverified",
        "error": "Feedback outcome unknown; inspect pane.",
        "tail": "first pane line\nlast pane line",
    }
    with (
        patch(
            "hopper.cli._resolve_lode",
            return_value=_watch_resolution({"id": "gate1234"}),
        ),
        patch("hopper.client.send_gate_feedback", return_value=response),
    ):
        assert cmd_gate(["feedback", "gate1234", "Needs work"]) == 1

    assert capsys.readouterr().err == (
        "Feedback outcome unknown; inspect pane.\n"
        "--- pane tail ---\n"
        "first pane line\n"
        "last pane line\n"
        "--- end pane tail ---\n"
    )


def test_gate_feedback_not_sent_does_not_print_pane_tail(capsys):
    response = {
        "type": "error",
        "outcome": "not_sent",
        "error": "Feedback was not submitted; retry safely.",
        "tail": "staged pane content",
    }
    with (
        patch(
            "hopper.cli._resolve_lode",
            return_value=_watch_resolution({"id": "gate1234"}),
        ),
        patch("hopper.client.send_gate_feedback", return_value=response),
    ):
        assert cmd_gate(["feedback", "gate1234", "Needs work"]) == 1

    assert capsys.readouterr().err == "Feedback was not submitted; retry safely.\n"


def test_gate_feedback_missing_response_never_probes_remote(capsys):
    with (
        patch(
            "hopper.cli._resolve_lode",
            return_value=_watch_resolution({"id": "gate1234"}),
        ) as resolver,
        patch("hopper.client.send_gate_feedback", return_value=None),
    ):
        assert cmd_gate(["feedback", "gate1234", "Needs work"]) == 1

    resolver.assert_called_once()
    error = capsys.readouterr().err
    assert "delivery outcome is unknown" in error
    assert "hop lode peek gate1234" in error


def test_gate_no_server(capsys):
    """gate returns error when server is not running."""
    with patch("hopper.client.probe_server", return_value="down"):
        with patch.dict(os.environ, {"HOPPER_LID": "test-session"}):
            result = cmd_gate([])
    assert result != 0


def test_gate_no_hopper_lid(capsys):
    """gate returns 1 when HOPPER_LID not set."""
    env = os.environ.copy()
    env.pop("HOPPER_LID", None)
    with patch.dict(os.environ, env, clear=True):
        with patch("hopper.cli.require_server", return_value=None):
            result = cmd_gate([])
    assert result == 1
    captured = capsys.readouterr()
    assert "HOPPER_LID not set" in captured.out


def test_gate_wrong_stage(capsys):
    """gate returns 1 when lode is in the mill stage."""
    lode_data = {"id": "test-session", "stage": "mill"}
    with patch.dict(os.environ, {"HOPPER_LID": "test-session"}):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.get_lode", return_value=lode_data):
                    result = cmd_gate([])
    assert result == 1
    captured = capsys.readouterr()
    assert "Lode test-session is not in refine or ship stage." in captured.out


def test_gate_empty_stdin(capsys):
    """gate returns 1 when stdin is empty."""
    from io import StringIO

    lode_data = {"id": "test-session", "stage": "refine"}
    with patch.dict(os.environ, {"HOPPER_LID": "test-session"}):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.get_lode", return_value=lode_data):
                    with patch("sys.stdin", StringIO("")):
                        result = cmd_gate([])
    assert result == 1
    captured = capsys.readouterr()
    assert "No input received" in captured.out


def test_gate_saves_file_and_sets_state(temp_config, capsys):
    """gate saves gate.md and sets lode state to gated."""
    from io import StringIO

    lode_id = "test-gate-1234"
    review_text = "# Design Review\n\nLooks good.\n"
    lode_data = {"id": lode_id, "stage": "refine"}

    with patch.dict(os.environ, {"HOPPER_LID": lode_id}):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.get_lode", return_value=lode_data):
                    with patch("hopper.client.set_lode_state", return_value=True) as mock_set:
                        with patch("sys.stdin", StringIO(review_text)):
                            result = cmd_gate([])

    assert result == 0
    captured = capsys.readouterr()
    assert "Gate set" in captured.out

    # Verify file was written as gate.md
    lode_dir = temp_config / "lodes" / lode_id
    gate_path = lode_dir / "gate.md"
    assert gate_path.exists()
    assert gate_path.read_text() == review_text

    # Verify state was set to gated
    mock_set.assert_called_once()
    _, sid, state, status = mock_set.call_args[0]
    assert sid == lode_id
    assert state == "gated"
    assert status == "Gate"


def test_gate_ship_stage_saves_file_and_sets_state(temp_config, capsys):
    """gate saves gate.md and gates a ship-stage lode."""
    from io import StringIO

    lode_id = "test-ship-gate-1234"
    review_text = "# Ship Blocked\n\nPush rejected.\n"
    lode_data = {"id": lode_id, "stage": "ship"}

    with patch.dict(os.environ, {"HOPPER_LID": lode_id}):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.get_lode", return_value=lode_data):
                    with patch("hopper.client.set_lode_state", return_value=True) as mock_set:
                        with patch("sys.stdin", StringIO(review_text)):
                            result = cmd_gate([])

    assert result == 0
    captured = capsys.readouterr()
    assert "Gate set" in captured.out

    lode_dir = temp_config / "lodes" / lode_id
    gate_path = lode_dir / "gate.md"
    assert gate_path.exists()
    assert gate_path.read_text() == review_text

    mock_set.assert_called_once()
    _, sid, state, status = mock_set.call_args[0]
    assert sid == lode_id
    assert state == "gated"
    assert status == "Gate"


# Tests for code command


def test_code_help(capsys):
    """code --help shows help and returns 0."""
    result = cmd_code(["--help"])
    assert result == 0
    captured = capsys.readouterr()
    assert "usage: hop code" in captured.out
    assert "stage" in captured.out


def test_code_missing_args(capsys):
    """code requires stage name argument."""
    result = cmd_code([])
    assert result == 1
    captured = capsys.readouterr()
    assert "error:" in captured.out


def test_code_requires_hopper_lid(capsys):
    """code returns 1 when HOPPER_LID not set."""
    env = os.environ.copy()
    env.pop("HOPPER_LID", None)
    with patch.dict(os.environ, env, clear=True):
        with patch("hopper.cli.require_server", return_value=None):
            result = cmd_code(["audit"])
    assert result == 1
    captured = capsys.readouterr()
    assert "HOPPER_LID not set" in captured.out


def test_code_validates_hopper_lid(capsys):
    """code validates HOPPER_LID exists on server."""
    with patch.dict(os.environ, {"HOPPER_LID": "bad-session"}):
        with patch("hopper.cli.require_server", return_value=None):
            with patch("hopper.client.lode_exists", return_value=False):
                result = cmd_code(["audit"])
    assert result == 1
    captured = capsys.readouterr()
    assert "not found or archived" in captured.out


def test_code_requires_stdin(capsys):
    """code returns 1 when no stdin provided."""
    from io import StringIO

    with patch.dict(os.environ, {"HOPPER_LID": "test-1234"}):
        with patch("hopper.cli.require_server", return_value=None):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("sys.stdin", StringIO("")):
                    result = cmd_code(["audit"])
    assert result == 1
    captured = capsys.readouterr()
    assert "No directions provided" in captured.out


def test_code_dispatches_to_run_code(capsys):
    """code dispatches to run_code on valid input."""
    from io import StringIO

    with patch.dict(os.environ, {"HOPPER_LID": "test-1234"}):
        with patch("hopper.cli.require_server", return_value=None):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("sys.stdin", StringIO("my directions")):
                    with patch("hopper.code.run_code", return_value=0) as mock_run:
                        result = cmd_code(["audit"])
    assert result == 0
    mock_run.assert_called_once()
    args = mock_run.call_args[0]
    assert args[0] == "test-1234"  # lode_id from env
    assert args[2] == "audit"  # stage_name
    assert args[3] == "my directions"  # request from stdin


# Tests for CLI aliases


def test_status_outside_lode_detail(capsys):
    """hop status <lode-id> outside a lode shows detailed lode info."""
    lode = {
        "id": "abc12345",
        "stage": "refine",
        "project": "myproj",
        "title": "My Title",
        "status": "Working",
        "state": "running",
        "scope": "Fix login",
        "branch": "hopper-abc12345-fix-login",
        "active": True,
        "created_at": 1000,
        "updated_at": 2000,
    }
    with (
        patch("hopper.cli.require_server") as require,
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
    ):
        result = cmd_status(["abc12345"])
    require.assert_not_called()
    assert result == 0
    out = capsys.readouterr().out
    assert "abc12345" in out
    assert "project:  myproj" in out
    assert "stage:    refine" in out
    assert "scope:    Fix login" in out
    assert "active:   yes" in out


def test_status_outside_lode_not_found(capsys):
    """hop status <lode-id> outside a lode errors when lode not found."""
    with (
        patch("hopper.cli.require_server") as require,
        patch("hopper.client.read_lode_snapshot", return_value=("absent", None)),
    ):
        result = cmd_status(["bad_id"])
    require.assert_not_called()
    assert result == 1
    captured = capsys.readouterr()
    assert captured.out == (
        "Observed: lode 'bad_id' was not found. Hopper did not route or mutate a lode. "
        "Recover with: hop lode list --all-hosts --json. Probes: local=absent.\n"
    )
    assert captured.err == ""


def test_status_outside_lode_title_rejected(capsys):
    """hop status -t outside a lode is rejected."""
    with patch("hopper.cli.require_server", return_value=None):
        result = cmd_status(["-t", "newtitle", "abc12345"])
    assert result == 1
    out = capsys.readouterr().out
    assert "Cannot set title from outside a lode" in out


def test_status_outside_lode_bare(capsys):
    """hop status bare (no args, no HOPPER_LID) shows HOPPER_LID error."""
    with patch("hopper.cli.require_server", return_value=None):
        result = cmd_status([])
    assert result == 1
    out = capsys.readouterr().out
    assert "HOPPER_LID not set" in out


def test_status_outside_lode_too_many_args(capsys):
    """hop status <id> <extra> outside a lode errors."""
    with patch("hopper.cli.require_server", return_value=None):
        result = cmd_status(["abc12345", "extra"])
    assert result == 1
    out = capsys.readouterr().out
    assert "Too many arguments" in out


def test_status_inside_lode_unchanged(capsys):
    """hop status inside a lode (with HOPPER_LID) still works."""
    lode = {"id": "test123", "title": "Title", "status": "Working"}
    with patch.dict(os.environ, {"HOPPER_LID": "test123"}):
        with patch("hopper.client.probe_server", return_value="up"):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.get_lode", return_value=lode):
                    result = cmd_status([])
    assert result == 0
    out = capsys.readouterr().out
    assert "Title: Title" in out
    assert "Working" in out


def test_implement_help_shows_implement(capsys):
    """hop implement --help shows 'hop implement' in usage."""
    result = cmd_implement(["--help"])
    assert result == 0
    out = capsys.readouterr().out
    assert "hop implement" in out
    assert "hop lode" not in out


def test_submit_help_shows_submit(capsys):
    """hop submit --help shows 'hop submit' in usage."""
    result = cmd_submit(["--help"])
    assert result == 0
    out = capsys.readouterr().out
    assert "hop submit" in out


def test_submit_delegates_to_lode_create(capsys):
    """hop submit delegates to hop lode create."""
    from io import StringIO

    created_lode = {"id": "abc12345", "project": "myproj", "stage": "mill"}
    project = Project(path="/fake/repo", name="myproj")
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.projects.find_project", return_value=project):
            with patch("hopper.git.dirty_status", return_value=""):
                with patch("hopper.client.create_lode", return_value=created_lode):
                    with patch("sys.stdin", StringIO(LONG_SCOPE)):
                        assert cmd_submit(["myproj"]) == 0
    out = capsys.readouterr().out
    assert "abc12345" in out


def test_list_delegates_to_lode_list(capsys):
    """hop list delegates to hop lode list."""
    lodes = [
        {
            "id": "abc123",
            "stage": "mill",
            "state": "running",
            "active": True,
            "project": "p",
            "title": "t",
            "status": "s",
        }
    ]
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.list_lodes", return_value=lodes):
            assert cmd_list([]) == 0
    out = capsys.readouterr().out
    assert "abc123" in out


def test_list_help_shows_list(capsys):
    """hop list --help shows 'hop list' in usage."""
    result = cmd_list(["--help"])
    assert result == 0
    out = capsys.readouterr().out
    assert "hop list" in out
    assert "--project" in out
    assert "every pool host" in out
    assert "unavailable_hosts" in out
    assert "exit 2" in out


def test_list_archived_flag(capsys):
    """hop list -a forwards archived flag."""
    lodes = [
        {
            "id": "old001",
            "stage": "shipped",
            "state": "shipped",
            "active": False,
            "project": "p",
            "title": "t",
            "status": "s",
            "updated_at": 100,
        }
    ]
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.list_archived_lodes", return_value=lodes):
            assert cmd_list(["-a"]) == 0
    out = capsys.readouterr().out
    assert "old001" in out


def test_projects_delegates_to_project_list(capsys):
    """hop projects delegates to hop project list."""
    from hopper.projects import Project

    projects = [Project(path="/path/to/foo", name="foo")]
    with patch("hopper.projects.load_projects", return_value=projects):
        assert cmd_projects([]) == 0
    out = capsys.readouterr().out
    assert "foo" in out


def test_projects_help_shows_projects(capsys):
    """hop projects --help shows 'hop projects' in usage."""
    result = cmd_projects(["--help"])
    assert result == 0
    out = capsys.readouterr().out
    assert "hop projects" in out


def test_wait_help_shows_wait(capsys):
    """hop wait --help shows 'hop wait' in usage."""
    result = cmd_wait(["--help"])
    assert result == 0
    out = capsys.readouterr().out
    assert "hop wait" in out
    assert "--timeout" in out
    assert "--observer-timeout" in out
    assert "Seconds without a valid status observation" in out
    assert "failing (0=disabled)" in out
    assert "Status reconciliation interval seconds" in out


def test_lode_wait_help_shows_observer_timeout(capsys):
    assert cmd_lode(["wait", "--help"]) == 0
    out = capsys.readouterr().out
    assert "--observer-timeout" in out
    assert "Seconds without a valid status observation" in out
    assert "Status reconciliation interval seconds" in out


def test_show_help_shows_show(capsys):
    """hop show --help shows 'hop show' in usage."""
    result = cmd_show(["--help"])
    assert result == 0
    out = capsys.readouterr().out
    assert "hop show" in out


def test_watch_help_shows_watch(capsys):
    """hop watch --help shows 'hop watch' in usage."""
    result = cmd_watch(["--help"])
    assert result == 0
    out = capsys.readouterr().out
    assert "hop watch" in out


def test_restart_help_shows_restart(capsys):
    """hop restart --help shows 'hop restart' in usage."""
    result = cmd_restart(["--help"])
    assert result == 0
    out = capsys.readouterr().out
    assert "hop restart" in out


def test_show_delegates_to_lode_show(capsys):
    """hop show delegates to hop lode show."""
    lode = {
        "id": "abc123",
        "stage": "mill",
        "state": "running",
        "active": True,
        "project": "p",
        "title": "t",
        "status": "s",
    }
    with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
        result = cmd_show(["abc123"])
    assert result == 0
    out = capsys.readouterr().out
    assert "abc123" in out


def test_show_alias_absent_has_exact_message(capsys):
    with patch("hopper.client.read_lode_snapshot", return_value=("absent", None)):
        result = cmd_show(["missing"])

    assert result == 1
    captured = capsys.readouterr()
    assert captured.out == (
        "Observed: lode 'missing' was not found. Hopper did not route or mutate a lode. "
        "Recover with: hop lode list --all-hosts --json. Probes: local=absent.\n"
    )
    assert captured.err == ""


def test_show_alias_unavailable_has_exact_message(capsys):
    with (
        patch(
            "hopper.client.read_lode_snapshot",
            return_value=("unavailable", "server not running at /tmp/server.sock"),
        ),
    ):
        result = cmd_show(["missing"])

    assert result == 2
    captured = capsys.readouterr()
    assert captured.out == (
        "Observed: lode status for 'missing' is unavailable because local could not be "
        "probed. Hopper did not treat the lode as absent or route the command. "
        "Recover with: hop lode list --json. "
        "Probes: local=unavailable (server not running at /tmp/server.sock).\n"
    )
    assert captured.err == ""


def test_restart_delegates_to_lode_restart(capsys):
    """hop restart delegates to hop lode restart."""
    lode = {
        "id": "abc123",
        "stage": "mill",
        "state": "idle",
        "active": False,
        "project": "p",
        "title": "t",
        "status": "s",
    }
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.cli.require_not_inside_lode", return_value=None):
            with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
                with patch(
                    "hopper.client.restart_lode",
                    return_value={
                        "type": "lode_action_ack",
                        "outcome": "completed",
                        "disposition": "replacement_spawned",
                    },
                ):
                    assert cmd_restart(["abc123"]) == 0
    out = capsys.readouterr().out
    assert "Restarted" in out
    assert "abc123" in out


def test_wait_delegates_to_lode_wait(capsys):
    """hop wait delegates to hop lode wait."""
    lode = {
        "id": "abc123",
        "stage": "shipped",
        "state": "shipped",
        "active": False,
        "project": "p",
        "title": "t",
        "status": "s",
    }
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.cli.require_not_inside_lode", return_value=None):
            with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
                result = cmd_wait(["abc123"])
    assert result == 0
    out = capsys.readouterr().out
    assert "✓ abc123 shipped (t)" in out


def test_lode_ls_alias(capsys):
    """hop lode ls works like hop lode list."""
    lodes = [
        {
            "id": "abc123",
            "stage": "mill",
            "state": "running",
            "active": True,
            "project": "p",
            "title": "t",
            "status": "s",
        }
    ]
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.list_lodes", return_value=lodes):
            assert cmd_lode(["ls"]) == 0
    out = capsys.readouterr().out
    assert "abc123" in out


def test_lode_status_subcommand(capsys):
    """hop lode status <id> shows detailed lode info."""
    lode = {
        "id": "abc12345",
        "stage": "mill",
        "project": "proj",
        "title": "Title",
        "status": "Working",
        "state": "running",
        "active": True,
        "created_at": 1000,
        "updated_at": 2000,
    }
    with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
        result = cmd_lode(["status", "abc12345"])
    assert result == 0
    out = capsys.readouterr().out
    assert "abc12345" in out
    assert "stage:    mill" in out


def test_lode_status_surfaces_exact_accepted_output_recovery(capsys):
    lode = {
        "id": "abcd2345",
        "stage": "mill",
        "project": "proj",
        "status": "Teardown blocked",
        "state": "teardown",
        "active": False,
        "created_at": 1000,
        "updated_at": 2000,
    }
    summary = {
        "stage": "mill",
        "action_id": "a" * 32,
        "sha256": "b" * 64,
        "byte_length": 17,
        "repair_token": "T" * 43,
        "failure": "staged blob missing",
        "command": f"hop lode repair-output abcd2345 - --token {'T' * 43}",
    }
    with (
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch("hopper.actions.load_pending_action", return_value={"pending": True}),
        patch("hopper.actions.pending_output_recovery", return_value=summary),
    ):
        assert cmd_lode(["status", "abcd2345"]) == 0

    output = capsys.readouterr().out
    for expected in (
        "stage:   mill",
        "action:  " + "a" * 32,
        "sha256:  " + "b" * 64,
        "bytes:   17",
        "token:   " + "T" * 43,
        "failure: staged blob missing",
        summary["command"],
    ):
        assert expected in output


def test_lode_status_uses_one_snapshot_exchange(capsys, make_lode):
    lode = make_lode(id="abc12345", active=True)
    exchanges = []

    def exchange(socket_path, message, timeout=2.0, wait_for_response=False):
        exchanges.append(("_exchange_message", socket_path, message, timeout, wait_for_response))
        return {"type": "lode_snapshot", "result": "found", "lode": lode}

    with (
        patch("hopper.client._exchange_message", side_effect=exchange) as snapshot_exchange,
        patch("hopper.client.socket.socket") as socket_constructor,
        patch("hopper.cli._remote_hosts") as remote_hosts,
    ):
        assert cmd_lode(["status", "abc12345"]) == 0

    assert len(exchanges) == 1
    transport, _path, message, timeout, wait_for_response = exchanges[0]
    assert transport == "_exchange_message"
    assert message == {"type": "lode_snapshot", "prefix": "abc12345"}
    assert timeout == 2.0
    assert wait_for_response is True
    snapshot_exchange.assert_called_once()
    socket_constructor.assert_not_called()
    remote_hosts.assert_not_called()
    assert "abc12345" in capsys.readouterr().out


def test_lode_status_error_emphasized(capsys):
    lode = {
        "id": "abc123",
        "stage": "mill",
        "project": "proj",
        "title": "Title",
        "status": "Something failed",
        "state": "error",
        "active": True,
        "created_at": 1000,
        "updated_at": 2000,
    }
    with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
        result = cmd_lode(["status", "abc123"])
    assert result == 0
    out = capsys.readouterr().out
    assert "error state" in out
    assert "hop lode restart abc123" in out


def test_lode_show_detail(capsys):
    """hop lode show <id> prints multiline lode detail."""
    lode = {
        "id": "abc12345",
        "stage": "refine",
        "project": "proj",
        "title": "Title",
        "status": "Working",
        "state": "running",
        "scope": "Fix login bug",
        "branch": "hopper-abc12345-fix-login",
        "active": True,
        "created_at": 1000,
        "updated_at": 2000,
    }
    with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
        result = cmd_lode(["show", "abc12345"])
    assert result == 0
    out = capsys.readouterr().out
    assert "abc12345" in out
    assert "project:  proj" in out
    assert "scope:    Fix login bug" in out
    assert "branch:   hopper-abc12345-fix-login" in out


def test_lode_show_prefix(capsys):
    """hop lode show resolves lode ID prefix."""
    lode = {
        "id": "abc12345",
        "stage": "refine",
        "project": "proj",
        "state": "running",
        "active": True,
        "created_at": 1000,
        "updated_at": 2000,
    }
    with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
        result = cmd_lode(["show", "abc"])
    assert result == 0
    out = capsys.readouterr().out
    assert "abc12345" in out


def test_lode_show_archived(capsys):
    """hop lode show finds lodes in archived data."""
    lode = {
        "id": "arc12345",
        "stage": "shipped",
        "project": "proj",
        "state": "ready",
        "active": False,
        "created_at": 1000,
        "updated_at": 2000,
    }
    with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
        result = cmd_lode(["show", "arc"])
    assert result == 0
    out = capsys.readouterr().out
    assert "arc12345" in out
    assert "stage:    shipped" in out


def test_lode_show_ambiguous_prefix(capsys):
    """hop lode show reports all matching IDs when prefix is ambiguous."""
    lodes = [
        {"id": "abc12345", "stage": "mill", "project": "proj"},
        {"id": "abc99999", "stage": "refine", "project": "proj"},
    ]
    with patch(
        "hopper.client.read_lode_snapshot",
        return_value=("ambiguous", [lode["id"] for lode in lodes]),
    ):
        result = cmd_lode(["show", "abc"])
    assert result == 1
    captured = capsys.readouterr()
    assert captured.out == (
        "Ambiguous lode prefix 'abc'. Matches: local:abc12345, local:abc99999. "
        "Probes: local=ambiguous (abc12345, abc99999). Hopper did not choose a lode "
        "or route the command. Recover with: hop lode status abc12345 --json.\n"
    )
    assert captured.err == ""


def test_lode_show_not_found(capsys):
    """hop lode show reports not found for unknown IDs/prefixes."""
    with patch("hopper.client.read_lode_snapshot", return_value=("absent", None)):
        result = cmd_lode(["show", "bad_id"])
    assert result == 1
    captured = capsys.readouterr()
    assert captured.out == (
        "Observed: lode 'bad_id' was not found. Hopper did not route or mutate a lode. "
        "Recover with: hop lode list --all-hosts --json. Probes: local=absent.\n"
    )
    assert captured.err == ""


def test_lode_show_subcommand(capsys):
    """Backward-compat coverage: hop lode show <id> still succeeds."""
    lode = {
        "id": "abc12345",
        "stage": "refine",
        "project": "proj",
        "state": "running",
        "active": True,
        "created_at": 1000,
        "updated_at": 2000,
    }
    with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
        result = cmd_lode(["show", "abc12345"])
    assert result == 0
    out = capsys.readouterr().out
    assert "abc12345" in out
    assert "stage:    refine" in out


def test_lode_status_not_found(capsys):
    """hop lode status <id> errors when not found."""
    with (
        patch("hopper.client.read_lode_snapshot", return_value=("absent", None)),
        patch("hopper.cli._remote_hosts", return_value=["fedora.local"]),
        patch("hopper.remote.load_lode_cache", return_value={}),
        patch("hopper.cli._remote_lode_status", return_value=(None, "absent")),
    ):
        result = cmd_lode(["status", "bad_id"])
    assert result == 1
    captured = capsys.readouterr()
    assert captured.out == (
        "Observed: lode 'bad_id' was not found. Hopper did not route or mutate a lode. "
        "Recover with: hop lode list --all-hosts --json. "
        "Probes: local=absent; fedora.local=absent.\n"
    )
    assert captured.err == ""


def test_lode_status_remote_unreadable_has_distinct_exit(capsys):
    """A busy/unreachable host is not reported as a dead lode."""
    with patch(
        "hopper.client.read_lode_snapshot",
        return_value=("unavailable", "server did not respond within 2s"),
    ):
        with (
            patch("hopper.cli._remote_hosts", return_value=["fedora.local"]),
            patch("hopper.remote.load_lode_cache", return_value={}),
            patch("hopper.cli._remote_lode_status", return_value=(None, "unreadable")),
        ):
            result = cmd_lode(["status", "busy-id"])

    assert result == 2
    captured = capsys.readouterr()
    assert captured.out == (
        "Observed: lode status for 'busy-id' is unavailable because fedora.local, local "
        "could not be probed. Hopper did not treat the lode as absent or route the command. "
        "Recover with: hop -H fedora.local lode list --json. "
        "Probes: local=unavailable (server did not respond within 2s); "
        "fedora.local=unreadable.\n"
    )
    assert captured.err == ""


def test_lode_status_local_unavailable_remote_absent_exits_2(capsys):
    with (
        patch(
            "hopper.client.read_lode_snapshot",
            return_value=("unavailable", "server not running at /tmp/server.sock"),
        ),
        patch("hopper.cli._remote_hosts", return_value=["fedora.local"]),
        patch("hopper.remote.load_lode_cache", return_value={}),
        patch("hopper.cli._remote_lode_status", return_value=(None, "absent")),
    ):
        result = cmd_lode(["status", "abc"])

    assert result == 2
    captured = capsys.readouterr()
    assert captured.out == (
        "Observed: lode status for 'abc' is unavailable because local could not be probed. "
        "Hopper did not treat the lode as absent or route the command. "
        "Recover with: hop lode list --json. "
        "Probes: local=unavailable (server not running at /tmp/server.sock); "
        "fedora.local=absent.\n"
    )
    assert captured.err == ""


def test_outside_status_local_unavailable_prints_honest_error(capsys):
    with (
        patch("hopper.cli.require_server") as require,
        patch(
            "hopper.client.read_lode_snapshot",
            return_value=("unavailable", "server not running at /tmp/server.sock"),
        ),
    ):
        result = cmd_status(["abc"])

    assert result == 2
    require.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == (
        "Observed: lode status for 'abc' is unavailable because local could not be probed. "
        "Hopper did not treat the lode as absent or route the command. "
        "Recover with: hop lode list --json. "
        "Probes: local=unavailable (server not running at /tmp/server.sock).\n"
    )
    assert captured.err == ""


def test_remote_lode_probe_classifies_timeout_as_unreadable():
    from hopper.cli import _remote_lode_status

    with patch(
        "hopper.remote.run_remote",
        side_effect=subprocess.TimeoutExpired(["ssh"], timeout=5),
    ):
        lode, state = _remote_lode_status("fedora.local", "busy-id")

    assert lode is None
    assert state == "unreadable"


@pytest.mark.parametrize("stdout", ["{", "[]", "{}"])
def test_remote_lode_probe_classifies_malformed_output_as_unreadable(stdout):
    from hopper.cli import _remote_lode_status

    result = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
    with patch("hopper.remote.run_remote", return_value=result):
        lode, state = _remote_lode_status("fedora.local", "busy-id")

    assert lode is None
    assert state == "unreadable"


@pytest.mark.parametrize(
    "lode_id",
    [1, "abc12345", "bcd23456"],
    ids=["non-string", "non-canonical", "prefix-mismatch"],
)
def test_remote_lode_probe_rejects_invalid_or_mismatched_success_id(lode_id):
    from hopper.cli import _remote_lode_status

    result = subprocess.CompletedProcess(
        [], 0, stdout=json.dumps({"id": lode_id, "project": "journal"}), stderr=""
    )
    with patch("hopper.remote.run_remote", return_value=result):
        lode, state = _remote_lode_status("fedora.local", "abc")

    assert lode is None
    assert state == "unreadable"


def test_remote_lode_probe_accepts_only_fully_typed_snapshot():
    from hopper.cli import _remote_lode_status

    lode = {
        "id": "abc23456",
        "project": "journal",
        "stage": "refine",
        "state": "running",
        "status": "working",
        "active": True,
    }
    result = subprocess.CompletedProcess([], 0, stdout=json.dumps(lode), stderr="")
    with patch("hopper.remote.run_remote", return_value=result):
        observed, state = _remote_lode_status("fedora.local", "abc")

    assert state == "found"
    assert observed == {**lode, "host": "fedora.local"}


@pytest.mark.parametrize(
    ("returncode", "failure", "expected"),
    [
        (1, {"outcome": "absent", "query": "abc", "error": "not found"}, "absent"),
        (2, {"outcome": "unavailable", "query": "abc", "error": "failed"}, "unreadable"),
    ],
)
def test_remote_lode_probe_requires_structured_nonzero_outcome(returncode, failure, expected):
    from hopper.cli import _remote_lode_status

    result = subprocess.CompletedProcess([], returncode, stdout="", stderr=json.dumps(failure))
    with patch("hopper.remote.run_remote", return_value=result):
        lode, state = _remote_lode_status("fedora.local", "abc")

    assert lode is None
    assert state == expected


def test_remote_lode_probe_preserves_structured_ambiguity_ids():
    from hopper.cli import _remote_lode_status

    result = subprocess.CompletedProcess(
        [],
        1,
        stdout="",
        stderr=json.dumps(
            {
                "outcome": "ambiguous",
                "query": "abc",
                "error": "ambiguous",
                "matches": ["abc22222", "abc33333"],
            }
        ),
    )
    with patch("hopper.remote.run_remote", return_value=result):
        lode, state = _remote_lode_status("fedora.local", "abc")

    assert lode is None
    assert state == "ambiguous"
    assert state.matches == ("abc22222", "abc33333")


def test_remote_lode_probe_treats_unstructured_exit_one_as_unreadable():
    from hopper.cli import _remote_lode_status

    result = subprocess.CompletedProcess(
        [],
        1,
        stdout="",
        stderr="generic remote failure",
    )
    with patch("hopper.remote.run_remote", return_value=result):
        lode, state = _remote_lode_status("fedora.local", "abc")

    assert lode is None
    assert state == "unreadable"
    assert state.matches == ()


def test_backlog_ls_alias(capsys):
    """hop backlog ls works like hop backlog list."""
    from hopper.backlog import BacklogItem

    items = [BacklogItem(id="abc123", project="proj", description="Do thing", created_at=1000)]
    with patch("hopper.backlog.load_backlog", return_value=items):
        assert cmd_backlog(["ls"]) == 0
    out = capsys.readouterr().out
    assert "abc123" in out
    assert "Do thing" in out


def test_backlog_ls_with_flags(capsys):
    """hop backlog list -p filters by project."""
    from hopper.backlog import BacklogItem

    items = [
        BacklogItem(id="abc123", project="proj", description="Do thing", created_at=1000),
        BacklogItem(
            id="def456",
            project="other",
            description="Other thing",
            created_at=2000,
        ),
    ]
    with patch("hopper.backlog.load_backlog", return_value=items):
        assert cmd_backlog(["list", "-p", "proj"]) == 0
    out = capsys.readouterr().out
    assert "abc123" in out
    assert "def456" not in out


def test_backlog_ls_project_not_found(capsys):
    """hop backlog list -p nonexistent prints specific message and exits 0."""
    from hopper.backlog import BacklogItem

    items = [BacklogItem(id="abc123", project="proj", description="Do thing", created_at=1000)]
    with patch("hopper.backlog.load_backlog", return_value=items):
        assert cmd_backlog(["list", "-p", "noexist"]) == 0
    out = capsys.readouterr().out
    assert "No backlog items for project: noexist" in out


def test_help_shows_aliases_group(capsys):
    """hop --help shows the Aliases group."""
    with patch.object(sys, "argv", ["hopper", "--help"]):
        result = main()
    assert result == 0
    out = capsys.readouterr().out
    assert "Aliases:" in out
    assert "list" in out
    assert "submit" in out
    assert "projects" in out
    assert "wait" in out
    assert "show" in out
    assert "watch" in out
    assert "restart" in out


def test_format_lode_line_basic():
    """format_lode_line returns expected format."""
    lode = {
        "id": "abc12345",
        "stage": "mill",
        "state": "running",
        "project": "myproj",
        "title": "My Title",
        "status": "Working",
    }
    line = format_lode_line(lode)
    assert "abc12345" in line
    assert "mill" in line
    assert "myproj" in line
    assert "My Title" in line
    assert "Working" in line


def test_format_lode_line_shows_spawn_refusal():
    lode = {
        "id": "abc12345",
        "stage": "refine",
        "state": "running",
        "status": "spawn refused: tmux unreachable — verify tmux is running, then retry",
    }

    assert "spawn refused: tmux unreachable" in format_lode_line(lode)


@pytest.mark.parametrize("failure_kind", ["oom", "runner_exit_unverified"])
def test_terminal_failure_renders_without_generic_retry(make_lode, failure_kind):
    status = format_terminal_failure_status(failure_kind, "test-id")
    lode = make_lode(
        id="test-id",
        state="error",
        status=status,
        failure_kind=failure_kind,
    )

    assert format_lode_line(lode).endswith(status)
    detail = format_lode_detail(lode)
    assert status in detail
    assert "to retry:" not in detail


def test_terminal_failure_json_is_unchanged(capsys, make_lode):
    status = format_terminal_failure_status("oom", "test-id")
    lode = make_lode(id="test-id", state="error", status=status, failure_kind="oom")

    with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
        assert cmd_lode(["status", "test-id", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["failure_kind"] == "oom"
    assert payload["status"] == status


def test_format_lode_detail_corrects_parked_gone_status(make_lode):
    reason = "no pane output"
    branch = "hopper-test-id"
    lode = make_lode(
        id="test-id",
        state="gated",
        status=format_park_status(reason, "test-id"),
        branch=branch,
        active=True,
        tmux_pane="%41",
    )
    expected = PARK_PANE_GONE_STATUS.format(
        reason=reason,
        lode_id="test-id",
        branch=branch,
    )

    with patch("hopper.lodes.pane_liveness", return_value=Liveness.GONE) as mock_liveness:
        output = format_lode_detail(lode)

    assert output.count(expected) == 2
    assert f"  status:   {expected}" in output
    assert mock_liveness.call_count == 2


def test_format_lode_line_corrects_parked_gone_status(make_lode):
    reason = "no pane output"
    branch = "hopper-test-id"
    lode = make_lode(
        id="test-id",
        state="gated",
        status=format_park_status(reason, "test-id"),
        branch=branch,
        active=True,
        tmux_pane="%42",
    )
    expected = PARK_PANE_GONE_STATUS.format(
        reason=reason,
        lode_id="test-id",
        branch=branch,
    )

    with patch("hopper.lodes.pane_liveness", return_value=Liveness.GONE) as mock_liveness:
        output = format_lode_line(lode)

    assert output.endswith(expected)
    mock_liveness.assert_called_once_with("%42")


def test_lode_list_all_hosts_probes_stamped_local_parked_lode(capsys, make_lode):
    reason = "no pane output"
    branch = "hopper-test-id"
    lode = make_lode(
        id="test-id",
        state="gated",
        status=format_park_status(reason, "test-id"),
        branch=branch,
        active=True,
        tmux_pane="%43",
    )
    expected = PARK_PANE_GONE_STATUS.format(
        reason=reason,
        lode_id="test-id",
        branch=branch,
    )

    with (
        patch("hopper.client.probe_server", return_value="up"),
        patch("hopper.client.read_lodes", return_value=[lode]),
        patch("hopper.remote.remote_registry", return_value={}),
        patch("hopper.lodes.pane_liveness", return_value=Liveness.GONE) as mock_liveness,
    ):
        assert cmd_lode(["list", "--all-hosts"]) == 0

    output = capsys.readouterr().out
    assert "local" in output
    assert expected in output
    mock_liveness.assert_called_once_with("%43")


def test_lode_list_all_hosts_json_overwrites_remote_annotations_without_probe(capsys, make_lode):
    stored = format_park_status("quiet", "abc23456")
    remote_lode = make_lode(
        id="abc23456",
        state="gated",
        status=stored,
        branch="hopper-abc23456",
        tmux_pane="%remote",
        status_display="remote-computed correction",
        pane_liveness="gone",
    )
    remote_result = subprocess.CompletedProcess(
        [],
        0,
        stdout=json.dumps({"lodes": [remote_lode]}),
        stderr="",
    )

    with (
        patch("hopper.client.probe_server", return_value="up"),
        patch("hopper.client.read_lodes", return_value=[]),
        patch("hopper.remote.remote_registry", return_value={"proj": ["builder.example"]}),
        patch("hopper.remote.run_remote", return_value=remote_result),
        patch(
            "hopper.lodes.pane_liveness",
            side_effect=AssertionError("pane_liveness must not be called"),
        ),
    ):
        assert cmd_lode(["list", "--all-hosts", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)["lodes"][0]
    assert payload["host"] == "builder.example"
    assert payload["status"] == stored
    assert payload["status_display"] == stored
    assert payload["pane_liveness"] == "not_probed"


def test_lode_list_all_hosts_uses_unique_union_of_pool_members(capsys):
    remote_result = subprocess.CompletedProcess(
        [],
        0,
        stdout='{"lodes": []}\n',
        stderr="",
    )
    with (
        patch("hopper.client.probe_server", return_value="up"),
        patch("hopper.client.read_lodes", return_value=[]),
        patch(
            "hopper.remote.remote_registry",
            return_value={
                "one": ["shared.example", "one.example"],
                "two": ["two.example", "shared.example"],
            },
        ),
        patch("hopper.remote.run_remote", return_value=remote_result) as remote,
    ):
        assert cmd_lode(["list", "--all-hosts", "--json"]) == 0

    assert sorted(call.args[0] for call in remote.call_args_list) == [
        "one.example",
        "shared.example",
        "two.example",
    ]
    assert json.loads(capsys.readouterr().out) == {
        "lodes": [],
        "unavailable_hosts": [],
    }


def test_lode_list_all_hosts_marks_local_read_failure_unavailable(capsys):
    with (
        patch("hopper.client.probe_server", return_value="up"),
        patch("hopper.client.read_lodes", return_value=None),
        patch("hopper.remote.remote_registry", return_value={}),
    ):
        assert cmd_lode(["list", "--all-hosts", "--json"]) == 2

    assert json.loads(capsys.readouterr().out) == {
        "lodes": [],
        "unavailable_hosts": [
            {
                "host": "local",
                "reason": (
                    "lode listing exited 1: server lode listing could not be read after "
                    "the server answered; retry with: hop lode list --json"
                ),
            }
        ],
    }


def test_remote_hosts_is_unique_first_seen_pool_union():
    from hopper.cli import _remote_hosts

    with patch(
        "hopper.remote.remote_registry",
        return_value={
            "one": ["shared.example", "one.example"],
            "two": ["two.example", "shared.example"],
        },
    ):
        assert _remote_hosts() == ["shared.example", "one.example", "two.example"]


def test_lode_list_all_hosts_json_preserves_rows_and_reports_partial_sources(
    capsys,
    make_lode,
):
    proven = make_lode(id="abc23456", project="project")

    def run_remote(host, args, *, timeout):
        assert args == ["lode", "list", "--json"]
        assert timeout == REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC
        if host == "timeout.example":
            raise subprocess.TimeoutExpired(["ssh", host], timeout)
        if host == "failed.example":
            return subprocess.CompletedProcess([], 9, stdout="", stderr="server failed")
        if host == "malformed.example":
            return subprocess.CompletedProcess([], 0, stdout="{", stderr="")
        return subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps({"lodes": [proven]}),
            stderr="",
        )

    with (
        patch("hopper.client.probe_server", return_value="down"),
        patch(
            "hopper.remote.remote_registry",
            return_value={
                "project": [
                    "ready.example",
                    "timeout.example",
                    "failed.example",
                    "malformed.example",
                ]
            },
        ),
        patch("hopper.remote.run_remote", side_effect=run_remote),
    ):
        assert cmd_lode(["list", "--all-hosts", "--json"]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert [(lode["id"], lode["host"]) for lode in payload["lodes"]] == [
        ("abc23456", "ready.example")
    ]
    assert payload["unavailable_hosts"] == [
        {
            "host": "local",
            "reason": "lode listing exited 1: server not running; start it with: hop up",
        },
        {"host": "timeout.example", "reason": "lode listing timed out"},
        {"host": "failed.example", "reason": "lode listing exited 9: server failed"},
        {"host": "malformed.example", "reason": "lode listing returned malformed JSON"},
    ]


def test_lode_list_all_hosts_human_reports_partial_sources(capsys, make_lode):
    proven = make_lode(id="abc23456", project="project")

    def run_remote(host, _args, *, timeout):
        assert timeout == REMOTE_CANDIDATE_PROBE_TIMEOUT_SEC
        if host == "down.example":
            raise OSError("network down")
        return subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps({"lodes": [proven]}),
            stderr="",
        )

    with (
        patch("hopper.client.probe_server", return_value="unresponsive"),
        patch(
            "hopper.remote.remote_registry",
            return_value={"project": ["ready.example", "down.example"]},
        ),
        patch("hopper.remote.run_remote", side_effect=run_remote),
    ):
        assert cmd_lode(["list", "--all-hosts"]) == 2

    captured = capsys.readouterr()
    assert "abc23456" in captured.out
    assert "ready.example" in captured.out
    assert (
        "Unavailable host local: lode listing exited 1: server did not answer within "
        f"{hopper_cli.LOCAL_DISCOVERY_PROBE_TIMEOUT_SEC:g}s" in captured.err
    )
    assert (
        "Unavailable host down.example: lode listing transport failed: network down" in captured.err
    )


def test_rendering_parked_gone_does_not_mutate_memory_or_files(temp_config, make_lode):
    reason = "no pane output"
    lode = make_lode(
        id="test-id",
        state="gated",
        status=format_park_status(reason, "test-id"),
        branch="hopper-test-id",
        active=True,
        tmux_pane="%44",
    )
    save_lodes([lode])
    recovery_path = get_lode_dir("test-id") / "recovery.json"
    recovery_path.parent.mkdir(parents=True)
    recovery_path.write_text(json.dumps({"state": "gated", "reason": reason}) + "\n")
    active_path = temp_config / "active.jsonl"
    before_lode = copy.deepcopy(lode)
    before_active = active_path.read_bytes()
    before_recovery = recovery_path.read_bytes()

    with patch("hopper.lodes.pane_liveness", return_value=Liveness.GONE):
        format_lode_detail(lode)

    assert lode == before_lode
    assert active_path.read_bytes() == before_active
    assert recovery_path.read_bytes() == before_recovery


def test_lode_status_json_does_not_mutate_memory_or_files(temp_config, capsys, make_lode):
    reason = "no pane output"
    lode = make_lode(
        id="test-id",
        state="gated",
        status=format_park_status(reason, "test-id"),
        branch="hopper-test-id",
        active=True,
        tmux_pane="%45",
    )
    save_lodes([lode])
    recovery_path = get_lode_dir("test-id") / "recovery.json"
    recovery_path.parent.mkdir(parents=True)
    recovery_path.write_text(json.dumps({"state": "gated", "reason": reason}) + "\n")
    active_path = temp_config / "active.jsonl"
    before_lode = copy.deepcopy(lode)
    before_active = active_path.read_bytes()
    before_recovery = recovery_path.read_bytes()

    with (
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch("hopper.lodes.pane_liveness", return_value=Liveness.GONE),
    ):
        assert cmd_lode(["status", "test-id", "--json"]) == 0

    capsys.readouterr()
    assert lode == before_lode
    assert active_path.read_bytes() == before_active
    assert recovery_path.read_bytes() == before_recovery


@pytest.mark.parametrize("subcommand", ["status", "show"])
def test_lode_status_json_reports_parked_gone_status(capsys, make_lode, subcommand):
    stored = format_park_status("no pane output", "test-id")
    branch = "hopper-test-id"
    lode = make_lode(
        id="test-id",
        state="gated",
        status=stored,
        branch=branch,
        active=True,
        tmux_pane="%46",
    )
    expected = PARK_PANE_GONE_STATUS.format(
        reason="no pane output",
        lode_id="test-id",
        branch=branch,
    )

    with (
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch("hopper.lodes.pane_liveness", return_value=Liveness.GONE) as mock_liveness,
    ):
        assert cmd_lode([subcommand, "test-id", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == stored
    assert payload["status_display"] == expected
    assert payload["pane_liveness"] == "gone"
    mock_liveness.assert_called_once_with("%46")


def test_lode_list_json_reports_parked_gone_status(capsys, make_lode):
    stored = format_park_status("no pane output", "test-id")
    branch = "hopper-test-id"
    lode = make_lode(
        id="test-id",
        state="gated",
        status=stored,
        branch=branch,
        active=True,
        tmux_pane="%47",
    )
    expected = PARK_PANE_GONE_STATUS.format(
        reason="no pane output",
        lode_id="test-id",
        branch=branch,
    )

    with (
        patch("hopper.cli.require_server", return_value=None),
        patch("hopper.client.list_lodes", return_value=[lode]),
        patch("hopper.lodes.pane_liveness", return_value=Liveness.GONE) as mock_liveness,
    ):
        assert cmd_lode(["list", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)["lodes"][0]
    assert payload["status"] == stored
    assert payload["status_display"] == expected
    assert payload["pane_liveness"] == "gone"
    mock_liveness.assert_called_once_with("%47")


def test_lode_status_json_display_matches_human_status(capsys, make_lode):
    reason = "no pane output"
    branch = "hopper-test-id"
    lode = make_lode(
        id="test-id",
        state="gated",
        status=format_park_status(reason, "test-id"),
        branch=branch,
        active=True,
        tmux_pane="%48",
    )

    with (
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch("hopper.lodes.pane_liveness", return_value=Liveness.GONE),
    ):
        assert cmd_lode(["status", "test-id"]) == 0
        human_output = capsys.readouterr().out
        human_status = next(
            line.removeprefix("  status:   ")
            for line in human_output.splitlines()
            if line.startswith("  status:   ")
        )

        assert cmd_lode(["status", "test-id", "--json"]) == 0
        json_status = json.loads(capsys.readouterr().out)["status_display"]

    assert json_status == human_status


def test_format_lode_detail_pane_activity_age(make_lode):
    """Detailed output shows the relative age of observed pane activity."""
    now = current_time_ms()
    lode = make_lode(last_pane_activity_at=now - 3 * 60_000)

    with patch("hopper.lodes.current_time_ms", return_value=now):
        output = format_lode_detail(lode)

    assert "  activity: 3m ago" in output.splitlines()


def test_format_lode_detail_pane_activity_unmeasured(make_lode):
    """Detailed output shows unmeasured when pane activity is None."""
    now = current_time_ms()
    lode = make_lode(
        created_at=now - 2 * 60_000,
        updated_at=now - 60_000,
        last_pane_activity_at=None,
    )

    output = format_lode_detail(lode)
    activity_line = next(line for line in output.splitlines() if line.startswith("  activity:"))

    assert activity_line == "  activity: unmeasured"
    assert " ago" not in activity_line
    assert f"{format_age(0)} ago" not in output


def test_format_lode_detail_pane_activity_key_absent(make_lode):
    """Detailed output shows unmeasured when the pane activity key is absent."""
    lode = make_lode()
    lode.pop("last_pane_activity_at")

    output = format_lode_detail(lode)

    assert "  activity: unmeasured" in output.splitlines()


def test_format_lode_detail_zero_pane_activity_is_unmeasured(make_lode):
    """Detailed output treats a zero pane activity timestamp as unmeasured."""
    lode = make_lode(last_pane_activity_at=0)

    output = format_lode_detail(lode)

    assert "  activity: unmeasured" in output.splitlines()


def test_lode_status_json_keeps_raw_pane_activity(capsys, make_lode):
    pane_activity_at = current_time_ms() - 3 * 60_000
    lode = make_lode(id="test-id", host="local", last_pane_activity_at=pane_activity_at)

    with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
        assert cmd_lode(["status", "test-id", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["last_pane_activity_at"] == pane_activity_at
    assert "activity" not in payload
    assert "activity_age" not in payload
    assert "pane_activity_age" not in payload
    assert set(payload) == set(lode) | {"status_display", "pane_liveness"}


def test_format_lode_detail_pane_active(make_lode):
    """Active lode with tmux_pane shows pane line."""
    lode = make_lode(active=True, tmux_pane="%123")
    output = format_lode_detail(lode)
    assert "  pane:     %123" in output
    lines = output.split("\n")
    active_idx = next(i for i, line in enumerate(lines) if "active:" in line)
    pane_idx = next(i for i, line in enumerate(lines) if "pane:" in line)
    assert pane_idx == active_idx + 1


def test_format_lode_detail_pane_inactive(make_lode):
    """Inactive lode with tmux_pane does NOT show pane line."""
    lode = make_lode(active=False, tmux_pane="%123")
    output = format_lode_detail(lode)
    assert "pane:" not in output


def test_format_lode_detail_pane_none(make_lode):
    """Active lode with no tmux_pane does NOT show pane line."""
    lode = make_lode(active=True, tmux_pane=None)
    output = format_lode_detail(lode)
    assert "pane:" not in output


def test_format_lode_detail_progress_line(make_lode):
    """Detailed output shows progress when a progress summary is present."""
    lode = make_lode(status="Working", last_progress_summary="codex thinking")
    output = format_lode_detail(lode)
    assert "  status:   Working" in output
    assert "  progress: codex thinking" in output


@pytest.mark.parametrize(
    ("snapshot", "detail"),
    [
        ({"outcome": "committed", "sha": "a" * 40}, f"    sha:       {'a' * 40}"),
        ({"outcome": "clean"}, "    outcome:   clean"),
        ({"outcome": "no_worktree"}, "    outcome:   no_worktree"),
        (
            {"outcome": "failed", "git_error": "git add -A failed: index locked"},
            "    git_error: git add -A failed: index locked",
        ),
    ],
)
def test_format_lode_detail_recovery_outcomes(make_lode, snapshot, detail):
    recovery = {
        "failed_at": 1234,
        "stage": "refine",
        "reason": "stuck reason",
        "branch": "hopper-test-id",
        "worktree_path": "/tmp/worktree",
        "snapshot": snapshot,
    }
    lode = make_lode(
        id="test-id",
        state="error",
        status="enriched error with restart command",
        recovery=recovery,
    )

    output = format_lode_detail(lode)

    assert "  recovery:" in output
    assert detail in output
    assert "    failed_at: 1234" in output
    assert "    stage:     refine" in output
    assert "    branch:    hopper-test-id" in output
    assert "    worktree:  /tmp/worktree" in output
    assert "    reason:    stuck reason" in output
    assert "to retry:" not in output


def test_lode_status_json_includes_recovery_without_mutating_lode(capsys, make_lode):
    lode = make_lode(id="test-id", state="error")
    recovery = {
        "failed_at": 1234,
        "stage": "mill",
        "reason": "stuck reason",
        "branch": None,
        "worktree_path": None,
        "snapshot": {"outcome": "no_worktree"},
    }
    lode_dir = get_lode_dir("test-id")
    lode_dir.mkdir(parents=True)
    (lode_dir / "recovery.json").write_text(json.dumps(recovery))

    with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
        assert cmd_lode(["status", "test-id", "--json"]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        **lode,
        "host": "local",
        "recovery": recovery,
        "status_display": lode["status"],
        "pane_liveness": "not_probed",
    }
    assert captured.err == ""
    assert "recovery" not in lode


def test_lode_status_missing_recovery_leaves_output_unchanged(capsys, make_lode):
    lode = make_lode(id="test-id", state="running")
    expected = format_lode_detail({**lode, "host": "local"})

    with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
        assert cmd_lode(["status", "test-id"]) == 0

    assert capsys.readouterr().out == f"{expected}\n"
    assert "recovery" not in lode


def test_format_lode_detail_appends_gate_hint_when_gated(make_lode):
    """Detailed output includes the gate review hint for gated lodes."""
    lode = make_lode(id="gate1234", state="gated")
    output = format_lode_detail(lode)
    assert "Gate blocked. Review with: hop gate show gate1234" in output


def test_format_lode_detail_no_hint_when_not_gated(make_lode):
    """Detailed output omits the gate review hint for non-gated lodes."""
    lode = make_lode(id="gate1234", state="running")
    output = format_lode_detail(lode)
    assert "Gate blocked. Review with: hop gate show gate1234" not in output


def test_remote_set_refuses_active_local_project(capsys):
    save_projects([Project(path="/fake/repo", name="myproj")])

    rc = cmd_remote(["set", "myproj", "fedora.local"])

    assert rc == 1
    assert capsys.readouterr().out.splitlines() == [
        "error: remote pool update refused",
        "observed: project 'myproj' is active locally",
        "Hopper did not change config.json.",
        "recover with: hop project disable myproj --reason 'moved to fedora.local'",
    ]


def test_remote_rm_absent_has_complete_refusal(capsys):
    assert cmd_remote(["rm", "journal"]) == 1

    assert capsys.readouterr().out.splitlines() == [
        "error: remote pool removal refused",
        "observed: project 'journal' has no configured host pool",
        "Hopper did not change config.json.",
        "recover with: hop remote list",
    ]


def test_remote_set_warns_but_saves_when_unreachable(capsys):
    with patch(
        "hopper.remote.run_remote",
        return_value=subprocess.CompletedProcess([], 255, stdout="", stderr="no route"),
    ):
        rc = cmd_remote(["set", "journal", "fedora.local"])

    assert rc == 0
    assert "remote.journal=fedora.local" in capsys.readouterr().out


def test_remote_set_replaces_with_ordered_deduplicated_pool_and_pings_each_host(capsys):
    with config.config_transaction() as stored:
        stored["remote.journal"] = ["old.example"]
    results = [
        subprocess.CompletedProcess([], 0, stdout="pong", stderr=""),
        subprocess.CompletedProcess([], 255, stdout="", stderr="no route"),
    ]
    with patch("hopper.remote.run_remote", side_effect=results) as ping:
        assert (
            cmd_remote(["set", "journal", " first.example ", "second.example", "first.example"])
            == 0
        )

    assert ping.call_args_list == [
        (("first.example", ["ping"]), {"timeout": REMOTE_SET_PING_TIMEOUT_SEC}),
        (("second.example", ["ping"]), {"timeout": REMOTE_SET_PING_TIMEOUT_SEC}),
    ]
    assert config.load_config()["remote.journal"] == ["first.example", "second.example"]
    captured = capsys.readouterr()
    assert captured.out == "remote.journal=first.example,second.example\n"
    assert "second.example did not answer" in captured.err
    assert "first.example did not answer" not in captured.err


@pytest.mark.parametrize("args", [["set", "journal"], ["set", "journal", " "]])
def test_remote_set_requires_a_non_empty_host_and_does_not_write(capsys, args):
    assert cmd_remote(args) == 1

    assert config.load_config() == {}
    assert capsys.readouterr().out.splitlines() == [
        "error: remote pool update refused",
        "observed: the requested pool contains no usable host",
        "Hopper did not change config.json.",
        "recover with: hop remote set journal <host> [host ...]",
    ]


@pytest.mark.parametrize(
    ("host", "diagnostic"),
    [
        ("-oProxyCommand=bad", "unrecognized arguments"),
        ("local", "invalid remote host 'local'"),
        ("bad\nhost", "invalid remote host 'bad\\nhost'"),
        ("host\n", "invalid remote host 'host\\n'"),
        ("bad\x85host", "invalid remote host 'bad\\x85host'"),
    ],
)
def test_remote_set_rejects_unsafe_host_before_probe_or_write(host, diagnostic, capsys):
    with patch("hopper.remote.run_remote") as probe:
        assert cmd_remote(["set", "journal", host]) == 1

    probe.assert_not_called()
    assert config.load_config() == {}
    assert diagnostic in capsys.readouterr().out


def test_remote_list_json(capsys):
    with patch(
        "hopper.remote.run_remote", return_value=subprocess.CompletedProcess([], 0, "pong", "")
    ):
        assert cmd_remote(["set", "journal", "fedora.local"]) == 0

    assert cmd_remote(["list", "--json"]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{") :])
    assert payload == {"remotes": [{"project": "journal", "hosts": ["fedora.local"]}]}


def test_remote_list_human_exposes_complete_pool(capsys):
    with patch(
        "hopper.remote.run_remote",
        return_value=subprocess.CompletedProcess([], 0, stdout="pong", stderr=""),
    ):
        assert cmd_remote(["set", "journal", "first.example", "second.example"]) == 0
    capsys.readouterr()

    assert cmd_remote(["list"]) == 0

    assert capsys.readouterr().out == "journal                  first.example, second.example\n"


@pytest.fixture
def emitted_create_json(monkeypatch, capsys):
    """Capture the real local lode-create JSON emitter for remote response fixtures."""
    from io import StringIO

    created_lode = {"id": "abcdefgh", "project": "journal", "stage": "mill"}
    project = Project(path="/srv/journal", name="journal")
    with (
        patch("hopper.cli.require_server", return_value=None),
        patch("hopper.projects.find_project", return_value=project),
        patch("hopper.git.dirty_status", return_value=""),
        patch("hopper.client.create_lode", return_value=created_lode),
        patch("sys.stdin", StringIO(LONG_SCOPE)),
    ):
        monkeypatch.setattr(sys, "argv", ["hop", "lode", "create", "journal", "--json"])
        assert main() == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "id": "abcdefgh",
        "project": "journal",
        "host": "local",
    }
    return output


def test_active_local_project_bypasses_pool_selection():
    with (
        patch("hopper.projects.find_project", return_value=Project("/srv/journal", "journal")),
        patch("hopper.remote.remote_registry") as registry,
        patch("hopper.remote.probe_candidates") as probes,
    ):
        assert hopper_cli._remote_pool_for_create("journal") is None

    registry.assert_not_called()
    probes.assert_not_called()


def test_explicit_host_create_bypasses_pool_selection(
    emitted_create_json,
    monkeypatch,
    capsys,
):
    from io import StringIO

    monkeypatch.setattr(sys, "argv", ["hop", "-H", "explicit.example", "implement", "journal"])
    monkeypatch.setattr(sys, "stdin", StringIO(LONG_SCOPE))
    result = subprocess.CompletedProcess([], 0, stdout=emitted_create_json, stderr="")
    with (
        patch("hopper.cli._remote_pool_for_create") as pool_selection,
        patch("hopper.remote.run_remote", return_value=result) as create,
        patch("hopper.remote.remember_lode") as remember,
    ):
        assert main() == 0

    pool_selection.assert_not_called()
    create.assert_called_once()
    assert create.call_args.args[:2] == (
        "explicit.example",
        ["implement", "journal", "--json"],
    )
    remember.assert_called_once_with("abcdefgh", "explicit.example", "journal")
    assert capsys.readouterr().out == "Created lode abcdefgh (journal) on explicit.example\n"


@pytest.mark.parametrize("json_output", [False, True], ids=["human", "json"])
def test_pooled_create_uses_eligible_host_and_reports_unavailable_siblings(
    emitted_create_json,
    monkeypatch,
    capsys,
    json_output,
):
    from io import StringIO

    selected = CandidateProbe("ready.example", eligible=True, load=1, reason=None)
    unavailable = CandidateProbe(
        "down.example",
        eligible=False,
        load=None,
        reason="project listing timed out; inspect with: hop -H down.example project list --json",
    )
    args = ["hop", "implement", "journal"]
    if json_output:
        args.append("--json")
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(sys, "stdin", StringIO(LONG_SCOPE))
    result = subprocess.CompletedProcess([], 0, stdout=emitted_create_json, stderr="")
    with (
        patch(
            "hopper.cli._remote_pool_for_create",
            return_value=(selected, [unavailable, selected]),
        ),
        patch("hopper.remote.run_remote", return_value=result) as create,
        patch("hopper.remote.remember_lode") as remember,
    ):
        assert main() == 0

    create.assert_called_once()
    assert create.call_args.args[0] == "ready.example"
    assert create.call_args.args[1][-1] == "--json"
    assert create.call_args.kwargs["timeout"] == REMOTE_CREATE_TIMEOUT_SEC
    remember.assert_called_once_with("abcdefgh", "ready.example", "journal")
    captured = capsys.readouterr()
    if json_output:
        assert json.loads(captured.out) == {
            "id": "abcdefgh",
            "project": "journal",
            "host": "ready.example",
            "unavailable_hosts": [
                {
                    "host": "down.example",
                    "reason": unavailable.reason,
                }
            ],
        }
        assert "pool host down.example unavailable" not in captured.err
    else:
        assert captured.out == "Created lode abcdefgh (journal) on ready.example\n"
        assert f"warning: pool host down.example unavailable: {unavailable.reason}" in captured.err


@pytest.mark.parametrize("json_output", [False, True], ids=["human", "json"])
def test_pooled_create_with_no_eligible_host_refuses_without_create(
    monkeypatch,
    capsys,
    json_output,
):
    from io import StringIO

    probes = [
        CandidateProbe(
            "one.example",
            False,
            None,
            "project missing; inspect with: hop -H one.example project list --json",
        ),
        CandidateProbe(
            "two.example",
            False,
            None,
            "inventory timed out; inspect with: hop -H two.example lode list --json",
        ),
    ]
    args = ["hop", "implement", "journal"]
    if json_output:
        args.append("--json")
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(sys, "stdin", StringIO(LONG_SCOPE))
    with (
        patch("hopper.cli._remote_pool_for_create", return_value=(None, probes)),
        patch("hopper.remote.run_remote") as create,
    ):
        assert main() == 2

    create.assert_not_called()
    captured = capsys.readouterr()
    if json_output:
        payload = json.loads(captured.out)
        assert payload["creation_attempted"] is False
        assert payload["unavailable_hosts"] == [
            {"host": probe.host, "reason": probe.reason} for probe in probes
        ]
    else:
        assert captured.out == ""
        assert "Hopper did not attempt lode creation" in captured.err
        for probe in probes:
            assert probe.host in captured.err
            assert probe.reason in captured.err


@pytest.mark.parametrize(
    "case",
    [
        "nonzero-valid-body",
        "human-text",
        "extra-json-value",
        "extra-member",
        "wrong-project",
        "wrong-host",
        "malformed-shape",
        "malformed-json",
        "invalid-id",
        "transport",
        "timeout",
    ],
)
def test_authoritative_remote_create_refuses_every_invalid_response(case, capsys):
    valid = {"id": "abcdefgh", "project": "journal", "host": "local"}
    if case == "nonzero-valid-body":
        outcome = subprocess.CompletedProcess([], 7, stdout=json.dumps(valid), stderr="rejected")
    elif case == "human-text":
        outcome = subprocess.CompletedProcess(
            [], 0, stdout="Created lode abcdefgh (journal)\n", stderr=""
        )
    elif case == "extra-json-value":
        outcome = subprocess.CompletedProcess(
            [], 0, stdout=f"{json.dumps(valid)}\n{json.dumps(valid)}\n", stderr=""
        )
    elif case == "extra-member":
        outcome = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({**valid, "extra": True}), stderr=""
        )
    elif case == "wrong-project":
        outcome = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({**valid, "project": "other"}), stderr=""
        )
    elif case == "wrong-host":
        outcome = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({**valid, "host": "selected.example"}), stderr=""
        )
    elif case == "malformed-shape":
        outcome = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
    elif case == "malformed-json":
        outcome = subprocess.CompletedProcess([], 0, stdout="{", stderr="")
    elif case == "invalid-id":
        outcome = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({**valid, "id": "abc12345"}), stderr=""
        )
    elif case == "transport":
        outcome = OSError("connection reset")
    else:
        outcome = subprocess.TimeoutExpired(["ssh"], REMOTE_CREATE_TIMEOUT_SEC)

    runner_patch = (
        patch("hopper.remote.run_remote", side_effect=outcome)
        if isinstance(outcome, BaseException)
        else patch("hopper.remote.run_remote", return_value=outcome)
    )
    with runner_patch as create, patch("hopper.remote.remember_lode") as remember:
        assert (
            hopper_cli._run_authoritative_remote_create(
                "selected.example",
                ["implement", "journal"],
                reason="remote.journal pool",
                project="journal",
                stdin_text=LONG_SCOPE,
                json_output=False,
                unavailable_hosts=[],
            )
            == 2
        )

    create.assert_called_once()
    assert create.call_args.kwargs["timeout"] == REMOTE_CREATE_TIMEOUT_SEC
    remember.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Created lode" not in captured.err
    assert "creation may have occurred on selected.example" in captured.err
    assert "no failover ran" in captured.err
    assert "hop -H selected.example lode list --project journal" in captured.err


def test_authoritative_remote_create_buffers_success_until_route_is_cached(
    emitted_create_json,
    capsys,
):
    result = subprocess.CompletedProcess([], 0, stdout=emitted_create_json, stderr="")
    observed = []

    def persist(lode_id, host, project):
        observed.append((lode_id, host, project, capsys.readouterr().out))

    with (
        patch("hopper.remote.run_remote", return_value=result) as create,
        patch("hopper.remote.remember_lode", side_effect=persist),
    ):
        assert (
            hopper_cli._run_authoritative_remote_create(
                "selected.example",
                ["implement", "journal"],
                reason="remote.journal pool",
                project="journal",
                stdin_text=LONG_SCOPE,
                json_output=False,
                unavailable_hosts=[],
            )
            == 0
        )

    create.assert_called_once()
    assert observed == [("abcdefgh", "selected.example", "journal", "")]
    assert capsys.readouterr().out == "Created lode abcdefgh (journal) on selected.example\n"


def test_authoritative_remote_create_cache_failure_reports_explicit_recovery(
    emitted_create_json,
    capsys,
):
    result = subprocess.CompletedProcess([], 0, stdout=emitted_create_json, stderr="")
    with (
        patch("hopper.remote.run_remote", return_value=result) as create,
        patch("hopper.remote.remember_lode", side_effect=OSError("disk full")),
    ):
        assert (
            hopper_cli._run_authoritative_remote_create(
                "selected.example",
                ["implement", "journal"],
                reason="remote.journal pool",
                project="journal",
                stdin_text=LONG_SCOPE,
                json_output=True,
                unavailable_hosts=[],
            )
            == 2
        )

    create.assert_called_once()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "valid lode ID abcdefgh" in captured.err
    assert "resident route was not saved" in captured.err
    assert "creation may have occurred on selected.example" in captured.err
    assert "no failover ran" in captured.err
    assert "hop -H selected.example lode status abcdefgh --json" in captured.err


def test_main_routes_disabled_project_to_remote(monkeypatch, capsys):
    from io import StringIO

    save_projects([Project(path="/fake/repo", name="journal", disabled=True)])
    selected = CandidateProbe("fedora.local", eligible=True, load=0, reason=None)
    with patch("hopper.remote.run_remote") as mock_remote:
        mock_remote.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout='{"id": "abcdefgh", "project": "journal", "host": "local"}\n',
            stderr="",
        )
        with patch("hopper.cli._remote_pool_for_create", return_value=(selected, [selected])):
            monkeypatch.setattr(sys, "argv", ["hop", "implement", "journal", "--json"])
            monkeypatch.setattr(sys, "stdin", StringIO(LONG_SCOPE))
            rc = main()

    assert rc == 0
    assert mock_remote.call_args.args[:2] == ("fedora.local", ["implement", "journal", "--json"])
    assert mock_remote.call_args.kwargs["timeout"] == REMOTE_CREATE_TIMEOUT_SEC
    assert json.loads(capsys.readouterr().out)["host"] == "fedora.local"


def test_main_rejects_locally_expanded_home_for_explicit_remote(monkeypatch, capsys):
    local_path = str(Path.home() / "src" / "project")
    monkeypatch.setattr(sys, "argv", ["hop", "-H", "fedora.local", "project", "add", local_path])

    with patch("hopper.remote.run_remote") as mock_remote:
        assert main() == 2

    mock_remote.assert_not_called()
    assert "quote the tilde" in capsys.readouterr().err


def test_lode_create_json(capsys):
    from io import StringIO

    created_lode = {"id": "abc12345", "project": "myproj", "stage": "mill"}
    project = Project(path="/fake/repo", name="myproj")
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.projects.find_project", return_value=project):
            with patch("hopper.git.dirty_status", return_value=""):
                with patch("hopper.client.create_lode", return_value=created_lode):
                    with patch("sys.stdin", StringIO(LONG_SCOPE)):
                        assert cmd_lode(["create", "myproj", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "id": "abc12345",
        "project": "myproj",
        "host": "local",
    }


def test_lode_status_json_remote(capsys):
    remote_lode = {
        "id": "remote123",
        "project": "journal",
        "stage": "mill",
        "state": "running",
        "status": "Working",
        "host": "fedora.local",
        "tmux_pane": "%remote",
        "status_display": "remote-computed status",
        "pane_liveness": "gone",
    }
    with (
        patch(
            "hopper.cli._resolve_lode",
            return_value={
                "outcome": "found",
                "lode": remote_lode,
                "host": "fedora.local",
                "canonical_id": "remote123",
                "error": None,
                "probe_summary": "fedora.local=found",
                "exit_code": 0,
            },
        ),
        patch(
            "hopper.lodes.pane_liveness",
            side_effect=AssertionError("pane_liveness must not be called"),
        ),
    ):
        assert cmd_lode(["status", "remote123", "--json"]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        **remote_lode,
        "status_display": remote_lode["status"],
        "pane_liveness": "not_probed",
    }
    assert captured.err == ""
    assert remote_lode["status_display"] == "remote-computed status"
    assert remote_lode["pane_liveness"] == "gone"


def test_lode_list_json_envelope(capsys):
    lode = {
        "id": "abc123",
        "stage": "mill",
        "state": "running",
        "active": True,
        "project": "proj",
        "status": "",
    }
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.list_lodes", return_value=[lode]):
            assert cmd_lode(["list", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"lodes": [{**lode, "status_display": "", "pane_liveness": "not_probed"}]}
    assert "status_display" not in lode
    assert "pane_liveness" not in lode


def test_lode_peek_plain_text(capsys):
    lode = {"id": "abc123", "tmux_pane": "%1", "active": True, "state": "running"}
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
            with patch("hopper.cli.capture_pane", return_value="one\ntwo\nthree\n"):
                assert cmd_lode(["peek", "abc123", "-n", "2"]) == 0

    assert capsys.readouterr().out == "two\nthree\n"


def test_lode_nudge_routes_through_server(capsys):
    lode = {"id": "abc123", "tmux_pane": "%1", "active": True, "state": "running"}
    response = {"type": "pane_input_sent", "lode_id": "abc123", "tmux_pane": "%1"}
    with (
        patch("hopper.cli.require_server", return_value=None),
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch("hopper.client.send_pane_input", return_value=response) as mock_send,
        patch("hopper.cli.capture_pane") as mock_capture,
    ):
        assert cmd_lode(["nudge", "abc123", "--text", "continue"]) == 0

    mock_send.assert_called_once_with(ANY, "abc123", "continue", paste=True)
    mock_capture.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == "submitted\n"
    assert captured.err == ""


def test_lode_answer_rejects_invalid_choice_before_delivery(capsys):
    lode = {"id": "abc123", "tmux_pane": "%1", "active": True, "state": "running"}
    with (
        patch("hopper.cli.require_server", return_value=None),
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch("hopper.client.send_pane_input") as mock_send,
    ):
        assert cmd_lode(["answer", "abc123", "10"]) == 1

    mock_send.assert_not_called()
    assert "choice must be a digit" in capsys.readouterr().out


def test_lode_answer_routes_through_server_without_paste(capsys):
    lode = {"id": "abc123", "tmux_pane": "%1", "active": True, "state": "running"}
    response = {"type": "pane_input_sent", "lode_id": "abc123", "tmux_pane": "%1"}
    with (
        patch("hopper.cli.require_server", return_value=None),
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch("hopper.client.send_pane_input", return_value=response) as mock_send,
        patch("hopper.cli.capture_pane") as mock_capture,
    ):
        assert cmd_lode(["answer", "abc123", "1"]) == 0

    mock_send.assert_called_once_with(ANY, "abc123", "1", paste=False)
    mock_capture.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == "submitted\n"
    assert captured.err == ""


def test_lode_pane_input_unverified_prints_framed_tail(capsys):
    lode = {"id": "abc123", "tmux_pane": "%1", "active": True, "state": "running"}
    response = {
        "type": "error",
        "outcome": "unverified",
        "error": "Delivery outcome unknown; inspect pane.",
        "tail": "first pane line\nlast pane line",
    }
    with (
        patch("hopper.cli.require_server", return_value=None),
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch("hopper.client.send_pane_input", return_value=response),
    ):
        assert cmd_lode(["nudge", "abc123"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "Delivery outcome unknown; inspect pane.\n"
        "--- pane tail ---\n"
        "first pane line\n"
        "last pane line\n"
        "--- end pane tail ---\n"
    )


def test_lode_pane_input_non_unverified_hides_tail(capsys):
    lode = {"id": "abc123", "tmux_pane": "%1", "active": True, "state": "running"}
    response = {
        "type": "error",
        "outcome": "pane_state_unknown",
        "error": "Input was not sent; inspect pane.",
        "tail": "pane content",
    }
    with (
        patch("hopper.cli.require_server", return_value=None),
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch("hopper.client.send_pane_input", return_value=response),
    ):
        assert cmd_lode(["answer", "abc123", "1"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Input was not sent; inspect pane.\n"
    assert "submitted" not in captured.err


def test_lode_pane_input_missing_response_is_unknown(capsys):
    lode = {"id": "abc123", "tmux_pane": "%1", "active": True, "state": "running"}
    with (
        patch("hopper.cli.require_server", return_value=None),
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch("hopper.client.send_pane_input", return_value=None),
    ):
        assert cmd_lode(["nudge", "abc123"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "delivery outcome is unknown" in captured.err
    assert "hop lode peek abc123" in captured.err
    assert "submitted" not in captured.err


def test_lode_restart_force_requests_server_side_termination(capsys):
    lode = {"id": "abc123", "stage": "mill", "state": "running", "active": True, "tmux_pane": "%9"}
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
            with patch(
                "hopper.client.restart_lode",
                return_value={
                    "type": "lode_action_ack",
                    "outcome": "completed",
                    "disposition": "replacement_spawned",
                },
            ) as mock_restart:
                assert cmd_lode(["restart", "abc123", "--force"]) == 0

    assert mock_restart.call_args.args == (Path(config.server_socket_path()), "abc123", "mill")
    assert mock_restart.call_args.kwargs["force"] is True
    assert isinstance(mock_restart.call_args.kwargs["action_id"], str)
    assert mock_restart.call_args.kwargs["expected_generation"] is None
    assert "Terminating the registered runner" in capsys.readouterr().out


def test_resolver_remote_probe_does_not_cascade(monkeypatch):
    from hopper.cli import _resolve_lode

    monkeypatch.setenv("HOP_NO_ROUTE", "1")
    with (
        patch("hopper.client.read_lode_snapshot", return_value=("absent", None)),
        patch("hopper.remote.remote_registry") as registry,
        patch("hopper.remote.load_lode_cache") as cache,
        patch("hopper.cli._remote_lode_status") as remote_probe,
    ):
        result = _resolve_lode(Path("sock"), "abc")

    assert result["outcome"] == "absent"
    assert result["probe_summary"] == "local=absent"
    registry.assert_not_called()
    cache.assert_not_called()
    remote_probe.assert_not_called()


def test_resolver_routes_exact_id_to_unregistered_resident_host():
    from hopper.cli import _resolve_lode

    resident = {
        "abc23456": {
            "host": "resident.example",
            "project": "project",
            "created_ms": 4_000_000_000_000,
        }
    }
    with (
        patch("hopper.client.read_lode_snapshot", return_value=("absent", None)),
        patch("hopper.remote.load_lode_cache", return_value=resident),
        patch("hopper.remote.remote_registry") as registry,
        patch(
            "hopper.cli._remote_lode_status",
            return_value=({"id": "abc23456", "project": "project"}, "found"),
        ) as probe,
        patch("hopper.cli._remember_lode_route"),
    ):
        result = _resolve_lode(Path("sock"), "abc23456")

    assert result["outcome"] == "found"
    assert result["host"] == "resident.example"
    probe.assert_called_once_with("resident.example", "abc23456")
    registry.assert_not_called()


def test_resolver_routes_unique_prefix_to_unregistered_resident_host():
    from hopper.cli import _resolve_lode

    resident = {
        "abc23456": {
            "host": "resident.example",
            "project": "project",
            "created_ms": 4_000_000_000_000,
        }
    }
    with (
        patch("hopper.client.read_lode_snapshot", return_value=("absent", None)),
        patch("hopper.remote.load_lode_cache", return_value=resident),
        patch("hopper.remote.remote_registry") as registry,
        patch(
            "hopper.cli._remote_lode_status",
            return_value=({"id": "abc23456", "project": "project"}, "found"),
        ) as probe,
        patch("hopper.cli._remember_lode_route"),
    ):
        result = _resolve_lode(Path("sock"), "abc")

    assert result["outcome"] == "found"
    assert result["canonical_id"] == "abc23456"
    probe.assert_called_once_with("resident.example", "abc23456")
    registry.assert_not_called()


@pytest.mark.parametrize(
    ("surface", "command", "args"),
    [
        ("status", cmd_lode, ["status", "abc"]),
        ("show", cmd_lode, ["show", "abc"]),
        ("path", cmd_lode, ["path", "abc"]),
        ("log", cmd_lode, ["log", "abc"]),
        ("pause", cmd_lode, ["pause", "abc"]),
        ("resume", cmd_lode, ["resume", "abc"]),
        ("restart", cmd_lode, ["restart", "abc"]),
        ("kill", cmd_lode, ["kill", "abc"]),
        ("peek", cmd_lode, ["peek", "abc"]),
        ("nudge", cmd_lode, ["nudge", "abc"]),
        ("answer", cmd_lode, ["answer", "abc", "1"]),
        ("watch", cmd_lode, ["watch", "abc"]),
        ("wait", cmd_lode, ["wait", "abc"]),
        ("gate-show", cmd_gate, ["show", "abc"]),
        ("gate-feedback", cmd_gate, ["feedback", "abc", "review"]),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_lode_id_surfaces_use_the_single_resolver(surface, command, args):
    unavailable = {
        "outcome": "unavailable",
        "error": "resident route unavailable",
        "exit_code": 2,
    }
    with patch(
        "hopper.cli._resolve_lode",
        return_value=unavailable,
    ) as resolver:
        assert command(args) == 2, surface

    resolver.assert_called_once_with(config.server_socket_path(), "abc")


def test_resident_prefix_ambiguity_refuses_without_probing():
    from hopper.cli import _resolve_lode

    cache = {
        lode_id: {
            "host": host,
            "project": "project",
            "created_ms": 4_000_000_000_000,
        }
        for lode_id, host in (
            ("abc23456", "one.example"),
            ("abc34567", "two.example"),
        )
    }
    with (
        patch("hopper.client.read_lode_snapshot", return_value=("absent", None)),
        patch("hopper.remote.load_lode_cache", return_value=cache),
        patch("hopper.cli._remote_lode_status") as probe,
        patch("hopper.cli._remote_hosts") as hosts,
    ):
        result = _resolve_lode(Path("sock"), "abc")

    assert result["outcome"] == "ambiguous"
    assert result["exit_code"] == 1
    probe.assert_not_called()
    hosts.assert_not_called()


def test_local_and_resident_prefix_matches_are_ambiguous():
    from hopper.cli import _resolve_lode

    local = {"id": "abc23456", "project": "local"}
    cache = {
        "abc34567": {
            "host": "resident.example",
            "project": "remote",
            "created_ms": 4_000_000_000_000,
        }
    }
    with (
        patch("hopper.client.read_lode_snapshot", return_value=("found", local)),
        patch("hopper.remote.load_lode_cache", return_value=cache),
        patch(
            "hopper.cli._remote_lode_status",
            return_value=({"id": "abc34567", "project": "remote"}, "found"),
        ),
        patch("hopper.cli._remember_lode_route") as remember,
    ):
        result = _resolve_lode(Path("sock"), "abc")

    assert result["outcome"] == "ambiguous"
    assert "local:abc23456" in result["error"]
    assert "resident.example:abc34567" in result["error"]
    remember.assert_not_called()


@pytest.mark.parametrize("state", ["absent", "unreadable"])
def test_resident_route_failure_never_fans_out(state):
    from hopper.cli import _resolve_lode

    cache = {
        "abc23456": {
            "host": "resident.example",
            "project": "project",
            "created_ms": 4_000_000_000_000,
        }
    }
    with (
        patch("hopper.client.read_lode_snapshot", return_value=("absent", None)),
        patch("hopper.remote.load_lode_cache", return_value=cache),
        patch("hopper.cli._remote_lode_status", return_value=(None, state)) as probe,
        patch("hopper.cli._remote_hosts") as hosts,
    ):
        result = _resolve_lode(Path("sock"), "abc")

    assert result["outcome"] == state.replace("unreadable", "unavailable")
    assert "hop -H resident.example lode status abc23456 --json" in result["error"]
    probe.assert_called_once_with("resident.example", "abc23456")
    hosts.assert_not_called()


def test_confirmed_stale_resident_route_preserves_unique_local_prefix_match():
    from hopper.cli import _resolve_lode

    local = {"id": "abc23456", "project": "local"}
    cache = {
        "abc34567": {
            "host": "former.example",
            "project": "remote",
            "created_ms": 4_000_000_000_000,
        }
    }
    with (
        patch("hopper.client.read_lode_snapshot", return_value=("found", local)),
        patch("hopper.remote.load_lode_cache", return_value=cache),
        patch("hopper.cli._remote_lode_status", return_value=(None, "absent")),
        patch("hopper.remote.forget_lode", return_value=True) as forget,
    ):
        result = _resolve_lode(Path("sock"), "abc")

    assert result["outcome"] == "found"
    assert result["host"] == "local"
    assert result["canonical_id"] == "abc23456"
    forget.assert_called_once_with("abc34567")


def test_discovered_remote_route_persistence_failure_refuses_mutation(capsys):
    remote_lode = {
        "id": "abc23456",
        "project": "journal",
        "stage": "refine",
        "state": "running",
        "status": "working",
        "active": True,
    }
    with (
        patch("hopper.client.read_lode_snapshot", return_value=("absent", None)),
        patch("hopper.remote.load_lode_cache", return_value={}),
        patch("hopper.cli._remote_hosts", return_value=["worker.example"]),
        patch("hopper.cli._remote_lode_status", return_value=(remote_lode, "found")),
        patch("hopper.cli._remember_lode_route", return_value=OSError("disk full")),
        patch("hopper.cli._run_remote_cli") as mutate,
    ):
        assert cmd_lode(["pause", "abc23456"]) == 2

    mutate.assert_not_called()
    output = capsys.readouterr().out
    assert "could not be saved: disk full" in output
    assert "did NOT route or mutate" in output


def test_expired_resident_route_is_not_authoritative():
    from hopper.cli import _resolve_lode
    from hopper.remote import REMOTE_LODE_CACHE_MAX_AGE_MS

    cache = {
        "abc23456": {
            "host": "former.example",
            "project": "project",
            "created_ms": 1,
        }
    }
    with (
        patch("hopper.client.read_lode_snapshot", return_value=("absent", None)),
        patch("hopper.remote.load_lode_cache", return_value=cache),
        patch(
            "hopper.remote.current_time_ms",
            return_value=REMOTE_LODE_CACHE_MAX_AGE_MS + 1,
        ),
        patch("hopper.cli._remote_hosts", return_value=["current.example"]),
        patch("hopper.cli._remote_lode_status", return_value=(None, "absent")) as probe,
    ):
        result = _resolve_lode(Path("sock"), "abc")

    assert result["outcome"] == "absent"
    probe.assert_called_once_with("current.example", "abc")


@pytest.mark.parametrize(
    "reason",
    ["malformed", "wrong_shape", "unreadable", "locked"],
)
def test_resident_cache_failure_is_unavailable_and_names_recovery(
    temp_config,
    reason,
):
    from hopper.cli import _resolve_lode
    from hopper.remote import LodeCacheError, remote_lode_cache_path

    cache_path = remote_lode_cache_path()
    with (
        patch("hopper.client.read_lode_snapshot", return_value=("absent", None)),
        patch(
            "hopper.remote.load_lode_cache",
            side_effect=LodeCacheError(cache_path, reason),
        ),
        patch("hopper.cli._remote_hosts") as hosts,
        patch("hopper.cli._remote_lode_status") as probe,
    ):
        result = _resolve_lode(Path("sock"), "abc")

    assert result["outcome"] == "unavailable"
    assert result["exit_code"] == 2
    assert str(cache_path) in result["error"]
    assert reason in result["error"]
    assert "Hopper did NOT treat the route as absent or fan out" in result["error"]
    assert "hop -H" in result["error"]
    hosts.assert_not_called()
    probe.assert_not_called()


def test_explicit_host_bypasses_unavailable_resident_cache(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hop", "-H", "known.example", "status", "abc"])
    with (
        patch("hopper.remote.load_lode_cache") as cache,
        patch("hopper.cli._run_remote_cli", return_value=0) as run_remote,
    ):
        assert main() == 0

    cache.assert_not_called()
    run_remote.assert_called_once_with(
        "known.example",
        ["status", "abc"],
        reason="-H known.example",
        stdin_text=None,
        annotate_json=False,
    )


@pytest.mark.parametrize(
    ("verb", "mutation"),
    [("pause", "pause_lode"), ("resume", "resume_lode")],
)
def test_resolver_two_host_prefix_is_ambiguous_without_mutation(verb, mutation, capsys):
    lodes = {
        "one.example": {"id": "abc11111", "project": "one"},
        "two.example": {"id": "abc22222", "project": "two"},
    }

    def probe(host, prefix):
        return {**lodes[host], "host": host}, "found"

    with (
        patch("hopper.client.read_lode_snapshot", return_value=("absent", None)),
        patch("hopper.remote.load_lode_cache", return_value={}),
        patch("hopper.cli._remote_hosts", return_value=sorted(lodes)),
        patch("hopper.cli._remote_lode_status", side_effect=probe),
        patch(f"hopper.client.{mutation}") as mutate,
        patch("hopper.cli._run_remote_cli") as run_remote,
    ):
        assert cmd_lode([verb, "abc"]) == 1

    mutate.assert_not_called()
    run_remote.assert_not_called()
    assert "one.example:abc11111, two.example:abc22222" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("verb", "mutation"),
    [("pause", "pause_lode"), ("resume", "resume_lode")],
)
def test_resolver_one_remote_host_ambiguity_refuses_without_mutation(
    verb,
    mutation,
    capsys,
):
    result = subprocess.CompletedProcess(
        [],
        1,
        stdout="",
        stderr=json.dumps(
            {
                "outcome": "ambiguous",
                "query": "abc",
                "error": "ambiguous",
                "matches": ["abc22222", "abc33333"],
            }
        ),
    )
    with (
        patch("hopper.client.read_lode_snapshot", return_value=("absent", None)),
        patch("hopper.remote.load_lode_cache", return_value={}),
        patch("hopper.cli._remote_hosts", return_value=["one.example"]),
        patch("hopper.remote.run_remote", return_value=result),
        patch(f"hopper.client.{mutation}") as mutate,
        patch("hopper.cli._run_remote_cli") as run_remote,
    ):
        assert cmd_lode([verb, "abc"]) == 1

    mutate.assert_not_called()
    run_remote.assert_not_called()
    out = capsys.readouterr().out
    assert "one.example:abc22222, one.example:abc33333" in out
    assert "one.example=ambiguous (abc22222, abc33333)" in out


@pytest.mark.parametrize(
    ("verb", "mutation"),
    [("pause", "pause_lode"), ("resume", "resume_lode")],
)
def test_resolver_match_plus_unavailable_is_unavailable_without_mutation(verb, mutation, capsys):
    def probe(host, prefix):
        if host == "one.example":
            return {"id": "abc11111", "project": "one", "host": host}, "found"
        return None, "unreadable"

    with (
        patch("hopper.client.read_lode_snapshot", return_value=("absent", None)),
        patch("hopper.remote.load_lode_cache", return_value={}),
        patch("hopper.cli._remote_hosts", return_value=["one.example", "two.example"]),
        patch("hopper.cli._remote_lode_status", side_effect=probe),
        patch(f"hopper.client.{mutation}") as mutate,
        patch("hopper.cli._run_remote_cli") as run_remote,
    ):
        assert cmd_lode([verb, "abc"]) == 2

    mutate.assert_not_called()
    run_remote.assert_not_called()
    assert "two.example=unreadable" in capsys.readouterr().out


def test_resolver_exact_full_id_ignores_unrelated_unavailable():
    from hopper.cli import _resolve_lode

    def probe(host, prefix):
        if host == "one.example":
            return {"id": prefix, "project": "one", "host": host}, "found"
        return None, "unreadable"

    with (
        patch("hopper.client.read_lode_snapshot", return_value=("absent", None)),
        patch("hopper.remote.load_lode_cache", return_value={}),
        patch("hopper.cli._remote_hosts", return_value=["one.example", "two.example"]),
        patch("hopper.cli._remote_lode_status", side_effect=probe),
        patch("hopper.cli._remember_lode_route"),
    ):
        result = _resolve_lode(Path("sock"), "abc12345")

    assert result["outcome"] == "found"
    assert result["canonical_id"] == "abc12345"
    assert result["host"] == "one.example"
    assert "two.example=unreadable" in result["probe_summary"]


@pytest.mark.parametrize("verb", ["pause", "resume"])
def test_lode_pause_resume_routes_canonical_full_id(verb):
    remote_lode = {
        "id": "abc12345",
        "host": "resident.example",
        "project": "project",
        "active": True,
        "stage": "mill",
        "state": "running",
        "run_generation": "a" * 32,
    }
    with (
        patch(
            "hopper.cli._resolve_lode",
            return_value=_watch_resolution(remote_lode, "resident.example"),
        ),
        patch("hopper.cli._run_remote_cli", return_value=0) as run_remote,
    ):
        assert cmd_lode([verb, "abc"]) == 0

    assert run_remote.call_args.args[0] == "resident.example"
    remote_args = run_remote.call_args.args[1]
    assert remote_args[:3] == ["lode", verb, "abc12345"]
    if verb == "pause":
        assert remote_args[3] == "--action-id"
        assert len(remote_args[4]) == 32
        assert remote_args[5:] == ["--expected-generation", "a" * 32]
    else:
        assert remote_args == ["lode", "resume", "abc12345"]


@pytest.mark.parametrize("verb", ["pause", "restart", "kill"])
def test_manual_action_hidden_identity_is_all_or_none_before_resolution(verb, capsys):
    with patch("hopper.cli._resolve_lode") as resolve:
        assert cmd_lode([verb, "abc12345", "--action-id", "a" * 32]) == 1

    resolve.assert_not_called()
    assert "must be provided together" in capsys.readouterr().out


@pytest.mark.parametrize("verb", ["pause", "restart", "kill"])
def test_routed_manual_action_without_identity_refuses_before_resolution(verb, monkeypatch, capsys):
    monkeypatch.setenv("HOP_NO_ROUTE", "1")
    with patch("hopper.cli._resolve_lode") as resolve:
        assert cmd_lode([verb, "abc12345"]) == 1

    resolve.assert_not_called()
    assert "upgrade the calling hop CLI" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("verb", "extra"),
    [("pause", []), ("restart", ["--force"]), ("kill", ["--force"])],
)
def test_remote_manual_action_forwards_existing_identity_without_regeneration(verb, extra):
    lode = {
        "id": "abc12345",
        "host": "resident.example",
        "active": False,
        "stage": "mill",
        "state": "running",
        "run_generation": "c" * 32,
    }
    action_id = "a" * 32
    expected_generation = "b" * 32
    with (
        patch(
            "hopper.cli._resolve_lode",
            return_value=_watch_resolution(lode, "resident.example"),
        ),
        patch("hopper.cli.uuid.uuid4", side_effect=AssertionError("identity regenerated")),
        patch("hopper.cli._run_remote_cli", return_value=0) as run_remote,
    ):
        assert (
            cmd_lode(
                [
                    verb,
                    "abc",
                    *extra,
                    "--action-id",
                    action_id,
                    "--expected-generation",
                    expected_generation,
                ]
            )
            == 0
        )

    remote_args = run_remote.call_args.args[1]
    assert remote_args[:3] == ["lode", verb, "abc12345"]
    assert "--action-id" in remote_args
    assert remote_args[remote_args.index("--action-id") + 1] == action_id
    assert remote_args[remote_args.index("--expected-generation") + 1] == expected_generation


@pytest.mark.parametrize(
    ("verb", "client_name", "disposition"),
    [
        ("pause", "pause_lode", "paused"),
        ("restart", "restart_lode", "replacement_spawned"),
        ("kill", "kill_lode", "killed_archived"),
    ],
)
def test_remote_parser_preserves_forwarded_generation_over_fresh_snapshot(
    verb, client_name, disposition, monkeypatch
):
    monkeypatch.setenv("HOP_NO_ROUTE", "1")
    lode = {
        "id": "abc12345",
        "active": False,
        "stage": "mill",
        "state": "running",
        "run_generation": "c" * 32,
    }
    with (
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch(
            f"hopper.client.{client_name}",
            return_value={
                "type": "lode_action_ack",
                "outcome": "completed",
                "disposition": disposition,
            },
        ) as operation,
    ):
        assert (
            cmd_lode(
                [
                    verb,
                    "abc12345",
                    "--action-id",
                    "a" * 32,
                    "--expected-generation",
                    "b" * 32,
                ]
            )
            == 0
        )

    assert operation.call_args.kwargs["action_id"] == "a" * 32
    assert operation.call_args.kwargs["expected_generation"] == "b" * 32


def test_manual_action_help_hides_protocol_identity_options(capsys):
    for verb in ("pause", "restart", "kill"):
        assert cmd_lode([verb, "--help"]) == 0
        output = capsys.readouterr().out
        assert "--action-id" not in output
        assert "--expected-generation" not in output


def test_lode_watch_unavailable_at_start(capsys):
    resolution = {
        "outcome": "unavailable",
        "lode": None,
        "host": None,
        "canonical_id": None,
        "error": ("Lode status unavailable for 'abc'. Probes: local=unavailable (server down)."),
        "probe_summary": "local=unavailable (server down)",
        "exit_code": 2,
    }
    with patch("hopper.cli._resolve_lode", return_value=resolution):
        assert cmd_lode(["watch", "abc"]) == 2

    assert capsys.readouterr().out == f"{resolution['error']}\n"


def test_lode_watch_routes_to_streaming_runner_without_stdout_banner(capsys):
    lode = {
        "id": "abc12345",
        "stage": "refine",
        "state": "running",
        "status": "Working",
        "active": True,
    }
    with (
        patch(
            "hopper.cli._resolve_lode",
            return_value=_watch_resolution(lode, "resident.example"),
        ),
        patch("hopper.remote.run_remote_streaming", return_value=7) as stream,
    ):
        assert cmd_lode(["watch", "abc"]) == 7

    stream.assert_called_once_with(
        "resident.example",
        ["lode", "watch", "abc12345"],
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize(
    "argv",
    [
        ["hop", "-H", "resident.example", "lode", "watch", "abc12345"],
        ["hop", "-H", "resident.example", "watch", "abc12345"],
    ],
)
def test_main_explicit_remote_watch_streams_without_banner(monkeypatch, argv, capsys):
    monkeypatch.setattr(sys, "argv", argv)
    with patch("hopper.remote.run_remote_streaming", return_value=0) as stream:
        assert main() == 0

    stream.assert_called_once_with("resident.example", argv[3:])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_lode_watch_archived_error_uses_existing_detail(capsys):
    lode = {
        "id": "abc123",
        "stage": "refine",
        "state": "error",
        "status": "Refine failed",
        "active": False,
        "archived_at": 1234,
    }
    with patch("hopper.cli._resolve_lode", return_value=_watch_resolution(lode)):
        assert cmd_lode(["watch", "abc123"]) == 1

    assert capsys.readouterr().out == (
        "⊘ abc123 refine  Refine failed\n"
        "error: lode abc123 is in error state\n"
        "  stage: refine\n"
        "  status: Refine failed\n"
        "\n"
        "to retry: hop lode restart abc123\n"
    )


def test_lode_watch_archived_non_shipped_is_terminal(capsys):
    lode = {
        "id": "abc123",
        "stage": "refine",
        "state": "ready",
        "status": "Archived by operator",
        "active": False,
        "archived_at": 1234,
    }
    with patch("hopper.cli._resolve_lode", return_value=_watch_resolution(lode)):
        assert cmd_lode(["watch", "abc123"]) == 1

    assert capsys.readouterr().out == (
        "⊘ abc123 refine  Archived by operator\n"
        "Lode 'abc123' is archived and cannot change.\n"
        "Inspect with: hop lode status abc123\n"
    )


def test_lode_watch_post_subscribe_reconcile_observes_archived_error(capsys):
    initial = {
        "id": "abc123",
        "stage": "refine",
        "state": "running",
        "status": "Working",
        "active": True,
    }
    archived = {
        **initial,
        "state": "error",
        "status": "Failed after subscribe",
        "active": False,
        "archived_at": 2345,
    }
    with (
        patch("hopper.cli._resolve_lode", return_value=_watch_resolution(initial)),
        patch(
            "hopper.cli._read_watch_snapshot",
            return_value=("found", archived, "local=found abc123"),
        ),
        patch("hopper.client.HopperConnection", return_value=_watch_connection()),
    ):
        assert cmd_lode(["watch", "abc123"]) == 1

    out = capsys.readouterr().out
    assert "Failed after subscribe" in out
    assert "error: lode abc123 is in error state" in out


def test_lode_watch_observer_loss_exits_after_300_seconds(monkeypatch, capsys):
    initial = {
        "id": "abc123",
        "stage": "refine",
        "state": "running",
        "status": "Working",
        "active": True,
    }
    now = [0.0]

    monkeypatch.setattr("hopper.cli._watch_monotonic", lambda: now[0])

    def condition_wait(condition, timeout_s):
        now[0] += timeout_s

    monkeypatch.setattr("hopper.cli._watch_condition_wait", condition_wait)
    with (
        patch("hopper.cli._resolve_lode", return_value=_watch_resolution(initial)),
        patch(
            "hopper.cli._read_watch_snapshot",
            return_value=(
                "unavailable",
                None,
                "local=unavailable (server did not respond within 2s)",
            ),
        ),
        patch("hopper.client.HopperConnection", return_value=_watch_connection()),
    ):
        assert cmd_lode(["watch", "abc123"]) == 2

    assert now[0] == 300.0
    out = capsys.readouterr().out
    assert "Lode status unavailable for 'abc123'." in out
    assert "local=unavailable (server did not respond within 2s)" in out
    assert "Retry with: hop lode status abc123" in out
    assert "not found" not in out


def test_lode_watch_observer_timeout_starts_after_successful_read(monkeypatch):
    initial = {
        "id": "abc123",
        "stage": "refine",
        "state": "running",
        "status": "Working",
        "active": True,
    }
    clock = [0.0]
    reads = [0]

    monkeypatch.setattr("hopper.cli._watch_monotonic", lambda: clock[0])

    def read_snapshot(socket_path, lode_id):
        reads[0] += 1
        if reads[0] == 1:
            clock[0] += 5.0
            return "found", initial, "local=found abc123"
        return "unavailable", None, "local=unavailable (server down)"

    def condition_wait(condition, timeout_s):
        clock[0] += timeout_s

    monkeypatch.setattr("hopper.cli._watch_condition_wait", condition_wait)
    with (
        patch("hopper.cli._resolve_lode", return_value=_watch_resolution(initial)),
        patch("hopper.cli._read_watch_snapshot", side_effect=read_snapshot),
        patch("hopper.client.HopperConnection", return_value=_watch_connection()),
    ):
        assert cmd_lode(["watch", "abc123"]) == 2

    assert clock[0] == 305.0


def test_routed_watch_forwards_stdout_before_remote_exit(monkeypatch, capsys):
    from hopper.remote import run_remote_streaming

    blocked = threading.Event()
    release = threading.Event()

    class BlockingStdout:
        def __iter__(self):
            yield "● abc123 refine  Working\n"
            blocked.set()
            release.wait(timeout=2)
            yield "✓ abc123 shipped  Done\n"

    class FakeProcess:
        stdout = BlockingStdout()

        def wait(self):
            return 7

        def terminate(self):
            release.set()

    calls = []

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr("hopper.remote.subprocess.Popen", popen)
    result = []
    thread = threading.Thread(
        target=lambda: result.append(
            run_remote_streaming("resident.example", ["lode", "watch", "abc123"])
        )
    )
    thread.start()
    assert blocked.wait(timeout=2)
    assert capsys.readouterr().out == "● abc123 refine  Working\n"
    assert thread.is_alive()
    release.set()
    thread.join(timeout=2)

    assert result == [7]
    assert "PYTHONUNBUFFERED=1" in calls[0][0][7]
    assert "stderr" not in calls[0][1]
    assert capsys.readouterr().out == "✓ abc123 shipped  Done\n"


@pytest.mark.parametrize("legacy", [False, True], ids=["canonical", "legacy"])
def test_lode_path_local_canonical_and_legacy(temp_config, legacy, capsys):
    lode = {"id": "abc12345", "stage": "refine", "state": "running", "active": True}
    if legacy:
        worktree = get_lode_dir(lode["id"]) / "worktree"
    else:
        worktree = config.worktree_root() / lode["id"]
    worktree.mkdir(parents=True)

    with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
        assert cmd_lode(["path", "abc12345"]) == 0

    assert capsys.readouterr().out == f"{worktree.resolve()}\n"


def test_lode_path_remote_plain(capsys):
    lode = {"id": "abc12345", "host": "resident.example"}
    remote_payload = {
        "id": "abc12345",
        "host": "local",
        "path": "/remote/worktrees/abc12345",
        "exists": True,
    }
    with (
        patch(
            "hopper.cli._resolve_lode",
            return_value=_watch_resolution(lode, "resident.example"),
        ),
        patch(
            "hopper.remote.run_remote",
            return_value=subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(remote_payload),
                stderr="",
            ),
        ),
    ):
        assert (
            cmd_lode(
                [
                    "path",
                    "abc",
                ]
            )
            == 0
        )

    assert capsys.readouterr().out == "resident.example:/remote/worktrees/abc12345\n"


def test_lode_path_remote_json_uses_resident_host(capsys):
    lode = {"id": "abc12345", "host": "resident.example"}
    remote_payload = {
        "id": "abc12345",
        "host": "local",
        "path": "/remote/worktrees/abc12345",
        "exists": True,
    }
    with (
        patch(
            "hopper.cli._resolve_lode",
            return_value=_watch_resolution(lode, "resident.example"),
        ),
        patch(
            "hopper.remote.run_remote",
            return_value=subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(remote_payload),
                stderr="",
            ),
        ),
    ):
        assert cmd_lode(["path", "abc", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        **remote_payload,
        "host": "resident.example",
    }


def test_lode_path_json_exact_object(temp_config, capsys):
    lode = {"id": "abc12345", "stage": "refine", "state": "running", "active": True}
    worktree = config.worktree_root() / lode["id"]
    worktree.mkdir(parents=True)
    with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
        assert cmd_lode(["path", "abc12345", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "id": "abc12345",
        "host": "local",
        "path": str(worktree.resolve()),
        "exists": True,
    }


def test_lode_path_absent_candidate_is_never_printed(temp_config, capsys):
    lode = {"id": "abc12345", "stage": "refine", "state": "running", "active": True}
    candidate = config.worktree_root() / lode["id"]
    with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
        assert cmd_lode(["path", "abc12345", "--json"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "outcome": "no_worktree",
        "id": "abc12345",
        "error": "No worktree exists for lode 'abc12345'.",
    }
    assert str(candidate) not in captured.err
    assert "exists:true" not in captured.err


@pytest.mark.parametrize(
    ("outcome", "error", "exit_code"),
    [
        ("absent", "Lode 'abc' not found. Probes: local=absent.", 1),
        (
            "ambiguous",
            "Ambiguous lode prefix 'abc'. Matches: local:abc111, local:abc222. "
            "Probes: local=ambiguous (abc111, abc222).",
            1,
        ),
        (
            "unavailable",
            "Lode status unavailable for 'abc'. Probes: local=unavailable (server down).",
            2,
        ),
    ],
)
def test_lode_path_json_resolution_errors_have_no_stdout(outcome, error, exit_code, capsys):
    resolution = {
        "outcome": outcome,
        "lode": None,
        "host": None,
        "canonical_id": None,
        "error": error,
        "probe_summary": "",
        "exit_code": exit_code,
    }
    with (
        patch("hopper.cli._resolve_lode", return_value=resolution),
        patch("hopper.remote.run_remote") as run_remote,
    ):
        assert cmd_lode(["path", "abc", "--json"]) == exit_code

    run_remote.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    expected = {"outcome": outcome, "query": "abc", "error": error}
    if outcome == "ambiguous":
        expected["matches"] = []
    assert json.loads(captured.err) == expected


@pytest.mark.parametrize(
    "failure",
    [
        OSError("ssh unavailable"),
        subprocess.TimeoutExpired(["ssh"], timeout=8),
    ],
    ids=["os-error", "timeout"],
)
def test_lode_path_remote_spawn_failure_has_no_stdout(failure, capsys):
    lode = {"id": "abc12345", "host": "resident.example"}
    with (
        patch(
            "hopper.cli._resolve_lode",
            return_value=_watch_resolution(lode, "resident.example"),
        ),
        patch("hopper.remote.run_remote", side_effect=failure),
    ):
        assert cmd_lode(["path", "abc", "--json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"Lode path unavailable for 'abc12345' on resident.example: {failure}\n"
    )


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "exit_code", "expected_error"),
    [
        (
            1,
            "",
            json.dumps(
                {
                    "outcome": "no_worktree",
                    "id": "abc12345",
                    "error": "No worktree exists for lode 'abc12345'.",
                }
            ),
            1,
            "No worktree exists for lode 'abc12345' on resident.example.\n",
        ),
        (
            1,
            "",
            json.dumps(
                {
                    "outcome": "absent",
                    "query": "abc12345",
                    "error": "not found",
                }
            ),
            1,
            "Lode 'abc12345' not found on resident.example.\n",
        ),
        (
            7,
            "",
            "remote failure",
            2,
            "Lode path unavailable for 'abc12345' on resident.example: remote command exited 7\n",
        ),
    ],
    ids=["no-worktree", "not-found", "generic"],
)
def test_lode_path_remote_nonzero_has_no_stdout(
    returncode,
    stdout,
    stderr,
    exit_code,
    expected_error,
    capsys,
):
    lode = {"id": "abc12345", "host": "resident.example"}
    result = subprocess.CompletedProcess(
        [],
        returncode,
        stdout=stdout,
        stderr=stderr,
    )
    with (
        patch(
            "hopper.cli._resolve_lode",
            return_value=_watch_resolution(lode, "resident.example"),
        ),
        patch("hopper.remote.run_remote", return_value=result),
    ):
        assert cmd_lode(["path", "abc", "--json"]) == exit_code

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == expected_error


@pytest.mark.parametrize(
    "stdout",
    [
        "{",
        json.dumps(
            {
                "id": "abc12345",
                "host": "local",
                "path": "/remote/worktrees/abc12345",
                "exists": True,
                "extra": "unexpected",
            }
        ),
        json.dumps(
            {
                "id": "different",
                "host": "local",
                "path": "/remote/worktrees/abc12345",
                "exists": True,
            }
        ),
        json.dumps(
            {
                "id": "abc12345",
                "host": "local",
                "path": "/remote/worktrees/abc12345",
                "exists": False,
            }
        ),
    ],
    ids=["malformed", "extra-key", "mismatched-id", "not-existing"],
)
def test_lode_path_rejects_invalid_remote_json_without_path_output(stdout, capsys):
    lode = {"id": "abc12345", "host": "resident.example"}
    result = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
    with (
        patch(
            "hopper.cli._resolve_lode",
            return_value=_watch_resolution(lode, "resident.example"),
        ),
        patch("hopper.remote.run_remote", return_value=result),
    ):
        assert cmd_lode(["path", "abc", "--json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "Lode path unavailable for 'abc12345' on resident.example: invalid remote response\n"
    )
    assert "/remote/worktrees/abc12345" not in captured.err


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["nudge", "abc123"], "continue"),
        (["nudge", "abc123", "focus the test"], "focus the test"),
        (["nudge", "abc123", "--text", "focus the test"], "focus the test"),
        (["nudge", "abc123", "--", "-leading"], "-leading"),
    ],
)
def test_lode_nudge_text_forms(args, expected, capsys):
    lode = {"id": "abc123", "tmux_pane": "%1", "active": True, "state": "running"}
    response = {"type": "pane_input_sent", "lode_id": "abc123", "tmux_pane": "%1"}
    with (
        patch("hopper.cli.require_server", return_value=None),
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch("hopper.client.send_pane_input", return_value=response) as send,
    ):
        assert cmd_lode(args) == 0

    send.assert_called_once_with(ANY, "abc123", expected, paste=True)
    assert capsys.readouterr().out == "submitted\n"


def test_lode_nudge_remote_preserves_adversarial_payload():
    payload = "-quoted ' \" ; $HOME $(touch nope)\nsecond line"
    lode = {"id": "abc12345", "host": "resident.example", "active": True, "state": "running"}
    resolution = {
        "outcome": "found",
        "lode": lode,
        "host": "resident.example",
        "canonical_id": "abc12345",
        "error": None,
        "probe_summary": "",
        "exit_code": 0,
    }
    with (
        patch("hopper.cli._resolve_lode", return_value=resolution),
        patch("hopper.cli._run_remote_cli", return_value=0) as run_remote,
    ):
        assert cmd_lode(["nudge", "abc", "--", payload]) == 0

    assert run_remote.call_args.args[:2] == (
        "resident.example",
        ["lode", "nudge", "abc12345", "--", payload],
    )


def test_lode_nudge_rejects_positional_and_flag_before_lookup(capsys):
    with (
        patch("hopper.cli.require_server") as require,
        patch("hopper.client.read_lode_snapshot") as snapshot,
        patch("hopper.cli._resolve_lode") as resolver,
        patch("hopper.client.send_pane_input") as send,
    ):
        assert cmd_lode(["nudge", "abc", "one", "--text", "two"]) == 1

    require.assert_not_called()
    snapshot.assert_not_called()
    resolver.assert_not_called()
    send.assert_not_called()
    out = capsys.readouterr().out
    assert "positional text and --text cannot be used together" in out
    assert out.count(HELP_SKILL_REMINDER) == 1


def test_top_status_matches_lode_status_human(capsys):
    lode = {
        "id": "abc12345",
        "stage": "refine",
        "state": "running",
        "status": "Working",
        "active": True,
        "project": "project",
        "created_at": 1000,
        "updated_at": 1000,
    }
    with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
        assert cmd_status(["abc12345"]) == 0
        top = capsys.readouterr().out
        assert cmd_lode(["status", "abc12345"]) == 0
        nested = capsys.readouterr().out

    assert top == nested


@pytest.mark.parametrize("host", ["local", "resident.example"])
def test_top_status_matches_lode_status_json(host, capsys):
    lode = {
        "id": "abc12345",
        "host": host,
        "stage": "refine",
        "state": "running",
        "status": "Working",
        "active": True,
        "project": "project",
        "created_at": 1000,
        "updated_at": 1000,
    }
    resolution = _watch_resolution(lode, host)
    with patch("hopper.cli._resolve_lode", return_value=resolution):
        assert cmd_status(["abc12345", "--json"]) == 0
        top = json.loads(capsys.readouterr().out)
        assert cmd_lode(["status", "abc12345", "--json"]) == 0
        nested = json.loads(capsys.readouterr().out)

    assert top == nested
    assert top["host"] == host


def test_status_inside_lode_json(monkeypatch, capsys):
    lode = {
        "id": "abc12345",
        "stage": "refine",
        "state": "running",
        "status": "Working",
        "active": True,
        "project": "project",
    }
    monkeypatch.setenv("HOPPER_LID", "abc12345")
    with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
        assert cmd_status(["--json"]) == 0

    assert json.loads(capsys.readouterr().out)["host"] == "local"


@pytest.mark.parametrize("args", [["--json", "new status"], ["--json", "--title", "new"]])
def test_status_json_rejects_mutation_before_write(monkeypatch, args, capsys):
    monkeypatch.setenv("HOPPER_LID", "abc12345")
    with (
        patch("hopper.cli.require_server") as require,
        patch("hopper.client.read_lode_snapshot") as snapshot,
        patch("hopper.client.set_lode_status") as set_status,
        patch("hopper.client.set_lode_title") as set_title,
    ):
        assert cmd_status(args) == 1

    require.assert_not_called()
    snapshot.assert_not_called()
    set_status.assert_not_called()
    set_title.assert_not_called()
    out = capsys.readouterr().out
    assert "--json cannot be combined" in out
    assert out.count(HELP_SKILL_REMINDER) == 1


# Tests for hop check — validation runner that preserves the command's exit status


@pytest.fixture
def check_server(tmp_path):
    """Run a real server on a temporary socket for hop check heartbeats."""
    socket_path = tmp_path / "check.sock"
    server = Server(socket_path)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    assert server.ready.wait(5), "Server did not start"

    yield server, socket_path

    server.stop()
    thread.join(timeout=2)


def test_check_help(capsys):
    """check --help shows help and returns 0."""
    result = cmd_check(["--help"])
    assert result == 0
    captured = capsys.readouterr()
    assert "usage: hop check" in captured.out


def test_check_refuses_nonterminal_stdout_before_dispatch(monkeypatch, capsys):
    calls = []

    def handler(args):
        calls.append(args)
        return 0

    stdout = MagicMock()
    stdout.isatty.return_value = False
    monkeypatch.setattr(sys, "argv", ["hop", "check", "--", "should-not-run"])
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setitem(hopper_cli.COMMANDS, "check", (handler, "test handler", "commands"))

    assert main() == 2
    assert calls == []
    error = capsys.readouterr().err
    assert "refusing non-terminal stdout" in error
    assert "No validation command was started." in error


def test_check_refusal_names_the_allow_capture_escape(monkeypatch, capsys):
    """The refusal must name an action a captured-stdout caller can actually take.

    Prescribing "run it bare" to an agent whose tool call has no TTY is an
    impossible instruction, and it pushed lodes into detaching the gate instead.
    """

    def handler(args):
        raise AssertionError("handler must not run")

    stdout = MagicMock()
    stdout.isatty.return_value = False
    monkeypatch.setattr(sys, "argv", ["hop", "check", "--", "should-not-run"])
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setitem(hopper_cli.COMMANDS, "check", (handler, "test handler", "commands"))

    assert main() == 2
    assert "--allow-capture" in capsys.readouterr().err


def test_check_allow_capture_dispatches_with_nonterminal_stdout(monkeypatch):
    """--allow-capture is the opt-in for a caller that propagates the exit code."""
    calls = []

    def handler(args):
        calls.append(args)
        return 7

    stdout = MagicMock()
    stdout.isatty.return_value = False
    monkeypatch.setattr(sys, "argv", ["hop", "check", "--allow-capture", "--", "command"])
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setitem(hopper_cli.COMMANDS, "check", (handler, "test handler", "commands"))

    assert main() == 7
    assert calls == [["--allow-capture", "--", "command"]]


def test_check_allow_capture_after_separator_does_not_bypass_the_guard(monkeypatch, capsys):
    """Everything after `--` belongs to the command, not to hop.

    `hop check -- make ci --allow-capture` must still refuse; otherwise the
    guard is defeated by any payload that happens to carry the flag.
    """

    def handler(args):
        raise AssertionError("handler must not run")

    stdout = MagicMock()
    stdout.isatty.return_value = False
    monkeypatch.setattr(sys, "argv", ["hop", "check", "--", "make", "ci", "--allow-capture"])
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setitem(hopper_cli.COMMANDS, "check", (handler, "test handler", "commands"))

    assert main() == 2
    assert "refusing non-terminal stdout" in capsys.readouterr().err


def test_check_help_after_separator_does_not_bypass_the_guard(monkeypatch, capsys):
    """The same payload-vs-own-flag split applies to --help."""

    def handler(args):
        raise AssertionError("handler must not run")

    stdout = MagicMock()
    stdout.isatty.return_value = False
    monkeypatch.setattr(sys, "argv", ["hop", "check", "--", "make", "--help"])
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setitem(hopper_cli.COMMANDS, "check", (handler, "test handler", "commands"))

    assert main() == 2
    assert "refusing non-terminal stdout" in capsys.readouterr().err


def test_check_allow_capture_returns_the_real_exit_status(tmp_path):
    """With the opt-in, a failing command's status reaches a captured caller."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from hopper.cli import main; raise SystemExit(main())",
            "check",
            "-n",
            "5",
            "--allow-capture",
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 7


def test_check_dispatches_with_terminal_stdout(monkeypatch):
    calls = []

    def handler(args):
        calls.append(args)
        return 7

    stdout = MagicMock()
    stdout.isatty.return_value = True
    monkeypatch.setattr(sys, "argv", ["hop", "check", "--", "command"])
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setitem(hopper_cli.COMMANDS, "check", (handler, "test handler", "commands"))

    assert main() == 7
    assert calls == [["--", "command"]]


def test_check_refusal_stops_producer_in_a_backgrounded_pipe(tmp_path):
    """A piped background check must not run a producer its caller cannot observe."""
    marker = tmp_path / "producer-ran"
    producer = [
        sys.executable,
        "-c",
        "from hopper.cli import main; raise SystemExit(main())",
        "check",
        "--",
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
    ]
    result = subprocess.run(
        ["bash", "-c", f"{shlex.join(producer)} | cat & wait"],
        capture_output=True,
        text=True,
        timeout=5,
    )

    # Bash reports the final pipe stage even when the backgrounded check refuses.
    assert result.returncode == 0
    assert not marker.exists()
    assert "refusing non-terminal stdout" in result.stderr
    assert "No validation command was started." in result.stderr


def _noisy_failing_cmd(lines: int, code: int) -> list[str]:
    """A portable command that prints `lines` numbered lines then exits `code`."""
    return [
        sys.executable,
        "-c",
        f"import sys\nfor i in range({lines}): print(i)\nsys.exit({code})",
    ]


def test_check_preserves_failure_exit_status_when_truncated(capsys):
    """The regression: a red command truncated to its tail must still exit non-zero.

    This is the exact false-green the ship stage hit — `make ci 2>&1 | tail -30`
    returned 0 because tail succeeded. `hop check` truncates the output but
    reports the producer's real status, so a red build cannot look green.
    """
    result = cmd_check(["-n", "5", "--", *_noisy_failing_cmd(100, 1)])
    assert result == 1  # producer exit status preserved, not tail's 0
    captured = capsys.readouterr()
    assert "99" in captured.out  # tail is present
    assert "94" not in captured.out  # earlier output was truncated away
    assert "exited 1" in captured.err  # failure surfaced explicitly
    assert "showing last 5 of 100 lines" in captured.err


def test_check_passes_through_success(capsys):
    """A green command exits 0 and its output is shown."""
    result = cmd_check(["--", sys.executable, "-c", "print('all good')"])
    assert result == 0
    captured = capsys.readouterr()
    assert "all good" in captured.out
    assert "exited 0" in captured.err


def test_check_redirects_output_to_a_file_not_an_inheritable_pipe(monkeypatch, capsys):
    observed = {}

    def launch(command, **kwargs):
        observed.update(kwargs)
        kwargs["stdout"].write("parent done\n")
        proc = MagicMock(pid=42, returncode=0)
        return proc

    monkeypatch.setattr("hopper.cli.subprocess.Popen", launch)

    assert cmd_check(["--", "command"]) == 0
    assert observed["stdout"] != subprocess.PIPE
    assert observed["stderr"] == subprocess.STDOUT
    assert capsys.readouterr().out == "parent done\n"


def test_check_preserves_nonzero_exit_code_value(capsys):
    """The specific non-zero code is passed through, not collapsed to 1."""
    result = cmd_check(["--", *_noisy_failing_cmd(3, 7)])
    assert result == 7
    assert "exited 7" in capsys.readouterr().err


def test_check_progress_surfaces_sustained_process_tree_cpu_silence(monkeypatch):
    progress = _CheckProgress("make ci", started_at=1_000)
    progress.bind(42)
    readings = iter([100, 100, 101])
    monkeypatch.setattr("hopper.cli._sum_process_tree_cpu_ms", lambda pid: next(readings))

    assert progress.summary(31_000) == "make ci — running 30s"
    assert progress.summary(91_000) == (
        "make ci — running 1m30s; no process-tree CPU progress for 1m00s"
    )
    assert progress.summary(121_000) == "make ci — running 2m00s"


def test_check_requires_a_command(capsys):
    """No command is a usage error, not a silent success."""
    result = cmd_check([])
    assert result == 1
    assert "no command" in capsys.readouterr().out


def test_check_command_not_found_returns_127(capsys):
    """A missing executable returns 127 rather than a false 0."""
    result = cmd_check(["--", "hopper-no-such-command-xyzzy"])
    assert result == 127
    assert "not found" in capsys.readouterr().err


def test_check_heartbeats_over_real_socket_and_stops_after_child(
    check_server, make_lode, monkeypatch, capsys
):
    """A pane-silent child reports progress only while it is running."""
    server, socket_path = check_server
    generation = "a" * 32
    lode = make_lode(
        id="check-id",
        state="running",
        active=True,
        run_generation=generation,
    )
    server.lodes = [lode]
    save_lodes(server.lodes)
    monkeypatch.setenv("HOPPER_LID", "check-id")
    monkeypatch.setenv(RUN_GENERATION_ENV, generation)
    monkeypatch.setattr("hopper.cli._socket", lambda: socket_path)
    monkeypatch.setattr("hopper.code.HEARTBEAT_INTERVAL_SEC", 0.05)
    command = [sys.executable, "-c", "import time; time.sleep(0.25); print('done')"]

    result = cmd_check(["--", *command])

    assert result == 0
    captured = capsys.readouterr()
    assert captured.out == "done\n"
    assert f"hop check: `{' '.join(command)}` exited 0" in captured.err
    deadline = time.monotonic() + 1
    while server.lodes[0].get("last_progress_at") is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.lodes[0]["last_progress_at"] is not None
    expected_prefix = hopper_code.truncate_progress_command(" ".join(command))
    assert server.lodes[0]["last_progress_summary"].startswith(f"{expected_prefix} — running ")

    # Drain any mutation already queued before stop() returned, then verify silence.
    time.sleep(0.05)
    last_progress_at = server.lodes[0]["last_progress_at"]
    time.sleep(0.12)
    assert server.lodes[0]["last_progress_at"] == last_progress_at


def test_check_without_lode_does_not_construct_heartbeat(monkeypatch, capsys):
    class UnexpectedHeartbeat:
        def __init__(self, *args, **kwargs):
            pytest.fail("heartbeat constructed without HOPPER_LID")

    monkeypatch.setattr("hopper.cli.hopper_code.ProgressHeartbeat", UnexpectedHeartbeat)

    assert cmd_check(["--", sys.executable, "-c", "print('all good')"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "all good\n"
    assert "exited 0" in captured.err


def test_check_dead_socket_preserves_command_contract(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOPPER_LID", "check-id")
    monkeypatch.setattr("hopper.cli._socket", lambda: tmp_path / "missing.sock")
    monkeypatch.setattr("hopper.code.HEARTBEAT_INTERVAL_SEC", 0.01)
    command = [sys.executable, "-c", "import time; time.sleep(0.05); print('all good')"]

    assert cmd_check(["--", *command]) == 0
    captured = capsys.readouterr()
    assert captured.out == "all good\n"
    assert captured.err == f"hop check: `{' '.join(command)}` exited 0\n"


def test_check_heartbeat_construction_failure_preserves_contract(monkeypatch, capsys):
    class BrokenHeartbeat:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("construction failed")

    monkeypatch.setenv("HOPPER_LID", "check-id")
    monkeypatch.setattr("hopper.cli.hopper_code.ProgressHeartbeat", BrokenHeartbeat)
    command = [sys.executable, "-c", "print('all good')"]

    assert cmd_check(["--", *command]) == 0
    captured = capsys.readouterr()
    assert captured.out == "all good\n"
    assert captured.err == f"hop check: `{' '.join(command)}` exited 0\n"


def test_check_heartbeat_lifecycle_failures_preserve_contract(monkeypatch, capsys):
    class BrokenHeartbeat:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("start failed")

        def stop(self):
            raise RuntimeError("stop failed")

    monkeypatch.setenv("HOPPER_LID", "check-id")
    monkeypatch.setattr("hopper.cli.hopper_code.ProgressHeartbeat", BrokenHeartbeat)
    command = [sys.executable, "-c", "print('all good')"]

    assert cmd_check(["--", *command]) == 0
    captured = capsys.readouterr()
    assert captured.out == "all good\n"
    assert captured.err == f"hop check: `{' '.join(command)}` exited 0\n"


def test_check_heartbeat_emit_failure_preserves_contract(monkeypatch, capsys):
    monkeypatch.setenv("HOPPER_LID", "check-id")
    monkeypatch.setattr("hopper.code.HEARTBEAT_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(
        "hopper.cli.set_lode_progress", MagicMock(side_effect=RuntimeError("emit failed"))
    )
    command = [sys.executable, "-c", "import time; time.sleep(0.05); print('all good')"]

    assert cmd_check(["--", *command]) == 0
    captured = capsys.readouterr()
    assert captured.out == "all good\n"
    assert captured.err == f"hop check: `{' '.join(command)}` exited 0\n"


def test_check_command_not_found_stops_heartbeat(monkeypatch, capsys):
    stopped = []

    class TrackingHeartbeat:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            stopped.append(True)

    monkeypatch.setenv("HOPPER_LID", "check-id")
    monkeypatch.setattr("hopper.cli.hopper_code.ProgressHeartbeat", TrackingHeartbeat)

    assert cmd_check(["--", "hopper-no-such-command-heartbeat"]) == 127
    assert stopped == [True]
    assert "not found" in capsys.readouterr().err


# --- unpushed-commit guard on kill -------------------------------------------


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def lode_worktree(temp_config, monkeypatch):
    """Build a real clone plus lode worktree at the path hopper would use."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)

    root = temp_config / "git"
    root.mkdir()
    remote = root / "origin.git"
    _git(root, "init", "--bare", "-b", "main", str(remote))

    seed = root / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Test User")
    (seed / "README.md").write_text("init\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "init")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")

    clone = root / "clone"
    _git(root, "clone", str(remote), str(clone))
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test User")

    worktree = get_worktree_dir("test1234")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(clone, "worktree", "add", str(worktree), "-b", "hopper-test1234")

    def add_commits(n):
        for i in range(n):
            (worktree / f"w{i}.txt").write_text(str(i))
            _git(worktree, "add", ".")
            _git(worktree, "commit", "-m", f"work {i}")

    return worktree, add_commits


def _kill(lode_id, extra=None):
    lode = {
        "id": "test1234",
        "stage": "mill",
        "state": "running",
        "active": True,
        "branch": "hopper-test1234",
        "run_generation": "a" * 32,
    }
    with (
        patch("hopper.cli.require_server", return_value=None),
        patch("hopper.client.read_lode_snapshot", return_value=("found", lode)),
        patch(
            "hopper.client.kill_lode",
            return_value={
                "type": "lode_action_ack",
                "outcome": "completed",
                "disposition": "killed_archived",
            },
        ) as mock_kill,
    ):
        rc = cmd_lode(["kill", lode_id, *(extra or [])])
    return rc, mock_kill


def test_lode_kill_submits_unpushed_safety_to_server(lode_worktree, capsys):
    _worktree, add_commits = lode_worktree
    add_commits(11)

    rc, mock_kill = _kill("test1234")

    assert rc == 0
    mock_kill.assert_called_once()
    assert "Killed lode test1234" in capsys.readouterr().out


def test_lode_kill_force_is_forwarded_as_durability_consent(lode_worktree, capsys):
    _worktree, add_commits = lode_worktree
    add_commits(2)

    rc, mock_kill = _kill("test1234", ["--force"])

    assert rc == 0
    mock_kill.assert_called_once()
    assert mock_kill.call_args.kwargs["force"] is True
    assert "Refusing to kill" not in capsys.readouterr().out


# --- status surfaces that would have made the stall visible ------------------


@pytest.mark.parametrize(
    ("unpushed", "expected"),
    [
        (
            {"count": 11, "basis": "a remote branch"},
            "  unpushed: 11 commits exist ONLY here",
        ),
        (
            {"count": 1, "basis": "a remote branch"},
            "  unpushed: 1 commit exists ONLY here",
        ),
        (
            {"count": 0, "basis": "a remote branch"},
            "  unpushed: none — every commit is on a remote branch",
        ),
        (
            {"count": 0, "basis": "main"},
            "  unpushed: none — every commit is on main",
        ),
        (
            {"count": None, "basis": "a remote branch"},
            "  unpushed: UNKNOWN — could not check this worktree for unpushed commits",
        ),
    ],
)
def test_format_lode_detail_reports_unpushed_commits(make_lode, unpushed, expected):
    lode = make_lode(id="test1234", branch="hopper-test1234", unpushed=unpushed)

    assert expected in format_lode_detail(lode)


def test_format_lode_detail_omits_unpushed_without_a_worktree(make_lode):
    assert "unpushed:" not in format_lode_detail(make_lode(id="test1234"))


def test_lode_status_annotates_unpushed_from_the_owning_host(lode_worktree, capsys, make_lode):
    _worktree, add_commits = lode_worktree
    add_commits(3)
    lode = make_lode(id="test1234", branch="hopper-test1234")

    with patch("hopper.client.read_lode_snapshot", return_value=("found", lode)):
        assert cmd_lode(["status", "test1234"]) == 0

    assert "  unpushed: 3 commits exist ONLY here" in capsys.readouterr().out


def test_progress_line_carries_its_age(make_lode):
    lode = make_lode(
        id="test1234",
        last_progress_summary="make ci — running 8m30s",
        last_progress_at=current_time_ms() - 2 * 60 * 60 * 1000,
    )

    detail = format_lode_detail(lode)

    assert "  progress: make ci — running 8m30s (2h ago)" in detail


def test_progress_line_without_a_timestamp_stays_bare(make_lode):
    lode = make_lode(id="test1234", last_progress_summary="make ci — running 8m30s")

    assert "  progress: make ci — running 8m30s\n" in format_lode_detail(lode) + "\n"


def test_park_record_renders_as_a_park_not_an_empty_failure(make_lode):
    """The park record is the durable proof the lifecycle stopped."""
    recovery = {
        "parked_at": current_time_ms() - 5 * 60 * 1000,
        "state": "gated",
        "stage": "mill",
        "reason": "completion was signalled, but the agent did not exit within 5 min",
        "branch": "hopper-test1234",
        "worktree_path": "/tmp/worktree",
        "terminated": False,
    }
    lode = make_lode(id="test1234", state="running", recovery=recovery)

    detail = format_lode_detail(lode)

    assert "    parked:    5m ago" in detail
    assert "    agent:     alive, NOT terminated" in detail
    assert "    stage:     mill" in detail
    assert "outcome:" not in detail
    assert "failed_at:" not in detail


def test_ages_under_a_minute_do_not_read_as_now_ago(make_lode):
    now = current_time_ms()
    lode = make_lode(
        id="test1234",
        last_progress_summary="make ci — running 4m00s",
        last_progress_at=now,
        last_pane_activity_at=now,
    )

    detail = format_lode_detail(lode)

    assert "  progress: make ci — running 4m00s (now)" in detail
    assert "  activity: now" in detail.splitlines()
    assert "now ago" not in detail
