"""Tests for the ore runner module."""

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

from hopper.ore import OreRunner, run_ore


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
            patch("hopper.runner.connect", return_value=mock_response),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.runner.get_current_pane_id", return_value=None),
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

        with patch("hopper.runner.connect", return_value=mock_response):
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
            patch("hopper.runner.connect", return_value=mock_response),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.runner.get_current_pane_id", return_value=None),
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
            patch("hopper.runner.connect", return_value=mock_response),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.runner.get_current_pane_id", return_value=None),
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
            patch("hopper.runner.connect", return_value=mock_response),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        # Check the command uses --resume for existing session
        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        assert call_args[0][0] == ["claude", "--dangerously-skip-permissions", "--resume", "my-session-id"]

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
            patch("hopper.runner.connect", return_value=mock_response),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        # Check the command uses --session-id and prompt for new session
        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        assert cmd[0] == "claude"
        assert cmd[1] == "--dangerously-skip-permissions"
        assert cmd[2:4] == ["--session-id", "my-session-id"]
        assert "--resume" not in cmd
        assert len(cmd) == 5  # ["claude", "--dangerously-skip-permissions", "--session-id", "<id>", "<prompt>"]

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
            patch("hopper.runner.connect", return_value=mock_response),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
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
            patch("hopper.runner.connect", return_value=mock_response),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", side_effect=FileNotFoundError),
            patch("hopper.runner.get_current_pane_id", return_value=None),
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
            patch("hopper.runner.connect", return_value=mock_response),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.runner.get_current_pane_id", return_value=None),
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
            patch("hopper.runner.connect", return_value=mock_response),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        # Check cwd was set
        mock_popen.assert_called_once()
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs["cwd"] == str(project_dir)

    def test_run_fails_if_project_dir_missing(self, tmp_path):
        """Runner returns error if project directory doesn't exist."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

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
            patch("hopper.runner.connect", return_value=mock_response),
            patch("hopper.runner.find_project", return_value=mock_project),
        ):
            exit_code = runner.run()

        assert exit_code == 1

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
            patch("hopper.runner.connect", return_value=mock_response),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        # Check cwd is None (inherit from parent)
        mock_popen.assert_called_once()
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs["cwd"] is None

    def test_loads_scope_from_session_data(self):
        """Runner loads scope from session data."""
        runner = OreRunner("test-session", Path("/tmp/test.sock"))

        mock_conn = MagicMock()
        mock_conn.emit = MagicMock(return_value=True)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        mock_response = {
            "type": "connected",
            "tmux": None,
            "session": {"state": "new", "scope": "build the widget"},
            "session_found": True,
        }

        with (
            patch("hopper.runner.connect", return_value=mock_response),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.runner.get_current_pane_id", return_value=None),
            patch("hopper.ore.prompt.load", return_value="prompt") as mock_load,
        ):
            runner.run()

        # Check scope was passed to prompt
        mock_load.assert_called_once()
        context = mock_load.call_args[1]["context"]
        assert context["scope"] == "build the widget"


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
            patch("hopper.runner.connect", return_value=mock_response),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = run_ore("test-id", Path("/tmp/test.sock"))

        assert exit_code == 0
        mock_popen.assert_called_once()


class TestShovelWorkflow:
    """Tests for the shovel completion and auto-dismiss workflow."""

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
            patch("hopper.runner.connect", return_value=mock_response),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            # Simulate shovel completing before proc.wait returns
            runner._done.set()
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
            patch("hopper.runner.connect", return_value=mock_response),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        # Should NOT emit stage transition or ready state
        assert not any(e[0] == "session_update" for e in emitted)
        assert not any(e[0] == "session_set_state" and e[1]["state"] == "ready" for e in emitted)
