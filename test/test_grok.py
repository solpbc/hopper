# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for Hopper's Grok CLI wrapper."""

import io
import json
from unittest.mock import MagicMock, patch

from hopper.grok import GROK_FLAGS, bootstrap_grok, grok_failure_message, run_grok

SESSION_ID = "00000000-0000-0000-0000-000000000123"


def _line(event: dict) -> str:
    return json.dumps(event) + "\n"


def _stream(*events: dict) -> io.StringIO:
    return io.StringIO("".join(_line(event) for event in events))


def test_grok_flags_match_hopper_permission_boundary_and_do_not_pin_a_model():
    assert "--trust" in GROK_FLAGS
    assert "--permission-mode" in GROK_FLAGS
    assert "bypassPermissions" in GROK_FLAGS
    assert "--sandbox" in GROK_FLAGS
    assert "off" in GROK_FLAGS
    assert "--disallowed-tools" in GROK_FLAGS
    assert "ask_user_question" in GROK_FLAGS
    assert "--no-auto-update" in GROK_FLAGS
    assert "--model" not in GROK_FLAGS
    assert "--max-turns" not in GROK_FLAGS


def test_bootstrap_grok_validates_end_event_and_returns_hopper_session_id():
    proc = MagicMock(returncode=0)
    proc.communicate.return_value = (
        _line({"type": "text", "data": "ready"})
        + _line({"type": "end", "sessionId": SESSION_ID, "stopReason": "end_turn"}),
        "",
    )
    with (
        patch("hopper.grok.uuid.uuid4", return_value=SESSION_ID),
        patch("hopper.grok.subprocess.Popen", return_value=proc) as popen,
    ):
        result = bootstrap_grok("prompt", "/work")

    assert result == (0, SESSION_ID, None)
    command = popen.call_args.args[0]
    assert command[:1] == ["grok"]
    assert command[-4:] == ["--session-id", SESSION_ID, "-p", "prompt"]
    assert "--model" not in command


def test_bootstrap_grok_rejects_mismatched_terminal_session():
    proc = MagicMock(returncode=0)
    proc.communicate.return_value = (
        _line({"type": "text", "data": "ready"})
        + _line({"type": "end", "sessionId": "other", "stopReason": "end_turn"}),
        "",
    )
    with (
        patch("hopper.grok.uuid.uuid4", return_value=SESSION_ID),
        patch("hopper.grok.subprocess.Popen", return_value=proc),
    ):
        exit_code, session_id, failure = bootstrap_grok("prompt", "/work")

    assert exit_code == 1
    assert session_id is None
    assert "did not match" in failure


def test_bootstrap_grok_rejects_native_error_even_if_process_exits_zero():
    proc = MagicMock(returncode=0)
    proc.communicate.return_value = (
        _line({"type": "error", "message": "authentication required"})
        + _line({"type": "end", "sessionId": SESSION_ID, "stopReason": "end_turn"}),
        "",
    )
    with (
        patch("hopper.grok.uuid.uuid4", return_value=SESSION_ID),
        patch("hopper.grok.subprocess.Popen", return_value=proc),
    ):
        assert bootstrap_grok("prompt", "/work") == (
            1,
            None,
            "authentication required",
        )


def test_run_grok_retains_raw_events_and_writes_only_final_post_tool_text(tmp_path):
    output_path = tmp_path / "audit.out.md"
    events = [
        {"type": "text", "data": "I will inspect."},
        {
            "type": "tool_call",
            "toolCallId": "tool-1",
            "toolName": "bash",
            "status": "pending",
            "rawInput": {"command": "make test"},
        },
        {"type": "tool_call_update", "toolCallId": "tool-1", "status": "completed"},
        {"type": "text", "data": "All tests pass."},
        {"type": "end", "sessionId": SESSION_ID, "stopReason": "end_turn"},
    ]
    proc = MagicMock(returncode=0)
    proc.stdout = _stream(*events)
    proc.stderr = io.StringIO("")
    observed = []
    with patch("hopper.grok.subprocess.Popen", return_value=proc) as popen:
        exit_code, command = run_grok(
            "continue",
            "/work",
            str(output_path),
            SESSION_ID,
            on_event=observed.append,
        )

    assert exit_code == 0
    assert output_path.read_text() == "All tests pass."
    assert (tmp_path / "audit.events.jsonl").read_text() == "".join(
        _line(event) for event in events
    )
    assert observed == events
    assert command == popen.call_args.args[0]
    assert command[-4:] == ["--resume", SESSION_ID, "-p", "continue"]
    assert "--model" not in command


def test_run_grok_converts_stderr_only_failure_to_persisted_turn_failure(tmp_path):
    output_path = tmp_path / "audit.out.md"
    proc = MagicMock(returncode=1)
    proc.stdout = io.StringIO("")
    proc.stderr = io.StringIO("authentication required\n")
    observed = []
    with patch("hopper.grok.subprocess.Popen", return_value=proc):
        exit_code, _command = run_grok(
            "continue",
            "/work",
            str(output_path),
            SESSION_ID,
            on_event=observed.append,
        )

    assert exit_code == 1
    assert not output_path.exists()
    synthetic = json.loads((tmp_path / "audit.events.jsonl").read_text())
    assert synthetic["type"] == "turn.failed"
    assert synthetic["synthetic"] is True
    assert synthetic["error"]["message"] == "authentication required"
    assert observed == [synthetic]


def test_run_grok_rejects_exit_zero_without_one_valid_end_event(tmp_path):
    output_path = tmp_path / "audit.out.md"
    proc = MagicMock(returncode=0)
    proc.stdout = _stream({"type": "text", "data": "partial"})
    proc.stderr = io.StringIO("")
    with patch("hopper.grok.subprocess.Popen", return_value=proc):
        exit_code, _command = run_grok("continue", "/work", str(output_path), SESSION_ID)

    assert exit_code == 1
    assert not output_path.exists()
    lines = (tmp_path / "audit.events.jsonl").read_text().splitlines()
    assert json.loads(lines[0]) == {"type": "text", "data": "partial"}
    assert json.loads(lines[1])["type"] == "turn.failed"


def test_grok_failure_message_reads_native_and_synthetic_shapes():
    assert grok_failure_message({"type": "error", "message": "quota exceeded"}) == (
        "quota exceeded"
    )
    assert (
        grok_failure_message({"type": "turn.failed", "error": {"message": "invalid resume"}})
        == "invalid resume"
    )
    assert grok_failure_message({"type": "text", "data": "hello"}) is None
