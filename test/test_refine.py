"""Tests for the refine runner module."""

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

from hopper.refine import RefineRunner, run_refine


class TestRefineRunner:
    def _make_runner(self, session_id="test-session-id"):
        return RefineRunner(session_id, Path("/tmp/test.sock"))

    def _mock_response(self, state="ready", active=False, project="my-project", stage="processing"):
        return {
            "type": "connected",
            "tmux": None,
            "session": {"state": state, "active": active, "project": project, "stage": stage},
            "session_found": True,
        }

    def _mock_conn(self, emitted=None):
        mock = MagicMock()
        if emitted is not None:
            mock.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        else:
            mock.emit = MagicMock(return_value=True)
        return mock

    def test_first_run_bootstraps_codex_then_runs_claude(self, tmp_path):
        """First run bootstraps Codex session, then runs Claude with refine prompt."""
        runner = self._make_runner()

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "sessions" / "test-session-id"
        session_dir.mkdir(parents=True)
        (session_dir / "shovel.md").write_text("Build the widget")

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        codex_calls = []

        def mock_set_codex_thread(sock, sid, tid):
            codex_calls.append((sid, tid))
            return True

        with (
            patch("hopper.runner.connect", return_value=self._mock_response()),
            patch("hopper.runner.HopperConnection", return_value=self._mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.refine.get_session_dir", return_value=session_dir),
            patch("hopper.refine.create_worktree", return_value=True),
            patch("hopper.refine.prompt.load", return_value="loaded prompt"),
            patch(
                "hopper.refine.bootstrap_codex", return_value=(0, "codex-thread-abc")
            ) as mock_boot,
            patch("hopper.refine.set_codex_thread_id", side_effect=mock_set_codex_thread),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0

        # Codex bootstrapped
        mock_boot.assert_called_once()
        boot_args = mock_boot.call_args
        assert boot_args[0][1] == str(session_dir / "worktree")

        # Thread ID sent to server
        assert codex_calls == [("test-session-id", "codex-thread-abc")]

        # Claude still invoked with --session-id and prompt
        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "claude"
        assert "--session-id" in cmd

    def test_resume_skips_bootstrap(self, tmp_path):
        """Resume (state!=ready) uses --resume and skips Codex bootstrap."""
        runner = self._make_runner()

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "sessions" / "test-session-id"
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
            patch("hopper.refine.get_session_dir", return_value=session_dir),
            patch("hopper.refine.create_worktree") as mock_wt,
            patch("hopper.refine.bootstrap_codex") as mock_boot,
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0

        # Worktree NOT created (already exists)
        mock_wt.assert_not_called()

        # Codex NOT bootstrapped
        mock_boot.assert_not_called()

        # Claude invoked with --resume
        cmd = mock_popen.call_args[0][0]
        assert cmd == ["claude", "--dangerously-skip-permissions", "--resume", "test-session-id"]
        assert mock_popen.call_args[1]["cwd"] == str(worktree)

    def test_bootstrap_failure_bails_runner(self, tmp_path, capsys):
        """Runner exits with code 1 if Codex bootstrap fails."""
        runner = self._make_runner()

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "sessions" / "test-session-id"
        session_dir.mkdir(parents=True)
        (session_dir / "shovel.md").write_text("Build the widget")

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        with (
            patch("hopper.runner.connect", return_value=self._mock_response()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.refine.get_session_dir", return_value=session_dir),
            patch("hopper.refine.create_worktree", return_value=True),
            patch("hopper.refine.prompt.load", return_value="task prompt"),
            patch("hopper.refine.bootstrap_codex", return_value=(1, None)),
        ):
            exit_code = runner.run()

        assert exit_code == 1
        assert "bootstrap failed" in capsys.readouterr().out

    def test_bootstrap_codex_not_found(self, tmp_path, capsys):
        """Runner exits with code 1 if codex command not found during bootstrap."""
        runner = self._make_runner()

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "sessions" / "test-session-id"
        session_dir.mkdir(parents=True)
        (session_dir / "shovel.md").write_text("Build the widget")

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        with (
            patch("hopper.runner.connect", return_value=self._mock_response()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.refine.get_session_dir", return_value=session_dir),
            patch("hopper.refine.create_worktree", return_value=True),
            patch("hopper.refine.prompt.load", return_value="task prompt"),
            patch("hopper.refine.bootstrap_codex", return_value=(127, None)),
        ):
            exit_code = runner.run()

        assert exit_code == 1
        assert "codex command not found" in capsys.readouterr().out

    def test_bootstrap_no_thread_id(self, tmp_path, capsys):
        """Runner exits with code 1 if bootstrap succeeds but no thread_id parsed."""
        runner = self._make_runner()

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "sessions" / "test-session-id"
        session_dir.mkdir(parents=True)
        (session_dir / "shovel.md").write_text("Build the widget")

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        with (
            patch("hopper.runner.connect", return_value=self._mock_response()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.refine.get_session_dir", return_value=session_dir),
            patch("hopper.refine.create_worktree", return_value=True),
            patch("hopper.refine.prompt.load", return_value="task prompt"),
            patch("hopper.refine.bootstrap_codex", return_value=(0, None)),
        ):
            exit_code = runner.run()

        assert exit_code == 1
        assert "Failed to capture" in capsys.readouterr().out

    def test_bails_if_session_already_active(self):
        """Runner exits with code 1 if session is already active."""
        runner = self._make_runner()

        with patch("hopper.runner.connect", return_value=self._mock_response(active=True)):
            exit_code = runner.run()

        assert exit_code == 1

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

    def test_fails_if_worktree_creation_fails(self, tmp_path):
        """Runner exits with code 1 if git worktree add fails."""
        runner = self._make_runner()

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "sessions" / "test-session-id"
        session_dir.mkdir(parents=True)

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        with (
            patch("hopper.runner.connect", return_value=self._mock_response()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.refine.get_session_dir", return_value=session_dir),
            patch("hopper.refine.create_worktree", return_value=False),
        ):
            exit_code = runner.run()

        assert exit_code == 1

    def test_fails_if_shovel_missing_on_first_run(self, tmp_path):
        """Runner exits with code 1 if shovel.md missing on first run."""
        runner = self._make_runner()

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "sessions" / "test-session-id"
        session_dir.mkdir(parents=True)

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        with (
            patch("hopper.runner.connect", return_value=self._mock_response()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.refine.get_session_dir", return_value=session_dir),
            patch("hopper.refine.create_worktree", return_value=True),
        ):
            exit_code = runner.run()

        assert exit_code == 1

    def test_emits_error_on_nonzero_exit(self, tmp_path):
        """Runner emits error state when Claude exits non-zero."""
        runner = self._make_runner()
        emitted = []

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "sessions" / "test-session-id"
        worktree = session_dir / "worktree"
        worktree.mkdir(parents=True)

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = io.BytesIO(b"Something broke\n")

        with (
            patch("hopper.runner.connect", return_value=self._mock_response(state="running")),
            patch("hopper.runner.HopperConnection", return_value=self._mock_conn(emitted)),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.refine.get_session_dir", return_value=session_dir),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 1
        error_emissions = [
            e for e in emitted if e[0] == "session_set_state" and e[1]["state"] == "error"
        ]
        assert len(error_emissions) == 1
        assert "Something broke" in error_emissions[0][1]["status"]

    def test_emits_running_state(self, tmp_path):
        """Runner emits running state when Claude starts."""
        runner = self._make_runner()
        emitted = []

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "sessions" / "test-session-id"
        worktree = session_dir / "worktree"
        worktree.mkdir(parents=True)

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        with (
            patch("hopper.runner.connect", return_value=self._mock_response(state="running")),
            patch("hopper.runner.HopperConnection", return_value=self._mock_conn(emitted)),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.refine.get_session_dir", return_value=session_dir),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        assert any(e[0] == "session_set_state" and e[1]["state"] == "running" for e in emitted)

    def test_handles_missing_claude(self, tmp_path):
        """Runner returns 127 if claude command not found."""
        runner = self._make_runner()
        emitted = []

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "sessions" / "test-session-id"
        worktree = session_dir / "worktree"
        worktree.mkdir(parents=True)

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        with (
            patch("hopper.runner.connect", return_value=self._mock_response(state="running")),
            patch("hopper.runner.HopperConnection", return_value=self._mock_conn(emitted)),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.refine.get_session_dir", return_value=session_dir),
            patch("subprocess.Popen", side_effect=FileNotFoundError),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 127
        assert any(
            e[0] == "session_set_state" and e[1]["status"] == "claude command not found"
            for e in emitted
        )

    def test_connection_stopped_on_exit(self, tmp_path):
        """Runner stops HopperConnection on exit."""
        runner = self._make_runner()
        mock_conn = self._mock_conn()

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "sessions" / "test-session-id"
        worktree = session_dir / "worktree"
        worktree.mkdir(parents=True)

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        with (
            patch("hopper.runner.connect", return_value=self._mock_response(state="running")),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.refine.get_session_dir", return_value=session_dir),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        mock_conn.start.assert_called_once()
        mock_conn.stop.assert_called_once()

    def test_sets_hopper_sid_env(self, tmp_path):
        """Runner sets HOPPER_SID environment variable."""
        runner = self._make_runner()

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "sessions" / "test-session-id"
        worktree = session_dir / "worktree"
        worktree.mkdir(parents=True)

        mock_project = MagicMock()
        mock_project.path = str(project_dir)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None

        with (
            patch("hopper.runner.connect", return_value=self._mock_response(state="running")),
            patch("hopper.runner.HopperConnection", return_value=self._mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.refine.get_session_dir", return_value=session_dir),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        env = mock_popen.call_args[1]["env"]
        assert env["HOPPER_SID"] == "test-session-id"


class TestRunRefine:
    def test_entry_point(self, tmp_path):
        """run_refine creates and runs RefineRunner."""
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "sessions" / "test-id"
        worktree = session_dir / "worktree"
        worktree.mkdir(parents=True)

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
            "session": {"state": "running", "project": "my-project", "stage": "processing"},
            "session_found": True,
        }

        with (
            patch("hopper.runner.connect", return_value=mock_response),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.refine.get_session_dir", return_value=session_dir),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = run_refine("test-id", Path("/tmp/test.sock"))

        assert exit_code == 0


class TestRefineCompletion:
    """Tests for the refine completion and stage transition."""

    def test_clean_exit_after_refine_emits_ready_and_ship(self, tmp_path):
        """Runner emits state=ready then stage=ship after clean refine exit."""
        runner = RefineRunner("test-session", Path("/tmp/test.sock"))
        emitted = []

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "sessions" / "test-session"
        worktree = session_dir / "worktree"
        worktree.mkdir(parents=True)
        (session_dir / "shovel.md").write_text("Build it")

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
                    "session": {"state": "ready", "project": "my-project", "stage": "processing"},
                    "session_found": True,
                },
            ),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.refine.get_session_dir", return_value=session_dir),
            patch("hopper.refine.create_worktree", return_value=True),
            patch("hopper.refine.prompt.load", return_value="prompt"),
            patch("hopper.refine.bootstrap_codex", return_value=(0, "thread-123")),
            patch("hopper.refine.set_codex_thread_id", return_value=True),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner._done.set()
            exit_code = runner.run()

        assert exit_code == 0
        state_idx = next(
            i
            for i, e in enumerate(emitted)
            if e[0] == "session_set_state" and e[1]["state"] == "ready"
        )
        stage_idx = next(
            i for i, e in enumerate(emitted) if e[0] == "session_update" and e[1]["stage"] == "ship"
        )
        assert state_idx < stage_idx
        assert "Refine complete" in emitted[state_idx][1]["status"]

    def test_clean_exit_without_refine_done_no_stage_transition(self, tmp_path):
        """Runner does NOT emit stage transition if refine was not completed."""
        runner = RefineRunner("test-session", Path("/tmp/test.sock"))
        emitted = []

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        session_dir = tmp_path / "sessions" / "test-session"
        worktree = session_dir / "worktree"
        worktree.mkdir(parents=True)

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
                    "session": {"state": "running", "project": "my-project", "stage": "processing"},
                    "session_found": True,
                },
            ),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.refine.get_session_dir", return_value=session_dir),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        assert not any(e[0] == "session_update" for e in emitted)
        assert not any(e[0] == "session_set_state" and e[1]["state"] == "ready" for e in emitted)
