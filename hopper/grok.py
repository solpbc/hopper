# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Grok CLI wrapper for Hopper's refine-stage coding sessions."""

import json
import logging
import os
import signal
import subprocess
import threading
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

GROK_BOOTSTRAP_TIMEOUT_SEC = 10 * 60
GROK_FLAGS = (
    "--trust",
    "--no-auto-update",
    "--no-subagents",
    "--disable-web-search",
    "--no-memory",
    "--no-plan",
    "--sandbox",
    "off",
    "--permission-mode",
    "bypassPermissions",
    "--disallowed-tools",
    "ask_user_question",
    "--output-format",
    "streaming-json",
)


def _new_command(prompt: str, session_id: str) -> list[str]:
    return ["grok", *GROK_FLAGS, "--session-id", session_id, "-p", prompt]


def _resume_command(prompt: str, session_id: str) -> list[str]:
    return ["grok", *GROK_FLAGS, "--resume", session_id, "-p", prompt]


def grok_failure_message(event: dict) -> str | None:
    """Return a failure message from a Grok error or synthetic turn.failed event."""
    if not isinstance(event, dict):
        return None
    if event.get("type") == "turn.failed":
        error = event.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        return message if isinstance(message, str) and message else None
    if event.get("type") != "error":
        return None
    for key in ("message", "error", "data"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            message = value.get("message")
            if isinstance(message, str) and message:
                return message
    return None


def _parse_stream(stdout: str) -> tuple[list[dict], str | None]:
    events: list[dict] = []
    for raw_line in stdout.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            return events, "Grok returned a non-JSON streaming event"
        if not isinstance(event, dict):
            return events, "Grok returned a non-object streaming event"
        events.append(event)
    return events, None


def _terminal_error(events: list[dict], session_id: str, *, require_text: bool) -> str | None:
    ends = [event for event in events if event.get("type") == "end"]
    if len(ends) != 1:
        return f"Grok stream contained {len(ends)} end events; expected exactly one"
    end = ends[0]
    if end.get("sessionId") != session_id:
        return "Grok end event did not match the requested session"
    if end.get("stopReason") != "end_turn":
        reason = end.get("stopReason") or "missing"
        return f"Grok turn stopped with {reason}"
    if require_text and not _final_text(events).strip():
        return "Grok turn ended without a final response"
    return None


def _final_text(events: list[dict]) -> str:
    chunks: list[str] = []
    for event in events:
        event_type = event.get("type")
        if event_type == "tool_call":
            chunks.clear()
        elif event_type == "text" and isinstance(event.get("data"), str):
            chunks.append(event["data"])
    return "".join(chunks)


def _first_failure(events: list[dict]) -> str | None:
    for event in events:
        if message := grok_failure_message(event):
            return message
    return None


def _diagnostic(events: list[dict], stderr: str, fallback: str) -> str:
    return _first_failure(events) or stderr.strip() or fallback


def _terminate_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        logger.debug("Failed to terminate Grok process group; terminating proc", exc_info=True)
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.debug("Grok process did not exit after SIGKILL")


def bootstrap_grok(
    prompt: str,
    cwd: str,
    env: dict | None = None,
    timeout_sec: float = GROK_BOOTSTRAP_TIMEOUT_SEC,
) -> tuple[int, str | None, str | None]:
    """Create and validate a fresh Grok session."""
    session_id = str(uuid.uuid4())
    cmd = _new_command(prompt, session_id)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            process_group=0,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            _terminate_process_group(proc)
            return 124, None, None
    except FileNotFoundError:
        return 127, None, None
    except KeyboardInterrupt:
        return 130, None, None

    events, parse_error = _parse_stream(stdout)
    return_code = proc.returncode if proc.returncode is not None else 1
    if return_code != 0:
        return return_code, None, _diagnostic(events, stderr, f"Grok exited {return_code}")
    protocol_error = (
        _first_failure(events)
        or parse_error
        or _terminal_error(events, session_id, require_text=True)
    )
    if protocol_error:
        return 1, None, protocol_error
    return 0, session_id, None


def _events_path(output_file: str) -> Path:
    path = Path(output_file)
    return path.with_name(path.name.replace(".out.md", ".events.jsonl"))


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(content)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _emit_synthetic_failure(events_file, message: str, on_event) -> None:
    event = {"type": "turn.failed", "error": {"message": message}, "synthetic": True}
    events_file.write(json.dumps(event) + "\n")
    events_file.flush()
    if on_event:
        try:
            on_event(event)
        except Exception:
            logger.debug("Failed to process synthetic Grok event", exc_info=True)


def run_grok(
    prompt: str,
    cwd: str,
    output_file: str,
    session_id: str,
    env: dict | None = None,
    on_event=None,
) -> tuple[int, list[str]]:
    """Resume a Grok session, retain its raw stream, and write its final response."""
    cmd = _resume_command(prompt, session_id)
    proc = None
    events: list[dict] = []
    stderr_chunks: list[str] = []
    events_path = _events_path(output_file)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        def _drain_stderr() -> None:
            if proc is not None and proc.stderr is not None:
                stderr_chunks.extend(proc.stderr.readlines())

        stderr_thread = threading.Thread(target=_drain_stderr, name="grok-stderr", daemon=True)
        stderr_thread.start()
        parse_error = None
        with events_path.open("a") as events_file:
            if proc.stdout is not None:
                for line in proc.stdout:
                    raw_line = line.rstrip("\n")
                    events_file.write(raw_line + "\n")
                    events_file.flush()
                    if not raw_line.strip():
                        continue
                    try:
                        event = json.loads(raw_line)
                    except json.JSONDecodeError:
                        parse_error = parse_error or "Grok returned a non-JSON streaming event"
                        continue
                    if not isinstance(event, dict):
                        parse_error = parse_error or "Grok returned a non-object streaming event"
                        continue
                    events.append(event)
                    if on_event:
                        try:
                            on_event(event)
                        except Exception:
                            logger.debug("Failed to process Grok event", exc_info=True)
            proc.wait()
            stderr_thread.join()
            return_code = proc.returncode if proc.returncode is not None else 1
            stderr = "".join(stderr_chunks)
            failure = _first_failure(events)
            if return_code == 0:
                failure = (
                    failure or parse_error or _terminal_error(events, session_id, require_text=True)
                )
                if failure:
                    return_code = 1
            elif not failure:
                failure = _diagnostic(events, stderr, f"Grok exited {return_code}")
            if failure and not _first_failure(events):
                _emit_synthetic_failure(events_file, failure, on_event)
        if return_code == 0:
            _atomic_write(Path(output_file), _final_text(events))
        return return_code, cmd
    except FileNotFoundError:
        logger.error("grok command not found")
        return 127, cmd
    except KeyboardInterrupt:
        try:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            logger.debug("Failed to clean up Grok process after interrupt", exc_info=True)
        return 130, cmd
