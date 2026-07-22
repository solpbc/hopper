# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for tmux interaction utilities."""

from unittest.mock import patch

import pytest

from hopper.tmux import (
    Liveness,
    capture_pane,
    get_current_pane_id,
    get_current_tmux_location,
    get_pane_pid,
    get_tmux_sessions,
    is_inside_tmux,
    is_tmux_server_running,
    kill_pane,
    pane_liveness,
    paste_buffer,
    rename_window,
    send_keys,
)


class TestGetPanePid:
    def test_returns_pane_root_pid(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "12345\n"

            assert get_pane_pid("%7") == 12345

        mock_run.assert_called_once_with(
            ["tmux", "display-message", "-p", "-t", "%7", "#{pane_pid}"],
            capture_output=True,
            text=True,
        )

    @pytest.mark.parametrize("stdout", ["", "not-a-pid\n"])
    def test_returns_none_for_invalid_pid(self, stdout):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = stdout

            assert get_pane_pid("%7") is None

    def test_returns_none_when_tmux_fails(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""

            assert get_pane_pid("%7") is None


class TestPaneLiveness:
    def test_alive_on_success(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""

            assert pane_liveness("%1") is Liveness.ALIVE

        mock_run.assert_called_once_with(
            ["tmux", "has-session", "-t", "%1"],
            capture_output=True,
            text=True,
        )

    def test_gone_only_for_missing_pane(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "can't find pane: %9\n"

            assert pane_liveness("%9") is Liveness.GONE

    @pytest.mark.parametrize(
        "stderr",
        [
            "no server running on /tmp/tmux/default\n",
            "error connecting to /tmp/tmux/default (No such file or directory)\n",
            "permission denied\n",
            "unexpected tmux failure\n",
        ],
    )
    def test_unknown_for_other_tmux_failures(self, stderr):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = stderr

            assert pane_liveness("%1") is Liveness.UNKNOWN

    @pytest.mark.parametrize("error", [FileNotFoundError(), PermissionError()])
    def test_unknown_when_tmux_cannot_execute(self, error):
        with patch("subprocess.run", side_effect=error):
            assert pane_liveness("%1") is Liveness.UNKNOWN


class TestIsInsideTmux:
    def test_returns_true_when_tmux_env_set(self):
        with patch.dict("os.environ", {"TMUX": "/tmp/tmux-1000/default,12345,0"}):
            assert is_inside_tmux() is True

    def test_returns_false_when_tmux_env_not_set(self):
        with patch.dict("os.environ", {}, clear=True):
            assert is_inside_tmux() is False


class TestIsTmuxServerRunning:
    def test_returns_true_when_sessions_exist(self):
        with patch("hopper.tmux.get_tmux_sessions", return_value=["main"]):
            assert is_tmux_server_running() is True

    def test_returns_false_when_no_sessions(self):
        with patch("hopper.tmux.get_tmux_sessions", return_value=[]):
            assert is_tmux_server_running() is False


class TestGetTmuxSessions:
    def test_returns_session_names(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "main\ndev\nhopper\n"
            sessions = get_tmux_sessions()
            assert sessions == ["main", "dev", "hopper"]
            mock_run.assert_called_once_with(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True,
                text=True,
            )

    def test_returns_empty_list_when_no_sessions(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            assert get_tmux_sessions() == []

    def test_returns_empty_list_when_tmux_not_installed(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert get_tmux_sessions() == []


class TestGetCurrentPaneId:
    def test_returns_pane_id_when_set(self):
        with patch.dict("os.environ", {"TMUX_PANE": "%5"}):
            assert get_current_pane_id() == "%5"

    def test_returns_none_when_not_set(self):
        with patch.dict("os.environ", {}, clear=True):
            assert get_current_pane_id() is None

    def test_returns_none_when_empty(self):
        with patch.dict("os.environ", {"TMUX_PANE": ""}):
            assert get_current_pane_id() is None


class TestGetCurrentTmuxLocation:
    def test_returns_location_when_inside_tmux(self):
        with patch.dict(
            "os.environ",
            {"TMUX": "/tmp/tmux-1000/default,12345,0", "TMUX_PANE": "%5"},
        ):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = "main\n"
                result = get_current_tmux_location()
                assert result == {"session": "main", "pane": "%5"}
                mock_run.assert_called_once_with(
                    ["tmux", "display-message", "-t", "%5", "-p", "#{session_name}"],
                    capture_output=True,
                    text=True,
                )

    def test_returns_none_when_not_inside_tmux(self):
        with patch.dict("os.environ", {}, clear=True):
            result = get_current_tmux_location()
            assert result is None

    def test_returns_none_when_no_tmux_pane(self):
        with patch.dict("os.environ", {"TMUX": "/tmp/tmux-1000/default,12345,0"}, clear=True):
            result = get_current_tmux_location()
            assert result is None

    def test_returns_none_when_command_fails(self):
        with patch.dict(
            "os.environ",
            {"TMUX": "/tmp/tmux-1000/default,12345,0", "TMUX_PANE": "%5"},
        ):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 1
                mock_run.return_value.stdout = ""
                result = get_current_tmux_location()
                assert result is None

    def test_returns_none_when_tmux_not_installed(self):
        with patch.dict(
            "os.environ",
            {"TMUX": "/tmp/tmux-1000/default,12345,0", "TMUX_PANE": "%5"},
        ):
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = get_current_tmux_location()
                assert result is None


class TestCapturePane:
    def test_returns_content_on_success(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "\x1b[32mGreen text\x1b[0m\n"
            result = capture_pane("@0")
            assert result == "\x1b[32mGreen text\x1b[0m\n"
            mock_run.assert_called_once_with(
                ["tmux", "capture-pane", "-e", "-p", "-t", "@0"],
                capture_output=True,
                text=True,
            )

    def test_plain_omits_ansi_flag(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "Plain text\n"
            result = capture_pane("@0", plain=True)
            assert result == "Plain text\n"
            mock_run.assert_called_once_with(
                ["tmux", "capture-pane", "-p", "-t", "@0"],
                capture_output=True,
                text=True,
            )

    def test_returns_none_when_command_fails(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            result = capture_pane("@99")
            assert result is None

    def test_returns_none_when_tmux_not_installed(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = capture_pane("@0")
            assert result is None


class TestKillPane:
    def test_kill_pane_success(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0

            result = kill_pane("%1")

        assert result is True
        mock_run.assert_called_once_with(
            ["tmux", "kill-pane", "-t", "%1"],
            capture_output=True,
            text=True,
        )

    def test_kill_pane_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1

            result = kill_pane("%1")

        assert result is False


class TestRenameWindow:
    def test_renames_successfully(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            result = rename_window("%0", "hop:mill")
            assert result is True
            mock_run.assert_called_once_with(
                ["tmux", "rename-window", "-t", "%0", "hop:mill"],
                capture_output=True,
                text=True,
            )

    def test_returns_false_when_command_fails(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            result = rename_window("%99", "test")
            assert result is False

    def test_returns_false_when_tmux_not_installed(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = rename_window("%0", "test")
            assert result is False


class TestSendKeys:
    def test_sends_keys_successfully(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            result = send_keys("@0", "C-d")
            assert result is True
            mock_run.assert_called_once_with(
                ["tmux", "send-keys", "-t", "@0", "C-d"],
                capture_output=True,
                text=True,
            )

    def test_returns_false_when_command_fails(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            result = send_keys("@99", "C-d")
            assert result is False

    def test_returns_false_when_tmux_not_installed(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = send_keys("@0", "C-d")
            assert result is False


class TestPasteBuffer:
    def test_pastes_buffer_successfully(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0

            result = paste_buffer("%1", "hello\nthere")

        assert result is True
        assert mock_run.call_args_list[0].args[0] == ["tmux", "set-buffer", "hello\nthere"]
        assert mock_run.call_args_list[1].args[0] == ["tmux", "paste-buffer", "-t", "%1"]

    def test_returns_false_when_set_buffer_fails(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1

            result = paste_buffer("%1", "hello")

        assert result is False
        assert mock_run.call_count == 1

    def test_returns_false_when_paste_fails(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                type("Result", (), {"returncode": 0})(),
                type("Result", (), {"returncode": 1})(),
            ]

            result = paste_buffer("%1", "hello")

        assert result is False
