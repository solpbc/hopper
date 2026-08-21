# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Grok CLI wrapper for Hopper's refine-stage coding sessions."""

import json
import logging
import os
import re
import signal
import subprocess
import threading
import uuid
from pathlib import Path

from hopper.tmux import KeyboardOwnership, PanePhase, normalize_terminal_text

logger = logging.getLogger(__name__)

LABEL = "Grok"
_SUPERVISOR_FLAGS = (
    "--fullscreen",
    "--permission-mode",
    "bypassPermissions",
    "--sandbox",
    "off",
    "--no-memory",
    "--no-plan",
    "--no-subagents",
    "--disable-web-search",
    "--disallowed-tools",
    "ask_user_question",
)
_TURN_ACTIVE_RE = re.compile(r"⇣\S+\s+\[stop\]\s*$", re.MULTILINE)
_HINT_LINE_RE = re.compile(r"^\s*\S+:\S[^│]*(?:│[^│]*\S+:\S[^│]*)+$")
_BACKGROUND_RE = re.compile(
    r"\b[1-9]\d*\s+commands?\s+still\s+running\b|^\s*[▾▸]\s*Tasks\s+[1-9]\d*\s*$",
    re.MULTILINE,
)
_AUTH_MARKERS = ("Approve in your browser to finish signing in.", "Waiting for approval...")

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


def build_command(*, session_id: str, prompt: str | None, resume: bool) -> list[str]:
    """Build one interactive Grok supervisor command."""
    if resume:
        return ["grok", *_SUPERVISOR_FLAGS, "--resume", session_id]
    if not isinstance(prompt, str):
        raise ValueError("a Grok first launch requires a prompt")
    return ["grok", *_SUPERVISOR_FLAGS, "--session-id", session_id, prompt]


def subprocess_environment() -> dict[str, str]:
    return {"GROK_DISABLE_AUTOUPDATER": "1"}


def requires_workspace_trust() -> bool:
    return False


def _normalized(snapshot: str) -> str | None:
    return normalize_terminal_text(snapshot)


def _hint_line(normalized: str) -> str:
    return next((line for line in reversed(normalized.splitlines()) if line.strip()), "")


def read_pane_input(snapshot: str) -> str | None:
    """Read the final visible Grok ordinary-composer row."""
    normalized = _normalized(snapshot)
    if normalized is None:
        return None
    for line in reversed(normalized.splitlines()):
        stripped = line.strip()
        if stripped.startswith("│ ❯") and stripped.endswith("│"):
            return stripped[3:-1].strip()
    return None


def observe_pane(
    title: str | None,
    snapshot: str,
    *,
    background_work_active: bool = False,
) -> tuple[PanePhase, KeyboardOwnership]:
    normalized = _normalized(snapshot)
    if not normalized:
        return PanePhase.UNKNOWN, KeyboardOwnership.UNKNOWN
    if any(marker in normalized for marker in _AUTH_MARKERS):
        return PanePhase.AUTH, KeyboardOwnership.NONE
    hint = _hint_line(normalized)
    if "Ctrl+x:shortcuts" in hint:
        keyboard = KeyboardOwnership.COMPOSER
        if _TURN_ACTIVE_RE.search(normalized):
            return PanePhase.BUSY, keyboard
        if background_work_active or _BACKGROUND_RE.search(normalized):
            return PanePhase.BACKGROUND, keyboard
        if read_pane_input(normalized) is not None:
            return PanePhase.IDLE, keyboard
        return PanePhase.UNKNOWN, KeyboardOwnership.UNKNOWN
    if _HINT_LINE_RE.match(hint):
        return PanePhase.BLOCKED, KeyboardOwnership.CARD
    if "grok build" in normalized.lower() or "starting grok" in normalized.lower():
        return PanePhase.STARTING, KeyboardOwnership.NONE
    return PanePhase.UNKNOWN, KeyboardOwnership.UNKNOWN


def pane_surface_readable(snapshot: str) -> bool:
    phase, keyboard = observe_pane(None, snapshot)
    return keyboard in {KeyboardOwnership.COMPOSER, KeyboardOwnership.CARD} and phase not in {
        PanePhase.UNKNOWN,
        PanePhase.AUTH,
    }


def pane_answer_choices(snapshot: str):
    return None


def pane_blocked_identity(snapshot: str):
    phase, _keyboard = observe_pane(None, snapshot)
    if phase is not PanePhase.BLOCKED:
        return None
    normalized = _normalized(snapshot) or ""
    for line in reversed(normalized.splitlines()):
        stripped = " ".join(line.split())
        if "Waiting on" in stripped:
            return stripped, ()
    return "Grok is waiting on an operator card", ()


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
