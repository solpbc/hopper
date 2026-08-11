# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the base runner module."""

import signal
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from hopper.lodes import current_time_ms, format_park_status
from hopper.runner import (
    DISMISS_DEADLINE_MS,
    BaseRunner,
    _descendant_pids,
    _parse_ps_time,
    _sum_descendant_cpu_ms,
    _sum_process_tree_cpu_ms,
    extract_error_message,
)
from hopper.workspace_trust import WorkspaceTrustError


class TestExtractErrorMessage:
    def test_empty_bytes_returns_none(self):
        """Empty stderr returns None."""
        assert extract_error_message(b"") is None

    def test_single_line(self):
        """Single line is returned as-is."""
        assert extract_error_message(b"Error: something broke\n") == "Error: something broke"

    def test_multiple_lines_under_limit(self):
        """Lines under the limit are all returned."""
        stderr = b"line1\nline2\nline3\n"
        result = extract_error_message(stderr)
        assert result == "line1\nline2\nline3"

    def test_multiple_lines_over_limit(self):
        """Only last 5 lines are returned when over limit."""
        stderr = b"line1\nline2\nline3\nline4\nline5\nline6\nline7\n"
        result = extract_error_message(stderr)
        assert result == "line3\nline4\nline5\nline6\nline7"

    def test_preserves_newlines(self):
        """Newlines are preserved in output."""
        stderr = b"error on\nmultiple lines\n"
        result = extract_error_message(stderr)
        assert "\n" in result

    def test_handles_unicode(self):
        """Unicode characters are handled correctly."""
        stderr = "Error: café ☕\n".encode("utf-8")
        result = extract_error_message(stderr)
        assert result == "Error: café ☕"

    def test_handles_invalid_utf8(self):
        """Invalid UTF-8 is replaced rather than raising."""
        stderr = b"Error: \xff\xfe invalid\n"
        result = extract_error_message(stderr)
        assert "Error:" in result
        assert "invalid" in result


def test_run_teardown_terminates_children_and_sweeps_platform_orphans():
    runner = BaseRunner("test-id", Path("server.sock"))

    with (
        patch("hopper.runner.connect", return_value=None),
        patch.object(runner, "_terminate_claude_process") as terminate,
        patch("hopper.runner.reap_swiftpm_testing_helpers") as sweep,
    ):
        assert runner.run() == 1

    terminate.assert_called_once_with()
    assert sweep.call_count == 2


class TestBaseRunnerRegistration:
    def test_missing_generation_refusal_exits_before_any_child_launch(self):
        runner = BaseRunner("test-id", Path("server.sock"))
        connection = MagicMock()

        def start(callback=None, on_connect=None):
            on_connect()
            callback({"type": "lode_register_refused", "lode_id": "test-id"})

        connection.emit.return_value = True
        connection.start.side_effect = start
        with (
            patch(
                "hopper.runner.connect",
                return_value={"lode": {"active": False, "claude": {}}},
            ),
            patch("hopper.runner.HopperConnection", return_value=connection),
            patch.object(runner, "_setup") as setup,
            patch.object(runner, "_run_claude") as run_model,
            patch("hopper.process._run_setup_command") as setup_command,
            patch("hopper.process.bootstrap_codex") as codex_bootstrap,
            patch("hopper.runner.subprocess.Popen") as claude_popen,
        ):
            assert runner.run() == 1

        setup.assert_not_called()
        run_model.assert_not_called()
        setup_command.assert_not_called()
        codex_bootstrap.assert_not_called()
        claude_popen.assert_not_called()


class TestPsCpuHelpers:
    def test_parse_ps_time_formats(self):
        assert _parse_ps_time("12:34.50") == 754.5
        assert _parse_ps_time("01:02:03") == 3723
        assert _parse_ps_time("2-01:02:03") == 176523
        assert _parse_ps_time("garbage") is None

    def test_sum_descendant_cpu_ms_sums_descendants_and_skips_bad_rows(self):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "\n".join(
            [
                "10 1 01:00:00",
                "11 10 00:01:00",
                "12 11 02:03.50",
                "13 11 garbage",
                "14 99 00:10:00",
                "bad row",
            ]
        )

        with patch("hopper.runner.subprocess.run", return_value=result) as mock_run:
            assert _sum_descendant_cpu_ms(10) == 183500

            mock_run.assert_called_once_with(
                ["ps", "-Ao", "pid=,ppid=,time="],
                capture_output=True,
                text=True,
                timeout=5.0,
            )

    def test_sum_process_tree_cpu_ms_includes_root(self):
        result = MagicMock(returncode=0)
        result.stdout = "\n".join(
            [
                "10 1 00:00:02",
                "11 10 00:00:03",
                "12 11 00:00:04",
            ]
        )

        with patch("hopper.runner.subprocess.run", return_value=result):
            assert _sum_process_tree_cpu_ms(10) == 9000

    def test_sum_descendant_cpu_ms_cycle_does_not_loop_or_count_root(self):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "\n".join(
            [
                "10 12 01:00:00",
                "11 10 00:01:00",
                "12 11 00:02:00",
            ]
        )

        with patch("hopper.runner.subprocess.run", return_value=result):
            assert _sum_descendant_cpu_ms(10) == 180_000

    def test_sum_descendant_cpu_ms_absent_on_command_failure(self):
        failed = MagicMock()
        failed.returncode = 1
        failed.stdout = ""

        with patch("hopper.runner.subprocess.run", return_value=failed):
            assert _sum_descendant_cpu_ms(10) is None
        with patch("hopper.runner.subprocess.run", side_effect=FileNotFoundError):
            assert _sum_descendant_cpu_ms(10) is None
        with patch("hopper.runner.subprocess.run", side_effect=subprocess.SubprocessError):
            assert _sum_descendant_cpu_ms(10) is None
        assert _sum_descendant_cpu_ms(None) is None

    def test_descendant_pids_walks_nested_tree_and_skips_bad_rows(self):
        result = MagicMock(returncode=0)
        result.stdout = "\n".join(
            [
                "10 1",
                "11 10",
                "12 11",
                "13 10",
                "bad row",
                "14 nope",
                "15 11 extra",
            ]
        )

        with patch("hopper.runner.subprocess.run", return_value=result) as mock_run:
            assert _descendant_pids(10) == [13, 11, 12]

        mock_run.assert_called_once_with(
            ["ps", "-Ao", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=5.0,
        )

    def test_descendant_pids_cycle_does_not_loop_or_include_root(self):
        result = MagicMock(returncode=0)
        result.stdout = "\n".join(["10 12", "11 10", "12 11"])

        with patch("hopper.runner.subprocess.run", return_value=result):
            assert _descendant_pids(10) == [11, 12]

    def test_descendant_pids_returns_empty_and_warns_on_ps_failure(self, caplog):
        failed = MagicMock(returncode=1, stdout="")

        with patch("hopper.runner.subprocess.run", return_value=failed):
            assert _descendant_pids(10) == []
        with patch("hopper.runner.subprocess.run", side_effect=FileNotFoundError):
            assert _descendant_pids(10) == []
        with patch("hopper.runner.subprocess.run", side_effect=subprocess.SubprocessError):
            assert _descendant_pids(10) == []

        assert caplog.messages == [
            "ps failed; descendant cleanup degraded to parent-only (exit code 1)",
            "ps failed; descendant cleanup degraded to parent-only (FileNotFoundError: )",
            "ps failed; descendant cleanup degraded to parent-only (SubprocessError: )",
        ]


class TestDescendantTermination:
    def _make_runner(self):
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner._claude_proc = MagicMock(pid=1234)
        runner._claude_proc.poll.return_value = None
        return runner

    def test_descendants_get_term_then_survivors_get_kill(self):
        runner = self._make_runner()
        runner._claude_proc.wait.side_effect = [
            subprocess.TimeoutExpired("claude", 5),
            None,
        ]
        events = []

        def descendants(pid):
            events.append(("collect", pid))
            return [2001, 2002]

        runner._claude_proc.terminate.side_effect = lambda: events.append(("parent-term", 1234))

        def send_signal(pid, sig):
            events.append(("signal", pid, sig))
            if sig == 0 and pid == 2001:
                raise ProcessLookupError

        with (
            patch("hopper.runner._descendant_pids", side_effect=descendants),
            patch("hopper.runner.os.kill", side_effect=send_signal),
            patch("hopper.runner.time.monotonic", side_effect=[0.0, 0.0, 6.0]),
            patch("hopper.runner.time.sleep"),
        ):
            runner._terminate_claude_process()

        assert events[:2] == [("collect", 1234), ("parent-term", 1234)]
        runner._claude_proc.kill.assert_called_once()
        assert runner._claude_proc.wait.call_args_list == [call(timeout=5), call(timeout=5)]
        assert ("signal", 2001, signal.SIGTERM) in events
        assert ("signal", 2002, signal.SIGTERM) in events
        assert ("signal", 2001, signal.SIGKILL) not in events
        assert ("signal", 2002, signal.SIGKILL) in events

    def test_already_dead_descendant_is_tolerated(self):
        runner = self._make_runner()

        with (
            patch("hopper.runner._descendant_pids", return_value=[2001]),
            patch("hopper.runner.os.kill", side_effect=ProcessLookupError) as mock_kill,
        ):
            runner._terminate_claude_process()

        mock_kill.assert_called_once_with(2001, signal.SIGTERM)

    def test_permission_errors_are_tolerated_and_logged(self, caplog):
        runner = self._make_runner()
        runner._claude_proc.terminate.side_effect = PermissionError
        runner._claude_proc.wait.side_effect = [
            subprocess.TimeoutExpired("claude", 5),
            None,
        ]
        runner._claude_proc.kill.side_effect = PermissionError

        with (
            patch("hopper.runner._descendant_pids", return_value=[2001]),
            patch("hopper.runner.os.kill", side_effect=PermissionError) as mock_kill,
            patch("hopper.runner.time.monotonic", side_effect=[0.0, 0.0, 6.0]),
            patch("hopper.runner.time.sleep"),
        ):
            runner._terminate_claude_process()

        assert mock_kill.call_args_list == [
            call(2001, signal.SIGTERM),
            call(2001, 0),
            call(2001, signal.SIGKILL),
        ]
        for message in (
            "Permission denied sending SIGTERM to descendant pid=2001",
            "Permission denied probing descendant pid=2001",
            "Permission denied sending SIGKILL to descendant pid=2001",
        ):
            assert message in caplog.messages


class TestBaseRunnerActivityMonitor:
    """Tests for BaseRunner activity monitor shared behavior."""

    def _make_runner(self):
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        return runner

    def test_park_idle_emits_template_status_after_delegation(self):
        runner = self._make_runner()
        runner._claude_stage = "mill"
        runner.connection = MagicMock()
        reason = "no pane output"

        with (
            patch("hopper.runner._write_recovery_record") as mock_write_recovery,
            patch.object(runner, "_open_gate") as mock_open_gate,
        ):
            runner._park_idle(reason)

        runner.connection.emit.assert_called_once_with(
            "lode_set_state",
            lode_id="test-session",
            state="gated",
            status=format_park_status(reason, "test-session"),
        )
        mock_write_recovery.assert_called_once()
        mock_open_gate.assert_called_once_with()

    def test_run_claude_pretrusts_workspace_before_launch(self):
        runner = self._make_runner()
        proc = MagicMock(returncode=0, stderr=None)
        events = []

        def trust_workspace(cwd, env):
            events.append(("trust", cwd, env["HOPPER_LID"]))
            return Path("/repo")

        def launch(*args, **kwargs):
            events.append(("launch", kwargs["cwd"]))
            return proc

        with (
            patch.object(runner, "_build_command", return_value=(["claude"], "/repo")),
            patch("hopper.runner.trust_claude_workspace", side_effect=trust_workspace),
            patch("hopper.runner.subprocess.Popen", side_effect=launch),
            patch.object(runner, "_emit_state"),
            patch.object(runner, "_start_monitor"),
        ):
            assert runner._run_claude() == (0, None)

        assert events == [
            ("trust", "/repo", "test-session"),
            ("launch", "/repo"),
        ]

    def test_run_claude_keeps_live_process_reference_after_interrupt(self):
        runner = self._make_runner()
        proc = MagicMock(stderr=None)
        proc.wait.side_effect = KeyboardInterrupt
        proc.poll.return_value = None

        with (
            patch.object(runner, "_build_command", return_value=(["claude"], "/repo")),
            patch("hopper.runner.subprocess.Popen", return_value=proc),
            patch.object(runner, "_emit_state"),
            patch.object(runner, "_start_monitor"),
        ):
            assert runner._run_claude() == (130, None)

        assert runner._claude_proc is proc

    def test_run_claude_refuses_launch_when_pretrust_fails(self):
        runner = self._make_runner()

        with (
            patch.object(runner, "_build_command", return_value=(["claude"], "/repo")),
            patch(
                "hopper.runner.trust_claude_workspace",
                side_effect=WorkspaceTrustError("config is locked"),
            ),
            patch("hopper.runner.subprocess.Popen") as launch,
        ):
            result = runner._run_claude()

        assert result == (
            1,
            "Failed to pre-trust Claude workspace: config is locked",
        )
        launch.assert_not_called()

    def test_subprocess_env_configures_managed_claude(self):
        """Managed Hopper stages configure Claude Code for machine-read panes."""
        runner = self._make_runner()

        env = runner._get_subprocess_env()

        assert env["HOPPER_LID"] == "test-session"
        assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        assert env["CLAUDE_CODE_DISABLE_MEMORY_PERIODIC_RESYNC"] == "1"
        assert env["CLAUDE_CODE_DISABLE_MEMORY_BULK_INFLATE"] == "1"
        assert env["CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION"] == "false"

    def test_check_activity_detects_stuck(self):
        """Monitor detects stuck state when pane content doesn't change."""
        runner = self._make_runner()
        runner._pane_id = "%1"

        emitted = []
        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        runner.connection = mock_conn

        runner._last_snapshot = "Hello World"
        runner._last_pane_activity_ms = current_time_ms() - 60_000

        with (
            patch("hopper.runner.capture_pane", return_value="Hello World"),
            patch("hopper.runner.connect", return_value=None),
        ):
            runner._check_activity()

        assert runner._stuck_since is not None
        stuck_emissions = [
            e for e in emitted if e[0] == "lode_set_state" and e[1]["state"] == "stuck"
        ]
        assert len(stuck_emissions) == 1
        assert "No output for " in stuck_emissions[0][1]["status"]
        assert "s" in stuck_emissions[0][1]["status"]

    def test_check_activity_gates_numbered_question_and_records_change(self):
        """Claude AskUserQuestion UI is operator wait state, not a stuck stage."""
        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "working"
        runner._last_pane_activity_ms = current_time_ms() - 10 * 60_000
        snapshot = (
            "Which implementation should I use?\n"
            "❯ 1. Keep compatibility\n"
            "  2. Use the new format\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel"
        )

        emitted = []
        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        runner.connection = mock_conn

        with (
            patch("hopper.runner.capture_pane", return_value=snapshot),
            patch("hopper.runner.send_keys"),
            patch("hopper.runner.get_current_pane_id", return_value="%test"),
            patch("hopper.runner.MONITOR_INTERVAL", 0.001),
            patch("hopper.runner.current_time_ms", return_value=100_000),
        ):
            runner._check_activity()

        assert runner._gated.is_set()
        assert runner._stuck_since is None
        assert any(
            event == "lode_set_state"
            and body["state"] == "gated"
            and body["status"] == "Awaiting operator answer"
            for event, body in emitted
        )
        assert not any(body.get("state") == "stuck" for _, body in emitted)
        assert any(
            event == "lode_set_pane_activity" and body["observed_at"] == 100_000
            for event, body in emitted
        )

    def test_check_activity_detects_running(self):
        """Monitor detects running state when pane content changes."""
        runner = self._make_runner()
        runner._pane_id = "%1"

        emitted = []
        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        runner.connection = mock_conn

        runner._last_snapshot = "Hello World"

        with (
            patch("hopper.runner.capture_pane", return_value="Hello World 2"),
            patch("hopper.runner.connect", return_value=None),
        ):
            runner._check_activity()

        assert runner._stuck_since is None
        assert not any(e[0] == "lode_set_state" and e[1]["state"] == "stuck" for e in emitted)
        assert runner._last_snapshot == "Hello World 2"

    def test_check_activity_recovers_from_stuck(self):
        """Monitor emits running when recovering from stuck state."""
        runner = self._make_runner()
        runner._pane_id = "%1"

        emitted = []
        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        runner.connection = mock_conn

        runner._last_snapshot = "Hello World"
        runner._stuck_since = 1000

        with (
            patch("hopper.runner.capture_pane", return_value="New content"),
            patch("hopper.runner.connect", return_value=None),
        ):
            runner._check_activity()

        assert runner._stuck_since is None
        assert any(
            e[0] == "lode_set_state"
            and e[1]["state"] == "running"
            and e[1]["status"] == "Claude running"
            for e in emitted
        )

    def test_heartbeat_vetos_stuck_when_recent(self):
        """Recent progress heartbeats clear stuck state without pane changes."""
        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "Hello World"
        runner._stuck_since = 1000
        runner._last_pane_activity_ms = current_time_ms() - 60_000

        emitted = []
        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        runner.connection = mock_conn

        with (
            patch("hopper.runner.capture_pane", return_value="Hello World"),
            patch(
                "hopper.runner.connect",
                return_value={
                    "lode": {
                        "last_progress_at": current_time_ms() - 3000,
                        "last_progress_summary": "codex thinking",
                    }
                },
            ),
        ):
            runner._check_activity()

        assert runner._stuck_since is None
        assert any(
            e[0] == "lode_set_state"
            and e[1]["state"] == "running"
            and e[1]["status"] == "codex thinking"
            for e in emitted
        )

    def test_stuck_when_heartbeat_stale_or_missing(self):
        """Stale or missing progress heartbeats fall back to normal stuck detection."""
        for last_progress_at in (current_time_ms() - 60_000, None):
            runner = self._make_runner()
            runner._pane_id = "%1"
            runner._last_snapshot = "Hello World"
            runner._last_pane_activity_ms = current_time_ms() - 60_000

            emitted = []
            mock_conn = MagicMock()
            mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
            runner.connection = mock_conn

            with (
                patch("hopper.runner.capture_pane", return_value="Hello World"),
                patch(
                    "hopper.runner.connect",
                    return_value={
                        "lode": {
                            "last_progress_at": last_progress_at,
                            "last_progress_summary": "codex thinking",
                            "last_pane_activity_at": current_time_ms(),
                        }
                    },
                ),
                patch("hopper.runner.send_keys"),
                patch("hopper.runner.get_current_pane_id", return_value="%test"),
                patch("hopper.runner.MONITOR_INTERVAL", 0.001),
            ):
                runner._check_activity()

            assert runner._stuck_since is not None
            assert any(e[0] == "lode_set_state" and e[1]["state"] == "stuck" for e in emitted)

    def test_claude_only_stuck_after_threshold(self, monkeypatch):
        """Unchanged pane without heartbeats only becomes stuck after the idle threshold."""
        monkeypatch.setattr("hopper.runner.IDLE_THRESHOLD_MS", 100)

        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "Hello World"
        runner._last_pane_activity_ms = current_time_ms()

        emitted = []
        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        runner.connection = mock_conn

        with (
            patch("hopper.runner.capture_pane", return_value="Hello World"),
            patch(
                "hopper.runner.connect",
                return_value={"lode": {"last_progress_at": None, "last_progress_summary": None}},
            ),
        ):
            runner._check_activity()
            assert runner._stuck_since is None
            assert not any(e[0] == "lode_set_state" and e[1]["state"] == "stuck" for e in emitted)
            runner._last_pane_activity_ms = current_time_ms() - 1200
            runner._check_activity()

        assert runner._stuck_since is not None
        stuck_emissions = [
            e for e in emitted if e[0] == "lode_set_state" and e[1]["state"] == "stuck"
        ]
        assert len(stuck_emissions) == 1
        assert stuck_emissions[0][1]["status"].startswith("No output for ")
        assert stuck_emissions[0][1]["status"].endswith("s")

    def test_idle_stage_is_parked_never_terminated(self, monkeypatch):
        """An idle stage is PARKED as gated and left alive. Hopper never kills it.

        A quiet stage may be blocked on a prompt, stalled on a model stream, or hung.
        Hopper cannot tell from the outside, and killing it destroys agent context an
        operator could often resume in one keystroke. So it parks and waits.
        """
        monkeypatch.setattr("hopper.runner.IDLE_THRESHOLD_MS", 100)
        monkeypatch.setattr("hopper.runner.STUCK_PARK_THRESHOLD_MS", 100)

        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "Hello World"
        runner._last_pane_activity_ms = current_time_ms() - 1200
        runner._stuck_since = current_time_ms() - 1200
        runner._claude_proc = MagicMock(pid=1234)
        runner._claude_proc.poll.return_value = None

        emitted = []
        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        runner.connection = mock_conn

        with (
            patch("hopper.runner.capture_pane", return_value="Hello World"),
            patch(
                "hopper.runner.connect",
                return_value={"lode": {"last_progress_at": None, "last_progress_summary": None}},
            ),
            patch("hopper.runner.send_keys"),
            patch("hopper.runner.get_current_pane_id", return_value="%test"),
            patch("hopper.runner.MONITOR_INTERVAL", 0.001),
        ):
            runner._check_activity()

        # THE POINT: the agent is still alive.
        runner._claude_proc.terminate.assert_not_called()
        runner._claude_proc.kill.assert_not_called()

        # It is parked as gated, not errored, and the monitor keeps watching so the
        # gate clears itself the moment the pane moves again.
        assert runner._gated.is_set()
        assert not runner._monitor_stop.is_set()

        states = [kw.get("state") for msg_type, kw in emitted if msg_type == "lode_set_state"]
        assert "gated" in states

    def test_codex_only_running_never_stuck(self, monkeypatch):
        """Fresh progress heartbeats keep an unchanged pane running across ticks."""
        monkeypatch.setattr("hopper.runner.IDLE_THRESHOLD_MS", 100)

        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "Hello World"
        runner._last_pane_activity_ms = current_time_ms() - 1200

        emitted = []
        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        runner.connection = mock_conn

        with (
            patch("hopper.runner.capture_pane", return_value="Hello World"),
            patch(
                "hopper.runner.connect",
                side_effect=lambda *args, **kwargs: {
                    "lode": {
                        "last_progress_at": current_time_ms() - 10,
                        "last_progress_summary": "codex thinking",
                    }
                },
            ),
        ):
            runner._check_activity()
            runner._check_activity()
            runner._check_activity()

        assert runner._stuck_since is None
        assert not any(e[0] == "lode_set_state" and e[1]["state"] == "stuck" for e in emitted)
        running_emissions = [
            e for e in emitted if e[0] == "lode_set_state" and e[1]["state"] == "running"
        ]
        assert not running_emissions or all(
            emission[1]["status"] == "codex thinking" for emission in running_emissions
        )

    def test_descendant_cpu_activity_keeps_silent_runner_alive(self, monkeypatch):
        """Increasing descendant CPU is activity while pane and heartbeat are quiet."""
        monkeypatch.setattr("hopper.runner.IDLE_THRESHOLD_MS", 100)
        times = iter([200_000, 351_000])
        monkeypatch.setattr("hopper.runner.current_time_ms", lambda: next(times))

        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "Hello World"
        runner._last_pane_activity_ms = 0
        runner._claude_proc = MagicMock(pid=1234)
        runner._claude_proc.poll.return_value = None

        emitted = []
        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        runner.connection = mock_conn

        with (
            patch("hopper.runner.capture_pane", return_value="Hello World"),
            patch(
                "hopper.runner.connect",
                return_value={"lode": {"last_progress_at": None, "last_progress_summary": None}},
            ),
            patch("hopper.runner._sum_descendant_cpu_ms", side_effect=[1000, 2000]),
            patch("hopper.runner.send_keys"),
            patch("hopper.runner.get_current_pane_id", return_value="%test"),
            patch("hopper.runner.MONITOR_INTERVAL", 0.001),
        ):
            runner._check_activity()
            runner._check_activity()

        runner._claude_proc.terminate.assert_not_called()
        assert any(
            e[0] == "lode_set_state"
            and e[1]["state"] == "running"
            and e[1]["status"] == "background work active (5m)"
            for e in emitted
        )

    def test_flat_descendant_cpu_parks_never_terminates(self, monkeypatch):
        """Flat descendant CPU does not veto the normal stuck timeout."""
        monkeypatch.setattr("hopper.runner.IDLE_THRESHOLD_MS", 100)
        monkeypatch.setattr("hopper.runner.STUCK_PARK_THRESHOLD_MS", 100)
        monkeypatch.setattr("hopper.runner.current_time_ms", lambda: 351_000)

        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "Hello World"
        runner._last_pane_activity_ms = 0
        runner._last_descendant_cpu_ms = 1000
        runner._stuck_since = 0
        runner._claude_proc = MagicMock(pid=1234)
        runner._claude_proc.poll.return_value = None

        with (
            patch("hopper.runner.capture_pane", return_value="Hello World"),
            patch(
                "hopper.runner.connect",
                return_value={"lode": {"last_progress_at": None, "last_progress_summary": None}},
            ),
            patch("hopper.runner._sum_descendant_cpu_ms", return_value=1000),
            patch("hopper.runner.send_keys"),
            patch("hopper.runner.get_current_pane_id", return_value="%test"),
            patch("hopper.runner.MONITOR_INTERVAL", 0.001),
        ):
            runner._check_activity()

        assert runner._gated.is_set()
        runner._claude_proc.terminate.assert_not_called()

    def test_real_silence_absolute_cap_parks_even_with_cpu(self, monkeypatch):
        """The absolute cap is based on pane silence, not heartbeat or CPU activity."""
        monkeypatch.setattr("hopper.runner.IDLE_THRESHOLD_MS", 100)
        monkeypatch.setattr("hopper.runner.ABSOLUTE_CAP_MS", 500)
        monkeypatch.setattr("hopper.runner.current_time_ms", lambda: 10_000)

        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "Hello World"
        runner._last_pane_activity_ms = 0
        runner._last_descendant_cpu_ms = 1000
        runner._claude_proc = MagicMock(pid=1234)
        runner._claude_proc.poll.return_value = None

        with (
            patch("hopper.runner.capture_pane", return_value="Hello World"),
            patch(
                "hopper.runner.connect",
                return_value={"lode": {"last_progress_at": None, "last_progress_summary": None}},
            ),
            patch("hopper.runner._sum_descendant_cpu_ms", return_value=2000),
            patch("hopper.runner.send_keys"),
            patch("hopper.runner.get_current_pane_id", return_value="%test"),
            patch("hopper.runner.MONITOR_INTERVAL", 0.001),
        ):
            runner._check_activity()

        assert runner._gated.is_set()
        runner._claude_proc.terminate.assert_not_called()

    def test_pane_silence_cap_parks_with_fresh_heartbeat_without_cpu_probe(self, monkeypatch):
        """Fresh heartbeats cannot hide a pane-silent stage from the absolute cap."""
        monkeypatch.setattr("hopper.runner.IDLE_THRESHOLD_MS", 100)
        monkeypatch.setattr("hopper.runner.ABSOLUTE_CAP_MS", 500)
        monkeypatch.setattr("hopper.runner.current_time_ms", lambda: 10_000)

        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "Hello World"
        runner._last_pane_activity_ms = 0

        expected = "no pane output for 0 min (sustained only by heartbeat/CPU activity)"
        with (
            patch("hopper.runner.capture_pane", return_value="Hello World"),
            patch(
                "hopper.runner.connect",
                return_value={
                    "lode": {
                        "last_progress_at": 9_990,
                        "last_progress_summary": "make ci — running 10s",
                    }
                },
            ),
            patch("hopper.runner._sum_descendant_cpu_ms") as mock_cpu,
            patch.object(runner, "_park_idle") as mock_fail,
        ):
            runner._check_activity()

        mock_cpu.assert_not_called()
        mock_fail.assert_called_once_with(expected)

    def test_refreshed_heartbeat_carries_pane_silent_stage_past_stuck_timeout(self, monkeypatch):
        """Recurring progress prevents an idle park across enough quiet ticks."""
        monkeypatch.setattr("hopper.runner.IDLE_THRESHOLD_MS", 100)
        monkeypatch.setattr("hopper.runner.STUCK_PARK_THRESHOLD_MS", 200)
        monkeypatch.setattr("hopper.runner.ABSOLUTE_CAP_MS", 10_000)
        now = [1_000]
        monkeypatch.setattr("hopper.runner.current_time_ms", lambda: now[0])

        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "Hello World"
        runner._last_pane_activity_ms = 0

        emitted = []
        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        runner.connection = mock_conn

        def heartbeat(*args, **kwargs):
            return {
                "lode": {
                    "last_progress_at": now[0] - 10,
                    "last_progress_summary": "make ci — running",
                }
            }

        with (
            patch("hopper.runner.capture_pane", return_value="Hello World"),
            patch("hopper.runner.connect", side_effect=heartbeat),
            patch("hopper.runner._sum_descendant_cpu_ms", return_value=0) as mock_cpu,
            patch.object(runner, "_park_idle") as mock_fail,
            patch("hopper.runner.send_keys"),
            patch("hopper.runner.get_current_pane_id", return_value="%test"),
            patch("hopper.runner.MONITOR_INTERVAL", 0.001),
        ):
            for tick in range(1_000, 1_701, 100):
                now[0] = tick
                runner._check_activity()

        mock_cpu.assert_not_called()
        mock_fail.assert_not_called()
        assert runner._stuck_since is None
        assert not any(
            event_type == "lode_set_state" and fields["state"] == "stuck"
            for event_type, fields in emitted
        )

    def test_parent_claude_idle_with_fresh_codex(self):
        """Fresh heartbeats keep the runner active even when the pane is older than 10 seconds."""
        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "Hello World"
        runner._last_pane_activity_ms = current_time_ms() - 30_000

        emitted = []
        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        runner.connection = mock_conn

        with (
            patch("hopper.runner.capture_pane", return_value="Hello World"),
            patch(
                "hopper.runner.connect",
                return_value={
                    "lode": {
                        "last_progress_at": current_time_ms() - 3000,
                        "last_progress_summary": "codex thinking",
                    }
                },
            ),
        ):
            runner._check_activity()

        assert runner._stuck_since is None
        assert not any(e[0] == "lode_set_state" and e[1]["state"] == "stuck" for e in emitted)

    def test_clean_handoff_from_codex_to_claude(self):
        """Pane activity cleanly takes over from stale heartbeats."""
        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "Old content"
        runner._last_pane_activity_ms = current_time_ms() - 10

        emitted = []
        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        runner.connection = mock_conn

        with (
            patch("hopper.runner.capture_pane", return_value="New content"),
            patch(
                "hopper.runner.connect",
                return_value={
                    "lode": {
                        "last_progress_at": current_time_ms() - 60_000,
                        "last_progress_summary": "codex thinking",
                    }
                },
            ),
        ):
            runner._check_activity()

        assert runner._stuck_since is None
        assert not any(e[0] == "lode_set_state" and e[1]["state"] == "stuck" for e in emitted)
        assert not any(e[0] == "lode_set_state" and e[1]["state"] == "running" for e in emitted)

        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "Old content"
        runner._stuck_since = 1000
        runner._last_pane_activity_ms = current_time_ms() - 60_000

        emitted = []
        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        runner.connection = mock_conn

        with (
            patch("hopper.runner.capture_pane", return_value="New content"),
            patch(
                "hopper.runner.connect",
                return_value={
                    "lode": {
                        "last_progress_at": current_time_ms() - 60_000,
                        "last_progress_summary": "codex thinking",
                    }
                },
            ),
        ):
            runner._check_activity()

        assert runner._stuck_since is None
        assert any(
            e[0] == "lode_set_state"
            and e[1]["state"] == "running"
            and e[1]["status"] == "Claude running"
            for e in emitted
        )

    def test_check_activity_capture_failure_does_not_stop_completion_watcher(self):
        """A monitor capture failure remains recoverable before completion."""
        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._monitor_stop.clear()

        with (
            patch("hopper.runner.capture_pane", return_value=None),
            patch("hopper.runner.send_keys"),
            patch("hopper.runner.get_current_pane_id", return_value="%test"),
            patch("hopper.runner.MONITOR_INTERVAL", 0.001),
            patch("hopper.runner.connect", return_value=None),
        ):
            runner._check_activity()

        assert not runner._monitor_stop.is_set()
        assert runner._pane_capture_failures == 1

    def test_pane_activity_emits_only_real_changes_beyond_throttle(self):
        runner = self._make_runner()
        runner._last_snapshot = "first"
        runner.connection = MagicMock()
        runner.connection.emit.return_value = True

        with (
            patch("hopper.runner.capture_pane", return_value="unused"),
            patch("hopper.runner.send_keys"),
            patch("hopper.runner.get_current_pane_id", return_value="%test"),
            patch("hopper.runner.MONITOR_INTERVAL", 0.001),
        ):
            runner._record_pane_snapshot("second", 40_000)
            runner._record_pane_snapshot("second", 80_000)
            runner._record_pane_snapshot("third", 80_000)

        assert runner.connection.emit.call_args_list == [
            call(
                "lode_set_pane_activity",
                lode_id="test-session",
                observed_at=40_000,
            ),
            call(
                "lode_set_pane_activity",
                lode_id="test-session",
                observed_at=80_000,
            ),
        ]

    def test_start_monitor_renames_window(self):
        """Monitor renames tmux window to session ID."""
        runner = self._make_runner()

        with (
            patch("hopper.runner.get_current_pane_id", return_value="%5"),
            patch("hopper.runner.rename_window") as mock_rename,
        ):
            runner._start_monitor()
            runner._stop_monitor()

        mock_rename.assert_called_once_with("%5", "test-session")
        assert runner._last_pane_activity_ms is not None

    def test_start_monitor_skips_without_tmux(self):
        """Monitor doesn't start when not in tmux."""
        runner = self._make_runner()

        with patch("hopper.runner.get_current_pane_id", return_value=None):
            runner._start_monitor()

        assert runner._monitor_thread is None

    def test_stop_monitor_handles_no_thread(self):
        """Stop monitor handles case where thread was never started."""
        runner = self._make_runner()
        runner._stop_monitor()  # Should not raise

    def test_check_activity_skips_when_done(self):
        """Monitor skips stuck detection once done event is set."""
        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "Hello World"
        runner._done.set()

        emitted = []
        mock_conn = MagicMock()
        mock_conn.emit = lambda msg_type, **kw: emitted.append((msg_type, kw)) or True
        runner.connection = mock_conn

        with (
            patch("hopper.runner.capture_pane", return_value="Hello World"),
            patch("hopper.runner.send_keys"),
            patch("hopper.runner.get_current_pane_id", return_value="%test"),
            patch("hopper.runner.MONITOR_INTERVAL", 0.001),
            patch("hopper.runner.connect", return_value=None),
        ):
            runner._check_activity()

        assert not any(e[0] == "lode_set_state" and e[1]["state"] == "stuck" for e in emitted)

    def test_check_activity_while_gated_emits_running_on_pane_change(self):
        """Once armed against the settled pane, a pane change resumes the gate."""
        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._open_gate()
        # Armed against the pane as it settled after the gate opened.
        runner._gate_snapshot = "Gate set. Review saved."
        runner._gate_armed = True
        runner._gate_epoch = 7
        runner._last_snapshot = "Gate set. Review saved."

        with (
            patch("hopper.runner.capture_pane", return_value="Gate set. Review saved.\n> go"),
            patch("hopper.runner.current_time_ms", return_value=12345),
            patch.object(runner, "_emit_state", return_value=True) as mock_emit,
        ):
            runner._check_activity()

        mock_emit.assert_called_once_with("running", "Gate resumed", gate_epoch=7)
        assert not runner._gated.is_set()
        assert runner._last_snapshot == "Gate set. Review saved.\n> go"
        assert runner._last_pane_activity_ms == 12345

    def test_gate_is_not_resumed_by_its_own_output(self):
        """A gate's own output must never read as an operator resume.

        Regression: `hop gate` prints "Gate set..." and Claude renders the end of
        its turn AFTER the gate opens. The monitor compared that against the
        pre-gate pane, called it "Gate resumed", cleared the gate, and left the
        correctly idle stage eligible for parking.
        """
        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "codex turn done (20205 tok)"  # the pre-gate pane
        runner._open_gate()

        with (
            patch("hopper.runner.capture_pane", return_value="Gate set. Review saved."),
            patch("hopper.runner.current_time_ms", return_value=12345),
            patch.object(runner, "_emit_state", return_value=True) as mock_emit,
            patch("hopper.runner.send_keys"),
            patch("hopper.runner.get_current_pane_id", return_value="%test"),
            patch("hopper.runner.MONITOR_INTERVAL", 0.001),
        ):
            runner._check_activity()

        # The gate holds. Nothing resumed; nothing was emitted.
        assert runner._gated.is_set()
        assert not runner._gate_armed
        mock_emit.assert_not_called()

    def test_gate_arms_only_after_the_pane_settles(self):
        """The detector arms against the settled pane, not the pre-gate pane."""
        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "codex turn done (20205 tok)"
        runner._open_gate()

        # Two ticks of the gate's own output, then the pane holds still.
        panes = ["Gate set. Review saved.", "Gate set. Review saved.\nSession will be resumed."]
        settled = "Gate set. Review saved.\nSession will be resumed."

        with (
            patch("hopper.runner.capture_pane", side_effect=[*panes, settled, settled]),
            patch("hopper.runner.current_time_ms", return_value=12345),
            patch.object(runner, "_emit_state", return_value=True) as mock_emit,
        ):
            runner._check_activity()  # baseline
            runner._check_activity()  # pane still moving -> re-baseline
            assert not runner._gate_armed
            runner._check_activity()  # pane held still -> arm
            assert runner._gate_armed
            runner._check_activity()  # still unchanged -> stay gated

        assert runner._gated.is_set()
        mock_emit.assert_not_called()

    def test_gated_stage_is_never_parked_while_it_waits(self, monkeypatch):
        """An open gate protects a correctly-idle stage for as long as it waits.

        The agent opens a review gate and idles at its prompt, exactly as instructed.
        The pane never changes again. It must never be parked for that.
        """
        monkeypatch.setattr("hopper.runner.IDLE_THRESHOLD_MS", 100)
        monkeypatch.setattr("hopper.runner.STUCK_PARK_THRESHOLD_MS", 200)
        monkeypatch.setattr("hopper.runner.ABSOLUTE_CAP_MS", 500)
        now = [1_000]
        monkeypatch.setattr("hopper.runner.current_time_ms", lambda: now[0])

        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "codex turn done (20205 tok)"
        runner._last_pane_activity_ms = 0
        runner._open_gate()

        with (
            patch("hopper.runner.capture_pane", return_value="Gate set. Review saved."),
            patch("hopper.runner.connect", return_value=None),
            patch.object(runner, "_emit_state", return_value=True),
            patch.object(runner, "_park_idle") as mock_park,
            patch("hopper.runner.send_keys"),
            patch("hopper.runner.get_current_pane_id", return_value="%test"),
            patch("hopper.runner.MONITOR_INTERVAL", 0.001),
        ):
            for tick in range(1_000, 5_001, 100):
                now[0] = tick
                runner._check_activity()

        mock_park.assert_not_called()
        assert runner._gated.is_set()
        assert runner._stuck_since is None

    def test_check_activity_capture_disable_recovers(self):
        """Capture failures neither stop nor permanently blind the monitor."""
        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._monitor_stop.clear()

        with (
            patch(
                "hopper.runner.capture_pane",
                side_effect=[None, None, None, "recovered"],
            ),
            patch("hopper.runner.send_keys"),
            patch("hopper.runner.get_current_pane_id", return_value="%test"),
            patch("hopper.runner.MONITOR_INTERVAL", 0.001),
            patch("hopper.runner.connect", return_value=None),
            patch.object(runner, "_park_idle") as park,
        ):
            for _ in range(3):
                runner._check_activity()
            assert runner._activity_capture_disabled
            runner._check_activity()

        assert not runner._monitor_stop.is_set()
        assert not runner._activity_capture_disabled
        park.assert_not_called()

    def test_check_activity_capture_recovery_credits_outage(self):
        """Unobservable capture time cannot count toward an immediate park."""
        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "unchanged"
        runner._last_pane_activity_ms = 1_000
        runner._stuck_since = 2_000
        now = [50_000]

        with (
            patch("hopper.runner.capture_pane", side_effect=[None, "unchanged"]),
            patch("hopper.runner.send_keys"),
            patch("hopper.runner.get_current_pane_id", return_value="%test"),
            patch("hopper.runner.MONITOR_INTERVAL", 0.001),
            patch("hopper.runner.current_time_ms", side_effect=lambda: now[0]),
            patch("hopper.runner.connect", return_value=None),
            patch.object(runner, "_park_idle") as park,
        ):
            runner._check_activity()
            now[0] = 100_000
            runner._check_activity()

        assert runner._last_pane_activity_ms == 51_000
        assert runner._stuck_since is None
        park.assert_not_called()

    def test_intermittent_capture_failures_do_not_prevent_idle_park(self, monkeypatch):
        """Known quiet time accumulates across recurring capture outages."""
        monkeypatch.setattr("hopper.runner.IDLE_THRESHOLD_MS", 100)
        monkeypatch.setattr("hopper.runner.STUCK_PARK_THRESHOLD_MS", 100)
        monkeypatch.setattr("hopper.runner.ABSOLUTE_CAP_MS", 10_000)

        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "unchanged"
        runner._last_pane_activity_ms = 1_000
        now = [1_000]
        captures = [value for _ in range(12) for value in (None, "unchanged")]

        with (
            patch("hopper.runner.capture_pane", side_effect=captures),
            patch("hopper.runner.send_keys"),
            patch("hopper.runner.get_current_pane_id", return_value="%test"),
            patch("hopper.runner.MONITOR_INTERVAL", 0.001),
            patch("hopper.runner.current_time_ms", side_effect=lambda: now[0]),
            patch("hopper.runner.connect", return_value=None),
            patch.object(runner, "_park_idle") as park,
        ):
            for pair in range(1, 12):
                now[0] = 1_000 + (pair * 2 - 1) * 20
                runner._check_activity()
                now[0] += 20
                runner._check_activity()
            park.assert_not_called()

            now[0] = 1_460
            runner._check_activity()
            now[0] = 1_480
            runner._check_activity()

        park.assert_called_once()
        assert runner._last_pane_activity_ms == 1_240
        assert runner._stuck_since == 1_360


class TestBaseRunnerServerMessages:
    """Tests for BaseRunner server message handling."""

    def test_on_server_message_sets_done(self):
        """The retained follow-up hook still recognizes its legacy signal."""
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))

        msg = {
            "type": "lode_updated",
            "lode": {"id": "test-session", "state": "completed"},
        }
        runner._on_server_message(msg)

        assert runner._done.is_set()

    def test_on_server_message_teardown_does_not_start_runner_dismissal(self):
        """The server owns exact pane closure after accepted completion."""
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))

        runner._on_server_message(
            {
                "type": "lode_updated",
                "lode": {"id": "test-session", "state": "teardown"},
            }
        )

        assert not runner._done.is_set()

    def test_on_server_message_records_gate_epoch(self):
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))

        runner._on_server_message(
            {
                "type": "lode_updated",
                "lode": {"id": "test-session", "state": "gated", "gate_epoch": 9},
            }
        )

        assert runner._gate_epoch == 9

    def test_on_server_message_disarms_gate_before_adopting_epoch(self):
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner._gate_epoch = 4
        runner._gated.set()
        runner._gate_armed = True
        observed = []
        open_gate = runner._open_gate

        def observe_then_open_gate():
            observed.append((runner._gate_epoch, runner._gate_armed))
            open_gate()

        with patch.object(runner, "_open_gate", side_effect=observe_then_open_gate):
            runner._on_server_message(
                {
                    "type": "lode_updated",
                    "lode": {"id": "test-session", "state": "gated", "gate_epoch": 9},
                }
            )

        assert observed == [(4, True)]
        assert runner._gate_epoch == 9
        assert not runner._gate_armed

    def test_on_server_message_defaults_missing_gate_epoch_to_zero(self):
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner._gate_epoch = 9

        runner._on_server_message(
            {
                "type": "lode_updated",
                "lode": {"id": "test-session", "state": "running"},
            }
        )

        assert runner._gate_epoch == 0

    def test_on_server_message_ignores_other_lodes(self):
        """Callback ignores messages for other sessions."""
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))

        msg = {
            "type": "lode_updated",
            "lode": {"id": "other-session", "state": "completed"},
        }
        runner._on_server_message(msg)

        assert not runner._done.is_set()

    def test_on_server_message_sets_gated(self):
        """Callback sets _gated when gated state received."""
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))

        msg = {
            "type": "lode_updated",
            "lode": {"id": "test-session", "state": "gated"},
        }
        runner._on_server_message(msg)

        assert runner._gated.is_set()
        assert not runner._done.is_set()

    def test_on_server_message_running_clears_gated(self):
        """Callback clears _gated when running state received."""
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner._gated.set()

        msg = {
            "type": "lode_updated",
            "lode": {"id": "test-session", "state": "running"},
        }
        runner._on_server_message(msg)

        assert not runner._gated.is_set()
        assert not runner._done.is_set()

    def test_on_server_message_ignores_other_states(self):
        """Callback ignores states unrelated to gate control."""
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))

        msg = {
            "type": "lode_updated",
            "lode": {"id": "test-session", "state": "running"},
        }
        runner._on_server_message(msg)

        assert not runner._done.is_set()
        assert not runner._gated.is_set()

    def test_on_server_message_ignores_other_message_types(self):
        """Callback ignores non-lode-updated messages."""
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))

        msg = {
            "type": "backlog_added",
            "lode": {"id": "test-session", "state": "completed"},
        }
        runner._on_server_message(msg)

        assert not runner._done.is_set()


class TestBaseRunnerDismiss:
    """Tests for BaseRunner auto-dismiss behavior."""

    @pytest.fixture(autouse=True)
    def isolate_tmux(self):
        with (
            patch("hopper.runner.capture_pane", return_value="stable"),
            patch("hopper.runner.send_keys", return_value=True),
            patch("hopper.runner.get_current_pane_id", return_value="%test"),
            patch("hopper.runner.MONITOR_INTERVAL", 0.001),
        ):
            yield

    def test_wait_and_dismiss_sends_ctrl_c(self):
        """Dismiss thread sends two Ctrl-C keystrokes after screen stabilizes."""
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner._pane_id = "%1"
        runner._done.set()

        send_keys_calls = []

        def on_send_keys(w, k):
            send_keys_calls.append((w, k))
            # Simulate process exit after the initial Ctrl-C pair.
            if len(send_keys_calls) == 2:
                runner._monitor_stop.set()
            return True

        snapshots = iter(["content A", "content A"])
        with (
            patch("hopper.runner.capture_pane", side_effect=lambda _: next(snapshots)),
            patch("hopper.runner.send_keys", side_effect=on_send_keys),
        ):
            runner._wait_and_dismiss_claude()

        assert send_keys_calls == [("%1", "C-c"), ("%1", "C-c")]

    def test_resumed_stage_sends_no_keys_until_completed_broadcast(self):
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner.is_first_run = False
        proc = MagicMock(returncode=0, stderr=None)
        dismissed = threading.Event()

        def send_key(*_args):
            if send.call_count == 2:
                runner._monitor_stop.set()
                dismissed.set()
            return True

        def wait_for_process():
            send.assert_not_called()
            runner._on_server_message(
                {
                    "type": "lode_updated",
                    "lode": {"id": "test-session", "state": "completed"},
                }
            )
            assert dismissed.wait(timeout=1)

        proc.wait.side_effect = wait_for_process
        with (
            patch.object(runner, "_build_command", return_value=(["claude"], None)),
            patch("hopper.runner.subprocess.Popen", return_value=proc),
            patch.object(runner, "_emit_state"),
            patch.object(
                runner, "_start_monitor", side_effect=lambda: setattr(runner, "_pane_id", "%1")
            ),
            patch("hopper.runner.capture_pane", return_value="stable"),
            patch("hopper.runner.send_keys", side_effect=send_key) as send,
        ):
            assert runner._run_claude() == (0, None)

        assert [call.args[1] for call in send.call_args_list] == ["C-c", "C-c"]

    def test_wait_and_dismiss_no_longer_exits_on_gate(self):
        """Dismiss loop still waits for completion even when gated."""
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner._pane_id = "%1"
        runner._gated.set()

        wait_calls = []

        def on_wait(timeout):
            wait_calls.append(timeout)
            raise RuntimeError("waited")

        try:
            with (
                patch.object(runner._done, "wait", side_effect=on_wait),
                patch("hopper.runner.capture_pane") as mock_capture,
                patch("hopper.runner.send_keys") as mock_send_keys,
            ):
                runner._wait_and_dismiss_claude()
        except RuntimeError as exc:
            assert str(exc) == "waited"
        else:
            raise AssertionError("Expected wait to be called")

        assert wait_calls == [1.0]
        mock_capture.assert_not_called()
        mock_send_keys.assert_not_called()

    def test_wait_and_dismiss_retries_when_process_survives(self):
        """Every retry re-sends the Ctrl-C pair; Ctrl-D exits Claude from no state."""
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner._pane_id = "%1"
        runner._done.set()

        send_keys_calls = []

        # First attempt: stable screen, Ctrl-C sent but process survives
        # Screen changes (Claude still outputting), then stabilizes again
        # Second attempt: Ctrl-D sent, process exits
        snapshots = iter(
            [
                "prompt v1",
                "prompt v1",  # first stability → Ctrl-C
                "new output",  # screen changed, not stable
                "prompt v2",
                "prompt v2",  # second stability → another Ctrl-C pair
            ]
        )

        def on_send_keys(w, k):
            send_keys_calls.append((w, k))
            if len(send_keys_calls) == 4:
                runner._monitor_stop.set()
            return True

        with (
            patch("hopper.runner.capture_pane", side_effect=lambda _: next(snapshots)),
            patch("hopper.runner.send_keys", side_effect=on_send_keys),
        ):
            runner._wait_and_dismiss_claude()

        assert send_keys_calls == [
            ("%1", "C-c"),
            ("%1", "C-c"),
            ("%1", "C-c"),
            ("%1", "C-c"),
        ]

    def test_dismiss_never_sends_ctrl_d(self):
        """Ctrl-D is inert against Claude Code, so it must never be a retry step.

        Measured against a live pane: Ctrl-D exits from no state -- not an idle
        prompt, not a composer holding unsubmitted text, not the exit
        confirmation a Ctrl-C raises while a tool call is running. Sending it
        instead of the Ctrl-C pair on attempt 2+ is what turned one mistimed
        dismissal into a permanently stalled stage.
        """
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner._pane_id = "%1"
        runner._done.set()
        sends = []

        def on_send_keys(_w, k):
            sends.append(k)
            if len(sends) >= 12:
                runner._monitor_stop.set()
            return True

        with (
            patch("hopper.runner.capture_pane", return_value="stable"),
            patch("hopper.runner.send_keys", side_effect=on_send_keys),
        ):
            runner._wait_and_dismiss_claude()

        assert sends, "dismiss sent nothing at all"
        assert "C-d" not in sends
        assert set(sends) == {"C-c"}
        assert len(sends) % 2 == 0, "Ctrl-C must always be sent as a pair"

    def test_wait_and_dismiss_acts_after_capture_failure_limit(self):
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner._pane_id = "%1"
        runner._done.set()

        def stop_after_pair(*_args):
            if send.call_count == 2:
                runner._monitor_stop.set()
            return True

        with (
            patch("hopper.runner.capture_pane", return_value=None) as capture,
            patch("hopper.runner.send_keys", side_effect=stop_after_pair) as send,
        ):
            runner._wait_and_dismiss_claude()

        assert capture.call_count == 3
        assert [call.args[1] for call in send.call_args_list] == ["C-c", "C-c"]

    def test_wait_and_dismiss_acts_when_stabilization_bound_expires(self):
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner._pane_id = "%1"
        runner._done.set()

        def stop_after_pair(*_args):
            if send.call_count == 2:
                runner._monitor_stop.set()
            return True

        with (
            patch("hopper.runner.DISMISS_STABILIZATION_TIMEOUT_SEC", 0),
            patch("hopper.runner.send_keys", side_effect=stop_after_pair) as send,
        ):
            runner._wait_and_dismiss_claude()

        assert [call.args[1] for call in send.call_args_list] == ["C-c", "C-c"]

    def test_wait_and_dismiss_aborts_when_monitor_stops(self):
        """Dismiss thread aborts if monitor stop is set."""
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner._pane_id = "%1"
        runner._monitor_stop.set()

        send_keys_calls = []
        with patch(
            "hopper.runner.send_keys",
            side_effect=lambda w, k: send_keys_calls.append((w, k)),
        ):
            runner._wait_and_dismiss_claude()

        assert send_keys_calls == []

    def test_wait_and_dismiss_aborts_without_pane(self):
        """Dismiss thread aborts if no pane ID."""
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner._pane_id = None
        runner._done.set()

        send_keys_calls = []
        with patch(
            "hopper.runner.send_keys",
            side_effect=lambda w, k: send_keys_calls.append((w, k)),
        ):
            runner._wait_and_dismiss_claude()

        assert send_keys_calls == []

    def test_wait_and_dismiss_pauses_while_parked_then_resumes(self):
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner._pane_id = "%1"
        runner._done.set()
        runner._done_at_ms = 1_000
        runner._dismiss_deadline_parked = True
        runner._dismiss_attempt = 1
        now = [2_000]
        wait_calls = []

        def on_wait(timeout):
            wait_calls.append(timeout)
            if len(wait_calls) > 3:
                raise AssertionError("dismiss worker did not resume after completion re-arm")
            if len(wait_calls) == 1:
                send.assert_not_called()
                runner._on_server_message(
                    {
                        "type": "lode_updated",
                        "lode": {"id": "test-session", "state": "completed"},
                    }
                )
            return False

        def on_send_keys(*_args):
            runner._monitor_stop.set()
            return True

        with (
            patch.object(runner._monitor_stop, "wait", side_effect=on_wait),
            patch("hopper.runner.current_time_ms", side_effect=lambda: now[0]),
            patch("hopper.runner.time.monotonic", return_value=0),
            patch("hopper.runner.send_keys", side_effect=on_send_keys) as send,
        ):
            runner._wait_and_dismiss_claude()

        assert wait_calls[0] == 1.0
        assert runner._done_at_ms == 2_000
        assert not runner._dismiss_deadline_parked
        assert [entry.args[1] for entry in send.call_args_list] == ["C-c", "C-c"]

    def test_wait_and_dismiss_restarts_stabilization_after_rearm(self):
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner._pane_id = "%1"
        runner._dismiss_attempt = 1
        now = [1_000]
        wait_calls = []
        capture_calls = [0]
        sends = []
        message = {
            "type": "lode_updated",
            "lode": {"id": "test-session", "state": "completed"},
        }

        def on_wait(_timeout):
            wait_calls.append(_timeout)
            if len(wait_calls) > 4:
                raise AssertionError("dismiss worker did not finish fresh stabilization")
            return False

        def on_capture(_pane_id):
            capture_calls[0] += 1
            if capture_calls[0] == 2:
                now[0] = 1_000 + DISMISS_DEADLINE_MS
                runner._check_activity()
                assert runner._dismiss_deadline_parked
                now[0] = 400_000
                runner._on_server_message(message)
                assert not runner._dismiss_deadline_parked
            return "stable"

        def on_send_keys(_pane_id, key):
            sends.append((capture_calls[0], key))
            runner._monitor_stop.set()
            return True

        with (
            patch.object(runner._monitor_stop, "wait", side_effect=on_wait),
            patch("hopper.runner.current_time_ms", side_effect=lambda: now[0]),
            patch("hopper.runner.time.monotonic", return_value=0),
            patch.object(runner, "_park_idle") as park,
            patch("hopper.runner.capture_pane", side_effect=on_capture),
            patch("hopper.runner.send_keys", side_effect=on_send_keys),
        ):
            runner._on_server_message(message)
            runner._wait_and_dismiss_claude()

        park.assert_called_once_with(
            "completion was signalled, but the agent did not exit within 5 min"
        )
        assert sends == [(4, "C-c"), (4, "C-c")]

    def test_parked_completion_ignores_pane_changes_without_rearming(self):
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner._pane_id = "%1"
        runner._last_snapshot = "before park"
        now = [1_000]

        with (
            patch("hopper.runner.current_time_ms", side_effect=lambda: now[0]),
            patch("hopper.runner.capture_pane", return_value="operator changed pane") as capture,
            patch.object(runner, "_park_idle") as park,
        ):
            runner._on_server_message(
                {
                    "type": "lode_updated",
                    "lode": {"id": "test-session", "state": "completed"},
                }
            )
            now[0] += DISMISS_DEADLINE_MS
            runner._check_activity()

            done_at_ms = runner._done_at_ms
            now[0] += DISMISS_DEADLINE_MS
            runner._check_activity()

        assert runner._dismiss_deadline_parked
        assert runner._done_at_ms == done_at_ms
        park.assert_called_once_with(
            "completion was signalled, but the agent did not exit within 5 min"
        )
        capture.assert_not_called()


def test_completion_deadline_latch_preserves_done_and_allows_advance():
    runner = BaseRunner("test-session", Path("/tmp/test.sock"))
    runner._pane_id = "%1"
    runner._done.set()
    runner._done_at_ms = 1_000
    runner._next_stage = "refine"
    runner._registration_complete.set()
    runner._registration_accepted = True

    with (
        patch("hopper.runner.capture_pane", return_value="stable"),
        patch("hopper.runner.send_keys"),
        patch("hopper.runner.get_current_pane_id", return_value="%test"),
        patch("hopper.runner.MONITOR_INTERVAL", 0.001),
        patch("hopper.runner.current_time_ms", return_value=301_000),
        patch.object(runner, "_park_idle") as park,
    ):
        runner._check_activity()
        runner._check_activity()

    assert runner._dismiss_deadline_parked
    assert runner._done.is_set()
    park.assert_called_once_with(
        "completion was signalled, but the agent did not exit within 5 min"
    )

    lode = {"active": False, "claude": {"": {}}, "project": ""}
    connection = MagicMock()
    with (
        patch("hopper.runner.capture_pane", return_value="stable"),
        patch("hopper.runner.send_keys"),
        patch("hopper.runner.get_current_pane_id", return_value="%test"),
        patch("hopper.runner.MONITOR_INTERVAL", 0.001),
        patch("hopper.runner.signal.signal", return_value=signal.SIG_DFL),
        patch("hopper.runner.reap_swiftpm_testing_helpers"),
        patch("hopper.runner.connect", return_value={"lode": lode}),
        patch("hopper.runner.HopperConnection", return_value=connection),
        patch.object(runner, "_setup", return_value=None),
        patch.object(runner, "_run_claude", return_value=(0, None)),
        patch.object(runner, "_emit_state", return_value=True) as emit_state,
        patch.object(runner, "_emit_stage") as emit_stage,
        patch.object(runner, "_stop_monitor"),
        patch.object(runner, "_terminate_claude_process"),
    ):
        assert runner.run() == 0

    emit_state.assert_called_once_with("ready", "Done")
    emit_stage.assert_called_once_with("refine")


def test_completion_deadline_rearms_and_allows_advance():
    now = [1_000]
    message = {
        "type": "lode_updated",
        "lode": {"id": "test-session", "state": "completed"},
    }

    with (
        patch("hopper.runner.capture_pane", return_value="stable"),
        patch("hopper.runner.send_keys"),
        patch("hopper.runner.get_current_pane_id", return_value="%test"),
        patch("hopper.runner.MONITOR_INTERVAL", 0.001),
        patch("hopper.runner.current_time_ms", side_effect=lambda: now[0]),
    ):
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner._pane_id = "%1"
        runner._next_stage = "refine"
        runner._registration_complete.set()
        runner._registration_accepted = True

        with patch.object(runner, "_park_idle") as park:
            runner._on_server_message(message)
            assert runner._done.is_set()
            assert runner._done_at_ms == 1_000

            now[0] = 2_000
            runner._on_server_message(message)
            assert runner._done_at_ms == 1_000

            now[0] = 1_000 + DISMISS_DEADLINE_MS
            runner._check_activity()
            assert runner._dismiss_deadline_parked
            park.assert_called_once()

            second_signal_ms = 400_000
            now[0] = second_signal_ms
            runner._on_server_message(message)
            assert runner._done.is_set()
            assert runner._done_at_ms == second_signal_ms
            assert not runner._dismiss_deadline_parked

            runner._check_activity()
            now[0] = second_signal_ms + DISMISS_DEADLINE_MS - 1
            runner._check_activity()
            park.assert_called_once()

            now[0] = second_signal_ms + DISMISS_DEADLINE_MS
            runner._check_activity()
            assert runner._done.is_set()
            assert runner._dismiss_deadline_parked
            assert park.call_count == 2

            lode = {"active": False, "claude": {"": {}}, "project": ""}
            connection = MagicMock()
            with (
                patch("hopper.runner.signal.signal", return_value=signal.SIG_DFL),
                patch("hopper.runner.reap_swiftpm_testing_helpers"),
                patch("hopper.runner.connect", return_value={"lode": lode}),
                patch("hopper.runner.HopperConnection", return_value=connection),
                patch.object(runner, "_setup", return_value=None),
                patch.object(runner, "_run_claude", return_value=(0, None)),
                patch.object(runner, "_emit_state", return_value=True) as emit_state,
                patch.object(runner, "_emit_stage") as emit_stage,
                patch.object(runner, "_stop_monitor"),
                patch.object(runner, "_terminate_claude_process"),
            ):
                assert runner.run() == 0

        assert runner._done.is_set()
        emit_state.assert_called_once_with("ready", "Done")
        emit_stage.assert_called_once_with("refine")


def test_healthy_completion_advances_without_park_stuck_or_gated():
    now = [1_000]

    with (
        patch("hopper.runner.capture_pane", return_value="stable"),
        patch("hopper.runner.send_keys"),
        patch("hopper.runner.get_current_pane_id", return_value="%test"),
        patch("hopper.runner.MONITOR_INTERVAL", 0.001),
        patch("hopper.runner.current_time_ms", side_effect=lambda: now[0]),
    ):
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner._next_stage = "refine"
        runner._registration_complete.set()
        runner._registration_accepted = True
        runner._on_server_message(
            {
                "type": "lode_updated",
                "lode": {"id": "test-session", "state": "completed"},
            }
        )

        lode = {"active": False, "claude": {"": {}}, "project": ""}
        connection = MagicMock()
        with (
            patch("hopper.runner.signal.signal", return_value=signal.SIG_DFL),
            patch("hopper.runner.reap_swiftpm_testing_helpers"),
            patch("hopper.runner.connect", return_value={"lode": lode}),
            patch("hopper.runner.HopperConnection", return_value=connection),
            patch.object(runner, "_setup", return_value=None),
            patch.object(runner, "_run_claude", return_value=(0, None)),
            patch.object(runner, "_emit_state", return_value=True) as emit_state,
            patch.object(runner, "_emit_stage") as emit_stage,
            patch.object(runner, "_park_idle") as park,
            patch.object(runner, "_stop_monitor"),
            patch.object(runner, "_terminate_claude_process"),
        ):
            assert runner.run() == 0

    states = [entry.args[0] for entry in emit_state.call_args_list]
    assert states == ["ready"]
    assert "stuck" not in states
    assert "gated" not in states
    emit_state.assert_called_once_with("ready", "Done")
    emit_stage.assert_called_once_with("refine")
    park.assert_not_called()
