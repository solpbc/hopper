# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Codex CLI wrapper for hopper."""

import json
import logging
import os
import signal
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

CODEX_FLAGS = "--dangerously-bypass-approvals-and-sandbox"
CODEX_BOOTSTRAP_TIMEOUT_SEC = 10 * 60


def turn_failed_message(event: dict) -> str | None:
    """Return the error message from a turn.failed event, else None."""
    if not isinstance(event, dict):
        return None
    if event.get("type") != "turn.failed":
        return None
    error = event.get("error")
    if not isinstance(error, dict):
        return None
    message = error.get("message")
    if not isinstance(message, str) or not message:
        return None
    return message


def bootstrap_codex(
    prompt: str,
    cwd: str,
    env: dict | None = None,
    timeout_sec: float = CODEX_BOOTSTRAP_TIMEOUT_SEC,
) -> tuple[int, str | None, str | None]:
    """Bootstrap a new Codex session and return its thread ID.

    Runs codex exec --json to create a fresh session. Parses the thread_id
    and any turn.failed error message from the JSONL output.

    Args:
        prompt: The prompt text to send to Codex.
        cwd: Working directory for Codex.
        env: Optional environment dict. Uses inherited env if None.
        timeout_sec: Maximum bootstrap runtime in seconds.

    Returns:
        (exit_code, thread_id, turn_failed_message) tuple. thread_id is None on failure.
        Exit code is 124 on timeout, 127 if codex not found, 130 on KeyboardInterrupt.
    """
    cmd = ["codex", "exec", CODEX_FLAGS, "--json", prompt]

    logger.debug(f"Bootstrapping codex session in {cwd}")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            text=True,
            process_group=0,
        )
        try:
            stdout, _ = proc.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            _terminate_process_group(proc)
            logger.error(f"codex bootstrap timed out after {timeout_sec}s")
            return 124, None, None
    except FileNotFoundError:
        logger.error("codex command not found")
        return 127, None, None
    except KeyboardInterrupt:
        return 130, None, None

    thread_id = _parse_thread_id(stdout)
    failed_msg = _parse_turn_failed_message(stdout)
    return_code = proc.returncode if proc.returncode is not None else 0
    if return_code == 0 and not thread_id:
        logger.error("Failed to parse thread_id from codex output")

    return return_code, thread_id, failed_msg


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """Terminate a subprocess group, then hard-kill if it ignores SIGTERM."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        logger.debug("Failed to terminate codex process group; terminating proc", exc_info=True)
        proc.terminate()

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            logger.debug("Failed to kill codex process group; killing proc", exc_info=True)
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.debug("Codex process did not exit after SIGKILL")


def run_codex(
    prompt: str,
    cwd: str,
    output_file: str,
    thread_id: str,
    env: dict | None = None,
    on_event=None,
) -> tuple[int, list[str]]:
    """Run Codex by resuming an existing session.

    Args:
        prompt: The prompt text to send to Codex.
        cwd: Working directory for Codex.
        output_file: Path to write the final agent message.
        thread_id: Codex thread ID to resume.
        env: Optional environment dict. Uses inherited env if None.
        on_event: Optional callback for each parsed JSON event.

    Returns:
        (exit_code, cmd) tuple. Exit code is 127 if codex not found,
        130 on KeyboardInterrupt.
    """
    cmd = [
        "codex",
        "exec",
        CODEX_FLAGS,
        "--json",
        "-o",
        output_file,
        "resume",
        thread_id,
        prompt,
    ]

    logger.debug(f"Running: codex exec resume {thread_id[:8]}... in {cwd}")
    proc = None

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        events_path = Path(output_file).with_name(
            Path(output_file).name.replace(".out.md", ".events.jsonl")
        )
        with events_path.open("a") as events_file:
            stdout = proc.stdout
            if stdout is not None:
                for line in stdout:
                    raw_line = line.rstrip("\n")
                    events_file.write(raw_line + "\n")
                    events_file.flush()
                    try:
                        event = json.loads(raw_line)
                        if on_event:
                            on_event(event)
                    except Exception:
                        logger.debug("Failed to process Codex event", exc_info=True)
            proc.wait()
        return proc.returncode, cmd
    except FileNotFoundError:
        logger.error("codex command not found")
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
            logger.debug("Failed to clean up codex process after interrupt", exc_info=True)
        return 130, cmd


def _parse_thread_id(stdout: str) -> str | None:
    """Parse thread_id from the first JSONL line of codex --json output.

    Looks for: {"type":"thread.started","thread_id":"<uuid>"}

    Args:
        stdout: Raw stdout from codex exec --json.

    Returns:
        The thread_id string, or None if not found.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if event.get("type") == "thread.started" and "thread_id" in event:
                return event["thread_id"]
        except json.JSONDecodeError:
            continue
    return None


def _parse_turn_failed_message(stdout: str) -> str | None:
    """Parse the first turn.failed error message from codex --json output."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            message = turn_failed_message(event)
            if message:
                return message
        except json.JSONDecodeError:
            continue
    return None
