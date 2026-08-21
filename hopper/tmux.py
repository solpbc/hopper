# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""tmux interaction utilities."""

import logging
import os
import re
import shlex
import subprocess
import sys
from enum import Enum

logger = logging.getLogger(__name__)

COMPLETION_ACTION_OPTION = "@hopper_completion_action"


def spawn_lode_processor(
    lode_id: str,
    project_path: str | None = None,
    foreground: bool = False,
    env: dict[str, str] | None = None,
    spawn_receipt: dict | None = None,
) -> tuple["WindowSpawnOutcome", str | None]:
    """Spawn Hopper's provider-neutral lode processor in a tmux window."""
    path = os.environ.get("PATH", "/usr/bin:/bin")
    # Run through /bin/sh so PATH and || work regardless of tmux's default shell.
    fail = "echo 'Failed. Press Enter to close.'; read"
    inner = f"export PATH={shlex.quote(path)}; hop process {lode_id} || {{ {fail}; }}"
    command = f"/bin/sh -c {shlex.quote(inner)}"
    kwargs = {"cwd": project_path, "env": env, "background": not foreground}
    if spawn_receipt is not None:
        kwargs["spawn_receipt"] = spawn_receipt
    return new_window(command, **kwargs)


def switch_to_pane(pane_id: str) -> bool:
    """Switch to the tmux window containing one pane."""
    return select_window(pane_id)


class Liveness(Enum):
    """Observed liveness of a recorded tmux pane."""

    ALIVE = "alive"
    GONE = "gone"
    UNKNOWN = "unknown"


class WindowSpawnOutcome(Enum):
    """Authoritative result of asking tmux to create one window."""

    SPAWNED = "spawned"
    PROVEN_NO_PANE = "proven_no_pane"
    UNKNOWN = "unknown"


class PanePhase(Enum):
    """Claude pane activity phase inferred from its tmux title."""

    IDLE = "idle"
    PROCESSING = "processing"
    UNKNOWN = "unknown"


def is_inside_tmux() -> bool:
    """Check if currently running inside a tmux session."""
    return "TMUX" in os.environ


def is_tmux_server_running() -> bool:
    """Check if a tmux server is running with active sessions."""
    return len(get_tmux_sessions()) > 0


def get_tmux_sessions() -> list[str]:
    """Get list of active tmux session names."""
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []
        return [s.strip() for s in result.stdout.strip().split("\n") if s.strip()]
    except FileNotFoundError:
        return []


def pane_liveness(pane_id: str, *, timeout: float | None = None) -> Liveness:
    """Return whether a pane is alive, gone, or uncheckable."""
    try:
        kwargs: dict[str, object] = {"capture_output": True, "text": True}
        if timeout is not None:
            kwargs["timeout"] = timeout
        result = subprocess.run(["tmux", "has-session", "-t", pane_id], **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        return Liveness.UNKNOWN

    if result.returncode == 0:
        return Liveness.ALIVE
    if result.stderr.strip().startswith("can't find pane"):
        return Liveness.GONE
    return Liveness.UNKNOWN


def get_pane_pid(target: str) -> int | None:
    """Return the root process ID for a tmux pane, or None when unavailable."""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", target, "#{pane_pid}"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def pane_identity(target: str) -> dict | None:
    """Return the exact pane, window, and pane-root PID, or None on ambiguity."""
    try:
        result = subprocess.run(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                target,
                "#{pane_id}\t#{window_id}\t#{pane_pid}",
            ],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    rows = [line for line in result.stdout.splitlines() if line]
    if len(rows) != 1:
        return None
    fields = rows[0].split("\t")
    if len(fields) != 3 or fields[0] != target or not fields[1]:
        return None
    try:
        pane_pid = int(fields[2])
    except ValueError:
        return None
    if pane_pid < 1:
        return None
    return {"pane_id": fields[0], "window_id": fields[1], "pane_pid": pane_pid}


def pane_title(target: str) -> str | None:
    """Return a pane's non-empty tmux title, or None when unavailable."""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", target, "#{pane_title}"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def classify_pane_phase(title: str | None) -> PanePhase:
    """Classify a Claude pane title without guessing on unrecognized titles."""
    if not title:
        return PanePhase.UNKNOWN
    marker = title[0]
    if marker == "\u2733":
        return PanePhase.IDLE
    if "\u25d0" <= marker <= "\u25d3" or "\u2800" <= marker <= "\u28ff":
        return PanePhase.PROCESSING
    return PanePhase.UNKNOWN


def read_pane_input(pane_text: str) -> str | None:
    """Read input after the prompt in the final complete Claude input box."""
    lines = pane_text.splitlines()
    rules = [
        index
        for index, line in enumerate(lines)
        if line.strip() and set(line.strip()) == {"\u2500"}
    ]
    if len(rules) < 2:
        return None

    top, bottom = rules[-2:]
    if bottom <= top + 1:
        return None
    for line in reversed(lines[top + 1 : bottom]):
        prompt_line = line.lstrip()
        if prompt_line.startswith("\u276f"):
            return prompt_line[1:].lstrip("\u00a0").strip()
    return None


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_QUESTION_OPTION_RE = re.compile(
    r"^\s*(?P<cursor>\u276f)?\s*(?P<number>\d+)\.(?:\s|$)",
)
_QUESTION_CHROME_RE = re.compile(
    r"(?:\u2191/\u2193 to navigate|Esc to cancel|Enter to select|Type something)",
    re.IGNORECASE,
)


def _parse_pane_answer(
    snapshot: str,
) -> tuple[list[str], list[tuple[int, int, str, bool]], int, int] | None:
    """Parse the current contiguous rows from a numbered selector."""
    if not snapshot:
        return None
    text = _ANSI_ESCAPE_RE.sub("", snapshot)
    lines = text.splitlines()
    chrome_lines = [index for index, line in enumerate(lines) if _QUESTION_CHROME_RE.search(line)]
    if not chrome_lines:
        return None

    rows: list[tuple[int, int, str, bool]] = []
    chrome_line = chrome_lines[-1]
    for index, line in enumerate(lines[:chrome_line]):
        match = _QUESTION_OPTION_RE.match(line)
        if match is None:
            continue
        number = int(match.group("number"))
        label = line[match.end() :].strip()
        rows.append((index, number, label, match.group("cursor") is not None))

    selected_positions = [index for index, row in enumerate(rows) if row[3]]
    if not selected_positions:
        return None
    selected_position = selected_positions[-1]

    first = selected_position
    while first > 0 and rows[first - 1][1] == rows[first][1] - 1:
        first -= 1
    last = selected_position
    while last + 1 < len(rows) and rows[last + 1][1] == rows[last][1] + 1:
        last += 1
    rows = rows[first : last + 1]
    return lines, rows, selected_position - first, chrome_line


def pane_answer_choices(snapshot: str) -> tuple[int, tuple[int, ...], frozenset[int]] | None:
    """Return the selected row, visible choices, and free-text rows for a selector."""
    parsed = _parse_pane_answer(snapshot)
    if parsed is None:
        return None
    lines, rows, selected_position, _chrome_line = parsed
    selected = rows[selected_position][1]
    choices = tuple(number for _index, number, _label, _cursor in rows)

    free_text: set[int] = set()
    for index, number, label, _cursor in rows:
        if "type something" in label.lower():
            free_text.add(number)
            continue
        following = next((line.strip() for line in lines[index + 1 :] if line.strip()), "")
        if following and set(following) == {"\u2500"}:
            free_text.add(number)

    return selected, choices, frozenset(free_text)


def _normalize_pane_text(parts: list[str]) -> str:
    """Collapse rendered whitespace in selector identity text."""
    return " ".join(" ".join(parts).split())


def pane_answer_identity(snapshot: str) -> tuple[str, tuple[tuple[int, str], ...]] | None:
    """Return stable rendered content identifying a numbered selector."""
    parsed = _parse_pane_answer(snapshot)
    if parsed is None:
        return None
    lines, rows, _selected_position, chrome_line = parsed

    question_parts: list[str] = []
    for line in reversed(lines[: rows[0][0]]):
        stripped = line.strip()
        if (
            not stripped
            or set(stripped) == {"\u2500"}
            or _QUESTION_OPTION_RE.match(line) is not None
        ):
            break
        question_parts.append(stripped)
    question = _normalize_pane_text(list(reversed(question_parts)))

    options: list[tuple[int, str]] = []
    for position, (index, number, label, _cursor) in enumerate(rows):
        next_index = rows[position + 1][0] if position + 1 < len(rows) else chrome_line
        row_indent = len(lines[index]) - len(lines[index].lstrip())
        label_parts = [label]
        for line in lines[index + 1 : next_index]:
            stripped = line.strip()
            if not stripped or set(stripped) == {"\u2500"}:
                break
            continuation_indent = len(line) - len(line.lstrip())
            if continuation_indent <= row_indent:
                break
            label_parts.append(stripped)
        options.append((number, _normalize_pane_text(label_parts)))
    return question, tuple(options)


def pane_surface_readable(snapshot: str) -> bool:
    """Return whether a non-empty capture contains a readable Claude input surface."""
    return bool(snapshot.strip()) and (
        read_pane_input(snapshot) is not None or pane_answer_choices(snapshot) is not None
    )


def pane_needs_answer(snapshot: str) -> bool:
    """Return whether a Claude pane is visibly waiting on a numbered answer.

    A numbered selector is a different input surface from the ordinary composer:
    keystrokes drive a highlight, not a text buffer. Any caller about to paste
    free text must consult this first -- pasting into a selector leaves the text
    staged with nothing to submit it, and every retry appends to whatever is
    already sitting there.

    Escapes are stripped first because `capture_pane` keeps them by default
    (`-e`) and Claude colorizes the selector: raw, the cursor and its number are
    separated by a colour sequence, so an anchored match cannot succeed. Both
    call sites matter -- one passes a plain capture and one does not.
    """
    return pane_answer_choices(snapshot) is not None


def new_window(
    command: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    background: bool = False,
    spawn_receipt: dict | None = None,
) -> tuple[WindowSpawnOutcome, str | None]:
    """Create a tmux window without conflating refusal with lost identity.

    Args:
        command: The command to run in the new window.
        cwd: Working directory for the new window.
        env: Environment variables to set in the new window.
        background: If True, don't switch to the new window.
        spawn_receipt: Durable completion action facts to publish before command start.

    Returns:
        A tri-state creation result and the exact pane ID when known.
    """
    if spawn_receipt is not None:
        fields = (
            spawn_receipt["path"],
            spawn_receipt["action_id"],
            spawn_receipt["source_lode_id"],
            spawn_receipt["target_lode_id"],
            spawn_receipt["target_generation"],
        )
        code = (
            "from hopper.tmux import bootstrap_spawn_receipt; "
            f"bootstrap_spawn_receipt({', '.join(repr(value) for value in fields)})"
        )
        wait_channel = f"hopper-spawn-{spawn_receipt['action_id']}"
        command = (
            f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}; "
            "receipt_status=$?; "
            f"tmux wait-for -U {shlex.quote(wait_channel)}; "
            '[ "$receipt_status" -eq 0 ] || exit "$receipt_status"; '
            f"exec {command}"
        )

    cmd = ["tmux", "new-window", "-P", "-F", "#{pane_id}"]
    if background:
        cmd.append("-d")
    if cwd:
        cmd.extend(["-c", cwd])
    if env:
        for key, value in env.items():
            cmd.extend(["-e", f"{key}={value}"])
    cmd.append(command)

    channel_locked = False
    outcome = (WindowSpawnOutcome.UNKNOWN, None)
    try:
        can_launch = True
        if spawn_receipt is not None:
            acquired = subprocess.run(
                ["tmux", "wait-for", "-L", wait_channel], capture_output=True, text=True
            )
            if acquired.returncode != 0:
                logger.error(f"tmux spawn receipt lock failed: {acquired.stderr.strip()}")
                can_launch = False
            else:
                channel_locked = True
        if can_launch:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                logger.error(f"tmux new-window failed: {result.stderr.strip()}")
                outcome = (WindowSpawnOutcome.PROVEN_NO_PANE, None)
            else:
                pane_id = result.stdout.strip()
                if re.fullmatch(r"%[0-9]+", pane_id) is None:
                    logger.error("tmux new-window returned an invalid pane ID: %r", pane_id)
                elif spawn_receipt is None:
                    outcome = (WindowSpawnOutcome.SPAWNED, pane_id)
                else:
                    waited = subprocess.run(
                        ["tmux", "wait-for", "-L", wait_channel],
                        capture_output=True,
                        text=True,
                    )
                    if waited.returncode != 0:
                        logger.error(f"tmux spawn receipt wait failed: {waited.stderr.strip()}")
                        # The lock is only a notification. The bootstrap tags its own
                        # pane and fsyncs the receipt *before* releasing it, so a failed
                        # wait says nothing about whether the pane was claimed. Ask tmux
                        # which pane carries this action's tag instead of discarding a
                        # pane id already in hand. Anything short of exactly our pane —
                        # including tmux being unreadable — stays UNKNOWN.
                        tagged = completion_action_panes(spawn_receipt["action_id"])
                        if tagged == [pane_id]:
                            logger.info(
                                "tmux spawn receipt confirmed by pane tag after failed wait: %s",
                                pane_id,
                            )
                            outcome = (WindowSpawnOutcome.SPAWNED, pane_id)
                    else:
                        outcome = (WindowSpawnOutcome.SPAWNED, pane_id)
    except FileNotFoundError:
        logger.error("tmux command not found")
        if spawn_receipt is None:
            outcome = (WindowSpawnOutcome.PROVEN_NO_PANE, None)
    except OSError as error:
        logger.error("tmux window creation is unverified: %s", error)
    finally:
        if channel_locked:
            try:
                released = subprocess.run(
                    ["tmux", "wait-for", "-U", wait_channel],
                    capture_output=True,
                    text=True,
                )
                if released.returncode != 0 and outcome[0] is WindowSpawnOutcome.SPAWNED:
                    logger.error("tmux spawn receipt unlock failed: %s", released.stderr.strip())
                    outcome = (WindowSpawnOutcome.UNKNOWN, None)
            except OSError as error:
                if outcome[0] is WindowSpawnOutcome.SPAWNED:
                    logger.error("tmux spawn receipt unlock failed: %s", error)
                    outcome = (WindowSpawnOutcome.UNKNOWN, None)
    return outcome


def bootstrap_spawn_receipt(
    path: str,
    action_id: str,
    source_lode_id: str,
    target_lode_id: str,
    target_generation: str,
) -> None:
    """Tag the pane and fsync its action receipt before supervisor launch."""
    pane_id = os.environ.get("TMUX_PANE")
    if not pane_id:
        raise RuntimeError("tmux did not provide an exact pane identity")
    result = subprocess.run(
        ["tmux", "set-option", "-p", "-t", pane_id, COMPLETION_ACTION_OPTION, action_id],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "tmux pane action tag could not be set")
    from hopper import actions

    expected_path = actions.spawn_receipt_path(source_lode_id, action_id)
    if str(expected_path) != path:
        raise ValueError("spawn receipt path does not match its action identity")
    actions.write_spawn_receipt(
        {
            "schema_version": actions.SPAWN_RECEIPT_SCHEMA_VERSION,
            "action_id": action_id,
            "source_lode_id": source_lode_id,
            "target_lode_id": target_lode_id,
            "target_generation": target_generation,
            "pane_id": pane_id,
        }
    )


def completion_action_panes(action_id: str) -> list[str] | None:
    """Return panes tagged for an action, or None when tmux is unknowable."""
    try:
        result = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                f"#{{pane_id}}\t#{{{COMPLETION_ACTION_OPTION}}}",
            ],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    panes = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 2:
            return None
        if fields[1] == action_id:
            panes.append(fields[0])
    return panes


def rename_window(target: str, name: str) -> bool:
    """Rename the tmux window containing the given pane.

    This disables automatic-rename for the window, so the name persists
    even when subprocesses change their process title.

    Args:
        target: The tmux target (pane ID like "%1" or window ID like "@1").
        name: The new window name.
    """
    try:
        result = subprocess.run(
            ["tmux", "rename-window", "-t", target, name],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def select_window(target: str) -> bool:
    """Switch to the tmux window containing the given pane.

    Args:
        target: The tmux target (pane ID like "%1" or window ID like "@1").
    """
    try:
        result = subprocess.run(
            ["tmux", "select-window", "-t", target],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def get_current_tmux_location() -> dict | None:
    """Get the current tmux session name and pane ID.

    Returns:
        Dict with 'session' and 'pane' keys, or None if not in tmux or on error.
    """
    if not is_inside_tmux():
        return None

    pane_id = os.environ.get("TMUX_PANE")
    if not pane_id:
        return None

    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", pane_id, "-p", "#{session_name}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None

        session_name = result.stdout.strip()
        if not session_name:
            return None

        return {"session": session_name, "pane": pane_id}
    except FileNotFoundError:
        return None


def get_current_pane_id() -> str | None:
    """Get the pane ID of the current process from the TMUX_PANE environment variable.

    This is the reliable way for a process to identify which tmux pane it is
    running in, regardless of which window is currently focused.

    Returns:
        The pane ID (e.g., "%1"), or None if not in tmux.
    """
    return os.environ.get("TMUX_PANE") or None


def send_keys(target: str, keys: str, *, literal: bool = False) -> bool:
    """Send keys to a tmux pane.

    Args:
        target: The tmux target (pane ID like "%1" or window ID like "@1").
        keys: The keys to send (e.g., "C-d" for Ctrl-D).
        literal: If True, send `keys` as literal characters (`tmux send-keys -l`)
            rather than as a named key. Required for a single-character payload
            so `-` is not parsed as a flag and `C` is the letter C.

    Returns:
        True if the command succeeded, False otherwise.
    """
    cmd = ["tmux", "send-keys"]
    if literal:
        cmd.append("-l")
    cmd.extend(["-t", target])
    if literal:
        cmd.append("--")
    cmd.append(keys)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def capture_pane(
    target: str,
    plain: bool = False,
    *,
    timeout: float | None = None,
) -> str | None:
    """Capture the contents of a tmux pane.

    Args:
        target: The tmux target (pane ID like "%1" or window ID like "@1").
        plain: If True, omit ANSI escape sequences.

    Returns:
        The pane contents, or None on failure.
    """
    cmd = ["tmux", "capture-pane"]
    if not plain:
        cmd.append("-e")
    cmd.extend(["-p", "-t", target])
    try:
        kwargs: dict[str, object] = {"capture_output": True, "text": True}
        if timeout is not None:
            kwargs["timeout"] = timeout
        result = subprocess.run(cmd, **kwargs)
        if result.returncode != 0:
            return None
        return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None


def paste_buffer(target: str, text: str) -> bool:
    """Paste text into a tmux pane via a tmux buffer.

    Args:
        target: The tmux target (pane ID like "%1" or window ID like "@1").
        text: Text to paste.

    Returns:
        True if both buffer creation and paste succeeded, False otherwise.
    """
    try:
        set_result = subprocess.run(
            ["tmux", "set-buffer", text],
            capture_output=True,
            text=True,
        )
        if set_result.returncode != 0:
            return False
        paste_result = subprocess.run(
            ["tmux", "paste-buffer", "-t", target],
            capture_output=True,
            text=True,
        )
        return paste_result.returncode == 0
    except FileNotFoundError:
        return False


def kill_pane(target: str) -> bool:
    """Kill a tmux pane.

    Args:
        target: The tmux target (pane ID like "%1").

    Returns:
        True if the command succeeded, False otherwise.
    """
    try:
        result = subprocess.run(
            ["tmux", "kill-pane", "-t", target],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False
