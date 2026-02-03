"""Claude Code wrapper for hopper."""

from hopper.tmux import new_window, select_window


def spawn_claude(session_id: str, project_path: str | None = None) -> str | None:
    """Spawn Claude via hopper ore in a new tmux window.

    Args:
        session_id: The hopper session ID.
        project_path: Working directory for the Claude session.

    Returns:
        The tmux window ID on success, None on failure.
    """
    # Use hop ore to manage session lifecycle
    # On failure, pause so user can see the error before window closes
    command = f"hop ore {session_id} || {{ echo 'Failed. Press Enter to close.'; read; }}"
    return new_window(command, cwd=project_path)


def switch_to_window(window_id: str) -> bool:
    """Switch to an existing tmux window.

    Args:
        window_id: The tmux window ID to switch to.

    Returns:
        True if successfully switched, False otherwise (window doesn't exist or other error).
    """
    return select_window(window_id)
