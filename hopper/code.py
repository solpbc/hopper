# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Code runner - runs a prompt via Codex, resuming the lode's Codex thread."""

import json
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

from hopper import prompt
from hopper.client import connect, set_lode_progress, set_lode_state
from hopper.codex import run_codex, turn_failed_message
from hopper.lodes import current_time_ms, format_duration_ms, get_lode_dir, get_worktree_dir
from hopper.projects import find_project

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SEC = 30.0
EXEC_HEARTBEAT_COMMAND_CHARS = 60

TURN_FAILED_BANNER = """\
============================================================
CODEX TURN FAILED
{message}
============================================================
"""

QUOTA_GUIDANCE = """\
The Codex seat is one shared account used by every hopper host: a
usage-limit failure is fleet-wide, not specific to this lode or machine.
Do NOT retry `hop code` and do NOT spend time diagnosing the dispatcher —
it will keep failing until the usage window resets. (The reset time
quoted above can be pessimistic; the seat may recover earlier.)

Do ONE of the following instead:
1. Implement this stage directly yourself, honoring the same review bar
   and test gates the stage prompt requires, then continue the normal
   stage flow.
2. If direct implementation is not possible, record the block with
   `hop status "codex usage limit - waiting for reset"` and stop.
"""


def _is_quota_message(message: str) -> bool:
    return "usage limit" in message.lower()


def truncate_progress_command(command: str) -> str:
    """Truncate a command for a compact progress summary."""
    if len(command) <= EXEC_HEARTBEAT_COMMAND_CHARS:
        return command
    return command[: EXEC_HEARTBEAT_COMMAND_CHARS - 3] + "..."


def format_progress_duration(duration_ms: int) -> str:
    """Format a progress duration while retaining seconds."""
    total_seconds = max(0, duration_ms) // 1000
    hours, remainder = divmod(total_seconds, 60 * 60)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


class ProgressHeartbeat:
    """Periodically emit a best-effort progress summary."""

    def __init__(
        self,
        emit: Callable[[str], object],
        summary: Callable[[int], str | None],
        interval: float = HEARTBEAT_INTERVAL_SEC,
    ) -> None:
        self.emit = emit
        self._summary = summary
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._emit_lock = threading.Lock()

    def start(self) -> None:
        """Start emitting progress summaries."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.wait(self.interval):
                try:
                    summary = self._summary(current_time_ms())
                except Exception:
                    logger.debug("progress heartbeat summary failed", exc_info=True)
                    continue
                if not summary:
                    continue
                with self._emit_lock:
                    if self._stop.is_set():
                        return
                    try:
                        self.emit(summary)
                    except Exception:
                        logger.debug("progress heartbeat emit failed", exc_info=True)

        self._thread = threading.Thread(target=_loop, name="progress-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the heartbeat and wait until no further emit can begin."""
        self._stop.set()
        with self._emit_lock:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join()


class ExecHeartbeat(ProgressHeartbeat):
    """Emit synthetic progress while a Codex command execution is in flight."""

    def __init__(
        self, emit: Callable[[str], object], interval: float = HEARTBEAT_INTERVAL_SEC
    ) -> None:
        self._in_flight: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()
        super().__init__(emit, self.summary, interval)

    def on_event(self, event) -> None:
        """Track command_execution item lifetime from Codex events."""
        try:
            if not isinstance(event, dict):
                return
            item = event.get("item")
            if not isinstance(item, dict):
                return
            event_type = event.get("type")
            item_id = item.get("id")
            if not item_id:
                return
            if event_type == "item.started" and item.get("type") == "command_execution":
                command = str(item.get("command") or "")
                with self._lock:
                    self._in_flight[item_id] = (command, current_time_ms())
            elif event_type == "item.completed":
                with self._lock:
                    self._in_flight.pop(item_id, None)
        except Exception:
            logger.debug("exec heartbeat event handling failed", exc_info=True)

    def summary(self, now_ms: int) -> str | None:
        """Return the current in-flight command summary, if any."""
        try:
            with self._lock:
                if not self._in_flight:
                    return None
                command, started_ms = max(self._in_flight.values(), key=lambda value: value[1])
            cmd = truncate_progress_command(command)
            elapsed = (now_ms - started_ms) // 1000
            return f"codex: running {cmd} ({elapsed}s)"
        except Exception:
            logger.debug("exec heartbeat summary failed", exc_info=True)
            return None


def _summarize_event(event: dict) -> str:
    """Summarize a Codex JSON event into a short progress label."""
    if not isinstance(event, dict):
        return ""
    event_type = event.get("type") or "event"
    if event_type == "thread.started":
        return "codex session started"
    if event_type == "turn.started":
        return "codex thinking"
    if event_type == "item.completed":
        item = event.get("item", {})
        item_type = item.get("type") or ""
        if item_type == "agent_message":
            text = item.get("text") or ""
            return f"codex: message ({len(text)} chars)"
        if "tool" in item_type.lower():
            return f"codex: {item.get('tool_name') or item_type}"
    if event_type == "turn.completed":
        try:
            return f"codex turn done ({event['usage']['output_tokens']} tok)"
        except Exception:
            return "codex turn done"
    return f"codex: {event_type}"


def run_code(lode_id: str, socket_path: Path, stage_name: str, request: str) -> int:
    """Run a stage prompt via Codex for a refine-stage lode.

    Resumes the lode's Codex thread so that context accumulates across
    stages. Validates the prompt exists, lode is in refine stage,
    cwd matches the lode worktree, and a Codex thread ID is present.
    Saves artifacts (<stage>.in.md, <stage>.out.md, <stage>.json) to the
    lode directory and prints the output to stdout.

    Args:
        lode_id: The hopper lode ID.
        socket_path: Path to the server Unix socket.
        stage_name: Name of the prompt file (without .md extension).
        request: The user's directions/request text from stdin.

    Returns:
        Exit code (0 on success).
    """
    # Query server for lode data
    response = connect(socket_path, lode_id=lode_id)
    if not response:
        print("Failed to connect to server.")
        return 1

    lode_data = response.get("lode")
    if not lode_data:
        print(f"Lode {lode_id} not found.")
        return 1

    # Validate lode is in refine stage
    if lode_data.get("stage") != "refine":
        print(f"Lode {lode_id} is not in refine stage.")
        return 1

    # Validate Codex thread ID exists
    codex_thread_id = lode_data.get("codex_thread_id")
    if not codex_thread_id:
        print(f"Lode {lode_id} has no Codex thread ID.")
        print("The Codex session is bootstrapped during 'hop refine' first run.")
        print("Re-run 'hop refine' to bootstrap the Codex session.")
        return 1

    # Validate cwd is the lode worktree
    worktree_path = get_worktree_dir(lode_id)
    cwd = Path.cwd()
    try:
        if cwd.resolve() != worktree_path.resolve():
            print(f"Must run from lode worktree: {worktree_path}")
            return 1
    except OSError:
        print(f"Must run from lode worktree: {worktree_path}")
        return 1

    # Build context for prompt template
    context: dict[str, str] = {"request": request}
    project_name = lode_data.get("project", "")
    if project_name:
        context["project"] = project_name
        project = find_project(project_name)
        if project:
            context["dir"] = project.path
    scope = lode_data.get("scope", "")
    if scope:
        context["scope"] = scope

    # Load prompt with context
    try:
        prompt_text = prompt.load(stage_name, context=context if context else None)
    except FileNotFoundError:
        print(f"Prompt not found: prompts/{stage_name}.md")
        return 1

    # Save input prompt
    lode_dir = get_lode_dir(lode_id)
    version = _next_version(lode_dir, stage_name)
    if version is None:
        suffix = stage_name
    else:
        suffix = f"{stage_name}_{version}"
    input_path = lode_dir / f"{suffix}.in.md"
    _atomic_write(input_path, prompt_text)

    # Set state to stage name while running
    set_lode_state(socket_path, lode_id, stage_name, f"Running {stage_name}")

    # Run codex (resume existing thread)
    output_path = lode_dir / f"{suffix}.out.md"
    started_at = current_time_ms()
    hb = ExecHeartbeat(lambda s: set_lode_progress(socket_path, lode_id, s))
    captured = {"turn_failed": None}

    def _on_event(event):
        hb.on_event(event)
        try:
            summary = _summarize_event(event)
            if summary:
                set_lode_progress(socket_path, lode_id, summary)
        except Exception:
            logger.debug("progress heartbeat failed", exc_info=True)
        try:
            msg = turn_failed_message(event)
            if msg:
                captured["turn_failed"] = msg
        except Exception:
            logger.debug("turn.failed capture failed", exc_info=True)

    hb.start()
    try:
        exit_code, cmd = run_codex(
            prompt_text,
            str(cwd),
            str(output_path),
            codex_thread_id,
            on_event=_on_event,
        )
    finally:
        hb.stop()
    finished_at = current_time_ms()
    turn_failed = captured["turn_failed"]

    # Save run metadata
    metadata = {
        "stage": stage_name,
        "lode_id": lode_id,
        "codex_thread_id": codex_thread_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": finished_at - started_at,
        "exit_code": exit_code,
        "cmd": cmd,
    }
    if turn_failed:
        metadata["turn_failed_message"] = turn_failed
    meta_path = lode_dir / f"{suffix}.json"
    _atomic_write(meta_path, json.dumps(metadata, indent=2) + "\n")

    # Update status with stage result and duration
    duration = format_duration_ms(finished_at - started_at)
    if exit_code == 0:
        status = f"{stage_name} ran for {duration}"
    elif turn_failed:
        if _is_quota_message(turn_failed):
            status = f"{stage_name} failed: codex usage limit"
        else:
            status = f"{stage_name} failed: codex turn failed"
    else:
        status = f"{stage_name} failed after {duration}"
    set_lode_state(socket_path, lode_id, "running", status)

    # Print output if it was written
    if turn_failed and exit_code != 0:
        print(TURN_FAILED_BANNER.format(message=turn_failed))
        if _is_quota_message(turn_failed):
            print(QUOTA_GUIDANCE)
    elif output_path.exists():
        content = output_path.read_text()
        if content:
            print(content)

    return exit_code


def _next_version(lode_dir: Path, stage_name: str) -> int | None:
    """Return the next version number for stage artifacts, or None for first run.

    Checks if the base output file exists. If not, returns None (first run uses
    base names). If it does, probes _1, _2, ... until a free slot is found.
    """
    if not (lode_dir / f"{stage_name}.out.md").exists():
        return None
    n = 1
    while (lode_dir / f"{stage_name}_{n}.out.md").exists():
        n += 1
    return n


def _atomic_write(path: Path, content: str) -> None:
    """Write content to a file atomically via tmp + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)
