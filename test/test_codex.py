# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the codex wrapper module."""

from unittest.mock import MagicMock, patch

from hopper.codex import _parse_thread_id, bootstrap_codex, run_codex


class TestParseThreadId:
    def test_parses_thread_started_event(self):
        """Extracts thread_id from thread.started JSONL event."""
        stdout = '{"type":"thread.started","thread_id":"abc-123"}\n{"type":"turn.started"}\n'
        assert _parse_thread_id(stdout) == "abc-123"

    def test_returns_none_on_empty(self):
        assert _parse_thread_id("") is None

    def test_returns_none_on_no_thread_started(self):
        stdout = '{"type":"turn.started"}\n{"type":"item.completed"}\n'
        assert _parse_thread_id(stdout) is None

    def test_skips_invalid_json(self):
        stdout = 'not json\n{"type":"thread.started","thread_id":"abc-123"}\n'
        assert _parse_thread_id(stdout) == "abc-123"

    def test_returns_none_when_thread_id_missing(self):
        stdout = '{"type":"thread.started"}\n'
        assert _parse_thread_id(stdout) is None


class TestBootstrapCodex:
    def test_returns_thread_id_on_success(self):
        """Parses thread_id from codex exec --json output."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"type":"thread.started","thread_id":"uuid-1234"}\n'

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            exit_code, thread_id = bootstrap_codex("hello", "/tmp/work")

        assert exit_code == 0
        assert thread_id == "uuid-1234"
        cmd = mock_run.call_args[0][0]
        assert cmd == [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            "hello",
        ]
        assert mock_run.call_args[1]["cwd"] == "/tmp/work"
        assert mock_run.call_args[1]["capture_output"] is True

    def test_returns_none_thread_id_on_parse_failure(self):
        """Returns None thread_id when output has no thread.started event."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"type":"turn.started"}\n'

        with patch("subprocess.run", return_value=mock_result):
            exit_code, thread_id = bootstrap_codex("hello", "/tmp")

        assert exit_code == 0
        assert thread_id is None

    def test_codex_not_found(self):
        """Returns 127 when codex command not found."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            exit_code, thread_id = bootstrap_codex("hello", "/tmp")

        assert exit_code == 127
        assert thread_id is None

    def test_keyboard_interrupt(self):
        """Returns 130 on KeyboardInterrupt."""
        with patch("subprocess.run", side_effect=KeyboardInterrupt):
            exit_code, thread_id = bootstrap_codex("hello", "/tmp")

        assert exit_code == 130
        assert thread_id is None

    def test_nonzero_exit_still_parses_thread_id(self):
        """Returns thread_id even on non-zero exit (partial output)."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = '{"type":"thread.started","thread_id":"partial-id"}\n'

        with patch("subprocess.run", return_value=mock_result):
            exit_code, thread_id = bootstrap_codex("hello", "/tmp")

        assert exit_code == 1
        assert thread_id == "partial-id"


class TestRunCodex:
    def test_builds_resume_command(self):
        """Builds correct codex exec resume command."""
        mock_result = MagicMock()
        mock_result.returncode = 0

        expected_cmd = [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-o",
            "/tmp/out.md",
            "resume",
            "thread-uuid-1234",
            "do the thing",
        ]

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            exit_code, cmd = run_codex(
                "do the thing", "/tmp/work", "/tmp/out.md", "thread-uuid-1234"
            )

        assert exit_code == 0
        assert cmd == expected_cmd
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] == expected_cmd
        assert mock_run.call_args[1]["cwd"] == "/tmp/work"

    def test_passthrough_no_capture(self):
        """Does not capture stdout or stderr (passthrough)."""
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            run_codex("prompt", "/tmp", "/tmp/out.md", "tid")

        kwargs = mock_run.call_args[1]
        assert "stdout" not in kwargs
        assert "stderr" not in kwargs

    def test_returns_exit_code(self):
        """Returns subprocess exit code."""
        mock_result = MagicMock()
        mock_result.returncode = 42

        with patch("subprocess.run", return_value=mock_result):
            exit_code, cmd = run_codex("prompt", "/tmp", "/tmp/out.md", "tid")
            assert exit_code == 42
            assert cmd[0] == "codex"

    def test_codex_not_found(self):
        """Returns 127 and cmd when codex command not found."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            exit_code, cmd = run_codex("prompt", "/tmp", "/tmp/out.md", "tid")
            assert exit_code == 127
            assert cmd[0] == "codex"

    def test_keyboard_interrupt(self):
        """Returns 130 and cmd on KeyboardInterrupt."""
        with patch("subprocess.run", side_effect=KeyboardInterrupt):
            exit_code, cmd = run_codex("prompt", "/tmp", "/tmp/out.md", "tid")
            assert exit_code == 130
            assert cmd[0] == "codex"
