# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Code runner - resumes coder sessions, except overgrown Antigravity sessions reset fresh."""

import json
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

from hopper import prompt
from hopper.antigravity import (
    ANTIGRAVITY_CONVERSATION_RESET_TOKENS,
    antigravity_usage_total_tokens,
    bootstrap_antigravity,
)
from hopper.client import (
    connect,
    set_coder_session,
    set_lode_progress,
    set_lode_state,
    set_lode_status,
)
from hopper.coder import coder_failure_message, run_coder, validate_coder_provider
from hopper.git import get_diff_stat, get_recent_commit_log
from hopper.lodes import (
    current_time_ms,
    format_duration_ms,
    get_lode_dir,
    get_worktree_dir,
    lode_coder,
    lode_coder_usage,
)
from hopper.projects import find_project

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SEC = 30.0
EXEC_HEARTBEAT_COMMAND_CHARS = 60

TURN_FAILED_BANNER = """\
============================================================
{provider} TURN FAILED
{message}
============================================================
"""

ANTIGRAVITY_RESET_FAILED_BANNER = """\
============================================================
ANTIGRAVITY RESET BOOTSTRAP FAILED
{message}
The previous Antigravity conversation was left intact and was not resumed
this round. Re-run this stage to retry the reset.
============================================================
"""

QUOTA_GUIDANCE = """\
Codex reported a usage-limit error for the account configured on this host.
This does not establish that the account is exhausted or that any other host
is affected. Verify the affected host/account before retrying.

If the limit is confirmed, do one of the following:
1. Implement this stage directly yourself, honoring the same review bar
   and test gates the stage prompt requires, then continue the normal
   stage flow.
2. If direct implementation is not possible, record the block with
   `hop status "codex usage limit on this host/account"` and stop.
"""


def _is_quota_message(provider: str, message: str) -> bool:
    lowered = message.lower()
    if provider == "codex":
        return "usage limit" in lowered
    return "usage limit" in lowered or "quota" in lowered or "rate limit" in lowered


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
    """Emit synthetic progress while a coder command execution is in flight."""

    def __init__(
        self,
        emit: Callable[[str], object],
        interval: float = HEARTBEAT_INTERVAL_SEC,
        provider: str = "codex",
    ) -> None:
        self.provider = validate_coder_provider(provider)
        self._in_flight: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()
        super().__init__(emit, self.summary, interval)

    def on_event(self, event) -> None:
        """Track provider command execution lifetime from stream events."""
        try:
            if not isinstance(event, dict):
                return
            if self.provider == "grok":
                self._on_grok_event(event)
                return
            if self.provider == "antigravity":
                self._on_antigravity_event(event)
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

    def _on_grok_event(self, event: dict) -> None:
        event_type = event.get("type")
        tool_call_id = event.get("toolCallId")
        if not tool_call_id:
            return
        if event_type == "tool_call":
            raw_input = event.get("rawInput")
            command = raw_input.get("command", "") if isinstance(raw_input, dict) else ""
            if not command:
                command = str(event.get("toolName") or event.get("kind") or "tool")
            with self._lock:
                self._in_flight[tool_call_id] = (str(command), current_time_ms())
        elif event_type == "tool_call_update" and event.get("status") not in {
            "pending",
            "running",
        }:
            with self._lock:
                self._in_flight.pop(tool_call_id, None)

    def _on_antigravity_event(self, event: dict) -> None:
        if event.get("event") != "step_update":
            return
        step = event.get("step_update")
        if not isinstance(step, dict) or step.get("step_type") != "tool":
            return
        state = step.get("state")
        if state == "ACTIVE":
            with self._lock:
                self._in_flight["tool"] = ("tool", current_time_ms())
        elif state in {"DONE", "ERROR"}:
            with self._lock:
                self._in_flight.pop("tool", None)

    def summary(self, now_ms: int) -> str | None:
        """Return the current in-flight command summary, if any."""
        try:
            with self._lock:
                if not self._in_flight:
                    return None
                command, started_ms = max(self._in_flight.values(), key=lambda value: value[1])
            cmd = truncate_progress_command(command)
            elapsed = (now_ms - started_ms) // 1000
            return f"{self.provider}: running {cmd} ({elapsed}s)"
        except Exception:
            logger.debug("exec heartbeat summary failed", exc_info=True)
            return None


def _summarize_event(event: dict, provider: str = "codex") -> str:
    """Summarize a provider event using the existing Codex progress semantics."""
    if not isinstance(event, dict):
        return ""
    provider = validate_coder_provider(provider)
    event_type = event.get("type") or "event"
    if provider == "antigravity":
        envelope = event.get("event") or "event"
        if envelope == "init":
            return "antigravity session started"
        if envelope == "step_update":
            step = event.get("step_update")
            if not isinstance(step, dict):
                return ""
            step_type, state = step.get("step_type"), step.get("state")
            if step_type == "agent_response" and state == "ACTIVE":
                return "antigravity thinking"
            if step_type == "tool" and state == "ACTIVE":
                return "antigravity: tool"
            return ""
        if envelope == "result":
            result = event.get("result")
            status = result.get("status") if isinstance(result, dict) else None
            return "antigravity turn done" if status == "SUCCESS" else "antigravity: turn failed"
        return f"antigravity: {envelope}"
    if provider == "grok":
        if event_type in {"available_commands", "thought", "text", "usage"}:
            return ""
        if event_type == "tool_call":
            return f"grok: {event.get('toolName') or event.get('kind') or 'tool'}"
        if event_type == "tool_call_update":
            return ""
        if event_type == "end":
            usage = event.get("usage")
            if isinstance(usage, dict):
                output_tokens = usage.get("outputTokens") or usage.get("output_tokens")
                if isinstance(output_tokens, int):
                    return f"grok turn done ({output_tokens} tok)"
            return "grok turn done"
        if event_type in {"error", "turn.failed"}:
            return "grok: turn failed"
        return ""
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
    """Run a stage prompt through the selected coder for a refine-stage lode.

    Resumes the lode's coder session, except Antigravity may bootstrap fresh
    after reaching its cumulative usage reset threshold. Validates the prompt
    exists, lode is in refine stage,
    cwd matches the lode worktree, and a coder session ID is present.
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

    try:
        provider, session_id = lode_coder(lode_data)
    except ValueError as error:
        print(f"Lode {lode_id} has invalid coder configuration: {error}")
        return 1
    if not session_id:
        label = provider.capitalize()
        print(f"Lode {lode_id} has no {label} session ID.")
        print(f"The {label} session is bootstrapped during 'hop refine' first run.")
        if provider == "codex":
            print("Re-run 'hop refine' to bootstrap the Codex session.")
        else:
            print("Re-run 'hop refine' to bootstrap the coder session.")
        return 1
    stored_usage = lode_coder_usage(lode_data) if provider == "antigravity" else 0
    reset_required = (
        provider == "antigravity" and stored_usage >= ANTIGRAVITY_CONVERSATION_RESET_TOKENS
    )

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

    if reset_required:
        diff_stat = get_diff_stat(str(cwd)) or "(no diff stat available or no differences reported)"
        commit_log = (
            get_recent_commit_log(str(cwd)) or "(no lode commits found or commit log unavailable)"
        )
        dispatch_prompt = (
            "Hopper recap: the prior Antigravity conversation was reset after "
            "reaching its conversation token budget. Work from the current "
            "worktree; this recap is context, not a replacement for the stage "
            "instruction.\n\n"
            "## Diff stat against the current default branch\n"
            f"{diff_stat}\n\n"
            "## Recent lode commits not on the current default branch (up to 10)\n"
            f"{commit_log}\n\n"
            "## Stage instruction\n"
            f"{prompt_text}"
        )
    else:
        dispatch_prompt = prompt_text

    # Save input prompt
    lode_dir = get_lode_dir(lode_id)
    version = _next_version(lode_dir, stage_name)
    if version is None:
        suffix = stage_name
    else:
        suffix = f"{stage_name}_{version}"
    input_path = lode_dir / f"{suffix}.in.md"
    _atomic_write(input_path, dispatch_prompt)

    set_lode_status(socket_path, lode_id, f"Running {stage_name}")

    # Resume the existing provider session, or reset an overgrown Antigravity conversation.
    output_path = lode_dir / f"{suffix}.out.md"
    started_at = current_time_ms()
    hb = ExecHeartbeat(lambda s: set_lode_progress(socket_path, lode_id, s), provider=provider)
    captured = {"turn_failed": None, "usage_total_tokens": None}

    def _on_event(event):
        hb.on_event(event)
        try:
            summary = _summarize_event(event, provider)
            if summary:
                set_lode_progress(socket_path, lode_id, summary)
        except Exception:
            logger.debug("progress heartbeat failed", exc_info=True)
        try:
            msg = coder_failure_message(provider, event)
            if msg:
                captured["turn_failed"] = msg
        except Exception:
            logger.debug("turn.failed capture failed", exc_info=True)
        if provider == "antigravity":
            try:
                usage = antigravity_usage_total_tokens(event)
                if usage is not None:
                    captured["usage_total_tokens"] = usage
            except Exception:
                logger.debug("usage capture failed", exc_info=True)

    hb.start()
    try:
        if reset_required:
            exit_code, new_session_id, reset_error = bootstrap_antigravity(
                dispatch_prompt,
                str(cwd),
                output_file=str(output_path),
                on_event=_on_event,
            )
            cmd = None
            if exit_code == 0 and not new_session_id:
                exit_code = 1
                reset_error = "Antigravity reset bootstrap did not return a conversation ID"
        else:
            exit_code, cmd = run_coder(
                provider,
                dispatch_prompt,
                str(cwd),
                str(output_path),
                session_id,
                on_event=_on_event,
            )
    finally:
        hb.stop()
    finished_at = current_time_ms()
    turn_failed = captured["turn_failed"]
    captured_usage = captured["usage_total_tokens"]
    reset_failed = reset_required and exit_code != 0

    if provider == "antigravity":
        if reset_required:
            if not reset_failed:
                assert new_session_id is not None
                session_id = new_session_id
                set_coder_session(
                    socket_path,
                    lode_id,
                    provider,
                    session_id,
                    usage_total_tokens=captured_usage if captured_usage is not None else 0,
                )
        elif captured_usage is not None:
            set_coder_session(
                socket_path,
                lode_id,
                provider,
                session_id,
                usage_total_tokens=stored_usage + captured_usage,
            )

    # Save run metadata
    metadata = {
        "stage": stage_name,
        "lode_id": lode_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": finished_at - started_at,
        "exit_code": exit_code,
        "cmd": cmd,
    }
    if reset_required:
        metadata["dispatch"] = "antigravity_reset_bootstrap"
        metadata["coder_provider"] = provider
        metadata["coder_session_id"] = session_id
        if reset_failed:
            metadata["reset_bootstrap_failed_message"] = reset_error or (
                f"Antigravity reset bootstrap failed (exit {exit_code})"
            )
    elif provider == "codex":
        metadata["codex_thread_id"] = session_id
    else:
        metadata["coder_provider"] = provider
        metadata["coder_session_id"] = session_id
    if turn_failed and not reset_failed:
        metadata["turn_failed_message"] = turn_failed
    meta_path = lode_dir / f"{suffix}.json"
    _atomic_write(meta_path, json.dumps(metadata, indent=2) + "\n")

    # Update status with stage result and duration
    duration = format_duration_ms(finished_at - started_at)
    if reset_failed:
        status = f"{stage_name} failed: antigravity reset bootstrap"
    elif exit_code == 0:
        status = f"{stage_name} ran for {duration}"
    elif turn_failed:
        if _is_quota_message(provider, turn_failed):
            status = f"{stage_name} failed: {provider} usage limit"
        else:
            status = f"{stage_name} failed: {provider} turn failed"
    else:
        status = f"{stage_name} failed after {duration}"
    set_lode_state(socket_path, lode_id, "running", status)

    # Print output if it was written
    if reset_failed:
        print(
            ANTIGRAVITY_RESET_FAILED_BANNER.format(
                message=reset_error or f"Antigravity reset bootstrap failed (exit {exit_code})"
            )
        )
    elif turn_failed and exit_code != 0:
        print(TURN_FAILED_BANNER.format(provider=provider.upper(), message=turn_failed))
        if provider == "codex" and _is_quota_message(provider, turn_failed):
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
