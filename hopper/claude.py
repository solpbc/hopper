"""Claude Code wrapper for hopper."""

from hopper.tmux import new_window, select_window


def spawn_claude(session_id: str) -> str | None:
    """Spawn Claude via hopper ore in a new tmux window.

    Args:
        session_id: The hopper session ID.

    Returns:
        The tmux window ID on success, None on failure.
    """
    # Use hop ore to manage session lifecycle
    command = f"hop ore {session_id}"
    return new_window(command)


def switch_to_window(window_id: str) -> bool:
    """Switch to an existing tmux window.

    Args:
        window_id: The tmux window ID to switch to.

    Returns:
        True if successfully switched, False otherwise (window doesn't exist or other error).
    """
    return select_window(window_id)
