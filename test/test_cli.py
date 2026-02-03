"""Tests for the hopper CLI."""

import os
import sys
from unittest.mock import patch

from hopper import __version__
from hopper.cli import (
    cmd_config,
    cmd_ore,
    cmd_ping,
    cmd_screenshot,
    cmd_status,
    cmd_up,
    get_hopper_sid,
    main,
    require_config_name,
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


def test_ore_rejects_running_session(capsys):
    """ore rejects session that is already running."""
    mock_session = {"id": "test-1234-session", "state": "running"}
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.get_session", return_value=mock_session):
            result = cmd_ore(["test-1234-session"])
    assert result == 1
    captured = capsys.readouterr()
    assert "test-123" in captured.out  # short_id
    assert "already running" in captured.out
    assert "--force" in captured.out


def test_ore_force_allows_running_session(capsys):
    """ore --force allows taking over a running session."""
    mock_session = {"id": "test-1234-session", "state": "running"}
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.get_session", return_value=mock_session):
            with patch("hopper.ore.run_ore", return_value=0) as mock_run:
                result = cmd_ore(["test-1234-session", "--force"])
    assert result == 0
    mock_run.assert_called_once()


def test_ore_allows_idle_session(capsys):
    """ore allows session that is idle."""
    mock_session = {"id": "test-1234-session", "state": "idle"}
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.get_session", return_value=mock_session):
            with patch("hopper.ore.run_ore", return_value=0) as mock_run:
                result = cmd_ore(["test-1234-session"])
    assert result == 0
    mock_run.assert_called_once()


def test_ore_session_not_found(capsys):
    """ore returns error when session not found."""
    with patch("hopper.cli.require_server", return_value=None):
        with patch("hopper.client.get_session", return_value=None):
            result = cmd_ore(["nonexistent-session"])
    assert result == 1
    captured = capsys.readouterr()
    assert "not found" in captured.out


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
    # connect returns session_found=False for invalid session
    mock_response = {"type": "connected", "tmux": None, "session": None, "session_found": False}
    with patch.object(sys, "argv", ["hopper", "ping"]):
        with patch("hopper.client.connect", return_value=mock_response):
            with patch.dict(os.environ, {"HOPPER_SID": "bad-session"}):
                result = main()
    assert result == 1
    captured = capsys.readouterr()
    assert "bad-session" in captured.out
    assert "not found or archived" in captured.out


def test_ping_command_success(capsys):
    """Ping command returns 0 when server running and no HOPPER_SID."""
    mock_response = {"type": "connected", "tmux": None}
    with patch.object(sys, "argv", ["hopper", "ping"]):
        with patch("hopper.client.connect", return_value=mock_response):
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
            with patch("hopper.cli.require_config_name", return_value=None):
                with patch("hopper.cli.require_projects", return_value=None):
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
            with patch("hopper.cli.require_config_name", return_value=None):
                with patch("hopper.cli.require_projects", return_value=None):
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


def test_up_command_requires_name_config(tmp_path, monkeypatch, capsys):
    """Up command returns 1 if name not configured."""
    monkeypatch.setattr("hopper.config.CONFIG_FILE", tmp_path / "config.json")
    with patch.object(sys, "argv", ["hopper", "up"]):
        with patch("hopper.cli.require_no_server", return_value=None):
            result = main()
    assert result == 1
    captured = capsys.readouterr()
    assert "Please set your name first" in captured.out
    assert "hop config name" in captured.out


# Tests for require_config_name


def test_require_config_name_success(tmp_path, monkeypatch):
    """require_config_name returns None when name is set."""
    config_file = tmp_path / "config.json"
    config_file.write_text('{"name": "jer"}')
    monkeypatch.setattr("hopper.config.CONFIG_FILE", config_file)

    result = require_config_name()
    assert result is None


def test_require_config_name_failure(tmp_path, monkeypatch, capsys):
    """require_config_name returns 1 when name not set."""
    monkeypatch.setattr("hopper.config.CONFIG_FILE", tmp_path / "config.json")

    result = require_config_name()
    assert result == 1
    captured = capsys.readouterr()
    assert "Please set your name first" in captured.out
    assert "hop config name" in captured.out


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
    """status command shows current status when no args."""
    session_data = {"id": "test-session", "status": "Working on feature X"}
    with patch.dict(os.environ, {"HOPPER_SID": "test-session"}):
        with patch("hopper.client.ping", return_value=True):
            with patch("hopper.client.session_exists", return_value=True):
                with patch("hopper.client.get_session", return_value=session_data):
                    result = cmd_status([])
    assert result == 0
    captured = capsys.readouterr()
    assert "Working on feature X" in captured.out


def test_status_show_empty(capsys):
    """status command shows placeholder when no status set."""
    session_data = {"id": "test-session", "status": ""}
    with patch.dict(os.environ, {"HOPPER_SID": "test-session"}):
        with patch("hopper.client.ping", return_value=True):
            with patch("hopper.client.session_exists", return_value=True):
                with patch("hopper.client.get_session", return_value=session_data):
                    result = cmd_status([])
    assert result == 0
    captured = capsys.readouterr()
    assert "(no status)" in captured.out


def test_status_update(capsys):
    """status command updates status when args provided."""
    session_data = {"id": "test-session", "status": "Old status"}
    with patch.dict(os.environ, {"HOPPER_SID": "test-session"}):
        with patch("hopper.client.ping", return_value=True):
            with patch("hopper.client.session_exists", return_value=True):
                with patch("hopper.client.get_session", return_value=session_data):
                    with patch("hopper.client.set_session_status", return_value=True):
                        result = cmd_status(["New", "status", "text"])
    assert result == 0
    captured = capsys.readouterr()
    assert "Updated from 'Old status' to 'New status text'" in captured.out


def test_status_update_from_empty(capsys):
    """status command shows simpler message when updating from empty."""
    session_data = {"id": "test-session", "status": ""}
    with patch.dict(os.environ, {"HOPPER_SID": "test-session"}):
        with patch("hopper.client.ping", return_value=True):
            with patch("hopper.client.session_exists", return_value=True):
                with patch("hopper.client.get_session", return_value=session_data):
                    with patch("hopper.client.set_session_status", return_value=True):
                        result = cmd_status(["New status"])
    assert result == 0
    captured = capsys.readouterr()
    assert "Updated to 'New status'" in captured.out
    assert "from" not in captured.out


def test_status_empty_text_error(capsys):
    """status command returns 1 when given empty text."""
    with patch.dict(os.environ, {"HOPPER_SID": "test-session"}):
        with patch("hopper.client.ping", return_value=True):
            with patch("hopper.client.session_exists", return_value=True):
                result = cmd_status(["", "  "])
    assert result == 1
    captured = capsys.readouterr()
    assert "Status text required" in captured.out


# Tests for config command


def test_config_help(capsys):
    """config --help shows help and returns 0."""
    result = cmd_config(["--help"])
    assert result == 0
    captured = capsys.readouterr()
    assert "usage: hop config" in captured.out
    assert "$variables" in captured.out


def test_config_list_empty(tmp_path, monkeypatch, capsys):
    """config with no args and no config shows help message."""
    monkeypatch.setattr("hopper.config.CONFIG_FILE", tmp_path / "config.json")
    result = cmd_config([])
    assert result == 0
    captured = capsys.readouterr()
    assert "No config set" in captured.out


def test_config_list_values(tmp_path, monkeypatch, capsys):
    """config with no args lists all values."""
    config_file = tmp_path / "config.json"
    config_file.write_text('{"name": "jer", "org": "acme"}')
    monkeypatch.setattr("hopper.config.CONFIG_FILE", config_file)

    result = cmd_config([])
    assert result == 0
    captured = capsys.readouterr()
    assert "name=jer" in captured.out
    assert "org=acme" in captured.out


def test_config_get_existing(tmp_path, monkeypatch, capsys):
    """config name returns value when set."""
    config_file = tmp_path / "config.json"
    config_file.write_text('{"name": "jer"}')
    monkeypatch.setattr("hopper.config.CONFIG_FILE", config_file)

    result = cmd_config(["name"])
    assert result == 0
    captured = capsys.readouterr()
    assert "jer" in captured.out


def test_config_get_missing(tmp_path, monkeypatch, capsys):
    """config name returns error when not set."""
    monkeypatch.setattr("hopper.config.CONFIG_FILE", tmp_path / "config.json")

    result = cmd_config(["name"])
    assert result == 1
    captured = capsys.readouterr()
    assert "Config 'name' not set" in captured.out


def test_config_set_value(tmp_path, monkeypatch, capsys):
    """config name value sets the value."""
    config_file = tmp_path / "config.json"
    monkeypatch.setattr("hopper.config.CONFIG_FILE", config_file)
    monkeypatch.setattr("hopper.config.DATA_DIR", tmp_path)

    result = cmd_config(["name", "jer"])
    assert result == 0
    captured = capsys.readouterr()
    assert "name=jer" in captured.out

    # Verify file was written
    import json

    saved = json.loads(config_file.read_text())
    assert saved == {"name": "jer"}


def test_config_set_updates_existing(tmp_path, monkeypatch, capsys):
    """config name value updates existing config."""
    config_file = tmp_path / "config.json"
    config_file.write_text('{"name": "old", "other": "keep"}')
    monkeypatch.setattr("hopper.config.CONFIG_FILE", config_file)
    monkeypatch.setattr("hopper.config.DATA_DIR", tmp_path)

    result = cmd_config(["name", "new"])
    assert result == 0

    import json

    saved = json.loads(config_file.read_text())
    assert saved == {"name": "new", "other": "keep"}


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
    mock_response = {"type": "connected", "tmux": {"session": "main", "window": "@0"}}
    with patch("hopper.client.ping", return_value=True):
        with patch("hopper.client.connect", return_value=mock_response):
            with patch("hopper.tmux.capture_pane", return_value=None):
                result = cmd_screenshot([])
    assert result == 1
    captured = capsys.readouterr()
    assert "Failed to capture" in captured.out


def test_screenshot_success(capsys):
    """screenshot prints captured content on success."""
    mock_response = {"type": "connected", "tmux": {"session": "main", "window": "@0"}}
    ansi_content = "\x1b[32mGreen text\x1b[0m\nMore lines\n"
    with patch("hopper.client.ping", return_value=True):
        with patch("hopper.client.connect", return_value=mock_response):
            with patch("hopper.tmux.capture_pane", return_value=ansi_content):
                result = cmd_screenshot([])
    assert result == 0
    captured = capsys.readouterr()
    assert captured.out == ansi_content
