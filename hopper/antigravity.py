# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Antigravity CLI wrapper for Hopper's refine-stage coding sessions."""

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

ANTIGRAVITY_BOOTSTRAP_TIMEOUT_SEC = 10 * 60
ANTIGRAVITY_MODEL = "gemini-3.7-flash-high"
_READINESS_TIMEOUT_SEC = 5.0


def _new_command(prompt: str) -> list[str]:
    # Without --new-project, agy can silently write to an internal scratch directory.
    return [
        "agy",
        "-p",
        prompt,
        "--new-project",
        "--dangerously-skip-permissions",
        "--model",
        ANTIGRAVITY_MODEL,
        "--output-format",
        "stream-json",
    ]


def _resume_command(prompt: str, conversation_id: str) -> list[str]:
    return [
        "agy",
        "-p",
        prompt,
        "--new-project",
        "--dangerously-skip-permissions",
        "--conversation",
        conversation_id,
        "--model",
        ANTIGRAVITY_MODEL,
        "--output-format",
        "stream-json",
    ]


def _parse_stream(stdout: str) -> tuple[list[dict], str | None]:
    events: list[dict] = []
    for raw_line in stdout.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            return events, "Antigravity returned a non-JSON streaming event"
        if not isinstance(event, dict):
            return events, "Antigravity returned a non-object streaming event"
        events.append(event)
    return events, None


def _parse_conversation_id(events: list[dict]) -> str | None:
    for event in events:
        if event.get("event") != "init":
            continue
        init = event.get("init")
        conversation_id = init.get("conversation_id") if isinstance(init, dict) else None
        if isinstance(conversation_id, str) and conversation_id:
            return conversation_id
    return None


def antigravity_failure_message(event: dict) -> str | None:
    """Return a failure message from a terminal Antigravity result event."""
    if not isinstance(event, dict) or event.get("event") != "result":
        return None
    result = event.get("result")
    if not isinstance(result, dict):
        return None
    status = result.get("status")
    if status == "SUCCESS":
        return None
    # Per-step ERROR is recoverable; only result events are terminal failures.
    for key in ("message", "error"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            message = value.get("message")
            if isinstance(message, str) and message:
                return message
    if status:
        return f"Antigravity turn ended with status {status!r}"
    return "Antigravity turn failed with no status"


def _first_failure(events: list[dict]) -> str | None:
    for event in events:
        if message := antigravity_failure_message(event):
            return message
    return None


def _final_text(events: list[dict]) -> str:
    # Verified live: the terminal `result` event's `response` field carries the
    # complete final text. Per-step agent_response updates use `text_delta`, not
    # `text`, and are incremental — they are not a reliable reconstruction source.
    for event in events:
        if event.get("event") != "result":
            continue
        result = event.get("result")
        if isinstance(result, dict):
            response = result.get("response")
            if isinstance(response, str):
                return response
    return ""


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


def _diagnostic(events: list[dict], stderr: str, fallback: str) -> str:
    return _first_failure(events) or stderr.strip() or fallback


def _terminate_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        logger.debug(
            "Failed to terminate Antigravity process group; terminating proc", exc_info=True
        )
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
            logger.debug("Antigravity process did not exit after SIGKILL")


def bootstrap_antigravity(
    prompt: str,
    cwd: str,
    env: dict | None = None,
    timeout_sec: float = ANTIGRAVITY_BOOTSTRAP_TIMEOUT_SEC,
) -> tuple[int, str | None, str | None]:
    """Create and validate a fresh Antigravity conversation."""
    cmd = _new_command(prompt)
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
        return return_code, None, _diagnostic(events, stderr, f"Antigravity exited {return_code}")
    if failure := _first_failure(events) or parse_error:
        return 1, None, failure
    conversation_id = _parse_conversation_id(events)
    if conversation_id is None:
        return 1, None, "Antigravity bootstrap did not return a conversation ID"
    return 0, conversation_id, None


def _emit_synthetic_failure(events_file, message: str, on_event) -> None:
    event = {
        "event": "result",
        "result": {"status": "ERROR", "message": message},
        "synthetic": True,
    }
    events_file.write(json.dumps(event) + "\n")
    events_file.flush()
    if on_event:
        try:
            on_event(event)
        except Exception:
            logger.debug("Failed to process synthetic Antigravity event", exc_info=True)


def _has_successful_result(events: list[dict]) -> bool:
    return any(
        event.get("event") == "result"
        and isinstance(event.get("result"), dict)
        and event["result"].get("status") == "SUCCESS"
        for event in events
    )


def run_antigravity(
    prompt: str,
    cwd: str,
    output_file: str,
    session_id: str,
    env: dict | None = None,
    on_event=None,
) -> tuple[int, list[str]]:
    """Resume an Antigravity conversation and retain its raw stream."""
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

        stderr_thread = threading.Thread(
            target=_drain_stderr, name="antigravity-stderr", daemon=True
        )
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
                        parse_error = (
                            parse_error or "Antigravity returned a non-JSON streaming event"
                        )
                        continue
                    if not isinstance(event, dict):
                        parse_error = (
                            parse_error or "Antigravity returned a non-object streaming event"
                        )
                        continue
                    events.append(event)
                    if on_event:
                        try:
                            on_event(event)
                        except Exception:
                            logger.debug("Failed to process Antigravity event", exc_info=True)
            proc.wait()
            stderr_thread.join()
            return_code = proc.returncode if proc.returncode is not None else 1
            stderr = "".join(stderr_chunks)
            native_failure = _first_failure(events)
            failure = native_failure
            if return_code == 0:
                failure = failure or parse_error
                if failure is None and not _has_successful_result(events):
                    failure = "Antigravity stream did not contain a successful result"
                if failure:
                    return_code = 1
            elif not failure:
                failure = _diagnostic(events, stderr, f"Antigravity exited {return_code}")
            if failure and native_failure is None:
                _emit_synthetic_failure(events_file, failure, on_event)
        if return_code == 0:
            _atomic_write(Path(output_file), _final_text(events))
        return return_code, cmd
    except FileNotFoundError:
        logger.error("agy command not found")
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
            logger.debug("Failed to clean up Antigravity process after interrupt", exc_info=True)
        return 130, cmd


def check_antigravity_ready(env: dict | None = None) -> tuple[bool, str, str]:
    """Check local Antigravity prerequisites without authenticating or using the network."""
    env = env if env is not None else os.environ
    if shutil.which("agy") is None:
        return False, "", "agy command not found"
    try:
        result = subprocess.run(
            ["agy", "--version"],
            capture_output=True,
            text=True,
            timeout=_READINESS_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, "", f"agy version check failed: {error}"

    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        detail = output.splitlines()[0] if output else f"exit {result.returncode}"
        return False, "", f"agy version check failed: {detail}"
    version = output

    settings_path = (
        Path(env.get("HOME", str(Path.home()))) / ".gemini" / "antigravity-cli" / "settings.json"
    )
    try:
        settings = json.loads(settings_path.read_text())
    except FileNotFoundError:
        return False, version, f"Antigravity settings file not found: {settings_path}"
    except OSError as error:
        return False, version, f"Antigravity settings file could not be read: {error}"
    except json.JSONDecodeError:
        return False, version, f"Antigravity settings file is invalid JSON: {settings_path}"
    if not isinstance(settings, dict):
        return False, version, f"Antigravity settings file is not a JSON object: {settings_path}"
    if settings.get("modelProvider") != "gemini":
        return (
            False,
            version,
            f"Antigravity settings file must set modelProvider to gemini: {settings_path}",
        )
    if not env.get("GEMINI_API_KEY"):
        return False, version, "GEMINI_API_KEY is not set"
    return True, version, ""
