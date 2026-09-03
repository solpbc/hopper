# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for Hopper's Antigravity CLI wrapper."""

import io
import json
import os
import subprocess
from unittest.mock import MagicMock, patch

from hopper.antigravity import (
    ANTIGRAVITY_MODEL,
    ANTIGRAVITY_PRINT_TIMEOUT,
    _new_command,
    _parse_conversation_id,
    _resume_command,
    antigravity_failure_message,
    antigravity_usage_total_tokens,
    bootstrap_antigravity,
    check_antigravity_ready,
    run_antigravity,
)

CONVERSATION_ID = "conversation-123"


def _line(event: dict) -> str:
    return json.dumps(event) + "\n"


def _stream(*events: dict) -> io.StringIO:
    return io.StringIO("".join(_line(event) for event in events))


def _ready_run(version: str = "agy 1.2.3\n"):
    return patch(
        "hopper.antigravity.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, stdout=version, stderr=""),
    )


def _write_settings(home, provider: str = "gemini"):
    settings_path = home / ".gemini" / "antigravity-cli" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"modelProvider": provider}))


def test_commands_keep_required_flags_on_new_and_resume():
    new_command = _new_command("start")
    resume_command = _resume_command("continue", CONVERSATION_ID)

    assert new_command == [
        "agy",
        "-p",
        "start",
        "--new-project",
        "--dangerously-skip-permissions",
        "--model",
        ANTIGRAVITY_MODEL,
        "--print-timeout",
        ANTIGRAVITY_PRINT_TIMEOUT,
        "--output-format",
        "stream-json",
    ]
    assert resume_command == [
        "agy",
        "-p",
        "continue",
        "--new-project",
        "--dangerously-skip-permissions",
        "--conversation",
        CONVERSATION_ID,
        "--model",
        ANTIGRAVITY_MODEL,
        "--print-timeout",
        ANTIGRAVITY_PRINT_TIMEOUT,
        "--output-format",
        "stream-json",
    ]
    assert "--conversation" not in new_command


def test_check_antigravity_ready_uses_supplied_home_and_environment(tmp_path):
    _write_settings(tmp_path)
    env = {"HOME": str(tmp_path), "GEMINI_API_KEY": "key"}
    with (
        patch("hopper.antigravity.shutil.which", return_value="/usr/bin/agy"),
        _ready_run(),
    ):
        assert check_antigravity_ready(env) == (True, "agy 1.2.3", "")


def test_check_antigravity_ready_reports_missing_binary(tmp_path):
    with patch("hopper.antigravity.shutil.which", return_value=None):
        ready, version, error = check_antigravity_ready({"HOME": str(tmp_path)})

    assert (ready, version) == (False, "")
    assert error == "agy command not found"


def test_check_antigravity_ready_reports_version_failure(tmp_path):
    completed = subprocess.CompletedProcess([], 3, stdout="", stderr="version unavailable\n")
    with (
        patch("hopper.antigravity.shutil.which", return_value="/usr/bin/agy"),
        patch("hopper.antigravity.subprocess.run", return_value=completed),
    ):
        ready, version, error = check_antigravity_ready({"HOME": str(tmp_path)})

    assert (ready, version) == (False, "")
    assert error == "agy version check failed: version unavailable"


def test_check_antigravity_ready_reports_missing_settings(tmp_path):
    env = {"HOME": str(tmp_path), "GEMINI_API_KEY": "key"}
    with (
        patch("hopper.antigravity.shutil.which", return_value="/usr/bin/agy"),
        _ready_run(),
    ):
        ready, version, error = check_antigravity_ready(env)

    assert (ready, version) == (False, "agy 1.2.3")
    assert "settings file not found" in error


def test_check_antigravity_ready_reports_wrong_model_provider(tmp_path):
    _write_settings(tmp_path, provider="other")
    env = {"HOME": str(tmp_path), "GEMINI_API_KEY": "key"}
    with (
        patch("hopper.antigravity.shutil.which", return_value="/usr/bin/agy"),
        _ready_run(),
    ):
        ready, version, error = check_antigravity_ready(env)

    assert (ready, version) == (False, "agy 1.2.3")
    assert "modelProvider" in error


def test_check_antigravity_ready_reports_missing_or_empty_api_key(tmp_path):
    _write_settings(tmp_path)
    for api_key in (None, ""):
        env = {"HOME": str(tmp_path)}
        if api_key is not None:
            env["GEMINI_API_KEY"] = api_key
        with (
            patch("hopper.antigravity.shutil.which", return_value="/usr/bin/agy"),
            _ready_run(),
        ):
            ready, version, error = check_antigravity_ready(env)

        assert (ready, version) == (False, "agy 1.2.3")
        assert error == "GEMINI_API_KEY is not set"


def test_antigravity_failure_message_only_reads_terminal_results():
    assert antigravity_failure_message({"event": "result", "result": {"status": "SUCCESS"}}) is None
    assert (
        antigravity_failure_message(
            {"event": "result", "result": {"status": "ERROR", "message": "bad token"}}
        )
        == "bad token"
    )
    assert (
        antigravity_failure_message(
            {"event": "result", "result": {"status": "ERROR", "error": {"message": "bad"}}}
        )
        == "bad"
    )
    assert "DENIED" in antigravity_failure_message(
        {"event": "result", "result": {"status": "DENIED"}}
    )
    assert (
        antigravity_failure_message(
            {
                "event": "step_update",
                "step_update": {"step_type": "tool", "state": "ERROR"},
            }
        )
        is None
    )


def test_antigravity_usage_total_tokens_reads_only_valid_terminal_usage():
    assert (
        antigravity_usage_total_tokens(
            {
                "event": "result",
                "result": {"status": "SUCCESS", "usage": {"total_tokens": 123}},
            }
        )
        == 123
    )
    assert (
        antigravity_usage_total_tokens(
            {
                "event": "result",
                "result": {"status": "ERROR", "usage": {"total_tokens": 7}},
            }
        )
        == 7
    )
    for event in (
        {"event": "step_update"},
        {"event": "result", "result": {}},
        {"event": "result", "result": {"usage": {}}},
        {"event": "result", "result": {"usage": {"total_tokens": True}}},
        {"event": "result", "result": {"usage": {"total_tokens": -1}}},
        {"event": "result", "result": {"usage": {"total_tokens": "123"}}},
    ):
        assert antigravity_usage_total_tokens(event) is None


def test_parse_conversation_id_returns_first_nonempty_init_value():
    assert (
        _parse_conversation_id(
            [
                {"event": "init", "conversation_id": "", "init": {}},
                {"event": "init", "conversation_id": CONVERSATION_ID, "init": {}},
            ]
        )
        == CONVERSATION_ID
    )


def test_bootstrap_antigravity_returns_conversation_id_from_init():
    proc = MagicMock(returncode=0)
    proc.communicate.return_value = (
        _line({"event": "init", "conversation_id": CONVERSATION_ID, "init": {}}),
        "",
    )
    with patch("hopper.antigravity.subprocess.Popen", return_value=proc) as popen:
        result = bootstrap_antigravity("prompt", "/work")

    assert result == (0, CONVERSATION_ID, None)
    assert popen.call_args.args[0] == _new_command("prompt")


def test_bootstrap_antigravity_emits_events_and_writes_output_on_success(tmp_path):
    output_path = tmp_path / "reset.out.md"
    events = [
        {"event": "init", "conversation_id": CONVERSATION_ID, "init": {}},
        {
            "event": "result",
            "result": {"status": "SUCCESS", "response": "reset result"},
        },
    ]
    proc = MagicMock(returncode=0)
    stdout = "".join(_line(event) for event in events)
    proc.communicate.return_value = (stdout, "")
    observed = []

    with patch("hopper.antigravity.subprocess.Popen", return_value=proc):
        result = bootstrap_antigravity(
            "prompt", "/work", output_file=str(output_path), on_event=observed.append
        )

    assert result == (0, CONVERSATION_ID, None)
    assert observed == events
    assert output_path.read_text() == "reset result"
    assert (tmp_path / "reset.events.jsonl").read_text() == stdout


def test_bootstrap_antigravity_emits_events_without_writing_output_on_failure(tmp_path):
    output_path = tmp_path / "reset.out.md"
    event = {"event": "result", "result": {"status": "ERROR", "message": "denied"}}
    proc = MagicMock(returncode=1)
    stdout = _line(event)
    proc.communicate.return_value = (stdout, "")
    observed = []

    with patch("hopper.antigravity.subprocess.Popen", return_value=proc):
        result = bootstrap_antigravity(
            "prompt", "/work", output_file=str(output_path), on_event=observed.append
        )

    assert result == (1, None, "denied")
    assert observed == [event]
    assert not output_path.exists()
    assert (tmp_path / "reset.events.jsonl").read_text() == stdout


def test_bootstrap_antigravity_rejects_missing_conversation_id():
    proc = MagicMock(returncode=0)
    proc.communicate.return_value = (
        _line({"event": "init", "init": {}}),
        "",
    )  # no top-level conversation_id
    with patch("hopper.antigravity.subprocess.Popen", return_value=proc):
        result = bootstrap_antigravity("prompt", "/work")

    assert result[0] == 1
    assert result[1] is None
    assert "did not return a conversation ID" in result[2]
    assert "parsed 1 events" in result[2]


def test_run_antigravity_retains_events_and_writes_final_result_response(tmp_path):
    output_path = tmp_path / "audit.out.md"
    events = [
        {
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "state": "DONE",
                "text_delta": "Before tool.",
            },
        },
        {"event": "step_update", "step_update": {"step_type": "tool", "state": "ACTIVE"}},
        {"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE"}},
        {
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "state": "DONE",
                "text_delta": "All tests pass.",
            },
        },
        {"event": "result", "result": {"status": "SUCCESS", "response": "All tests pass."}},
    ]
    proc = MagicMock(returncode=0)
    proc.stdout = _stream(*events)
    proc.stderr = io.StringIO("")
    observed = []
    with patch("hopper.antigravity.subprocess.Popen", return_value=proc) as popen:
        exit_code, command = run_antigravity(
            "continue", "/work", str(output_path), CONVERSATION_ID, on_event=observed.append
        )

    assert exit_code == 0
    assert output_path.read_text() == "All tests pass."
    assert (tmp_path / "audit.events.jsonl").read_text() == "".join(
        _line(event) for event in events
    )
    assert observed == events
    assert command == popen.call_args.args[0]
    assert command == _resume_command("continue", CONVERSATION_ID)


def test_run_antigravity_emits_synthetic_failure_without_native_result(tmp_path):
    output_path = tmp_path / "audit.out.md"
    proc = MagicMock(returncode=1)
    proc.stdout = io.StringIO("")
    proc.stderr = io.StringIO("authentication required\n")
    observed = []
    with patch("hopper.antigravity.subprocess.Popen", return_value=proc):
        exit_code, _command = run_antigravity(
            "continue", "/work", str(output_path), CONVERSATION_ID, on_event=observed.append
        )

    assert exit_code == 1
    assert not output_path.exists()
    synthetic = json.loads((tmp_path / "audit.events.jsonl").read_text())
    assert synthetic == {
        "event": "result",
        "result": {"status": "ERROR", "message": "authentication required"},
        "synthetic": True,
    }
    assert observed == [synthetic]


def test_run_antigravity_terminates_and_reports_on_timeout(tmp_path):
    output_path = tmp_path / "audit.out.md"
    read_fd, write_fd = os.pipe()
    proc = MagicMock(returncode=None)
    proc.stdout = os.fdopen(read_fd, "r")
    proc.stderr = io.StringIO("")
    terminated = []

    def fake_terminate(target):
        terminated.append(target)
        target.returncode = -15
        os.close(write_fd)  # unblocks the real, still-open read end with EOF

    with (
        patch("hopper.antigravity.subprocess.Popen", return_value=proc),
        patch("hopper.antigravity._terminate_process_group", side_effect=fake_terminate),
    ):
        exit_code, _command = run_antigravity(
            "continue", "/work", str(output_path), CONVERSATION_ID, timeout_sec=0.05
        )

    assert exit_code == 124
    assert terminated == [proc]
    assert not output_path.exists()
    synthetic = json.loads((tmp_path / "audit.events.jsonl").read_text())
    assert synthetic["result"]["status"] == "ERROR"
    assert "exceeded" in synthetic["result"]["message"]
