"""Tests for the ore runner module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from hopper.ore import OreRunner, run_ore


class TestOreRunner:
    def test_run_notifies_active_then_inactive(self):
        """Runner notifies server of state changes with messages."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        notifications = []

        def mock_set_state(socket_path, session_id, state, message, timeout=2.0):
            notifications.append((session_id, state, message))
            return True

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("hopper.ore.get_session_state", return_value="idle"),
            patch("hopper.ore.set_session_state", side_effect=mock_set_state),
            patch("hopper.ore.ping", return_value=True),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        # Should notify running, then idle (exit code 0)
        assert ("test-session", "running", "Claude running") in notifications
        assert ("test-session", "idle", "Completed successfully") in notifications

    def test_run_sets_error_state_on_nonzero_exit(self):
        """Runner sets error state when Claude exits with non-zero."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        notifications = []

        def mock_set_state(socket_path, session_id, state, message, timeout=2.0):
            notifications.append((session_id, state, message))
            return True

        mock_proc = MagicMock()
        mock_proc.returncode = 1

        with (
            patch("hopper.ore.get_session_state", return_value="idle"),
            patch("hopper.ore.set_session_state", side_effect=mock_set_state),
            patch("hopper.ore.ping", return_value=True),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            exit_code = runner.run()

        assert exit_code == 1
        assert ("test-session", "running", "Claude running") in notifications
        assert ("test-session", "error", "Exited with code 1") in notifications

    def test_run_claude_with_resume_for_existing_session(self):
        """Runner invokes claude with --resume for existing (non-new) sessions."""
        runner = OreRunner("my-session-id", Path("/tmp/test.sock"))

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("hopper.ore.get_session_state", return_value="idle"),
            patch("hopper.ore.set_session_state", return_value=True),
            patch("hopper.ore.ping", return_value=True),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
        ):
            runner.run()

        # Check the command uses --resume for existing session
        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        assert call_args[0][0] == ["claude", "--resume", "my-session-id"]

        # Check environment includes HOPPER_SID
        env = call_args[1]["env"]
        assert env["HOPPER_SID"] == "my-session-id"

    def test_run_claude_without_resume_for_new_session(self):
        """Runner invokes claude without --resume for new sessions."""
        runner = OreRunner("my-session-id", Path("/tmp/test.sock"))

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("hopper.ore.get_session_state", return_value="new"),
            patch("hopper.ore.set_session_state", return_value=True),
            patch("hopper.ore.ping", return_value=True),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
        ):
            runner.run()

        # Check the command does NOT use --resume for new session
        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        assert call_args[0][0] == ["claude"]

    def test_run_handles_missing_claude(self):
        """Runner returns 127 if claude command not found."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        notifications = []

        def mock_set_state(socket_path, session_id, state, message, timeout=2.0):
            notifications.append((session_id, state, message))
            return True

        with (
            patch("hopper.ore.get_session_state", return_value="idle"),
            patch("hopper.ore.set_session_state", side_effect=mock_set_state),
            patch("hopper.ore.ping", return_value=True),
            patch("subprocess.Popen", side_effect=FileNotFoundError),
        ):
            exit_code = runner.run()

        assert exit_code == 127
        assert ("test-session", "error", "claude command not found") in notifications

    def test_server_disconnect_tracked(self):
        """Runner tracks server connection state."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("hopper.ore.get_session_state", return_value=None),
            patch("hopper.ore.set_session_state", return_value=False),
            patch("hopper.ore.ping", return_value=False),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            runner.run()

        # Should have marked as disconnected
        assert runner.server_connected is False


class TestRunOre:
    def test_run_ore_creates_runner(self):
        """run_ore entry point creates and runs OreRunner."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("hopper.ore.get_session_state", return_value="idle"),
            patch("hopper.ore.set_session_state", return_value=True),
            patch("hopper.ore.ping", return_value=True),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
        ):
            exit_code = run_ore("test-id", Path("/tmp/test.sock"))

        assert exit_code == 0
        mock_popen.assert_called_once()
