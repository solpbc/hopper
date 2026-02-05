# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""tmux interaction utilities."""

import logging
import os
import subprocess

logger = logging.getLogger(__name__)


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


def new_window(
    command: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    background: bool = False,
) -> str | None:
    """Create a new tmux window and return its pane ID.

    Args:
        command: The command to run in the new window.
        cwd: Working directory for the new window.
        env: Environment variables to set in the new window.
        background: If True, don't switch to the new window.

    Returns:
        The tmux pane ID (e.g., "%1") on success, None on failure.
    """
    cmd = ["tmux", "new-window", "-P", "-F", "#{pane_id}"]
    if background:
        cmd.append("-d")
    if cwd:
        cmd.extend(["-c", cwd])
    if env:
        for key, value in env.items():
            cmd.extend(["-e", f"{key}={value}"])
    cmd.append(command)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"tmux new-window failed: {result.stderr.strip()}")
            return None
        return result.stdout.strip()
    except FileNotFoundError:
        logger.error("tmux command not found")
        return None


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


def send_keys(target: str, keys: str) -> bool:
    """Send keys to a tmux pane.

    Args:
        target: The tmux target (pane ID like "%1" or window ID like "@1").
        keys: The keys to send (e.g., "C-d" for Ctrl-D).

    Returns:
        True if the command succeeded, False otherwise.
    """
    try:
        result = subprocess.run(
            ["tmux", "send-keys", "-t", target, keys],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def capture_pane(target: str) -> str | None:
    """Capture the contents of a tmux pane with ANSI escape sequences.

    Args:
        target: The tmux target (pane ID like "%1" or window ID like "@1").

    Returns:
        The pane contents with ANSI styling, or None on failure.
    """
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-e", "-p", "-t", target],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except FileNotFoundError:
        return None
