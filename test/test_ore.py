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
            "session": {"state": "idle"},
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        # Should emit running state (server handles idle on disconnect)
        assert any(e[0] == "session_set_state" and e[1]["state"] == "running" for e in emitted)
        # Should NOT emit idle - server handles that on disconnect
        assert not any(e[0] == "session_set_state" and e[1]["state"] == "idle" for e in emitted)

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
            "session": {"state": "idle"},
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc),
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
            "session": {"state": "idle"},
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc),
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
            "session": {"state": "idle"},
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
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
            "session": {"state": "idle"},
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", side_effect=FileNotFoundError),
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
            "session": {"state": "idle"},
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc),
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
            "session": {"state": "idle", "project": "my-project"},
            "session_found": True,
        }

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("hopper.ore.find_project", return_value=mock_project),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
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
            "session": {"state": "idle", "project": "my-project"},
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
            "session": {"state": "idle"},  # No project
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
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
            "session": {"state": "idle"},
            "session_found": True,
        }

        with (
            patch("hopper.ore.connect", return_value=mock_response),
            patch("hopper.ore.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
        ):
            exit_code = run_ore("test-id", Path("/tmp/test.sock"))

        assert exit_code == 0
        mock_popen.assert_called_once()
