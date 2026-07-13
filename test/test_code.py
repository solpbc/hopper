# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the code runner module."""

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from hopper.code import (
    EXEC_HEARTBEAT_COMMAND_CHARS,
    ExecHeartbeat,
    ProgressHeartbeat,
    _next_version,
    _summarize_event,
    format_progress_duration,
    run_code,
    truncate_progress_command,
)

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
        progress = MagicMock(return_value=True)

        def mock_set_state(sock, sid, state, status):
            state_calls.append((state, status))
            return True

        def mock_run_codex(prompt, cwd, output_file, thread_id, env=None, on_event=None):
            assert thread_id == THREAD_ID
            if on_event:
                on_event({"type": "thread.started"})
                on_event(
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}}
                )
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
            patch("hopper.code.set_lode_progress", progress),
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
        assert "turn_failed_message" not in meta
        assert meta["duration_ms"] >= 0
        assert meta["started_at"] <= meta["finished_at"]
        assert progress.called

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

    def test_quota_turn_failed_prints_banner_and_guidance(self, tmp_path, monkeypatch, capsys):
        """Prints a loud banner and quota guidance for Codex usage-limit failures."""
        session_dir = tmp_path / "lodes" / "test-sid"
        worktree = session_dir / "worktree"
        worktree.mkdir(parents=True)
        monkeypatch.chdir(worktree)

        message = "You've hit your usage limit. try again at Jul 11th, 2026 9:36 AM."
        state_calls = []

        def mock_set_state(sock, sid, state, status):
            state_calls.append((state, status))
            return True

        def mock_run_codex(prompt, cwd, output_file, thread_id, env=None, on_event=None):
            if on_event:
                on_event({"type": "turn.failed", "error": {"message": message}})
            return 1, MOCK_CMD

        with (
            patch("hopper.code.prompt.load", return_value="prompt text"),
            patch("hopper.code.connect", return_value=_mock_response()),
            patch("hopper.code.find_project", return_value=None),
            patch("hopper.code.get_lode_dir", return_value=session_dir),
            patch("hopper.code.set_lode_state", side_effect=mock_set_state),
            patch("hopper.code.run_codex", side_effect=mock_run_codex),
        ):
            exit_code = run_code("test-sid", Path("/tmp/test.sock"), "audit", "test request")

        output = capsys.readouterr().out
        assert exit_code == 1
        assert "CODEX TURN FAILED" in output
        assert message in output
        assert "one shared account used by every hopper host" in output
        assert state_calls[-1][0] == "running"
        assert "codex usage limit" in state_calls[-1][1]

        meta = json.loads((session_dir / "audit.json").read_text())
        assert meta["turn_failed_message"] == message

    def test_nonquota_turn_failed_prints_banner_without_guidance(
        self, tmp_path, monkeypatch, capsys
    ):
        """Prints the banner without quota guidance for other turn.failed messages."""
        session_dir = tmp_path / "lodes" / "test-sid"
        worktree = session_dir / "worktree"
        worktree.mkdir(parents=True)
        monkeypatch.chdir(worktree)

        message = "stream disconnected"
        state_calls = []

        def mock_set_state(sock, sid, state, status):
            state_calls.append((state, status))
            return True

        def mock_run_codex(prompt, cwd, output_file, thread_id, env=None, on_event=None):
            if on_event:
                on_event({"type": "turn.failed", "error": {"message": message}})
            return 1, MOCK_CMD

        with (
            patch("hopper.code.prompt.load", return_value="prompt text"),
            patch("hopper.code.connect", return_value=_mock_response()),
            patch("hopper.code.find_project", return_value=None),
            patch("hopper.code.get_lode_dir", return_value=session_dir),
            patch("hopper.code.set_lode_state", side_effect=mock_set_state),
            patch("hopper.code.run_codex", side_effect=mock_run_codex),
        ):
            exit_code = run_code("test-sid", Path("/tmp/test.sock"), "audit", "test request")

        output = capsys.readouterr().out
        assert exit_code == 1
        assert "CODEX TURN FAILED" in output
        assert message in output
        assert "one shared account used by every hopper host" not in output
        assert state_calls[-1][0] == "running"
        assert "codex turn failed" in state_calls[-1][1]

        meta = json.loads((session_dir / "audit.json").read_text())
        assert meta["turn_failed_message"] == message

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

        def mock_run_codex(prompt, cwd, output_file, thread_id, env=None, on_event=None):
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

    def test_first_run_uses_base_names(self, tmp_path, monkeypatch):
        """First run writes unversioned artifact names."""
        session_dir = tmp_path / "lodes" / "test-sid"
        worktree = session_dir / "worktree"
        worktree.mkdir(parents=True)
        monkeypatch.chdir(worktree)

        def mock_run_codex(prompt, cwd, output_file, thread_id, env=None, on_event=None):
            Path(output_file).write_text("# Audit Result\nAll good.")
            return 0, MOCK_CMD

        with (
            patch("hopper.code.prompt.load", return_value="prompt text"),
            patch("hopper.code.connect", return_value=_mock_response()),
            patch("hopper.code.find_project", return_value=None),
            patch("hopper.code.get_lode_dir", return_value=session_dir),
            patch("hopper.code.set_lode_state", return_value=True),
            patch("hopper.code.run_codex", side_effect=mock_run_codex),
        ):
            exit_code = run_code("test-sid", Path("/tmp/test.sock"), "audit", "test request")

        assert exit_code == 0
        assert (session_dir / "audit.in.md").exists()
        assert (session_dir / "audit.out.md").exists()
        assert (session_dir / "audit.json").exists()
        assert not (session_dir / "audit_1.in.md").exists()

    def test_second_run_uses_version_1(self, tmp_path, monkeypatch):
        """Second run writes _1 artifact names when base output already exists."""
        session_dir = tmp_path / "lodes" / "test-sid"
        worktree = session_dir / "worktree"
        worktree.mkdir(parents=True)
        monkeypatch.chdir(worktree)

        original_content = "existing output\n"
        (session_dir / "audit.out.md").write_text(original_content)

        def mock_run_codex(prompt, cwd, output_file, thread_id, env=None, on_event=None):
            Path(output_file).write_text("# Audit Result\nAll good.")
            return 0, MOCK_CMD

        with (
            patch("hopper.code.prompt.load", return_value="prompt text"),
            patch("hopper.code.connect", return_value=_mock_response()),
            patch("hopper.code.find_project", return_value=None),
            patch("hopper.code.get_lode_dir", return_value=session_dir),
            patch("hopper.code.set_lode_state", return_value=True),
            patch("hopper.code.run_codex", side_effect=mock_run_codex),
        ):
            exit_code = run_code("test-sid", Path("/tmp/test.sock"), "audit", "test request")

        assert exit_code == 0
        assert (session_dir / "audit_1.in.md").exists()
        assert (session_dir / "audit_1.out.md").exists()
        assert (session_dir / "audit_1.json").exists()
        assert (session_dir / "audit.out.md").read_text() == original_content

    def test_third_run_uses_version_2(self, tmp_path, monkeypatch):
        """Third run writes _2 artifact names when base and _1 outputs exist."""
        session_dir = tmp_path / "lodes" / "test-sid"
        worktree = session_dir / "worktree"
        worktree.mkdir(parents=True)
        monkeypatch.chdir(worktree)

        (session_dir / "audit.out.md").write_text("existing output\n")
        (session_dir / "audit_1.out.md").write_text("existing output 1\n")

        def mock_run_codex(prompt, cwd, output_file, thread_id, env=None, on_event=None):
            Path(output_file).write_text("# Audit Result\nAll good.")
            return 0, MOCK_CMD

        with (
            patch("hopper.code.prompt.load", return_value="prompt text"),
            patch("hopper.code.connect", return_value=_mock_response()),
            patch("hopper.code.find_project", return_value=None),
            patch("hopper.code.get_lode_dir", return_value=session_dir),
            patch("hopper.code.set_lode_state", return_value=True),
            patch("hopper.code.run_codex", side_effect=mock_run_codex),
        ):
            exit_code = run_code("test-sid", Path("/tmp/test.sock"), "audit", "test request")

        assert exit_code == 0
        assert (session_dir / "audit_2.in.md").exists()
        assert (session_dir / "audit_2.out.md").exists()
        assert (session_dir / "audit_2.json").exists()


class TestNextVersion:
    def test_no_existing_files_returns_none(self, tmp_path):
        """Returns None when base output does not exist."""
        assert _next_version(tmp_path, "audit") is None

    def test_base_exists_returns_1(self, tmp_path):
        """Returns 1 when only base output exists."""
        (tmp_path / "audit.out.md").write_text("existing output\n")
        assert _next_version(tmp_path, "audit") == 1

    def test_base_and_1_exist_returns_2(self, tmp_path):
        """Returns 2 when base and _1 outputs exist."""
        (tmp_path / "audit.out.md").write_text("existing output\n")
        (tmp_path / "audit_1.out.md").write_text("existing output 1\n")
        assert _next_version(tmp_path, "audit") == 2


class TestSummarizeEvent:
    def test_turn_completed_missing_usage(self):
        assert _summarize_event({"type": "turn.completed"}) == "codex turn done"

    def test_turn_completed_with_output_tokens(self):
        assert (
            _summarize_event({"type": "turn.completed", "usage": {"output_tokens": 123}})
            == "codex turn done (123 tok)"
        )

    def test_tool_item_with_tool_name(self):
        assert (
            _summarize_event(
                {
                    "type": "item.completed",
                    "item": {"type": "tool_use", "tool_name": "shell"},
                }
            )
            == "codex: shell"
        )

    def test_tool_item_without_tool_name(self):
        assert (
            _summarize_event({"type": "item.completed", "item": {"type": "tool_use"}})
            == "codex: tool_use"
        )

    def test_item_completed_with_other_type(self):
        assert (
            _summarize_event({"type": "item.completed", "item": {"type": "something_else"}})
            == "codex: item.completed"
        )

    def test_unknown_event_type(self):
        assert _summarize_event({"type": "custom.event"}) == "codex: custom.event"

    def test_non_dict_input(self):
        assert _summarize_event("not a dict") == ""


class TestProgressHeartbeat:
    def test_periodically_emits_summary(self):
        emitted = []
        emitted_once = threading.Event()

        def emit(summary):
            emitted.append(summary)
            emitted_once.set()

        hb = ProgressHeartbeat(emit, lambda now_ms: f"working at {now_ms}", interval=0.01)
        hb.start()
        try:
            assert emitted_once.wait(timeout=1)
        finally:
            hb.stop()

        assert emitted
        assert emitted[0].startswith("working at ")
        assert hb._thread is not None
        assert not hb._thread.is_alive()

    def test_stop_is_emit_barrier_and_joins(self):
        emit_started = threading.Event()
        release_emit = threading.Event()
        stop_returned = threading.Event()
        emitted = []

        def emit(summary):
            emitted.append(summary)
            emit_started.set()
            assert release_emit.wait(timeout=1)

        hb = ProgressHeartbeat(emit, lambda now_ms: "working", interval=0.01)
        hb.start()
        assert emit_started.wait(timeout=1)

        def stop():
            hb.stop()
            stop_returned.set()

        stop_thread = threading.Thread(target=stop)
        stop_thread.start()
        assert not stop_returned.wait(timeout=0.05)
        release_emit.set()
        stop_thread.join(timeout=1)

        assert stop_returned.is_set()
        assert hb._thread is not None
        assert not hb._thread.is_alive()
        emitted_after_stop = len(emitted)
        time.sleep(0.03)
        assert len(emitted) == emitted_after_stop

    def test_stop_recheck_skips_emit_after_summary(self):
        summary_started = threading.Event()
        release_summary = threading.Event()
        stop_returned = threading.Event()
        emitted = []

        def summary(now_ms):
            summary_started.set()
            assert release_summary.wait(timeout=1)
            return "working"

        hb = ProgressHeartbeat(emitted.append, summary, interval=0.01)
        hb.start()
        assert summary_started.wait(timeout=1)

        def stop():
            hb.stop()
            stop_returned.set()

        stop_thread = threading.Thread(target=stop)
        stop_thread.start()
        assert not stop_returned.wait(timeout=0.05)
        release_summary.set()
        stop_thread.join(timeout=1)

        assert stop_returned.is_set()
        assert emitted == []
        assert hb._thread is not None
        assert not hb._thread.is_alive()

    def test_summary_and_emit_exceptions_are_isolated(self):
        summary_calls = 0
        emit_called = threading.Event()

        def summary(now_ms):
            nonlocal summary_calls
            summary_calls += 1
            if summary_calls == 1:
                raise RuntimeError("summary failed")
            return "working"

        def emit(value):
            emit_called.set()
            raise RuntimeError("emit failed")

        hb = ProgressHeartbeat(emit, summary, interval=0.01)
        hb.start()
        try:
            assert emit_called.wait(timeout=1)
        finally:
            hb.stop()

        assert summary_calls >= 2
        assert hb._thread is not None
        assert not hb._thread.is_alive()

    def test_format_progress_duration(self):
        assert format_progress_duration(-1) == "0s"
        assert format_progress_duration(0) == "0s"
        assert format_progress_duration(12_999) == "12s"
        assert format_progress_duration(4 * 60_000 + 12_000) == "4m12s"
        assert format_progress_duration(60 * 60_000 + 2 * 60_000 + 3_000) == "1h02m03s"

    def test_truncate_progress_command(self):
        assert truncate_progress_command("make ci") == "make ci"
        truncated = truncate_progress_command("x" * 100)
        assert len(truncated) == EXEC_HEARTBEAT_COMMAND_CHARS
        assert truncated.endswith("...")


class TestExecHeartbeat:
    def test_summary_reports_command_and_elapsed(self, monkeypatch):
        monkeypatch.setattr("hopper.code.current_time_ms", lambda: 10_000)
        hb = ExecHeartbeat(lambda summary: None)

        hb.on_event(
            {
                "type": "item.started",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "command": "pytest test/test_code.py",
                    "status": "in_progress",
                },
            }
        )

        summary = hb.summary(16_000)
        assert summary is not None
        assert "pytest test/test_code.py" in summary
        assert "(6s)" in summary

    def test_summary_truncates_long_command(self, monkeypatch):
        command = "x" * 100
        monkeypatch.setattr("hopper.code.current_time_ms", lambda: 1_000)
        hb = ExecHeartbeat(lambda summary: None)

        hb.on_event(
            {
                "type": "item.started",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "command": command,
                },
            }
        )

        summary = hb.summary(2_000)
        assert summary is not None
        cmd = summary.removeprefix("codex: running ").split(" (", 1)[0]
        assert len(cmd) == EXEC_HEARTBEAT_COMMAND_CHARS
        assert cmd.endswith("...")

    def test_matching_completed_clears_in_flight(self, monkeypatch):
        monkeypatch.setattr("hopper.code.current_time_ms", lambda: 1_000)
        hb = ExecHeartbeat(lambda summary: None)

        hb.on_event(
            {
                "type": "item.started",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "command": "make test",
                },
            }
        )
        hb.on_event({"type": "item.completed", "item": {"id": "item_1"}})

        assert hb.summary(2_000) is None

    def test_summary_without_in_flight_returns_none(self):
        hb = ExecHeartbeat(lambda summary: None)

        assert hb.summary(2_000) is None

    def test_summary_uses_most_recently_started_command(self, monkeypatch):
        now = 1_000
        monkeypatch.setattr("hopper.code.current_time_ms", lambda: now)
        hb = ExecHeartbeat(lambda summary: None)

        hb.on_event(
            {
                "type": "item.started",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "command": "first command",
                },
            }
        )
        now = 3_000
        hb.on_event(
            {
                "type": "item.started",
                "item": {
                    "id": "item_2",
                    "type": "command_execution",
                    "command": "second command",
                },
            }
        )

        summary = hb.summary(5_000)
        assert summary is not None
        assert "second command" in summary
        assert "first command" not in summary

    def test_non_command_execution_started_is_ignored(self, monkeypatch):
        monkeypatch.setattr("hopper.code.current_time_ms", lambda: 1_000)
        hb = ExecHeartbeat(lambda summary: None)

        hb.on_event(
            {
                "type": "item.started",
                "item": {
                    "id": "item_1",
                    "type": "agent_message",
                    "text": "hello",
                },
            }
        )

        assert hb.summary(2_000) is None
