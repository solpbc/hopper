# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the codex wrapper module."""

import subprocess
from pathlib import Path
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
        assert mock_run.call_args[1]["stdout"] == subprocess.PIPE
        assert "stderr" not in mock_run.call_args[1]

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
    class _MockPopen:
        def __init__(self, stdout_lines, returncode=0, wait_side_effect=None):
            self.stdout = stdout_lines
            self.returncode = returncode
            self._wait_side_effect = wait_side_effect
            self.terminate_called = False
            self.kill_called = False
            self._running = True

        def wait(self, timeout=None):
            if isinstance(self._wait_side_effect, list):
                effect = self._wait_side_effect.pop(0) if self._wait_side_effect else None
            else:
                effect = self._wait_side_effect
            if isinstance(effect, BaseException):
                raise effect
            return self.returncode

        def poll(self):
            return None if self._running else self.returncode

        def terminate(self):
            self.terminate_called = True
            self._running = False

        def kill(self):
            self.kill_called = True
            self._running = False

    def test_streams_events_and_writes_events_file(self, tmp_path):
        """Streams JSON events to the callback and appends raw lines to .events.jsonl."""
        output_file = tmp_path / "refine.out.md"
        on_event = MagicMock()
        proc = self._MockPopen(
            [
                '{"type":"turn.started"}\n',
                '{"type":"item.completed","item":{"type":"agent_message","text":"hello"}}\n',
            ],
            returncode=0,
        )
        expected_cmd = [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            "-o",
            str(output_file),
            "resume",
            "thread-uuid-1234",
            "do the thing",
        ]

        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            exit_code, cmd = run_codex(
                "do the thing",
                "/tmp/work",
                str(output_file),
                "thread-uuid-1234",
                on_event=on_event,
            )

        assert exit_code == 0
        assert cmd == expected_cmd
        mock_popen.assert_called_once_with(
            expected_cmd,
            cwd="/tmp/work",
            env=None,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert on_event.call_count == 2
        assert on_event.call_args_list[0].args[0]["type"] == "turn.started"
        assert on_event.call_args_list[1].args[0]["item"]["type"] == "agent_message"
        events_path = Path(tmp_path / "refine.events.jsonl")
        assert events_path.read_text() == (
            '{"type":"turn.started"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"hello"}}\n'
        )

    def test_persists_invalid_json_lines_before_parse(self, tmp_path):
        """Writes raw lines to .events.jsonl even when JSON parsing fails."""
        output_file = tmp_path / "refine.out.md"
        on_event = MagicMock()
        proc = self._MockPopen(
            [
                "not-json\n",
                '{"type":"turn.started"}\n',
            ],
            returncode=0,
        )

        with patch("subprocess.Popen", return_value=proc):
            exit_code, _cmd = run_codex(
                "do the thing",
                "/tmp/work",
                str(output_file),
                "thread-uuid-1234",
                on_event=on_event,
            )

        assert exit_code == 0
        assert on_event.call_count == 1
        assert on_event.call_args.args[0]["type"] == "turn.started"
        events_path = Path(tmp_path / "refine.events.jsonl")
        assert events_path.read_text() == 'not-json\n{"type":"turn.started"}\n'

    def test_returns_exit_code(self):
        """Returns subprocess exit code."""
        proc = self._MockPopen([], returncode=42)

        with patch("subprocess.Popen", return_value=proc):
            exit_code, cmd = run_codex("prompt", "/tmp", "/tmp/out.md", "tid")
            assert exit_code == 42
            assert cmd[0] == "codex"

    def test_codex_not_found(self):
        """Returns 127 and cmd when codex command not found."""
        with patch("subprocess.Popen", side_effect=FileNotFoundError):
            exit_code, cmd = run_codex("prompt", "/tmp", "/tmp/out.md", "tid")
            assert exit_code == 127
            assert cmd[0] == "codex"

    def test_keyboard_interrupt(self):
        """Returns 130 and cmd on KeyboardInterrupt."""
        proc = self._MockPopen([], wait_side_effect=[KeyboardInterrupt(), None])
        with patch("subprocess.Popen", return_value=proc):
            exit_code, cmd = run_codex("prompt", "/tmp", "/tmp/out.md", "tid")
            assert exit_code == 130
            assert cmd[0] == "codex"
            assert proc.terminate_called is True
