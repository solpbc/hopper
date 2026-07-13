# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the base runner module."""

import json
import signal
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from hopper.lodes import current_time_ms, get_lode_dir
from hopper.runner import (
    BaseRunner,
    _descendant_pids,
    _parse_ps_time,
    _sum_descendant_cpu_ms,
    extract_error_message,
)


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
        )

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

    def test_base_stuck_kill_writes_no_worktree_recovery(self):
        runner = self._make_runner()
        runner._claude_stage = "mill"

        with (
            patch("hopper.runner.current_time_ms", return_value=1234),
            patch.object(runner, "_terminate_claude_process"),
        ):
            runner._fail_stuck("stuck reason")

        record = json.loads((get_lode_dir("test-session") / "recovery.json").read_text())
        assert record == {
            "failed_at": 1234,
            "stage": "mill",
            "reason": "stuck reason",
            "branch": None,
            "worktree_path": None,
            "snapshot": {"outcome": "no_worktree"},
        }
        assert runner._stuck_error == (
            "stuck reason Recovery branch unavailable; no worktree existed for stage mill, "
            "so no snapshot was created. Restart with: hop lode restart test-session"
        )
        assert runner._monitor_stop.is_set()
        assert runner._stuck_failure_complete.is_set()

    def test_format_stuck_error_outcomes(self):
        runner = self._make_runner()
        reason = "stuck reason"
        record = {
            "stage": "refine",
            "branch": "hopper-test",
            "worktree_path": "/tmp/worktree",
        }

        assert runner._format_stuck_error(
            reason, {**record, "snapshot": {"outcome": "committed", "sha": "abc123"}}
        ) == (
            "stuck reason Recovery snapshot committed on branch hopper-test at abc123. "
            "Restart with: hop lode restart test-session"
        )
        assert runner._format_stuck_error(reason, {**record, "snapshot": {"outcome": "clean"}}) == (
            "stuck reason Recovery branch hopper-test; worktree was clean, so no snapshot "
            "commit was created. Restart with: hop lode restart test-session"
        )
        assert runner._format_stuck_error(
            reason, {**record, "snapshot": {"outcome": "no_worktree"}}
        ) == (
            "stuck reason Recovery branch unavailable; no worktree existed for stage refine, "
            "so no snapshot was created. Restart with: hop lode restart test-session"
        )
        assert runner._format_stuck_error(
            reason,
            {
                **record,
                "snapshot": {"outcome": "failed", "git_error": "index locked"},
            },
        ) == (
            "stuck reason Recovery snapshot failed on branch hopper-test: index locked. "
            "Inspect /tmp/worktree before restarting with: hop lode restart test-session"
        )

    def test_recovery_write_failure_is_appended_to_enriched_error(self):
        runner = self._make_runner()
        runner._claude_stage = "mill"

        with (
            patch.object(runner, "_terminate_claude_process"),
            patch("hopper.runner._write_recovery_record", side_effect=OSError("disk full")),
        ):
            runner._fail_stuck("stuck reason")

        assert runner._stuck_error == (
            "stuck reason Recovery branch unavailable; no worktree existed for stage mill, "
            "so no snapshot was created. Restart with: hop lode restart test-session "
            "Recovery record could not be written: disk full."
        )
        assert runner._monitor_stop.is_set()
        assert runner._stuck_failure_complete.is_set()

    def test_run_claude_waits_for_enriched_stuck_error(self):
        runner = self._make_runner()
        runner._claude_stage = "mill"
        proc = MagicMock(returncode=1, stderr=None)
        snapshot_started = threading.Event()
        release_snapshot = threading.Event()
        failure_threads = []

        def slow_snapshot():
            snapshot_started.set()
            assert release_snapshot.wait(timeout=1)
            return {"outcome": "no_worktree"}

        def wait_for_process():
            thread = threading.Thread(target=runner._fail_stuck, args=("stuck reason",))
            failure_threads.append(thread)
            thread.start()
            assert snapshot_started.wait(timeout=1)

        proc.wait.side_effect = wait_for_process
        result = []

        with (
            patch.object(runner, "_build_command", return_value=(["claude"], None)),
            patch("hopper.runner.subprocess.Popen", return_value=proc),
            patch.object(runner, "_emit_state"),
            patch.object(runner, "_start_monitor"),
            patch.object(runner, "_terminate_claude_process"),
            patch.object(runner, "_snapshot_stuck_worktree", side_effect=slow_snapshot),
            patch("hopper.runner._write_recovery_record"),
        ):
            run_thread = threading.Thread(target=lambda: result.append(runner._run_claude()))
            run_thread.start()
            assert snapshot_started.wait(timeout=1)
            assert run_thread.is_alive()
            release_snapshot.set()
            run_thread.join(timeout=1)

        for thread in failure_threads:
            thread.join(timeout=1)

        assert not run_thread.is_alive()
        assert result == [
            (
                1,
                "stuck reason Recovery branch unavailable; no worktree existed for stage mill, "
                "so no snapshot was created. Restart with: hop lode restart test-session",
            )
        ]

    def test_run_claude_stuck_recovery_wait_timeout_returns_current_error(self, caplog):
        runner = self._make_runner()
        runner._stuck_error = "stuck reason"
        proc = MagicMock(returncode=1, stderr=None)

        with (
            patch.object(runner, "_build_command", return_value=(["claude"], None)),
            patch("hopper.runner.subprocess.Popen", return_value=proc),
            patch.object(runner, "_emit_state"),
            patch.object(runner, "_start_monitor"),
            patch("hopper.runner.STUCK_FAILURE_WAIT_SEC", 0),
        ):
            result = runner._run_claude()

        assert result == (1, "stuck reason")
        assert "timed out waiting for stuck recovery lode=test-session" in caplog.messages

    def test_subprocess_env_disables_claude_auto_memory(self):
        """Managed Hopper stages disable Claude Code auto-memory."""
        runner = self._make_runner()

        env = runner._get_subprocess_env()

        assert env["HOPPER_LID"] == "test-session"
        assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        assert env["CLAUDE_CODE_DISABLE_MEMORY_PERIODIC_RESYNC"] == "1"
        assert env["CLAUDE_CODE_DISABLE_MEMORY_BULK_INFLATE"] == "1"

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

    def test_check_activity_gates_numbered_question_instead_of_killing(self):
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

        with patch("hopper.runner.capture_pane", return_value=snapshot):
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
                        }
                    },
                ),
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

    def test_stuck_timeout_terminates_claude_process(self, monkeypatch):
        """A long-stuck active lode terminates its Claude process."""
        monkeypatch.setattr("hopper.runner.IDLE_THRESHOLD_MS", 100)
        monkeypatch.setattr("hopper.runner.STUCK_FAIL_THRESHOLD_MS", 100)

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
        ):
            runner._check_activity()

        assert runner._stuck_error is not None
        assert "timed out stuck Claude stage" in runner._stuck_error
        runner._claude_proc.terminate.assert_called_once()
        runner._claude_proc.wait.assert_called_once_with(timeout=5)
        assert runner._monitor_stop.is_set()

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
        ):
            runner._check_activity()
            runner._check_activity()

        assert runner._stuck_error is None
        runner._claude_proc.terminate.assert_not_called()
        assert any(
            e[0] == "lode_set_state"
            and e[1]["state"] == "running"
            and e[1]["status"] == "background work active (5m)"
            for e in emitted
        )

    def test_flat_descendant_cpu_still_times_out(self, monkeypatch):
        """Flat descendant CPU does not veto the normal stuck timeout."""
        monkeypatch.setattr("hopper.runner.IDLE_THRESHOLD_MS", 100)
        monkeypatch.setattr("hopper.runner.STUCK_FAIL_THRESHOLD_MS", 100)
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
        ):
            runner._check_activity()

        assert runner._stuck_error is not None
        assert "timed out stuck Claude stage" in runner._stuck_error
        runner._claude_proc.terminate.assert_called_once()

    def test_real_silence_absolute_cap_terminates_even_with_cpu(self, monkeypatch):
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
        ):
            runner._check_activity()

        assert runner._stuck_error is not None
        assert "pane-silence cap" in runner._stuck_error
        runner._claude_proc.terminate.assert_called_once()

    def test_pane_silence_cap_terminates_with_fresh_heartbeat_without_cpu_probe(self, monkeypatch):
        """Fresh heartbeats cannot hide a pane-silent stage from the absolute cap."""
        monkeypatch.setattr("hopper.runner.IDLE_THRESHOLD_MS", 100)
        monkeypatch.setattr("hopper.runner.ABSOLUTE_CAP_MS", 500)
        monkeypatch.setattr("hopper.runner.current_time_ms", lambda: 10_000)

        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "Hello World"
        runner._last_pane_activity_ms = 0

        expected = (
            "Exceeded 0-min pane-silence cap; stage was sustained only by heartbeat/CPU "
            "activity with no pane output."
        )
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
            patch.object(runner, "_fail_stuck") as mock_fail,
        ):
            runner._check_activity()

        mock_cpu.assert_not_called()
        mock_fail.assert_called_once_with(expected)

    def test_refreshed_heartbeat_carries_pane_silent_stage_past_stuck_timeout(self, monkeypatch):
        """Recurring progress prevents the ordinary stuck kill across enough quiet ticks."""
        monkeypatch.setattr("hopper.runner.IDLE_THRESHOLD_MS", 100)
        monkeypatch.setattr("hopper.runner.STUCK_FAIL_THRESHOLD_MS", 200)
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
            patch.object(runner, "_fail_stuck") as mock_fail,
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

    def test_check_activity_stops_on_capture_failure(self):
        """Monitor stops when pane capture fails."""
        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._monitor_stop.clear()

        with (
            patch("hopper.runner.capture_pane", return_value=None),
            patch("hopper.runner.connect", return_value=None),
        ):
            runner._check_activity()

        assert runner._monitor_stop.is_set()

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
            patch("hopper.runner.connect", return_value=None),
        ):
            runner._check_activity()

        assert not any(e[0] == "lode_set_state" and e[1]["state"] == "stuck" for e in emitted)

    def test_check_activity_while_gated_emits_running_on_pane_change(self):
        """Gated monitor emits running and clears gate when pane changes."""
        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._last_snapshot = "Hello World"

        with (
            patch.object(runner._gated, "is_set", return_value=True),
            patch.object(runner._gated, "clear") as mock_clear,
            patch("hopper.runner.capture_pane", return_value="Hello World 2"),
            patch("hopper.runner.current_time_ms", return_value=12345),
            patch.object(runner, "_emit_state", return_value=True) as mock_emit,
        ):
            runner._check_activity()

        mock_emit.assert_called_once_with("running", "Gate resumed")
        mock_clear.assert_called_once_with()
        assert runner._last_snapshot == "Hello World 2"
        assert runner._last_pane_activity_ms == 12345

    def test_check_activity_while_gated_dead_pane_sets_monitor_stop(self):
        """Gated monitor stops if pane capture fails."""
        runner = self._make_runner()
        runner._pane_id = "%1"
        runner._monitor_stop.clear()

        with (
            patch.object(runner._gated, "is_set", return_value=True),
            patch("hopper.runner.capture_pane", return_value=None),
        ):
            runner._check_activity()

        assert runner._monitor_stop.is_set()


class TestBaseRunnerServerMessages:
    """Tests for BaseRunner server message handling."""

    def test_on_server_message_sets_done(self):
        """Callback sets _done when completed state received."""
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))

        msg = {
            "type": "lode_updated",
            "lode": {"id": "test-session", "state": "completed"},
        }
        runner._on_server_message(msg)

        assert runner._done.is_set()

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
        """Callback ignores non-completed states."""
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

    def test_wait_and_dismiss_sends_ctrl_c(self):
        """Dismiss thread sends two Ctrl-D after screen stabilizes."""
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner._pane_id = "%1"
        runner._done.set()

        send_keys_calls = []

        def on_send_keys(w, k):
            send_keys_calls.append((w, k))
            # Simulate process exit after Ctrl-D pair
            if len(send_keys_calls) == 2:
                runner._monitor_stop.set()
            return True

        snapshots = iter(["content A", "content A"])
        with (
            patch("hopper.runner.capture_pane", side_effect=lambda _: next(snapshots)),
            patch("hopper.runner.send_keys", side_effect=on_send_keys),
            patch("hopper.runner.MONITOR_INTERVAL", 0.01),
        ):
            runner._wait_and_dismiss_claude()

        assert send_keys_calls == [("%1", "C-c"), ("%1", "C-c")]

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
        """Dismiss retries Ctrl-D if process doesn't exit after first attempt."""
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        runner._pane_id = "%1"
        runner._done.set()

        send_keys_calls = []

        # First attempt: stable screen, Ctrl-D sent but process survives
        # Screen changes (Claude still outputting), then stabilizes again
        # Second attempt: Ctrl-D sent, process exits
        snapshots = iter(
            [
                "prompt v1",
                "prompt v1",  # first stability → Ctrl-D
                "new output",  # screen changed, not stable
                "prompt v2",
                "prompt v2",  # second stability → Ctrl-D
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
            patch("hopper.runner.MONITOR_INTERVAL", 0.01),
        ):
            runner._wait_and_dismiss_claude()

        # Two Ctrl-D pairs: first attempt failed, second succeeded
        assert send_keys_calls == [
            ("%1", "C-c"),
            ("%1", "C-c"),
            ("%1", "C-c"),
            ("%1", "C-c"),
        ]

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
