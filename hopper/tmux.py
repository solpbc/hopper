"""tmux interaction utilities."""

import os
import subprocess


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
    command: str, cwd: str | None = None, env: dict[str, str] | None = None
) -> str | None:
    """Create a new tmux window and return its unique window ID.

    Args:
        command: The command to run in the new window.
        cwd: Working directory for the new window.
        env: Environment variables to set in the new window.

    Returns:
        The tmux window ID (e.g., "@1") on success, None on failure.
    """
    cmd = ["tmux", "new-window", "-P", "-F", "#{window_id}"]
    if cwd:
        cmd.extend(["-c", cwd])
    if env:
        for key, value in env.items():
            cmd.extend(["-e", f"{key}={value}"])
    cmd.append(command)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except FileNotFoundError:
        return None


def select_window(window_id: str) -> bool:
    """Switch to a tmux window by its unique ID."""
    try:
        result = subprocess.run(
            ["tmux", "select-window", "-t", window_id],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def get_current_tmux_location() -> dict | None:
    """Get the current tmux session name and window ID.

    Returns:
        Dict with 'session' and 'window' keys, or None if not in tmux or on error.
    """
    if not is_inside_tmux():
        return None

    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#{session_name}\n#{window_id}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None

        lines = result.stdout.strip().split("\n")
        if len(lines) != 2:
            return None

        return {"session": lines[0], "window": lines[1]}
    except FileNotFoundError:
        return None
