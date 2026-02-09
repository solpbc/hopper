# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the unified process runner module."""

import copy
import io
import logging
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from hopper.process import STAGES, ProcessRunner, _get_worktree_env, run_process

CLAUDE_SESSIONS = {
    "mill": {"session_id": "11111111-1111-1111-1111-111111111111", "started": False},
    "refine": {"session_id": "22222222-2222-2222-2222-222222222222", "started": False},
    "ship": {"session_id": "33333333-3333-3333-3333-333333333333", "started": False},
}


def _claude_sessions(**stage_overrides):
    """Return claude sessions dict with per-stage overrides."""
    sessions = copy.deepcopy(CLAUDE_SESSIONS)
    for stage, overrides in stage_overrides.items():
        sessions[stage].update(overrides)
    return sessions


def _mock_response(stage="mill", state="new", active=False, project="", claude=None, **extra):
    lode = {
        "state": state,
        "active": active,
        "project": project,
        "stage": stage,
        "scope": extra.get("scope", ""),
        "claude": claude or _claude_sessions(),
    }
    lode.update(extra)
    return {"type": "connected", "tmux": None, "lode": lode, "lode_found": True}


def _mock_conn(emitted=None):
    mock = MagicMock()
    if emitted is not None:
        mock.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
    else:
        mock.emit = MagicMock(return_value=True)
    return mock


def test_ship_next_stage_is_shipped():
    """Ship stage should transition to shipped on completion."""
    assert STAGES["ship"]["next_stage"] == "shipped"


# ---------------------------------------------------------------------------
# Mill stage tests
# ---------------------------------------------------------------------------


class TestMillStage:
    def test_emits_running_state(self):
        """Mill runner emits running state when Claude starts."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        emitted = []

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        assert any(e[0] == "lode_set_state" and e[1]["state"] == "running" for e in emitted)

    def test_bails_if_already_active(self):
        """Runner exits 1 if lode is already active."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="mill", active=True),
            ),
        ):
            assert runner.run() == 1

        assert runner.connection is None

    def test_validates_stage(self):
        """Mill runner rejects lode not in mill stage."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine"),
            ),
            patch("hopper.runner.HopperConnection") as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 1

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status="Lode test-id is not in mill stage.",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_emits_error_on_nonzero_exit(self, capsys):
        """Runner emits error state on non-zero exit."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        emitted = []

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch(
                "subprocess.Popen",
                return_value=MagicMock(returncode=1, stderr=io.BytesIO(b"")),
            ),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            assert runner.run() == 1

        assert "Exited with code 1" in capsys.readouterr().out
        assert any(e[0] == "lode_set_state" and e[1]["state"] == "error" for e in emitted)

    def test_captures_stderr_on_error(self, capsys):
        """Runner captures stderr as error message."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        emitted = []

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch(
                "subprocess.Popen",
                return_value=MagicMock(
                    returncode=1, stderr=io.BytesIO(b"Error: something broke\n")
                ),
            ),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        assert "something broke" in capsys.readouterr().out
        error_emissions = [
            e for e in emitted if e[0] == "lode_set_state" and e[1]["state"] == "error"
        ]
        assert "something broke" in error_emissions[0][1]["status"]

    def test_resume_uses_resume_flag(self):
        """Existing session uses --resume."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch(
                "subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)
            ) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        cmd = mock_popen.call_args[0][0]
        assert cmd == [
            "claude",
            "--dangerously-skip-permissions",
            "--resume",
            CLAUDE_SESSIONS["mill"]["session_id"],
        ]

    def test_new_session_uses_session_id_and_prompt(self):
        """New session uses --session-id and prompt."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="mill", state="new"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch(
                "subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)
            ) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "claude"
        assert cmd[2:4] == ["--session-id", CLAUDE_SESSIONS["mill"]["session_id"]]
        assert len(cmd) == 5  # claude, skip, --session-id, id, prompt

    def test_sets_cwd_to_project_dir(self, tmp_path):
        """Runner sets cwd to project directory."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()
        mock_project = MagicMock(path=str(project_dir))

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill",
                    state="running",
                    project="my-project",
                    claude=_claude_sessions(mill={"started": True}),
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch(
                "subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)
            ) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        assert mock_popen.call_args[1]["cwd"] == str(project_dir)

    def test_no_project_uses_none_cwd(self):
        """Runner passes cwd=None when no project set."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch(
                "subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)
            ) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        assert mock_popen.call_args[1]["cwd"] is None

    def test_fails_if_project_dir_missing(self, tmp_path):
        """Runner returns 1 if project dir doesn't exist."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        mock_project = MagicMock(path=str(tmp_path / "nope"))

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="mill", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.runner.HopperConnection") as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 1

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status=f"Project directory not found: {mock_project.path}",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_loads_scope_in_context(self):
        """Runner passes scope to prompt template."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="mill", state="new", scope="build the widget"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
            patch("hopper.process.prompt.load", return_value="prompt") as mock_load,
        ):
            runner.run()

        context = mock_load.call_args[1]["context"]
        assert context["scope"] == "build the widget"

    def test_handles_missing_claude(self, capsys):
        """Runner returns 127 if claude not found."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        emitted = []

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch("subprocess.Popen", side_effect=FileNotFoundError),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            assert runner.run() == 127

        assert "command not found" in capsys.readouterr().out.lower()

    def test_prints_on_unexpected_exception(self, capsys):
        """Runner prints and emits error on unexpected exception."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        emitted = []

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch.object(runner, "_run_claude", side_effect=RuntimeError("disk full")),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            assert runner.run() == 1

        assert "disk full" in capsys.readouterr().out
        assert any(
            e[0] == "lode_set_state" and e[1]["state"] == "error" and e[1]["status"] == "disk full"
            for e in emitted
        )

    def test_clean_exit_after_done_emits_ready_and_next_stage(self):
        """Mill emits state=ready then stage=refine after completion."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        emitted = []

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="mill", state="new"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner._done.set()
            runner.run()

        state_idx = next(
            i
            for i, e in enumerate(emitted)
            if e[0] == "lode_set_state" and e[1]["state"] == "ready"
        )
        stage_idx = next(
            i
            for i, e in enumerate(emitted)
            if e[0] == "lode_set_stage" and e[1]["stage"] == "refine"
        )
        assert state_idx < stage_idx
        assert "Mill complete" in emitted[state_idx][1]["status"]

    def test_clean_exit_without_done_no_transition(self):
        """No ready/stage transition if done was never signalled."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        emitted = []

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        assert not any(e[0] == "lode_set_stage" for e in emitted)
        assert not any(e[0] == "lode_set_state" and e[1]["state"] == "ready" for e in emitted)

    def test_connection_stopped_on_exit(self):
        """Runner stops connection on exit."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        mock_conn = _mock_conn()

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        mock_conn.start.assert_called_once()
        mock_conn.stop.assert_called_once()


# ---------------------------------------------------------------------------
# Refine stage tests
# ---------------------------------------------------------------------------


class TestRefineStage:
    def _setup_refine(self, tmp_path, lode_id="test-id"):
        """Set up common refine test fixtures. Returns (session_dir, project_dir, mock_project)."""
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()
        session_dir = tmp_path / "lodes" / lode_id
        session_dir.mkdir(parents=True)
        mock_project = MagicMock(path=str(project_dir))
        return session_dir, project_dir, mock_project

    def test_first_run_bootstraps_codex_then_runs_claude(self, tmp_path):
        """First run bootstraps Codex then runs Claude with refine prompt."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)
        (session_dir / "mill_out.md").write_text("Build the widget")

        codex_calls = []

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.create_worktree", return_value=True),
            patch("hopper.process.prompt.load", return_value="loaded prompt"),
            patch(
                "hopper.process.bootstrap_codex", return_value=(0, "codex-thread-abc")
            ) as mock_boot,
            patch(
                "hopper.process.set_codex_thread_id",
                side_effect=lambda s, sid, tid: codex_calls.append((sid, tid)),
            ),
            patch(
                "subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)
            ) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        mock_boot.assert_called_once()
        assert codex_calls == [("test-id", "codex-thread-abc")]
        cmd = mock_popen.call_args[0][0]
        assert "--session-id" in cmd

    def test_first_run_emits_setup_status(self, tmp_path):
        """First-run refine emits setup status updates in order."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)
        (session_dir / "mill_out.md").write_text("Build the widget")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.create_worktree", return_value=True),
            patch("hopper.process._has_makefile", return_value=True),
            patch("hopper.process._run_make_install", return_value=True),
            patch("hopper.process.prompt.load", return_value="loaded prompt"),
            patch("hopper.process.bootstrap_codex", return_value=(0, "codex-thread-abc")),
            patch("hopper.process.set_codex_thread_id", return_value=True),
            patch("hopper.process.set_lode_status") as mock_status,
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        assert mock_status.call_args_list == [
            call(runner.socket_path, runner.lode_id, "Creating worktree..."),
            call(runner.socket_path, runner.lode_id, "Running make install..."),
            call(runner.socket_path, runner.lode_id, "Bootstrapping Codex..."),
        ]

    def test_no_makefile_skips_make_install(self, tmp_path):
        """First-run refine without Makefile skips make install and env setup."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)
        (session_dir / "mill_out.md").write_text("Build the widget")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.create_worktree", return_value=True),
            patch("hopper.process._has_makefile", return_value=False),
            patch("hopper.process._run_make_install") as mock_make_install,
            patch("hopper.process.prompt.load", return_value="loaded prompt"),
            patch("hopper.process.bootstrap_codex", return_value=(0, "codex-thread-abc")),
            patch("hopper.process.set_codex_thread_id", return_value=True),
            patch("hopper.process.set_lode_status"),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        mock_make_install.assert_not_called()
        assert runner.use_env is False

    def test_resume_skips_bootstrap(self, tmp_path):
        """Resume uses --resume and skips Codex bootstrap."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)
        worktree = session_dir / "worktree"
        worktree.mkdir()

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="refine",
                    state="running",
                    project="my-project",
                    claude=_claude_sessions(refine={"started": True}),
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.create_worktree") as mock_wt,
            patch("hopper.process.bootstrap_codex") as mock_boot,
            patch(
                "subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)
            ) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        mock_wt.assert_not_called()
        mock_boot.assert_not_called()
        cmd = mock_popen.call_args[0][0]
        assert "--resume" in cmd
        assert mock_popen.call_args[1]["cwd"] == str(worktree)

    def test_resume_skips_setup_status_with_node_modules(self, tmp_path):
        """Resume with existing worktree and node_modules emits no setup status updates."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)
        worktree = session_dir / "worktree"
        worktree.mkdir()
        (worktree / "node_modules").mkdir()

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="refine",
                    state="running",
                    project="my-project",
                    claude=_claude_sessions(refine={"started": True}),
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process._has_makefile", return_value=True),
            patch("hopper.process._run_make_install", return_value=True) as mock_install,
            patch("hopper.process.set_lode_status") as mock_status,
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        mock_status.assert_not_called()
        mock_install.assert_not_called()

    def test_resume_skips_setup_status(self, tmp_path):
        """Resume with existing worktree and venv emits no setup status updates."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)
        worktree = session_dir / "worktree"
        worktree.mkdir()
        (worktree / ".venv").mkdir()

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="refine",
                    state="running",
                    project="my-project",
                    claude=_claude_sessions(refine={"started": True}),
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process._has_makefile", return_value=True),
            patch("hopper.process._run_make_install", return_value=True),
            patch("hopper.process.set_lode_status") as mock_status,
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        mock_status.assert_not_called()

    def test_validates_stage(self):
        """Refine runner rejects lode not in refine stage."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="mill"),
            ),
            patch("hopper.runner.HopperConnection") as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 1

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status="Lode test-id is not in refine stage.",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_fails_if_no_project(self):
        """Runner exits 1 if no project directory."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", project=""),
            ),
            patch("hopper.runner.HopperConnection") as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 1

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status="No project directory found for lode.",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_fails_if_project_dir_missing(self, tmp_path):
        """Runner exits 1 if project dir doesn't exist."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        mock_project = MagicMock(path=str(tmp_path / "nope"))

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.runner.HopperConnection") as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 1

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status=f"Project directory not found: {mock_project.path}",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_fails_if_worktree_creation_fails(self, tmp_path):
        """Runner exits 1 if git worktree creation fails."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.create_worktree", return_value=False),
            patch("hopper.runner.HopperConnection") as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 1

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status="Failed to create git worktree.",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_fails_if_input_missing_on_first_run(self, tmp_path):
        """Runner exits 1 if mill_out.md missing on first run."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.create_worktree", return_value=True),
            patch("hopper.runner.HopperConnection") as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 1

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status=f"Input not found: {session_dir / 'mill_out.md'}",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_bootstrap_failure_bails(self, tmp_path, capsys):
        """Runner exits 1 if Codex bootstrap fails."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)
        (session_dir / "mill_out.md").write_text("Build it")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.create_worktree", return_value=True),
            patch("hopper.process.prompt.load", return_value="prompt"),
            patch("hopper.process.bootstrap_codex", return_value=(1, None)),
            patch("hopper.runner.HopperConnection") as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 1

        assert "bootstrap failed" in capsys.readouterr().out
        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status="Codex bootstrap failed (exit 1).",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_clean_exit_after_done_emits_ready_and_ship(self, tmp_path):
        """Refine emits state=ready then stage=ship after completion."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        emitted = []
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)
        (session_dir / "mill_out.md").write_text("Build it")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.create_worktree", return_value=True),
            patch("hopper.process.prompt.load", return_value="prompt"),
            patch("hopper.process.bootstrap_codex", return_value=(0, "thread-123")),
            patch("hopper.process.set_codex_thread_id", return_value=True),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner._done.set()
            runner.run()

        state_idx = next(
            i
            for i, e in enumerate(emitted)
            if e[0] == "lode_set_state" and e[1]["state"] == "ready"
        )
        stage_idx = next(
            i for i, e in enumerate(emitted) if e[0] == "lode_set_stage" and e[1]["stage"] == "ship"
        )
        assert state_idx < stage_idx
        assert "Refine complete" in emitted[state_idx][1]["status"]


# ---------------------------------------------------------------------------
# _get_worktree_env tests
# ---------------------------------------------------------------------------


class TestGetWorktreeEnv:
    def test_venv_only(self, tmp_path):
        """Prepends .venv/bin to PATH and sets VIRTUAL_ENV."""
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        env = _get_worktree_env(tmp_path, {"PATH": "/usr/bin"})
        assert env["PATH"].startswith(str(tmp_path / ".venv" / "bin"))
        assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")
        assert "node_modules" not in env["PATH"]

    def test_node_modules_only(self, tmp_path):
        """Prepends node_modules/.bin to PATH, no VIRTUAL_ENV."""
        (tmp_path / "node_modules" / ".bin").mkdir(parents=True)
        env = _get_worktree_env(tmp_path, {"PATH": "/usr/bin"})
        assert str(tmp_path / "node_modules" / ".bin") in env["PATH"]
        assert "VIRTUAL_ENV" not in env

    def test_both(self, tmp_path):
        """Both .venv/bin and node_modules/.bin prepended."""
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        (tmp_path / "node_modules" / ".bin").mkdir(parents=True)
        env = _get_worktree_env(tmp_path, {"PATH": "/usr/bin"})
        venv_pos = env["PATH"].index(str(tmp_path / ".venv" / "bin"))
        node_pos = env["PATH"].index(str(tmp_path / "node_modules" / ".bin"))
        assert venv_pos < node_pos  # venv first
        assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")

    def test_neither(self, tmp_path):
        """No tooling dirs — PATH unchanged, no VIRTUAL_ENV."""
        env = _get_worktree_env(tmp_path, {"PATH": "/usr/bin"})
        assert env["PATH"] == "/usr/bin"
        assert "VIRTUAL_ENV" not in env


# ---------------------------------------------------------------------------
# Ship stage tests
# ---------------------------------------------------------------------------


class TestShipStage:
    def _setup_ship(self, tmp_path, lode_id="test-id"):
        """Set up common ship test fixtures."""
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()
        session_dir = tmp_path / "lodes" / lode_id
        session_dir.mkdir(parents=True)
        (session_dir / "worktree").mkdir()
        mock_project = MagicMock(path=str(project_dir))
        return session_dir, project_dir, mock_project

    def test_first_run_uses_ship_prompt(self, tmp_path):
        """First run loads ship prompt with branch and worktree context."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        session_dir, project_dir, mock_project = self._setup_ship(tmp_path)
        (session_dir / "refine_out.md").write_text("Refine summary")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.is_dirty", return_value=False),
            patch("hopper.process.current_branch", return_value="main"),
            patch("hopper.process.prompt.load", return_value="loaded prompt") as mock_load,
            patch(
                "subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)
            ) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        context = mock_load.call_args[1]["context"]
        assert context["branch"] == "hopper-test-id"
        assert context["worktree"] == str(session_dir / "worktree")
        assert context["input"] == "Refine summary"
        assert mock_popen.call_args[1]["cwd"] == str(project_dir)

    def test_resume_uses_resume_flag(self, tmp_path):
        """Resume uses --resume."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        session_dir, project_dir, mock_project = self._setup_ship(tmp_path)

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="ship",
                    state="running",
                    project="my-project",
                    claude=_claude_sessions(ship={"started": True}),
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.is_dirty", return_value=False),
            patch("hopper.process.current_branch", return_value="main"),
            patch(
                "subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)
            ) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        cmd = mock_popen.call_args[0][0]
        assert "--resume" in cmd

    def test_validates_stage(self, capsys):
        """Ship runner rejects lode not in ship stage."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection") as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 1

        assert "not in ship stage" in capsys.readouterr().out
        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status="Lode test-id is not in ship stage.",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_fails_if_no_project(self):
        """Runner exits 1 if no project directory."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", project=""),
            ),
            patch("hopper.runner.HopperConnection") as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 1

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status="No project directory found for lode.",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_fails_if_worktree_missing(self, tmp_path, capsys):
        """Runner exits 1 if worktree doesn't exist."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()
        session_dir = tmp_path / "lodes" / "test-id"
        session_dir.mkdir(parents=True)
        # No worktree
        mock_project = MagicMock(path=str(project_dir))

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.runner.HopperConnection") as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 1

        assert "Worktree not found" in capsys.readouterr().out
        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status=f"Worktree not found: {session_dir / 'worktree'}",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_fails_if_repo_dirty(self, tmp_path, capsys):
        """Runner exits 1 if project repo has uncommitted changes."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        session_dir, project_dir, mock_project = self._setup_ship(tmp_path)

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.is_dirty", return_value=True),
            patch("hopper.runner.HopperConnection") as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 1

        assert "uncommitted changes" in capsys.readouterr().out
        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status=f"Project repo has uncommitted changes: {project_dir}",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_fails_if_not_on_main(self, tmp_path, capsys):
        """Runner exits 1 if not on main or master."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        session_dir, project_dir, mock_project = self._setup_ship(tmp_path)

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.is_dirty", return_value=False),
            patch("hopper.process.current_branch", return_value="feature-xyz"),
            patch("hopper.runner.HopperConnection") as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 1

        assert "feature-xyz" in capsys.readouterr().out
        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status="Project repo is on branch 'feature-xyz', expected 'main' or 'master'.",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_accepts_master_branch(self, tmp_path):
        """Runner accepts 'master' as the main branch."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        session_dir, project_dir, mock_project = self._setup_ship(tmp_path)
        (session_dir / "refine_out.md").write_text("done")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.is_dirty", return_value=False),
            patch("hopper.process.current_branch", return_value="master"),
            patch("hopper.process.prompt.load", return_value="prompt"),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            assert runner.run() == 0

    def test_emits_shipped_stage_transition_on_completion(self, tmp_path):
        """Ship emits a stage transition to shipped after completion."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        emitted = []
        session_dir, project_dir, mock_project = self._setup_ship(tmp_path)
        (session_dir / "refine_out.md").write_text("done")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.is_dirty", return_value=False),
            patch("hopper.process.current_branch", return_value="main"),
            patch("hopper.process.prompt.load", return_value="prompt"),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner._done.set()
            runner.run()

        assert any(
            e[0] == "lode_set_state"
            and e[1]["state"] == "ready"
            and "Ship complete" in e[1]["status"]
            for e in emitted
        )
        assert any(e[0] == "lode_set_stage" and e[1]["stage"] == "shipped" for e in emitted)

    def test_first_run_writes_diff_txt(self, tmp_path):
        """First run captures diff numstat to diff.txt."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        session_dir, project_dir, mock_project = self._setup_ship(tmp_path)
        (session_dir / "refine_out.md").write_text("Refine summary")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.is_dirty", return_value=False),
            patch("hopper.process.current_branch", return_value="main"),
            patch("hopper.process.get_diff_numstat", return_value="10\t5\tfile.py"),
            patch("hopper.process.prompt.load", return_value="loaded prompt"),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        diff_file = session_dir / "diff.txt"
        assert diff_file.exists()
        assert diff_file.read_text() == "10\t5\tfile.py"

    def test_first_run_no_diff_txt_when_empty(self, tmp_path):
        """No diff.txt when diff numstat returns empty."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        session_dir, project_dir, mock_project = self._setup_ship(tmp_path)
        (session_dir / "refine_out.md").write_text("Refine summary")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.is_dirty", return_value=False),
            patch("hopper.process.current_branch", return_value="main"),
            patch("hopper.process.get_diff_numstat", return_value=""),
            patch("hopper.process.prompt.load", return_value="loaded prompt"),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        assert not (session_dir / "diff.txt").exists()

    def test_diff_failure_does_not_abort_setup(self, tmp_path):
        """Diff numstat failure does not prevent ship setup."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        session_dir, project_dir, mock_project = self._setup_ship(tmp_path)
        (session_dir / "refine_out.md").write_text("Refine summary")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.is_dirty", return_value=False),
            patch("hopper.process.current_branch", return_value="main"),
            patch("hopper.process.get_diff_numstat", side_effect=Exception("git broke")),
            patch("hopper.process.prompt.load", return_value="loaded prompt"),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        assert not (session_dir / "diff.txt").exists()


# ---------------------------------------------------------------------------
# run_process entry point tests
# ---------------------------------------------------------------------------


class TestRunProcess:
    def test_dispatches_to_correct_stage(self):
        """run_process reads stage from server and creates correct runner."""
        with (
            patch(
                "hopper.client.connect",
                return_value={"lode": {"stage": "mill"}},
            ),
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = run_process("test-id", Path("/tmp/test.sock"))

        assert exit_code == 0

    def test_fails_on_unknown_stage(self, capsys):
        """run_process fails for unknown stage."""
        with (
            patch(
                "hopper.client.connect",
                return_value={"lode": {"stage": "unknown"}},
            ),
            patch("hopper.process.set_lode_state") as mock_set_state,
        ):
            assert run_process("test-id", Path("/tmp/test.sock")) == 1

        assert "Unknown stage" in capsys.readouterr().out
        mock_set_state.assert_called_once_with(
            Path("/tmp/test.sock"),
            "test-id",
            "error",
            "Unknown stage: unknown",
        )

    def test_fails_if_lode_not_found(self, capsys):
        """run_process fails if lode not on server."""
        with patch("hopper.client.connect", return_value={"lode": None}):
            assert run_process("test-id", Path("/tmp/test.sock")) == 1

    def test_fails_if_connect_fails(self, capsys):
        """run_process fails if server connection fails."""
        with patch("hopper.client.connect", return_value=None):
            assert run_process("test-id", Path("/tmp/test.sock")) == 1

    def test_prints_on_unexpected_exception(self, capsys):
        """run_process prints and sets error state on unexpected exception."""
        with (
            patch(
                "hopper.client.connect",
                return_value={"lode": {"stage": "mill"}},
            ),
            patch.object(ProcessRunner, "run", side_effect=RuntimeError("unexpected crash")),
            patch("hopper.process.set_lode_state") as mock_set_state,
        ):
            assert run_process("test-id", Path("/tmp/test.sock")) == 1

        assert "unexpected crash" in capsys.readouterr().out
        mock_set_state.assert_called_once_with(
            Path("/tmp/test.sock"),
            "test-id",
            "error",
            "unexpected crash",
        )


class TestProcessingLog:
    """Tests for processing.log file handler setup."""

    def test_processing_log_created(self, isolate_config):
        """processing.log is created by run_process."""
        log_path = isolate_config / "processing.log"
        with (
            patch("hopper.client.connect", return_value={"lode": {"stage": "mill"}}),
            patch("hopper.runner.connect", return_value=_mock_response()),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.process.ProcessRunner.run", return_value=0),
        ):
            run_process("test-id", Path("/tmp/test.sock"))

        assert log_path.exists()
        content = log_path.read_text()
        assert "process start" in content
        assert "lode=test-id" in content

    def test_processing_log_contains_stage(self, isolate_config):
        """processing.log includes the loaded stage."""
        log_path = isolate_config / "processing.log"
        with (
            patch("hopper.client.connect", return_value={"lode": {"stage": "refine"}}),
            patch("hopper.runner.connect", return_value=_mock_response()),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.process.ProcessRunner.run", return_value=0),
        ):
            run_process("test-id", Path("/tmp/test.sock"))

        content = log_path.read_text()
        assert "stage=refine" in content

    def test_processing_log_error_path(self, isolate_config):
        """processing.log captures connection failures."""
        log_path = isolate_config / "processing.log"
        with patch("hopper.client.connect", return_value=None):
            run_process("fail-id", Path("/tmp/test.sock"))

        content = log_path.read_text()
        assert "connect failed" in content
        assert "lode=fail-id" in content

    def test_processing_log_handler_cleaned_up(self, isolate_config):
        """Handler is removed after run_process completes."""
        log_path = isolate_config / "processing.log"
        hopper_logger = logging.getLogger("hopper")
        initial_count = len(hopper_logger.handlers)
        with patch("hopper.client.connect", return_value=None):
            run_process("test-id", Path("/tmp/test.sock"))
        assert len(hopper_logger.handlers) == initial_count
        assert not any(
            isinstance(handler, logging.FileHandler)
            and Path(getattr(handler, "baseFilename", "")) == log_path
            for handler in hopper_logger.handlers
        )
