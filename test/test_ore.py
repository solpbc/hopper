"""Tests for the ore runner module."""

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

from hopper.ore import OreRunner, _extract_error_message, run_ore


class TestExtractErrorMessage:
    def test_empty_bytes_returns_none(self):
        """Empty stderr returns None."""
        assert _extract_error_message(b"") is None

    def test_single_line(self):
        """Single line is returned as-is."""
        assert _extract_error_message(b"Error: something broke\n") == "Error: something broke"

    def test_multiple_lines_under_limit(self):
        """Lines under the limit are all returned."""
        stderr = b"line1\nline2\nline3\n"
        result = _extract_error_message(stderr)
        assert result == "line1\nline2\nline3"

    def test_multiple_lines_over_limit(self):
        """Only last 5 lines are returned when over limit."""
        stderr = b"line1\nline2\nline3\nline4\nline5\nline6\nline7\n"
        result = _extract_error_message(stderr)
        assert result == "line3\nline4\nline5\nline6\nline7"

    def test_preserves_newlines(self):
        """Newlines are preserved in output."""
        stderr = b"error on\nmultiple lines\n"
        result = _extract_error_message(stderr)
        assert "\n" in result

    def test_handles_unicode(self):
        """Unicode characters are handled correctly."""
        stderr = "Error: café ☕\n".encode("utf-8")
        result = _extract_error_message(stderr)
        assert result == "Error: café ☕"

    def test_handles_invalid_utf8(self):
        """Invalid UTF-8 is replaced rather than raising."""
        stderr = b"Error: \xff\xfe invalid\n"
        result = _extract_error_message(stderr)
        assert "Error:" in result
        assert "invalid" in result


class TestOreRunner:
    def test_run_emits_running_state(self):
        """Runner emits running state when Claude starts successfully."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        emitted = []

        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        mock_response = {
            "type": "connected",
            "tmux": None,
            "session": {"state": "running"},
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.ore.get_current_window_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        # Should emit running state
        assert any(e[0] == "session_set_state" and e[1]["state"] == "running" for e in emitted)

    def test_run_bails_if_session_already_active(self):
        """Runner exits with code 1 if session is already active."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        mock_response = {
            "type": "connected",
            "tmux": None,
            "session": {"state": "running", "active": True},
            "session_found": True,
        }

        with patch("hopper.ore.connect", return_value=mock_response):
            exit_code = runner.run()

        assert exit_code == 1

    def test_run_emits_error_state_on_nonzero_exit(self):
        """Runner emits error state when Claude exits with non-zero."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        emitted = []

        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = io.BytesIO(b"")  # Empty stderr

        mock_response = {
            "type": "connected",
            "tmux": None,
            "session": {"state": "running"},
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.ore.get_current_window_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 1
        assert any(
            e[0] == "session_set_state"
            and e[1]["state"] == "error"
            and e[1]["status"] == "Exited with code 1"
            for e in emitted
        )

    def test_run_captures_stderr_on_error(self):
        """Runner captures stderr and uses last 5 lines as error message."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        emitted = []

        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = io.BytesIO(b"Error: something went wrong\nDetails here\n")

        mock_response = {
            "type": "connected",
            "tmux": None,
            "session": {"state": "running"},
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.ore.get_current_window_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 1
        # Find error emission
        error_emissions = [
            e for e in emitted if e[0] == "session_set_state" and e[1]["state"] == "error"
        ]
        assert len(error_emissions) == 1
        assert "something went wrong" in error_emissions[0][1]["status"]
        assert "Details here" in error_emissions[0][1]["status"]

    def test_run_claude_with_resume_for_existing_session(self):
        """Runner invokes claude with --resume for existing (non-new) sessions."""
        runner = OreRunner("my-session-id", Path("/tmp/test.sock"))

        mock_conn = MagicMock()
        mock_conn.emit = MagicMock(return_value=True)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        mock_response = {
            "type": "connected",
            "tmux": None,
            "session": {"state": "running"},
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("hopper.ore.get_current_window_id", return_value=None),
        ):
            runner.run()

        # Check the command uses --resume for existing session
        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        assert call_args[0][0] == ["claude", "--resume", "my-session-id"]

        # Check environment includes HOPPER_SID
        env = call_args[1]["env"]
        assert env["HOPPER_SID"] == "my-session-id"

    def test_run_claude_with_prompt_for_new_session(self):
        """Runner invokes claude with --session-id and prompt for new sessions."""
        runner = OreRunner("my-session-id", Path("/tmp/test.sock"))

        mock_conn = MagicMock()
        mock_conn.emit = MagicMock(return_value=True)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        mock_response = {
            "type": "connected",
            "tmux": None,
            "session": {"state": "new"},
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("hopper.ore.get_current_window_id", return_value=None),
        ):
            runner.run()

        # Check the command uses --session-id and prompt for new session
        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        assert cmd[0] == "claude"
        assert cmd[1:3] == ["--session-id", "my-session-id"]
        assert "--resume" not in cmd
        assert len(cmd) == 4  # ["claude", "--session-id", "<id>", "<prompt>"]

    def test_run_fails_if_prompt_missing_for_new_session(self):
        """Runner raises FileNotFoundError if shovel prompt is missing for new session."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        mock_conn = MagicMock()
        mock_conn.emit = MagicMock(return_value=True)

        mock_response = {
            "type": "connected",
            "tmux": None,
            "session": {"state": "new"},
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch(
                "hopper.ore.prompt.load",
                side_effect=FileNotFoundError("Prompt not found: shovel.md"),
            ),
        ):
            import pytest

            with pytest.raises(FileNotFoundError, match="Prompt not found"):
                runner.run()

    def test_run_handles_missing_claude(self):
        """Runner returns 127 if claude command not found."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        emitted = []

        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True

        mock_response = {
            "type": "connected",
            "tmux": None,
            "session": {"state": "running"},
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", side_effect=FileNotFoundError),
            patch("hopper.ore.get_current_window_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 127
        assert any(
            e[0] == "session_set_state"
            and e[1]["state"] == "error"
            and e[1]["status"] == "claude command not found"
            for e in emitted
        )

    def test_connection_stopped_on_exit(self):
        """Runner stops HopperConnection on exit."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        mock_conn = MagicMock()
        mock_conn.emit = MagicMock(return_value=True)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        mock_response = {
            "type": "connected",
            "tmux": None,
            "session": {"state": "running"},
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.ore.get_current_window_id", return_value=None),
        ):
            runner.run()

        # Verify start() and stop() were called
        mock_conn.start.assert_called_once()
        mock_conn.stop.assert_called_once()

    def test_run_sets_cwd_to_project_dir(self, tmp_path):
        """Runner sets cwd to project directory when available."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        mock_conn = MagicMock()
        mock_conn.emit = MagicMock(return_value=True)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        # Create a real temp directory for the project
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        mock_response = {
            "type": "connected",
            "tmux": None,
            "session": {"state": "running", "project": "my-project"},
            "session_found": True,
        }

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("hopper.ore.find_project", return_value=mock_project),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("hopper.ore.get_current_window_id", return_value=None),
        ):
            runner.run()

        # Check cwd was set
        mock_popen.assert_called_once()
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs["cwd"] == str(project_dir)

    def test_run_fails_if_project_dir_missing(self, tmp_path):
        """Runner returns error if project directory doesn't exist."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        emitted = []

        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True

        mock_response = {
            "type": "connected",
            "tmux": None,
            "session": {"state": "running", "project": "my-project"},
            "session_found": True,
        }

        # Point to a non-existent directory
        missing_dir = tmp_path / "does-not-exist"

        mock_project = MagicMock()
        mock_project.path = str(missing_dir)

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("hopper.ore.find_project", return_value=mock_project),
            patch("hopper.ore.get_current_window_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 1
        # Should emit error state
        error_emissions = [
            e for e in emitted if e[0] == "session_set_state" and e[1]["state"] == "error"
        ]
        assert len(error_emissions) == 1
        assert "not found" in error_emissions[0][1]["status"]

    def test_run_without_project_uses_no_cwd(self):
        """Runner passes cwd=None when no project is set."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        mock_conn = MagicMock()
        mock_conn.emit = MagicMock(return_value=True)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        mock_response = {
            "type": "connected",
            "tmux": None,
            "session": {"state": "running"},  # No project
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("hopper.ore.get_current_window_id", return_value=None),
        ):
            runner.run()

        # Check cwd is None (inherit from parent)
        mock_popen.assert_called_once()
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs["cwd"] is None


class TestRunOre:
    def test_run_ore_creates_runner(self):
        """run_ore entry point creates and runs OreRunner."""
        mock_conn = MagicMock()
        mock_conn.emit = MagicMock(return_value=True)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        mock_response = {
            "type": "connected",
            "tmux": None,
            "session": {"state": "running"},
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("hopper.ore.get_current_window_id", return_value=None),
        ):
            exit_code = run_ore("test-id", Path("/tmp/test.sock"))

        assert exit_code == 0
        mock_popen.assert_called_once()


class TestActivityMonitor:
    """Tests for the activity monitor functionality."""

    def test_check_activity_detects_stuck(self):
        """Monitor detects stuck state when pane content doesn't change."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))
        runner._window_id = "@1"

        emitted = []
        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        runner.connection = mock_conn

        # Set initial snapshot
        runner._last_snapshot = "Hello World"

        # Same content should trigger stuck
        with patch("hopper.ore.capture_pane", return_value="Hello World"):
            runner._check_activity()

        assert runner._stuck_since is not None
        stuck_emissions = [
            e for e in emitted if e[0] == "session_set_state" and e[1]["state"] == "stuck"
        ]
        assert len(stuck_emissions) == 1
        # First stuck should report MONITOR_INTERVAL seconds, not 0
        assert "5s" in stuck_emissions[0][1]["status"]

    def test_check_activity_detects_running(self):
        """Monitor detects running state when pane content changes."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))
        runner._window_id = "@1"

        emitted = []
        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        runner.connection = mock_conn

        # Set initial snapshot
        runner._last_snapshot = "Hello World"

        # Different content should not trigger stuck
        with patch("hopper.ore.capture_pane", return_value="Hello World 2"):
            runner._check_activity()

        assert runner._stuck_since is None
        assert not any(e[0] == "session_set_state" and e[1]["state"] == "stuck" for e in emitted)
        # Snapshot should be updated
        assert runner._last_snapshot == "Hello World 2"

    def test_check_activity_recovers_from_stuck(self):
        """Monitor emits running when recovering from stuck state."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))
        runner._window_id = "@1"

        emitted = []
        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        runner.connection = mock_conn

        # Set initial stuck state
        runner._last_snapshot = "Hello World"
        runner._stuck_since = 1000

        # Different content should recover from stuck
        with patch("hopper.ore.capture_pane", return_value="New content"):
            runner._check_activity()

        assert runner._stuck_since is None
        assert any(
            e[0] == "session_set_state"
            and e[1]["state"] == "running"
            and e[1]["status"] == "Claude running"
            for e in emitted
        )

    def test_check_activity_stops_on_capture_failure(self):
        """Monitor stops when pane capture fails."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))
        runner._window_id = "@1"
        runner._monitor_stop.clear()

        with patch("hopper.ore.capture_pane", return_value=None):
            runner._check_activity()

        assert runner._monitor_stop.is_set()

    def test_start_monitor_skips_without_tmux(self):
        """Monitor doesn't start when not in tmux."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        with patch("hopper.ore.get_current_window_id", return_value=None):
            runner._start_monitor()

        assert runner._monitor_thread is None

    def test_stop_monitor_handles_no_thread(self):
        """Stop monitor handles case where thread was never started."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))
        runner._stop_monitor()  # Should not raise

    def test_check_activity_skips_when_shovel_done(self):
        """Monitor skips stuck detection once shovel is complete."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))
        runner._window_id = "@1"
        runner._last_snapshot = "Hello World"
        runner._shovel_done.set()

        emitted = []
        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        runner.connection = mock_conn

        with patch("hopper.ore.capture_pane", return_value="Hello World"):
            runner._check_activity()

        # Should not emit stuck since shovel is done
        assert not any(e[0] == "session_set_state" and e[1]["state"] == "stuck" for e in emitted)


class TestShovelWorkflow:
    """Tests for the shovel completion and auto-dismiss workflow."""

    def test_on_server_message_sets_shovel_done(self):
        """Callback sets _shovel_done when completed state received."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        msg = {
            "type": "session_state_changed",
            "session": {"id": "test-session", "state": "completed"},
        }
        runner._on_server_message(msg)

        assert runner._shovel_done.is_set()

    def test_on_server_message_ignores_other_sessions(self):
        """Callback ignores messages for other sessions."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        msg = {
            "type": "session_state_changed",
            "session": {"id": "other-session", "state": "completed"},
        }
        runner._on_server_message(msg)

        assert not runner._shovel_done.is_set()

    def test_on_server_message_ignores_other_states(self):
        """Callback ignores non-completed states."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        msg = {
            "type": "session_state_changed",
            "session": {"id": "test-session", "state": "running"},
        }
        runner._on_server_message(msg)

        assert not runner._shovel_done.is_set()

    def test_on_server_message_ignores_other_message_types(self):
        """Callback ignores non-state-changed messages."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        msg = {
            "type": "session_updated",
            "session": {"id": "test-session", "state": "completed"},
        }
        runner._on_server_message(msg)

        assert not runner._shovel_done.is_set()

    def test_wait_and_dismiss_sends_ctrl_d(self):
        """Dismiss thread sends two Ctrl-D after screen stabilizes."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))
        runner._window_id = "@1"
        runner._shovel_done.set()  # Already done

        send_keys_calls = []

        # Return same content twice to indicate stable screen
        snapshots = iter(["content A", "content A"])
        with (
            patch("hopper.ore.capture_pane", side_effect=lambda _: next(snapshots)),
            patch(
                "hopper.ore.send_keys",
                side_effect=lambda w, k: send_keys_calls.append((w, k)) or True,
            ),
            patch("hopper.ore.MONITOR_INTERVAL", 0.01),
        ):
            runner._wait_and_dismiss_claude()

        assert send_keys_calls == [("@1", "C-d"), ("@1", "C-d")]

    def test_wait_and_dismiss_aborts_when_monitor_stops(self):
        """Dismiss thread aborts if monitor stop is set."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))
        runner._window_id = "@1"
        runner._monitor_stop.set()  # Already stopped

        send_keys_calls = []
        with patch("hopper.ore.send_keys", side_effect=lambda w, k: send_keys_calls.append((w, k))):
            runner._wait_and_dismiss_claude()

        assert send_keys_calls == []

    def test_wait_and_dismiss_aborts_without_window(self):
        """Dismiss thread aborts if no window ID."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))
        runner._window_id = None
        runner._shovel_done.set()

        send_keys_calls = []
        with patch("hopper.ore.send_keys", side_effect=lambda w, k: send_keys_calls.append((w, k))):
            runner._wait_and_dismiss_claude()

        assert send_keys_calls == []

    def test_clean_exit_after_shovel_emits_ready(self):
        """Runner emits state=ready then stage=processing after clean shovel exit."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        emitted = []

        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        mock_response = {
            "type": "connected",
            "tmux": None,
            "session": {"state": "new"},
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.ore.get_current_window_id", return_value=None),
        ):
            # Simulate shovel completing before proc.wait returns
            runner._shovel_done.set()
            exit_code = runner.run()

        assert exit_code == 0
        # Should emit ready state before stage transition
        state_idx = next(
            i
            for i, e in enumerate(emitted)
            if e[0] == "session_set_state" and e[1]["state"] == "ready"
        )
        stage_idx = next(
            i
            for i, e in enumerate(emitted)
            if e[0] == "session_update" and e[1]["stage"] == "processing"
        )
        assert state_idx < stage_idx
        assert "Shovel-ready" in emitted[state_idx][1]["status"]

    def test_clean_exit_without_shovel_does_not_emit_ready(self):
        """Runner does NOT emit ready if shovel was never completed."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        emitted = []

        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        mock_response = {
            "type": "connected",
            "tmux": None,
            "session": {"state": "running"},
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.ore.get_current_window_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        # Should NOT emit stage transition or ready state
        assert not any(e[0] == "session_update" for e in emitted)
        assert not any(e[0] == "session_set_state" and e[1]["state"] == "ready" for e in emitted)
