# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the base runner module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from hopper.lodes import current_time_ms
from hopper.runner import BaseRunner, extract_error_message


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


class TestBaseRunnerActivityMonitor:
    """Tests for BaseRunner activity monitor shared behavior."""

    def _make_runner(self):
        runner = BaseRunner("test-session", Path("/tmp/test.sock"))
        return runner

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
