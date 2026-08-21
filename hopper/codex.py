# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Codex CLI wrapper for hopper."""

import json
import logging
import os
import signal
import subprocess
import uuid
from pathlib import Path

from hopper.tmux import KeyboardOwnership, PanePhase, normalize_terminal_text

logger = logging.getLogger(__name__)

CODEX_FLAGS = "--dangerously-bypass-approvals-and-sandbox"
CODEX_MODEL = "gpt-5.6-terra"
CODEX_REASONING_CONFIG = 'model_reasoning_effort="xhigh"'
CODEX_BOOTSTRAP_TIMEOUT_SEC = 10 * 60
LABEL = "Codex"
_SUPERVISOR_BOOTSTRAP_PROMPT = "Reply with exactly HOPPER_READY."
_SUPERVISOR_BOOTSTRAP_FLAGS = (
    "--ignore-user-config",
    "--ignore-rules",
    "-s",
    "read-only",
    "-m",
    CODEX_MODEL,
    "-c",
    CODEX_REASONING_CONFIG,
    "--json",
)
_SUPERVISOR_INTERACTIVE_FLAGS = (
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
    "-c",
    "check_for_update_on_startup=false",
)
_CODEX_LONG_WAIT_MARKERS = (
    "Our systems are thinking a bit more about this request before responding.",
    "› 1. Retry with a faster model",
    "2. Dismiss and keep waiting",
    "3. Learn more",
)
_CODEX_LONG_WAIT_FOOTER = (
    "No action is required. Codex will keep waiting, and this menu will close when the "
    "response is ready."
)
_CODEX_AUTH_MARKERS = ("please run `codex login`", "run `codex login`", "sign in to codex")


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
    cmd = [
        "codex",
        "exec",
        CODEX_FLAGS,
        "-m",
        CODEX_MODEL,
        "-c",
        CODEX_REASONING_CONFIG,
        "--json",
        prompt,
    ]

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


def prepare_session(
    *,
    cwd: str | None,
    env: dict,
    timeout_sec: float = CODEX_BOOTSTRAP_TIMEOUT_SEC,
) -> tuple[int, str | None, str | None]:
    """Create one restricted Codex thread for an interactive supervisor."""
    cmd = [
        "codex",
        "-a",
        "never",
        "exec",
        *_SUPERVISOR_BOOTSTRAP_FLAGS,
        _SUPERVISOR_BOOTSTRAP_PROMPT,
    ]
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
            return 124, None, f"Codex bootstrap timed out after {timeout_sec:g}s"
        except KeyboardInterrupt:
            _terminate_process_group(proc)
            raise
    except FileNotFoundError:
        return 127, None, "codex command not found"
    except KeyboardInterrupt:
        return 130, None, None
    except OSError as error:
        return 1, None, f"Codex bootstrap failed: {error}"

    return_code = proc.returncode if proc.returncode is not None else 1
    failed = _parse_turn_failed_message(stdout)
    if return_code != 0 or failed:
        diagnostic = failed or stderr.strip() or f"Codex bootstrap exited {return_code}"
        return return_code or 1, None, diagnostic
    thread_id = _parse_unique_thread_id(stdout)
    if thread_id is None:
        return 1, None, "Codex bootstrap did not return exactly one valid thread UUID"
    return 0, thread_id, None


def build_command(*, session_id: str, prompt: str | None, resume: bool) -> list[str]:
    """Resume the prepared Codex thread in its full interactive TUI."""
    cmd = ["codex", "resume", session_id, *_SUPERVISOR_INTERACTIVE_FLAGS]
    if prompt is not None:
        cmd.append(prompt)
    return cmd


def subprocess_environment() -> dict[str, str]:
    return {}


def requires_workspace_trust() -> bool:
    return False


def _normalized(snapshot: str) -> str | None:
    return normalize_terminal_text(snapshot)


def read_pane_input(snapshot: str) -> str | None:
    """Read Codex's final ordinary composer line."""
    normalized = _normalized(snapshot)
    if normalized is None:
        return None
    for line in reversed(normalized.splitlines()):
        stripped = line.strip()
        if stripped.startswith("›"):
            value = stripped[1:].strip()
            return "" if value == "Ask Codex to do anything" else value
    return None


def _is_long_wait(normalized: str) -> bool:
    collapsed = " ".join(normalized.split()).strip()
    return collapsed.endswith(_CODEX_LONG_WAIT_FOOTER) and all(
        marker in collapsed for marker in _CODEX_LONG_WAIT_MARKERS
    )


def observe_pane(
    title: str | None,
    snapshot: str,
    *,
    background_work_active: bool = False,
) -> tuple[PanePhase, KeyboardOwnership]:
    normalized = _normalized(snapshot)
    if normalized is None:
        return PanePhase.UNKNOWN, KeyboardOwnership.UNKNOWN
    lowered = normalized.lower()
    if any(marker in lowered for marker in _CODEX_AUTH_MARKERS):
        return PanePhase.AUTH, KeyboardOwnership.NONE
    if _is_long_wait(normalized):
        return PanePhase.BLOCKED, KeyboardOwnership.CARD
    if "esc to interrupt" in lowered and "working (" in lowered:
        return PanePhase.BUSY, KeyboardOwnership.NONE
    composer = read_pane_input(normalized)
    if composer is not None and "gpt-" in lowered:
        if background_work_active:
            return PanePhase.BACKGROUND, KeyboardOwnership.COMPOSER
        return PanePhase.IDLE, KeyboardOwnership.COMPOSER
    if "openai codex" in lowered or "starting codex" in lowered:
        return PanePhase.STARTING, KeyboardOwnership.NONE
    return PanePhase.UNKNOWN, KeyboardOwnership.UNKNOWN


def pane_surface_readable(snapshot: str) -> bool:
    normalized = _normalized(snapshot)
    return normalized is not None and (
        read_pane_input(normalized) is not None or _is_long_wait(normalized)
    )


def pane_answer_choices(snapshot: str):
    return None


def pane_blocked_identity(snapshot: str):
    normalized = _normalized(snapshot)
    if normalized is not None and _is_long_wait(normalized):
        return "Codex is waiting on an operator menu", ()
    return None


def _parse_unique_thread_id(stdout: str) -> str | None:
    thread_ids: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "thread.started":
            continue
        thread_id = event.get("thread_id")
        if not isinstance(thread_id, str):
            return None
        try:
            uuid.UUID(thread_id)
        except ValueError:
            return None
        thread_ids.append(thread_id)
    return thread_ids[0] if len(thread_ids) == 1 else None


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
        "-m",
        CODEX_MODEL,
        "-c",
        CODEX_REASONING_CONFIG,
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
