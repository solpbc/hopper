# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the hopper CLI."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from hopper import __version__
from hopper.cli import (
    cmd_backlog,
    cmd_code,
    cmd_config,
    cmd_lode,
    cmd_ping,
    cmd_process,
    cmd_processed,
    cmd_screenshot,
    cmd_status,
    cmd_up,
    detect_coding_agent,
    get_hopper_lid,
    main,
    require_config_name,
    require_no_server,
    require_not_coding_agent,
    require_server,
    validate_hopper_lid,
)


@pytest.fixture(autouse=True)
def clear_hopper_lid_env(monkeypatch):
    """Default tests to not running inside a lode unless explicitly set."""
    monkeypatch.delenv("HOPPER_LID", raising=False)


def test_main_is_callable():
    assert callable(main)


# Tests for help and version


def test_no_args_shows_help(capsys):
    """No arguments shows help and returns 0."""
    with patch.object(sys, "argv", ["hopper"]):
        result = main()
    assert result == 0
    captured = capsys.readouterr()
    assert "Usage:" in captured.out
    assert "Commands:" in captured.out


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


# Tests for subcommand help


def test_ping_help(capsys):
    """ping --help shows help and returns 0."""
    result = cmd_ping(["--help"])
    assert result == 0
    captured = capsys.readouterr()
    assert "usage: hop ping" in captured.out
    assert "Check if the hopper server is running" in captured.out


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
        with patch("hopper.process.run_process", return_value=0) as mock_run:
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
            with patch("hopper.client.ping", return_value=True):
                result = main()
    assert result == 1
    captured = capsys.readouterr()
    assert "Server already running" in captured.out


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
    with patch("hopper.client.ping", return_value=True):
        result = require_server()
    assert result is None


def test_require_server_failure(capsys):
    """require_server returns 1 when server not running."""
    with patch("hopper.client.ping", return_value=False):
        result = require_server()
    assert result == 1
    captured = capsys.readouterr()
    assert "Server not running" in captured.out
    assert "hop up" in captured.out


# Tests for require_no_server


def test_require_no_server_success():
    """require_no_server returns None when server is not running."""
    with patch("hopper.client.ping", return_value=False):
        result = require_no_server()
    assert result is None


def test_require_no_server_failure(capsys):
    """require_no_server returns 1 when server is running."""
    with patch("hopper.client.ping", return_value=True):
        result = require_no_server()
    assert result == 1
    captured = capsys.readouterr()
    assert "Server already running" in captured.out


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


def test_status_no_server(capsys):
    """status command returns 1 when server not running."""
    with patch("hopper.client.ping", return_value=False):
        result = cmd_status([])
    assert result == 1
    captured = capsys.readouterr()
    assert "Server not running" in captured.out


def test_status_no_hopper_lid(capsys):
    """status command returns 1 when HOPPER_LID not set."""
    env = os.environ.copy()
    env.pop("HOPPER_LID", None)
    with patch.dict(os.environ, env, clear=True):
        with patch("hopper.client.ping", return_value=True):
            result = cmd_status([])
    assert result == 1
    captured = capsys.readouterr()
    assert "HOPPER_LID not set" in captured.out


def test_status_invalid_session(capsys):
    """status command returns 1 when session doesn't exist."""
    with patch.dict(os.environ, {"HOPPER_LID": "bad-session"}):
        with patch("hopper.client.ping", return_value=True):
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
        with patch("hopper.client.ping", return_value=True):
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
        with patch("hopper.client.ping", return_value=True):
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
        with patch("hopper.client.ping", return_value=True):
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
        with patch("hopper.client.ping", return_value=True):
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
        with patch("hopper.client.ping", return_value=True):
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
        with patch("hopper.client.ping", return_value=True):
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
        with patch("hopper.client.ping", return_value=True):
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
        with patch("hopper.client.ping", return_value=True):
            with patch("hopper.client.lode_exists", return_value=True):
                result = cmd_status(["", "  "])
    assert result == 1
    captured = capsys.readouterr()
    assert "Status text required" in captured.out


# --- cmd_backlog tests ---


def test_backlog_add_reads_description_from_stdin(capsys):
    """backlog add accepts description from stdin when text args are omitted."""
    from io import StringIO

    with patch("hopper.client.ping", return_value=False):
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


# --- cmd_lode tests ---


def test_lode_help(capsys):
    """--help prints usage and exits."""
    assert cmd_lode(["--help"]) == 0
    out = capsys.readouterr().out
    assert "list" in out
    assert "create" in out


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


def test_lode_create_happy(capsys):
    """Create sends correct message and prints confirmation."""
    created_lode = {"id": "abc12345", "project": "myproj", "stage": "mill"}
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.projects.find_project", return_value=object()):
            with patch("hopper.client.create_lode", return_value=created_lode) as mock_create:
                assert cmd_lode(["create", "myproj", "fix", "the", "bug"]) == 0
                mock_create.assert_called_once()
                assert mock_create.call_args.kwargs["spawn"] is True
    out = capsys.readouterr().out
    assert "abc12345" in out
    assert "myproj" in out


def test_lode_create_rejects_inside_lode(monkeypatch, capsys):
    monkeypatch.setenv("HOPPER_LID", "test-lode-123")

    rc = cmd_lode(["create", "proj", "scope"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "Cannot run this command inside lode test-lode-123." in out
    assert "hop backlog add" in out


def test_lode_create_missing_project(capsys):
    """Create with no project arg reports missing required argument."""
    assert cmd_lode(["create"]) == 1
    out = capsys.readouterr().out
    assert "required" in out


def test_lode_create_reads_scope_from_stdin(capsys):
    """Create accepts scope from stdin when positional scope is omitted."""
    from io import StringIO

    created_lode = {"id": "abc12345", "project": "myproj", "stage": "mill"}
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.projects.find_project", return_value=object()):
            with patch("hopper.client.create_lode", return_value=created_lode) as mock_create:
                with patch("sys.stdin", StringIO("stdin scope")):
                    assert cmd_lode(["create", "myproj"]) == 0
                mock_create.assert_called_once()
                assert mock_create.call_args.args[2] == "stdin scope"


def test_lode_create_missing_scope(capsys):
    """Create with no positional scope and empty stdin returns a helpful error."""
    from io import StringIO

    with patch("sys.stdin", StringIO("")):
        assert cmd_lode(["create", "myproj"]) == 1
    out = capsys.readouterr().out
    assert "Error: no scope provided" in out


def test_lode_create_invalid_project(capsys):
    """Create with unknown project prints error."""
    with patch("hopper.projects.find_project", return_value=None):
        assert cmd_lode(["create", "badproj", "some scope"]) == 1
    out = capsys.readouterr().out
    assert "not found" in out.lower()


def test_lode_restart_happy(capsys):
    """Restart sends correct message and prints confirmation."""
    lode = {"id": "test1234", "stage": "mill", "state": "new", "active": False}
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.get_lode", return_value=lode):
            with patch("hopper.client.restart_lode", return_value=True) as mock_restart:
                assert cmd_lode(["restart", "test1234"]) == 0
                mock_restart.assert_called_once()
    out = capsys.readouterr().out
    assert "test1234" in out
    assert "mill" in out


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
        with patch("hopper.client.get_lode", return_value=None):
            assert cmd_lode(["restart", "bad_id"]) == 1
    out = capsys.readouterr().out
    assert "not found" in out.lower()


def test_lode_restart_active(capsys):
    """Restart of active lode prints error."""
    lode = {"id": "test1234", "stage": "mill", "state": "running", "active": True}
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.get_lode", return_value=lode):
            assert cmd_lode(["restart", "test1234"]) == 1
    out = capsys.readouterr().out
    assert "active" in out.lower()


def test_lode_restart_shipped(capsys):
    """Restart of shipped lode prints error."""
    lode = {"id": "test1234", "stage": "shipped", "state": "shipped", "active": False}
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.get_lode", return_value=lode):
            assert cmd_lode(["restart", "test1234"]) == 1
    out = capsys.readouterr().out
    assert "shipped" in out.lower()


def test_lode_restart_missing_id(capsys):
    """Restart with no lode ID reports missing required argument."""
    assert cmd_lode(["restart"]) == 1
    out = capsys.readouterr().out
    assert "required" in out


def test_lode_watch_happy_shipped(capsys):
    """watch exits 0 when lode reaches shipped stage."""
    lode = {
        "id": "abc123",
        "stage": "refine",
        "state": "running",
        "status": "Working...",
        "active": True,
    }
    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.get_lode", return_value=lode):
            mock_conn = MagicMock()

            def fake_start(callback, on_connect=None):
                callback(
                    {"type": "lode_updated", "lode": {**lode, "stage": "shipped", "status": "Done"}}
                )

            mock_conn.start = fake_start
            with patch("hopper.client.HopperConnection", return_value=mock_conn):
                result = cmd_lode(["watch", "abc123"])
    assert result == 0
    out = capsys.readouterr().out
    assert "abc123" in out
    assert "shipped" in out


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
    """watch exits 1 when lode enters error state."""
    lode = {
        "id": "abc123",
        "stage": "mill",
        "state": "running",
        "status": "Working",
        "active": True,
    }
    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.get_lode", return_value=lode):
            mock_conn = MagicMock()

            def fake_start(callback, on_connect=None):
                callback(
                    {"type": "lode_updated", "lode": {**lode, "state": "error", "status": "Failed"}}
                )

            mock_conn.start = fake_start
            with patch("hopper.client.HopperConnection", return_value=mock_conn):
                result = cmd_lode(["watch", "abc123"])
    assert result == 1


def test_lode_watch_archived_exit(capsys):
    """watch exits 0 when lode is archived."""
    lode = {
        "id": "abc123",
        "stage": "shipped",
        "state": "ready",
        "status": "Shipped",
        "active": True,
    }
    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.get_lode", return_value=lode):
            mock_conn = MagicMock()

            def fake_start(callback, on_connect=None):
                callback({"type": "lode_archived", "lode": {**lode, "active": False}})

            mock_conn.start = fake_start
            with patch("hopper.client.HopperConnection", return_value=mock_conn):
                result = cmd_lode(["watch", "abc123"])
    assert result == 0


def test_lode_watch_not_found(capsys):
    """watch fails when lode not found."""
    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.get_lode", return_value=None):
            result = cmd_lode(["watch", "bogus"])
    assert result == 1
    assert "not found" in capsys.readouterr().out


def test_lode_watch_not_active(capsys):
    """watch fails when lode is not active."""
    lode = {"id": "abc123", "active": False, "stage": "mill", "state": "new", "status": ""}
    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.get_lode", return_value=lode):
            result = cmd_lode(["watch", "abc123"])
    assert result == 1
    assert "not active" in capsys.readouterr().out


def test_lode_watch_initial_state(capsys):
    """watch prints initial lode state before streaming."""
    lode = {
        "id": "abc123",
        "stage": "mill",
        "state": "running",
        "status": "Starting",
        "active": True,
    }
    with patch("hopper.cli.require_server", return_value=0):
        with patch("hopper.client.get_lode", return_value=lode):
            mock_conn = MagicMock()

            def fake_start(callback, on_connect=None):
                callback(
                    {"type": "lode_updated", "lode": {**lode, "stage": "shipped", "status": "Done"}}
                )

            mock_conn.start = fake_start
            with patch("hopper.client.HopperConnection", return_value=mock_conn):
                cmd_lode(["watch", "abc123"])
    out = capsys.readouterr().out
    lines = out.strip().split("\n")
    assert len(lines) >= 2  # initial + at least one update
    assert "Starting" in lines[0]  # initial state
    assert "shipped" in lines[-1]  # final state


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


def test_config_delete_missing(capsys):
    """config delete on missing key returns error."""
    result = cmd_config(["delete", "nope"])
    assert result == 1
    captured = capsys.readouterr()
    assert "not set" in captured.out


def test_config_delete_missing_key_arg(capsys):
    """config delete without a key shows error."""
    result = cmd_config(["delete"])
    assert result == 1
    captured = capsys.readouterr()
    assert "key required" in captured.out


def test_config_delete_complex_blocked(temp_config, capsys):
    """config delete refuses to delete complex values."""
    import json

    config_file = temp_config / "config.json"
    config_file.write_text(json.dumps({"projects": [{"path": "/tmp", "name": "x"}]}))

    result = cmd_config(["delete", "projects"])
    assert result == 1
    captured = capsys.readouterr()
    assert "Cannot delete complex key" in captured.out


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
    ]
    monkeypatch.setattr("hopper.projects.load_projects", lambda: projects)
    result = cmd_project(["list"])
    assert result == 0
    captured = capsys.readouterr()
    assert "foo" in captured.out
    assert "/path/to/foo" in captured.out
    assert "bar" in captured.out
    assert "(disabled)" in captured.out


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


# Tests for screenshot command


def test_screenshot_help(capsys):
    """screenshot --help shows help and returns 0."""
    result = cmd_screenshot(["--help"])
    assert result == 0
    captured = capsys.readouterr()
    assert "usage: hop screenshot" in captured.out


def test_screenshot_no_server(capsys):
    """screenshot returns 1 when server not running."""
    with patch("hopper.client.ping", return_value=False):
        result = cmd_screenshot([])
    assert result == 1
    captured = capsys.readouterr()
    assert "Server not running" in captured.out


def test_screenshot_no_tmux_location(capsys):
    """screenshot returns 1 when server has no tmux location."""
    mock_response = {"type": "connected", "tmux": None}
    with patch("hopper.client.ping", return_value=True):
        with patch("hopper.client.connect", return_value=mock_response):
            result = cmd_screenshot([])
    assert result == 1
    captured = capsys.readouterr()
    assert "not started inside tmux" in captured.out


def test_screenshot_capture_fails(capsys):
    """screenshot returns 1 when capture_pane fails."""
    mock_response = {"type": "connected", "tmux": {"lode": "main", "pane": "%0"}}
    with patch("hopper.client.ping", return_value=True):
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
    with patch("hopper.client.ping", return_value=True):
        with patch("hopper.client.connect", return_value=mock_response):
            with patch("hopper.tmux.capture_pane", return_value=ansi_content):
                result = cmd_screenshot([])
    assert result == 0
    captured = capsys.readouterr()
    assert captured.out == ansi_content


# Tests for processed command


def test_processed_help(capsys):
    """processed --help shows help and returns 0."""
    result = cmd_processed(["--help"])
    assert result == 0
    captured = capsys.readouterr()
    assert "usage: hop processed" in captured.out


def test_processed_no_server(capsys):
    """processed returns 1 when server not running."""
    with patch("hopper.client.ping", return_value=False):
        result = cmd_processed([])
    assert result == 1
    captured = capsys.readouterr()
    assert "Server not running" in captured.out


def test_processed_no_hopper_lid(capsys):
    """processed returns 1 when HOPPER_LID not set."""
    env = os.environ.copy()
    env.pop("HOPPER_LID", None)
    with patch.dict(os.environ, env, clear=True):
        with patch("hopper.client.ping", return_value=True):
            result = cmd_processed([])
    assert result == 1
    captured = capsys.readouterr()
    assert "HOPPER_LID not set" in captured.out


def test_processed_invalid_session(capsys):
    """processed returns 1 when session doesn't exist."""
    with patch.dict(os.environ, {"HOPPER_LID": "bad-session"}):
        with patch("hopper.client.ping", return_value=True):
            with patch("hopper.client.lode_exists", return_value=False):
                result = cmd_processed([])
    assert result == 1
    captured = capsys.readouterr()
    assert "bad-session" in captured.out
    assert "not found or archived" in captured.out


def test_processed_empty_stdin(capsys):
    """processed returns 1 on empty stdin."""
    from io import StringIO

    lode_data = {"id": "test-session", "stage": "mill"}
    with patch.dict(os.environ, {"HOPPER_LID": "test-session"}):
        with patch("hopper.client.ping", return_value=True):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.get_lode", return_value=lode_data):
                    with patch("sys.stdin", StringIO("")):
                        result = cmd_processed([])
    assert result == 1
    captured = capsys.readouterr()
    assert "No input received" in captured.out


def test_processed_saves_file(temp_config, capsys):
    """processed saves output to lode directory and updates state."""
    from io import StringIO

    lode_id = "test-session-1234"
    lode_dir = temp_config / "lodes" / lode_id
    output_text = "# Mill output\n\nDo the thing.\n"
    lode_data = {"id": lode_id, "stage": "mill"}

    with patch.dict(os.environ, {"HOPPER_LID": lode_id}):
        with patch("hopper.client.ping", return_value=True):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.get_lode", return_value=lode_data):
                    with patch("hopper.client.set_lode_state", return_value=True) as mock_set:
                        with patch("sys.stdin", StringIO(output_text)):
                            result = cmd_processed([])

    assert result == 0
    captured = capsys.readouterr()
    assert "Saved to" in captured.out

    # Verify file was written as <stage>_out.md
    output_path = lode_dir / "mill_out.md"
    assert output_path.exists()
    assert output_path.read_text() == output_text

    # Verify state was updated: set_lode_state(socket_path, lode_id, state, status)
    mock_set.assert_called_once()
    _, sid, state, status = mock_set.call_args[0]
    assert sid == lode_id
    assert state == "completed"
    assert "complete" in status.lower()


def test_processed_no_stage(capsys):
    """processed returns 1 when lode has no stage."""
    lode_data = {"id": "test-session", "stage": ""}
    with patch.dict(os.environ, {"HOPPER_LID": "test-session"}):
        with patch("hopper.client.ping", return_value=True):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.get_lode", return_value=lode_data):
                    result = cmd_processed([])
    assert result == 1
    captured = capsys.readouterr()
    assert "no stage" in captured.out


def test_processed_refine_stage(temp_config, capsys):
    """processed saves refine_out.md for refine stage."""
    from io import StringIO

    lode_id = "test-refine-1234"
    lode_dir = temp_config / "lodes" / lode_id
    output_text = "# Refine summary\n\nFeature implemented.\n"
    lode_data = {"id": lode_id, "stage": "refine"}

    with patch.dict(os.environ, {"HOPPER_LID": lode_id}):
        with patch("hopper.client.ping", return_value=True):
            with patch("hopper.client.lode_exists", return_value=True):
                with patch("hopper.client.get_lode", return_value=lode_data):
                    with patch("hopper.client.set_lode_state", return_value=True) as mock_set:
                        with patch("sys.stdin", StringIO(output_text)):
                            result = cmd_processed([])

    assert result == 0

    # Verify file was written as refine_out.md
    output_path = lode_dir / "refine_out.md"
    assert output_path.exists()
    assert output_path.read_text() == output_text

    # Verify state: "Refine complete"
    mock_set.assert_called_once()
    _, sid, state, status = mock_set.call_args[0]
    assert sid == lode_id
    assert state == "completed"
    assert "Refine complete" in status


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
