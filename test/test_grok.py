# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for Hopper's Grok CLI wrapper."""

import io
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hopper.grok import (
    GROK_FLAGS,
    _completion_marker,
    bootstrap_grok,
    grok_failure_message,
    run_grok,
)

SESSION_ID = "00000000-0000-0000-0000-00000000012a"
OTHER_SESSION_ID = "00000000-0000-0000-0000-000000000456"
TASK_A = "36574ab5-03c3-4aee-96c2-43094bee5540"
TASK_B = "11111111-1111-4111-8111-111111111111"
TASK_DONE = "22222222-2222-4222-8222-222222222222"
TASK_OLD = "33333333-3333-4333-8333-333333333333"


@pytest.fixture(autouse=True)
def isolate_grok_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _background_record(session_id: str, task_id: str) -> dict:
    return {
        "timestamp": "2026-11-01T00:00:00Z",
        "method": "_x.ai/session/update",
        "params": {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "task_backgrounded",
                "tool_call_id": f"call-{task_id[:8]}",
                "task_id": task_id,
                "command": "sleep 900",
            },
        },
    }


def _completed_record(session_id: str, task_id: str, *, rich: bool = False) -> dict:
    snapshot: dict = {"task_id": task_id}
    if rich:
        snapshot.update({"exit_code": 0, "output": "ok", "signal": None})
    record: dict = {
        "timestamp": "2026-11-01T00:01:00Z",
        "method": "_x.ai/session/update",
        "params": {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "task_completed",
                "task_snapshot": snapshot,
            },
        },
    }
    if rich:
        record["_meta"] = {"source": "test"}
        record["params"]["update"]["will_wake"] = False
    return record


def _marker_line(session_id: str, task_id: str) -> bytes:
    return (
        json.dumps(_completion_marker(session_id, task_id), separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def _mock_success_proc(session_id: str, text: str = "success"):
    proc = MagicMock(returncode=0)
    proc.stdout = _stream(
        {"type": "text", "data": text},
        {"type": "end", "sessionId": session_id, "stopReason": "end_turn"},
    )
    proc.stderr = io.StringIO("")
    return proc


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


def test_run_grok_appends_one_marker_per_orphaned_task(tmp_path, caplog):
    child_home = tmp_path / "child-home"
    env = {"HOME": str(child_home)}

    # Autouse HOME holds a decoy session with an orphan; it must stay unchanged
    decoy_dir = tmp_path / "home" / ".grok" / "sessions" / "bucket-not-cwd" / SESSION_ID
    decoy_dir.mkdir(parents=True)
    decoy_log = decoy_dir / "updates.jsonl"
    decoy_bytes = json.dumps(_background_record(SESSION_ID, TASK_A)).encode("utf-8") + b"\n"
    decoy_log.write_bytes(decoy_bytes)

    child_session_dir = child_home / ".grok" / "sessions" / "bucket-not-cwd" / SESSION_ID
    child_session_dir.mkdir(parents=True)
    log_path = child_session_dir / "updates.jsonl"
    records = [
        _background_record(SESSION_ID, TASK_B),
        _background_record(SESSION_ID, TASK_A),
        _background_record(SESSION_ID, TASK_A),
        _background_record(SESSION_ID, TASK_DONE),
        _completed_record(SESSION_ID, TASK_DONE, rich=True),
        _background_record(SESSION_ID, TASK_OLD),
        _completed_record(SESSION_ID, TASK_OLD, rich=False),
        {"type": "ping"},
    ]
    original_bytes = b"".join(json.dumps(r).encode("utf-8") + b"\n" for r in records)
    log_path.write_bytes(original_bytes)

    bystander_dir = child_home / ".grok" / "sessions" / "bucket-not-cwd" / OTHER_SESSION_ID
    bystander_dir.mkdir(parents=True)
    bystander_log = bystander_dir / "updates.jsonl"
    bystander_bytes = (
        json.dumps(_background_record(OTHER_SESSION_ID, TASK_A)).encode("utf-8") + b"\n"
    )
    bystander_log.write_bytes(bystander_bytes)

    output_path = tmp_path / "audit.out.md"
    proc = _mock_success_proc(SESSION_ID, "final answer")

    spawn_bytes = None

    def _on_popen(*args, **kwargs):
        nonlocal spawn_bytes
        spawn_bytes = log_path.read_bytes()
        return proc

    with caplog.at_level(logging.WARNING, logger="hopper.grok"):
        with patch("hopper.grok.subprocess.Popen", side_effect=_on_popen) as popen:
            exit_code, command = run_grok(
                "continue",
                "/work",
                str(output_path),
                SESSION_ID,
                env=env,
            )

    assert exit_code == 0
    assert output_path.read_text() == "final answer"
    assert command == popen.call_args.args[0]
    assert command[-4:] == ["--resume", SESSION_ID, "-p", "continue"]
    assert "--model" not in command

    expected_healed = (
        original_bytes + _marker_line(SESSION_ID, TASK_B) + _marker_line(SESSION_ID, TASK_A)
    )
    healed_bytes = log_path.read_bytes()
    assert healed_bytes == expected_healed
    assert healed_bytes.startswith(original_bytes)
    assert spawn_bytes == healed_bytes

    # Appended lines match completion markers with no extra keys
    appended_lines = healed_bytes[len(original_bytes) :].decode("utf-8").splitlines()
    assert len(appended_lines) == 2
    assert json.loads(appended_lines[0]) == _completion_marker(SESSION_ID, TASK_B)
    assert json.loads(appended_lines[1]) == _completion_marker(SESSION_ID, TASK_A)

    # Decoy and bystander unchanged
    assert decoy_log.read_bytes() == decoy_bytes
    assert bystander_log.read_bytes() == bystander_bytes

    # caplog at WARNING
    grok_warnings = [
        r for r in caplog.records if r.name == "hopper.grok" and r.levelno == logging.WARNING
    ]
    assert len(grok_warnings) == 1
    msg = grok_warnings[0].getMessage()
    assert SESSION_ID in msg
    assert TASK_B in msg
    assert TASK_A in msg
    assert TASK_DONE not in msg
    assert TASK_OLD not in msg


def test_run_grok_second_resume_does_not_rewrite_healed_log(tmp_path, caplog):
    child_home = tmp_path / "child-home"
    env = {"HOME": str(child_home)}
    session_dir = child_home / ".grok" / "sessions" / "bucket-not-cwd" / SESSION_ID
    session_dir.mkdir(parents=True)
    log_path = session_dir / "updates.jsonl"
    log_path.write_bytes(json.dumps(_background_record(SESSION_ID, TASK_A)).encode("utf-8") + b"\n")

    output_path = tmp_path / "audit.out.md"
    proc1 = _mock_success_proc(SESSION_ID, "run 1")
    with caplog.at_level(logging.WARNING, logger="hopper.grok"):
        with patch("hopper.grok.subprocess.Popen", return_value=proc1):
            exit_code1, _ = run_grok("continue", "/work", str(output_path), SESSION_ID, env=env)
    assert exit_code1 == 0
    healed_bytes = log_path.read_bytes()

    caplog.clear()
    proc2 = _mock_success_proc(SESSION_ID, "run 2")
    with caplog.at_level(logging.WARNING, logger="hopper.grok"):
        with patch("hopper.grok.subprocess.Popen", return_value=proc2):
            exit_code2, _ = run_grok("continue", "/work", str(output_path), SESSION_ID, env=env)
    assert exit_code2 == 0
    assert log_path.read_bytes() == healed_bytes

    warnings = [
        r for r in caplog.records if r.name == "hopper.grok" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 0


def test_run_grok_separates_unterminated_last_record_only_when_appending(tmp_path):
    child_home = tmp_path / "child-home"
    env = {"HOME": str(child_home)}

    # Session 1: orphan without trailing newline
    session_dir1 = child_home / ".grok" / "sessions" / "bucket-not-cwd" / SESSION_ID
    session_dir1.mkdir(parents=True)
    log1 = session_dir1 / "updates.jsonl"
    raw1 = json.dumps(_background_record(SESSION_ID, TASK_A)).encode("utf-8")
    log1.write_bytes(raw1)

    output1 = tmp_path / "out1.out.md"
    proc1 = _mock_success_proc(SESSION_ID, "run 1")
    with patch("hopper.grok.subprocess.Popen", return_value=proc1):
        exit_code1, _ = run_grok("continue", "/work", str(output1), SESSION_ID, env=env)
    assert exit_code1 == 0
    assert log1.read_bytes() == raw1 + b"\n" + _marker_line(SESSION_ID, TASK_A)

    # Session 2: already completed, also without trailing newline
    session_dir2 = child_home / ".grok" / "sessions" / "bucket-not-cwd" / OTHER_SESSION_ID
    session_dir2.mkdir(parents=True)
    log2 = session_dir2 / "updates.jsonl"
    raw2 = json.dumps(_completed_record(OTHER_SESSION_ID, TASK_DONE)).encode("utf-8")
    log2.write_bytes(raw2)

    output2 = tmp_path / "out2.out.md"
    proc2 = _mock_success_proc(OTHER_SESSION_ID, "run 2")
    with patch("hopper.grok.subprocess.Popen", return_value=proc2):
        exit_code2, _ = run_grok("continue", "/work", str(output2), OTHER_SESSION_ID, env=env)
    assert exit_code2 == 0
    assert log2.read_bytes() == raw2


def test_run_grok_resume_noop_preserves_session_bytes(tmp_path):
    cases = [
        "no_orphans",
        "missing_sessions_root",
        "missing_session_dir",
        "missing_updates_jsonl",
        "unrelated_json_only",
    ]
    for case in cases:
        case_home = tmp_path / f"home_{case}"
        env = {"HOME": str(case_home)}
        bystander_dir = case_home / ".grok" / "sessions" / "bystander-bucket" / OTHER_SESSION_ID
        bystander_dir.mkdir(parents=True)
        bystander_log = bystander_dir / "updates.jsonl"
        bystander_bytes = b"bystander\n"
        bystander_log.write_bytes(bystander_bytes)

        pre_seeded_bytes = None
        log_path = None

        if case == "no_orphans":
            s_dir = case_home / ".grok" / "sessions" / "bucket-not-cwd" / SESSION_ID
            s_dir.mkdir(parents=True)
            log_path = s_dir / "updates.jsonl"
            records = [
                _background_record(SESSION_ID, TASK_A),
                _completed_record(SESSION_ID, TASK_A),
            ]
            pre_seeded_bytes = b"".join(json.dumps(r).encode("utf-8") + b"\n" for r in records)
            log_path.write_bytes(pre_seeded_bytes)
        elif case == "missing_sessions_root":
            # Remove .grok entirely so sessions root doesn't exist
            import shutil

            shutil.rmtree(case_home / ".grok")
        elif case == "missing_session_dir":
            # sessions root exists (from bystander), but SESSION_ID dir does not exist
            pass
        elif case == "missing_updates_jsonl":
            s_dir = case_home / ".grok" / "sessions" / "bucket-not-cwd" / SESSION_ID
            s_dir.mkdir(parents=True)
            # no updates.jsonl created
        elif case == "unrelated_json_only":
            s_dir = case_home / ".grok" / "sessions" / "bucket-not-cwd" / SESSION_ID
            s_dir.mkdir(parents=True)
            log_path = s_dir / "updates.jsonl"
            pre_seeded_bytes = json.dumps({"type": "ping"}).encode("utf-8")
            log_path.write_bytes(pre_seeded_bytes)

        out = tmp_path / f"out_{case}.out.md"
        proc = _mock_success_proc(SESSION_ID, f"result {case}")
        with patch("hopper.grok.subprocess.Popen", return_value=proc) as popen:
            exit_code, _ = run_grok("continue", "/work", str(out), SESSION_ID, env=env)

        assert exit_code == 0
        popen.assert_called_once()
        if pre_seeded_bytes is not None and log_path is not None:
            assert log_path.read_bytes() == pre_seeded_bytes
        if case == "missing_sessions_root":
            assert not (case_home / ".grok").exists()
        elif case == "missing_session_dir":
            assert not (case_home / ".grok" / "sessions" / "bucket-not-cwd" / SESSION_ID).exists()
        elif case == "missing_updates_jsonl":
            assert not (
                case_home / ".grok" / "sessions" / "bucket-not-cwd" / SESSION_ID / "updates.jsonl"
            ).exists()

        if case != "missing_sessions_root":
            assert bystander_log.read_bytes() == bystander_bytes


def test_run_grok_repair_failure_does_not_spawn(tmp_path, monkeypatch):
    failure_cases = [
        ("non_uuid", "not-a-uuid"),
        ("upper_uuid", SESSION_ID.upper()),
        ("braced_uuid", f"{{{SESSION_ID}}}"),
        ("dot_dot_x", "../x"),
        ("abs_path", "/abs/path"),
        ("a_slash_b", "a/b"),
        ("ambiguous", SESSION_ID),
        ("bad_json", SESSION_ID),
        ("no_task_id", SESSION_ID),
        ("no_snapshot", SESSION_ID),
        ("session_mismatch", SESSION_ID),
        ("read_error", SESSION_ID),
        ("write_error", SESSION_ID),
    ]

    for case_name, sid in failure_cases:
        case_dir = tmp_path / f"case_{case_name}"
        case_home = case_dir / "home"
        env = {"HOME": str(case_home)}
        out_file = case_dir / "test.out.md"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text("keep\n")
        events_path = case_dir / "test.events.jsonl"

        seeded_logs: list[tuple[Path, bytes]] = []

        if case_name == "ambiguous":
            for b in ("bucket-not-cwd", "other-bucket"):
                d = case_home / ".grok" / "sessions" / b / sid
                d.mkdir(parents=True)
                f = d / "updates.jsonl"
                content = json.dumps(_background_record(sid, TASK_A)).encode("utf-8") + b"\n"
                f.write_bytes(content)
                seeded_logs.append((f, content))
        elif case_name in (
            "bad_json",
            "no_task_id",
            "no_snapshot",
            "session_mismatch",
            "read_error",
            "write_error",
        ):
            d = case_home / ".grok" / "sessions" / "bucket-not-cwd" / sid
            d.mkdir(parents=True)
            f = d / "updates.jsonl"
            if case_name == "bad_json":
                content = b"{\n"
            elif case_name == "no_task_id":
                rec = _background_record(sid, TASK_A)
                del rec["params"]["update"]["task_id"]
                content = json.dumps(rec).encode("utf-8") + b"\n"
            elif case_name == "no_snapshot":
                rec = _completed_record(sid, TASK_A)
                del rec["params"]["update"]["task_snapshot"]
                content = json.dumps(rec).encode("utf-8") + b"\n"
            elif case_name == "session_mismatch":
                rec = _background_record(OTHER_SESSION_ID, TASK_A)
                content = json.dumps(rec).encode("utf-8") + b"\n"
            elif case_name == "read_error":
                content = json.dumps(_background_record(sid, TASK_A)).encode("utf-8") + b"\n"
            elif case_name == "write_error":
                content = json.dumps(_background_record(sid, TASK_A)).encode("utf-8") + b"\n"
            f.write_bytes(content)
            seeded_logs.append((f, content))

        observed_events = []
        with patch("hopper.grok.subprocess.Popen") as popen:
            with monkeypatch.context() as m:
                if case_name in (
                    "non_uuid",
                    "upper_uuid",
                    "braced_uuid",
                    "dot_dot_x",
                    "abs_path",
                    "a_slash_b",
                ):

                    def _fail_iterdir(self):
                        raise AssertionError("iterdir should not be called for invalid session ID")

                    m.setattr(Path, "iterdir", _fail_iterdir)

                if case_name == "read_error":
                    orig_read_bytes = Path.read_bytes

                    def _mock_read(self, *args, **kwargs):
                        if self.name == "updates.jsonl":
                            raise OSError("read failed")
                        return orig_read_bytes(self, *args, **kwargs)

                    m.setattr(Path, "read_bytes", _mock_read)

                if case_name == "write_error":
                    orig_open = Path.open

                    def _mock_open(self, *args, **kwargs):
                        mode = args[0] if args else kwargs.get("mode", "r")
                        if self.name == "updates.jsonl" and "a" in mode and "b" in mode:
                            raise OSError("write failed")
                        return orig_open(self, *args, **kwargs)

                    m.setattr(Path, "open", _mock_open)

                exit_code, _cmd = run_grok(
                    "continue",
                    "/work",
                    str(out_file),
                    sid,
                    env=env,
                    on_event=observed_events.append,
                )

        assert exit_code == 1
        popen.assert_not_called()
        assert out_file.read_text() == "keep\n"

        assert events_path.exists()
        event_lines = events_path.read_text().splitlines()
        assert len(event_lines) == 1
        event = json.loads(event_lines[0])
        assert event["type"] == "turn.failed"
        assert event["synthetic"] is True
        assert sid in event["error"]["message"]
        assert observed_events == [event]

        for log_file, expected_bytes in seeded_logs:
            assert log_file.read_bytes() == expected_bytes

        if case_name in (
            "non_uuid",
            "upper_uuid",
            "braced_uuid",
            "dot_dot_x",
            "abs_path",
            "a_slash_b",
        ):
            assert not (case_home / sid).exists()
            assert not (case_home / ".grok" / "sessions" / sid).exists()
