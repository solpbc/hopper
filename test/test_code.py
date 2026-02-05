# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the code runner module."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from hopper.code import run_code

MOCK_CMD = [
    "codex",
    "exec",
    "--dangerously-bypass-approvals-and-sandbox",
    "-o",
    "/tmp/out.md",
    "resume",
    "codex-thread-1234",
    "p",
]
THREAD_ID = "codex-thread-1234"


def _mock_response(
    stage="refine",
    project="my-project",
    scope="build widget",
    codex_thread_id=THREAD_ID,
):
    return {
        "type": "connected",
        "tmux": None,
        "lode": {
            "stage": stage,
            "project": project,
            "scope": scope,
            "codex_thread_id": codex_thread_id,
        },
        "lode_found": True,
    }


class TestRunCode:
    def test_session_not_found(self, capsys):
        """Returns 1 when session doesn't exist."""
        with patch("hopper.code.connect", return_value={"lode": None}):
            exit_code = run_code("sid", Path("/tmp/test.sock"), "audit", "test request")

        assert exit_code == 1
        assert "not found" in capsys.readouterr().out

    def test_not_refine_stage(self, capsys):
        """Returns 1 when session is not in refine stage."""
        with patch("hopper.code.connect", return_value=_mock_response(stage="mill")):
            exit_code = run_code("test-1234", Path("/tmp/test.sock"), "audit", "test request")

        assert exit_code == 1
        assert "not in refine stage" in capsys.readouterr().out

    def test_missing_codex_thread_id(self, capsys):
        """Returns 1 with helpful message when codex_thread_id is missing."""
        with patch("hopper.code.connect", return_value=_mock_response(codex_thread_id=None)):
            exit_code = run_code("test-1234", Path("/tmp/test.sock"), "audit", "test request")

        assert exit_code == 1
        output = capsys.readouterr().out
        assert "no Codex thread ID" in output
        assert "hop refine" in output

    def test_empty_codex_thread_id(self, capsys):
        """Returns 1 when codex_thread_id is empty string."""
        with patch("hopper.code.connect", return_value=_mock_response(codex_thread_id="")):
            exit_code = run_code("test-1234", Path("/tmp/test.sock"), "audit", "test request")

        assert exit_code == 1
        assert "no Codex thread ID" in capsys.readouterr().out

    def test_wrong_cwd(self, tmp_path, monkeypatch, capsys):
        """Returns 1 when cwd doesn't match worktree."""
        monkeypatch.chdir(tmp_path)

        session_dir = tmp_path / "lodes" / "test-sid"
        worktree = session_dir / "worktree"
        worktree.mkdir(parents=True)

        with (
            patch("hopper.code.connect", return_value=_mock_response()),
            patch("hopper.code.find_project", return_value=None),
            patch("hopper.code.get_lode_dir", return_value=session_dir),
        ):
            exit_code = run_code("test-sid", Path("/tmp/test.sock"), "audit", "test request")

        assert exit_code == 1
        assert "worktree" in capsys.readouterr().out

    def test_prompt_not_found(self, tmp_path, monkeypatch, capsys):
        """Returns 1 when stage prompt doesn't exist."""
        session_dir = tmp_path / "lodes" / "test-sid"
        worktree = session_dir / "worktree"
        worktree.mkdir(parents=True)
        monkeypatch.chdir(worktree)

        with (
            patch("hopper.code.connect", return_value=_mock_response()),
            patch("hopper.code.find_project", return_value=None),
            patch("hopper.code.get_lode_dir", return_value=session_dir),
            patch("hopper.code.prompt.load", side_effect=FileNotFoundError("nope")),
        ):
            exit_code = run_code("test-sid", Path("/tmp/test.sock"), "nonexistent", "test request")

        assert exit_code == 1
        assert "not found" in capsys.readouterr().out

    def test_runs_codex_resume_and_saves_artifacts(self, tmp_path, monkeypatch, capsys):
        """Runs codex resume, saves input/output/metadata, prints output, manages state."""
        session_dir = tmp_path / "lodes" / "test-sid"
        worktree = session_dir / "worktree"
        worktree.mkdir(parents=True)
        monkeypatch.chdir(worktree)

        state_calls = []

        def mock_set_state(sock, sid, state, status):
            state_calls.append((state, status))
            return True

        def mock_run_codex(prompt, cwd, output_file, thread_id):
            assert thread_id == THREAD_ID
            Path(output_file).write_text("# Audit Result\nAll good.")
            return 0, MOCK_CMD

        mock_project = MagicMock()
        mock_project.path = str(tmp_path / "project")

        with (
            patch("hopper.code.prompt.load", return_value="prompt text"),
            patch("hopper.code.connect", return_value=_mock_response()),
            patch("hopper.code.find_project", return_value=mock_project),
            patch("hopper.code.get_lode_dir", return_value=session_dir),
            patch("hopper.code.set_lode_state", side_effect=mock_set_state),
            patch("hopper.code.run_codex", side_effect=mock_run_codex),
        ):
            exit_code = run_code("test-sid", Path("/tmp/test.sock"), "audit", "test request")

        assert exit_code == 0
        assert state_calls[0] == ("audit", "Running audit")
        assert state_calls[1][0] == "running"
        assert "audit ran for" in state_calls[1][1]

        output = capsys.readouterr().out
        assert "Audit Result" in output
        assert "All good" in output

        # Input prompt saved
        assert (session_dir / "audit.in.md").read_text() == "prompt text"

        # Output saved
        assert (session_dir / "audit.out.md").exists()

        # Metadata saved with codex_thread_id
        meta = json.loads((session_dir / "audit.json").read_text())
        assert meta["stage"] == "audit"
        assert meta["lode_id"] == "test-sid"
        assert meta["codex_thread_id"] == THREAD_ID
        assert meta["exit_code"] == 0
        assert meta["cmd"] == MOCK_CMD
        assert meta["duration_ms"] >= 0
        assert meta["started_at"] <= meta["finished_at"]

    def test_restores_state_on_failure(self, tmp_path, monkeypatch):
        """Restores state and writes metadata even when codex fails."""
        session_dir = tmp_path / "lodes" / "test-sid"
        worktree = session_dir / "worktree"
        worktree.mkdir(parents=True)
        monkeypatch.chdir(worktree)

        state_calls = []

        def mock_set_state(sock, sid, state, status):
            state_calls.append((state, status))
            return True

        with (
            patch("hopper.code.prompt.load", return_value="prompt text"),
            patch("hopper.code.connect", return_value=_mock_response()),
            patch("hopper.code.find_project", return_value=None),
            patch("hopper.code.get_lode_dir", return_value=session_dir),
            patch("hopper.code.set_lode_state", side_effect=mock_set_state),
            patch("hopper.code.run_codex", return_value=(1, MOCK_CMD)),
        ):
            exit_code = run_code("test-sid", Path("/tmp/test.sock"), "audit", "test request")

        assert exit_code == 1
        assert state_calls[-1][0] == "running"
        assert "audit failed after" in state_calls[-1][1]

        # Metadata written even on failure
        meta = json.loads((session_dir / "audit.json").read_text())
        assert meta["exit_code"] == 1

    def test_server_unreachable(self, capsys):
        """Returns 1 when server connection fails."""
        with patch("hopper.code.connect", return_value=None):
            exit_code = run_code("sid", Path("/tmp/test.sock"), "audit", "test request")

        assert exit_code == 1
        assert "Failed to connect" in capsys.readouterr().out

    def test_loads_prompt_with_context(self, tmp_path, monkeypatch):
        """Loads prompt with project context."""
        session_dir = tmp_path / "lodes" / "test-sid"
        worktree = session_dir / "worktree"
        worktree.mkdir(parents=True)
        monkeypatch.chdir(worktree)

        load_calls = []

        def mock_load(name, context=None):
            load_calls.append((name, context))
            return "prompt text"

        mock_project = MagicMock()
        mock_project.path = "/path/to/project"

        with (
            patch("hopper.code.prompt.load", side_effect=mock_load),
            patch("hopper.code.connect", return_value=_mock_response()),
            patch("hopper.code.find_project", return_value=mock_project),
            patch("hopper.code.get_lode_dir", return_value=session_dir),
            patch("hopper.code.set_lode_state", return_value=True),
            patch("hopper.code.run_codex", return_value=(0, MOCK_CMD)),
        ):
            run_code("test-sid", Path("/tmp/test.sock"), "audit", "test request")

        # Single load with context
        assert len(load_calls) == 1
        assert load_calls[0][0] == "audit"
        assert load_calls[0][1]["request"] == "test request"
        assert load_calls[0][1]["project"] == "my-project"
        assert load_calls[0][1]["dir"] == "/path/to/project"
        assert load_calls[0][1]["scope"] == "build widget"

    def test_input_saved_before_codex_runs(self, tmp_path, monkeypatch):
        """Input prompt is saved before codex is invoked."""
        session_dir = tmp_path / "lodes" / "test-sid"
        worktree = session_dir / "worktree"
        worktree.mkdir(parents=True)
        monkeypatch.chdir(worktree)

        input_existed = []

        def mock_run_codex(prompt, cwd, output_file, thread_id):
            # Check that input was already written when codex starts
            input_existed.append((session_dir / "audit.in.md").exists())
            return 0, MOCK_CMD

        with (
            patch("hopper.code.prompt.load", return_value="the prompt"),
            patch("hopper.code.connect", return_value=_mock_response()),
            patch("hopper.code.find_project", return_value=None),
            patch("hopper.code.get_lode_dir", return_value=session_dir),
            patch("hopper.code.set_lode_state", return_value=True),
            patch("hopper.code.run_codex", side_effect=mock_run_codex),
        ):
            run_code("test-sid", Path("/tmp/test.sock"), "audit", "test request")

        assert input_existed == [True]
        assert (session_dir / "audit.in.md").read_text() == "the prompt"
