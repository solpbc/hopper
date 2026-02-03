"""Tests for the codex wrapper module."""

from unittest.mock import MagicMock, patch

from hopper.codex import run_codex


class TestRunCodex:
    def test_correct_command(self):
        """Builds correct codex exec command and returns it."""
        mock_result = MagicMock()
        mock_result.returncode = 0

        expected_cmd = [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-o",
            "/tmp/out.md",
            "do the thing",
        ]

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            exit_code, cmd = run_codex("do the thing", "/tmp/work", "/tmp/out.md")

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
            run_codex("prompt", "/tmp", "/tmp/out.md")

        kwargs = mock_run.call_args[1]
        assert "stdout" not in kwargs
        assert "stderr" not in kwargs

    def test_returns_exit_code(self):
        """Returns subprocess exit code."""
        mock_result = MagicMock()
        mock_result.returncode = 42

        with patch("subprocess.run", return_value=mock_result):
            exit_code, cmd = run_codex("prompt", "/tmp", "/tmp/out.md")
            assert exit_code == 42
            assert cmd[0] == "codex"

    def test_codex_not_found(self):
        """Returns 127 and cmd when codex command not found."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            exit_code, cmd = run_codex("prompt", "/tmp", "/tmp/out.md")
            assert exit_code == 127
            assert cmd[0] == "codex"

    def test_keyboard_interrupt(self):
        """Returns 130 and cmd on KeyboardInterrupt."""
        with patch("subprocess.run", side_effect=KeyboardInterrupt):
            exit_code, cmd = run_codex("prompt", "/tmp", "/tmp/out.md")
            assert exit_code == 130
            assert cmd[0] == "codex"
