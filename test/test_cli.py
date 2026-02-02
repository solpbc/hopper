"""Tests for the hopper CLI."""

import os
import sys
from unittest.mock import patch

from hopper import __version__
from hopper.cli import (
    cmd_ore,
    cmd_ping,
    cmd_status,
    cmd_up,
    get_hopper_sid,
    main,
    require_no_server,
    require_server,
    validate_hopper_sid,
)


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


def test_ore_help(capsys):
    """ore --help shows help and returns 0."""
    result = cmd_ore(["--help"])
    assert result == 0
    captured = capsys.readouterr()
    assert "usage: hop ore" in captured.out
    assert "session_id" in captured.out


def test_status_help(capsys):
    """status --help shows help and returns 0."""
    result = cmd_status(["--help"])
    assert result == 0
    captured = capsys.readouterr()
    assert "usage: hop status" in captured.out
    assert "message" in captured.out


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


def test_ore_unknown_arg(capsys):
    """ore rejects unknown arguments."""
    result = cmd_ore(["session-123", "--unknown"])
    assert result == 1
    captured = capsys.readouterr()
    assert "error: unrecognized arguments: --unknown" in captured.out
    assert "usage: hop ore" in captured.out


def test_status_unknown_arg(capsys):
    """status rejects unknown arguments."""
    result = cmd_status(["--unknown"])
    assert result == 1
    captured = capsys.readouterr()
    assert "error: unrecognized arguments: --unknown" in captured.out
    assert "usage: hop status" in captured.out


def test_ore_missing_session_id(capsys):
    """ore requires session_id argument."""
    result = cmd_ore([])
    assert result == 1
    captured = capsys.readouterr()
    assert "error:" in captured.out
    assert "session_id" in captured.out


# Tests for ping command


def test_ping_command_no_server(capsys):
    """Ping command returns 1 when server not running."""
    with patch.object(sys, "argv", ["hopper", "ping"]):
        with patch("hopper.cli.SOCKET_PATH", "/tmp/nonexistent.sock"):
            result = main()
    assert result == 1
    captured = capsys.readouterr()
    assert "Server not running" in captured.out


def test_ping_command_validates_hopper_sid(capsys):
    """Ping command validates HOPPER_SID if set."""
    with patch.object(sys, "argv", ["hopper", "ping"]):
        with patch("hopper.client.ping", return_value=True):
            with patch.dict(os.environ, {"HOPPER_SID": "bad-session"}):
                with patch("hopper.client.session_exists", return_value=False):
                    result = main()
    assert result == 1
    captured = capsys.readouterr()
    assert "bad-session" in captured.out
    assert "not found or archived" in captured.out


def test_ping_command_success(capsys):
    """Ping command returns 0 when server running and no HOPPER_SID."""
    with patch.object(sys, "argv", ["hopper", "ping"]):
        with patch("hopper.client.ping", return_value=True):
            env = os.environ.copy()
            env.pop("HOPPER_SID", None)
            with patch.dict(os.environ, env, clear=True):
                result = main()
    assert result == 0
    captured = capsys.readouterr()
    assert "pong" in captured.out


# Tests for up command


def test_up_command_requires_tmux(capsys):
    """Up command returns 1 when not inside tmux."""
    with patch.object(sys, "argv", ["hopper", "up"]):
        with patch("hopper.cli.require_no_server", return_value=None):
            with patch("hopper.tmux.is_inside_tmux", return_value=False):
                with patch("hopper.tmux.get_tmux_sessions", return_value=[]):
                    result = main()
    assert result == 1
    captured = capsys.readouterr()
    assert "hop up must run inside tmux" in captured.out
    assert "tmux new 'hop up'" in captured.out


def test_up_command_shows_existing_sessions(capsys):
    """Up command shows existing sessions when tmux is running."""
    with patch.object(sys, "argv", ["hopper", "up"]):
        with patch("hopper.cli.require_no_server", return_value=None):
            with patch("hopper.tmux.is_inside_tmux", return_value=False):
                with patch("hopper.tmux.get_tmux_sessions", return_value=["main", "dev"]):
                    result = main()
    assert result == 1
    captured = capsys.readouterr()
    assert "tmux attach -t main" in captured.out
    assert "tmux attach -t dev" in captured.out


def test_up_command_fails_if_server_running(capsys):
    """Up command returns 1 if server already running."""
    with patch.object(sys, "argv", ["hopper", "up"]):
        with patch("hopper.client.ping", return_value=True):
            result = main()
    assert result == 1
    captured = capsys.readouterr()
    assert "Server already running" in captured.out


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


# Tests for get_hopper_sid


def test_get_hopper_sid_set():
    """get_hopper_sid returns value when set."""
    with patch.dict(os.environ, {"HOPPER_SID": "test-session-123"}):
        result = get_hopper_sid()
    assert result == "test-session-123"


def test_get_hopper_sid_not_set():
    """get_hopper_sid returns None when not set."""
    env = os.environ.copy()
    env.pop("HOPPER_SID", None)
    with patch.dict(os.environ, env, clear=True):
        result = get_hopper_sid()
    assert result is None


# Tests for validate_hopper_sid


def test_validate_hopper_sid_not_set():
    """validate_hopper_sid returns None when HOPPER_SID not set."""
    env = os.environ.copy()
    env.pop("HOPPER_SID", None)
    with patch.dict(os.environ, env, clear=True):
        result = validate_hopper_sid()
    assert result is None


def test_validate_hopper_sid_valid():
    """validate_hopper_sid returns None when session exists."""
    with patch.dict(os.environ, {"HOPPER_SID": "valid-session"}):
        with patch("hopper.client.session_exists", return_value=True):
            result = validate_hopper_sid()
    assert result is None


def test_validate_hopper_sid_invalid(capsys):
    """validate_hopper_sid returns 1 when session doesn't exist."""
    with patch.dict(os.environ, {"HOPPER_SID": "invalid-session"}):
        with patch("hopper.client.session_exists", return_value=False):
            result = validate_hopper_sid()
    assert result == 1
    captured = capsys.readouterr()
    assert "invalid-session" in captured.out
    assert "not found or archived" in captured.out
    assert "unset HOPPER_SID" in captured.out


# Tests for status command


def test_status_no_server(capsys):
    """status command returns 1 when server not running."""
    with patch("hopper.client.ping", return_value=False):
        result = cmd_status([])
    assert result == 1
    captured = capsys.readouterr()
    assert "Server not running" in captured.out


def test_status_no_hopper_sid(capsys):
    """status command returns 1 when HOPPER_SID not set."""
    env = os.environ.copy()
    env.pop("HOPPER_SID", None)
    with patch.dict(os.environ, env, clear=True):
        with patch("hopper.client.ping", return_value=True):
            result = cmd_status([])
    assert result == 1
    captured = capsys.readouterr()
    assert "HOPPER_SID not set" in captured.out


def test_status_invalid_session(capsys):
    """status command returns 1 when session doesn't exist."""
    with patch.dict(os.environ, {"HOPPER_SID": "bad-session"}):
        with patch("hopper.client.ping", return_value=True):
            with patch("hopper.client.session_exists", return_value=False):
                result = cmd_status([])
    assert result == 1
    captured = capsys.readouterr()
    assert "bad-session" in captured.out
    assert "not found or archived" in captured.out


def test_status_show(capsys):
    """status command shows current message when no args."""
    session_data = {"id": "test-session", "message": "Working on feature X"}
    with patch.dict(os.environ, {"HOPPER_SID": "test-session"}):
        with patch("hopper.client.ping", return_value=True):
            with patch("hopper.client.session_exists", return_value=True):
                with patch("hopper.client.get_session", return_value=session_data):
                    result = cmd_status([])
    assert result == 0
    captured = capsys.readouterr()
    assert "Working on feature X" in captured.out


def test_status_show_empty(capsys):
    """status command shows placeholder when no message set."""
    session_data = {"id": "test-session", "message": ""}
    with patch.dict(os.environ, {"HOPPER_SID": "test-session"}):
        with patch("hopper.client.ping", return_value=True):
            with patch("hopper.client.session_exists", return_value=True):
                with patch("hopper.client.get_session", return_value=session_data):
                    result = cmd_status([])
    assert result == 0
    captured = capsys.readouterr()
    assert "(no status message)" in captured.out


def test_status_update(capsys):
    """status command updates message when args provided."""
    session_data = {"id": "test-session", "message": "Old status"}
    with patch.dict(os.environ, {"HOPPER_SID": "test-session"}):
        with patch("hopper.client.ping", return_value=True):
            with patch("hopper.client.session_exists", return_value=True):
                with patch("hopper.client.get_session", return_value=session_data):
                    with patch("hopper.client.set_session_message", return_value=True):
                        result = cmd_status(["New", "status", "message"])
    assert result == 0
    captured = capsys.readouterr()
    assert "Updated from 'Old status' to 'New status message'" in captured.out


def test_status_update_from_empty(capsys):
    """status command shows simpler message when updating from empty."""
    session_data = {"id": "test-session", "message": ""}
    with patch.dict(os.environ, {"HOPPER_SID": "test-session"}):
        with patch("hopper.client.ping", return_value=True):
            with patch("hopper.client.session_exists", return_value=True):
                with patch("hopper.client.get_session", return_value=session_data):
                    with patch("hopper.client.set_session_message", return_value=True):
                        result = cmd_status(["New status"])
    assert result == 0
    captured = capsys.readouterr()
    assert "Updated to 'New status'" in captured.out
    assert "from" not in captured.out


def test_status_empty_message_error(capsys):
    """status command returns 1 when given empty message."""
    with patch.dict(os.environ, {"HOPPER_SID": "test-session"}):
        with patch("hopper.client.ping", return_value=True):
            with patch("hopper.client.session_exists", return_value=True):
                result = cmd_status(["", "  "])
    assert result == 1
    captured = capsys.readouterr()
    assert "Status message required" in captured.out
