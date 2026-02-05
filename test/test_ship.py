"""Tests for the ship runner module."""

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

from hopper.ship import ShipRunner, run_ship


class TestShipRunner:
    def _make_runner(self, lode_id="test-session-id"):
        return ShipRunner(lode_id, Path("/tmp/test.sock"))

    def _mock_response(self, state="ready", active=False, project="my-project", stage="ship"):
        return {
            "type": "connected",
            "tmux": None,
            "lode": {"state": state, "active": active, "project": project, "stage": stage},
            "lode_found": True,
        }

    def _mock_conn(self, emitted=None):
        mock = MagicMock()
        if emitted is not None:
            mock.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        else:
            mock.emit = MagicMock(return_value=True)
        return mock

    def test_first_run_uses_ship_prompt(self, tmp_path):
        """First run loads ship prompt with branch and worktree context."""
        runner = self._make_runner()

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "lodes" / "test-session-id"
        session_dir.mkdir(parents=True)
        worktree = session_dir / "worktree"
        worktree.mkdir()

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        with (
            patch("hopper.runner.connect", return_value=self._mock_response()),
            patch("hopper.runner.HopperConnection", return_value=self._mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.ship.get_lode_dir", return_value=session_dir),
            patch("hopper.ship.is_dirty", return_value=False),
            patch("hopper.ship.current_branch", return_value="main"),
            patch("hopper.ship.prompt.load", return_value="loaded prompt") as mock_load,
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0

        # Prompt loaded with correct context
        mock_load.assert_called_once()
        context = mock_load.call_args[1]["context"]
        assert context["branch"] == "hopper-test-session-id"
        assert context["worktree"] == str(worktree)
        assert context["project"] == "my-project"

        # Claude invoked with --session-id and prompt
        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "claude"
        assert "--session-id" in cmd
        assert mock_popen.call_args[1]["cwd"] == str(project_dir)

    def test_resume_uses_resume_flag(self, tmp_path):
        """Resume (state!=ready) uses --resume."""
        runner = self._make_runner()

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "lodes" / "test-session-id"
        session_dir.mkdir(parents=True)
        worktree = session_dir / "worktree"
        worktree.mkdir()

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        with (
            patch("hopper.runner.connect", return_value=self._mock_response(state="running")),
            patch("hopper.runner.HopperConnection", return_value=self._mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.ship.get_lode_dir", return_value=session_dir),
            patch("hopper.ship.is_dirty", return_value=False),
            patch("hopper.ship.current_branch", return_value="main"),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        cmd = mock_popen.call_args[0][0]
        assert cmd == ["claude", "--dangerously-skip-permissions", "--resume", "test-session-id"]
        assert mock_popen.call_args[1]["cwd"] == str(project_dir)

    def test_fails_if_not_ship_stage(self, capsys):
        """Runner exits with code 1 if session is not in ship stage."""
        runner = self._make_runner()

        with patch("hopper.runner.connect", return_value=self._mock_response(stage="processing")):
            exit_code = runner.run()

        assert exit_code == 1
        assert "not in ship stage" in capsys.readouterr().out

    def test_fails_if_no_project_dir(self):
        """Runner exits with code 1 if no project directory found."""
        runner = self._make_runner()

        response = self._mock_response(project="")
        with patch("hopper.runner.connect", return_value=response):
            exit_code = runner.run()

        assert exit_code == 1

    def test_fails_if_project_dir_missing(self, tmp_path):
        """Runner exits with code 1 if project directory doesn't exist."""
        runner = self._make_runner()

        mock_project = MagicMock()
        mock_project.path = str(tmp_path / "does-not-exist")

        with (
            patch("hopper.runner.connect", return_value=self._mock_response()),
            patch("hopper.runner.find_project", return_value=mock_project),
        ):
            exit_code = runner.run()

        assert exit_code == 1

    def test_fails_if_worktree_missing(self, tmp_path, capsys):
        """Runner exits with code 1 if worktree directory doesn't exist."""
        runner = self._make_runner()

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "lodes" / "test-session-id"
        session_dir.mkdir(parents=True)
        # No worktree created

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        with (
            patch("hopper.runner.connect", return_value=self._mock_response()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.ship.get_lode_dir", return_value=session_dir),
        ):
            exit_code = runner.run()

        assert exit_code == 1
        assert "Worktree not found" in capsys.readouterr().out

    def test_fails_if_project_repo_dirty(self, tmp_path, capsys):
        """Runner exits with code 1 if project repo has uncommitted changes."""
        runner = self._make_runner()

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "lodes" / "test-session-id"
        session_dir.mkdir(parents=True)
        (session_dir / "worktree").mkdir()

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        with (
            patch("hopper.runner.connect", return_value=self._mock_response()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.ship.get_lode_dir", return_value=session_dir),
            patch("hopper.ship.is_dirty", return_value=True),
        ):
            exit_code = runner.run()

        assert exit_code == 1
        assert "uncommitted changes" in capsys.readouterr().out

    def test_fails_if_not_on_main_branch(self, tmp_path, capsys):
        """Runner exits with code 1 if project repo is not on main or master."""
        runner = self._make_runner()

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "lodes" / "test-session-id"
        session_dir.mkdir(parents=True)
        (session_dir / "worktree").mkdir()

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        with (
            patch("hopper.runner.connect", return_value=self._mock_response()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.ship.get_lode_dir", return_value=session_dir),
            patch("hopper.ship.is_dirty", return_value=False),
            patch("hopper.ship.current_branch", return_value="feature-xyz"),
        ):
            exit_code = runner.run()

        assert exit_code == 1
        out = capsys.readouterr().out
        assert "feature-xyz" in out
        assert "main" in out or "master" in out

    def test_accepts_master_branch(self, tmp_path):
        """Runner accepts 'master' as the main branch name."""
        runner = self._make_runner()

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "lodes" / "test-session-id"
        session_dir.mkdir(parents=True)
        (session_dir / "worktree").mkdir()

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        with (
            patch("hopper.runner.connect", return_value=self._mock_response()),
            patch("hopper.runner.HopperConnection", return_value=self._mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.ship.get_lode_dir", return_value=session_dir),
            patch("hopper.ship.is_dirty", return_value=False),
            patch("hopper.ship.current_branch", return_value="master"),
            patch("hopper.ship.prompt.load", return_value="prompt"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0

    def test_bails_if_session_already_active(self):
        """Runner exits with code 1 if session is already active."""
        runner = self._make_runner()

        with patch("hopper.runner.connect", return_value=self._mock_response(active=True)):
            exit_code = runner.run()

        assert exit_code == 1

    def test_emits_error_on_nonzero_exit(self, tmp_path):
        """Runner emits error state when Claude exits non-zero."""
        runner = self._make_runner()
        emitted = []

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "lodes" / "test-session-id"
        session_dir.mkdir(parents=True)
        (session_dir / "worktree").mkdir()

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = io.BytesIO(b"Merge failed\n")

        with (
            patch("hopper.runner.connect", return_value=self._mock_response(state="running")),
            patch("hopper.runner.HopperConnection", return_value=self._mock_conn(emitted)),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.ship.get_lode_dir", return_value=session_dir),
            patch("hopper.ship.is_dirty", return_value=False),
            patch("hopper.ship.current_branch", return_value="main"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 1
        error_emissions = [
            e for e in emitted if e[0] == "lode_set_state" and e[1]["state"] == "error"
        ]
        assert len(error_emissions) == 1
        assert "Merge failed" in error_emissions[0][1]["status"]

    def test_emits_running_state(self, tmp_path):
        """Runner emits running state when Claude starts."""
        runner = self._make_runner()
        emitted = []

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "lodes" / "test-session-id"
        session_dir.mkdir(parents=True)
        (session_dir / "worktree").mkdir()

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        with (
            patch("hopper.runner.connect", return_value=self._mock_response(state="running")),
            patch("hopper.runner.HopperConnection", return_value=self._mock_conn(emitted)),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.ship.get_lode_dir", return_value=session_dir),
            patch("hopper.ship.is_dirty", return_value=False),
            patch("hopper.ship.current_branch", return_value="main"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        assert any(e[0] == "lode_set_state" and e[1]["state"] == "running" for e in emitted)

    def test_no_stage_transition_on_completion(self, tmp_path):
        """Ship runner has no next stage — no stage transition emitted."""
        runner = self._make_runner("test-session")
        emitted = []

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "lodes" / "test-session"
        session_dir.mkdir(parents=True)
        (session_dir / "worktree").mkdir()

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True

        with (
            patch(
                "hopper.runner.connect",
                return_value={
                    "type": "connected",
                    "tmux": None,
                    "lode": {"state": "ready", "project": "my-project", "stage": "ship"},
                    "lode_found": True,
                },
            ),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.ship.get_lode_dir", return_value=session_dir),
            patch("hopper.ship.is_dirty", return_value=False),
            patch("hopper.ship.current_branch", return_value="main"),
            patch("hopper.ship.prompt.load", return_value="prompt"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner._done.set()
            exit_code = runner.run()

        assert exit_code == 0
        # State emitted as ready with Ship complete
        assert any(
            e[0] == "lode_set_state"
            and e[1]["state"] == "ready"
            and "Ship complete" in e[1]["status"]
            for e in emitted
        )
        # No stage transition (ship is terminal)
        assert not any(e[0] == "lode_update" for e in emitted)


class TestRunShip:
    def test_entry_point(self, tmp_path):
        """run_ship creates and runs ShipRunner."""
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "lodes" / "test-id"
        (session_dir / "worktree").mkdir(parents=True)

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        mock_conn = MagicMock()
        mock_conn.emit = MagicMock(return_value=True)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        mock_response = {
            "type": "connected",
            "tmux": None,
            "lode": {"state": "running", "project": "my-project", "stage": "ship"},
            "lode_found": True,
        }

        with (
            patch("hopper.runner.connect", return_value=mock_response),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.ship.get_lode_dir", return_value=session_dir),
            patch("hopper.ship.is_dirty", return_value=False),
            patch("hopper.ship.current_branch", return_value="main"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = run_ship("test-id", Path("/tmp/test.sock"))

        assert exit_code == 0
